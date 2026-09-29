"""Kilo Gateway 渠道测试：SSE 映射、免费模型过滤、provider 协议、端点白名单与种子。

覆盖硬门槛按 AGENTS.md：新增分支必须测到（不靠 pragma 达标）。
Kilo 与 Zen 同为无凭证免费层，但无门禁伪装、免费模型由 `isFree` 权威标记识别、
不做探活，故这里独立于 test_zen.py。
"""

from __future__ import annotations

import json

import httpx
import pytest

from src.engine.sse import SSEFrame
from src.provider.base import ErrKind, EventKind
from src.provider.kilo import events as kilo_events
from src.provider.kilo.client import (
    EP_CHAT,
    EP_MODELS,
    KILO_HOST,
    KiloClient,
    KiloProvider,
    Model,
    UpstreamHTTPError,
    _model_from_item,
    prepare_body,
    request_headers,
)


def frame(data: object) -> SSEFrame:
    return SSEFrame(event="", data=data if isinstance(data, str) else json.dumps(data))


def chunk(delta: dict, *, finish: str | None = None, usage: dict | None = None,
          choices: list | None = None) -> dict:
    payload: dict = {"id": "x", "object": "chat.completion.chunk"}
    if choices is None:
        payload["choices"] = [{"index": 0, "delta": delta,
                               "finish_reason": finish}]
    else:
        payload["choices"] = choices
    if usage is not None:
        payload["usage"] = usage
    return payload


# --------------------------------------------------------------- 请求体

def test_prepare_body_forces_stream_and_deepcopies_messages():
    messages = [{"role": "user", "content": "hi"}]
    body = prepare_body({"messages": messages, "stream": False, "temperature": 0.5},
                        "stealth/space-bunny-alpha")
    assert body["stream"] is True
    assert body["model"] == "stealth/space-bunny-alpha"
    assert body["temperature"] == 0.5
    # 深拷贝：改写上游 body 不影响引擎持有的原始请求体
    body["messages"][0]["content"] = "changed"
    assert messages[0]["content"] == "hi"


def test_request_headers_are_anonymous():
    headers = request_headers()
    assert headers["Accept"] == "text/event-stream"
    assert headers["Content-Type"] == "application/json"
    # 不带 Authorization：Kilo 把任何 Authorization 头当真实凭证校验，
    # 带占位符反而 401（与 Zen 相反）
    assert "Authorization" not in headers


# ------------------------------------------------------------ SSE 事件映射

def test_parse_frame_returns_none_for_done_and_empty():
    assert kilo_events.parse_frame(SSEFrame(event="", data="")) is None
    assert kilo_events.parse_frame(SSEFrame(event="", data=" [DONE] ")) is None


def test_parse_frame_content_and_reasoning():
    content = kilo_events.parse_frame(frame(chunk({"content": "hi"})))
    assert content is not None and content.kind is EventKind.CONTENT
    # Kilo 用 delta.reasoning（不是 zen 的 reasoning_content）
    reasoning = kilo_events.parse_frame(frame(chunk({"reasoning": "think"})))
    assert reasoning is not None and reasoning.kind is EventKind.REASONING
    assert reasoning.content == "think"


def test_parse_frame_ignores_reasoning_content_field():
    """zen 的 reasoning_content 在 Kilo 不是思考通道：不产出 REASONING。"""
    assert kilo_events.parse_frame(frame(chunk({"reasoning_content": "x"}))) is None


def test_parse_frame_tool_calls_drops_blank_noise():
    payload = chunk({"tool_calls": [
        {"index": 0, "function": {"name": "", "arguments": ""}},    # 噪声
        {"index": 1, "function": {"name": "", "arguments": "{}"}},   # "{}" 空壳也算噪声
        {"index": 2, "function": {"arguments": {}}},                 # 空对象也算噪声
        {"index": 3, "function": {"name": "grep", "arguments": ""}},
    ]})
    event = kilo_events.parse_frame(frame(payload))
    assert event is not None and event.kind is EventKind.TOOL_CALLS
    assert [c["index"] for c in event.tool_calls] == [3]


def test_parse_frame_tool_calls_all_blank_falls_through():
    """全部是空名噪声：整批丢弃，不产出 tool_calls 事件。"""
    payload = chunk({"tool_calls": [
        {"index": 0, "function": {"name": "  ", "arguments": ""}},
        {"index": 1, "function": {"name": "", "arguments": "{}"}},
    ], "content": "text"})
    event = kilo_events.parse_frame(frame(payload))
    assert event is not None and event.kind is EventKind.CONTENT


def test_parse_frame_tool_calls_tolerates_non_dict_entries():
    """非 dict 的 tool_call 条目被跳过，不崩。"""
    payload = chunk({"tool_calls": [
        "junk",
        {"index": 1, "function": {"name": "grep", "arguments": "x"}},
    ]})
    event = kilo_events.parse_frame(frame(payload))
    assert event is not None and event.kind is EventKind.TOOL_CALLS
    assert [c["index"] for c in event.tool_calls] == [1]


def test_parse_frame_usage_and_finish():
    usage_event = kilo_events.parse_frame(frame(chunk(
        {}, choices=[], usage={"prompt_tokens": 3})))
    assert usage_event is not None and usage_event.kind is EventKind.USAGE
    finish = kilo_events.parse_frame(frame(chunk({}, finish="stop")))
    assert finish is not None and finish.kind is EventKind.FINISH


def test_parse_frame_no_choices_returns_none():
    assert kilo_events.parse_frame(frame({"choices": []})) is None
    assert kilo_events.parse_frame(frame({"usage": None})) is None
    # choices 缺失 → None
    assert kilo_events.parse_frame(frame({"usage": None, "id": "x"})) is None


def test_parse_frame_bad_payload_raises():
    with pytest.raises(kilo_events.UpstreamProtocolViolation):
        kilo_events.parse_frame(SSEFrame(event="", data="{not json"))
    with pytest.raises(kilo_events.UpstreamProtocolViolation):
        kilo_events.parse_frame(SSEFrame(event="", data="[1, 2]"))
    with pytest.raises(kilo_events.UpstreamProtocolViolation):
        kilo_events.parse_frame(frame({"choices": "nope"}))
    with pytest.raises(kilo_events.UpstreamProtocolViolation):
        kilo_events.parse_frame(frame({"choices": ["nope"]}))


def test_parse_frame_error_envelope_int_code():
    event = kilo_events.parse_frame(frame({"error": {"code": 429, "message": "slow"}}))
    assert event is not None and event.kind is EventKind.ERROR
    assert event.error_code == 429 and event.error_message == "slow"
    assert event.error_kind is ErrKind.SOFT


def test_parse_frame_error_envelope_string_code_and_missing_message():
    event = kilo_events.parse_frame(frame({"error": {"code": "bad", "message": None}}))
    assert event is not None and event.error_kind is ErrKind.OTHER
    assert event.error_code is None and event.error_message == ""


def test_parse_frame_error_string_envelope_is_ignored():
    """Kilo 的 HTTP 404 用 `{"error": "..."}`（字符串），流内不产 ERROR 事件。"""
    assert kilo_events.parse_frame(frame({"error": "not found"})) is None


def test_parse_all_events_splits_content_and_finish():
    """最后一段正文与 finish_reason 同帧：两者都要产出。"""
    payload = chunk({"content": "!"}, finish="stop")
    kinds = [e.kind for e in kilo_events.parse_all_events(frame(payload))]
    assert kinds == [EventKind.CONTENT, EventKind.FINISH]


def test_parse_all_events_appends_usage_to_finish_chunk():
    payload = chunk({"content": "!"}, finish="stop",
                    usage={"prompt_tokens": 1, "completion_tokens": 2})
    events = kilo_events.parse_all_events(frame(payload))
    assert [e.kind for e in events] == [
        EventKind.CONTENT, EventKind.USAGE, EventKind.FINISH]


def test_parse_all_events_usage_only_chunk():
    events = kilo_events.parse_all_events(frame(chunk(
        {}, choices=[],
        usage={"prompt_tokens": 10, "completion_tokens": 5,
               "prompt_tokens_details": {"cached_tokens": 2}})))
    assert [e.kind for e in events] == [EventKind.USAGE]
    assert events[0].usage is not None
    assert events[0].usage.input_tokens == 10
    assert events[0].usage.cached_tokens == 2


def test_parse_all_events_empty_and_done():
    assert kilo_events.parse_all_events(SSEFrame(event="", data="")) == []
    assert kilo_events.parse_all_events(SSEFrame(event="", data="[DONE]")) == []


def test_parse_all_events_usage_without_finish():
    """usage 有、finish_reason 缺：只补 usage。"""
    events = kilo_events.parse_all_events(frame(chunk(
        {}, choices=[{"index": 0, "delta": {}, "finish_reason": None}],
        usage={"prompt_tokens": 1})))
    assert [e.kind for e in events] == [EventKind.USAGE]


def test_usage_tolerates_bad_types():
    """usage 字段类型不对（bool / 字符串 / 非 dict details）不能崩。"""
    payload = {"choices": [], "usage": {
        "prompt_tokens": True, "completion_tokens": "x",
        "reasoning_tokens": True, "cached_tokens": True,
        "prompt_tokens_details": "nope"}}
    event = kilo_events.parse_frame(frame(payload))
    assert event is not None and event.usage is not None
    assert event.usage.input_tokens is None
    assert event.usage.output_tokens is None
    assert event.usage.reasoning_tokens is None
    assert event.usage.cached_tokens is None


def test_usage_top_level_cached_tokens_fallback():
    """details 缺失或非 dict 时回落顶层 cached_tokens。"""
    payload = {"choices": [], "usage": {"prompt_tokens": 4, "cached_tokens": 3}}
    event = kilo_events.parse_frame(frame(payload))
    assert event is not None and event.usage is not None
    assert event.usage.cached_tokens == 3


def test_usage_details_without_cached_tokens_falls_back():
    """details 是 dict 但无 cached_tokens → 回落顶层。"""
    payload = {"choices": [], "usage": {
        "prompt_tokens_details": {}, "cached_tokens": 7}}
    event = kilo_events.parse_frame(frame(payload))
    assert event is not None and event.usage is not None
    assert event.usage.cached_tokens == 7


def test_classify_status_maps_each_branch():
    # 401 只代表「该模型需要付费 key」，不是虚拟凭证失效；归 DEAD 会因一次
    # 强制 @kilo 的付费模型请求把整条渠道硬禁用。
    assert kilo_events.classify_status(401) is ErrKind.INVALID
    assert kilo_events.classify_status(429) is ErrKind.SOFT
    assert kilo_events.classify_status(400) is ErrKind.INVALID
    assert kilo_events.classify_status(404) is ErrKind.INVALID
    assert kilo_events.classify_status(422) is ErrKind.INVALID
    assert kilo_events.classify_status(403) is ErrKind.REQUEST
    assert kilo_events.classify_status(500) is ErrKind.OTHER


def test_classify_error_code_maps_each_branch():
    assert kilo_events.classify_error_code(401) is ErrKind.INVALID
    assert kilo_events.classify_error_code(429) is ErrKind.SOFT
    assert kilo_events.classify_error_code(400) is ErrKind.INVALID
    assert kilo_events.classify_error_code(404) is ErrKind.INVALID
    assert kilo_events.classify_error_code(422) is ErrKind.INVALID
    assert kilo_events.classify_error_code(403) is ErrKind.REQUEST
    assert kilo_events.classify_error_code(999) is ErrKind.OTHER
    assert kilo_events.classify_error_code(None) is ErrKind.OTHER


# ------------------------------------------------------------ 上游客户端

def _sse(*payloads: object) -> str:
    body = "".join(f"data: {json.dumps(p)}\n\n" for p in payloads)
    return body + "data: [DONE]\n\n"


def _client(handler, **kw) -> KiloClient:
    transport = httpx.MockTransport(handler)
    # 默认用无路径的测试主机：request.url.path 直接是 /chat/completions、
    # /models，便于按端点常量断言；host 覆盖（含带路径/尾斜杠）行为单独测。
    kw.setdefault("host", "https://kilo.test")
    return KiloClient(
        stream_client=httpx.AsyncClient(transport=transport, timeout=None),
        short_client=httpx.AsyncClient(transport=transport, timeout=None), **kw)


async def test_stream_chat_yields_named_events():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == EP_CHAT
        assert "Authorization" not in request.headers
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["model"] == "m"
        return httpx.Response(200, text=_sse(
            chunk({"role": "assistant", "reasoning": "r"}),
            chunk({"content": "hi"}),
            chunk({"content": "!"}, finish="stop"),
            chunk({}, choices=[], usage={"prompt_tokens": 1, "completion_tokens": 1}),
        ))

    events = [e async for e in _client(handler).stream_chat(
        {"messages": [{"role": "user", "content": "x"}]}, "m")]
    kinds = [e.kind for e in events]
    assert kinds == [EventKind.REASONING, EventKind.CONTENT, EventKind.CONTENT,
                     EventKind.FINISH, EventKind.USAGE]


async def test_stream_chat_raises_classified_http_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b'{"error":"slow down"}')

    with pytest.raises(UpstreamHTTPError) as caught:
        [e async for e in _client(handler).stream_chat({"messages": []}, "m")]
    assert caught.value.kind() is ErrKind.SOFT


def test_upstream_http_error_exposes_status_and_kind():
    error = UpstreamHTTPError(404, b'{"error":"model not found"}')
    assert error.status == 404
    assert error.kind() is ErrKind.INVALID


def test_model_from_item_passes_through_metadata():
    item = {
        "id": "stealth/space-bunny-alpha",
        "name": "Space Bunny Alpha",
        "isFree": True,
        "context_length": 131072,
        "top_provider": {"max_completion_tokens": 8192,
                         "context_length": 65536},
        "architecture": {"input_modalities": ["text", "image"]},
        "supported_parameters": ["tools", "reasoning", "temperature"],
    }
    model = _model_from_item(item, item["id"])
    assert model.id == "stealth/space-bunny-alpha"
    assert model.name == "Space Bunny Alpha"
    assert model.credit_rate == 0.0
    assert model.max_input_tokens == 131072          # 顶层优先
    assert model.max_output_tokens == 8192
    assert model.supports_images is True
    assert model.supports_tool_call is True
    assert model.supports_reasoning is True


def test_model_from_item_falls_back_to_top_provider_context():
    model = _model_from_item(
        {"id": "m", "top_provider": {"context_length": 4096}}, "m")
    assert model.max_input_tokens == 4096
    assert model.name == ""


def test_model_from_item_handles_missing_metadata():
    """元数据缺失/类型不对 → 各字段 None，不崩。"""
    model = _model_from_item(
        {"id": "m", "name": 123, "context_length": True,
         "top_provider": "junk", "architecture": "junk",
         "supported_parameters": "junk"}, "m")
    assert model.name == ""                          # 非字符串 name
    assert model.max_input_tokens is None            # bool 不算 int
    assert model.max_output_tokens is None
    assert model.supports_images is None
    assert model.supports_tool_call is None
    assert model.supports_reasoning is None


def test_model_from_item_modalities_and_params_without_capabilities():
    model = _model_from_item(
        {"id": "m", "architecture": {"input_modalities": ["text"]},
         "supported_parameters": ["temperature"]}, "m")
    assert model.supports_images is False
    assert model.supports_tool_call is False
    assert model.supports_reasoning is False


async def test_fetch_models_keeps_only_isfree_models():
    """只保留 isFree=true；非免费、畸形条目一律不出现在结果里。"""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == EP_MODELS
        return httpx.Response(200, json={"object": "list", "data": [
            {"id": "kilo-auto/free", "name": "Auto", "isFree": True},
            {"id": "gpt-4o", "isFree": False},          # 付费 → 隐藏
            {"id": "no-flag"},                          # 无 isFree → 隐藏
            {"id": "isFree-truthy", "isFree": 1},       # 非布尔 True → 隐藏
            "junk",                                     # 非 dict 跳过
            {"id": ""},                                 # 空 id 跳过
            {"id": 123},                                # 非字符串 id 跳过
            {"id": "openrouter/free", "name": "OR", "isFree": True},
        ]})

    models = await _client(handler).fetch_models()
    assert [m.id for m in models] == ["kilo-auto/free", "openrouter/free"]
    # 免费层显式标 0 倍率（列表 UI 显示 x0，排序时也排最省的一档）
    assert all(m.credit_rate == 0.0 for m in models)


async def test_fetch_models_no_probe_request_sent():
    """不做探活：只打一次 /models，不向 /chat/completions 发任何请求。"""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"data": [{"id": "a/free", "isFree": True}]})

    models = await _client(handler).fetch_models()
    assert [m.id for m in models] == ["a/free"]
    assert paths == [EP_MODELS]


async def test_fetch_models_errors():
    def http_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=b"bad gateway")

    with pytest.raises(UpstreamHTTPError):
        await _client(http_error).fetch_models()

    def non_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    with pytest.raises(kilo_events.UpstreamProtocolViolation):
        await _client(non_json).fetch_models()

    # JSON 合法但不是对象（无 data 可取）
    def json_scalar(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2])

    with pytest.raises(kilo_events.UpstreamProtocolViolation):
        await _client(json_scalar).fetch_models()

    def no_list(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": "list"})

    with pytest.raises(kilo_events.UpstreamProtocolViolation):
        await _client(no_list).fetch_models()

    # 有 data 但没有任何 isFree 模型
    def no_free(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": ["junk", {"id": "gpt-4o"}]})

    with pytest.raises(kilo_events.UpstreamProtocolViolation, match="no free models"):
        await _client(no_free).fetch_models()


async def test_client_lazy_clients_and_aclose():
    """未注入 client 时惰性创建；aclose 释放两侧连接池。"""
    client = KiloClient()
    assert client._stream() is client._stream()      # noqa: SLF001 - 惰性复用同一实例
    assert client._short() is client._short()        # noqa: SLF001
    await client.aclose()

    # 都没创建过：aclose 不报错（两个分支都为 None）
    await KiloClient().aclose()


async def test_client_host_trailing_slash_stripped():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/gateway" + EP_CHAT
        return httpx.Response(200, text=_sse(chunk({"content": "x"}, finish="stop")))

    client = _client(handler, host="https://kilo.test/api/gateway/")
    assert client.host == "https://kilo.test/api/gateway"
    _ = [e async for e in client.stream_chat({"messages": []}, "m")]
    await client.aclose()


# --------------------------------------------------------------- provider

async def test_provider_probe_quota_is_unknown_not_exhausted():
    quota = await KiloProvider().probe_quota({})
    assert quota.probe_failed is True
    assert quota.total is None and quota.remaining is None
    assert quota.probed_at is not None


def test_provider_import_credential_is_empty():
    assert KiloProvider().import_credential({"anything": 1}) == {}


def test_provider_classify_delegates():
    assert KiloProvider().classify(429, b"") is ErrKind.SOFT


def test_provider_default_client_and_id():
    provider = KiloProvider()
    assert provider.id == "kilo"
    assert provider.client.host == KILO_HOST


async def test_provider_list_models_delegates():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == EP_MODELS
        return httpx.Response(200, json={"data": [{"id": "m/free", "isFree": True}]})

    provider = KiloProvider(client=_client(handler))
    models = await provider.list_models({})
    assert [m.id for m in models] == ["m/free"]
    await provider.aclose()


async def test_provider_stream_chat_with_and_without_pacer():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse(chunk({"content": "hi"}, finish="stop")))

    class Pacer:
        def __init__(self) -> None:
            self.turns = 0
            self.released = 0

        async def wait_turn(self, key=None) -> None:
            self.turns += 1

        def release(self, key=None) -> None:
            self.released += 1

    pacer = Pacer()
    provider = KiloProvider(client=_client(handler), pacer=pacer)
    events = [e async for e in provider.stream_chat({}, {"messages": []}, "m")]
    assert any(e.kind is EventKind.CONTENT for e in events)
    assert pacer.turns == 1 and pacer.released == 1     # 并发模式必须配对归还
    await provider.aclose()

    plain = KiloProvider(client=_client(handler))
    _ = [e async for e in plain.stream_chat({}, {"messages": []}, "m")]
    await plain.aclose()


async def test_provider_stream_chat_releases_pacer_on_error():
    """上游抛错也要归还名额，否则该桶被当成永远在途而失去节流。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    class Pacer:
        def __init__(self) -> None:
            self.released = 0

        async def wait_turn(self, key=None) -> None:
            pass

        def release(self, key=None) -> None:
            self.released += 1

    pacer = Pacer()
    provider = KiloProvider(client=_client(handler), pacer=pacer)
    with pytest.raises(UpstreamHTTPError):
        [e async for e in provider.stream_chat({}, {"messages": []}, "m")]
    assert pacer.released == 1
    await provider.aclose()


# ------------------------------------------------------- 端点白名单与种子

def test_kilo_endpoint_rejects_unallowlisted_host():
    from src.config import Settings
    from src.main import _kilo_endpoint
    from tests.conftest import SECRET

    settings = Settings(_env_file=None, APP_SECRET=SECRET,
                        KILO_API_ENDPOINT="https://evil.example.com")
    with pytest.raises(ValueError, match="KILO_ALLOWED_ENDPOINTS"):
        _kilo_endpoint(settings)


def test_kilo_endpoint_accepts_default():
    from src.config import Settings
    from src.main import _kilo_endpoint
    from tests.conftest import SECRET

    settings = Settings(_env_file=None, APP_SECRET=SECRET)
    assert _kilo_endpoint(settings) == KILO_HOST


def test_kilo_allowed_parses_comma_separated_endpoints():
    from src.config import Settings, validate_kilo_endpoint_allowed
    from tests.conftest import SECRET

    settings = Settings(_env_file=None, APP_SECRET=SECRET,
                        KILO_ALLOWED_ENDPOINTS="https://api.kilo.ai/api/gateway/, https://alt.example.com")
    assert settings.kilo_allowed == ("https://api.kilo.ai/api/gateway",
                                     "https://alt.example.com")
    assert validate_kilo_endpoint_allowed("https://alt.example.com/", settings) is True
    assert validate_kilo_endpoint_allowed("https://nope.example.com", settings) is False


def test_seed_kilo_credential_is_idempotent(tmp_path):
    from src.db.conn import Database
    from src.db.crypto import CredentialCipher
    from src.db.migrate import apply_schema
    from src.db.repo import CredentialRepository
    from src.main import _seed_kilo_credential
    from tests.conftest import SECRET

    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    repo = CredentialRepository(db, CredentialCipher(SECRET))

    _seed_kilo_credential(repo)
    _seed_kilo_credential(repo)                      # 已有 → 不再补
    rows = repo.candidates(["kilo"])
    assert len(rows) == 1
    assert repo.credential_data(rows[0].credential_id) == {}
    db.close()


async def test_build_app_seeds_kilo_and_resolves_forced_route(tmp_path):
    """端到端：默认装配种子 kilo 凭证，`模型@kilo` 能路由到 kilo 渠道。"""
    from src.config import Settings
    from src.engine.model_resolver import resolve
    from src.main import build_app
    from tests.conftest import SECRET

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    assert "kilo" in app.state.executor._deps.providers
    rows = app.state.credentials.candidates(["kilo"])
    assert len(rows) == 1 and rows[0].health is None      # 未知而非耗尽
    target = resolve("some-model@kilo", "glm-5.2")
    assert target.providers == ("kilo",) and target.forced
    app.state.executor._deps.providers["kilo"].client          # noqa: B018 - 存在即可
    for provider in app.state.executor._deps.providers.values():
        closer = getattr(provider, "aclose", None)
        if callable(closer):
            await closer()


def test_kilo_credential_can_be_restored_after_delete(tmp_path, monkeypatch):
    """管理台删除 kilo 虚拟凭证后，可用「添加 Kilo Gateway」从 UI 补回（无需重启）。"""
    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token
    from src.config import Settings
    from src.main import build_app
    from tests.conftest import SECRET

    async def fake_fetch_models(self):
        return [Model(id="offline/free", name="Kilo")]

    # TestClient 进 lifespan 会预热模型列表（真连上游），这里保持离线。
    monkeypatch.setattr(KiloClient, "fetch_models", fake_fetch_models)
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        credentials = app.state.credentials
        seeded = credentials.candidates(["kilo"])
        assert len(seeded) == 1
        assert credentials.delete(seeded[0].credential_id)          # 模拟管理台删除
        assert credentials.candidates(["kilo"]) == []

        client.cookies.set("coding2api_session",
                           create_session_token("root", SECRET))
        created = client.post("/api/credentials",
                              json={"provider": "kilo", "credential": {},
                                    "nickname": "Kilo Gateway"})
        assert created.status_code == 200
        credential_id = created.json()["id"]
        restored = credentials.candidates(["kilo"])
        assert [row.credential_id for row in restored] == [credential_id]
        assert credentials.credential_data(credential_id) == {}
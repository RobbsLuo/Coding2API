"""OpenCode Zen 渠道测试：门禁伪装、SSE 映射、回包过滤、provider 协议。

覆盖硬门槛按 AGENTS.md：新增分支必须测到（不靠 pragma 达标）。
"""

from __future__ import annotations

import asyncio
import json
import re

import httpx
import pytest

from src.engine.sse import SSEFrame
from src.provider.base import ErrKind, EventKind
from src.provider.zen import events as zen_events
from src.provider.zen.client import (
    EP_CHAT,
    EP_MODELS,
    MIN_OPENCODE_VERSION,
    Model,
    UpstreamHTTPError,
    ZenClient,
    ZenProvider,
    _InjectedToolFilter,
    ensure_gate_tools,
    gate_headers,
    new_session_id,
    prepare_body,
)

SESSION_RE = re.compile(r"^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$")


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


# --------------------------------------------------------------- 门禁伪装

def test_new_session_id_matches_gate_regex():
    session = new_session_id()
    assert SESSION_RE.match(session), session
    assert new_session_id() != session


def test_gate_headers_include_version_and_session():
    headers = gate_headers(version="2.3.4")
    assert headers["User-Agent"] == "opencode/2.3.4"
    assert SESSION_RE.match(headers["x-opencode-session"])
    assert headers["Accept"] == "text/event-stream"
    assert headers["Authorization"] == "Bearer public"


def test_gate_headers_reuse_explicit_session():
    headers = gate_headers(session_id="ses_" + "a" * 12 + "b" * 14)
    assert headers["x-opencode-session"] == "ses_" + "a" * 12 + "b" * 14


def test_ensure_gate_tools_injects_both_when_missing():
    body: dict = {"messages": []}
    injected = ensure_gate_tools(body)
    assert injected == frozenset({"bash", "read"})
    names = [tool["function"]["name"] for tool in body["tools"]]
    assert names == ["bash", "read"]


def test_ensure_gate_tools_injects_only_missing():
    body = {"tools": [{"type": "function", "function": {"name": "bash"}}]}
    injected = ensure_gate_tools(body)
    assert injected == frozenset({"read"})
    assert [t["function"]["name"] for t in body["tools"]] == ["bash", "read"]


def test_ensure_gate_tools_keeps_user_tools_verbatim():
    body = {"tools": [
        {"type": "function", "function": {"name": "bash"}},
        {"type": "function", "function": {"name": "read"}},
        {"type": "function", "function": {"name": "grep"}},
    ]}
    injected = ensure_gate_tools(body)
    assert injected == frozenset()
    assert len(body["tools"]) == 3


def test_ensure_gate_tools_tolerates_malformed_entries():
    """非 dict 工具 / function 非 dict：不能崩，按「缺失」补门禁工具。"""
    body = {"tools": [None, "junk", {"type": "function"}, {"function": "x"}]}
    injected = ensure_gate_tools(body)
    assert injected == frozenset({"bash", "read"})


def test_ensure_gate_tools_ignores_non_list_tools():
    body = {"tools": {"not": "a list"}}
    injected = ensure_gate_tools(body)
    assert injected == frozenset({"bash", "read"})
    assert isinstance(body["tools"], list)


def test_prepare_body_forces_stream_and_deepcopies_messages():
    messages = [{"role": "user", "content": "hi"}]
    body, injected = prepare_body({"messages": messages, "stream": False, "tools": []},
                                  "big-pickle")
    assert body["stream"] is True
    assert body["model"] == "big-pickle"
    assert injected == frozenset({"bash", "read"})
    # 深拷贝：改写上游 body 不影响引擎持有的原始请求体
    body["messages"][0]["content"] = "changed"
    assert messages[0]["content"] == "hi"


# --------------------------------------------------------------- 回包过滤

def test_tool_filter_drops_injected_by_name():
    flt = _InjectedToolFilter(frozenset({"bash"}))
    kept = flt.keep([{"index": 0, "function": {"name": "bash"}},
                     {"index": 1, "function": {"name": "grep"}}])
    assert [c["index"] for c in kept] == [1]


def test_tool_filter_drops_index_continuations():
    """分片续传：首片带 name 被丢，后续只有 arguments 的同 index 片一并丢。"""
    flt = _InjectedToolFilter(frozenset({"read"}))
    assert flt.keep([{"index": 3, "function": {"name": "read"}}]) == []
    assert flt.keep([{"index": 3, "function": {"arguments": "{\"p\""}}]) == []
    # 其他 index 不受影响
    assert flt.keep([{"index": 4, "function": {"arguments": "x"}}])


def test_tool_filter_drops_injected_without_index():
    flt = _InjectedToolFilter(frozenset({"bash"}))
    assert flt.keep([{"function": {"name": "bash"}}]) == []


def test_tool_filter_keeps_real_tools_and_non_dict_function():
    flt = _InjectedToolFilter(frozenset({"bash"}))
    kept = flt.keep([{"index": 0, "function": "weird"}, {"index": 1}])
    assert len(kept) == 2


# ------------------------------------------------------------ SSE 事件映射

def test_parse_frame_returns_none_for_done_and_empty():
    assert zen_events.parse_frame(SSEFrame(event="", data="")) is None
    assert zen_events.parse_frame(SSEFrame(event="", data=" [DONE] ")) is None


def test_parse_frame_content_and_reasoning():
    content = zen_events.parse_frame(frame(chunk({"content": "hi"})))
    assert content is not None and content.kind is EventKind.CONTENT
    reasoning = zen_events.parse_frame(frame(chunk({"reasoning_content": "think"})))
    assert reasoning is not None and reasoning.kind is EventKind.REASONING
    assert reasoning.content == "think"


def test_parse_frame_tool_calls_drops_blank_noise():
    payload = chunk({"tool_calls": [
        {"index": 0, "function": {"name": "", "arguments": ""}},   # 噪声
        {"index": 1, "function": {"name": "", "arguments": "{}"}},  # "{}" 空壳参数也算噪声
        {"index": 2, "function": {"name": "grep", "arguments": ""}},
    ]})
    event = zen_events.parse_frame(frame(payload))
    assert event is not None and event.kind is EventKind.TOOL_CALLS
    assert [c["index"] for c in event.tool_calls] == [2]


def test_parse_frame_tool_calls_all_blank_falls_through():
    """全部是空名噪声：整批丢弃，不产出 tool_calls 事件。"""
    payload = chunk({"tool_calls": [
        {"index": 0, "function": {"name": "  ", "arguments": ""}},
        {"index": 1, "function": {"name": "", "arguments": "{}"}},  # "{}" 空壳参数也算噪声
        {"index": 2, "function": {"arguments": {}}},
    ], "content": "text"})
    event = zen_events.parse_frame(frame(payload))
    assert event is not None and event.kind is EventKind.CONTENT


def test_parse_frame_usage_and_finish():
    usage_event = zen_events.parse_frame(frame(chunk(
        {}, choices=[], usage={"prompt_tokens": 3})))
    assert usage_event is not None and usage_event.kind is EventKind.USAGE
    finish = zen_events.parse_frame(frame(chunk({}, finish="stop")))
    assert finish is not None and finish.kind is EventKind.FINISH


def test_parse_frame_no_choices_returns_none():
    assert zen_events.parse_frame(frame({"choices": []})) is None
    assert zen_events.parse_frame(frame({"usage": None})) is None


def test_parse_frame_bad_payload_raises():
    with pytest.raises(zen_events.UpstreamProtocolViolation):
        zen_events.parse_frame(SSEFrame(event="", data="{not json"))
    with pytest.raises(zen_events.UpstreamProtocolViolation):
        zen_events.parse_frame(SSEFrame(event="", data="[1, 2]"))
    with pytest.raises(zen_events.UpstreamProtocolViolation):
        zen_events.parse_frame(frame({"choices": "nope"}))
    with pytest.raises(zen_events.UpstreamProtocolViolation):
        zen_events.parse_frame(frame({"choices": ["nope"]}))


def test_parse_frame_error_envelope_int_code():
    event = zen_events.parse_frame(frame({"error": {"code": 429, "message": "slow"}}))
    assert event is not None and event.kind is EventKind.ERROR
    assert event.error_code == 429 and event.error_message == "slow"
    assert event.error_kind is ErrKind.SOFT


def test_parse_frame_error_envelope_string_code_and_missing_message():
    event = zen_events.parse_frame(frame({"error": {"code": "bad", "message": None}}))
    assert event is not None and event.error_kind is ErrKind.OTHER
    assert event.error_code is None and event.error_message == ""


def test_parse_all_events_splits_content_and_finish():
    """最后一段正文与 finish_reason 同帧：两者都要产出。"""
    payload = chunk({"content": "!"}, finish="stop")
    kinds = [e.kind for e in zen_events.parse_all_events(frame(payload))]
    assert kinds == [EventKind.CONTENT, EventKind.FINISH]


def test_parse_all_events_appends_usage_to_finish_chunk():
    payload = chunk({"content": "!"}, finish="stop",
                    usage={"prompt_tokens": 1, "completion_tokens": 2})
    events = zen_events.parse_all_events(frame(payload))
    assert [e.kind for e in events] == [
        EventKind.CONTENT, EventKind.USAGE, EventKind.FINISH]


def test_parse_all_events_usage_only_chunk():
    events = zen_events.parse_all_events(frame(chunk(
        {}, choices=[],
        usage={"prompt_tokens": 10, "completion_tokens": 5,
               "prompt_tokens_details": {"cached_tokens": 2}})))
    assert [e.kind for e in events] == [EventKind.USAGE]
    assert events[0].usage is not None
    assert events[0].usage.input_tokens == 10
    assert events[0].usage.cached_tokens == 2


def test_parse_all_events_empty_and_done():
    assert zen_events.parse_all_events(SSEFrame(event="", data="")) == []
    assert zen_events.parse_all_events(SSEFrame(event="", data="[DONE]")) == []


def test_usage_tolerates_bad_types():
    """usage 字段类型不对（bool / 字符串 / 非 dict details）不能崩。"""
    payload = {"choices": [], "usage": {
        "prompt_tokens": True, "completion_tokens": "x",
        "cached_tokens": True, "prompt_tokens_details": "nope",
        "credit": True}}
    event = zen_events.parse_frame(frame(payload))
    assert event is not None and event.usage is not None
    assert event.usage.input_tokens is None
    assert event.usage.output_tokens is None
    assert event.usage.cached_tokens is None
    assert event.usage.credit is None


def test_classify_status_maps_each_branch():
    # 401 只代表「该模型需要付费 key」，不是虚拟凭证失效；归 DEAD 会因一次
    # 强制 @zen 的付费模型请求把整条渠道硬禁用。
    assert zen_events.classify_status(401) is ErrKind.INVALID
    assert zen_events.classify_status(429) is ErrKind.SOFT
    assert zen_events.classify_status(400) is ErrKind.INVALID
    assert zen_events.classify_status(404) is ErrKind.INVALID
    assert zen_events.classify_status(422) is ErrKind.INVALID
    assert zen_events.classify_status(403) is ErrKind.REQUEST
    assert zen_events.classify_status(500) is ErrKind.OTHER
    assert zen_events.classify_status(426) is ErrKind.OTHER


def test_classify_error_code_maps_each_branch():
    assert zen_events.classify_error_code(401) is ErrKind.INVALID
    assert zen_events.classify_error_code(429) is ErrKind.SOFT
    assert zen_events.classify_error_code(400) is ErrKind.INVALID
    assert zen_events.classify_error_code(403) is ErrKind.REQUEST
    assert zen_events.classify_error_code(999) is ErrKind.OTHER
    assert zen_events.classify_error_code(None) is ErrKind.OTHER


# ------------------------------------------------------------ 上游客户端

def _sse(*payloads: object) -> str:
    body = "".join(f"data: {json.dumps(p)}\n\n" for p in payloads)
    return body + "data: [DONE]\n\n"


def _client(handler, **kw) -> ZenClient:
    transport = httpx.MockTransport(handler)
    return ZenClient(
        stream_client=httpx.AsyncClient(transport=transport, timeout=None),
        short_client=httpx.AsyncClient(transport=transport, timeout=None), **kw)


async def test_stream_chat_yields_named_events():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == EP_CHAT
        assert request.headers["User-Agent"].startswith("opencode/")
        assert SESSION_RE.match(request.headers["x-opencode-session"])
        body = json.loads(request.content)
        assert body["stream"] is True
        names = [t["function"]["name"] for t in body["tools"]]
        assert names == ["bash", "read"]
        return httpx.Response(200, text=_sse(
            chunk({"role": "assistant", "reasoning_content": "r"}),
            chunk({"content": "hi"}),
            chunk({"content": "!"}, finish="stop"),
            chunk({}, choices=[], usage={"prompt_tokens": 1, "completion_tokens": 1}),
        ))

    events = [e async for e in _client(handler).stream_chat(
        {"messages": [{"role": "user", "content": "x"}]}, "big-pickle")]
    kinds = [e.kind for e in events]
    assert kinds == [EventKind.REASONING, EventKind.CONTENT, EventKind.CONTENT,
                     EventKind.FINISH, EventKind.USAGE]


async def test_stream_chat_drops_injected_tool_calls_and_downgrades_finish():
    """模型调用了我们伪造的 bash → 丢弃该 tool_call，finish 收敛为 stop。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse(
            chunk({"tool_calls": [
                {"index": 0, "id": "c1", "type": "function",
                 "function": {"name": "bash", "arguments": ""}}]},
                finish="tool_calls"),
            chunk({}, choices=[], usage={"prompt_tokens": 1}),
        ))

    events = [e async for e in _client(handler).stream_chat({"messages": []}, "m")]
    assert all(e.kind is not EventKind.TOOL_CALLS for e in events)
    finishes = [e for e in events if e.kind is EventKind.FINISH]
    assert finishes and finishes[0].finish_reason == "stop"


async def test_stream_chat_keeps_real_tool_calls():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse(
            chunk({"tool_calls": [
                {"index": 0, "id": "c1", "type": "function",
                 "function": {"name": "get_weather", "arguments": ""}}]},
                finish="tool_calls"),
        ))

    events = [e async for e in _client(handler).stream_chat({"messages": []}, "m")]
    tool_events = [e for e in events if e.kind is EventKind.TOOL_CALLS]
    assert tool_events and tool_events[0].tool_calls[0]["function"]["name"] == "get_weather"
    finishes = [e for e in events if e.kind is EventKind.FINISH]
    assert finishes[0].finish_reason == "tool_calls"


async def test_stream_chat_raises_classified_http_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b'{"error":"forbidden"}')

    with pytest.raises(UpstreamHTTPError) as caught:
        [e async for e in _client(handler).stream_chat({"messages": []}, "m")]
    assert caught.value.kind() is ErrKind.REQUEST


async def test_fetch_models_keeps_only_live_free_candidates():
    """后缀收窄 + 探活：非后缀、探活非 2xx、畸形条目一律不出现在结果里。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_MODELS:
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": "big-pickle", "owned_by": "opencode"},  # 无 -free 后缀 → 隐藏
                {"id": "alpha-free", "owned_by": "opencode"},  # 探活 200 → 保留
                {"id": "beta-free", "owned_by": "opencode"},   # 探活 401（付费/需 key）→ 剔除
                {"id": "GAMMA-FREE"},                          # 大写后缀也认，探活 200 → 保留
                {"id": "delta-free"},                          # 探活 400（已下线）→ 剔除
                {"id": "no-owner-free"},                       # owned_by 缺失 → name ""
                "junk",                                        # 非 dict 跳过
                {"id": ""},                                    # 空 id 跳过
                {"id": 123},                                   # 非字符串 id 跳过
            ]})
        assert request.url.path == EP_CHAT
        body = json.loads(request.content)
        assert body["stream"] is True
        assert [t["function"]["name"] for t in body["tools"]] == ["bash", "read"]
        status = {"alpha-free": 200, "GAMMA-FREE": 200, "no-owner-free": 200,
                  "beta-free": 401, "delta-free": 400}[body["model"]]
        return httpx.Response(status, text=_sse(chunk({"content": "x"}, finish="stop")))

    models = await _client(handler).fetch_models()
    assert [m.id for m in models] == ["alpha-free", "GAMMA-FREE", "no-owner-free"]
    assert models[0].name == "opencode"
    assert models[2].name == ""


async def test_fetch_models_custom_suffix_and_probe_network_error():
    """后缀可覆盖；探活连接异常按不可用处理。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_MODELS:
            return httpx.Response(200, json={"data": [
                {"id": "m1-free"}, {"id": "m2-lite"}]})
        raise httpx.ConnectError("boom")

    with pytest.raises(zen_events.UpstreamProtocolViolation, match="no free model"):
        await _client(handler, free_suffix="-lite").fetch_models()


async def test_fetch_models_probe_timeout_is_unavailable(monkeypatch):
    """探活首字超时按不可用：整体套 `asyncio.timeout`，不拖垮模型列表。"""
    from src.provider.zen import client as zen_client

    monkeypatch.setattr(zen_client, "PROBE_TIMEOUT", 0.01)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_CHAT:
            await asyncio.sleep(1)   # 首字过慢，超过 PROBE_TIMEOUT
        return httpx.Response(200, json={"data": [{"id": "slow-free"}]})

    with pytest.raises(zen_events.UpstreamProtocolViolation, match="no free model"):
        await _client(handler).fetch_models()


async def test_fetch_models_reuses_probe_result_within_ttl():
    """判活结果在 models_cache_ttl 内复用：服务层每 300s 重拉列表不会重探上游。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == EP_MODELS:
            return httpx.Response(200, json={"data": [
                {"id": "a-free", "owned_by": "opencode"}]})
        assert request.url.path == EP_CHAT
        return httpx.Response(200, text="")

    client = _client(handler)
    first = await client.fetch_models()
    assert [m.id for m in first] == ["a-free"]
    upstream_calls = len(calls)              # 一次清单 + 一次探活
    assert upstream_calls == 2

    second = await client.fetch_models()
    assert [m.id for m in second] == ["a-free"]
    assert len(calls) == upstream_calls      # 命中判活缓存，没再打上游


async def test_fetch_models_cache_expiry_reprobes():
    """超过 models_cache_ttl（此处设为 0）后重新拉清单并重探。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == EP_MODELS:
            return httpx.Response(200, json={"data": [{"id": "a-free"}]})
        return httpx.Response(200, text="")

    client = _client(handler, models_cache_ttl=0)
    await client.fetch_models()
    assert len(calls) == 2
    await client.fetch_models()
    assert len(calls) == 4


async def test_fetch_models_errors():
    def http_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=b"bad gateway")

    with pytest.raises(UpstreamHTTPError):
        await _client(http_error).fetch_models()

    def non_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    with pytest.raises(zen_events.UpstreamProtocolViolation):
        await _client(non_json).fetch_models()

    # JSON 合法但不是对象（无 data 可取）
    def json_scalar(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2])

    with pytest.raises(zen_events.UpstreamProtocolViolation):
        await _client(json_scalar).fetch_models()

    def no_list(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": "list"})

    with pytest.raises(zen_events.UpstreamProtocolViolation):
        await _client(no_list).fetch_models()

    # 有 data 但没有任何 -free 候选（含非 dict 噪声）
    def no_candidates(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": ["junk", {"id": "gpt"}]})

    with pytest.raises(zen_events.UpstreamProtocolViolation, match="no free candidates"):
        await _client(no_candidates).fetch_models()

    # 候选都有，但探活全军覆没（上游整体故障）→ 交给上层缓存兜底
    def all_dead(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_MODELS:
            return httpx.Response(200, json={"data": [{"id": "a-free"},
                                                      {"id": "b-free"}]})
        return httpx.Response(500, text="down")

    with pytest.raises(zen_events.UpstreamProtocolViolation, match="liveness probe"):
        await _client(all_dead).fetch_models()


async def test_client_lazy_clients_and_aclose():
    """未注入 client 时惰性创建；aclose 释放两侧连接池。"""
    client = ZenClient()
    assert client._stream() is client._stream()      # noqa: SLF001 - 惰性复用同一实例
    assert client._short() is client._short()        # noqa: SLF001
    await client.aclose()

    # 都没创建过：aclose 不报错（两个分支都为 None）
    await ZenClient().aclose()


async def test_client_host_trailing_slash_stripped():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == EP_CHAT
        return httpx.Response(200, text=_sse(chunk({"content": "x"}, finish="stop")))

    client = _client(handler, host="https://opencode.ai/")
    assert client.host == "https://opencode.ai"
    _ = [e async for e in client.stream_chat({"messages": []}, "m")]
    await client.aclose()


# --------------------------------------------------------------- provider

async def test_provider_probe_quota_is_unknown_not_exhausted():
    quota = await ZenProvider().probe_quota({})
    assert quota.probe_failed is True
    assert quota.total is None and quota.remaining is None
    assert quota.probed_at is not None


def test_provider_import_credential_is_empty():
    assert ZenProvider().import_credential({"anything": 1}) == {}


def test_provider_classify_delegates():
    assert ZenProvider().classify(429, b"") is ErrKind.SOFT


async def test_provider_list_models_delegates():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_MODELS:
            return httpx.Response(200, json={"data": [{"id": "m1-free"}]})
        return httpx.Response(200, text=_sse(chunk({"content": "x"}, finish="stop")))

    provider = ZenProvider(client=_client(handler))
    models = await provider.list_models({})
    assert [m.id for m in models] == ["m1-free"]
    await provider.aclose()


async def test_provider_stream_chat_with_and_without_pacer():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse(chunk({"content": "hi"}, finish="stop")))

    class Pacer:
        def __init__(self) -> None:
            self.turns = 0

        async def wait_turn(self) -> None:
            self.turns += 1

    pacer = Pacer()
    provider = ZenProvider(client=_client(handler), pacer=pacer)
    events = [e async for e in provider.stream_chat({}, {"messages": []}, "m")]
    assert any(e.kind is EventKind.CONTENT for e in events)
    assert pacer.turns == 1
    await provider.aclose()

    plain = ZenProvider(client=_client(handler))
    _ = [e async for e in plain.stream_chat({}, {"messages": []}, "m")]
    await plain.aclose()


def test_provider_default_client_uses_min_version():
    provider = ZenProvider()
    assert provider.client.version == MIN_OPENCODE_VERSION
    assert provider.id == "zen"


# ------------------------------------------------------- 端点白名单与种子

def test_zen_endpoint_rejects_unallowlisted_host():
    from src.config import Settings
    from src.main import _zen_endpoint
    from tests.conftest import SECRET

    settings = Settings(_env_file=None, APP_SECRET=SECRET,
                        ZEN_API_ENDPOINT="https://evil.example.com")
    with pytest.raises(ValueError, match="ZEN_ALLOWED_ENDPOINTS"):
        _zen_endpoint(settings)


def test_zen_endpoint_accepts_trailing_slash():
    from src.config import Settings
    from src.main import _zen_endpoint
    from tests.conftest import SECRET

    settings = Settings(_env_file=None, APP_SECRET=SECRET,
                        ZEN_API_ENDPOINT="https://opencode.ai/")
    assert _zen_endpoint(settings) == "https://opencode.ai/"


def test_seed_zen_credential_is_idempotent(tmp_path):
    from src.db.conn import Database
    from src.db.crypto import CredentialCipher
    from src.db.migrate import apply_schema
    from src.db.repo import CredentialRepository
    from src.main import _seed_zen_credential
    from tests.conftest import SECRET

    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    repo = CredentialRepository(db, CredentialCipher(SECRET))

    _seed_zen_credential(repo)
    _seed_zen_credential(repo)                      # 已有 → 不再补
    rows = repo.candidates(["zen"])
    assert len(rows) == 1
    assert repo.credential_data(rows[0].credential_id) == {}
    db.close()


async def test_build_app_seeds_zen_and_resolves_forced_route(tmp_path):
    """端到端：默认装配种子 zen 凭证，`模型@zen` 能路由到 zen 渠道。"""
    from src.config import Settings
    from src.engine.model_resolver import resolve
    from src.main import build_app
    from tests.conftest import SECRET

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    assert "zen" in app.state.executor._deps.providers
    rows = app.state.credentials.candidates(["zen"])
    assert len(rows) == 1 and rows[0].health is None      # 未知而非耗尽
    target = resolve("big-pickle@zen", "glm-5.2")
    assert target.providers == ("zen",) and target.forced
    app.state.executor._deps.providers["zen"].client          # noqa: B018 - 存在即可
    for provider in app.state.executor._deps.providers.values():
        closer = getattr(provider, "aclose", None)
        if callable(closer):
            await closer()


def test_zen_credential_can_be_restored_after_delete(tmp_path, monkeypatch):
    """管理台删除 zen 虚拟凭证后，可用「添加 OpenCode Zen」从 UI 补回（无需重启）。

    回归背景：种子只在 build_app 跑一次，用户删掉凭证后 /v1/models 就再也
    没有 zen；前端「登录渠道账号」面板提供的一键补回走通用导入端点
    （provider=zen 时 import_credential 忽略入参、落空对象），这条链路必须稳定。
    """
    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token
    from src.config import Settings
    from src.main import build_app
    from tests.conftest import SECRET

    async def fake_fetch_models(self):
        return [Model(id="offline-free", name="opencode")]

    # TestClient 进 lifespan 会预热模型列表（真连上游），这里保持离线。
    monkeypatch.setattr(ZenClient, "fetch_models", fake_fetch_models)
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        credentials = app.state.credentials
        seeded = credentials.candidates(["zen"])
        assert len(seeded) == 1
        assert credentials.delete(seeded[0].credential_id)          # 模拟管理台删除
        assert credentials.candidates(["zen"]) == []

        client.cookies.set("coding2api_session",
                           create_session_token("root", SECRET))
        created = client.post("/api/credentials",
                              json={"provider": "zen", "credential": {},
                                    "nickname": "OpenCode Zen"})
        assert created.status_code == 200
        credential_id = created.json()["id"]
        restored = credentials.candidates(["zen"])
        assert [row.credential_id for row in restored] == [credential_id]
        assert credentials.credential_data(credential_id) == {}

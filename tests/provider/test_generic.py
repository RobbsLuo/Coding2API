"""通用 OpenAI 兼容上游：凭证导入 / 状态码分类 / 模型表 / 流式翻译 / 装配。"""

from __future__ import annotations

import json

import httpx
import pytest

from src.config import Settings
from src.provider.base import ErrKind, EventKind, UpstreamProtocolViolation
from src.provider.generic.client import (
    GenericClient,
    GenericProvider,
    UpstreamHTTPError,
    classify_status,
    prepare_body,
    split_models,
)


def _client(handler, **kwargs) -> GenericClient:
    return GenericClient(
        endpoint="https://api.example.com/v1",
        stream_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        short_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        **kwargs)


def _sse(*frames: str) -> str:
    return "".join(f"data: {frame}\n\n" for frame in frames)


# ------------------------------------------------------------------ 分类

@pytest.mark.parametrize(("status", "expected"), [
    (401, ErrKind.DEAD),
    (403, ErrKind.DEAD),
    (402, ErrKind.CREDIT),
    (400, ErrKind.INVALID),
    (404, ErrKind.INVALID),
    (422, ErrKind.INVALID),
    (429, ErrKind.SOFT),
    (500, ErrKind.OTHER),
    (503, ErrKind.OTHER),
])
def test_classify_status(status, expected):
    assert classify_status(status) is expected


def test_classify_401_is_dead_unlike_keyless_channels():
    """与 Zen / Kilo 相反：这里 401 就是「这把 key 废了」，必须硬禁用。"""
    assert GenericProvider().classify(401, b"") is ErrKind.DEAD


@pytest.mark.parametrize(("status", "body", "expected"), [
    # 免费池（OpenRouter :free / ModelScope）的模型级限额也回 401/402：
    # 判 DEAD 会一次限额硬禁用整池 key
    (401, b'{"error":{"code":"model_not_found","message":"No endpoints found"}}',
     ErrKind.INVALID),
    (401, b'{"error":{"message":"Rate limit exceeded (free tier)"}}', ErrKind.CREDIT),
    (401, b'{"error":{"code":"insufficient_quota"}}', ErrKind.CREDIT),
    # 纯鉴权失败才 DEAD
    (401, b'{"error":{"code":"invalid_api_key","message":"Incorrect API key"}}',
     ErrKind.DEAD),
    (403, b'{"error":{"code":"unauthorized"}}', ErrKind.DEAD),
    # 证据不足（空 / 非 JSON / 多类命中 / 无关文案）→ 保持现状 DEAD
    (401, b"", ErrKind.DEAD),
    (401, b"\xff\xfe not json", ErrKind.DEAD),
    (401, b'{"error":{"message":"invalid_api_key and quota"}}', ErrKind.DEAD),
    (401, b'{"error":{"message":"something else"}}', ErrKind.DEAD),
    # 402 本身已是 CREDIT；只有点名「没有这个模型」才改判 INVALID
    (402, b"", ErrKind.CREDIT),
    (402, b'{"error":{"message":"Insufficient credits"}}', ErrKind.CREDIT),
    (402, b'{"error":{"code":"model_not_found"}}', ErrKind.INVALID),
])
def test_classify_splits_model_quota_from_dead_key(status, body, expected):
    """401/403 按响应体拆分：模型级限额不硬禁用凭证，纯鉴权失败才 DEAD。"""
    assert classify_status(status, body) is expected
    # provider 入口必须把 body 透传下去（否则等价于只看状态码）
    assert GenericProvider().classify(status, body) is expected


# ------------------------------------------------------------------ 凭证

@pytest.mark.parametrize(("raw", "expected"), [
    ({"api_key": "sk-1"}, {"api_key": "sk-1"}),
    ({"apiKey": "sk-2"}, {"api_key": "sk-2"}),
    ({"token": "sk-3"}, {"api_key": "sk-3"}),
    ({"access_token": "sk-4"}, {"api_key": "sk-4"}),
    ("sk-5", {"api_key": "sk-5"}),
    ({"other": "x"}, {}),
    (123, {}),
])
def test_import_credential(raw, expected):
    assert GenericProvider().import_credential(raw) == expected


def test_probe_quota_is_unknown_not_exhausted():
    quota = __import__("asyncio").run(GenericProvider().probe_quota({}))
    assert quota.probe_failed is True


# ------------------------------------------------------------------ 请求体

def test_prepare_body_deep_copies_and_forces_stream():
    payload = {"messages": [{"role": "user", "content": "hi"}], "stream": False,
               "temperature": 0.5}
    body = prepare_body(payload, "gpt-6.2")
    assert body["model"] == "gpt-6.2"
    assert body["stream"] is True
    assert body["temperature"] == 0.5
    assert body["messages"] is not payload["messages"]


def test_headers_include_bearer_and_user_agent():
    client = GenericClient(endpoint="https://x/v1", api_key="sk-a", user_agent="ua/1")
    headers = client.headers()
    assert headers["Authorization"] == "Bearer sk-a"
    assert headers["User-Agent"] == "ua/1"


def test_headers_omit_authorization_without_key():
    assert "Authorization" not in GenericClient(endpoint="https://x/v1").headers()


# ------------------------------------------------------------------ 流式

async def test_stream_chat_parses_standard_chunks():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer sk-cred"
        body = json.loads(request.content)
        assert body["stream"] is True
        return httpx.Response(200, text=_sse(
            json.dumps({"choices": [{"delta": {"reasoning_content": "r"}}]}),
            json.dumps({"choices": [{"delta": {"content": "hi"}}]}),
            json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 2, "completion_tokens": 3}}),
            "[DONE]"))

    events = [e async for e in _client(handler).stream_chat(
        {"messages": []}, "gpt-6.2", "sk-cred")]
    kinds = [e.kind for e in events]
    assert EventKind.REASONING in kinds
    assert EventKind.CONTENT in kinds
    assert EventKind.USAGE in kinds
    assert EventKind.FINISH in kinds


async def test_stream_chat_raises_classified_http_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b"bad key")

    with pytest.raises(UpstreamHTTPError) as caught:
        [e async for e in _client(handler).stream_chat({}, "m", "sk-x")]
    assert caught.value.kind() is ErrKind.DEAD


async def test_stream_chat_uses_client_key_when_credential_blank():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-client"
        return httpx.Response(200, text=_sse("[DONE]"))

    events = [e async for e in _client(handler, api_key="sk-client").stream_chat(
        {}, "m", "")]
    assert events == []


# ------------------------------------------------------------------ 模型表

async def test_fetch_models_reads_data_list():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [
            {"id": "gpt-6.2", "name": "GPT 6.2"}, {"id": ""}, {"no_id": 1},
            {"id": "kimi-k3.1-pro"}]})

    models = await _client(handler).fetch_models("sk-x")
    assert [m.id for m in models] == ["gpt-6.2", "kimi-k3.1-pro"]
    assert models[0].name == "GPT 6.2"


async def test_fetch_models_rejects_bad_payloads():
    for body in (b"not json", b'{"data": "x"}', b'{"data": []}'):
        def handler(_request: httpx.Request, body=body) -> httpx.Response:
            return httpx.Response(200, content=body)

        with pytest.raises(UpstreamProtocolViolation):
            await _client(handler).fetch_models("sk-x")


async def test_fetch_models_raises_on_http_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"nope")

    with pytest.raises(UpstreamHTTPError):
        await _client(handler).fetch_models("sk-x")


async def test_provider_list_models_falls_back_to_static():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"no models api")

    provider = GenericProvider(client=_client(handler),
                               static_models=split_models("a,b"))
    models = await provider.list_models({"api_key": "sk-x"})
    assert [m.id for m in models] == ["a", "b"]


async def test_provider_list_models_raises_without_static():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"no models api")

    with pytest.raises(UpstreamHTTPError):
        await GenericProvider(client=_client(handler)).list_models({})


async def test_split_models_trims_and_skips_blank():
    assert [m.id for m in split_models(" a , ,b ")] == ["a", "b"]


# ------------------------------------------------------------------ 装配

def test_provider_stream_chat_with_and_without_pacer():
    """pacer 为 None 时直通；有 pacer 时配对 wait/release。"""
    calls: list[str] = []

    class FakePacer:
        async def wait_turn(self, key=None):
            calls.append(f"wait:{key}")

        def release(self, key=None):
            calls.append(f"release:{key}")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse(
            json.dumps({"choices": [{"delta": {"content": "x"}}]}), "[DONE]"))

    import asyncio

    async def run(provider):
        return [e async for e in provider.stream_chat({}, {}, "m")]

    provider = GenericProvider(client=_client(handler), pacer=FakePacer())
    events = asyncio.run(run(provider))
    assert [e.kind for e in events] == [EventKind.CONTENT]
    assert calls == ["wait:generic", "release:generic"]

    calls.clear()
    plain = GenericProvider(client=_client(handler))
    asyncio.run(run(plain))
    assert calls == []


def test_generic_endpoint_disabled_by_default():
    settings = Settings(_env_file=None, APP_SECRET="x" * 32, DATA_DIR="/tmp")
    assert settings.generic_enabled is False
    assert settings.generic_allowed == ()


def test_generic_allowed_whitelist_and_validation():
    from src.config import validate_generic_endpoint_allowed

    settings = Settings(_env_file=None, APP_SECRET="x" * 32, DATA_DIR="/tmp",
                        GENERIC_API_ENDPOINT="https://api.example.com/v1",
                        GENERIC_ALLOWED_ENDPOINTS="https://api.example.com/v1")
    assert settings.generic_enabled is True
    assert validate_generic_endpoint_allowed("https://api.example.com/v1", settings)
    assert not validate_generic_endpoint_allowed("https://evil.example/x", settings)
    # 非 http(s) 一律拒绝（即使白名单为空）
    assert not validate_generic_endpoint_allowed("file:///etc/passwd", settings)


def test_generic_empty_whitelist_allows_https_and_loopback_http():
    """白名单为空 = 放行任意 https；明文 http 只限回环（凭证是真 key）。"""
    from src.config import validate_generic_endpoint_allowed

    settings = Settings(_env_file=None, APP_SECRET="x" * 32, DATA_DIR="/tmp",
                        GENERIC_API_ENDPOINT="http://127.0.0.1:9000/v1")
    assert settings.generic_allowed == ()
    assert validate_generic_endpoint_allowed("https://10.0.0.5:8080/v1", settings)
    assert validate_generic_endpoint_allowed("http://127.0.0.1:9000/v1", settings)
    # 公网明文 http 拒绝：key 会在链路上裸奔
    assert not validate_generic_endpoint_allowed("http://10.0.0.5:8080/v1", settings)


def test_known_providers_and_tos_include_generic():
    from src.engine.model_resolver import KNOWN_PROVIDERS
    from src.provider.tos import tos_for

    assert "generic" in KNOWN_PROVIDERS
    assert tos_for("generic").level == "green"


# ------------------------------------------------------------------ 覆盖补丁

async def test_stream_error_envelope_maps_to_error_event():
    """流内 `{"error": {...}}` 信封 → ERROR 事件（标准 OpenAI 形状）。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse(
            json.dumps({"error": {"code": 429, "message": "slow down"}}), "[DONE]"))

    events = [e async for e in _client(handler).stream_chat({}, "m", "sk-x")]
    assert [e.kind for e in events] == [EventKind.ERROR]
    assert events[0].error_code == 429
    assert events[0].error_message == "slow down"


async def test_stream_error_envelope_without_code_or_message():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse(
            json.dumps({"error": {"code": "invalid_request_error"}}), "[DONE]"))

    events = [e async for e in _client(handler).stream_chat({}, "m", "sk-x")]
    assert events[0].kind is EventKind.ERROR
    assert events[0].error_code is None      # 字符串码不进 int 字段
    assert events[0].error_message == ""


async def test_fetch_models_skips_items_with_non_string_id():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "data": [{"id": 5}, {"id": "ok"}, "not-a-dict"]})

    models = await _client(handler).fetch_models("sk-x")
    assert [m.id for m in models] == ["ok"]


async def test_aclose_releases_pools():
    provider = GenericProvider(client=_client(lambda _r: httpx.Response(200)))
    await provider.aclose()                  # 不抛即可（LazyClients 跳过未建的池）


def test_parse_frame_helper_returns_none_for_done():
    from src.engine.sse import SSEFrame
    from src.provider.generic.client import parse_frame

    assert parse_frame(SSEFrame(event="", data="[DONE]")) is None
    event = parse_frame(SSEFrame(event="", data=json.dumps(
        {"choices": [{"delta": {"content": "x"}}]})))
    assert event is not None and event.kind is EventKind.CONTENT


def test_generic_endpoint_registered_only_when_configured(tmp_path):
    """配了端点才注册 generic；没配时 registry 里没有这条渠道。"""
    from src.config import Settings
    from src.main import build_app
    from tests.helpers.constants import SECRET

    off = build_app(Settings(_env_file=None, APP_SECRET=SECRET,
                             DATA_DIR=str(tmp_path / "off"), ADMIN_USERNAMES="root"))
    assert "generic" not in off.state.services.registry

    on = build_app(Settings(_env_file=None, APP_SECRET=SECRET,
                            DATA_DIR=str(tmp_path / "on"), ADMIN_USERNAMES="root",
                            GENERIC_API_ENDPOINT="https://api.example.com/v1",
                            GENERIC_ALLOWED_ENDPOINTS="https://api.example.com/v1",
                            GENERIC_MODELS="gpt-6.2,kimi-k3.1-pro"))
    registry = on.state.services.registry
    assert "generic" in registry
    assert registry["generic"].id == "generic"
    assert [m.id for m in registry["generic"].static_models] == [
        "gpt-6.2", "kimi-k3.1-pro"]


def test_generic_endpoint_rejects_bad_scheme(tmp_path):
    from src.config import Settings
    from src.main import build_app
    from tests.helpers.constants import SECRET

    with pytest.raises(ValueError, match="GENERIC_API_ENDPOINT"):
        build_app(Settings(_env_file=None, APP_SECRET=SECRET,
                           DATA_DIR=str(tmp_path), ADMIN_USERNAMES="root",
                           GENERIC_API_ENDPOINT="file:///etc/passwd"))

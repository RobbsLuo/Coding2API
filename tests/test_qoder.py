"""Qoder 渠道测试：COSY 编码/签名、凭证、信封 SSE、额度、签到三态、刷新、
provider 协议与设备码登录。

覆盖硬门槛按 AGENTS.md：新增分支必须测到（不靠 pragma 达标）。全部用
MockTransport，不触网；`Settings` 一律显式传值，不依赖 `.env`。
"""

from __future__ import annotations

import base64
import hashlib
import json
import time

import httpx
import pytest

from src.engine.sse import SSEFrame
from src.provider.base import ErrKind, EventKind
from src.provider.qoder import auth as qoder_auth
from src.provider.qoder import client as qoder_client
from src.provider.qoder import credential_key, status_to_dict
from src.provider.qoder import events as qoder_events
from src.provider.qoder.auth import (
    AuthStateStore,
    QoderOAuth,
    _fallback_machine_id,
    build_auth_url,
    machine_id_for,
    pkce_pair,
)
from src.provider.qoder.client import (
    CHECKIN_UNAVAILABLE_STATUS,
    QoderClient,
    UpstreamHTTPError,
    build_chat_body,
    build_openapi_headers,
    realm_for,
    session_dead,
)
from src.provider.qoder.cosy import (
    COSY_VERSION,
    CosySession,
    CosySessionCache,
    _pkcs7_unpad,
    aes_cbc_decrypt,
    cosy_signature,
    derive_id,
    derive_machine_token,
    derive_machine_type,
    derive_request_id,
    json_sorted_compact,
    qoder_decode,
    qoder_encode,
    sign_path,
)
from src.provider.qoder.credential import (
    QoderCredential,
    credential_from_device,
    expires_from_device,
    merge_refreshed,
    parse_credential,
)
from src.provider.qoder.events import UpstreamProtocolViolation

GATEWAY = "https://gateway.test"
OPENAPI = "https://openapi.test"


def cred(**overrides) -> QoderCredential:
    base = {"access_token": "dt-token-1", "uid": "u-1", "realm": "cn",
            "refresh_token": "drt-1", "auth_source": "oauth",
            "nickname": "Tester", "expires_at": int(time.time()) + 86400}
    base.update(overrides)
    return QoderCredential(**base)


def envelope(body: object, status: int = 200) -> str:
    payload = {"headers": {}, "body": body if isinstance(body, str) else json.dumps(body),
               "statusCodeValue": status}
    return f"data: {json.dumps(payload)}\n\n"


def inner(delta: dict, *, finish: str | None = None, usage: dict | None = None,
          choices: list | None = None) -> dict:
    payload: dict = {"id": "c1", "object": "chat.completion.chunk"}
    if choices is None:
        payload["choices"] = [{"index": 0, "delta": delta, "finish_reason": finish}]
    else:
        payload["choices"] = choices
    if usage is not None:
        payload["usage"] = usage
    return payload


def sse(*payloads: object) -> str:
    return "".join(envelope(p) for p in payloads) + envelope("[DONE]")


def handler_for(handler, **kwargs) -> QoderClient:
    transport = httpx.MockTransport(handler)
    kwargs.setdefault("gateway_fallbacks", (GATEWAY,))
    return QoderClient(
        host=OPENAPI, gateway=GATEWAY,
        stream_client=httpx.AsyncClient(transport=transport, timeout=None),
        short_client=httpx.AsyncClient(transport=transport, timeout=None),
        **kwargs)


# --------------------------------------------------------- 自定义 Base64

def test_qoder_encode_roundtrip_and_custom_alphabet():
    for plain in (b"", b"a", b"ab", b"abc", b"hello world!" * 7):
        encoded = qoder_encode(plain)
        assert qoder_decode(encoded) == plain
    # 标准字母表字符不出现在编码结果里，填充符换成 "$"
    encoded = qoder_encode(b"\x00")
    assert "+" not in encoded and "/" not in encoded and "=" not in encoded
    assert "$" in encoded


def test_qoder_encode_matches_documented_transform():
    plain = b"{\"a\":1}"
    std = base64.b64encode(plain).decode("ascii")
    size = len(std)
    head = size // 3
    rearranged = std[size - head:] + std[head:size - head] + std[:head]
    expected = rearranged.translate(str.maketrans(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=",
        "_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!$"))
    assert qoder_encode(plain) == expected


def test_json_sorted_compact_none_becomes_empty_string():
    assert json_sorted_compact({"b": 1, "a": None}) == b'{"a":"","b":1}'
    assert json_sorted_compact({}) == b"{}"


# --------------------------------------------------------- 设备指纹/签名

def test_derive_helpers_are_stable_and_shaped():
    assert derive_id("u1", "machine") == derive_id("u1", "machine")
    assert derive_id("u1", "machine") != derive_id("u2", "machine")
    assert derive_id("", "machine") == derive_id("", "machine")   # anonymous 稳定
    machine_type = derive_machine_type("u1")
    assert len(machine_type) == 18 and "-" not in machine_type
    assert len(derive_machine_token("u1")) == 43
    assert derive_request_id("u1").startswith(derive_id("u1", "req") + "-")


def test_sign_path_strips_algo_prefix_and_query():
    assert sign_path(GATEWAY + qoder_events.EP_MODELS) == (
        "/api/v2/model/list")
    assert sign_path(GATEWAY + qoder_events.EP_CHAT) == (
        "/api/v2/service/pro/sse/agent_chat_generation")
    assert sign_path("https://gw.test/other") == "/other"
    assert sign_path("https://gw.test") == "/"


def test_session_headers_and_signature_vector():
    session = CosySession(uid="u1", access_token="tok", temp_key="0123456789abcdef")
    url = GATEWAY + qoder_events.EP_CHAT
    headers = session.headers(body="BODY", raw_url=url, model_key="qmodel_38max",
                              sse=True, date="1700000000", request_id="rid")
    payload = base64.b64encode(json_sorted_compact({
        "cosyVersion": COSY_VERSION, "ideVersion": "", "info": session.info,
        "requestId": "rid", "version": "v1"})).decode("ascii")
    digest = hashlib.md5("\n".join([
        payload, session.cosy_key, "1700000000", "BODY",
        "/api/v2/service/pro/sse/agent_chat_generation"]).encode()).hexdigest()
    assert headers["authorization"] == f"Bearer COSY.{payload}.{digest}"
    assert headers["cosy-date"] == "1700000000"
    assert headers["cosy-user"] == "u1"
    assert headers["x-model-key"] == "qmodel_38max"
    assert headers["x-model-source"] == "system"
    assert headers["cache-control"] == "no-cache"
    assert headers["cosy-key"] == session.cosy_key
    assert headers["cosy-machineid"] == derive_id("u1", "machine")


def test_session_without_sse_and_model_key_omits_optional_headers():
    session = CosySession(uid="", access_token="tok", temp_key="0123456789abcdef")
    headers = session.headers(body="x", raw_url=GATEWAY + qoder_events.EP_MODELS,
                              sse=False, accept="application/json")
    assert "cache-control" not in headers
    assert "x-model-key" not in headers
    assert headers["accept"] == "application/json"
    assert headers["cosy-user"] == ""


def test_session_info_is_decryptable_identity():
    session = CosySession(uid="u9", access_token="tok", nickname="N",
                          refresh_token="drt-9", temp_key="0123456789abcdef",
                          user_type="pro", org_id="org-1", org_name="Org")
    plain = aes_cbc_decrypt(base64.b64decode(session.info), b"0123456789abcdef",
                            b"0123456789abcdef")
    identity = json.loads(plain)
    assert identity["uid"] == "u9" and identity["name"] == "N"
    assert identity["security_oauth_token"] == "tok"
    assert identity["refresh_token"] == "drt-9"
    assert identity["organization_id"] == "org-1" and identity["yx_uid"] == ""


def test_session_rejects_empty_token_and_bad_temp_key():
    with pytest.raises(ValueError, match="empty access token"):
        CosySession(uid="u", access_token="")
    with pytest.raises(ValueError, match="temp key"):
        CosySession(uid="u", access_token="t", temp_key="short")


def test_cosy_signature_is_md5_of_parts():
    assert cosy_signature(payload_b64="p", cosy_key="k", date="d", body="b",
                          path="/x") == hashlib.md5(
        b"p\nk\nd\nb\n/x").hexdigest()


def test_session_cache_rebuilds_on_token_change():
    cache = CosySessionCache(temp_key_factory=lambda: "0123456789abcdef")
    first = cache.get(cred())
    assert cache.get(cred()) is first                       # 同 token 复用
    rotated = cache.get(cred(access_token="dt-token-2"))
    assert rotated is not first
    cache.invalidate("u-1")
    assert cache.get(cred()) is not first
    cache.clear()
    with pytest.raises(ValueError, match="empty access token"):
        cache.get(cred(access_token=""))


def test_session_cache_keys_by_token_when_uid_missing():
    cache = CosySessionCache(temp_key_factory=lambda: "0123456789abcdef")
    session = cache.get(cred(uid=""))
    assert session.uid == ""
    assert cache.get(cred(uid="")) is session


# ------------------------------------------------------------- 凭证

def test_credential_roundtrip_and_realm_detection():
    raw = {"access_token": "t", "uid": "u", "realm": "intl",
           "refresh_token": "r", "expires_at": 123, "auth_source": "oauth",
           "user_type": "pro", "organization_id": "o", "organization_name": "O",
           "nickname": "N", "domain": "qoder.com"}
    restored = QoderCredential.from_dict(QoderCredential.from_dict(raw).to_dict())
    assert restored == QoderCredential.from_dict(raw)
    # 无 realm 时按 domain 推断
    assert QoderCredential.from_dict({"domain": "qoder.sh"}).realm == "intl"
    assert QoderCredential.from_dict({"domain": "qoder.com.cn"}).realm == "cn"
    assert QoderCredential.from_dict({}).realm == "cn"
    # 非法 auth_source 保持 unknown
    assert QoderCredential.from_dict({"auth_source": "hacked"}).auth_source == "unknown"
    # 驼峰键名兼容
    camel = QoderCredential.from_dict({"accessToken": "t", "userId": "u2",
                                       "refreshToken": "r2", "userType": "pro"})
    assert (camel.access_token, camel.uid, camel.refresh_token) == ("t", "u2", "r2")
    assert camel.user_type == "pro"
    assert QoderCredential.from_dict({"access_token": "t"}).user_type == (
        qoder_events.DEFAULT_USER_TYPE)


def test_credential_realm_config_and_needs_refresh():
    credential = cred(expires_at=int(time.time()) + 100)
    assert credential.realm_config().openapi.endswith("qoder.com.cn")
    assert credential.needs_refresh(3600) is True
    assert credential.needs_refresh(0) is False
    assert credential.needs_refresh(3600, now=int(time.time()) - 100000) is False


def test_needs_refresh_requires_oauth_and_refresh_token():
    assert cred(auth_source="manual").needs_refresh(10**9) is False
    assert cred(refresh_token="").needs_refresh(10**9) is False
    assert cred(expires_at=0, access_token="not-a-jwt").needs_refresh(10**9) is False


def test_token_expires_at_falls_back_to_jwt_exp():
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": 2000000000}).encode()).decode().rstrip("=")
    jwt = f"h.{payload}.s"
    assert QoderCredential(access_token=jwt).token_expires_at() == 2000000000
    # 到期时刻本身即进入刷新窗口（now == exp）；刷新只对 OAuth 凭证生效
    oauth = QoderCredential(access_token=jwt, auth_source="oauth",
                            refresh_token="drt")
    assert oauth.needs_refresh(0, now=2000000000) is True


def test_parse_credential_accepts_bytes_and_rejects_bad_input():
    parsed = parse_credential(b'{"token": "t", "user_id": "u"}')
    assert parsed.access_token == "t" and parsed.uid == "u"
    assert parsed.auth_source == "manual"
    oauth = parse_credential({"access_token": "t"}, auth_source="oauth")
    assert oauth.auth_source == "oauth"
    # 显式非法来源不被入口参数覆盖
    assert parse_credential({"access_token": "t",
                             "auth_source": "nope"}).auth_source == "unknown"
    with pytest.raises(UpstreamProtocolViolation, match="not valid JSON"):
        parse_credential(b"{not json")
    with pytest.raises(UpstreamProtocolViolation, match="not an object"):
        parse_credential(["nope"])  # type: ignore[arg-type]
    with pytest.raises(UpstreamProtocolViolation, match="missing access token"):
        parse_credential({"uid": "u"})


def test_device_expiry_forms():
    now = int(time.time())
    assert expires_from_device({"expires_in": 7_200_000}) == pytest.approx(now + 7200,
                                                                         abs=5)
    iso = expires_from_device({"expires_at": "2030-01-01T00:00:00Z"})
    assert iso > now
    # 带时区的 RFC3339 与小数秒
    assert expires_from_device({"expires_at": "2030-01-01T08:00:00+08:00"}) == iso
    assert expires_from_device({"expires_at": "not-a-date"}) == 0
    assert expires_from_device({}) == 0
    assert expires_from_device({"expires_in": 0}) == 0
    assert expires_from_device({"expires_at": 123}) == 0


def test_credential_from_device_prefers_userinfo_and_falls_back_uid():
    data = {"token": "dt-9", "refresh_token": "drt-9", "expires_in": 1000,
            "user_id": "from-device"}
    built = credential_from_device(data, realm="intl",
                                   userinfo={"id": "from-userinfo", "name": "N",
                                             "organization_id": "o"})
    assert built.uid == "from-userinfo" and built.nickname == "N"
    assert built.realm == "intl" and built.domain == "qoder.com"
    assert built.refresh_token == "drt-9" and built.expires_at > 0
    assert credential_from_device(data).uid == "from-device"
    assert credential_from_device({}).uid == ""
    assert credential_from_device({}, fallback_uid="fb").uid == "fb"


def test_merge_refreshed_keeps_identity():
    old = cred()
    merged = merge_refreshed(old, {"token": "dt-new"})
    assert merged.access_token == "dt-new"
    assert merged.uid == old.uid and merged.nickname == old.nickname
    assert merged.refresh_token == old.refresh_token
    assert merged.expires_at == old.expires_at
    # 响应不带 token 时保留旧值（不把凭证刷成空）
    kept = merge_refreshed(old, {"refresh_token": "drt-2"})
    assert kept.access_token == old.access_token
    assert kept.refresh_token == "drt-2"
    assert merge_refreshed(old, {"token": "x", "expires_in": 3_600_000}).expires_at > 0


# ------------------------------------------------------------- 请求体

def test_build_chat_body_maps_fields_and_is_stream_only():
    payload = {"messages": [{"role": "user", "content": "hi"}], "stream": False,
               "temperature": 0.5, "max_tokens": 128, "reasoning_effort": "high",
               "tools": [{"type": "function"}], "top_p": 0.9}
    body = build_chat_body(payload, "qmodel_38max")
    assert body["stream"] is True and body["model"] == "qmodel_38max"
    assert body["agent_id"] == "agent_common"
    assert body["parameters"] == {"max_tokens": 128, "reasoning_effort": "high"}
    assert body["tools"] == [{"type": "function"}] and body["top_p"] == 0.9
    assert body["messages"] == payload["messages"]
    # 深拷贝：改写上游 body 不影响引擎持有的请求原文
    body["messages"][0]["content"] = "changed"
    assert payload["messages"][0]["content"] == "hi"


def test_build_chat_body_empty_and_alternate_effort_shapes():
    body = build_chat_body({}, "m")
    assert body["messages"] == [] and "parameters" not in body
    nested = build_chat_body({"reasoning": {"effort": "low"},
                              "max_completion_tokens": 10}, "m")
    assert nested["parameters"] == {"max_tokens": 10, "reasoning_effort": "low"}
    assert "parameters" not in build_chat_body({"max_tokens": 0, "tools": None}, "m")


def test_openapi_headers_use_stable_device_ids():
    headers = build_openapi_headers(cred())
    assert headers["Authorization"] == "Bearer dt-token-1"
    assert headers["X-Machine-ID"] == derive_id("u-1", "machine")
    assert headers["X-Session-ID"] == derive_id("u-1", "session")
    assert headers["User-Agent"] == qoder_events.CLIENT_UA
    assert headers["Origin"].startswith("https://qoder.cn")


def test_realm_for_and_session_dead_helpers():
    assert realm_for(cred()) == "cn"
    assert realm_for(cred(realm="", domain="qoder.sh")) == "intl"
    assert realm_for(cred(realm="", domain="")) == "cn"
    assert session_dead('{"error":"TOKEN_EXPIRE"}') is True
    assert session_dead("Offline user session not found") is True
    assert session_dead("12153") is True
    assert session_dead("ok") is False


# ------------------------------------------------------------ 推理流

async def test_stream_chat_decodes_envelope_and_done():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/algo/api/v2/service/pro/sse/agent_chat_generation"
        assert request.headers["authorization"].startswith("Bearer COSY.")
        assert request.headers["x-model-key"] == "qmodel_38max"
        # 请求体是自定义 Base64，解码后是带 stream=true 的 JSON
        body = json.loads(qoder_decode(request.content.decode("ascii")))
        assert body["stream"] is True and body["model"] == "qmodel_38max"
        return httpx.Response(200, text=sse(
            inner({"role": "assistant", "reasoning_content": "why"}),
            inner({"content": "hi"}),
            inner({"content": "!"}, finish="stop",
                  usage={"prompt_tokens": 3, "completion_tokens": 2}),
            inner({}, choices=[], usage={"prompt_tokens": 5}),
        ))

    events = [e async for e in handler_for(handler).stream_chat(
        cred(), {"messages": [{"role": "user", "content": "x"}]}, "qmodel_38max")]
    kinds = [e.kind for e in events]
    # 与 codebuddy/zen 的 parse_all_events 同序：同一帧先 usage 后 finish
    assert kinds == [EventKind.REASONING, EventKind.CONTENT, EventKind.CONTENT,
                     EventKind.USAGE, EventKind.FINISH, EventKind.USAGE]
    assert events[1].content == "hi"
    assert events[3].usage.input_tokens == 3 and events[3].usage.output_tokens == 2
    assert events[4].finish_reason == "stop"


async def test_stream_chat_inner_error_frame_is_classified():
    """`statusCodeValue != 200` 是流内错误（HTTP 仍 200）。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=(
            envelope(inner({"content": "part"}))
            + envelope("upstream boom", status=418)
            + envelope("[DONE]")))

    events = [e async for e in handler_for(handler).stream_chat(
        cred(), {"messages": []}, "m")]
    assert [e.kind for e in events] == [EventKind.CONTENT, EventKind.ERROR]
    assert events[1].error_code == 418
    assert events[1].error_kind is ErrKind.SOFT


async def test_stream_chat_top_level_done_and_blank_frames():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="data: \n\ndata: [DONE]\n\n")

    events = [e async for e in handler_for(handler).stream_chat(cred(), {}, "m")]
    assert events == []


async def test_stream_chat_http_error_is_classified():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b"slow")

    with pytest.raises(UpstreamHTTPError) as caught:
        [e async for e in handler_for(handler).stream_chat(cred(), {}, "m")]
    assert caught.value.kind() is ErrKind.MODEL


async def test_stream_chat_falls_back_to_second_gateway_on_transport_error():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "gw1.test":
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, text=sse(inner({"content": "ok"}, finish="stop")))

    client = handler_for(handler, gateway_fallbacks=("https://gw1.test",
                                                     "https://gw2.test"))
    events = [e async for e in client.stream_chat(cred(), {}, "m")]
    assert [e.kind for e in events] == [EventKind.CONTENT, EventKind.FINISH]
    assert seen == ["gw1.test", "gw2.test"]
    await client.aclose()


async def test_stream_chat_reraises_transport_error_on_last_gateway():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = handler_for(handler, gateway_fallbacks=("https://gw1.test",))
    with pytest.raises(httpx.ConnectError):
        [e async for e in client.stream_chat(cred(), {}, "m")]
    await client.aclose()


async def test_decoded_envelope_protocol_violation_propagates():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="data: {not json}\n\n")

    with pytest.raises(UpstreamProtocolViolation):
        [e async for e in handler_for(handler).stream_chat(cred(), {}, "m")]


# ------------------------------------------------------------ 模型发现

async def test_fetch_models_signs_get_and_sends_no_body():
    """模型清单端点只接受 GET：带 COSY 签名头、请求体为空。

    实测（2026-09-30）带头 POST 会被上游 400「Request method 'POST' not
    supported」拒绝，故断言方法为 GET 且无请求体。
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["body"] = request.content
        seen["auth"] = request.headers["authorization"]
        seen["key"] = request.headers["cosy-key"]
        seen["date"] = request.headers["cosy-date"]
        seen["path"] = request.url.path
        assert "x-model-key" not in request.headers       # 清单请求不带 x-model-key
        return httpx.Response(200, json={"chat": [
            {"key": "qmodel_38max", "display_name": "Qwen3.8-Max",
             "is_vl": True, "supportsToolCall": True, "is_reasoning": True,
             "max_input_tokens": 180000},
            {"key": ""},                                    # 空 key 跳过
            "junk",                                         # 非 dict 跳过
            {"key": "dmodel", "display_name": "DeepSeek"},
        ]})

    client = handler_for(handler)
    models = await client.fetch_models(cred())
    sign_body = qoder_encode(qoder_events.MODELS_SIGN_PLAIN)
    assert seen["method"] == "GET"
    assert seen["body"] == b""                           # GET 无请求体
    assert seen["key"] == client.sessions.get(cred()).cosy_key
    expected = hashlib.md5("\n".join([
        seen["auth"].split(".")[1], seen["key"], seen["date"], sign_body,
        sign_path(seen["path"])]).encode()).hexdigest()
    assert seen["auth"] == f"Bearer COSY.{seen['auth'].split('.')[1]}.{expected}"
    assert [m.id for m in models] == ["qmodel_38max", "dmodel"]
    assert models[0].name == "Qwen3.8-Max" and models[0].supports_images is True
    assert models[0].max_input_tokens == 180000
    # 缓存：同区域 10 分钟内不再打上游
    called = {"n": 0}

    def counting(_request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"chat": [{"key": "m2"}]})

    client2 = handler_for(counting)
    client2._model_cache["cn"] = (time.time(), models)   # noqa: SLF001
    assert await client2.fetch_models(cred()) == models
    assert called["n"] == 0
    await client.aclose()
    await client2.aclose()


async def test_fetch_models_errors_and_host_fallback():
    def http_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"forbidden")

    with pytest.raises(UpstreamHTTPError) as caught:
        await handler_for(http_error).fetch_models(cred())
    assert caught.value.kind() is ErrKind.SOFT

    def not_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    with pytest.raises(UpstreamProtocolViolation, match="model list request failed"):
        await handler_for(not_json).fetch_models(cred())

    def not_object(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2])

    with pytest.raises(UpstreamProtocolViolation, match="not an object"):
        await handler_for(not_object).fetch_models(cred())

    def no_models(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"chat": []})

    with pytest.raises(UpstreamProtocolViolation, match="no usable models"):
        await handler_for(no_models).fetch_models(cred())

    # 第一台主机传输失败 → 换备用主机
    def flaky(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gw1.test":
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"chat": [{"key": "m"}]})

    client = handler_for(flaky, gateway_fallbacks=("https://gw1.test",
                                                   "https://gw2.test"))
    assert [m.id for m in await client.fetch_models(cred())] == ["m"]
    await client.aclose()

    # 所有主机都传输失败 → UpstreamTransportError（可翻译成网络不可达），
    # 而不是协议违规（「响应格式不符」会误导用户去查上游改版）
    def all_down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    from src.provider.base import UpstreamTransportError

    with pytest.raises(UpstreamTransportError):
        await handler_for(all_down, gateway_fallbacks=("https://gw1.test",
                                                       "https://gw2.test")).fetch_models(cred())


# ---------------------------------------------------------------- 额度

async def test_probe_quota_aggregates_base_and_addon():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == qoder_events.EP_QUOTA
        assert request.headers["Authorization"] == "Bearer dt-token-1"
        return httpx.Response(200, json={
            "userQuota": {"total": 100, "used": 40, "remaining": 60},
            "addOnQuota": {"total": 50, "used": 10, "remaining": 40},
            "expiresAt": 2000000000})

    quota = await handler_for(handler).probe_quota(cred())
    assert quota.total == 150 and quota.remaining == 100
    assert quota.cycle_end == 2000000000
    assert [p["name"] for p in quota.packages] == ["基础额度", "赠送额度"]


async def test_probe_quota_http_error_and_bad_shape():
    def http_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b'{"error":"TOKEN_EXPIRE"}')

    with pytest.raises(UpstreamHTTPError) as caught:
        await handler_for(http_error).probe_quota(cred())
    assert caught.value.kind() is ErrKind.DEAD

    def bad_shape(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="nope")

    with pytest.raises(UpstreamProtocolViolation):
        await handler_for(bad_shape).probe_quota(cred())


# ---------------------------------------------------------------- 签到

def _status_payload(status: str, *, last: int | None = None) -> dict:
    payload = {"status": status, "currentStreakDays": 3, "rewardCredits": 100,
               "totalRewardCredits": 400}
    if last is not None:
        payload["lastClaimedAt"] = last
    return payload


def _campaign(campaign_id: str = "c-1", *, action: str = "CLAIM_BENEFIT",
              status: str = "CLAIMABLE", amount: object = 100,
              kind: object = "CREDITS") -> dict:
    benefit: dict = {}
    if kind is not None:
        benefit["kind"] = kind
    if amount is not None:
        benefit["amount"] = amount
    return {"campaignId": campaign_id, "campaignKey": "act-1",
            "actionType": action, "claimStatus": status, "benefit": benefit}


def _campaigns(*items: dict) -> dict:
    return {"showCampaign": bool(items), "campaigns": list(items)}


def _assert_campaign_headers(request: httpx.Request) -> None:
    assert request.headers["Cosy-ClientType"] == qoder_events.COSY_CLIENT_TYPE


async def test_checkin_campaign_claims_and_reports_credit():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        _assert_campaign_headers(request)
        paths.append(request.url.path)
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(200, json=_campaigns(_campaign("abc-123")))
        assert request.url.path == "/sash/api/v1/me/campaigns/abc-123/claim"
        return httpx.Response(200, json={
            "status": "CLAIMED", "replayed": False, "benefit": {"amount": 100},
            "expiresAt": "2026-10-30T00:00:00Z"})

    result = await handler_for(handler).checkin(cred())
    assert result.ok is True and result.already_checked_in is False
    assert result.credit == 100.0 and result.code == 0
    assert result.message == "签到成功"
    # 新协议不提供连续天数：不编造 0，置 None 交前端隐藏
    assert result.status is not None and result.status.streak_days is None
    assert result.status.today_credit == 100
    assert paths == [qoder_events.EP_CAMPAIGNS,
                     "/sash/api/v1/me/campaigns/abc-123/claim"]


async def test_checkin_campaign_already_claimed_needs_no_claim():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=_campaigns(
            _campaign(status="CLAIMED")))

    result = await handler_for(handler).checkin(cred())
    assert result.ok is True and result.already_checked_in is True
    assert result.message == "今日已签到"
    assert paths == [qoder_events.EP_CAMPAIGNS]


async def test_checkin_campaign_skips_view_details_and_picks_claimable():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(200, json=_campaigns(
                _campaign("detail-1", action="VIEW_DETAILS"),
                _campaign("reward-9"),
            ))
        # 必须领到 CLAIM_BENEFIT 那条，而不是展示位
        assert request.url.path == "/sash/api/v1/me/campaigns/reward-9/claim"
        return httpx.Response(200, json={
            "status": "CLAIMED", "replayed": False, "benefit": {"amount": 100}})

    result = await handler_for(handler).checkin(cred())
    assert result.ok is True and result.credit == 100.0


async def test_checkin_campaign_replayed_and_same_person_are_already():
    def replayed(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(200, json=_campaigns(_campaign()))
        return httpx.Response(200, json={"status": "CLAIMED", "replayed": True,
                                         "benefit": {"amount": 100}})

    result = await handler_for(replayed).checkin(cred())
    assert result.ok is True and result.already_checked_in is True

    def same_person(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(200, json=_campaigns(_campaign()))
        return httpx.Response(200, json={
            "status": "BLOCKED", "failureCode": "SAME_PERSON_ALREADY_CLAIMED"})

    blocked = await handler_for(same_person).checkin(cred())
    assert blocked.ok is True and blocked.already_checked_in is True

    def blocked_other(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(200, json=_campaigns(_campaign()))
        return httpx.Response(200, json={"status": "BLOCKED",
                                         "failureCode": "RATE_LIMITED"})

    rejected = await handler_for(blocked_other).checkin(cred())
    assert rejected.ok is False and rejected.already_checked_in is False
    assert "RATE_LIMITED" in rejected.message


async def test_checkin_campaign_no_claimable_and_abnormal_benefit():
    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_campaigns())

    inactive = await handler_for(empty).checkin(cred())
    assert inactive.ok is True and inactive.already_checked_in is False
    assert inactive.message == "官方签到活动未开放"
    assert inactive.status is not None and inactive.status.active is False

    def claimable_but_disabled(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_campaigns(
            _campaign(status="DISABLED")))

    nothing = await handler_for(claimable_but_disabled).checkin(cred())
    assert nothing.ok is True
    assert nothing.message == "今日暂无可领取的签到奖励"

    def abnormal(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(200, json=_campaigns(_campaign()))
        return httpx.Response(200, json={"status": "CLAIMED"})   # 无 benefit

    weird = await handler_for(abnormal).checkin(cred())
    assert weird.ok is True and weird.credit is None
    assert weird.message == "签到成功（活动响应异常）"


async def test_checkin_legacy_fallback_when_campaigns_unavailable():
    """活动制接口 404 → 整体回退旧 daily-check-in 流程。"""
    now = int(time.time())
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(404, content=b"not found")
        assert request.url.path == qoder_events.EP_CHECKIN_STATUS
        return httpx.Response(200, json=_status_payload("CLAIMED", last=now))

    result = await handler_for(handler).checkin(cred())
    assert result.ok is True and result.already_checked_in is True
    assert result.status is not None and result.status.streak_days == 3
    assert paths == [qoder_events.EP_CAMPAIGNS, qoder_events.EP_CHECKIN_STATUS]


async def test_checkin_legacy_fallback_claim_paths():
    def claimable(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(410)
        if request.url.path == qoder_events.EP_CHECKIN_STATUS:
            return httpx.Response(200, json=_status_payload("CLAIMABLE"))
        assert request.url.path == qoder_events.EP_CHECKIN_CLAIM
        return httpx.Response(200, json={"success": True, "rewardCredits": 120})

    result = await handler_for(claimable).checkin(cred())
    assert result.ok is True and result.credit == 120.0
    assert result.status is not None and result.status.streak_days == 3

    def conflict(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(405)
        if request.url.path == qoder_events.EP_CHECKIN_STATUS:
            return httpx.Response(200, json=_status_payload("CLAIMABLE"))
        return httpx.Response(409, content=b'{"result":"ALREADY_CLAIMED"}')

    conflicted = await handler_for(conflict).checkin(cred())
    assert conflicted.ok is True and conflicted.already_checked_in is True
    assert conflicted.code == 409

    def marker(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(404)
        if request.url.path == qoder_events.EP_CHECKIN_STATUS:
            return httpx.Response(200, json=_status_payload("CLAIMABLE"))
        return httpx.Response(200, json={"result": "ALREADY_CLAIMED"})

    assert (await handler_for(marker).checkin(cred())).already_checked_in is True


async def test_checkin_unavailable_region_is_not_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        # 活动制与旧接口都不存在：才算本区域无签到
        assert request.url.path in (qoder_events.EP_CAMPAIGNS,
                                    qoder_events.EP_CHECKIN_STATUS)
        return httpx.Response(404, content=b"not found")

    result = await handler_for(handler).checkin(cred(realm="intl"))
    assert result.ok is False and result.already_checked_in is False
    assert "本区域未开放签到接口" in result.message
    assert 404 in CHECKIN_UNAVAILABLE_STATUS


async def test_checkin_legacy_failure_paths():
    def inactive(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(404)
        return httpx.Response(200, json=_status_payload("DISABLED"))

    inactive_result = await handler_for(inactive).checkin(cred())
    assert inactive_result.ok is True
    assert inactive_result.message == "官方签到活动未开放"

    def server_error(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(404)
        if request.url.path == qoder_events.EP_CHECKIN_STATUS:
            return httpx.Response(200, json=_status_payload("CLAIMABLE"))
        return httpx.Response(500, content=b"boom")

    with pytest.raises(UpstreamHTTPError):
        await handler_for(server_error).checkin(cred())

    def rejected(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(404)
        if request.url.path == qoder_events.EP_CHECKIN_STATUS:
            return httpx.Response(200, json=_status_payload("CLAIMABLE"))
        return httpx.Response(200, json={"success": False, "error": "denied",
                                         "code": 7})

    failed = await handler_for(rejected).checkin(cred())
    assert failed.ok is False and failed.code == 7 and failed.message == "denied"


async def test_checkin_campaign_http_server_error_propagates():
    """活动制接口非 404 类错误不吞（不误判成「本区域无签到」）。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    with pytest.raises(UpstreamHTTPError):
        await handler_for(handler).checkin(cred())


async def test_checkin_status_query_and_provider_wrapper():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_campaigns(_campaign(status="CLAIMED")))

    status, unavailable = await handler_for(handler).fetch_checkin_status(cred())
    assert unavailable == "" and status is not None
    assert status.today_checked_in is True and status.streak_days is None
    payload = status_to_dict(status)
    assert payload["today_checked_in"] is True
    assert payload["streak_days"] is None
    assert payload["activity_name"] == "Qoder 每日签到"

    from src.provider.qoder import QoderProvider

    provider = QoderProvider(client=handler_for(handler))
    assert (await provider.checkin_status({}))["streak_days"] is None
    await provider.aclose()


async def test_checkin_status_unavailable_returns_inactive_status():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    from src.provider.qoder import QoderProvider

    provider = QoderProvider(client=handler_for(handler))
    status = await provider.checkin_status({})
    assert status["active"] is False and status["today_checked_in"] is False
    await provider.aclose()


async def test_checkin_legacy_status_server_error_propagates():
    """活动制 404 回退后，旧状态接口的非 404 错误不吞。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(404)
        return httpx.Response(500, content=b"boom")

    with pytest.raises(UpstreamHTTPError):
        await handler_for(handler).fetch_checkin_status(cred())


def test_campaign_helpers_edges():
    # campaigns 缺失 / 非列表 → 无可领项
    assert qoder_events.checkin_campaign({}) is None
    assert qoder_events.checkin_campaign({"campaigns": "x"}) is None
    # 非 dict 项 + 无可领状态 / 缺 campaignId 的项都被跳过
    assert qoder_events.checkin_campaign({"campaigns": [
        1,
        {"actionType": "CLAIM_BENEFIT", "claimStatus": "CLAIMED"},
        {"actionType": "CLAIM_BENEFIT", "claimStatus": "CLAIMABLE",
         "campaignId": ""},
    ]}) is None
    picked = qoder_events.checkin_campaign({"campaigns": [
        3,
        {"actionType": "CLAIM_BENEFIT", "claimStatus": "CLAIMABLE",
         "campaignId": "x"},
    ]})
    assert picked is not None and picked["campaignId"] == "x"

    # benefit 解析：非 CREDITS 的 kind 不当积分；kind 缺失按 CREDITS
    assert qoder_events.campaign_claim_credit(
        {"benefit": {"kind": "CREDITS", "amount": 5}}) == 5.0
    assert qoder_events.campaign_claim_credit(
        {"benefit": {"kind": "RATE", "amount": 5}}) is None
    assert qoder_events.campaign_claim_credit({"benefit": {}}) is None
    assert qoder_events._campaign_amount(
        {"benefit": {"kind": "RATE", "amount": 5}}) is None

    # 状态解析：campaigns 非列表 → 未开放；CLAIMABLE 存在 → 未签
    inactive = qoder_events.checkin_status_from_campaigns({"campaigns": 1})
    assert inactive.active is False and inactive.today_checked_in is False
    mixed = qoder_events.checkin_status_from_campaigns(
        _campaigns(_campaign(status="CLAIMED"), _campaign("c-2")))
    assert mixed.today_checked_in is False and mixed.active is True

    # claim_already_done：replayed / BLOCKED 同自然人 / 其它
    assert qoder_events.claim_already_done({"replayed": True}) is True
    assert qoder_events.claim_already_done(
        {"status": "BLOCKED",
         "failureCode": "SAME_PERSON_ALREADY_CLAIMED"}) is True
    assert qoder_events.claim_already_done(
        {"status": "BLOCKED", "failureCode": "RATE_LIMITED"}) is False
    assert qoder_events.claim_already_done({"status": "CLAIMED"}) is False


# ---------------------------------------------------------------- 刷新

async def test_refresh_token_rotates_and_preserves_identity():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == qoder_events.EP_DEVICE_REFRESH
        assert "Authorization" not in request.headers     # 刷新不带旧 token
        assert json.loads(request.content) == {"refresh_token": "drt-1"}
        return httpx.Response(200, json={"token": "dt-2", "refresh_token": "drt-2",
                                         "expires_in": 3_600_000})

    client = handler_for(handler)
    session = client.sessions.get(cred())
    refreshed = await client.refresh_token(cred())
    assert refreshed["access_token"] == "dt-2"
    assert refreshed["refresh_token"] == "drt-2"
    assert refreshed["uid"] == "u-1" and refreshed["nickname"] == "Tester"
    assert refreshed["expires_at"] > 0
    assert client.sessions.get(cred(access_token="dt-2")) is not session
    await client.aclose()


async def test_refresh_token_requires_refresh_token_and_result():
    def no_token(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"refresh_token": "drt-x"})

    with pytest.raises(UpstreamProtocolViolation, match="missing refresh_token"):
        await handler_for(no_token).refresh_token(cred(refresh_token=""))

    def empty(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"refresh_token": "drt-x"})

    with pytest.raises(UpstreamProtocolViolation, match="returned no token"):
        await handler_for(empty).refresh_token(cred(access_token=""))

    def dead(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b"TOKEN_EXPIRE")

    with pytest.raises(UpstreamHTTPError) as caught:
        await handler_for(dead).refresh_token(cred())
    assert caught.value.status == 401


async def test_fetch_userinfo():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == qoder_events.EP_USERINFO
        return httpx.Response(200, json={"id": "u-1", "name": "Tester"})

    assert (await handler_for(handler).fetch_userinfo(cred()))["name"] == "Tester"


# ------------------------------------------------------------- provider

async def test_provider_classify_import_and_credential_from():
    from src.provider.qoder import QoderProvider

    provider = QoderProvider()
    assert provider.id == "qoder"
    assert provider.classify(402, b"") is ErrKind.CREDIT
    assert provider.classify(401, b"") is ErrKind.SOFT
    assert provider.classify(429, b"") is ErrKind.MODEL
    imported = provider.import_credential({"token": "t", "uid": "u", "realm": "intl"})
    assert imported["access_token"] == "t" and imported["realm"] == "intl"
    assert provider.credential_from(imported).uid == "u"


async def test_provider_stream_chat_pacer_and_error_release():
    from src.provider.qoder import QoderProvider

    class Pacer:
        def __init__(self) -> None:
            self.turns = 0
            self.released = 0

        async def wait_turn(self, key=None) -> None:
            self.turns += 1

        def release(self, key=None) -> None:
            self.released += 1

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse(inner({"content": "hi"}, finish="stop")))

    pacer = Pacer()
    provider = QoderProvider(client=handler_for(handler), pacer=pacer)
    events = [e async for e in provider.stream_chat(
        cred().to_dict(), {"messages": []}, "m")]
    assert any(e.kind is EventKind.CONTENT for e in events)
    assert pacer.turns == 1 and pacer.released == 1
    await provider.aclose()

    def boom(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"err")

    pacer2 = Pacer()
    provider2 = QoderProvider(client=handler_for(boom), pacer=pacer2)
    with pytest.raises(UpstreamHTTPError):
        [e async for e in provider2.stream_chat(cred().to_dict(), {}, "m")]
    assert pacer2.released == 1
    await provider2.aclose()


async def test_provider_models_quota_refresh_and_scopes():
    from src.provider.qoder import QoderProvider

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_MODELS.split("?")[0]:
            return httpx.Response(200, json={"chat": [{"key": "m1"}]})
        if request.url.path == qoder_events.EP_QUOTA:
            return httpx.Response(200, json={"userQuota": {"total": 10,
                                                           "remaining": 10}})
        raise AssertionError(request.url.path)

    provider = QoderProvider(client=handler_for(handler))
    assert [m.id for m in await provider.list_models(cred().to_dict())] == ["m1"]
    assert (await provider.probe_quota(cred().to_dict())).total == 10
    # 非 OAuth 凭证不刷新（原样返回）
    manual = {"access_token": "t", "auth_source": "manual", "refresh_token": "r"}
    assert await provider.refresh(manual) is manual
    assert await provider.refresh({"access_token": "t"}) == {"access_token": "t"}
    assert provider.checkin_scope({"uid": "u", "realm": "intl"}) == "intl|u"
    assert provider.checkin_scope({"realm": "intl"}) == ""
    await provider.aclose()


async def test_provider_refresh_delegates_for_oauth():
    from src.provider.qoder import QoderProvider

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token": "dt-new"})

    provider = QoderProvider(client=handler_for(handler))
    refreshed = await provider.refresh(cred().to_dict())
    assert refreshed["access_token"] == "dt-new"
    await provider.aclose()


def test_credential_key_prefers_uid():
    assert credential_key(cred()) == credential_key(cred(access_token="other"))
    assert credential_key(cred(uid="")) != credential_key(cred())


async def test_client_lazy_clients_and_aclose():
    client = QoderClient()
    assert client._stream is client._stream       # noqa: SLF001
    assert client._short is client._short         # noqa: SLF001
    assert client.host == "https://openapi.qoder.com.cn"
    assert client.gateway == "https://gateway.qoder.com.cn"
    await client.aclose()
    await QoderClient().aclose()                  # 未创建过：不报错


def test_realm_configs_and_gateway_candidates():
    cn = qoder_events.get_realm_config("cn")
    # issue #3：cn 授权页参数对齐官方 CN CLI 1.1.32（website/client_id 更新，
    # 不带 redirect_uri）；domain 仍是 qoder.com.cn（找 machine_id 落盘文件用）
    assert cn.website == "https://qoder.cn"
    assert cn.client_id == "e883ade2-e6e3-4d6d-adf7-f92ceff5fdcb"
    assert cn.send_redirect_uri is False
    assert cn.domain == "qoder.com.cn"
    assert cn.openapi.endswith("qoder.com.cn")
    assert cn.nonce_dashed is True
    intl = qoder_events.get_realm_config("intl")
    assert intl.send_redirect_uri is False and intl.nonce_dashed is False
    assert qoder_events.get_realm_config("nope") is cn          # 未知回落国内
    assert qoder_events.gateway_candidates("intl")[1:] == ["https://api2.qoder.sh",
                                                           "https://api3.qoder.sh"]
    assert qoder_events.detect_realm_from_domain("a.qoder.sh") == "intl"
    assert qoder_events.detect_realm_from_domain("qoder.com.cn") == "cn"


def test_envelope_helpers_tolerate_malformed_input():
    assert qoder_events.decode_envelope(SSEFrame(event="", data="  ")) is None
    assert qoder_events.decode_envelope(SSEFrame(event="", data="[DONE]")) is not None
    envelope_none_body = qoder_events.decode_envelope(
        SSEFrame(event="", data=json.dumps({"statusCodeValue": 418})))
    assert envelope_none_body is not None and envelope_none_body.status == 418
    assert qoder_events.decode_envelope(
        SSEFrame(event="", data=json.dumps({"body": None}))) is None
    for bad in ("{not json", "[1,2]"):
        with pytest.raises(UpstreamProtocolViolation):
            qoder_events.decode_envelope(SSEFrame(event="", data=bad))
    with pytest.raises(UpstreamProtocolViolation):
        qoder_events.parse_inner_chunk("[]")
    with pytest.raises(UpstreamProtocolViolation):
        qoder_events.parse_inner_chunk("nope")
    # 非 200 且无 body 时把外层 JSON 当详情
    with_status = qoder_events.decode_envelope(
        SSEFrame(event="", data=json.dumps({"statusCodeValue": "bad"})))
    assert with_status is not None and with_status.status == 502


# --------------------------------------------------------- 设备码登录

async def test_oauth_start_and_poll_success():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_DEVICE_POLL:
            assert request.url.params["verifier"]
            assert request.url.params["challenge_method"] == "S256"
            return httpx.Response(200, json={"token": "dt-1", "refresh_token": "drt-1",
                                             "expires_in": 3_600_000,
                                             "user_id": "from-device"})
        assert request.url.path == qoder_events.EP_USERINFO
        assert request.headers["Authorization"] == "Bearer dt-1"
        return httpx.Response(200, json={"id": "u-9", "name": "Nick",
                                         "organization_id": "org"})

    oauth = QoderOAuth("cn", client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=None), machine_id="m-1")
    session = await oauth.start("root")
    assert session.flow == "poll" and session.interval == 5
    assert session.auth_url is not None
    assert "qoder.cn/device/selectAccounts?" in session.auth_url
    assert "client_id=" in session.auth_url
    assert "redirect_uri=" not in session.auth_url
    assert "machine_id=m-1" in session.auth_url

    result = await oauth.poll(session.state, "root")
    assert result is not None
    assert result.credential_data["access_token"] == "dt-1"
    assert result.credential_data["uid"] == "u-9"
    assert result.credential_data["realm"] == "cn"
    assert result.credential_data["auth_source"] == "oauth"
    assert result.nickname == "Nick"
    # state 已消费：再次轮询必须抛错
    with pytest.raises(UpstreamProtocolViolation, match="unknown or consumed"):
        await oauth.poll(session.state, "root")
    await oauth.aclose()


async def test_oauth_poll_pending_and_owner_checks():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    store = AuthStateStore()
    oauth = QoderOAuth("cn", client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=None), store=store,
        machine_id="m")
    session = await oauth.start("root")
    assert await oauth.poll(session.state, "root") is None        # 404 = 待授权
    # 其他用户不能轮询/取消
    with pytest.raises(UpstreamProtocolViolation, match="unknown or consumed"):
        await oauth.poll(session.state, "other")
    assert store.cancel(session.state, "other") is False
    assert store.cancel(session.state, "root") is True
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.poll(session.state, "root")
    await oauth.aclose()


async def test_oauth_poll_pending_202_and_missing_token():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(202)

    oauth = QoderOAuth("intl", client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=None), machine_id="m")
    session = await oauth.start("root")
    # intl 不带 redirect_uri，nonce 为 32 位 hex
    assert "redirect_uri" not in session.auth_url
    assert await oauth.poll(session.state, "root") is None
    await oauth.aclose()

    def no_token(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "pending"})

    oauth2 = QoderOAuth("cn", client=httpx.AsyncClient(
        transport=httpx.MockTransport(no_token), timeout=None), machine_id="m")
    session2 = await oauth2.start("root")
    assert await oauth2.poll(session2.state, "root") is None
    await oauth2.aclose()


async def test_oauth_poll_errors_and_userinfo_failure():
    def bad_status(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    oauth = QoderOAuth("cn", client=httpx.AsyncClient(
        transport=httpx.MockTransport(bad_status), timeout=None), machine_id="m")
    session = await oauth.start("root")
    with pytest.raises(UpstreamProtocolViolation, match="device poll http 500"):
        await oauth.poll(session.state, "root")
    await oauth.aclose()

    def not_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="nope")

    oauth2 = QoderOAuth("cn", client=httpx.AsyncClient(
        transport=httpx.MockTransport(not_json), timeout=None), machine_id="m")
    session2 = await oauth2.start("root")
    with pytest.raises(UpstreamProtocolViolation, match="not JSON"):
        await oauth2.poll(session2.state, "root")
    await oauth2.aclose()

    def userinfo_down(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_DEVICE_POLL:
            return httpx.Response(200, json={"token": "dt-x"})
        if request.url.path == qoder_events.EP_USERINFO:
            raise httpx.ConnectError("down", request=request)
        raise AssertionError(request.url.path)

    oauth3 = QoderOAuth("cn", client=httpx.AsyncClient(
        transport=httpx.MockTransport(userinfo_down), timeout=None), machine_id="m")
    session3 = await oauth3.start("root")
    result = await oauth3.poll(session3.state, "root")
    assert result is not None
    assert result.credential_data["uid"] == "dt-x"[:16]      # token 前缀兜底
    await oauth3.aclose()


async def test_oauth_poll_survives_userinfo_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_DEVICE_POLL:
            return httpx.Response(200, json={"token": "dt-y", "user_id": "u-y"})
        return httpx.Response(500, content=b"nope")

    oauth = QoderOAuth("cn", client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=None), machine_id="m")
    session = await oauth.start("root")
    result = await oauth.poll(session.state, "root")
    assert result is not None and result.credential_data["uid"] == "u-y"
    await oauth.aclose()


def test_auth_store_ttl_and_cleanup():
    store = AuthStateStore(ttl_seconds=10)
    state = store.begin("root", verifier="v", nonce="n", realm="cn",
                        machine_id="m", now=1000)
    assert store.owner(state, "root") is True
    assert store.reservation(state, "root") is not None
    assert store.reservation(state, "other") is None
    store.cleanup(now=1009)
    assert store.owner(state, "root") is True
    store.cleanup(now=1010)
    assert store.owner(state, "root") is False
    assert store.consume(state, "root") is False


def test_pkce_pair_shape_and_auth_url_realm_difference():
    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert "=" not in challenge
    assert challenge == base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
    fixed = pkce_pair("abc")
    assert fixed[0] == "abc"
    url = build_auth_url("cn", challenge="c", nonce="n", machine_id="m")
    assert "challenge=c" in url and "challenge_method=S256" in url
    assert "client_id=e883ade2" in url
    assert "redirect_uri" not in url
    intl = build_auth_url("intl", challenge="c", nonce="n", machine_id="m")
    assert "redirect_uri" not in intl
    assert "client_id=e883ade2" in intl


def test_machine_id_prefers_official_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    realm = "cn"
    path = tmp_path / qoder_events.get_realm_config(realm).domain / ".auth"
    path.mkdir(parents=True)
    (path / "machine_id").write_text("official-id\n", encoding="utf-8")
    assert machine_id_for(realm) == "official-id"


def test_machine_id_falls_back_to_stable_value(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    first = machine_id_for("cn")
    assert first and first == machine_id_for("cn")
    # 无 HOME 可读时也不抛
    monkeypatch.setenv("HOME", str(tmp_path / "missing"))
    assert machine_id_for("intl")


# ------------------------------------------------- 补测：闭合 qoder 包覆盖缺口


def test_machine_id_skips_empty_and_reads_second_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = qoder_events.get_realm_config("cn")
    # 第一候选存在但为空 → 继续看第二候选
    first = tmp_path / config.domain / ".auth"
    first.mkdir(parents=True)
    (first / "machine_id").write_text("   \n", encoding="utf-8")
    second = tmp_path / f".{config.domain}" / ".auth"
    second.mkdir(parents=True)
    (second / "machine_id").write_text("second-id\n", encoding="utf-8")
    assert machine_id_for("cn") == "second-id"


def test_fallback_machine_id_is_process_stable():
    value = _fallback_machine_id()
    assert value == _fallback_machine_id()
    # issue #3：官方 machine_id 是带横线 UUID 形态，回落值必须同形态
    parts = value.split("-")
    assert [len(part) for part in parts] == [8, 4, 4, 4, 12]
    assert all(ch in "0123456789abcdef-" for ch in value)


def test_auth_url_without_client_id_and_build_variants():
    # 显式构造一个不发送 client_id 的区域配置（覆盖分支）
    realm = "custom"
    config = qoder_events.RealmConfig(
        name="custom", openapi="https://o.test", gateway="https://g.test",
        website="https://w.test", client_id="cid", redirect_uri="r",
        domain="d.test", user_agent="ua", send_client_id=False)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setitem(qoder_events.REALM_CONFIGS, realm, config)
    try:
        url = build_auth_url(realm, challenge="c", nonce="n", machine_id="m")
        assert "client_id" not in url and "machine_id" not in url
    finally:
        monkeypatch.undo()


def test_auth_url_redirect_uri_switch():
    # issue #3 后所有内置区域都不发 redirect_uri；开关分支用注入配置覆盖
    # （上游参数若再变，改 RealmConfig 即可恢复）
    config = qoder_events.RealmConfig(
        name="legacy", openapi="https://o.test", gateway="https://g.test",
        website="https://w.test", client_id="cid", redirect_uri="app://cb",
        domain="d.test", user_agent="ua", send_redirect_uri=True)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setitem(qoder_events.REALM_CONFIGS, "legacy", config)
    try:
        url = build_auth_url("legacy", challenge="c", nonce="n", machine_id="m")
        assert "redirect_uri=app%3A%2F%2Fcb" in url
    finally:
        monkeypatch.undo()


async def test_oauth_lazy_http_and_aclose_without_client(monkeypatch):
    oauth = QoderOAuth("cn", machine_id="m")
    assert oauth._client is None                              # noqa: SLF001
    created: list[httpx.AsyncClient] = []

    class _Stub:
        def __init__(self, *args, **kwargs):
            created.append(self)

        async def aclose(self):
            created.append("closed")

    monkeypatch.setattr(qoder_auth.httpx, "AsyncClient", _Stub)
    assert oauth._http is oauth._http                          # noqa: SLF001
    assert len(created) == 1
    await oauth.aclose()
    assert created[-1] == "closed"


async def test_oauth_aclose_noop_without_client():
    oauth = QoderOAuth("cn", machine_id="m")
    await oauth.aclose()                                      # 未创建过：不报错


async def test_oauth_poll_missing_token_and_concurrent_consume():
    def missing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token": "  "})      # 授权完成但无 token

    oauth = QoderOAuth("cn", client=httpx.AsyncClient(
        transport=httpx.MockTransport(missing), timeout=None), machine_id="m")
    session = await oauth.start("root")
    assert await oauth.poll(session.state, "root") is None
    await oauth.aclose()

    # 拿到 token 但 state 已被并发消费 → 抛协议违规
    def ok(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_DEVICE_POLL:
            return httpx.Response(200, json={"token": "dt-1", "user_id": "u"})
        return httpx.Response(200, json={"id": "u"})

    store = AuthStateStore()
    oauth2 = QoderOAuth("cn", client=httpx.AsyncClient(
        transport=httpx.MockTransport(ok), timeout=None), store=store,
        machine_id="m")
    session2 = await oauth2.start("root")
    real_consume = store.consume

    def racy(state: str, username: str) -> bool:
        real_consume(state, username)                         # 先被别处消费
        return real_consume(state, username)

    store.consume = racy                                      # type: ignore[method-assign]
    with pytest.raises(UpstreamProtocolViolation, match="concurrently"):
        await oauth2.poll(session2.state, "root")
    await oauth2.aclose()


async def test_oauth_userinfo_non_200_and_non_object(monkeypatch):
    def not_200(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_DEVICE_POLL:
            return httpx.Response(200, json={"token": "dt-z", "user_id": "u"})
        return httpx.Response(503, content=b"down")

    oauth = QoderOAuth("cn", client=httpx.AsyncClient(
        transport=httpx.MockTransport(not_200), timeout=None), machine_id="m")
    session = await oauth.start("root")
    result = await oauth.poll(session.state, "root")
    assert result is not None and result.credential_data["uid"] == "u"
    await oauth.aclose()

    def not_object(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_DEVICE_POLL:
            return httpx.Response(200, json={"token": "dt-z"})
        return httpx.Response(200, json=[1, 2])

    oauth2 = QoderOAuth("cn", client=httpx.AsyncClient(
        transport=httpx.MockTransport(not_object), timeout=None), machine_id="m")
    session2 = await oauth2.start("root")
    with pytest.raises(UpstreamProtocolViolation, match="not an object"):
        await oauth2.poll(session2.state, "root")
    await oauth2.aclose()


def test_pkcs7_unpad_rejects_bad_input():
    with pytest.raises(ValueError, match="block aligned"):
        _pkcs7_unpad(b"")
    with pytest.raises(ValueError, match="block aligned"):
        _pkcs7_unpad(b"short")
    with pytest.raises(ValueError, match="bad PKCS7 padding"):
        _pkcs7_unpad(b"A" * 15 + b"\x00")                    # pad=0 非法
    with pytest.raises(ValueError, match="bad PKCS7 padding"):
        _pkcs7_unpad(b"A" * 15 + b"\x05")                    # 尾部不足 pad


def test_credential_from_dict_rejects_non_dict():
    assert QoderCredential.from_dict(None).realm == "cn"      # type: ignore[arg-type]


def test_classify_error_code_full_table():
    assert qoder_events.classify_error_code(400) is ErrKind.INVALID
    assert qoder_events.classify_error_code(404) is ErrKind.INVALID
    assert qoder_events.classify_error_code(422) is ErrKind.INVALID
    assert qoder_events.classify_error_code(999) is ErrKind.OTHER
    assert qoder_events.classify_error_code(None) is ErrKind.OTHER


def test_node_failure_400_is_model_scoped_transient():
    """上游把自身节点故障包成 400：归模型级瞬时故障，不能当「模型不存在」。"""
    body = ('{"code":"400","message":"[FAIL]node:oa_qwen-plus-main '
            'msg:Execution failed: null"}')
    assert qoder_events.is_node_failure(body) is True
    assert qoder_events.is_node_failure("model not found") is False

    # 字符串与 bytes 两种入参都支持（信封走 str，HTTP body 走 bytes）
    assert qoder_events.classify_error_code(400, body) is ErrKind.MODEL
    assert qoder_events.classify_error_code(400, body.encode()) is ErrKind.MODEL
    assert qoder_events.classify_error_code(404, body) is ErrKind.MODEL
    # 普通 400（无节点故障标记）仍是 INVALID
    assert qoder_events.classify_error_code(400, "bad request") is ErrKind.INVALID
    assert qoder_events.classify_status(400, body.encode()) is ErrKind.MODEL


def test_to_status_variants():
    assert qoder_events._to_status(None) == 200
    assert qoder_events._to_status(True) == 502
    assert qoder_events._to_status(418) == 418
    assert qoder_events._to_status("429") == 429
    assert qoder_events._to_status("nope") == 502
    assert qoder_events._to_status([1]) == 502


def test_gateway_candidates_deduplicates():
    config = qoder_events.RealmConfig(
        name="dup", openapi="o", gateway="https://g", website="w", client_id="c",
        redirect_uri="r", domain="d", user_agent="ua",
        gateway_fallbacks=("https://g", "https://h"))
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setitem(qoder_events.REALM_CONFIGS, "dup", config)
    try:
        assert qoder_events.gateway_candidates("dup") == ["https://g", "https://h"]
    finally:
        monkeypatch.undo()


def test_clean_delta_strips_noise_and_blank_calls():
    cleaned = qoder_events.clean_delta({
        "content": "x", "extra_fields": {}, "refusal": None,
        "reasoning_content": "", "tool_calls": [],
        "function_call": {"name": "", "arguments": ""}})
    assert cleaned == {"content": "x"}
    # 有实际参数/名称的调用必须保留
    kept = qoder_events.clean_delta({
        "function_call": {"name": "f", "arguments": "{}"},
        "tool_calls": [{"id": "1", "function": {"name": "f", "arguments": "{}"}}]})
    assert "function_call" in kept and "tool_calls" in kept


def test_blank_function_call_and_tool_calls_helpers():
    assert qoder_events._is_blank_function_call(None) is True
    assert qoder_events._is_blank_function_call("x") is False
    assert qoder_events._is_blank_function_call({"name": "f", "arguments": ""}) is False
    assert qoder_events._is_blank_function_call({"arguments": {}}) is True
    # _tool_calls：旧式 function_call + 过滤空调用
    calls = qoder_events._tool_calls({
        "tool_calls": ["junk", {"function": {"name": "", "arguments": ""}},
                       {"id": "1", "function": {"name": "f", "arguments": "{}"}}],
        "function_call": {"name": "g", "arguments": "{}"}})
    assert [c["function"]["name"] for c in calls] == ["f", "g"]


def test_first_choice_error_paths():
    # first_choice 已抽到 provider/openai_chunk（多 provider 共用），经 qoder 命名空间仍可直达
    assert qoder_events.first_choice({}) is None
    with pytest.raises(UpstreamProtocolViolation, match="not an array"):
        qoder_events.first_choice({"choices": "x"})
    assert qoder_events.first_choice({"choices": []}) is None
    with pytest.raises(UpstreamProtocolViolation, match="choices\\[0\\]"):
        qoder_events.first_choice({"choices": [1]})


def test_parse_models_requires_chat_list():
    with pytest.raises(UpstreamProtocolViolation, match="missing chat list"):
        qoder_events.parse_models({"chat": "x"})


def test_parse_models_maps_price_factor_to_credit_rate():
    """`price_factor` 即官方「Credit 消耗倍率」，映射为 credit_rate。

    来源：docs.qoder.com/zh/cli/model 的「Credit 消耗倍率」表脚注明说
    「表中倍率来自当前服务端模型列表的 price_factor」。免费模型上游给
    0.0（显示「免费」）；缺失或非数值时留 None，不编造。
    """
    models = qoder_events.parse_models({"chat": [
        {"key": "qmodel_38max", "display_name": "Qwen3.8-Max", "price_factor": 0.2},
        {"key": "qfmodel", "display_name": "Qwen3.8-Flash", "price_factor": 0.0},
        {"key": "nofactor"},                          # 无 price_factor → None
        {"key": "badfactor", "price_factor": "x"},    # 非数值 → None
        {"key": "boolfactor", "price_factor": True},  # 布尔不算数值 → None
    ]})
    by_id = {m.id: m for m in models}
    assert by_id["qmodel_38max"].credit_rate == 0.2
    assert by_id["qfmodel"].credit_rate == 0.0        # 免费
    assert by_id["nofactor"].credit_rate is None
    assert by_id["badfactor"].credit_rate is None
    assert by_id["boolfactor"].credit_rate is None


def test_usage_cached_tokens_fallback():
    usage = qoder_events._usage({"prompt_tokens": 5, "cached_tokens": 2})
    assert usage.cached_tokens == 2 and usage.input_tokens == 5
    # details 里的 cached_tokens 优先；非 int 回落到顶层
    usage2 = qoder_events._usage({"prompt_tokens_details": {"cached_tokens": True},
                                  "cached_tokens": 7})
    assert usage2.cached_tokens == 7


def test_claimed_today_edges():
    now = int(time.time())
    assert qoder_events._claimed_today("x", now) is False
    assert qoder_events._claimed_today(True, now) is False
    assert qoder_events._claimed_today(0, now) is False
    assert qoder_events._claimed_today(now, now) is True


def test_opt_helpers_edges():
    assert qoder_events._opt_float(True) is None
    assert qoder_events._opt_float(1) == 1.0
    assert qoder_events._opt_epoch(True) is None
    assert qoder_events._opt_epoch(0) is None
    assert qoder_events._opt_epoch(2000000000) == 2000000000
    assert qoder_events._opt_bool(1) is None and qoder_events._opt_bool(True) is True


def test_client_gateway_hosts_and_fallbacks(monkeypatch):
    client = QoderClient(host=OPENAPI, gateway="https://api1.qoder.sh")
    assert client._gateway_hosts(cred(realm="intl")) == [        # noqa: SLF001
        "https://api1.qoder.sh", "https://api2.qoder.sh", "https://api3.qoder.sh"]
    # client gateway 覆盖区域主选 → 替换首项
    override = QoderClient(host=OPENAPI, gateway="https://custom.test")
    assert override._gateway_hosts(cred(realm="intl"))[0] == "https://custom.test"  # noqa: SLF001
    # 显式 fallbacks 覆盖一切
    forced = QoderClient(host=OPENAPI, gateway="g",
                         gateway_fallbacks=("https://only.test",))
    assert forced._gateway_hosts(cred()) == ["https://only.test"]   # noqa: SLF001
    # 区域无候选主机时回落 client.gateway（防御兜底）
    monkeypatch.setattr(qoder_client, "gateway_candidates", lambda realm: [])
    assert forced._gateway_hosts(cred()) == ["https://only.test"]   # noqa: SLF001
    bare = QoderClient(host=OPENAPI, gateway="https://solo.test")
    assert bare._gateway_hosts(cred()) == ["https://solo.test"]     # noqa: SLF001


async def test_client_parse_json_response_non_object_and_error():
    def non_object(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2])

    with pytest.raises(UpstreamProtocolViolation, match="unexpected response shape"):
        await handler_for(non_object).fetch_userinfo(cred())


async def test_fetch_checkin_status_reraises_non_unavailable_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    with pytest.raises(UpstreamHTTPError):
        await handler_for(handler).fetch_checkin_status(cred())


def test_client_opt_helpers():
    assert qoder_client._opt_int(True) is None
    assert qoder_client._opt_int(3) == 3
    assert qoder_client._opt_float(True) is None
    assert qoder_client._opt_float(2) == 2.0


async def test_stream_host_switches_on_second_gateway_failure():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host in ("gw1.test", "gw2.test"):
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, text=sse(inner({"content": "ok"}, finish="stop")))

    client = handler_for(handler, gateway_fallbacks=("https://gw1.test",
                                                     "https://gw2.test",
                                                     "https://gw3.test"))
    events = [e async for e in client.stream_chat(cred(), {}, "m")]
    assert [e.kind for e in events] == [EventKind.CONTENT, EventKind.FINISH]
    assert seen == ["gw1.test", "gw2.test", "gw3.test"]
    await client.aclose()


def test_cosy_session_cache_clear_and_invalidate_missing():
    cache = CosySessionCache(temp_key_factory=lambda: "0123456789abcdef")
    cache.invalidate("nobody")                                # 不存在也不抛
    cache.clear()


async def test_provider_checkin_and_status_without_client_pacer():
    from src.provider.qoder import QoderProvider

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(200, json=_campaigns(_campaign("p-1")))
        return httpx.Response(200, json={"status": "CLAIMED", "replayed": False,
                                         "benefit": {"amount": 5}})

    provider = QoderProvider(client=handler_for(handler))     # pacer=None
    result = await provider.checkin(cred().to_dict())
    assert result.ok is True and result.credit == 5.0
    await provider.aclose()


async def test_provider_stream_chat_without_pacer():
    from src.provider.qoder import QoderProvider

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse(inner({"content": "hi"}, finish="stop")))

    provider = QoderProvider(client=handler_for(handler))     # pacer=None
    events = [e async for e in provider.stream_chat(cred().to_dict(), {}, "m")]
    assert any(e.kind is EventKind.CONTENT for e in events)
    await provider.aclose()


def test_detect_realm_from_plain_qoder_com():
    assert qoder_events.detect_realm_from_domain("www.qoder.com") == "intl"


def test_parse_inner_chunk_emits_tool_calls():
    events = qoder_events.parse_inner_chunk(json.dumps({
        "choices": [{"delta": {"tool_calls": [
            {"id": "1", "function": {"name": "f", "arguments": "{}"}}]}}]}))
    assert [e.kind for e in events] == [EventKind.TOOL_CALLS]
    assert events[0].tool_calls[0]["function"]["name"] == "f"


def test_usage_cached_tokens_from_details_preferred():
    usage = qoder_events._usage({"prompt_tokens_details": {"cached_tokens": 4}})
    assert usage.cached_tokens == 4


async def test_stream_chat_succeeds_on_middle_gateway():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "gw1.test":
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, text=sse(inner({"content": "ok"}, finish="stop")))

    client = handler_for(handler, gateway_fallbacks=("https://gw1.test",
                                                     "https://gw2.test",
                                                     "https://gw3.test"))
    events = [e async for e in client.stream_chat(cred(), {}, "m")]
    assert [e.kind for e in events] == [EventKind.CONTENT, EventKind.FINISH]
    assert seen == ["gw1.test", "gw2.test"]                   # 中途主机成功即止
    await client.aclose()


async def test_oauth_fetch_userinfo_without_token_returns_empty():
    oauth = QoderOAuth("cn", machine_id="m")
    assert await oauth._fetch_userinfo(QoderCredential()) == {}   # noqa: SLF001


async def test_oauth_poll_rejects_empty_token_after_device(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_DEVICE_POLL:
            return httpx.Response(200, json={"token": "dt-1", "user_id": "u"})
        return httpx.Response(200, json={"id": "u"})

    oauth = QoderOAuth("cn", client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=None), machine_id="m")
    session = await oauth.start("root")
    # 模拟上游返回的 token 无法归一（deviceToken 形状漂移）→ 防御兜底
    monkeypatch.setattr(qoder_auth, "credential_from_device",
                        lambda *a, **k: QoderCredential())
    with pytest.raises(UpstreamProtocolViolation, match="missing token"):
        await oauth.poll(session.state, "root")
    await oauth.aclose()

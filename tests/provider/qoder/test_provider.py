"""QoderProvider 委托与惰性客户端。"""

from __future__ import annotations

import json

import httpx
import pytest

from src.engine.sse import SSEFrame
from src.provider.base import ErrKind, EventKind
from src.provider.qoder import events as qoder_events
from src.provider.qoder.client import QoderClient, UpstreamHTTPError
from src.provider.qoder.events import UpstreamProtocolViolation
from tests.provider.qoder._support import campaign as _campaign
from tests.provider.qoder._support import campaigns as _campaigns
from tests.provider.qoder._support import cred, handler_for, inner, sse


async def test_provider_classify_import_and_credential_from():
    from src.provider.qoder import QoderProvider

    provider = QoderProvider()
    assert provider.id == "qoder"
    assert provider.classify(402, b"") is ErrKind.CREDIT
    assert provider.classify(401, b"") is ErrKind.DEAD
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

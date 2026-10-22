"""officeAce Provider 门面：凭证导入、节流、分类、签到隔离键。"""

from __future__ import annotations

import httpx
import pytest

from src.provider.base import ErrKind, EventKind, UpstreamProtocolViolation
from src.provider.officeace import OfficeAceProvider, credential_key
from src.provider.officeace.client import OfficeAceClient, UpstreamHTTPError
from tests.helpers.http import chunk_payload as _chunk
from tests.helpers.http import sse_text_done
from tests.provider.officeace._support import MOMENT
from tests.provider.officeace._support import client as _client
from tests.provider.officeace._support import cred as _cred


async def _empty_stream(handler, **kw):
    client = _client(handler, **kw)
    return [event async for event in client.stream_chat(_cred(), {}, "m")]


# ------------------------------------------------------------------ 身份

def test_credential_key_prefers_account_id():
    assert credential_key(_cred(account_id="acct")) == credential_key(
        _cred(account_id="acct", model_app_key="other"))
    assert credential_key(_cred(account_id="")) == credential_key(
        _cred(account_id="", model_app_key="AK"))


def test_credential_key_is_channel_scoped():
    """跨渠道同名账号不能共桶：否则两个上游的请求会互相排队。"""
    from src.tasks.pacer import stable_key

    assert credential_key(_cred(account_id="a")) == stable_key("officeace", "a")


def test_provider_defaults():
    provider = OfficeAceProvider()
    assert provider.id == "officeace"
    assert isinstance(provider.client, OfficeAceClient)
    assert provider.pacer is None


# ------------------------------------------------------------------ 导入

def _importable(**over) -> dict:
    base = {
        "model_app_key": "K", "model_app_secret": "S",
        "model_base_url": "https://gw.test/v2",
    }
    base.update(over)
    return base


def test_import_credential_minimal_chat_only():
    """只有聊天凭据也允许：那种凭证能聊天但查不到积分（与现状一致）。"""
    provider = OfficeAceProvider()
    saved = provider.import_credential(_importable())
    assert saved["model_app_key"] == "K"
    assert saved["model_base_url"] == "https://gw.test/v2"
    assert saved["access_key_id"] == ""


def test_import_credential_keeps_full_set():
    provider = OfficeAceProvider()
    saved = provider.import_credential({
        **_importable(), "access_key_id": "AK", "secret_access_key": "SK",
        "security_token": "T", "project_id": "pid", "refresh_token": "rt",
    })
    assert saved["access_key_id"] == "AK"
    assert saved["project_id"] == "pid"


def test_import_credential_rejects_non_object():
    with pytest.raises(UpstreamProtocolViolation):
        OfficeAceProvider().import_credential("nope")  # type: ignore[arg-type]


@pytest.mark.parametrize("over, hint", [
    ({"model_app_key": ""}, "model_app_key"),
    ({"model_app_secret": ""}, "model_app_key"),
    ({"model_base_url": ""}, "model_base_url"),
])
def test_import_credential_requires_chat_fields(over, hint):
    with pytest.raises(UpstreamProtocolViolation) as caught:
        OfficeAceProvider().import_credential(_importable(**over))
    assert hint in str(caught.value)


def test_import_credential_validates_dpop_key():
    """坏 JWK 只有到刷新才暴露，而那时用户已以为登录成功。"""
    with pytest.raises(UpstreamProtocolViolation) as caught:
        OfficeAceProvider().import_credential(
            _importable(dpop_private_jwk={"kty": "EC", "d": "bad"}))
    assert "invalid dpop_private_jwk" in str(caught.value)


def test_import_credential_skips_empty_dpop_key():
    saved = OfficeAceProvider().import_credential(_importable())
    assert saved["dpop_private_jwk"] == {}


def test_credential_from_roundtrips():
    provider = OfficeAceProvider()
    original = _cred()
    assert provider.credential_from(original.to_dict()) == original


def test_classify_delegates_to_events():
    provider = OfficeAceProvider()
    assert provider.classify(403, b"ModelArts.81004") is ErrKind.BLOCKED
    assert provider.classify(401, b"APIG.1009") is ErrKind.DEAD


# ------------------------------------------------------------------ 执行

async def test_stream_chat_passes_events_through():
    provider = OfficeAceProvider(client=_client(lambda request: httpx.Response(
        200, text=sse_text_done(_chunk({"content": "hi"})))))
    events = [event async for event in provider.stream_chat(
        _cred().to_dict(), {"messages": []}, "m")]
    assert [event.kind for event in events] == [EventKind.CONTENT]


class _RecordingPacer:
    def __init__(self) -> None:
        self.waited: list[str] = []
        self.released: list[str] = []

    async def wait_turn(self, key: str) -> None:
        self.waited.append(key)

    def release(self, key: str) -> None:
        self.released.append(key)


async def test_stream_chat_uses_account_scoped_pacer():
    pacer = _RecordingPacer()
    provider = OfficeAceProvider(
        client=_client(lambda request: httpx.Response(
            200, text=sse_text_done(_chunk({"content": "hi"})))),
        pacer=pacer)
    async for _ in provider.stream_chat(_cred().to_dict(), {}, "m"):
        pass
    assert pacer.waited == [credential_key(_cred())]
    assert pacer.released == pacer.waited


async def test_stream_chat_releases_pacer_on_error():
    """异常路径也必须归还名额，否则该凭证被当成永远在途而失去节流。"""
    pacer = _RecordingPacer()
    provider = OfficeAceProvider(
        client=_client(lambda request: httpx.Response(403, content=b"81004")),
        pacer=pacer)
    with pytest.raises(UpstreamHTTPError):
        async for _ in provider.stream_chat(_cred().to_dict(), {}, "m"):
            pass
    assert pacer.released == pacer.waited


async def test_stream_chat_releases_pacer_on_early_break():
    """客户端提前断开：`aclose_stream` 后才能归还（见 base.aclose_stream）。"""
    pacer = _RecordingPacer()
    provider = OfficeAceProvider(
        client=_client(lambda request: httpx.Response(
            200, text=sse_text_done(_chunk({"content": "a"}),
                                    _chunk({"content": "b"})))),
        pacer=pacer)
    stream = provider.stream_chat(_cred().to_dict(), {}, "m")
    await stream.__anext__()
    await stream.aclose()
    assert pacer.released == pacer.waited


async def test_list_models_delegates():
    provider = OfficeAceProvider(client=_client(lambda request: httpx.Response(
        200, json={"data": [{"id": "m"}]})))
    models = await provider.list_models(_cred().to_dict())
    assert [model.id for model in models] == ["m"]


# ------------------------------------------------------------------ 额度

async def test_probe_quota_delegates():
    provider = OfficeAceProvider(client=_client(lambda request: httpx.Response(
        200, json={"bonus_skus": [{"activity_id": "d", "points": "100",
                                   "current_value": "0"}]})))
    quota = await provider.probe_quota(_cred().to_dict())
    assert quota.remaining == 100.0
    assert quota.probed_at == MOMENT


# ------------------------------------------------------------------ 刷新

async def test_refresh_returns_rotated_dict():
    provider = OfficeAceProvider(client=_client(lambda request: httpx.Response(
        200, json={"credentials": {"access_key_id": "AK2",
                                   "secret_access_key": "SK2"},
                   "refresh_token": "rt2"})))
    refreshed = await provider.refresh(_cred().to_dict())
    assert refreshed["access_key_id"] == "AK2"
    assert refreshed["refresh_token"] == "rt2"
    assert refreshed["model_app_key"] == "AK"


# ------------------------------------------------------------------ 签到

async def test_checkin_delegates():
    claimed = {"bonus_skus": [{"activity_id": "d", "points": "100",
                               "current_value": "0"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        # 签到前快照为空 → 增量即「全算新领」
        if request.method == "POST":
            return httpx.Response(200, json=claimed)
        return httpx.Response(200, json={"bonus_skus": []})

    result = await OfficeAceProvider(client=_client(handler)).checkin(
        _cred().to_dict())
    assert result.ok is True and result.credit == 100.0


async def test_checkin_status_delegates():
    provider = OfficeAceProvider(client=_client(lambda request: httpx.Response(
        200, json={"bonus_skus": []})))
    status = await provider.checkin_status(_cred().to_dict())
    assert status["active"] is False


def test_checkin_scope_is_account_scoped():
    provider = OfficeAceProvider()
    assert provider.checkin_scope(_cred().to_dict()) == "officeace|acct-1"
    assert provider.checkin_scope(_cred(account_id="").to_dict()) == ""


def test_checkin_scope_unknown_identity_returns_empty():
    """返回空串而非 `officeace|`：后者会让同渠道所有未知身份凭证共用一个
    scope，`CheckinTask` 的 seen 集合只跑第一个账号。"""
    provider = OfficeAceProvider()
    assert provider.checkin_scope({}) == ""


# ------------------------------------------------------------------ 资源

async def test_aclose_closes_client():
    client = _client(lambda request: httpx.Response(200))
    stream = client._stream()
    await OfficeAceProvider(client=client).aclose()
    assert stream.is_closed

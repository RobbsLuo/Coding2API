"""灵犀 Provider 门面：导入、探活、签到、模型与 run 重建。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from src.provider.base import (
    CheckinResult,
    ErrKind,
    Event,
    EventKind,
    Model,
    Quota,
    UpstreamProtocolViolation,
)
from src.provider.lingxi import LingxiProvider
from src.provider.lingxi.client import RunExpired, UpstreamHTTPError


class FakeClient:
    """只实现门面用到的接口；调用记录便于断言（不替实现真实 HTTP）。"""

    def __init__(self, *, quota=None, models=None, events=None,
                 checkin=None, status=None, errors=None) -> None:
        self.quota = quota
        self.models = models or []
        self._events = events or [Event(kind=EventKind.CONTENT, content="x")]
        self._checkin = checkin
        self._status = status or {}
        self._errors = list(errors or [])
        self.calls: list[Any] = []
        self.forgotten: list[Any] = []
        self.closed = False

    async def probe_quota(self, credential):
        self.calls.append(("probe", credential))
        return self.quota

    async def fetch_models(self, credential):
        self.calls.append(("models", credential))
        return self.models

    async def checkin(self, credential):
        self.calls.append(("checkin", credential))
        if self._checkin is None:
            raise AssertionError("unexpected checkin")
        return self._checkin

    async def checkin_status(self, credential):
        self.calls.append(("status", credential))
        return self._status

    async def stream_chat(self, credential, payload, model) -> AsyncIterator[Event]:
        self.calls.append(("stream", credential, payload, model))
        if self._errors:
            raise self._errors.pop(0)
        for event in self._events:
            yield event

    def forget_run(self, credential, model):
        self.forgotten.append((credential, model))

    async def aclose(self):
        self.closed = True


def _provider(client) -> LingxiProvider:
    return LingxiProvider(client=client)


def test_import_credential_normalizes_cookie():
    provider = _provider(FakeClient())
    raw = provider.import_credential({"cookie": "wps_sid=abc; uid=9"})
    assert raw == {"wps_sid": "abc", "uid": "9"}
    with pytest.raises(UpstreamProtocolViolation):
        provider.import_credential({"uid": "1"})


def test_classify_forwards_status():
    assert _provider(FakeClient()).classify(401, b"") is ErrKind.DEAD


def test_login_identity_and_scope():
    provider = _provider(FakeClient())
    assert provider.login_identity({"wps_sid": "abc", "uid": "42"}) == "42"
    assert provider.checkin_scope({"wps_sid": "abc", "uid": "42"}) == "lingxi|42"
    assert provider.checkin_scope({}) == ""


async def test_probe_quota_without_sid_is_unknown():
    quota = await _provider(FakeClient()).probe_quota({})
    assert quota.probe_failed is True


async def test_probe_quota_delegates():
    expected = Quota(remaining=5, total=5, probed_at=1)
    client = FakeClient(quota=expected)
    assert await _provider(client).probe_quota({"wps_sid": "abc"}) is expected
    assert client.calls[0][1].wps_sid == "abc"


async def test_checkin_branches():
    provider = _provider(FakeClient())
    result = await provider.checkin({})
    assert not result.ok and "wps_sid" in result.message

    ok = CheckinResult(ok=True, credit=5)
    provider = _provider(FakeClient(checkin=ok))
    assert (await provider.checkin({"wps_sid": "abc"})).credit == 5

    provider = _provider(FakeClient())
    provider.client.checkin = _raiser(UpstreamHTTPError(401, b""))
    result = await provider.checkin({"wps_sid": "abc"})
    assert not result.ok and "登录态" in result.message

    provider = _provider(FakeClient())
    provider.client.checkin = _raiser(UpstreamHTTPError(500, b""))
    result = await provider.checkin({"wps_sid": "abc"})
    assert not result.ok and "HTTP 500" in result.message

    provider = _provider(FakeClient())
    provider.client.checkin = _raiser(UpstreamProtocolViolation("bad"))
    result = await provider.checkin({"wps_sid": "abc"})
    assert not result.ok and "格式异常" in result.message


async def test_checkin_status_branches():
    provider = _provider(FakeClient())
    assert await provider.checkin_status({}) == {"active": False,
                                                 "today_checked_in": False}
    provider = _provider(FakeClient(status={"active": True}))
    assert await provider.checkin_status({"wps_sid": "abc"}) == {"active": True}


async def test_list_models_requires_sid_and_delegates():
    provider = _provider(FakeClient())
    with pytest.raises(UpstreamProtocolViolation):
        await provider.list_models({})
    expected = [Model(id="m")]
    provider = _provider(FakeClient(models=expected))
    assert await provider.list_models({"wps_sid": "abc"}) == expected


async def test_stream_chat_requires_sid():
    provider = _provider(FakeClient())
    with pytest.raises(UpstreamProtocolViolation):
        [event async for event in provider.stream_chat({}, {}, "m")]


async def test_stream_chat_passthrough():
    provider = _provider(FakeClient())
    events = [event async for event in provider.stream_chat(
        {"wps_sid": "abc"}, {"messages": []}, "m")]
    assert [event.content for event in events] == ["x"]
    assert provider.client.calls[0][0] == "stream"


async def test_stream_chat_rebuilds_run_once():
    client = FakeClient(errors=[RunExpired("会话不存在")])
    provider = _provider(client)
    events = [event async for event in provider.stream_chat(
        {"wps_sid": "abc", "uid": "42"}, {}, "m")]
    assert events and client.forgotten[0][1] == "m"
    assert len([call for call in client.calls if call[0] == "stream"]) == 2


async def test_stream_chat_gives_up_after_second_expiry():
    client = FakeClient(errors=[RunExpired("a"), RunExpired("b")])
    provider = _provider(client)
    with pytest.raises(UpstreamProtocolViolation) as error:
        [event async for event in provider.stream_chat({"wps_sid": "abc"}, {}, "m")]
    assert "反复失效" in str(error.value)


async def test_aclose_closes_client():
    client = FakeClient()
    await _provider(client).aclose()
    assert client.closed is True


def _raiser(error: BaseException):
    async def _call(credential):
        raise error
    return _call

async def test_stream_chat_generator_closed_early():
    provider = _provider(FakeClient())
    generator = provider.stream_chat({"wps_sid": "abc"}, {}, "m")
    assert (await anext(generator)).content == "x"
    await generator.aclose()

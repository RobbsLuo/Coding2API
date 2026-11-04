"""小浣熊 Provider：导入、额度、目录缓存、未知模型防护、节流。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from src.provider.base import ErrKind, Event, EventKind, Model, Quota
from src.provider.raccoon.client import (
    RaccoonProvider,
    UpstreamHTTPError,
    UpstreamProtocolViolation,
)


class FakeClient:
    """最小替身：只实现 provider 用到的三个方法（本用例不覆盖未调用项）。"""

    def __init__(self, *, models: list[Model] | None = None,
                 quota: Quota | None = None, events: list[Event] | None = None,
                 error: Exception | None = None) -> None:
        self.models = models or []
        self.quota = quota or Quota(remaining=1, total=1)
        self.events = events or []
        self.error = error
        self.calls: list[tuple[Any, ...]] = []

    async def fetch_models(self, token: str) -> list[Model]:
        self.calls.append(("models", token))
        return self.models

    async def fetch_quota(self, token: str) -> Quota:
        self.calls.append(("quota", token))
        if self.error is not None:
            raise self.error
        return self.quota

    async def stream_chat(self, payload: dict, model: str,
                          token: str) -> AsyncIterator[Event]:
        self.calls.append(("chat", model, token, payload))
        for event in self.events:
            yield event

    async def aclose(self) -> None:
        self.calls.append(("aclose",))


TOKEN = "tok"
DATA = {"access_token": TOKEN, "uid": "u", "nickname": "n", "expires_at": 5}


def test_import_credential_normalizes_and_extracts_uid():
    provider = RaccoonProvider(client=FakeClient())
    jwt = "a.b.c"
    stored = provider.import_credential({"access_token": jwt})
    assert stored["access_token"] == jwt
    # `a.b.c` 不是合法 JWT claims → uid 留空（不猜）
    assert stored["uid"] == ""


def test_import_credential_rejects_empty_token():
    provider = RaccoonProvider(client=FakeClient())
    with pytest.raises(UpstreamProtocolViolation, match="missing access token"):
        provider.import_credential({"cookie": "a=1"})


def test_credential_expires_at():
    provider = RaccoonProvider(client=FakeClient())
    assert provider.credential_expires_at(DATA) == 5
    assert provider.credential_expires_at({}) == 0


def test_classify_delegates_to_events():
    provider = RaccoonProvider(client=FakeClient())
    assert provider.classify(401, b"") is ErrKind.DEAD


async def test_probe_quota_without_token_is_unknown_not_exhausted():
    provider = RaccoonProvider(client=FakeClient())
    quota = await provider.probe_quota({})
    assert quota.probe_failed is True
    assert quota.total is None


async def test_probe_quota_reads_upstream():
    fake = FakeClient(quota=Quota(remaining=12, total=12))
    provider = RaccoonProvider(client=fake)
    quota = await provider.probe_quota(DATA)
    assert quota.remaining == 12
    assert fake.calls == [("quota", TOKEN)]


async def test_list_models_requires_token_and_updates_known_models():
    provider = RaccoonProvider(client=FakeClient())
    with pytest.raises(UpstreamProtocolViolation, match="missing token"):
        await provider.list_models({})

    fake = FakeClient(models=[Model(id="sn-GLM-5-3"), Model(id="raccoon-090dd3")])
    provider = RaccoonProvider(client=fake)
    models = await provider.list_models(DATA)
    assert [m.id for m in models] == ["sn-GLM-5-3", "raccoon-090dd3"]
    assert provider.known_models == {"sn-glm-5-3", "raccoon-090dd3"}


async def test_stream_chat_requires_token():
    provider = RaccoonProvider(client=FakeClient())
    with pytest.raises(UpstreamProtocolViolation, match="missing token"):
        [e async for e in provider.stream_chat({}, {"messages": []}, "m")]


async def test_stream_chat_rejects_model_missing_from_catalog_before_upstream():
    fake = FakeClient()
    provider = RaccoonProvider(client=fake)
    provider.known_models = {"sn-glm-5-3"}
    with pytest.raises(UpstreamHTTPError) as caught:
        [e async for e in provider.stream_chat(DATA, {"messages": []}, "raccoon-090dd3")]
    assert caught.value.status == 404
    assert caught.value.kind() is ErrKind.INVALID
    # 目录里没有 → 一次上游请求都不发
    assert fake.calls == []


async def test_stream_chat_allows_known_model_and_passes_through():
    fake = FakeClient(events=[Event(kind=EventKind.CONTENT, content="hi")])
    provider = RaccoonProvider(client=fake)
    provider.known_models = {"sn-glm-5-3"}
    events = [e async for e in provider.stream_chat(DATA, {"messages": []},
                                                    "SN-GLM-5-3")]
    assert [e.kind for e in events] == [EventKind.CONTENT]
    assert fake.calls[0][0] == "chat"
    assert fake.calls[0][1] == "SN-GLM-5-3"


async def test_stream_chat_passes_through_when_catalog_not_loaded():
    """目录还没拉过（已知集为空）时放行：真实保护靠上游回显的 model。"""
    fake = FakeClient(events=[Event(kind=EventKind.CONTENT, content="x")])
    provider = RaccoonProvider(client=fake)
    events = [e async for e in provider.stream_chat(DATA, {"messages": []}, "any")]
    assert [e.kind for e in events] == [EventKind.CONTENT]


async def test_provider_aclose_closes_client():
    fake = FakeClient()
    provider = RaccoonProvider(client=fake)
    await provider.aclose()
    assert fake.calls == [("aclose",)]


async def test_stream_chat_uses_pacer():
    """pacer 必须被取用并释放；替身只实现 wait_turn/release（与生产同接口）。"""

    class PacerSpy:
        def __init__(self) -> None:
            self.keys: list[str] = []
            self.released: list[str] = []

        async def wait_turn(self, key: Any = None) -> None:
            self.keys.append(str(key))

        def release(self, key: Any = None) -> None:
            self.released.append(str(key))

    spy = PacerSpy()
    provider = RaccoonProvider(client=FakeClient(
        events=[Event(kind=EventKind.CONTENT, content="x")]), pacer=spy)
    events = [e async for e in provider.stream_chat(DATA, {"messages": []}, "m")]
    assert [e.kind for e in events] == [EventKind.CONTENT]
    assert spy.keys == ["u"]          # 按凭证 uid 分桶
    assert spy.released == ["u"]

# ------------------------------------------------------------------ 签到 / 活跃


class CheckinFakeClient(FakeClient):
    """签到/活跃用的替身：bills 与 heartbeat 分别可控。"""

    def __init__(self, *, bills=None, heartbeat_payload=None, error=None):
        super().__init__()
        self.bills = bills if bills is not None else []
        # 显式区分「空字典」与「未传」：`or` 会把 {} 换成默认值
        self.heartbeat_payload = (heartbeat_payload if heartbeat_payload is not None
                                  else {"trusted_device": True,
                                        "last_seen_at": "2026-11-04T23:20:49+08:00"})
        self.error = error

    async def fetch_bills(self, token, limit=60):
        self.calls.append(("bills", token))
        if self.error is not None:
            raise self.error
        return self.bills

    async def heartbeat(self, token, device_id):
        self.calls.append(("heartbeat", token, device_id))
        if self.error is not None:
            raise self.error
        return self.heartbeat_payload


def _grant(points=300, created="2026-11-04T21:59:30+08:00"):
    return {"biz_type": "daily_grant", "points": points,
            "grant_at": "2026-11-04T00:00:00+08:00",
            "created_at": created, "expire_at": "2026-11-10T23:59:59+08:00"}


async def test_checkin_triggers_lazy_grant_then_reads_bills():
    fake = CheckinFakeClient(bills=[_grant()])
    provider = RaccoonProvider(client=fake)
    result = await provider.checkin(DATA)
    assert result.ok and result.credit == 300
    # 先摸余额（触发懒发放），再读流水核对
    assert fake.calls[0] == ("quota", TOKEN)
    assert fake.calls[1] == ("bills", TOKEN)


async def test_checkin_reports_already_when_grant_is_old():
    fake = CheckinFakeClient(bills=[_grant(created="2026-11-04T08:00:00+08:00")])
    provider = RaccoonProvider(client=fake)
    result = await provider.checkin(DATA)
    assert result.ok and result.already_checked_in


async def test_checkin_fails_when_no_grant_today():
    provider = RaccoonProvider(client=CheckinFakeClient(bills=[]))
    result = await provider.checkin(DATA)
    assert not result.ok and "尚未发放" in result.message


async def test_checkin_without_token():
    provider = RaccoonProvider(client=CheckinFakeClient())
    result = await provider.checkin({})
    assert not result.ok and "token" in result.message


class BillsErrorClient(FakeClient):
    """只让 bills 失败（余额探测正常）：验证流水的失败会变成可读的签到失败。"""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    async def fetch_quota(self, token):
        # 余额探测必须成功：签到先摸余额触发懒发放，失败的是随后的流水
        self.calls.append(("quota", token))
        return self.quota

    async def fetch_bills(self, token, limit=60):
        raise self.error


async def test_checkin_bills_http_error_and_protocol_violation():
    http_error = BillsErrorClient(UpstreamHTTPError(500, b"boom"))
    result = await RaccoonProvider(client=http_error).checkin(DATA)
    assert not result.ok and "HTTP 500" in result.message

    bad = BillsErrorClient(UpstreamProtocolViolation("bad"))
    result = await RaccoonProvider(client=bad).checkin(DATA)
    assert not result.ok and "格式异常" in result.message


async def test_checkin_status_reads_bills():
    """状态查询只看「今天发过没有」，不依赖本次是否触发。"""
    provider = RaccoonProvider(client=CheckinFakeClient(bills=[_grant()]))
    status = await provider.checkin_status(DATA)
    assert status["active"] is True
    assert status["today_checked_in"] is True
    assert status["today_credit"] == 300


async def test_checkin_status_when_nothing_granted_today():
    provider = RaccoonProvider(client=CheckinFakeClient(bills=[]))
    status = await provider.checkin_status(DATA)
    assert status["active"] is True
    assert status["today_checked_in"] is False
    assert status["today_credit"] is None


async def test_checkin_status_without_token():
    provider = RaccoonProvider(client=CheckinFakeClient())
    status = await provider.checkin_status({})
    assert status == {"active": False, "today_checked_in": False}


def test_checkin_scope_and_activity_scope_use_uid():
    provider = RaccoonProvider(client=FakeClient())
    assert provider.checkin_scope(DATA) == "raccoon|u"
    assert provider.activity_scope(DATA) == "raccoon|u"
    # 身份未知返回空串（调用方回落凭证 ID），绝不让所有凭证算出同一 scope
    assert provider.checkin_scope({}) == ""
    assert provider.activity_scope({}) == ""


async def test_activity_sends_stable_device_id():
    fake = CheckinFakeClient()
    provider = RaccoonProvider(client=fake)
    result = await provider.activity(DATA)
    assert result.ok and result.user_id == "u"
    call = fake.calls[0]
    assert call[0] == "heartbeat"
    # 设备 id 由 uid 稳定派生：两次调用必须一致（随机值会被当成新设备）
    assert call[2].startswith("dev")
    second = await provider.activity(DATA)
    assert second.ok and fake.calls[1][2] == call[2]


async def test_activity_without_token():
    provider = RaccoonProvider(client=CheckinFakeClient())
    result = await provider.activity({})
    assert not result.ok and "token" in result.message


async def test_activity_reports_untrusted_device():
    fake = CheckinFakeClient(heartbeat_payload={"trusted_device": False})
    result = await RaccoonProvider(client=fake).activity(DATA)
    assert result.ok and "未被上游信任" in result.message


async def test_activity_errors():
    http_error = CheckinFakeClient(error=UpstreamHTTPError(400, b"boom"))
    result = await RaccoonProvider(client=http_error).activity(DATA)
    assert not result.ok and "HTTP 400" in result.message

    bad = CheckinFakeClient(error=UpstreamProtocolViolation("bad"))
    result = await RaccoonProvider(client=bad).activity(DATA)
    assert not result.ok and "格式异常" in result.message


async def test_activity_message_without_last_seen():
    fake = CheckinFakeClient(heartbeat_payload={})
    result = await RaccoonProvider(client=fake).activity(DATA)
    assert result.ok and result.message == "设备心跳已上报"

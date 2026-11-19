"""小浣熊 Provider：导入、额度、目录缓存、未知模型防护、节流。"""

from __future__ import annotations

import json
import types
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import pytest

from src.provider.base import ErrKind, Event, EventKind, Model, Quota
from src.provider.raccoon import activity as raccoon_activity
from src.provider.raccoon import client as raccoon_client
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

    async def visit(self, token: str) -> dict:
        self.calls.append(("visit", token))
        return {}

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
    """签到/活跃用的替身：bills、visit 与 heartbeat 分别可控。"""

    def __init__(self, *, bills=None, heartbeat_payload=None, error=None,
                 visit_error=None, user_info=None):
        super().__init__()
        self.bills = bills if bills is not None else []
        # 显式区分「空字典」与「未传」：`or` 会把 {} 换成默认值
        self.heartbeat_payload = (heartbeat_payload if heartbeat_payload is not None
                                  else {"trusted_device": True,
                                        "last_seen_at": "2026-11-04T23:20:49+08:00"})
        self.error = error
        self.visit_error = visit_error
        # `user_info` 的返回：签到/活跃靠它读会话信任状态（trusted_device）
        self.user_info = user_info if user_info is not None else {}

    async def visit(self, token):
        self.calls.append(("visit", token))
        if self.visit_error is not None:
            raise self.visit_error
        return self.user_info

    async def fetch_bills(self, token, limit=60, bill_type="all"):
        self.calls.append(("bills", token, bill_type))
        if self.error is not None:
            raise self.error
        return self.bills

    async def heartbeat(self, token, device_id):
        self.calls.append(("heartbeat", token, device_id))
        if self.error is not None:
            raise self.error
        return self.heartbeat_payload

    async def page_boot(self, token, device_id):
        """页面启动序列（`checkin` 当天未到账时调用；本用例按需断言）。"""
        self.calls.append(("page_boot", token, device_id))
        return {"device_id": device_id}


def _grant(points=300, created="2026-11-04T21:59:30+08:00"):
    return {"biz_type": "daily_grant", "points": points,
            "grant_at": "2026-11-04T00:00:00+08:00",
            "created_at": created, "expire_at": "2026-11-10T23:59:59+08:00"}


#: 固定「现在」：签到结论依赖「今天」，真实时钟会让用例隔天失效
NOW = datetime.fromisoformat("2026-11-04T22:00:00+08:00")


@pytest.fixture
def frozen_now(monkeypatch):
    """把签到用到的两个时间源都钉在 NOW。

    `now_cst` 决定「今天」，`client.time.time` 决定本次调用的 started；
    两者不一致会让「本次触发」的判定飘掉。`time` 换成替身而非打补丁到
    真模块，避免影响同进程里的其他代码。
    """
    monkeypatch.setattr(raccoon_activity, "now_cst", lambda: NOW)
    monkeypatch.setattr(raccoon_client, "time",
                        types.SimpleNamespace(time=lambda: NOW.timestamp()))


async def test_checkin_returns_early_when_grant_already_present(frozen_now):
    """首次读流水就有今天这笔 → 直接判定，不再白走页面启动（省 5~6 个请求）。"""
    fake = CheckinFakeClient(bills=[_grant()])
    provider = RaccoonProvider(client=fake)
    result = await provider.checkin(DATA)
    assert result.ok and result.credit == 300
    assert fake.calls == [("visit", TOKEN), ("bills", TOKEN, "income")]


async def test_checkin_boots_page_then_rereads_bills(frozen_now):
    """首次读流水没有今天这笔 → 走一遍页面启动（每日积分的实际入账点）再核对。"""
    fake = CheckinFakeClient(bills=[])
    original = fake.fetch_bills
    state = {"n": 0}

    async def two_phase(token, limit=60, bill_type="all"):
        state["n"] += 1
        if state["n"] == 1:
            fake.calls.append(("bills", token, bill_type))
            return []
        fake.bills = [_grant()]
        return await original(token, limit=limit, bill_type=bill_type)

    fake.fetch_bills = two_phase
    result = await RaccoonProvider(client=fake).checkin(DATA)
    assert result.ok and result.credit == 300
    assert fake.calls[0] == ("visit", TOKEN)
    assert fake.calls[1] == ("bills", TOKEN, "income")
    # 页面启动用的是由 uid 派生的稳定设备 id
    assert fake.calls[2][0] == "page_boot" and fake.calls[2][1] == TOKEN
    assert fake.calls[3] == ("quota", TOKEN)


async def test_checkin_ignores_visit_failure(frozen_now):
    """页面访问失败不阻断积分核对（活跃任务还会再试一次）。"""
    fake = CheckinFakeClient(bills=[_grant()],
                             visit_error=UpstreamHTTPError(500, b"boom"))
    result = await RaccoonProvider(client=fake).checkin(DATA)
    assert result.ok and result.credit == 300


async def test_checkin_fails_when_only_unexpired_old_grants(frozen_now):
    """今天没有新发放 → 失败（昨天没花完的积分不算今天签到）。"""
    old = {**_grant(created="2026-11-03T08:00:00+08:00"),
           "grant_at": "2026-11-03T00:00:00+08:00"}
    fake = CheckinFakeClient(bills=[old])
    result = await RaccoonProvider(client=fake).checkin(DATA)
    assert not result.ok
    assert "尚未发放" in result.message


async def test_checkin_reports_relogin_when_session_untrusted(frozen_now):
    """`user_info` 说会话不被信任 → 失败文案点名「重新登录」。"""
    fake = CheckinFakeClient(bills=[], user_info={"trusted_device": {
        "trusted": False, "device_bind_state": "suspicious",
        "needs_relogin": True, "reason": "device_session_mismatch"}})
    result = await RaccoonProvider(client=fake).checkin(DATA)
    assert not result.ok
    assert "重新登录" in result.message
    assert "device_session_mismatch" in result.message


async def test_checkin_reports_already_when_grant_is_old(frozen_now):
    fake = CheckinFakeClient(bills=[_grant(created="2026-11-04T08:00:00+08:00")])
    provider = RaccoonProvider(client=fake)
    result = await provider.checkin(DATA)
    assert result.ok and result.already_checked_in


async def test_checkin_fails_when_no_grant_today(frozen_now):
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

    async def page_boot(self, token, device_id):
        """首读流水就失败 → 不会走到页面启动（本用例不调用）。"""
        self.calls.append(("page_boot", token, device_id))
        return {}

    async def fetch_bills(self, token, limit=60, bill_type="all"):
        raise self.error


async def test_checkin_bills_http_error_and_protocol_violation():
    http_error = BillsErrorClient(UpstreamHTTPError(500, b"boom"))
    result = await RaccoonProvider(client=http_error).checkin(DATA)
    assert not result.ok and "HTTP 500" in result.message

    bad = BillsErrorClient(UpstreamProtocolViolation("bad"))
    result = await RaccoonProvider(client=bad).checkin(DATA)
    assert not result.ok and "格式异常" in result.message


async def test_checkin_status_reads_bills(frozen_now):
    """状态查询只看「今天发过没有」，不依赖本次是否触发。"""
    provider = RaccoonProvider(client=CheckinFakeClient(bills=[_grant()]))
    status = await provider.checkin_status(DATA)
    assert status["active"] is True
    assert status["today_checked_in"] is True
    assert status["today_credit"] == 300


async def test_checkin_status_when_nothing_granted_today(frozen_now):
    provider = RaccoonProvider(client=CheckinFakeClient(bills=[]))
    status = await provider.checkin_status(DATA)
    assert status["active"] is True
    assert status["today_checked_in"] is False
    assert status["today_credit"] is None


async def test_checkin_status_ignores_unexpired_old_grants(frozen_now):
    """今天没有新发放 → 未签。状态查询与 `checkin` 同口径，不许假绿。"""
    old = {**_grant(created="2026-11-03T08:00:00+08:00"),
           "grant_at": "2026-11-03T00:00:00+08:00"}
    provider = RaccoonProvider(client=CheckinFakeClient(bills=[old]))
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


async def test_activity_visits_page_then_sends_stable_device_id():
    fake = CheckinFakeClient()
    provider = RaccoonProvider(client=fake)
    result = await provider.activity(DATA)
    assert result.ok and result.user_id == "u"
    # 先页面访问（触发延期），再心跳
    assert fake.calls[0] == ("visit", TOKEN)
    beats = [call for call in fake.calls if call[0] == "heartbeat"]
    assert beats and beats[0][2].startswith("dev")
    # 设备 id 由 uid 稳定派生：两次调用必须一致（随机值会被当成新设备）
    second = await provider.activity(DATA)
    beats = [call for call in fake.calls if call[0] == "heartbeat"]
    assert second.ok and beats[1][2] == beats[0][2]


async def test_activity_visit_failure_still_sends_heartbeat_but_reports_failure():
    fake = CheckinFakeClient(visit_error=UpstreamHTTPError(403, b"no"))
    result = await RaccoonProvider(client=fake).activity(DATA)
    assert not result.ok and "页面访问失败（HTTP 403）" in result.message
    # 访问失败不影响心跳照发
    assert [call[0] for call in fake.calls] == ["visit", "heartbeat"]


async def test_activity_visit_protocol_failure():
    fake = CheckinFakeClient(visit_error=UpstreamProtocolViolation("bad"))
    result = await RaccoonProvider(client=fake).activity(DATA)
    assert not result.ok and "页面访问响应格式异常" in result.message


async def test_activity_without_token():
    provider = RaccoonProvider(client=CheckinFakeClient())
    result = await provider.activity({})
    assert not result.ok and "token" in result.message


async def test_activity_reports_untrusted_device():
    fake = CheckinFakeClient(heartbeat_payload={"trusted_device": False})
    result = await RaccoonProvider(client=fake).activity(DATA)
    assert result.ok and "重新登录" in result.message


async def test_activity_short_circuits_when_session_needs_relogin():
    """`user_info` 已说会话不被信任 → 不再白发心跳，直接报可操作原因。"""
    fake = CheckinFakeClient(user_info={"trusted_device": {
        "trusted": False, "device_bind_state": "suspicious",
        "needs_relogin": True, "reason": "device_session_mismatch"}})
    result = await RaccoonProvider(client=fake).activity(DATA)
    assert not result.ok
    assert "重新登录" in result.message
    assert "device_session_mismatch" in result.message
    # 心跳在这种会话下必然 400：不该再打一次上游
    assert [call[0] for call in fake.calls] == ["visit"]


async def test_activity_visit_error_kept_when_session_needs_relogin():
    """访问失败但会话状态可疑：两条原因都要进一行汇报。"""
    fake = CheckinFakeClient(user_info={"trusted_device": {
        "trusted": False, "device_bind_state": "suspicious"}})
    result = await RaccoonProvider(client=fake).activity(DATA)
    assert not result.ok
    assert "重新登录" in result.message

async def test_activity_heartbeat_bind_required_names_relogin():
    """心跳回 `200811 device_bind_required` → 汇报里要点名重新登录。"""
    fake = CheckinFakeClient(error=UpstreamHTTPError(400, (
        b'{"code":200811,"message":"device_bind_required",'
        b'"details":"current session requires trusted device binding"}')))
    result = await RaccoonProvider(client=fake).activity(DATA)
    assert not result.ok
    assert "心跳失败（HTTP 400）" in result.message
    assert "重新登录" in result.message


async def test_activity_errors():
    http_error = CheckinFakeClient(error=UpstreamHTTPError(400, b"boom"))
    result = await RaccoonProvider(client=http_error).activity(DATA)
    assert not result.ok and "HTTP 400" in result.message

    bad = CheckinFakeClient(error=UpstreamProtocolViolation("bad"))
    result = await RaccoonProvider(client=bad).activity(DATA)
    assert not result.ok and "格式异常" in result.message


async def test_activity_heartbeat_error_keeps_visit_error_in_message():
    """访问和心跳同时失败：两条原因都要进一行汇报。"""
    http = CheckinFakeClient(error=UpstreamHTTPError(400, b"boom"),
                             visit_error=UpstreamHTTPError(403, b"no"))
    result = await RaccoonProvider(client=http).activity(DATA)
    assert not result.ok
    assert "心跳失败（HTTP 400）" in result.message
    assert "页面访问失败（HTTP 403）" in result.message

    bad = CheckinFakeClient(error=UpstreamProtocolViolation("bad"),
                            visit_error=UpstreamProtocolViolation("bad2"))
    result = await RaccoonProvider(client=bad).activity(DATA)
    assert not result.ok
    assert "心跳响应格式异常" in result.message
    assert "页面访问响应格式异常" in result.message


async def test_activity_message_without_last_seen():
    fake = CheckinFakeClient(heartbeat_payload={})
    result = await RaccoonProvider(client=fake).activity(DATA)
    assert result.ok and result.message == "页面访问已上报；设备心跳已上报"


class LoginFakeClient(CheckinFakeClient):
    """带 `login` 的替身：只有按需登录路径会用到（其余用例不调用）。"""

    def __init__(self, *, token="new-token", error=None, **kw):
        super().__init__(**kw)
        self.login_token = token
        self.login_error = error
        self.logins: list[tuple[str, str]] = []
        self.binds: list[tuple[str, str]] = []

    async def login(self, phone, password):
        self.logins.append((phone, password))
        if self.login_error is not None:
            raise self.login_error
        return {"access_token": self.login_token}

    async def bind_device(self, token, device_id):
        self.binds.append((token, device_id))
        return {"device_id": device_id}

    async def page_boot(self, token, device_id):
        """按需登录后必须走的页面启动（绑设备 + 首页加载 + 活动上报）。"""
        self.binds.append((token, device_id))
        return {"device_id": device_id}


async def test_relogin_returns_none_without_credentials():
    """凭证里没有 phone/password → 不登录（纯 token 导入的老用法照旧）。"""
    fake = LoginFakeClient()
    assert await RaccoonProvider(client=fake).relogin(DATA) is None
    assert fake.logins == []


async def test_relogin_returns_fresh_session_and_keeps_login_secret():
    """登录成功 → 新 token + 从 claims 重解 uid，phone/password 必须保留。

    不保留密码的话，下一次按需登录就没凭据了（每 30 天得让人重填一次）。
    """
    fake = LoginFakeClient(token="fresh-token")
    data = {**DATA, "phone": "13900000001", "password": "Abcd123456",
            "nickname": "elijah"}
    stored = await RaccoonProvider(client=fake).relogin(data)

    assert fake.logins == [("13900000001", "Abcd123456")]
    assert stored["access_token"] == "fresh-token"
    assert stored["phone"] == "13900000001"
    assert stored["password"] == "Abcd123456"
    assert stored["nickname"] == "elijah"
    # 新 token 不是合法 JWT（测试值）→ uid/expires_at 归零，不猜
    assert stored["uid"] == "" and stored["expires_at"] == 0


async def test_relogin_propagates_login_rejection():
    """账号密码错 → LoginRejected 向上抛（调用方据此停手并标需重登）。"""
    from src.provider.raccoon.client import LoginRejected

    fake = LoginFakeClient(error=LoginRejected("密码错误"))
    data = {**DATA, "phone": "13900000001", "password": "Abcd123456"}
    with pytest.raises(LoginRejected):
        await RaccoonProvider(client=fake).relogin(data)


async def test_relogin_binds_device_after_login():
    """登录后必须绑设备：不绑的会话 trusted=false、心跳 400、也不延期。

    用真 JWT（带 sub）才能走到绑定；设备 id 必须是派生值且 ≤64 字符
    （上游 devices_current_bind 对超长值回 500）。
    """
    import base64

    def _jwt(claims: dict) -> str:
        def seg(data: dict) -> str:
            raw = json.dumps(data).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")
        return f"{seg({'alg': 'HS256'})}.{seg(claims)}.sig"

    token = _jwt({"sub": "pu-01a1", "exp": 1796398080})
    fake = LoginFakeClient(token=token)
    data = {**DATA, "phone": "13900000001", "password": "Abcd123456"}

    stored = await RaccoonProvider(client=fake).relogin(data)

    assert stored["uid"] == "pu-01a1"
    assert len(fake.binds) == 1
    bound_token, device_id = fake.binds[0]
    assert bound_token == token
    assert device_id.startswith("dev") and len(device_id) <= 64

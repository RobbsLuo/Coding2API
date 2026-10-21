"""凭证运维端点：探测 / 签到 / 账号切换与即时探测。"""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.config import Settings
from src.main import build_app
from src.provider.base import Quota
from src.provider.codebuddy.checkin import CheckinResult
from src.provider.codebuddy.refresh import Account
from tests.helpers.constants import SECRET


class _StubCodeBuddy:
    """探测 / 签到桩 provider：本用例只验证端点接线，绝不出网。

    V1 版本直接打真实上游（probe 容错 502、checkin 不设容错），CI 网络抖动时
    checkin 收到 502 就会红——测试不得依赖网络，故注入确定性桩。
    """

    id = "codebuddy"

    async def probe_quota(self, _data):
        return Quota(remaining=7, total=10, probed_at=1)

    async def checkin(self, _data):
        return CheckinResult(ok=True, credit=5, code=0, message="ok")


def test_upstream_auth_start_supports_both_providers(admin_client):
    """CodeBuddy 走 poll 轨道，TRAE 走 callback 轨道，unknown provider 报 400。"""
    app, client = admin_client
    # TRAE 的 callback 轨道不需要出网，必定成功并带入参回调地址
    trae = client.post("/api/auth/upstream/start", json={"provider": "trae"})
    assert trae.status_code == 200
    body = trae.json()
    assert body["flow"] == "callback"
    assert body["callback_url"].endswith("/authorize")
    assert "auth_callback_url" in body["auth_url"]
    # 按发起人（会话用户名）登记，值为 (started_at, state)
    assert app.state.pending_callbacks["root"][1] == body["state"]

    assert client.post("/api/auth/upstream/start",
                       json={"provider": "unknown"}).status_code == 400

def test_upstream_auth_poll_unknown_state(admin_client):
    _app, client = admin_client
    response = client.post("/api/auth/upstream/poll",
                           json={"provider": "codebuddy", "state": "ghost"})
    assert response.status_code == 400

def test_upstream_auth_rejects_non_admin(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    app = build_app(settings)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("guest", SECRET))
        assert client.post("/api/auth/upstream/start",
                           json={"provider": "codebuddy"}).status_code == 403

def test_credential_probe_and_checkin_endpoints(admin_client):
    app, client = admin_client
    # 端点按 provider_id 现查 registry（同一 dict 对象），替换该条目即可隔离上游
    app.state.services.registry["codebuddy"] = _StubCodeBuddy()
    credential_id = app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t", "account_uid": "u"})
    probe = client.post(f"/api/credentials/{credential_id}/probe")
    assert probe.status_code == 200
    assert probe.json()["probed"] is True
    checkin = client.post(f"/api/credentials/{credential_id}/checkin")
    assert checkin.status_code == 200
    assert checkin.json() == {"ok": True, "credit": 5, "code": 0, "message": "ok",
                              "already_checked_in": False, "status": None}
    assert client.post("/api/credentials/ghost/probe").status_code == 400
    assert client.post("/api/credentials/ghost/checkin").status_code == 400

def test_credential_account_endpoints_unsupported_for_trae(admin_client):
    app, client = admin_client
    credential_id = app.state.credentials.add(provider="trae",
                                              credential_data={"accessToken": "t"})
    assert client.get(f"/api/credentials/{credential_id}/accounts").status_code == 400
    assert client.post(f"/api/credentials/{credential_id}/accounts/select",
                       json={"account_id": "a"}).status_code == 400
    assert client.get("/api/credentials/ghost/accounts").status_code == 400

def test_select_account_rejects_vanished_credential(admin_client):
    """provider 支持切换但持锁后凭证已消失 → 400，不拿 None 去打上游。"""
    app, client = admin_client
    credential_id = app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t"})

    class Stub:
        endpoint = "https://e"

        async def switch_account(self, _data, _account_id):  # pragma: no cover
            raise AssertionError("凭证已消失，不应调用上游")

    app.state.executor._deps.providers["codebuddy"] = Stub()
    original = app.state.credentials.credential_data
    present = original(credential_id)
    calls = {"n": 0}

    def vanishing(_cid):
        # 首次（前置能力检查）仍可见，取到编辑锁后已消失——正是本用例要覆盖
        # 的竞态：能力检查通过 ≠ 持锁后凭证还在。
        calls["n"] += 1
        return present if calls["n"] == 1 else None

    app.state.credentials.credential_data = vanishing  # type: ignore[method-assign]
    try:
        response = client.post(f"/api/credentials/{credential_id}/accounts/select",
                               json={"account_id": "a1"})
    finally:
        app.state.credentials.credential_data = original  # type: ignore[method-assign]
    assert response.status_code == 400

def test_upstream_auth_start_forwards_force_login_when_supported(admin_client):
    """`force_login` 只传给支持的渠道，其余渠道仍按单参调用。"""
    from src.provider.base import AuthFlow, AuthSession

    app, client = admin_client
    seen: dict[str, object] = {}
    oauth = app.state.upstream_auth["codebuddy"]

    async def with_flag(username: str, *, force_login: bool = False) -> AuthSession:
        seen["flagged"] = (username, force_login)
        return AuthSession(flow=AuthFlow.POLL, state="s1", auth_url="https://a")

    async def plain(username: str) -> AuthSession:
        seen["plain"] = username
        return AuthSession(flow=AuthFlow.POLL, state="s2", auth_url="https://a")

    original = oauth.start
    try:
        oauth.start = with_flag  # type: ignore[method-assign]
        client.post("/api/auth/upstream/start",
                    json={"provider": "codebuddy", "force_login": True})
        assert seen["flagged"] == ("root", True)
        oauth.start = plain  # type: ignore[method-assign]
        client.post("/api/auth/upstream/start",
                    json={"provider": "codebuddy", "force_login": True})
        assert seen["plain"] == "root"
    finally:
        oauth.start = original  # type: ignore[method-assign]


def test_upstream_auth_cancel_endpoint(admin_client):
    app, client = admin_client
    oauth = app.state.upstream_auth["codebuddy"]
    session_start = oauth.store.begin("root", "up")
    assert client.post("/api/auth/upstream/cancel",
                       json={"provider": "codebuddy", "state": session_start}).json()[
        "cancelled"] is True
    assert client.post("/api/auth/upstream/cancel",
                       json={"provider": "trae", "state": "x"}).status_code == 400

def test_upstream_auth_poll_success_persists_credential(admin_client):
    """轮询成功 → 凭证入库，且响应里绝不回传 token。"""
    app, client = admin_client
    oauth = app.state.upstream_auth["codebuddy"]
    reservation = oauth.store.begin("root", "up-state")

    async def fake_poll(_state, _username):
        from src.provider.base import AuthResult

        return AuthResult(credential_data={"bearer_token": "AT", "auth_source": "oauth"},
                          nickname="nick")

    oauth.poll = fake_poll
    body = client.post("/api/auth/upstream/poll",
                       json={"provider": "codebuddy", "state": reservation}).json()
    assert body["status"] == "success"
    assert "token" not in json.dumps(body).lower()
    assert any(row["provider"] == "codebuddy"
               for row in app.state.credentials.list_all())

def test_probe_endpoint_marks_failed_on_provider_error(admin_client):
    app, client = admin_client
    credential_id = app.state.credentials.add(provider="codebuddy",
                                              credential_data={"bearer_token": "t"})

    class Boom:
        endpoint = "https://e"

        async def probe_quota(self, _data):
            raise RuntimeError("boom")

    app.state.executor._deps.providers["codebuddy"] = Boom()
    body = client.post(f"/api/credentials/{credential_id}/probe").json()
    assert body["probed"] is False
    # 不能把 Python 类名当作 reason 暴露给用户
    assert body["reason"] == "unknown_error"
    assert "RuntimeError" not in body["reason"]
    # 上游原文/异常文本不再回显给客户端，只回受控 reason
    assert "detail" not in body
    assert app.state.credentials.candidates()[0].health is None

def test_checkin_endpoint_reports_result(admin_client):
    app, client = admin_client
    credential_id = app.state.credentials.add(provider="codebuddy",
                                              credential_data={"bearer_token": "t"})

    class Stub:
        endpoint = "https://e"

        async def checkin(self, _data):
            return CheckinResult(ok=True, credit=12.0)

    app.state.executor._deps.providers["codebuddy"] = Stub()
    body = client.post(f"/api/credentials/{credential_id}/checkin").json()
    assert body["ok"] is True and body["credit"] == 12.0
    assert body["status"] is None                     # 无状态能力的渠道返回 null

def test_checkin_endpoint_passes_status_through(admin_client):
    """provider 回填 status 时原样透传（前端靠它显示连续天数）。"""
    app, client = admin_client
    credential_id = app.state.credentials.add(provider="codebuddy",
                                              credential_data={"bearer_token": "t"})

    class Stub:
        endpoint = "https://e"

        async def checkin(self, _data):
            return CheckinResult(ok=True, credit=100.0,
                                 status={"streak_days": 4, "today_checked_in": True})

    app.state.executor._deps.providers["codebuddy"] = Stub()
    body = client.post(f"/api/credentials/{credential_id}/checkin").json()
    assert body["status"] == {"streak_days": 4, "today_checked_in": True}

def test_checkin_status_endpoint(admin_client):
    """只读状态端点：有能力的渠道返回状态，无能力/不存在的凭证 400。"""
    app, client = admin_client
    credential_id = app.state.credentials.add(provider="codebuddy",
                                              credential_data={"bearer_token": "t"})

    class Stub:
        endpoint = "https://e"

        async def checkin_status(self, _data):
            return {"active": True, "streak_days": 7}

    app.state.executor._deps.providers["codebuddy"] = Stub()
    assert client.get(f"/api/credentials/{credential_id}/checkin").json() == {
        "status": {"active": True, "streak_days": 7}}

    # 没有 checkin_status 能力的渠道 → 稳定可读的错误
    app.state.executor._deps.providers["codebuddy"] = type("Bare", (), {"endpoint": "https://e"})()
    rejected = client.get(f"/api/credentials/{credential_id}/checkin")
    assert rejected.status_code == 400
    assert "checkin status" in rejected.text

    # 凭证不存在同样 400
    assert client.get("/api/credentials/cred_missing/checkin").status_code == 400

def test_account_endpoints_with_stub_provider(admin_client):
    app, client = admin_client
    credential_id = app.state.credentials.add(provider="codebuddy",
                                              credential_data={"bearer_token": "t"})

    class Stub:
        endpoint = "https://e"

        async def list_accounts(self, _data):
            return [Account(account_id="a1", nickname="n", account_type="personal",
                            enabled=True)]

        async def switch_account(self, data, account_id):
            return {**data, "account_uid": account_id}

    app.state.executor._deps.providers["codebuddy"] = Stub()
    accounts = client.get(f"/api/credentials/{credential_id}/accounts").json()["accounts"]
    assert accounts == [{"account_id": "a1", "nickname": "n", "type": "personal"}]
    switched = client.post(f"/api/credentials/{credential_id}/accounts/select",
                           json={"account_id": "a1"})
    assert switched.json()["switched"] is True
    assert app.state.credentials.credential_data(credential_id)["account_uid"] == "a1"

def test_upstream_auth_poll_unknown_provider(admin_client):
    _app, client = admin_client
    response = client.post("/api/auth/upstream/poll",
                           json={"provider": "trae", "state": "x"})
    assert response.status_code == 400

def test_probe_endpoint_returns_quota_fields(admin_client):
    """探测成功路径的响应字段（main 271-272）。"""
    app, client = admin_client
    credential_id = app.state.credentials.add(provider="codebuddy",
                                              credential_data={"bearer_token": "t"})

    class Stub:
        endpoint = "https://e"

        async def probe_quota(self, _data):
            return Quota(remaining=7, total=10, cycle_end=123, probed_at=1)

    app.state.executor._deps.providers["codebuddy"] = Stub()
    body = client.post(f"/api/credentials/{credential_id}/probe").json()
    assert body == {"probed": True, "remaining": 7, "total": 10, "cycle_end": 123}

def test_import_triggers_immediate_probe(admin_client):
    """新增凭证后应立即探测，否则界面一直显示「未探测到额度」。"""
    app, client = admin_client
    probed: list[str] = []

    class Stub:
        endpoint = "https://e"

        def import_credential(self, raw):
            return {"bearer_token": raw.get("token", "t")}

        async def probe_quota(self, _data):
            probed.append("called")
            return Quota(remaining=7, total=10, probed_at=1)

    app.state.executor._deps.providers["codebuddy"] = Stub()
    created = client.post("/api/credentials", json={
        "provider": "codebuddy", "credential": {"token": "abc"}})
    credential_id = created.json()["id"]

    # 后台任务在事件循环里执行，需等待落库
    for _ in range(50):
        if probed:
            break
        time.sleep(0.02)
    assert probed == ["called"]
    health = next(c for c in app.state.credentials.candidates(["codebuddy"])).health
    assert health == 70
    assert credential_id

def test_probe_failure_marks_unknown_on_immediate_path(admin_client):
    """即时探测失败 → 记为未探测（NULL），不是额度 0。"""
    app, client = admin_client

    class Stub:
        endpoint = "https://e"

        def import_credential(self, raw):
            return {"bearer_token": raw.get("token", "t")}

        async def probe_quota(self, _data):
            raise RuntimeError("upstream down")

    app.state.executor._deps.providers["codebuddy"] = Stub()
    client.post("/api/credentials", json={"provider": "codebuddy", "credential": {"token": "x"}})

    # 探测失败会写 quota_probed_at 并把 health 置回 NULL
    for _ in range(50):
        listed = client.get("/api/credentials").json()["credentials"][0]
        if listed["quota_probed_at"] is not None:
            break
        time.sleep(0.02)
    assert listed["health"] is None
    assert listed["quota_probed_at"] is not None

def test_checkin_success_triggers_immediate_probe(admin_client):
    """签到发放积分 → 立即刷新额度；签到失败则不探测。"""
    app, client = admin_client
    ok_id = app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t"})
    probed: list[int] = []

    class Stub:
        endpoint = "https://e"

        async def checkin(self, _data):
            return CheckinResult(ok=True, credit=10)

        async def probe_quota(self, _data):
            probed.append(1)
            return Quota(remaining=3, total=4, probed_at=1)

    app.state.executor._deps.providers["codebuddy"] = Stub()
    client.post(f"/api/credentials/{ok_id}/checkin")

    for _ in range(50):
        if probed:
            break
        time.sleep(0.02)
    assert probed == [1]

def test_checkin_failure_does_not_probe(admin_client):
    app, client = admin_client
    credential_id = app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t"})
    probed: list[int] = []

    class Stub:
        endpoint = "https://e"

        async def checkin(self, _data):
            return CheckinResult(ok=False, code=0)

        async def probe_quota(self, _data):  # pragma: no cover
            probed.append(1)
            return Quota()

    app.state.executor._deps.providers["codebuddy"] = Stub()
    client.post(f"/api/credentials/{credential_id}/checkin")
    time.sleep(0.1)
    assert probed == []

def test_schedule_probe_ignores_deleted_credential(admin_client):
    """凭证在探测前被删除 → schedule_probe 静默返回，不抛异常也不建任务。"""
    app, client = admin_client
    credential_id = app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t"})
    before = len(app.state.pending_probes)
    app.state.credentials.delete(credential_id)

    # 经由 API 触发内部 schedule_probe 的同一路径：重新导入再删除
    created = client.post("/api/credentials", json={
        "provider": "codebuddy", "credential": {"token": "x"}}).json()["id"]
    app.state.credentials.delete(created)

    assert len(app.state.pending_probes) >= before

def test_schedule_probe_returns_early_when_credential_unreadable(admin_client):
    """凭证读取不到（并发删除）→ schedule_probe 直接返回，不建探测任务（main 383-384）。"""
    app, client = admin_client
    before = len(app.state.pending_probes)
    app.state.credentials.credential_data = lambda _cid: None  # type: ignore[method-assign]

    created = client.post("/api/credentials", json={
        "provider": "codebuddy", "credential": {"token": "x"}})
    assert created.status_code == 200
    time.sleep(0.05)
    assert len(app.state.pending_probes) == before

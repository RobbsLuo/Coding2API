"""批量运维端点：批量探测 / 签到 / 启停 / 恢复。

覆盖重点：
- 逐条隔离（一条失败不影响其余条，不整批 400）；
- 参数校验（未知动作 / 空 ids / 非法 ids / 超上限）→ 400；
- 权限门槛（viewer 403）与审计（toggle/revive 落一条汇总，probe/checkin 不落）。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from src.api.admin_credentials import BATCH_MAX_IDS
from src.auth.session import create_session_token
from src.config import Settings
from src.main import build_app
from src.provider.base import Quota
from src.provider.codebuddy.checkin import CheckinResult
from tests.helpers.constants import SECRET


class _StubProvider:
    """批量探测 / 签到桩：本用例只验证端点接线，绝不出网。"""

    id = "codebuddy"
    endpoint = "https://e"

    def __init__(self, *, probe_error: Exception | None = None,
                 checkin: CheckinResult | None = None) -> None:
        self._probe_error = probe_error
        self._checkin = checkin or CheckinResult(ok=True, credit=5, code=0, message="ok")
        self.probed: list[str] = []
        self.checked_in: list[str] = []

    async def probe_quota(self, _data):
        if self._probe_error is not None:
            raise self._probe_error
        self.probed.append("called")
        return Quota(remaining=7, total=10, probed_at=1)

    async def checkin(self, _data):
        self.checked_in.append("called")
        return self._checkin


@pytest.fixture()
def batch_app(tmp_path):
    """独立 app + admin 会话 + 桩 provider（批量用例需要直接操作仓库造数据）。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    app.state.executor._deps.providers["codebuddy"] = _StubProvider()
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        yield app, client


def _add(app, provider: str = "codebuddy") -> str:
    return app.state.credentials.add(provider=provider,
                                     credential_data={"bearer_token": "t"})


def test_batch_probe_reports_per_item_results(batch_app):
    app, client = batch_app
    ok_id = _add(app)
    boom = _add(app)
    app.state.executor._deps.providers["codebuddy"] = _StubProvider(
        probe_error=RuntimeError("boom"))

    body = client.post("/api/credentials/batch",
                       json={"action": "probe", "ids": [ok_id, boom]}).json()
    assert body["action"] == "probe"
    assert body["total"] == 2 and body["succeeded"] == 0 and body["failed"] == 2
    by_id = {row["id"]: row for row in body["results"]}
    # 探测失败是「结果」而非「异常」：ok=False 且带上受控 reason
    assert by_id[ok_id]["ok"] is False
    assert by_id[ok_id]["detail"] == {"probed": False, "reason": "unknown_error"}
    # 失败也不改健康度（仍是「未探测」），与单条端点同口径
    assert by_id[boom]["ok"] is False

def test_batch_probe_success_path(batch_app):
    app, client = batch_app
    first = _add(app)
    second = _add(app)
    body = client.post("/api/credentials/batch",
                       json={"action": "probe", "ids": [first, second]}).json()
    assert body["succeeded"] == 2 and body["failed"] == 0
    assert all(row["ok"] for row in body["results"])
    assert body["results"][0]["detail"] == {
        "probed": True, "remaining": 7, "total": 10, "cycle_end": None}
    # 探测是只读语义：不写审计（与单条端点一致）
    assert app.state.audit.query(action="credential.toggle") == []

def test_batch_checkin_passes_detail_and_schedules_probe(batch_app):
    app, client = batch_app
    credential_id = _add(app)
    stub = _StubProvider(checkin=CheckinResult(ok=True, credit=12.0))
    app.state.executor._deps.providers["codebuddy"] = stub

    body = client.post("/api/credentials/batch",
                       json={"action": "checkin", "ids": [credential_id]}).json()
    row = body["results"][0]
    assert row["ok"] is True
    assert row["detail"]["credit"] == 12.0
    assert row["detail"]["already_checked_in"] is False
    assert row["detail"]["status"] is None
    assert stub.checked_in == ["called"]
    # 真签到成功 → 触发即时探测（后台协程，需等待落库）
    for _ in range(50):
        if stub.probed:
            break
        time.sleep(0.02)
    assert stub.probed == ["called"]

def test_batch_checkin_failure_keeps_ok_false(batch_app):
    app, client = batch_app
    credential_id = _add(app)
    app.state.executor._deps.providers["codebuddy"] = _StubProvider(
        checkin=CheckinResult(ok=False, code=0))

    body = client.post("/api/credentials/batch",
                       json={"action": "checkin", "ids": [credential_id]}).json()
    assert body["results"][0]["ok"] is False
    assert body["succeeded"] == 0

def test_batch_checkin_already_checked_in_does_not_schedule(batch_app):
    """今天已签过：ok=True 但不触发探测（重复签到不发积分）。"""
    app, client = batch_app
    credential_id = _add(app)
    app.state.executor._deps.providers["codebuddy"] = _StubProvider(
        checkin=CheckinResult(ok=True, credit=0, already_checked_in=True))
    before = len(app.state.pending_probes)

    body = client.post("/api/credentials/batch",
                       json={"action": "checkin", "ids": [credential_id]}).json()
    assert body["results"][0]["detail"]["already_checked_in"] is True
    assert len(app.state.pending_probes) == before

def test_batch_toggle_sets_explicit_state(batch_app):
    app, client = batch_app
    first = _add(app)
    second = _add(app)
    body = client.post("/api/credentials/batch",
                       json={"action": "toggle", "ids": [first, second],
                             "enabled": False}).json()
    assert body["succeeded"] == 2
    enabled = {row["id"]: row["enabled"] for row in app.state.credentials.list_all()}
    assert enabled[first] == 0 and enabled[second] == 0
    # 写操作落一条汇总审计（不逐条刷流水）
    events = app.state.audit.query(action="credential.toggle")
    assert len(events) == 1
    assert events[0]["detail"] == "批量 toggle（暂停）：2/2 成功"
    assert events[0]["actor"] == "root"

def test_batch_toggle_defaults_to_enabled(batch_app):
    """缺省 enabled 视为 True（与单条端点同口径）。"""
    app, client = batch_app
    credential_id = _add(app)
    app.state.credentials.set_enabled(credential_id, False)
    client.post("/api/credentials/batch",
                json={"action": "toggle", "ids": [credential_id]})
    # 按 id 取：`list_all()` 首位是启动时补的 zen 占位行，取 [0] 会断言到它
    by_id = {row["id"]: row for row in app.state.credentials.list_all()}
    assert by_id[credential_id]["enabled"] == 1

def test_batch_toggle_reports_missing_credential(batch_app):
    """toggle 对不存在的凭证逐条报错，同批其余条照常。"""
    app, client = batch_app
    credential_id = _add(app)
    body = client.post("/api/credentials/batch",
                       json={"action": "toggle", "ids": [credential_id, "cred_ghost"],
                             "enabled": False}).json()
    assert body["succeeded"] == 1 and body["failed"] == 1
    by_id = {row["id"]: row for row in body["results"]}
    assert by_id[credential_id]["ok"] is True
    assert by_id["cred_ghost"]["error"] == "credential not found"
    # 审计 detail 反映真实成功率，不按「全部成功」写死
    events = app.state.audit.query(action="credential.toggle")
    assert events[0]["detail"] == "批量 toggle（暂停）：1/2 成功"

def test_batch_revive_reports_missing_credential(batch_app):
    app, client = batch_app
    credential_id = _add(app)
    app.state.credentials.set_enabled(credential_id, False)
    # 先硬禁用 + 冷却，验证 revive 清干净
    app.state.credentials.mark_relogin_required(credential_id, "session 失效")

    body = client.post("/api/credentials/batch",
                       json={"action": "revive",
                             "ids": [credential_id, "cred_ghost"]}).json()
    assert body["succeeded"] == 1 and body["failed"] == 1
    by_id = {row["id"]: row for row in body["results"]}
    assert by_id[credential_id]["ok"] is True
    assert by_id["cred_ghost"] == {"id": "cred_ghost", "ok": False,
                                   "error": "credential not found"}
    rows = {row["id"]: row for row in app.state.credentials.list_all()}
    assert rows[credential_id]["disabled"] == 0
    events = app.state.audit.query(action="credential.revive")
    assert len(events) == 1 and events[0]["detail"] == "批量 revive：1/2 成功"

def test_batch_toggle_resume_writes_enabled_direction(batch_app):
    """批量「取消暂停」（enabled=true）与「暂停」是同一动作，靠审计方向区分。

    没有这个方向的话，审计里只有「批量 toggle：N/M 成功」，看不出操作员到底
    把号放回了池子还是摘了出去。
    """
    app, client = batch_app
    credential_id = _add(app)
    app.state.credentials.set_enabled(credential_id, False)
    body = client.post("/api/credentials/batch",
                       json={"action": "toggle", "ids": [credential_id],
                             "enabled": True}).json()
    assert body["succeeded"] == 1
    rows = {row["id"]: row for row in app.state.credentials.list_all()}
    assert rows[credential_id]["enabled"] == 1
    events = app.state.audit.query(action="credential.toggle")
    assert events[0]["detail"] == "批量 toggle（启用）：1/1 成功"

def test_batch_audit_failure_does_not_lose_per_item_results(batch_app, monkeypatch):
    """审计落库失败不得让整批返回 500——逐条结果比留痕更重要。

    真实故障：DB 损坏时 `audit.record` 抛 `sqlite3.DatabaseError`，未捕获会
    冒泡成 500 纯文本，前端 `res.json()` 拿到 "Internal Server Error" 解析失败。
    此时逐条变更其实**已经提交生效**，返回 500 会让操作员以为整批没跑。
    """
    import sqlite3

    app, client = batch_app
    credential_id = _add(app)

    def _boom(**_kwargs):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(app.state.audit, "record", _boom)
    response = client.post("/api/credentials/batch",
                           json={"action": "toggle", "ids": [credential_id],
                                 "enabled": False})
    assert response.status_code == 200
    body = response.json()
    assert body["succeeded"] == 1 and body["failed"] == 0
    assert body["results"] == [{"id": credential_id, "ok": True}]
    # 变更本身仍然生效，不因留痕失败回滚
    by_id = {row["id"]: row for row in app.state.credentials.list_all()}
    assert by_id[credential_id]["enabled"] == 0

def test_batch_rejects_unknown_action(batch_app):
    _app, client = batch_app
    response = client.post("/api/credentials/batch",
                           json={"action": "refresh", "ids": ["x"]})
    assert response.status_code == 400
    assert "unknown batch action" in response.text

@pytest.mark.parametrize("ids", [[], "cred_1", None, ["   "]])
def test_batch_rejects_empty_or_invalid_ids(batch_app, ids):
    _app, client = batch_app
    response = client.post("/api/credentials/batch",
                           json={"action": "probe", "ids": ids})
    assert response.status_code == 400
    assert "non-empty list" in response.text

def test_batch_rejects_missing_ids_key(batch_app):
    _app, client = batch_app
    response = client.post("/api/credentials/batch", json={"action": "probe"})
    assert response.status_code == 400

def test_batch_deduplicates_ids(batch_app):
    """前端「先单选再全选」会带重复项：重复探测同一凭证纯属浪费上游额度。"""
    app, client = batch_app
    credential_id = _add(app)
    body = client.post("/api/credentials/batch",
                       json={"action": "probe",
                             "ids": [credential_id, credential_id, ""]}).json()
    assert body["total"] == 1 and len(body["results"]) == 1

def test_batch_rejects_too_many_ids(batch_app):
    _app, client = batch_app
    response = client.post(
        "/api/credentials/batch",
        json={"action": "probe", "ids": [f"cred_{index}" for index in range(BATCH_MAX_IDS + 1)]})
    assert response.status_code == 400
    assert "too many credentials" in response.text

def test_batch_unsupported_capability_is_per_item_failure(batch_app):
    """渠道不支持签到：只有该条失败，同批其余条照常执行。"""
    app, client = batch_app
    supported = _add(app)
    unsupported = _add(app, provider="zen")
    body = client.post("/api/credentials/batch",
                       json={"action": "checkin", "ids": [supported, unsupported]}).json()
    by_id = {row["id"]: row for row in body["results"]}
    assert by_id[supported]["ok"] is True
    assert by_id[unsupported]["ok"] is False
    assert by_id[unsupported]["error"] == "credential does not support checkin"

def test_batch_upstream_exception_is_contained(batch_app):
    """上游抛异常：该条回受控 action_failed，不回显异常文本，其余条不受影响。"""
    app, client = batch_app
    good = _add(app)
    bad = _add(app)

    class _Split:
        id = "codebuddy"
        endpoint = "https://e"

        async def probe_quota(self, _data):
            raise RuntimeError("upstream leaked token abc123")

    app.state.executor._deps.providers["codebuddy"] = _Split()
    body = client.post("/api/credentials/batch",
                       json={"action": "probe", "ids": [good, bad]}).json()
    by_id = {row["id"]: row for row in body["results"]}
    # 探测内部已把异常转成受控 reason，不会走到外层 except
    assert by_id[good]["ok"] is False
    assert by_id[good]["detail"]["reason"] == "unknown_error"
    assert "abc123" not in str(body)

def test_batch_non_upstream_exception_is_contained(batch_app):
    """非探测路径的意外异常（如 checkin 抛错）也逐条兜住，不整批 500。

    签到失败现在由 `checkin_credential_once` 收成 `ok=False` 的**正常结果**
    （见 `src/api/admin_credentials.py`）：上游拒绝是这条凭证的真实答复，
    不是端点故障。故这里拿到的 detail 是失败结果而非 `action_failed`——
    两种形态都不整批 500、都不回显上游原文。
    """
    app, client = batch_app
    credential_id = _add(app)

    class _Boom:
        id = "codebuddy"
        endpoint = "https://e"

        async def checkin(self, _data):
            raise RuntimeError("upstream leaked token abc123")

    app.state.executor._deps.providers["codebuddy"] = _Boom()
    response = client.post("/api/credentials/batch",
                           json={"action": "checkin", "ids": [credential_id]})
    assert response.status_code == 200
    body = response.json()
    row = body["results"][0]
    assert row["id"] == credential_id and row["ok"] is False
    # 失败原因可读，且不回显上游原文（可能带凭证片段）
    assert row["detail"]["message"] == "签到失败：unknown_error"
    assert "abc123" not in response.text

def test_batch_unexpected_exception_is_contained_per_item(batch_app):
    """非签到/非探测路径的意外异常逐条兜住：整批仍 200，不回显异常原文。"""
    app, client = batch_app
    credential_id = _add(app)

    def boom(_credential_id: str) -> bool:
        raise RuntimeError("db exploded, secret abc123")

    original = app.state.credentials.set_enabled
    app.state.credentials.set_enabled = boom
    try:
        response = client.post("/api/credentials/batch",
                               json={"action": "toggle", "ids": [credential_id],
                                     "enabled": False})
    finally:
        app.state.credentials.set_enabled = original

    assert response.status_code == 200
    body = response.json()
    assert body["results"][0] == {"id": credential_id, "ok": False,
                                  "error": "action_failed"}
    assert "abc123" not in response.text


def test_batch_rejects_viewer(tmp_path):
    """viewer（非 operator）不能批量写：403。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "guest", "password": "guestpw"})
        response = client.post("/api/credentials/batch",
                               json={"action": "probe", "ids": ["cred_1"]})
        assert response.status_code == 403

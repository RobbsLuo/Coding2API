"""凭证可用性时间线端点：operator 门槛、时间窗、分页。

时间线是**只读**回看（不触发上游请求），所以对已禁用 / 冷却中的凭证同样可用；
门槛与其它凭证运维端点一致（operator+），因为 detail 里带运维判断依据。
"""

from __future__ import annotations

import time

from src.auth.session import create_session_token
from src.db.repo import CredentialEventRepository
from src.engine.scheduler import ErrorOutcome, ModelCooldown
from src.provider.base import ErrKind
from tests.helpers.constants import SECRET


def _insert(events: CredentialEventRepository, credential_id: str, *rows) -> None:
    conn = events._db.connect()
    for event_id, ts in rows:
        conn.execute(
            "INSERT INTO credential_events "
            "(id, credential_id, ts, event, reason, model, detail) "
            "VALUES (?,?,?,?,?,?,?)",
            (event_id, credential_id, ts, "cooldown_start", "soft", None, "冷却"))
    conn.commit()


def test_timeline_requires_operator(admin_client):
    """viewer 读不到（403）；admin/operator 才看得到。"""
    app, client = admin_client
    credential_id = app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t"})

    client.cookies.set("coding2api_session", create_session_token("guest", SECRET))
    assert client.get(f"/api/credentials/{credential_id}/timeline").status_code == 403

    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    assert client.get(f"/api/credentials/{credential_id}/timeline").status_code == 200


def test_timeline_rejects_unknown_credential(admin_client):
    """凭证不存在 → 400（与 checkin-events / credit-events 同一口径）。"""
    _app, client = admin_client
    assert client.get("/api/credentials/ghost/timeline").status_code == 400


def test_timeline_returns_recorded_state_transitions(admin_client):
    """真实状态迁移（冷却开始/到期/恢复）按倒序下发，字段脱敏且可读。"""
    app, client = admin_client
    credentials = app.state.credentials
    credential_id = credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t"})
    future = int(time.time()) + 3600

    credentials.save_error(credential_id, ErrorOutcome(model_cooldowns={
        "glm-5.2": ModelCooldown(cooling_until=future, hits=1, reason="model")}))
    credentials.save_error(credential_id, ErrorOutcome(
        cooling_until=future, cooling_kind=ErrKind.SOFT.value))
    credentials.revive(credential_id, force=True)

    body = client.get(f"/api/credentials/{credential_id}/timeline").json()
    events = body["events"]
    # 同一秒内的三条迁移按 (ts, id) 稳定排序（不保证秒内先后），故按集合断言
    assert sorted(event["event"] for event in events) == [
        "cooldown_start", "cooldown_start", "revived"]
    revived = next(event for event in events if event["event"] == "revived")
    assert revived["reason"] == "forced"
    # 模型级那行的 model 非空、账号级为 None：读侧靠它区分两类冷却
    assert {event["model"] for event in events} == {"glm-5.2", None}
    assert body["has_more"] is False and body["limit"] == 50


def test_timeline_window_limit_and_pagination(admin_client):
    """since/until 闭区间、limit 收敛、offset 翻页（has_more 指示还有更早的行）。"""
    app, client = admin_client
    credential_id = app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t"})
    events = CredentialEventRepository(app.state.credentials._db)
    _insert(events, credential_id, *[(f"evt_{index}", 100 + index) for index in range(5)])

    body = client.get(
        f"/api/credentials/{credential_id}/timeline?since=101&until=103").json()
    assert [event["id"] for event in body["events"]] == ["evt_3", "evt_2", "evt_1"]

    first_page = client.get(
        f"/api/credentials/{credential_id}/timeline?limit=2").json()
    assert [event["id"] for event in first_page["events"]] == ["evt_4", "evt_3"]
    assert first_page["limit"] == 2 and first_page["has_more"] is True

    second_page = client.get(
        f"/api/credentials/{credential_id}/timeline?limit=2&offset=2").json()
    assert [event["id"] for event in second_page["events"]] == ["evt_2", "evt_1"]

    # limit 越界收敛到 [1, 200]：0 → 1 行，负数 offset 当 0
    clamped = client.get(
        f"/api/credentials/{credential_id}/timeline?limit=0&offset=-5").json()
    assert len(clamped["events"]) == 1 and clamped["limit"] == 1

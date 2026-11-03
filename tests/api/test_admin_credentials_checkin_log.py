"""凭证运维端点：签到审计日志（手动签到落库 + 历史查询）。"""

from __future__ import annotations

from src.provider.base import CheckinResult, Quota


class _Stub:
    """签到桩：按队列依次返回结果；probe_quota 吸收签到成功后调度的探测。"""

    id = "codebuddy"

    def __init__(self, *results: CheckinResult) -> None:
        self.queue = list(results)

    async def probe_quota(self, _data):
        return Quota(remaining=1, total=2, probed_at=1)

    async def checkin(self, _data):
        return self.queue.pop(0)


def test_manual_checkin_records_and_lists_history(admin_client):
    """手动签到逐条落库（trigger=manual），历史端点倒序返回，limit 生效。"""
    app, client = admin_client
    credential_id = app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t", "account_uid": "u"})
    # 第一次成功、第二次业务失败：两条都应落库（失败也要能回看）
    app.state.services.registry["codebuddy"] = _Stub(
        CheckinResult(ok=True, credit=1000.0, code=0, message="领取成功"),
        CheckinResult(ok=False, code=40001, message="尚未到达权益刷新时间"))

    assert client.post(f"/api/credentials/{credential_id}/checkin").json()["ok"] is True
    assert client.post(f"/api/credentials/{credential_id}/checkin").json()["ok"] is False

    body = client.get(f"/api/credentials/{credential_id}/checkin-events").json()
    events = body["events"]
    assert len(events) == 2
    assert events[0]["ok"] == 0 and events[0]["code"] == 40001
    assert events[0]["message"] == "尚未到达权益刷新时间"
    assert events[1]["ok"] == 1 and events[1]["credit"] == 1000.0
    assert all(event["trigger"] == "manual" for event in events)

    limited = client.get(
        f"/api/credentials/{credential_id}/checkin-events?limit=1").json()["events"]
    assert len(limited) == 1 and limited[0]["code"] == 40001

    # 不存在的凭证 → 400（与其它历史端点同口径）
    assert client.get("/api/credentials/ghost/checkin-events").status_code == 400


def test_checkin_history_empty_for_new_credential(admin_client):
    """从未签过的凭证返回空数组（不是 404）。"""
    app, client = admin_client
    credential_id = app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t"})
    assert client.get(
        f"/api/credentials/{credential_id}/checkin-events").json() == {"events": []}


def test_manual_checkin_upstream_exception_also_recorded(admin_client):
    """上游抛异常时也落一行「签到失败」，而不只留在服务端日志里。"""
    app, client = admin_client
    credential_id = app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t", "account_uid": "u"})

    class Boom:
        id = "codebuddy"

        async def checkin(self, _data):
            raise RuntimeError("boom")

    app.state.services.registry["codebuddy"] = Boom()
    assert client.post(f"/api/credentials/{credential_id}/checkin").json()["ok"] is False

    events = client.get(
        f"/api/credentials/{credential_id}/checkin-events").json()["events"]
    assert len(events) == 1 and events[0]["ok"] == 0
    assert "签到失败" in events[0]["message"]
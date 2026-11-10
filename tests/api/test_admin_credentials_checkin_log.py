"""凭证运维端点：签到审计日志（手动签到落库 + 历史查询）。"""

from __future__ import annotations

import time

from src.engine.limits import day_start
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


def test_credentials_list_attaches_today_checkin_result(admin_client):
    """列表按凭证下发「今日最近一条签到结果」：今日无记录/只有昨天记录都是 null。

    组头靠它汇总「已签 / 失败 / 未签」，所以边界必须准：昨天的成功不能冒充
    今天（否则凌晨会显示已签、任务实际没跑）；没有记录也不能硬填成失败。
    """
    app, client = admin_client
    first = app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t", "account_uid": "u1"})
    second = app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t", "account_uid": "u2"})
    unsign = app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t", "account_uid": "u3"})
    events = app.state.checkin_events
    midnight = day_start(int(time.time()))
    # 失败后又补签成功：最新一条才是「签没签成功」的答案
    events.record(credential_id=first, now=midnight + 1,
                  result=CheckinResult(ok=False, code=40001, message="尚未刷新"))
    events.record(credential_id=first, now=midnight + 2,
                  result=CheckinResult(ok=True, credit=100.0, code=0, message="领取成功"))
    # 第二张只有昨天的记录（午夜前 1 秒）→ 今天应视为没有结论
    events.record(credential_id=second, now=midnight - 1,
                  result=CheckinResult(ok=True, credit=100.0, message="昨天领的"))

    rows = {row["id"]: row
            for row in client.get("/api/credentials").json()["credentials"]}
    assert rows[first]["checkin_today"]["ok"] == 1
    assert rows[first]["checkin_today"]["credit"] == 100.0
    assert rows[first]["checkin_today"]["message"] == "领取成功"
    assert rows[second]["checkin_today"] is None
    assert rows[unsign]["checkin_today"] is None


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
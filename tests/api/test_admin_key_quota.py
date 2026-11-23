"""管理台 Key 配额读写：创建时带上、PUT 改配额、审计、列表当日已用。"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from src.audit.actions import ACTION_API_KEY_QUOTA, ACTION_LABELS, ACTIONS
from src.engine.limits import day_start
from src.main import build_app
from tests.helpers.auth import isolated_settings, set_session_cookie


@pytest.fixture()
def app(tmp_path):
    return build_app(isolated_settings(tmp_path))


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        set_session_cookie(test_client, "root")
        yield test_client


def _create(client, **payload) -> dict:
    response = client.post("/api/api-keys", json={"name": "k", **payload})
    assert response.status_code == 200, response.text
    return response.json()


def test_create_key_with_quota_round_trips(client):
    created = _create(client, quota={"daily_requests": 100, "rpm": 5})
    assert json.loads(created["quota"]) == {"daily_requests": 100, "rpm": 5}
    listed = client.get("/api/api-keys").json()["api_keys"][0]
    assert json.loads(listed["quota"]) == {"daily_requests": 100, "rpm": 5}


def test_create_key_without_quota_stores_empty(client):
    assert _create(client)["quota"] == ""


def test_create_key_with_json_text_quota(client):
    created = _create(client, quota='{"tpm": 1000}')
    assert json.loads(created["quota"]) == {"tpm": 1000}


@pytest.mark.parametrize("bad", [
    {"daily_request": 1},                       # 未知键
    {"rpm": -1},                                # 负数
    {"rpm": "10"},                              # 非数字
    {"daily_requests": 1.5},                    # 整数项给了小数
    "[1,2]",                                    # 非对象
])
def test_create_key_rejects_invalid_quota(client, bad):
    response = client.post("/api/api-keys", json={"name": "k", "quota": bad})
    assert response.status_code == 400
    assert "quota" in response.json()["error"]["message"]


def test_put_quota_updates_and_audits(client, app):
    created = _create(client)
    response = client.put(f"/api/api-keys/{created['id']}/quota",
                          json={"quota": {"daily_tokens": 500, "max_concurrency": 2}})
    assert response.status_code == 200
    assert response.json()["quota"]["daily_tokens"] == 500
    assert json.loads(app.state.api_keys.list_for("root")[0]["quota"]) == {
        "daily_tokens": 500, "max_concurrency": 2}
    events = app.state.audit.query(action=ACTION_API_KEY_QUOTA)
    assert len(events) == 1
    assert events[0]["target"] == created["id"]
    assert "每日 token 500" in events[0]["detail"]
    assert "并发 2" in events[0]["detail"]


def test_put_quota_empty_clears(client, app):
    created = _create(client, quota={"rpm": 5})
    response = client.put(f"/api/api-keys/{created['id']}/quota", json={"quota": {}})
    assert response.status_code == 200
    assert app.state.api_keys.list_for("root")[0]["quota"] == ""
    # 清除也算一次审计动作，detail 写明「清除配额」
    assert app.state.audit.query(action=ACTION_API_KEY_QUOTA)[0]["detail"] == "清除配额"


def test_put_quota_missing_key_is_400(client):
    response = client.put("/api/api-keys/key_ghost/quota", json={"quota": {"rpm": 1}})
    assert response.status_code == 400
    assert "not found" in response.json()["error"]["message"]


def test_put_quota_cannot_touch_other_users_key(client, app):
    other = app.state.api_keys.create("alice", "theirs")
    response = client.put(f"/api/api-keys/{other['id']}/quota", json={"quota": {"rpm": 1}})
    assert response.status_code == 400
    assert app.state.api_keys.list_for("alice")[0]["quota"] == ""


def test_put_quota_rejects_invalid_payload(client):
    created = _create(client)
    response = client.put(f"/api/api-keys/{created['id']}/quota",
                          json={"quota": {"rpm": -3}})
    assert response.status_code == 400


def test_put_quota_requires_session(app):
    with TestClient(app) as http:      # 不带 Cookie
        created = app.state.api_keys.create("root")
        assert http.put(f"/api/api-keys/{created['id']}/quota",
                        json={"quota": {"rpm": 1}}).status_code == 401


def test_list_includes_usage_window(client, app):
    from src.request_context import set_api_key_id

    created = _create(client)
    set_api_key_id(created["id"])
    try:
        app.state.stats_collector.record(username="root", provider="trae",
                                         model="glm-5.2", ok=True,
                                         input_tokens=7, output_tokens=3)
    finally:
        set_api_key_id(None)
    row = client.get("/api/api-keys").json()["api_keys"][0]
    # 没有装配价表/汇率时成本为 NULL → 按 0 累加（不猜）
    assert row["usage"] == {"requests": 1, "tokens": 10, "cost_usd": 0.0}
    # 没有任何明细的 Key 显示 0（而不是缺键）
    _create(client, name="idle")
    idle = [item for item in client.get("/api/api-keys").json()["api_keys"]
            if item["name"] == "idle"][0]
    assert idle["usage"] == {"requests": 0, "tokens": 0, "cost_usd": 0.0}


def _record_at(app, key_id: str, ts: int) -> None:
    """在指定时刻写一条明细（`now` 注入让窗口边界可测）。"""
    from src.request_context import set_api_key_id

    set_api_key_id(key_id)
    try:
        app.state.stats_collector.record(username="root", provider="trae",
                                         model="glm-5.2", ok=True,
                                         input_tokens=7, output_tokens=3, now=ts)
    finally:
        set_api_key_id(None)


def test_list_usage_window_today_7d_30d(client, app):
    """三档窗口：含今天在内的最近 N 个自然日（北京时间 0 点起）——边界都按秒卡。"""
    created = _create(client)
    start = day_start(int(time.time()))
    _record_at(app, created["id"], start)                    # 今天 00:00
    _record_at(app, created["id"], start - 1)                # 昨天 23:59:59
    _record_at(app, created["id"], start - 6 * 86400)        # 6 天前 00:00（7d 内）
    _record_at(app, created["id"], start - 6 * 86400 - 1)    # 7d 外一秒
    _record_at(app, created["id"], start - 29 * 86400)       # 29 天前 00:00（30d 内）
    _record_at(app, created["id"], start - 29 * 86400 - 1)   # 30d 外一秒

    def usage(window: str) -> dict:
        payload = client.get("/api/api-keys", params={"window": window}).json()
        assert payload["window"] == window
        return payload["api_keys"][0]["usage"]

    assert usage("today")["requests"] == 1
    assert usage("7d") == {"requests": 3, "tokens": 30, "cost_usd": 0.0}
    assert usage("30d") == {"requests": 5, "tokens": 50, "cost_usd": 0.0}
    # 不传 window 时口径与「今日」一致（老前端向后兼容）
    default = client.get("/api/api-keys").json()
    assert default["window"] == "today"
    assert default["api_keys"][0]["usage"]["requests"] == 1


@pytest.mark.parametrize("window", ["1d", "week", "", "7D"])
def test_list_rejects_unknown_window(client, window):
    response = client.get("/api/api-keys", params={"window": window})
    assert response.status_code == 400
    assert "window" in response.json()["error"]["message"]


def test_quota_action_registered_with_label():
    assert ACTION_API_KEY_QUOTA in ACTIONS
    assert ACTION_LABELS[ACTION_API_KEY_QUOTA] == "设置 Key 配额"

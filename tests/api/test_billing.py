"""月度账单端点：按人 / 按 Key 聚合、CSV / JSONL 导出、权限与参数校验。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src.api.admin_billing import month_range
from src.auth.session import create_session_token
from src.compat.openai.request import InvalidRequest
from src.config import Settings
from src.main import build_app
from src.request_context import set_api_key_id
from tests.helpers.constants import SECRET


@pytest.fixture()
def billing_env(admin_client):
    """admin 会话 + 一条 Key + 三条明细（含一个 Key 归属人）。"""
    app, client = admin_client
    key_id = app.state.api_keys.create(username="root", name="ci")["id"]
    # 9 月一条、当月两条：验证月份过滤
    app.state.stats_collector.record(
        username="root", provider="trae", model="m", ok=True,
        input_tokens=10, output_tokens=5, now=1_700_000_000)
    set_api_key_id(key_id)
    try:
        app.state.stats_collector.record(
            username="root", provider="trae", model="m", ok=True,
            input_tokens=100, output_tokens=50, now=1_790_000_000)
        app.state.stats_collector.record(
            username="bob", provider="kilo", model="m", ok=False,
            input_tokens=1, output_tokens=0, now=1_790_000_100)
    finally:
        set_api_key_id(None)
    app.state.stats_collector.rollup_hourly()
    return app, client, key_id


def _month_of(epoch: int) -> str:
    import time

    return time.strftime("%Y-%m", time.localtime(epoch))


def test_month_range_starts_local_month():
    start, end = month_range("2026-10")
    import time

    assert time.localtime(start).tm_mday == 1
    assert time.localtime(start).tm_hour == 0
    assert time.localtime(end).tm_mon == 11


@pytest.mark.parametrize("bad", ["2026-1", "2026-13", "2026-00", "abc",
                                 "20-10", "2026/10", "2026-10-01", "20a6-10"])
def test_month_range_rejects_bad_input(bad):
    with pytest.raises(InvalidRequest):
        month_range(bad)


def test_month_range_rejects_out_of_epoch_year():
    with pytest.raises(InvalidRequest):
        month_range("0000-01")


def test_billing_defaults_to_current_month_and_sums(billing_env):
    app, client, key_id = billing_env
    month = _month_of(1_790_000_000)
    body = client.get("/api/billing", params={"month": month}).json()
    assert body["month"] == month
    assert body["totals"]["requests"] == 2
    assert body["totals"]["input_tokens"] == 101
    assert body["totals"]["success_rate"] == 0.5
    users = {row["username"]: row for row in body["users"]}
    assert users["root"]["requests"] == 1
    assert users["bob"]["requests"] == 1
    # 9 月那条不在 10 月账单里
    assert body["totals"]["requests"] == 2

    keys = {row["api_key_id"]: row for row in body["keys"]}
    assert key_id in keys
    assert keys[key_id]["username"] == "root"
    assert keys[key_id]["key_name"] == "ci"


def test_billing_without_month_uses_current(billing_env):
    _app, client, _key = billing_env
    body = client.get("/api/billing").json()
    import time

    assert body["month"] == time.strftime("%Y-%m", time.localtime())


def test_billing_csv_exports_users_and_audits(billing_env):
    app, client, _key = billing_env
    month = _month_of(1_790_000_000)
    response = client.get("/api/billing", params={"month": month, "format": "csv"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    text = response.text
    assert text.startswith("\ufeff")            # Excel 中文表头不乱码
    assert "username,requests" in text
    assert "bob" in text
    audit = app.state.audit.query(limit=20)
    assert any("CSV 账单" in row["detail"] for row in audit)


def test_billing_jsonl_exports_events(billing_env):
    app, client, _key = billing_env
    month = _month_of(1_790_000_000)
    response = client.get("/api/billing", params={"month": month, "format": "jsonl"})
    assert response.status_code == 200
    lines = [line for line in response.text.splitlines() if line]
    assert len(lines) == 2
    assert all(json.loads(line)["model"] == "m" for line in lines)
    assert response.headers["X-Export-Row-Count"] == "2"
    audit = app.state.audit.query(limit=20)
    assert any("JSONL 账单" in row["detail"] for row in audit)


def test_billing_rejects_unknown_format(billing_env):
    _app, client, _key = billing_env
    assert client.get("/api/billing", params={"format": "xml"}).status_code == 400


def test_billing_rejects_bad_month(billing_env):
    _app, client, _key = billing_env
    assert client.get("/api/billing", params={"month": "2026-13"}).status_code == 400


def test_billing_requires_operator(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    app = build_app(settings)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("guest", SECRET))
        assert client.get("/api/billing").status_code == 403

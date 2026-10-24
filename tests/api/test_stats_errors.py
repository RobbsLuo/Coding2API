"""`GET /api/stats/errors`（失败归因）与 `/api/stats/events` 的 `ok` 过滤。

守住四件事：**operator-only（viewer 403）**、**默认窗口 24h + limit 夹取**、
**分组口径（渠道/模型/错误类型 + 计数 + 最近时刻）**、以及**下钻过滤 `ok=0`
在 JSON 与 CSV 两条出口都生效**（点失败归因卡片 → 明细只剩失败）。
"""

from __future__ import annotations

import time

from src.auth.rbac import ROLE_OPERATOR
from src.auth.session import create_session_token
from src.stats.query import ERROR_MAX_GROUPS, ERROR_WINDOW_SECONDS
from tests.helpers.constants import SECRET

PATH = "/api/stats/errors"


def _seed_failures(app, *, now: int | None = None) -> int:
    """写 3 条 rate_limit + 1 条 upstream_error（全部失败）+ 1 条成功。"""
    collector = app.state.stats_collector
    base = int(time.time()) if now is None else now
    for offset in range(3):
        collector.record(username="root", provider="trae", model="glm", ok=False,
                         error_type="rate_limit", now=base - 10 + offset)
    collector.record(username="root", provider="trae", model="glm", ok=False,
                     error_type="upstream_error", now=base - 5)
    collector.record(username="root", provider="trae", model="glm", ok=True, now=base)
    return base


def test_errors_requires_operator_rejects_viewer(admin_client):
    """viewer 一律 403：跨用户失败归因是运维视图（自己的失败明细另有出口）。"""
    _app, client = admin_client
    client.cookies.set("coding2api_session", create_session_token("guest", SECRET))
    assert client.get(PATH).status_code == 403


def test_errors_allows_operator(admin_client):
    app, client = admin_client
    app.state.user_repo.update_role("guest", ROLE_OPERATOR)      # epoch 0 → 1
    client.cookies.set("coding2api_session",
                       create_session_token("guest", SECRET, epoch=1))
    assert client.get(PATH).status_code == 200


def test_errors_groups_and_orders_by_failures(admin_client):
    app, client = admin_client
    now = _seed_failures(app)
    rows = client.get(PATH, params={"since": now - 3600}).json()["errors"]
    assert rows[0] == {"provider": "trae", "model": "glm", "error_type": "rate_limit",
                       "failures": 3, "rescued": 0, "last_ts": now - 8}
    assert rows[1]["error_type"] == "upstream_error" and rows[1]["failures"] == 1
    # 成功请求不进归因
    assert sum(row["failures"] for row in rows) == 4


def test_errors_default_window_is_24h(admin_client):
    """不给 since：默认最近 24 小时（更早的失败不进结果）。"""
    app, client = admin_client
    now = int(time.time())
    collector = app.state.stats_collector
    collector.record(username="root", provider="trae", model="glm", ok=False,
                     error_type="rate_limit", now=now - 60)
    collector.record(username="root", provider="codebuddy", model="kimi", ok=False,
                     error_type="rate_limit", now=now - ERROR_WINDOW_SECONDS - 60)
    rows = client.get(PATH).json()["errors"]
    assert [row["provider"] for row in rows] == ["trae"]


def test_errors_clamps_limit(admin_client):
    """limit 夹到 1..ERROR_MAX_GROUPS：0 也给一组，超大值不炸。"""
    app, client = admin_client
    now = _seed_failures(app)
    assert len(client.get(PATH, params={"since": now - 3600, "limit": 0})
               .json()["errors"]) == 1
    assert len(client.get(PATH, params={"since": now - 3600, "limit": 1})
               .json()["errors"]) == 1
    huge = client.get(PATH, params={"since": now - 3600,
                                    "limit": ERROR_MAX_GROUPS + 100})
    assert huge.status_code == 200
    assert len(huge.json()["errors"]) == 2


def test_errors_respects_until_and_username_scope(admin_client):
    app, client = admin_client
    now = int(time.time())
    collector = app.state.stats_collector
    collector.record(username="root", provider="trae", model="glm", ok=False,
                     error_type="rate_limit", now=now - 100)
    collector.record(username="other", provider="codebuddy", model="kimi", ok=False,
                     error_type="rate_limit", now=now - 50)
    # 半开区间：until 本身不计入
    rows = client.get(PATH, params={"since": now - 200, "until": now - 100}).json()["errors"]
    assert rows == []
    rows = client.get(PATH, params={"since": now - 200, "until": now - 60}).json()["errors"]
    assert len(rows) == 1 and rows[0]["provider"] == "trae"
    # username 作用域收窄（admin 可查指定人）
    rows = client.get(PATH, params={"since": now - 200, "username": "other"}
                      ).json()["errors"]
    assert [row["provider"] for row in rows] == ["codebuddy"]


def test_events_ok_filter_json_and_csv(admin_client):
    """`ok=0` 在明细 JSON 与 CSV 导出两条出口都生效。"""
    app, client = admin_client
    now = _seed_failures(app)
    failed = client.get("/api/stats/events", params={"ok": 0, "since": now - 3600}).json()
    assert len(failed["events"]) == 4
    assert all(row["ok"] == 0 for row in failed["events"])
    succeeded = client.get("/api/stats/events", params={"ok": 1, "since": now - 3600}).json()
    assert [row["ok"] for row in succeeded["events"]] == [1]
    # 不传 = 不限制（历史行为）
    everything = client.get("/api/stats/events", params={"since": now - 3600}).json()
    assert len(everything["events"]) == 5

    csv = client.get("/api/stats/events",
                     params={"ok": 0, "since": now - 3600, "format": "csv"})
    assert csv.status_code == 200
    assert csv.headers["x-export-row-count"] == "4"


def test_events_ok_filter_offset_mode_total(admin_client):
    """offset 模式下 total 也跟着 `ok` 过滤（前端据此算页数）。"""
    app, client = admin_client
    now = _seed_failures(app)
    page = client.get("/api/stats/events", params={"ok": 0, "since": now - 3600,
                                                   "sort": "ts", "order": "asc",
                                                   "limit": 2}).json()
    assert page["total"] == 4 and len(page["events"]) == 2

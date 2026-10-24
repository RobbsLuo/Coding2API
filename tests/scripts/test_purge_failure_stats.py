"""scripts/purge_failure_stats.py 的过滤、删除与汇总同步测试。"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from src.db.conn import Database
from src.db.migrate import apply_schema
from src.stats.collector import StatsCollector
from tests.helpers.paths import load_script

PURGE = load_script("purge_failure_stats")


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "t.sqlite3")
    apply_schema(database.connect())
    yield database, StatsCollector(database)
    database.close()


def _seed(collector: StatsCollector) -> None:
    """两条 qoder 失败 + 一条 zen 失败 + 一条成功（成功行永不被删）。"""
    collector.record(username="u", provider="qoder", model="m", ok=False,
                     error_type="invalid_request")
    collector.record(username="u", provider="qoder", model="m", ok=False,
                     error_type="invalid_request")
    collector.record(username="u", provider="zen", model="m", ok=False,
                     error_type="upstream_error")
    collector.record(username="u", provider="qoder", model="m", ok=True)


def test_failure_groups_group_and_order(db):
    """分组口径：provider+model+error_type，按条数降序。"""
    _database, collector = db
    _seed(collector)
    groups = PURGE.failure_groups(collector._db.connect())
    assert [(g[0], g[2], g[3]) for g in groups] == [
        ("qoder", "invalid_request", 2), ("zen", "upstream_error", 1)]


def test_filters_narrow_by_provider_and_error_type(db):
    """过滤条件只能收窄，成功行永不进入待删集合。"""
    _database, collector = db
    _seed(collector)
    conn = collector._db.connect()
    assert [g[3] for g in PURGE.failure_groups(conn, provider="qoder")] == [2]
    assert [g[3] for g in PURGE.failure_groups(conn, error_type="upstream_error")] == [1]
    assert PURGE.failure_groups(conn, provider="trae") == []


def test_days_filter_uses_window(db):
    """`--days` 只覆盖窗口内的失败明细。"""
    _database, collector = db
    conn = collector._db.connect()
    old = int(time.time()) - 3 * 86400
    conn.execute(
        "INSERT INTO usage_events (id, ts, username, provider, model, ok, error_type) "
        "VALUES ('old', ?, 'u', 'qoder', 'm', 0, 'invalid_request')", (old,))
    conn.commit()
    assert PURGE.failure_groups(conn, days=1) == []
    assert [g[3] for g in PURGE.failure_groups(conn, days=7)] == [1]


def test_delete_groups_keeps_success_rows(db):
    """删除只命中 ok=0，成功明细与其它渠道保持原样。"""
    database, collector = db
    _seed(collector)
    conn = database.connect()
    assert PURGE.delete_groups(conn, provider="qoder") == 2
    conn.commit()
    remaining = [tuple(r) for r in conn.execute(
        "SELECT provider, ok FROM usage_events ORDER BY provider")]
    assert remaining == [("qoder", 1), ("zen", 0)]


def test_sync_hourly_aligns_aggregate(db):
    """删完重算：该小时 requests 下降，孤儿小时行被清掉。"""
    database, collector = db
    _seed(collector)
    conn = database.connect()
    hours = PURGE.hour_range(conn, "ok = 0", [])
    PURGE.delete_groups(conn, provider="qoder", error_type="invalid_request")
    conn.commit()
    PURGE.sync_hourly(conn, hours)
    rows = [tuple(r) for r in conn.execute(
        "SELECT provider, requests, ok_count FROM usage_hourly ORDER BY provider")]
    assert rows == [("qoder", 1, 1), ("zen", 1, 0)]


def test_sync_hourly_keeps_permanent_hours_outside_deleted_range(db):
    """删除区间的孤儿清理不能碰历史小时：90 天前明细已被 retention 删掉，
    那些小时行是永久汇总（PROPOSAL §6），无条件清孤儿会整批删掉。"""
    database, collector = db
    _seed(collector)
    old = int(time.time()) - 200 * 86400
    collector.record(username="u", provider="qoder", model="m", ok=True, now=old)
    collector.rollup_hourly()
    conn = database.connect()
    collector.purge_expired(90)                     # 模拟保留期任务：明细没了，汇总还在
    old_hour = (old // 3600) * 3600
    assert conn.execute("SELECT COUNT(*) FROM usage_hourly WHERE hour_utc = ?",
                        (old_hour,)).fetchone()[0] == 1

    hours = PURGE.hour_range(conn, "ok = 0", [])
    PURGE.delete_groups(conn, provider="qoder")
    conn.commit()
    PURGE.sync_hourly(conn, hours)
    assert conn.execute("SELECT COUNT(*) FROM usage_hourly WHERE hour_utc = ?",
                        (old_hour,)).fetchone()[0] == 1


def test_main_preview_and_apply(db, tmp_path, capsys):
    database, collector = db
    _seed(collector)
    db_path = Path(database.path)
    database.close()

    assert PURGE.main(["--db", str(db_path), "--provider", "qoder"]) == 0
    assert "共 2 条失败明细" in capsys.readouterr().out
    assert sqlite3.connect(str(db_path)).execute(
        "SELECT COUNT(*) FROM usage_events").fetchone()[0] == 4

    assert PURGE.main(["--db", str(db_path), "--provider", "qoder", "--apply"]) == 0
    assert "已删除 2 条明细" in capsys.readouterr().out
    assert sqlite3.connect(str(db_path)).execute(
        "SELECT COUNT(*) FROM usage_events").fetchone()[0] == 2
    assert list(tmp_path.glob("*.bak-*")), "apply 前必须留下备份"


def test_main_all_flag_purges_every_failure(db, capsys):
    database, collector = db
    _seed(collector)
    db_path = Path(database.path)
    database.close()
    assert PURGE.main(["--db", str(db_path), "--all", "--apply", "--yes"]) == 0
    assert "已删除 3 条明细" in capsys.readouterr().out
    assert sqlite3.connect(str(db_path)).execute(
        "SELECT COUNT(*) FROM usage_events WHERE ok = 0").fetchone()[0] == 0


def test_main_reports_nothing_to_clean(db, capsys):
    database, _collector = db
    db_path = Path(database.path)
    database.close()
    assert PURGE.main(["--db", str(db_path), "--all"]) == 0
    assert "没有需要清理的记录" in capsys.readouterr().out


def test_main_requires_filter_and_rejects_missing_db(db, tmp_path):
    database, _collector = db
    db_path = Path(database.path)
    database.close()
    with pytest.raises(SystemExit):
        PURGE.main(["--db", str(db_path)])                    # 没给条件 → 拒绝
    with pytest.raises(SystemExit):
        PURGE.main(["--db", str(db_path), "--days", "0"])     # 非正窗口 → 拒绝
    with pytest.raises(SystemExit):
        PURGE.main(["--db", str(tmp_path / "nope.sqlite3"), "--all"])


def test_main_all_requires_yes_and_rejects_narrowing_flags(db):
    """`--all` 是「无条件删全部」：必须 --yes 二次确认，且不能叠加收窄条件。"""
    database, collector = db
    _seed(collector)
    db_path = Path(database.path)
    database.close()
    with pytest.raises(SystemExit):
        PURGE.main(["--db", str(db_path), "--all", "--apply"])       # 缺 --yes
    with pytest.raises(SystemExit):
        PURGE.main(["--db", str(db_path), "--all", "--days", "7"])   # 静默收窄

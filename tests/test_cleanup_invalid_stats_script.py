"""scripts/cleanup_invalid_stats.py 的白打判定与汇总同步测试。

脚本此前只被 ruff 检查、无测试：`_DbAdapter` 在 Database.transaction()
重构后坏掉也没被发现（rollup 调用 transaction() → AttributeError）。
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from src.db.conn import Database
from src.db.migrate import apply_schema
from src.stats.collector import StatsCollector

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "cleanup_invalid_stats.py"


def _load():
    spec = importlib.util.spec_from_file_location("cleanup_invalid_stats", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CLEANUP = _load()


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "t.sqlite3")
    apply_schema(database.connect())
    yield database, StatsCollector(database)
    database.close()


def test_stale_only_when_other_provider_succeeded(db):
    """白打 = 本上游对该模型从无成功 + 另一上游有成功；其余一律保留。"""
    _database, collector = db
    collector.record(username="u", provider="trae", model="solo", ok=False,
                     error_type="invalid_request")          # trae 白打
    collector.record(username="u", provider="codebuddy", model="solo", ok=True)
    collector.record(username="u", provider="trae", model="typo", ok=False,
                     error_type="invalid_request")          # 两边都没成功 → 保留
    collector.record(username="u", provider="trae", model="dual", ok=True)   # 本上游成功过 → 保留
    collector.record(username="u", provider="trae", model="dual", ok=False,
                     error_type="invalid_request")
    collector.record(username="u", provider="trae", model="solo", ok=False,
                     error_type="upstream_error")           # 非 invalid → 不删
    stale = CLEANUP.stale_groups(collector._db.connect())
    assert [(p, m, c) for p, m, c in stale] == [("trae", "solo", 1)]


def test_delete_and_sync_hourly_aligns_aggregate(db):
    """删除后 sync_hourly 让小时汇总与明细严格一致（transaction 可用）。"""
    database, collector = db
    collector.record(username="u", provider="trae", model="solo", ok=False,
                     error_type="invalid_request")
    collector.record(username="u", provider="codebuddy", model="solo", ok=True)
    conn = database.connect()
    assert CLEANUP.delete_stale(conn, CLEANUP.stale_groups(conn)) == 1
    conn.commit()
    CLEANUP.sync_hourly(conn)
    rows = [tuple(r) for r in conn.execute(
        "SELECT provider, model, requests FROM usage_hourly ORDER BY provider")]
    assert rows == [("codebuddy", "solo", 1)]


def test_main_preview_and_apply(db, tmp_path, capsys):
    database, collector = db
    collector.record(username="u", provider="trae", model="solo", ok=False,
                     error_type="invalid_request")
    collector.record(username="u", provider="codebuddy", model="solo", ok=True)
    db_path = Path(database.path)
    database.close()

    assert CLEANUP.main(["--db", str(db_path)]) == 0        # 默认只预览
    assert "共 1 条白打明细" in capsys.readouterr().out
    assert sqlite3.connect(str(db_path)).execute(
        "SELECT COUNT(*) FROM usage_events").fetchone()[0] == 2

    assert CLEANUP.main(["--db", str(db_path), "--apply"]) == 0
    assert "已删除 1 条明细" in capsys.readouterr().out
    assert sqlite3.connect(str(db_path)).execute(
        "SELECT COUNT(*) FROM usage_events").fetchone()[0] == 1
    assert list(tmp_path.glob("*.bak-*")), "apply 前必须留下备份"


def test_main_reports_nothing_to_clean(db, capsys):
    database, _collector = db
    db_path = Path(database.path)
    database.close()
    assert CLEANUP.main(["--db", str(db_path)]) == 0
    assert "没有需要清理的记录" in capsys.readouterr().out


def test_main_rejects_missing_db(tmp_path):
    with pytest.raises(SystemExit):
        CLEANUP.main(["--db", str(tmp_path / "nope.sqlite3")])

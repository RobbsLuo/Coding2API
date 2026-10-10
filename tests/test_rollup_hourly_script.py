"""scripts/rollup_hourly.py 的历史汇总重算（诊断 + 预览 + 写库）测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from src.db.conn import Database
from src.db.migrate import apply_schema
from src.stats.backfill import pending_hourly
from src.stats.collector import StatsCollector

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "rollup_hourly.py"


def _load():
    spec = importlib.util.spec_from_file_location("rollup_hourly", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ROLLUP = _load()


def _insert(db, *, id, ts=1_700_000_000, username="u", provider="trae",
            model="glm-5.2", ok=1, input_tokens=10, output_tokens=5, cached_tokens=None):
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO usage_events (id, ts, username, provider, credential_id, model,"
            " ok, input_tokens, output_tokens, cached_tokens)"
            " VALUES (?,?,?,?,NULL,?,?,?,?,?)",
            (id, ts, username, provider, model, ok, input_tokens, output_tokens,
             cached_tokens))


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "t.sqlite3")
    apply_schema(database.connect())
    yield database
    database.close()


def test_pending_hourly_reports_missing_and_mismatch(db):
    """诊断区分「缺汇总行」与「数值不符」；已一致的键不报。"""
    collector = StatsCollector(db)
    _insert(db, id="a")                                   # 会被 rollup 覆盖
    _insert(db, id="b", model="kimi-k3", cached_tokens=7)  # 汇总后一致 → 不报
    collector.rollup_hourly()
    _insert(db, id="c", model="glm-5.2", ok=0, input_tokens=99)  # 汇总值变旧
    _insert(db, id="d", provider="zen", model="deepseek")      # 从未汇总过

    report = pending_hourly(db.connect())
    assert report["missing"] == 1 and report["mismatched"] == 1
    assert report["events"] == 4 and report["hourly"] == 2
    # 样例：zen 那个键缺行，trae 那个键数值不符（顺序按小时+键名，不做假设）
    kinds = {gap[2]: gap[5] for gap in report["samples"]}
    assert kinds == {"zen": True, "trae": False}
    assert report["events_span"] is not None and report["hourly_span"] is not None


def test_pending_hourly_reports_credit_columns_gap(db):
    """汇总在、计数一致但 credit 过期：诊断不算缺口（重算走整行 REPLACE）。"""
    collector = StatsCollector(db)
    _insert(db, id="a")
    collector.rollup_hourly()
    report = pending_hourly(db.connect())
    assert report["missing"] == 0 and report["mismatched"] == 0


def test_pending_hourly_empty_db_spans_none(db):
    """空库：两侧跨度都是 None，不炸。"""
    report = pending_hourly(db.connect())
    assert report == {
        "missing": 0, "mismatched": 0, "samples": [],
        "events": 0, "hourly": 0,
        "events_span": None, "hourly_span": None,
    }


def test_main_preview_does_not_write(db, capsys):
    """默认预览：报缺口与样例，不写库。"""
    _insert(db, id="a")
    assert ROLLUP.main(["--db", db.path]) == 0
    out = capsys.readouterr().out
    assert "缺汇总行 1 个分组键" in out
    assert "缺汇总行" in out            # 样例行标注了原因
    assert "预览模式" in out
    assert db.connect().execute("SELECT COUNT(*) c FROM usage_hourly").fetchone()["c"] == 0


def test_main_reports_consistent_without_apply(db, capsys):
    """汇总已一致：无需重算。"""
    _insert(db, id="a")
    StatsCollector(db).rollup_hourly()
    assert ROLLUP.main(["--db", db.path]) == 0
    assert "小时汇总与明细一致，无需重算" in capsys.readouterr().out


def test_main_warns_when_hourly_starts_later(db, capsys):
    """汇总起点晚于明细起点：提示该段明细可能已清理、无法还原。"""
    _insert(db, id="a", ts=1_700_000_000)
    _insert(db, id="b", ts=1_700_000_000 + 7200)
    StatsCollector(db).rollup_hourly()
    with db.transaction() as conn:      # 抹掉早那一小时的汇总，模拟历史缺口
        conn.execute("DELETE FROM usage_hourly WHERE hour_utc < 1700003600")
    assert ROLLUP.main(["--db", db.path]) == 0
    assert "可能已被清理，无法还原" in capsys.readouterr().out


def test_main_apply_rolls_up_and_backs_up(db, capsys):
    """--apply：备份 + 全量重算，缺口归零。"""
    _insert(db, id="a")
    _insert(db, id="b", model="kimi-k3", cached_tokens=7)
    assert ROLLUP.main(["--db", db.path, "--apply"]) == 0
    assert "已重算 2 行小时汇总" in capsys.readouterr().out
    assert list(Path(db.path).parent.glob("t.sqlite3.bak-*"))
    report = pending_hourly(db.connect())
    assert report["missing"] == 0 and report["mismatched"] == 0

    # 幂等：重跑无缺口可补
    assert ROLLUP.main(["--db", db.path, "--apply"]) == 0
    assert "小时汇总与明细一致，无需重算" in capsys.readouterr().out


def test_main_rejects_missing_db(tmp_path):
    """库不存在：argparse 报错退出。"""
    with pytest.raises(SystemExit):
        ROLLUP.main(["--db", str(tmp_path / "nope.sqlite3")])
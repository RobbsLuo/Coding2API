"""scripts/backfill_trae_credit.py 的历史明细补齐与汇总重算测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from src.db.conn import Database
from src.db.migrate import apply_schema

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backfill_trae_credit.py"


def _load():
    spec = importlib.util.spec_from_file_location("backfill_trae_credit", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


BACKFILL = _load()


def _insert(db, *, id, ts, provider, model, input_tokens, output_tokens,
            cached_tokens=None, credit=None):
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO usage_events (id, ts, username, provider, credential_id, model, ok,"
            " input_tokens, output_tokens, cached_tokens, credit, credit_estimated)"
            " VALUES (?,?,?,?,?,?,1,?,?,?,?,0)",
            (id, ts, "u", provider, None, model, input_tokens, output_tokens,
             cached_tokens, credit))


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "t.sqlite3")
    apply_schema(database.connect())
    yield database
    database.close()


def test_pending_lists_only_priced_trae_rows(db):
    """预览只统计「trae + credit 为空 + 模型已收录」的行。"""
    _insert(db, id="a", ts=1_700_000_000, provider="trae", model="glm-5.2",
            input_tokens=1_000_000, output_tokens=0)
    _insert(db, id="b", ts=1_700_000_000, provider="trae", model="glm-5.2",
            input_tokens=1_000_000, output_tokens=0)
    _insert(db, id="c", ts=1_700_000_000, provider="trae", model="sagitta",
            input_tokens=10, output_tokens=1)                      # 未收录 → 不计
    _insert(db, id="d", ts=1_700_000_000, provider="codebuddy", model="glm-5.2",
            input_tokens=1_000_000, output_tokens=0)               # 非 trae → 不计
    _insert(db, id="e", ts=1_700_000_000, provider="trae", model="glm-5.2",
            input_tokens=1_000_000, output_tokens=0, credit=9.9)   # 已有真值 → 不计
    assert BACKFILL.pending(db.connect()) == [("glm-5.2", 2, 432.0)]


def test_main_preview_does_not_write(db, capsys):
    """默认预览：不写库，打印待补统计。"""
    _insert(db, id="a", ts=1_700_000_000, provider="trae", model="glm-5.2",
            input_tokens=1_000_000, output_tokens=0)
    assert BACKFILL.main(["--db", db.path]) == 0
    out = capsys.readouterr().out
    assert "共 1 条明细待补" in out and "预览模式" in out
    row = db.connect().execute(
        "SELECT credit FROM usage_events WHERE id = 'a'").fetchone()
    assert row["credit"] is None


def test_main_empty_reports_nothing_to_do(db, capsys):
    """无待补行：直接返回，不提示预览。"""
    assert BACKFILL.main(["--db", db.path]) == 0
    assert "没有需要补的" in capsys.readouterr().out


def test_main_apply_backfills_and_rolls_up(db, capsys):
    """--apply：备份 + 补明细 + 重算小时汇总。"""
    _insert(db, id="a", ts=1_700_000_000, provider="trae", model="glm-5.2",
            input_tokens=1_000_000, output_tokens=0)
    assert BACKFILL.main(["--db", db.path, "--apply"]) == 0
    assert "已补 1 条明细" in capsys.readouterr().out
    event = db.connect().execute(
        "SELECT credit, credit_estimated FROM usage_events WHERE id = 'a'").fetchone()
    assert (event["credit"], event["credit_estimated"]) == (216.0, 1)
    hourly = db.connect().execute(
        "SELECT credit_sum, credit_known, credit_estimated_known FROM usage_hourly"
    ).fetchone()
    assert (hourly["credit_sum"], hourly["credit_known"],
            hourly["credit_estimated_known"]) == (216.0, 1, 1)
    assert list(Path(db.path).parent.glob("t.sqlite3.bak-*"))


def test_main_rejects_missing_db(tmp_path):
    """库不存在：argparse 报错退出。"""
    with pytest.raises(SystemExit):
        BACKFILL.main(["--db", str(tmp_path / "nope.sqlite3")])
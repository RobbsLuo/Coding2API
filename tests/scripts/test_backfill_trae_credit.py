"""scripts/backfill_trae_credit.py 的历史明细补齐与汇总重算测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.paths import load_script, script_path

SCRIPT = script_path("backfill_trae_credit")


def _load():
    return load_script("backfill_trae_credit")


BACKFILL = _load()


def _insert(db, *, id, ts, provider, model, input_tokens, output_tokens,
            cached_tokens=None, credit=None, estimated=0):
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO usage_events (id, ts, username, provider, credential_id, model, ok,"
            " input_tokens, output_tokens, cached_tokens, credit, credit_estimated)"
            " VALUES (?,?,?,?,?,?,1,?,?,?,?,?)",
            (id, ts, "u", provider, None, model, input_tokens, output_tokens,
             cached_tokens, credit, estimated))


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
    assert BACKFILL.pending(db.connect()) == [("glm-5.2", 2, 432.0, 0)]


def test_pending_includes_stale_estimated_rows(db):
    """旧推算值（credit_estimated=1 且与当前价不符）计入重算；一致的跳过。"""
    _insert(db, id="a", ts=1_700_000_000, provider="trae", model="deepseek-v4.1-flash",
            input_tokens=1_000_000, output_tokens=0, credit=0.56, estimated=1)
    _insert(db, id="b", ts=1_700_000_000, provider="trae", model="glm-5.2",
            input_tokens=1_000_000, output_tokens=0, credit=216.0, estimated=1)  # 已最新
    _insert(db, id="c", ts=1_700_000_000, provider="trae", model="glm-5.2",
            input_tokens=1_000_000, output_tokens=0, credit=9.9, estimated=0)    # 上游真值
    assert BACKFILL.pending(db.connect()) == [("deepseek-v4.1-flash", 1, 28.0, 1)]


def _set_ratio(db, value: str) -> None:
    """写假定命中率热更覆盖：让断言不依赖本地 .env / env 默认值。"""
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO runtime_settings (key, value, updated_at)"
            " VALUES ('trae_assumed_cache_ratio', ?, 0)", (value,))


def test_main_preview_does_not_write(db, capsys):
    """默认预览：不写库，打印待补统计。"""
    _insert(db, id="a", ts=1_700_000_000, provider="trae", model="glm-5.2",
            input_tokens=1_000_000, output_tokens=0)
    _set_ratio(db, "0")
    assert BACKFILL.main(["--db", db.path]) == 0
    out = capsys.readouterr().out
    assert "共 1 条明细待处理" in out and "预览模式" in out
    row = db.connect().execute(
        "SELECT credit FROM usage_events WHERE id = 'a'").fetchone()
    assert row["credit"] is None


def test_main_empty_reports_nothing_to_do(db, capsys):
    """无待补行：直接返回，不提示预览。"""
    _set_ratio(db, "0")
    assert BACKFILL.main(["--db", db.path]) == 0
    assert "没有需要处理的" in capsys.readouterr().out


def test_main_apply_backfills_and_rolls_up(db, capsys):
    """--apply：备份 + 补明细 + 重算小时汇总。"""
    _insert(db, id="a", ts=1_700_000_000, provider="trae", model="glm-5.2",
            input_tokens=1_000_000, output_tokens=0)
    _set_ratio(db, "0")
    assert BACKFILL.main(["--db", db.path, "--apply"]) == 0
    assert "已处理 1 条明细" in capsys.readouterr().out
    event = db.connect().execute(
        "SELECT credit, credit_estimated FROM usage_events WHERE id = 'a'").fetchone()
    assert (event["credit"], event["credit_estimated"]) == (216.0, 1)
    hourly = db.connect().execute(
        "SELECT credit_sum, credit_known, credit_estimated_known FROM usage_hourly"
    ).fetchone()
    assert (hourly["credit_sum"], hourly["credit_known"],
            hourly["credit_estimated_known"]) == (216.0, 1, 1)
    assert list(Path(db.path).parent.glob("t.sqlite3.bak-*"))


def test_main_applies_assumed_ratio_to_unreported_rows(db, capsys):
    """cached_tokens 为 NULL（上游未上报）时按假定命中率折算，不按全价高估。"""
    _insert(db, id="a", ts=1_700_000_000, provider="trae", model="glm-5.2",
            input_tokens=1_000_000, output_tokens=0)
    _set_ratio(db, "0.95")
    assert BACKFILL.main(["--db", db.path, "--apply"]) == 0
    event = db.connect().execute(
        "SELECT credit FROM usage_events WHERE id = 'a'").fetchone()
    assert event["credit"] == pytest.approx(62.1)


def test_pending_keeps_true_zero_and_handles_bad_ratio(db):
    """真实上报的 0 不折算；坏比例 / 无输入都退回不推算。"""
    _insert(db, id="a", ts=1_700_000_000, provider="trae", model="glm-5.2",
            input_tokens=1_000_000, output_tokens=0, cached_tokens=0)
    assert BACKFILL.pending(db.connect(), 0.95) == [("glm-5.2", 1, 216.0, 0)]
    _insert(db, id="b", ts=1_700_000_001, provider="trae", model="glm-5.2",
            input_tokens=1_000_000, output_tokens=0)
    assert BACKFILL.pending(db.connect(), 0.0) == [("glm-5.2", 2, 432.0, 0)]


def test_effective_cached_edges():
    """推算只在合法输入/比例下生效，且不超过输入总量。"""
    eff = BACKFILL._effective_cached
    assert eff(None, 100, 0.95) == 95
    assert eff(None, 100, 1.5) is None          # ratio 越界不在脚本侧静默钳
    assert eff(None, None, 0.95) is None
    assert eff(None, True, 0.95) is None
    assert eff(None, -1, 0.95) is None
    assert eff(None, 100, None) is None
    assert eff(7, 100, 0.95) == 7               # 上游真值优先


def test_main_apply_recomputes_stale_estimates(db, capsys):
    """--apply 把过期的旧推算值刷新到当前价，且不动上游真值。"""
    _insert(db, id="a", ts=1_700_000_000, provider="trae", model="deepseek-v4.1-flash",
            input_tokens=1_000_000, output_tokens=0, credit=0.56, estimated=1)
    _insert(db, id="b", ts=1_700_000_000, provider="trae", model="glm-5.2",
            input_tokens=1_000_000, output_tokens=0, credit=9.9, estimated=0)
    _set_ratio(db, "0")
    assert BACKFILL.main(["--db", db.path, "--apply"]) == 0
    out = capsys.readouterr().out
    assert "重算 1" in out and "已处理 1 条明细" in out
    rows = {r["id"]: (r["credit"], r["credit_estimated"]) for r in db.connect().execute(
        "SELECT id, credit, credit_estimated FROM usage_events")}
    assert rows["a"] == (28.0, 1)          # 旧推算 0.56 → 28.0
    assert rows["b"] == (9.9, 0)           # 上游真值原样保留
    hourly = db.connect().execute(
        "SELECT ROUND(SUM(credit_sum), 4) s, SUM(credit_known) k FROM usage_hourly").fetchone()
    assert (hourly["s"], hourly["k"]) == (37.9, 2)


def test_main_rejects_missing_db(tmp_path):
    """库不存在：argparse 报错退出。"""
    with pytest.raises(SystemExit):
        BACKFILL.main(["--db", str(tmp_path / "nope.sqlite3")])
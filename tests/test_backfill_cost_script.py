"""scripts/backfill_cost.py 的历史成本重算与汇总重算测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from src.db.conn import Database
from src.db.migrate import apply_schema
from src.pricing import save_prices

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backfill_cost.py"
TABLE = {"glm-5.2": (1.0, 2.0, 0.5), "deepseek-v4.1-flash": (0.5, 1.5, 0.05)}


def _load():
    spec = importlib.util.spec_from_file_location("backfill_cost", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


BACKFILL = _load()


def _insert(db, *, id, ts=1_700_000_000, provider="trae", model="glm-5.2",
            input_tokens=1_000_000, output_tokens=0, cached_tokens=None,
            cost_usd=None, cost_cny=None):
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO usage_events (id, ts, username, provider, credential_id, model, ok,"
            " input_tokens, output_tokens, cached_tokens, cost_usd, cost_cny)"
            " VALUES (?,?,?,?,?,?,1,?,?,?,?,?)",
            (id, ts, "u", provider, None, model, input_tokens, output_tokens,
             cached_tokens, cost_usd, cost_cny))


def _set_rate(db, value: str) -> None:
    """写汇率热更覆盖：让断言不依赖本地 .env / env 默认值。"""
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO runtime_settings (key, value, updated_at) VALUES ('usd_cny_rate', ?, 0)",
            (value,))


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "t.sqlite3")
    apply_schema(database.connect())
    yield database
    database.close()


def test_pending_lists_only_changed_priced_rows(db):
    """只统计「能定价且与当前口径不同」的行：未收录 / 无输入 / 已最新都跳过。"""
    _insert(db, id="a", model="glm-5.2")                              # 补：无 cost
    _insert(db, id="b", model="glm-5.2", cost_usd=1.0, cost_cny=7.0)  # 已最新 → 跳过
    _insert(db, id="c", model="sagitta")                             # 未收录 → 跳过
    _insert(db, id="d", model="glm-5.2", input_tokens=None)          # 无输入 → 跳过
    assert BACKFILL.pending_cost(db.connect(), TABLE, 7.0) == [("glm-5.2", 1, 7.0)]


def test_recompute_costs_writes_and_is_idempotent(db):
    """改写缺失成本；重跑无变化返回 0（幂等）。"""
    _insert(db, id="a", model="deepseek-v4.1-flash", input_tokens=1_000_000,
            output_tokens=1_000_000)
    _insert(db, id="b", model="sagitta")                             # 未收录，保持 NULL
    assert BACKFILL.recompute_costs(db, TABLE, 7.0) == 1
    row = db.connect().execute(
        "SELECT cost_usd, cost_cny FROM usage_events WHERE id = 'a'").fetchone()
    assert (row["cost_usd"], row["cost_cny"]) == (2.0, 14.0)          # 0.5 + 1.5
    assert db.connect().execute(
        "SELECT COUNT(*) c FROM usage_events WHERE id = 'b' AND cost_usd IS NULL"
    ).fetchone()["c"] == 1
    assert BACKFILL.recompute_costs(db, TABLE, 7.0) == 0             # 幂等


def test_main_preview_does_not_write(db, tmp_path, capsys):
    """默认预览：不写库，打印待补统计。"""
    save_prices(str(tmp_path), TABLE)
    _set_rate(db, "7.0")
    _insert(db, id="a", model="glm-5.2")
    assert BACKFILL.main(["--db", db.path]) == 0
    out = capsys.readouterr().out
    assert "共 1 条明细待处理" in out and "预览模式" in out
    row = db.connect().execute(
        "SELECT cost_usd FROM usage_events WHERE id = 'a'").fetchone()
    assert row["cost_usd"] is None


def test_main_empty_reports_nothing_to_do(db, tmp_path, capsys):
    """有价表但无待补行：直接返回。"""
    save_prices(str(tmp_path), TABLE)
    assert BACKFILL.main(["--db", db.path]) == 0
    assert "没有需要处理的成本明细" in capsys.readouterr().out


def test_main_empty_price_table_aborts(db, tmp_path, capsys):
    """价表缺失：拒绝执行（否则会把所有行都「保持原样」，白跑一趟）。"""
    assert BACKFILL.main(["--db", db.path]) == 1
    assert "价表为空" in capsys.readouterr().out


def test_main_apply_backfills_and_rolls_up(db, tmp_path, capsys):
    """--apply：备份 + 补明细 + 重算小时汇总。"""
    save_prices(str(tmp_path), TABLE)
    _set_rate(db, "7.0")
    _insert(db, id="a", model="glm-5.2")
    assert BACKFILL.main(["--db", db.path, "--apply"]) == 0
    assert "已重算 1 条明细" in capsys.readouterr().out
    event = db.connect().execute(
        "SELECT cost_usd, cost_cny FROM usage_events WHERE id = 'a'").fetchone()
    assert (event["cost_usd"], event["cost_cny"]) == (1.0, 7.0)
    hourly = db.connect().execute(
        "SELECT cost_usd_sum, cost_cny_sum, cost_known FROM usage_hourly").fetchone()
    assert (hourly["cost_usd_sum"], hourly["cost_cny_sum"], hourly["cost_known"]) == (1.0, 7.0, 1)
    assert list(Path(db.path).parent.glob("t.sqlite3.bak-*"))


def test_main_apply_is_idempotent(db, tmp_path, capsys):
    """--apply 重跑：明细已是当前口径，无待处理行。"""
    save_prices(str(tmp_path), TABLE)
    _set_rate(db, "7.0")
    _insert(db, id="a", model="glm-5.2")
    assert BACKFILL.main(["--db", db.path, "--apply"]) == 0
    capsys.readouterr()
    assert BACKFILL.main(["--db", db.path, "--apply"]) == 0
    assert "没有需要处理的成本明细" in capsys.readouterr().out


def test_main_uses_explicit_data_dir(db, tmp_path, capsys):
    """--data-dir 指定价表快照目录（默认数据库同目录）。"""
    other = tmp_path / "snap"
    other.mkdir()
    save_prices(str(other), TABLE)
    _set_rate(db, "7.0")
    _insert(db, id="a", model="glm-5.2")
    assert BACKFILL.main(["--db", db.path, "--data-dir", str(other)]) == 0
    assert "共 1 条明细待处理" in capsys.readouterr().out


def test_main_rejects_missing_db(tmp_path):
    """库不存在：argparse 报错退出。"""
    with pytest.raises(SystemExit):
        BACKFILL.main(["--db", str(tmp_path / "nope.sqlite3")])

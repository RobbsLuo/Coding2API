"""scripts/backfill_codearts_credit.py 的历史明细补齐与汇总重算测试。

覆盖 `backfill_estimated_credit` 的全部跳过条件：非福利模型、失败请求、
token 全空、已有 credit（上游真值 / 旧推算值）都不动。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from src.db.conn import Database
from src.db.migrate import apply_schema
from src.provider.codearts.backfill import (
    backfill_estimated_credit,
    benefit_models,
)

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backfill_codearts_credit.py"


def _load():
    spec = importlib.util.spec_from_file_location("backfill_codearts_credit", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


BACKFILL = _load()


def _insert(db, *, id, ts, provider="codearts", model, ok=1, input_tokens=None,
            output_tokens=None, credit=None, estimated=0):
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO usage_events (id, ts, username, provider, credential_id, model, ok,"
            " error_type, input_tokens, output_tokens, credit, credit_estimated)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (id, ts, "u", provider, None, model, ok,
             None if ok else "invalid_request", input_tokens, output_tokens, credit, estimated))


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "c.sqlite3")
    apply_schema(database.connect())
    yield database
    database.close()


def _prove_benefit(db, model="deepseek-v4.1-flash"):
    """插一行已推算过的明细，证明该模型走过福利路由。

    `deepseek-v4.1-flash` 是账号按期授予的福利模型、不在冷启动种子里，回填靠的
    正是这条证据（真实库里同模型有 20+ 条 `credit_estimated=1` 的行）。
    """
    _insert(db, id="proof", ts=1, model=model, input_tokens=10, output_tokens=1,
            credit=0.0011, estimated=1)


def test_benefit_models_from_evidence_plus_seed(db):
    """福利模型集合 = 曾记过推算积分的模型 ∪ 冷启动种子。"""
    _insert(db, id="a", ts=1, model="deepseek-v4.1-flash", input_tokens=10,
            output_tokens=1, credit=0.0011, estimated=1)
    _insert(db, id="b", ts=1, model="openpangu-2.0-flash", input_tokens=10,
            output_tokens=1, credit=0.5, estimated=0)          # 上游真值 → 不算证据
    _insert(db, id="c", ts=1, provider="trae", model="glm-5.2", input_tokens=10,
            output_tokens=1, credit=0.2, estimated=1)          # 非 codearts → 不算证据
    assert benefit_models(db.connect()) == {"deepseek-v4.1-flash", "glm-5.3-flash",
                                           "deepseek-v4-flash-0731", "deepseek-v4-pro-0813"}


def test_backfill_fills_only_proven_benefit_rows(db):
    """只补福利模型的成功行；内置模型 / 失败行 / 空 token / 已有值都不动。"""
    _prove_benefit(db)
    _insert(db, id="benefit", ts=1, model="deepseek-v4.1-flash",
            input_tokens=2_000_000, output_tokens=1_000)
    _insert(db, id="builtin", ts=1, model="openpangu-2.0-flash",
            input_tokens=2_000_000, output_tokens=1_000)        # 无证据 → 不动
    _insert(db, id="failed", ts=1, model="deepseek-v4.1-flash", ok=0,
            input_tokens=2_000_000, output_tokens=1_000)        # 失败 → 不动
    _insert(db, id="notoken", ts=1, model="deepseek-v4.1-flash")  # 无 token → 不动
    _insert(db, id="truth", ts=1, model="deepseek-v4.1-flash", input_tokens=1,
            output_tokens=1, credit=9.9, estimated=0)          # 上游真值 → 不动
    _insert(db, id="other", ts=1, provider="trae", model="deepseek-v4.1-flash",
            input_tokens=100, output_tokens=0)                 # 非 codearts → 不动

    assert backfill_estimated_credit(db) == 1
    rows = {r["id"]: (r["credit"], r["credit_estimated"]) for r in db.connect().execute(
        "SELECT id, credit, credit_estimated FROM usage_events")}
    assert rows["benefit"] == (200.1, 1)
    assert rows["proof"] == (0.0011, 1)      # 证据行本身有值，天然不动
    assert rows["builtin"] == (None, 0)
    assert rows["failed"] == (None, 0)
    assert rows["notoken"] == (None, 0)
    assert rows["truth"] == (9.9, 0)
    assert rows["other"] == (None, 0)


def test_backfill_is_idempotent(db):
    """第二次执行返回 0（只动 credit 为空的行）。"""
    _prove_benefit(db)
    _insert(db, id="a", ts=1, model="deepseek-v4.1-flash", input_tokens=10, output_tokens=0)
    assert backfill_estimated_credit(db) == 1
    assert backfill_estimated_credit(db) == 0


def test_backfill_counts_zero_tokens(db):
    """成功但 token 全 0 的行折成 0 积分（区别于 token 全空的行）。"""
    _prove_benefit(db)
    _insert(db, id="a", ts=1, model="deepseek-v4.1-flash", input_tokens=0, output_tokens=0)
    assert backfill_estimated_credit(db) == 1
    row = db.connect().execute(
        "SELECT credit, credit_estimated FROM usage_events WHERE id = 'a'").fetchone()
    assert (row["credit"], row["credit_estimated"]) == (0.0, 1)


def test_pending_matches_backfill_scope(db):
    """预览与实际处理范围一致（福利模型 × 成功 × 有 token × credit 为空）。"""
    _prove_benefit(db)
    _insert(db, id="benefit", ts=1, model="deepseek-v4.1-flash",
            input_tokens=2_000_000, output_tokens=1_000)
    _insert(db, id="builtin", ts=1, model="openpangu-2.0-flash",
            input_tokens=2_000_000, output_tokens=1_000)
    _insert(db, id="failed", ts=1, model="deepseek-v4.1-flash", ok=0,
            input_tokens=2_000_000, output_tokens=1_000)
    _insert(db, id="notoken", ts=1, model="deepseek-v4.1-flash")
    assert BACKFILL.pending(db.connect()) == [("deepseek-v4.1-flash", 1, pytest.approx(200.1))]


def test_main_preview_does_not_write(db, capsys):
    """默认预览：不写库。"""
    _prove_benefit(db)
    _insert(db, id="a", ts=1, model="deepseek-v4.1-flash", input_tokens=1_000_000)
    assert BACKFILL.main(["--db", db.path]) == 0
    out = capsys.readouterr().out
    assert "共 1 条明细待处理" in out and "预览模式" in out
    row = db.connect().execute(
        "SELECT credit FROM usage_events WHERE id = 'a'").fetchone()
    assert row["credit"] is None


def test_main_empty_reports_nothing_to_do(db, capsys):
    """无待补行：直接返回。"""
    assert BACKFILL.main(["--db", db.path]) == 0
    assert "没有需要处理的" in capsys.readouterr().out


def test_main_apply_backfills_and_rolls_up(db, capsys):
    """--apply：备份 + 补明细 + 重算小时汇总。"""
    _prove_benefit(db)
    _insert(db, id="a", ts=1_700_000_000, model="deepseek-v4.1-flash",
            input_tokens=2_000_000, output_tokens=1_000)
    assert BACKFILL.main(["--db", db.path, "--apply"]) == 0
    assert "已处理 1 条明细" in capsys.readouterr().out
    event = db.connect().execute(
        "SELECT credit, credit_estimated FROM usage_events WHERE id = 'a'").fetchone()
    assert (event["credit"], event["credit_estimated"]) == (200.1, 1)
    hourly = db.connect().execute(
        "SELECT credit_sum, credit_known, credit_estimated_known FROM usage_hourly "
        "WHERE hour_utc = 1699999200").fetchone()      # 证据行在另一个小时，不参与本断言
    assert (hourly["credit_sum"], hourly["credit_known"],
            hourly["credit_estimated_known"]) == (200.1, 1, 1)
    assert list(Path(db.path).parent.glob("c.sqlite3.bak-*"))


def test_main_rejects_missing_db(tmp_path):
    """库不存在：argparse 报错退出。"""
    with pytest.raises(SystemExit):
        BACKFILL.main(["--db", str(tmp_path / "nope.sqlite3")])
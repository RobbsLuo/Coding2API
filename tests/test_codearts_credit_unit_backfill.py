"""CodeArts token→积分 历史数据折算：backfill 模块与 scripts 脚本测试。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from src.db.conn import Database
from src.db.migrate import apply_schema
from src.provider.codearts.backfill import (
    _ladder_to_credits,
    _number_to_credits,
    _packages_to_credits,
    convert_codearts_credit_unit,
    pending,
)

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "convert_codearts_credit_unit.py"


def _load():
    spec = importlib.util.spec_from_file_location("convert_codearts_credit_unit", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CONVERT = _load()


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "t.sqlite3")
    apply_schema(database.connect())
    yield database
    database.close()


def _cred(db, *, id, provider, remaining=None, total=None, ladder=None, packages=None):
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO credentials (id, provider, data_enc, quota_remaining, quota_total,"
            " quota_expiry_ladder, quota_packages, created_at)"
            " VALUES (?,?,'x',?,?,?,?,1)",
            (id, provider, remaining, total, ladder, packages))


def _event(db, *, id, provider, credit, ts=1_700_000_000):
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO usage_events (id, ts, username, provider, model, ok, credit)"
            " VALUES (?,?, 'u', ?, 'm', 1, ?)", (id, ts, provider, credit))


def _hourly(db, *, provider, credit_sum, known, ts=1_700_000_000):
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO usage_hourly (hour_utc, username, provider, model, requests,"
            " ok_count, credit_sum, credit_known, credit_estimated_known)"
            " VALUES (?, 'u', ?, 'm', 1, 1, ?, ?, ?)",
            ((ts // 3600) * 3600, provider, credit_sum, known, known))


def _credit_event(db, *, id, credential_id, before=None, after=None, delta=None):
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO credit_events (id, credential_id, ts, before, after, delta, source)"
            " VALUES (?,?,1,?,?,?,'observed')",
            (id, credential_id, before, after, delta))


def test_number_to_credits_passes_non_numbers_through():
    assert _number_to_credits(10000) == 1
    assert _number_to_credits(726.0) == pytest.approx(0.0726)
    # bool 不是数值、字符串/None 保留原样（历史脏数据不因折算改变形状）
    assert _number_to_credits(True) is True
    assert _number_to_credits("x") == "x"
    assert _number_to_credits(None) is None


def test_ladder_to_credits_handles_shapes_and_dirty_data():
    assert _ladder_to_credits(None) is None
    assert _ladder_to_credits("") == ""
    assert _ladder_to_credits("not json") == "not json"
    assert _ladder_to_credits('{"a": 1}') == '{"a": 1}'
    # 正常阶梯：金额折算，epoch 原样
    assert json.loads(_ladder_to_credits("[[1790870400, 100000.0]]")) == [[1790870400, 10.0]]
    # 畸形条目原样保留，其余照折
    assert json.loads(_ladder_to_credits('[[1, 10000], "bad"]')) == [[1, 1.0], "bad"]


def test_packages_to_credits_handles_shapes_and_dirty_data():
    assert _packages_to_credits(None) is None
    assert _packages_to_credits("") == ""
    assert _packages_to_credits("not json") == "not json"
    assert _packages_to_credits('{"a": 1}') == '{"a": 1}'
    # 只折 total/used，其它字段（含中文包名）原样保留
    out = json.loads(_packages_to_credits(
        '[{"name": "福利积分", "total": 20000, "used": 5000, "end": 9}]'))
    assert out == [{"name": "福利积分", "total": 2.0, "used": 0.5, "end": 9}]
    # 缺 total/used 的包与畸形条目原样保留
    assert json.loads(_packages_to_credits('[{"name": "x"}, 5]')) == [{"name": "x"}, 5]


def test_pending_counts_only_codearts(db):
    _cred(db, id="c1", provider="codearts", remaining=10000)
    _cred(db, id="c2", provider="codebuddy", remaining=10000)
    _event(db, id="e1", provider="codearts", credit=10000)
    _event(db, id="e2", provider="codearts", credit=None)
    _event(db, id="e3", provider="trae", credit=10000)
    _hourly(db, provider="codearts", credit_sum=10000, known=1)
    _credit_event(db, id="ce1", credential_id="c1", before=0, after=10000, delta=10000)
    _credit_event(db, id="ce2", credential_id="c2", before=0, after=10000, delta=10000)
    assert pending(db.connect()) == {
        "credentials": 1, "usage_events": 1, "usage_hourly": 1, "credit_events": 1,
    }


def test_convert_rescales_codearts_and_leaves_others(db):
    _cred(db, id="c1", provider="codearts", remaining=4414076.0, total=10000000.0,
          ladder="[[1790870400, 4414076.0]]",
          packages='[{"name": "p", "total": 10000000, "used": 1132}]')
    _cred(db, id="c2", provider="codebuddy", remaining=500.0, total=1000.0,
          ladder="[[1790870400, 500.0]]", packages=None)
    _event(db, id="e1", provider="codearts", credit=1845050.0)
    _event(db, id="e2", provider="codebuddy", credit=1845050.0)
    _hourly(db, provider="codearts", credit_sum=1845050.0, known=3)
    _hourly(db, provider="trae", credit_sum=1845050.0, known=3)
    _credit_event(db, id="ce1", credential_id="c1", before=0, after=4414076.0, delta=-1132.0)
    _credit_event(db, id="ce2", credential_id="c2", before=0, after=500.0, delta=-1.0)

    counts = convert_codearts_credit_unit(db)
    assert counts == {"credentials": 1, "usage_events": 1, "usage_hourly": 1,
                      "credit_events": 1}
    cred = db.connect().execute(
        "SELECT * FROM credentials WHERE id = 'c1'").fetchone()
    assert cred["quota_remaining"] == pytest.approx(441.4076)
    assert cred["quota_total"] == pytest.approx(1000.0)
    assert json.loads(cred["quota_expiry_ladder"]) == [[1790870400, 441.4076]]
    assert json.loads(cred["quota_packages"]) == [
        {"name": "p", "total": 1000.0, "used": 0.1132}]
    # 其它渠道原样不动
    other = db.connect().execute("SELECT * FROM credentials WHERE id = 'c2'").fetchone()
    assert other["quota_remaining"] == 500.0 and other["quota_total"] == 1000.0
    assert json.loads(other["quota_expiry_ladder"]) == [[1790870400, 500.0]]
    assert db.connect().execute(
        "SELECT credit FROM usage_events WHERE id = 'e1'").fetchone()[0] == \
        pytest.approx(184.505)
    assert db.connect().execute(
        "SELECT credit FROM usage_events WHERE id = 'e2'").fetchone()[0] == 1845050.0
    assert db.connect().execute(
        "SELECT credit_sum FROM usage_hourly WHERE provider = 'codearts'").fetchone()[0] == \
        pytest.approx(184.505)
    assert db.connect().execute(
        "SELECT credit_sum FROM usage_hourly WHERE provider = 'trae'").fetchone()[0] == 1845050.0
    ce1 = db.connect().execute(
        "SELECT * FROM credit_events WHERE id = 'ce1'").fetchone()
    assert ce1["after"] == pytest.approx(441.4076) and ce1["delta"] == pytest.approx(-0.1132)
    ce2 = db.connect().execute(
        "SELECT delta FROM credit_events WHERE id = 'ce2'").fetchone()[0]
    assert ce2 == -1.0


def test_main_preview_does_not_write(db, capsys):
    _cred(db, id="c1", provider="codearts", remaining=10000.0, total=10000000.0)
    assert CONVERT.main(["--db", db.path]) == 0
    out = capsys.readouterr().out
    assert "凭证额度" in out and "预览模式" in out
    assert db.connect().execute(
        "SELECT quota_remaining FROM credentials WHERE id = 'c1'").fetchone()[0] == 10000.0


def test_main_empty_reports_nothing(db, capsys):
    assert CONVERT.main(["--db", db.path]) == 0
    assert "没有需要折算的" in capsys.readouterr().out


def test_main_apply_backs_up_and_converts(db, capsys):
    _cred(db, id="c1", provider="codearts", remaining=10000.0, total=10000000.0)
    _event(db, id="e1", provider="codearts", credit=10000.0)
    assert CONVERT.main(["--db", db.path, "--apply"]) == 0
    out = capsys.readouterr().out
    assert "已折算" in out and "备份" in out
    assert db.connect().execute(
        "SELECT quota_remaining FROM credentials WHERE id = 'c1'").fetchone()[0] == 1.0
    assert db.connect().execute(
        "SELECT credit FROM usage_events WHERE id = 'e1'").fetchone()[0] == 1.0
    assert list(Path(db.path).parent.glob("t.sqlite3.bak-*"))


def test_main_rejects_missing_db(tmp_path):
    with pytest.raises(SystemExit):
        CONVERT.main(["--db", str(tmp_path / "nope.sqlite3")])

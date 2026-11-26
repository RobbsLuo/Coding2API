"""scripts/normalize_trae_cache.py：把 TRAE 未上报的缓存 0 归一为 NULL。"""

from __future__ import annotations

import pytest

from tests.helpers.paths import load_script, script_path

SCRIPT = script_path("normalize_trae_cache")


def _load():
    return load_script("normalize_trae_cache")


NORMALIZE = _load()

T0 = 1_700_000_000


def _insert(db, *, id, ts=T0, provider="trae", cached_tokens=0,
            input_tokens=1_000, output_tokens=10):
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO usage_events (id, ts, username, provider, credential_id, model,"
            " ok, input_tokens, output_tokens, cached_tokens)"
            " VALUES (?,?,?,?,NULL,'glm-5.3',1,?,?,?)",
            (id, ts, "u", provider, input_tokens, output_tokens, cached_tokens))


def _cached(db, id):
    return db.connect().execute(
        "SELECT cached_tokens FROM usage_events WHERE id = ?", (id,)).fetchone()[
            "cached_tokens"]


def test_cutoff_uses_last_real_hit(db):
    """分界 = 最后一条 cached_tokens>0 的 TRAE 明细。"""
    _insert(db, id="hit", ts=T0, cached_tokens=512)
    _insert(db, id="zero", ts=T0 + 60)
    _insert(db, id="other", ts=T0 + 120, provider="zen", cached_tokens=7)
    assert NORMALIZE.cutoff(db.connect()) == T0


def test_cutoff_none_without_real_hit(db):
    _insert(db, id="zero")
    assert NORMALIZE.cutoff(db.connect()) is None


def test_pending_counts_only_after_cutoff(db):
    """只统计分界之后的 TRAE 0 值行：之前的真实 0 与其他渠道都不算。"""
    _insert(db, id="old-zero", ts=T0 - 1)
    _insert(db, id="other", ts=T0 + 1, provider="codebuddy")
    _insert(db, id="a", ts=T0 + 10, input_tokens=100)
    _insert(db, id="b", ts=T0 + 20, input_tokens=200)
    report = NORMALIZE.pending(db.connect(), T0)
    assert report["rows"] == 2
    assert report["input_tokens"] == 300
    assert report["lo"] == T0 + 10 and report["hi"] == T0 + 20


def test_main_preview_does_not_write(db, capsys):
    _insert(db, id="hit", ts=T0, cached_tokens=512)
    _insert(db, id="a", ts=T0 + 10)
    assert NORMALIZE.main(["--db", db.path]) == 0
    out = capsys.readouterr().out
    assert "待置 NULL：1 条明细" in out and "预览模式" in out
    assert _cached(db, "a") == 0


def test_main_apply_nulls_and_rolls_up(db, capsys):
    """--apply：只把分界后的 0 置 NULL，并重算小时汇总。"""
    _insert(db, id="hit", ts=T0, cached_tokens=512)
    _insert(db, id="a", ts=T0 + 10)
    _insert(db, id="b", ts=T0 + 20)
    assert NORMALIZE.main(["--db", db.path, "--apply"]) == 0
    assert "已置 NULL 并重算小时汇总" in capsys.readouterr().out
    assert _cached(db, "a") is None
    assert _cached(db, "b") is None
    assert _cached(db, "hit") == 512            # 分界之前不动
    hourly = db.connect().execute(
        "SELECT SUM(cached_known) k, SUM(cached_input_tokens) c FROM usage_hourly"
    ).fetchone()
    assert (hourly["k"], hourly["c"]) == (1, 1000)   # 只剩真实命中那条


def test_main_requires_since_without_inference(db, capsys):
    """库里没有真实命中：拒绝猜分界，要求显式 --since。"""
    _insert(db, id="a")
    assert NORMALIZE.main(["--db", db.path]) == 1
    assert "--since" in capsys.readouterr().out


def test_main_since_accepts_epoch_and_iso(db, capsys):
    _insert(db, id="a", ts=T0 + 10)
    assert NORMALIZE.main(["--db", db.path, "--since", str(T0)]) == 0
    assert "待置 NULL：1 条明细" in capsys.readouterr().out
    assert NORMALIZE._parse_since("2026-10-24") == 1792800000
    assert NORMALIZE._parse_since("2026-10-24T00:00:00+00:00") == 1792800000
    with pytest.raises(ValueError):
        NORMALIZE._parse_since("not-a-date")


def test_main_empty_range(db, capsys):
    _insert(db, id="hit", ts=T0, cached_tokens=512)
    assert NORMALIZE.main(["--db", db.path]) == 0
    assert "没有需要修正的" in capsys.readouterr().out


def test_main_rejects_missing_db(tmp_path):
    with pytest.raises(SystemExit):
        NORMALIZE.main(["--db", str(tmp_path / "nope.sqlite3")])

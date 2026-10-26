"""scripts/cleanup_stale_blocked.py 的判定与删除测试。

背景（2026-10-26 实测）：`deepseek-v4.1-flash-1017` 只有 officeAce 的
`/v2/models` 挂着，探活判死后请求扇出到 codebuddy / codearts /
codebuddy_intl，三家各回「无此模型」→ 8 条负缓存写在了**从没有该模型**的
渠道上。负缓存的语义是「该后端无此模型」，前提是该后端曾被问过这个模型；
目录里没有 → 这条缓存拦不到任何东西 → 是噪声。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from src.db.conn import Database
from src.db.migrate import apply_schema
from tests.helpers.paths import load_script

CLEAN = load_script("cleanup_stale_blocked")


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "t.sqlite3")
    apply_schema(database.connect())
    yield database
    database.close()


def _credential(conn, provider: str) -> str:
    conn.execute(
        "INSERT INTO credentials (id, provider, data_enc, enabled, disabled, created_at) "
        "VALUES (?, ?, ?, 1, 0, ?)", (f"cred_{provider}", provider, b"{}", int(time.time())))
    return f"cred_{provider}"


def _blocked(conn, credential_id: str, model: str, *,
             until: int | None = None, reason: str = "blocked") -> None:
    conn.execute(
        "INSERT INTO credential_model_cooldowns "
        "(credential_id, model, cooling_until, hits, reason) VALUES (?,?,?,?,?)",
        (credential_id, model, until if until is not None else int(time.time()) + 3600,
         1, reason))


def _catalog(data_dir: Path, tables: dict[str, list[str]]) -> None:
    payload = {"version": 1, "providers": {
        provider_id: {"saved_at": time.time(),
                      "models": [{"id": model_id} for model_id in models]}
        for provider_id, models in tables.items()}}
    (data_dir / "model_catalog.json").write_text(
        json.dumps(payload), encoding="utf-8")


def test_stale_rows_only_when_channel_never_had_the_model(db, tmp_path):
    """目录里没有该模型的渠道 → 负缓存是扇出噪声；目录里有的 → 保留。"""
    conn = db.connect()
    _credential(conn, "officeace")
    _credential(conn, "codebuddy")
    _blocked(conn, "cred_officeace", "deepseek-v4.1-flash-1017")
    _blocked(conn, "cred_codebuddy", "deepseek-v4.1-flash-1017")
    conn.commit()
    _catalog(tmp_path, {"officeace": ["deepseek-v4.1-flash-1017"],
                        "codebuddy": ["deepseek-v4.1-flash"]})

    tables = CLEAN._catalog_models(tmp_path)
    stale = CLEAN.stale_rows(conn, tables)
    assert [(row[0], row[1]) for row in stale] == [
        ("codebuddy", "deepseek-v4.1-flash-1017")]


def test_expired_and_non_blocked_rows_are_ignored(db, tmp_path):
    """已过期的负缓存与其它 reason（模型限流）都不是本脚本的目标。"""
    conn = db.connect()
    _credential(conn, "codebuddy")
    _blocked(conn, "cred_codebuddy", "gone", until=int(time.time()) - 1)
    _blocked(conn, "cred_codebuddy", "limited", reason="model")
    conn.commit()
    _catalog(tmp_path, {"codebuddy": ["other"]})

    stale = CLEAN.stale_rows(conn, CLEAN._catalog_models(tmp_path))
    assert stale == []


def test_channels_absent_from_catalog_are_kept(db, tmp_path):
    """目录快照里没有该渠道 → 无从判断，保守保留（不删）。"""
    conn = db.connect()
    _credential(conn, "kilo")
    _blocked(conn, "cred_kilo", "m")
    conn.commit()
    _catalog(tmp_path, {"officeace": ["m"]})

    stale = CLEAN.stale_rows(conn, CLEAN._catalog_models(tmp_path))
    assert stale == []


def test_include_known_also_returns_catalogued_models(db, tmp_path):
    """`--all` 连「目录里登记过」的也返回（上游已恢复时强制清理）。"""
    conn = db.connect()
    _credential(conn, "officeace")
    _blocked(conn, "cred_officeace", "m")
    conn.commit()
    _catalog(tmp_path, {"officeace": ["m"]})

    tables = CLEAN._catalog_models(tmp_path)
    assert CLEAN.stale_rows(conn, tables) == []
    known = CLEAN.stale_rows(conn, tables, include_known=True)
    assert [(row[0], row[1]) for row in known] == [("officeace", "m")]


def test_delete_rows_removes_only_targeted_entries(db, tmp_path):
    """删除只命中待清理的行，其余负缓存原样保留。"""
    conn = db.connect()
    _credential(conn, "officeace")
    _credential(conn, "codebuddy")
    _blocked(conn, "cred_officeace", "m-ok")
    _blocked(conn, "cred_codebuddy", "m-stale")
    conn.commit()
    _catalog(tmp_path, {"officeace": ["m-ok"], "codebuddy": ["other"]})

    stale = CLEAN.stale_rows(conn, CLEAN._catalog_models(tmp_path))
    assert CLEAN.delete_rows(conn, stale) == 1
    conn.commit()
    remaining = [row[0] for row in conn.execute(
        "SELECT model FROM credential_model_cooldowns")]
    assert remaining == ["m-ok"]


def test_missing_or_broken_catalog_deletes_nothing(db, tmp_path, capsys):
    """没有可读目录快照 → 无判据，一律不删（绝不能把负缓存清空）。"""
    conn = db.connect()
    _credential(conn, "codebuddy")
    _blocked(conn, "cred_codebuddy", "m")
    conn.commit()
    db_path = Path(db.path)
    db.close()

    assert CLEAN.main(["--db", str(db_path), "--data-dir", str(tmp_path)]) == 0
    assert "无判据，不删任何行" in capsys.readouterr().out
    assert sqlite3.connect(str(db_path)).execute(
        "SELECT COUNT(*) FROM credential_model_cooldowns").fetchone()[0] == 1

    # 快照损坏：同样不删
    (tmp_path / "model_catalog.json").write_text("{broken", encoding="utf-8")
    assert CLEAN.main(["--db", str(db_path), "--data-dir", str(tmp_path)]) == 0
    assert sqlite3.connect(str(db_path)).execute(
        "SELECT COUNT(*) FROM credential_model_cooldowns").fetchone()[0] == 1


def test_main_preview_and_apply(db, tmp_path, capsys):
    """预览不动库；`--apply` 删除并留备份。"""
    conn = db.connect()
    _credential(conn, "codebuddy")
    _blocked(conn, "cred_codebuddy", "m-stale")
    conn.commit()
    _catalog(tmp_path, {"codebuddy": ["other"]})
    db_path = Path(db.path)
    db.close()

    assert CLEAN.main(["--db", str(db_path), "--data-dir", str(tmp_path)]) == 0
    assert "共 1 条负缓存" in capsys.readouterr().out
    assert sqlite3.connect(str(db_path)).execute(
        "SELECT COUNT(*) FROM credential_model_cooldowns").fetchone()[0] == 1

    assert CLEAN.main(["--db", str(db_path), "--data-dir", str(tmp_path),
                       "--apply"]) == 0
    assert "已删除 1 条负缓存" in capsys.readouterr().out
    assert sqlite3.connect(str(db_path)).execute(
        "SELECT COUNT(*) FROM credential_model_cooldowns").fetchone()[0] == 0
    assert list(tmp_path.glob("*.bak-*")), "apply 前必须留下备份"


def test_main_reports_nothing_to_clean(db, tmp_path, capsys):
    conn = db.connect()
    _credential(conn, "codebuddy")
    _blocked(conn, "cred_codebuddy", "m")
    conn.commit()
    _catalog(tmp_path, {"codebuddy": ["m"]})
    db_path = Path(db.path)
    db.close()

    assert CLEAN.main(["--db", str(db_path), "--data-dir", str(tmp_path)]) == 0
    assert "没有需要清理的负缓存" in capsys.readouterr().out


def test_main_rejects_missing_db(db, tmp_path):
    db.close()
    with pytest.raises(SystemExit):
        CLEAN.main(["--db", str(tmp_path / "nope.sqlite3")])

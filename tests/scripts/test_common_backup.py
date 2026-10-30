"""`scripts/_common.py`：运维脚本共享的写库前备份与保留策略。

这些脚本不在 `--cov=src` 范围内，但备份保留策略曾让本机 `data/` 涨到
870MB（28 份 `.bak-*` 从不清），所以行为必须有测试兜住。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from tests.helpers.paths import load_script

COMMON = load_script("_common")


def _make_db(path: Path, value: str = "v") -> Path:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("INSERT INTO t VALUES (?)", (value,))
    conn.commit()
    conn.close()
    return path


def test_backup_creates_snapshot_with_rows(tmp_path):
    """在线备份 API 落出的快照可独立打开且含数据。"""
    db_path = _make_db(tmp_path / "coding2api.sqlite3", "hello")
    target = COMMON.backup(db_path)
    assert target.exists()
    assert target.name.startswith("coding2api.sqlite3.bak-")
    conn = sqlite3.connect(str(target))
    try:
        assert conn.execute("SELECT x FROM t").fetchone() == ("hello",)
    finally:
        conn.close()


def test_backup_prunes_to_keep(tmp_path):
    """默认只留最近 5 份：一次性脚本每跑一次落 40MB+，不清会撑爆 data/。"""
    db_path = _make_db(tmp_path / "coding2api.sqlite3")
    for index in range(8):
        stale = tmp_path / f"coding2api.sqlite3.bak-2026010{index + 1}-000000"
        stale.write_text("old", encoding="utf-8")
        # mtime 递增，保证「最近 5 份」是确定的
        time.sleep(0.01)
    COMMON.backup(db_path, keep=5)
    remaining = sorted(tmp_path.glob("coding2api.sqlite3.bak-*"))
    assert len(remaining) == 5


def test_backup_keep_zero_disables_pruning(tmp_path):
    """`keep <= 0` = 不清理（保留全部，给需要人工比对的场景）。"""
    db_path = _make_db(tmp_path / "coding2api.sqlite3")
    for index in range(3):
        (tmp_path / f"coding2api.sqlite3.bak-2026010{index + 1}-000000").write_text(
            "old", encoding="utf-8")
    COMMON.backup(db_path, keep=0)
    assert len(list(tmp_path.glob("coding2api.sqlite3.bak-*"))) == 4


def test_prune_ignores_unlink_failure(tmp_path, monkeypatch):
    """清理失败不阻断备份：权限/竞态导致的 unlink 失败只跳过。"""
    db_path = _make_db(tmp_path / "coding2api.sqlite3")
    for index in range(3):
        (tmp_path / f"coding2api.sqlite3.bak-2026010{index + 1}-000000").write_text(
            "old", encoding="utf-8")

    real_unlink = Path.unlink

    def boom(self, *args, **kwargs):
        raise OSError("denied")

    monkeypatch.setattr(Path, "unlink", boom)
    try:
        COMMON._prune_backups(db_path, keep=1)
    finally:
        monkeypatch.setattr(Path, "unlink", real_unlink)
    # 没删掉也不抛；备份本身已经完成
    assert len(list(tmp_path.glob("coding2api.sqlite3.bak-*"))) == 3


def test_prune_survives_stat_failure(tmp_path, monkeypatch):
    """glob 后 stat 失败（文件被并发删除）时静默返回，不冒泡。"""
    db_path = _make_db(tmp_path / "coding2api.sqlite3")
    real_stat = Path.stat

    def boom(self, *args, **kwargs):
        raise OSError("gone")

    monkeypatch.setattr(Path, "stat", boom)
    try:
        COMMON._prune_backups(db_path, keep=1)
    finally:
        monkeypatch.setattr(Path, "stat", real_stat)

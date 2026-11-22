"""`tasks/backup.py` 与 `scripts/backup_db.py` / `scripts/restore_db.py`。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.tasks.backup import BackupTask
from tests.helpers.paths import load_script

BACKUP_SCRIPT = load_script("backup_db")
RESTORE_SCRIPT = load_script("restore_db")


def _make_db(path: Path, value: str = "v") -> Path:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("INSERT INTO t VALUES (?)", (value,))
    conn.commit()
    conn.close()
    return path


def _make_app_db(path: Path, value: str = "v") -> Path:
    """建一份**本服务的**库（核心表 + user_version），恢复用例必须用它。

    `restore_db` 走 `verify_app_database`：任意完好 sqlite 都能过
    integrity_check，但缺核心表的库换进生产会让服务启动即挂。
    """
    from src.db.migrate import apply_schema

    conn = sqlite3.connect(str(path))
    apply_schema(conn)
    conn.execute("CREATE TABLE marker (x TEXT)")
    conn.execute("INSERT INTO marker VALUES (?)", (value,))
    conn.commit()
    conn.close()
    return path


def test_backup_task_run_once_returns_report(tmp_path):
    """一轮 = 落一份快照 + 报告文件名（供管理台任务卡片展示）。"""
    db_path = _make_db(tmp_path / "coding2api.sqlite3")
    report = BackupTask(db_path).run_once()
    assert report["backup"].startswith("coding2api-")
    assert Path(str(report["path"])).exists()
    assert report["keep"] == 7


def test_backup_task_keep_is_live_callable(tmp_path):
    """保留份数是取值器：每轮现读，热更立即生效。"""
    db_path = _make_db(tmp_path / "coding2api.sqlite3")
    value = {"keep": 1}
    task = BackupTask(db_path, keep=lambda: value["keep"])
    assert task.run_once()["keep"] == 1
    value["keep"] = 3
    assert task.run_once()["keep"] == 3


def test_backup_script_writes_snapshot(tmp_path, capsys):
    db_path = _make_db(tmp_path / "coding2api.sqlite3")
    assert BACKUP_SCRIPT.main(["--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert "已备份" in out
    assert list((tmp_path / "backups").glob("coding2api-*.sqlite3"))


def test_backup_script_honors_keep_and_out(tmp_path, capsys):
    db_path = _make_db(tmp_path / "coding2api.sqlite3")
    out_dir = tmp_path / "snaps"
    for _ in range(4):
        assert BACKUP_SCRIPT.main(
            ["--db", str(db_path), "--keep", "2", "--out", str(out_dir)]) == 0
    capsys.readouterr()
    assert len(list(out_dir.glob("coding2api-*.sqlite3"))) == 2


def test_backup_script_rejects_missing_db(tmp_path):
    with pytest.raises(SystemExit):
        BACKUP_SCRIPT.main(["--db", str(tmp_path / "nope.sqlite3")])


def test_restore_script_preview_does_not_touch_db(tmp_path, capsys):
    """默认预览：校验备份、打印计划，不动主库。"""
    db_path = _make_app_db(tmp_path / "coding2api.sqlite3", "live")
    assert BACKUP_SCRIPT.main(["--db", str(db_path), "--out", str(tmp_path / "b")]) == 0
    backup = next((tmp_path / "b").glob("coding2api-*.sqlite3"))
    capsys.readouterr()

    assert RESTORE_SCRIPT.main([str(backup), "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert "备份校验通过" in out and "预览模式" in out
    conn = sqlite3.connect(str(db_path))
    assert [r[0] for r in conn.execute("SELECT x FROM marker")] == ["live"]
    conn.close()


def test_restore_script_apply_replaces_db(tmp_path, capsys):
    """--apply：主库被备份内容替换，并打印安全副本路径。"""
    db_path = _make_app_db(tmp_path / "coding2api.sqlite3", "old")
    assert BACKUP_SCRIPT.main(["--db", str(db_path), "--out", str(tmp_path / "b")]) == 0
    backup = next((tmp_path / "b").glob("coding2api-*.sqlite3"))
    # 备份后主库又变了，恢复应回到备份时的内容
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO marker VALUES ('new')")
    conn.commit()
    conn.close()
    capsys.readouterr()

    assert RESTORE_SCRIPT.main([str(backup), "--db", str(db_path), "--apply"]) == 0
    out = capsys.readouterr().out
    assert "已从备份恢复" in out and "安全副本" in out and "请重启服务" in out
    conn = sqlite3.connect(str(db_path))
    assert [r[0] for r in conn.execute("SELECT x FROM marker")] == ["old"]
    conn.close()
    assert list(tmp_path.glob("coding2api.sqlite3.pre-restore-*"))


def test_restore_script_rejects_foreign_sqlite_that_passes_integrity(
        tmp_path, capsys):
    """完好但不是本服务的库（缺核心表）必须拒绝，且主库保持原样。

    这是最容易出的事故：拿一个随手 `.sqlite3` 覆盖生产库，完整性校验全过，
    服务下次启动直接挂（缺表）。
    """
    db_path = _make_app_db(tmp_path / "coding2api.sqlite3", "live")
    foreign = _make_db(tmp_path / "foreign.sqlite3", "other")

    with pytest.raises(RuntimeError, match="不是本服务的数据库"):
        RESTORE_SCRIPT.main([str(foreign), "--db", str(db_path)])

    conn = sqlite3.connect(str(db_path))
    assert [r[0] for r in conn.execute("SELECT x FROM marker")] == ["live"]
    conn.close()
    # 预览阶段就拒绝，没有留下任何安全副本/临时文件
    assert not list(tmp_path.glob("*.pre-restore-*"))
    assert not list(tmp_path.glob("*.restore-tmp"))


def test_restore_script_rejects_corrupt_backup(tmp_path):
    """坏备份在预览阶段就被拦下（SystemExit 前先 verify 抛 RuntimeError）。"""
    db_path = _make_db(tmp_path / "coding2api.sqlite3")
    bad = tmp_path / "bad.sqlite3"
    bad.write_bytes(b"garbage")
    with pytest.raises(RuntimeError):
        RESTORE_SCRIPT.main([str(bad), "--db", str(db_path)])


def test_restore_script_rejects_missing_backup(tmp_path):
    with pytest.raises(SystemExit):
        RESTORE_SCRIPT.main([str(tmp_path / "nope.sqlite3"), "--db", str(tmp_path / "x")])

"""`Database` 的启动期权限门禁（issue #45）。

数据目录是「凭证密文 + 明文审计」的信任边界：收紧失败**默认阻断启动**，
而不是像以前那样 `suppress(OSError)` 静默继续。
"""

from __future__ import annotations

import stat

import pytest

from src.db.conn import Database
from src.security.private_files import PrivateAccessError


def test_database_tightens_parent_dir(tmp_path):
    target = tmp_path / "data"
    target.mkdir()
    target.chmod(0o755)
    db = Database(target / "coding2api.sqlite3")
    try:
        assert stat.S_IMODE(target.stat().st_mode) == 0o700
    finally:
        db.close()


def test_database_refuses_startup_when_not_private(tmp_path, monkeypatch):
    """收紧失败且目录本就不私有 → 启动失败，并给出可照做的提示。"""
    target = tmp_path / "shared"
    target.mkdir()
    target.chmod(0o755)

    def boom(_path, _mode):
        raise PermissionError("operation not permitted")

    monkeypatch.setattr("src.security.private_files.os.chmod", boom)
    with pytest.raises(PrivateAccessError) as excinfo:
        Database(target / "coding2api.sqlite3")
    assert "无法收紧" in str(excinfo.value)


def test_database_can_opt_out_of_strict_permissions(tmp_path, monkeypatch, caplog):
    """`scripts/*` 这类运维入口可放宽为「告警但不阻断」。"""
    target = tmp_path / "shared"
    target.mkdir()
    target.chmod(0o755)

    def boom(_path, _mode):
        raise PermissionError("operation not permitted")

    monkeypatch.setattr("src.security.private_files.os.chmod", boom)
    with caplog.at_level("WARNING"):
        db = Database(target / "coding2api.sqlite3", strict_permissions=False)
        db.close()
    assert "无法收紧" in caplog.text


def test_database_skips_relative_path(tmp_path, monkeypatch):
    """相对路径（parent 为空）不尝试收紧——没有目录可收，也不该抛。"""
    called: list[object] = []
    monkeypatch.setattr("src.security.private_files.secure_dir",
                        lambda path, **_kwargs: called.append(path))
    db = Database("plain.sqlite3")
    db.close()
    assert called == []

def test_database_tightens_db_file_and_sidecars(tmp_path):
    """库文件本身也要 0600：父目录 0700 只挡遍历，不挡直接读文件。

    sqlite3 建库按 umask（实测 0644），WAL/SHM 在首个写事务里才出现，
    所以必须在建连后收紧一次。
    """
    db = Database(tmp_path / "data" / "coding2api.sqlite3", strict_permissions=False)
    try:
        conn = db.connect()
        conn.execute("CREATE TABLE t(a INTEGER)")
        conn.commit()
        for name in ("coding2api.sqlite3", "coding2api.sqlite3-wal",
                     "coding2api.sqlite3-shm"):
            target = tmp_path / "data" / name
            if target.exists():
                assert stat.S_IMODE(target.stat().st_mode) == 0o600, name
    finally:
        db.close()


def test_database_secure_files_skips_missing_paths(tmp_path):
    """WAL/SHM 尚未创建时 `_secure_files` 不报错（首次建连的常态）。"""
    db = Database(tmp_path / "coding2api.sqlite3", strict_permissions=False)
    try:
        db._secure_files()          # 三件套都还不存在
        db._secure_files()          # 幂等
    finally:
        db.close()

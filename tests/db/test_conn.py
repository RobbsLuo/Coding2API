"""`Database` 的启动期权限门禁（issue #45）。

数据目录是「凭证密文 + 明文审计」的信任边界：收紧失败**默认阻断启动**，
而不是像以前那样 `suppress(OSError)` 静默继续。
"""

from __future__ import annotations

import sqlite3
import stat

import pytest

from src.db.conn import Database
from src.security.private_files import PrivateAccessError


def _make_db(path):
    """建一个普通（非 WAL）库并写入一行，供只读用例使用。"""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("INSERT INTO t VALUES ('v')")
    conn.commit()
    conn.close()
    return path


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


# ------------------------------------------------------------------ 只读连接


def test_read_only_connection_requires_existing_db(tmp_path):
    """预览/诊断入口打开不存在的库必须报错，且**不创建**任何文件。

    `cleanup_*` / `rollup_hourly` 的预览模式此前走普通连接：WAL pragma 会持久
    改写库头并建出 `-wal`/`-shm`，等于「预览」也在改库。
    """
    target = tmp_path / "missing.sqlite3"
    db = Database(target, read_only=True)
    try:
        with pytest.raises(sqlite3.Error):
            db.connect()
    finally:
        db.close()
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_read_only_connection_rejects_writes(tmp_path):
    """只读连接上的写操作由 SQLite 拒绝（不是靠调用方自觉）。"""
    target = _make_db(tmp_path / "coding2api.sqlite3")
    db = Database(target, read_only=True)
    try:
        conn = db.connect()
        assert conn.execute("SELECT x FROM t").fetchall()[0][0] == "v"
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t VALUES ('w')")
    finally:
        db.close()


def test_read_only_connection_does_not_set_wal(tmp_path):
    """只读连接不执行 `PRAGMA journal_mode=WAL`：库头的 journal mode 不变。"""
    target = _make_db(tmp_path / "coding2api.sqlite3")
    db = Database(target, read_only=True)
    try:
        conn = db.connect()
        # delete 是 SQLite 默认（未启用 WAL）的 journal mode
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        db.close()
    # 也没有建出 WAL 边车
    assert not (tmp_path / "coding2api.sqlite3-wal").exists()


def test_read_only_connection_skips_permission_tightening(tmp_path, monkeypatch):
    """只读连接不 chmod 库与边车、不建目录——预览不该有副作用。"""
    called: list[object] = []
    monkeypatch.setattr("src.db.conn.secure_file",
                        lambda path, **_kwargs: called.append(path))
    monkeypatch.setattr("src.db.conn.secure_dir",
                        lambda path, **_kwargs: called.append(path))
    db = Database(tmp_path / "sub" / "dir" / "x.sqlite3", read_only=True,
                  strict_permissions=False)
    try:
        db._secure_files()
    finally:
        db.close()
    assert called == []
    assert not (tmp_path / "sub").exists()


def test_default_connection_still_enables_wal(tmp_path):
    """默认（读写）行为不变：WAL 仍然开启。"""
    target = _make_db(tmp_path / "coding2api.sqlite3")
    db = Database(target, strict_permissions=False)
    try:
        assert db.connect().execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        db.close()


def test_close_invalidates_cached_connection(tmp_path):
    """`close()` 后同线程再 `connect()` 拿到新句柄（代际失效，不复用死连接）。"""
    db = Database(tmp_path / "coding2api.sqlite3", strict_permissions=False)
    try:
        first = db.connect()
        db.close()
        second = db.connect()
        assert second is not first
        # 新连接可用（旧句柄已关闭）
        assert second.execute("SELECT 1").fetchone()[0] == 1
        # 代际切换同时把事务深度归零：幂等，且让后续事务从最外层开始
        assert getattr(db._local, "tx_depth", 0) == 0
    finally:
        db.close()


def test_transaction_rolls_back_and_propagates(tmp_path):
    """事务异常回滚并原样抛出（批量统计写失败依赖这个语义）。"""
    db = Database(tmp_path / "coding2api.sqlite3", strict_permissions=False)
    try:
        conn = db.connect()
        conn.execute("CREATE TABLE t(x INTEGER)")
        conn.commit()
        with pytest.raises(RuntimeError), db.transaction() as tx:
            tx.execute("INSERT INTO t VALUES (1)")
            raise RuntimeError("boom")
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    finally:
        db.close()


def test_nested_transaction_only_outermost_commits(tmp_path):
    """嵌套事务只有最外层提交/回滚（内层异常不丢外层的未提交写入）。"""
    db = Database(tmp_path / "coding2api.sqlite3", strict_permissions=False)
    try:
        conn = db.connect()
        conn.execute("CREATE TABLE t(x INTEGER)")
        conn.commit()
        with db.transaction() as outer:
            outer.execute("INSERT INTO t VALUES (1)")
            with db.transaction() as inner:
                inner.execute("INSERT INTO t VALUES (2)")
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
        # 内层异常不提交外层：外层自己决定回滚
        with pytest.raises(RuntimeError), db.transaction() as outer:
            outer.execute("INSERT INTO t VALUES (3)")
            with db.transaction():
                raise RuntimeError("inner boom")
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
    finally:
        db.close()

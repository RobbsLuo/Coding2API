"""`src/db/backup.py` 的备份/恢复核心：快照、校验、保留、原子替换。"""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from src.db.backup import (
    BACKUP_DIRNAME,
    DEFAULT_KEEP,
    apply_pending_restore,
    backup_database,
    backup_dir,
    pending_restore_info,
    pending_restore_path,
    restore_database,
    stage_restore,
    verify_app_database,
    verify_database,
)
from src.db.conn import Database
from src.db.migrate import SCHEMA_VERSION, apply_schema


def _seed(db_path: Path, value: str = "hello") -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE IF NOT EXISTS t (x TEXT)")
    conn.execute("INSERT INTO t VALUES (?)", (value,))
    conn.commit()
    conn.close()


def _read(db_path: Path) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return [row[0] for row in conn.execute("SELECT x FROM t")]
    finally:
        conn.close()


def test_backup_dir_is_sibling_backups(tmp_path):
    """备份目录固定为主库同级的 `backups/`（DATA_DIR/backups/）。"""
    assert backup_dir(tmp_path / "coding2api.sqlite3") == tmp_path / BACKUP_DIRNAME


def test_backup_creates_verifiable_snapshot(tmp_path):
    """备份 = 可读的一致性快照，且落盘后能通过完整性校验。"""
    db_path = tmp_path / "coding2api.sqlite3"
    _seed(db_path)
    target = backup_database(db_path, now=1_700_000_000)
    assert target.parent == tmp_path / BACKUP_DIRNAME
    assert target.name.startswith("coding2api-")
    assert target.suffix == ".sqlite3"
    verify_database(target)          # 不抛即通过
    assert _read(target) == ["hello"]


def test_backup_file_is_owner_only(tmp_path):
    """库内是密文凭证 + 明文用量：备份文件 0600、目录 0700。"""
    db_path = tmp_path / "coding2api.sqlite3"
    _seed(db_path)
    target = backup_database(db_path)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


def test_backup_includes_uncheckpointed_wal(tmp_path):
    """WAL 里尚未 checkpoint 的已提交事务必须进快照（直接拷文件会丢）。"""
    db_path = tmp_path / "coding2api.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("INSERT INTO t VALUES ('wal-only')")
    conn.commit()                    # 仍在 WAL，未 checkpoint
    assert Path(f"{db_path}-wal").exists()
    target = backup_database(db_path)
    conn.close()
    assert _read(target) == ["wal-only"]


def test_backup_missing_source_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup_database(tmp_path / "nope.sqlite3")


def test_backup_same_second_gets_unique_names(tmp_path):
    """同一秒内两次备份不能撞名（VACUUM INTO 对已存在目标直接报错）。"""
    db_path = tmp_path / "coding2api.sqlite3"
    _seed(db_path)
    first = backup_database(db_path, now=1_700_000_000)
    second = backup_database(db_path, now=1_700_000_000)
    assert first != second and second.name.endswith("-1.sqlite3")
    assert _read(second) == ["hello"]


def test_backup_prunes_to_keep(tmp_path):
    """保留最近 N 份（按 mtime），更旧的删掉。"""
    db_path = tmp_path / "coding2api.sqlite3"
    _seed(db_path)
    directory = tmp_path / BACKUP_DIRNAME
    made = [backup_database(db_path, now=1_700_000_000 + i * 3600) for i in range(5)]
    # 名字带时间戳但排序用 mtime：显式把 mtime 拉开，避免同秒创建的歧义
    for index, path in enumerate(made):
        os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
    backup_database(db_path, keep=3, now=1_700_000_500)
    remaining = sorted(directory.glob("coding2api-*.sqlite3"))
    assert len(remaining) == 3
    assert made[0] not in remaining and made[1] not in remaining


def test_backup_keep_zero_disables_pruning(tmp_path):
    """keep<=0 = 不清理（只备份）。"""
    db_path = tmp_path / "coding2api.sqlite3"
    _seed(db_path)
    for i in range(4):
        backup_database(db_path, keep=0, now=1_700_000_000 + i)
    assert len(list((tmp_path / BACKUP_DIRNAME).glob("coding2api-*.sqlite3"))) == 4


def test_backup_custom_directory(tmp_path):
    """显式目录优先于默认的 backups/。"""
    db_path = tmp_path / "coding2api.sqlite3"
    _seed(db_path)
    target = backup_database(db_path, directory=tmp_path / "elsewhere")
    assert target.parent == tmp_path / "elsewhere"


def test_backup_failure_removes_partial_file(tmp_path, monkeypatch):
    """快照/校验失败必须删掉半成品：写坏的备份比没有更危险。"""
    db_path = tmp_path / "coding2api.sqlite3"
    _seed(db_path)
    calls = {"n": 0}

    def boom(path):
        calls["n"] += 1
        raise RuntimeError("bad backup")

    monkeypatch.setattr("src.db.backup.verify_database", boom)
    with pytest.raises(RuntimeError):
        backup_database(db_path)
    assert calls["n"] == 1
    assert list((tmp_path / BACKUP_DIRNAME).glob("coding2api-*.sqlite3")) == []


def test_verify_database_rejects_garbage(tmp_path):
    """非库文件 / 校验不通过一律抛 RuntimeError。"""
    junk = tmp_path / "junk.sqlite3"
    junk.write_bytes(b"not a database at all")
    with pytest.raises(RuntimeError):
        verify_database(junk)


def test_verify_database_rejects_corrupt_page(tmp_path):
    """文件头像库、页数据被破坏：integrity_check 非 ok 即拒。"""
    db_path = tmp_path / "coding2api.sqlite3"
    _seed(db_path, "x" * 5000)
    raw = bytearray(db_path.read_bytes())
    # 只破坏数据页（跳过 100 字节文件头），保留合法的 SQLite 魔数
    for offset in range(4096, min(len(raw), 8192)):
        raw[offset] ^= 0xFF
    db_path.write_bytes(bytes(raw))
    with pytest.raises(RuntimeError):
        verify_database(db_path)


def test_verify_database_missing_file(tmp_path):
    with pytest.raises(RuntimeError):
        verify_database(tmp_path / "nope.sqlite3")


def test_verify_database_rejects_empty_result(tmp_path, monkeypatch):
    """`integrity_check` 返回空行（非 'ok'）同样视为不通过。

    正常库恒返回一行 `ok`；这里桩掉连接以覆盖「拿不到结果」的防御分支。
    """

    class _Cursor:
        def fetchone(self):
            return None

    class _Conn:
        def execute(self, _sql):
            return _Cursor()

        def close(self):
            pass

    monkeypatch.setattr("src.db.backup.sqlite3.connect", lambda *a, **k: _Conn())
    with pytest.raises(RuntimeError, match="完整性校验未通过"):
        verify_database(tmp_path / "whatever.sqlite3")


def test_restore_replaces_db_and_keeps_safety_copy(tmp_path):
    """恢复：原子替换主库 + 留安全副本 + 清掉旧库的 -wal/-shm。"""
    db_path = tmp_path / "coding2api.sqlite3"
    _seed(db_path, "current")
    backup = backup_database(db_path, now=1_700_000_000)
    # 备份之后主库又变了，恢复应回到备份时的内容
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO t VALUES ('later')")
    conn.commit()
    conn.close()
    # 造出残留的 WAL/SHM：恢复后必须被清掉（否则新库被旧 WAL 污染）
    Path(f"{db_path}-wal").write_bytes(b"stale")
    Path(f"{db_path}-shm").write_bytes(b"stale")

    safety = restore_database(db_path, backup, now=1_700_000_100)

    assert safety is not None and safety.exists()
    assert safety.name.startswith("coding2api.sqlite3.pre-restore-")
    assert "current" in _read(safety) and "later" in _read(safety)
    assert _read(db_path) == ["current"]
    assert not Path(f"{db_path}-wal").exists()
    assert not Path(f"{db_path}-shm").exists()
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


def test_restore_without_existing_db_returns_none(tmp_path):
    """主库原本不存在：没有安全副本可留，返回 None。"""
    backup = tmp_path / "snap.sqlite3"
    _seed(backup, "only")
    target = tmp_path / "coding2api.sqlite3"
    assert restore_database(target, backup) is None
    assert _read(target) == ["only"]


def test_restore_rejects_corrupt_backup(tmp_path):
    """先验证再动手：坏备份绝不换进主库。"""
    db_path = tmp_path / "coding2api.sqlite3"
    _seed(db_path, "good")
    bad = tmp_path / "bad.sqlite3"
    bad.write_bytes(b"garbage")
    with pytest.raises(RuntimeError):
        restore_database(db_path, bad)
    assert _read(db_path) == ["good"]      # 原库未被改动


def test_restore_missing_backup_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        restore_database(tmp_path / "coding2api.sqlite3", tmp_path / "nope.sqlite3")


def test_restore_cleans_temp_on_write_failure(tmp_path, monkeypatch):
    """写临时文件失败时也要清掉临时文件，不留垃圾。"""
    db_path = tmp_path / "coding2api.sqlite3"
    _seed(db_path, "good")
    backup = backup_database(db_path, now=1_700_000_000)

    real_write = Path.write_bytes

    def failing_write(self, data):
        if self.name.endswith(".restore-tmp"):
            raise OSError("disk full")
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", failing_write)
    with pytest.raises(OSError):
        restore_database(db_path, backup)
    assert not (tmp_path / "coding2api.sqlite3.restore-tmp").exists()
    assert _read(db_path) == ["good"]


def test_default_keep_is_seven():
    assert DEFAULT_KEEP == 7


# ---------------------------------------------------------------- 管理台恢复


def _app_db(tmp_path: Path, name: str = "app.sqlite3") -> Path:
    path = tmp_path / name
    db = Database(path)
    apply_schema(db.connect())
    db.close()
    return path


def test_verify_app_database_accepts_real_schema(tmp_path):
    verify_app_database(_app_db(tmp_path))


def test_verify_app_database_rejects_foreign_sqlite(tmp_path):
    path = tmp_path / "plain.sqlite3"
    _seed(path)
    with pytest.raises(RuntimeError, match="缺少表"):
        verify_app_database(path)


def test_verify_app_database_rejects_newer_schema(tmp_path):
    path = _app_db(tmp_path)
    conn = sqlite3.connect(str(path))
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="高于本程序支持"):
        verify_app_database(path)


def test_stage_restore_validates_and_writes_pending(tmp_path):
    source = _app_db(tmp_path)
    db_path = tmp_path / "main.sqlite3"
    meta = stage_restore(db_path, source.read_bytes(), filename="../evil.sqlite3",
                         now=1234)
    assert meta == {"filename": "evil.sqlite3", "size": source.stat().st_size,
                    "uploaded_at": 1234}
    pending = pending_restore_path(db_path)
    assert pending.is_file()
    assert pending.stat().st_mode & stat.S_IRWXO == 0
    assert pending_restore_info(db_path)["filename"] == "evil.sqlite3"


def test_stage_restore_rejects_empty_and_corrupt(tmp_path):
    db_path = tmp_path / "main.sqlite3"
    with pytest.raises(RuntimeError, match="为空"):
        stage_restore(db_path, b"")
    with pytest.raises(RuntimeError):
        stage_restore(db_path, b"not a database")
    assert not pending_restore_path(db_path).exists()


def test_pending_restore_info_falls_back_without_or_with_broken_meta(tmp_path):
    db_path = tmp_path / "main.sqlite3"
    assert pending_restore_info(db_path) is None
    pending = pending_restore_path(db_path)
    pending.parent.mkdir(parents=True)
    pending.write_bytes(b"abc")
    info = pending_restore_info(db_path)  # 没有 meta → 用文件大小/mtime
    assert info["filename"] == pending.name and info["size"] == 3
    pending.with_name("pending.json").write_text("{broken", encoding="utf-8")
    assert pending_restore_info(db_path)["size"] == 3


def test_apply_pending_restore_replaces_db_and_keeps_safety_copy(tmp_path):
    db_path = _app_db(tmp_path, "main.sqlite3")
    source = _app_db(tmp_path, "source.sqlite3")
    conn = sqlite3.connect(str(source))
    conn.execute("CREATE TABLE marker (x TEXT)")
    conn.execute("INSERT INTO marker VALUES ('restored')")
    conn.commit()
    conn.close()

    stage_restore(db_path, source.read_bytes(), now=5)
    safety = apply_pending_restore(db_path, now=6)

    assert safety is not None and safety.is_file()
    assert not pending_restore_path(db_path).exists()
    conn = sqlite3.connect(str(db_path))
    assert conn.execute("SELECT x FROM marker").fetchone()[0] == "restored"
    conn.close()


def test_apply_pending_restore_rejects_corrupt_without_blocking_start(tmp_path):
    db_path = _app_db(tmp_path, "main.sqlite3")
    pending = pending_restore_path(db_path)
    pending.parent.mkdir(parents=True)
    pending.write_bytes(b"corrupt")

    assert apply_pending_restore(db_path, now=7) is None
    assert not pending.exists()
    rejected = list(pending.parent.glob("pending.sqlite3.rejected-*"))
    assert len(rejected) == 1
    # 老库仍在，且能正常打开
    conn = sqlite3.connect(str(db_path))
    assert conn.execute("SELECT 1").fetchone()[0] == 1
    conn.close()


def test_pending_restore_info_ignores_non_object_meta(tmp_path):
    db_path = tmp_path / "main.sqlite3"
    pending = pending_restore_path(db_path)
    pending.parent.mkdir(parents=True)
    pending.write_bytes(b"abc")
    pending.with_name("pending.json").write_text("[]", encoding="utf-8")
    info = pending_restore_info(db_path)  # 顶层不是对象 → 回落文件信息
    assert info["filename"] == pending.name and info["size"] == 3


def test_verify_app_database_reports_integrity_failure(tmp_path, monkeypatch):
    """`PRAGMA integrity_check` 返回非 ok（文件能打开但有逻辑损坏）→ RuntimeError。"""
    class _Result:
        def fetchone(self):
            return ("wrong page type",)

    class _Conn:
        def execute(self, _sql, *_args):
            return _Result()

        def close(self):
            pass

    monkeypatch.setattr("src.db.backup.sqlite3.connect", lambda *_a, **_k: _Conn())
    with pytest.raises(RuntimeError, match="完整性校验未通过"):
        verify_app_database(tmp_path / "x.sqlite3")

"""SQLite 备份 / 恢复的核心实现（后台任务与 scripts/ 共用一份）。

为什么不用「复制文件」：WAL 模式下主库文件不含尚未 checkpoint 的已提交事务，
直接 `shutil.copy` 会丢数据（scripts/_common.py 的 `backup()` 早就因此改用
在线备份 API）。这里用 `VACUUM INTO`——它同样读一份一致性快照，但额外把目标
库**重写成紧凑、无 WAL 的独立文件**：恢复时只需原子替换主库、删掉 `-wal`/
`-shm` 残留，不必担心源库的 journal 模式。

纪律：
- 备份写进 `DATA_DIR/backups/`（目录 0700、文件 0600）——库内是凭证密文与
  明文用量/审计，不能落到同机其他用户可读的位置。
- 落盘后**立刻 `PRAGMA integrity_check`**：一个写坏的备份比没有备份更危险
  （真出事时才发现恢复不了）。校验不过就删掉半成品并报错。
- 保留最近 N 份，按 mtime 倒序删旧（名字带时间戳，但排序用 mtime 更稳：
  手工拷进来的文件 mtime 也参与，不会被名字规则漏掉）。
- `VACUUM INTO` 目标文件已存在会直接 `OperationalError`，因此文件名按秒级
  时间戳生成，同秒冲突再补序号。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# 默认保留份数（后台任务与 CLI 的默认值；可经 BACKUP_KEEP 覆盖）。
DEFAULT_KEEP = 7
# 备份子目录名（相对 DATA_DIR）。
BACKUP_DIRNAME = "backups"


def backup_dir(db_path: str | Path) -> Path:
    """备份落盘目录：与主库同级的 `backups/`（DATA_DIR/backups/）。"""
    return Path(db_path).resolve().parent / BACKUP_DIRNAME


def _stamp(now: float | None) -> str:
    return datetime.fromtimestamp(now if now is not None else time.time(),
                                  tz=UTC).strftime("%Y%m%d-%H%M%S")


def verify_database(path: str | Path) -> None:
    """`PRAGMA integrity_check`；非 ok 或打不开即抛 RuntimeError。

    以只读 URI 打开：校验一个坏文件不该顺手在它旁边建出 `-wal`/`-shm`。
    """
    try:
        with closing(sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)) as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as error:
        raise RuntimeError(f"数据库无法打开或校验失败: {path}: {error}") from error
    result = row[0] if row else None
    if result != "ok":
        raise RuntimeError(f"数据库完整性校验未通过: {path}: {result!r}")


def _snapshot(source: Path, target: Path) -> None:
    """`VACUUM INTO target` 读一份一致性快照；目标必须不存在。

    busy_timeout 放宽到 30s：`VACUUM INTO` 期间若有写者在跑，等它而不是立刻
    失败（默认 5s 在并发写入时太短）。VACUUM 需要读锁，不会阻塞写入。
    """
    with closing(sqlite3.connect(str(source))) as conn:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("VACUUM INTO ?", (str(target),))


def _unique_target(directory: Path, stem: str, stamp: str) -> Path:
    """目标名 `{stem}-{stamp}.sqlite3`；同秒再备份时补 `-1`/`-2`。

    `VACUUM INTO` 对已存在的目标文件直接报错，故必须保证名字唯一。上限 100
    只是防御死循环——正常节奏（秒级）远达不到。
    """
    candidate = directory / f"{stem}-{stamp}.sqlite3"
    suffix = 1
    while candidate.exists():
        if suffix > 100:  # pragma: no cover - 同一秒内备份 100 次的防御上限
            raise RuntimeError(f"备份目标名冲突过多: {candidate}")
        candidate = directory / f"{stem}-{stamp}-{suffix}.sqlite3"
        suffix += 1
    return candidate


def _chmod(path: Path, mode: int) -> None:
    """chmod；失败（共享目录、他人属主）不阻断——与 Database.__init__ 同口径。"""
    with suppress(OSError):
        os.chmod(path, mode)


def _prune(directory: Path, stem: str, keep: int) -> int:
    """只保留最近 `keep` 份（按 mtime 倒序）；`keep <= 0` 表示不清理。

    只删本模块自己生成的 `{stem}-*.sqlite3`：目录里可能有人工留的其它文件
    （手工拷贝、`.pre-restore` 安全副本），一律不动。
    """
    if keep <= 0:
        return 0
    files = sorted(directory.glob(f"{stem}-*.sqlite3"),
                   key=lambda path: path.stat().st_mtime, reverse=True)
    removed = 0
    for stale in files[keep:]:
        stale.unlink()
        removed += 1
    return removed


def backup_database(db_path: str | Path, *, keep: int = DEFAULT_KEEP,
                    directory: str | Path | None = None,
                    now: float | None = None) -> Path:
    """把主库一致性快照写成独立文件，返回落盘路径。

    - `VACUUM INTO`：读一份一致性快照并重写为紧凑、无 WAL 的独立库；
    - 落盘后立即校验完整性，不过则删除半成品并抛错；
    - chmod 600（库内是密文凭证与明文用量/审计）；
    - 按 `keep` 清理旧备份（`keep <= 0` 不清理）。
    """
    source = Path(db_path)
    if not source.is_file():
        raise FileNotFoundError(f"主库不存在: {source}")
    directory = Path(directory) if directory is not None else backup_dir(source)
    directory.mkdir(parents=True, exist_ok=True)
    _chmod(directory, 0o700)
    target = _unique_target(directory, source.stem, _stamp(now))
    try:
        _snapshot(source, target)
        verify_database(target)
    except BaseException:
        # 半成品（写坏的备份）比没有更危险：校验/写入失败一律清掉再抛。
        target.unlink(missing_ok=True)
        raise
    _chmod(target, 0o600)
    _prune(directory, source.stem, keep)
    return target


def restore_database(db_path: str | Path, source: str | Path, *,
                     now: float | None = None) -> Path | None:
    """用 `source` 覆盖主库；返回「安全副本」路径（主库原本不存在时为 None）。

    步骤（顺序即安全性）：
    1. 校验 `source` 完整性——**先验证再动手**，否则会把坏文件换进生产库；
    2. 若主库存在，先给它做一份一致性快照 `.pre-restore-<stamp>`（同样走
       `VACUUM INTO`，不能直接拷主库文件——WAL 里可能有未 checkpoint 的事务）；
       万一恢复错了能回滚；
    3. 拷 `source` 到同目录临时文件 → `os.replace` 原子替换主库（跨文件系统
       rename 不原子，必须同目录）；
    4. 删除 `-wal`/`-shm` 残留——替换后它们属于**旧库**，留着会让 SQLite 把
       新主库与旧 WAL 拼在一起（可能直接损坏）。

    不做「服务是否在跑」的探测：那种探测不可靠（容器/其它主机/权限都可能
    误判），宁可要求调用方先停服。调用方负责在恢复后重启服务。
    """
    target = Path(db_path)
    src = Path(source)
    if not src.is_file():
        raise FileNotFoundError(f"备份文件不存在: {src}")
    verify_database(src)
    safety: Path | None = None
    if target.exists():
        safety = target.with_name(f"{target.name}.pre-restore-{_stamp(now)}")
        _snapshot(target, safety)
        verify_database(safety)
        _chmod(safety, 0o600)
    temp = target.with_name(f"{target.name}.restore-tmp")
    try:
        temp.write_bytes(src.read_bytes())
        _chmod(temp, 0o600)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    for sidecar in (f"{target.name}-wal", f"{target.name}-shm"):
        Path(target.parent, sidecar).unlink(missing_ok=True)
    return safety


# ---------------------------------------------------------------- 管理台恢复
# 运行中直接换库不安全：旧连接/未 checkpoint 的 WAL 会把新库拼坏。管理台上传
# 的备份先落 `DATA_DIR/restore/pending.sqlite3`，**下次启动、建连接之前**由
# `apply_pending_restore` 走 `restore_database` 原子替换；期间服务照常用老库。

PENDING_DIRNAME = "restore"
PENDING_NAME = "pending.sqlite3"
PENDING_META = "pending.json"
# 恢复要求的最小表集合：一份「随便什么 sqlite 文件」不能当备份恢复
REQUIRED_TABLES = ("credentials", "api_keys", "users")


def pending_restore_path(db_path: str | Path) -> Path:
    return Path(db_path).resolve().parent / PENDING_DIRNAME / PENDING_NAME


def verify_app_database(path: str | Path) -> None:
    """完整性 + 「确实是本服务的库」双校验。

    - `PRAGMA integrity_check`（同 `verify_database`）；
    - 必含核心表（防止把任意 sqlite 文件恢复进生产）；
    - `user_version` 不高于当前程序支持的版本（更高的库说明来自更新的版本，
      老程序不认识的列/迁移会直接把服务启动打挂）。
    """
    from .migrate import SCHEMA_VERSION  # 延迟导入：migrate 也会 import 本模块

    try:
        with closing(sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)) as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            if (row[0] if row else None) != "ok":
                raise RuntimeError(
                    f"数据库完整性校验未通过: {path}: {row[0] if row else None!r}")
            tables = {item[0] for item in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error as error:
        raise RuntimeError(f"数据库无法读取: {path}: {error}") from error
    missing = [name for name in REQUIRED_TABLES if name not in tables]
    if missing:
        raise RuntimeError(f"不是本服务的数据库（缺少表 {', '.join(missing)}）")
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"备份的 schema 版本 {version} 高于本程序支持的 {SCHEMA_VERSION}，"
            "请先升级程序再恢复")


def stage_restore(db_path: str | Path, data: bytes, *, filename: str = "",
                  now: float | None = None) -> dict:
    """把上传文件校验后落到待恢复位置；返回可展示的元信息。

    校验失败抛 `RuntimeError`（不留下任何文件）；成功后旧待恢复文件被覆盖。
    """
    if not data:
        raise RuntimeError("上传内容为空")
    pending = pending_restore_path(db_path)
    pending.parent.mkdir(parents=True, exist_ok=True)
    _chmod(pending.parent, 0o700)
    temp = pending.with_suffix(".uploading")
    try:
        temp.write_bytes(data)
        _chmod(temp, 0o600)
        verify_app_database(temp)
        os.replace(temp, pending)
    finally:
        temp.unlink(missing_ok=True)
    meta = {
        "filename": Path(filename).name if filename else pending.name,
        "size": len(data),
        "uploaded_at": int(now if now is not None else time.time()),
    }
    meta_path = pending.with_name(PENDING_META)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    _chmod(meta_path, 0o600)
    return meta


def pending_restore_info(db_path: str | Path) -> dict | None:
    """待恢复文件元信息；没有待恢复文件返回 None（meta 缺失也能读大小/时间）。"""
    pending = pending_restore_path(db_path)
    if not pending.is_file():
        return None
    meta_path = pending.with_name(PENDING_META)
    meta: dict = {}
    if meta_path.is_file():
        try:
            loaded = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                meta = loaded
        except (OSError, ValueError):
            meta = {}
    stat = pending.stat()
    return {
        "filename": str(meta.get("filename") or pending.name),
        "size": int(meta.get("size") or stat.st_size),
        "uploaded_at": int(meta.get("uploaded_at") or stat.st_mtime),
    }


def apply_pending_restore(db_path: str | Path, *,
                          now: float | None = None) -> Path | None:
    """启动时应用待恢复文件；返回安全副本路径（未恢复 / 主库原本不存在时为 None）。

    待恢复文件校验不过时**不阻断启动**：改名留档后返回 None，服务继续用当前库。
    否则一个坏上传会让服务再也起不来，恰好与「备份恢复」的目的相反。
    """
    pending = pending_restore_path(db_path)
    if not pending.is_file():
        return None
    try:
        safety = restore_database(db_path, pending, now=now)
    except (RuntimeError, OSError, sqlite3.Error) as error:
        rejected = pending.with_name(f"{PENDING_NAME}.rejected-{_stamp(now)}")
        with suppress(OSError):
            pending.replace(rejected)
        pending.with_name(PENDING_META).unlink(missing_ok=True)
        logger.warning("待恢复备份不可用，已留档为 %s：%s", rejected, error)
        return None
    pending.unlink(missing_ok=True)
    pending.with_name(PENDING_META).unlink(missing_ok=True)
    logger.warning("已应用待恢复备份；恢复前安全副本：%s", safety)
    return safety

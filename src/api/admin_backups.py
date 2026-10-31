"""备份管理：列表 / 立即备份 / 下载 / 删除 / 上传恢复。

恢复**不在运行中换库**：上传文件先校验（完整性 + 核心表 + schema 版本），落到
`DATA_DIR/restore/pending.sqlite3`，下次启动在建连接之前原子替换（见
`db/backup.apply_pending_restore`）。这样旧连接与 WAL 不会和新库拼在一起。

下载只认 `backups/` 下的普通 `*.sqlite3` 文件：拒绝路径分隔符、`..`、符号
链接，避免把任意文件当备份下发。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse

from ..audit.actions import (
    ACTION_BACKUP_CREATE,
    ACTION_BACKUP_DELETE,
    ACTION_BACKUP_DOWNLOAD,
    ACTION_BACKUP_RESTORE,
)
from ..auth.rbac import require_admin
from ..compat.openai.request import InvalidRequest
from ..db.backup import (
    backup_database,
    backup_dir,
    pending_restore_info,
    stage_restore,
)
from .deps import Services, csrf_protected, principal_from_request

logger = logging.getLogger(__name__)

# 上传上限：备份是整库文件，正常几十 MB；512MB 足够兜住异常体积又不至于让
# 单请求把内存吃光（FastAPI 的原始 body 读取是全量的）。
# **必须与 `webapp/limits.BACKUP_UPLOAD_LIMIT` 同值**：中间件先于端点裁决，
# 不同值时端点声称的 512MB 永远不可达，大库的「恢复备份」静默失效。
MAX_UPLOAD_BYTES = 512 * 1024 * 1024


def _entries(directory: Path) -> list[dict]:
    """备份目录下的普通 sqlite 文件（含手工放入的），按 mtime 倒序。"""
    if not directory.is_dir():
        return []
    items = []
    for path in directory.glob("*.sqlite3"):
        if path.is_symlink() or not path.is_file():
            continue
        stat = path.stat()
        items.append({"name": path.name, "size": stat.st_size,
                      "created_at": int(stat.st_mtime)})
    items.sort(key=lambda item: item["created_at"], reverse=True)
    return items


def _resolve(directory: Path, name: str) -> Path:
    """把文件名收敛到备份目录内的普通文件；非法名/不存在一律 400。"""
    if not name or Path(name).name != name or not name.endswith(".sqlite3"):
        raise InvalidRequest("backup name must be a plain *.sqlite3 filename")
    path = directory / name
    if path.is_symlink() or not path.is_file():
        raise InvalidRequest("backup not found")
    return path


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    runtime = services.settings
    db_path = runtime.db_path

    @router.get("/api/backups")
    async def list_backups(principal=Depends(principal_from_request)):
        require_admin(principal)
        return {"backups": _entries(backup_dir(db_path)),
                # 已上传、等重启生效的恢复文件（页面顶部提示）
                "pending": pending_restore_info(db_path)}

    @router.post("/api/backups")
    async def create_backup(_csrf: None = Depends(csrf_protected),
                            principal=Depends(principal_from_request)):
        require_admin(principal)
        try:
            # VACUUM INTO 是阻塞 IO；大库时不能占着事件循环
            target = await asyncio.to_thread(
                backup_database, db_path, keep=runtime.backup_keep)
        except (OSError, RuntimeError) as error:
            raise InvalidRequest(str(error)) from error
        stat = target.stat()
        logger.info("管理员 %s 手动备份 %s（%d 字节）",
                    principal.username, target.name, stat.st_size)
        services.audit.record(actor=principal.username, action=ACTION_BACKUP_CREATE,
                              target=target.name, detail=f"size={stat.st_size}")
        return {"backup": {"name": target.name, "size": stat.st_size,
                           "created_at": int(stat.st_mtime)}}

    @router.get("/api/backups/{name}")
    async def download_backup(name: str,
                              principal=Depends(principal_from_request)):
        require_admin(principal)
        path = _resolve(backup_dir(db_path), name)
        services.audit.record(actor=principal.username, action=ACTION_BACKUP_DOWNLOAD,
                              target=name)
        return FileResponse(path, media_type="application/x-sqlite3", filename=name)

    @router.delete("/api/backups/{name}")
    async def delete_backup(name: str, _csrf: None = Depends(csrf_protected),
                            principal=Depends(principal_from_request)):
        require_admin(principal)
        path = _resolve(backup_dir(db_path), name)
        path.unlink()
        logger.info("管理员 %s 删除备份 %s", principal.username, name)
        services.audit.record(actor=principal.username, action=ACTION_BACKUP_DELETE,
                              target=name)
        return {"ok": True}

    @router.post("/api/backups/restore")
    async def upload_restore(request: Request, filename: str = "",
                             _csrf: None = Depends(csrf_protected),
                             principal=Depends(principal_from_request)):
        """上传备份文件 → 校验 → 暂存；响应 `restart_required=true`。"""
        require_admin(principal)
        # 读完再判体积：Starlette 的 body() 本来就是全量读取，预检 Content-Length
        # 只多了「客户端可撒谎」的分支；上限由下面的实际长度兜住。
        body = await request.body()
        if len(body) > MAX_UPLOAD_BYTES:
            raise InvalidRequest(f"备份文件不能超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB")
        try:
            meta = await asyncio.to_thread(
                stage_restore, db_path, body, filename=filename)
        except (OSError, RuntimeError) as error:
            raise InvalidRequest(str(error)) from error
        logger.warning("管理员 %s 上传待恢复备份 %s（%d 字节），重启后生效",
                       principal.username, meta["filename"], meta["size"])
        services.audit.record(actor=principal.username, action=ACTION_BACKUP_RESTORE,
                              target=meta["filename"],
                              detail=f"size={meta['size']}；重启后生效")
        return {"staged": True, "restart_required": True, **meta}

    return router

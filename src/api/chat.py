"""OpenAI 兼容出口：POST /v1/chat/completions（流式 + 非流式）。"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request

from ..compat.openai.request import parse_chat_request
from .deps import ApiKeyPrincipal, Services, api_key_user
from .endpoints import dispatch_compat

# dump 目录最多保留的文件数：诊断开关长期开着时不能无限侵占磁盘
DUMP_KEEP_FILES = 200
# 单个 dump 文件的最长保留时间：即使文件数没超，24h 前的请求体也该消失
DUMP_MAX_AGE_SECONDS = 24 * 3600
# 目录/文件权限：请求体含完整对话内容，同机其它用户不该读到
DUMP_DIR_MODE = 0o700
DUMP_FILE_MODE = 0o600


def dump_request_body(data_dir: Path, body: object) -> None:
    """把原始请求体写到 data/dumps/（诊断用），并淘汰超量/超期的旧文件。

    请求体包含完整对话内容，因此这个开关只应用于排查客户端差异，
    不能长期开启；这里做三道收口：

    - 权限：目录 0700、文件 0600（含**已存在**目录的收紧，老版本建过 0755 的
      目录也要拉回来）；
    - 条数上限：`DUMP_KEEP_FILES`；
    - 时间上限：`DUMP_MAX_AGE_SECONDS`（按 mtime），条数没超也不会长期留存。

    开关本身是热更项（`dump_request_bodies`），改动进审计（`settings.update`）。
    """
    dump_dir = data_dir / "dumps"
    try:
        dump_dir.mkdir(parents=True, exist_ok=True, mode=DUMP_DIR_MODE)
        # mkdir 的 mode 只影响**新建**目录，且会被 umask 削；已存在的目录
        # （老版本建的、或数据目录搬迁带过来的）必须显式收紧。
        dump_dir.chmod(DUMP_DIR_MODE)
        path = dump_dir / f"{int(time.time() * 1000)}.json"
        # 用 os.open 建文件而不是 write_text：要先落权限再写内容，避免
        # 「先写后 chmod」窗口内同机其它用户能读到对话正文。
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, DUMP_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(body, ensure_ascii=False, indent=1))
    except OSError:  # 磁盘只读/满：诊断失败绝不能影响请求
        return
    files = sorted(dump_dir.glob("*.json"))
    # 条数超限的最旧那批先淘汰；再补上「条数没超但已过 24h」的
    stale = files[:-DUMP_KEEP_FILES]
    for item in files:
        if item in stale:
            continue
        with contextlib.suppress(OSError):
            if item.stat().st_mtime < time.time() - DUMP_MAX_AGE_SECONDS:
                stale.append(item)
    for item in stale:
        with contextlib.suppress(OSError):
            item.unlink()


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    executor = services.executor
    settings = services.settings

    @router.post("/v1/chat/completions")
    async def chat_completions(request: Request,
                               principal: ApiKeyPrincipal = Depends(api_key_user)):
        return await dispatch_compat(
            executor, settings, request, principal,
            parse=parse_chat_request,
            on_body=(lambda body: dump_request_body(Path(settings.data_dir), body))
            if settings.dump_request_bodies else None)

    return router

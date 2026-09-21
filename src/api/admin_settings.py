"""运行时配置热更：读取覆盖层快照 / 写入覆盖值（B3.2）。

语义约定：**DB 覆盖值优先于 .env**。管理台写下的值存进 runtime_settings 表，
进程重启后依然生效；要回到 .env 的默认值，把该项「恢复默认」（提交 null）。
界面与日志都必须明示这一点，否则用户会以为改 .env 就能改回来。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from ..auth.rbac import require_admin
from ..compat.openai.request import InvalidRequest
from ..runtime_settings import InvalidSetting
from .deps import Services, csrf_protected, principal_from_request

logger = logging.getLogger(__name__)


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    runtime = services.settings

    @router.get("/api/settings")
    async def list_settings(principal=Depends(principal_from_request)):
        require_admin(principal)
        return {
            "settings": runtime.snapshot(),
            "overridden": sum(1 for item in runtime.snapshot() if item["overridden"]),
        }

    @router.put("/api/settings")
    async def update_settings(payload: dict,
                              _csrf: None = Depends(csrf_protected),
                              principal=Depends(principal_from_request)):
        require_admin(principal)
        values = payload.get("values")
        if not isinstance(values, dict):
            raise InvalidRequest("values must be an object")
        try:
            runtime.set_many(values)
        except InvalidSetting as error:
            raise InvalidRequest(str(error)) from error
        keys = ", ".join(sorted(values)) or "(空)"
        logger.info("管理员 %s 更新运行时配置 %s（DB 覆盖 .env）", principal.username, keys)
        return {"settings": runtime.snapshot()}

    return router

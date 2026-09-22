"""运行时配置热更：读取覆盖层快照 / 写入覆盖值（B3.2）。

语义约定：**DB 覆盖值优先于 .env**。管理台写下的值存进 runtime_settings 表，
进程重启后依然生效；要回到 .env 的默认值，把该项「恢复默认」（提交 null）。
界面与日志都必须明示这一点，否则用户会以为改 .env 就能改回来。

同一页还展示 `/api/tasks` 的**后台任务运行态**（进程内）：任务清单与周期来自
同一份 `HOT_SETTINGS.task` 归属，页面把两者拼成「任务与配置」卡片——看周期的
人就在看这个任务最近跑得怎么样，分开两页反而要来回跳。
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, Request

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

    @router.get("/api/tasks")
    async def list_tasks(request: Request,
                         principal=Depends(principal_from_request)):
        """后台任务运行态（进程内）：上次真跑于何时、结果如何、当前周期与开关。

        与「运行时配置」同页展示：改周期的入口和「这个任务最近跑得怎么样」
        必须在同一个视野里。`server_time` 供前端算「距今多久」，避免浏览器
        时钟偏移把刚跑完的任务显示成几小时前。
        """
        require_admin(principal)
        runner = getattr(request.app.state, "task_runner", None)
        return {
            "tasks": runner.task_status() if runner is not None else [],
            "server_time": int(time.time()),
        }

    return router

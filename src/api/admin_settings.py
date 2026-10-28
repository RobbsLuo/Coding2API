"""运行时配置热更：读取覆盖层快照 / 写入覆盖值。

语义约定：**DB 覆盖值优先于 .env**。管理台写下的值存进 runtime_settings 表，
进程重启后依然生效；要回到 .env 的默认值，把该项「恢复默认」（提交 null）。
界面与日志都必须明示这一点，否则用户会以为改 .env 就能改回来。

同一页还展示 `/api/tasks` 的**后台任务运行态**（进程内）：任务清单与周期来自
同一份 `HOT_SETTINGS.task` 归属，非任务项按 `HotSetting.group` 归入网关卡组
（`GATEWAY_GROUPS`）；页面把两者拼成「任务与配置」，每个任务 / 每个网关卡组
各一个 tab——看周期的人就在看这个任务最近跑得怎么样，分开两页反而要来回跳。
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, Request

from ..audit.actions import ACTION_SETTINGS_UPDATE
from ..auth.rbac import require_admin
from ..compat.openai.request import InvalidRequest
from ..runtime_settings import GATEWAY_GROUPS, InvalidSetting
from ..tasks.runner import UnknownTaskError
from .deps import Services, csrf_protected, principal_from_request, request_ip

logger = logging.getLogger(__name__)


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    runtime = services.settings

    def snapshot_payload() -> dict:
        return {
            "settings": runtime.snapshot(),
            "overridden": sum(1 for item in runtime.snapshot() if item["overridden"]),
            # 网关卡组随响应下发：前端据此把无任务归属的项拆成一级 tab，
            # 不在前端硬编码 key→分组映射（后端加项/改归属不用动前端）。
            "groups": [{"value": key, "label": label} for key, label in GATEWAY_GROUPS],
        }

    @router.get("/api/settings")
    async def list_settings(principal=Depends(principal_from_request)):
        require_admin(principal)
        return snapshot_payload()

    @router.put("/api/settings")
    async def update_settings(payload: dict, request: Request,
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
        if "dump_request_bodies" in values:
            # 这是全服务唯一「把用户对话落盘」的开关：开关动作既进审计（下面那条
            # settings.update 带 keys），也在日志里单独喊一声——排查完没关是运维
            # 事故，日志里必须一眼可见。
            logger.warning("管理员 %s 把「请求体诊断落盘」改为 %s（DATA_DIR/dumps 将保存"
                           "完整请求体，含对话内容）",
                           principal.username, runtime.get("dump_request_bodies"))
        if "gateway_paused" in values:
            # kill switch 的两个方向都要留痕：开启是止血动作，关闭同样要能追到人。
            logger.warning("管理员 %s 把「网关暂停」改为 %s（原因：%s）",
                           principal.username, runtime.get("gateway_paused"),
                           runtime.get("gateway_pause_reason") or "未填原因")
        # 配置写入本身就是一次「谁能改网关行为」的变更，必须留痕：模型黑名单
        # （C3 的一键黑名单按钮也走这条接口）与告警 webhook 被改掉却不进审计，
        # 事后无法回答「是谁把它关的」。
        services.audit.record(
            actor=principal.username, action=ACTION_SETTINGS_UPDATE,
            detail=keys, ip=request_ip(request, services.settings))
        return snapshot_payload()

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

    @router.post("/api/tasks/{key}/run")
    async def run_task(key: str, request: Request,
                       _csrf: None = Depends(csrf_protected),
                       principal=Depends(principal_from_request)):
        """手动运行一个后台任务一轮（管理台「立即运行」）。

        与定时循环同一条路径（`TaskRunner._loop_specs` 单一来源），只多两点：
        **不等间隔、不绕开关**。绕过时点窗口（`due()`）是因为手动就是
        「立刻跑」；`activity` / `alert` 关闭时不绕——其 `run_once` 自身返回
        None，响应里 `ran=false`，不把「关了」谎报成「跑过了」。

        同步等结果返回（与 per-credential 的 probe / checkin / growth 手动
        端点同口径）：管理台点完要能立刻看到 report / error，而不是先看到
        「已触发」再轮询。任务正在跑时立刻返回 `busy=true`，不排队。
        """
        require_admin(principal)
        runner = getattr(request.app.state, "task_runner", None)
        if runner is None:
            raise InvalidRequest("task runner not available")
        try:
            result = await runner.run_task(key)
        except UnknownTaskError as error:
            raise InvalidRequest(f"unknown task {error.key!r}") from error
        logger.info("管理员 %s 手动运行后台任务 %s（ran=%s busy=%s）",
                    principal.username, key, result["ran"], result["busy"])
        return result

    return router

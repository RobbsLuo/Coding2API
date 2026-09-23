"""管理台凭证运维：CRUD / toggle / pin / probe / checkin / 成长中心 / 账号切换。

探测失败原因翻译（describe_probe_failure）在此，因为只有这里返回探测结果。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from ..audit.actions import (
    ACTION_CREDENTIAL_DELETE,
    ACTION_CREDENTIAL_IMPORT,
    ACTION_CREDENTIAL_PIN,
    ACTION_CREDENTIAL_REVIVE,
    ACTION_CREDENTIAL_SWITCH_ACCOUNT,
    ACTION_CREDENTIAL_TOGGLE,
)
from ..auth.rbac import require_operator
from ..compat.openai.request import InvalidRequest
from ..provider.base import GrowthResult, GrowthStep, StepStatus
from .deps import Services, csrf_protected, principal_from_request

logger = logging.getLogger(__name__)


def describe_probe_failure(error: Exception) -> str:
    """把探测异常翻译成用户能据以行动的原因。

    不能直接暴露 Python 类名（如 UpstreamProtocolViolation）——那是实现细节，
    用户看到它既判断不出问题，也不知道下一步该做什么。
    """
    from ..provider.codebuddy.client import UpstreamHTTPError as CodeBuddyHTTPError
    from ..provider.codebuddy.events import (
        UpstreamProtocolViolation as CodeBuddyViolation,
    )
    from ..provider.trae.client import UpstreamHTTPError as TraeHTTPError
    from ..provider.trae.events import UpstreamProtocolViolation as TraeViolation

    http_errors = (CodeBuddyHTTPError, TraeHTTPError)
    if isinstance(error, http_errors):
        status = getattr(error, "status", 0)
        if status in (401, 403):
            return "credential_rejected"      # 凭证失效，需要重新登录
        if status == 429:
            return "rate_limited"             # 上游限流，稍后再试
        if status >= 500:
            return "upstream_unavailable"     # 上游故障，与凭证无关
        return "upstream_rejected"            # 上游拒绝该请求
    if isinstance(error, (CodeBuddyViolation, TraeViolation)):
        return "upstream_response_invalid"    # 响应结构不符，可能是上游改版
    if isinstance(error, TimeoutError):
        return "upstream_timeout"
    return "unknown_error"


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    credentials = services.credentials
    registry = services.registry
    schedule_probe = services.schedule_probe

    @router.get("/api/credentials")
    async def list_credentials(principal=Depends(principal_from_request)):
        window = services.settings.quota_expiry_window_seconds
        secondary = services.settings.quota_expiry_secondary_window_seconds
        return {"credentials": credentials.list_all(
                    expiring_window=window, expiring_secondary_window=secondary),
                "expiry_window_seconds": window,
                "expiry_secondary_window_seconds": secondary,
                # 到期预警阈值（B3.3）：前端进度条与 <1h 红字同源，避免两处各写一个数
                "token_expiry_warning_seconds":
                    services.settings.token_expiry_warning_seconds,
                "viewer": principal.username,
                "is_admin": principal.is_admin}

    @router.post("/api/credentials")
    async def import_credential(payload: dict,
                                _csrf: None = Depends(csrf_protected),
                                principal=Depends(principal_from_request)):
        require_operator(principal)
        provider_id = str(payload.get("provider") or "")
        if provider_id not in registry:
            raise InvalidRequest(f"unknown provider {provider_id!r}")
        credential_data = registry[provider_id].import_credential(
            payload.get("credential") or {})
        credential_id = credentials.add(provider=provider_id,
                                        credential_data=credential_data,
                                        nickname=str(payload.get("nickname") or ""),
                                        added_by=principal.username)
        logger.info("管理员 %s 新增凭证 %s（上游 %s）", principal.username, credential_id,
                    provider_id)
        services.audit.record(actor=principal.username, action=ACTION_CREDENTIAL_IMPORT,
                              target=credential_id, detail=f"上游 {provider_id}")
        schedule_probe(credential_id)
        return {"id": credential_id}

    @router.post("/api/credentials/{credential_id}/toggle")
    async def toggle_credential(credential_id: str, payload: dict,
                                _csrf: None = Depends(csrf_protected),
                                principal=Depends(principal_from_request)):
        require_operator(principal)
        if not credentials.set_enabled(credential_id, bool(payload.get("enabled", True))):
            raise InvalidRequest("credential not found")
        logger.info("管理员 %s 开关凭证 %s -> %s", principal.username, credential_id,
                    payload.get("enabled", True))
        services.audit.record(actor=principal.username, action=ACTION_CREDENTIAL_TOGGLE,
                              target=credential_id,
                              detail=f"enabled={bool(payload.get('enabled', True))}")
        return {"ok": True}

    @router.post("/api/credentials/{credential_id}/revive")
    async def revive_credential(credential_id: str,
                                _csrf: None = Depends(csrf_protected),
                                principal=Depends(principal_from_request)):
        """解除硬禁用/冷却，让重新登录后的凭证回到池子。"""
        require_operator(principal)
        if not credentials.revive(credential_id):
            raise InvalidRequest("credential not found")
        logger.info("管理员 %s 恢复了凭证 %s", principal.username, credential_id)
        services.audit.record(actor=principal.username, action=ACTION_CREDENTIAL_REVIVE,
                              target=credential_id)
        return {"ok": True}

    @router.post("/api/credentials/pin")
    async def pin_credential(payload: dict,
                             _csrf: None = Depends(csrf_protected),
                             principal=Depends(principal_from_request)):
        require_operator(principal)
        pin = payload.get("credential_id")
        credentials.set_pinned(pin)
        logger.info("管理员 %s 固定凭证 %s", principal.username, pin)
        services.audit.record(actor=principal.username, action=ACTION_CREDENTIAL_PIN,
                              target=pin if isinstance(pin, str) else None,
                              detail=f"pinned={pin!r}")
        return {"ok": True}

    @router.delete("/api/credentials/{credential_id}")
    async def delete_credential(credential_id: str,
                                _csrf: None = Depends(csrf_protected),
                                principal=Depends(principal_from_request)):
        require_operator(principal)
        if not credentials.delete(credential_id):
            raise InvalidRequest("credential not found")
        logger.info("管理员 %s 删除凭证 %s", principal.username, credential_id)
        services.audit.record(actor=principal.username, action=ACTION_CREDENTIAL_DELETE,
                              target=credential_id)
        return {"ok": True}

    @router.post("/api/credentials/{credential_id}/probe")
    async def probe_credential(credential_id: str,
                               _csrf: None = Depends(csrf_protected),
                               principal=Depends(principal_from_request)):
        require_operator(principal)
        provider_id = credentials.provider_of(credential_id)
        provider = registry.get(provider_id or "")
        data = credentials.credential_data(credential_id)
        if provider is None or data is None:
            raise InvalidRequest("credential not found")
        try:
            quota = await provider.probe_quota(data)
        except Exception as error:  # noqa: BLE001 - 探测失败 → unknown，不当作 0
            credentials.mark_probe_failed(credential_id)
            reason = describe_probe_failure(error)
            logger.info("额度探测失败 %s: %s", credential_id, reason)
            return {"probed": False, "reason": reason, "detail": str(error)[:200]}
        credentials.save_quota(credential_id, quota)
        return {"probed": True, "remaining": quota.remaining, "total": quota.total,
                "cycle_end": quota.cycle_end}

    @router.post("/api/credentials/{credential_id}/checkin")
    async def checkin_credential(credential_id: str,
                                 _csrf: None = Depends(csrf_protected),
                                 principal=Depends(principal_from_request)):
        require_operator(principal)
        provider_id = credentials.provider_of(credential_id)
        provider = registry.get(provider_id or "")
        data = credentials.credential_data(credential_id)
        if provider is None or data is None or not hasattr(provider, "checkin"):
            raise InvalidRequest("credential does not support checkin")
        result = await provider.checkin(data)
        if result.ok and not result.already_checked_in:
            schedule_probe(credential_id)      # 只有真签到了才会发积分
        # already_checked_in 必须透传：前端靠它区分「刚签到」与「今天已签过」
        return {"ok": result.ok, "credit": result.credit, "code": result.code,
                "message": result.message,
                "already_checked_in": result.already_checked_in,
                # 渠道可选的活动状态（连续天数/今日积分）；无此能力的渠道为 None
                "status": result.status}

    @router.get("/api/credentials/{credential_id}/checkin")
    async def checkin_status(credential_id: str,
                             principal=Depends(principal_from_request)):
        """只读查询签到状态（连续天数 / 今日是否已签）；不产生任何写入。"""
        require_operator(principal)
        provider_id = credentials.provider_of(credential_id)
        provider = registry.get(provider_id or "")
        data = credentials.credential_data(credential_id)
        if provider is None or data is None or not hasattr(provider, "checkin_status"):
            raise InvalidRequest("credential does not support checkin status")
        return {"status": await provider.checkin_status(data)}

    @router.get("/api/credentials/{credential_id}/growth")
    async def growth_history(credential_id: str, limit: int = 20,
                             principal=Depends(principal_from_request)):
        """成长中心历史：最近几轮的一行汇报（仅 CodeBuddy 有该活动）。"""
        require_operator(principal)
        if services.credentials.provider_of(credential_id) is None:
            raise InvalidRequest("credential not found")
        return {"events": services.growth_events.recent(credential_id, limit=limit)}

    @router.get("/api/credentials/{credential_id}/credit-events")
    async def credit_events(credential_id: str, limit: int = 20,
                            principal=Depends(principal_from_request)):
        """积分变动流水：两次额度探测之间的净变化（倒序）。

        刻意不返回「来源」枚举：上游不打日志，diff 看到的只是区间净变化，
        写「签到 +5」就是把猜测当事实。source 只表达归因已知度
        （observed=常规区间 / sync=首次建立基线）。
        """
        require_operator(principal)
        if services.credentials.provider_of(credential_id) is None:
            raise InvalidRequest("credential not found")
        return {"events": services.credit_events.recent(credential_id, limit=limit)}

    @router.post("/api/credentials/{credential_id}/growth")
    async def run_growth(credential_id: str,
                         _csrf: None = Depends(csrf_protected),
                         principal=Depends(principal_from_request)):
        """手动跑一轮成长中心：与定时任务同一条路径，结果同样落库。

        不可逆动作（抽奖/兑换/开盲盒/补登卡）遵循与定时任务相同的配置开关：
        手动入口不另设开关，否则「保守部署」只挡得住定时任务。
        """
        require_operator(principal)
        provider_id = credentials.provider_of(credential_id)
        provider = registry.get(provider_id or "")
        data = credentials.credential_data(credential_id)
        growth = getattr(provider, "growth", None)
        if provider is None or data is None or growth is None:
            raise InvalidRequest("credential does not support growth center")
        result = await growth(
            data, allow_irreversible=services.settings.growth_irreversible_actions)
        credentials.save_growth_result(credential_id, result.report)
        services.growth_events.record(credential_id=credential_id, result=result,
                                      trigger="manual")
        logger.info("管理员 %s 手动执行成长中心 %s（ok=%s）", principal.username,
                    credential_id, result.ok)
        return {"ok": result.ok, "report": result.report, "credit": result.credit,
                "energy": result.energy, "streak_days": result.streak_days,
                "session_dead": result.session_dead,
                "steps": [{"name": step.name, "status": step.status, "detail": step.detail,
                           "credit": step.credit} for step in result.steps]}

    @router.post("/api/credentials/{credential_id}/activity")
    async def run_activity(credential_id: str,
                           _csrf: None = Depends(csrf_protected),
                           principal=Depends(principal_from_request)):
        """手动补发一条活跃上报（B1.7）：与定时任务同一条路径。

        独立于 activity_report_enabled 开关：定时任务默认关闭不影响管理员在
        管理台手动补报一次（用途就是部署后验证闭环）。结果记一行 growth_events。
        """
        require_operator(principal)
        provider_id = credentials.provider_of(credential_id)
        provider = registry.get(provider_id or "")
        data = credentials.credential_data(credential_id)
        activity = getattr(provider, "activity", None)
        if provider is None or data is None or activity is None:
            raise InvalidRequest("credential does not support activity report")
        result = await activity(data)
        if result.ok:
            detail = result.message or "已上报一条对话事件"
            services.growth_events.record(
                credential_id=credential_id, trigger="manual",
                result=GrowthResult(
                    report=f"活跃上报：{detail}",
                    steps=[GrowthStep("活跃上报", StepStatus.DONE, detail)]))
        logger.info("管理员 %s 手动活跃上报 %s（ok=%s）", principal.username,
                    credential_id, result.ok)
        return {"ok": result.ok, "message": result.message}

    @router.get("/api/credentials/{credential_id}/accounts")
    async def list_credential_accounts(credential_id: str,
                                       principal=Depends(principal_from_request)):
        require_operator(principal)
        provider_id = credentials.provider_of(credential_id)
        provider = registry.get(provider_id or "")
        data = credentials.credential_data(credential_id)
        if provider is None or data is None or not hasattr(provider, "list_accounts"):
            raise InvalidRequest("credential does not support account switching")
        accounts = await provider.list_accounts(data)
        return {"accounts": [{"account_id": a.account_id, "nickname": a.nickname,
                              "type": a.account_type} for a in accounts]}

    @router.post("/api/credentials/{credential_id}/accounts/select")
    async def select_credential_account(credential_id: str, payload: dict,
                                        _csrf: None = Depends(csrf_protected),
                                        principal=Depends(principal_from_request)):
        require_operator(principal)
        provider_id = credentials.provider_of(credential_id)
        provider = registry.get(provider_id or "")
        data = credentials.credential_data(credential_id)
        if provider is None or data is None or not hasattr(provider, "switch_account"):
            raise InvalidRequest("credential does not support account switching")
        switched = await provider.switch_account(data, str(payload.get("account_id") or ""))
        credentials.save_credential_data(credential_id, switched)
        logger.info("管理员 %s 切换凭证 %s 账号 -> %s", principal.username, credential_id,
                    payload.get("account_id"))
        services.audit.record(actor=principal.username,
                              action=ACTION_CREDENTIAL_SWITCH_ACCOUNT,
                              target=credential_id,
                              detail=f"账号 -> {payload.get('account_id')}")
        # 账号切换后额度对应的是新账号，必须重探测而不是沿用旧值
        schedule_probe(credential_id)
        return {"switched": True}

    return router

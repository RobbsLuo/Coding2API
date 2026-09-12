"""管理台凭证运维：CRUD / toggle / pin / probe / checkin / 账号切换。

探测失败原因翻译（describe_probe_failure）在此，因为只有这里返回探测结果。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from ..auth.rbac import require_admin
from ..compat.openai.request import InvalidRequest
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
        return {"credentials": credentials.list_all(), "viewer": principal.username,
                "is_admin": principal.is_admin}

    @router.post("/api/credentials")
    async def import_credential(payload: dict,
                                _csrf: None = Depends(csrf_protected),
                                principal=Depends(principal_from_request)):
        require_admin(principal)
        provider_id = str(payload.get("provider") or "")
        if provider_id not in registry:
            raise InvalidRequest(f"unknown provider {provider_id!r}")
        credential_data = registry[provider_id].import_credential(
            payload.get("credential") or {})
        credential_id = credentials.add(provider=provider_id,
                                        credential_data=credential_data,
                                        nickname=str(payload.get("nickname") or ""),
                                        added_by=principal.username)
        schedule_probe(credential_id)
        return {"id": credential_id}

    @router.post("/api/credentials/{credential_id}/toggle")
    async def toggle_credential(credential_id: str, payload: dict,
                                _csrf: None = Depends(csrf_protected),
                                principal=Depends(principal_from_request)):
        require_admin(principal)
        if not credentials.set_enabled(credential_id, bool(payload.get("enabled", True))):
            raise InvalidRequest("credential not found")
        return {"ok": True}

    @router.post("/api/credentials/pin")
    async def pin_credential(payload: dict,
                             _csrf: None = Depends(csrf_protected),
                             principal=Depends(principal_from_request)):
        require_admin(principal)
        credentials.set_pinned(payload.get("credential_id"))
        return {"ok": True}

    @router.delete("/api/credentials/{credential_id}")
    async def delete_credential(credential_id: str,
                                _csrf: None = Depends(csrf_protected),
                                principal=Depends(principal_from_request)):
        require_admin(principal)
        if not credentials.delete(credential_id):
            raise InvalidRequest("credential not found")
        return {"ok": True}

    @router.post("/api/credentials/{credential_id}/probe")
    async def probe_credential(credential_id: str,
                               _csrf: None = Depends(csrf_protected),
                               principal=Depends(principal_from_request)):
        require_admin(principal)
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
        require_admin(principal)
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
                "already_checked_in": result.already_checked_in}

    @router.get("/api/credentials/{credential_id}/accounts")
    async def list_credential_accounts(credential_id: str,
                                       principal=Depends(principal_from_request)):
        require_admin(principal)
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
        require_admin(principal)
        provider_id = credentials.provider_of(credential_id)
        provider = registry.get(provider_id or "")
        data = credentials.credential_data(credential_id)
        if provider is None or data is None or not hasattr(provider, "switch_account"):
            raise InvalidRequest("credential does not support account switching")
        switched = await provider.switch_account(data, str(payload.get("account_id") or ""))
        credentials.save_credential_data(credential_id, switched)
        # 账号切换后额度对应的是新账号，必须重探测而不是沿用旧值
        schedule_probe(credential_id)
        return {"switched": True}

    return router

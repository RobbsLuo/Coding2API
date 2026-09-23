"""审计查看端点（B5，admin only）。

只读：写入散落在各业务端点（登录、账号变动、凭证写操作），不在这里做。
响应形状与 /api/stats/events 的分页约定一致（limit/offset）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..audit.actions import ACTION_LABELS, ACTIONS
from ..auth.rbac import require_admin
from .deps import Services, principal_from_request


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/api/audit")
    async def list_audit(actor: str = "", action: str = "", since: int = 0,
                         before: int = 0, limit: int = 100, offset: int = 0,
                         principal=Depends(principal_from_request)):
        require_admin(principal)
        rows = services.audit.query(
            actor=actor or None, action=action or None,
            since=since or None, before=before or None,
            limit=limit, offset=offset)
        return {"events": rows, "actions": list(ACTIONS), "labels": ACTION_LABELS}

    return router

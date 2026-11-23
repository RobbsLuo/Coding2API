"""访问冻结管理（内容拒绝的发起者归因）：列出 / 解冻。

operator+：冻结是按**人**的访问策略，属于运维处置面；viewer 只读大盘。
列表按 `(subject, provider)` 聚合出累计命中次数，并按当前热更阈值标注
`frozen`——阈值 0 时永远 false（只记录不冻结）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..auth.rbac import require_operator
from ..compat.openai.request import InvalidRequest
from .deps import Services, csrf_protected, principal_from_request, request_ip

# 审计动作名（与 audit_events.action 同域）。冻结管理属于新增端点，动作常量
# 先定义在本模块，避免把「改 audit/actions.py」变成冻结功能的前置依赖。
ACTION_ACCESS_FREEZE_UNFREEZE = "access_freeze.unfreeze"


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/api/access-freezes")
    async def list_access_freezes(limit: int = 100, offset: int = 0,
                                  subject: str = "", provider: str = "",
                                  principal=Depends(principal_from_request)):
        """按 (发起者, 渠道) 聚合的内容拒绝命中列表。"""
        require_operator(principal)
        threshold = int(services.settings.access_freeze_threshold)
        entries = []
        for row in services.access_freezes.subjects():
            if subject and row["subject"] != subject:
                continue
            if provider and row["provider"] != provider:
                continue
            entries.append({**row,
                            "frozen": threshold > 0 and row["hits"] >= threshold})
        start = max(0, offset)
        end = start + max(1, min(500, limit))
        return {
            "threshold": threshold,
            "entries": entries[start:end],
            "total": len(entries),
        }

    @router.post("/api/access-freezes/unfreeze")
    async def unfreeze_access(payload: dict, request: Request,
                              _csrf: None = Depends(csrf_protected),
                              principal=Depends(principal_from_request)):
        """解冻一个 (发起者, 渠道)：删除全部命中行并写审计。

        解冻是幂等的：没有命中行时返回 removed=0（仍写审计，便于回答
        「谁什么时候点过解冻」）。
        """
        require_operator(principal)
        subject = str(payload.get("subject") or "").strip()
        provider = str(payload.get("provider") or "").strip()
        if not subject or not provider:
            raise InvalidRequest("subject and provider are required")
        removed = services.access_freezes.unfreeze(subject=subject, provider=provider)
        services.audit.record(
            actor=principal.username, action=ACTION_ACCESS_FREEZE_UNFREEZE,
            target=f"{subject}@{provider}",
            detail=f"删除 {removed} 条内容拒绝命中记录",
            ip=request_ip(request, services.settings))
        return {"ok": True, "removed": removed}

    return router
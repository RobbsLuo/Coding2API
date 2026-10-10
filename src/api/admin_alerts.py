"""运维告警记录回看（P1-7）：GET /api/alerts。

只读端点：告警由后台 AlertTask 写入，管理台「站内告警记录」按时间倒序回看。
admin-only——告警详情含凭证 id / 渠道等运维信息，与任务与配置页同一门槛。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..auth.rbac import require_admin
from .deps import Services, principal_from_request


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    alerts = services.alerts

    @router.get("/api/alerts")
    async def list_alerts(limit: int = 50, sort: str | None = None,
                          order: str | None = None,
                          principal=Depends(principal_from_request)):
        """最近告警（默认倒序）。limit 由仓储收敛到 [1, 200]。"""
        require_admin(principal)
        return {"alerts": alerts.recent(limit, sort=sort, order=order)}

    return router

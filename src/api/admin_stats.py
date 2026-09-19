"""用量统计查询：overview / by-provider / timeline（admin 可跨用户聚合）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..auth.rbac import Principal
from .deps import Services, principal_from_request


def _scope(principal: Principal, username: str | None) -> str | None:
    """admin 传 username 可查全局/指定人；普通用户永远只能看自己。"""
    return username if principal.is_admin else principal.username


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    stats_query = services.stats_query

    @router.get("/api/stats/overview")
    async def stats_overview(principal=Depends(principal_from_request),
                             username: str | None = None, since: int | None = None):
        return stats_query.overview(username=_scope(principal, username), since=since)

    @router.get("/api/stats/by-provider")
    async def stats_by_provider(principal=Depends(principal_from_request),
                                username: str | None = None, since: int | None = None):
        return {"providers": stats_query.by_provider(
            username=_scope(principal, username), since=since)}

    @router.get("/api/stats/timeline")
    async def stats_timeline(principal=Depends(principal_from_request),
                             username: str | None = None, since: int | None = None,
                             metric: str = "requests"):
        return {"points": stats_query.timeline(
            username=_scope(principal, username), since=since, metric=metric)}

    @router.get("/api/stats/model-timeline")
    async def stats_model_timeline(principal=Depends(principal_from_request),
                                   username: str | None = None, since: int | None = None,
                                   metric: str = "requests"):
        return stats_query.model_timeline(
            username=_scope(principal, username), since=since, metric=metric)

    @router.get("/api/stats/events")
    async def stats_events(principal=Depends(principal_from_request),
                           username: str | None = None, since: int | None = None,
                           before: int | None = None, limit: int = 50):
        # 明细保留 90 天；单页上限 200，防止一次拉爆
        return stats_query.events(username=_scope(principal, username), since=since,
                                  before=before, limit=max(1, min(limit, 200)))

    return router

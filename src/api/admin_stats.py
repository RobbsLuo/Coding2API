"""用量统计查询：overview / by-provider / timeline（admin 可跨用户聚合）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from .deps import Services, principal_from_request


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    stats_query = services.stats_query

    @router.get("/api/stats/overview")
    async def stats_overview(principal=Depends(principal_from_request),
                             username: str | None = None, since: int | None = None):
        target = username if principal.is_admin else principal.username
        return stats_query.overview(username=target, since=since)

    @router.get("/api/stats/by-provider")
    async def stats_by_provider(principal=Depends(principal_from_request),
                                username: str | None = None, since: int | None = None):
        target = username if principal.is_admin else principal.username
        return {"providers": stats_query.by_provider(username=target, since=since)}

    @router.get("/api/stats/timeline")
    async def stats_timeline(principal=Depends(principal_from_request),
                             username: str | None = None, since: int | None = None,
                             metric: str = "requests"):
        target = username if principal.is_admin else principal.username
        return {"points": stats_query.timeline(username=target, since=since, metric=metric)}

    @router.get("/api/stats/model-timeline")
    async def stats_model_timeline(principal=Depends(principal_from_request),
                                   username: str | None = None, since: int | None = None,
                                   metric: str = "requests"):
        target = username if principal.is_admin else principal.username
        return stats_query.model_timeline(username=target, since=since, metric=metric)

    @router.get("/api/stats/events")
    async def stats_events(principal=Depends(principal_from_request),
                           username: str | None = None, since: int | None = None,
                           before: int | None = None, limit: int = 50):
        # 明细保留 90 天；单页上限 200，防止一次拉爆
        target = username if principal.is_admin else principal.username
        return stats_query.events(username=target, since=since, before=before,
                                  limit=max(1, min(limit, 200)))

    return router

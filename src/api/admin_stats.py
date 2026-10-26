"""用量统计查询：overview / by-provider / timeline（admin 可跨用户聚合）。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth.rbac import Principal, require_operator
from ..compat.openai.errors import error_payload
from ..stats.csv import CSV_BOM, EXPORT_HEADERS, csv_line, event_cells
from ..stats.live import LiveEventHub, Subscription, TooManySubscribers
from ..stats.query import ERROR_MAX_GROUPS, EXPORT_MAX_ROWS
from .deps import Services, principal_from_request

# 心跳间隔：代理/浏览器对「长时间没数据」的连接会主动断（nginx 默认 60s）。
# 15s 一条注释帧，既保活又不至于把流量抬起来。
HEARTBEAT_SECONDS = 15.0


def _scope(principal: Principal, username: str | None) -> str | None:
    """admin/operator 传 username 可查全局/指定人；viewer 永远只能看自己。

    operator 是「凭证运维」角色，按 PROPOSAL §3.7 与 README 的角色表应能看
    全量统计（否则看不到自己维护的渠道整体健康）；viewer 才是严格自负。
    """
    return username if principal.is_operator else principal.username


def _export_filename(now: int) -> str:
    stamp = datetime.fromtimestamp(now, tz=UTC).strftime("%Y%m%d-%H%M%S")
    return f"coding2api-events-{stamp}Z.csv"


def _csv_stream(rows: list[dict]) -> Iterator[str]:
    """把明细行编成 CSV 文本块（含 BOM 与表头）。"""
    yield CSV_BOM + csv_line(list(EXPORT_HEADERS))
    for row in rows:
        yield csv_line(event_cells(row))


def _event_matches(event: dict[str, Any], *, username: str | None,
                   provider: str | None, model: str | None,
                   credential_id: str | None, ok: int | None = None) -> bool:
    """实时行是否属于该订阅者的过滤条件（与 `/api/stats/events` 同口径）。

    过滤放在**订阅者侧**而不是发布侧：一条事件可能同时被多个订阅者看到，
    各自的条件不同（一人看全量、一人钻取某渠道）。`None` = 不限制。
    """
    if username is not None and event.get("username") != username:
        return False
    if provider is not None and event.get("provider") != provider:
        return False
    if model is not None and event.get("model") != model:
        return False
    if ok is not None and event.get("ok") != ok:
        return False
    return not (credential_id is not None and event.get("credential_id") != credential_id)


def _sse_frame(event: dict[str, Any]) -> str:
    """一帧 SSE：`data: <json>` + 空行。

    不加 `event:` 名，前端用 `onmessage` 统一处理；JSON 不转义非 ASCII，
    与其它统计端点（`JSONResponse`）保持同一可读性。
    """
    return f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"


async def _stream_events(request: Request, hub: LiveEventHub,
                         subscription: Subscription, *,
                         username: str | None, provider: str | None,
                         model: str | None, credential_id: str | None,
                         resolve_name: Callable[[str], str | None] | None,
                         ok: int | None = None,
                         heartbeat: float = HEARTBEAT_SECONDS) -> AsyncIterator[str]:
    """SSE 主体：订阅 → 逐条下发 → 心跳 → 断开即注销。

    `finally` 里注销是硬要求：客户端关标签页时生成器被取消，不注销的话
    订阅槽位会泄漏，8 次之后所有人 429。
    """
    try:
        # 先发一条注释帧：浏览器 EventSource 收到任何字节才进入 OPEN，
        # 否则打开面板后要等到第一条真实事件才算「已连接」。
        yield ": connected\n\n"
        while True:
            if await request.is_disconnected():
                return
            batch = subscription.drain()
            if batch:
                for event in batch:
                    if not _event_matches(event, username=username, provider=provider,
                                          model=model, credential_id=credential_id,
                                          ok=ok):
                        continue
                    if resolve_name is not None and event.get("credential_id"):
                        event = {**event,
                                 "credential_name": resolve_name(event["credential_id"])}
                    yield _sse_frame(event)
                continue
            try:
                await asyncio.wait_for(subscription.wakeup.wait(), timeout=heartbeat)
            except TimeoutError:
                # 注释帧（以 `:` 开头）被 EventSource 忽略，只用于保活
                yield ": heartbeat\n\n"
    finally:
        hub.unsubscribe(subscription)


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    stats_query = services.stats_query

    @router.get("/api/stats/overview")
    async def stats_overview(principal=Depends(principal_from_request),
                             username: str | None = None, since: int | None = None,
                             until: int | None = None):
        return stats_query.overview(username=_scope(principal, username), since=since,
                                    until=until)

    @router.get("/api/stats/by-provider")
    async def stats_by_provider(principal=Depends(principal_from_request),
                                username: str | None = None, since: int | None = None,
                                until: int | None = None,
                                sort: str | None = None, order: str | None = None):
        return {"providers": stats_query.by_provider(
            username=_scope(principal, username), since=since, until=until,
            sort=sort, order=order)}

    @router.get("/api/stats/by-model")
    async def stats_by_model(principal=Depends(principal_from_request),
                             username: str | None = None, since: int | None = None,
                             until: int | None = None,
                             sort: str | None = None, order: str | None = None):
        return {"models": stats_query.by_model(
            username=_scope(principal, username), since=since, until=until,
            sort=sort, order=order)}

    @router.get("/api/stats/timeline")
    async def stats_timeline(principal=Depends(principal_from_request),
                             username: str | None = None, since: int | None = None,
                             until: int | None = None,
                             metric: str = "requests"):
        return {"points": stats_query.timeline(
            username=_scope(principal, username), since=since, until=until, metric=metric)}

    @router.get("/api/stats/model-timeline")
    async def stats_model_timeline(principal=Depends(principal_from_request),
                                   username: str | None = None, since: int | None = None,
                                   until: int | None = None,
                                   metric: str = "requests"):
        return stats_query.model_timeline(
            username=_scope(principal, username), since=since, until=until, metric=metric)

    @router.get("/api/stats/percentile-timeline")
    async def stats_percentile_timeline(principal=Depends(principal_from_request),
                                        username: str | None = None,
                                        since: int | None = None,
                                        until: int | None = None,
                                        metric: str = "latency"):
        """延迟分位时序（P50 中位数 + P95 上界）：延迟图的「典型值 + 长尾」两层。"""
        return stats_query.percentile_timeline(
            username=_scope(principal, username), since=since, until=until, metric=metric)

    @router.get("/api/stats/by-user")
    async def stats_by_user(principal=Depends(principal_from_request),
                            username: str | None = None, since: int | None = None,
                            until: int | None = None,
                            sort: str | None = None, order: str | None = None):
        return {"users": stats_query.by_user(
            username=_scope(principal, username), since=since, until=until,
            sort=sort, order=order)}

    @router.get("/api/stats/by-credential")
    async def stats_by_credential(principal=Depends(principal_from_request),
                                  username: str | None = None, since: int | None = None,
                                  until: int | None = None,
                                  sort: str | None = None, order: str | None = None):
        # 凭证昵称来自共享池（常含邮箱/手机）：仅 admin/operator 可见，
        # 与明细端点同口径；provider 无隐私顾虑，无条件下发（前端画渠道 icon）
        return {"credentials": stats_query.by_credential(
            username=_scope(principal, username), since=since, until=until,
            sort=sort, order=order,
            include_credential_name=principal.is_operator)}

    @router.get("/api/stats/errors")
    async def stats_errors(principal=Depends(principal_from_request),
                           username: str | None = None, since: int | None = None,
                           until: int | None = None, limit: int = 100):
        """失败归因（operator+）：按渠道 / 模型 / 错误类型分组计数。

        仅 operator/admin 可见：这是「池子里哪里在坏」的运维视图，跨用户聚合；
        viewer 的失败明细已在 `/api/stats/events` 里（只看自己）。

        窗口缺省为最近 24 小时（明细表几十万行，不给下界等于全表扫）；
        `limit` 夹到 `1..ERROR_MAX_GROUPS`，按失败数降序取前 N 组。
        """
        require_operator(principal)
        return {"errors": stats_query.errors(
            username=_scope(principal, username), since=since, until=until,
            limit=max(1, min(limit, ERROR_MAX_GROUPS)))}

    @router.get("/api/stats/events")
    async def stats_events(principal=Depends(principal_from_request),
                           username: str | None = None, since: int | None = None,
                           until: int | None = None,
                           before: int | None = None, limit: int = 50,
                           offset: int = 0, sort: str | None = None,
                           order: str | None = None,
                           provider: str | None = None, model: str | None = None,
                           credential_id: str | None = None,
                           ok: int | None = None,
                           format: str = "json"):
        # 明细保留 90 天；单页上限 200，防止一次拉爆
        # 凭证昵称来自共享池：仅 admin/operator 可见，viewer 只看自己的记录
        scope = _scope(principal, username)
        names = principal.is_operator
        if format == "csv":
            # 导出仅 admin/operator：整份明细落盘比翻页浏览的暴露面大得多，
            # 且文件名/内容都带池级信息，viewer 不该有。
            require_operator(principal)
            rows = stats_query.export_events(
                username=scope, since=since, until=until, include_credential_name=names,
                provider=provider, model=model, credential_id=credential_id, ok=ok)
            truncated = len(rows) > EXPORT_MAX_ROWS
            rows = rows[:EXPORT_MAX_ROWS]
            now = int(datetime.now(tz=UTC).timestamp())
            headers = {
                "Content-Disposition": f'attachment; filename="{_export_filename(now)}"',
                "X-Export-Row-Count": str(len(rows)),
                "X-Export-Truncated": "1" if truncated else "0",
                "Cache-Control": "no-store",
            }
            return StreamingResponse(
                _csv_stream(rows), media_type="text/csv; charset=utf-8", headers=headers)
        return stats_query.events(username=scope, since=since, until=until,
                                  before=before, limit=max(1, min(limit, 200)),
                                  offset=max(0, offset), sort=sort, order=order,
                                  provider=provider, model=model,
                                  credential_id=credential_id, ok=ok,
                                  include_credential_name=names)

    @router.get("/api/stats/events/stream")
    async def stats_events_stream(request: Request,
                                  principal=Depends(principal_from_request),
                                  username: str | None = None,
                                  provider: str | None = None,
                                  model: str | None = None,
                                  credential_id: str | None = None,
                                  ok: int | None = None):
        """明细实时流（SSE）：`StatsCollector.record` 落库后逐条推送。

        与 `/api/stats/events` 同一套作用域与过滤（`username` / `provider` /
        `model` / `credential_id` / `ok`），只是「新记录主动推」而不是翻页拉。凭证
        昵称仅 admin/operator 解析（与明细端点同口径）；`since`/`until` 不参与——
        实时流没有时间窗语义，客户端自己按需丢弃。

        订阅数超上限回 429（不排队）：每个订阅者占一条长连接，静默接受会
        把内存和连接数拖到不可控。`Cache-Control: no-store` + `X-Accel-Buffering:
        no` 防止中间层缓存或整段缓冲（否则「实时」变成批量）。
        """
        try:
            subscription = services.live_events.subscribe()
        except TooManySubscribers:
            return JSONResponse(
                status_code=429,
                content=error_payload("too many live subscribers",
                                      "rate_limit_exceeded", 429),
                headers={"Retry-After": "10"})
        scope = _scope(principal, username)
        resolve_name = services.credentials.nickname_of if principal.is_operator else None
        return StreamingResponse(
            _stream_events(request, services.live_events, subscription,
                           username=scope, provider=provider, model=model,
                           credential_id=credential_id, ok=ok,
                           resolve_name=resolve_name),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no",
                     "Connection": "keep-alive"},
        )

    return router

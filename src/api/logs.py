"""服务端日志页的数据出口：`GET /api/logs` 与 `GET /api/logs/stream`（E3）。

**只读、admin-only**：日志里即便已脱敏，也带着路由、上游错误文案与请求 ID，
是运维视角的全量视图；不提供任何写入/执行入口——页面上没有输入框能影响
服务，订阅者只能读。

关闭方式：热更项 `log_buffer_lines = 0`，两个端点都回 404（与 `/metrics`
关闭时的口径一致），而不是回空列表——「页面关了」和「确实没有日志」必须
能区分。

SSE 与 `/api/stats/events/stream` 同一范式（有界缓冲、每订阅者上限、
超限 429 + `Retry-After`、15s 注释帧、`Cache-Control: no-store` +
`X-Accel-Buffering: no`）。差异只有两处：

1. **首帧回放**：先把缓冲里的最近 N 行按旧→新发一遍，再转实时。否则打开
   页面是一条空白，要等到下一条日志才知道连上了。回放与实时用 `seq` 去重：
   订阅先建、快照后取，两者交叠的那几行只发一次。
2. 每行都是独立 JSON，前端按 `seq` 排序/去重。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth.rbac import require_admin
from ..compat.openai.errors import error_payload
from ..stats.live import Subscription, TooManySubscribers
from ..webapp.log_buffer import LogBuffer, sync_root_handler
from .deps import Services, principal_from_request

# 心跳间隔：与统计实时流同一口径（代理通常在 60s 断静默连接）。
HEARTBEAT_SECONDS = 15.0

# 非流式端点默认返回行数；上限由缓冲容量决定（不能超过实际保留的行）。
DEFAULT_LOG_LIMIT = 200

_DISABLED = error_payload("log page disabled", "not_found", 404)


def _sse_frame(line: dict[str, Any]) -> str:
    """一帧 SSE：与统计实时流同格式（单行 `data:` + 空行）。"""
    return f"data: {json.dumps(line, ensure_ascii=False, separators=(',', ':'))}\n\n"


async def _stream_logs(request: Request, buffer: LogBuffer,
                       subscription: Subscription, *, limit: int,
                       heartbeat: float = HEARTBEAT_SECONDS) -> AsyncIterator[str]:
    """SSE 主体：注释帧 → 回放快照 → 实时推送（按 seq 去重）→ 断开即注销。

    `finally` 注销是硬要求：客户端关标签页时生成器被取消，漏注销会把
    订阅槽位耗到全员 429。
    """
    try:
        # 首条注释帧：让 EventSource 立刻进入 OPEN，页面不必等到第一行真实日志。
        yield ": connected\n\n"
        last_seq = 0
        for line in buffer.snapshot(limit=limit):
            last_seq = max(last_seq, int(line.get("seq") or 0))
            yield _sse_frame(line)
        while True:
            if await request.is_disconnected():
                return
            # 热更关闭日志页：结束流（浏览器会自动重连并拿到 404，页面显示关闭状态）。
            if not buffer.enabled:
                return
            batch = subscription.drain()
            if batch:
                for line in batch:
                    seq = int(line.get("seq") or 0)
                    # 快照与 push 可能交叠（订阅先建、快照后取）：按 seq 去重。
                    if seq <= last_seq:
                        continue
                    last_seq = seq
                    yield _sse_frame(line)
                continue
            try:
                await asyncio.wait_for(subscription.wakeup.wait(), timeout=heartbeat)
            except TimeoutError:
                yield ": heartbeat\n\n"
    finally:
        buffer.hub.unsubscribe(subscription)


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    buffer = services.log_buffer

    @router.get("/api/logs")
    async def list_logs(principal=Depends(principal_from_request),
                        limit: int = DEFAULT_LOG_LIMIT,
                        request_id: str | None = None,
                        since: int | None = None,
                        level: str | None = None):
        """最近 N 行（旧→新）；可按 `request_id` / 时间窗 / 级别过滤。

        三条数据路径，按优先级：

        1. **带 `request_id`** → 直接查落库表（`log_events`）。一次请求的全部
           日志（含轮换到别的渠道的尝试）都带同一个 id，这是排障主入口。
        2. **落库开启** → 查落库表（重启后仍有历史），内存环只用于 SSE。
        3. **落库关闭** → 退回内存环（与旧行为逐字节一致）。

        为什么 2 不合并内存环：两边是同一份数据的两种保留期，合并要去重 `seq`
        且顺序会交错；页面只需要一份稳定的历史。
        """
        require_admin(principal)
        sync_root_handler(buffer)          # 热更从 0 打开后无需重启即可挂上
        repo = services.log_repo
        if repo is not None and repo.enabled:
            capacity = repo.capacity()
            clamped = max(1, min(limit, capacity))
            return {"lines": repo.query(limit=clamped, request_id=request_id,
                                        since=since, level=level),
                    "capacity": capacity, "limit": clamped,
                    "persisted": True}
        if not buffer.enabled:
            return JSONResponse(status_code=404, content=_DISABLED)
        capacity = buffer.capacity()
        clamped = max(1, min(limit, capacity))
        lines = buffer.snapshot(limit=clamped)
        if request_id:
            lines = [line for line in lines
                     if line.get("request_id") == request_id]
        return {"lines": lines, "capacity": capacity, "limit": clamped,
                "persisted": False}

    @router.get("/api/logs/stream")
    async def stream_logs(request: Request,
                          principal=Depends(principal_from_request),
                          limit: int = DEFAULT_LOG_LIMIT):
        """实时流（SSE）：先回放最近 N 行，再逐条推送新行。"""
        require_admin(principal)
        sync_root_handler(buffer)
        if not buffer.enabled:
            return JSONResponse(status_code=404, content=_DISABLED)
        try:
            subscription = buffer.hub.subscribe()
        except TooManySubscribers:
            return JSONResponse(
                status_code=429,
                content=error_payload("too many log subscribers",
                                      "rate_limit_exceeded", 429),
                headers={"Retry-After": "10"})
        buffer.bind_loop(asyncio.get_running_loop())
        capacity = buffer.capacity()
        return StreamingResponse(
            _stream_logs(request, buffer, subscription,
                         limit=max(1, min(limit, capacity))),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no",
                     "Connection": "keep-alive"},
        )

    return router

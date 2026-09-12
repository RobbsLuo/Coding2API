"""SSE 流包装：长空隙插入心跳帧（防反向代理空闲超时断流）。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from ..engine.sse import SSE_COMMENT

# 上游思考/排队时可能长时间不吐字节；nginx proxy_read_timeout 默认 60s，
# 空闲超过它就会断开连接。SSE 注释帧被客户端忽略，只为保活。
KEEPALIVE_SECONDS = 15.0


async def with_keepalive(frames: AsyncIterator[bytes],
                         interval: float = KEEPALIVE_SECONDS) -> AsyncIterator[bytes]:
    """在上游无输出的空隙插入 SSE 注释帧。

    实现要点：取下一帧用**可复用的 Task** 而不是 `wait_for(anext(...))`。
    后者在超时时会 cancel 掉 `anext` 协程，取消会穿透进上游生成器并把
    它关闭——心跳反而变成了断流。这里用 `asyncio.wait` 与定时器竞争，
    超时只发注释、待取帧的 Task 继续存活。
    """
    iterator = frames.__aiter__()
    pending: asyncio.Task[bytes] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(anext(iterator))
            ticker = asyncio.ensure_future(asyncio.sleep(interval))
            done, _ = await asyncio.wait({pending, ticker},
                                         return_when=asyncio.FIRST_COMPLETED)
            if pending in done:
                ticker.cancel()
                try:
                    frame = pending.result()
                except StopAsyncIteration:
                    return
                pending = None
                yield frame
                continue
            # 定时器先到：上游暂无输出 → 发心跳，保留 pending 继续等
            ticker.cancel()
            yield SSE_COMMENT
    finally:
        # 客户端断开 / 生成器关闭：清掉在途任务，避免 "Task was destroyed"
        for task in (pending,):
            if task is not None and not task.done():
                task.cancel()

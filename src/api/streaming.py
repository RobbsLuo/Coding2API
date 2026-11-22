"""SSE 流包装：长空隙插入心跳帧（防反向代理空闲超时断流）。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from starlette.responses import JSONResponse, Response, StreamingResponse

from ..compat.openai.errors import error_payload
from ..engine.sse import SSE_COMMENT
from ..provider.base import aclose_stream

logger = logging.getLogger(__name__)

# 上游思考/排队时可能长时间不吐字节；nginx proxy_read_timeout 默认 60s，
# 空闲超过它就会断开连接。SSE 注释帧被客户端忽略，只为保活。
KEEPALIVE_SECONDS = 15.0

# 非流式保活字节：JSON 允许任意前导空白（RFC 8259 §2 `JSON-text = ws value ws`），
# 解析器（Python json / JS JSON.parse / Go encoding/json）都照常接受
# `"\n\n{\"a\":1}"`。发换行比发空格更不容易被中间盒做行首嗅探改写。
JSON_KEEPALIVE_BYTES = b"\n"

# 首帧宽限（秒）：先不发 200 头，等上游第一帧到达再决定状态码。
# 只用于「未出帧就用尽」的请求级错误（400）；等不到就照常发头继续流，
# **不能取消在途取帧任务**——取消会穿透关闭上游流，慢上下文首字 40–71s
# 的请求会被误杀成空回复。
STREAM_START_GRACE_SECONDS = 5.0


async def with_keepalive(frames: AsyncIterator[bytes],
                         interval: float | None = None,
                         keepalive_frame: bytes = SSE_COMMENT) -> AsyncIterator[bytes]:
    """在上游无输出的空隙插入心跳帧。

    `keepalive_frame` 由出口协议决定：chat / Responses 用 SSE 注释帧（被客户端
    忽略），Anthropic 用具名 `event: ping`（部分客户端只解析 `event:` 行，注释帧
    对它们等于没有字节，长静默下仍会判超时）。

    `interval=None` 读模块常量 `KEEPALIVE_SECONDS`（运行期读而非默认参数，
    否则 import 时就定死，测试无法缩短）。

    实现要点：取下一帧用**可复用的 Task** 而不是 `wait_for(anext(...))`。
    后者在超时时会 cancel 掉 `anext` 协程，取消会穿透进上游生成器并把
    它关闭——心跳反而变成了断流。这里用 `asyncio.wait` 与定时器竞争，
    超时只发心跳、待取帧的 Task 继续存活。
    """
    if interval is None:
        interval = KEEPALIVE_SECONDS
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
            yield keepalive_frame
    finally:
        # 客户端断开 / 生成器关闭：清掉在途任务，避免 "Task was destroyed"。
        # cancel() 只是「请求取消」，必须 await 到任务真正结束再退出——否则
        # 生成器所在事件循环很快回收，任务以 pending 状态被销毁并打印告警。
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        # 同样要显式关闭上游迭代器：提前结束（首帧改判 400 / 客户端断开）时
        # `async for` 不会关闭它，节流名额只能等 GC。
        await aclose_stream(iterator)


async def _first_then(first: bytes, iterator: AsyncIterator[bytes],
                      ) -> AsyncIterator[bytes]:
    """已取到首帧：先回填，再续拉内层迭代器。

    `finally` 必须显式关闭内层：消费方提前 `break` / `aclose()` 时 `async for`
    不会关闭它，上游流的节流名额要等 GC 才归还——突发断连会把上游并发槽占满。
    """
    try:
        yield first
        async for frame in iterator:
            yield frame
    finally:
        await aclose_stream(iterator)


async def _after_pending(pending: asyncio.Task[bytes],
                         iterator: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """宽限期已过：先发头，再等同一个在途取帧任务（不取消、不重取）。

    `finally` 显式关闭内层迭代器：消费方提前 `break` / `aclose()` 时 `async
    for` 不会关闭它，上游流的节流名额只能等 GC 归还。客户端断开时外层任务
    被取消，取消会穿透进 `await pending`，故这里不必再手动取消在途任务。
    """
    try:
        try:
            first = await pending
        except StopAsyncIteration:
            return
        yield first
        async for frame in iterator:
            yield frame
    finally:
        await aclose_stream(iterator)


async def respond_with_stream_start(frames: AsyncIterator[bytes],
                                    terminal: dict[str, str],
                                    media_type: str = "text/event-stream",
                                    ) -> Response:
    """拉首帧后再发响应头：请求级错误可在头阶段返回 400。

    `terminal` 由 `Executor.stream(on_terminal=...)` 在整条回退链**未出帧就用尽**
    时写入 `{"code": ..., "message": ...}`。此前 200 头先发出，随后只能吐错误帧，
    SDK 把「内容风控 / 上下文超限 / 坏图 / 工具 schema 拒收」当成成功响应继续重试。

    只改判 `invalid_request`：其余终态（`no_healthy_credential` 等）维持 200 错误帧，
    避免改掉既有 503 / 排队语义。宽限内无首帧 → 照旧发头，取帧任务保留。
    """
    iterator = frames.__aiter__()
    pending: asyncio.Task[bytes] = asyncio.ensure_future(anext(iterator))
    grace = asyncio.ensure_future(asyncio.sleep(STREAM_START_GRACE_SECONDS))
    try:
        done, _ = await asyncio.wait({pending, grace},
                                     return_when=asyncio.FIRST_COMPLETED)
    except BaseException:                    # 客户端断开 / 任务被取消
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await aclose_stream(iterator)
        raise
    finally:
        grace.cancel()
    if pending not in done:                    # 宽限到点：发头，继续等原任务
        return StreamingResponse(_after_pending(pending, iterator),
                                 media_type=media_type)
    try:
        first = pending.result()
    except StopAsyncIteration:
        await aclose_stream(iterator)          # 空流：保持原有 200 空 body
        return StreamingResponse(b"", media_type=media_type)
    if terminal.get("code") == "invalid_request":
        await aclose_stream(iterator)
        return JSONResponse(
            error_payload(terminal.get("message", ""), "invalid_request", 400),
            status_code=400)
    return StreamingResponse(_first_then(first, iterator), media_type=media_type)


# 保活已提交 200 之后上游才失败时的正文：状态码已经发出去了，改不回来，只能
# 给一个可解析的 JSON 错误体（比截断强：截断的 JSON 在客户端是解码异常，
# 完全看不出是上游问题）。文案固定，不回显上游正文。
LATE_FAILURE_PAYLOAD: dict[str, Any] = {
    "error": {
        "message": "upstream request failed after the response had started",
        "type": "api_error",
        "code": "upstream_error",
        "status": 502,
    }
}


def _dumps(payload: Any) -> bytes:
    """与 `JSONResponse.render` 逐字节一致（分隔符/allow_nan 都对齐）。"""
    return json.dumps(payload, ensure_ascii=False, allow_nan=False,
                      indent=None, separators=(",", ":")).encode("utf-8")


async def respond_with_keepalive(
        work: Callable[[], Awaitable[dict[str, Any]]],
        *, grace: float,
        convert: Callable[[dict[str, Any]], Any] | None = None,
        release: Callable[[], None] | None = None) -> Response:
    """非流式出口的保活（`nonstream_keepalive_seconds`，默认 0 关闭）。

    **宽限期设计**：先等 `grace` 秒。

    - 期间完成 → 返回普通 `JSONResponse`：状态码、`Content-Length`、字节内容
      与不开保活时**完全一致**，异常也照常抛给上层异常处理器（400/503 不变）。
    - 超时 → 才提交 200 + chunked，先吐空白字节再补正文。

    为什么不一开始就用 `StreamingResponse`：那样 200 在聚合前就发出去了，
    之后任何失败（无可用凭证 503 / 请求无效 400）都改不回来。宽限期把
    「改不回来」的窗口压到只有真正超过 `grace` 的请求。

    空白字节合法：RFC 8259 §2 规定 `JSON-text = ws value ws`，值前允许任意
    空白，故 `"\\n\\n{...}"` 仍是合法 JSON（Python `json.loads` / JS
    `JSON.parse` 均已实测接受）。
    """
    task = asyncio.ensure_future(work())
    try:
        done, _ = await asyncio.wait({task}, timeout=grace)
    except BaseException:
        # 客户端断开 / 上层取消：必须把工作协程一起收掉，否则它继续跑完整个
        # 上游调用（真实计费）却没人接收结果。
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if release is not None:
            release()
        raise
    if task in done:
        try:
            completion = task.result()
        finally:
            if release is not None:
                release()
        return JSONResponse(convert(completion) if convert else completion)
    return StreamingResponse(
        _json_frames(task, grace, convert, release),
        media_type="application/json",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"})


async def _json_frames(task: asyncio.Task[dict[str, Any]], interval: float,
                       convert: Callable[[dict[str, Any]], Any] | None,
                       release: Callable[[], None] | None) -> AsyncIterator[bytes]:
    """慢请求的正文生成器：空闲 `interval` 秒就吐一个空白字节。"""
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=interval)
            if not task.done():
                yield JSON_KEEPALIVE_BYTES
        try:
            completion = task.result()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 已提交 200，只能写进正文
            logger.warning("非流式保活：200 已提交后上游才失败，正文降级为错误体",
                           exc_info=True)
            yield _dumps(LATE_FAILURE_PAYLOAD)
            return
        yield _dumps(convert(completion) if convert else completion)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if release is not None:
            release()

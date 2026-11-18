"""首帧延迟发头：请求级错误在 200 头发出前改判 400。

覆盖四条分支（`api/streaming.respond_with_stream_start`）：
- 首帧即请求级错误 → 400 JSON，并关闭出口迭代器（归还并发租约）；
- 首帧正常 → SSE 200，帧序列逐字节不变；
- 宽限期内无首帧 → 先发 200，再用同一个在途任务继续等（**不取消**，慢首字
  请求不能被误杀）；
- 空流 → 保持原有 200 空 body。
"""

from __future__ import annotations

import asyncio

import pytest
from starlette.responses import JSONResponse, StreamingResponse

from src.api import streaming as streaming_module
from src.api.streaming import respond_with_stream_start

pytestmark = pytest.mark.anyio


def _gen(frames: list[bytes], *, mark: list[str] | None = None,
         delay: float = 0.0):
    async def iterator():
        try:
            if delay:
                await asyncio.sleep(delay)
            for frame in frames:
                yield frame
        finally:
            if mark is not None:
                mark.append("closed")
    return iterator()


async def test_first_frame_request_error_becomes_400():
    """首帧就是请求级错误 → 400 JSON，不再以 200 + 错误帧欺骗 SDK。"""
    mark: list[str] = []
    terminal: dict[str, str] = {"code": "invalid_request",
                                "message": "bad tool schema"}
    frames = _gen([b'data: {"error": {"code": "invalid_request"}}\n\n'],
                  mark=mark)

    response = await respond_with_stream_start(frames, terminal)

    assert isinstance(response, JSONResponse)
    assert response.status_code == 400
    body = response.body.decode()
    assert "invalid_request" in body and "bad tool schema" in body
    assert mark == ["closed"]          # 出口生成器被关闭 → 租约归还


async def test_first_frame_content_keeps_200_and_same_frames():
    """正常首帧：状态码仍是 200，首帧回填后帧序列逐字节不变。"""
    terminal: dict[str, str] = {}
    frames = _gen([b"first", b"second"])

    response = await respond_with_stream_start(frames, terminal)

    assert isinstance(response, StreamingResponse)
    assert response.status_code == 200
    chunks = [chunk async for chunk in response.body_iterator]
    assert chunks == [b"first", b"second"]


async def test_grace_timeout_sends_headers_and_keeps_pending_task(monkeypatch):
    """宽限期到点：先发头，但**不取消**在途取帧任务（慢首字 40–71s 场景）。"""
    monkeypatch.setattr(streaming_module, "STREAM_START_GRACE_SECONDS", 0.01)
    terminal: dict[str, str] = {}
    frames = _gen([b"late", b"later"], delay=0.05)

    response = await respond_with_stream_start(frames, terminal)

    assert isinstance(response, StreamingResponse)
    chunks = [chunk async for chunk in response.body_iterator]
    assert chunks == [b"late", b"later"]


async def test_empty_stream_returns_empty_200():
    """空流（上游什么都没发就正常结束）保持原有 200 空 body。"""
    terminal: dict[str, str] = {}
    response = await respond_with_stream_start(_gen([]), terminal)

    assert isinstance(response, StreamingResponse)
    chunks = [chunk async for chunk in response.body_iterator]
    assert chunks == []


async def test_other_terminal_codes_stay_200_frames():
    """非请求级终态（如全池耗尽）不改状态码，维持既有 200 错误帧语义。"""
    terminal: dict[str, str] = {"code": "no_healthy_credential",
                                "message": "all credentials cooling"}
    response = await respond_with_stream_start(
        _gen([b'data: {"error": {"code": "no_healthy_credential"}}\n\n']),
        terminal)

    assert isinstance(response, StreamingResponse)
    assert response.status_code == 200


async def test_grace_timeout_then_empty_stream_stops_cleanly(monkeypatch):
    """宽限到点后上游也没帧就结束：body 为空，不抛异常。"""
    monkeypatch.setattr(streaming_module, "STREAM_START_GRACE_SECONDS", 0.01)
    terminal: dict[str, str] = {}

    async def slow_empty():
        await asyncio.sleep(0.05)
        if False:                       # pragma: no cover - 生成器占位
            yield b""

    response = await respond_with_stream_start(slow_empty(), terminal)

    chunks = [chunk async for chunk in response.body_iterator]
    assert chunks == []


async def test_close_tolerates_iterator_without_aclose():
    """出口迭代器若不是 async generator（无 aclose），关闭阶段不能崩。"""

    class NoAclose:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    response = await respond_with_stream_start(NoAclose(), {})

    assert isinstance(response, StreamingResponse)
    assert [chunk async for chunk in response.body_iterator] == []


async def test_cancelled_while_waiting_cleans_pending_and_iterator(monkeypatch):
    """首帧等待期间客户端断开：取消在途任务并关闭迭代器（否则上游名额泄漏）。"""
    monkeypatch.setattr(streaming_module, "STREAM_START_GRACE_SECONDS", 10.0)
    mark: list[str] = []
    frames = _gen([b"never"], mark=mark, delay=10.0)

    task = asyncio.ensure_future(respond_with_stream_start(frames, {}))
    await asyncio.sleep(0)                     # 让 pending 真正开始等上游
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert mark == ["closed"]
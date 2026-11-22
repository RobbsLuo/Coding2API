"""流包装器收尾必须关闭内层迭代器（否则上游节流名额等 GC 才归还）。

`_first_then` / `_after_pending` 与 Playground 的 `_release_when_done` 都是
「包一层再把帧转发出去」的生成器。消费方提前 `break` / `aclose()` 时
`async for` **不会**关闭被包装的迭代器：不显式 `aclose_stream` 的话，客户端
中途断连只能靠 GC 归还，突发断连会把上游并发槽占满。
"""

from __future__ import annotations

import asyncio

import pytest

from src.api import streaming as streaming_module
from src.api.playground import _release_when_done
from src.api.streaming import respond_with_stream_start


def _gen(frames: list[bytes], marks: list[str]):
    """内层迭代器：记录自己被关闭（`finally` 只会在关闭/耗尽时跑一次）。"""
    async def iterator():
        try:
            for frame in frames:
                yield frame
        finally:
            marks.append("closed")
    return iterator()


@pytest.mark.anyio
async def test_first_then_closes_inner_after_full_iteration():
    """正常迭代完：内层被关闭一次（不是靠 GC）。"""
    marks: list[str] = []
    frames = _gen([b"first", b"second"], marks)

    response = await respond_with_stream_start(frames, {})
    chunks = [chunk async for chunk in response.body_iterator]

    assert chunks == [b"first", b"second"]
    assert marks == ["closed"]


@pytest.mark.anyio
async def test_first_then_closes_inner_on_early_break():
    """消费方提前 break（客户端断开）：内层立即关闭。"""
    marks: list[str] = []
    frames = _gen([b"first", b"second", b"third"], marks)

    response = await respond_with_stream_start(frames, {})
    async for chunk in response.body_iterator:
        assert chunk == b"first"
        break
    await response.body_iterator.aclose()

    assert marks == ["closed"]


@pytest.mark.anyio
async def test_first_then_closes_inner_and_propagates_error():
    """内层抛错：原异常继续上抛，内层仍被关闭。"""
    marks: list[str] = []

    async def boom():
        try:
            yield b"first"
            raise RuntimeError("upstream broke")
        finally:
            marks.append("closed")

    response = await respond_with_stream_start(boom(), {})
    with pytest.raises(RuntimeError, match="upstream broke"):
        [chunk async for chunk in response.body_iterator]

    assert marks == ["closed"]


@pytest.mark.anyio
async def test_after_pending_closes_inner_after_full_iteration(monkeypatch):
    """宽限到点后正常走完：内层被关闭一次。"""
    monkeypatch.setattr(streaming_module, "STREAM_START_GRACE_SECONDS", 0.01)
    marks: list[str] = []

    async def slow():
        try:
            await asyncio.sleep(0.05)
            yield b"late"
        finally:
            marks.append("closed")

    response = await respond_with_stream_start(slow(), {})
    chunks = [chunk async for chunk in response.body_iterator]

    assert chunks == [b"late"]
    assert marks == ["closed"]


@pytest.mark.anyio
async def test_after_pending_closes_inner_on_early_break(monkeypatch):
    """宽限到点后消费方提前断开：内层被关闭。"""
    monkeypatch.setattr(streaming_module, "STREAM_START_GRACE_SECONDS", 0.01)
    marks: list[str] = []

    async def slow():
        try:
            await asyncio.sleep(0.05)
            yield b"late"
            yield b"later"
        finally:
            marks.append("closed")

    response = await respond_with_stream_start(slow(), {})
    async for _chunk in response.body_iterator:
        break
    await response.body_iterator.aclose()

    assert marks == ["closed"]


@pytest.mark.anyio
async def test_after_pending_cancel_during_first_frame_closes_inner():
    """首帧仍在途时客户端断开（取消外层）：在途取帧随取消终止，内层被关闭。"""
    marks: list[str] = []

    async def slow():
        try:
            await asyncio.sleep(10)
            yield b"never"
        finally:
            marks.append("closed")

    iterator = slow()
    pending = asyncio.ensure_future(anext(iterator))   # 与真实路径一致
    body = streaming_module._after_pending(pending, iterator)
    reader = asyncio.ensure_future(body.__anext__())
    await asyncio.sleep(0.02)             # 让外层真正挂在 `await pending` 上

    reader.cancel()                       # 模拟客户端断开
    with pytest.raises(asyncio.CancelledError):
        await reader

    assert pending.cancelled()
    assert marks == ["closed"]


# ------------------------------------------------- Playground 包装器（租约 + 关闭）


class _Lease:
    def __init__(self) -> None:
        self.released = 0

    def release(self) -> None:
        self.released += 1


@pytest.mark.anyio
async def test_playground_release_when_done_closes_inner_after_iteration():
    """正常走完：租约归还 + 内层关闭。"""
    marks: list[str] = []
    lease = _Lease()

    chunks = [chunk async for chunk in _release_when_done(
        _gen([b"a", b"b"], marks), lease)]

    assert chunks == [b"a", b"b"]
    assert marks == ["closed"]
    assert lease.released == 1


@pytest.mark.anyio
async def test_playground_release_when_done_closes_inner_on_disconnect():
    """客户端断开（提前 break 后 aclose）：租约与内层都要立即释放。"""
    marks: list[str] = []
    lease = _Lease()
    wrapper = _release_when_done(_gen([b"a", b"b", b"c"], marks), lease)

    async for _chunk in wrapper:
        break
    await wrapper.aclose()

    assert marks == ["closed"]
    assert lease.released == 1


@pytest.mark.anyio
async def test_playground_release_when_done_releases_on_error():
    """内层抛错：原异常上抛，租约与内层都处理干净。"""
    marks: list[str] = []
    lease = _Lease()

    async def boom():
        try:
            yield b"a"
            raise RuntimeError("upstream broke")
        finally:
            marks.append("closed")

    with pytest.raises(RuntimeError, match="upstream broke"):
        [chunk async for chunk in _release_when_done(boom(), lease)]

    assert marks == ["closed"]
    assert lease.released == 1

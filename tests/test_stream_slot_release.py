"""上游流提前结束时，节流名额必须同步归还（max_concurrency 的正确性前提）。

背景：provider 在 `stream_chat` 的 `finally` 里调 `pacer.release(key)` 归还
在途名额，而 `async for ... break` 不会关闭 async generator——CPython 只在耗尽 /
显式 `aclose()` / GC 的 asyncgen finalizer 时才跑 `finally`。于是名额会推迟归还
甚至永久丢失：桶停在满载，新请求在 `wait_turn` 无限阻塞，表现为「用了三次就
限制」而非「并发三」。

这些测试直接断言 `Pacer._inflight` 归零，覆盖全部提前结束消费的路径。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from src.compat.openai.request import ChatRequest
from src.engine.executor import Executor, ExecutorDeps, NoHealthyCredential
from src.engine.scheduler import Candidate
from src.provider.base import ErrKind, Event, EventKind
from src.provider.codearts import CodeArtsProvider
from src.tasks.pacer import Pacer

CRED_DATA = {"access_key_id": "AK", "secret_access_key": "SK"}
CRED_KEY = "codearts:3c4e58eff203b042"          # stable_key("codearts", access_key_id)


# --------------------------------------------------------- 辅助替身


@dataclass
class _Credentials:
    data: dict = field(default_factory=lambda: dict(CRED_DATA))

    def candidates(self, providers=None, *, selectable_only=False):
        return [Candidate(credential_id="c1", provider="codearts")]

    def provider_of(self, credential_id):
        return "codearts"

    def credential_data(self, credential_id):
        return self.data

    def save_success(self, credential_id, *, model=None):
        pass

    def save_error(self, credential_id, outcome):  # pragma: no cover - 未调用
        pass


class _Scheduler:
    """`should_rotate` 可配，用来强制 executor 走换号重试。"""

    def __init__(self, rotate: bool = False) -> None:
        self._rotate = rotate

    def select(self, candidates, tried, now):
        for candidate in candidates:
            if candidate.credential_id not in tried:
                return candidate.credential_id
        return None

    def should_rotate(self, tried):
        return self._rotate

    def note_error(self, candidate, kind, now, *, model=None):
        return type("Outcome", (), {"cooling_until": None, "err_count": 1,
                                    "disabled": False})()


class _ErrorStreamClient:
    """吐一个正文后立刻给流内并发超限错误（TM.00001041）。"""

    async def stream_chat(self, credential, payload, model) -> AsyncIterator[Event]:
        yield Event(kind=EventKind.CONTENT, content="hi")
        yield Event(kind=EventKind.ERROR, error_message="TM.00001041",
                    error_kind=ErrKind.CONCURRENCY)


class _HangingClient:
    """吐一个正文后永久挂起：只能靠外部关闭结束，用于模拟客户端断开。"""

    async def stream_chat(self, credential, payload, model) -> AsyncIterator[Event]:
        try:
            yield Event(kind=EventKind.CONTENT, content="hi")
            await asyncio.sleep(3600)
        finally:
            yield Event(kind=EventKind.FINISH, finish_reason="stop")   # pragma: no cover


def _request(index: int = 0, *, stream: bool = True) -> ChatRequest:
    messages = [{"role": "user", "content": f"hi{index}"}]
    return ChatRequest(model="deepseek-v4-flash-0731", messages=messages,
                       stream=stream, raw={"messages": messages})


def _build(client, *, rotate: bool = False, max_auto_continues: int = 0,
           complete_timeout_seconds: float = 600):
    pacer = Pacer(0, 0, allow_concurrent=True, max_concurrency=3)
    provider = CodeArtsProvider(client=client, pacer=pacer)
    executor = Executor(ExecutorDeps(
        providers={"codearts": provider}, credentials=_Credentials(),
        scheduler=_Scheduler(rotate=rotate),
        max_auto_continues=max_auto_continues,
        complete_timeout_seconds=complete_timeout_seconds,
    ))
    return pacer, executor


def _inflight(pacer: Pacer) -> int:
    return sum(pacer._inflight.values())


# --------------------------------------------------------- 流内错误 break


@pytest.mark.asyncio
async def test_stream_error_break_releases_slot():
    """流内错误 → executor break：名额必须当场归还，不能等 GC。"""
    pacer, executor = _build(_ErrorStreamClient())

    for index in range(6):
        async for _ in executor.stream_guarded(_request(index), username="u"):
            pass
        assert _inflight(pacer) == 0, f"第 {index} 次请求后名额未归还"

    # 桶空着，第 7 个请求立刻拿到名额（原缺陷：攒满 3 个后永久阻塞）
    await asyncio.wait_for(pacer.wait_turn(CRED_KEY), timeout=0.5)


@pytest.mark.asyncio
async def test_rotation_retry_does_not_accumulate_slots():
    """换号重试每次重新占名额：上一次必须已归还，否则单调累积到满载。"""
    pacer, executor = _build(_ErrorStreamClient(), rotate=True)

    for index in range(4):
        async for _ in executor.stream_guarded(_request(index), username="u"):
            pass
        assert _inflight(pacer) == 0, f"第 {index} 次重试后名额未归还"

    await asyncio.wait_for(pacer.wait_turn(CRED_KEY), timeout=0.5)


@pytest.mark.asyncio
async def test_three_concurrent_requests_really_run_in_parallel():
    """三个同时在途的请求应各自拿到名额（并发三），第四个才排队。"""
    pacer = Pacer(0, 0, allow_concurrent=True, max_concurrency=3)
    assert await pacer.wait_turn(CRED_KEY) is None
    assert await pacer.wait_turn(CRED_KEY) is None
    assert await pacer.wait_turn(CRED_KEY) is None
    assert _inflight(pacer) == 3

    # 第四个挂起等名额
    pending = asyncio.ensure_future(pacer.wait_turn(CRED_KEY))
    await asyncio.sleep(0)
    assert not pending.done()

    pacer.release(CRED_KEY)
    await asyncio.wait_for(pending, timeout=0.5)
    assert _inflight(pacer) == 3            # 归还一个、占用一个，总数不变


# --------------------------------------------------------- 客户端断开


@pytest.mark.asyncio
async def test_client_disconnect_releases_slot():
    """只取首帧就 aclose（客户端断开）：上游挂起的流也必须被关闭。"""
    pacer, executor = _build(_HangingClient())

    frames = executor.stream_guarded(_request(), username="u")
    async for _ in frames:
        break
    await frames.aclose()
    assert _inflight(pacer) == 0


@pytest.mark.asyncio
async def test_client_disconnect_with_continuation_releases_slot():
    """开启截断续写时断开：ContinuationStream 内层的 provider 流也要关闭。"""
    pacer, executor = _build(_HangingClient(), max_auto_continues=3)

    frames = executor.stream_guarded(_request(), username="u")
    async for _ in frames:
        break
    await frames.aclose()
    assert _inflight(pacer) == 0


@pytest.mark.asyncio
async def test_task_cancellation_releases_slot():
    """执行任务被 cancel（上游重试耗尽/超时）：名额同样要归还。"""
    pacer, executor = _build(_HangingClient())

    async def run() -> None:
        async for _ in executor.stream_guarded(_request(), username="u"):
            pass

    task = asyncio.ensure_future(run())
    await asyncio.sleep(0)                  # 让流真正开始
    task.cancel()
    with pytest.raises((asyncio.CancelledError, GeneratorExit)):
        await task
    assert _inflight(pacer) == 0


# --------------------------------------------------------- 非流式路径


@pytest.mark.asyncio
async def test_complete_error_releases_slot():
    """非流式聚合遇流内错误：同样必须归还名额。"""
    pacer, executor = _build(_ErrorStreamClient())

    for index in range(6):
        with pytest.raises(NoHealthyCredential):
            await executor.complete(_request(index, stream=False), username="u")
        assert _inflight(pacer) == 0, f"第 {index} 次请求后名额未归还"


@pytest.mark.asyncio
async def test_complete_timeout_releases_slot():
    """非流式聚合超时（上游挂起）：cancel 之后名额要归还。"""
    pacer, executor = _build(_HangingClient(), complete_timeout_seconds=0.05)

    with pytest.raises(NoHealthyCredential):
        await executor.complete(_request(stream=False), username="u")
    assert _inflight(pacer) == 0


# --------------------------------------------------------- 工具函数


@pytest.mark.asyncio
async def test_aclose_stream_skips_streams_without_aclose():
    """没有 aclose 的流（纯 AsyncIterator 实现）跳过关闭，不抛异常。"""
    from src.provider.base import aclose_stream

    class _Plain:
        async def __aiter__(self):
            yield Event(kind=EventKind.FINISH, finish_reason="stop")

    await aclose_stream(_Plain())


@pytest.mark.asyncio
async def test_aclose_stream_closes_async_generator():
    from src.provider.base import aclose_stream

    closed: list[str] = []

    async def gen():
        try:
            yield Event(kind=EventKind.CONTENT, content="hi")
        finally:
            closed.append("closed")

    iterator = gen()
    async for _ in iterator:
        break
    await aclose_stream(iterator)
    assert closed == ["closed"]

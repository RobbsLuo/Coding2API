"""本轮隐患修复回归（HTTP 层）——流式断连/心跳与用量记账（#6/#7）。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import pytest

from src.api.streaming import with_keepalive
from src.compat.openai.response import StreamTranslator
from src.engine.sse import SSE_COMMENT
from src.provider.base import Event, EventKind, Usage


@dataclass
class _FakeCredentials:
    provider: str = "trae"
    data: dict = field(default_factory=lambda: {"accessToken": "a"})

    def candidates(self, providers=None, *, selectable_only=False):
        from src.engine.scheduler import Candidate

        return [Candidate(credential_id="c1", provider=self.provider)]

    def provider_of(self, credential_id):
        return self.provider

    def credential_data(self, credential_id):
        return self.data

    def save_success(self, credential_id, *, model=None):
        pass

    def save_error(self, credential_id, outcome):  # pragma: no cover
        pass

class _Scheduler:
    def select(self, candidates, tried, now):
        for candidate in candidates:
            if candidate.credential_id not in tried:
                return candidate.credential_id
        return None

    def should_rotate(self, tried):
        return False

    def note_error(self, candidate, kind, now, *, model=None,
                   retry_at=None):  # pragma: no cover
        raise AssertionError("unexpected error path")

class _Collector:
    def __init__(self, sink):
        self._sink = sink

    def record(self, **fields):
        self._sink.append(fields)

def _request():
    from src.compat.openai.request import ChatRequest

    return ChatRequest(model="glm-5.2", messages=[{"role": "user", "content": "hi"}],
                       stream=True, raw={"messages": [{"role": "user", "content": "hi"}]})

# ------------------------------------------------------ #6 心跳 / #7 断连


@pytest.mark.asyncio
async def test_with_keepalive_emits_comment_on_idle():
    async def slow():
        await asyncio.sleep(0.05)
        yield b"data: x\n\n"

    frames = [f async for f in with_keepalive(slow(), interval=0.01)]
    assert SSE_COMMENT in frames
    assert frames[-1] == b"data: x\n\n"

@pytest.mark.asyncio
async def test_with_keepalive_passes_frames_through_when_fast():
    async def fast():
        yield b"a"
        yield b"b"

    assert [f async for f in with_keepalive(fast(), interval=5)] == [b"a", b"b"]


@pytest.mark.asyncio
async def test_with_keepalive_uses_protocol_specific_frame():
    """心跳帧随出口协议走：Anthropic 的 ping 事件替换默认注释帧。"""
    ping = b'event: ping\ndata: {"type": "ping"}\n\n'

    async def slow():
        await asyncio.sleep(0.05)
        yield b"data: x\n\n"

    frames = [f async for f in with_keepalive(slow(), interval=0.01,
                                              keepalive_frame=ping)]
    assert ping in frames
    assert SSE_COMMENT not in frames

@pytest.mark.asyncio
async def test_stream_disconnect_records_usage():
    """客户端断开时把已知用量记账（client_disconnect），不再静默丢弃。

    上游序列：content → usage → content。usage 不单独成帧，
    所以第二帧已经是 usage 记录之后的内容，此时断开能看到 token。
    """
    from src.engine.executor import Executor, ExecutorDeps

    recorded = []

    @dataclass
    class Provider:
        id: str = "trae"

        async def stream_chat(self, credential_data, payload, model):
            yield Event(kind=EventKind.CONTENT, content="hello")
            yield Event(kind=EventKind.USAGE, usage=Usage(input_tokens=7, output_tokens=3))
            yield Event(kind=EventKind.CONTENT, content="world")

    executor = Executor(ExecutorDeps(
        providers={"trae": Provider()}, credentials=_FakeCredentials(),
        scheduler=_Scheduler(), stats=_Collector(recorded)))

    stream = executor.stream(_request(), username="alice")
    assert await anext(stream) is not None      # content
    assert await anext(stream) is not None      # 第二条 content（usage 已记录）
    await stream.aclose()
    assert [r["error_type"] for r in recorded] == ["client_disconnect"]
    assert recorded[0]["input_tokens"] == 7
    assert recorded[0]["ttfb_ms"] is not None

@pytest.mark.asyncio
async def test_stream_disconnect_after_done_records_success():
    """[DONE] 已产出后客户端断开：拿到完整回调，按成功记账而非 client_disconnect。

    真实场景（opencode2）：SSE 流的 [DONE] 发出到结束帧 more_body=False
    之间有一拍竞态，框架（Starlette listen_for_disconnect）会把收尾断开
    当中途断开并取消生成器；此时响应内容已完整送达，应记 ok=True。
    """
    from src.engine.executor import Executor, ExecutorDeps

    recorded = []
    successes = []

    @dataclass
    class Provider:
        id: str = "trae"

        async def stream_chat(self, credential_data, payload, model):
            yield Event(kind=EventKind.CONTENT, content="hello")
            yield Event(kind=EventKind.USAGE,
                        usage=Usage(input_tokens=7, output_tokens=3))
            yield Event(kind=EventKind.FINISH, finish_reason="stop")

    class Creds(_FakeCredentials):
        def save_success(self, credential_id, *, model=None):
            successes.append(credential_id)

    executor = Executor(ExecutorDeps(
        providers={"trae": Provider()}, credentials=Creds(),
        scheduler=_Scheduler(), stats=_Collector(recorded)))

    stream = executor.stream(_request(), username="alice")
    while True:
        frame = await anext(stream)
        if b"[DONE]" in frame:
            await stream.aclose()
            break
    assert successes == ["c1"]
    assert len(recorded) == 1
    r = recorded[0]
    assert r["ok"] is True
    assert "error_type" not in r
    assert (r["username"], r["provider"], r["credential_id"]) == ("alice", "trae", "c1")
    assert r["input_tokens"] == 7
    assert r["output_tokens"] == 3
    assert r["ttfb_ms"] is not None

@pytest.mark.asyncio
async def test_stream_success_then_tail_disconnect_not_double_recorded():
    """成功路径已记账后，收尾阶段的断开不再重复记账。

    上游发 FINISH 后 executor 收尾成帧；若客户端在 [DONE] 之后断开，
    断连分支不得再记第二条。
    """
    from src.engine.executor import Executor, ExecutorDeps

    recorded = []

    @dataclass
    class Provider:
        id: str = "trae"

        async def stream_chat(self, credential_data, payload, model):
            yield Event(kind=EventKind.CONTENT, content="a")
            yield Event(kind=EventKind.CONTENT, content="b")
            yield Event(kind=EventKind.FINISH, finish_reason="stop")

    executor = Executor(ExecutorDeps(
        providers={"trae": Provider()}, credentials=_FakeCredentials(),
        scheduler=_Scheduler(), stats=_Collector(recorded)))

    stream = executor.stream(_request(), username="alice")
    while True:
        frame = await anext(stream)
        if b"[DONE]" in frame:
            await stream.aclose()
            break
    assert len(recorded) == 1
    assert recorded[0]["ok"] is True

@pytest.mark.asyncio
async def test_stream_done_disconnect_without_credential(monkeypatch):
    """done_sent 断开但凭证缺失（防御分支）：跳过 save_success，仍按成功记账。"""
    from src.engine.executor import Executor, ExecutorDeps

    recorded = []
    successes = []

    @dataclass
    class Provider:
        id: str = "trae"

        async def stream_chat(self, credential_data, payload, model):
            yield Event(kind=EventKind.CONTENT, content="x")
            yield Event(kind=EventKind.FINISH, finish_reason="stop")

    class Creds(_FakeCredentials):
        def save_success(self, credential_id, *, model=None):
            successes.append(credential_id)

    executor = Executor(ExecutorDeps(
        providers={"trae": Provider()}, credentials=Creds(),
        scheduler=_Scheduler(), stats=_Collector(recorded)))
    async def _no_pick(target, tried, affinity_id=None):
        return None, None

    monkeypatch.setattr(executor, "_pick", _no_pick)

    stream = executor.stream(_request(), username="alice")
    while True:
        frame = await anext(stream)
        if b"[DONE]" in frame:
            await stream.aclose()
            break
    assert successes == []
    assert recorded[0]["ok"] is True
    assert recorded[0]["credential_id"] == "-"

@pytest.mark.asyncio
async def test_stream_guarded_converts_unexpected_error_to_frame():
    from src.engine.executor import Executor, ExecutorDeps

    class Boom:
        id = "trae"

        async def stream_chat(self, credential_data, payload, model):
            yield Event(kind=EventKind.CONTENT, content="x")
            raise RuntimeError("boom")

    executor = Executor(ExecutorDeps(
        providers={"trae": Boom()}, credentials=_FakeCredentials(),
        scheduler=_Scheduler(), stats=_Collector([])))
    frames = [f async for f in executor.stream_guarded(_request())]
    assert b"internal_error" in frames[-1]

@pytest.mark.asyncio
async def test_with_keepalive_cancels_pending_on_close():
    """生成器被关闭时，在途的取帧任务必须被取消（不留悬挂 Task）。"""
    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.sleep(60)
        yield b"never"

    stream = with_keepalive(slow(), interval=0.01)
    first = await anext(stream)
    assert first is SSE_COMMENT and started.is_set()
    await stream.aclose()

def test_stream_guarded_records_disconnect_before_error_frame():
    """stream_guarded 对客户端断开不吞异常（保留 499 语义）。"""
    from src.engine.executor import Executor, ExecutorDeps

    class Slow:
        id = "trae"

        async def stream_chat(self, credential_data, payload, model):
            yield Event(kind=EventKind.CONTENT, content="x")
            await asyncio.sleep(60)

    async def run():
        executor = Executor(ExecutorDeps(
            providers={"trae": Slow()}, credentials=_FakeCredentials(),
            scheduler=_Scheduler(), stats=_Collector([])))
        stream = executor.stream_guarded(_request())
        assert await anext(stream) is not None
        await stream.aclose()          # 不应抛异常

    asyncio.run(run())

@pytest.mark.asyncio
async def test_disconnect_with_bytes_but_no_usage_still_recorded():
    """已吐字节但上游未给 usage 时，断开也要记账（ttfb 分支）。"""
    from src.engine.executor import Executor, ExecutorDeps

    recorded = []

    @dataclass
    class Provider:
        id: str = "trae"

        async def stream_chat(self, credential_data, payload, model):
            yield Event(kind=EventKind.CONTENT, content="partial")
            yield Event(kind=EventKind.CONTENT, content="more")

    executor = Executor(ExecutorDeps(
        providers={"trae": Provider()}, credentials=_FakeCredentials(),
        scheduler=_Scheduler(), stats=_Collector(recorded)))
    stream = executor.stream(_request(), username="alice")
    assert await anext(stream) is not None
    await stream.aclose()
    assert recorded[0]["error_type"] == "client_disconnect"
    assert recorded[0]["input_tokens"] is None
    assert recorded[0]["ttfb_ms"] is not None

@pytest.mark.asyncio
async def test_disconnect_after_usage_only_records_once():
    """断开时 usage 已到 → 走 usage 分支（第一个条件成立）。"""
    from src.engine.executor import Executor, ExecutorDeps

    recorded = []

    @dataclass
    class Provider:
        id: str = "trae"

        async def stream_chat(self, credential_data, payload, model):
            yield Event(kind=EventKind.USAGE, usage=Usage(input_tokens=1))
            yield Event(kind=EventKind.CONTENT, content="x")

    executor = Executor(ExecutorDeps(
        providers={"trae": Provider()}, credentials=_FakeCredentials(),
        scheduler=_Scheduler(), stats=_Collector(recorded)))
    stream = executor.stream(_request(), username="alice")
    assert await anext(stream) is not None      # content 帧（usage 已记录）
    await stream.aclose()
    assert recorded[0]["input_tokens"] == 1

@pytest.mark.asyncio
async def test_disconnect_before_any_output_records_nothing():
    """上游尚未吐任何字节就断开 → 不记账（无用量可言）。"""
    from src.engine.executor import Executor, ExecutorDeps

    recorded = []

    @dataclass
    class Provider:
        id: str = "trae"

        async def stream_chat(self, credential_data, payload, model):
            await asyncio.sleep(60)
            yield Event(kind=EventKind.CONTENT, content="never")

    executor = Executor(ExecutorDeps(
        providers={"trae": Provider()}, credentials=_FakeCredentials(),
        scheduler=_Scheduler(), stats=_Collector(recorded)))

    async def run():
        stream = executor.stream(_request(), username="alice")
        pending = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0.01)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await stream.aclose()

    await run()
    assert recorded == []

def test_translator_error_event_emits_error_frame():
    """ERROR 事件直接喂给 translator 时产出错误帧。

    executor 在流式路径上会先看到 ERROR 并 break（改走换号），
    所以这条分支只有直接使用 translator 时才走到——此处把它固定下来，
    避免以后有人把 ERROR 静默丢弃。
    """
    translator = StreamTranslator("glm-5.2")
    frames = list(translator.translate(
        Event(kind=EventKind.ERROR, error_code=1005, error_message="no quota")))
    assert len(frames) == 1
    payload = json.loads(frames[0].decode().removeprefix("data: ").strip())
    assert payload["error"]["code"] == 1005
    assert payload["error"]["message"] == "no quota"

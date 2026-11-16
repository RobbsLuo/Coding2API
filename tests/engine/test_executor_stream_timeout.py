"""A1：流式首事件超时 + 流内空闲超时。

分两层验证，各自避免真实等待：

- **计时口径**（`_next_event`）用假时钟 + `_TimedReader` 替身逐次断言「这次
  拿到多长的窗口」「deadline 有没有被重置」。这条路径与真实 `asyncio.wait`
  无关，所以毫秒级跑完、零抖动。
- **行为**（换号 / 错误帧收尾 / 记账）用**很短的超时**（50ms）跑真流：上游
  卡死在 `asyncio.Event().wait()` 上，超时必然触发，不存在竞态。

口径（计划 A1）：

- 未出帧超时 → SOFT 冷却 + 换号；**客户端不能收到任何内容**。
- 已出帧超时 → 只发一帧错误 + 收尾，**不换号**，`error_type=upstream_idle_timeout`。
- 计时器只被数据事件重置；注释帧 / 空事件不重置。
- 超时关闭（0）时行为与现状一致。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from src.compat.openai.request import parse_chat_request
from src.compat.openai.response import StreamTranslator
from src.engine.executor import (
    Executor,
    ExecutorDeps,
    _is_data_event,
    _stream_error_type,
    _StreamState,
    _StreamTimeout,
)
from src.engine.scheduler import Scheduler
from src.provider.base import Event, EventKind, Usage
from tests.helpers.events import GOOD

# 假时钟：`Executor._clock` 指向它，超时判断全读这里，不碰真实单调钟
_CLOCK = {"now": 1000.0}


class _FakeReader:
    """`_TimedReader` 的替身：按脚本回放事件，并记录每次收到的窗口长度。

    脚本里用 `TimeoutError` 表示「窗口内没等到事件」（真实实现抛同一个异常），
    用 `StopAsyncIteration` 表示上游正常结束。
    """

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.timeouts: list[float | None] = []

    async def next(self, timeout):
        self.timeouts.append(timeout)
        item = self._script.pop(0)
        if item is TimeoutError:
            raise TimeoutError
        if item is StopAsyncIteration:
            raise StopAsyncIteration
        return item


def _unit_executor(**deps) -> Executor:
    """只用于 `_next_event` 的 executor：无 provider / 无凭证访问。"""
    executor = Executor(ExecutorDeps(providers={}, credentials=None,
                                     scheduler=Scheduler(), **deps))
    executor._clock = lambda: _CLOCK["now"]
    return executor


def _state() -> _StreamState:
    return _StreamState(translator=StreamTranslator("m"), started=0.0, username="u")


@pytest.fixture(autouse=True)
def _reset_clock():
    _CLOCK["now"] = 1000.0
    yield


# --------------------------------------------------------- 计时口径（假时钟）


async def test_first_event_timeout_raises_with_controlled_code():
    """未出帧且窗口耗尽 → `_StreamTimeout(first_event)`（SOFT + 受控 error_code）。"""
    executor = _unit_executor(first_event_timeout_seconds=5)
    state = _state()
    reader = _FakeReader([TimeoutError])
    with pytest.raises(_StreamTimeout) as caught:
        await executor._next_event(reader, state, False)
    assert caught.value.phase == "first_event"
    assert caught.value.error_code == "upstream_first_event_timeout"
    assert caught.value.kind().name == "SOFT"
    # 窗口就是注入值，且只等一次
    assert reader.timeouts == [5]


async def test_idle_timeout_uses_idle_setting_after_first_frame():
    """已出帧 → 用空闲窗口（而不是首事件窗口），phase=idle。"""
    executor = _unit_executor(first_event_timeout_seconds=5,
                             stream_idle_timeout_seconds=30)
    state = _state()
    reader = _FakeReader([TimeoutError])
    with pytest.raises(_StreamTimeout) as caught:
        await executor._next_event(reader, state, True)
    assert caught.value.phase == "idle"
    assert caught.value.error_code == "upstream_idle_timeout"
    assert reader.timeouts == [30]


async def test_only_data_events_reset_deadline():
    """空事件（无载荷）不刷新 deadline：剩余窗口继续变短，最终超时。"""
    executor = _unit_executor(first_event_timeout_seconds=5)
    state = _state()
    reader = _FakeReader([
        Event(kind=EventKind.CONTENT),          # 空正文
        Event(kind=EventKind.TOOL_CALLS),       # 空工具调用
        Event(kind=EventKind.USAGE),            # 无 usage
        TimeoutError,
    ])
    await executor._next_event(reader, state, False)
    assert state.deadline == 1005                  # 计时起点
    _CLOCK["now"] = 1002
    await executor._next_event(reader, state, False)
    assert state.deadline == 1005                  # 空事件不重置
    _CLOCK["now"] = 1004
    await executor._next_event(reader, state, False)
    assert state.deadline == 1005
    # 每次剩下的窗口 = deadline - now
    assert reader.timeouts == [5, 3, 1]
    _CLOCK["now"] = 1005
    with pytest.raises(_StreamTimeout):
        await executor._next_event(reader, state, False)


async def test_data_events_reset_deadline():
    """正文 / 思考 / 工具调用 / usage / 结束都算数据，各自重置计时。"""
    executor = _unit_executor(first_event_timeout_seconds=5)
    state = _state()
    payloads = [
        Event(kind=EventKind.CONTENT, content="x"),
        Event(kind=EventKind.REASONING, content="想"),
        Event(kind=EventKind.TOOL_CALLS, tool_calls=[{"id": "t"}]),
        Event(kind=EventKind.USAGE, usage=Usage(1, 2)),
        Event(kind=EventKind.FINISH, finish_reason="stop"),
    ]
    reader = _FakeReader(payloads)
    for event in payloads:
        _CLOCK["now"] += 4                         # 每帧都在窗口内到达
        assert await executor._next_event(reader, state, False) is event
        assert state.deadline is None              # 数据事件 → 下一帧重新计时
    assert reader.timeouts == [5] * 5


@pytest.mark.parametrize("event, expected", [
    (Event(kind=EventKind.CONTENT, content="x"), True),
    (Event(kind=EventKind.CONTENT), False),
    (Event(kind=EventKind.REASONING, content="x"), True),
    (Event(kind=EventKind.REASONING), False),
    (Event(kind=EventKind.TOOL_CALLS, tool_calls=[{"id": "t"}]), True),
    (Event(kind=EventKind.TOOL_CALLS), False),
    (Event(kind=EventKind.USAGE, usage=Usage(1, 2)), True),
    (Event(kind=EventKind.USAGE), False),
    (Event(kind=EventKind.FINISH, finish_reason="stop"), True),
    # 流内 error 不算「推进响应」：上游连发错误也不能把空闲计时续命
    (Event(kind=EventKind.ERROR, error_message="boom"), False),
])
def test_is_data_event_matrix(event, expected):
    assert _is_data_event(event) is expected


async def test_disabled_timeouts_pass_no_deadline():
    """两个值都是 0（默认）→ 不设窗口（timeout=None），也不写 deadline。"""
    executor = _unit_executor()
    state = _state()
    reader = _FakeReader([Event(kind=EventKind.CONTENT, content="x"),
                          Event(kind=EventKind.CONTENT, content="y")])
    await executor._next_event(reader, state, False)
    await executor._next_event(reader, state, True)
    assert reader.timeouts == [None, None]
    assert state.deadline is None


async def test_timeout_values_are_read_per_event():
    """两个值都经零参 callable 现读：请求进行中改值，下一帧即生效（热更）。"""
    limits = {"first": 5, "idle": 0}
    executor = _unit_executor(
        first_event_timeout_seconds=lambda: limits["first"],
        stream_idle_timeout_seconds=lambda: limits["idle"])
    state = _state()
    reader = _FakeReader([Event(kind=EventKind.CONTENT, content="a"),
                          Event(kind=EventKind.CONTENT, content="b"),
                          Event(kind=EventKind.CONTENT, content="c")])
    await executor._next_event(reader, state, False)      # 首帧：5s
    limits["idle"] = 7
    await executor._next_event(reader, state, True)       # 已出帧：改成 7s
    limits["idle"] = 0
    await executor._next_event(reader, state, True)       # 关闭：None
    assert reader.timeouts == [5, 7, None]


async def test_stream_end_returns_none():
    """上游正常结束（StopAsyncIteration）→ None，由调用方按成功收尾。"""
    executor = _unit_executor(first_event_timeout_seconds=5)
    assert await executor._next_event(_FakeReader([StopAsyncIteration]),
                                      _state(), False) is None


# ------------------------------------------------ 首事件超时：换号（真实短窗口）

STALL = 0.05          # 真实等待 50ms：超时必然触发，不会与「事件恰好到达」竞态


class _StallProvider:
    """按调用次数回放脚本；`stall_at` 指定的那几次调用在回放完后卡死。

    卡死用 `asyncio.Event().wait()`：既不结束也不吐下一帧，模拟「上游接了
    连接却不再出数据」，让首事件/空闲超时必然触发。
    """

    id = "trae"

    def __init__(self, *, first: list[Event], later: list[Event] | None = None,
                 stall_at: tuple[int, ...] = (0,)) -> None:
        self.first = first
        self.later = later
        self.stall_at = stall_at
        self.calls = 0

    async def stream_chat(self, _credential_data, _payload, _model):
        index = self.calls
        self.calls += 1
        for event in self.first if index == 0 else (self.later or []):
            yield event
        if index in self.stall_at:
            await asyncio.Event().wait()          # 卡住：既不结束也不吐下一帧


def _executor(credentials, provider, **deps) -> Executor:
    return Executor(ExecutorDeps(providers={"trae": provider}, credentials=credentials,
                                 scheduler=Scheduler(), default_model="glm-5.2", **deps))


def _stream_request():
    return parse_chat_request({"messages": [{"role": "user", "content": "hi"}],
                               "model": "glm-5.2", "stream": True})


async def _drain(executor, request=None) -> list[bytes]:
    return [frame async for frame in executor.stream(request or _stream_request(),
                                                     username="alice")]


def _content_of(frames: list[bytes]) -> str:
    """把 SSE 帧里的 delta.content 拼起来（只取正文，忽略 role/usage/[DONE]）。"""
    parts: list[str] = []
    for frame in frames:
        text = frame.decode("utf-8")
        if not text.startswith("data: ") or "[DONE]" in text:
            continue
        payload = json.loads(text[len("data: "):].strip())
        for choice in payload.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                parts.append(delta["content"])
    return "".join(parts)


def _error_of(frames: list[bytes]) -> dict | None:
    """从 SSE 帧里找 error 对象（一帧可能同时带错误 JSON 与 [DONE]）。"""
    for frame in frames:
        for line in frame.decode("utf-8").splitlines():
            if not line.startswith("data: ") or line.strip() == "data: [DONE]":
                continue
            payload = json.loads(line[len("data: "):].strip())
            if isinstance(payload, dict) and "error" in payload:
                return payload["error"]
    return None


async def test_first_event_timeout_rotates_to_second_credential(repo):
    """第一个凭证永不吐帧 → SOFT 冷却后换号；客户端只收到第二个凭证的内容。"""
    credentials, db = repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="trae", credential_data={"accessToken": "b"})
    provider = _StallProvider(first=[], later=GOOD)
    executor = _executor(credentials, provider,
                         first_event_timeout_seconds=STALL,
                         short_cooldown_wait_seconds=0)

    frames = await _drain(executor)

    assert _content_of(frames) == "ok"           # 只有第二个凭证的正文
    assert provider.calls == 2
    assert _error_of(frames) is None
    # 被换掉的那个凭证进 SOFT 冷却（短冷却、err_count 不累计）
    cooled = db.connect().execute(
        "SELECT err_count, cooling_until FROM credentials "
        "WHERE cooling_until IS NOT NULL").fetchall()
    assert len(cooled) == 1 and cooled[0]["err_count"] == 0
    db.close()


async def test_role_only_frames_do_not_switch_to_idle_budget(repo):
    """上游只发空 role/空 delta（无正文）时仍按**首事件**窗口计时。

    R93（`ruo40018293/OmniGate` 7e574baf）为同类上游补了独立的「首内容截止」；
    本仓的 role 帧不会变成可发送事件——`StreamTranslator.translate` 对空 content
    不产帧，`emitted` 保持 False，故天然沿用 first_event 预算，不需要额外截止。
    """
    credentials, db = repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="trae", credential_data={"accessToken": "b"})
    provider = _StallProvider(first=[Event(kind=EventKind.CONTENT),
                                      Event(kind=EventKind.CONTENT)],
                              later=GOOD)
    executor = _executor(credentials, provider,
                         first_event_timeout_seconds=STALL,
                         stream_idle_timeout_seconds=0,   # 空闲窗口关闭
                         short_cooldown_wait_seconds=0)

    frames = await _drain(executor)

    assert _content_of(frames) == "ok"           # 空帧之后仍按首事件超时换号
    assert provider.calls == 2
    assert _error_of(frames) is None
    db.close()


async def test_first_event_timeout_without_candidates_is_503(repo):
    """唯一凭证首事件超时且无候选 → 受控 503 帧（文案含超时标识）。"""
    credentials, db = repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    provider = _StallProvider(first=[])
    executor = _executor(credentials, provider,
                         first_event_timeout_seconds=STALL,
                         short_cooldown_wait_seconds=0)

    frames = await _drain(executor)

    error = _error_of(frames)
    assert error is not None
    assert error["code"] == "no_healthy_credential"
    assert "upstream first-event timeout" in error["message"]
    assert provider.calls == 1
    db.close()


async def test_first_event_timeout_cools_account_level(repo):
    """首事件超时按 SOFT 落库：账号级冷却 + 不累计 err_count（不罚死号）。"""
    credentials, db = repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    executor = _executor(credentials, _StallProvider(first=[]),
                         first_event_timeout_seconds=STALL,
                         short_cooldown_wait_seconds=0)
    await _drain(executor)
    row = db.connect().execute(
        "SELECT err_count, cooling_until FROM credentials").fetchone()
    assert row["err_count"] == 0
    assert row["cooling_until"] is not None
    db.close()


# ------------------------------------------- 空闲超时：只收尾不换号（真实短窗口）


async def test_idle_timeout_after_frames_sends_error_and_does_not_rotate(repo):
    """已出帧后上游停住 → 一帧错误收尾，不换号（半截输出不可回滚）。"""
    from src.stats.collector import StatsCollector

    credentials, db = repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="trae", credential_data={"accessToken": "b"})
    collector = StatsCollector(db)
    # 第一个凭证出一帧就停住；第二个凭证本可成功——但绝不能被调用
    provider = _StallProvider(
        first=[Event(kind=EventKind.CONTENT, content="half"),
               Event(kind=EventKind.USAGE, usage=Usage(7, 3))],
        later=GOOD)
    executor = _executor(credentials, provider,
                         stream_idle_timeout_seconds=STALL, stats=collector)

    frames = await _drain(executor)

    assert _content_of(frames) == "half"         # 只有第一个凭证的半截正文
    assert provider.calls == 1                   # 不换号
    error = _error_of(frames)
    assert error is not None and "upstream idle timeout" in error["message"]
    assert error["code"] == "no_healthy_credential"
    # 统计：error_type 是受控值，且带上了已知 usage（半截输出也真实计费）
    row = db.connect().execute(
        "SELECT error_type, input_tokens, output_tokens FROM usage_events").fetchone()
    assert row["error_type"] == "upstream_idle_timeout"
    assert row["input_tokens"] == 7 and row["output_tokens"] == 3
    db.close()


async def test_idle_timeout_records_controlled_error_type(repo):
    """统计白名单：`upstream_idle_timeout` 不被归一成兜底值。"""
    from src.stats.collector import CONTROLLED_ERROR_TYPES, StatsCollector

    credentials, db = repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    executor = _executor(credentials,
                         _StallProvider(first=[Event(kind=EventKind.CONTENT,
                                                     content="x")]),
                         stream_idle_timeout_seconds=STALL,
                         stats=StatsCollector(db))
    await _drain(executor)
    row = db.connect().execute("SELECT error_type FROM usage_events").fetchone()
    assert row["error_type"] in CONTROLLED_ERROR_TYPES
    db.close()


# ----------------------------------------------------------- 关闭（0）保持原行为


async def test_timeouts_disabled_behaves_as_before(repo):
    """两个超时都关（默认 0）：慢流照常完成，不产生错误帧。"""
    credentials, db = repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})

    class Slow:
        id = "trae"

        async def stream_chat(self, _credential_data, _payload, _model):
            await asyncio.sleep(0)
            yield Event(kind=EventKind.CONTENT, content="ok")
            yield Event(kind=EventKind.FINISH, finish_reason="stop")

    frames = await _drain(_executor(credentials, Slow()))
    assert _content_of(frames) == "ok"
    assert _error_of(frames) is None
    db.close()


# --------------------------------------------------- `_TimedReader` 的复用语义


async def test_timed_reader_reuses_task_after_timeout():
    """`_TimedReader`：超时不取消在途任务，下一次 next() 接着等同一个 Task。"""
    from src.engine.executor import _TimedReader

    released = asyncio.Event()

    async def source():
        yield Event(kind=EventKind.CONTENT, content="first")
        await released.wait()
        yield Event(kind=EventKind.CONTENT, content="second")

    reader = _TimedReader(source().__aiter__())
    first = await reader.next(timeout=5)
    assert first.content == "first"
    with pytest.raises(TimeoutError):
        await reader.next(timeout=0.01)
    # 上游后来才吐：同一个 Task 继续存活，没有被取消
    released.set()
    second = await reader.next(timeout=5)
    assert second.content == "second"
    with pytest.raises(StopAsyncIteration):
        await reader.next(timeout=5)
    await reader.aclose()


async def test_timed_reader_aclose_cancels_pending_task():
    """关闭时清掉在途任务，避免 "Task was destroyed but it is pending"。"""
    from src.engine.executor import _TimedReader

    never = asyncio.Event()

    async def source():
        await never.wait()
        yield Event(kind=EventKind.CONTENT, content="x")

    reader = _TimedReader(source().__aiter__())
    with pytest.raises(TimeoutError):
        await reader.next(timeout=0.01)
    await reader.aclose()
    # 已清空：再次 next 会重新起任务（上游已被取消 → StopAsyncIteration）
    with pytest.raises(StopAsyncIteration):
        await reader.next(timeout=1)


async def test_timed_reader_without_timeout_just_awaits():
    """`timeout=None`：不做竞速，直接等上游（关闭超时的路径）。"""
    from src.engine.executor import _TimedReader

    async def source():
        yield Event(kind=EventKind.CONTENT, content="only")

    reader = _TimedReader(source().__aiter__())
    assert (await reader.next(None)).content == "only"
    await reader.aclose()


# --------------------------------------------------------------- 受控标识口径


def test_stream_timeout_kind_and_summary():
    """超时异常的受控标识与冷却口径（SOFT）。"""
    first = _StreamTimeout(60, "first_event")
    assert first.kind().name == "SOFT"
    assert first.error_code == "upstream_first_event_timeout"
    assert first.summary == "upstream first-event timeout"
    idle = _StreamTimeout(300, "idle")
    assert idle.error_code == "upstream_idle_timeout"
    assert idle.summary == "upstream idle timeout"


def test_error_type_for_stream_timeout_is_controlled():
    """`_stream_error_type` 只产出 collector 白名单里的值。"""
    from src.stats.collector import CONTROLLED_ERROR_TYPES

    assert _stream_error_type(_StreamTimeout(1, "first_event")) in CONTROLLED_ERROR_TYPES
    assert _stream_error_type(_StreamTimeout(1, "idle")) in CONTROLLED_ERROR_TYPES
    assert _stream_error_type(None) == "upstream_protocol"

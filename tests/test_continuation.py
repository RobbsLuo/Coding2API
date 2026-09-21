"""B1.4 截断续写：判定、请求体扩展、包装器行为。"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from src.engine.continuation import (
    ContinuationStream,
    continues,
    extend_payload,
)
from src.provider.base import Event, EventKind, Usage


def test_continues_only_on_length():
    assert continues("length") is True
    assert continues("stop") is False
    assert continues(None) is False
    assert continues("tool_calls") is False
    assert continues("LENGTH") is False          # 精确匹配，不做大小写模糊


def test_extend_payload_appends_assistant_and_instruction():
    payload = {"model": "m", "messages": [{"role": "user", "content": "hi"}],
               "max_tokens": 8, "max_completion_tokens": 4, "temperature": 0.3}
    body = extend_payload(payload, "part1", "think1")
    assert body["messages"][0] == {"role": "user", "content": "hi"}
    assert body["messages"][1] == {"role": "assistant", "content": "part1",
                                   "reasoning_content": "think1"}
    assert body["messages"][2]["role"] == "user"
    assert "Continue exactly where you left off" in body["messages"][2]["content"]
    # 输出上限两键都移除：否则本轮会在同一处再次截断（死循环）
    assert "max_tokens" not in body
    assert "max_completion_tokens" not in body
    assert body["temperature"] == 0.3
    assert payload["messages"] == [{"role": "user", "content": "hi"}]  # 原体不动


def test_extend_payload_without_reasoning_and_non_list_messages():
    body = extend_payload({"messages": "nope"}, "text", "")
    assert body["messages"] == "nope"                    # 非 list 不动
    body2 = extend_payload({"messages": [{"role": "user", "content": "u"}]}, "", "")
    assert body2["messages"][1]["content"] == ""         # 空正文也追加
    assert "reasoning_content" not in body2["messages"][1]


async def _agen(events: list[Event]) -> AsyncIterator[Event]:
    for event in events:
        yield event


class _FakeProvider:
    """按调用次序返回预设事件流；记录每次收到的 payload。"""

    def __init__(self, rounds: list[list[Event]]) -> None:
        self._rounds = list(rounds)
        self.payloads: list[dict] = []

    def stream_chat(self, credential_data, payload, model) -> AsyncIterator[Event]:
        self.payloads.append(dict(payload))
        events = self._rounds.pop(0) if self._rounds else [Event(kind=EventKind.FINISH,
                                                                 finish_reason="stop")]
        return _agen(events)


async def _collect(stream: ContinuationStream) -> list[Event]:
    return [event async for event in stream]


@pytest.mark.asyncio
async def test_no_continuation_when_finish_reason_stop():
    provider = _FakeProvider([[Event(kind=EventKind.CONTENT, content="done"),
                               Event(kind=EventKind.USAGE,
                                     usage=Usage(input_tokens=1, output_tokens=2)),
                               Event(kind=EventKind.FINISH, finish_reason="stop")]])
    stream = ContinuationStream(provider, {}, {"messages": []}, "m", max_continues=10)
    events = await _collect(stream)
    assert [e.kind for e in events] == [EventKind.CONTENT, EventKind.USAGE, EventKind.FINISH]
    assert events[-1].finish_reason == "stop"
    assert stream.continues_done == 0
    assert len(provider.payloads) == 1


@pytest.mark.asyncio
async def test_continues_on_length_and_accumulates_usage():
    provider = _FakeProvider([
        [Event(kind=EventKind.CONTENT, content="part1"),
         Event(kind=EventKind.USAGE, usage=Usage(input_tokens=10, output_tokens=5,
                                                 reasoning_tokens=2)),
         Event(kind=EventKind.FINISH, finish_reason="length")],
        [Event(kind=EventKind.CONTENT, content="part2"),
         Event(kind=EventKind.USAGE, usage=Usage(input_tokens=3, output_tokens=4,
                                                 reasoning_tokens=1)),
         Event(kind=EventKind.FINISH, finish_reason="length")],
        [Event(kind=EventKind.CONTENT, content="part3"),
         Event(kind=EventKind.USAGE, usage=Usage(input_tokens=1, output_tokens=2)),
         Event(kind=EventKind.FINISH, finish_reason="stop")],
    ])
    stream = ContinuationStream(provider, {}, {"messages": [], "max_tokens": 8}, "m",
                                max_continues=10)
    events = await _collect(stream)
    contents = [e.content for e in events if e.kind is EventKind.CONTENT]
    assert contents == ["part1", "part2", "part3"]
    assert stream.continues_done == 2
    # 末端补发一条累计 usage + 最后一轮真实 finish_reason
    final_usage = [e for e in events if e.kind is EventKind.USAGE][-1]
    assert final_usage.usage.input_tokens == 14
    assert final_usage.usage.output_tokens == 11
    assert final_usage.usage.reasoning_tokens == 3
    assert [e for e in events if e.kind is EventKind.FINISH][-1].finish_reason == "stop"
    # 第二轮请求体已带上第一轮正文
    assert provider.payloads[1]["messages"][-2]["content"] == "part1"
    assert "max_tokens" not in provider.payloads[1]


@pytest.mark.asyncio
async def test_continuation_stops_at_limit():
    rounds = [
        [Event(kind=EventKind.CONTENT, content=f"p{i}"),
         Event(kind=EventKind.FINISH, finish_reason="length")]
        for i in range(10)
    ]
    provider = _FakeProvider(rounds)
    stream = ContinuationStream(provider, {}, {"messages": []}, "m", max_continues=2)
    events = await _collect(stream)
    assert stream.continues_done == 2
    assert len(provider.payloads) == 3                       # 首轮 + 2 次续写
    # 达到上限时如实上报 length（客户端据此知道被截断）
    assert [e for e in events if e.kind is EventKind.FINISH][-1].finish_reason == "length"


@pytest.mark.asyncio
async def test_zero_limit_never_continues():
    provider = _FakeProvider([[Event(kind=EventKind.FINISH, finish_reason="length")]])
    stream = ContinuationStream(provider, {}, {"messages": []}, "m", max_continues=0)
    events = await _collect(stream)
    assert stream.continues_done == 0
    assert [e for e in events if e.kind is EventKind.FINISH][-1].finish_reason == "length"


@pytest.mark.asyncio
async def test_reasoning_only_round_is_forwarded_and_continued():
    """仅 reasoning 的轮次：正文照样原样下发 + 续写（reasoning 参与上下文）。"""
    provider = _FakeProvider([
        [Event(kind=EventKind.REASONING, content="thinking"),
         Event(kind=EventKind.FINISH, finish_reason="length")],
        [Event(kind=EventKind.CONTENT, content="answer"),
         Event(kind=EventKind.FINISH, finish_reason="stop")],
    ])
    stream = ContinuationStream(provider, {}, {"messages": []}, "m", max_continues=5)
    events = await _collect(stream)
    assert [e.kind for e in events if e.kind is not EventKind.FINISH] == [
        EventKind.REASONING, EventKind.CONTENT, EventKind.USAGE]
    assert provider.payloads[1]["messages"][-2]["reasoning_content"] == "thinking"


@pytest.mark.asyncio
async def test_tool_calls_and_empty_reasoning_passthrough():
    """工具调用事件原样下发；content 为空的 reasoning 事件不参与拼接。"""
    provider = _FakeProvider([
        [Event(kind=EventKind.TOOL_CALLS, tool_calls=[{"index": 0, "id": "c",
                                                       "function": {"name": "f"}}]),
         Event(kind=EventKind.REASONING, content=None),
         Event(kind=EventKind.FINISH, finish_reason="stop")],
    ])
    stream = ContinuationStream(provider, {}, {"messages": []}, "m", max_continues=5)
    events = await _collect(stream)
    assert [e.kind for e in events] == [EventKind.TOOL_CALLS, EventKind.REASONING,
                                        EventKind.USAGE, EventKind.FINISH]
    assert events[0].tool_calls[0]["function"]["name"] == "f"
    assert stream.continues_done == 0


@pytest.mark.asyncio
async def test_usage_accumulation_with_missing_fields():
    from src.engine.continuation import _add_usage, _sum

    assert _sum(None, 5) == 5
    assert _sum(5, None) == 5
    assert _sum(None, None) is None
    assert _sum(2, 3) == 5
    base = Usage(input_tokens=1, output_tokens=None)
    assert _add_usage(base, None) is base
    merged = _add_usage(base, Usage(input_tokens=4, output_tokens=6, credit=0.5))
    assert merged.input_tokens == 5
    assert merged.output_tokens == 6
    assert merged.credit == 0.5
    assert merged.reasoning_tokens is None

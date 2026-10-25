"""Zen SSE 帧解析、事件聚合与错误分类。"""

from __future__ import annotations

import pytest

from src.engine.sse import SSEFrame
from src.provider.base import ErrKind, EventKind
from src.provider.zen import events as zen_events
from tests.helpers.http import chunk_payload as chunk
from tests.helpers.http import sse_frame as frame

pytestmark = pytest.mark.real_upstream("zen")


def test_parse_frame_returns_none_for_done_and_empty():
    assert zen_events.parse_frame(SSEFrame(event="", data="")) is None
    assert zen_events.parse_frame(SSEFrame(event="", data=" [DONE] ")) is None

def test_parse_frame_content_and_reasoning():
    content = zen_events.parse_frame(frame(chunk({"content": "hi"})))
    assert content is not None and content.kind is EventKind.CONTENT
    reasoning = zen_events.parse_frame(frame(chunk({"reasoning_content": "think"})))
    assert reasoning is not None and reasoning.kind is EventKind.REASONING
    assert reasoning.content == "think"

def test_parse_frame_tool_calls_drops_blank_noise():
    payload = chunk({"tool_calls": [
        {"index": 0, "function": {"name": "", "arguments": ""}},   # 噪声
        {"index": 1, "function": {"name": "", "arguments": "{}"}},  # "{}" 空壳参数也算噪声
        {"index": 2, "function": {"name": "grep", "arguments": ""}},
    ]})
    event = zen_events.parse_frame(frame(payload))
    assert event is not None and event.kind is EventKind.TOOL_CALLS
    assert [c["index"] for c in event.tool_calls] == [2]

def test_parse_frame_tool_calls_all_blank_falls_through():
    """全部是空名噪声：整批丢弃，不产出 tool_calls 事件。"""
    payload = chunk({"tool_calls": [
        {"index": 0, "function": {"name": "  ", "arguments": ""}},
        {"index": 1, "function": {"name": "", "arguments": "{}"}},  # "{}" 空壳参数也算噪声
        {"index": 2, "function": {"arguments": {}}},
    ], "content": "text"})
    event = zen_events.parse_frame(frame(payload))
    assert event is not None and event.kind is EventKind.CONTENT

def test_parse_frame_usage_and_finish():
    usage_event = zen_events.parse_frame(frame(chunk(
        {}, choices=[], usage={"prompt_tokens": 3})))
    assert usage_event is not None and usage_event.kind is EventKind.USAGE
    finish = zen_events.parse_frame(frame(chunk({}, finish="stop")))
    assert finish is not None and finish.kind is EventKind.FINISH

def test_parse_frame_no_choices_returns_none():
    assert zen_events.parse_frame(frame({"choices": []})) is None
    assert zen_events.parse_frame(frame({"usage": None})) is None

def test_parse_frame_bad_payload_raises():
    with pytest.raises(zen_events.UpstreamProtocolViolation):
        zen_events.parse_frame(SSEFrame(event="", data="{not json"))
    with pytest.raises(zen_events.UpstreamProtocolViolation):
        zen_events.parse_frame(SSEFrame(event="", data="[1, 2]"))
    with pytest.raises(zen_events.UpstreamProtocolViolation):
        zen_events.parse_frame(frame({"choices": "nope"}))
    with pytest.raises(zen_events.UpstreamProtocolViolation):
        zen_events.parse_frame(frame({"choices": ["nope"]}))

def test_parse_frame_error_envelope_int_code():
    event = zen_events.parse_frame(frame({"error": {"code": 429, "message": "slow"}}))
    assert event is not None and event.kind is EventKind.ERROR
    assert event.error_code == 429 and event.error_message == "slow"
    assert event.error_kind is ErrKind.SOFT

def test_parse_frame_error_envelope_string_code_and_missing_message():
    event = zen_events.parse_frame(frame({"error": {"code": "bad", "message": None}}))
    assert event is not None and event.error_kind is ErrKind.OTHER
    assert event.error_code is None and event.error_message == ""

def test_parse_all_events_splits_content_and_finish():
    """最后一段正文与 finish_reason 同帧：两者都要产出。"""
    payload = chunk({"content": "!"}, finish="stop")
    kinds = [e.kind for e in zen_events.parse_all_events(frame(payload))]
    assert kinds == [EventKind.CONTENT, EventKind.FINISH]

def test_parse_all_events_appends_usage_to_finish_chunk():
    payload = chunk({"content": "!"}, finish="stop",
                    usage={"prompt_tokens": 1, "completion_tokens": 2})
    events = zen_events.parse_all_events(frame(payload))
    assert [e.kind for e in events] == [
        EventKind.CONTENT, EventKind.USAGE, EventKind.FINISH]

def test_parse_all_events_usage_only_chunk():
    events = zen_events.parse_all_events(frame(chunk(
        {}, choices=[],
        usage={"prompt_tokens": 10, "completion_tokens": 5,
               "prompt_tokens_details": {"cached_tokens": 2}})))
    assert [e.kind for e in events] == [EventKind.USAGE]
    assert events[0].usage is not None
    assert events[0].usage.input_tokens == 10
    assert events[0].usage.cached_tokens == 2

def test_parse_all_events_empty_and_done():
    assert zen_events.parse_all_events(SSEFrame(event="", data="")) == []
    assert zen_events.parse_all_events(SSEFrame(event="", data="[DONE]")) == []

def test_usage_tolerates_bad_types():
    """usage 字段类型不对（bool / 字符串 / 非 dict details）不能崩。"""
    payload = {"choices": [], "usage": {
        "prompt_tokens": True, "completion_tokens": "x",
        "cached_tokens": True, "prompt_tokens_details": "nope",
        "credit": True}}
    event = zen_events.parse_frame(frame(payload))
    assert event is not None and event.usage is not None
    assert event.usage.input_tokens is None
    assert event.usage.output_tokens is None
    assert event.usage.cached_tokens is None
    assert event.usage.credit is None

def test_usage_parses_credit_when_present():
    """Zen 是上游真给 credit 的渠道之一：数值必须解析出来（不是默认值吞掉）。"""
    event = zen_events.parse_frame(frame(
        {"choices": [], "usage": {"prompt_tokens": 5, "credit": 0.25}}))
    assert event is not None and event.usage is not None
    assert event.usage.credit == 0.25

def test_classify_status_maps_each_branch():
    # 401 只代表「该模型需要付费 key」，不是虚拟凭证失效；归 DEAD 会因一次
    # 强制 @zen 的付费模型请求把整条渠道硬禁用。
    assert zen_events.classify_status(401) is ErrKind.INVALID
    assert zen_events.classify_status(429) is ErrKind.SOFT
    assert zen_events.classify_status(400) is ErrKind.INVALID
    assert zen_events.classify_status(404) is ErrKind.INVALID
    assert zen_events.classify_status(422) is ErrKind.INVALID
    assert zen_events.classify_status(403) is ErrKind.REQUEST
    assert zen_events.classify_status(500) is ErrKind.OTHER
    assert zen_events.classify_status(426) is ErrKind.OTHER

def test_classify_error_code_maps_each_branch():
    assert zen_events.classify_error_code(401) is ErrKind.INVALID
    assert zen_events.classify_error_code(429) is ErrKind.SOFT
    assert zen_events.classify_error_code(400) is ErrKind.INVALID
    assert zen_events.classify_error_code(403) is ErrKind.REQUEST
    assert zen_events.classify_error_code(999) is ErrKind.OTHER
    assert zen_events.classify_error_code(None) is ErrKind.OTHER

"""TRAE 事件映射与错误分类。"""

from __future__ import annotations

import json

import pytest

from src.compat.openai.response import aggregate
from src.provider.base import ErrKind, EventKind, Usage
from src.provider.trae import events as trae_events
from tests.helpers.sse import parse_frames
from tests.provider.trae._support import fixture


def test_real_fixture_maps_to_neutral_events():
    events = [e for e in (trae_events.parse_frame(f)
                          for f in parse_frames(fixture("chat-basic.sse"))) if e]
    kinds = [e.kind for e in events]
    assert kinds == [EventKind.CONTENT, EventKind.CONTENT, EventKind.USAGE, EventKind.FINISH]
    assert events[0].content == "中国"
    assert events[0].kind is EventKind.CONTENT          # parse_frame 单事件：content 优先
    assert events[2].usage == Usage(21, 142, 135)
    assert events[3].finish_reason == "stop"

def test_reasoning_only_output_maps_to_reasoning_event():
    frame = trae_events.SSEFrame(event="output",
                                 data='{"response":"","reasoning_content":"想想","tool_calls":null}')
    event = trae_events.parse_frame(frame)
    assert event.kind is EventKind.REASONING and event.content == "想想"

def test_tool_calls_fixture_maps_to_tool_call_event():
    events = [e for e in (trae_events.parse_frame(f)
                          for f in parse_frames(fixture("tool-calls.sse"))) if e]
    assert events[0].kind is EventKind.TOOL_CALLS
    assert events[0].tool_calls[0]["function"]["name"] == "get_weather"
    assert events[1].finish_reason == "tool_calls"

def test_empty_output_frame_yields_nothing():
    frame = trae_events.SSEFrame(event="output",
                                 data='{"response":"","reasoning_content":"","tool_calls":null}')
    assert trae_events.parse_frame(frame) is None

def test_metadata_and_heartbeat_frames_yield_nothing():
    for frame in parse_frames(fixture("chat-basic.sse")):
        if frame.event in ("metadata", "timing_cost", "extra_info"):
            assert trae_events.parse_frame(frame) is None

def test_frames_without_data_yield_nothing():
    assert trae_events.parse_frame(trae_events.SSEFrame(event="output", data="")) is None

@pytest.mark.parametrize("frame", [
    trae_events.SSEFrame(event="output", data="{broken"),
    trae_events.SSEFrame(event="output", data="[1,2]"),
    trae_events.SSEFrame(event="token_usage", data='"text"'),
])
def test_malformed_frames_raise_not_silently(frame):
    with pytest.raises(trae_events.UpstreamProtocolViolation):
        trae_events.parse_frame(frame)

def test_error_fixtures_classify_plan_and_other():
    plan = trae_events.parse_frame(parse_frames(fixture("error-1005.sse"))[0])
    other = trae_events.parse_frame(parse_frames(fixture("error-param.sse"))[0])
    assert plan.error_code == 1005 and trae_events.classify_error_code(1005) is ErrKind.PLAN
    # 4001 = 参数/模型不可用：流内与 HTTP 两条路径都必须判 INVALID（跳过该上游）
    assert other.error_code == 4001 and trae_events.classify_error_code(4001) is ErrKind.INVALID
    assert trae_events.classify_error_code(None) is ErrKind.OTHER

def test_error_frame_4008_quota_exhausted_is_credit():
    """4008 = "Your requests have exceeded the quota."：按配额耗尽处理（等签到恢复）。"""
    frame = trae_events.SSEFrame(
        event="error",
        data='{"code":4008,"message":"Your requests have exceeded the quota."}')
    event = trae_events.parse_frame(frame)
    assert event.error_code == 4008
    assert event.error_kind is ErrKind.CREDIT

@pytest.mark.parametrize(("status", "expected"), [
    (401, ErrKind.DEAD), (404, ErrKind.SOFT), (429, ErrKind.SOFT),
    (500, ErrKind.OTHER), (400, ErrKind.INVALID), (402, ErrKind.CREDIT),
])
def test_classify_status(status, expected):
    assert trae_events.classify_status(status) is expected

def test_classify_status_detects_1005_in_body():
    assert trae_events.classify_status(400, b'{"code": 1005}') is ErrKind.PLAN

@pytest.mark.parametrize(("status", "body", "expected"), [
    (402, b"", ErrKind.CREDIT),
    (400, b'{"code": 14018}', ErrKind.CREDIT),
    (400, b'{"code": 11102}', ErrKind.BLOCKED),
    (404, b'{"code": 11102}', ErrKind.BLOCKED),
    (429, b'{"code": 6004}', ErrKind.MODEL),
    (429, b'{"code": 500}', ErrKind.SOFT),
    (500, b'{"code": 14018}', ErrKind.CREDIT),      # 业务码优先于状态码
    (403, b'{"code": 4008, "message": "Your requests have exceeded the quota."}',
     ErrKind.CREDIT),                                # 配额耗尽：HTTP 形态同样判 CREDIT
])
def test_classify_status_business_codes(status, body, expected):
    assert trae_events.classify_status(status, body) is expected

@pytest.mark.parametrize(("code", "expected"), [
    (1005, ErrKind.PLAN), (14018, ErrKind.CREDIT), (4008, ErrKind.CREDIT),
    (6004, ErrKind.MODEL), (4001, ErrKind.INVALID),
    (None, ErrKind.OTHER), (9999, ErrKind.OTHER),      # 未知码仍走 OTHER 兜底
])
def test_classify_error_code_mapping(code, expected):
    assert trae_events.classify_error_code(code) is expected

def test_parse_all_events_non_dict_tool_call_entry_skipped():
    """非对象 tool_call 条目（88）与全空结果（158-160）覆盖。"""
    frame = trae_events.SSEFrame(
        event="output",
        data='{"tool_calls":["junk",{"function_call":{"name":"f","arguments":"{}"}}]}')
    events = trae_events.parse_all_events(frame)
    tools = [e for e in events if e.kind is EventKind.TOOL_CALLS]
    assert len(tools) == 1 and tools[0].tool_calls[0]["function"]["name"] == "f"

    empty = trae_events.SSEFrame(
        event="output",
        data='{"tool_calls":["junk"],"response":"x"}')
    kinds = [e.kind for e in trae_events.parse_all_events(empty)]
    assert EventKind.TOOL_CALLS not in kinds

def test_tool_call_argument_fragments_are_preserved_and_merged():
    """分片流：首片带 name，续片只有 arguments 增量——续片必须保留。

    回归：此前无 name 的条目被一律丢弃，客户端聚合出截断的 JSON 参数，
    工具执行报错后原样重试同一轮（死循环）。
    """
    frames = [
        trae_events.SSEFrame(event="output", data=(
            '{"tool_calls":[{"id":"call_a","type":"function","index":0,'
            '"function_call":{"name":"get_weather","arguments":"{\\"city\\":"}}]}')),
        trae_events.SSEFrame(event="output", data=(
            '{"tool_calls":[{"index":0,"function_call":{"arguments":"\\"北京\\"}"}}]}')),
    ]
    events = [e for f in frames for e in trae_events.parse_all_events(f)]
    assert [e.kind for e in events] == [EventKind.TOOL_CALLS, EventKind.TOOL_CALLS]
    # 续片无 name 但保留 arguments，归一为 OpenAI function 形状
    assert events[1].tool_calls == [{"index": 0, "function": {"arguments": '"北京"}'}}]
    merged = aggregate(events, "m")["choices"][0]["message"]["tool_calls"]
    assert merged[0]["function"] == {
        "name": "get_weather", "arguments": '{"city":"北京"}'}

@pytest.mark.parametrize("arguments", [None, "", "{}", {}])
def test_blank_nameless_tool_call_is_dropped(arguments):
    """无 name 且 arguments 为空（None/空串/空 JSON 串/空对象）的噪声整条丢弃。"""
    payload = json.dumps({"tool_calls": [
        {"id": "n", "function_call": {"name": "", "arguments": arguments}}],
        "response": "x"})
    events = trae_events.parse_all_events(
        trae_events.SSEFrame(event="output", data=payload))
    assert EventKind.TOOL_CALLS not in [e.kind for e in events]


def test_output_frame_attaches_upstream_reported_model():
    """TRAE 帧里带 model 时挂到事件上（观测用）。"""
    frame = trae_events.SSEFrame(
        event="output",
        data='{"model":"trae-model","response":"hi","reasoning_content":"",'
             '"tool_calls":null}')
    assert trae_events.parse_frame(frame).model == "trae-model"
    plain = trae_events.SSEFrame(event="output", data='{"response":"hi"}')
    assert trae_events.parse_frame(plain).model is None

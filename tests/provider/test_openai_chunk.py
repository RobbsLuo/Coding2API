"""共享 OpenAI chunk 解析原语：usage 合并后的并集语义。

7 个渠道原先各写一份 `_usage`，抽到 `usage_from_openai` 后必须保证
「details 优先、顶层兜底」的并集行为不丢——尤其顶层 `reasoning_tokens`
（TRAE / Zen / Kilo / Qoder 实测会给）不能因 details 缺失而归 None。
"""

from __future__ import annotations

import json

import pytest

from src.provider.base import Event, EventKind, UpstreamProtocolViolation
from src.provider.openai_chunk import (
    details_int,
    openai_event,
    openai_events,
    payload_of,
    usage_from_openai,
)


def test_reasoning_tokens_falls_back_to_top_level():
    """details 缺失时顶层 reasoning_tokens 必须保留（TRAE 等渠道的真实形状）。"""
    usage = usage_from_openai({"prompt_tokens": 1, "reasoning_tokens": 148})
    assert usage.reasoning_tokens == 148


def test_details_win_over_top_level():
    """details 与顶层同时存在时以 details 为准（CB / OfficeAce / CodeArts 实测）。"""
    usage = usage_from_openai({
        "prompt_tokens": 1,
        "reasoning_tokens": 9,
        "completion_tokens_details": {"reasoning_tokens": 100},
    })
    assert usage.reasoning_tokens == 100


@pytest.mark.parametrize("bad", [True, "5", None, 1.5])
def test_details_rejects_non_int(bad):
    """details 里的非法值（bool / 字符串 / 浮点）回落顶层，不当成 0。"""
    usage = usage_from_openai({
        "completion_tokens_details": {"reasoning_tokens": bad},
        "reasoning_tokens": 7,
    })
    assert usage.reasoning_tokens == 7


def test_credit_only_when_channel_reports_it():
    """`credit=True` 的渠道才解析 credit；其余渠道忽略该字段。"""
    assert usage_from_openai({"credit": 1.5}, credit=True).credit == 1.5
    assert usage_from_openai({"credit": 1.5}).credit is None
    # bool 不是有效 credit（True 会被当成 1.0）
    assert usage_from_openai({"credit": True}, credit=True).credit is None


def test_cached_fallbacks_tried_in_order():
    """details → 顶层 → 渠道私有兜底键，逐级回落；0 是有效值不算缺失。"""
    assert usage_from_openai(
        {"cache_read_input_tokens": 7},
        cached_fallbacks=("cache_read_input_tokens",)).cached_tokens == 7
    # 顶层 cached_tokens 优先于私有兜底键
    assert usage_from_openai(
        {"cached_tokens": 2, "cache_read_input_tokens": 5},
        cached_fallbacks=("cache_read_input_tokens",)).cached_tokens == 2
    # 0 必须保留（TRAE 未命中报 0）
    assert usage_from_openai(
        {"cache_read_input_tokens": 0},
        cached_fallbacks=("cache_read_input_tokens",)).cached_tokens == 0
    assert usage_from_openai({}).cached_tokens is None


def test_details_int_handles_non_dict_details():
    """details 不是对象时直接回落顶层，不崩。"""
    assert details_int({"prompt_tokens_details": "nope", "cached_tokens": 3},
                       "prompt_tokens_details", "cached_tokens") == 3
    assert details_int({"prompt_tokens_details": None, "cached_tokens": 3},
                       "prompt_tokens_details", "cached_tokens") == 3


# ---------------------------------------------------------------- 帧解析


def _frame(data: str):
    from src.engine.sse import SSEFrame

    return SSEFrame(event="message", data=data)


def test_payload_of_skips_empty_and_done():
    assert payload_of(_frame(""), "X") is None
    assert payload_of(_frame("[DONE]"), "X") is None
    assert payload_of(_frame('{"a":1}'), "X") == {"a": 1}


def test_payload_of_rejects_bad_json_and_non_object():
    """坏帧必须显式失败，不能静默返回空回复；报错文案带渠道名便于定位。"""
    with pytest.raises(UpstreamProtocolViolation, match="unparsable X SSE data"):
        payload_of(_frame("{bad"), "X")
    with pytest.raises(UpstreamProtocolViolation, match="X SSE data is not an object"):
        payload_of(_frame("[1,2]"), "X")


def test_openai_event_priority_tool_calls_over_content():
    """tool_calls > 正文：同一 delta 两者都有时只产出一个代表性事件。"""
    event = openai_event({"choices": [{"delta": {
        "tool_calls": [{"function": {"name": "f", "arguments": ""}}],
        "content": "文本"}}]})
    assert event is not None and event.kind is EventKind.TOOL_CALLS


def test_openai_event_drops_blank_tool_calls_and_falls_through():
    """空名空参的噪声 tool_call 整条丢弃，继续看正文。"""
    event = openai_event({"choices": [{"delta": {
        "tool_calls": [{"function": {"name": "", "arguments": ""}}],
        "content": "正文"}}]})
    assert event is not None and event.kind is EventKind.CONTENT


@pytest.mark.parametrize("key", ["reasoning_content", "reasoning"])
def test_openai_event_reasoning_keys_are_parameterised(key):
    """不猜键：只认调用方点名的 reasoning 字段。"""
    payload = {"choices": [{"delta": {key: "思考"}}]}
    assert openai_event(payload, reasoning_keys=(key,)).kind is EventKind.REASONING
    # 点错键就读不到（默认只认 reasoning_content，Kilo 必须显式传 reasoning）
    assert openai_event(payload, reasoning_keys=("other",)) is None


def test_openai_event_error_as_string_only_when_asked():
    """OfficeAce 实测有 `{"error": "文本"}`；其他渠道不该认这个形态。"""

    def classify(error: dict) -> Event:
        return Event(kind=EventKind.ERROR, error_message=str(error.get("message")))

    payload = {"error": "炸了"}
    assert openai_event(payload, error_event=classify,
                        error_as_string=True).kind is EventKind.ERROR
    assert openai_event(payload, error_event=classify) is None


def test_openai_events_appends_usage_and_finish_on_merged_frame():
    """合并帧（正文 + usage + finish）三者都要出，否则客户端收不到结束信号。"""
    events = openai_events(_frame(json.dumps({
        "choices": [{"delta": {"content": "尾"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3}})), "X")
    kinds = [e.kind for e in events]
    assert kinds == [EventKind.CONTENT, EventKind.USAGE, EventKind.FINISH]


def test_openai_events_does_not_duplicate_usage_or_finish():
    """代表性事件已经是 usage / finish 时不再补一条。"""
    assert [e.kind for e in openai_events(_frame(json.dumps({
        "choices": [], "usage": {"prompt_tokens": 1}})), "X")] == [EventKind.USAGE]
    assert [e.kind for e in openai_events(_frame(json.dumps({
        "choices": [{"delta": {}, "finish_reason": "stop"}]})), "X")] == [EventKind.FINISH]


def test_openai_events_empty_frame_returns_no_events():
    assert openai_events(_frame("[DONE]"), "X") == []


def test_openai_event_attaches_upstream_reported_model():
    """上游自报模型挂到事件上（executor 用它做静默替换观测）。"""
    event = openai_event({"model": "glm-4.6",
                          "choices": [{"delta": {"content": "x"}}]})
    assert event.model == "glm-4.6"
    # 没报模型 / 空串：保持 None
    assert openai_event({"choices": [{"delta": {"content": "x"}}]}).model is None
    assert openai_event({"model": "",
                         "choices": [{"delta": {"content": "x"}}]}).model is None


def test_openai_events_attaches_model_to_appended_usage_and_finish():
    frame = _frame(json.dumps({
        "model": "m-1",
        "choices": [{"delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2},
    }))
    events = openai_events(frame, "Test")
    assert events, "finish/usage 帧必须产出事件"
    assert all(event.model == "m-1" for event in events)

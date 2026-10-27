"""CodeBuddy SSE 事件映射：正文 / 思考 / 工具调用 / usage / finish 与畸形帧。"""

from __future__ import annotations

import json

import pytest

from src.provider.base import ErrKind, EventKind
from src.provider.codebuddy import events as cb_events
from src.provider.codebuddy.client import CodeBuddyProvider
from src.provider.codebuddy.events import UpstreamProtocolViolation
from src.provider.codebuddy.intl import CodeBuddyIntlProvider
from tests.helpers.paths import provider_fixture
from tests.helpers.sse import parse_frames


def fixture(name: str) -> str:
    return provider_fixture("codebuddy", name)

def test_basic_fixture_maps_to_events():
    events = [e for e in (cb_events.parse_frame(f)
                          for f in parse_frames(fixture("chat-basic.sse"))) if e]
    assert [e.kind for e in events] == [EventKind.CONTENT, EventKind.CONTENT, EventKind.USAGE]
    assert events[0].content == "你好"
    assert events[1].content == "，世界"
    assert events[2].usage.input_tokens == 11

def test_done_marker_and_empty_frame_yield_nothing():
    assert cb_events.parse_frame(cb_events.SSEFrame(event="", data="[DONE]")) is None
    assert cb_events.parse_frame(cb_events.SSEFrame(event="", data="")) is None

def test_parse_all_events_emits_usage_and_finish_together():
    """收尾帧同时带 finish_reason 与 usage → 两个事件都产出。"""
    frame = cb_events.SSEFrame(
        event="",
        data='{"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3}}')
    kinds = [e.kind for e in cb_events.parse_all_events(frame)]
    assert kinds == [EventKind.USAGE, EventKind.FINISH]

def test_parse_all_events_keeps_reasoning_then_content_priority():
    """reasoning 与 content 同帧时按 content 优先（单事件语义）。"""
    frame = cb_events.SSEFrame(
        event="", data='{"choices":[{"delta":{"reasoning_content":"r","content":"c"}}]}')
    events = cb_events.parse_all_events(frame)
    assert [e.kind for e in events] == [EventKind.CONTENT]

def test_reasoning_fixture_events():
    events = [e for e in (cb_events.parse_frame(f)
                          for f in parse_frames(fixture("chat-reasoning.sse"))) if e]
    assert [e.kind for e in events] == [EventKind.REASONING, EventKind.REASONING,
                                        EventKind.CONTENT]
    assert events[0].content == "我"

def test_tool_calls_fixture_keeps_upstream_ids():
    events = [e for e in (cb_events.parse_frame(f)
                          for f in parse_frames(fixture("tool-calls.sse"))) if e]
    assert all(e.kind is EventKind.TOOL_CALLS for e in events)
    assert events[0].tool_calls[0]["id"] == "call_1"
    assert "id" not in events[1].tool_calls[0]          # 分片不重生成 id

def test_tool_call_list_with_non_dict_entries_is_filtered():
    frame = cb_events.SSEFrame(
        event="", data='{"choices":[{"delta":{"tool_calls":[1,{"id":"ok",'
                       '"function":{"name":"f","arguments":"{}"}}]}}]}')
    assert cb_events.parse_frame(frame).tool_calls == [
        {"id": "ok", "function": {"name": "f", "arguments": "{}"}}]

def test_tool_call_without_name_is_dropped():
    """空名 tool_call（上游噪声）被丢弃，不转发给客户端。"""
    frame = cb_events.SSEFrame(
        event="", data='{"choices":[{"delta":{"tool_calls":'
                       '[{"id":"x","function":{"arguments":"{}"}}]}}]}')
    assert cb_events.parse_frame(frame) is None

@pytest.mark.parametrize("data", ["{broken", "[1,2]", '{"choices":"no"}',
                                  '{"choices":[5]}'])
def test_malformed_frames_raise(data):
    with pytest.raises(UpstreamProtocolViolation):
        cb_events.parse_frame(cb_events.SSEFrame(event="", data=data))

def test_choices_empty_array_is_tolerated():
    frame = cb_events.SSEFrame(event="", data='{"choices":[],"usage":{"prompt_tokens":3}}')
    assert cb_events.parse_frame(frame).usage.input_tokens == 3

def test_finish_only_frame():
    frame = cb_events.SSEFrame(
        event="", data='{"choices":[{"delta":{},"finish_reason":"length"}]}')
    assert cb_events.parse_frame(frame).finish_reason == "length"

def test_parse_credit_rate_malformed_returns_none():
    """credit_rate 解析：非数值/无数字等坏格式走异常路径返回 None。"""
    from src.provider.codebuddy.client import _parse_credit_rate

    assert _parse_credit_rate("x") is None            # IndexError
    assert _parse_credit_rate("xabc") is None         # ValueError
    assert _parse_credit_rate("x0.25 credits") == 0.25

def test_usage_credit_is_optional():
    frame = cb_events.SSEFrame(event="", data='{"usage":{"credit":0.25,"prompt_tokens":1}}')
    usage = cb_events.parse_frame(frame).usage
    assert usage.credit == 0.25

def test_usage_cached_tokens_from_details_and_top_level():
    """缓存命中：优先 prompt_tokens_details.cached_tokens，顶层 cached_tokens 兜底。"""
    frame = cb_events.SSEFrame(event="", data=(
        '{"usage":{"prompt_tokens":10,"prompt_tokens_details":{"cached_tokens":7}}}'))
    usage = cb_events.parse_frame(frame).usage
    assert usage.cached_tokens == 7

    frame = cb_events.SSEFrame(event="", data='{"usage":{"prompt_tokens":10,"cached_tokens":4}}')
    assert cb_events.parse_frame(frame).usage.cached_tokens == 4

    frame = cb_events.SSEFrame(event="", data='{"usage":{"prompt_tokens":10}}')
    assert cb_events.parse_frame(frame).usage.cached_tokens is None

def test_usage_ignores_boolean_and_non_numeric_values():
    frame = cb_events.SSEFrame(
        event="", data='{"usage":{"prompt_tokens":true,"credit":true,"completion_tokens":"x"}}')
    usage = cb_events.parse_frame(frame).usage
    assert usage.input_tokens is None and usage.output_tokens is None and usage.credit is None

def test_usage_reasoning_tokens_from_details_and_top_level():
    """思考 token：优先 completion_tokens_details，顶层 reasoning_tokens 兜底。

    顶层恒缺是 CB 的既有实测事实，真值只在 details 里——早先只读顶层导致
    统计页 CB 思考 token 恒 0（被误判为上游口径差异）。
    """
    frame = cb_events.SSEFrame(event="", data=(
        '{"usage":{"completion_tokens":20,'
        '"completion_tokens_details":{"reasoning_tokens":13}}}'))
    assert cb_events.parse_frame(frame).usage.reasoning_tokens == 13

    frame = cb_events.SSEFrame(
        event="", data='{"usage":{"completion_tokens":20,"reasoning_tokens":9}}')
    assert cb_events.parse_frame(frame).usage.reasoning_tokens == 9

    frame = cb_events.SSEFrame(event="", data='{"usage":{"completion_tokens":20}}')
    assert cb_events.parse_frame(frame).usage.reasoning_tokens is None

def test_usage_details_invalid_values_fall_back_to_top_level():
    """details 值非法（bool / 字符串）时回落顶层；顶层也非法则 None。"""
    frame = cb_events.SSEFrame(event="", data=(
        '{"usage":{"cached_tokens":4,"reasoning_tokens":6,'
        '"prompt_tokens_details":{"cached_tokens":"7"},'
        '"completion_tokens_details":{"reasoning_tokens":false}}}'))
    usage = cb_events.parse_frame(frame).usage
    assert usage.cached_tokens == 4
    assert usage.reasoning_tokens == 6

    frame = cb_events.SSEFrame(event="", data=(
        '{"usage":{"completion_tokens_details":{"reasoning_tokens":true}}}'))
    assert cb_events.parse_frame(frame).usage.reasoning_tokens is None

    # details 不是对象时同样走顶层兜底，不抛
    frame = cb_events.SSEFrame(
        event="", data='{"usage":{"completion_tokens_details":"x","reasoning_tokens":2}}')
    assert cb_events.parse_frame(frame).usage.reasoning_tokens == 2

def test_parse_all_events_returns_empty_for_done_and_blank():
    assert cb_events.parse_all_events(cb_events.SSEFrame(event="", data="[DONE]")) == []
    assert cb_events.parse_all_events(cb_events.SSEFrame(event="", data="")) == []

@pytest.mark.parametrize("data", ["{broken", "[1,2]"])
def test_parse_all_events_raises_on_malformed(data):
    with pytest.raises(UpstreamProtocolViolation):
        cb_events.parse_all_events(cb_events.SSEFrame(event="", data=data))

def test_parse_all_events_with_tool_calls_does_not_duplicate_finish():
    """带 tool_calls 的收尾帧：工具事件 + finish，且 usage 不重复。"""
    frame = cb_events.SSEFrame(event="", data=(
        '{"choices":[{"delta":{"tool_calls":[{"id":"c","function":'
        '{"name":"f","arguments":"{}"}}]},"finish_reason":"tool_calls"}],'
        '"usage":{"prompt_tokens":2}}'))
    kinds = [e.kind for e in cb_events.parse_all_events(frame)]
    assert kinds == [EventKind.TOOL_CALLS, EventKind.USAGE, EventKind.FINISH]

def test_tool_calls_list_of_only_non_dicts_falls_through():
    """tool_calls 全是非 dict → 不算工具事件，继续走 content（events 42→45）。"""
    frame = cb_events.SSEFrame(
        event="", data='{"choices":[{"delta":{"tool_calls":[1,2],"content":"c"}}]}')
    assert cb_events.parse_frame(frame).kind is EventKind.CONTENT

def test_content_non_string_falls_through_to_reasoning():
    """content 非字符串 → 继续看 reasoning（events 46→下一分支）。"""
    frame = cb_events.SSEFrame(
        event="", data='{"choices":[{"delta":{"content":5,"reasoning_content":"r"}}]}')
    assert cb_events.parse_frame(frame).kind is EventKind.REASONING

def test_reasoning_non_string_is_ignored():
    frame = cb_events.SSEFrame(
        event="", data='{"choices":[{"delta":{"reasoning_content":5}}]}')
    assert cb_events.parse_frame(frame) is None

def test_finish_reason_non_string_is_ignored():
    frame = cb_events.SSEFrame(
        event="", data='{"choices":[{"delta":{},"finish_reason":5}]}')
    assert cb_events.parse_frame(frame) is None

def test_choices_missing_returns_none_choice():
    """没有 choices 键 → 不报错，按无 choice 处理。"""
    frame = cb_events.SSEFrame(event="", data='{"usage":{"prompt_tokens":1}}')
    assert cb_events.parse_frame(frame).kind is EventKind.USAGE

def test_blank_noise_tool_call_is_dropped_but_argument_shards_kept():
    """噪声（无 name 且空 arguments）丢弃；正常分片（带实际 arguments）保留。"""
    body_obj = {
        "choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {}},
            {"index": 0, "function": {"arguments": "{}"}},
            {"index": 0, "id": "c1", "type": "function",
             "function": {"name": "bash", "arguments": ""}},
            {"index": 0, "function": {"arguments": '{"x":1}'}},
        ]}}],
    }
    frame = cb_events.SSEFrame(event="", data=json.dumps(body_obj))
    event = cb_events.parse_frame(frame)
    assert event is not None and event.kind is EventKind.TOOL_CALLS
    kept = event.tool_calls
    assert len(kept) == 2                                  # 两个噪声被丢
    assert kept[0]["function"]["name"] == "bash"           # 首块保留
    assert kept[1]["function"]["arguments"] == '{"x":1}'   # 分片保留

def test_clean_history_tool_calls_removes_dirty_and_orphans(tmp_path):
    """历史清理：空名剔除、空占位丢弃、悬空 tool 成对清理。"""
    from src.provider.codebuddy.client import _clean_history_tool_calls

    body = {"messages": [
        {"role": "system", "content": "s"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "bad", "type": "function",
                         "function": {"arguments": "{}"}},
                        {"id": "good", "type": "function",
                         "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "bad", "content": "x"},
        {"role": "tool", "tool_call_id": "good", "content": "y"},
        {"role": "assistant", "content": None, "tool_calls": []},
        {"role": "user", "content": "hi"},
    ]}
    _clean_history_tool_calls(body)
    # bad 被剔 → 其 tool 消息删；good 保留 → 其 tool 消息保留；
    # 空占位 assistant 丢弃
    roles = [(m["role"], m.get("tool_call_id", "")) for m in body["messages"]]
    assert roles == [
        ("system", ""), ("assistant", ""), ("tool", "good"), ("user", "")]

    # 边界：非 list messages 原样返回；非 dict 消息与非 dict tc 跳过
    body2 = {"messages": "nope"}
    _clean_history_tool_calls(body2)
    assert body2["messages"] == "nope"

    body3 = {"messages": [
        "junk",
        {"role": "assistant", "content": None, "tool_calls": ["junk", {"id": "bad"}]},
        {"role": "tool", "tool_call_id": "bad", "content": "x"},
    ]}
    _clean_history_tool_calls(body3)
    # 非dict消息保留、非dict tc 剔除；assistant 带 content 保留；悬空 tool 清理
    assert [(m if isinstance(m, str) else m["role"]) for m in body3["messages"]] == ["junk"]

    # assistant 有实际 content → tool_calls 剔除后消息本身保留
    body4 = {"messages": [
        {"role": "assistant", "content": "文本",
         "tool_calls": [{"id": "bad", "type": "function",
                         "function": {"arguments": "{}"}}]},
    ]}
    _clean_history_tool_calls(body4)
    assert body4["messages"][0]["role"] == "assistant"
    assert body4["messages"][0]["content"] == "文本"
    assert "tool_calls" not in body4["messages"][0]

def test_classify_429_model_saturated_code_is_model_level():
    """429 + 14003「模型饱和」→ 模型级，不能落账号级 SOFT。

    国际版实测原文：displayMsg「Model busy. Please switch models or retry
    later」、displayTips「This model is currently saturated」。上游要求换模型，
    说明同凭证其他模型可用；落 SOFT 会让整张凭证 60s 全部模型一起不可用。
    """
    body = (b'{"code":14003,"msg":"too many requests",'
            b'"displayMsg":{"en":"Model busy. Please switch models or retry later"},'
            b'"displayTips":{"en":"This model is currently saturated"}}')
    assert cb_events.classify_status(429, body) is ErrKind.MODEL

def test_classify_429_model_rate_limit_code_still_model_level():
    """回归保护：6004「该模型用量超限」仍是模型级。"""
    assert cb_events.classify_status(429, b'{"code":6004}') is ErrKind.MODEL

def test_classify_429_unknown_code_stays_soft():
    """回归保护：429 里认不出的业务码仍按账号级软限流。"""
    assert cb_events.classify_status(429, b'{"code":9999}') is ErrKind.SOFT
    assert cb_events.classify_status(429, b"") is ErrKind.SOFT

def test_provider_classify_routes_14003_to_model():
    """国际版复用同一分类：provider 入口（含 intl 子类）同样是模型级。"""
    body = b'{"code":14003,"msg":"too many requests"}'
    assert CodeBuddyProvider().classify(429, body) is ErrKind.MODEL
    assert CodeBuddyIntlProvider().classify(429, body) is ErrKind.MODEL
    assert CodeBuddyIntlProvider().classify(429, b'{"code":9999}') is ErrKind.SOFT


def test_disabled_reason_flags_account_restriction():
    """403/11140「账号异常行为被限制」→ 可操作原因，不能写成 session dead。

    实测 2026-10-27 08:21 codebuddy_intl 六个号同时回
    `{"code":11140,...,"displayMsg":{"en":"Your account has been restricted
    due to unusual activity","zh":"账号存在异常行为，已被限制使用"}}`：重登无效，
    只能申诉或换号，管理台必须显示真实动作。
    """
    body = (b'{"code":11140,"msg":"request illegal",'
            b'"displayMsg":{"en":"Your account has been restricted due to unusual activity",'
            b'"zh":"\xe8\xb4\xa6\xe5\x8f\xb7\xe5\xad\x98\xe5\x9c\xa8\xe5\xbc\x82\xe5\xb8\xb8'
            b'\xe8\xa1\x8c\xe4\xb8\xba\xef\xbc\x8c\xe5\xb7\xb2\xe8\xa2\xab\xe9\x99\x90'
            b'\xe5\x88\xb6\xe4\xbd\xbf\xe7\x94\xa8"}}')
    reason = cb_events.disabled_reason(body)
    assert "账号被上游限制" in reason and "申诉" in reason
    # 国际版子类走同一实现
    assert CodeBuddyIntlProvider().disabled_reason(body) == reason


def test_disabled_reason_empty_for_plain_403_and_garbage():
    """认不出原因就返回空串（落库回落 session dead），并且不因坏字节崩。"""
    assert cb_events.disabled_reason(b'{"code":11128}') == ""
    assert cb_events.disabled_reason(b"") == ""
    assert cb_events.disabled_reason(b"\xff\xfe") == ""

"""灵犀 SSE 帧 → 中立事件、错误分类与 usage 归一。"""

from __future__ import annotations

from src.provider.base import ErrKind, EventKind
from src.provider.lingxi import events as lingxi_events


def test_text_of_variants():
    assert lingxi_events.text_of("boom") == "boom"
    assert lingxi_events.text_of(None) == ""
    assert lingxi_events.text_of({"hint": "h"}) == "h"
    assert lingxi_events.text_of({"msg": "m"}) == "m"
    assert lingxi_events.text_of({"message": "x"}) == "x"
    assert lingxi_events.text_of({"detail": "d"}) == "d"
    assert lingxi_events.text_of({"error_message": "e"}) == "e"
    assert lingxi_events.text_of({"data": {"hint": "inner"}}) == "inner"
    assert lingxi_events.text_of({"data": {"other": 1}}) == ""
    assert lingxi_events.text_of({"hint": "  "}) == ""


def test_frame_error_variants():
    assert lingxi_events.frame_error("x") is None
    assert lingxi_events.frame_error({"code": "Success"}) is None
    assert lingxi_events.frame_error({"code": ""}) is None
    code, message = lingxi_events.frame_error({"code": "Boom", "hint": "要挂"})
    assert (code, message) == ("Boom", "要挂")
    code, message = lingxi_events.frame_error({"code": "Boom"})
    assert code == "Boom" and "Boom" in message
    code, message = lingxi_events.frame_error({"result": "failed", "msg": "坏了"})
    assert (code, message) == ("failed", "坏了")
    code, message = lingxi_events.frame_error({"result": "failed"})
    assert code == "failed" and "failed" in message
    assert lingxi_events.frame_error({"result": "ok"}) is None


def test_classify_code_covers_documented_semantics():
    assert lingxi_events.classify_code("AuditFailed") is ErrKind.INVALID
    assert lingxi_events.classify_code("x", "内容审核未通过") is ErrKind.INVALID
    assert lingxi_events.classify_code("x", "wps_sid is required") is ErrKind.DEAD
    assert lingxi_events.classify_code("x", "登录态已失效") is ErrKind.DEAD
    assert lingxi_events.classify_code("x", "insufficient credits") is ErrKind.CREDIT
    assert lingxi_events.classify_code("x", "积分不足") is ErrKind.CREDIT
    assert lingxi_events.classify_code("x", "model not found") is ErrKind.BLOCKED
    assert lingxi_events.classify_code("x", "模型不存在") is ErrKind.BLOCKED
    assert lingxi_events.classify_code("x", "AI服务异常") is ErrKind.OTHER
    assert lingxi_events.classify_code(None) is ErrKind.OTHER


def test_session_expired_and_audit_predicates():
    assert lingxi_events.is_session_expired("SQLNoRows: 会话不存在")
    assert not lingxi_events.is_session_expired("rate limited")
    assert lingxi_events.is_audit_blocked("AuditFailed")
    assert lingxi_events.is_audit_blocked("暂时无法回答此问题")
    assert not lingxi_events.is_audit_blocked("normal")


def test_classify_status_matrix():
    assert lingxi_events.classify_status(401) is ErrKind.DEAD
    assert lingxi_events.classify_status(402) is ErrKind.CREDIT
    assert lingxi_events.classify_status(403) is ErrKind.SOFT
    assert lingxi_events.classify_status(404) is ErrKind.INVALID
    assert lingxi_events.classify_status(400) is ErrKind.REQUEST
    assert lingxi_events.classify_status(422) is ErrKind.REQUEST
    assert lingxi_events.classify_status(429) is ErrKind.MODEL
    assert lingxi_events.classify_status(500) is ErrKind.OTHER


def test_usage_computes_total_and_moves_flat_fields():
    events = lingxi_events.parse_frame({
        "code": "Success",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                  "total_tokens": 999, "cached_tokens": 3,
                  "reasoning_tokens": 2}})
    usage = [event for event in events if event.kind == EventKind.USAGE][0].usage
    assert usage is not None
    assert usage.input_tokens == 10
    assert usage.output_tokens == 5
    assert usage.cached_tokens == 3
    assert usage.reasoning_tokens == 2


def test_usage_accepts_nested_details_and_strings():
    events = lingxi_events.parse_frame({
        "usage": {"prompt_tokens": "7", "completion_tokens": 1.9,
                  "prompt_tokens_details": {"cached_tokens": 4},
                  "completion_tokens_details": {"reasoning_tokens": 2}}})
    usage = events[0].usage
    assert usage.input_tokens == 7
    assert usage.output_tokens == 1
    assert usage.cached_tokens == 4
    assert usage.reasoning_tokens == 2


def test_usage_ignores_broken_values_and_empty():
    assert lingxi_events.parse_frame({"usage": {}}) == []
    assert lingxi_events.parse_frame({"usage": None}) == []
    events = lingxi_events.parse_frame({"usage": {"prompt_tokens": True,
                                                  "completion_tokens": None}})
    usage = events[0].usage
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.cached_tokens is None
    assert usage.reasoning_tokens is None


def test_parse_frame_emits_all_kinds_together():
    events = lingxi_events.parse_frame({
        "code": "Success",
        "choices": [{"text": "hi", "reasoning_content": "think",
                     "tool_calls": [{"index": 0, "id": "c1",
                                     "function": {"name": "f",
                                                  "arguments": "{}"}}],
                     "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2}})
    kinds = [event.kind for event in events]
    assert kinds == [EventKind.CONTENT, EventKind.REASONING,
                     EventKind.TOOL_CALLS, EventKind.FINISH, EventKind.USAGE]
    call = events[2].tool_calls[0]
    assert call["id"] == "c1" and call["type"] == "function"
    assert call["function"] == {"name": "f", "arguments": "{}"}


def test_parse_frame_error_frame():
    events = lingxi_events.parse_frame({"code": "InsufficientCredits",
                                        "hint": "积分不足"})
    assert len(events) == 1
    assert events[0].kind == EventKind.ERROR
    assert events[0].error_kind is ErrKind.CREDIT
    assert events[0].error_message == "积分不足"


def test_parse_frame_ignores_non_dict_and_empty_choices():
    assert lingxi_events.parse_frame(None) == []
    assert lingxi_events.parse_frame({"choices": []}) == []
    assert lingxi_events.parse_frame({"choices": [None]}) == []
    assert lingxi_events.parse_frame({"choices": [{}]}) == []


def test_tool_call_shapes():
    events = lingxi_events.parse_frame({"choices": [{"tool_calls": [
        {"index": "1", "function": {"arguments": "{}"}},
        {"function": {"name": "f"}},
        {"index": None, "function": "bad"},
        "junk",
    ]}]})
    calls = events[0].tool_calls
    assert calls is not None
    assert calls[0] == {"index": 1, "function": {"arguments": "{}"}}
    assert calls[1] == {"function": {"name": "f"}}


def test_tool_calls_non_list_and_empty():
    assert lingxi_events.parse_frame({"choices": [{"tool_calls": "x"}]}) == []
    assert lingxi_events.parse_frame({"choices": [{"tool_calls": []}]}) == []

def test_int_rejects_non_numeric_string():
    assert lingxi_events.parse_frame({"usage": {"prompt_tokens": "x"}})[0].usage.input_tokens == 0

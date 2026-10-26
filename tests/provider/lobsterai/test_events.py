"""LobsterAI 协议层：SSE 解析、错误分类、信封解析。"""

from __future__ import annotations

import pytest

from src.engine.sse import SSEFrame
from src.provider.base import ErrKind, EventKind, UpstreamProtocolViolation
from src.provider.lobsterai import events as lobsterai_events


def _frame(payload) -> SSEFrame:
    import json

    return SSEFrame(event="", data=json.dumps(payload))


# ------------------------------------------------------------------ SSE

def test_parse_frame_yields_content_and_usage():
    content = lobsterai_events.parse_frame(
        _frame({"choices": [{"delta": {"content": "hi"}}]}))
    assert content is not None and content.kind is EventKind.CONTENT
    assert content.content == "hi"
    usage = lobsterai_events.parse_frame(
        _frame({"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 4}}))
    assert usage is not None and usage.kind is EventKind.USAGE
    assert usage.usage.input_tokens == 3 and usage.usage.output_tokens == 4


def test_parse_frame_reads_reasoning_content():
    event = lobsterai_events.parse_frame(
        _frame({"choices": [{"delta": {"reasoning_content": "r"}}]}))
    assert event is not None and event.kind is EventKind.REASONING


def test_parse_frame_ignores_done():
    assert lobsterai_events.parse_frame(SSEFrame(event="", data="[DONE]")) is None


def test_parse_all_events_splits_finish_and_usage():
    events = lobsterai_events.parse_all_events(_frame({
        "choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2}}))
    kinds = [event.kind for event in events]
    assert EventKind.CONTENT in kinds
    assert EventKind.FINISH in kinds
    assert EventKind.USAGE in kinds


def test_parse_frame_rejects_bad_json():
    with pytest.raises(UpstreamProtocolViolation):
        lobsterai_events.parse_frame(SSEFrame(event="", data="{oops"))


# ------------------------------------------------------------ 流内错误帧

def test_error_event_classifies_credit_exhaustion():
    """额度耗尽（40201）→ CREDIT：等签到恢复，不是死禁。"""
    event = lobsterai_events._error_event({"code": 40201, "message": "免费额度已用完"})
    assert event.kind is EventKind.ERROR
    assert event.error_kind is ErrKind.CREDIT
    assert event.error_code == 40201


def test_error_event_classifies_unknown_model_as_invalid():
    event = lobsterai_events._error_event({"code": 40300, "message": "unknown model"})
    assert event.error_kind is ErrKind.INVALID


def test_error_event_classifies_dead_session():
    event = lobsterai_events._error_event({"code": 40100, "message": "token rejected"})
    assert event.error_kind is ErrKind.DEAD


def test_error_event_defaults_to_other_not_none():
    """未命中任何判据时归 OTHER（必须换号），不能是 none（不换号）。"""
    event = lobsterai_events._error_event({"message": "something odd"})
    assert event.error_kind is ErrKind.OTHER


def test_error_event_falls_back_to_type_field():
    event = lobsterai_events._error_event({"type": "proxy_error"})
    assert event.error_message == "proxy_error"


# ------------------------------------------------------------ HTTP 分类

def test_classify_status_credit_markers_beat_status_code():
    """实测上游用 400 + 中文「积分不足」表达余额耗尽 → CREDIT 而非可重试。"""
    assert lobsterai_events.classify_status(400, "积分不足".encode()) is ErrKind.CREDIT
    assert lobsterai_events.classify_status(400, b"insufficient credit") is ErrKind.CREDIT
    assert lobsterai_events.classify_status(400, "额度已用完，请升级套餐".encode()) \
        is ErrKind.CREDIT


def test_classify_status_dead_beats_rate_limit():
    """会话死亡（40100）必须先于 429 判定：换号重试对死会话毫无意义。"""
    assert lobsterai_events.classify_status(429, b"40100") is ErrKind.DEAD
    assert lobsterai_events.classify_status(429, b"") is ErrKind.SOFT


def test_classify_status_request_side_is_invalid():
    assert lobsterai_events.classify_status(400, b"unknown model") is ErrKind.INVALID
    assert lobsterai_events.classify_status(400, b'{"code":40300}') is ErrKind.INVALID
    assert lobsterai_events.classify_status(404, b"") is ErrKind.INVALID
    assert lobsterai_events.classify_status(422, b"") is ErrKind.INVALID


def test_classify_status_credit_and_dead_and_server():
    assert lobsterai_events.classify_status(402, b"") is ErrKind.CREDIT
    assert lobsterai_events.classify_status(401, b"") is ErrKind.DEAD
    assert lobsterai_events.classify_status(403, b"") is ErrKind.DEAD
    assert lobsterai_events.classify_status(500, b"") is ErrKind.OTHER
    assert lobsterai_events.classify_status(503, b"") is ErrKind.OTHER


def test_classify_error_code_paths():
    assert lobsterai_events.classify_error_code(402, "") is ErrKind.CREDIT
    assert lobsterai_events.classify_error_code(40201, "") is ErrKind.CREDIT
    assert lobsterai_events.classify_error_code(429, "") is ErrKind.SOFT
    assert lobsterai_events.classify_error_code(40300, "") is ErrKind.INVALID
    assert lobsterai_events.classify_error_code(None, "40101") is ErrKind.DEAD
    assert lobsterai_events.classify_error_code(None, "额度不足") is ErrKind.CREDIT
    assert lobsterai_events.classify_error_code(None, "weird") is ErrKind.OTHER


def test_relogin_required_marks_terminal_only():
    assert lobsterai_events.relogin_required(b"code=40100") != ""
    assert lobsterai_events.relogin_required(b"token rejected") != ""
    assert lobsterai_events.relogin_required(b"500 internal error") == ""
    assert lobsterai_events.relogin_required(b"") == ""


# ------------------------------------------------------------ 信封解析

def test_parse_envelope_returns_data_verbatim():
    """`data` 形状随端点而变（数组 / 对象），信封层不做类型要求。"""
    assert lobsterai_events.parse_envelope(b'{"code":0,"data":[1,2]}', "X") == [1, 2]
    assert lobsterai_events.parse_envelope(b'{"code":0,"data":{"a":1}}', "X") == {"a": 1}


def test_parse_envelope_accepts_string_code():
    assert lobsterai_events.parse_envelope(b'{"code":"0","data":{}}', "X") == {}


def test_parse_envelope_rejects_nonzero_code():
    with pytest.raises(UpstreamProtocolViolation, match="rejected"):
        lobsterai_events.parse_envelope(b'{"code":1,"msg":"nope"}', "X")


def test_parse_envelope_rejects_bad_json_and_shape():
    with pytest.raises(UpstreamProtocolViolation):
        lobsterai_events.parse_envelope(b"not json", "X")
    with pytest.raises(UpstreamProtocolViolation):
        lobsterai_events.parse_envelope(b"[]", "X")


def test_parse_envelope_rejects_null_data():
    """凭据失效时上游回 `code:0` 但 `data:null`：只看 code 会误判成功。"""
    with pytest.raises(UpstreamProtocolViolation, match="no data"):
        lobsterai_events.parse_envelope(b'{"code":0,"data":null}', "X")


def test_expect_object_rejects_non_object():
    assert lobsterai_events.expect_object({"a": 1}, "X") == {"a": 1}
    with pytest.raises(UpstreamProtocolViolation):
        lobsterai_events.expect_object([1], "X")

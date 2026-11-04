"""小浣熊 SSE → 事件映射与错误分类。"""

from __future__ import annotations

from src.provider.base import ErrKind, EventKind
from src.provider.raccoon import events as raccoon_events
from tests.helpers.http import chunk_payload as chunk
from tests.helpers.http import sse_frame


def test_parse_frame_maps_content_and_finish():
    """正文与 finish 同帧：finish 不能被正文吞掉（客户端否则永远收不到结束）。"""
    frame = sse_frame(chunk({"content": "hi"}, finish="stop"))
    kinds = [event.kind for event in raccoon_events.parse_frame(frame)]
    assert kinds == [EventKind.CONTENT, EventKind.FINISH]


def test_parse_frame_reasoning_content_is_separate_when_alone():
    frame = sse_frame(chunk({"reasoning_content": "why"}))
    kinds = [event.kind for event in raccoon_events.parse_frame(frame)]
    assert kinds == [EventKind.REASONING]


def test_parse_frame_accepts_reasoning_alias():
    frame = sse_frame(chunk({"reasoning": "why"}))
    kinds = [event.kind for event in raccoon_events.parse_frame(frame)]
    assert kinds == [EventKind.REASONING]


def test_parse_frame_done_and_empty_frames_are_ignored():
    assert raccoon_events.parse_frame(sse_frame("[DONE]")) == []
    assert raccoon_events.parse_frame(sse_frame("")) == []


def test_parse_frame_usage_and_model_echo():
    frame = sse_frame({"model": "sn-glm-5-3", "choices": [],
                       "usage": {"prompt_tokens": 3, "completion_tokens": 2}})
    events = raccoon_events.parse_frame(frame)
    assert events[0].kind is EventKind.USAGE
    assert events[0].model == "sn-glm-5-3"


def test_parse_frame_error_envelope_uses_string_code():
    frame = sse_frame({"error": {"code": "user_model_rate_limited",
                                 "message": "您的模型请求频率超限，请稍后重试"}})
    event = raccoon_events.parse_frame(frame)[0]
    assert event.kind is EventKind.ERROR
    assert event.error_kind is ErrKind.CONCURRENCY


def test_parse_frame_error_without_message():
    frame = sse_frame({"error": {"code": "some_new_error"}})
    event = raccoon_events.parse_frame(frame)[0]
    assert event.error_message == ""
    assert event.error_kind is ErrKind.OTHER


def test_classify_error_markers():
    assert raccoon_events.classify_error(b"user identity could not be verified") \
        is ErrKind.DEAD
    assert raccoon_events.classify_error("unauthorized") is ErrKind.DEAD
    assert raccoon_events.classify_error("user_model_rate_limited") \
        is ErrKind.CONCURRENCY
    assert raccoon_events.classify_error("积分不足") is ErrKind.CREDIT
    assert raccoon_events.classify_error("balance exhausted") is ErrKind.CREDIT
    assert raccoon_events.classify_error("weird") is ErrKind.OTHER


def test_classify_status_auth_and_identity():
    assert raccoon_events.classify_status(401) is ErrKind.DEAD
    assert raccoon_events.classify_status(
        403, b'{"message":"User identity could not be verified"}') is ErrKind.DEAD
    # 403 但没点到身份校验 → 请求级（不硬禁用一条好凭证）
    assert raccoon_events.classify_status(403, b'{"message":"forbidden"}') \
        is ErrKind.REQUEST


def test_classify_status_credit_and_rate_limit():
    assert raccoon_events.classify_status(402) is ErrKind.CREDIT
    assert raccoon_events.classify_status(
        429, b'{"error":{"code":"user_model_rate_limited"}}') is ErrKind.CONCURRENCY
    assert raccoon_events.classify_status(429, b'{"error":"slow down"}') \
        is ErrKind.SOFT


def test_classify_status_other_statuses():
    for status in (400, 404, 422):
        assert raccoon_events.classify_status(status) is ErrKind.INVALID
    for status in (502, 503, 504, 500):
        assert raccoon_events.classify_status(status) is ErrKind.OTHER
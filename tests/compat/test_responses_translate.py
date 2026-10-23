"""Responses 出口：Event → Responses SSE 帧翻译与 completion_to_response。"""

from __future__ import annotations

import json

from src.compat.responses.response import ResponsesStreamTranslator, completion_to_response
from src.provider.base import Event, EventKind, Usage


def _frames(chunks: list[bytes]) -> list[tuple[str, dict]]:
    """把若干字节块里的**全部** SSE 帧摊平（一个块可能含多帧）。"""
    parsed: list[tuple[str, dict]] = []
    for chunk in chunks:
        lines = chunk.decode().split("\n")
        for index, line in enumerate(lines):
            if line.startswith("event: "):
                parsed.append((line[7:], json.loads(lines[index + 1][6:])))
    return parsed

def _translate(events: list[Event]) -> list[tuple[str, dict]]:
    translator = ResponsesStreamTranslator("m")
    chunks: list[bytes] = []
    for event in events:
        chunks.extend(translator.translate(event))
    chunks.extend(translator.finish())
    return _frames(chunks)

def test_translate_text_lifecycle():
    frames = _translate([
        Event(kind=EventKind.CONTENT, content="he"),
        Event(kind=EventKind.CONTENT, content="llo"),
        Event(kind=EventKind.USAGE, usage=Usage(input_tokens=3, output_tokens=2)),
        Event(kind=EventKind.FINISH, finish_reason="stop"),
    ])
    types = [name for name, _ in frames]
    assert types == [
        "response.created", "response.output_item.added",
        "response.content_part.added", "response.output_text.delta",
        "response.output_text.delta", "response.output_text.done",
        "response.content_part.done", "response.output_item.done",
        "response.completed",
    ]
    assert [p["sequence_number"] for _, p in frames] == list(range(len(frames)))
    deltas = [p["delta"] for name, p in frames
              if name == "response.output_text.delta"]
    assert deltas == ["he", "llo"]
    completed = frames[-1][1]["response"]
    assert completed["status"] == "completed"
    assert completed["usage"]["total_tokens"] == 5
    assert completed["output"][0]["content"][0]["text"] == "hello"
    assert completed["object"] == "response"
    for key in ("id", "created_at", "model", "output", "parallel_tool_calls",
                "tool_choice", "tools"):
        assert key in completed

def test_translate_reasoning_summary():
    frames = _translate([
        Event(kind=EventKind.REASONING, content="think"),
        Event(kind=EventKind.FINISH, finish_reason="stop"),
    ])
    types = [name for name, _ in frames]
    assert "response.reasoning_summary_text.delta" in types
    assert "response.reasoning_summary_part.added" in types
    assert "response.reasoning_summary_text.done" in types
    assert "response.reasoning_summary_part.done" in types
    item = frames[-1][1]["response"]["output"][0]
    assert item["type"] == "reasoning"
    assert item["summary"][0]["text"] == "think"

def test_translate_tool_calls_accumulate_arguments():
    frames = _translate([
        Event(kind=EventKind.TOOL_CALLS, tool_calls=[
            {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": "f", "arguments": ""}}]),
        Event(kind=EventKind.TOOL_CALLS, tool_calls=[
            {"index": 0, "function": {"arguments": '{"a"'}}]),
        Event(kind=EventKind.TOOL_CALLS, tool_calls=[
            {"index": 0, "function": {"arguments": ":1}"}}]),
        Event(kind=EventKind.FINISH, finish_reason="tool_calls"),
    ])
    deltas = [p["delta"] for name, p in frames
              if name == "response.function_call_arguments.delta"]
    assert deltas == ['{"a"', ":1}"]
    done = [p for name, p in frames
            if name == "response.function_call_arguments.done"]
    assert done[0]["arguments"] == '{"a":1}'
    item = frames[-1][1]["response"]["output"][0]
    assert item["type"] == "function_call"
    assert item["call_id"] == "call_1"
    assert item["name"] == "f"
    assert item["arguments"] == '{"a":1}'
    assert item["status"] == "completed"

def test_translate_reasoning_after_text_reuses_single_item():
    """先正文后思考：created 只发一次，reasoning item 只建一个、逐片累积。"""
    frames = _translate([
        Event(kind=EventKind.CONTENT, content="answer"),
        Event(kind=EventKind.REASONING, content="a"),
        Event(kind=EventKind.REASONING, content="b"),
        Event(kind=EventKind.FINISH, finish_reason="stop"),
    ])
    assert [name for name, _ in frames].count("response.created") == 1
    assert [name for name, _ in frames].count("response.output_item.added") == 2
    reasoning = frames[-1][1]["response"]["output"][1]
    assert reasoning["summary"][0]["text"] == "ab"

def test_stale_events_after_terminate_are_ignored():
    """终止事件之后再来 FINISH / ERROR：不重复发终止事件。"""
    translator = ResponsesStreamTranslator("m")
    list(translator.translate(Event(kind=EventKind.CONTENT, content="x")))
    list(translator.translate(Event(kind=EventKind.FINISH, finish_reason="stop")))
    assert translator.done_sent is True
    assert list(translator.translate(Event(kind=EventKind.FINISH,
                                           finish_reason="stop"))) == []
    assert list(translator.translate(Event(kind=EventKind.ERROR,
                                           error_message="late"))) == []
    assert translator.error_frame("late", "invalid_request") == b""

def test_fail_after_content_keeps_created_single():
    translator = ResponsesStreamTranslator("m")
    list(translator.translate(Event(kind=EventKind.CONTENT, content="x")))
    frames = _frames(list(translator.translate(
        Event(kind=EventKind.ERROR, error_message="boom"))))
    assert [name for name, _ in frames] == ["response.failed"]

def test_translate_tool_call_without_index_or_id():
    """上游偶发不给 index/id：补稳定位置与合成 id，不能崩。"""
    frames = _translate([
        Event(kind=EventKind.TOOL_CALLS, tool_calls=[
            {"function": {"name": "f", "arguments": ""}}]),
        Event(kind=EventKind.FINISH, finish_reason="stop"),
    ])
    item = frames[-1][1]["response"]["output"][0]
    assert item["type"] == "function_call"
    assert item["call_id"].startswith("call_")
    assert item["name"] == "f"

def test_translate_late_function_name_backfilled():
    frames = _translate([
        Event(kind=EventKind.TOOL_CALLS, tool_calls=[
            {"index": 0, "id": "c1", "function": {"arguments": '{"a"'}}]),
        Event(kind=EventKind.TOOL_CALLS, tool_calls=[
            {"index": 0, "function": {"name": "late", "arguments": ":1}"}}]),
        Event(kind=EventKind.FINISH, finish_reason="stop"),
    ])
    item = frames[-1][1]["response"]["output"][0]
    assert item["name"] == "late"          # 首个分片没给函数名，后续分片补齐
    assert item["arguments"] == '{"a":1}'  # 两片参数照常拼接

def test_translate_length_is_incomplete():
    frames = _translate([
        Event(kind=EventKind.CONTENT, content="cut"),
        Event(kind=EventKind.FINISH, finish_reason="length"),
    ])
    assert frames[-1][0] == "response.incomplete"
    response = frames[-1][1]["response"]
    assert response["status"] == "incomplete"
    assert response["incomplete_details"] == {"reason": "max_output_tokens"}

def test_translate_content_filter_is_incomplete():
    frames = _translate([Event(kind=EventKind.FINISH, finish_reason="content_filter")])
    assert frames[-1][0] == "response.incomplete"
    assert frames[-1][1]["response"]["incomplete_details"] == {"reason": "content_filter"}

def test_finish_without_upstream_finish_event():
    """上游断流：finish() 补终止事件，客户端不会挂住。"""
    translator = ResponsesStreamTranslator("m")
    chunks = list(translator.translate(Event(kind=EventKind.CONTENT, content="x")))
    chunks.extend(translator.finish())
    frames = _frames(chunks)
    assert frames[-1][0] == "response.completed"
    assert translator.done_sent is True
    # 二次 finish 不重复发
    assert list(translator.finish()) == []

def test_finish_with_no_output_still_terminates():
    frames = _translate([Event(kind=EventKind.FINISH, finish_reason="stop")])
    assert frames[0][0] == "response.created"
    assert frames[-1][0] == "response.completed"
    assert frames[-1][1]["response"]["output"] == []

def test_translate_error_event_becomes_failed():
    frames = _translate([Event(kind=EventKind.ERROR, error_message="boom")])
    assert frames[-1][0] == "response.failed"
    response = frames[-1][1]["response"]
    assert response["status"] == "failed"
    assert response["error"] == {"code": "upstream_error", "message": "boom"}

def test_error_frame_maps_codes_for_codex():
    translator = ResponsesStreamTranslator("m")
    frames = _frames([translator.error_frame("all credentials unavailable",
                                             "no_healthy_credential")])
    assert frames[-1][0] == "response.failed"
    assert frames[-1][1]["response"]["error"]["code"] == "server_is_overloaded"

    other = ResponsesStreamTranslator("m")
    frames = _frames([other.error_frame("nope", "invalid_request")])
    assert frames[-1][1]["response"]["error"]["code"] == "invalid_request"

def test_empty_events_do_not_create_items():
    """空 content 事件不产生任何项；只有 FINISH 时补一个空 completed。"""
    frames = _translate([Event(kind=EventKind.CONTENT)])
    assert [name for name, _ in frames] == ["response.created", "response.completed"]
    assert frames[-1][1]["response"]["output"] == []

    frames = _translate([Event(kind=EventKind.USAGE, usage=Usage(input_tokens=1,
                                                                 output_tokens=1)),
                         Event(kind=EventKind.FINISH, finish_reason="stop")])
    assert frames[-1][1]["response"]["usage"]["input_tokens"] == 1

def test_keepalive_is_sse_comment():
    """Responses 出口必须用注释帧：该协议没有 ping 事件类型。"""
    from src.engine.sse import SSE_COMMENT

    assert ResponsesStreamTranslator("m").keepalive() is SSE_COMMENT

def test_usage_missing_fields_default_to_zero():
    frames = _translate([Event(kind=EventKind.USAGE, usage=Usage()),
                         Event(kind=EventKind.FINISH, finish_reason="stop")])
    usage = frames[-1][1]["response"]["usage"]
    assert usage == {"input_tokens": 0, "input_tokens_details": {"cached_tokens": 0},
                     "output_tokens": 0,
                     "output_tokens_details": {"reasoning_tokens": 0},
                     "total_tokens": 0}

def test_completion_to_response_full_message():
    payload = completion_to_response({
        "id": "chatcmpl-1", "object": "chat.completion", "created": 1,
        "model": "m",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": "hi",
            "reasoning_content": "why",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "f", "arguments": ""}}]}}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6,
                  "prompt_tokens_details": {"cached_tokens": 1},
                  "completion_tokens_details": {"reasoning_tokens": 1}},
    }, response_id="resp_x", created_at=99)
    assert (payload["id"], payload["created_at"]) == ("resp_x", 99)
    assert payload["status"] == "completed"
    assert payload["incomplete_details"] is None
    assert [item["type"] for item in payload["output"]] == [
        "reasoning", "message", "function_call"]
    assert payload["usage"] == {
        "input_tokens": 4, "input_tokens_details": {"cached_tokens": 1},
        "output_tokens": 2, "output_tokens_details": {"reasoning_tokens": 1},
        "total_tokens": 6}

def test_completion_to_response_length_and_missing_usage():
    payload = completion_to_response({
        "model": "m",
        "choices": [{"finish_reason": "length", "message": {"content": None}}],
    })
    assert payload["status"] == "incomplete"
    assert payload["incomplete_details"] == {"reason": "max_output_tokens"}
    assert payload["output"] == []
    assert payload["usage"]["total_tokens"] == 0

def test_completion_to_response_content_filter():
    payload = completion_to_response({
        "choices": [{"finish_reason": "content_filter", "message": {}}]})
    assert payload["incomplete_details"] == {"reason": "content_filter"}

def test_completion_to_response_tolerates_bad_tool_call_entries():
    payload = completion_to_response({"choices": [{"message": {
        "tool_calls": [None, {"id": "c1"}]}}]})
    item = payload["output"][0]
    assert item["call_id"] == "c1"
    assert item["name"] == "" and item["arguments"] == ""

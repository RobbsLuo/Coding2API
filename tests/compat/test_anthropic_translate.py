"""Anthropic 出口：Event → Anthropic SSE 帧翻译与 completion_to_message。"""

from __future__ import annotations

from src.compat.anthropic.response import AnthropicStreamTranslator, completion_to_message
from src.provider.base import Event, EventKind, Usage


def _drain(*events: Event, model: str = "glm-5.2") -> tuple[AnthropicStreamTranslator, list[str]]:
    translator = AnthropicStreamTranslator(model)
    frames: list[bytes] = []
    for event in events:
        frames.extend(translator.translate(event))
    names = [f.decode().split("\n", 1)[0][7:] for f in frames]
    return translator, names

def _drain_names(*events: Event) -> list[str]:
    translator = AnthropicStreamTranslator("m")
    frames: list[bytes] = []
    for event in events:
        frames.extend(translator.translate(event))
    return [f.decode().split("\n", 1)[0][7:] for f in frames]

def test_translator_text_sequence():
    translator, names = _drain(
        Event(kind=EventKind.CONTENT, content="hi"),
        Event(kind=EventKind.USAGE, usage=Usage(input_tokens=2, output_tokens=3)),
        Event(kind=EventKind.FINISH, finish_reason="stop"))
    assert names == ["message_start", "content_block_start", "content_block_delta",
                     "content_block_stop", "message_delta", "message_stop"]
    assert translator.usage.input_tokens == 2
    assert translator.done_sent is True

def test_translator_reasoning_emits_signature_before_stop():
    _, names = _drain(
        Event(kind=EventKind.REASONING, content="th"),
        Event(kind=EventKind.FINISH, finish_reason="stop"))
    assert "content_block_delta" in names
    assert names.count("content_block_stop") == 1

def test_translator_tool_calls_open_block_per_index():
    translator = AnthropicStreamTranslator("m")
    frames = list(translator.translate(Event(kind=EventKind.TOOL_CALLS, tool_calls=[
        {"index": 0, "id": "c1", "function": {"name": "f", "arguments": '{"a":'}}])))
    frames += list(translator.translate(Event(kind=EventKind.TOOL_CALLS, tool_calls=[
        {"index": 0, "function": {"arguments": "1}"}}])))
    frames += list(translator.translate(Event(kind=EventKind.TOOL_CALLS, tool_calls=[
        {"id": "c2", "function": {"name": "g"}}])))
    text = b"".join(frames).decode()
    assert text.count("event: content_block_start") == 2       # 两个工具块
    assert '"type": "input_json_delta"' in text
    assert '"name": "g"' in text

def test_translator_ignores_empty_events_and_unknown():
    translator = AnthropicStreamTranslator("m")
    assert list(translator.translate(Event(kind=EventKind.CONTENT, content=""))) == []
    assert list(translator.translate(Event(kind=EventKind.REASONING, content=""))) == []
    assert list(translator.translate(Event(kind=EventKind.TOOL_CALLS, tool_calls=[]))) == []
    assert list(translator.translate(Event(kind=EventKind.USAGE))) == []

def test_translator_finish_without_finish_event_closes_stream():
    translator = AnthropicStreamTranslator("m")
    list(translator.translate(Event(kind=EventKind.CONTENT, content="x")))
    frames = list(translator.finish())
    names = [f.decode().split("\n", 1)[0][7:] for f in frames]
    assert names[-1] == "message_stop"
    # 已收尾后再 finish 是幂等的
    assert list(translator.finish()) == []

def test_translator_finish_before_any_event_still_emits_start_and_stop():
    translator = AnthropicStreamTranslator("m")
    names = [f.decode().split("\n", 1)[0][7:] for f in translator.finish()]
    assert names == ["message_start", "message_delta", "message_stop"]

def test_translator_error_event_and_frame():
    translator = AnthropicStreamTranslator("m")
    frames = list(translator.translate(Event(kind=EventKind.ERROR,
                                             error_message="boom")))
    assert frames[0].decode().startswith("event: error")
    assert translator.done_sent is True
    assert b"invalid_request_error" in AnthropicStreamTranslator("m").error_frame(
        "bad", "invalid_request")
    assert b"overloaded_error" in AnthropicStreamTranslator("m").error_frame(
        "x", "no_healthy_credential")
    other = AnthropicStreamTranslator("m")
    assert b"api_error" in other.error_frame("x", "weird")
    assert other.done_sent is True
    # 已收尾后的错误帧不再发出
    assert list(other._fail("again")) == []

def test_translator_error_without_message_uses_default():
    translator = AnthropicStreamTranslator("m")
    frames = list(translator.translate(Event(kind=EventKind.ERROR)))
    assert b"upstream error" in frames[0]

def test_translator_keepalive_is_ping_event():
    """Anthropic 心跳是具名 `ping` 事件，不是 SSE 注释帧。

    官方流式规范里 ping 带 `event: ping` + `data: {"type": "ping"}`；只解析
    `event:` 行的客户端会忽略注释帧，长静默下仍会判超时断开。
    """
    assert (AnthropicStreamTranslator("m").keepalive()
            == b'event: ping\ndata: {"type": "ping"}\n\n')

def test_translator_stop_reason_mapping():
    from src.compat.anthropic.response import _stop_reason

    assert _stop_reason("length") == "max_tokens"
    assert _stop_reason("tool_calls") == "tool_use"
    assert _stop_reason("content_filter") == "end_turn"
    assert _stop_reason("weird") == "end_turn"

def test_translator_usage_payload_none():
    from src.compat.anthropic.response import _usage_payload

    assert _usage_payload(None) == {"input_tokens": 0, "output_tokens": 0}

def test_usage_payload_splits_cache_read_from_input():
    """内部 input_tokens 含缓存命中（OpenAI 口径），Anthropic 要拆成两笔。"""
    from src.compat.anthropic.response import _usage_payload

    assert _usage_payload(
        Usage(input_tokens=10, output_tokens=2, cached_tokens=6)) == {
            "input_tokens": 4, "output_tokens": 2, "cache_read_input_tokens": 6}

def test_usage_payload_cache_edge_cases():
    from src.compat.anthropic.response import _usage_payload

    # cached=0 不算命中，不补占位字段
    assert _usage_payload(
        Usage(input_tokens=10, output_tokens=1, cached_tokens=0)) == {
            "input_tokens": 10, "output_tokens": 1}
    # 上游数据异常（命中 > 输入）时夹到 0，不出现负输入
    assert _usage_payload(
        Usage(input_tokens=3, output_tokens=1, cached_tokens=9)) == {
            "input_tokens": 0, "output_tokens": 1, "cache_read_input_tokens": 9}
    # 总输入未知（None）时不拆缓存：只报 input=0 会与 cache_read 配成
    # 「整段提示全部命中缓存」，把用量算反——宁可不报命中，也不伪造。
    assert _usage_payload(Usage(cached_tokens=5)) == {
        "input_tokens": 0, "output_tokens": 0}

def test_translator_reuses_open_block_for_consecutive_same_kind():
    """连续同类型增量并入同一个块（不重复 start/stop），已收尾后再收尾是幂等的。"""
    translator = AnthropicStreamTranslator("m")
    frames: list[bytes] = []
    for event in (Event(kind=EventKind.CONTENT, content="a"),
                  Event(kind=EventKind.CONTENT, content="b"),
                  Event(kind=EventKind.REASONING, content="t1"),
                  Event(kind=EventKind.REASONING, content="t2"),
                  Event(kind=EventKind.FINISH, finish_reason="stop"),
                  Event(kind=EventKind.FINISH, finish_reason="stop")):
        frames.extend(translator.translate(event))
    names = [f.decode().split("\n", 1)[0][7:] for f in frames]
    assert names.count("content_block_start") == 2
    assert names.count("content_block_stop") == 2
    assert names.count("message_stop") == 1

def test_translator_reopens_same_kind_after_other_block():
    """先文本 → 再思考 → 再文本：第三个文本块要重新开一个（不并入旧块）。"""
    names = _drain_names(
        Event(kind=EventKind.CONTENT, content="a"),
        Event(kind=EventKind.REASONING, content="t"),
        Event(kind=EventKind.CONTENT, content="b"),
        Event(kind=EventKind.FINISH, finish_reason="stop"))
    assert names.count("content_block_start") == 3
    assert names.count("content_block_stop") == 3

def test_content_from_message_covers_all_block_kinds():
    from src.compat.anthropic.response import _content_from_message

    blocks = _content_from_message({
        "content": "x", "reasoning_content": "r",
        "tool_calls": [None, {"id": "c", "function": {"arguments": {"a": 1}}}]})
    assert [b["type"] for b in blocks] == ["thinking", "text", "tool_use"]
    assert blocks[-1]["input"] == {"a": 1}
    assert _content_from_message({}) == []

def test_error_frame_after_finish_is_empty():
    translator = AnthropicStreamTranslator("m")
    list(translator.finish())
    assert translator.error_frame("x", "api_error") == b""

def test_completion_to_message_shapes():
    payload = completion_to_message({
        "model": "glm-5.2",
        "choices": [{"finish_reason": "stop", "message": {"content": "x"}}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3}})
    assert payload["type"] == "message" and payload["role"] == "assistant"
    assert payload["content"] == [{"type": "text", "text": "x"}]
    assert payload["stop_reason"] == "end_turn"
    assert payload["usage"] == {"input_tokens": 2, "output_tokens": 3}

def test_completion_to_message_tolerates_missing_fields():
    payload = completion_to_message({"choices": [{"message": {}}]})
    assert payload["content"] == []
    assert payload["usage"] == {"input_tokens": 0, "output_tokens": 0}

def test_completion_to_message_maps_cache_read():
    payload = completion_to_message({
        "choices": [{"message": {}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2,
                  "prompt_tokens_details": {"cached_tokens": 6}}})
    assert payload["usage"] == {"input_tokens": 4, "output_tokens": 2,
                                "cache_read_input_tokens": 6}

def test_completion_to_message_ignores_malformed_usage_counts():
    payload = completion_to_message({
        "choices": [{"message": {}}],
        "usage": {"prompt_tokens": True, "completion_tokens": "3",
                  "prompt_tokens_details": 5}})
    assert payload["usage"] == {"input_tokens": 0, "output_tokens": 0}

def test_completion_to_message_ignores_non_int_cached():
    payload = completion_to_message({
        "choices": [{"message": {}}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 1,
                  "prompt_tokens_details": {"cached_tokens": "2"}}})
    assert payload["usage"] == {"input_tokens": 4, "output_tokens": 1}

def test_completion_to_message_parses_arguments():
    payload = completion_to_message({"choices": [{"message": {"tool_calls": [
        {"id": "c1", "function": {"name": "f", "arguments": '{"a": 1}'}},
        {"id": "c2", "function": {"name": "g", "arguments": {}}},
        {"id": "c3", "function": {"name": "h", "arguments": "not-json"}},
        {"id": "c4", "function": {"name": "i", "arguments": "[1,2]"}},
        None,
    ]}}]})
    inputs = [b["input"] for b in payload["content"]]
    assert inputs == [{"a": 1}, {}, {}, {}]

"""覆盖率收尾（OpenAI response 翻译）：工具调用聚合与推理帧边界。"""

from __future__ import annotations

from src.provider.base import Event, EventKind


def test_output_frame_with_non_string_reasoning_is_ignored():
    from src.provider.trae import events as trae_events

    frame = trae_events.SSEFrame(event="output",
                                 data='{"response":"x","reasoning_content":5}')
    assert [e.kind for e in trae_events.parse_all_events(frame)] == [EventKind.CONTENT]

def test_usage_frame_from_sync_parse_keeps_nulls_for_missing_keys():
    from src.provider.trae import events as trae_events

    event = trae_events.parse_frame(trae_events.SSEFrame(event="token_usage", data="{}"))
    assert event.usage == event.usage and event.usage.input_tokens is None

# ------------------------------------------------- response 剩余分支

def test_tool_call_index_bool_is_treated_as_missing():
    from src.compat.openai.response import ToolIndexState

    state = ToolIndexState()
    _, index = state.resolve({"id": "x", "index": True})
    assert index == 0

def test_translator_reasoning_after_role_already_sent():
    from src.compat.openai.response import StreamTranslator

    translator = StreamTranslator("m")
    list(translator.translate(Event(kind=EventKind.CONTENT, content="a")))
    frames = list(translator.translate(Event(kind=EventKind.REASONING, content="r")))
    assert b"reasoning_content" in frames[0]

def test_aggregate_tool_call_without_existing_function_key():
    from src.compat.openai.response import aggregate

    result = aggregate([
        Event(kind=EventKind.TOOL_CALLS, tool_calls=[{"id": "a", "index": 0}]),
        Event(kind=EventKind.TOOL_CALLS,
              tool_calls=[{"id": "a", "index": 0, "function": {"arguments": "{}"}}]),
    ], "m")
    args = result["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    assert args == "{}"

def test_aggregate_tool_call_fragment_when_function_missing():
    """合并分片时 function 缺失 → setdefault 分支（response 155）。"""
    from src.compat.openai.response import aggregate

    result = aggregate([
        Event(kind=EventKind.TOOL_CALLS, tool_calls=[{"id": "a", "index": 0}]),
        Event(kind=EventKind.TOOL_CALLS,
              tool_calls=[{"id": "a", "index": 0,
                           "function": {"arguments": '{"a": 1}'}}]),
    ], "m")
    message = result["choices"][0]["message"]
    assert message["tool_calls"][0]["function"]["arguments"] == '{"a": 1}'

def test_aggregate_reasoning_after_tool_calls():
    """tool_calls 之后再有 reasoning（response 146 分支顺序）。"""
    from src.compat.openai.response import aggregate

    result = aggregate([
        Event(kind=EventKind.TOOL_CALLS, tool_calls=[{"id": "a", "index": 0}]),
        Event(kind=EventKind.REASONING, content="r"),
        Event(kind=EventKind.CONTENT, content="c"),
    ], "m")
    message = result["choices"][0]["message"]
    assert message["content"] == "c" and message["reasoning_content"] == "r"

def test_aggregate_reasoning_before_content_branch_order():
    """聚合里 reasoning 先于 content 出现，覆盖 elif 顺序（response 146→135）。"""
    from src.compat.openai.response import aggregate

    message = aggregate([Event(kind=EventKind.REASONING, content="r"),
                         Event(kind=EventKind.CONTENT, content="c")], "m")["choices"][0]["message"]
    assert message["reasoning_content"] == "r" and message["content"] == "c"

def test_aggregate_tool_call_fragment_non_string_is_ignored():
    """arguments 非字符串 → 不合并（response 155→147）。"""
    from src.compat.openai.response import aggregate

    result = aggregate([
        Event(kind=EventKind.TOOL_CALLS,
              tool_calls=[{"id": "a", "index": 0, "function": {"name": "f"}}]),
        Event(kind=EventKind.TOOL_CALLS,
              tool_calls=[{"id": "a", "index": 0, "function": {"arguments": 5}}]),
    ], "m")
    fn = result["choices"][0]["message"]["tool_calls"][0]["function"]
    assert fn["name"] == "f" and "arguments" not in fn

def test_aggregate_tool_calls_event_with_none_payload():
    """kind 为 TOOL_CALLS 但 tool_calls 为空 → 跳过（response 146→135）。"""
    from src.compat.openai.response import aggregate

    result = aggregate([Event(kind=EventKind.TOOL_CALLS, tool_calls=None),
                        Event(kind=EventKind.CONTENT, content="c")], "m")
    assert result["choices"][0]["message"]["content"] == "c"
    assert "tool_calls" not in result["choices"][0]["message"]

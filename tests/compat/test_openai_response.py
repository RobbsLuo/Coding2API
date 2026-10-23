"""OpenAI 出口适配：流式翻译与非流式聚合。"""

from __future__ import annotations

import json

import pytest

from src.compat.openai.errors import UpstreamStreamError, error_payload
from src.compat.openai.response import StreamTranslator, aggregate
from src.engine.sse import SSE_DONE
from src.provider.base import ErrKind, Event, EventKind, Usage
from src.provider.trae import events as trae_events
from tests.helpers.paths import provider_fixture
from tests.helpers.sse import parse_frames


def test_stream_translator_emits_role_then_content_then_done():
    """首块补 role:assistant（与 codebuddy2api 的 OpenAIStreamNormalizer 语义一致）。"""
    t = StreamTranslator("glm-5.2")
    frames = list(t.translate(Event(kind=EventKind.CONTENT, content="你")))
    frames += list(t.translate(Event(kind=EventKind.FINISH, finish_reason="stop")))
    first = json.loads(frames[0].decode()[6:])
    assert first["choices"][0]["delta"] == {"role": "assistant", "content": "你"}
    assert json.loads(frames[1].decode()[6:])["choices"][0]["finish_reason"] == "stop"
    assert frames[2] == SSE_DONE

def test_stream_translator_usage_is_not_a_frame():
    t = StreamTranslator("m")
    assert list(t.translate(Event(kind=EventKind.USAGE, usage=Usage(1, 2)))) == []

def test_stream_translator_reasoning_and_tools():
    t = StreamTranslator("m")
    list(t.translate(Event(kind=EventKind.CONTENT, content="x")))
    frames = list(t.translate(Event(kind=EventKind.REASONING, content="think")))
    frames += list(t.translate(Event(kind=EventKind.TOOL_CALLS,
                                     tool_calls=[{"id": "c1", "function": {"name": "f"}}])))
    reasoning = json.loads(frames[0].decode()[6:])
    tools = json.loads(frames[1].decode()[6:])
    assert reasoning["choices"][0]["delta"] == {"reasoning_content": "think"}
    assert tools["choices"][0]["delta"]["tool_calls"][0]["index"] == 0

def test_stream_translator_missing_tool_index_gets_stable_position():
    t = StreamTranslator("m")
    list(t.translate(Event(kind=EventKind.CONTENT, content="x")))
    frames = list(t.translate(Event(kind=EventKind.TOOL_CALLS,
                                    tool_calls=[{"id": "a", "function": {}},
                                                {"id": "b", "function": {}}])))
    indices = [tc["index"] for tc in json.loads(frames[0].decode()[6:])
               ["choices"][0]["delta"]["tool_calls"]]
    assert indices == [0, 1]

def test_stream_translator_missing_tool_id_gets_stable_id():
    """`id` 缺失时按 index 补稳定 id：OpenAI 客户端按 id 聚合分片。

    同一个 index 的后续分片必须沿用同一个 id，否则一次调用会被拆成多次。
    """
    t = StreamTranslator("m")
    list(t.translate(Event(kind=EventKind.CONTENT, content="x")))
    first = list(t.translate(Event(kind=EventKind.TOOL_CALLS,
                                   tool_calls=[{"index": 0,
                                                "function": {"name": "f",
                                                             "arguments": "{"}}])))
    second = list(t.translate(Event(kind=EventKind.TOOL_CALLS,
                                    tool_calls=[{"index": 0,
                                                 "function": {"arguments": "}"}}])))
    id_a = json.loads(first[0].decode()[6:])["choices"][0]["delta"]["tool_calls"][0]["id"]
    id_b = json.loads(second[0].decode()[6:])["choices"][0]["delta"]["tool_calls"][0]["id"]
    assert id_a == id_b
    assert id_a.startswith("call_0_")


def test_stream_translator_keeps_upstream_tool_id():
    """上游给了 id 就沿用，不覆盖。"""
    t = StreamTranslator("m")
    frames = list(t.translate(Event(kind=EventKind.TOOL_CALLS,
                                    tool_calls=[{"id": "call_up", "index": 0,
                                                 "function": {"name": "f"}}])))
    assert json.loads(frames[0].decode()[6:])["choices"][0]["delta"][
        "tool_calls"][0]["id"] == "call_up"


def test_stream_translator_error_frame():
    t = StreamTranslator("m")
    frames = list(t.translate(Event(kind=EventKind.ERROR, error_code=1005,
                                    error_message="quota")))
    payload = json.loads(frames[0].decode()[6:])
    assert payload["error"]["code"] == 1005
    assert b"[DONE]" not in frames[0]

def test_stream_translator_finish_without_upstream_done():
    t = StreamTranslator("m")
    frames = list(t.finish())
    assert json.loads(frames[0].decode()[6:])["choices"][0]["finish_reason"] == "stop"
    assert frames[1] == SSE_DONE

def test_stream_translator_finish_after_done_emits_nothing():
    t = StreamTranslator("m")
    list(t.translate(Event(kind=EventKind.FINISH, finish_reason="stop")))
    assert list(t.finish()) == []             # DONE 已随 _close 发出，不重复

def test_stream_translator_empty_delta_is_skipped():
    t = StreamTranslator("m")
    assert list(t.translate(Event(kind=EventKind.CONTENT, content=None))) == []

def test_aggregate_builds_completion_from_fixture_events():
    events = [e for e in (trae_events.parse_frame(f)
                          for f in parse_frames(provider_fixture("trae", "chat-basic.sse"))) if e]
    result = aggregate(events, "glm-5.2")
    choice = result["choices"][0]
    assert choice["message"]["content"] == "中国的首都是北京。"
    assert choice["finish_reason"] == "stop"
    assert result["usage"]["prompt_tokens"] == 21
    assert result["usage"]["total_tokens"] == 163
    assert result["usage"]["completion_tokens_details"]["reasoning_tokens"] == 135

def test_aggregate_merges_tool_call_arguments_and_reasoning():
    events = [
        Event(kind=EventKind.REASONING, content="想"),
        Event(kind=EventKind.TOOL_CALLS,
              tool_calls=[{"id": "c1", "index": 0,
                           "function": {"name": "f", "arguments": '{"a"'}}]),
        Event(kind=EventKind.TOOL_CALLS,
              tool_calls=[{"id": "c1", "index": 0, "function": {"arguments": ":1}"}}]),
        Event(kind=EventKind.FINISH, finish_reason="tool_calls"),
    ]
    message = aggregate(events, "m")["choices"][0]["message"]
    assert message["content"] is None
    assert message["reasoning_content"] == "想"
    assert message["tool_calls"][0]["function"]["arguments"] == '{"a":1}'

def test_aggregate_without_usage_reports_nulls():
    result = aggregate([Event(kind=EventKind.CONTENT, content="x")], "m")
    assert result["usage"]["prompt_tokens"] is None
    assert result["usage"]["total_tokens"] is None

def test_aggregate_raises_on_stream_error():
    with pytest.raises(UpstreamStreamError):
        aggregate([Event(kind=EventKind.ERROR, error_code=1005, error_message="quota")], "m")

def test_upstream_stream_error_kind_uses_event_classification():
    """流内业务错误经 `.kind()` 暴露真实分类。

    此前没有这个方法，executor 的分类器（只认 `.kind()`）兜底成
    `upstream_protocol`，界面显示「渠道响应异常」，真实类型被吞掉。
    """
    error = UpstreamStreamError(Event(
        kind=EventKind.ERROR, error_code=1005, error_kind=ErrKind.PLAN))
    assert error.kind() is ErrKind.PLAN

def test_upstream_stream_error_kind_defaults_to_other():
    """未带业务分类的流内错误回落 OTHER（与 executor 的兜底同口径）。"""
    error = UpstreamStreamError(Event(kind=EventKind.ERROR, error_message="boom"))
    assert error.kind() is ErrKind.OTHER

def test_error_payload_shape():
    payload = error_payload("msg", "code", 400)
    assert payload["error"]["message"] == "msg" and payload["error"]["status"] == 400

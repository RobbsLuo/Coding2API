"""B2.1：OpenAI Responses 出口（`POST /v1/responses`，Codex CLI）的契约测试。

三块：
1. 入站映射 `src/compat/responses/request.py`——Responses 请求体 → chat 载荷。
   形状依据官方 openai-python 类型与 openai/codex 源码（见各用例 docstring）。
2. 出口翻译 `src/compat/responses/response.py`——中立 Event → Responses SSE 帧。
3. 端点契约（流式 / 非流式 / 工具调用 / 不支持项 400 / 未知模型 400）。

所有断言都对着官方 SDK 能解析的形状写；端到端已另外用 `openai` Python SDK
与真实上游冒烟核对过（见 TECHNICAL §3.7）。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src.compat.openai.request import InvalidRequest
from src.compat.responses.request import parse_responses_request
from src.compat.responses.response import (
    ResponsesStreamTranslator,
    completion_to_response,
)
from src.config import Settings
from src.main import build_app
from src.provider.base import Event, EventKind, Quota, Usage
from tests.conftest import SECRET


def _events(frame: bytes) -> tuple[str, dict]:
    text = frame.decode()
    lines = [line for line in text.split("\n") if line]
    assert lines[0].startswith("event: "), text
    return lines[0][7:], json.loads(lines[1][6:])


def _frames(chunks: list[bytes]) -> list[tuple[str, dict]]:
    """把若干字节块里的**全部** SSE 帧摊平（一个块可能含多帧）。"""
    parsed: list[tuple[str, dict]] = []
    for chunk in chunks:
        lines = chunk.decode().split("\n")
        for index, line in enumerate(lines):
            if line.startswith("event: "):
                parsed.append((line[7:], json.loads(lines[index + 1][6:])))
    return parsed


# ------------------------------------------------------------- 入站映射


def test_maps_instructions_and_string_input():
    request = parse_responses_request({
        "model": "m", "instructions": "be brief", "input": "hi"})
    assert request.raw["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
    ]
    assert request.raw["model"] == "m"
    assert request.raw["stream"] is False


def test_maps_message_items_and_developer_role():
    request = parse_responses_request({
        "input": [
            {"type": "message", "role": "developer",
             "content": [{"type": "input_text", "text": "rules"}]},
            {"role": "user", "content": "plain string content"},
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "answer"}]},
        ]})
    assert request.raw["messages"] == [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "plain string content"},
        {"role": "assistant", "content": "answer"},
    ]


def test_maps_function_call_and_output_history():
    """Codex 的 FunctionCallOutputPayload 线上是 {content} / {content_items}。"""
    request = parse_responses_request({"input": [
        {"type": "function_call", "name": "shell", "arguments": '{"a":1}',
         "call_id": "call_1"},
        {"type": "function_call_output", "call_id": "call_1",
         "output": {"content": "done"}},
        {"type": "function_call_output", "call_id": "call_2",
         "output": {"content_items": [{"type": "output_text", "text": "part"}]}},
        {"type": "function_call_output", "call_id": "call_3", "output": "raw"},
        {"type": "function_call_output", "call_id": "call_4"},
    ]})
    messages = request.raw["messages"]
    assert messages[0] == {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "call_1", "type": "function",
                        "function": {"name": "shell", "arguments": '{"a":1}'}}],
    }
    assert messages[1] == {"role": "tool", "tool_call_id": "call_1", "content": "done"}
    assert messages[2] == {"role": "tool", "tool_call_id": "call_2", "content": "part"}
    assert messages[3] == {"role": "tool", "tool_call_id": "call_3", "content": "raw"}
    assert messages[4] == {"role": "tool", "tool_call_id": "call_4", "content": ""}


def test_function_call_falls_back_to_id_and_empty_arguments():
    request = parse_responses_request({"input": [
        {"type": "function_call", "name": "f", "id": "fc_9"}]})
    call = request.raw["messages"][0]["tool_calls"][0]
    assert call["id"] == "fc_9"
    assert call["function"]["arguments"] == ""


def test_reasoning_history_becomes_reasoning_content():
    request = parse_responses_request({"input": [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "a"}],
         "content": [{"type": "reasoning_text", "text": "b"}]},
        {"type": "message", "role": "user", "content": "go"},
    ]})
    assert request.raw["messages"][0] == {
        "role": "assistant", "content": "", "reasoning_content": "ab"}


def test_reasoning_and_compaction_without_text_are_dropped():
    """Codex 每轮回传 encrypted_content 的 reasoning/compaction：无声丢弃。"""
    request = parse_responses_request({"input": [
        {"type": "reasoning", "summary": [], "content": None,
         "encrypted_content": "gAAAA"},
        {"type": "compaction", "encrypted_content": "gAAAA"},
        {"type": "message", "role": "user", "content": "hi"},
    ]})
    assert request.raw["messages"] == [{"role": "user", "content": "hi"}]


def test_maps_tools_tool_choice_and_sampling():
    request = parse_responses_request({
        "input": "hi", "temperature": 0.3, "top_p": 0.9,
        "max_output_tokens": 128, "reasoning": {"effort": "high"},
        "prompt_cache_key": "k1", "text": {"verbosity": "low"},
        "parallel_tool_calls": False,
        "tools": [{"type": "function", "name": "f", "description": "d",
                   "parameters": {"type": "object"}}],
        "tool_choice": {"type": "function", "name": "f"},
    })
    raw = request.raw
    assert raw["temperature"] == 0.3 and raw["top_p"] == 0.9
    assert raw["max_tokens"] == 128
    assert raw["reasoning_effort"] == "high"
    assert raw["prompt_cache_key"] == "k1"
    assert raw["verbosity"] == "low"
    assert raw["parallel_tool_calls"] is False
    assert raw["tools"] == [{"type": "function",
                             "function": {"name": "f", "description": "d",
                                          "parameters": {"type": "object"}}}]
    assert raw["tool_choice"] == {"type": "function", "function": {"name": "f"}}


def test_tool_choice_strings_pass_through():
    for value in ("auto", "none", "required"):
        parsed = parse_responses_request({"input": "hi", "tool_choice": value})
        assert parsed.raw["tool_choice"] == value


def test_empty_tools_are_omitted():
    request = parse_responses_request({"input": "hi", "tools": []})
    assert "tools" not in request.raw


def test_include_only_encrypted_reasoning_allowed():
    """Codex CLI 每轮都发 include=["reasoning.encrypted_content"]。"""
    request = parse_responses_request(
        {"input": "hi", "include": ["reasoning.encrypted_content"]})
    assert "include" not in request.raw


def test_stream_flag_flows_through():
    assert parse_responses_request({"input": "hi", "stream": True}).stream is True


@pytest.mark.parametrize(("body", "fragment"), [
    (None, "JSON object"),
    ({}, "input must be a string or an array"),
    ({"input": 5}, "input must be a string or an array"),
    ({"input": []}, "at least one message"),
    ({"input": [{"type": "reasoning"}]}, "at least one message"),
    ({"input": "hi", "store": True}, "store=true"),
    ({"input": "hi", "previous_response_id": "resp_1"}, "previous_response_id"),
    ({"input": "hi", "background": True}, "background=true"),
    ({"input": "hi", "include": "x"}, "include must be an array"),
    ({"input": "hi", "include": ["other"]}, "include ['other']"),
    ({"input": "hi", "model": 5}, "model must be a string"),
    ({"input": "hi", "stream": "yes"}, "stream must be a boolean"),
    ({"input": "hi", "messages": "x", "tools": {}}, "tools must be an array"),
    ({"input": "hi", "tools": [{"type": "web_search"}]}, "web_search"),
    ({"input": "hi", "tools": [5]}, "tools[0] must be an object"),
    ({"input": "hi", "tools": [{"type": "function"}]}, "name must be a non-empty"),
    ({"input": "hi", "tool_choice": {"type": "mcp"}}, "tool_choice form"),
    ({"input": "hi", "tool_choice": {"type": "function"}}, "tool_choice.name"),
    ({"input": [5]}, "input items must be objects"),
    ({"input": [{"type": "local_shell_call"}]}, "local_shell_call"),
    ({"input": [{"type": "custom_tool_call"}]}, "custom_tool_call"),
    ({"input": [{"type": "nope"}]}, "unknown input item type"),
    ({"input": [{"type": "message", "role": "tool", "content": "x"}]},
     "role 'tool'"),
    ({"input": [{"type": "message", "role": "user", "content": {"a": 1}}]},
     "content must be a string or an array"),
    ({"input": [{"type": "message", "role": "user", "content": [5]}]},
     "content[0] must be an object"),
    ({"input": [{"type": "message", "role": "user",
                 "content": [{"type": "input_image", "image_url": "x"}]}]},
     "input_image"),
    ({"input": [{"type": "message", "role": "user",
                 "content": [{"type": "input_text"}]}]}, "text must be a string"),
    ({"input": [{"type": "message", "role": "user", "content": [{"type": "z"}]}]},
     "unknown content part type"),
    ({"input": [{"type": "function_call", "arguments": "x"}]},
     "name must be a non-empty"),
    ({"input": [{"type": "function_call", "name": "f", "arguments": 5}]},
     "arguments must be a string"),
    ({"input": [{"type": "function_call_output", "output": "x"}]},
     "call_id must be a non-empty"),
    ({"input": [{"type": "function_call_output", "call_id": "c", "output": 5}]},
     "output must be a string, object or array"),
    ({"instructions": 5, "input": "hi"}, "content must be a string or an array"),
])
def test_rejected_bodies(body, fragment):
    with pytest.raises(InvalidRequest, match=fragment.replace("[", r"\[")):
        parse_responses_request(body)


def test_function_call_output_accepts_array_and_empty_object():
    request = parse_responses_request({"input": [
        {"type": "function_call_output", "call_id": "c1",
         "output": [{"type": "input_text", "text": "arr"}]},
        {"type": "function_call_output", "call_id": "c2", "output": {}},
    ]})
    assert request.raw["messages"][0]["content"] == "arr"
    assert request.raw["messages"][1]["content"] == ""


def test_reasoning_with_malformed_blocks_is_dropped():
    request = parse_responses_request({"input": [
        {"type": "reasoning", "summary": [5], "content": {"nope": 1}},
        {"type": "message", "role": "user", "content": "hi"},
    ]})
    assert request.raw["messages"] == [{"role": "user", "content": "hi"}]


def test_empty_instructions_are_omitted():
    request = parse_responses_request({"instructions": "", "input": "hi"})
    assert request.raw["messages"] == [{"role": "user", "content": "hi"}]


def test_tool_without_description_or_parameters():
    request = parse_responses_request({"input": "hi", "tools": [
        {"type": "function", "name": "bare", "description": 5, "parameters": "x"}]})
    assert request.raw["tools"] == [{"type": "function",
                                     "function": {"name": "bare"}}]


def test_ignore_unknown_extra_fields():
    """Responses 私有键（client_metadata 等）不进上游载荷，也不报错。"""
    request = parse_responses_request({
        "input": "hi", "client_metadata": {"a": "b"}, "service_tier": "auto",
        "stream_options": {"x": 1}})
    for key in ("client_metadata", "service_tier", "stream_options", "store",
                "include", "input", "instructions"):
        assert key not in request.raw


# ------------------------------------------------------------- 出口翻译


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


# ------------------------------------------------------------- 端点契约


class _Provider:
    id = "codebuddy"

    def __init__(self, scenario: str = "text") -> None:
        self.scenario = scenario
        self.last_payload: dict = {}
        self.calls = 0

    async def stream_chat(self, credential_data, payload, model):
        self.calls += 1
        self.last_payload = payload
        if self.scenario == "tools":
            yield Event(kind=EventKind.TOOL_CALLS, tool_calls=[
                {"index": 0, "id": "call_9", "type": "function",
                 "function": {"name": "f", "arguments": '{"a":1}'}}])
            yield Event(kind=EventKind.FINISH, finish_reason="tool_calls")
            return
        yield Event(kind=EventKind.CONTENT, content="hi there")
        yield Event(kind=EventKind.USAGE,
                    usage=Usage(input_tokens=2, output_tokens=3))
        yield Event(kind=EventKind.FINISH, finish_reason="stop")

    async def probe_quota(self, credential_data):
        return Quota(remaining=10, total=10)

    async def list_models(self, credential_data):
        return []

    async def refresh(self, credential_data):
        return credential_data

    async def aclose(self):
        return None


def _app(tmp_path, provider=None):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"codebuddy": provider or _Provider()})
    app.state.credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    return app


def _auth(app) -> dict:
    return {"Authorization": f"Bearer {app.state.api_keys.create('root')['api_key']}"}


def test_endpoint_stream_events(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/v1/responses", headers=_auth(app), json={
            "model": "glm-5.2@codebuddy", "instructions": "sys", "input": "hi",
            "stream": True, "max_output_tokens": 32,
        })
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    names = [line[7:] for line in response.text.split("\n") if line.startswith("event: ")]
    assert names[0] == "response.created"
    assert names[-1] == "response.completed"
    assert "response.output_text.delta" in names


def test_endpoint_non_stream(tmp_path):
    provider = _Provider()
    app = _app(tmp_path, provider)
    with TestClient(app) as client:
        response = client.post("/v1/responses", headers=_auth(app),
                               json={"model": "glm-5.2@codebuddy", "input": "hi"})
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["output"][0]["content"][0]["text"] == "hi there"
    assert body["usage"]["total_tokens"] == 5
    assert provider.last_payload["messages"] == [{"role": "user", "content": "hi"}]


def test_endpoint_tool_call_stream(tmp_path):
    provider = _Provider("tools")
    app = _app(tmp_path, provider)
    with TestClient(app) as client:
        response = client.post("/v1/responses", headers=_auth(app), json={
            "model": "glm-5.2@codebuddy", "input": "hi", "stream": True,
            "tools": [{"type": "function", "name": "f",
                       "parameters": {"type": "object"}}],
        })
    assert "response.function_call_arguments.done" in response.text
    item = json.loads([line[6:] for line in response.text.split("\n")
                       if line.startswith("data: ") and '"function_call"' in line][0])
    assert item["item"]["name"] == "f"


def test_endpoint_unsupported_param_400(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/v1/responses", headers=_auth(app),
                               json={"model": "glm-5.2@codebuddy", "input": "hi",
                                     "store": True})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert "store=true" in response.json()["error"]["message"]


def test_endpoint_unknown_provider_400(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/v1/responses", headers=_auth(app),
                               json={"model": "glm-5.2@nope", "input": "hi",
                                     "stream": True})
    assert response.status_code == 400
    assert "unknown provider" in response.json()["error"]["message"]


def test_endpoint_requires_api_key(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/v1/responses", json={"model": "m", "input": "hi"})
    assert response.status_code == 401


def test_endpoint_no_healthy_credential_returns_503(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"codebuddy": _Provider()})
    with TestClient(app) as client:
        response = client.post("/v1/responses", headers=_auth(app),
                               json={"model": "glm-5.2@codebuddy", "input": "hi"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "no_healthy_credential"


def test_stream_error_frame_prefers_translator_then_falls_back():
    """`_stream_error_frame`：translator 有 error_frame 就用它，否则 chat 兜底。"""
    from src.engine.executor import _stream_error_frame

    class _WithFrame:
        def error_frame(self, message, code):
            return f"custom:{code}".encode()

    class _WithoutFrame:
        pass

    assert _stream_error_frame(_WithFrame(), "m", "c") == b"custom:c"
    fallback = _stream_error_frame(_WithoutFrame(), "m", "c")
    assert b'"code": "c"' in fallback
    assert fallback.endswith(b"data: [DONE]\n\n")
    assert _stream_error_frame(None, "m", "c") == fallback


def test_endpoint_malformed_json_400(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/v1/responses", headers=_auth(app),
                               content=b"{not json",
                               headers_extra=None) if False else client.post(
            "/v1/responses", content=b"{not json",
            headers={**_auth(app), "Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_stream_midway_rejection_emits_failed_event(tmp_path):
    """流内 4001（模型不属于该上游）：400 语义以 response.failed 事件表达。"""

    class Rejecting(_Provider):
        async def stream_chat(self, credential_data, payload, model):
            yield Event(kind=EventKind.ERROR, error_code=4001,
                        error_message="model not available")

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"codebuddy": Rejecting()})
    app.state.credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    with TestClient(app) as client:
        response = client.post("/v1/responses", headers=_auth(app),
                               json={"model": "glm-5.2@codebuddy", "input": "hi",
                                     "stream": True})
    assert response.status_code == 200
    assert "response.failed" in response.text
    assert '"invalid_request"' in response.text


def test_endpoint_model_alias_and_session_indicator(tmp_path):
    """prompt_cache_key 透传到上游，供会话粘性使用（B1.5）。"""
    provider = _Provider()
    app = _app(tmp_path, provider)
    with TestClient(app) as client:
        client.post("/v1/responses", headers=_auth(app), json={
            "model": "glm-5.2@codebuddy", "input": "hi",
            "prompt_cache_key": "thread-1"})
    assert provider.last_payload["prompt_cache_key"] == "thread-1"

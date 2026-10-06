"""P0-1：Anthropic Messages 出口（`/v1/messages`）契约测试。

三块：
1. 入站映射 `src/compat/anthropic/request.py`——Anthropic 请求体 → chat 载荷。
2. 出口翻译 `src/compat/anthropic/response.py`——中立 Event → Anthropic SSE 帧。
3. 端点契约（流式 / 非流式 / 工具调用 / count_tokens / x-api-key / 400）。

形状依据 Anthropic Messages API（anthropic-python SDK 类型）。本服务只实现
chat 子集，不支持项显式 400。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src.compat.anthropic.request import parse_messages_request
from src.compat.anthropic.response import (
    AnthropicStreamTranslator,
    completion_to_message,
)
from src.compat.openai.request import InvalidRequest
from src.config import Settings
from src.main import build_app
from src.provider.base import Event, EventKind, Quota, Usage
from tests.conftest import SECRET


def _events(text: str) -> list[tuple[str, dict]]:
    """SSE 文本 → [(event_name, data_dict), ...]。"""
    out: list[tuple[str, dict]] = []
    for block in text.split("\n\n"):
        lines = [line for line in block.split("\n") if line]
        name = ""
        data = ""
        for line in lines:
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if name and data:
            out.append((name, json.loads(data)))
    return out


# --------------------------------------------------------------- 入站映射

def _body(**overrides) -> dict:
    body = {"model": "glm-5.2", "max_tokens": 64, "messages": [{"role": "user",
                                                                "content": "hi"}]}
    body.update(overrides)
    return body


def test_parse_requires_json_object():
    with pytest.raises(InvalidRequest, match="JSON object"):
        parse_messages_request([])


def test_parse_requires_model_string():
    with pytest.raises(InvalidRequest, match="model must be a string"):
        parse_messages_request(_body(model=5))


def test_parse_requires_non_empty_messages():
    with pytest.raises(InvalidRequest, match="non-empty array"):
        parse_messages_request(_body(messages=[]))
    with pytest.raises(InvalidRequest, match="non-empty array"):
        parse_messages_request(_body(messages="hi"))


def test_parse_requires_boolean_stream():
    with pytest.raises(InvalidRequest, match="stream must be a boolean"):
        parse_messages_request(_body(stream="yes"))


def test_parse_system_string_and_list():
    system = parse_messages_request(_body(system="sys"))
    assert system.messages[0] == {"role": "system", "content": "sys"}
    blocks = parse_messages_request(_body(system=[
        {"type": "text", "text": "a"}, {"type": "text", "text": "b"}]))
    assert blocks.messages[0] == {"role": "system", "content": "ab"}


def test_parse_system_rejects_bad_shapes():
    with pytest.raises(InvalidRequest, match="system\\[0\\] must be an object"):
        parse_messages_request(_body(system=["x"]))
    with pytest.raises(InvalidRequest, match="not supported"):
        parse_messages_request(_body(system=[{"type": "image"}]))
    with pytest.raises(InvalidRequest, match="text must be a string"):
        parse_messages_request(_body(system=[{"type": "text", "text": 1}]))
    with pytest.raises(InvalidRequest, match="string or an array"):
        parse_messages_request(_body(system=5))


def test_parse_empty_system_is_dropped():
    parsed = parse_messages_request(_body(system=""))
    assert parsed.messages[0]["role"] == "user"


def test_parse_string_content_by_role():
    parsed = parse_messages_request(_body(messages=[
        {"role": "user", "content": "u"}, {"role": "assistant", "content": "a"}]))
    assert [m["role"] for m in parsed.messages] == ["user", "assistant"]


def test_parse_rejects_unknown_role_and_bad_message():
    with pytest.raises(InvalidRequest, match="role 'system' is not supported"):
        parse_messages_request(_body(messages=[{"role": "system", "content": "x"}]))
    # 数组 content 下的非法 role 走另一分支
    with pytest.raises(InvalidRequest, match="role 'system' is not supported"):
        parse_messages_request(_body(messages=[{"role": "system", "content": [
            {"type": "text", "text": "x"}]}]))
    with pytest.raises(InvalidRequest, match=r"messages\[0\] must be an object"):
        parse_messages_request(_body(messages=["x"]))
    with pytest.raises(InvalidRequest, match="string or an array"):
        parse_messages_request(_body(messages=[{"role": "user", "content": 5}]))


def test_parse_user_blocks_split_text_and_tool_results():
    parsed = parse_messages_request(_body(messages=[{"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "tool_result", "tool_use_id": "t1", "content": "res"},
        {"type": "text", "text": "again"},
    ]}]))
    assert parsed.messages == [
        {"role": "user", "content": "look"},
        {"role": "tool", "tool_call_id": "t1", "content": "res"},
        {"role": "user", "content": "again"},
    ]


def test_parse_tool_result_text_shapes():
    parsed = parse_messages_request(_body(messages=[{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1",
         "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]},
        {"type": "tool_result", "tool_use_id": "t2"},
    ]}]))
    assert parsed.messages[0]["content"] == "ab"
    assert parsed.messages[1]["content"] == ""


def test_parse_user_blocks_reject_bad_entries():
    with pytest.raises(InvalidRequest, match=r"content\[0\] must be an object"):
        parse_messages_request(_body(messages=[{"role": "user", "content": [1]}]))
    with pytest.raises(InvalidRequest, match="text must be a string"):
        parse_messages_request(_body(messages=[{"role": "user", "content": [
            {"type": "text", "text": 1}]}]))
    with pytest.raises(InvalidRequest, match="tool_use_id must be a non-empty"):
        parse_messages_request(_body(messages=[{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": ""}]}]))
    with pytest.raises(InvalidRequest, match="not supported"):
        parse_messages_request(_body(messages=[{"role": "user", "content": [
            {"type": "image"}]}]))
    with pytest.raises(InvalidRequest, match="unknown content block type"):
        parse_messages_request(_body(messages=[{"role": "user", "content": [
            {"type": "bogus"}]}]))


def test_parse_tool_result_content_bad_shapes():
    with pytest.raises(InvalidRequest, match=r"content\[0\] must be an object"):
        parse_messages_request(_body(messages=[{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "content": [1]}]}]))
    with pytest.raises(InvalidRequest, match="not supported"):
        parse_messages_request(_body(messages=[{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t",
             "content": [{"type": "image"}]}]}]))
    with pytest.raises(InvalidRequest, match="unknown tool_result.content type"):
        parse_messages_request(_body(messages=[{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t",
             "content": [{"type": "bogus"}]}]}]))
    with pytest.raises(InvalidRequest, match="string or an array"):
        parse_messages_request(_body(messages=[{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "content": 5}]}]))


def test_parse_assistant_blocks_merge_into_one_message():
    parsed = parse_messages_request(_body(messages=[{"role": "assistant", "content": [
        {"type": "thinking", "thinking": "th"},
        {"type": "text", "text": "a"},
        {"type": "redacted_thinking", "data": "x"},
        {"type": "tool_use", "id": "t1", "name": "f", "input": {"x": 1}},
    ]}]))
    assert parsed.messages == [{
        "role": "assistant", "content": "a", "reasoning_content": "th",
        "tool_calls": [{"id": "t1", "type": "function",
                        "function": {"name": "f", "arguments": '{"x": 1}'}}]}]


def test_parse_assistant_empty_is_dropped():
    parsed = parse_messages_request(_body(messages=[
        {"role": "assistant", "content": [{"type": "redacted_thinking", "data": "x"}]},
        {"role": "user", "content": "hi"}]))
    assert parsed.messages == [{"role": "user", "content": "hi"}]


def test_parse_assistant_rejects_bad_blocks():
    with pytest.raises(InvalidRequest, match=r"content\[0\] must be an object"):
        parse_messages_request(_body(messages=[{"role": "assistant", "content": [1]}]))
    with pytest.raises(InvalidRequest, match="thinking must be a string"):
        parse_messages_request(_body(messages=[{"role": "assistant", "content": [
            {"type": "thinking", "thinking": 1}]}]))
    with pytest.raises(InvalidRequest, match="tool_use.name must be a non-empty"):
        parse_messages_request(_body(messages=[{"role": "assistant", "content": [
            {"type": "tool_use", "id": "t", "name": ""}]}]))
    with pytest.raises(InvalidRequest, match="unknown content block type"):
        parse_messages_request(_body(messages=[{"role": "assistant", "content": [
            {"type": "bogus"}]}]))


def test_parse_tool_use_input_shapes():
    def args(value):
        return parse_messages_request(_body(messages=[{"role": "assistant", "content": [
            {"type": "tool_use", "id": "t", "name": "f", "input": value}]}])) \
            .messages[0]["tool_calls"][0]["function"]["arguments"]

    assert args(None) == ""
    assert args({"a": 1}) == '{"a": 1}'
    assert args('{"a":1}') == '{"a":1}'
    with pytest.raises(InvalidRequest, match="input must be an object"):
        args(5)


def test_parse_tools_mapping_and_validation():
    parsed = parse_messages_request(_body(tools=[
        {"name": "f", "description": "d", "input_schema": {"type": "object"}}]))
    assert parsed.raw["tools"] == [{"type": "function", "function": {
        "name": "f", "description": "d", "parameters": {"type": "object"}}}]
    # 无 description / input_schema：只保留 name（不写 None）
    bare = parse_messages_request(_body(tools=[{"name": "f"}]))
    assert bare.raw["tools"][0]["function"] == {"name": "f"}
    assert "description" not in bare.raw["tools"][0]["function"]
    # 空工具列表不写入 upstream
    assert "tools" not in parse_messages_request(_body(tools=[])).raw
    with pytest.raises(InvalidRequest, match="tools must be an array"):
        parse_messages_request(_body(tools="x"))
    with pytest.raises(InvalidRequest, match=r"tools\[0\] must be an object"):
        parse_messages_request(_body(tools=[1]))
    with pytest.raises(InvalidRequest, match=r"tools\[0\].name must be a non-empty"):
        parse_messages_request(_body(tools=[{"name": ""}]))


def test_parse_assistant_reasoning_only_and_tools_only():
    reasoning = parse_messages_request(_body(messages=[{"role": "assistant", "content": [
        {"type": "thinking", "thinking": "th"}]}]))
    assert reasoning.messages[0] == {"role": "assistant", "content": "",
                                     "reasoning_content": "th"}
    tools = parse_messages_request(_body(messages=[{"role": "assistant", "content": [
        {"type": "tool_use", "id": "t", "name": "f", "input": {}}]}]))
    assert tools.messages[0]["content"] == "" and "reasoning_content" not in tools.messages[0]


def test_parse_drops_all_messages_when_only_redacted_thinking():
    with pytest.raises(InvalidRequest, match="at least one message"):
        parse_messages_request(_body(messages=[
            {"role": "assistant", "content": [{"type": "redacted_thinking"}]},
            {"role": "assistant", "content": [{"type": "redacted_thinking"}]}]))


def test_parse_tool_choice_mapping():
    def choice(value):
        return parse_messages_request(_body(tool_choice=value)).raw["tool_choice"]

    assert choice({"type": "auto"}) == "auto"
    assert choice({"type": "any"}) == "required"
    assert choice({"type": "none"}) == "none"
    assert choice({"type": "tool", "name": "f"}) == {
        "type": "function", "function": {"name": "f"}}
    assert "tool_choice" not in parse_messages_request(_body()).raw


def test_parse_tool_choice_rejects_bad_shapes():
    with pytest.raises(InvalidRequest, match="tool_choice must be an object"):
        parse_messages_request(_body(tool_choice="auto"))
    with pytest.raises(InvalidRequest, match="tool_choice.name must be a non-empty"):
        parse_messages_request(_body(tool_choice={"type": "tool", "name": ""}))
    with pytest.raises(InvalidRequest, match="tool_choice type"):
        parse_messages_request(_body(tool_choice={"type": "bogus"}))


def test_parse_passthrough_fields():
    parsed = parse_messages_request(_body(max_tokens=10, temperature=0.5, top_p=0.9,
                                          stop_sequences=["x"], top_k=5))
    assert parsed.raw["max_tokens"] == 10
    assert parsed.raw["temperature"] == 0.5 and parsed.raw["top_p"] == 0.9
    assert parsed.raw["stop"] == ["x"]
    assert "top_k" not in parsed.raw            # chat 无等价物，丢弃
    assert "max_tokens" not in parse_messages_request(
        _body(max_tokens=None)).raw


def test_parse_rejects_bad_max_tokens_and_stop_sequences():
    with pytest.raises(InvalidRequest, match="max_tokens must be an integer"):
        parse_messages_request(_body(max_tokens="x"))
    with pytest.raises(InvalidRequest, match="max_tokens must be an integer"):
        parse_messages_request(_body(max_tokens=True))
    with pytest.raises(InvalidRequest, match="stop_sequences must be an array"):
        parse_messages_request(_body(stop_sequences="x"))


def test_parse_rejects_out_of_range_and_non_finite_numbers():
    """M4：负数/超大 max_tokens、NaN/Inf 采样参数在入站即 400，不透传给上游。"""
    with pytest.raises(InvalidRequest, match="between 1 and"):
        parse_messages_request(_body(max_tokens=-1))
    with pytest.raises(InvalidRequest, match="between 1 and"):
        parse_messages_request(_body(max_tokens=10**9))
    with pytest.raises(InvalidRequest, match="must be a finite number"):
        parse_messages_request(_body(temperature=float("nan")))
    with pytest.raises(InvalidRequest, match="must be a number"):
        parse_messages_request(_body(top_p="hot"))
    with pytest.raises(InvalidRequest, match="temperature must be <="):
        parse_messages_request(_body(temperature=99999))
    with pytest.raises(InvalidRequest, match="array of strings"):
        parse_messages_request(_body(stop_sequences=["ok", 1]))


# --------------------------------------------------------------- 出口翻译

def _drain(*events: Event, model: str = "glm-5.2") -> tuple[AnthropicStreamTranslator, list[str]]:
    translator = AnthropicStreamTranslator(model)
    frames: list[bytes] = []
    for event in events:
        frames.extend(translator.translate(event))
    names = [f.decode().split("\n", 1)[0][7:] for f in frames]
    return translator, names


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


def test_translator_keepalive_is_comment_frame():
    from src.engine.sse import SSE_COMMENT

    assert AnthropicStreamTranslator("m").keepalive() == SSE_COMMENT


def test_translator_stop_reason_mapping():
    from src.compat.anthropic.response import _stop_reason

    assert _stop_reason("length") == "max_tokens"
    assert _stop_reason("tool_calls") == "tool_use"
    assert _stop_reason("content_filter") == "end_turn"
    assert _stop_reason("weird") == "end_turn"


def test_translator_usage_payload_none():
    from src.compat.anthropic.response import _usage_payload

    assert _usage_payload(None) == {"input_tokens": 0, "output_tokens": 0}


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


def _drain_names(*events: Event) -> list[str]:
    translator = AnthropicStreamTranslator("m")
    frames: list[bytes] = []
    for event in events:
        frames.extend(translator.translate(event))
    return [f.decode().split("\n", 1)[0][7:] for f in frames]


def test_content_from_message_covers_all_block_kinds():
    from src.compat.anthropic.response import _content_from_message

    blocks = _content_from_message({
        "content": "x", "reasoning_content": "r",
        "tool_calls": [None, {"id": "c", "function": {"arguments": {"a": 1}}}]})
    assert [b["type"] for b in blocks] == ["thinking", "text", "tool_use"]
    assert blocks[-1]["input"] == {"a": 1}
    assert _content_from_message({}) == []


def test_parse_arguments_shapes():
    from src.compat.anthropic.response import _parse_arguments

    assert _parse_arguments({"a": 1}) == {"a": 1}
    assert _parse_arguments("") == {}
    assert _parse_arguments(None) == {}
    assert _parse_arguments("not-json") == {}
    assert _parse_arguments("[1,2]") == {}


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
        yield Event(kind=EventKind.USAGE, usage=Usage(input_tokens=2, output_tokens=3))
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
    return {"x-api-key": app.state.api_keys.create("root")["api_key"]}


def _messages(**overrides) -> dict:
    body = {"model": "glm-5.2@codebuddy", "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}]}
    body.update(overrides)
    return body


def test_endpoint_stream_events(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/v1/messages", headers=_auth(app),
                               json=_messages(stream=True))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _events(response.text)
    names = [name for name, _ in events]
    assert names[0] == "message_start"
    assert names[-1] == "message_stop"
    assert any(delta["delta"]["type"] == "text_delta"
               for name, delta in events if name == "content_block_delta")


def test_endpoint_stream_tool_use(tmp_path):
    app = _app(tmp_path, _Provider("tools"))
    with TestClient(app) as client:
        response = client.post("/v1/messages", headers=_auth(app),
                               json=_messages(stream=True, tools=[
                                   {"name": "f", "input_schema": {"type": "object"}}]))
    assert "tool_use" in response.text
    assert "input_json_delta" in response.text
    assert '"stop_reason": "tool_use"' in response.text


def test_endpoint_non_stream(tmp_path):
    provider = _Provider()
    app = _app(tmp_path, provider)
    with TestClient(app) as client:
        response = client.post("/v1/messages", headers=_auth(app), json=_messages())
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "message"
    assert body["content"] == [{"type": "text", "text": "hi there"}]
    assert body["usage"] == {"input_tokens": 2, "output_tokens": 3}
    assert provider.last_payload["messages"][0] == {"role": "user", "content": "hi"}


def test_endpoint_accepts_bearer_fallback(tmp_path):
    app = _app(tmp_path)
    token = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        response = client.post("/v1/messages",
                               headers={"Authorization": f"Bearer {token}"},
                               json=_messages())
    assert response.status_code == 200


def test_endpoint_requires_api_key(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        assert client.post("/v1/messages", json=_messages()).status_code == 401


def test_endpoint_rejects_unsupported_block(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/v1/messages", headers=_auth(app), json=_messages(
            messages=[{"role": "user", "content": [{"type": "image"}]}]))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_endpoint_unknown_provider_400(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/v1/messages", headers=_auth(app),
                               json=_messages(model="glm-5.2@nope", stream=True))
    assert response.status_code == 400
    assert "unknown provider" in response.json()["error"]["message"]


def test_endpoint_no_healthy_credential_503(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"codebuddy": _Provider()})
    with TestClient(app) as client:
        response = client.post("/v1/messages", headers=_auth(app), json=_messages())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "no_healthy_credential"


def test_endpoint_model_allowlist_blocks(tmp_path):
    app = _app(tmp_path)
    token = app.state.api_keys.create("root", allowed_models="glm-*")["api_key"]
    headers = {"x-api-key": token}
    with TestClient(app) as client:
        blocked = client.post("/v1/messages", headers=headers,
                              json=_messages(model="kimi-k3@codebuddy"))
        allowed = client.post("/v1/messages", headers=headers, json=_messages())
    assert blocked.status_code == 400
    assert "not allowed" in blocked.json()["error"]["message"]
    assert allowed.status_code == 200


def test_endpoint_count_tokens(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/v1/messages/count_tokens", headers=_auth(app),
                               json=_messages())
    assert response.status_code == 200
    assert response.json()["input_tokens"] > 0


def test_endpoint_count_tokens_respects_allowlist(tmp_path):
    app = _app(tmp_path)
    token = app.state.api_keys.create("root", allowed_models="glm-*")["api_key"]
    with TestClient(app) as client:
        response = client.post("/v1/messages/count_tokens", headers={"x-api-key": token},
                               json=_messages(model="kimi-k3@codebuddy"))
    assert response.status_code == 400

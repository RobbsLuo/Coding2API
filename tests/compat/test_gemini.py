"""Gemini 兼容出口：入站映射 / 出站翻译 / 端点契约。

映射规则按官方 Generative Language API：
`contents[].parts[]`（text/inlineData/functionCall/functionResponse）、
`systemInstruction`、`generationConfig`、`tools[].functionDeclarations`、
`toolConfig.functionCallingConfig`。不支持项必须 400，不静默降级。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src.compat.gemini.request import gemini_to_chat_body, parse_gemini_request
from src.compat.gemini.response import GeminiStreamTranslator, completion_to_gemini
from src.compat.openai.request import InvalidRequest
from src.provider.base import Event, EventKind, Usage
from tests.compat._support import build_compat_app

# ------------------------------------------------------------------ 入站映射

def test_text_and_system_instruction_map_to_chat_messages():
    body = {
        "systemInstruction": {"parts": [{"text": "be brief"}]},
        "contents": [
            {"role": "user", "parts": [{"text": "hi"}]},
            {"role": "model", "parts": [{"text": "hello"}]},
            {"role": "user", "parts": [{"text": "again"}]},
        ],
        "generationConfig": {"temperature": 0.2, "topP": 0.9,
                             "maxOutputTokens": 64, "stopSequences": ["END"]},
    }
    chat = gemini_to_chat_body(body)
    assert chat["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "again"},
    ]
    assert chat["temperature"] == 0.2
    assert chat["top_p"] == 0.9
    assert chat["max_tokens"] == 64
    assert chat["stop"] == ["END"]


def test_inline_data_becomes_image_url_part():
    body = {"contents": [{"role": "user", "parts": [
        {"text": "what is this"},
        {"inlineData": {"mimeType": "image/png", "data": "AAA"}},
    ]}]}
    chat = gemini_to_chat_body(body)
    content = chat["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "what is this"}
    assert content[1] == {"type": "image_url",
                          "image_url": {"url": "data:image/png;base64,AAA"}}


def test_file_data_passes_uri_through():
    body = {"contents": [{"role": "user", "parts": [
        {"fileData": {"fileUri": "gs://bucket/a.png", "mimeType": "image/png"}}]}]}
    content = gemini_to_chat_body(body)["messages"][0]["content"]
    assert content[0]["image_url"]["url"] == "gs://bucket/a.png"


def test_function_call_and_response_pair_by_name():
    body = {"contents": [
        {"role": "user", "parts": [{"text": "weather?"}]},
        {"role": "model", "parts": [{"functionCall": {"name": "get_weather",
                                                     "args": {"city": "SF"}}}]},
        {"role": "user", "parts": [{"functionResponse": {
            "name": "get_weather", "response": {"temp": 20}}}]},
    ]}
    messages = gemini_to_chat_body(body)["messages"]
    assert messages[1]["role"] == "assistant"
    assert messages[1]["tool_calls"][0]["id"] == "call_get_weather"
    assert json.loads(messages[1]["tool_calls"][0]["function"]["arguments"]) == {
        "city": "SF"}
    assert messages[2] == {"role": "tool", "tool_call_id": "call_get_weather",
                           "content": '{"temp": 20}'}


def test_thought_parts_map_to_reasoning_content():
    body = {"contents": [
        {"role": "model", "parts": [{"text": "thinking", "thought": True},
                                    {"text": "answer"}]}]}
    chat = gemini_to_chat_body(body)
    assert chat["messages"][0]["reasoning_content"] == "thinking"
    assert chat["messages"][0]["content"] == "answer"


def test_tools_and_tool_choice_map():
    body = {
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        "tools": [{"functionDeclarations": [
            {"name": "f", "description": "d",
             "parameters": {"type": "object", "properties": {}}}]}],
        "toolConfig": {"functionCallingConfig": {"mode": "ANY"}},
    }
    chat = gemini_to_chat_body(body)
    assert chat["tools"] == [{"type": "function", "function": {
        "name": "f", "description": "d",
        "parameters": {"type": "object", "properties": {}}}}]
    assert chat["tool_choice"] == "required"


def test_tool_choice_none_and_auto():
    for mode, expected in (("AUTO", "auto"), ("NONE", "none")):
        chat = gemini_to_chat_body({
            "contents": [{"role": "user", "parts": [{"text": "x"}]}],
            "toolConfig": {"functionCallingConfig": {"mode": mode}}})
        assert chat.get("tool_choice") == expected


def test_json_response_mime_and_schema():
    chat = gemini_to_chat_body({
        "contents": [{"role": "user", "parts": [{"text": "x"}]}],
        "generationConfig": {"responseMimeType": "application/json"}})
    assert chat["response_format"] == {"type": "json_object"}

    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    chat = gemini_to_chat_body({
        "contents": [{"role": "user", "parts": [{"text": "x"}]}],
        "generationConfig": {"responseSchema": schema}})
    assert chat["response_format"]["type"] == "json_schema"
    assert chat["response_format"]["json_schema"]["schema"] == schema


def test_parse_sets_model_and_stream():
    parsed = parse_gemini_request(
        {"contents": [{"role": "user", "parts": [{"text": "x"}]}]},
        model="gemini-3.8-flash", stream=True)
    assert parsed.model == "gemini-3.8-flash"
    assert parsed.stream is True


@pytest.mark.parametrize(("body", "match"), [
    (None, "JSON object"),
    ({}, "contents must be a non-empty array"),
    ({"contents": []}, "contents must be a non-empty array"),
    ({"contents": [{"parts": []}]}, "parts must be a non-empty array"),
    ({"contents": [{"role": "robot", "parts": [{"text": "x"}]}]}, "role must be one of"),
    ({"contents": [{"role": "user", "parts": [{"text": "x"}]}],
      "generationConfig": {"candidateCount": 2}}, "candidateCount must be 1"),
    ({"contents": [{"role": "user", "parts": [{"text": "x"}]}],
      "generationConfig": {"responseMimeType": "text/csv"}}, "responseMimeType"),
    ({"contents": [{"role": "user", "parts": [{"text": "x"}]}],
      "tools": [{"googleSearch": {}}]}, "server-side Gemini tools"),
    ({"contents": [{"role": "user", "parts": [{"text": "x"}]}],
      "tools": [{"functionDeclarations": [{"name": ""}]}], }, "name must be"),
    ({"contents": [{"role": "user", "parts": [{"inlineData": {"data": "x"}}]}]},
     "mimeType"),
])
def test_invalid_requests_raise(body, match):
    with pytest.raises(InvalidRequest, match=match):
        gemini_to_chat_body(body)


# ------------------------------------------------------------------ 出站翻译

def test_stream_translator_emits_text_thought_tool_and_finish():
    translator = GeminiStreamTranslator("gemini-3.8-flash", response_id="resp_1")
    frames = []
    for event in [
        Event(kind=EventKind.REASONING, content="think"),
        Event(kind=EventKind.CONTENT, content="hi"),
        Event(kind=EventKind.TOOL_CALLS, tool_calls=[
            {"index": 0, "function": {"name": "f", "arguments": '{"a":1}'}},
            {"index": 1, "function": {"name": "skip", "arguments": '{"a"'}},
        ]),
        Event(kind=EventKind.USAGE, usage=Usage(input_tokens=2, output_tokens=3)),
        Event(kind=EventKind.FINISH, finish_reason="stop"),
    ]:
        frames.extend(translator.translate(event))
    payloads = [json.loads(frame.decode().removeprefix("data: ")) for frame in frames]
    def first_part(payload):
        candidates = payload.get("candidates") or []
        parts = (candidates[0].get("content") or {}).get("parts") or []
        return parts[0] if parts else {}

    answer = [p for p in payloads if first_part(p).get("text") == "hi"]
    assert answer
    # 首帧（本用例是 thought 帧）带 modelVersion / responseId，后续帧不重复
    assert payloads[0]["modelVersion"] == "gemini-3.8-flash"
    assert payloads[0]["responseId"] == "resp_1"
    assert "modelVersion" not in answer[0]
    assert any(first_part(p).get("thought") is True for p in payloads)
    calls = [first_part(p)["functionCall"]["name"] for p in payloads
             if "functionCall" in first_part(p)]
    assert calls == ["f"]                       # 坏 JSON 分片不下发
    final = payloads[-1]
    assert final["candidates"][0]["finishReason"] == "STOP"
    assert final["usageMetadata"]["promptTokenCount"] == 2
    assert translator.done_sent is True


def test_stream_translator_finish_without_finish_event_and_error_frame():
    translator = GeminiStreamTranslator("m")
    frames = list(translator.finish())
    assert len(frames) == 1 and b"finishReason" in frames[0]
    assert list(translator.finish()) == []      # 幂等

    failing = GeminiStreamTranslator("m")
    frame = failing.error_frame("bad", "invalid_request")
    payload = json.loads(frame.decode().removeprefix("data: "))
    assert payload["error"]["status"] == "INVALID_ARGUMENT"
    assert failing.error_frame("again", "internal_error") == b""


def test_completion_to_gemini_shape():
    completion = {
        "model": "glm-5.2",
        "choices": [{"finish_reason": "tool_calls", "message": {
            "content": "text", "reasoning_content": "r",
            "tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "f", "arguments": '{"a":1}'}}]}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7,
                  "prompt_tokens_details": {"cached_tokens": 2}},
    }
    payload = completion_to_gemini(completion, response_id="resp_9")
    candidate = payload["candidates"][0]
    kinds = [list(part)[0] for part in candidate["content"]["parts"]]
    assert kinds == ["text", "text", "functionCall"]
    assert candidate["content"]["parts"][2]["functionCall"]["args"] == {"a": 1}
    assert candidate["finishReason"] == "STOP"
    assert payload["usageMetadata"] == {
        "promptTokenCount": 5, "candidatesTokenCount": 7, "totalTokenCount": 12,
        "cachedContentTokenCount": 2}
    assert payload["responseId"] == "resp_9"


def test_completion_to_gemini_empty_and_bad_arguments():
    payload = completion_to_gemini({"choices": [{"message": {
        "content": None, "tool_calls": [{"function": {"name": "f",
                                                     "arguments": "not json"}}]}}]})
    assert payload["candidates"][0]["content"]["parts"] == [{"text": ""}]


# ------------------------------------------------------------------ 端点契约

class _Provider:
    id = "codebuddy"

    def __init__(self, *, stream: bool = True) -> None:
        self.stream = stream
        self.last_payload: dict = {}

    async def stream_chat(self, credential_data, payload, model):
        self.last_payload = payload
        yield Event(kind=EventKind.CONTENT, content="hi there")
        yield Event(kind=EventKind.USAGE, usage=Usage(input_tokens=2, output_tokens=3))
        yield Event(kind=EventKind.FINISH, finish_reason="stop")

    async def probe_quota(self, credential_data):  # pragma: no cover - 本用例不调用
        return None

    async def list_models(self, credential_data):
        from src.provider.base import Model

        return [Model(id="glm-5.3")]

    async def aclose(self):
        return None


def _app(tmp_path, provider=None):
    return build_compat_app(tmp_path, provider or _Provider())


def _auth(app) -> dict:
    return {"x-goog-api-key": app.state.api_keys.create("root")["api_key"]}


def test_endpoint_stream_generate_content(tmp_path):
    provider = _Provider()
    app = _app(tmp_path, provider)
    with TestClient(app) as client:
        response = client.post(
            "/v1beta/models/glm-5.2@codebuddy:streamGenerateContent?alt=sse",
            headers=_auth(app),
            json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    payloads = [json.loads(line[6:]) for line in response.text.splitlines()
                if line.startswith("data: ")]
    assert payloads[0]["candidates"][0]["content"]["parts"][0]["text"] == "hi there"
    assert payloads[-1]["candidates"][0]["finishReason"] == "STOP"
    # 入站映射真的走到了上游载荷
    assert provider.last_payload["messages"] == [{"role": "user", "content": "hi"}]


def test_endpoint_non_stream_generate_content(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/v1beta/models/glm-5.2@codebuddy:generateContent",
            headers=_auth(app),
            json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]})
    assert response.status_code == 200
    body = response.json()
    assert body["candidates"][0]["content"]["parts"][0]["text"] == "hi there"
    assert body["usageMetadata"]["promptTokenCount"] == 2


def test_endpoint_accepts_query_key_and_lists_models(tmp_path):
    app = _app(tmp_path)
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        response = client.get(f"/v1beta/models?key={key}")
    assert response.status_code == 200
    assert response.json()["models"][0]["name"].startswith("models/")


def test_endpoint_count_tokens_and_bad_method(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        ok = client.post(
            "/v1beta/models/glm-5.2:countTokens", headers=_auth(app),
            json={"contents": [{"role": "user", "parts": [{"text": "hello world"}]}]})
        bad = client.post("/v1beta/models/glm-5.2:unknownMethod",
                          headers=_auth(app), json={"contents": []})
    assert ok.status_code == 200
    assert ok.json()["totalTokens"] > 0
    assert bad.status_code == 400
    assert "unsupported method" in bad.text


def test_endpoint_requires_key(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/v1beta/models/glm-5.2:generateContent",
            json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]})
    assert response.status_code == 401


# ------------------------------------------------------------------ 覆盖补丁

@pytest.mark.parametrize(("body", "match"), [
    # parts 不是数组 / 元素不是对象
    ({"contents": [{"parts": "x"}]}, "non-empty array"),
    ({"contents": [{"parts": ["x"]}]}, r"parts\[0\] must be an object"),
    # systemInstruction 形状
    ({"systemInstruction": 5, "contents": [{"parts": [{"text": "x"}]}]},
     "systemInstruction must be an object"),
    # inlineData 缺 data / fileData 缺 uri
    ({"contents": [{"parts": [{"inlineData": {"mimeType": "image/png"}}]}]},
     "data must be a base64 string"),
    ({"contents": [{"parts": [{"fileData": {"fileUri": 5}}]}]},
     "fileUri must be a string"),
    # functionCall args 非对象
    ({"contents": [{"role": "model", "parts": [
        {"functionCall": {"name": "f", "args": []}}]}]}, "args must be an object"),
    # functionResponse 缺 name / content 元素非对象
    ({"contents": [{"role": "user", "parts": [{"functionResponse": {"response": {}}}]}]},
     "functionResponse.name required"),
    ({"contents": [{"role": "user", "parts": [42]}]}, "must be an object"),
    # functionCall 缺 name
    ({"contents": [{"role": "model", "parts": [{"functionCall": {"args": {}}}]}]},
     "functionCall.name required"),
    # tools 非数组 / 元素非对象 / declarations 非数组 / 声明非对象 / parameters 非对象
    ({"contents": [{"parts": [{"text": "x"}]}], "tools": "x"}, "tools must be an array"),
    ({"contents": [{"parts": [{"text": "x"}]}], "tools": [1]},
     r"tools\[0\] must be an object"),
    ({"contents": [{"parts": [{"text": "x"}]}],
      "tools": [{"functionDeclarations": "x"}]}, "must be an array"),
    ({"contents": [{"parts": [{"text": "x"}]}],
      "tools": [{"functionDeclarations": [1]}]}, "must be an object"),
    ({"contents": [{"parts": [{"text": "x"}]}],
      "tools": [{"functionDeclarations": [{"name": "f",
                                           "parameters": "x"}]}]},
     "parameters must be an object"),
    # toolConfig 形状与未知 mode
    ({"contents": [{"parts": [{"text": "x"}]}], "toolConfig": 1},
     "toolConfig must be an object"),
    ({"contents": [{"parts": [{"text": "x"}]}],
      "toolConfig": {"functionCallingConfig": 1}}, "functionCallingConfig"),
    ({"contents": [{"parts": [{"text": "x"}]}],
      "toolConfig": {"functionCallingConfig": {"mode": "MAYBE"}}}, "mode unsupported"),
    # generationConfig 形状 / stopSequences / responseSchema
    ({"contents": [{"parts": [{"text": "x"}]}], "generationConfig": 1},
     "generationConfig must be an object"),
    ({"contents": [{"parts": [{"text": "x"}]}],
      "generationConfig": {"stopSequences": [1]}}, "array of strings"),
    ({"contents": [{"parts": [{"text": "x"}]}],
      "generationConfig": {"responseSchema": "x"}}, "responseSchema must be an object"),
    # contents 元素非对象 / 只产出空 model 消息
    ({"contents": [1]}, r"contents\[0\] must be an object"),
    ({"contents": [{"role": "model", "parts": [{"functionCall": None}]}]},
     "contents produced no messages"),
])
def test_more_invalid_requests(body, match):
    with pytest.raises(InvalidRequest, match=match):
        gemini_to_chat_body(body)


def test_string_system_instruction_and_function_role():
    chat = gemini_to_chat_body({
        "systemInstruction": "plain",
        "contents": [{"role": "function", "parts": [{"text": "x"}]}]})
    assert chat["messages"][0] == {"role": "system", "content": "plain"}
    assert chat["messages"][1] == {"role": "user", "content": "x"}


def test_any_mode_with_single_allowed_function():
    chat = gemini_to_chat_body({
        "contents": [{"parts": [{"text": "x"}]}],
        "toolConfig": {"functionCallingConfig": {
            "mode": "ANY", "allowedFunctionNames": ["f"]}}})
    assert chat["tool_choice"] == {"type": "function", "function": {"name": "f"}}


def test_tool_config_without_mode_or_config_is_ignored():
    chat = gemini_to_chat_body({
        "contents": [{"parts": [{"text": "x"}]}],
        "toolConfig": {}})
    assert "tool_choice" not in chat


def test_model_content_without_text_or_calls_is_dropped():
    with pytest.raises(InvalidRequest, match="contents produced no messages"):
        gemini_to_chat_body({
            "contents": [{"role": "model", "parts": [{"text": ""}]}]})


def test_stream_translator_error_event_maps_to_error_frame():
    translator = GeminiStreamTranslator("m")
    frames = list(translator.translate(
        Event(kind=EventKind.ERROR, error_message="boom")))
    payload = json.loads(frames[0].decode().removeprefix("data: "))
    assert payload["error"]["status"] == "INTERNAL"
    assert translator.done_sent is True


def test_stream_translator_tool_without_name_or_empty_args():
    translator = GeminiStreamTranslator("m")
    frames = list(translator.translate(Event(
        kind=EventKind.TOOL_CALLS,
        tool_calls=[{"index": 0, "function": {"arguments": ""}},
                    {"index": 1, "function": {"name": "f", "arguments": ""}}])))
    payloads = [json.loads(frame.decode().removeprefix("data: ")) for frame in frames]
    calls = [p["candidates"][0]["content"]["parts"][0]["functionCall"]
             for p in payloads]
    assert calls == [{"name": "f", "args": {}}]


def test_stream_translator_usage_with_cache_and_reasoning():
    translator = GeminiStreamTranslator("m")
    list(translator.translate(Event(kind=EventKind.USAGE, usage=Usage(
        input_tokens=1, output_tokens=2, cached_tokens=0, reasoning_tokens=4))))
    frames = list(translator.finish())
    payload = json.loads(frames[0].decode().removeprefix("data: "))
    assert payload["usageMetadata"]["cachedContentTokenCount"] == 0
    assert payload["usageMetadata"]["thoughtsTokenCount"] == 4


def test_completion_to_gemini_usage_non_int_and_bad_details():
    empty = completion_to_gemini({"choices": [{"message": {"content": "x"}}],
                                  "usage": {}})
    assert "usageMetadata" not in empty
    partial = completion_to_gemini({
        "choices": [{"message": {"content": "x"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4,
                  "prompt_tokens_details": {"cached_tokens": 0}}})
    assert partial["usageMetadata"] == {
        "promptTokenCount": 3, "candidatesTokenCount": 4, "totalTokenCount": 7}


def test_split_model_path_rejects_missing_parts():
    from src.api.gemini import split_model_path

    with pytest.raises(InvalidRequest, match="path must end with"):
        split_model_path("glm-5.2")
    with pytest.raises(InvalidRequest, match="model is required"):
        split_model_path("models/:generateContent")


def test_text_of_accepts_string_and_skips_non_text_parts():
    # systemInstruction.parts 直接给字符串（官方也允许）
    chat = gemini_to_chat_body({
        "systemInstruction": {"parts": "plain"},
        "contents": [{"parts": [{"image": {}}, {"text": "x"}]}]})
    assert chat["messages"][0] == {"role": "system", "content": "plain"}


@pytest.mark.parametrize(("body", "match"), [
    ({"systemInstruction": {"parts": 1}, "contents": [{"parts": [{"text": "x"}]}]},
     "systemInstruction.parts must be an array"),
    ({"systemInstruction": {"parts": [1]},
      "contents": [{"parts": [{"text": "x"}]}]},
     r"systemInstruction.parts\[0\] must be an object"),
    ({"contents": [{"role": "model", "parts": [1]}]}, r"parts\[0\] must be an object"),
])
def test_text_and_part_shape_errors(body, match):
    with pytest.raises(InvalidRequest, match=match):
        gemini_to_chat_body(body)


def test_snake_case_keys_and_missing_args():
    body = {
        "contents": [
            {"role": "user", "parts": [{"function_response": {
                "name": "f", "response": {"ok": True}}}]},
            {"role": "model", "parts": [{"function_call": {"name": "f"}}]},
        ],
        "system_instruction": {"parts": [{"text": "sys"}]},
        "tools": [{"function_declarations": [{"name": "f"}]}],
        "tool_config": {"function_calling_config": {"mode": "AUTO"}},
    }
    chat = gemini_to_chat_body(body)
    assert chat["messages"][0] == {"role": "system", "content": "sys"}
    # function_response 先出现 → tool 消息；function_call 无 args → arguments ""
    assert chat["messages"][1]["role"] == "tool"
    assert chat["messages"][2]["tool_calls"][0]["function"]["arguments"] == ""
    assert chat["tools"] == [{"type": "function", "function": {"name": "f"}}]


def test_tool_config_present_without_mode_is_ignored():
    chat = gemini_to_chat_body({
        "contents": [{"parts": [{"text": "x"}]}],
        "toolConfig": {"functionCallingConfig": {}}})
    assert "tool_choice" not in chat


def test_translator_finish_twice_and_no_finish_event():
    translator = GeminiStreamTranslator("m")
    first = list(translator.translate(Event(kind=EventKind.FINISH,
                                            finish_reason="length")))
    second = list(translator.translate(Event(kind=EventKind.FINISH)))
    assert first and second == []
    payload = json.loads(first[0].decode().removeprefix("data: "))
    assert payload["candidates"][0]["finishReason"] == "MAX_TOKENS"


def test_completion_to_gemini_skips_call_without_name():
    payload = completion_to_gemini({"choices": [{"message": {
        "content": "x", "tool_calls": [{"function": {"arguments": ""}}]}}]})
    assert payload["candidates"][0]["content"]["parts"] == [{"text": "x"}]


def test_system_instruction_skips_non_text_parts():
    chat = gemini_to_chat_body({
        "systemInstruction": {"parts": [{"image": {}}, {"text": "sys"}]},
        "contents": [{"parts": [{"text": "x"}]}]})
    assert chat["messages"][0] == {"role": "system", "content": "sys"}


def test_tool_config_non_empty_without_mode():
    chat = gemini_to_chat_body({
        "contents": [{"parts": [{"text": "x"}]}],
        "toolConfig": {"functionCallingConfig": {"allowedFunctionNames": ["f"]}}})
    assert "tool_choice" not in chat


def test_translator_skips_non_object_arguments_and_partial_content():
    translator = GeminiStreamTranslator("m")
    content_frames = list(translator.translate(
        Event(kind=EventKind.CONTENT, content="x")))
    assert content_frames
    tool_frames = list(translator.translate(Event(
        kind=EventKind.TOOL_CALLS,
        tool_calls=[{"index": 0, "function": {"name": "f", "arguments": "[1,2]"}}])))
    assert tool_frames == []


def test_translator_ignores_empty_event_and_unknown_kinds():
    """空 tool_calls / USAGE 之后的空 loop 退出分支（不发任何帧、不炸）。"""
    translator = GeminiStreamTranslator("m")
    assert list(translator.translate(
        Event(kind=EventKind.TOOL_CALLS, tool_calls=[]))) == []
    assert list(translator.translate(Event(kind=EventKind.USAGE))) == []
    assert translator.usage is None

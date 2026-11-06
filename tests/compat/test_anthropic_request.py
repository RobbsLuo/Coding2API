"""Anthropic 出口：入站请求映射（Messages 请求体 → chat 载荷）。"""

from __future__ import annotations

import re

import pytest

from src.compat.anthropic.request import parse_messages_request
from src.compat.openai.request import InvalidRequest


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
    with pytest.raises(InvalidRequest, match="role 'tool' is not supported"):
        parse_messages_request(_body(messages=[{"role": "tool", "content": "x"}]))
    # 数组 content 下的非法 role 走另一分支
    with pytest.raises(InvalidRequest, match="role 'tool' is not supported"):
        parse_messages_request(_body(messages=[{"role": "tool", "content": [
            {"type": "text", "text": "x"}]}]))
    with pytest.raises(InvalidRequest, match=r"messages\[0\] must be an object"):
        parse_messages_request(_body(messages=["x"]))
    with pytest.raises(InvalidRequest, match="string or an array"):
        parse_messages_request(_body(messages=[{"role": "user", "content": 5}]))


def test_parse_mid_conversation_system_role():
    """Claude Code 2.1.318 起在新会话首轮发 `role:"system"`（特性
    `mid-conversation-system-2026-04-07`）。就地转成 system 消息，不重排。

    此前 400 会让客户端白打一次往返（它收到 400 就重发去掉该消息并永久
    关掉特性）。
    """
    parsed = parse_messages_request(_body(messages=[
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "be terse"},
        {"role": "assistant", "content": "ok"},
    ]))
    assert parsed.messages == [
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "be terse"},
        {"role": "assistant", "content": "ok"},
    ]


def test_parse_mid_conversation_system_block_content():
    parsed = parse_messages_request(_body(messages=[
        {"role": "system", "content": [{"type": "text", "text": "a"},
                                       {"type": "text", "text": "b"}]},
    ]))
    assert parsed.messages == [{"role": "system", "content": "ab"}]

    parsed = parse_messages_request(_body(messages=[
        {"role": "system", "content": None},
        {"role": "user", "content": "hi"},
    ]))
    assert parsed.messages == [{"role": "system", "content": ""},
                               {"role": "user", "content": "hi"}]


def test_parse_mid_conversation_system_rejects_bad_shapes():
    with pytest.raises(InvalidRequest, match="string or an array"):
        parse_messages_request(_body(messages=[{"role": "system", "content": 5}]))
    with pytest.raises(InvalidRequest, match=r"content\[0\] must be an object"):
        parse_messages_request(_body(messages=[{"role": "system", "content": [1]}]))
    with pytest.raises(InvalidRequest, match="not supported in a system message"):
        parse_messages_request(_body(messages=[{"role": "system", "content": [
            {"type": "image"}]}]))
    with pytest.raises(InvalidRequest, match="text must be a string"):
        parse_messages_request(_body(messages=[{"role": "system", "content": [
            {"type": "text", "text": 1}]}]))

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
    with pytest.raises(InvalidRequest, match="unknown content block type"):
        parse_messages_request(_body(messages=[{"role": "user", "content": [
            {"type": "bogus"}]}]))


def test_parse_user_image_blocks():
    """`image` 块归一为 chat 的 image_url：base64 拼 data URL，url 直传。"""
    parsed = parse_messages_request(_body(messages=[{"role": "user", "content": [
        {"type": "text", "text": "看图"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": "QUJD"}},
        {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}},
    ]}]))
    assert parsed.messages == [{"role": "user", "content": [
        {"type": "text", "text": "看图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
        {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
    ]}]


def test_parse_pure_text_still_folds_to_string():
    """纯文本块仍折成字符串：历史请求（无图）映射结果必须与改动前逐字节一致。"""
    parsed = parse_messages_request(_body(messages=[{"role": "user", "content": [
        {"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}]))
    assert parsed.messages == [{"role": "user", "content": "ab"}]


def test_parse_image_only_user_block():
    """只有图片、没有文本块时也产出 user 消息（不能因无文本而丢整条）。"""
    parsed = parse_messages_request(_body(messages=[{"role": "user", "content": [
        {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}}]}]))
    assert parsed.messages == [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "https://x/y.png"}}]}]


@pytest.mark.parametrize("block, fragment", [
    ({"type": "image", "source": "x"}, "source must be an object"),
    ({"type": "image", "source": {"type": "file", "file_id": "f"}},
     "source.type 'file' is not supported"),
    ({"type": "image", "source": {"type": "base64", "data": "x"}},
     "media_type must be a non-empty string"),
    ({"type": "image", "source": {"type": "base64", "media_type": "image/png"}},
     "data must be a non-empty string"),
    ({"type": "image", "source": {"type": "base64", "media_type": "",
                                  "data": "x"}},
     "media_type must be a non-empty string"),
    ({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                  "data": ""}},
     "data must be a non-empty string"),
    ({"type": "image", "source": {"type": "url"}}, "url must be a non-empty string"),
    ({"type": "image", "source": {"type": "url", "url": ""}},
     "url must be a non-empty string"),
])
def test_parse_image_block_bad_shapes(block, fragment):
    with pytest.raises(InvalidRequest, match=re.escape(fragment)):
        parse_messages_request(_body(messages=[{"role": "user", "content": [block]}]))

def test_parse_tool_result_content_bad_shapes():
    with pytest.raises(InvalidRequest, match=r"content\[0\] must be an object"):
        parse_messages_request(_body(messages=[{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "content": [1]}]}]))
    with pytest.raises(InvalidRequest, match="unknown tool_result.content type"):
        parse_messages_request(_body(messages=[{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t",
             "content": [{"type": "bogus"}]}]}]))
    with pytest.raises(InvalidRequest, match="string or an array"):
        parse_messages_request(_body(messages=[{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "content": 5}]}]))


def test_parse_document_and_tool_reference_become_placeholders():
    """`document` / `tool_reference` 不再整轮 400。

    chat 没有「文档」与「服务端 tool_search 结果」的等价物；显式 400 会让
    客户端整轮重发一次（`tool_reference` 实测 28/30 次 400 都出在它身上）。
    改成中性占位文本，模型至少知道这里原本有个附件 / 工具引用。
    """
    parsed = parse_messages_request(_body(messages=[
        {"role": "user", "content": [
            {"type": "document", "title": "resume.pdf",
             "source": {"type": "base64", "media_type": "application/pdf",
                        "data": "AAA"}},
            {"type": "tool_reference", "tool_name": "mcp__fs__read"},
            {"type": "tool_reference"},
        ]},
        {"role": "assistant", "content": [
            {"type": "tool_reference"},
            {"type": "text", "text": "done"},
        ]},
    ]))
    # 全是文本（占位也是文本）→ 按既有规则折成字符串，与旧行为一致
    assert parsed.messages[0] == {"role": "user", "content": (
        "[resume.pdf omitted: this gateway cannot forward document content]"
        "[tool available: mcp__fs__read][tool available]")}
    # assistant 侧的 tool_reference 直接丢（不占上下文）
    assert parsed.messages[1] == {"role": "assistant", "content": "done"}


def test_parse_document_inside_tool_result_is_placeholder():
    parsed = parse_messages_request(_body(messages=[{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": [
            {"type": "document", "source": {"type": "text", "data": "x"}}]}]}]))
    assert parsed.messages[0]["content"] == (
        "[document omitted: this gateway cannot forward document content]")


def test_parse_tool_result_with_image():
    """`tool_result` 内图片 → tool 消息 content 数组（chat 的 tool 消息带
    image_url 已实测被上游接受）。纯文本结果仍返回字符串。"""
    parsed = parse_messages_request(_body(messages=[{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": [
            {"type": "text", "text": "读到的图："},
            {"type": "image", "source": {"type": "base64",
                                         "media_type": "image/png", "data": "QUJD"}},
        ]},
    ]}]))
    assert parsed.messages[0]["content"] == [
        {"type": "text", "text": "读到的图："},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
    ]


def test_parse_image_part_bad_source_in_tool_result():
    with pytest.raises(InvalidRequest, match=r"tool_result.content\[0\].source"):
        parse_messages_request(_body(messages=[{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t",
             "content": [{"type": "image"}]}]}]))

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
    """负数/超大 max_tokens、NaN/Inf 采样参数在入站即 400，不透传给上游。"""
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

def test_parse_arguments_shapes():
    from src.compat.anthropic.response import _parse_arguments

    assert _parse_arguments({"a": 1}) == {"a": 1}
    assert _parse_arguments("") == {}
    assert _parse_arguments(None) == {}
    assert _parse_arguments("not-json") == {}
    assert _parse_arguments("[1,2]") == {}

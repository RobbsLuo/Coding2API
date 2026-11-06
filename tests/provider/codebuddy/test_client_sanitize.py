"""CodeBuddy 客户端：渠道标记（prompt 角色/文本块）净化。"""

from __future__ import annotations


def test_sanitize_channel_markers_neutralizes_prompt_roles_only():
    """system/assistant 正文替换为占位符；user/tool 与非正文位置不动。"""
    from src.provider.codebuddy.client import CHANNEL_MARKERS, sanitize_channel_markers

    marker = CHANNEL_MARKERS[0]
    body = {"messages": [
        {"role": "system", "content": f"前缀 {marker} 后缀"},
        {"role": "assistant", "content": marker},
        {"role": "user", "content": marker},                      # user 不动
        {"role": "tool", "tool_call_id": "t", "content": marker},  # tool 不动
        {"role": "assistant", "tool_calls": [{"id": "c", "type": "function",
                                             "function": {"name": "f",
                                                          "arguments": f'"{marker}"'}}]},
    ]}
    hits = sanitize_channel_markers(body)
    assert hits == 2
    assert body["messages"][0]["content"] == "前缀 [external-client-identity] 后缀"
    assert body["messages"][1]["content"] == "[external-client-identity]"
    assert body["messages"][2]["content"] == marker
    assert body["messages"][3]["content"] == marker
    args = body["messages"][4]["tool_calls"][0]["function"]["arguments"]
    assert marker in args                          # tool_calls 参数不动

def test_sanitize_channel_markers_neutralizes_text_blocks():
    """content 为文本块列表时逐块中和（2026-09-21 实测块形态指纹同样 11128）。"""
    from src.provider.codebuddy.client import CHANNEL_MARKERS, sanitize_channel_markers

    marker = CHANNEL_MARKERS[0]
    body = {"messages": [
        {"role": "system", "content": [
            {"type": "text", "text": f"前缀 {marker} 后缀"},
            {"type": "text", "text": "无指纹"},
            {"type": "image_url", "image_url": {"url": marker}},  # 非文本块不动
            {"type": "text", "text": 123},                        # 非 str 不动
            "junk",                                               # 非 dict 不动
        ]},
        {"role": "assistant", "content": [
            {"type": "text", "text": marker},
            {"type": "text", "text": f"{marker} x{marker}"},
        ]},
        {"role": "user", "content": [{"type": "text", "text": marker}]},  # user 不动
    ]}
    assert sanitize_channel_markers(body) == 4
    blocks = body["messages"][0]["content"]
    assert blocks[0]["text"] == "前缀 [external-client-identity] 后缀"
    assert blocks[1]["text"] == "无指纹"
    assert blocks[2]["image_url"]["url"] == marker
    assert blocks[3]["text"] == 123
    assert blocks[4] == "junk"
    assistant_blocks = body["messages"][1]["content"]
    assert assistant_blocks[0]["text"] == "[external-client-identity]"
    assert assistant_blocks[1]["text"] == "[external-client-identity] x[external-client-identity]"
    assert body["messages"][2]["content"][0]["text"] == marker

def test_sanitize_channel_markers_counts_repeats_and_skips_noise():
    """同条消息多指纹多出处计数；非 list/非 dict/非 str content 安全跳过。"""
    from src.provider.codebuddy.client import CHANNEL_MARKERS, sanitize_channel_markers

    a, b = CHANNEL_MARKERS[1], CHANNEL_MARKERS[2]
    body = {"messages": [
        "junk",
        {"role": "assistant", "content": None},
        {"role": "assistant"},
        {"role": "assistant", "content": ""},
        {"role": "assistant", "content": {"list": "content"}},
        {"role": "assistant", "content": f"{a} 中间 {b} 结尾 {a}"},
    ]}
    assert sanitize_channel_markers(body) == 3
    assert body["messages"][5]["content"] == (
        "[external-client-identity] 中间 [external-client-identity] 结尾 "
        "[external-client-identity]")

    assert sanitize_channel_markers({}) == 0
    assert sanitize_channel_markers({"messages": "nope"}) == 0


def test_sanitize_channel_markers_covers_tool_descriptions():
    """`tools[].function.description` 是同一类指纹的高发区（客户端写进去的
    「你是 Claude Code…」），不洗会被上游整单 11128 拒。

    `parameters` 属模型调用契约，逐串替换会改坏枚举/示例值，故不动。
    """
    from src.provider.codebuddy.client import CHANNEL_MARKERS, sanitize_channel_markers

    marker = CHANNEL_MARKERS[0]
    body = {"messages": [], "tools": [
        {"type": "function", "function": {
            "name": "f", "description": f"说明 {marker} 结束",
            "parameters": {"type": "object", "description": marker}}},
        {"type": "function", "function": {"name": "bare"}},          # 无 description
        {"type": "function", "function": {"name": "g", "description": 5}},
        {"type": "function", "function": {"name": "clean",
                                          "description": "无指纹"}},
        {"type": "function", "function": "not-a-dict"},
        "junk",
        {"function": {"description": marker}},                       # 无 type 也洗
    ]}
    assert sanitize_channel_markers(body) == 2
    tools = body["tools"]
    assert tools[0]["function"]["description"] == "说明 [external-client-identity] 结束"
    assert tools[0]["function"]["parameters"]["description"] == marker
    assert tools[1]["function"] == {"name": "bare"}
    assert tools[2]["function"]["description"] == 5
    assert tools[3]["function"]["description"] == "无指纹"
    assert tools[4]["function"] == "not-a-dict"
    assert tools[5] == "junk"
    assert tools[6]["function"]["description"] == "[external-client-identity]"


def test_sanitize_channel_markers_tolerates_bad_tools_shape():
    """`tools` 非数组 / 元素非 dict 时不炸（出站载荷可能来自宽松客户端）。"""
    from src.provider.codebuddy.client import sanitize_channel_markers

    assert sanitize_channel_markers({"messages": [], "tools": "nope"}) == 0
    assert sanitize_channel_markers({"messages": [], "tools": [5, None]}) == 0

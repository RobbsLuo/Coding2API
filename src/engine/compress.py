"""上下文压缩（P0-2）：按模型输入上限裁剪消息，避免撞上游硬限制。

动机（来源：workbuddy-openai-proxy §6）：上游对输入长度有硬限制，超了直接
返回（CodeBuddy `11115 prompt is too long`、`{"code":11115,"msg":
"prompt is too long: 100001 tokens > 100000 maximum"}`）。反代若原样转发，
长会话（编码助手把整份文件塞进上下文）必然撞墙，客户端只看到一个裸的 400，
而换号重试毫无意义（每个号的上限一样）。

策略（刻意保守，只做确定性裁剪、不做启发式猜测）：

1. 估算 token：中文按 0.55 tok/字、数字 0.33、其他 0.25——沿用实测口径。
   「3 字符 ≈ 1 token」的英文口径会把中文低估约 1.6 倍，反而「以为装得下、
   其实装不下」，压缩根本不触发。
2. 超过「模型上限 × safety_ratio − reserve_for_output」时从**最老**的消息
   开始丢弃；`system` 永远保留（丢了会改变模型行为），至少保留最近 N 条
   （保证当前这轮对话完整）。回填从最新往最老，装不下就跳过继续往前看。
3. `assistant.tool_calls` 与其 `tool` 结果同生共死：按「组」处理，只删一半
   会让上游报 tool_call_id 找不到。
4. 裁剪后仍超限（system / 最近 N 条本身太大）→ 截断最长的那条消息内容
   （留头尾 + 标记），而不是整条丢弃。

纯函数模块：输入输出都是消息列表，不依赖 Request / Services，便于直测。
"""

from __future__ import annotations

from typing import Any

# token 估算权（实测口径，见模块 docstring）
_CJK_WEIGHT = 0.55
_DIGIT_WEIGHT = 0.33
_OTHER_WEIGHT = 0.25

# 默认参数（可被热更覆盖）
DEFAULT_RESERVE_FOR_OUTPUT = 4096
DEFAULT_MIN_KEEP_MESSAGES = 4
DEFAULT_SAFETY_RATIO = 0.95

# 单条消息内容被截断时保留的头尾字符数，以及插入的显式标记
_TRUNCATE_HEAD = 4000
_TRUNCATE_TAIL = 4000
_TRUNCATE_MARKER = "\n…[内容过长已截断]…\n"


def _is_cjk(code: int) -> bool:
    """中日韩统一表意文字 / CJK 标点 / 全角符号。"""
    return (0x4E00 <= code <= 0x9FFF or 0x3000 <= code <= 0x303F
            or 0xFF00 <= code <= 0xFFEF)


def estimate_tokens(text: str) -> float:
    """按字符类型加权的 token 估算（纯启发式，不引入分词器依赖）。"""
    total = 0.0
    for char in text:
        if _is_cjk(ord(char)):
            total += _CJK_WEIGHT
        elif char.isdigit():
            total += _DIGIT_WEIGHT
        else:
            total += _OTHER_WEIGHT
    return total


def _content_text(content: Any) -> str:
    """一条 message 的 content（字符串或 part 数组）→ 纯文本（仅用于估算）。

    非文本 part（图片/文件）按 100 字符占位计入，避免被当成 0 而低估体积到
    「永远不触发压缩」。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                parts.append(text if isinstance(text, str) else "x" * 100)
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    if content is None:
        return ""
    return str(content)


def _message_tokens(message: dict[str, Any]) -> float:
    total = estimate_tokens(_content_text(message.get("content")))
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str):
        total += estimate_tokens(reasoning)
    for call in message.get("tool_calls") or []:
        if isinstance(call, dict):
            function = call.get("function") or {}
            total += estimate_tokens(str(function.get("name") or ""))
            total += estimate_tokens(str(function.get("arguments") or ""))
    tool_call_id = message.get("tool_call_id")
    if isinstance(tool_call_id, str):
        total += estimate_tokens(tool_call_id)
    return total


def estimate_messages(messages: list[dict[str, Any]]) -> float:
    return sum(_message_tokens(message) for message in messages)


def _truncate_content(content: Any) -> Any:
    """单条消息内容截断：字符串直接截；part 数组整体降级为纯文本。"""
    text = _content_text(content)
    if len(text) <= _TRUNCATE_HEAD + _TRUNCATE_TAIL:
        return content
    truncated = text[:_TRUNCATE_HEAD] + _TRUNCATE_MARKER + text[-_TRUNCATE_TAIL:]
    return truncated


def _truncate_to_budget(messages: list[dict[str, Any]],
                        budget: float) -> list[dict[str, Any]]:
    """兜底：仍超预算时按内容长度从大到小逐条截断（system 不动）。

    每条只截一次；截完仍装不下就接受（上游还有硬上限兜着，这里只是尽力）。
    """
    result = list(messages)
    candidates = sorted(
        ((index, len(_content_text(message.get("content"))))
         for index, message in enumerate(result)
         if message.get("role") != "system"),
        key=lambda pair: pair[1], reverse=True)
    for index, _length in candidates:
        if estimate_messages(result) <= budget:
            break
        result[index] = {**result[index],
                         "content": _truncate_content(result[index].get("content"))}
    return result


def _group_atomic(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """把消息切成「不可拆分的组」。

    `assistant.tool_calls` 与其后连续的 `tool` 结果共属一组：只删一半会让
    上游报 tool_call_id 找不到。其余消息各自成组。
    """
    groups: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        group = [message]
        if message.get("role") == "assistant" and message.get("tool_calls"):
            following = index + 1
            while following < len(messages) and messages[following].get("role") == "tool":
                group.append(messages[following])
                following += 1
            index = following
        else:
            index += 1
        groups.append(group)
    return groups


def compress_messages(
    messages: list[dict[str, Any]],
    *,
    max_input_tokens: int,
    reserve_for_output: int = DEFAULT_RESERVE_FOR_OUTPUT,
    min_keep_messages: int = DEFAULT_MIN_KEEP_MESSAGES,
    safety_ratio: float = DEFAULT_SAFETY_RATIO,
) -> list[dict[str, Any]]:
    """把消息列表裁到模型输入上限之内；未超限时**原样返回**（零拷贝）。

    返回新列表，不修改入参（仅对超限单条做浅拷贝替换）。
    """
    if max_input_tokens <= 0:
        return messages
    budget = max_input_tokens * safety_ratio - reserve_for_output
    if budget <= 0 or estimate_messages(messages) <= budget:
        return messages

    system_messages = [m for m in messages if m.get("role") == "system"]
    body = [m for m in messages if m.get("role") != "system"]

    running = estimate_messages(system_messages)
    kept_groups: list[list[dict[str, Any]]] = []
    kept_count = 0
    # 从最新往最老回填：最近 min_keep_messages 条无条件保留（保证当前这轮对话
    # 完整），其后再装得下就继续往前留；装不下就跳过看更老的（贪心）
    for group in reversed(_group_atomic(body)):
        cost = estimate_messages(group)
        if kept_count < min_keep_messages or running + cost <= budget:
            kept_groups.append(group)
            running += cost
            kept_count += len(group)
    kept_groups.reverse()
    result = [*system_messages, *[m for group in kept_groups for m in group]]
    return _truncate_to_budget(result, budget)
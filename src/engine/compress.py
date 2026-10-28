"""上下文压缩：按模型输入上限裁剪消息，避免撞上游硬限制。

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
   （保证当前这轮对话完整）。保留集合**必须是连续后缀**：跳着留（捞更老的
   小消息）会在集合里留洞，下一轮重算时洞变了，发往上游的前缀整段失效，
   上游前缀缓存只能命中 system 段。
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


def _part_text(part: Any) -> str:
    """单个 content part 的估算用文本。

    图片 part 按 base64 正文长度计入：早期实现对非文本 part 一律记 100 字符，
    而一张 1MB 的图 base64 后约 1.4M 字符，按 100 估等于「永远不触发压缩」——
    图片会话会一路撞上游输入上限。上游按图片的实际 token 计费，这里用 base64
    长度作代理（`estimate_tokens` 仍按字符类型加权）。
    """
    if not isinstance(part, dict):
        return part if isinstance(part, str) else ""
    text = part.get("text")
    if isinstance(text, str):
        return text
    if part.get("type") == "image_url":
        image = part.get("image_url")
        url = image.get("url") if isinstance(image, dict) else None
        if isinstance(url, str):
            return url
    return ""


def _content_text(content: Any) -> str:
    """一条 message 的 content（字符串或 part 数组）→ 估算用文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_part_text(part) for part in content)
    if content is None:
        return ""
    return str(content)


def _part_tokens(part: Any) -> float:
    """单个 content part 的 token 估算。

    图片 part 按 base64 正文长度线性计入：早期实现对非文本 part 一律记 100
    字符，而一张 1MB 的图 base64 后约 1.4M 字符，按 100 估等于「永远不触发
    压缩」——图片会话会一路撞上游输入上限。上游按图片的实际 token 计费，
    这里用 base64 长度作代理。

    **按长度一次算完，不逐字符加权**：请求体上限 16MB
    （`BodySizeLimitMiddleware`），逐字符跑 `estimate_tokens` 是千万级 Python
    迭代，会在事件循环里阻塞整个进程。base64 字符集只有字母数字 `+/=`，
    按 `_OTHER_WEIGHT` 记与逐字符加权（中文/数字权重）的偏差在纯启发式估算
    里可忽略。
    """
    if not isinstance(part, dict):
        return estimate_tokens(part) if isinstance(part, str) else 0.0
    text = part.get("text")
    if isinstance(text, str):
        return estimate_tokens(text)
    if part.get("type") == "image_url":
        image = part.get("image_url")
        url = image.get("url") if isinstance(image, dict) else None
        if isinstance(url, str):
            return _OTHER_WEIGHT * len(url)
    return 0.0


def _content_tokens(content: Any) -> float:
    """一条 message 的 content（字符串或 part 数组）→ 估算 token 数。"""
    if isinstance(content, str):
        return estimate_tokens(content)
    if isinstance(content, list):
        return sum(_part_tokens(part) for part in content)
    if content is None:
        return 0.0
    return estimate_tokens(str(content))


def _message_tokens(message: dict[str, Any]) -> float:
    total = _content_tokens(message.get("content"))
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


def _has_non_text_part(content: Any) -> bool:
    """content 是否含非文本 part（图片等）。"""
    return isinstance(content, list) and any(
        not (isinstance(part, dict) and isinstance(part.get("text"), str))
        for part in content)


def _truncate_content(content: Any) -> Any:
    """单条消息内容截断：字符串直接截；part 数组整体降级为纯文本。

    **含图片的 content 不截**：截 base64 只会得到一段无效 data URL（上游照样
    400），不如原样留给上游裁决；返回同一对象，调用方据此 `continue` 跳过。
    """
    if _has_non_text_part(content):
        return content
    text = _content_text(content)
    if len(text) <= _TRUNCATE_HEAD + _TRUNCATE_TAIL:
        return content
    truncated = text[:_TRUNCATE_HEAD] + _TRUNCATE_MARKER + text[-_TRUNCATE_TAIL:]
    return truncated


def _truncate_to_budget(messages: list[dict[str, Any]],
                        budget: float) -> list[dict[str, Any]]:
    """兜底：仍超预算时按内容长度从大到小逐条截断（system 不动）。

    每条只截一次；截完仍装不下就接受（上游还有硬上限兜着，这里只是尽力）。

    复杂度：先算一次全量估算与每条的可削减量，循环里只做**增量**更新——
    早期实现每轮重算 `estimate_messages(result)`，`min_keep_messages` 调大时
    候选条数多、且截断收益为 0 的条目会耗尽循环，退化成 O(n²) 同步 CPU
    （实测 400 条 × 8001 字符 = 60s，卡死事件循环）。截断收益为 0 时直接
    跳出，避免对不可能再缩小的条目空转。
    """
    result = list(messages)
    candidates = sorted(
        ((index, len(_content_text(message.get("content"))))
         for index, message in enumerate(result)
         if message.get("role") != "system"),
        key=lambda pair: pair[1], reverse=True)
    total = estimate_messages(result)
    for index, _length in candidates:
        if total <= budget:
            break
        before = _message_tokens(result[index])
        truncated = _truncate_content(result[index].get("content"))
        if truncated is result[index].get("content"):
            continue                      # 无可截断（已是最短形态）
        result[index] = {**result[index], "content": truncated}
        total -= before - _message_tokens(result[index])
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


def trim_to_budget(
    messages: list[dict[str, Any]],
    *,
    budget: float,
    min_keep_messages: int = DEFAULT_MIN_KEEP_MESSAGES,
) -> tuple[list[dict[str, Any]], int]:
    """按预算保留尾部；返回 `(结果, 第一条被保留正文消息在 body 中的绝对序号)`。

    `system` 全部保留且不计入 body 序号；`assistant.tool_calls` 与其后连续
    `tool` 结果同生共死；至少保留最近 `min_keep_messages` 条；保留集合是**连续
    后缀**（装不下就停，不跳着捞更老的小消息，否则前缀带洞、缓存无法复用）。
    仍超预算时按单条内容截断（`_truncate_to_budget`）。

    序号是给**粘性裁剪**记窗口起点用的（见 `engine/sticky_trim.py`）：下一轮
    只要该位置的消息没变、且裁后总量仍在预算内，就能复用同一前缀，上游前缀
    缓存才命中得上。调用方保证 `budget > 0`。
    """
    system_messages = [m for m in messages if m.get("role") == "system"]
    body = [m for m in messages if m.get("role") != "system"]

    groups = _group_atomic(body)
    starts: list[int] = []
    cursor = 0
    for group in groups:
        starts.append(cursor)
        cursor += len(group)

    running = estimate_messages(system_messages)
    kept_groups: list[list[dict[str, Any]]] = []
    kept_count = 0
    start_index = len(body)
    # 从最新往最老回填，**只保留连续后缀**：装不下就停，不再跳过去捞更老的
    # 小消息。跳着留会让「保留集合」带洞，下一轮重算时洞的位置一变，发往上游
    # 的前缀就整段失效（前缀缓存只能命中 system 段）；连续后缀则只会在尾部
    # 增长，配合粘性裁剪可以长期复用同一前缀。
    # 最近 min_keep_messages 条无条件保留（保证当前这轮对话完整）。
    for position in range(len(groups) - 1, -1, -1):
        group = groups[position]
        cost = estimate_messages(group)
        if kept_count >= min_keep_messages and running + cost > budget:
            break
        kept_groups.append(group)
        running += cost
        kept_count += len(group)
        start_index = starts[position]
    kept_groups.reverse()
    result = [*system_messages, *[m for group in kept_groups for m in group]]
    return _truncate_to_budget(result, budget), start_index


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

    trimmed, _start = trim_to_budget(messages, budget=budget,
                                     min_keep_messages=min_keep_messages)
    return trimmed
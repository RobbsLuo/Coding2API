"""tool 输出确定性压缩（默认关，热更开关 `tool_output_compress_enabled`）。

只动 `role=tool` 的纯文本 `content`：

1. 去掉 ANSI 转义序列（进度条 / 彩色日志，无信息量却照样占 token）；
2. 折叠**连续重复行**（`xxx (×12)`），日志刷屏时这一步最省；
3. 超长时保留「头 N 行 + 尾 N 行 + 任何命中错误关键字的行」，中间标注
   `[... 省略 K 行 ...]`——错误行永远不丢，避免把「为什么失败」压没了。

**确定性**：同样输入必得同样输出。客户端每轮重发完整历史时，同一段 tool
输出每轮都压成同一份文本，上游前缀缓存才能继续命中（同 `compress.py` 的
连续后缀理由）。不做模型化摘要：网关不引入第二个模型依赖，也不改写错误行原文。

只压缩超过 `max_chars` 的文本：阈值以下原样返回（含 ANSI），保证默认配置下
未超限的输出逐字节不变。
"""

from __future__ import annotations

import re
from typing import Any

# 默认阈值：约 2K token 的纯 ASCII，足以覆盖正常命令输出，又把「刷屏日志」
# 挡在门外。热更层可改（`tool_output_compress_max_chars`）。
DEFAULT_MAX_CHARS = 8000
DEFAULT_HEAD_LINES = 40
DEFAULT_TAIL_LINES = 40

# CSI / OSC 之类的 ANSI 转义：`ESC [ ... m` 是常见形态，按宽松正则收口。
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _is_error_line(line: str) -> bool:
    """是否值得无条件保留的行（错误 / 异常 / 超时等定位信息）。"""
    lowered = line.lower()
    return any(marker in lowered for marker in _ERROR_MARKERS)


_ERROR_MARKERS: tuple[str, ...] = (
    "error", "fail", "fatal", "exception", "traceback", "panic",
    "denied", "timeout", "timed out", "错误", "失败", "异常", "超时", "拒绝",
)


def _collapse_runs(lines: list[str]) -> list[str]:
    """连续重复行折叠成一行 + `(×N)` 计数；单行不影响。"""
    collapsed: list[str] = []
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        repeat = 1
        while index + repeat < total and lines[index + repeat] == line:
            repeat += 1
        collapsed.append(line if repeat == 1 else f"{line} (×{repeat})")
        index += repeat
    return collapsed


def _hard_trim(text: str, max_chars: int) -> str:
    """最后兜底：按字符数留头尾，中间用标记代替（保证不超阈值）。"""
    marker = "\n[... 省略 ...]\n"
    if max_chars <= len(marker):
        return text[:max_chars]
    keep = max_chars - len(marker)
    head = keep // 2
    return text[:head] + marker + text[-(keep - head):]


def compress_tool_text(text: str, *, max_chars: int = DEFAULT_MAX_CHARS,
                       head_lines: int = DEFAULT_HEAD_LINES,
                       tail_lines: int = DEFAULT_TAIL_LINES) -> str:
    """单段 tool 输出 → 压缩后文本；未超阈值时**原样**返回（不碰 ANSI）。"""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    lines = _ANSI_RE.sub("", text).splitlines()
    collapsed = _collapse_runs(lines)
    head = max(head_lines, 0)
    tail = max(tail_lines, 0)
    if len(collapsed) > head + tail:
        middle = collapsed[head:len(collapsed) - tail] if tail else collapsed[head:]
        kept = [line for line in middle if _is_error_line(line)]
        omitted = len(middle) - len(kept)
        parts = [*collapsed[:head], f"[... 省略 {omitted} 行 ...]", *kept]
        if tail:
            parts.extend(collapsed[-tail:])
        text = "\n".join(parts)
    else:
        text = "\n".join(collapsed)
    return _hard_trim(text, max_chars) if len(text) > max_chars else text


def compress_tool_outputs(messages: list[dict[str, Any]], *,
                          max_chars: int = DEFAULT_MAX_CHARS,
                          head_lines: int = DEFAULT_HEAD_LINES,
                          tail_lines: int = DEFAULT_TAIL_LINES) -> list[dict[str, Any]]:
    """压缩消息列表里的 tool 结果；无改动时返回**原列表**（零拷贝）。"""
    changed = False
    result: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if (isinstance(message, dict) and message.get("role") == "tool"
                and isinstance(content, str)):
            compressed = compress_tool_text(
                content, max_chars=max_chars,
                head_lines=head_lines, tail_lines=tail_lines)
            if compressed != content:
                result.append({**message, "content": compressed})
                changed = True
                continue
        result.append(message)
    return result if changed else messages

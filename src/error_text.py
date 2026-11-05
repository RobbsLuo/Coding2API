"""异常 → 一行日志/展示文本。

`str(error)` 对部分异常为空（`TimeoutError`、某些 `httpx.ConnectError`），
直接 `"%s" % error` 会打出「quota probe failed for cred_x: 」这种无因日志
（2026-11 实测 89/91 条额度探测失败、54/58 条动态模型拉取失败都是空原因），
排障时只能看到「失败了」却不知道是超时、DNS 还是协议错误。

统一带上异常类名，保证至少能区分大类；有消息时保留消息（压缩空白、限长）。
"""

from __future__ import annotations

import re

_WHITESPACE = re.compile(r"\s+")


def error_text(error: BaseException, *, limit: int = 200) -> str:
    """异常 → `类名: 消息`；消息为空时只有类名（如 `TimeoutError`）。"""
    message = _WHITESPACE.sub(" ", str(error)).strip()
    if not message:
        return type(error).__name__
    if len(message) > limit:
        message = message[:limit] + "…"
    return f"{type(error).__name__}: {message}"
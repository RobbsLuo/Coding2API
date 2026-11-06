"""异常 → 一行日志/展示文本。

`str(error)` 对部分异常为空（`TimeoutError`、某些 `httpx.ConnectError`），
直接 `"%s" % error` 会打出「quota probe failed for cred_x: 」这种无因日志
（2026-11 实测 89/91 条额度探测失败、54/58 条动态模型拉取失败都是空原因），
排障时只能看到「失败了」却不知道是超时、DNS 还是协议错误。

统一带上异常类名，保证至少能区分大类；有消息时保留消息（压缩空白、限长）。

**出口统一过日志脱敏**：`UpstreamHTTPError` 的消息里带 `body_hint`（上游错误
响应体，最长 2 KB）。上游把请求原样回显、或在错误体里带上鉴权字段时，
这些文本会原样进 logger → 管理台日志页。`webapp.log_buffer` 的
`redact_or_drop` 是最后一道门，但它的标识符正则只覆盖 token/secret/password/
cookie/authorization/api_key，漏了 `jwt` / `ticket` / `code` / `openId` 与
邮箱/账号。所以在**产生日志文本的这一处**先过同一套判定：命中敏感形态就
只留类名，宁可少一条排障细节，也不把凭证推给浏览器。
"""

from __future__ import annotations

import re

from .webapp.log_buffer import redact_or_drop

_WHITESPACE = re.compile(r"\s+")

#: 消息上限：够放上游业务码与一句文案，又不至于把一段响应体灌进日志页。
_LIMIT = 200


def error_text(error: BaseException) -> str:
    """异常 → `类名: 消息`；消息为空时只有类名（如 `TimeoutError`）。

    消息命中日志脱敏规则（疑似凭证/PII）时退化成只有类名：类名已经能区分
    超时/协议/传输大类，具体响应体不值得冒泄露风险。
    """
    message = _WHITESPACE.sub(" ", str(error)).strip()
    if not message:
        return type(error).__name__
    if len(message) > _LIMIT:
        message = message[:_LIMIT] + "…"
    text = f"{type(error).__name__}: {message}"
    return text if redact_or_drop(text) is not None else type(error).__name__

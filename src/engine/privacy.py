"""出站请求体隐私脱敏（热更开关，默认关）。

**为什么在网关侧做**：本服务是共享凭证池，上游收到的请求体代表「池主」。
用户把工单 / 日志 / 配置粘进对话时，邮箱、手机号、内网地址、带密码的连接串
会原样发给第三方上游；日志脱敏只解决落盘，不解决出站。

**口径（与日志脱敏同取向，比它更保守）**：

- 只改**出站副本**，绝不改 `request.raw`（会话粘性指纹与续写都读原文）。
- 占位符单向、不做还原：上游若把占位符回显，客户端看到的是占位符。宁可少
  一点信息，也不冒险把可还原的映射表留在进程里（`10router` 有 restore 侧，
  本仓不跟进）。
- 只处理 `messages[].content` 文本（含 text 块）与 `tools[].function.description`：
  `tool_calls` 的 `arguments` 是模型要执行的代码/数据，改它会改坏语义。
- 规则名拼错/为空一律忽略，绝不因为一条配置笔误把请求打死。

规则（`PRIVACY_RULES`，逗号或空格分隔，默认空 = 关闭）：

- `url_credentials`：`scheme://user:pass@host` 里的 userinfo
- `email`
- `cn_mobile`：中国大陆 11 位手机号
- `private_ip`：RFC1918 私有地址与 `172.16/12`
"""
from __future__ import annotations

import copy
import re
from functools import lru_cache
from typing import Any

__all__ = ["RULE_ORDER", "parse_rules", "redact_payload", "redact_text"]

# 规范顺序（也是应用顺序）：先吃 URL userinfo，否则邮箱/手机号会先被替换，
# `user:pass@host` 的正则就匹配不到了。
RULE_ORDER: tuple[str, ...] = ("url_credentials", "email", "cn_mobile", "private_ip")

_RULE_PATTERNS: dict[str, tuple[re.Pattern[str], str]] = {
    "url_credentials": (
        re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+@"), "[credentials]@"),
    "email": (
        re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"),
        "[email]"),
    # 前后不能紧贴数字：订单号里的 11 位片段不是手机号。
    "cn_mobile": (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[phone]"),
    "private_ip": (
        re.compile(r"(?<![\d.])(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
                   r"|192\.168\.\d{1,3}\.\d{1,3}"
                   r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?![\d.])"),
        "[private-ip]"),
}


def parse_rules(raw: str | None) -> tuple[str, ...]:
    """配置文本 → 生效规则（规范顺序、去重、忽略未知名）。"""
    if not raw:
        return ()
    names = {part.strip().lower() for part in re.split(r"[,\s]+", raw) if part.strip()}
    return tuple(rule for rule in RULE_ORDER if rule in names)


@lru_cache(maxsize=32)
def _compiled(raw: str) -> tuple[tuple[re.Pattern[str], str], ...]:
    """规则文本 → 编译后的 (正则, 占位符)；同配置只编译一次。"""
    return tuple(_RULE_PATTERNS[rule] for rule in parse_rules(raw))


def redact_text(text: str, raw_rules: str | None) -> tuple[str, int]:
    """一段文本按规则打码；返回 (结果, 命中处数)。"""
    rules = _compiled(raw_rules or "")
    hits = 0
    for pattern, replacement in rules:
        text, count = pattern.subn(replacement, text)
        hits += count
    return text, hits


def redact_payload(payload: dict[str, Any],
                   raw_rules: str | None) -> tuple[dict[str, Any], int]:
    """出站请求体副本按规则打码；返回 (副本, 命中处数)。

    无规则时**原样返回**（零拷贝、逐字节不变，默认关）。
    """
    rules = _compiled(raw_rules or "")
    if not rules:
        return payload, 0
    body = copy.deepcopy(payload)
    hits = 0
    messages = body.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                message["content"], count = redact_text(content, raw_rules)
                hits += count
            elif isinstance(content, list):
                for block in content:
                    if (isinstance(block, dict) and block.get("type") == "text"
                            and isinstance(block.get("text"), str)):
                        block["text"], count = redact_text(block["text"], raw_rules)
                        hits += count
    tools = body.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            function = tool.get("function") if isinstance(tool, dict) else None
            if isinstance(function, dict) and isinstance(function.get("description"), str):
                function["description"], count = redact_text(
                    function["description"], raw_rules)
                hits += count
    return body, hits
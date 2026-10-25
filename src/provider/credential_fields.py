"""华为云系渠道（CodeArts / OfficeAce）共用的凭证字段解析。

两家凭证落盘形状一致（STS 临时 AK/SK + DPoP 私钥 + ISO8601 到期时间），
解析规则曾各写一份且字节级相同，统一到这里。协议私有部分（字段名清单、
client_id 默认值、刷新绑定字段）仍留在各渠道，本模块只做「取值/归一」。
"""

from __future__ import annotations

import json
from typing import Any

from .token_expiry import normalize_epoch


def text_field(raw: dict[str, Any], *keys: str) -> str:
    """按优先级取第一个非空字符串字段。"""
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def epoch_from_iso(value: Any) -> int:
    """ISO8601（带 `Z` 或偏移）→ epoch 秒；坏值 / 缺失返回 0。"""
    if not isinstance(value, str) or not value.strip():
        return 0
    from datetime import datetime

    text = value.strip().replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError:
        return 0


def expiration_from_raw(raw: Any) -> int:
    """到期时间归一：数字（epoch，可为毫秒）/ ISO8601 / 数字字符串 → 秒。"""
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, (int, float)):
        return normalize_epoch(int(raw))
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        try:
            return normalize_epoch(int(text))
        except ValueError:
            pass
        return epoch_from_iso(text)
    return 0


def jwk_from_raw(raw: Any) -> dict[str, str]:
    """DPoP 私钥 JWK：只收字符串字段，畸形输入返回空（由刷新时报错兜底）。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if isinstance(value, str)}


def relogin_required(body: bytes, markers: tuple[str, ...]) -> str:
    """令牌端点响应体 → 该凭证是否已**不可自愈**，需要重新登录。

    返回可直接展示的原因（不含上游正文，符合不回显上游响应的约束）；
    可重试的失败（网络、5xx、限流、绑定项齐全的普通 4xx）返回空串。

    为什么必须在这里判终态而不是让 RefreshTask 重试：refresh_token 一次性，
    被消费/失配之后每次重试都是同一张废票。判错的代价是「一次偶发故障永久
    废掉一条好凭证」，所以只认各渠道 `_RELOGIN_MARKERS` 里上游点名的那几类。
    """
    text = body.decode("utf-8", errors="replace").lower()
    if any(marker in text for marker in markers):
        return "刷新令牌已失效，需重新登录"
    return ""

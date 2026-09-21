"""access token 到期时间提取（渠道中立，不依赖任何 provider 子模块）。

凭证 JSON 自带的 `expires_at` 不总是可靠：实测 CodeBuddy 的 token 响应不含
`created_at` / `expires_in` / `expires_at`，OAuth 登录与刷新后该字段恒为 0。
`CodeBuddyCredential.needs_refresh` 首行要求 `expires_at > 0`，于是预刷新任务
永不触发——token 到期后上游回 401，被分类成 DEAD 硬禁用，而 `revive` 也不会
补一次刷新，凭证无法自愈。

两个渠道的 access token 都是标准 JWT，payload 里的 `exp` 是上游签发的权威
到期时间，作为 `expires_at` 缺失/非法时的回落来源。两者都拿不到时返回 0
（未知）：**不猜本地 TTL**，否则管理台会显示一个凭空捏造的到期预警。

本模块刻意不 import provider 子模块：`db/repo.py` 也要用它写 `token_expires_at`
列，反向依赖 `provider.codebuddy.credential` 会把 `provider.codebuddy.events`
→ `engine.sse` 拖进 db 层。
"""

from __future__ import annotations

import base64
import json
from typing import Any

# 凭证 JSON 里可能出现 access token 的键名（顺序即优先级）。
_TOKEN_KEYS: tuple[str, ...] = (
    "bearer_token", "bearerToken", "accessToken", "access_token", "token")
# 凭证 JSON 里可能出现到期时间的键名。
_EXPIRY_KEYS: tuple[str, ...] = ("expires_at", "expiresAt")

# 上游可能返回毫秒时间戳；超过该阈值（≈ 公元 33658 年）视为毫秒。
_MILLISECOND_THRESHOLD = 1_000_000_000_000


def normalize_epoch(value: int) -> int:
    """毫秒时间戳归一到秒；>1e12 视为毫秒。"""
    return value // 1000 if value > _MILLISECOND_THRESHOLD else value


def _claim(payload: dict[str, Any], key: str) -> int:
    """取一个时间类 claim（秒）；缺失/布尔/非数字/非正一律 0。"""
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    value = normalize_epoch(int(value))
    return value if value > 0 else 0


def jwt_times(token: str) -> tuple[int, int]:
    """取 JWT payload 的 `(iat, exp)`（秒）；非 JWT / 结构异常返回 (0, 0)。

    只解码不验签：签名由上游校验，这里仅用于展示与预刷新判定，
    伪造的凭证交上去也会被上游拒绝，无需在本地重复校验。

    同时取 iat 与 exp：exp 决定「还剩多久」，iat 决定「进度条满量程是多少」。
    缺 iat 时无法知道 token 寿命，进度条只能退化成「未知」——用固定量程
    会把 50 天的 CodeBuddy token 永远画成满格。
    """
    if not isinstance(token, str):
        return 0, 0
    parts = token.split(".")
    if len(parts) != 3 or not parts[1]:
        return 0, 0
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        # binascii.Error 继承自 ValueError，JSONDecodeError 亦然
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return 0, 0
    if not isinstance(payload, dict):
        return 0, 0
    return _claim(payload, "iat"), _claim(payload, "exp")


def jwt_expiry(token: str) -> int:
    """`jwt_times` 的到期时间便捷入口。"""
    return jwt_times(token)[1]


def credential_expiry(data: dict[str, Any]) -> int:
    """凭证 JSON 的 access token 到期 epoch（秒）；不可得返回 0。

    优先取显式 `expires_at`（上游直接给出的，比 JWT 更贴渠道语义），
    缺失或非法时回落到 access token 的 JWT `exp`。
    """
    if not isinstance(data, dict):
        return 0
    for key in _EXPIRY_KEYS:
        raw = data.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        value = normalize_epoch(int(raw))
        if value > 0:
            return value
    for key in _TOKEN_KEYS:
        token = data.get(key)
        if isinstance(token, str):
            expiry = jwt_expiry(token)
            if expiry:
                return expiry
    return 0


def credential_token_times(data: dict[str, Any]) -> tuple[int, int]:
    """凭证的 `(签发, 到期)` epoch（秒），各自不可得时为 0。

    到期优先显式字段、回落 JWT（同 `credential_expiry`）；签发时间只有 JWT
    的 `iat` 能给（上游不会单独回传），所以显式 `expires_at` 的渠道拿不到
    `iat` 时签发时间为 0——此时进度条按「未知寿命」处理，而不是拿回填时刻
    冒充（回填时刻是「我们什么时候写这条记录」，不是「上游什么时候签发的」）。

    挑选规则与 `credential_expiry` 保持一致：遍历 token 键、取第一个真正
    带得出信息的那个，而不是碰到的第一个字符串就收手——否则 `access_token`
    是明文、`token` 才是 JWT 时，到期取得到、签发时间却莫名丢成 0。
    """
    expires_at = credential_expiry(data)
    if not isinstance(data, dict):
        return 0, expires_at
    for key in _TOKEN_KEYS:
        token = data.get(key)
        if isinstance(token, str):
            issued_at, jwt_exp = jwt_times(token)
            if issued_at or jwt_exp:
                # 显式 expires_at 更贴渠道语义：JWT 只补 iat，不覆盖到期时间
                return issued_at, expires_at or jwt_exp
    return 0, expires_at

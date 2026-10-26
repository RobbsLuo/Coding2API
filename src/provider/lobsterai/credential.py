"""LobsterAI（有道龙虾）凭证类型与解析。

上游的登录产物是「一组必须一起存」的字段，其中三个是**客户端生成的身份**、
由上游在续期时校验：

| 字段 | 来源 | 作用 |
|---|---|---|
| `access_token` | 上游签发 | `Authorization: Bearer` |
| `refresh_token` | 上游签发 | 静默续期（**不保证轮转**，缺失时沿用旧值） |
| `uuid` | **本地生成** | 设备/会话标识，exchange 与 refresh 都要带 |
| `first_keyfrom` | **本地生成** | 首次登录时间戳（毫秒），续期时原样回放 |
| `latest_keyfrom` | **本地生成** | 最近活动时间戳，续期时原样回放（不随续期更新） |

**丢一个身份字段的后果不是「少一个头」，而是「永远续不了期」**：上游把
`uuid` / `firstKeyfrom` / `latestKeyfrom` 当作同一台客户端的指纹，缺失或
不匹配时 refresh 直接被拒。因此这三个字段与 refresh_token 同等重要，
缺任何一个的凭证导入时就要拒绝（`import_credential`），而不是等续期失败
才暴露。

`uid` 的取值按四级回落（`user.id` → `user.userId` → `user.yid` →
`access_token` 的 sha256 前 16 位）。末级是**兜底**：不同账号形态下上游
给哪个 user 字段并不固定，全空时若拿不到稳定 id，账号池里多个账号会互相
覆盖（`credentials.add` 按 id 去重）。用 token 哈希而非 token 本身是
为了不把明文 token 写进主键（主键会出现在面板、日志与导出里）。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any

from ..base import UpstreamProtocolViolation
from ..token_expiry import credential_expiry


def _text(raw: dict[str, Any], *keys: str) -> str:
    """按优先级取第一个非空字符串字段。"""
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _int(raw: dict[str, Any], *keys: str) -> int:
    """按优先级取第一个正整数（毫秒时间戳会归一到秒）。"""
    for key in keys:
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = int(value)
        if number > 1_000_000_000_000:      # 毫秒 → 秒
            number //= 1000
        if number > 0:
            return number
    return 0


@dataclass(slots=True)
class LobsteraiCredential:
    """归一化后的 LobsterAI 凭证（扁平形落盘）。"""

    uid: str = ""
    access_token: str = ""
    refresh_token: str = ""
    expires_at: int = 0
    uuid: str = ""
    first_keyfrom: str = ""
    latest_keyfrom: str = ""
    user_id: str = ""
    nickname: str = ""

    def token_expires_at(self) -> int:
        """access token 到期 epoch（秒）：显式 expiresAt 缺失时回落 JWT `exp`。

        显式值优先于 JWT 派生值：上游签发的到期时间是权威口径，而 JWT 的
        `exp` 只是它的另一份表达。两者都拿不到时返回 0（未知）——**不猜
        本地 TTL**，否则管理台会显示一个凭空捏造的到期预警。
        """
        return credential_expiry(self.to_dict())

    def needs_refresh(self, skew_seconds: int, now: int | None = None) -> bool:
        """到期前 `skew_seconds` 是否需要刷新。

        到期未知（0）→ **保守判 True**：宁可多刷一次（刷新是幂等的），也不要
        让凭证在没人察觉的时候过期、被上游 401 打成硬禁用。
        """
        expires_at = self.token_expires_at()
        if expires_at <= 0:
            return True
        current = int(now if now is not None else time.time())
        return current + skew_seconds >= expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "accessToken": self.access_token,
            "refreshToken": self.refresh_token,
            "expiresAt": self.expires_at,
            "uuid": self.uuid,
            "firstKeyfrom": self.first_keyfrom,
            "latestKeyfrom": self.latest_keyfrom,
            "userId": self.user_id,
            "nickname": self.nickname,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LobsteraiCredential:
        """宽容解析：camelCase / snake_case 都认（两种写法上游都用过）。"""
        if not isinstance(raw, dict):
            raise UpstreamProtocolViolation("credential is not an object")
        return cls(
            uid=_text(raw, "uid", "account_id", "accountId"),
            access_token=_text(raw, "accessToken", "access_token", "token"),
            refresh_token=_text(raw, "refreshToken", "refresh_token"),
            expires_at=_int(raw, "expiresAt", "expires_at"),
            uuid=_text(raw, "uuid", "installUuid", "install_uuid"),
            first_keyfrom=_text(raw, "firstKeyfrom", "first_keyfrom"),
            latest_keyfrom=_text(raw, "latestKeyfrom", "latest_keyfrom"),
            user_id=_text(raw, "userId", "user_id", "yid"),
            nickname=_text(raw, "nickname", "name"),
        )


def resolve_uid(payload: dict[str, Any], access_token: str) -> str:
    """登录/续期响应 → 账号唯一 ID（四级回落）。

    顺序与上游实现一致：`user.id` → `user.userId` → `user.yid` →
    `access_token` 的 sha256 前 16 位。末级保证「任何情况下都有稳定 id」。
    """
    user = payload.get("user")
    user = user if isinstance(user, dict) else {}
    for key in ("id", "userId", "yid"):
        value = user.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return hashlib.sha256(access_token.encode("utf-8")).hexdigest()[:16]

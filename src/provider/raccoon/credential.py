"""小浣熊凭证：网页会话 JWT（`xhx_token`）。

上游没有「API Key」形态，凭据就是浏览器登录后的 JWT。导入侧要尽量宽容——
用户可能粘：

* 纯 JWT（`eyJ…`）；
* `{"access_token": "eyJ…"}` / `{"token": …}` / `{"xhx_token": …}`；
* 整段 cookie（`xhx_token=eyJ…; user_id=…`，从浏览器 DevTools 复制）。

`uid` / 过期时间从 JWT claims 解出（**不验签**：验签是上游的事，这里只把
`sub` 当账号标识、`exp` 当「30 天后要重取」的提示）。解不出来就留空，
不影响调用。
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

#: 允许的入参键名（按优先级）。`token` / `jwt` 是用户手写习惯，`xhx_token`
#: 是浏览器 cookie 的原名。
_TOKEN_KEYS = ("access_token", "token", "jwt", "xhx_token")


def _decode_segment(segment: str) -> dict[str, Any]:
    text = segment.replace("-", "+").replace("_", "/")
    text += "=" * (-len(text) % 4)
    try:
        data = json.loads(base64.b64decode(text).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def jwt_claims(token: str) -> dict[str, Any]:
    """JWT 中间段 → claims；不是 JWT（段数不对 / base64 坏）返回空 dict。"""
    parts = token.split(".")
    if len(parts) != 3 or not parts[1]:
        return {}
    return _decode_segment(parts[1])


def token_from_cookie(raw: str) -> str:
    """整段 cookie / 只有 `xhx_token=…` 的串 → token；拿不到返回空串。"""
    for chunk in raw.split(";"):
        name, _, value = chunk.strip().partition("=")
        if name.strip() == "xhx_token" and value.strip():
            return value.strip()
    return ""


@dataclass(slots=True)
class RaccoonCredential:
    """归一后的凭证。字段全为字符串/整数，便于原样落库。"""

    access_token: str = ""
    uid: str = ""
    nickname: str = ""
    #: JWT `exp`（epoch 秒）；0 表示未提供/不可解析
    expires_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "uid": self.uid,
            "nickname": self.nickname,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RaccoonCredential:
        """落库形态 → 凭证；字段缺失按空处理（调用方负责校验 token 非空）。"""
        expires = raw.get("expires_at")
        return cls(
            access_token=_text(raw.get("access_token")),
            uid=_text(raw.get("uid")),
            nickname=_text(raw.get("nickname")),
            expires_at=expires if isinstance(expires, int) and not isinstance(
                expires, bool) else 0,
        )

    @classmethod
    def from_import(cls, raw: Any) -> RaccoonCredential:
        """导入入参 → 凭证。空 token 由调用方（`import_credential`）报错。"""
        if isinstance(raw, str):
            return cls._from_token(raw)
        if not isinstance(raw, dict):
            return cls()
        token = ""
        for key in _TOKEN_KEYS:
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                token = value.strip()
                break
        if not token:
            cookie = raw.get("cookie")
            if isinstance(cookie, str):
                token = token_from_cookie(cookie)
        credential = cls._from_token(token)
        # 显式字段优先于 JWT 推断（用户可能填了昵称）
        credential.uid = _text(raw.get("uid")) or _text(raw.get("user_id")) \
            or credential.uid
        credential.nickname = _text(raw.get("nickname")) or _text(
            raw.get("user_name")) or credential.nickname
        return credential

    @classmethod
    def _from_token(cls, token: str) -> RaccoonCredential:
        token = token.strip()
        claims = jwt_claims(token)
        uid = _text(claims.get("sub")) or _text(claims.get("uid"))
        expires = claims.get("exp")
        return cls(
            access_token=token,
            uid=uid,
            nickname=_text(claims.get("name")) or _text(claims.get("nickname")),
            expires_at=expires if isinstance(expires, int) and not isinstance(
                expires, bool) else 0,
        )


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""
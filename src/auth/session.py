"""会话 Cookie：HMAC 签名，不落库（重启失效可接受）。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

SESSION_COOKIE = "coding2api_session"


def _sign(payload_b64: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"),
                    hashlib.sha256).hexdigest()


def create_session_token(username: str, secret: str, *, issued_at: int | None = None,
                         ttl_seconds: int = 12 * 3600) -> str:
    payload = {"u": username, "iat": int(issued_at if issued_at is not None else time.time()),
               "exp": int((issued_at if issued_at is not None else time.time()) + ttl_seconds)}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{payload_b64}.{_sign(payload_b64, secret)}"


def verify_session_token(token: str, secret: str, *, now: int | None = None) -> str | None:
    """返回用户名；签名无效、过期或格式错误返回 None。"""
    if not token or "." not in token:
        return None
    payload_b64, _, signature = token.partition(".")
    if not hmac.compare_digest(_sign(payload_b64, secret), signature):
        return None
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    username = payload.get("u")
    expires_at = payload.get("exp")
    if not isinstance(username, str) or not username:
        return None
    if not isinstance(expires_at, int) or isinstance(expires_at, bool):
        return None
    if expires_at < int(now if now is not None else time.time()):
        return None
    return username

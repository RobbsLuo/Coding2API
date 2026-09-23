"""会话 Cookie：HMAC 签名，不落库（重启失效可接受）。

payload 里的 `ep` 是 users.session_epoch 的快照（B5）：Cookie 无状态，
无法逐条作废，于是改密/禁用/改角色时把 DB 里的 epoch +1，这里每请求比对，
不等即失效（见 deps.principal_from_request）。

兼容性：老版本签的 Cookie 没有 `ep` 字段，一律按 0 处理而不是判非法——
否则升级后所有已登录用户会被集体登出。新字段的值非法（类型错/负数）才拒绝。
"""

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


def create_session_token(username: str, secret: str, *, epoch: int = 0,
                         issued_at: int | None = None,
                         ttl_seconds: int = 12 * 3600) -> str:
    issued = int(issued_at if issued_at is not None else time.time())
    payload = {"u": username, "ep": int(epoch), "iat": issued,
               "exp": issued + ttl_seconds}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{payload_b64}.{_sign(payload_b64, secret)}"


def verify_session_token(token: str, secret: str, *,
                         now: int | None = None) -> tuple[str, int] | None:
    """返回 `(用户名, 会话 epoch)`；签名无效、过期或格式错误返回 None。"""
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
    # 缺 ep 按 0（老 Cookie 兼容）；显式写了非法值才拒绝
    epoch = payload.get("ep", 0)
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
        return None
    return username, epoch

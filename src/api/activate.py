"""账号激活（B5）：用一次性令牌自设密码。**无鉴权**（令牌即凭证）。

与 `/api/auth/*` 的其余端点不同，这里不依赖会话 Cookie：用户还没法登录，
拿到的是 admin 带外转交的令牌。因此：

- 令牌明文不落库，只存 SHA-256 摘要；校验用摘要查人。
- 一次性：成功即清空（repository.set_password 会清），二次使用失败。
- 有 TTL（默认 24h），过期同样失败。
- 响应绝不回显密码或摘要。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends

from ..audit.actions import ACTION_USER_ACTIVATE
from ..auth.users import create_password_hash
from ..compat.openai.request import InvalidRequest
from .admin_auth import MIN_PASSWORD_LENGTH
from .admin_users import digest_activation_token
from .deps import Services, csrf_protected


def _lookup(services: Services, token: str):
    """按令牌摘要找用户并校验有效期；任何失败都归一为 400（不区分原因，
    避免成为「这个令牌是否存在」的探测器）。"""
    if not token:
        raise InvalidRequest("activation token is required")
    row = services.user_repo.find_by_activation(digest_activation_token(token))
    if row is None or row["activation_expires_at"] is None:
        return None
    if int(row["activation_expires_at"]) < int(time.time()):
        return None
    return row


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/api/auth/activate")
    async def describe_activation(token: str = ""):
        """校验令牌并返回待激活用户名，供前端显示「正在为 X 设置密码」。"""
        row = _lookup(services, token)
        if row is None:
            raise InvalidRequest("invalid or expired activation token")
        return {"username": row["username"], "valid": True}

    @router.post("/api/auth/activate")
    async def activate(payload: dict, _csrf: None = Depends(csrf_protected)):
        token = str(payload.get("token") or "")
        new_password = str(payload.get("password") or "")
        if len(new_password) < MIN_PASSWORD_LENGTH:
            raise InvalidRequest(
                f"password must be at least {MIN_PASSWORD_LENGTH} characters")
        row = _lookup(services, token)
        if row is None:
            raise InvalidRequest("invalid or expired activation token")
        username = row["username"]
        # set_password 会同时清掉 activation_digest（一次性）并 bump epoch
        services.user_repo.set_password(username, create_password_hash(new_password))
        services.audit.record(actor=username, action=ACTION_USER_ACTIVATE,
                              target=username, detail="自设密码完成激活")
        return {"ok": True, "username": username}

    return router

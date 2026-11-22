"""账号激活：用一次性令牌自设密码。**无鉴权**（令牌即凭证）。

与 `/api/auth/*` 的其余端点不同，这里不依赖会话 Cookie：用户还没法登录，
拿到的是 admin 带外转交的令牌。因此：

- 令牌明文不落库，只存 SHA-256 摘要；校验用摘要查人。
- 一次性：成功即清空（repository.set_password 会清），二次使用失败。
- 有 TTL（默认 24h），过期同样失败。
- 响应绝不回显密码或摘要。
- 令牌无效计入登录同款 IP 失败窗口：否则无鉴权入口等价于「无限次试令牌」，
  而试出令牌就等于接管账号。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request

from ..audit.actions import ACTION_USER_ACTIVATE
from ..auth.users import create_password_hash
from ..compat.openai.request import InvalidRequest
from .admin_auth import MIN_PASSWORD_LENGTH
from .admin_users import digest_activation_token
from .deps import Services, csrf_protected, request_ip

# 限流用户名维度只用令牌前缀：整枚令牌不进内存键、不进任何日志。
_THROTTLE_KEY_PREFIX_LENGTH = 8


def _throttle_key(token: str) -> str:
    """限流键：`activate:<令牌前缀>`（与登录的用户名维度共用一个窗口表）。"""
    return f"activate:{token[:_THROTTLE_KEY_PREFIX_LENGTH]}"


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


def _checked_lookup(services: Services, request: Request, token: str):
    """带 IP 失败窗口的令牌校验：无效令牌计一次失败，超限由全局处理器回 429。

    顺序照抄 `/api/auth/login`：哈希/查库前先卡传输层窗口（超限直接 429），
    失败时再 `check` 一次用户名窗口并 `record_failure`；成功 `record_success`
    清掉该令牌前缀的计数（不误伤同 IP 的其他人）。
    """
    ip = request_ip(request, services.settings)
    throttle = services.login_throttle
    key = _throttle_key(token)
    throttle.check_transport_windows(ip=ip)
    row = _lookup(services, token)
    if row is None:
        throttle.check(ip=ip, username=key)
        throttle.record_failure(ip=ip, username=key)
        raise InvalidRequest("invalid or expired activation token")
    throttle.record_success(username=key)
    return row


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/api/auth/activate")
    async def describe_activation(request: Request, token: str = ""):
        """校验令牌并返回待激活用户名，供前端显示「正在为 X 设置密码」。

        **token 在 query string 里会进前置反代的访问日志**（nginx `$request` /
        `$request_uri`），推荐改用 `POST /api/auth/activate/describe`
        （令牌在 body 里，不落访问日志）。GET 仅为兼容已发布的前端而保留。
        """
        row = _checked_lookup(services, request, token)
        return {"username": row["username"], "valid": True}

    @router.post("/api/auth/activate/describe")
    async def describe_activation_post(request: Request, payload: dict,
                                       _csrf: None = Depends(csrf_protected)):
        """POST 版令牌校验：body `{"token": "..."}`，结果与 GET 完全一致。"""
        row = _checked_lookup(services, request, str(payload.get("token") or ""))
        return {"username": row["username"], "valid": True}

    @router.post("/api/auth/activate")
    async def activate(request: Request, payload: dict,
                       _csrf: None = Depends(csrf_protected)):
        token = str(payload.get("token") or "")
        new_password = str(payload.get("password") or "")
        if len(new_password) < MIN_PASSWORD_LENGTH:
            raise InvalidRequest(
                f"password must be at least {MIN_PASSWORD_LENGTH} characters")
        row = _checked_lookup(services, request, token)
        username = row["username"]
        # set_password 会同时清掉 activation_digest（一次性）并 bump epoch
        services.user_repo.set_password(username, create_password_hash(new_password))
        services.audit.record(actor=username, action=ACTION_USER_ACTIVATE,
                              target=username, detail="自设密码完成激活")
        return {"ok": True, "username": username}

    return router
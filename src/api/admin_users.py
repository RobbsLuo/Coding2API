"""用户管理端点（admin only）。

三条必须守住的规则：

1. **绝不回传 password_hash / activation_digest**。仓储返回整行，投影成
   响应字段的动作只在这一处做（测试 test_users_never_leak_hash 守着）。
2. **防锁死**：不能把最后一个活跃 admin 降级/禁用/删除，也不能对自己降级/禁用
   ——否则下一次请求就没人能管理了。三层里的另外两层在 bootstrap。
3. **一次性激活令牌**：明文只在创建/重置的响应里回显一次，库里只存摘要。
   与 API Key 同一心智模型（项目无邮件设施，不能发魔法链接）。
"""

from __future__ import annotations

import hashlib
import secrets
import time

from fastapi import APIRouter, Depends, Request

from ..audit.actions import (
    ACTION_USER_CREATE,
    ACTION_USER_DISABLE,
    ACTION_USER_ENABLE,
    ACTION_USER_PASSWORD_RESET,
    ACTION_USER_POLICY,
    ACTION_USER_ROLE_CHANGE,
)
from ..auth.access import normalize_allowed_models
from ..auth.rbac import (
    ROLE_ADMIN,
    ROLES,
    LastAdminError,
    SelfTargetError,
    require_admin,
)
from ..auth.users import create_password_hash
from ..compat.openai.request import InvalidRequest
from ..key_quota import KeyQuota, parse_key_quota, quota_text
from .admin_auth import ACTIVATION_TTL_SECONDS
from .deps import Services, csrf_protected, principal_from_request, request_ip

# 激活令牌明文长度（URL 安全）。32 字节 ≈ 256 bit，足够。
ACTIVATION_TOKEN_BYTES = 32


def digest_activation_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _public_view(row: dict) -> dict:
    """仓储整行 → 对外字段。**绝不 include password_hash / activation_digest**。"""
    return {
        "username": row["username"],
        "role": row["role"],
        "enabled": bool(row["enabled"]),
        "must_change_password": bool(row["must_change_password"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "created_by": row["created_by"],
        # 是否有待激活令牌（不泄露摘要本身）
        "pending_activation": row["activation_digest"] is not None,
        # 用户级策略（Playground）：模型白名单与配额，界面直接可编
        "allowed_models": row.get("allowed_models") or "",
        "quota": row.get("quota") or "",
    }


def _policy_detail(models: str, quota: KeyQuota) -> str:
    """用户策略的审计明细：一行说清「这个人在 Playground 能碰什么」。

    不含密码/令牌——审计的 detail 只放可读策略，不放凭据。
    """
    parts = [f"模型白名单 {models}" if models else "模型不限"]
    if quota.is_empty():
        parts.append("配额不限")
    else:
        labels = (("daily_requests", "每日请求"), ("daily_tokens", "每日 token"),
                  ("daily_cost_usd", "每日成本 USD"), ("rpm", "RPM"),
                  ("tpm", "TPM"), ("max_concurrency", "并发"))
        parts.append("；".join(f"{label} {getattr(quota, name):g}"
                               for name, label in labels if getattr(quota, name)))
    return "；".join(parts)


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    def _require(principal) -> None:
        require_admin(principal)

    def _guard_last_admin(username: str) -> None:
        """拦住「把 admin 数清零」的操作。必须排在自我检查**之前**。

        顺序有讲究：操作者本身一定是活跃 admin（require_admin + is_active），
        所以「活跃 admin 数为 1」时那唯一一个就是操作者自己。若把 self_target
        排在前面，本守卫永远不会触发，等于只有一层防护；先跑守卫才能让
        「最后一个 admin 不许降级/禁用」这条真正生效（对别人动手时它由
        self_target 覆盖：别人的 admin 数必然 ≥ 2）。
        """
        row = services.user_repo.get(username)
        if (row is not None and row["role"] == ROLE_ADMIN and row["enabled"]
                and services.user_repo.count_active_admins() <= 1):
            raise LastAdminError("cannot remove the last active admin")

    @router.get("/api/users")
    async def list_users(sort: str | None = None, order: str | None = None,
                         limit: int = 50, offset: int = 0,
                         principal=Depends(principal_from_request)):
        _require(principal)
        # limit/offset 分页；total 供前端算页数（仓储与端点的收敛口径一致）
        return {"users": [_public_view(row) for row in
                          services.user_repo.list_all(sort=sort, order=order,
                                                      limit=limit, offset=offset)],
                "total": services.user_repo.count_all()}

    @router.post("/api/users")
    async def create_user(request: Request, payload: dict,
                          _csrf: None = Depends(csrf_protected),
                          principal=Depends(principal_from_request)):
        _require(principal)
        username = str(payload.get("username") or "").strip()
        role = str(payload.get("role") or "viewer")
        if not username:
            raise InvalidRequest("username is required")
        if role not in ROLES:
            raise InvalidRequest(f"unknown role {role!r}")
        if services.user_repo.get(username) is not None:
            raise InvalidRequest(f"user {username!r} already exists")
        token = secrets.token_urlsafe(ACTIVATION_TOKEN_BYTES)
        issued = int(time.time())
        # 建号时先给一个不可用的随机密码占位：用户必须用激活令牌自设密码，
        # 这样系统里不存在「admin 知道但用户不知道」的共享密码。
        services.user_repo.create(
            username, create_password_hash(secrets.token_urlsafe(ACTIVATION_TOKEN_BYTES)),
            role=role, must_change_password=False,
            created_by=principal.username,
            activation_digest=digest_activation_token(token),
            activation_expires_at=issued + ACTIVATION_TTL_SECONDS)
        services.audit.record(actor=principal.username, action=ACTION_USER_CREATE,
                              target=username, detail=f"角色 {role}",
                              ip=request_ip(request, services.settings) or None)
        # 明文令牌只在此回显一次
        return {"username": username, "role": role, "activate_token": token,
                "expires_at": issued + ACTIVATION_TTL_SECONDS}

    @router.patch("/api/users/{username}")
    async def update_user(username: str, request: Request, payload: dict,
                          _csrf: None = Depends(csrf_protected),
                          principal=Depends(principal_from_request)):
        _require(principal)
        role = str(payload.get("role") or "")
        if role not in ROLES:
            raise InvalidRequest(f"unknown role {role!r}")
        if services.user_repo.get(username) is None:
            raise InvalidRequest(f"user {username!r} not found")
        if role != ROLE_ADMIN:
            _guard_last_admin(username)
        if principal.username == username:
            raise SelfTargetError("cannot change your own role")
        services.user_repo.update_role(username, role)
        services.audit.record(actor=principal.username, action=ACTION_USER_ROLE_CHANGE,
                              target=username, detail=f"角色 -> {role}",
                              ip=request_ip(request, services.settings) or None)
        return {"ok": True}

    @router.post("/api/users/{username}/disable")
    async def disable_user(username: str, request: Request,
                           _csrf: None = Depends(csrf_protected),
                           principal=Depends(principal_from_request)):
        _require(principal)
        if services.user_repo.get(username) is None:
            raise InvalidRequest(f"user {username!r} not found")
        _guard_last_admin(username)
        if principal.username == username:
            raise SelfTargetError("cannot disable yourself")
        services.user_repo.set_enabled(username, False)
        services.audit.record(actor=principal.username, action=ACTION_USER_DISABLE,
                              target=username,
                              ip=request_ip(request, services.settings) or None)
        return {"ok": True}

    @router.post("/api/users/{username}/enable")
    async def enable_user(username: str, request: Request,
                          _csrf: None = Depends(csrf_protected),
                          principal=Depends(principal_from_request)):
        _require(principal)
        if services.user_repo.get(username) is None:
            raise InvalidRequest(f"user {username!r} not found")
        services.user_repo.set_enabled(username, True)
        services.audit.record(actor=principal.username, action=ACTION_USER_ENABLE,
                              target=username,
                              ip=request_ip(request, services.settings) or None)
        return {"ok": True}

    @router.post("/api/users/{username}/reset-password")
    async def reset_password(username: str, request: Request,
                             _csrf: None = Depends(csrf_protected),
                             principal=Depends(principal_from_request)):
        """重置：挂一次性令牌 + 强制改密；明文令牌只在本响应回显一次。"""
        _require(principal)
        if services.user_repo.get(username) is None:
            raise InvalidRequest(f"user {username!r} not found")
        token = secrets.token_urlsafe(ACTIVATION_TOKEN_BYTES)
        issued = int(time.time())
        services.user_repo.set_password(
            username,
            create_password_hash(secrets.token_urlsafe(ACTIVATION_TOKEN_BYTES)),
            must_change_password=True, now=issued)
        services.user_repo.set_activation_token(
            username, digest_activation_token(token),
            issued + ACTIVATION_TTL_SECONDS, now=issued)
        services.audit.record(actor=principal.username,
                              action=ACTION_USER_PASSWORD_RESET, target=username,
                              ip=request_ip(request, services.settings) or None)
        return {"username": username, "activate_token": token,
                "expires_at": issued + ACTIVATION_TTL_SECONDS}

    @router.put("/api/users/{username}/policy")
    async def set_user_policy(username: str, request: Request, payload: dict,
                              _csrf: None = Depends(csrf_protected),
                              principal=Depends(principal_from_request)):
        """用户级策略：管理台 Playground 的模型白名单 + 配额（整体替换）。

        为什么策略挂在**用户**上而不是 Key 上：Playground 走会话 Cookie、不经过
        Key 鉴权，只按 Key 判策略等于给每个登录用户留一条无限直连通道。语义与
        Key 那套一致（白名单是 fnmatch glob，配额是同一份六项 JSON），但两者
        独立：Key 的策略管外部调用，这里的策略管这个人在管理台里能干什么。

        配额用量按用户名聚合，该用户经由任何入口（含自己的 Key）的流量都计入。
        """
        _require(principal)
        if services.user_repo.get(username) is None:
            raise InvalidRequest(f"user {username!r} not found")
        try:
            models = normalize_allowed_models(str(payload.get("allowed_models") or ""))
        except ValueError as error:
            raise InvalidRequest(str(error)) from error
        try:
            quota = parse_key_quota(payload.get("quota"))
        except ValueError as error:
            raise InvalidRequest(str(error)) from error
        services.user_repo.set_policy(username, allowed_models=models,
                                      quota=quota_text(quota))
        services.audit.record(actor=principal.username, action=ACTION_USER_POLICY,
                              target=username, detail=_policy_detail(models, quota),
                              ip=request_ip(request, services.settings) or None)
        return {"ok": True, "allowed_models": models, "quota": quota.as_dict()}

    # 故意不提供 DELETE：硬删会让 usage_events 里留下查不到用户名的孤儿统计，
    # 而「停用账号」的实际需求已被 disable 完全覆盖。硬删只走 CLI。
    return router

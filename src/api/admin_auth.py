"""管理台鉴权端点：登录 / 登出 / 会话 / 自助改密 + 上游 OAuth 登录（start/poll/cancel）。

登录响应带上 `role` 与 `must_change_password`：前端据此决定导航可见性与是否
弹出强制改密对话框，避免再发一次 `/api/auth/session` 才知道。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..audit.actions import (
    ACTION_LOGIN_FAILURE,
    ACTION_LOGIN_SUCCESS,
    ACTION_USER_PASSWORD_CHANGE,
)
from ..auth.rbac import ROLE_ADMIN, UnauthorizedError, require_admin
from ..auth.session import create_session_token
from ..auth.users import create_password_hash, verify_password
from ..compat.openai.request import InvalidRequest
from ..config import Settings
from .deps import SESSION_COOKIE, Services, csrf_protected, principal_from_request

MIN_PASSWORD_LENGTH = 8
ACTIVATION_TTL_SECONDS = 24 * 3600


def resolve_public_callback_url(settings: Settings) -> str:
    """PUBLIC_BASE_URL + /authorize（远程部署必须可被浏览器访问）。"""
    return settings.public_base_url.rstrip("/") + "/authorize"


def issue_session(response: JSONResponse, services: Services, username: str, *,
                  role: str, must_change_password: bool) -> None:
    """按当前 epoch 签发会话 Cookie（三处签发共用，避免 TTL/属性漂移）。"""
    epoch = services.users.session_epoch(username) or 0
    token = create_session_token(username, services.settings.app_secret, epoch=epoch)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                        secure=services.settings.public_base_url.startswith("https://"),
                        max_age=12 * 3600, path="/")


def _record_login(services: Services, *, username: str, ok: bool, ip: str) -> None:
    """登录留痕。**绝不记密码**，只记身份与成败。"""
    services.audit.record(
        actor=username, action=ACTION_LOGIN_SUCCESS if ok else ACTION_LOGIN_FAILURE,
        ip=ip or None, ok=ok,
        detail="" if ok else "密码错误或用户不存在")


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    @router.post("/api/auth/login")
    async def login(request: Request, payload: dict):
        username = str(payload.get("username") or "")
        password = str(payload.get("password") or "")
        ip = request.client.host if request.client else ""
        throttle = services.login_throttle
        # 哈希前先卡全局/IP 窗口：PBKDF2（600k 迭代）是 CPU 密集操作，
        # 不限流的话无效尝试就能占满线程池（DoS）。
        # 用户名窗口故意放到验证之后：它可能被他人输错用户名抬高，
        # 提前拦截会误伤合法用户。
        throttle.check_transport_windows(ip=ip)
        # PBKDF2 是 CPU 密集同步操作：线程池 + 信号量限并发，防事件循环卡死。
        verified = await throttle.verify(services.users.verify, username, password)
        if not verified:
            # 超限时抛 429 且不再计数；未超限则记一次失败并回 401
            throttle.check(ip=ip, username=username)
            throttle.record_failure(ip=ip, username=username)
            _record_login(services, username=username, ok=False, ip=ip)
            raise UnauthorizedError("invalid credentials")
        throttle.record_success(username=username)
        _record_login(services, username=username, ok=True, ip=ip)
        role = services.users.role_of(username) or ""
        must_change = services.users.must_change_password(username)
        response = JSONResponse({"username": username,
                                 "is_admin": role == ROLE_ADMIN,
                                 "role": role,
                                 "must_change_password": must_change})
        issue_session(response, services, username, role=role,
                      must_change_password=must_change)
        return response

    @router.post("/api/auth/logout")
    async def logout(_csrf: None = Depends(csrf_protected)):
        # 不校验 CSRF 时，任何跨站页面都能强制登出（拒绝服务式骚扰）。
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @router.get("/api/auth/session")
    async def session_info(principal=Depends(principal_from_request)):
        return {"username": principal.username, "is_admin": principal.is_admin,
                "role": principal.role,
                "must_change_password": services.users.must_change_password(
                    principal.username)}

    @router.post("/api/auth/password")
    async def change_password(request: Request, payload: dict,
                              _csrf: None = Depends(csrf_protected),
                              principal=Depends(principal_from_request)):
        """自助改密：当前密码 + 新密码。任意角色可用（首登强制改密也走这里）。

        当前密码必须正确：会话被盗时不能直接改密把主人锁在外面。
        """
        current = str(payload.get("current_password") or "")
        new_password = str(payload.get("new_password") or "")
        if len(new_password) < MIN_PASSWORD_LENGTH:
            raise InvalidRequest(
                f"new password must be at least {MIN_PASSWORD_LENGTH} characters")
        row = services.user_repo.get(principal.username)
        if row is None or not verify_password(current, row["password_hash"]):
            raise InvalidRequest("current password is incorrect")
        services.user_repo.set_password(
            principal.username, create_password_hash(new_password))
        services.audit.record(actor=principal.username,
                              action=ACTION_USER_PASSWORD_CHANGE,
                              target=principal.username,
                              ip=request.client.host if request.client else None)
        # 改密会 bump epoch：换发一张新 Cookie，否则本次请求返回后自己也被登出。
        response = JSONResponse({"ok": True})
        role = services.users.role_of(principal.username) or ""
        issue_session(response, services, principal.username, role=role,
                      must_change_password=False)
        return response

    # ------------------------------------------------- 上游登录（poll 轨道）

    @router.post("/api/auth/upstream/start")
    async def upstream_auth_start(request: Request, payload: dict,
                                  _csrf: None = Depends(csrf_protected),
                                  principal=Depends(principal_from_request)):
        require_admin(principal)
        provider_id = str(payload.get("provider") or "")
        if provider_id in services.upstream_auth:
            session = await services.upstream_auth[provider_id].start(principal.username)
        else:
            provider = services.registry.get(provider_id)
            builder = getattr(provider, "start_auth", None)
            if not callable(builder):
                raise InvalidRequest(f"provider {provider_id!r} does not support login")
            session = builder(resolve_public_callback_url(services.settings))
            request.app.state.pending_callback_state = session.state
            request.app.state.pending_callback_user = principal.username
            # 回调轨道没有本地轮询：登录结果由 /authorize 落库后由前端查凭证列表
        return {"flow": session.flow, "state": session.state, "auth_url": session.auth_url,
                "interval": session.interval, "callback_url": session.callback_url}

    @router.post("/api/auth/upstream/poll")
    async def upstream_auth_poll(payload: dict,
                                 _csrf: None = Depends(csrf_protected),
                                 principal=Depends(principal_from_request)):
        require_admin(principal)
        provider_id = str(payload.get("provider") or "")
        state = str(payload.get("state") or "")
        oauth = services.upstream_auth.get(provider_id)
        if oauth is None:
            raise InvalidRequest(f"provider {provider_id!r} does not support polling login")
        result = await oauth.poll(state, principal.username)
        if result is None:
            return {"status": "pending"}
        # 登录成功：直接落库，绝不在响应里回传 token
        credential_id = services.credentials.add(
            provider=provider_id, credential_data=result.credential_data,
            nickname=result.nickname, added_by=principal.username)
        services.schedule_probe(credential_id)
        return {"status": "success", "credential_id": credential_id}

    @router.post("/api/auth/upstream/cancel")
    async def upstream_auth_cancel(payload: dict,
                                   _csrf: None = Depends(csrf_protected),
                                   principal=Depends(principal_from_request)):
        require_admin(principal)
        oauth = services.upstream_auth.get(str(payload.get("provider") or ""))
        if oauth is None:
            raise InvalidRequest("provider does not support polling login")
        cancelled = oauth.store.cancel(str(payload.get("state") or ""), principal.username)
        return {"cancelled": cancelled}

    return router

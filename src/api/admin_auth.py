"""管理台鉴权端点：登录 / 登出 / 会话 + 上游 OAuth 登录（start/poll/cancel）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..auth.rbac import UnauthorizedError, require_admin
from ..auth.session import create_session_token
from ..compat.openai.request import InvalidRequest
from ..config import Settings
from .deps import SESSION_COOKIE, Services, csrf_protected, principal_from_request


def resolve_public_callback_url(settings: Settings) -> str:
    """PUBLIC_BASE_URL + /authorize（远程部署必须可被浏览器访问）。"""
    return settings.public_base_url.rstrip("/") + "/authorize"


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    @router.post("/api/auth/login")
    async def login(request: Request, payload: dict):
        username = str(payload.get("username") or "")
        password = str(payload.get("password") or "")
        ip = request.client.host if request.client else ""
        throttle = services.login_throttle
        # PBKDF2 是 CPU 密集同步操作：线程池 + 信号量限并发，防事件循环卡死。
        # 先验证再限流：正确密码永不被窗口卡死（一次成功即解锁），
        # 失败才走窗口计数——爆破频率被压到可用性以下。
        verified = await throttle.verify(services.users.verify, username, password)
        if not verified:
            # 超限时抛 429 且不再计数；未超限则记一次失败并回 401
            throttle.check(ip=ip, username=username)
            throttle.record_failure(ip=ip, username=username)
            raise UnauthorizedError("invalid credentials")
        throttle.record_success(username=username)
        token = create_session_token(username, services.settings.app_secret)
        response = JSONResponse(
            {"username": username,
             "is_admin": services.settings.is_admin(username)})
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                            secure=services.settings.public_base_url.startswith("https://"),
                            max_age=12 * 3600, path="/")
        return response

    @router.post("/api/auth/logout")
    async def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @router.get("/api/auth/session")
    async def session_info(principal=Depends(principal_from_request)):
        return {"username": principal.username, "is_admin": principal.is_admin}

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

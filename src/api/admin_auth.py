"""管理台鉴权端点：登录 / 登出 / 会话 / 自助改密 + 上游 OAuth 登录（start/poll/cancel）。

登录响应带上 `role` 与 `must_change_password`：前端据此决定导航可见性与是否
弹出强制改密对话框，避免再发一次 `/api/auth/session` 才知道。
"""

from __future__ import annotations

import inspect
import ipaddress
import time
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..audit.actions import (
    ACTION_LOGIN_FAILURE,
    ACTION_LOGIN_SUCCESS,
    ACTION_USER_PASSWORD_CHANGE,
)
from ..auth.rbac import ROLE_ADMIN, ROLE_OPERATOR, UnauthorizedError, require_admin
from ..auth.session import create_session_token
from ..auth.users import create_password_hash, verify_password
from ..compat.openai.request import InvalidRequest
from ..config import Settings
from ..provider.base import AuthResult
from ..provider.trae.callback import login_trace_id
from ..webapp.security import session_cookie_secure
from .authorize import PENDING_CALLBACK_TTL_SECONDS
from .deps import (
    SESSION_COOKIE,
    Services,
    csrf_protected,
    origin_protected,
    principal_from_request,
    request_ip,
)

MIN_PASSWORD_LENGTH = 8
ACTIVATION_TTL_SECONDS = 24 * 3600


# 浏览器打不开的基址主机：回环与通配监听。PUBLIC_BASE_URL 默认值就是
# 127.0.0.1:8000，远程/容器部署忘了改时，回调会跳到**用户自己这台机器**，
# 浏览器既回不到服务也不报错（issue #21）。
_LOCALHOST_NAMES = frozenset({"localhost"})


def _host_of(authority: str) -> str:
    """从 `host[:port]` / `scheme://host[:port]/path` 取出小写主机名。"""
    text = authority.strip().split("://", 1)[-1].split("/", 1)[0]
    if text.startswith("["):
        return text.partition("]")[0].lstrip("[").lower()
    return text.split(":", 1)[0].lower()


def _browser_cannot_reach(host: str) -> bool:
    """回环 / 通配监听：浏览器打开它到不了本服务（或到不了发请求的那个人）。"""
    if host in _LOCALHOST_NAMES:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:          # 普通域名 / 无效字面量：按可达处理
        return False
    return address.is_loopback or address.is_unspecified


def _request_origin(request: Request) -> str:
    """当前请求的浏览器 origin（采信反代/隧道的 X-Forwarded-Proto）。"""
    proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    if proto not in ("http", "https"):
        proto = request.url.scheme
    authority = request.headers.get("host", "") or request.url.netloc
    return f"{proto}://{authority}"


def resolve_public_callback_url(settings: Settings,
                                request: Request | None = None) -> str:
    """回调基址 + /authorize。

    `PUBLIC_BASE_URL` 是权威值，但它有两个已知的「静默失效」形态：
    远程部署忘了改（默认 `http://127.0.0.1:8000` 指向用户自己的机器）、
    docker-compose 只透传了默认值。这两种情况下回调地址由**浏览器**打开，
    必然是坏的，而用户只会看到「授权完成但没回调」。

    因此基址落在回环/通配主机、而当前请求来自别的主机时，改用请求自身的
    origin：能打到本服务的那个地址，浏览器同样能打到。请求已通过
    `host_allowed` 白名单，故这里采信 Host 头不会放大攻击面。
    """
    base = settings.public_base_url.rstrip("/")
    if request is not None and _browser_cannot_reach(_host_of(base)):
        request_host = _host_of(request.headers.get("host", "") or request.url.netloc)
        if request_host and not _browser_cannot_reach(request_host):
            base = _request_origin(request).rstrip("/")
    return base + "/authorize"


def issue_session(response: JSONResponse, services: Services, username: str, *,
                  request: Request, role: str, must_change_password: bool) -> None:
    """按当前 epoch 签发会话 Cookie（三处签发共用，避免 TTL/属性漂移）。

    `Secure` 按请求的实际协议判定（见 `session_cookie_secure`）：同一实例可能
    同时有 LAN(http) 与隧道(https) 两条入口，写死任一侧都会打断另一侧。
    """
    epoch = services.users.session_epoch(username) or 0
    token = create_session_token(username, services.settings.app_secret, epoch=epoch)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                        secure=session_cookie_secure(request, services.settings),
                        max_age=12 * 3600, path="/")


def _record_login(services: Services, *, username: str, ok: bool, ip: str) -> None:
    """登录留痕。**绝不记密码**，只记身份与成败。"""
    services.audit.record(
        actor=username, action=ACTION_LOGIN_SUCCESS if ok else ACTION_LOGIN_FAILURE,
        ip=ip or None, ok=ok,
        detail="" if ok else "密码错误或用户不存在")


async def _complete_callback_paste(services: Services, pending: dict[str, tuple[float, str]],
                                   username: str, provider_id: str,
                                   raw_url: str, state: str) -> AuthResult:
    """callback 轨道（TRAE）的粘贴兑换：按发起人的 pending 登录完成回调。

    与 `/authorize` 的差别只在**入口**：那边是浏览器 302（无鉴权，靠
    `loginTraceID` 绑定），这边是已鉴权的管理台写请求，因此用会话里的
    用户名直接定位 pending。仍复用同一条 TTL，且 state 必须与 pending 一致
    （`/authorize` 是从 pending 里取 state，这边是用户可能贴错链接）。

    粘回来的地址若带 `loginTraceID`，必须与这次登录的 trace 相符——防止
    用户把另一次登录（另一个浏览器标签）的回调贴进来，把两边的
    machine/device id 搞混。
    """
    provider = services.registry.get(provider_id)
    completer = getattr(provider, "complete_callback", None)
    if provider is None or not callable(completer):
        raise InvalidRequest(
            f"provider {provider_id!r} does not support callback completion")
    entry = pending.get(username)
    if entry is None:
        raise InvalidRequest(
            "没有进行中的登录。请回到管理台重新点击「登录」，再把回调地址粘回来。")
    started_at, pending_state = entry
    if time.monotonic() - started_at > PENDING_CALLBACK_TTL_SECONDS:
        pending.pop(username, None)
        raise InvalidRequest(
            f"这次登录已超过 {PENDING_CALLBACK_TTL_SECONDS // 60} 分钟有效期，"
            "请回到管理台重新发起登录。")
    if state and state != pending_state:
        raise InvalidRequest(
            "粘贴的回调地址不属于当前这次登录，请重新发起登录后再粘贴。")
    pasted_trace = (parse_qs(urlparse(raw_url).query).get("loginTraceID") or [""])[0]
    machine_id, _, device_id = pending_state.partition(":")
    if pasted_trace and pasted_trace != login_trace_id(machine_id, device_id):
        raise InvalidRequest(
            "粘贴的回调地址不属于当前这次登录，请重新发起登录后再粘贴。")
    pending.pop(username, None)
    data = await completer(raw_url, pending_state)
    return AuthResult(credential_data=data, nickname=str(data.get("nickname") or ""))


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    @router.post("/api/auth/login")
    async def login(request: Request, payload: dict,
                    _csrf: None = Depends(origin_protected)):
        # CSRF 校验对登录同样生效（login CSRF：跨站请求可把受害者登录到
        # 攻击者账号）。登录时还没有会话 Cookie，`csrf_protected` 会跳过，
        # 因此这里用 `origin_protected`：只查 Origin/Referer 同源，无头
        # （curl / SDK）才放行。
        username = str(payload.get("username") or "")
        password = str(payload.get("password") or "")
        ip = request_ip(request, services.settings)
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
                                 "is_operator": role in (ROLE_ADMIN, ROLE_OPERATOR),
                                 "role": role,
                                 "must_change_password": must_change})
        issue_session(response, services, username, request=request, role=role,
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
                "is_operator": principal.is_operator,
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
                              ip=request_ip(request, services.settings) or None)
        # 改密会 bump epoch：换发一张新 Cookie，否则本次请求返回后自己也被登出。
        response = JSONResponse({"ok": True})
        role = services.users.role_of(principal.username) or ""
        issue_session(response, services, principal.username, request=request,
                      role=role, must_change_password=False)
        return response

    # ------------------------------------------------- 上游登录（poll 轨道）

    @router.post("/api/auth/upstream/start")
    async def upstream_auth_start(request: Request, payload: dict,
                                  _csrf: None = Depends(csrf_protected),
                                  principal=Depends(principal_from_request)):
        require_admin(principal)
        provider_id = str(payload.get("provider") or "")
        if provider_id in services.upstream_auth:
            oauth = services.upstream_auth[provider_id]
            # `force_login`（换账号）只有支持它的渠道认：浏览器里还留着上一个
            # 账号的上游会话时，不先登出就会被云端直接放行成「同一个账号再登
            # 一遍」，用户点了「新增账号」却始终拿不到另一条凭证。
            session = (await oauth.start(principal.username,
                                         force_login=bool(payload.get("force_login")))
                       if "force_login" in inspect.signature(oauth.start).parameters
                       else await oauth.start(principal.username))
        else:
            provider = services.registry.get(provider_id)
            builder = getattr(provider, "start_auth", None)
            if not callable(builder):
                raise InvalidRequest(f"provider {provider_id!r} does not support login")
            session = builder(resolve_public_callback_url(services.settings, request))
            # 按发起人分键登记待完成回调：单槽全局态会让两个管理员的
            # 并发登录互相覆盖，把 A 的凭证记到 B 名下；按用户名分键后，
            # 同网段的人也无法用自己的 refreshToken 顶掉别人进行中的登录。
            pending = request.app.state.pending_callbacks
            pending[principal.username] = (time.monotonic(), session.state)
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
    async def upstream_auth_cancel(request: Request, payload: dict,
                                   _csrf: None = Depends(csrf_protected),
                                   principal=Depends(principal_from_request)):
        require_admin(principal)
        provider_id = str(payload.get("provider") or "")
        state = str(payload.get("state") or "")
        oauth = services.upstream_auth.get(provider_id)
        if oauth is None:
            # callback 轨道（TRAE）没有上游轮询会话可取消，但要清掉待完成
            # 登录，否则取消后它还会挂到 TTL 到期，期间用户重新点「登录」
            # 会被这条僵尸 pending 抢走（按用户名分键，后写覆盖前写，
            # 本身不会错记，但残留条目会让「已取消」的假象持续）。
            pending = request.app.state.pending_callbacks
            entry = pending.get(principal.username)
            if (provider_id in services.registry and entry is not None
                    and (not state or entry[1] == state)):
                pending.pop(principal.username, None)
                return {"cancelled": True}
            raise InvalidRequest("provider does not support polling login")
        cancelled = oauth.store.cancel(state, principal.username)
        return {"cancelled": cancelled}

    @router.post("/api/auth/upstream/complete")
    async def upstream_auth_complete(request: Request, payload: dict,
                                     _csrf: None = Depends(csrf_protected),
                                     principal=Depends(principal_from_request)):
        """粘贴回调链接完成登录（回调落不到本服务、或浏览器打不开回调地址的渠道）。

        两条路：

        - **paste 轨道**（CodeArts 等）：服务端从 start 起就登记了 PKCE/ticket
          会话，`oauth.complete_callback(url, state, username)` 自己按 state 找。
        - **callback 轨道**（TRAE）：回调本应 302 回本服务 `/authorize`，但
          远程 / NAS 部署里 TRAE 侧跳过去常直接「网络错误」，refreshToken 只
          存在浏览器地址栏里，服务端永远收不到（issue #21 后半段）。用户把
          那条地址整条粘回来，这里用与 `/authorize` 相同的 pending 绑定换 token。

        与 `poll` 一样直接落库、不回传 token。
        """
        require_admin(principal)
        provider_id = str(payload.get("provider") or "")
        raw_url = str(payload.get("url") or "")
        state = str(payload.get("state") or "")
        oauth = services.upstream_auth.get(provider_id)
        completer = getattr(oauth, "complete_callback", None)
        if oauth is not None and callable(completer):
            result = await completer(raw_url, state, principal.username)
        else:
            result = await _complete_callback_paste(
                services, request.app.state.pending_callbacks,
                principal.username, provider_id, raw_url, state)
        credential_id = services.credentials.add(
            provider=provider_id, credential_data=result.credential_data,
            nickname=result.nickname, added_by=principal.username)
        services.schedule_probe(credential_id)
        return {"status": "success", "credential_id": credential_id}

    return router

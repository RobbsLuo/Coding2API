"""FastAPI 组装：外部 OpenAI 端点 + 管理端点 + TRAE 回调落点。"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .auth.rbac import ForbiddenError, Principal, UnauthorizedError, require_admin
from .compat.openai.request import InvalidRequest, parse_chat_request
from .compat.openai.response import error_payload
from .config import Settings, load_settings
from .db.conn import Database
from .db.crypto import CredentialCipher
from .db.migrate import apply_schema
from .db.repo import ApiKeyRepository, CredentialRepository
from .engine.executor import Executor, ExecutorDeps, NoHealthyCredential, NoProviderForModel
from .engine.model_resolver import UnknownModelError
from .engine.scheduler import Scheduler
from .provider.codebuddy.client import CodeBuddyProvider
from .provider.codebuddy.events import (
    UpstreamProtocolViolation as CodeBuddyProtocolViolation,
)
from .provider.codebuddy.oauth import CodeBuddyOAuth
from .provider.trae.client import TraeProvider
from .provider.trae.events import UpstreamProtocolViolation
from .stats.collector import StatsCollector, StatsQuery

SESSION_COOKIE = "coding2api_session"


def build_app(settings: Settings | None = None, *, providers: dict | None = None,
              users: object | None = None) -> FastAPI:
    config = settings or load_settings()
    db = Database(config.db_path)
    apply_schema(db.connect())
    cipher = CredentialCipher(config.app_secret)
    credentials = CredentialRepository(db, cipher)
    api_keys = ApiKeyRepository(db)
    store = users if users is not None else _load_users(settings=config)
    registry = providers if providers is not None else {
        "trae": TraeProvider(),
        "codebuddy": CodeBuddyProvider(),
    }
    executor = Executor(ExecutorDeps(providers=registry, credentials=credentials,
                                     scheduler=Scheduler(),
                                     default_model=config.default_model))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        db.close()

    app = FastAPI(title="coding2api", version="0.1.0", lifespan=lifespan)
    app.state.settings = config
    app.state.users = store
    app.state.credentials = credentials
    app.state.api_keys = api_keys
    app.state.executor = executor
    app.state.stats_collector = StatsCollector(db)
    app.state.stats_query = StatsQuery(db)
    app.state.upstream_auth = _upstream_auth(registry, config)

    # ------------------------------------------------------------ 鉴权依赖

    async def principal_from_request(request: Request) -> Principal:
        from .auth.session import verify_session_token

        token = request.cookies.get(SESSION_COOKIE, "")
        username = verify_session_token(token, config.app_secret)
        if not username:
            raise UnauthorizedError("session missing or expired")
        return Principal(username=username, is_admin=config.is_admin(username))

    async def api_key_user(request: Request) -> str:
        header = request.headers.get("authorization", "")
        prefix = "Bearer "
        if not header.lower().startswith(prefix.lower()):
            raise UnauthorizedError("missing api key")
        username = api_keys.verify(header[len(prefix):].strip())
        if not username:
            raise UnauthorizedError("invalid api key")
        return username

    @app.exception_handler(UnauthorizedError)
    async def _unauthorized(_request: Request, _error: UnauthorizedError):
        return JSONResponse(status_code=401,
                            content=error_payload("invalid authentication credentials",
                                                 "invalid_api_key", 401))

    @app.exception_handler(InvalidRequest)
    async def _invalid(_request: Request, error: InvalidRequest):
        return JSONResponse(status_code=400,
                            content=error_payload(str(error), "invalid_request", 400))

    @app.exception_handler(UnknownModelError)
    async def _unknown_model(_request: Request, error: UnknownModelError):
        return JSONResponse(status_code=400,
                            content=error_payload(str(error), "invalid_request", 400))

    @app.exception_handler(UpstreamProtocolViolation)
    async def _bad_credential(_request: Request, error: UpstreamProtocolViolation):
        return JSONResponse(status_code=400,
                            content=error_payload(str(error), "invalid_credential", 400))

    @app.exception_handler(CodeBuddyProtocolViolation)
    async def _bad_codebuddy_credential(_request: Request,
                                        error: CodeBuddyProtocolViolation):
        return JSONResponse(status_code=400,
                            content=error_payload(str(error), "invalid_credential", 400))

    @app.exception_handler(NoHealthyCredential)
    async def _no_health(_request: Request, error: NoHealthyCredential):
        return JSONResponse(status_code=503,
                            content=error_payload(str(error), "no_healthy_credential", 503))

    @app.exception_handler(NoProviderForModel)
    async def _no_provider(_request: Request, error: NoProviderForModel):
        return JSONResponse(status_code=400,
                            content=error_payload(str(error), "invalid_request", 400))

    @app.exception_handler(ForbiddenError)
    async def _forbidden(_request: Request, _error: ForbiddenError):
        return JSONResponse(status_code=403,
                            content=error_payload("admin only", "forbidden", 403))

    # ----------------------------------------------------------- 管理台登录

    @app.post("/api/auth/login")
    async def login(payload: dict):
        from .auth.session import create_session_token

        username = str(payload.get("username") or "")
        password = str(payload.get("password") or "")
        if not store.verify(username, password):
            raise UnauthorizedError("invalid credentials")
        token = create_session_token(username, config.app_secret)
        response = JSONResponse({"username": username, "is_admin": config.is_admin(username)})
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                            secure=config.public_base_url.startswith("https://"),
                            max_age=12 * 3600, path="/")
        return response

    @app.post("/api/auth/logout")
    async def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/api/auth/session")
    async def session_info(principal: Principal = Depends(principal_from_request)):
        return {"username": principal.username, "is_admin": principal.is_admin}

    # ------------------------------------------------------------- 对外端点

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request, _user: str = Depends(api_key_user)):
        body = await request.json()
        chat_request = parse_chat_request(body)
        if chat_request.stream:
            return StreamingResponse(executor.stream(chat_request),
                                     media_type="text/event-stream")
        return JSONResponse(await executor.complete(chat_request))

    @app.get("/v1/models")
    async def list_models(_user: str = Depends(api_key_user)):
        models: dict[str, set[str]] = {}
        for provider_id, provider in registry.items():
            for model in provider.list_models({}):
                models.setdefault(model.id, set()).add(provider_id)
        return {"object": "list", "data": [
            {"id": model, "object": "model", "owned_by": "coding2api",
             "providers": sorted(providers)}
            for model, providers in sorted(models.items())
        ]}

    # --------------------------------------------------------- 管理端点（读）

    @app.get("/api/credentials")
    async def list_credentials(principal: Principal = Depends(principal_from_request)):
        return {"credentials": credentials.list_all(), "viewer": principal.username,
                "is_admin": principal.is_admin}

    @app.get("/api/api-keys")
    async def list_keys(principal: Principal = Depends(principal_from_request)):
        return {"api_keys": api_keys.list_for(principal.username)}

    # --------------------------------------------------------- 管理端点（写）

    @app.post("/api/credentials")
    async def import_credential(payload: dict,
                                principal: Principal = Depends(principal_from_request)):
        require_admin(principal)
        provider_id = str(payload.get("provider") or "")
        if provider_id not in registry:
            raise InvalidRequest(f"unknown provider {provider_id!r}")
        credential_data = registry[provider_id].import_credential(payload.get("credential") or {})
        credential_id = credentials.add(provider=provider_id, credential_data=credential_data,
                                        nickname=str(payload.get("nickname") or ""),
                                        added_by=principal.username)
        return {"id": credential_id}

    @app.post("/api/credentials/{credential_id}/toggle")
    async def toggle_credential(credential_id: str, payload: dict,
                                principal: Principal = Depends(principal_from_request)):
        require_admin(principal)
        if not credentials.set_enabled(credential_id, bool(payload.get("enabled", True))):
            raise InvalidRequest("credential not found")
        return {"ok": True}

    @app.post("/api/credentials/pin")
    async def pin_credential(payload: dict,
                             principal: Principal = Depends(principal_from_request)):
        require_admin(principal)
        credentials.set_pinned(payload.get("credential_id"))
        return {"ok": True}

    @app.delete("/api/credentials/{credential_id}")
    async def delete_credential(credential_id: str,
                                principal: Principal = Depends(principal_from_request)):
        require_admin(principal)
        if not credentials.delete(credential_id):
            raise InvalidRequest("credential not found")
        return {"ok": True}

    @app.post("/api/api-keys")
    async def create_key(payload: dict,
                         principal: Principal = Depends(principal_from_request)):
        created = api_keys.create(principal.username, str(payload.get("name") or ""))
        return created          # 明文只在此返回一次

    @app.delete("/api/api-keys/{key_id}")
    async def delete_key(key_id: str, principal: Principal = Depends(principal_from_request)):
        if not api_keys.delete(key_id, principal.username):
            raise InvalidRequest("api key not found")
        return {"ok": True}

    # ------------------------------------------------- 上游登录（poll 轨道）

    @app.post("/api/auth/upstream/start")
    async def upstream_auth_start(payload: dict,
                                  principal: Principal = Depends(principal_from_request)):
        require_admin(principal)
        provider_id = str(payload.get("provider") or "")
        oauth = app.state.upstream_auth.get(provider_id)
        if oauth is None:
            raise InvalidRequest(f"provider {provider_id!r} does not support polling login")
        session = await oauth.start(principal.username)
        return {"flow": session.flow, "state": session.state, "auth_url": session.auth_url,
                "interval": session.interval}

    @app.post("/api/auth/upstream/poll")
    async def upstream_auth_poll(payload: dict,
                                 principal: Principal = Depends(principal_from_request)):
        require_admin(principal)
        provider_id = str(payload.get("provider") or "")
        state = str(payload.get("state") or "")
        oauth = app.state.upstream_auth.get(provider_id)
        if oauth is None:
            raise InvalidRequest(f"provider {provider_id!r} does not support polling login")
        result = await oauth.poll(state, principal.username)
        if result is None:
            return {"status": "pending"}
        # 登录成功：直接落库，绝不在响应里回传 token
        credential_id = credentials.add(
            provider=provider_id, credential_data=result.credential_data,
            nickname=result.nickname, added_by=principal.username)
        return {"status": "success", "credential_id": credential_id}

    @app.post("/api/auth/upstream/cancel")
    async def upstream_auth_cancel(payload: dict,
                                   principal: Principal = Depends(principal_from_request)):
        require_admin(principal)
        oauth = app.state.upstream_auth.get(str(payload.get("provider") or ""))
        if oauth is None:
            raise InvalidRequest("provider does not support polling login")
        cancelled = oauth.store.cancel(str(payload.get("state") or ""), principal.username)
        return {"cancelled": cancelled}

    # ------------------------------------------------------- 凭证运维（M1.5）

    @app.post("/api/credentials/{credential_id}/probe")
    async def probe_credential(credential_id: str,
                               principal: Principal = Depends(principal_from_request)):
        require_admin(principal)
        provider_id = credentials.provider_of(credential_id)
        provider = registry.get(provider_id or "")
        data = credentials.credential_data(credential_id)
        if provider is None or data is None:
            raise InvalidRequest("credential not found")
        try:
            quota = await provider.probe_quota(data)
        except Exception as error:  # noqa: BLE001 - 探测失败 → unknown，不当作 0
            credentials.mark_probe_failed(credential_id)
            return {"probed": False, "reason": type(error).__name__}
        credentials.save_quota(credential_id, quota)
        return {"probed": True, "remaining": quota.remaining, "total": quota.total,
                "cycle_end": quota.cycle_end}

    @app.post("/api/credentials/{credential_id}/checkin")
    async def checkin_credential(credential_id: str,
                                 principal: Principal = Depends(principal_from_request)):
        require_admin(principal)
        provider_id = credentials.provider_of(credential_id)
        provider = registry.get(provider_id or "")
        data = credentials.credential_data(credential_id)
        if provider is None or data is None or not hasattr(provider, "checkin"):
            raise InvalidRequest("credential does not support checkin")
        result = await provider.checkin(data)
        return {"ok": result.ok, "credit": result.credit, "code": result.code,
                "message": result.message}

    @app.get("/api/credentials/{credential_id}/accounts")
    async def list_credential_accounts(credential_id: str,
                                       principal: Principal = Depends(principal_from_request)):
        require_admin(principal)
        provider_id = credentials.provider_of(credential_id)
        provider = registry.get(provider_id or "")
        data = credentials.credential_data(credential_id)
        if provider is None or data is None or not hasattr(provider, "list_accounts"):
            raise InvalidRequest("credential does not support account switching")
        accounts = await provider.list_accounts(data)
        return {"accounts": [{"account_id": a.account_id, "nickname": a.nickname,
                              "type": a.account_type} for a in accounts]}

    @app.post("/api/credentials/{credential_id}/accounts/select")
    async def select_credential_account(credential_id: str, payload: dict,
                                        principal: Principal = Depends(principal_from_request)):
        require_admin(principal)
        provider_id = credentials.provider_of(credential_id)
        provider = registry.get(provider_id or "")
        data = credentials.credential_data(credential_id)
        if provider is None or data is None or not hasattr(provider, "switch_account"):
            raise InvalidRequest("credential does not support account switching")
        switched = await provider.switch_account(data, str(payload.get("account_id") or ""))
        credentials.save_credential_data(credential_id, switched)
        return {"switched": True}

    # ------------------------------------------------------------- 统计

    @app.get("/api/stats/overview")
    async def stats_overview(principal: Principal = Depends(principal_from_request),
                             username: str | None = None, since: int | None = None):
        target = username if principal.is_admin else principal.username
        return app.state.stats_query.overview(username=target, since=since)

    @app.get("/api/stats/by-provider")
    async def stats_by_provider(principal: Principal = Depends(principal_from_request),
                                username: str | None = None, since: int | None = None):
        target = username if principal.is_admin else principal.username
        return {"providers": app.state.stats_query.by_provider(username=target, since=since)}

    # -------------------------------------------------- TRAE 回调（无鉴权）

    @app.get("/authorize")
    async def authorize(request: Request):
        """TRAE 浏览器 302 落点：只捕获 query，不落盘。"""
        raw = str(request.url)
        app.state.last_callback_url = raw
        return {"ok": True, "captured": True, "at": int(time.time())}

    # 静态资源必须最后注册：catch-all 会匹配所有未命中的路径
    # ------------------------------------------------------- 前端静态资源

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        """生产模式服务 web/dist；开发模式由 Vite 代理，无需此路由。"""
        from fastapi.responses import FileResponse, PlainTextResponse

        dist = Path("web/dist")
        if not dist.is_dir():
            return PlainTextResponse("frontend build not found; run pnpm build in web/",
                                     status_code=404)
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        index = dist / "index.html"
        if not index.is_file():
            return PlainTextResponse("index.html missing", status_code=404)
        return FileResponse(index)

    return app


def resolve_public_callback_url(settings: Settings) -> str:
    """PUBLIC_BASE_URL + /authorize（远程部署必须可被浏览器访问）。"""
    return settings.public_base_url.rstrip("/") + "/authorize"


def _upstream_auth(registry: dict, settings: Settings) -> dict:
    """返回支持 poll 轨道的 provider 的 OAuth 实现（当前仅 CodeBuddy）。"""
    flows: dict = {}
    codebuddy = registry.get("codebuddy")
    endpoint = getattr(getattr(codebuddy, "client", None), "endpoint", None)
    if endpoint is not None:
        flows["codebuddy"] = CodeBuddyOAuth(endpoint)
    return flows


def _load_users(*, settings: Settings):
    """用户文件是唯一用户源（PROPOSAL §5）。启动时必须存在且至少一个有效用户。"""
    import os

    from .auth.users import UsersFileStore

    path = os.environ.get("USERS_FILE", os.path.join("secrets", "users.txt"))
    store = UsersFileStore(path)
    store.validate()
    return store

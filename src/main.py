"""FastAPI 组装：外部 OpenAI 端点 + 管理端点 + TRAE 回调落点。"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

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
from .provider.trae.client import TraeProvider
from .provider.trae.events import UpstreamProtocolViolation

SESSION_COOKIE = "coding2api_session"


def build_app(settings: Settings | None = None, *, providers: dict | None = None) -> FastAPI:
    config = settings or load_settings()
    db = Database(config.db_path)
    apply_schema(db.connect())
    cipher = CredentialCipher(config.app_secret)
    credentials = CredentialRepository(db, cipher)
    api_keys = ApiKeyRepository(db)
    registry = providers if providers is not None else {"trae": TraeProvider()}
    executor = Executor(ExecutorDeps(providers=registry, credentials=credentials,
                                     scheduler=Scheduler(),
                                     default_model=config.default_model))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        db.close()

    app = FastAPI(title="coding2api", version="0.1.0", lifespan=lifespan)
    app.state.settings = config
    app.state.credentials = credentials
    app.state.api_keys = api_keys
    app.state.executor = executor

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
        return {"credentials": credentials.list_all(), "viewer": principal.username}

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

    # -------------------------------------------------- TRAE 回调（无鉴权）

    @app.get("/authorize")
    async def authorize(request: Request):
        """TRAE 浏览器 302 落点：只捕获 query，不落盘。"""
        raw = str(request.url)
        app.state.last_callback_url = raw
        return {"ok": True, "captured": True, "at": int(time.time())}

    return app


def resolve_public_callback_url(settings: Settings) -> str:
    """PUBLIC_BASE_URL + /authorize（远程部署必须可被浏览器访问）。"""
    return settings.public_base_url.rstrip("/") + "/authorize"

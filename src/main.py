"""FastAPI 组装：外部 OpenAI 端点 + 管理端点 + TRAE 回调落点。"""

from __future__ import annotations

import asyncio
import logging
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
from .tasks.runner import build_runner

SESSION_COOKIE = "coding2api_session"

logger = logging.getLogger(__name__)


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
                                     default_model=config.default_model,
                                     stats=StatsCollector(db)))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runner = build_runner(credentials, registry, app.state.stats_collector, config)
        app.state.task_runner = runner
        await runner.start()
        try:
            yield
        finally:
            await runner.stop()
            for task in app.state.pending_probes:
                task.cancel()
            app.state.pending_probes.clear()
            for provider in registry.values():
                closer = getattr(provider, "aclose", None)
                if callable(closer):
                    await closer()
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
    app.state.pending_probes = []
    app.state.pending_callback_state = None
    app.state.pending_callback_user = None

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
    async def chat_completions(request: Request, user: str = Depends(api_key_user)):
        body = await request.json()
        chat_request = parse_chat_request(body)
        if chat_request.stream:
            return StreamingResponse(executor.stream(chat_request, username=user),
                                     media_type="text/event-stream")
        return JSONResponse(await executor.complete(chat_request, username=user))

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
        schedule_probe(credential_id)
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
        if provider_id in app.state.upstream_auth:
            session = await app.state.upstream_auth[provider_id].start(principal.username)
        else:
            provider = registry.get(provider_id)
            builder = getattr(provider, "start_auth", None)
            if not callable(builder):
                raise InvalidRequest(f"provider {provider_id!r} does not support login")
            session = builder(resolve_public_callback_url(config))
            app.state.pending_callback_state = session.state
            app.state.pending_callback_user = principal.username
            # 回调轨道没有本地轮询：登录结果由 /authorize 落库后由前端查凭证列表
        return {"flow": session.flow, "state": session.state, "auth_url": session.auth_url,
                "interval": session.interval, "callback_url": session.callback_url}

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
        schedule_probe(credential_id)
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
            reason = describe_probe_failure(error)
            logger.info("额度探测失败 %s: %s", credential_id, reason)
            return {"probed": False, "reason": reason, "detail": str(error)[:200]}
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
        if result.ok and not result.already_checked_in:
            schedule_probe(credential_id)      # 只有真签到了才会发积分
        # already_checked_in 必须透传：前端靠它区分「刚签到」与「今天已签过」
        return {"ok": result.ok, "credit": result.credit, "code": result.code,
                "message": result.message,
                "already_checked_in": result.already_checked_in}

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
        # 账号切换后额度对应的是新账号，必须重探测而不是沿用旧值
        schedule_probe(credential_id)
        return {"switched": True}

    # --------------------------------------------------- 即时额度探测

    def schedule_probe(credential_id: str) -> None:
        """新增凭证 / OAuth 保存 / 账号切换 / 签到后立即重探测（不阻塞响应）。

        周期扫描是 60 分钟一轮，若不等这一轮，刚加进来的凭证在界面上会一直
        显示「未探测到额度」，调度器也只能把它排在 known 之后。
        """
        provider_id = credentials.provider_of(credential_id)
        provider = registry.get(provider_id or "")
        data = credentials.credential_data(credential_id)
        if provider is None or data is None:
            return

        async def probe() -> None:
            try:
                quota = await provider.probe_quota(data)
            except Exception as error:  # noqa: BLE001 - 探测失败标记为未探测
                logger.warning("即时额度探测失败 %s: %s", credential_id, error)
                credentials.mark_probe_failed(credential_id)
                return
            credentials.save_quota(credential_id, quota)

        app.state.pending_probes.append(asyncio.create_task(probe()))

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
        """TRAE 浏览器 302 落点。

        回调不需要 API Key（浏览器不会带），因此这里不做鉴权，但：
        - 只接受带 refreshToken 的链接，其他一律拒绝
        - 换到的凭证直接落库，响应里绝不回传 token
        - state 必须与 start_auth 发放的一致，防止任意回调被塞进池子
        """
        raw = str(request.url)
        app.state.last_callback_url = raw
        state = request.query_params.get("state")
        provider = registry.get("trae")
        pending = app.state.pending_callback_state
        if provider is None or pending is None or state != pending:
            # 没有进行中的登录，或 state 不匹配（含已消费后的重放）→ 拒绝。
            # 不允许回退到 URL 里的 state，否则旧链接可以被重复兑换。
            return JSONResponse(status_code=400, content=error_payload(
                "no pending TRAE login in progress", "invalid_request", 400))
        if not request.query_params.get("refreshToken") and not request.query_params.get("userJwt"):
            return JSONResponse(status_code=400, content=error_payload(
                "callback missing refreshToken", "invalid_request", 400))
        try:
            credential_data = await provider.complete_callback(raw, state)
        except UpstreamProtocolViolation as error:
            return JSONResponse(status_code=400, content=error_payload(
                str(error), "invalid_credential", 400))
        app.state.pending_callback_state = None
        credential_id = credentials.add(provider="trae", credential_data=credential_data,
                                        nickname=str(credential_data.get("nickname") or ""),
                                        added_by=app.state.pending_callback_user or "")
        schedule_probe(credential_id)
        return {"ok": True, "captured": True, "at": int(time.time())}

    # 静态资源必须最后注册：catch-all 会匹配所有未命中的路径
    # ------------------------------------------------------- 前端静态资源

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        """生产模式服务前端产物；开发模式由 Vite 代理，不经过此路由。

        路径锚定到项目根（而不是当前工作目录），否则从其他目录启动服务时
        会找不到前端产物。找不到时给出可执行的下一步，而不是一句
        「frontend build not found」。
        """
        from fastapi.responses import FileResponse, HTMLResponse

        dist = _frontend_dist()
        if dist is None:
            return HTMLResponse(_FRONTEND_MISSING_HTML, status_code=503)
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        index = dist / "index.html"
        if not index.is_file():
            return HTMLResponse(_FRONTEND_MISSING_HTML, status_code=503)
        return FileResponse(index)

    return app


def resolve_public_callback_url(settings: Settings) -> str:
    """PUBLIC_BASE_URL + /authorize（远程部署必须可被浏览器访问）。"""
    return settings.public_base_url.rstrip("/") + "/authorize"


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONTAINER_DIST = Path("/app/web/dist")

_FRONTEND_MISSING_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>coding2api</title>
<style>body{font-family:ui-sans-serif,system-ui,sans-serif;max-width:44rem;margin:4rem auto;
padding:0 1.5rem;line-height:1.7;color:#1f2937}
code{background:#f3f4f6;padding:.15rem .4rem;border-radius:.25rem;font-size:.9em}
pre{background:#f3f4f6;padding:1rem;border-radius:.5rem;overflow-x:auto}
h1{font-size:1.25rem}</style></head>
<body><h1>管理台前端尚未构建</h1>
<p>后端已经在运行，但找不到前端产物，因此无法显示管理界面。</p>
<p>在项目根目录执行：</p>
<pre>cd web &amp;&amp; pnpm install &amp;&amp; pnpm build</pre>
<p>构建完成后刷新本页即可。API 端点（<code>/v1/*</code>、<code>/api/*</code>）
不受影响，现在就可以用。</p>
</body></html>"""


def _frontend_dist() -> Path | None:
    """定位前端产物目录；不存在时返回 None。"""
    candidates = (
        _PROJECT_ROOT / "web" / "dist",       # 源码运行
        _CONTAINER_DIST,                       # 容器内固定路径
        Path.cwd() / "web" / "dist",           # 兜底：从仓库根启动
    )
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


def describe_probe_failure(error: Exception) -> str:
    """把探测异常翻译成用户能据以行动的原因。

    不能直接暴露 Python 类名（如 UpstreamProtocolViolation）——那是实现细节，
    用户看到它既判断不出问题，也不知道下一步该做什么。
    """
    from .provider.codebuddy.client import UpstreamHTTPError as CodeBuddyHTTPError
    from .provider.codebuddy.events import (
        UpstreamProtocolViolation as CodeBuddyViolation,
    )
    from .provider.trae.client import UpstreamHTTPError as TraeHTTPError
    from .provider.trae.events import UpstreamProtocolViolation as TraeViolation

    http_errors = (CodeBuddyHTTPError, TraeHTTPError)
    if isinstance(error, http_errors):
        status = getattr(error, "status", 0)
        if status in (401, 403):
            return "credential_rejected"      # 凭证失效，需要重新登录
        if status == 429:
            return "rate_limited"             # 上游限流，稍后再试
        if status >= 500:
            return "upstream_unavailable"     # 上游故障，与凭证无关
        return "upstream_rejected"            # 上游拒绝该请求
    if isinstance(error, (CodeBuddyViolation, TraeViolation)):
        return "upstream_response_invalid"    # 响应结构不符，可能是上游改版
    if isinstance(error, TimeoutError):
        return "upstream_timeout"
    return "unknown_error"


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


def run() -> None:
    """本地启动入口：python -m src.main 或 coding2api 命令。"""
    import uvicorn

    config = load_settings()
    uvicorn.run(
        build_app(config), host=config.host, port=config.port, log_level=config.log_level.lower()
    )


if __name__ == "__main__":  # pragma: no cover
    run()

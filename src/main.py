"""FastAPI 组装：基础设施装配 + 中间件/异常处理器，路由委托 src/api 各模块。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .api import (
    admin_auth,
    admin_credentials,
    admin_keys,
    admin_stats,
    authorize,
    chat,
    models,
    playground,
)
from .api.deps import Services
from .auth.csrf import CsrfRejectedError
from .auth.rbac import ForbiddenError, UnauthorizedError
from .auth.throttle import LoginThrottle, ThrottledError
from .compat.openai.errors import error_payload
from .compat.openai.request import InvalidRequest
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
from .stats.collector import StatsCollector
from .stats.query import StatsQuery
from .tasks.pacer import Pacer
from .tasks.runner import build_runner

logger = logging.getLogger(__name__)


def _similar_models(name: str, aliases: dict[str, dict[str, str]],
                    limit: int = 4) -> list[str]:
    """从全部上游的已知模型里找与 name 相近的（400 报错时给用户指路）。"""
    import difflib

    known = sorted({original for per in aliases.values() for original in per.values()})
    close = difflib.get_close_matches(name, known, n=limit, cutoff=0.3)
    if not close:
        prefix = name.lower().split("-")[0]
        close = [m for m in known if m.lower().startswith(prefix)][:limit]
    return [m for m in close if m.lower() != name.lower()][:limit]


def build_app(settings: Settings | None = None, *, providers: dict | None = None,
              users: object | None = None) -> FastAPI:
    config = settings or load_settings()
    db = Database(config.db_path)
    apply_schema(db.connect())
    cipher = CredentialCipher(config.app_secret)
    credentials = CredentialRepository(db, cipher)
    api_keys = ApiKeyRepository(db)
    store = users if users is not None else _load_users(settings=config)
    chat_pacer = (
        None
        if config.codebuddy_chat_min_interval <= 0
        else Pacer(config.codebuddy_chat_min_interval,
                   config.codebuddy_chat_min_interval)
    )
    # TRAE/CB 共享同一 pacer：两渠道请求共同保持最小间隔，
    # 避开各自的频率风控（CB 11128 / TRAE 流内错误）
    registry = providers if providers is not None else {
        "trae": TraeProvider(pacer=chat_pacer),
        "codebuddy": CodeBuddyProvider(pacer=chat_pacer),
    }
    # provider → {小写模型名: 上游原始 id}；api/models.list_models 拉取后就地更新，
    # executor 发请求前把归一名映射回各上游的原始大小写
    model_aliases: dict[str, dict[str, str]] = {}
    executor = Executor(ExecutorDeps(providers=registry, credentials=credentials,
                                     scheduler=Scheduler(),
                                     default_model=config.default_model,
                                     stats=StatsCollector(db),
                                     upstream_model_name=lambda provider_id, model_name: (
                                         model_aliases.get(provider_id, {}).get(
                                             model_name.lower(), model_name)
                                     ),
                                     model_suggestions=lambda name: _similar_models(
                                         name, model_aliases)))

    @asynccontextmanager
    async def lifespan(app_: FastAPI):
        services_ = app_.state.services
        runner = build_runner(credentials, registry, app_.state.stats_collector, config)
        app_.state.task_runner = runner
        await runner.start()
        # 预热模型别名表（动态拉取失败仅记日志，不阻塞启动）
        try:
            await models.list_models(services_)
        except Exception as error:  # noqa: BLE001
            logger.warning("启动预热模型列表失败: %s", error)
        try:
            yield
        finally:
            await runner.stop()
            for task in app_.state.pending_probes:
                task.cancel()
            app_.state.pending_probes.clear()
            for provider in registry.values():
                closer = getattr(provider, "aclose", None)
                if callable(closer):
                    await closer()
            db.close()

    app = FastAPI(title="Coding2API", version="0.1.0", lifespan=lifespan)
    app.state.settings = config
    app.state.users = store
    app.state.credentials = credentials
    app.state.api_keys = api_keys
    app.state.executor = executor
    app.state.stats_collector = StatsCollector(db)
    app.state.stats_query = StatsQuery(db)
    app.state.upstream_auth = _upstream_auth(registry, config)
    app.state.pending_probes = []
    app.state.model_aliases = model_aliases
    app.state.pending_callback_state = None
    app.state.pending_callback_user = None
    app.state.login_throttle = LoginThrottle()

    # 路由层共享依赖容器（TECHNICAL §2 deps.py）
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

    services = Services(
        settings=config,
        credentials=credentials,
        api_keys=api_keys,
        executor=executor,
        registry=registry,
        users=store,
        stats_query=app.state.stats_query,
        login_throttle=app.state.login_throttle,
        upstream_auth=app.state.upstream_auth,
        model_aliases=model_aliases,
        schedule_probe=schedule_probe,
    )
    app.state.services = services

    # --------------------------------------------- 安全中间件（PROPOSAL §8）
    # 1. Host 白名单（防 DNS rebinding）；2. 请求体上限（登录 8KB / 其余 16MB）；
    # 3. 安全响应头（CSP frame-ancestors + X-Frame-Options + nosniff）。

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        host_header = request.headers.get("host", "")
        if not _host_allowed(host_header, config):
            return JSONResponse(status_code=400,
                                content=error_payload("invalid host header",
                                                      "invalid_request", 400))
        content_length = request.headers.get("content-length")
        if content_length is not None and content_length.isdigit():
            limit = (8 * 1024 if request.url.path == "/api/auth/login"
                     else 16 * 1024 * 1024)
            if int(content_length) > limit:
                return JSONResponse(status_code=413,
                                    content=error_payload("request body too large",
                                                          "invalid_request", 413))
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        return response

    # ------------------------------------------------------- 异常处理器

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

    @app.exception_handler(CsrfRejectedError)
    async def _csrf_rejected(_request: Request, _error: CsrfRejectedError):
        return JSONResponse(status_code=403,
                            content=error_payload("cross-origin write rejected",
                                                  "forbidden", 403))

    @app.exception_handler(ThrottledError)
    async def _throttled(_request: Request, _error: ThrottledError):
        return JSONResponse(status_code=429,
                            content=error_payload("too many login attempts, slow down",
                                                  "rate_limited", 429))

    # ------------------------------------------------------------- 对外端点

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # 路由挂载（src/api 各模块；静态资源最后注册，catch-all 会匹配所有路径）
    app.include_router(admin_auth.create_router(services))
    app.include_router(admin_credentials.create_router(services))
    app.include_router(admin_keys.create_router(services))
    app.include_router(admin_stats.create_router(services))
    app.include_router(chat.create_router(services))
    app.include_router(models.create_router(services))
    app.include_router(playground.create_router(services))
    app.include_router(authorize.create_router(services))

    # ------------------------------------------------------- 前端静态资源

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        """生产模式服务前端产物；开发模式由 Vite 代理，不经过此路由。

        路径锚定到项目根（而不是当前工作目录），否则从其他目录启动服务时
        会找不到前端产物。找不到时给出可执行的下一步，而不是一句
        「frontend build not found」。
        """
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


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONTAINER_DIST = Path("/app/web/dist")

_FRONTEND_MISSING_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Coding2API</title>
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


def _host_allowed(host_header: str, settings: Settings) -> bool:
    """Host 白名单（防 DNS rebinding，PROPOSAL §8）。

    ALLOWED_HOSTS 配置优先（逗号分隔）；未配置时放行本地回环、PUBLIC_BASE_URL
    的主机与 testserver（FastAPI TestClient 默认 Host，仅测试场景）。
    只比较主机名，忽略端口。
    """
    hostname = host_header.split(":", 1)[0].strip().lower().strip("[]")
    if settings.allowed_hosts:
        allowed = {h.split(":", 1)[0].strip().lower().strip("[]")
                   for h in settings.allowed_hosts.split(",") if h.strip()}
        return hostname in allowed
    allowed = {"localhost", "127.0.0.1", "::1", "testserver"}
    base = settings.public_base_url
    if base.startswith(("http://", "https://")):
        base = base.split("://", 1)[1]
    path_host = base.split("/", 1)[0].split(":", 1)[0].strip().lower().strip("[]")
    if path_host:
        allowed.add(path_host)
    return hostname in allowed


def run() -> None:
    """本地启动入口：python -m src.main 或 coding2api 命令。"""
    import uvicorn

    config = load_settings()
    uvicorn.run(
        build_app(config), host=config.host, port=config.port, log_level=config.log_level.lower()
    )


if __name__ == "__main__":  # pragma: no cover
    run()

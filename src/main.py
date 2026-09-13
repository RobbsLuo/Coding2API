"""FastAPI 组装：基础设施装配 + 中间件/异常处理器，路由委托 src/api 各模块。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
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
from .config import Settings, load_settings, validate_endpoint_allowed
from .db.conn import Database
from .db.crypto import CredentialCipher, CredentialDecryptError
from .db.migrate import apply_schema
from .db.repo import ApiKeyRepository, CredentialRepository
from .engine.executor import Executor, ExecutorDeps, NoHealthyCredential, NoProviderForModel
from .engine.model_resolver import UnknownModelError
from .engine.scheduler import Scheduler
from .provider.codebuddy.client import CodeBuddyClient, CodeBuddyProvider
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

# 上游连接/超时失败：502（调用方可重试），与凭证健康度无关。
UPSTREAM_ERROR_STATUS = 502

# 请求体上限：登录 8KB（PBKDF2 是 CPU 密集操作，超大 body 无意义）；
# 其余 16MB（聊天请求可能带图片 base64）。
LOGIN_BODY_LIMIT = 8 * 1024
DEFAULT_BODY_LIMIT = 16 * 1024 * 1024

# 每个路径前缀对应的 API 错误形状：/v1 走 OpenAI 兼容体，其余走管理台形状
_API_PREFIXES = ("api/", "v1/")


def _body_limit(path: str) -> int:
    # rstrip 处理尾斜杠：/api/auth/login/ 同样按登录上限（8KB），否则会先被
    # 按 16MB 读完再 307 重定向，绕过登录限流一次
    return LOGIN_BODY_LIMIT if path.rstrip("/") == "/api/auth/login" else DEFAULT_BODY_LIMIT


async def _send_too_large(send) -> None:
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body",
                "body": b'{"error":{"message":"request body too large",'
                        b'"type":"api_error","code":"invalid_request","status":413}}'})


class BodySizeLimitMiddleware:
    """请求体上限（纯 ASGI）：content-length 与实际分块计数双管。

    只看 content-length 头会被 `Transfer-Encoding: chunked` 绕过——
    分块请求根本不带这个头。这里在 receive 层累计字节数，
    超限立即换成 413 响应并截断下游消费。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = _body_limit(scope.get("path", ""))
        # content-length 已超限时直接拒，不必读 body
        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                try:
                    if int(value) > limit:
                        await _send_too_large(send)
                        return
                except ValueError:
                    break

        received = 0
        exceeded = False
        replaced = False

        async def limited_receive():
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    # 截断：让下游读到 EOF，避免继续消费攻击流量
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        async def guarded_send(message):
            """超限后丢弃下游的全部响应，只发出我们自己的 413。"""
            nonlocal replaced
            if not exceeded:
                await send(message)
                return
            if message["type"] != "http.response.start":
                return                      # 丢弃下游 body
            if not replaced:
                replaced = True
                await _send_too_large(send)

        await self.app(scope, limited_receive, guarded_send)


def _api_not_found(path: str) -> JSONResponse:
    """未匹配的 /api、/v1 路径返回 JSON 404。

    静态资源是 catch-all 路由（返回 index.html），不排除 API 前缀的话，
    客户端拼错端点会拿到 200 + HTML，看起来“调用成功”，极难排查。
    """
    return JSONResponse(status_code=404,
                        content=error_payload(f"no such endpoint: /{path}",
                                              "invalid_request", 404))


def _codebuddy_endpoint(config: Settings) -> str:
    """解析 CodeBuddy 上游地址，并强制白名单校验。

    CODEBUDDY_API_ENDPOINT 是文档化的配置项，但之前从未被接线——
    改这个值对实际请求无效，属于隐蔽的配置陷阱。白名单校验确保
    真实 Token 不会被发往未授权主机。
    """
    endpoint = config.codebuddy_api_endpoint.strip()
    if not validate_endpoint_allowed(endpoint, config):
        raise ValueError(
            f"CODEBUDDY_API_ENDPOINT {endpoint!r} is not in CODEBUDDY_ALLOWED_ENDPOINTS")
    return endpoint


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


def _forget_task(task: asyncio.Task, pending: list) -> None:
    """从 pending_probes 摘除已完成的探测任务（幂等，关机清理后不报错）。"""
    with contextlib.suppress(ValueError):
        pending.remove(task)


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
        "codebuddy": CodeBuddyProvider(
            client=CodeBuddyClient(endpoint=_codebuddy_endpoint(config)), pacer=chat_pacer),
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
                                         name, model_aliases),
                                     model_aliases=model_aliases))

    @asynccontextmanager
    async def lifespan(app_: FastAPI):
        services_ = app_.state.services
        runner = build_runner(credentials, registry, app_.state.stats_collector, config)
        app_.state.task_runner = runner
        await runner.start()
        # 预热模型别名表（动态拉取失败仅记日志，不阻塞启动）；force 绕过 TTL
        try:
            await models.list_models(services_, force=True)
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
    # BodySizeLimitMiddleware 必须在最外层：FastAPI.add_middleware 会把后加
    # 的包在更外层，所以它在最后添加（见 build_app 末尾）。
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

        # 完成后从列表里摘除：否则长时间运行会无限累积已完成的 Task 对象
        task = asyncio.create_task(probe())
        app.state.pending_probes.append(task)
        task.add_done_callback(lambda done: _forget_task(done, app.state.pending_probes))

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
        # 请求体上限由 BodySizeLimitMiddleware 在 ASGI 层处理
        # （纯读 content-length 会被 chunked 请求绕过）。
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

    @app.exception_handler(CredentialDecryptError)
    async def _decrypt_failed(_request: Request, _error: CredentialDecryptError):
        """APP_SECRET 变更或密文损坏：必须给出可行动提示，而不是 500。"""
        logger.error("凭证解密失败：APP_SECRET 是否被更换过？")
        return JSONResponse(status_code=500,
                            content=error_payload(
                                "credential decryption failed; APP_SECRET may have changed",
                                "credential_decrypt_failed", 500))

    @app.exception_handler(httpx.TransportError)
    async def _transport_error(_request: Request, error: httpx.TransportError):
        """上游连接/超时失败：502 而不是 500（调用方可重试）。

        httpx 的 TimeoutException/ConnectError 在引擎里不被 _classify 认识
        （没有 kind()），会直接冒泡——以前表现成 500，语义错误。
        """
        logger.warning("上游传输层失败: %s: %s", type(error).__name__, error)
        return JSONResponse(
            status_code=UPSTREAM_ERROR_STATUS,
            content=error_payload(f"upstream transport failed: {type(error).__name__}",
                                  "upstream_unavailable", UPSTREAM_ERROR_STATUS))

    @app.exception_handler(ThrottledError)
    async def _throttled(_request: Request, _error: ThrottledError):
        response = JSONResponse(status_code=429,
                                content=error_payload(
                                    "too many login attempts, slow down",
                                    "rate_limited", 429))
        # OpenAI 客户端按 Retry-After 退避；缺失会立即重试加剧限流
        response.headers.setdefault("Retry-After", "60")
        return response

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

        /api、/v1 前缀不当作前端路由：让拼错的端点显式失败。
        """
        if path.startswith(_API_PREFIXES) or path in ("api", "v1"):
            return _api_not_found(path)
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

    app.add_middleware(BodySizeLimitMiddleware)
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

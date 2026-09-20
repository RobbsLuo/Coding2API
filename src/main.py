"""FastAPI 组装：基础设施装配 + 生命周期，横切逻辑委托 src/webapp/ 与 src/api/。

这里只做「把零件接起来」：数据库/加密/仓储 → provider 注册表 → 执行引擎 →
Services 容器 → 路由挂载 → 生命周期。中间件、异常处理器、静态资源分别在
src/webapp/ 的 limits / security / handlers / static 模块里。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api import (
    admin_auth,
    admin_credentials,
    admin_keys,
    admin_stats,
    authorize,
    balance,
    chat,
    models,
    playground,
)
from .api.deps import Services
from .auth.throttle import LoginThrottle
from .config import Settings, load_settings, validate_endpoint_allowed
from .db.conn import Database
from .db.crypto import CredentialCipher
from .db.migrate import apply_schema
from .db.repo import ApiKeyRepository, CredentialRepository, GrowthRepository
from .engine.affinity import ConversationAffinity
from .engine.executor import Executor, ExecutorDeps
from .engine.scheduler import Scheduler
from .provider.codebuddy.client import CodeBuddyClient, CodeBuddyProvider
from .provider.codebuddy.oauth import CodeBuddyOAuth
from .provider.trae.client import TraeProvider
from .stats.collector import StatsCollector
from .stats.query import StatsQuery
from .tasks.pacer import Pacer
from .tasks.runner import build_runner
from .version import app_version
from .webapp import limits as _limits
from .webapp import static as _static
from .webapp.handlers import register_exception_handlers
from .webapp.limits import BodySizeLimitMiddleware
from .webapp.logging import configure_logging
from .webapp.security import host_allowed, security_middleware

logger = logging.getLogger(__name__)

# --------------------------------------------------------------- 向后兼容再导出
# 这些名字历史上住在 main.py，测试与外部脚本直接 import。真实定义已移到
# src/webapp/ 下，保留别名以免 import 失败。
#
# 注意：**打补丁必须打到真实定义处**（`src.webapp.static.frontend_dist` /
# `src.webapp.static._PROJECT_ROOT`）。patch 这里的别名只是改了 main 的模块
# 属性，webapp 内部调用读的是自己的全局，不会生效（会静默失效）。
_frontend_dist = _static.frontend_dist
_api_not_found = _static.api_not_found
_API_PREFIXES = _static._API_PREFIXES
LOGIN_BODY_LIMIT = _limits.LOGIN_BODY_LIMIT
DEFAULT_BODY_LIMIT = _limits.DEFAULT_BODY_LIMIT


def _host_allowed(host_header: str, settings: Settings) -> bool:
    return host_allowed(host_header, settings)


def _body_limit(path: str) -> int:
    return _limits._body_limit(path)


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
    # 这里必须配：生产路径是 `uvicorn src.main:build_app --factory`（launchd /
    # 容器 / systemd 均如此），不经过 run()，否则审计等 INFO 日志仍被丢弃。
    # 幂等，测试反复调用 build_app 不会叠加 handler。
    configure_logging(config.log_level)
    db = Database(config.db_path)
    apply_schema(db.connect())
    cipher = CredentialCipher(config.app_secret)
    credentials = CredentialRepository(db, cipher)
    growth_events = GrowthRepository(db)
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
    stats_collector = StatsCollector(db)
    executor = Executor(ExecutorDeps(providers=registry, credentials=credentials,
                                     scheduler=Scheduler(
                                         expiry_window=config.quota_expiry_window_seconds),
                                     default_model=config.default_model,
                                     stats=stats_collector,
                                     affinity=ConversationAffinity(
                                         ttl_seconds=config.conversation_sticky_seconds),
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
        runner = build_runner(credentials, registry, app_.state.stats_collector, config,
                              growth_events=app_.state.growth_events)
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

    app = FastAPI(title="Coding2API", version=app_version(), lifespan=lifespan)
    # BodySizeLimitMiddleware 必须在最外层：FastAPI.add_middleware 会把后加
    # 的包在更外层，所以它在最后添加（见 build_app 末尾）。
    app.state.settings = config
    app.state.users = store
    app.state.credentials = credentials
    app.state.api_keys = api_keys
    app.state.executor = executor
    app.state.stats_collector = stats_collector
    app.state.growth_events = growth_events
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
        growth_events=growth_events,
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
    # Host 白名单（防 DNS rebinding）+ 安全响应头；请求体上限由 ASGI 中间件处理
    # （纯读 content-length 会被 chunked 请求绕过）。
    app.middleware("http")(security_middleware)

    register_exception_handlers(app)

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
    app.include_router(balance.create_router(services))
    app.include_router(playground.create_router(services))
    app.include_router(authorize.create_router(services))

    # ------------------------------------------------------- 前端静态资源
    _static.register_spa_routes(app)

    app.add_middleware(BodySizeLimitMiddleware)
    return app


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
    from .auth.users import UsersFileStore

    store = UsersFileStore(settings.users_file)
    store.validate()
    return store


def run() -> None:
    """本地启动入口：python -m src.main 或 coding2api 命令。"""
    import uvicorn

    config = load_settings()
    # 应用日志（审计、上游错误等）在此之前无 handler 会被丢弃
    configure_logging(config.log_level)
    uvicorn.run(
        build_app(config), host=config.host, port=config.port, log_level=config.log_level.lower()
    )


if __name__ == "__main__":  # pragma: no cover
    run()

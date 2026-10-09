"""FastAPI 组装：基础设施装配 + 生命周期，横切逻辑委托 src/webapp/ 与 src/api/。

这里只做「把零件接起来」：数据库/加密/仓储 → provider 注册表 → 执行引擎 →
Services 容器 → 路由挂载 → 生命周期。中间件、异常处理器、静态资源分别在
src/webapp/ 的 limits / security / handlers / static 模块里。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from .api import (
    activate,
    admin_alerts,
    admin_audit,
    admin_auth,
    admin_credentials,
    admin_keys,
    admin_pricing,
    admin_settings,
    admin_stats,
    admin_users,
    authorize,
    balance,
    chat,
    messages,
    models,
    playground,
    responses,
)
from .api.deps import Services
from .auth.throttle import LoginThrottle
from .config import (
    Settings,
    load_settings,
    validate_codearts_endpoint_allowed,
    validate_endpoint_allowed,
    validate_kilo_endpoint_allowed,
    validate_qoder_endpoint_allowed,
    validate_zen_endpoint_allowed,
)
from .db.conn import Database
from .db.crypto import CredentialCipher
from .db.migrate import apply_schema
from .db.repo import (
    AlertRepository,
    ApiKeyRepository,
    AuditRepository,
    CredentialRepository,
    CreditEventRepository,
    GrowthRepository,
    RuntimeSettingsRepository,
    UserRepository,
)
from .engine.affinity import ConversationAffinity
from .engine.executor import Executor, ExecutorDeps
from .engine.model_resolver import KNOWN_PROVIDERS, parse_fallback_groups
from .engine.scheduler import Scheduler
from .pricing import fetch_prices, load_prices_snapshot, save_prices
from .provider.codearts import CodeArtsProvider
from .provider.codearts.client import CodeArtsClient
from .provider.codearts.oauth import CodeArtsOAuth
from .provider.codebuddy.client import CodeBuddyClient, CodeBuddyProvider
from .provider.codebuddy.oauth import CodeBuddyOAuth
from .provider.kilo.client import KiloClient, KiloProvider
from .provider.proxy import parse_provider_proxies
from .provider.qoder import QoderProvider
from .provider.qoder.auth import QoderOAuth
from .provider.qoder.client import QoderClient
from .provider.qoder.events import detect_realm_from_domain
from .provider.trae.client import TraeClient, TraeProvider
from .provider.zen.client import ZenClient, ZenProvider
from .runtime_settings import load_runtime_settings
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


def _zen_endpoint(config: Settings) -> str:
    """解析 Zen 端点并强制白名单校验（防把请求发往未授权主机）。"""
    endpoint = config.zen_api_endpoint.strip()
    if not validate_zen_endpoint_allowed(endpoint, config):
        raise ValueError(
            f"ZEN_API_ENDPOINT {endpoint!r} is not in ZEN_ALLOWED_ENDPOINTS")
    return endpoint


def _kilo_endpoint(config: Settings) -> str:
    """解析 Kilo 端点并强制白名单校验（防把请求发往未授权主机）。"""
    endpoint = config.kilo_api_endpoint.strip()
    if not validate_kilo_endpoint_allowed(endpoint, config):
        raise ValueError(
            f"KILO_API_ENDPOINT {endpoint!r} is not in KILO_ALLOWED_ENDPOINTS")
    return endpoint


def _qoder_host(config: Settings) -> str:
    """解析 Qoder openapi 端点并强制白名单校验。"""
    endpoint = config.qoder_api_endpoint.strip()
    if not validate_qoder_endpoint_allowed(endpoint, config):
        raise ValueError(
            f"QODER_API_ENDPOINT {endpoint!r} is not in QODER_ALLOWED_ENDPOINTS")
    return endpoint


def _qoder_gateway(config: Settings) -> str:
    """解析 Qoder 推理网关端点并强制白名单校验。"""
    endpoint = config.qoder_gateway_endpoint.strip()
    if not validate_qoder_endpoint_allowed(endpoint, config):
        raise ValueError(
            f"QODER_GATEWAY_ENDPOINT {endpoint!r} is not in QODER_ALLOWED_ENDPOINTS")
    return endpoint


def _codearts_endpoint(config: Settings) -> str:
    """解析 CodeArts 端点并强制白名单校验。"""
    endpoint = config.codearts_api_endpoint.strip()
    if not validate_codearts_endpoint_allowed(endpoint, config):
        raise ValueError(
            f"CODEARTS_API_ENDPOINT {endpoint!r} is not in CODEARTS_ALLOWED_ENDPOINTS")
    return endpoint


def _seed_free_credential(credentials: CredentialRepository, *, provider: str,
                          nickname: str) -> None:
    """确保池里有一条无凭证渠道的虚拟凭证（幂等）。

    无凭证渠道（Zen / Kilo）没有凭证概念，但调度/冷却/统计全部按
    credentials 行工作；没有这条占位行，该渠道永远不会被选为候选。启动时
    若发现没有会补上；用户在凭证页删除后，也可点「添加 …」立即补回（走通用
    导入端点）。要永久停用请用「暂停」而不是删除。
    """
    if credentials.candidates([provider]):
        return
    credentials.add(provider=provider, credential_data={}, nickname=nickname,
                    added_by="system")


def _seed_zen_credential(credentials: CredentialRepository) -> None:
    """确保池里有一条 zen 虚拟凭证（幂等）。"""
    _seed_free_credential(credentials, provider="zen", nickname="OpenCode Zen")


def _seed_kilo_credential(credentials: CredentialRepository) -> None:
    """确保池里有一条 kilo 虚拟凭证（幂等）。"""
    _seed_free_credential(credentials, provider="kilo", nickname="Kilo Gateway")


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


async def _warm_model_list(services) -> None:
    """后台预热模型列表 / 别名表：失败仅记日志，绝不影响启动与聊天。

    不强制刷新：`restore_model_catalog` 已按快照年龄播种 TTL，这里只补「没有
    快照 / 快照已超 TTL」的渠道。刚恢复的表被立刻重拉一遍纯属白花（kilo 实测
    10–22s、zen 探活 12–15s），而预热本来就是后台跑的，不抢这个时间。
    """
    try:
        await models.list_models(services)
    except Exception as error:  # noqa: BLE001 - 预热失败不阻断服务
        logger.warning("启动预热模型列表失败: %s", error)


async def _warm_price_table(refresh, table: dict) -> None:
    """后台补价表：仅在**没有落盘快照**时立即拉一次。

    有快照就交给周期性任务——models.dev 是数 MB 公开大表，刚恢复就重拉纯属
    白花；但没有快照（首次部署 / 快照损坏）时若不补，成本要等到下一轮
    `PRICE_CATALOG_MINUTES`（默认每日）才可用，期间全显示 —。放后台跑不阻塞
    启动；失败仅记日志（成本显示 — 而已，不影响聊天）。
    """
    if table:
        return
    try:
        await refresh()
    except Exception as error:  # noqa: BLE001 - 预热失败不阻断服务
        logger.warning("启动预热价表失败: %s", error)


def _restore_model_list(services) -> None:
    """启动时同步回灌落盘模型目录：失败仅记日志（缓存是加速手段，不是必需项）。"""
    try:
        models.restore_model_catalog(services)
    except Exception as error:  # noqa: BLE001 - 恢复失败不阻断服务
        logger.warning("恢复落盘模型目录失败: %s", error)


def build_app(settings: Settings | None = None, *, providers: dict | None = None,
              users: object | None = None) -> FastAPI:
    config = settings or load_settings()
    # 这里必须配：生产路径是 `uvicorn src.main:build_app --factory`（launchd /
    # 容器 / systemd 均如此），不经过 run()，否则审计等 INFO 日志仍被丢弃。
    # 幂等，测试反复调用 build_app 不会叠加 handler。
    configure_logging(config.log_level)
    db = Database(config.db_path)
    apply_schema(db.connect())
    # 运行时配置覆盖层（B3.2）：热更键读 DB 覆盖值，其余透明委托给 env 快照。
    # 必须在 apply_schema 之后构造——新库的 runtime_settings 表由上面建好。
    runtime = load_runtime_settings(config, RuntimeSettingsRepository(db))
    cipher = CredentialCipher(config.app_secret)
    credentials = CredentialRepository(db, cipher)
    growth_events = GrowthRepository(db)
    credit_events = CreditEventRepository(db)
    api_keys = ApiKeyRepository(db)
    user_repo = UserRepository(db)
    audit = AuditRepository(db)
    alerts = AlertRepository(db)
    store = users if users is not None else _load_users(
        settings=config, user_repo=user_repo)
    # 聊天节流器存「取值器」而不是快照：管理台改最小间隔后立即生效。
    # 必须传 lambda 而不是 live(runtime.x)——后者会当场求值一次再包成常量，
    # 对 RuntimeSettings 就等于没热更。0 表示关闭，由 Pacer.disabled 处理。
    # allow_concurrent：按凭证分桶并在桶内放行并发（同渠道同模型并发不再被
    # 逐级 +interval 串行化，实测 3 并发 TTFB 1.5/6.7/11.5s → 齐平）。
    chat_pacer = Pacer(lambda: runtime.codebuddy_chat_min_interval,
                       lambda: runtime.codebuddy_chat_min_interval,
                       allow_concurrent=True)
    # TRAE/CB 共享同一 pacer 实例，但桶键带渠道前缀 + 凭证身份，彼此不互堵。
    # zen 单独一个 pacer：zen 是匿名免费层，没有账号级频率风控，共享只会让
    # zen 请求排在 CB/TRAE 之后空等满最小间隔（连发/并发时每个 +5s，实测）。
    zen_pacer = Pacer(lambda: runtime.zen_chat_min_interval,
                      lambda: runtime.zen_chat_min_interval,
                      allow_concurrent=True)
    # kilo 同样独立一个 pacer：同为匿名免费层，与 zen / CB / TRAE 互不排队。
    kilo_pacer = Pacer(lambda: runtime.kilo_chat_min_interval,
                       lambda: runtime.kilo_chat_min_interval,
                       allow_concurrent=True)
    # Qoder / CodeArts 是真实账号渠道，上游按账号频控；各自独立 pacer，
    # 与其余渠道互不排队（同渠道同凭证共桶、桶内允许并发）。
    qoder_pacer = Pacer(lambda: runtime.qoder_chat_min_interval,
                        lambda: runtime.qoder_chat_min_interval,
                        allow_concurrent=True)
    codearts_pacer = Pacer(lambda: runtime.codearts_chat_min_interval,
                           lambda: runtime.codearts_chat_min_interval,
                           allow_concurrent=True,
                           max_concurrency=lambda: runtime.codearts_max_concurrency,
                           window_seconds=lambda: runtime.codearts_request_window_seconds)
    # 按渠道出站代理（P1-6）：启动期解析一次，装配时注入到各 provider 的
    # httpx client；运行中改值需重启（连接池已建立，见 provider/proxy.py）。
    proxies = parse_provider_proxies(config.provider_proxies, KNOWN_PROVIDERS)
    registry = providers if providers is not None else {
        "trae": TraeProvider(
            client=TraeClient(proxy=proxies.get("trae")), pacer=chat_pacer),
        "codebuddy": CodeBuddyProvider(
            client=CodeBuddyClient(
                endpoint=_codebuddy_endpoint(config),
                sanitize_markers=config.codebuddy_sanitize_channel_markers,
                proxy=proxies.get("codebuddy"),
            ), pacer=chat_pacer),
        "zen": ZenProvider(
            client=ZenClient(host=_zen_endpoint(config),
                             version=config.zen_opencode_version,
                             proxy=proxies.get("zen")),
            pacer=zen_pacer),
        "kilo": KiloProvider(
            client=KiloClient(host=_kilo_endpoint(config),
                              proxy=proxies.get("kilo")),
            pacer=kilo_pacer),
        "qoder": QoderProvider(
            client=QoderClient(host=_qoder_host(config),
                               gateway=_qoder_gateway(config),
                               proxy=proxies.get("qoder")),
            pacer=qoder_pacer),
        "codearts": CodeArtsProvider(
            client=CodeArtsClient(endpoint=_codearts_endpoint(config),
                                  proxy=proxies.get("codearts")),
            pacer=codearts_pacer),
    }
    # 默认装配路径（生产）才种子无凭证渠道的虚拟凭证：测试注入自定义 registry
    # 时不应凭空多出一条无对应 provider 的凭证行。
    if providers is None:
        _seed_zen_credential(credentials)
        _seed_kilo_credential(credentials)
    # provider → {小写模型名: 上游原始 id}；api/models.list_models 拉取后就地更新，
    # executor 发请求前把归一名映射回各上游的原始大小写
    model_aliases: dict[str, dict[str, str]] = {}
    # 模型目录原始表的**同一引用**交给 executor（credit_rate 查询）与 Services
    # （list_models 缓存）——这里先建空 dict，Services 装配时直接挂它
    model_cache: dict[str, dict[str, Any]] = {}
    # 成本价表（models.dev 刊例价，USD/百万 token）：启动时同步读回落盘快照
    # （零上游请求），后台 price_catalog 循环再周期刷新。同一份 dict 引用交给
    # StatsCollector，刷新时就地替换后新写入的明细立即用上新价。
    price_table: dict[str, tuple[float, float, float]] = {}
    # 快照保存时刻（供管理台「价表」页展示「这份表何时拉取」）：启动读回，
    # 每轮后台刷新后更新。
    price_saved_at: float | None = None
    try:
        price_table, price_saved_at = load_prices_snapshot(config.data_dir)
    except Exception as error:  # noqa: BLE001 - 价表是加速手段，失败不阻断服务
        logger.warning("恢复落盘价表失败: %s", error)
    stats_collector = StatsCollector(
        db, prices=lambda: price_table, usd_cny_rate=lambda: runtime.usd_cny_rate)
    executor = Executor(ExecutorDeps(providers=registry, credentials=credentials,
                                     scheduler=Scheduler(
                                         expiry_window=lambda: runtime.quota_expiry_window_seconds,
                                         secondary_expiry_window=lambda: (
                                             runtime.quota_expiry_secondary_window_seconds)),
                                     default_model=lambda: runtime.default_model,
                                     stats=stats_collector,
                                     max_auto_continues=config.auto_continue_max,
                                     complete_timeout_seconds=(
                                         config.upstream_complete_timeout_seconds),
                                     affinity=ConversationAffinity(
                                         ttl_seconds=lambda: runtime.conversation_sticky_seconds),
                                     upstream_model_name=lambda provider_id, model_name: (
                                         model_aliases.get(provider_id, {}).get(
                                             model_name.lower(), model_name)
                                     ),
                                     model_suggestions=lambda name: _similar_models(
                                         name, model_aliases),
                                     fallback_groups=lambda: parse_fallback_groups(
                                         runtime.model_fallback_groups),
                                     model_aliases=model_aliases,
                                     # 同一份 dict 的引用：模型目录恢复 / 拉取后
                                     # executor 立即可见（倍率查询走它）
                                     model_list_cache=model_cache))

    @asynccontextmanager
    async def lifespan(app_: FastAPI):
        services_ = app_.state.services
        # 落盘目录回灌（同步、零上游请求）：别名表立刻可用，扁平名请求马上就能
        # 把候选收窄到真正持有该模型的渠道。放在最前面（runner 与预热之前）——
        # 否则启动到预热跑完这段时间里别名表是空的，请求会按全部渠道扇出，
        # 真实打一轮不认该模型的上游（CodeBuddy 11102 / TRAE 4001）。
        # 读取失败只丢缓存，见 model_catalog。
        _restore_model_list(services_)

        async def _refresh_model_catalog() -> dict[str, int]:
            """后台兜底刷新模型目录一轮。

            只回报条目数：整份列表有几百条，塞进任务运行态会被管理台原样渲染。
            没有这条循环时，模型表只在有人调 `/v1/models` / Playground 时按 TTL
            刷新——纯 API 用法的部署（客户端自己缓存了列表）会让归属表与落盘
            快照一起变陈旧：上游新增的模型不认识 → 扁平名请求扇出，各渠道回
            11102/4001 并写上 6 小时起步的 (凭证, 模型) 负缓存。
            """
            result = await models.list_models(services_)
            return {"models": len(result.get("data") or [])}

        async def _refresh_price_catalog() -> dict[str, int]:
            """后台刷新价表一轮：拉 models.dev → 落盘 → 就地换入。

            就地替换（clear + update）而不是重新绑定变量：StatsCollector 持有
            的是这份 dict 的引用，换引用会让它读到旧表。返回条目数供运行态展示
            （整张表几百条，不透传原始数据）。
            """
            table = await fetch_prices(config.models_dev_url)
            if not table:
                raise RuntimeError("models.dev 返回空价表")
            price_table.clear()
            price_table.update(table)
            save_prices(config.data_dir, price_table)
            app_.state.price_saved_at = time.time()
            return {"models": len(price_table)}

        # 传 runtime（而非 env 快照）：后台循环的热更值每轮现读覆盖层。
        runner = build_runner(credentials, registry, app_.state.stats_collector, runtime,
                              growth_events=app_.state.growth_events,
                              credit_events=credit_events,
                              audit=audit,
                              alerts=alerts,
                              model_catalog=_refresh_model_catalog,
                              price_catalog=_refresh_price_catalog)
        app_.state.task_runner = runner
        await runner.start()
        # 预热模型别名表：放后台跑（force 绕过 TTL）。
        # 不内联 await 的原因：zen 免费层探活最慢的模型可占十几秒，内联会让应用
        # 在这段时间里不响应 /health，容器存活探针可能误判；动态拉取失败仅记日志。
        app_.state.model_warmup_task = asyncio.create_task(_warm_model_list(services_))
        # 价表同理：仅在无落盘快照时后台补拉一次，避免首次部署成本空窗到下一轮。
        app_.state.price_warmup_task = asyncio.create_task(
            _warm_price_table(_refresh_price_catalog, price_table))
        # 让预热任务先跑一步：失败时日志立即落盘（成功与否都不阻塞下面 yield）。
        await asyncio.sleep(0)
        try:
            yield
        finally:
            await runner.stop()
            warmup = getattr(app_.state, "model_warmup_task", None)
            if warmup is not None and not warmup.done():
                warmup.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await warmup
            price_warmup = getattr(app_.state, "price_warmup_task", None)
            if price_warmup is not None and not price_warmup.done():
                price_warmup.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await price_warmup
            # stale-while-revalidate 的后台刷新任务：不取消会把 in-flight 的
            # 上游请求（zen 探活可占十几秒）带出事件循环，关闭变慢且报错。
            for task in list(services_.model_refresh_tasks):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            services_.model_refresh_tasks.clear()
            services_.pending_model_refreshes.clear()
            for task in app_.state.pending_probes:
                task.cancel()
            app_.state.pending_probes.clear()
            for provider in registry.values():
                closer = getattr(provider, "aclose", None)
                if callable(closer):
                    await closer()
            for oauth in getattr(app_.state, "upstream_auth", {}).values():
                closer = getattr(oauth, "aclose", None)
                if callable(closer):
                    await closer()
            db.close()

    app = FastAPI(title="Coding2API", version=app_version(), lifespan=lifespan,
                  # API 结构不对外暴露：匿名可拉全量端点清单等于送侦察图。
                  # 本地调试需要 Swagger 时 ENABLE_DOCS=true 显式打开。
                  redoc_url=None,
                  docs_url="/docs" if config.enable_docs else None,
                  openapi_url="/openapi.json" if config.enable_docs else None)
    # BodySizeLimitMiddleware 必须在最外层：FastAPI.add_middleware 会把后加
    # 的包在更外层，所以它在最后添加（见 build_app 末尾）。
    app.state.settings = config
    # 热更覆盖层单独挂一份：管理台写完后要能立刻拿到新的 snapshot，
    # 同时避免把「进程启动时的 env 快照」和「当前生效值」混为一谈。
    app.state.runtime_settings = runtime
    app.state.users = store
    app.state.user_repo = user_repo
    app.state.audit = audit
    app.state.credentials = credentials
    app.state.api_keys = api_keys
    app.state.executor = executor
    app.state.stats_collector = stats_collector
    app.state.growth_events = growth_events
    app.state.stats_query = StatsQuery(db)
    app.state.upstream_auth = _upstream_auth(registry, config)
    app.state.pending_probes = []
    app.state.model_aliases = model_aliases
    app.state.model_list_cache = model_cache
    app.state.price_table = price_table
    app.state.price_saved_at = price_saved_at
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
        settings=runtime,
        credentials=credentials,
        growth_events=growth_events,
        credit_events=credit_events,
        api_keys=api_keys,
        executor=executor,
        registry=registry,
        users=store,
        user_repo=user_repo,
        audit=audit,
        alerts=alerts,
        stats_query=app.state.stats_query,
        login_throttle=app.state.login_throttle,
        upstream_auth=app.state.upstream_auth,
        model_aliases=model_aliases,
        # executor 拿的就是这一份引用：目录恢复 / 拉取 / TTL 刷新写进来后，
        # 聊天路径的倍率查询立即看到新数据
        model_list_cache=model_cache,
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

    @app.get("/healthz")
    async def healthz():
        """池健康：进程存活 + 凭证池四类计数（互斥，合计 = total）。

        `/health` 只回答「进程还在吗」，适合做容器存活探针；`/healthz` 额外
        暴露可用凭证数，供外部监控在池子耗尽（ready=0）时提前告警——那是
        服务「活着但用不了」的状态，存活探针看不出来。
        """
        return {"status": "ok", "service": "coding2api", "version": app_version(),
                "credentials": services.credentials.pool_counts()}

    # 路由挂载（src/api 各模块；静态资源最后注册，catch-all 会匹配所有路径）
    app.include_router(admin_auth.create_router(services))
    app.include_router(admin_credentials.create_router(services))
    app.include_router(admin_keys.create_router(services))
    app.include_router(admin_settings.create_router(services))
    app.include_router(admin_pricing.create_router(services))
    app.include_router(admin_alerts.create_router(services))
    app.include_router(admin_stats.create_router(services))
    app.include_router(admin_users.create_router(services))
    app.include_router(admin_audit.create_router(services))
    app.include_router(activate.create_router(services))
    app.include_router(chat.create_router(services))
    app.include_router(responses.create_router(services))
    app.include_router(messages.create_router(services))
    app.include_router(models.create_router(services))
    app.include_router(balance.create_router(services))
    app.include_router(playground.create_router(services))
    app.include_router(authorize.create_router(services))

    # ------------------------------------------------------- 前端静态资源
    _static.register_spa_routes(app)

    app.add_middleware(BodySizeLimitMiddleware)
    return app


def _upstream_auth(registry: dict, settings: Settings) -> dict:
    """返回支持 poll 轨道的 provider 的 OAuth 实现。

    CodeBuddy 走设备码轮询；Qoder 走设备码 PKCE（区域由 openapi 主机推导）；
    CodeArts 走门户 ticket 轮询（token 响应直接给临时 AK/SK，无本地回调）。
    """
    flows: dict = {}
    proxies = parse_provider_proxies(settings.provider_proxies, KNOWN_PROVIDERS)
    codebuddy = registry.get("codebuddy")
    endpoint = getattr(getattr(codebuddy, "client", None), "endpoint", None)
    if endpoint is not None:
        flows["codebuddy"] = CodeBuddyOAuth(endpoint, proxy=proxies.get("codebuddy"))
    qoder = registry.get("qoder")
    host = getattr(getattr(qoder, "client", None), "host", None)
    if host is not None:
        flows["qoder"] = QoderOAuth(detect_realm_from_domain(host),
                                    proxy=proxies.get("qoder"))
    codearts = registry.get("codearts")
    client = getattr(codearts, "client", None)
    login = getattr(client, "login", None)
    if login is not None:
        # 登录后补账号身份：token 响应不带用户名，不补则凭证昵称为空、
        # 统计明细的凭证列空白。复用 provider 的签名客户端，不另建连接池。
        flows["codearts"] = CodeArtsOAuth(login, identity_client=client,
                                          proxy=proxies.get("codearts"))
    return flows


def _load_users(*, settings: Settings, user_repo):
    """构造 DB 用户存储并完成引导（B5）。

    引导把 users.txt + ADMIN_USERNAMES 的职责交接给 SQLite（见
    auth/bootstrap.py）；文件缺失不再是致命错误——只要库里已有用户就能启动，
    这让「部署时忘了挂 users.txt」不再等于服务不可用。
    """
    from .auth.bootstrap import bootstrap_users
    from .auth.users import DbUserStore, UsersFileError, UsersFileStore

    store = DbUserStore(user_repo)
    file_store = None
    path = settings.users_file
    if Path(path).is_file():
        try:
            file_store = UsersFileStore(path)
            file_store.validate()
        except UsersFileError:
            # 文件存在但格式非法/无用户：当作「没有文件」，让库里的用户说了算；
            # 真正的死局（库也为空）由 bootstrap 第 3 步统一报错。
            file_store = None
    bootstrap_users(settings, user_repo, file_store=file_store,
                    log=lambda msg, *args: logger.info(msg, *args))
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

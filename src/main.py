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
from fastapi.responses import JSONResponse

from .api import (
    activate,
    admin_alerts,
    admin_audit,
    admin_auth,
    admin_backups,
    admin_badges,
    admin_billing,
    admin_credentials,
    admin_keys,
    admin_model_availability,
    admin_model_catalog,
    admin_settings,
    admin_stats,
    admin_upstream,
    admin_users,
    authorize,
    balance,
    chat,
    logs,
    messages,
    metrics,
    models,
    playground,
    responses,
    usage,
)
from .api.context import build_context_compressor, build_prefix_observer
from .api.deps import Services
from .auth.throttle import LoginThrottle
from .benchmarks import (
    BenchmarkTable,
    build_benchmark_table,
    build_model_catalog,
    build_price_table,
    fetch_openrouter_models,
    load_catalog_snapshot,
    save_catalog,
)
from .config import (
    Settings,
    load_settings,
    validate_codearts_endpoint_allowed,
    validate_codebuddy_intl_endpoint_allowed,
    validate_endpoint_allowed,
    validate_kilo_endpoint_allowed,
    validate_officeace_endpoint_allowed,
    validate_qoder_endpoint_allowed,
    validate_zen_endpoint_allowed,
)
from .db.backup import apply_pending_restore
from .db.conn import Database
from .db.crypto import CredentialCipher, CredentialDecryptError
from .db.key_usage import KeyUsageRepository
from .db.limits import LimitsRepository, day_usage
from .db.migrate import apply_schema
from .db.repo import (
    AlertRepository,
    ApiKeyRepository,
    AuditRepository,
    CredentialRepository,
    CreditEventRepository,
    GrowthRepository,
    ModelProbeRepository,
    RuntimeSettingsRepository,
    UserRepository,
)
from .engine.affinity import ConversationAffinity
from .engine.executor import Executor, ExecutorDeps
from .engine.half_open import HalfOpenGuard
from .engine.inflight import InflightGuard
from .engine.limits import LimitGuard
from .engine.model_resolver import KNOWN_PROVIDERS, parse_fallback_groups
from .engine.scheduler import Scheduler
from .instance_lock import AlreadyRunning, hold_instance_lock
from .key_quota import KeyQuotaGuard
from .provider.codearts import CodeArtsProvider
from .provider.codearts.client import CodeArtsClient
from .provider.codearts.oauth import CodeArtsOAuth
from .provider.codebuddy.client import CodeBuddyClient, CodeBuddyProvider
from .provider.codebuddy.intl import CodeBuddyIntlProvider
from .provider.codebuddy.model_access_store import (
    cache_path as model_access_cache_path,
)
from .provider.codebuddy.oauth import CodeBuddyOAuth
from .provider.kilo.client import KiloClient, KiloProvider
from .provider.officeace import OfficeAceProvider
from .provider.officeace.client import OfficeAceClient
from .provider.officeace.oauth import OfficeAceOAuth
from .provider.proxy import (
    CredentialProxyPool,
    parse_provider_proxies,
    parse_proxy_pool,
    set_proxy_pool,
)
from .provider.qoder import QoderProvider
from .provider.qoder.auth import QoderOAuth
from .provider.qoder.client import QoderClient
from .provider.qoder.events import detect_realm_from_domain
from .provider.trae.client import TraeClient, TraeProvider
from .provider.zen.client import ZenClient, ZenProvider
from .runtime_settings import load_runtime_settings
from .stats.collector import StatsCollector
from .stats.live import LiveEventHub
from .stats.query import StatsQuery
from .tasks.backup import BackupTask
from .tasks.model_probe import ModelProbeTask
from .tasks.pacer import Pacer
from .tasks.runner import build_runner
from .tasks.upstream_status import UpstreamStatusTask
from .upstream_status import UpstreamStatusStore
from .version import app_version
from .webapp import limits as _limits
from .webapp import static as _static
from .webapp.access_log import RequestLogMiddleware
from .webapp.handlers import register_exception_handlers
from .webapp.limits import BodySizeLimitMiddleware
from .webapp.log_buffer import LogBuffer, detach_root_handler, sync_root_handler
from .webapp.logging import configure_logging
from .webapp.request_id import RequestIdMiddleware
from .webapp.security import host_allowed, security_middleware

logger = logging.getLogger(__name__)

# SIGTERM 后允许在途请求（含长流）收尾的最长秒数。四处必须一致：
# 本常量、deploy/launchd 的 ExitTimeOut、deploy/systemd 的 TimeoutStopSec、
# docker-compose 的 stop_grace_period（后三者是「平台给进程的停机窗口」，
# 必须 >= 本值，否则 SIGKILL 会打断排空）。
#
# 30s 的来历已不成立：实测 p99 延迟 57.7s、7 天内 27 次请求超过 120s
# （`usage_events.latency_ms`），30s 窗口下日志留下 38 次
# `Cancel N running task(s), timeout graceful shutdown exceeded` —— 每次重启
# 都在硬砍长流请求，统计也落不下来。90s 覆盖到 p99 以上，超过这个量级的
# 只剩「客户端本来就会断开」的极长会话。
GRACEFUL_SHUTDOWN_SECONDS = 90

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


def _resolve_endpoint(config: Settings, *, attr: str, validator, env_name: str,
                      allowed_name: str, strip_slash: bool = False) -> str:
    """解析并白名单校验一个上游端点（7 个渠道共用一套逻辑）。

    端点走白名单是安全约束而非风格问题：这些值决定真实 Token 发往哪个主机，
    配错即泄露。此前 7 份几乎逐字相同的实现，改一处（如错误文案）要改七遍。

    `env_name` 是配置项名（报错前缀），`allowed_name` 是对应白名单名；
    两者不总是同前缀（如 QODER_GATEWAY_ENDPOINT 属于 QODER_ALLOWED_ENDPOINTS）。
    """
    endpoint = getattr(config, attr).strip()
    if strip_slash:
        endpoint = endpoint.rstrip("/")
    if not validator(endpoint, config):
        raise ValueError(f"{env_name} {endpoint!r} is not in {allowed_name}")
    return endpoint


def _codebuddy_endpoint(config: Settings) -> str:
    """解析 CodeBuddy 上游地址，并强制白名单校验。

    CODEBUDDY_API_ENDPOINT 是文档化的配置项，但之前从未被接线——
    改这个值对实际请求无效，属于隐蔽的配置陷阱。白名单校验确保
    真实 Token 不会被发往未授权主机。
    """
    return _resolve_endpoint(config, attr="codebuddy_api_endpoint",
                             validator=validate_endpoint_allowed,
                             env_name="CODEBUDDY_API_ENDPOINT",
                             allowed_name="CODEBUDDY_ALLOWED_ENDPOINTS")


def _codebuddy_intl_endpoint(config: Settings) -> str:
    """解析 CodeBuddy 国际站地址，并强制**独立**白名单校验。

    与国内站的白名单刻意互不包含：两站 Token 由不同网关签发，混发即泄露。
    """
    return _resolve_endpoint(config, attr="codebuddy_intl_api_endpoint",
                             validator=validate_codebuddy_intl_endpoint_allowed,
                             env_name="CODEBUDDY_INTL_API_ENDPOINT",
                             allowed_name="CODEBUDDY_INTL_ALLOWED_ENDPOINTS",
                             strip_slash=True)


def _zen_endpoint(config: Settings) -> str:
    """解析 Zen 端点并强制白名单校验（防把请求发往未授权主机）。"""
    return _resolve_endpoint(config, attr="zen_api_endpoint",
                             validator=validate_zen_endpoint_allowed,
                             env_name="ZEN_API_ENDPOINT",
                             allowed_name="ZEN_ALLOWED_ENDPOINTS")


def _kilo_endpoint(config: Settings) -> str:
    """解析 Kilo 端点并强制白名单校验（防把请求发往未授权主机）。"""
    return _resolve_endpoint(config, attr="kilo_api_endpoint",
                             validator=validate_kilo_endpoint_allowed,
                             env_name="KILO_API_ENDPOINT",
                             allowed_name="KILO_ALLOWED_ENDPOINTS")


def _qoder_host(config: Settings) -> str:
    """解析 Qoder openapi 端点并强制白名单校验。"""
    return _resolve_endpoint(config, attr="qoder_api_endpoint",
                             validator=validate_qoder_endpoint_allowed,
                             env_name="QODER_API_ENDPOINT",
                             allowed_name="QODER_ALLOWED_ENDPOINTS")


def _qoder_gateway(config: Settings) -> str:
    """解析 Qoder 推理网关端点并强制白名单校验。"""
    return _resolve_endpoint(config, attr="qoder_gateway_endpoint",
                             validator=validate_qoder_endpoint_allowed,
                             env_name="QODER_GATEWAY_ENDPOINT",
                             allowed_name="QODER_ALLOWED_ENDPOINTS")


def _codearts_endpoint(config: Settings) -> str:
    """解析 CodeArts 端点并强制白名单校验。"""
    return _resolve_endpoint(config, attr="codearts_api_endpoint",
                             validator=validate_codearts_endpoint_allowed,
                             env_name="CODEARTS_API_ENDPOINT",
                             allowed_name="CODEARTS_ALLOWED_ENDPOINTS")


def _officeace_endpoint(config: Settings) -> str:
    """解析 officeAce 控制面端点并强制白名单校验。

    模型网关（每账号一个 `modelgw-*.officeace.*`）不在这里：它是上游
    `client-permission-validate` 下发的、存在凭证里的值，改不了也写不错。
    """
    return _resolve_endpoint(config, attr="officeace_api_endpoint",
                             validator=validate_officeace_endpoint_allowed,
                             env_name="OFFICEACE_API_ENDPOINT",
                             allowed_name="OFFICEACE_ALLOWED_ENDPOINTS",
                             strip_slash=True)


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
    _seed_free_credential(credentials, provider="kilo", nickname="Kilo")


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


async def _warm_model_catalog(refresh, table: dict) -> None:
    """后台补 OpenRouter 模型目录：仅在**没有落盘快照**时立即拉一次。

    与原先 `_warm_price_table` / `_warm_benchmark_table` 同一口径：有快照就交给
    周期任务（上游变动少，24 小时一次足够），没有快照（首次部署 / 快照损坏 /
    升级后新引入的文件缺失）时若不补，成本与能力分要等到下一轮
    `OPENROUTER_CATALOG_MINUTES` 才可用。放后台跑不阻塞启动；失败仅记日志
    （成本与分数显示 — 而已，不影响聊天）。
    """
    if table:
        return
    try:
        await refresh()
    except Exception as error:  # noqa: BLE001 - 预热失败不阻断服务
        logger.warning("启动预热模型目录失败: %s", error)


async def _warm_upstream_status(task: UpstreamStatusTask) -> None:
    """启动时后台补抓一轮上游状态页。

    与 `_warm_model_catalog` 同一口径：循环体是先睡后跑，不预热的话页面在
    重启后要空等一个周期（默认 5 分钟）才第一次有数据——运维页刚部署完就是
    空白最容易被当成坏了。放后台跑不阻塞启动；失败仅记日志（下一轮循环会
    重试，且单源失败本来就被 `fetch_source` 兜成 unknown 条目）。
    """
    try:
        await task.run_once()
    except Exception as error:  # noqa: BLE001 - 预热失败不阻断服务
        logger.warning("启动预热上游状态页失败: %s", error)


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
    # 管理台上传的待恢复备份在**建连接之前**应用；坏文件只留档、不阻断启动
    apply_pending_restore(config.db_path)
    db = Database(config.db_path)
    apply_schema(db.connect())
    # 运行时配置覆盖层：热更键读 DB 覆盖值，其余透明委托给 env 快照。
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
    # 上游状态页快照：纯内存、不落盘（状态分钟级易变，旧快照比没有更危险）。
    # 装配在这里、路由层每请求现读 app.state，任务就地替换整批。
    upstream_status_store = UpstreamStatusStore()
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
    # CB 国际站独立 pacer：另一套账号体系与另一条网关，共享会让两站请求排进
    # 同一条队列（国内站请求排队时国际站请求白等满间隔，反之亦然）。
    codebuddy_intl_pacer = Pacer(lambda: runtime.codebuddy_intl_chat_min_interval,
                                 lambda: runtime.codebuddy_intl_chat_min_interval,
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
    # officeAce 独立 pacer：真实账号渠道（单账号单用户），上游没有 CodeArts
    # 那种硬并发限，默认不节流；与其余渠道互不排队。
    officeace_pacer = Pacer(lambda: runtime.officeace_chat_min_interval,
                            lambda: runtime.officeace_chat_min_interval,
                            allow_concurrent=True)
    # 按渠道出站代理：启动期解析一次，装配时注入到各 provider 的
    # httpx client；运行中改值需重启（连接池已建立，见 provider/proxy.py）。
    proxies = parse_provider_proxies(config.provider_proxies, KNOWN_PROVIDERS)
    # 出口 IP 粘性：池按凭证 id 稳定分配，装配期一次设定（连接池已建，改值需重启）
    set_proxy_pool(CredentialProxyPool(parse_proxy_pool(config.credential_proxy_pool)))
    registry = providers if providers is not None else {
        "trae": TraeProvider(
            client=TraeClient(proxy=proxies.get("trae")), pacer=chat_pacer),
        "codebuddy": CodeBuddyProvider(
            client=CodeBuddyClient(
                endpoint=_codebuddy_endpoint(config),
                sanitize_markers=config.codebuddy_sanitize_channel_markers,
                proxy=proxies.get("codebuddy"),
            ), pacer=chat_pacer),
        "codebuddy_intl": CodeBuddyIntlProvider(
            client=CodeBuddyClient(
                endpoint=_codebuddy_intl_endpoint(config),
                sanitize_markers=config.codebuddy_sanitize_channel_markers,
                # 国际站网关硬性要求首条 system（实测 400/11128），国内站无此要求
                require_leading_system=True,
                proxy=proxies.get("codebuddy_intl"),
                # 国际站指纹：稳定会话 id + 官方版本常量 + 会话/模型头 + JWT uid；
                # 体走 gzip（官方 CLI 形态）；探活结果落盘，避免重启后 20 连发
                intl=True,
                gzip_body=True,
                access_cache_path=model_access_cache_path(config.data_dir),
            ), pacer=codebuddy_intl_pacer),
        "zen": ZenProvider(
            client=ZenClient(host=_zen_endpoint(config),
                             version=config.zen_opencode_version,
                             api_root=config.zen_api_root,
                             gate_tools=tuple(
                                 t.strip() for t in config.zen_gate_tools.split(",")
                                 if t.strip()) or ("bash", "read"),
                             user_agent=config.zen_user_agent,
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
        "officeace": OfficeAceProvider(
            client=OfficeAceClient(cloud_base=_officeace_endpoint(config),
                                   proxy=proxies.get("officeace")),
            pacer=officeace_pacer),
    }
    # 默认装配路径（生产）才种子无凭证渠道的虚拟凭证：测试注入自定义 registry
    # 时不应凭空多出一条无对应 provider 的凭证行。
    if providers is None:
        _seed_zen_credential(credentials)
        _seed_kilo_credential(credentials)
    # provider → {小写模型名: 上游原始 id}；api/models.list_models 拉取后就地更新，
    # executor 发请求前把归一名映射回各上游的原始大小写
    model_aliases: dict[str, dict[str, str]] = {}
    # 逐请求事件广播：StatsCollector 写入明细后 publish，SSE 实时流订阅。
    # 进程内即可——明细写入与订阅都在同一个进程（单进程 SQLite 部署）。
    live_events = LiveEventHub()
    # 日志环形缓冲（E3）：容量现读热更项 `log_buffer_lines`（0 = 关闭整页）。
    # 这里只建对象；handler 在 lifespan 里按当前值挂/摘，避免每个 build_app
    # 都往 root 上叠一个（测试会反复建 app）。
    log_buffer = LogBuffer(capacity=lambda: runtime.log_buffer_lines)
    # 模型目录原始表的**同一引用**交给 executor（credit_rate 查询）与 Services
    # （list_models 缓存）——这里先建空 dict，Services 装配时直接挂它
    model_cache: dict[str, dict[str, Any]] = {}
    # 成本价表（OpenRouter 刊例价，USD/百万 token）：启动时同步读回落盘快照
    # （零上游请求），后台 model_catalog 循环再周期刷新。同一份 dict 引用交给
    # StatsCollector，刷新时就地替换后新写入的明细立即用上新价。
    price_table: dict[str, tuple[float, float, float]] = {}
    # 模型目录明细（管理台「模型列表」页）：与价表同一次抓取的更多元数据，
    # 同样启动读回、后台就地刷新。命名避开 main 里既有的「模型目录」缓存
    # （那是网关的 model_cache）。
    models_dev_catalog: dict[str, dict[str, Any]] = {}
    # 能力排行（Artificial Analysis 指数）：与价表同一次抓取（同一份 OpenRouter
    # 响应），同一节奏刷新。同一份 dict 引用交给路由层（每请求现读
    # app.state），刷新时就地替换后新请求立即用上新分数。
    model_benchmarks: BenchmarkTable = {}
    # 快照保存时刻（供管理台展示「这份数据何时拉取」）：启动读回，每轮后台
    # 刷新后更新。三个表同一次抓取，故只有一个时刻。
    price_saved_at: float | None = None
    try:
        price_table, model_benchmarks, models_dev_catalog, price_saved_at = (
            load_catalog_snapshot(config.data_dir))
    except Exception as error:  # noqa: BLE001 - 价表是加速手段，失败不阻断服务
        logger.warning("恢复落盘模型目录失败: %s", error)
    stats_collector = StatsCollector(
        db, prices=lambda: price_table, usd_cny_rate=lambda: runtime.usd_cny_rate,
        on_record=live_events.publish)
    # 凭证级限额护栏：配置表 + 当日用量聚合注入 LimitGuard（快照缓存），
    # executor 选号前用它过滤触顶候选；管理台改完 invalidate() 立即生效。
    limits_repo = LimitsRepository(db)
    # 主动探活结论表（只供管理台展示 + 探活成功清负缓存）：与调度状态严格分开，
    # 见 src/tasks/model_probe.py 的模块 docstring。
    model_probes = ModelProbeRepository(db)
    limit_guard = LimitGuard(limits_repo,
                             lambda since: day_usage(db, since=since))
    # Key 级配额：当日用量按 api_key_id 聚合（明细表），RPM/TPM/并发走进程内
    # 滑动窗口——不落库、重启清零，单进程部署下即精确。
    key_usage = KeyUsageRepository(db)
    key_quota = KeyQuotaGuard(key_usage.for_key)
    # 用户级配额（Playground 会话直连）：按用户名聚合当日用量——该用户经任何
    # 入口的流量都计入，否则「你自己 Key 刷爆了但仍能用 Playground 无限调」。
    user_quota = KeyQuotaGuard(key_usage.for_user)
    # 凭证在途租约：跨渠道的「单账号并发上限」，选号前过滤 + 请求结束释放。
    # 上限现读运行态覆盖层，管理台改完下一请求即生效（无快照，无需 invalidate）。
    inflight_guard = InflightGuard(lambda: runtime.max_inflight_per_credential)
    # 冷却到期半开探测：到期先放 1 个请求，成功再全量回池（进程内状态，
    # 重启丢失只会让每个号多探一次）。
    half_open_guard = HalfOpenGuard()
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
                                     # A1 流式计时（默认 0=关，管理台可热更）：每次取
                                     # 下一个上游事件时现读，改完下一帧即生效
                                     first_event_timeout_seconds=lambda: (
                                         runtime.stream_first_event_timeout_seconds),
                                     stream_idle_timeout_seconds=lambda: (
                                         runtime.stream_idle_timeout_seconds),
                                     # A4 短冷却「等待续上」（默认 0=关）：全池都在
                                     # 短冷却时等最近的一个到期，而不是直接 503
                                     short_cooldown_wait_seconds=lambda: (
                                         runtime.short_cooldown_wait_seconds),
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
                                     # reasoning 控制（默认全关）：剥离出站思考内容
                                     # + 按渠道改写客户端显式给的档位。两者都现读
                                     # 运行态覆盖层，管理台改完下一请求即生效
                                     force_disable_thinking=lambda: runtime.force_disable_thinking,
                                     reasoning_effort_map=lambda: runtime.reasoning_effort_map,
                                     model_aliases=model_aliases,
                                     # 同一份 dict 的引用：模型目录恢复 / 拉取后
                                     # executor 立即可见（倍率查询走它）
                                     model_list_cache=model_cache,
                                     # 上下文压缩（修订）：选号后按实际服务
                                     # 渠道的窗口裁剪；settings 传运行态覆盖层，
                                     # 开关与四个参数每请求现读（热更）
                                     context_compress=build_context_compressor(
                                         model_cache, model_aliases, runtime),
                                     # 前缀缓存诊断（默认关）：命中率异常时
                                     # 在管理台打开，日志里就能看到前缀在哪条
                                     # 消息分叉、压缩是否每轮都在裁
                                     prefix_debug=build_prefix_observer(runtime),
                                     limits=limit_guard,
                                     inflight=inflight_guard,
                                     half_open=half_open_guard))

    @asynccontextmanager
    async def lifespan(app_: FastAPI):
        services_ = app_.state.services
        # 落盘目录回灌（同步、零上游请求）：别名表立刻可用，扁平名请求马上就能
        # 把候选收窄到真正持有该模型的渠道。放在最前面（runner 与预热之前）——
        # 否则启动到预热跑完这段时间里别名表是空的，请求会按全部渠道扇出，
        # 真实打一轮不认该模型的上游（CodeBuddy 11102 / TRAE 4001）。
        # 读取失败只丢缓存，见 model_catalog。
        _restore_model_list(services_)

        async def _refresh_channel_models() -> dict[str, int]:
            """后台兜底刷新渠道模型列表一轮。

            只回报条目数：整份列表有几百条，塞进任务运行态会被管理台原样渲染。
            没有这条循环时，模型表只在有人调 `/v1/models` / Playground 时按 TTL
            刷新——纯 API 用法的部署（客户端自己缓存了列表）会让归属表与落盘
            快照一起变陈旧：上游新增的模型不认识 → 扁平名请求扇出，各渠道回
            11102/4001 并写上 6 小时起步的 (凭证, 模型) 负缓存。
            """
            result = await models.list_models(services_)
            return {"models": len(result.get("data") or [])}

        async def _refresh_model_catalog() -> dict[str, int]:
            """后台刷新模型目录一轮：拉 OpenRouter → 建三张表 → 落盘 → 就地换入。

            价格（成本估算）、明细（「模型列表」页）与能力分（Playground /
            `/v1/models`）来自同一次抓取，故一个任务、一个快照文件、一个
            `saved_at`。

            就地替换（clear + update）而不是重新绑定变量：StatsCollector 持有
            价表 dict 的引用、路由层每请求现读 `app.state` 里那两个，换引用会
            让它们读到旧表。返回条目数供运行态展示（整张表几百条，不透传原始
            数据）。
            """
            raw = await fetch_openrouter_models(config.openrouter_models_url)
            table = build_price_table(raw)
            if not table:
                raise RuntimeError("OpenRouter 返回空价表")
            benchmarks = build_benchmark_table(raw)
            catalog = build_model_catalog(raw)
            price_table.clear()
            price_table.update(table)
            model_benchmarks.clear()
            model_benchmarks.update(benchmarks)
            models_dev_catalog.clear()
            models_dev_catalog.update(catalog)
            save_catalog(config.data_dir, price_table, model_benchmarks,
                         models_dev_catalog)
            app_.state.price_saved_at = time.time()
            return {"models": len(price_table)}

        # 传 runtime（而非 env 快照）：后台循环的热更值每轮现读覆盖层。
        upstream_status_task = UpstreamStatusTask(
            upstream_status_store,
            timeout=config.upstream_status_timeout_seconds,
            enabled=lambda: runtime.upstream_status_enabled)
        model_probe_task = ModelProbeTask(
            credentials, registry, model_probes,
            upstream_model=lambda provider_id, name: (
                model_aliases.get(provider_id, {}).get(name.lower(), name)))
        app_.state.model_probe_task = model_probe_task
        runner = build_runner(credentials, registry, app_.state.stats_collector, runtime,
                              growth_events=app_.state.growth_events,
                              credit_events=credit_events,
                              audit=audit,
                              alerts=alerts,
                              backup=BackupTask(config.db_path,
                                                keep=lambda: runtime.backup_keep),
                              upstream_status=upstream_status_task,
                              model_probe=model_probe_task,
                              model_probe_minutes=lambda: runtime.model_probe_minutes,
                              model_catalog=_refresh_channel_models,
                              openrouter_catalog=_refresh_model_catalog,
                              openrouter_catalog_minutes=(
                                  lambda: runtime.openrouter_catalog_minutes))
        app_.state.task_runner = runner
        await runner.start()
        # 预热模型别名表：放后台跑（force 绕过 TTL）。
        # 不内联 await 的原因：zen 免费层探活最慢的模型可占十几秒，内联会让应用
        # 在这段时间里不响应 /health，容器存活探针可能误判；动态拉取失败仅记日志。
        app_.state.model_warmup_task = asyncio.create_task(_warm_model_list(services_))
        # 价表 / 能力分 / 模型明细同理：仅在无落盘快照时后台补拉一次，避免首次
        # 部署的成本与分数空窗到下一轮（默认 24 小时后）。
        app_.state.catalog_warmup_task = asyncio.create_task(
            _warm_model_catalog(_refresh_model_catalog, price_table))
        # 上游状态页同理：不预热的话运维页在重启后要空等一个周期（默认 5 分钟）
        # 才第一次有数据。放在后台跑（8 个外部端点，不抢启动时间）。
        app_.state.upstream_warmup_task = asyncio.create_task(
            _warm_upstream_status(upstream_status_task))
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
            catalog_warmup = getattr(app_.state, "catalog_warmup_task", None)
            if catalog_warmup is not None and not catalog_warmup.done():
                catalog_warmup.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await catalog_warmup
            # 上游状态页预热同理：它在等 8 个外部端点，不取消会把 in-flight
            # 的 httpx 请求带出事件循环。
            upstream_warmup = getattr(app_.state, "upstream_warmup_task", None)
            if upstream_warmup is not None and not upstream_warmup.done():
                upstream_warmup.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await upstream_warmup
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
            # 摘掉日志缓冲处理器：否则已关闭的 app 实例仍被 root 引用，
            # 反复建 app 的测试里每条日志会写进多个死实例。
            detach_root_handler(log_buffer)
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
    # SSE 实时流的订阅中心（与 StatsCollector 的 on_record 同源）
    app.state.live_events = live_events
    app.state.log_buffer = log_buffer
    app.state.upstream_auth = _upstream_auth(registry, config)
    app.state.pending_probes = []
    app.state.model_aliases = model_aliases
    app.state.model_list_cache = model_cache
    app.state.price_table = price_table
    app.state.models_dev_catalog = models_dev_catalog
    app.state.price_saved_at = price_saved_at
    # 能力排行表：与 models_dev_catalog 同一形态挂在 app.state（运行数据，不是
    # 仓储/引擎依赖），刷新时就地换值，路由层每请求现读。
    app.state.model_benchmarks = model_benchmarks
    # 待完成的 TRAE 回调登录：{用户名: (started_at_monotonic, state)}。
    # 按发起人分键：单槽全局态会被两个管理员的并发登录互相覆盖，
    # 把 A 的凭证记到 B 名下。回调不带 state，故用会话 Cookie 认发起人。
    app.state.pending_callbacks = {}
    app.state.login_throttle = LoginThrottle()
    # 上游状态页快照（纯内存）：任务就地替换，路由每请求现读。
    app.state.upstream_status = upstream_status_store

    # 路由层共享依赖容器（TECHNICAL §2 deps.py）
    def schedule_probe(credential_id: str) -> None:
        """新增凭证 / OAuth 保存 / 账号切换 / 签到后立即重探测（不阻塞响应）。

        周期扫描是 60 分钟一轮，若不等这一轮，刚加进来的凭证在界面上会一直
        显示「未探测到额度」，调度器也只能把它排在 known 之后。
        """
        provider_id = credentials.provider_of(credential_id)
        provider = registry.get(provider_id or "")
        # 解不开（APP_SECRET 换过 / 密文损坏）→ 跳过这条凭证，不让后台任务
        # 因一条坏凭证整轮失败
        try:
            data = credentials.credential_data(credential_id)
        except CredentialDecryptError:
            return
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
        limits=limits_repo,
        model_probes=model_probes,
        limit_guard=limit_guard,
        key_quota=key_quota,
        key_usage=key_usage,
        user_quota=user_quota,
        stats_query=app.state.stats_query,
        login_throttle=app.state.login_throttle,
        live_events=live_events,
        log_buffer=log_buffer,
        upstream_auth=app.state.upstream_auth,
        model_aliases=model_aliases,
        # executor 拿的就是这一份引用：目录恢复 / 拉取 / TTL 刷新写进来后，
        # 聊天路径的倍率查询立即看到新数据
        model_list_cache=model_cache,
        schedule_probe=schedule_probe,
    )
    app.state.services = services

    # --------------------------------------------- 安全中间件（PROPOSAL §6）
    # Host 白名单（防 DNS rebinding）+ 安全响应头；请求体上限由 ASGI 中间件处理
    # （纯读 content-length 会被 chunked 请求绕过）。
    app.middleware("http")(security_middleware)

    register_exception_handlers(app)

    # ------------------------------------------------------------- 对外端点

    # 同时接受 GET 与 HEAD：FastAPI 的 APIRoute 不像 Starlette 那样自动给 GET
    # 补 HEAD，只注册 GET 时监控发 `curl -I /health` 会一直吃 405。HEAD 用单独
    # 装饰器挂同一个处理函数并排除出 schema——若写成 api_route(methods=[...])，
    # OpenAPI 里 GET/HEAD 会撞出重复 operationId。HEAD 的 body 由 ASGI 层按
    # 协议丢弃，处理函数照常返回同一份内容即可。
    @app.get("/health")
    @app.head("/health", include_in_schema=False)
    async def health():
        return {"status": "ok"}

    @app.get("/healthz")
    @app.head("/healthz", include_in_schema=False)
    async def healthz():
        """就绪探针：可用凭证数为 0 时回 503（ready>0 回 200）。

        `/health` 只回答「进程还在吗」（恒 200，容器存活探针用）；`/healthz`
        额外暴露凭证池计数，并在 `ready == 0` 时回 503——那是「进程活着但没有
        任何可用凭证」的状态，对话请求必然 503，负载均衡/外部监控应据此摘流量。
        计数在两种状态下都返回，监控可读到「为什么不可用」。

        注意：容器 healthcheck 请继续用 `/health`（存活语义）。把探针指到
        `/healthz` 会在池子耗尽时把容器标成 unhealthy，而重启并不能修好一个
        没有可用凭证的池子。
        """
        counts = services.credentials.pool_counts()
        ready = counts["ready"] > 0
        payload = {"status": "ok" if ready else "unavailable",
                   "service": "coding2api", "version": app_version(),
                   "credentials": counts}
        return JSONResponse(status_code=200 if ready else 503, content=payload)

    # 路由挂载（src/api 各模块；静态资源最后注册，catch-all 会匹配所有路径）
    app.include_router(admin_auth.create_router(services))
    app.include_router(admin_credentials.create_router(services))
    app.include_router(admin_keys.create_router(services))
    app.include_router(admin_settings.create_router(services))
    app.include_router(admin_backups.create_router(services))
    app.include_router(admin_model_catalog.create_router(services))
    app.include_router(admin_model_availability.create_router(services))
    app.include_router(admin_alerts.create_router(services))
    app.include_router(admin_badges.create_router(services))
    app.include_router(admin_upstream.create_router(services))
    app.include_router(admin_stats.create_router(services))
    app.include_router(admin_billing.create_router(services))
    app.include_router(admin_users.create_router(services))
    app.include_router(admin_audit.create_router(services))
    # 服务端日志页（E3）：admin-only 只读视图；`log_buffer_lines=0` 时回 404。
    app.include_router(logs.create_router(services))
    app.include_router(activate.create_router(services))
    app.include_router(chat.create_router(services))
    app.include_router(responses.create_router(services))
    app.include_router(messages.create_router(services))
    app.include_router(models.create_router(services))
    app.include_router(balance.create_router(services))
    app.include_router(playground.create_router(services))
    app.include_router(authorize.create_router(services))
    app.include_router(usage.create_router(services))
    # Prometheus 指标出口（无鉴权，与 /healthz 同口径）：必须注册在 SPA
    # catch-all 之前，否则会被通配路由吞掉返回 index.html。
    app.include_router(metrics.create_router(services))

    # ------------------------------------------------------- 前端静态资源
    _static.register_spa_routes(app)

    app.add_middleware(BodySizeLimitMiddleware)
    # 请求级结构化日志：在 RequestId 内侧（要用它生成的 ID）、在 BodySizeLimit
    # 外侧（413 也是请求结果，要记下来）。热更开关走取值器，改完下一请求生效。
    app.add_middleware(RequestLogMiddleware,
                       enabled=lambda: bool(runtime.request_log_enabled))
    # 请求 ID 必须比 BodySizeLimitMiddleware 更外层：请求体超限时后者自己造
    # 413 响应（不经过更内层的 security），只有最外层才能保证**任何**响应
    # （含 413 / Host 校验 400）都带上 X-Request-ID。
    app.add_middleware(RequestIdMiddleware)
    # 日志缓冲挂到 root（幂等）：只在 `log_buffer_lines > 0` 时挂，关闭时摘掉；
    # 与 `_RootHandler`（stdout/stderr）并存，不改变既有落盘行为。放在最后
    # 是为了让上面所有装配期的日志也能进缓冲（首个快照更有用）。
    sync_root_handler(log_buffer)
    return app


def _upstream_auth(registry: dict, settings: Settings) -> dict:
    """返回支持 poll 轨道的 provider 的 OAuth 实现。

    CodeBuddy 走设备码轮询（国内站与国际站各一套：endpoint 不同、登录域名不同）；
    Qoder 走设备码 PKCE（区域由 openapi 主机推导）；
    CodeArts 走门户 ticket 轮询（token 响应直接给临时 AK/SK，无本地回调）；
    officeAce 走云端授权码轮询（回调由云端承接，同样无本地回调）。
    """
    flows: dict = {}
    proxies = parse_provider_proxies(settings.provider_proxies, KNOWN_PROVIDERS)
    for channel in ("codebuddy", "codebuddy_intl"):
        provider = registry.get(channel)
        endpoint = getattr(getattr(provider, "client", None), "endpoint", None)
        if endpoint is not None:
            flows[channel] = CodeBuddyOAuth(endpoint, proxy=proxies.get(channel))
    qoder = registry.get("qoder")
    host = getattr(getattr(qoder, "client", None), "host", None)
    if host is not None:
        flows["qoder"] = QoderOAuth(detect_realm_from_domain(host),
                                    proxy=proxies.get("qoder"))
    officeace = registry.get("officeace")
    officeace_client = getattr(officeace, "client", None)
    officeace_login = getattr(officeace_client, "login", None)
    if officeace_login is not None:
        # officeAce 走 poll 轨道：授权回调由**云端**承接，服务端只轮询
        # `/v1/claw/auth/code`。登录流量（每次登录几十次短请求）与聊天流量
        # 的并发特征完全不同，故用独立连接池；但共用同一个出站代理设置。
        flows["officeace"] = OfficeAceOAuth(
            officeace_login, proxy=proxies.get("officeace"))
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
    """构造 DB 用户存储并完成引导。

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
    import sys

    import uvicorn

    config = load_settings()
    # 应用日志（审计、上游错误等）在此之前无 handler 会被丢弃
    configure_logging(config.log_level)
    # 单实例保护必须在 build_app 之前：后者会建库、跑迁移、应用待恢复备份，
    # 这些写操作在第二个实例上重复执行就是双写（见 src/instance_lock.py）。
    try:
        instance_lock = hold_instance_lock(config.data_dir)
    except AlreadyRunning as error:
        # **退出码 0 而不是非零**：这是「已经有人在服务」，不是故障。
        # 三条启动路径的守护策略都是「非零退出才重拉」
        # （launchd KeepAlive.SuccessfulExit=false、systemd Restart=on-failure、
        #   compose restart），用非零退出会变回每 10s 一次的重启循环——
        # 实测 2026-09-19~10-17 刷了 2377 条 `[Errno 48]`，完整 traceback
        # 把真正的故障挤出视野。真崩溃仍然是非零退出，照样被拉起来。
        logger.error("%s", error)
        print(f"启动中止（这不是故障，已有实例在服务）：{error}", file=sys.stderr)
        return
    try:
        uvicorn.run(
            build_app(config), host=config.host, port=config.port,
            log_level=config.log_level.lower(),
            # 关掉 uvicorn 自带 access log：请求级留痕已由 RequestLogMiddleware
            # 覆盖（logfmt，带时间戳与 request_id，可与 usage_events 关联），而
            # uvicorn 的裸行无时间戳、含完整 query string，只是把同样的请求再写满
            # 另一个轮转文件。
            access_log=False,
            # 收到 SIGTERM 后先停止接受新连接，给在途请求（含长流）最多
            # GRACEFUL_SHUTDOWN_SECONDS 秒收尾，再强关。默认 None 是无限等，
            # 配合容器/launchd 的停机超时会变成「卡住直到被 SIGKILL」。
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
        )
    finally:
        instance_lock.release()


if __name__ == "__main__":  # pragma: no cover
    run()

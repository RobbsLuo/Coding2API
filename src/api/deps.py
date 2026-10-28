"""API 依赖注入：共享服务容器 + 鉴权依赖（TECHNICAL §2 deps.py）。

build_app 把数据库仓储、执行引擎、provider 注册表等打包成 Services 挂到
app.state.services；各路由模块通过 create_router(services) 闭包捕获，或
通过 get_services 从 request 取（鉴权依赖用后者，因为路由签名里只有
request 可用）。
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request

from ..auth.access import client_ip, ip_allowed
from ..auth.api_key import key_expired
from ..auth.csrf import check_csrf, check_origin
from ..auth.rbac import (
    ROLE_ADMIN,
    ForbiddenError,
    PasswordChangeRequiredError,  # noqa: F401 - 对外经 deps 暴露，handlers 引用
    Principal,
    UnauthorizedError,
)
from ..auth.session import SESSION_COOKIE, verify_session_token
from ..auth.throttle import LoginThrottle
from ..compat.openai.request import InvalidRequest
from ..config import Settings
from ..db.key_usage import KeyUsageRepository
from ..db.limits import LimitsRepository
from ..db.repo import (
    AlertRepository,
    ApiKeyRepository,
    AuditRepository,
    CredentialRepository,
    CreditEventRepository,
    GrowthRepository,
    ModelProbeRepository,
    UserRepository,
)
from ..engine.executor import Executor
from ..engine.limits import LimitGuard
from ..key_quota import KeyQuota, KeyQuotaGuard, load_key_quota
from ..request_context import set_api_key_id
from ..runtime_settings import RuntimeSettings
from ..stats.live import LiveEventHub
from ..stats.query import StatsQuery
from ..webapp.log_buffer import LogBuffer

# 首登强制改密（must_change_password=1）时放行的端点，精确白名单。
#
# 刻意不用 `/api/auth/*` 通配：`/api/auth/upstream/start|poll|cancel` 正是
# 「建上游凭证」的写操作，恰恰是最该在改密前拦住的。这里只放行三件必需的事：
# 看会话（前端据此判断）、改密本身、登出。
PASSWORD_CHANGE_ALLOWLIST = frozenset({
    ("GET", "/api/auth/session"),
    ("POST", "/api/auth/password"),
    ("POST", "/api/auth/logout"),
})

logger = logging.getLogger(__name__)


@dataclass
class Services:
    """路由层共享依赖（只读仓储/引擎 + 能力方法）。

    运行时可变状态（TRAE 回调登录的 pending_callbacks、
    pending_probes）仍挂 app.state——既有测试直接断言这些字段，
    统一迁移会扩大改动面而无实际收益。
    """

    settings: Settings | RuntimeSettings
    credentials: CredentialRepository
    growth_events: GrowthRepository
    credit_events: CreditEventRepository
    api_keys: ApiKeyRepository
    executor: Executor
    registry: dict[str, Any]
    users: Any                                  # DbUserStore
    stats_query: StatsQuery
    login_throttle: LoginThrottle
    # 逐请求事件广播（SSE 实时流）：`GET /api/stats/events/stream` 订阅它，
    # `StatsCollector.record` 落库后 publish。进程内、单进程部署。
    live_events: LiveEventHub
    # 进程内日志环形缓冲（E3）：`GET /api/logs[/stream]` 的只读数据源。
    # 与 root logger 上的 handler 共用同一实例；`log_buffer_lines=0` 即关闭。
    log_buffer: LogBuffer
    upstream_auth: dict[str, Any]
    model_aliases: dict[str, dict[str, str]]
    schedule_probe: Callable[[str], None]
    # 用户仓储：用户管理端点直接读写行，不经过 DbUserStore（后者只做鉴权读）。
    user_repo: UserRepository
    # 审计流水：登录/账号变动/凭证写操作。
    audit: AuditRepository
    # 运维告警事件：后台评估命中后落库，/api/alerts 回看。
    alerts: AlertRepository
    # 凭证级限额护栏配置（/api/credentials/{id}/limits）。
    limits: LimitsRepository
    # 主动探活结论（`model_probes`）：管理台展示 + 人工接管标记；`failed` 时
    # `merged_entries` 会把该 (渠道, 模型) 从列表里抽掉（不看负缓存是否还在，
    # 恢复靠复探成功）。与调度侧的 `credential_model_cooldowns` 严格分开
    # （见 src/tasks/model_probe.py）。
    model_probes: ModelProbeRepository
    # Key 级配额：准入判定（当日用量注入 KeyUsageRepository）+ 自助用量共用。
    # 进程内滑动窗口/在途计数在 guard 里，重启清零（单进程部署语义）。
    key_quota: KeyQuotaGuard
    # 用户级配额：只用于管理台 Playground 的会话直连（`users.quota`），用量按
    # **用户名**聚合（该用户经由任何入口的流量都算）。独立 guard 实例 =
    # 独立的滑动窗口/在途计数，与 Key 的窗口互不干扰。
    user_quota: KeyQuotaGuard
    # 按 API Key 聚合用量（Key 配额判定与 GET /v1/usage 的数据源）。
    key_usage: KeyUsageRepository
    # 同一个限流护栏实例（executor 也持它）：管理台改完限额后 invalidate()
    # 让新值立即生效，不必等选号侧的快照 TTL。
    limit_guard: LimitGuard
    # 模型列表缓存：provider_id → {小写模型名: Model}（含元数据）。
    # list_models 成功时更新，某上游拉取失败时用缓存兜底（v1/models 稳定返回）。
    # **存未过滤的原始表**：MODEL_BLOCKLIST 是可热更项，过滤在每个出口现做，
    # 否则改完黑名单要等 TTL（300s）才生效、被滤模型还会从兜底缓存复活。
    # executor 也持有**同一引用**（ExecutorDeps.model_list_cache）：聊天路径的
    # 倍率查询（免费优先调度档）从这里读 credit_rate，目录刷新后立即可见。
    model_list_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 上次**尝试**拉取时间（monotonic，成功或失败都刷新）：TTL 内的请求不再打
    # 上游——有缓存用缓存，无缓存则跳过该渠道（负缓存）。
    model_list_fetched_at: dict[str, float] = field(default_factory=dict)
    # 拉取连续失败次数：`_needs_refresh` 按 2^(n-1)×TTL 拉长重试间隔（封顶见
    # `MODEL_LIST_BACKOFF_MAX_SECONDS`），一次成功即清零。上游被长期封禁时
    # 避免每 5 分钟重探 + 刷一条同样内容的 WARNING。
    model_list_failures: dict[str, int] = field(default_factory=dict)
    # stale-while-revalidate：正在后台刷新的渠道（`_schedule_refresh` 去重用，
    # 同一渠道同一时刻只跑一个刷新任务）；任务句柄与 all-tasks 列表供 lifespan
    # 关闭时取消，避免把 in-flight 的上游请求带出事件循环。
    model_refreshing: set[str] = field(default_factory=set)
    model_refresh_tasks: set[Any] = field(default_factory=set)
    pending_model_refreshes: list[Any] = field(default_factory=list)


def get_services(request: Request) -> Services:
    return request.app.state.services


def request_ip(request: Request, settings: Settings | RuntimeSettings) -> str:
    """来源 IP 统一入口：受信反代（trust_proxy）取 XFF 最后条目，否则取对端。

    限流与审计必须走这里与 api_key_user 同源：反代部署下拿裸对端地址，
    全站请求会共享同一个 IP 限流桶（互相锁死），审计里也全是代理 IP。
    """
    return client_ip(request.client.host if request.client else None,
                     request.headers.get("x-forwarded-for"),
                     trust_proxy=bool(settings.trust_proxy))


async def principal_from_request(request: Request) -> Principal:
    """会话 Cookie → Principal（管理台内部端点）。

    四道校验，缺一不可：
    1. 签名与过期（`verify_session_token`）。
    2. 用户仍存在**且未禁用**—— cookie 无状态，删除/禁用后不查库会让旧会话
       继续可用到过期为止（权限撤销漏洞）。
    3. 会话 epoch 与 DB 一致——改密/禁用/改角色后旧 Cookie 立即失效。
       缺 `ep` 的老 Cookie 按 0 处理（升级平滑），因此引导期签发的 Cookie
       仍然有效。
    4. 角色从 DB 现读，不进 Cookie——降级立即生效。
    """
    services = get_services(request)
    token = request.cookies.get(SESSION_COOKIE, "")
    verified = verify_session_token(token, services.settings.app_secret)
    if verified is None:
        raise UnauthorizedError("session missing or expired")
    username, epoch = verified
    if not services.users.is_active(username):
        raise UnauthorizedError("session missing or expired")
    if services.users.session_epoch(username) != epoch:
        raise UnauthorizedError("session missing or expired")
    role = services.users.role_of(username)
    principal = Principal(username=username, is_admin=role == ROLE_ADMIN, role=role)
    if services.users.must_change_password(username):
        _enforce_password_change(request)
    return principal


def _enforce_password_change(request: Request) -> None:
    """首登强制改密：只放行白名单端点，其余 403。"""
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    if (request.method, path) in PASSWORD_CHANGE_ALLOWLIST:
        return
    raise PasswordChangeRequiredError("password change required")


@dataclass(frozen=True)
class ApiKeyPrincipal:
    """外部 /v1 出口的鉴权结果：归属用户 + 该 Key 的访问策略。

    返回结构体而不是裸用户名，是因为出口需要 `provider_binding` 去收窄
    候选上游、需要 `allowed_models` 去拒绝未授权模型；IP 白名单与到期时间
    在鉴权当场就判掉，不往上传递。
    """

    username: str
    provider_binding: str = ""      # '' = 不限定渠道
    allowed_models: str = ""        # '' = 不限定模型（fnmatch glob，逗号分隔）
    key_id: str = ""                # api_keys.id（Key 级配额与自助用量的归属）
    quota: KeyQuota = field(default_factory=KeyQuota)  # 该 Key 的配额（全关=不受限）


class ServicePaused(Exception):
    """全站暂停（kill switch，`gateway_paused`）：/v1 一律 503。

    管理台走会话鉴权、不经过这里，因此**暂停期间运维仍能进去关掉它**。
    对外只回受控文案，不回显运维写的原因（可能含事故编号等内部信息）；
    原因进服务端日志。
    """

    def __init__(self, reason: str = "") -> None:
        super().__init__("service temporarily paused by operator")
        self.reason = reason


def _ensure_not_paused(services: Services) -> None:
    """暂停期拦掉 /v1：放在鉴权依赖里而不是各端点，避免新端点漏拦。"""
    settings = services.settings
    if not getattr(settings, "gateway_paused", False):
        return
    reason = str(getattr(settings, "gateway_pause_reason", "") or "")
    logger.warning("网关暂停中（kill switch），拒绝 /v1 请求：%s",
                   reason or "(未填原因)")
    raise ServicePaused(reason)


def _api_key_principal(services: Services, request: Request,
                       raw_key: str) -> ApiKeyPrincipal:
    """校验裸 Key 文本并组装 Principal（Bearer 与 x-api-key 两条入口共用）。"""
    _ensure_not_paused(services)
    record = services.api_keys.authenticate(raw_key)
    # 用户被删除或禁用后旧 Key 必须立即失效（同会话 Cookie 的理由）
    if not record or not services.users.is_active(record["username"]):
        raise UnauthorizedError("invalid api key")
    if key_expired(record.get("expires_at")):
        raise UnauthorizedError("api key expired")
    source = request_ip(request, services.settings)
    if not ip_allowed(source, record.get("allowed_ips") or ""):
        raise ForbiddenError("source ip not allowed for this api key")
    principal = ApiKeyPrincipal(username=record["username"],
                                provider_binding=record.get("provider_binding") or "",
                                allowed_models=record.get("allowed_models") or "",
                                key_id=record["id"],
                                quota=load_key_quota(record.get("quota")))
    # 归属权只认鉴权结果（绝不读入站头）：统计写入经 contextvar 现读，
    # 请求结束由 RequestIdMiddleware 复原（流式响应体也在中间件作用域内）。
    set_api_key_id(principal.key_id)
    return principal


async def api_key_user(request: Request) -> ApiKeyPrincipal:
    """Bearer API Key → 归属用户与访问策略（外部 /v1 端点）。"""
    services = get_services(request)
    header = request.headers.get("authorization", "")
    prefix = "Bearer "
    if not header.lower().startswith(prefix.lower()):
        raise UnauthorizedError("missing api key")
    return _api_key_principal(services, request, header[len(prefix):].strip())


async def api_key_user_anthropic(request: Request) -> ApiKeyPrincipal:
    """Anthropic 客户端鉴权：优先 `x-api-key`，其次 `Authorization: Bearer`。

    Anthropic SDK / Claude Code 把 Key 放在 `x-api-key`（`ANTHROPIC_API_KEY`）
    或 `Authorization: Bearer`（`ANTHROPIC_AUTH_TOKEN`）两处，两者都要认，
    否则「Base URL 一改就 401」。
    """
    key = request.headers.get("x-api-key")
    if key and key.strip():
        return _api_key_principal(get_services(request), request, key.strip())
    return await api_key_user(request)


async def csrf_protected(request: Request) -> None:
    """写操作 CSRF 校验（仅对带会话 cookie 的请求生效）。"""
    check_csrf(request)


async def origin_protected(request: Request) -> None:
    """无会话 Cookie 的写操作（登录）CSRF 校验：只查 Origin/Referer 同源。

    登录发生在「还没有会话 Cookie」时，`csrf_protected` 会因无 Cookie 跳过，
    于是跨站表单能强制把受害者登录进攻击者账号（login CSRF）。这里不依赖
    Cookie，直接比对 Origin/Referer 与 Host 是否同源。
    """
    check_origin(request)


async def read_json_body(request: Request) -> Any:
    """读取并解析 JSON 请求体；非法 JSON 归一为 400 而不是 500。

    FastAPI 把 `payload: dict` 声明在签名里时，解析失败会返回 422；
    但本项目的 /v1 与 playground 走原始 Request（需要透传任意字段），
    裸调 `request.json()` 会让 json.JSONDecodeError 冒泡成 500。

    两道 JSON 层的数值把关：

    - `parse_constant` 拒绝 `NaN` / `Infinity` / `-Infinity`（JSON 标准不允许，
      但 Python json 默认接受）：这些值会经本服务重新序列化透传给上游，把
      本应 400 的输入变成上游 4xx，污染凭证健康度与统计。
    - `parse_float` 拒绝**溢出成 inf 的合法 JSON 数字**（`1e999`）：字面量拦不住
      它（`json.loads('{"x":1e999}')` → `inf`），而 `httpx` 序列化时抛
      `ValueError: Out of range float values are not JSON compliant` → 变 500。
      逐字段校验（`compat/*/request.py`）只覆盖已知的几个键，chat 出口会把整个
      请求体透传给上游，未知键里的 inf 会一路带到序列化那一步炸掉。

    `parse_float` 拦在**解析层**而非逐字段：三个出口（chat / Anthropic /
    Responses）共用这一个入口，一处生效全生效；逐字段补齐要各写一遍且仍会漏
    掉 chat 出口的未知透传键。
    """
    def _reject_constant(token: str) -> Any:
        raise InvalidRequest(f"JSON value {token} is not allowed")

    def _reject_non_finite(token: str) -> float:
        # Python 的 json 在这里已把 1e999 解析成 inf（不抛 OverflowError）
        value = float(token)
        if not math.isfinite(value):
            raise InvalidRequest(f"JSON number {token} is not finite")
        return value

    try:
        return json.loads(await request.body(), parse_constant=_reject_constant,
                          parse_float=_reject_non_finite)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise InvalidRequest("request body is not valid JSON") from error

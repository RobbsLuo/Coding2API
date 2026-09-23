"""API 依赖注入：共享服务容器 + 鉴权依赖（TECHNICAL §2 deps.py）。

build_app 把数据库仓储、执行引擎、provider 注册表等打包成 Services 挂到
app.state.services；各路由模块通过 create_router(services) 闭包捕获，或
通过 get_services 从 request 取（鉴权依赖用后者，因为路由签名里只有
request 可用）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request

from ..auth.access import client_ip, ip_allowed
from ..auth.csrf import check_csrf
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
from ..db.repo import (
    ApiKeyRepository,
    AuditRepository,
    CredentialRepository,
    CreditEventRepository,
    GrowthRepository,
    UserRepository,
)
from ..engine.executor import Executor
from ..runtime_settings import RuntimeSettings
from ..stats.query import StatsQuery

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


@dataclass
class Services:
    """路由层共享依赖（只读仓储/引擎 + 能力方法）。

    运行时可变状态（TRAE 回调登录的 pending_callback_state / user、
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
    upstream_auth: dict[str, Any]
    model_aliases: dict[str, dict[str, str]]
    schedule_probe: Callable[[str], None]
    # 用户仓储（B5）：用户管理端点直接读写行，不经过 DbUserStore（后者只做鉴权读）。
    user_repo: UserRepository
    # 审计流水（B5）：登录/账号变动/凭证写操作。
    audit: AuditRepository
    # 模型列表缓存：provider_id → {小写模型名: Model}（含元数据）。
    # list_models 成功时更新，某上游拉取失败时用缓存兜底（v1/models 稳定返回）。
    # **存未过滤的原始表**：MODEL_BLOCKLIST 是可热更项，过滤在每个出口现做，
    # 否则改完黑名单要等 TTL（300s）才生效、被滤模型还会从兜底缓存复活。
    model_list_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 上次成功拉取时间（monotonic）：TTL 内的请求直接用缓存，
    # 避免客户端频繁调 /v1/models 时上游被打成大。
    model_list_fetched_at: dict[str, float] = field(default_factory=dict)


def get_services(request: Request) -> Services:
    return request.app.state.services


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
    """外部 /v1 出口的鉴权结果：归属用户 + 该 Key 的访问策略（B3.5）。

    返回结构体而不是裸用户名，是因为出口需要 `provider_binding` 去收窄
    候选上游；IP 白名单在鉴权当场就判掉，不往上传递。
    """

    username: str
    key_id: str
    provider_binding: str = ""      # '' = 不限定渠道


async def api_key_user(request: Request) -> ApiKeyPrincipal:
    """Bearer API Key → 归属用户与访问策略（外部 /v1 端点）。"""
    services = get_services(request)
    header = request.headers.get("authorization", "")
    prefix = "Bearer "
    if not header.lower().startswith(prefix.lower()):
        raise UnauthorizedError("missing api key")
    record = services.api_keys.authenticate(header[len(prefix):].strip())
    # 用户被删除或禁用后旧 Key 必须立即失效（同会话 Cookie 的理由）
    if not record or not services.users.is_active(record["username"]):
        raise UnauthorizedError("invalid api key")
    source = client_ip(request.client.host if request.client else None,
                       request.headers.get("x-forwarded-for"),
                       trust_proxy=bool(services.settings.trust_proxy))
    if not ip_allowed(source, record.get("allowed_ips") or ""):
        raise ForbiddenError("source ip not allowed for this api key")
    return ApiKeyPrincipal(username=record["username"], key_id=record["id"],
                           provider_binding=record.get("provider_binding") or "")


async def csrf_protected(request: Request) -> None:
    """写操作 CSRF 校验（仅对带会话 cookie 的请求生效）。"""
    check_csrf(request)


async def read_json_body(request: Request) -> Any:
    """读取并解析 JSON 请求体；非法 JSON 归一为 400 而不是 500。

    FastAPI 把 `payload: dict` 声明在签名里时，解析失败会返回 422；
    但本项目的 /v1 与 playground 走原始 Request（需要透传任意字段），
    裸调 `request.json()` 会让 json.JSONDecodeError 冒泡成 500。
    """
    try:
        return await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise InvalidRequest("request body is not valid JSON") from error

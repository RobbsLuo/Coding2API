"""API 依赖注入：共享服务容器 + 鉴权依赖（TECHNICAL §2 deps.py）。

build_app 把数据库仓储、执行引擎、provider 注册表等打包成 Services 挂到
app.state.services；各路由模块通过 create_router(services) 闭包捕获，或
通过 get_services 从 request 取（鉴权依赖用后者，因为路由签名里只有
request 可用）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request

from ..auth.csrf import check_csrf
from ..auth.rbac import Principal, UnauthorizedError
from ..auth.session import verify_session_token
from ..auth.throttle import LoginThrottle
from ..config import Settings
from ..db.repo import ApiKeyRepository, CredentialRepository
from ..engine.executor import Executor
from ..stats.query import StatsQuery

SESSION_COOKIE = "coding2api_session"


@dataclass
class Services:
    """路由层共享依赖（只读仓储/引擎 + 能力方法）。

    运行时可变状态（TRAE 回调登录的 pending_callback_state / user、
    pending_probes）仍挂 app.state——既有测试直接断言这些字段，
    统一迁移会扩大改动面而无实际收益。
    """

    settings: Settings
    credentials: CredentialRepository
    api_keys: ApiKeyRepository
    executor: Executor
    registry: dict[str, Any]
    users: Any                                  # UsersFileStore
    stats_query: StatsQuery
    login_throttle: LoginThrottle
    upstream_auth: dict[str, Any]
    model_aliases: dict[str, dict[str, str]]
    schedule_probe: Callable[[str], None]
    # 模型列表缓存：provider_id → {小写模型名: Model}（含元数据）。
    # list_models 成功时更新，某上游拉取失败时用缓存兜底（v1/models 稳定返回）。
    model_list_cache: dict[str, dict[str, Any]] = field(default_factory=dict)


def get_services(request: Request) -> Services:
    return request.app.state.services


async def principal_from_request(request: Request) -> Principal:
    """会话 Cookie → Principal（管理台内部端点）。"""
    services = get_services(request)
    token = request.cookies.get(SESSION_COOKIE, "")
    username = verify_session_token(token, services.settings.app_secret)
    if not username:
        raise UnauthorizedError("session missing or expired")
    return Principal(username=username,
                     is_admin=services.settings.is_admin(username))


async def api_key_user(request: Request) -> str:
    """Bearer API Key → username（外部 /v1 端点）。"""
    services = get_services(request)
    header = request.headers.get("authorization", "")
    prefix = "Bearer "
    if not header.lower().startswith(prefix.lower()):
        raise UnauthorizedError("missing api key")
    username = services.api_keys.verify(header[len(prefix):].strip())
    if not username:
        raise UnauthorizedError("invalid api key")
    return username


async def csrf_protected(request: Request) -> None:
    """写操作 CSRF 校验（仅对带会话 cookie 的请求生效）。"""
    check_csrf(request)

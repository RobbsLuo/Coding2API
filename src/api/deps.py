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

from ..auth.csrf import check_csrf
from ..auth.rbac import Principal, UnauthorizedError
from ..auth.session import verify_session_token
from ..auth.throttle import LoginThrottle
from ..compat.openai.request import InvalidRequest
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
    # 上次成功拉取时间（monotonic）：TTL 内的请求直接用缓存，
    # 避免客户端频繁调 /v1/models 时上游被打成大。
    model_list_fetched_at: dict[str, float] = field(default_factory=dict)


def get_services(request: Request) -> Services:
    return request.app.state.services


async def principal_from_request(request: Request) -> Principal:
    """会话 Cookie → Principal（管理台内部端点）。

    除签名与过期外还要确认用户仍存在于 users.txt：删除用户后旧会话
    最长还能再用 12 小时，属于权限撤销漏洞（cookie 是无状态签名）。
    """
    services = get_services(request)
    token = request.cookies.get(SESSION_COOKIE, "")
    username = verify_session_token(token, services.settings.app_secret)
    if not username or not services.users.has(username):
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
    # 用户被删除后旧 Key 永久有效，必须同样校验
    if not username or not services.users.has(username):
        raise UnauthorizedError("invalid api key")
    return username


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

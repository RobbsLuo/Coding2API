"""LobsterAI 测试共享替身与装配助手。"""

from __future__ import annotations

import httpx

from src.provider.lobsterai.client import LobsteraiClient
from src.provider.lobsterai.credential import LobsteraiCredential
from tests.helpers.http import provider_client

__all__ = ["MOMENT", "client", "cred", "envelope", "version_response"]

#: 固定「当前时刻」：2026-10-26T04:30:00Z（北京时间同日 12:30）。
MOMENT = 1792991400


def cred(**over) -> LobsteraiCredential:
    """构造 LobsterAI 凭证（默认身份三元组齐全、access token 未过期）。"""
    base = {
        "uid": "uid-1",
        "access_token": "AT",
        "refresh_token": "RT",
        "expires_at": MOMENT + 86400,
        "uuid": "uuid-1",
        "first_keyfrom": "1700000000000",
        "latest_keyfrom": "1700000000000",
        "user_id": "user-1",
        "nickname": "alice",
    }
    base.update(over)
    return LobsteraiCredential.from_dict(base)


def client(handler, **kw) -> LobsteraiClient:
    """用 `handler` 造 LobsteraiClient（默认三个测试主机与固定时钟）。

    客户端每次请求前都会先查版本号（签到按它门禁），故这里**代理**一层
    handler：版本域的请求一律回固定版本，测试只需关心自己断言的端点。
    """
    kw.setdefault("api_base", "https://api.test")
    kw.setdefault("portal_base", "https://portal.test")
    kw.setdefault("version_api", "https://version.test/update")
    kw.setdefault("now", lambda: MOMENT)

    def routed(request: httpx.Request) -> httpx.Response:
        if request.url.host == "version.test":
            return httpx.Response(200, json=version_response())
        return handler(request)

    return provider_client(LobsteraiClient, routed, **kw)


def envelope(data, code: int = 0, message: str = "success") -> dict:
    """构造统一信封响应体。"""
    return {"code": code, "msg": message, "data": data}


def version_response(version: str = "2026.10.23") -> dict:
    """版本接口响应（载荷在 `data.value.version`，不是统一信封）。"""
    return {"data": {"value": {"version": version, "date": "2026-10-23"}}}

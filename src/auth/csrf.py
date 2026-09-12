"""CSRF 纵深防护（PROPOSAL §8）：写操作必须带自定义头或同源 Origin/Referer。

分层说明：
- SameSite=Lax 已挡掉跨站请求自动携带的会话 cookie（第一道防线）。
- 本模块是第二道：跨站表单/简单请求无法携带自定义头；跨站 fetch
  必然带不同源 Origin。同源浏览器 POST 会带同源 Origin/Referer。
- 无 Origin/Referer/自定义头的请求视为非浏览器客户端（curl、SDK、
  内部测试），放行——浏览器不会发裸写请求到跨站。

只对带会话 cookie 的请求生效：/v1 的 API Key 认证不走 cookie，
跨站无法自动带 Bearer 头，不需要 CSRF 校验。
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Request

from .rbac import Principal  # noqa: F401 - 依赖签名类型引用

SESSION_COOKIE = "coding2api_session"

# 前端统一携带的自定义头（web/src/api/client.ts）
X_REQUESTED_WITH = "x-requested-with"


class CsrfRejectedError(Exception):
    """CSRF 校验失败（跨站写请求）。"""


def _same_host(reference: str, request: Request) -> bool:
    """reference（origin/referer URL）与请求 Host 的主机名+端口相同。"""
    try:
        parts = urlsplit(reference)
    except ValueError:
        return False
    if parts.hostname is None:
        return False
    host_header = request.headers.get("host", "")
    request_name, _, request_port = host_header.partition(":")
    if request_name.startswith("["):  # IPv6 字面量
        request_name = request_name.strip("[]")
    try:
        reference_port = parts.port
    except ValueError:
        reference_port = None
    if parts.hostname.lower().strip("[]") != request_name.strip().lower().strip("[]"):
        return False
    # 默认端口（http=80 / https=443）与缺省端口视为一致
    default_port = 443 if parts.scheme == "https" else 80
    request_port_int = None
    if request_port:
        try:
            request_port_int = int(request_port)
        except ValueError:
            return False
    return (reference_port or default_port) == (request_port_int or default_port)


def check_csrf(request: Request) -> None:
    """管理台写操作（session 认证）调用；无会话 cookie 的请求直接跳过。"""
    if SESSION_COOKIE not in request.cookies:
        return
    if request.headers.get(X_REQUESTED_WITH, "").lower() == "xmlhttprequest":
        return
    origin = request.headers.get("origin")
    if origin:
        if _same_host(origin, request):
            return
        raise CsrfRejectedError("cross-origin write rejected")
    referer = request.headers.get("referer")
    if referer:
        if _same_host(referer, request):
            return
        raise CsrfRejectedError("cross-origin write rejected")
    # 无 Origin/Referer：非浏览器客户端，SameSite=Lax 兜底

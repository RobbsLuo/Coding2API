"""CSRF 纵深防护（PROPOSAL §6）：写操作必须带自定义头或同源 Origin/Referer。

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
    if host_header.startswith("["):
        # IPv6 字面量：[::1]:8000 → 主机 ::1，端口 8000（partition(":") 会在
        # 第一个冒号处切开，必须先取 "]" 前的部分）
        request_name, _, request_port = host_header.partition("]")
        request_name = request_name.lstrip("[")
        request_port = request_port.lstrip(":")
    else:
        request_name, _, request_port = host_header.partition(":")
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


def check_origin(request: Request) -> None:
    """只做 Origin/Referer 同源校验（不依赖会话 Cookie）。

    `check_csrf` 对无 Cookie 的请求直接放行（/v1 走 API Key，不涉 Cookie）。
    但**登录**恰恰发生在无 Cookie 时，若也放行则跨站表单可把受害者登录到
    攻击者账号（login CSRF）。浏览器发起的跨站表单 POST 必带 Origin，因此
    登录单独走这条：有 Origin/Referer 就查同源，两者都缺（curl/SDK）才放行。
    """
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
    # 无 Origin/Referer：非浏览器客户端（curl、SDK、内部测试），放行


def check_csrf(request: Request) -> None:
    """管理台写操作（session 认证）调用；无会话 cookie 的请求直接跳过。

    **不再豁免 `X-Requested-With`**：该头是「浏览器无法跨源设置」的推论，
    一旦将来引入允许非同源自定义头的 CORS 配置，这条豁免立刻变成 CSRF
    绕过面。前端所有请求都与本服务同源（Vite 代理 / 同域反代），浏览器
    必然带同源 Origin/Referer，`check_origin` 的口径已经够用。
    """
    if SESSION_COOKIE not in request.cookies:
        return
    check_origin(request)

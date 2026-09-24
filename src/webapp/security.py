"""Host 白名单与安全响应头（PROPOSAL §8）。

拆出来是为了让 build_app 只负责装配；这两个策略本身与业务无关，
改动理由也不同（安全策略调整 vs 服务组装）。
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from ..compat.openai.errors import error_payload
from ..config import Settings


def host_allowed(host_header: str, settings: Settings) -> bool:
    """Host 白名单（防 DNS rebinding）。

    ALLOWED_HOSTS 配置优先（逗号分隔）；未配置时放行本地回环、PUBLIC_BASE_URL
    的主机与 testserver（FastAPI TestClient 默认 Host，仅测试场景）。
    只比较主机名，忽略端口。
    """
    hostname = host_header.split(":", 1)[0].strip().lower().strip("[]")
    if settings.allowed_hosts:
        allowed = {h.split(":", 1)[0].strip().lower().strip("[]")
                   for h in settings.allowed_hosts.split(",") if h.strip()}
        return hostname in allowed
    allowed = {"localhost", "127.0.0.1", "::1", "testserver"}
    base = settings.public_base_url
    if base.startswith(("http://", "https://")):
        base = base.split("://", 1)[1]
    path_host = base.split("/", 1)[0].split(":", 1)[0].strip().lower().strip("[]")
    if path_host:
        allowed.add(path_host)
    return hostname in allowed


async def security_middleware(request: Request, call_next):
    """Host 校验 + 安全响应头。请求体上限由 BodySizeLimitMiddleware 处理。

    注册方式（app.middleware("http") 装饰器）留在 build_app，因为那是
    FastAPI 的接线细节；返回什么由这里决定。
    """
    host_header = request.headers.get("host", "")
    if not host_allowed(host_header, request.app.state.settings):
        return JSONResponse(status_code=400,
                            content=error_payload("invalid host header",
                                                  "invalid_request", 400))
    # 纯读 content-length 会被 chunked 请求绕过，因此上限在 ASGI 层做
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    if request.app.state.settings.public_base_url.startswith("https://"):
        # 仅 https 部署下发 HSTS：明文部署发了无意义，还会预锁本地 http 访问
        response.headers.setdefault("Strict-Transport-Security",
                                    "max-age=31536000; includeSubDomains")
    return response

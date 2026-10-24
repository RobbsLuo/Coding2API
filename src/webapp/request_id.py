"""请求 ID 中间件：为每个请求确定一个 ID，写响应头 + 放进 contextvar（PROPOSAL §6）。

注册顺序：**必须挂在最外层**（`build_app` 里最后 `add_middleware`，见那里的注释）。
理由与 `BodySizeLimitMiddleware` 相同——内层中间件（如请求体超限的 413、Host
校验失败的 400）自己造响应时，外层还没机会加头；挂在最外层才能保证**任何**响应
都带 `X-Request-ID`，包括被拒的请求（排查「谁被打回来了」恰恰最需要它）。

用纯 ASGI 而不是 `BaseHTTPMiddleware`：后者每个请求多起一层 anyio 任务，对
纯头注入是纯开销；且本中间件要在 `http.response.start` 上就地改 headers，
纯 ASGI 直接拿得到 message。
"""

from __future__ import annotations

from ..request_context import (
    REQUEST_ID_HEADER,
    resolve_request_id,
    session_id_from_headers,
    set_api_key_id,
    set_request_id,
    set_session_id,
)


def _header(scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


class RequestIdMiddleware:
    """确定请求 ID：入站合法则沿用，否则自生成；写响应头并放进 contextvar。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = resolve_request_id(_header(scope, b"x-request-id"))
        set_request_id(request_id)
        # 会话标识（E1）：与请求 ID 同一中间件设置即可——它只被会话粘性读，
        # 且必须在校验之前落进上下文（executor 早于任何业务层）。
        set_session_id(session_id_from_headers(scope.get("headers", ())))
        header = (REQUEST_ID_HEADER.lower().encode("latin-1"),
                  request_id.encode("latin-1"))

        async def send_with_header(message):
            if message["type"] == "http.response.start":
                # 就地追加：下游（含被拒请求）可能已经写了头，不能覆盖别人的。
                message.setdefault("headers", []).append(header)
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            # 复原 Key 归属与会话标识：contextvar 由鉴权依赖/本中间件设置，而
            # 流式响应体在依赖的 finally **之后**才发送，只靠依赖复原会让
            # 「响应体期间」的后台写入串到下一个请求。中间件包住整个 ASGI
            # 调用，最可靠。
            set_api_key_id(None)
            set_session_id(None)
            # request_id 也要复原：超时/后台任务可能比请求活得久，残留旧 ID 会
            # 让后续写入落进别人的 request_id（归因的「换号救回」按它关联）。
            set_request_id(None)

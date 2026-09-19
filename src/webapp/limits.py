"""请求体上限（纯 ASGI 中间件，PROPOSAL §8）。

登录接口 8KB（PBKDF2 是 CPU 密集操作，超大 body 无意义），其余 16MB
（聊天请求可能带图片 base64）。必须挂在最外层：放在内层时下游会先读完
body，限制就失去意义。
"""

from __future__ import annotations

# 请求体上限：登录 8KB（PBKDF2 是 CPU 密集操作，超大 body 无意义）；
# 其余 16MB（聊天请求可能带图片 base64）。
LOGIN_BODY_LIMIT = 8 * 1024
DEFAULT_BODY_LIMIT = 16 * 1024 * 1024


def _body_limit(path: str) -> int:
    # rstrip 处理尾斜杠：/api/auth/login/ 同样按登录上限（8KB），否则会先被
    # 按 16MB 读完再 307 重定向，绕过登录限流一次
    return LOGIN_BODY_LIMIT if path.rstrip("/") == "/api/auth/login" else DEFAULT_BODY_LIMIT


async def _send_too_large(send) -> None:
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body",
                "body": b'{"error":{"message":"request body too large",'
                        b'"type":"api_error","code":"invalid_request","status":413}}'})


class BodySizeLimitMiddleware:
    """请求体上限（纯 ASGI）：content-length 与实际分块计数双管。

    只看 content-length 头会被 `Transfer-Encoding: chunked` 绕过——
    分块请求根本不带这个头。这里在 receive 层累计字节数，
    超限立即换成 413 响应并截断下游消费。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = _body_limit(scope.get("path", ""))
        # content-length 已超限时直接拒，不必读 body
        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                try:
                    if int(value) > limit:
                        await _send_too_large(send)
                        return
                except ValueError:
                    break

        received = 0
        exceeded = False
        replaced = False

        async def limited_receive():
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    # 截断：让下游读到 EOF，避免继续消费攻击流量
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        async def guarded_send(message):
            """超限后丢弃下游的全部响应，只发出我们自己的 413。"""
            nonlocal replaced
            if not exceeded:
                await send(message)
                return
            if message["type"] != "http.response.start":
                return                      # 丢弃下游 body
            if not replaced:
                replaced = True
                await _send_too_large(send)

        await self.app(scope, limited_receive, guarded_send)

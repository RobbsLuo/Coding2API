"""上游尝试响应头中间件（纯 ASGI）。

`X-Upstream-Attempts` / `X-Upstream-Degraded` 让调用方一眼看出「这单换过几个
号、是否走了 fallback 组」，不必翻服务端日志。**默认关闭**（热更项
`EXPOSE_UPSTREAM_HEADERS`）：这两个头会泄露账号池规模、成本与路由指纹，
对任何拿到 API Key 的调用方都可见——它们本来只是本仓排障用的信息。

关闭时不仅不写，还要**主动剥离**同名响应头：上游/中间盒理论上可能把这些
头回传给客户端，不剥离就会出现「关了开关仍有值」的假象（且值不可信）。

**流式的时点**：响应头在首帧就绪时发出（`respond_with_stream_start` 要抢在
首帧前决定 200/400），所以流式响应的 `X-Upstream-Attempts` 只包含**发头前**
已发生的尝试；发头之后才发生的轮换无法回头改头（HTTP 响应头不可追加）。
非流式响应则是在聚合完成后发头，值是本请求的最终尝试次数。

注册位置：在 `RequestIdMiddleware` 内侧（响应头已带请求 ID）、在
`BodySizeLimitMiddleware` 外侧（413 也能被规范成「无上游尝试信息」）。

未处理 500：Starlette 的 `ServerErrorMiddleware` 在**所有**用户中间件之外，
异常逃逸时它的响应不会经过本中间件。所以这里就地兜住未处理异常、按统一
错误信封回 500，并照样带上尝试头；否则「未处理 500」恰好是唯一没有这两个
头的路径。已开始发送响应（流式）后的异常仍原样上抛——此时改不了头。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from ..compat.openai.errors import error_payload
from ..engine.executor import (
    begin_upstream_trace,
    clear_upstream_trace,
)

logger = logging.getLogger(__name__)

ATTEMPTS_HEADER = b"x-upstream-attempts"
DEGRADED_HEADER = b"x-upstream-degraded"
_MANAGED_HEADERS = (ATTEMPTS_HEADER, DEGRADED_HEADER)


def _strip_managed(headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    """去掉同名头（无论本次是否要重写）：防上游回传/重复值。"""
    return [(key, value) for key, value in headers
            if key.lower() not in _MANAGED_HEADERS]


class UpstreamHeadersMiddleware:
    """把本次请求的尝试次数 / 是否降级写进响应头（开关热更、关闭即剥离）。"""

    def __init__(self, app, *, enabled: Callable[[], bool] | None = None) -> None:
        self.app = app
        # 取值器而不是快照：热更开关改完下一请求立即生效
        self._enabled = enabled or (lambda: False)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        trace = begin_upstream_trace()
        enabled = bool(self._enabled())
        started = False

        async def send_with_meta(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = _strip_managed(list(message.get("headers", [])))
                if enabled:
                    headers.append((ATTEMPTS_HEADER,
                                    str(trace.attempts).encode("latin-1")))
                    headers.append((DEGRADED_HEADER,
                                    b"true" if trace.degraded else b"false"))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_meta)
        except Exception:  # noqa: BLE001 - 未处理异常：就地回统一 500 信封
            if started:
                # 流式已发头/部分正文：改不了响应头，保持既有「上抛由服务器
                # 断开」的行为（错误帧由 stream_guarded 收尾，走到这里极少）。
                raise
            logger.exception("未处理异常：按 internal_error 回 500")
            body = json.dumps(error_payload("internal server error",
                                            "internal_error", 500),
                              ensure_ascii=False).encode("utf-8")
            await send_with_meta({
                "type": "http.response.start",
                "status": 500,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({"type": "http.response.body", "body": body})
        finally:
            clear_upstream_trace()
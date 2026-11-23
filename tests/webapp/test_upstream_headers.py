"""上游尝试响应头中间件：四条路径 + 关闭时剥离。"""

from __future__ import annotations

import pytest

from src.engine.executor import get_upstream_trace
from src.webapp.upstream_headers import (
    ATTEMPTS_HEADER,
    DEGRADED_HEADER,
    UpstreamHeadersMiddleware,
)


def _headers(message) -> dict[bytes, bytes]:
    return {key.lower(): value for key, value in message.get("headers", [])}


async def _run(app, *, enabled: bool):
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    middleware = UpstreamHeadersMiddleware(app, enabled=lambda: enabled)
    await middleware({"type": "http", "path": "/v1/chat/completions"},
                     receive, send)
    return sent


def _writer(status: int, *, chunks: int = 1, spoof: bool = False):
    async def app(_scope, _receive, send):
        trace = get_upstream_trace()
        assert trace is not None
        trace.attempts = 2
        trace.degraded = True
        headers = []
        if spoof:
            headers = [(b"X-Upstream-Attempts", b"99"),
                       (b"x-upstream-degraded", b"true")]
        await send({"type": "http.response.start", "status": status,
                    "headers": headers})
        for _ in range(chunks):
            await send({"type": "http.response.body", "body": b"x",
                        "more_body": chunks > 1})
    return app


@pytest.mark.parametrize(("status", "chunks"), [(200, 1), (200, 3), (503, 1)])
async def test_enabled_writes_headers_on_all_response_paths(status, chunks):
    """正常 / 流式 / 错误路径：开关开 → 两个头都在，且值是本次轨迹。"""
    sent = await _run(_writer(status, chunks=chunks), enabled=True)
    headers = _headers(sent[0])
    assert headers[ATTEMPTS_HEADER] == b"2"
    assert headers[DEGRADED_HEADER] == b"true"


async def test_enabled_writes_headers_on_unhandled_500():
    """未处理异常：中间件就地回 500 信封，并照样带尝试头。

    Starlette 的 ServerErrorMiddleware 在用户中间件之外，异常逃逸时它的
    响应不会经过本层；不在这里兜住就没有这两个头。
    """
    async def app(_scope, _receive, _send):
        trace = get_upstream_trace()
        assert trace is not None
        trace.attempts = 1
        raise RuntimeError("boom")

    sent = await _run(app, enabled=True)
    assert sent[0]["status"] == 500
    headers = _headers(sent[0])
    assert headers[ATTEMPTS_HEADER] == b"1"
    assert headers[DEGRADED_HEADER] == b"false"
    assert b"internal_error" in sent[1]["body"]


async def test_disabled_strips_spoofed_headers_on_all_paths():
    """开关关：不写，且主动剥离同名头（防上游/中间盒回传）。"""
    send = await _run(_writer(200, spoof=True), enabled=False)
    headers = _headers(send[0])
    assert ATTEMPTS_HEADER not in headers
    assert DEGRADED_HEADER not in headers


async def test_disabled_rethrows_after_response_started():
    """已开始发送响应后异常照旧上抛（此时改不了头，也不该吞错）。"""
    sent = []

    async def send(message):
        sent.append(message)

    async def app(_scope, _receive, _send):
        await _send({"type": "http.response.start", "status": 200,
                     "headers": [(b"x-upstream-attempts", b"7")]})
        raise RuntimeError("after start")

    middleware = UpstreamHeadersMiddleware(app, enabled=lambda: False)
    with pytest.raises(RuntimeError):
        await middleware({"type": "http"}, _empty_receive, send)
    assert sent[0]["status"] == 200


async def _empty_receive():
    return {"type": "http.request", "body": b"", "more_body": False}


async def _noop_send(_message):
    return None


async def test_non_http_scope_passes_through():
    """lifespan / websocket 不介入（否则会为它们创建请求级轨迹）。"""
    called = []

    async def app(scope, _receive, _send):
        called.append(scope["type"])

    middleware = UpstreamHeadersMiddleware(app, enabled=lambda: True)
    await middleware({"type": "lifespan"}, _empty_receive, _noop_send)
    assert called == ["lifespan"]


def test_header_names_are_lowercase_ascii():
    assert ATTEMPTS_HEADER.lower() == ATTEMPTS_HEADER
    assert DEGRADED_HEADER.lower() == DEGRADED_HEADER


async def test_trace_cleared_after_request():
    await _run(_writer(200), enabled=True)
    assert get_upstream_trace() is None


async def test_disabled_does_not_require_trace_values():
    """关闭时即使轨迹为空也不崩（中间件仍可清理上下文）。"""
    async def app(_scope, _receive, send):
        await send({"type": "http.response.start", "status": 204})

    sent = await _run(app, enabled=False)
    assert sent[0]["status"] == 204
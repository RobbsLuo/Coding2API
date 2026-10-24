"""请求 ID：生成/校验/contextvar、中间件响应头、落库与统计查询透出。"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.request_context import (
    REQUEST_ID_PATTERN,
    get_request_id,
    new_request_id,
    resolve_request_id,
    sanitize_request_id,
    set_request_id,
)
from src.webapp.request_id import RequestIdMiddleware


@pytest.fixture(autouse=True)
def _reset_request_id():
    """contextvar 在同一线程的用例之间会残留：每个用例前后都复原。"""
    set_request_id(None)
    yield
    set_request_id(None)


# ------------------------------------------------------------------ 纯函数

def test_new_request_id_is_32_hex():
    value = new_request_id()
    assert len(value) == 32
    assert REQUEST_ID_PATTERN.match(value)


def test_sanitize_accepts_safe_ids():
    for raw in ("abc", "a-b.c_d:e", "0123456789" * 12 + "12345678"):  # 128 位
        assert sanitize_request_id(raw) == raw


def test_sanitize_rejects_unsafe_ids():
    """客户端可写：换行/空格/引号/超长/空一律拒绝（否则能污染日志与 DB）。"""
    for raw in (None, "", "has space", "line\nbreak", "quote\"x", "slash/x",
                "a" * 129):
        assert sanitize_request_id(raw) is None


def test_resolve_keeps_valid_and_generates_otherwise():
    assert resolve_request_id("client-id") == "client-id"
    generated = resolve_request_id("bad id!")
    assert generated != "bad id!" and REQUEST_ID_PATTERN.match(generated)
    assert REQUEST_ID_PATTERN.match(resolve_request_id(None))


def test_contextvar_defaults_none_and_round_trips():
    # 无请求上下文（后台任务/脚本）时为 None
    assert get_request_id() is None
    set_request_id("rid-1")
    assert get_request_id() == "rid-1"
    set_request_id(None)        # 复原，避免污染同线程的后续用例


# ------------------------------------------------------------------ 中间件

def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/ok")
    async def ok():
        return {"rid": get_request_id()}

    return app


def test_middleware_generates_and_echoes_request_id():
    with TestClient(_app()) as client:
        response = client.get("/ok")
    assert response.status_code == 200
    rid = response.headers["X-Request-ID"]
    assert REQUEST_ID_PATTERN.match(rid)
    assert response.json()["rid"] == rid        # 下游可见（contextvar 已设置）


def test_middleware_keeps_inbound_request_id():
    with TestClient(_app()) as client:
        response = client.get("/ok", headers={"X-Request-ID": "client-rid-1"})
    assert response.headers["X-Request-ID"] == "client-rid-1"
    assert response.json()["rid"] == "client-rid-1"


def test_middleware_discards_invalid_inbound_id():
    with TestClient(_app()) as client:
        response = client.get("/ok", headers={"X-Request-ID": "bad id"})
    rid = response.headers["X-Request-ID"]
    assert rid != "bad id" and REQUEST_ID_PATTERN.match(rid)


def test_middleware_ignores_non_http_scope():
    """非 http scope（如 lifespan）原样透传，不加头也不报错。"""
    import asyncio

    seen: list[str] = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    middleware = RequestIdMiddleware(app)
    asyncio.run(middleware({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


def test_middleware_appends_header_without_dropping_existing():
    """就地追加：下游已写的响应头不能被覆盖。"""
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/headers")
    async def headers():
        from fastapi import Response

        return Response(status_code=201, headers={"X-Custom": "kept"})

    with TestClient(app) as client:
        response = client.get("/headers")
    assert response.headers["X-Custom"] == "kept"
    assert response.headers["X-Request-ID"]


# ------------------------------------------------------- 落库与统计查询

def test_record_persists_current_request_id(db):
    from src.stats.collector import StatsCollector
    from src.stats.query import StatsQuery

    collector, query = StatsCollector(db), StatsQuery(db)
    set_request_id("rid-persisted")
    collector.record(username="u", provider="trae", model="m", ok=True)
    rows = query.events(username="u")["events"]
    assert rows[0]["request_id"] == "rid-persisted"


def test_record_without_request_context_stores_null(db):
    """后台任务 / 脚本写入没有请求上下文 → request_id 为 NULL。"""
    from src.stats.collector import StatsCollector
    from src.stats.query import StatsQuery

    collector, query = StatsCollector(db), StatsQuery(db)
    assert get_request_id() is None
    collector.record(username="u", provider="trae", model="m", ok=True)
    assert query.events(username="u")["events"][0]["request_id"] is None


# --------------------------------------------------- Key 归属 contextvar

def test_middleware_resets_api_key_id_after_request():
    """Key 归属必须随请求结束复原（含流式响应体期间的写入）。"""
    from src.request_context import get_api_key_id, set_api_key_id

    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/keyed")
    async def keyed():
        set_api_key_id("key_1")        # 模拟鉴权依赖
        return {"key": get_api_key_id()}

    with TestClient(app) as client:
        assert client.get("/keyed").json() == {"key": "key_1"}
        # 同一线程的下一个请求看不到上一个请求的归属
        assert client.get("/plain").status_code == 404
    assert get_api_key_id() is None


def test_middleware_resets_request_id_after_request():
    """request_id 随请求结束复原：超时存活的子任务不能继承旧 ID 写统计。"""
    from src.request_context import get_request_id

    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/rid")
    async def rid():
        from src.request_context import set_request_id
        set_request_id("stale-after-this")
        return {"rid": get_request_id()}

    with TestClient(app) as client:
        assert client.get("/rid").json() == {"rid": "stale-after-this"}
    assert get_request_id() is None


def test_middleware_resets_api_key_id_even_when_handler_raises():
    from src.request_context import get_api_key_id, set_api_key_id

    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/boom")
    async def boom():
        set_api_key_id("key_2")
        raise RuntimeError("handler exploded")

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/boom").status_code == 500
    assert get_api_key_id() is None


def test_record_persists_api_key_id_from_context(db):
    from src.request_context import set_api_key_id
    from src.stats.collector import StatsCollector

    collector = StatsCollector(db)
    set_api_key_id("key_ctx")
    try:
        collector.record(username="u", provider="trae", model="m", ok=True)
    finally:
        set_api_key_id(None)
    assert db.connect().execute(
        "SELECT api_key_id FROM usage_events").fetchone()["api_key_id"] == "key_ctx"


def test_record_without_key_context_stores_null(db):
    """后台任务 / 内部请求没有 Key 归属 → NULL（= 未归属，不猜）。"""
    from src.stats.collector import StatsCollector
    from src.stats.query import StatsQuery

    collector, _query = StatsCollector(db), StatsQuery(db)
    collector.record(username="u", provider="trae", model="m", ok=True)
    assert db.connect().execute(
        "SELECT api_key_id FROM usage_events").fetchone()["api_key_id"] is None


# ------------------------------------------------------- 会话标识（E1 的地基）

def test_session_id_defaults_none_and_round_trips():
    from src.request_context import get_session_id, set_session_id

    assert get_session_id() is None
    set_session_id("sess-1")
    assert get_session_id() == "sess-1"
    set_session_id(None)
    assert get_session_id() is None


def test_sanitize_session_id_accepts_and_rejects():
    from src.request_context import SESSION_ID_PATTERN, sanitize_session_id

    for raw in ("abc", "a-b.c_d:e", "x" * 128):
        assert sanitize_session_id(raw) == raw
    for raw in (None, "", "   ", "has space", "line\nbreak", "x" * 129):
        assert sanitize_session_id(raw) is None
    assert SESSION_ID_PATTERN.match("ok")


def test_session_id_from_headers_prefers_earlier_header_names():
    """按 SESSION_ID_HEADERS 顺序取第一个合法值（头名大小写不敏感）。"""
    from src.request_context import session_id_from_headers

    headers = [(b"conversation-id", b"third"), (b"x-session-id", b"first"),
               (b"x-opencode-session", b"second")]
    assert session_id_from_headers(headers) == "first"
    only_later = [(b"conversation-id", b"conversation")]
    assert session_id_from_headers(only_later) == "conversation"
    # 大小写混合的头名同样识别
    assert session_id_from_headers([(b"X-Session-Id", b"mixed")]) == "mixed"


def test_session_id_from_headers_skips_invalid_and_returns_none():
    from src.request_context import session_id_from_headers

    assert session_id_from_headers([]) is None
    assert session_id_from_headers([(b"x-session-id", b"bad id")]) is None
    # 第一个头非法时继续看下一个头，而不是整体放弃
    assert session_id_from_headers([(b"x-session-id", b"bad id"),
                                    (b"x-opencode-session", b"good")]) == "good"


def test_middleware_sets_and_clears_session_id():
    """中间件从入站头设置会话标识，请求结束后复原。"""
    from fastapi import FastAPI as _FastAPI
    from fastapi.testclient import TestClient as _TestClient

    from src.request_context import get_session_id

    app = _FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/sess")
    async def sess():
        return {"session": get_session_id()}

    with _TestClient(app) as client:
        response = client.get("/sess", headers={"x-session-id": "abc-123"})
        assert response.json()["session"] == "abc-123"
        # 复原：下一个请求不带头时读不到上一次的值
        assert client.get("/sess").json()["session"] is None

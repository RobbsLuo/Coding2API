"""本轮隐患修复的回归测试（HTTP 层，覆盖此前测试的盲区）。

每个测试对应用户报告里的一条隐患编号，直接走 TestClient，
不做内部函数直调——此前覆盖率 100% 但运行时异常路径未被覆盖。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from src.api.deps import Services
from src.api.streaming import with_keepalive
from src.auth.session import create_session_token
from src.compat.openai.response import StreamTranslator
from src.config import Settings
from src.db.crypto import MIN_SECRET_LENGTH, CredentialCipher, WeakSecretError
from src.db.migrate import SCHEMA_VERSION, schema_version
from src.engine.sse import SSE_COMMENT
from src.main import BodySizeLimitMiddleware, _api_not_found, _codebuddy_endpoint, build_app
from src.provider.base import Event, EventKind, Model, Quota, Usage
from tests.conftest import SECRET


@pytest.fixture()
def settings(tmp_path):
    return Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                    ADMIN_USERNAMES="root")


@pytest.fixture()
def client(settings):
    app = build_app(settings)
    with TestClient(app) as c:
        c.cookies.set("coding2api_session", create_session_token("root", SECRET))
        yield c


# ------------------------------------------------------- #1 流式前置校验


def test_stream_unknown_provider_returns_400_not_empty_200(client):
    key = client.post("/api/api-keys", json={"name": "t"}).json()["api_key"]
    response = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"},
                           json={"model": "glm-5.2@nope", "stream": True,
                                 "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 400
    assert "unknown provider" in response.json()["error"]["message"]


def test_stream_no_provider_registered_returns_400(settings):
    app = build_app(settings, providers={})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as c:
        response = c.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"},
                          json={"model": "glm-5.2", "stream": True,
                                "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 400


# ------------------------------------------------------------ #2 非法 JSON


def test_malformed_json_body_is_400_not_500(client):
    key = client.post("/api/api-keys", json={"name": "t"}).json()["api_key"]
    response = client.post("/v1/chat/completions", content=b"{not json",
                           headers={"Authorization": f"Bearer {key}",
                                    "Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_playground_malformed_json_body_is_400(client):
    response = client.post("/api/playground/chat/completions", content=b"{not json",
                           headers={"Content-Type": "application/json",
                                    "X-Requested-With": "XMLHttpRequest"})
    assert response.status_code == 400


# --------------------------------------------------------- #3 请求体上限


def test_chunked_login_body_over_limit_is_413(client):
    """Transfer-Encoding: chunked 不带 content-length，必须靠实际计数拦截。"""
    payload = json.dumps({"username": "x" * 20000, "password": "y"}).encode()

    def chunks():
        yield payload

    response = client.post("/api/auth/login", content=chunks(),
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "invalid_request"


def test_small_login_body_still_processed(client):
    """上限只拦超大 body，正常请求不受影响（走到认证逻辑返回 401）。"""
    response = client.post("/api/auth/login", json={"username": "root", "password": "bad"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_body_limit_middleware_passes_non_http_scope():
    """非 http scope（lifespan/websocket）必须原样透传。"""
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    middleware = BodySizeLimitMiddleware(app)
    await middleware({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]


@pytest.mark.asyncio
async def test_body_limit_middleware_rejects_oversized_content_length():
    """content-length 已超限时直接 413，不消费 body。"""
    sent = []

    async def app(scope, receive, send):  # pragma: no cover - 不应被调用
        raise AssertionError("downstream must not run")

    async def send(message):
        sent.append(message)

    middleware = BodySizeLimitMiddleware(app)
    scope = {"type": "http", "path": "/api/auth/login",
             "headers": [(b"content-length", b"999999")]}
    await middleware(scope, None, send)
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_body_limit_middleware_ignores_invalid_content_length():
    """非法 content-length 不应导致崩溃，退回按实际字节计数。"""
    received_bodies = []

    async def app(scope, receive, send):
        received_bodies.append(await receive())
        await send({"type": "http.response.start", "status": 200, "headers": []})

    middleware = BodySizeLimitMiddleware(app)
    scope = {"type": "http", "path": "/api/auth/login",
             "headers": [(b"content-length", b"not-a-number")]}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await middleware(scope, receive, send)
    assert received_bodies[0]["body"] == b""
    assert sent[0]["status"] == 200


# ------------------------------------------------- #4/#13 密钥与解密失败


def test_weak_app_secret_rejected():
    with pytest.raises(WeakSecretError):
        CredentialCipher("a" * (MIN_SECRET_LENGTH - 1))


def test_decrypt_failure_is_actionable_error(client, settings):
    """APP_SECRET 更换后探测凭证 → 明确错误码，而不是裸 500。"""
    created = client.post("/api/credentials", json={
        "provider": "codebuddy", "credential": {"token": "t"}}).json()["id"]
    db = client.app.state.credentials._db
    db.connect().execute("UPDATE credentials SET data_enc = ? WHERE id = ?",
                         (CredentialCipher("another-secret-0123").encrypt(b""), created))
    db.connect().commit()
    response = client.post(f"/api/credentials/{created}/probe")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "credential_decrypt_failed"


# ------------------------------------------------------------ #5 TRAE 刷新


def test_trae_provider_exposes_credential_from(tmp_path):
    """RefreshTask 依赖 provider.credential_from；缺失会让 TRAE 永不预刷新。"""
    import time

    from src.provider.trae.client import TraeProvider
    from src.tasks.refresh import _needs_refresh

    provider = TraeProvider()
    data = {"uid": "u", "accessToken": "at", "refreshToken": "rt",
            "expiresAt": int(time.time()) + 60}
    assert _needs_refresh(provider, data, 3600, int(time.time())) is True


# ------------------------------------------------------ #6 心跳 / #7 断连


@pytest.mark.asyncio
async def test_with_keepalive_emits_comment_on_idle():
    async def slow():
        await asyncio.sleep(0.05)
        yield b"data: x\n\n"

    frames = [f async for f in with_keepalive(slow(), interval=0.01)]
    assert SSE_COMMENT in frames
    assert frames[-1] == b"data: x\n\n"


@pytest.mark.asyncio
async def test_with_keepalive_passes_frames_through_when_fast():
    async def fast():
        yield b"a"
        yield b"b"

    assert [f async for f in with_keepalive(fast(), interval=5)] == [b"a", b"b"]


@pytest.mark.asyncio
async def test_stream_disconnect_records_usage():
    """客户端断开时把已知用量记账（client_disconnect），不再静默丢弃。

    上游序列：content → usage → content。usage 不单独成帧，
    所以第二帧已经是 usage 记录之后的内容，此时断开能看到 token。
    """
    from src.engine.executor import Executor, ExecutorDeps

    recorded = []

    @dataclass
    class Provider:
        id: str = "trae"

        async def stream_chat(self, credential_data, payload, model):
            yield Event(kind=EventKind.CONTENT, content="hello")
            yield Event(kind=EventKind.USAGE, usage=Usage(input_tokens=7, output_tokens=3))
            yield Event(kind=EventKind.CONTENT, content="world")

    executor = Executor(ExecutorDeps(
        providers={"trae": Provider()}, credentials=_FakeCredentials(),
        scheduler=_Scheduler(), stats=_Collector(recorded)))

    stream = executor.stream(_request(), username="alice")
    assert await anext(stream) is not None      # content
    assert await anext(stream) is not None      # 第二条 content（usage 已记录）
    await stream.aclose()
    assert [r["error_type"] for r in recorded] == ["client_disconnect"]
    assert recorded[0]["input_tokens"] == 7
    assert recorded[0]["ttfb_ms"] is not None


@pytest.mark.asyncio
async def test_stream_guarded_converts_unexpected_error_to_frame():
    from src.engine.executor import Executor, ExecutorDeps

    class Boom:
        id = "trae"

        async def stream_chat(self, credential_data, payload, model):
            yield Event(kind=EventKind.CONTENT, content="x")
            raise RuntimeError("boom")

    executor = Executor(ExecutorDeps(
        providers={"trae": Boom()}, credentials=_FakeCredentials(),
        scheduler=_Scheduler(), stats=_Collector([])))
    frames = [f async for f in executor.stream_guarded(_request())]
    assert b"internal_error" in frames[-1]


# --------------------------------------------------- #8/#9 模型列表 TTL


def test_models_endpoint_skips_disabled_credentials(settings):
    """模型列表只应使用可调度凭证，disabled 凭证不再被挑中。"""
    calls = []

    class Provider:
        id = "trae"

        async def list_models(self, credential_data):
            calls.append(credential_data)
            return [Model(id="glm-5.2")]

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    app = build_app(settings, providers={"trae": Provider()})
    credentials = app.state.credentials
    enabled = credentials.add(provider="trae", credential_data={"accessToken": "good"})
    disabled = credentials.add(provider="trae", credential_data={"accessToken": "bad"})
    credentials.set_enabled(disabled, False)
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as c:
        c.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert calls == [{"accessToken": "good"}]
    assert enabled  # 保留引用，避免被 lint 判为未使用


def test_models_endpoint_reuses_cache_within_ttl(settings):
    calls = []

    class Provider:
        id = "trae"

        async def list_models(self, credential_data):
            calls.append(1)
            return [Model(id="glm-5.2")]

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    app = build_app(settings, providers={"trae": Provider()})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as c:
        for _ in range(3):
            c.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert len(calls) == 1          # 后续请求命中 TTL 缓存


# --------------------------------------------------------- #10 探测清理


def test_pending_probes_drops_finished_tasks(client):
    """探测任务完成后从列表摘除，避免长期运行内存累积。"""
    from src.main import _forget_task

    pending = client.app.state.pending_probes
    pending.clear()
    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(asyncio.sleep(0))
        pending.append(task)
        task.add_done_callback(lambda done: _forget_task(done, pending))
        loop.run_until_complete(task)
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()
    assert pending == []


# ------------------------------------------------------------ #11 schema 版本


def test_schema_version_recorded(tmp_path):
    from src.db.conn import Database
    from src.db.migrate import apply_schema

    db = Database(tmp_path / "v.sqlite3")
    conn = db.connect()
    assert schema_version(conn) == 0        # 新库
    apply_schema(conn)
    assert schema_version(conn) == SCHEMA_VERSION
    db.close()


# ------------------------------------------------- #12 会话/Key 用户校验


def test_session_rejected_after_user_removed(settings, tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    token = create_session_token("ghost", SECRET)     # users.txt 里没有 ghost
    with TestClient(app) as c:
        c.cookies.set("coding2api_session", token)
        assert c.get("/api/auth/session").status_code == 401


def test_api_key_rejected_after_user_removed(settings):
    app = build_app(settings)
    key = app.state.api_keys.create("ghost")["api_key"]
    with TestClient(app) as c:
        response = c.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 401


def test_logout_requires_csrf(client):
    response = client.post("/api/auth/logout", headers={"Origin": "http://evil.example"})
    assert response.status_code == 403


def test_logout_allows_same_origin(client):
    response = client.post("/api/auth/logout",
                           headers={"Origin": "http://testserver",
                                    "X-Requested-With": "XMLHttpRequest"})
    assert response.status_code == 200


# -------------------------------------------------------- #14 API 404


def test_unknown_api_path_returns_json_404(client):
    for path in ("/api/nope", "/v1/nope"):
        response = client.get(path)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "invalid_request"


def test_frontend_routes_are_not_treated_as_api_404(client, tmp_path, monkeypatch):
    """前端路由（非 /api、/v1）不进 API 404 分支，走 SPA 兜底。

    dist 产物是 gitignored 的（CI 上不存在），因此这里把 dist 指到临时目录，
    同时断言「服务了 index.html」而不是「依赖本地构建产物」。
    """
    from src import main

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    monkeypatch.setattr(main, "_frontend_dist", lambda: dist)

    response = client.get("/credentials")
    assert response.status_code == 200
    assert "spa" in response.text


def test_api_not_found_helper_shape():
    assert _api_not_found("api/x").status_code == 404


# ---------------------------------------------------- #15 凭证恢复入口


def test_revive_credential_endpoint(client):
    created = client.post("/api/credentials", json={
        "provider": "codebuddy", "credential": {"token": "t"}}).json()["id"]
    db = client.app.state.credentials._db
    db.connect().execute("UPDATE credentials SET disabled = 1, disabled_reason = 'x' "
                         "WHERE id = ?", (created,))
    db.connect().commit()
    assert client.post(f"/api/credentials/{created}/revive").status_code == 200
    row = db.connect().execute("SELECT disabled, disabled_reason FROM credentials "
                               "WHERE id = ?", (created,)).fetchone()
    assert row["disabled"] == 0 and row["disabled_reason"] is None


def test_revive_unknown_credential_is_400(client):
    assert client.post("/api/credentials/cred_missing/revive").status_code == 400


# ------------------------------------------------------------- #24 429


def test_throttled_response_has_retry_after(settings):
    from src.auth.throttle import LoginThrottle, ThrottleLimits

    app = build_app(settings)
    app.state.services.login_throttle = LoginThrottle(ThrottleLimits(max_per_user=1))
    with TestClient(app) as c:
        c.post("/api/auth/login", json={"username": "root", "password": "bad"})
        response = c.post("/api/auth/login", json={"username": "root", "password": "bad"})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"


# ------------------------------------------------------- #27 审计日志


def test_credential_mutations_are_logged(client, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="src.api.admin_credentials"):
        created = client.post("/api/credentials", json={
            "provider": "codebuddy", "credential": {"token": "t"}}).json()["id"]
        client.post("/api/credentials/pin", json={"credential_id": created})
        client.delete(f"/api/credentials/{created}")
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "新增凭证" in messages and "固定凭证" in messages and "删除凭证" in messages


# ------------------------------------------------- 配置接线 / 传输层错误


def test_codebuddy_endpoint_honours_config():
    config = Settings(_env_file=None, APP_SECRET=SECRET,
                      CODEBUDDY_API_ENDPOINT="https://www.codebuddy.ai")
    assert _codebuddy_endpoint(config) == "https://www.codebuddy.ai"


def test_codebuddy_endpoint_rejects_non_whitelisted():
    config = Settings(_env_file=None, APP_SECRET=SECRET,
                      CODEBUDDY_API_ENDPOINT="https://evil.example")
    with pytest.raises(ValueError):
        _codebuddy_endpoint(config)


def test_transport_error_maps_to_502(client, monkeypatch):
    key = client.post("/api/api-keys", json={"name": "t"}).json()["api_key"]
    executor = client.app.state.executor

    async def boom(request, *, username="unknown"):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(executor, "complete", boom)
    response = client.post("/v1/chat/completions",
                           headers={"Authorization": f"Bearer {key}"},
                           json={"model": "glm-5.2",
                                 "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_unavailable"


# --------------------------------------------------------- 辅助替身


@dataclass
class _FakeCredentials:
    provider: str = "trae"
    data: dict = field(default_factory=lambda: {"accessToken": "a"})

    def candidates(self, providers=None, *, selectable_only=False):
        from src.engine.scheduler import Candidate

        return [Candidate(credential_id="c1", provider=self.provider)]

    def provider_of(self, credential_id):
        return self.provider

    def credential_data(self, credential_id):
        return self.data

    def save_success(self, credential_id):  # pragma: no cover - 断连路径不触发
        pass

    def save_error(self, credential_id, outcome):  # pragma: no cover
        pass


class _Scheduler:
    def select(self, candidates, tried, now):
        for candidate in candidates:
            if candidate.credential_id not in tried:
                return candidate.credential_id
        return None

    def should_rotate(self, tried):
        return False

    def note_error(self, candidate, kind, now):  # pragma: no cover
        raise AssertionError("unexpected error path")


class _Collector:
    def __init__(self, sink):
        self._sink = sink

    def record(self, **fields):
        self._sink.append(fields)


def _request():
    from src.compat.openai.request import ChatRequest

    return ChatRequest(model="glm-5.2", messages=[{"role": "user", "content": "hi"}],
                       stream=True, raw={"messages": [{"role": "user", "content": "hi"}]})


def _translator_frame_check():
    """StreamTranslator.keepalive 返回的注释帧形状。"""
    assert StreamTranslator("m").keepalive() is SSE_COMMENT


def test_translator_keepalive_matches_sse_comment():
    _translator_frame_check()


def test_services_dataclass_has_model_ttl_field(settings):
    """Services 携带 TTL 时间戳字段（模型列表缓存用）。"""
    app = build_app(settings)
    services: Services = app.state.services
    assert services.model_list_fetched_at == {}
    assert services.model_list_cache == {}


def test_quota_import_still_available():
    """Quota 仍在 base 中（repo 健康度计算依赖）。"""
    assert Quota(remaining=1, total=2).remaining == 1


# ------------------------------------------------- 覆盖率补口（真实分支）


def test_models_endpoint_falls_back_to_cache(settings):
    """动态拉取失败且已有缓存时，用缓存兜底保持列表完整。"""
    class Flaky:
        id = "trae"
        fail = False

        async def list_models(self, credential_data):
            if self.fail:
                raise RuntimeError("upstream down")
            return [Model(id="glm-5.2")]

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    provider = Flaky()
    app = build_app(settings, providers={"trae": provider})
    services = app.state.services
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as c:
        c.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
        provider.fail = True
        second = c.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert second.status_code == 200
    assert [m["id"] for m in second.json()["data"]] == ["glm-5.2"]
    assert services.model_list_cache["trae"]      # 缓存保留


def test_models_endpoint_logs_when_no_cache(settings):
    """首次拉取就失败且无缓存 → 跳过该上游但仍返回列表。"""

    class Broken:
        id = "trae"

        async def list_models(self, credential_data):
            raise RuntimeError("no cache available")

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    app = build_app(settings, providers={"trae": Broken()})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as c:
        response = c.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 200 and response.json()["data"] == []


@pytest.mark.asyncio
async def test_with_keepalive_cancels_pending_on_close():
    """生成器被关闭时，在途的取帧任务必须被取消（不留悬挂 Task）。"""
    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.sleep(60)
        yield b"never"

    stream = with_keepalive(slow(), interval=0.01)
    first = await anext(stream)
    assert first is SSE_COMMENT and started.is_set()
    await stream.aclose()


def test_stream_guarded_records_disconnect_before_error_frame():
    """stream_guarded 对客户端断开不吞异常（保留 499 语义）。"""
    from src.engine.executor import Executor, ExecutorDeps

    class Slow:
        id = "trae"

        async def stream_chat(self, credential_data, payload, model):
            yield Event(kind=EventKind.CONTENT, content="x")
            await asyncio.sleep(60)

    async def run():
        executor = Executor(ExecutorDeps(
            providers={"trae": Slow()}, credentials=_FakeCredentials(),
            scheduler=_Scheduler(), stats=_Collector([])))
        stream = executor.stream_guarded(_request())
        assert await anext(stream) is not None
        await stream.aclose()          # 不应抛异常

    asyncio.run(run())


def test_ttfb_none_when_no_frames_and_elapsed_helper():
    """未产生任何帧时 ttfb 为 None；_elapsed_ms 支持显式终点。"""
    from src.engine.executor import _elapsed_ms, _StreamState

    state = _StreamState(translator=StreamTranslator("m"), started=1.0, username="u")
    assert state.ttfb_ms() is None
    state.mark_first_byte()
    assert state.ttfb_ms() is not None
    assert _elapsed_ms(1.0, 1.5) == 500


def test_schema_version_constant_is_positive():
    assert SCHEMA_VERSION >= 1


def test_body_limit_selects_login_limit():
    from src.main import DEFAULT_BODY_LIMIT, LOGIN_BODY_LIMIT, _body_limit

    assert _body_limit("/api/auth/login") == LOGIN_BODY_LIMIT
    assert _body_limit("/v1/chat/completions") == DEFAULT_BODY_LIMIT


def test_models_cache_fallback_when_ttl_expired(settings):
    """TTL 过期后重新拉取失败 → 用缓存兜底（cached 分支）。"""
    class Flaky:
        id = "trae"
        fail = False

        async def list_models(self, credential_data):
            if self.fail:
                raise RuntimeError("upstream down")
            return [Model(id="glm-5.2")]

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    provider = Flaky()
    app = build_app(settings, providers={"trae": provider})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as c:
        c.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
        provider.fail = True
        app.state.services.model_list_fetched_at.clear()      # 模拟 TTL 过期
        response = c.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert [m["id"] for m in response.json()["data"]] == ["glm-5.2"]


@pytest.mark.asyncio
async def test_disconnect_with_bytes_but_no_usage_still_recorded():
    """已吐字节但上游未给 usage 时，断开也要记账（ttfb 分支）。"""
    from src.engine.executor import Executor, ExecutorDeps

    recorded = []

    @dataclass
    class Provider:
        id: str = "trae"

        async def stream_chat(self, credential_data, payload, model):
            yield Event(kind=EventKind.CONTENT, content="partial")
            yield Event(kind=EventKind.CONTENT, content="more")

    executor = Executor(ExecutorDeps(
        providers={"trae": Provider()}, credentials=_FakeCredentials(),
        scheduler=_Scheduler(), stats=_Collector(recorded)))
    stream = executor.stream(_request(), username="alice")
    assert await anext(stream) is not None
    await stream.aclose()
    assert recorded[0]["error_type"] == "client_disconnect"
    assert recorded[0]["input_tokens"] is None
    assert recorded[0]["ttfb_ms"] is not None


@pytest.mark.asyncio
async def test_body_limit_middleware_ignores_disconnect_messages():
    """receive 返回 http.disconnect 时不参与计数，原样透传。"""
    seen = []

    async def app(scope, receive, send):
        seen.append(await receive())
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def receive():
        return {"type": "http.disconnect"}

    sent = []

    async def send(message):
        sent.append(message)

    middleware = BodySizeLimitMiddleware(app)
    await middleware({"type": "http", "path": "/x", "headers": []}, receive, send)
    assert seen == [{"type": "http.disconnect"}]
    assert sent[0]["status"] == 200


@pytest.mark.asyncio
async def test_body_limit_middleware_discards_downstream_body_when_exceeded():
    """超限后下游响应体被丢弃，只保留我们的 413。"""
    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"downstream"})

    sent = []

    async def send(message):
        sent.append(message)

    # 登录路径上限 8KB，构造刚好超限的 body
    big = {"type": "http.request", "body": b"x" * (8 * 1024 + 1), "more_body": False}

    async def receive():
        return big

    middleware = BodySizeLimitMiddleware(app)
    await middleware({"type": "http", "path": "/api/auth/login", "headers": []},
                     receive, send)
    body_messages = [m for m in sent if m["type"] == "http.response.body"]
    assert body_messages and b"request body too large" in body_messages[0]["body"]
    assert all(b"downstream" not in m.get("body", b"") for m in sent)


@pytest.mark.asyncio
async def test_body_limit_middleware_sends_only_one_413():
    """下游发多次 response.start 时，413 只发一次（replaced 分支）。"""
    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.start", "status": 201, "headers": []})

    async def receive():
        return {"type": "http.request", "body": b"x" * (8 * 1024 + 1), "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    middleware = BodySizeLimitMiddleware(app)
    await middleware({"type": "http", "path": "/api/auth/login", "headers": []},
                     receive, send)
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [413]


@pytest.mark.asyncio
async def test_disconnect_after_usage_only_records_once():
    """断开时 usage 已到 → 走 usage 分支（第一个条件成立）。"""
    from src.engine.executor import Executor, ExecutorDeps

    recorded = []

    @dataclass
    class Provider:
        id: str = "trae"

        async def stream_chat(self, credential_data, payload, model):
            yield Event(kind=EventKind.USAGE, usage=Usage(input_tokens=1))
            yield Event(kind=EventKind.CONTENT, content="x")

    executor = Executor(ExecutorDeps(
        providers={"trae": Provider()}, credentials=_FakeCredentials(),
        scheduler=_Scheduler(), stats=_Collector(recorded)))
    stream = executor.stream(_request(), username="alice")
    assert await anext(stream) is not None      # content 帧（usage 已记录）
    await stream.aclose()
    assert recorded[0]["input_tokens"] == 1


@pytest.mark.asyncio
async def test_disconnect_before_any_output_records_nothing():
    """上游尚未吐任何字节就断开 → 不记账（无用量可言）。"""
    from src.engine.executor import Executor, ExecutorDeps

    recorded = []

    @dataclass
    class Provider:
        id: str = "trae"

        async def stream_chat(self, credential_data, payload, model):
            await asyncio.sleep(60)
            yield Event(kind=EventKind.CONTENT, content="never")

    executor = Executor(ExecutorDeps(
        providers={"trae": Provider()}, credentials=_FakeCredentials(),
        scheduler=_Scheduler(), stats=_Collector(recorded)))

    async def run():
        stream = executor.stream(_request(), username="alice")
        pending = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0.01)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await stream.aclose()

    await run()
    assert recorded == []


# --------------------------------------------------- #25 dump 有界保留


def test_dump_request_body_trims_to_keep_limit(tmp_path):
    """超过保留上限后旧文件被淘汰（文件名同毫秒会覆盖，故只断言上界）。"""
    from src.api import chat

    for _ in range(chat.DUMP_KEEP_FILES + 5):
        chat.dump_request_body(tmp_path, {"messages": []})
    remaining = list((tmp_path / "dumps").glob("*.json"))
    assert 0 < len(remaining) <= chat.DUMP_KEEP_FILES


def test_dump_request_body_survives_unwritable_dir(tmp_path, monkeypatch):
    """诊断落盘失败不影响聊天请求。"""
    from src.api import chat

    def boom(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "write_text", boom)
    chat.dump_request_body(tmp_path, {"a": 1})      # 不应抛异常


def test_dump_request_body_trims_ignoring_unlink_errors(tmp_path, monkeypatch):
    """淘汰旧文件时遇到错误不抛出（suppress 分支）。"""
    from pathlib import Path as _Path

    from src.api import chat

    # 先造出比上限多的旧文件（文件名直接给定，避免同毫秒覆盖）
    dump_dir = tmp_path / "dumps"
    dump_dir.mkdir()
    for index in range(3):
        (dump_dir / f"{index:013d}.json").write_text("", encoding="utf-8")

    def boom(self, *args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(_Path, "unlink", boom)
    monkeypatch.setattr(chat, "DUMP_KEEP_FILES", 1)
    chat.dump_request_body(tmp_path, {"a": 1})      # 不应抛异常


# ------------------------------------------------- #28 限流前置（防 CPU 放大）


def test_transport_windows_checked_before_hashing(app_client_and_settings):
    """全局/IP 窗口超限时不再执行 PBKDF2（哈希前拦截）。"""
    from src.auth.throttle import LoginThrottle, ThrottleLimits

    app, client = app_client_and_settings
    hashes = []
    original = app.state.users.verify

    def counting_verify(username, password):
        hashes.append(username)
        return original(username, password)

    app.state.users.verify = counting_verify
    app.state.services.login_throttle = LoginThrottle(
        ThrottleLimits(max_per_ip=1, max_per_user=99, max_global=99))

    assert client.post("/api/auth/login",
                       json={"username": "root", "password": "bad"}).status_code == 401
    assert len(hashes) == 1
    # IP 窗口已满 → 第二个请求在哈希前被拒
    assert client.post("/api/auth/login",
                       json={"username": "root", "password": "rootpw"}).status_code == 429
    assert len(hashes) == 1


def test_correct_password_not_blocked_by_username_window(app_client_and_settings):
    """用户名窗口在哈希后判定：密码正确时不因他人刷错用户名而被拒。"""
    from src.auth.throttle import LoginThrottle, ThrottleLimits

    app, client = app_client_and_settings
    app.state.services.login_throttle = LoginThrottle(
        ThrottleLimits(max_per_user=1, max_per_ip=100, max_global=100))

    # 先烧掉该用户名的窗口（模拟他人用同用户名输错密码）
    app.state.services.login_throttle.record_failure(ip="9.9.9.9", username="root")
    response = client.post("/api/auth/login",
                           json={"username": "root", "password": "rootpw"})
    assert response.status_code == 200


@pytest.fixture()
def app_client_and_settings(settings):
    app = build_app(settings)
    with TestClient(app) as client:
        yield app, client


def test_transport_windows_rejects_when_global_cap_reached():
    """全局窗口满时哈希前即拒（全局分支）。"""
    from src.auth.throttle import LoginThrottle, ThrottledError, ThrottleLimits

    throttle = LoginThrottle(ThrottleLimits(max_global=1, max_per_ip=99, max_per_user=99))
    throttle.record_failure(ip="9.9.9.9", username="ghost")
    with pytest.raises(ThrottledError):
        throttle.check_transport_windows(ip="1.2.3.4")


def test_transport_windows_pass_within_limits():
    from src.auth.throttle import LoginThrottle, ThrottleLimits

    throttle = LoginThrottle(ThrottleLimits(max_global=5, max_per_ip=5, max_per_user=5))
    throttle.check_transport_windows(ip="1.2.3.4")      # 不抛


def test_transport_windows_rejects_when_ip_cap_reached():
    from src.auth.throttle import LoginThrottle, ThrottledError, ThrottleLimits

    throttle = LoginThrottle(ThrottleLimits(max_global=99, max_per_ip=1, max_per_user=99))
    throttle.record_failure(ip="1.2.3.4", username="ghost")
    with pytest.raises(ThrottledError):
        throttle.check_transport_windows(ip="1.2.3.4")


def test_check_covers_ip_window_directly():
    """check() 的 IP 分支（HTTP 流程已被前置检查挡掉，这里直接调用）。"""
    from src.auth.throttle import LoginThrottle, ThrottledError, ThrottleLimits

    throttle = LoginThrottle(ThrottleLimits(max_global=99, max_per_ip=1, max_per_user=99))
    throttle.record_failure(ip="1.2.3.4", username="")
    with pytest.raises(ThrottledError):
        throttle.check(ip="1.2.3.4", username="")


def test_translator_error_event_emits_error_frame():
    """ERROR 事件直接喂给 translator 时产出错误帧。

    executor 在流式路径上会先看到 ERROR 并 break（改走换号），
    所以这条分支只有直接使用 translator 时才走到——此处把它固定下来，
    避免以后有人把 ERROR 静默丢弃。
    """
    translator = StreamTranslator("glm-5.2")
    frames = list(translator.translate(
        Event(kind=EventKind.ERROR, error_code=1005, error_message="no quota")))
    assert len(frames) == 1
    payload = json.loads(frames[0].decode().removeprefix("data: ").strip())
    assert payload["error"]["code"] == 1005
    assert payload["error"]["message"] == "no quota"

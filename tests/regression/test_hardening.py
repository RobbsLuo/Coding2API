"""本轮隐患修复回归（HTTP 层）——前置校验与请求体（#1-#5/#11/#13）。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src.api.deps import Services
from src.compat.openai.response import StreamTranslator
from src.config import Settings
from src.db.crypto import MIN_SECRET_LENGTH, CredentialCipher, WeakSecretError
from src.db.migrate import SCHEMA_VERSION, schema_version
from src.engine.sse import SSE_COMMENT
from src.main import BodySizeLimitMiddleware, _codebuddy_endpoint, build_app
from src.provider.base import Quota
from tests.helpers.constants import SECRET


def _translator_frame_check():
    """StreamTranslator.keepalive 返回的注释帧形状。"""
    assert StreamTranslator("m").keepalive() is SSE_COMMENT

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

def test_nan_json_body_is_400_not_passed_upstream(client):
    """JSON 里的 NaN/Infinity（Python 默认接受）必须 400，不能透传给上游。"""
    key = client.post("/api/api-keys", json={"name": "t"}).json()["api_key"]
    for literal in (b'{"messages":[1],"temperature":NaN}',
                    b'{"messages":[1],"temperature":Infinity}'):
        response = client.post(
            "/v1/chat/completions", content=literal,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"

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
    assert _body_limit("/api/auth/login/") == LOGIN_BODY_LIMIT
    assert _body_limit("/v1/chat/completions") == DEFAULT_BODY_LIMIT

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
    assert body_messages
    # 实测大小与配置上限都回填（分块路径没有 content-length）
    assert (b"request body 8193 bytes exceeds limit 8192 bytes"
            in body_messages[0]["body"])
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

def test_openai_chat_rejects_bad_numeric_fields():
    """OpenAI chat 出口与 Anthropic / Responses 用同一套数值边界，
    合法 JSON 里的 `-1`/`true`/超上限不能透传给上游。"""
    from src.compat.openai.request import InvalidRequest, parse_chat_request

    base = {"messages": [{"role": "user", "content": "hi"}]}
    for bad in ({"max_tokens": -1}, {"max_tokens": 0},
                {"max_tokens": 10**9}, {"max_tokens": True},
                {"max_completion_tokens": -5},
                {"temperature": 99999}, {"top_p": "hot"},
                {"temperature": float("nan")}):
        with pytest.raises(InvalidRequest):
            parse_chat_request({**base, **bad})
    # 合法值原样保留（归一为 float 便于后续透传）
    parsed = parse_chat_request({**base, "temperature": 0.3, "top_p": 0.9,
                                 "max_tokens": 128})
    assert parsed.raw["temperature"] == 0.3 and parsed.raw["max_tokens"] == 128
    # 显式 null 上限视为未设置，不报错
    assert parse_chat_request({**base, "max_tokens": None}).raw["max_tokens"] is None

def test_openai_chat_rejects_penalty_seed_n_and_logit_bias():
    """`1e999` 等溢出成 inf 的采样参数、越界 penalty、畸形 seed/n/logit_bias
    都必须 400，而不是重新序列化成非法 JSON `Infinity` 透传给上游。"""
    from src.compat.openai.request import InvalidRequest, parse_chat_request

    base = {"messages": [{"role": "user", "content": "hi"}]}
    inf = float("inf")
    for bad in ({"presence_penalty": inf}, {"frequency_penalty": -inf},
                {"presence_penalty": 3}, {"frequency_penalty": -3},
                {"presence_penalty": True}, {"frequency_penalty": "x"},
                {"seed": inf}, {"seed": True}, {"seed": "x"},
                {"n": 0}, {"n": 2}, {"n": 99}, {"n": True},
                {"logit_bias": "x"}, {"logit_bias": {1: 1}},
                {"logit_bias": {"1": inf}}):
        with pytest.raises(InvalidRequest):
            parse_chat_request({**base, **bad})
    # 合法值：penalty 归一为 float，seed 取整，n 只允许 1（A3：出口结构单 choice），
    # logit_bias 原样
    parsed = parse_chat_request({**base, "presence_penalty": -1.5,
                                 "frequency_penalty": 2, "seed": 42.9,
                                 "n": 1, "logit_bias": {"1": -100}})
    assert parsed.raw["presence_penalty"] == -1.5
    assert parsed.raw["frequency_penalty"] == 2.0
    assert parsed.raw["seed"] == 42 and parsed.raw["n"] == 1
    assert parsed.raw["logit_bias"] == {"1": -100}

def test_openai_chat_validates_image_url_parts():
    """`image_url` 内容块只校验本服务要读的字段：畸形图片是客户端错误，
    放行会让上游回 400，而本服务按上游 4xx 冷却凭证。"""
    from src.compat.openai.request import InvalidRequest, parse_chat_request

    good = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "看图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]}]}
    parsed = parse_chat_request(good)
    assert parsed.messages[0]["content"][1]["image_url"]["url"].startswith("data:")

    for bad_content in (
        [{"type": "image_url", "image_url": "https://x/y.png"}],
        [{"type": "image_url", "image_url": {}}],
        [{"type": "image_url", "image_url": {"url": ""}}],
        [{"type": "image_url", "image_url": {"url": 5}}],
        [{"type": "image_url"}],
    ):
        with pytest.raises(InvalidRequest, match="image_url"):
            parse_chat_request({"messages": [{"role": "user", "content": bad_content}]})

    # 非图片 part 一律放行（宽松透传，语义交上游裁决）
    parse_chat_request({"messages": [{"role": "user", "content": [
        {"type": "input_audio", "input_audio": {"data": "x"}}]}]})

    # 直接调用校验函数：非 dict 消息 / 非数组 content 一律跳过。
    # `parse_chat_request` 已先行保证消息是 dict，故这条防御分支只能从函数入口测。
    from src.compat.openai.request import validate_content_parts

    validate_content_parts([5, {"role": "user", "content": "hi"},
                            {"role": "user", "content": None}])

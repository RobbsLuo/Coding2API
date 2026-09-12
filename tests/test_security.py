"""安全边界测试（PROPOSAL §8）：登录限流 / CSRF / 请求体上限 / 安全头 / Host 白名单。

每个测试独立 build_app（独立 throttle 与数据库），互不污染窗口计数。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.auth.csrf import X_REQUESTED_WITH
from src.auth.session import create_session_token
from src.auth.throttle import LoginThrottle, ThrottleLimits
from src.config import Settings
from src.main import build_app


@pytest.fixture()
def app(tmp_path):
    settings = Settings(
        _env_file=None, APP_SECRET="s", DATA_DIR=str(tmp_path), ADMIN_USERNAMES="root"
    )
    application = build_app(settings)
    with TestClient(application) as client:
        yield application, client


@pytest.fixture()
def admin_client(app):
    _app, client = app
    client.cookies.set("coding2api_session", create_session_token("root", "s"))
    return client


# ---------------------------------------------------------------- 登录限流


def test_login_throttled_after_repeated_failures(app):
    _app, client = app
    _app.state.services.login_throttle = LoginThrottle(
        limits=ThrottleLimits(max_per_user=3, max_global=100, max_per_ip=100)
    )
    payload = {"username": "root", "password": "wrong"}
    for _ in range(3):
        assert client.post("/api/auth/login", json=payload).status_code == 401
    # 第四次触发用户名窗口 → 429，不再执行 PBKDF2
    assert client.post("/api/auth/login", json=payload).status_code == 429


def test_login_throttle_per_ip_isolates_users(app):
    _app, client = app
    _app.state.services.login_throttle = LoginThrottle(
        limits=ThrottleLimits(max_per_user=100, max_per_ip=2, max_global=100)
    )
    for _ in range(2):
        assert (
            client.post("/api/auth/login", json={"username": "root", "password": "bad"}).status_code
            == 401
        )
    # 同 IP 下即使换用户名也被限流（IP 窗口独立于用户名窗口）
    assert (
        client.post("/api/auth/login", json={"username": "guest", "password": "bad"}).status_code
        == 429
    )


def test_login_throttle_success_clears_user_window(app):
    _app, client = app
    _app.state.services.login_throttle = LoginThrottle(
        limits=ThrottleLimits(max_per_user=2, max_global=100, max_per_ip=100)
    )
    # 失败 2 次达到阈值
    for _ in range(2):
        assert (
            client.post("/api/auth/login", json={"username": "root", "password": "bad"}).status_code
            == 401
        )
    assert (
        client.post("/api/auth/login", json={"username": "root", "password": "bad"}).status_code
        == 429
    )
    # 窗口内成功登录一次后，该用户名窗口清空，可再次尝试
    client.post("/api/auth/logout")
    # 等待窗口过期不必要：record_success 直接清空该用户计数
    assert (
        client.post("/api/auth/login", json={"username": "root", "password": "rootpw"}).status_code
        == 200
    )
    assert (
        client.post("/api/auth/login", json={"username": "root", "password": "bad"}).status_code
        == 401
    )


# ------------------------------------------------------------------- CSRF


def test_csrf_rejects_cross_origin_write(admin_client):
    response = admin_client.post(
        "/api/credentials",
        json={"provider": "trae", "credential": {}},
        headers={"Origin": "http://evil.example.com"},
    )
    assert response.status_code == 403


def test_csrf_rejects_cross_origin_referer(admin_client):
    response = admin_client.post(
        "/api/api-keys", json={"name": "x"}, headers={"Referer": "http://evil.example.com/admin"}
    )
    assert response.status_code == 403


def test_csrf_allows_same_origin(admin_client):
    # TestClient 默认 Host=testserver；同源 Origin 必须放行
    response = admin_client.post(
        "/api/credentials",
        json={
            "provider": "trae",
            "credential": {"accessToken": "t", "uid": "u", "refreshToken": "r"},
        },
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 200


def test_csrf_allows_custom_header(admin_client):
    response = admin_client.post(
        "/api/credentials",
        json={
            "provider": "trae",
            "credential": {"accessToken": "t", "uid": "u", "refreshToken": "r"},
        },
        headers={X_REQUESTED_WITH: "XMLHttpRequest"},
    )
    assert response.status_code == 200


def test_csrf_allows_headerless_non_browser(admin_client):
    # 无 Origin/Referer/自定义头 → 非浏览器客户端（curl/SDK/测试），放行
    response = admin_client.post(
        "/api/credentials",
        json={
            "provider": "trae",
            "credential": {"accessToken": "t", "uid": "u", "refreshToken": "r"},
        },
    )
    assert response.status_code == 200


def test_csrf_not_applied_to_api_key_endpoints(app):
    # /v1 走 API Key（无 cookie），不受 CSRF 校验影响
    _app, client = app
    assert client.get("/v1/models").status_code == 401  # 无 key
    assert (
        client.post(
            "/v1/chat/completions",
            json={"messages": []},
            headers={"Origin": "http://evil.example.com"},
        ).status_code
        == 401
    )


# ------------------------------------------------------------ 请求体上限


def test_login_body_limit_8kb(app):
    _app, client = app
    payload = b'{"username": "root", "password": "' + b"x" * (8 * 1024) + b'"}'
    response = client.post(
        "/api/auth/login", content=payload, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413


def test_chat_body_limit_16mb(app):
    _app, client = app
    oversized = b"{" + b"x" * (16 * 1024 * 1024 + 1) + b"}"
    response = client.post(
        "/v1/chat/completions", content=oversized, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413


# -------------------------------------------------------------- 安全响应头


def test_security_headers_present(app):
    _app, client = app
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["content-security-policy"] == "frame-ancestors 'none'"


# ------------------------------------------------------------- Host 白名单


def test_host_whitelist_rejects_foreign(app):
    _app, client = app
    response = client.get("/health", headers={"Host": "evil.example.com"})
    assert response.status_code == 400


def test_host_whitelist_allows_default(app):
    _app, client = app
    assert client.get("/health").status_code == 200  # testserver 默认放行


def test_host_whitelist_allows_configured(tmp_path):
    settings = Settings(
        _env_file=None,
        APP_SECRET="s",
        DATA_DIR=str(tmp_path),
        ADMIN_USERNAMES="root",
        ALLOWED_HOSTS="api.example.com",
    )
    application = build_app(settings)
    with TestClient(application) as client:
        assert client.get("/health", headers={"Host": "api.example.com"}).status_code == 200
        assert client.get("/health", headers={"Host": "127.0.0.1"}).status_code == 400

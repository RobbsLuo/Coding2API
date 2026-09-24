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
from tests.conftest import SECRET


@pytest.fixture()
def app(tmp_path):
    settings = Settings(
        _env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path), ADMIN_USERNAMES="root"
    )
    application = build_app(settings)
    with TestClient(application) as client:
        yield application, client


@pytest.fixture()
def admin_client(app):
    _app, client = app
    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
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


def test_hsts_only_for_https_deployments(tmp_path):
    """HSTS 仅 https 部署下发：明文场景发了无意义，还会预锁本地 http 访问。"""
    https_app = build_app(Settings(
        _env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
        ADMIN_USERNAMES="root", PUBLIC_BASE_URL="https://gw.example.com"))
    with TestClient(https_app) as client:
        assert client.get("/health").headers["strict-transport-security"] \
            .startswith("max-age=")


def test_hsts_absent_for_http_deployment(app):
    _app, client = app
    assert "strict-transport-security" not in client.get("/health").headers


# ------------------------------------------------------------ 文档端点开关


def test_docs_disabled_by_default(app):
    """默认不开 docs：openapi schema 绝不对外。

    /docs 与 /openapi.json 的具体落点随环境不同（本地有 web/dist 时由 SPA
    catch-all 接住返回前端壳；CI 无 dist 时是 503 引导页），但共同不变量是：
    任何路径都不会吐出 OpenAPI schema 或 Swagger UI。
    """
    _app, client = app
    for path in ("/docs", "/openapi.json"):
        response = client.get(path)
        assert '"openapi"' not in response.text
        assert "swagger" not in response.text.lower()


def test_docs_opt_in(tmp_path):
    application = build_app(Settings(
        _env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
        ADMIN_USERNAMES="root", ENABLE_DOCS=True))
    with TestClient(application) as client:
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200


# ------------------------------------------------------------ 登录入口加固


def test_login_ip_respects_trusted_proxy(tmp_path):
    """反代部署（trust_proxy=true）下，登录审计按 XFF 最后条目取来源 IP；
    限流桶因此按真实客户端分桶，而不是全站共享代理地址一个桶。"""
    from src.audit.actions import ACTION_LOGIN_FAILURE
    from src.db.repo import AuditRepository

    application = build_app(Settings(
        _env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
        ADMIN_USERNAMES="root", TRUST_PROXY=True))
    headers = {"X-Forwarded-For": "203.0.113.7, 198.51.100.9"}
    with TestClient(application) as client:
        assert client.post("/api/auth/login",
                           json={"username": "root", "password": "bad"},
                           headers=headers).status_code == 401
        audit = AuditRepository(application.state.services.audit._db)
        rows = audit.query(action=ACTION_LOGIN_FAILURE, limit=1)
    assert rows and rows[0]["ip"] == "198.51.100.9"


def test_login_csrf_rejected_with_session_cookie_and_cross_origin(admin_client):
    """login CSRF：带会话 cookie 的跨站登录请求一并拦截；无 cookie 的
    登录（curl / 首次登录）不受影响（由 csrf_protected 的跳过逻辑保证）。"""
    response = admin_client.post(
        "/api/auth/login", json={"username": "root", "password": "bad"},
        headers={"Origin": "http://evil.example.com"})
    assert response.status_code == 403


def test_login_without_cookie_skips_csrf(app):
    _app, client = app
    # 无会话 cookie：非浏览器客户端直接放行到密码校验（401 而非 403）
    assert client.post("/api/auth/login",
                       json={"username": "root", "password": "bad"}).status_code == 401


# ------------------------------------------------------------- Host 白名单


def test_same_host_malformed_references_and_ipv6():
    """_same_host 的异常与边界：坏 URL、无 host、IPv6、非法端口。"""
    from starlette.requests import Request

    from src.auth.csrf import _same_host

    def request_with_host(host: str) -> Request:
        return Request(scope={"type": "http",
                              "headers": [(b"host", host.encode())]})

    request = request_with_host("localhost:8000")
    # urlsplit 异常（非法 IPv6）与无 hostname 的引用
    assert _same_host("http://[::1", request) is False
    assert _same_host("not-a-url", request) is False
    # IPv6 字面量 host 头
    ipv6 = request_with_host("[::1]:8000")
    assert _same_host("http://[::1]:8000/x", ipv6) is True
    # reference 端口非法 → 视为缺省端口，与 8000 不一致
    assert _same_host("http://localhost:abc", request) is False
    # host 头端口非法 → 直接 False
    assert _same_host("http://localhost", request_with_host("localhost:abc")) is False


def test_csrf_referer_same_origin_allows_write():
    """带会话 cookie 且 Referer 同源（无 Origin）的写请求放行。"""
    from starlette.requests import Request

    from src.auth.csrf import SESSION_COOKIE, check_csrf

    request = Request(scope={"type": "http", "headers": [
        (b"cookie", f"{SESSION_COOKIE}=tok".encode()),
        (b"referer", b"http://testserver/page"),
        (b"host", b"testserver"),
    ]})
    check_csrf(request)          # 不抛 CsrfRejectedError 即放行


def test_login_throttle_prunes_expired_and_global_cap():
    """过期事件被清理；全局窗口超限抛 ThrottledError；空用户名成功不清空。"""
    import time as _time

    from src.auth.throttle import LoginThrottle, ThrottledError, ThrottleLimits

    throttle = LoginThrottle(ThrottleLimits(max_global=40, max_per_ip=8, max_per_user=5))
    # 塞一条过期失败记录，check 的 _prune 应清理它（popleft 分支）
    throttle._events[("g", "")].append(_time.monotonic() - 61)
    throttle.check(ip="1.2.3.4", username="alice")            # 不抛

    # 全局窗口：塞满 max_global 条新失败 → 任意 IP/用户都被拒
    global_throttle = LoginThrottle(
        ThrottleLimits(max_global=1, max_per_ip=100, max_per_user=100))
    global_throttle.record_failure(ip="9.9.9.9", username="ghost")
    with pytest.raises(ThrottledError):
        global_throttle.check(ip="1.2.3.4", username="alice")

    # 空用户名的成功登录不清空任何计数
    throttle.record_success(username="")
    assert len(throttle._events[("g", "")]) == 0              # 上一行 check 已清


def test_host_allowed_public_base_url_edge_shapes():
    """PUBLIC_BASE_URL 无 scheme / path_host 为空时不炸也不误放行。"""
    from src.config import Settings
    from src.main import _host_allowed

    # 无 scheme：startswith 分支不命中，主机仍进白名单
    settings = Settings(_env_file=None, APP_SECRET=SECRET, PUBLIC_BASE_URL="localhost:8000")
    assert _host_allowed("localhost", settings)
    # base 主机为空：path_host 分支不命中，仅默认白名单生效
    empty = Settings(_env_file=None, APP_SECRET=SECRET, PUBLIC_BASE_URL="/oops")
    assert _host_allowed("localhost", empty)
    assert not _host_allowed("evil.example", empty)


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
        APP_SECRET=SECRET,
        DATA_DIR=str(tmp_path),
        ADMIN_USERNAMES="root",
        ALLOWED_HOSTS="api.example.com",
    )
    application = build_app(settings)
    with TestClient(application) as client:
        assert client.get("/health", headers={"Host": "api.example.com"}).status_code == 200
        assert client.get("/health", headers={"Host": "127.0.0.1"}).status_code == 400

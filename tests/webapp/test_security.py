"""安全边界测试（PROPOSAL §6）：登录限流 / CSRF / 请求体上限 / 安全头 / Host 白名单。

每个测试独立 build_app（独立 throttle 与数据库），互不污染窗口计数。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.auth.throttle import LoginThrottle, ThrottleLimits
from src.config import Settings
from src.main import build_app
from tests.helpers.constants import SECRET


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


def test_csrf_custom_header_alone_does_not_bypass(admin_client):
    """`X-Requested-With` 不再豁免同源校验。

    该头此前是「浏览器无法跨源设置」的推论，一旦将来引入允许非同源自定义
    头的 CORS，它立刻变成 CSRF 绕过面。现在只有 Origin/Referer 同源才算数，
    跨源请求带上这个头也必须被拒。
    """
    response = admin_client.post(
        "/api/credentials",
        json={
            "provider": "trae",
            "credential": {"accessToken": "t", "uid": "u", "refreshToken": "r"},
        },
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert response.status_code == 200          # 无 Origin/Referer：非浏览器客户端

    cross = admin_client.post(
        "/api/credentials",
        json={
            "provider": "trae",
            "credential": {"accessToken": "t", "uid": "u", "refreshToken": "r"},
        },
        headers={"X-Requested-With": "XMLHttpRequest", "Origin": "https://evil.example"},
    )
    assert cross.status_code == 403


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
    # content-length 路径：回填声明的字节数与配置上限
    assert response.json()["error"]["message"] == (
        f"request body {len(payload)} bytes exceeds limit 8192 bytes")


def test_chat_body_limit_16mb(app):
    _app, client = app
    oversized = b"{" + b"x" * (16 * 1024 * 1024 + 1) + b"}"
    response = client.post(
        "/v1/chat/completions", content=oversized, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["error"]["message"] == (
        f"request body {len(oversized)} bytes exceeds limit 16777216 bytes")


def test_chunked_body_limit_reports_actual_size(app):
    """分块请求没有 content-length：回填 receive 层累计的实际字节数。

    httpx 对迭代器形式的 body 走 `Transfer-Encoding: chunked`，中间件只能靠
    receive 计数发现超限——这条路径的 size 是实测值，不是声明值。
    """
    _app, client = app

    def chunks():
        yield b"x" * (8 * 1024)      # 刚好到上限，不触发
        yield b"y"                   # 超 1 字节 → 413

    response = client.post(
        "/api/auth/login", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["error"]["message"] == (
        "request body 8193 bytes exceeds limit 8192 bytes")


# -------------------------------------------------------------- 安全响应头


def test_security_headers_present(app):
    _app, client = app
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "same-origin"
    csp = response.headers["content-security-policy"]
    # 默认只允许同源脚本/连接，且不再为内联主题脚本放开 script-src
    assert "default-src 'self'" in csp
    assert "script-src 'self';" in csp
    assert "connect-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    assert "base-uri 'self'" in csp and "form-action 'self'" in csp


def test_csp_allows_jsdelivr_only_when_docs_enabled(tmp_path):
    """Swagger UI 从 jsdelivr 拉资源：只有开 /docs 时才放行该 CDN。"""
    from src.webapp.security import content_security_policy

    off = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    assert "cdn.jsdelivr.net" not in content_security_policy(off)
    on = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                  ENABLE_DOCS="true")
    csp = content_security_policy(on)
    assert "script-src 'self' https://cdn.jsdelivr.net" in csp
    assert "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net" in csp


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
    """过期事件被清理；全局窗口**不再**用于拒绝（防全站登录锁死）。"""
    import time as _time

    from src.auth.throttle import LoginThrottle, ThrottleLimits

    throttle = LoginThrottle(ThrottleLimits(max_global=40, max_per_ip=8, max_per_user=5))
    # 塞一条过期失败记录，check 的 _prune 应清理它（popleft 分支）
    throttle._events[("g", "")].append(_time.monotonic() - 61)
    throttle.check(ip="1.2.3.4", username="alice")            # 不抛

    # 全局窗口满 ≠ 拒绝：未认证攻击者能把它打满，据它拒绝等于远程 DoS
    global_throttle = LoginThrottle(
        ThrottleLimits(max_global=1, max_per_ip=100, max_per_user=100))
    global_throttle.record_failure(ip="9.9.9.9", username="ghost")
    global_throttle.check(ip="1.2.3.4", username="alice")     # 干净 IP 仍放行
    global_throttle.check_transport_windows(ip="1.2.3.4")     # 哈希前同样放行

    # 空用户名的成功登录不清空任何计数
    throttle.record_success(username="")
    assert len(throttle._events[("g", "")]) == 0              # 上一行 check 已清


def test_login_throttle_per_ip_still_blocks():
    """IP 窗口是真正的拒绝判据（全局窗口退出后仍要挡住单来源爆破）。"""
    from src.auth.throttle import LoginThrottle, ThrottledError, ThrottleLimits

    throttle = LoginThrottle(ThrottleLimits(max_per_ip=2, max_per_user=100))
    throttle.record_failure(ip="9.9.9.9", username="a")
    throttle.record_failure(ip="9.9.9.9", username="b")
    with pytest.raises(ThrottledError):
        throttle.check_transport_windows(ip="9.9.9.9")
    with pytest.raises(ThrottledError):
        throttle.check(ip="9.9.9.9", username="c")
    # 同名不同 IP 不受牵连
    throttle.check(ip="8.8.8.8", username="c")


def test_login_throttle_loopback_never_blocked():
    """回环地址永不被限流：反代/隧道下所有请求共用 127.0.0.1。

    限它等于把所有用户塞进同一个桶——哪怕桶本身就是攻击者打满的。
    """
    from src.auth.throttle import LoginThrottle, ThrottleLimits

    for loopback in ("127.0.0.1", "::1", "127.0.0.2"):
        throttle = LoginThrottle(ThrottleLimits(max_per_ip=1, max_per_user=100))
        for _ in range(10):
            throttle.record_failure(ip=loopback, username="x")
        throttle.check_transport_windows(ip=loopback)   # 不抛
        throttle.check(ip=loopback, username="other")   # 用户名窗口也干净


def test_login_throttle_loopback_helper_handles_bad_input():
    """IP 解析失败按「非回环」处理（不能因一条脏数据把所有人当本机放行）。"""
    from src.auth.throttle import _is_loopback

    assert _is_loopback("127.0.0.1") is True
    assert _is_loopback("::1") is True
    assert _is_loopback("10.0.0.1") is False
    assert _is_loopback("") is False
    assert _is_loopback("not-an-ip") is False
    assert _is_loopback("  ::1  ") is True


def test_login_throttle_eviction_keeps_at_limit_keys():
    """容量淘汰不能挤掉已限流的键。

    否则攻击者先撞满目标用户名的 max_per_user，再灌一批唯一用户名把容量
    挤爆，就能通过淘汰把它清零、继续爆破。
    """
    import time as _time

    from src.auth.throttle import LoginThrottle, ThrottleLimits

    throttle = LoginThrottle(ThrottleLimits(max_per_user=2, max_per_ip=100))
    throttle._MAX_KEYS = 3
    now = _time.monotonic()
    # 目标用户名已限流，且它的最近失败时间最早（旧实现会第一个淘汰它）
    throttle._events[("u", "target")].extend([now - 10, now - 9])
    throttle._events[("u", "fresh1")].append(now)
    throttle._events[("u", "fresh2")].append(now)
    throttle._events[("u", "fresh3")].append(now)
    throttle._evict_over_capacity(now)
    assert len(throttle._events) == 3
    assert ("u", "target") in throttle._events
    assert ("u", "fresh1") not in throttle._events


def test_login_throttle_evicts_keys_over_capacity():
    """失败键超上限时的清理：先删过期键，仍超再淘汰最旧的（防内存 DoS）。"""
    import time as _time

    from src.auth.throttle import LoginThrottle

    throttle = LoginThrottle()
    throttle._MAX_KEYS = 2
    # 过期键清理后回到容量内 → 提前返回，不再淘汰
    throttle._events[("u", "stale")].append(_time.monotonic() - 61)
    throttle._events[("g", "")].append(_time.monotonic())
    throttle._events[("i", "1.1.1.1")].append(_time.monotonic())
    throttle._evict_over_capacity(_time.monotonic())
    assert ("u", "stale") not in throttle._events
    assert len(throttle._events) == 2

    # 全是新键、超容量 → 按最近失败时间淘汰最旧的（全局键保留）
    throttle._events[("u", "old")].append(_time.monotonic() - 5)
    throttle._events[("u", "new")].append(_time.monotonic())
    throttle._events[("i", "2.2.2.2")].append(_time.monotonic())
    throttle._evict_over_capacity(_time.monotonic())
    assert len(throttle._events) == 2
    assert ("u", "old") not in throttle._events
    assert ("g", "") in throttle._events


def _request(headers: dict[str, str]):
    """构造只带指定头的 Request（用于 session_cookie_secure 分支测试）。"""
    from starlette.requests import Request

    return Request(scope={"type": "http",
                          "headers": [(k.lower().encode(), v.encode())
                                      for k, v in headers.items()]})


def test_session_cookie_secure_follows_forwarded_proto():
    """有 XFP 时以第一个条目为准；非法值落到 Host 判定分支。"""
    from src.webapp.security import session_cookie_secure

    http_settings = Settings(_env_file=None, APP_SECRET=SECRET,
                             PUBLIC_BASE_URL="http://127.0.0.1:8000")
    assert session_cookie_secure(
        _request({"host": "any.example", "x-forwarded-proto": "https"}),
        http_settings) is True
    assert session_cookie_secure(
        _request({"host": "any.example", "x-forwarded-proto": "http"}),
        http_settings) is False
    # 多值取第一段（隧道叠加多级代理时会追加）
    assert session_cookie_secure(
        _request({"host": "any.example", "x-forwarded-proto": "HTTPS, http"}),
        http_settings) is True
    # 非法协议视为无该头 → 明文部署一律不 Secure
    assert session_cookie_secure(
        _request({"host": "any.example", "x-forwarded-proto": "ftp"}),
        http_settings) is False


def test_session_cookie_secure_without_forwarded_proto():
    """无 XFP：仅当 Host 命中 https 的 PUBLIC_BASE_URL 主机才判 Secure。"""
    from src.webapp.security import session_cookie_secure

    https_settings = Settings(_env_file=None, APP_SECRET=SECRET,
                              PUBLIC_BASE_URL="https://ai-api.robbs.work")
    assert session_cookie_secure(_request({"host": "ai-api.robbs.work"}),
                                 https_settings) is True
    assert session_cookie_secure(_request({"host": "ai-api.robbs.work:443"}),
                                 https_settings) is True
    # 直连其它地址（LAN IP）视为明文
    assert session_cookie_secure(_request({"host": "192.168.124.35:8000"}),
                                 https_settings) is False
    assert session_cookie_secure(_request({}), https_settings) is False
    # https 但 base 主机解析为空（无 scheme / 无 host）→ 不 Secure
    # Settings 现在会拒绝非法形态，这里模拟升级前写进 .env 的坏值
    empty = Settings.model_construct(
        app_secret=SECRET, public_base_url="https:///oops")
    assert session_cookie_secure(_request({"host": "oops"}), empty) is False


def test_host_allowed_public_base_url_edge_shapes():
    """PUBLIC_BASE_URL 无 scheme / path_host 为空时不炸也不误放行。

    非法形态已由 `Settings` 的 field_validator 拒绝；这里用 model_construct
    模拟升级前写入的老 `.env`，确认下游仍能安全降级。
    """
    from src.config import Settings
    from src.main import _host_allowed

    # 无 scheme：startswith 分支不命中，主机仍进白名单
    settings = Settings.model_construct(
        app_secret=SECRET, public_base_url="localhost:8000",
        allowed_hosts="", allow_test_host=False)
    assert _host_allowed("localhost", settings)
    # base 主机为空：path_host 分支不命中，仅默认白名单生效
    empty = Settings.model_construct(
        app_secret=SECRET, public_base_url="/oops",
        allowed_hosts="", allow_test_host=False)
    assert _host_allowed("localhost", empty)
    assert not _host_allowed("evil.example", empty)


def test_host_allowed_ipv6_literals_and_allowlist():
    """IPv6 字面量 Host（`[::1]:8000`）剥括号后与白名单比对。

    旧实现 `split(":")` 会把 `[::1]` 截成空串导致所有 IPv6 主机被 400；
    ALLOWED_HOSTS 里的 IPv6 字面量同样要能命中。
    """
    from src.config import Settings
    from src.main import _host_allowed

    loopback = Settings(_env_file=None, APP_SECRET=SECRET)
    assert _host_allowed("[::1]", loopback) is True
    assert _host_allowed("[::1]:8000", loopback) is True
    configured = Settings(_env_file=None, APP_SECRET=SECRET,
                          ALLOWED_HOSTS="[2001:db8::1],example.com")
    assert _host_allowed("[2001:db8::1]:443", configured) is True
    assert _host_allowed("example.com:8443", configured) is True
    assert _host_allowed("[2001:db8::2]", configured) is False


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


def test_host_allowed_excludes_testserver_unless_enabled():
    """testserver 只在显式开启 ALLOW_TEST_HOST 时放行。

    单标签主机名可能被本地 DNS 搜索域解析到本服务，生产默认放行等于给
    DNS rebinding 留免校验入口。
    """
    from src.config import Settings
    from src.main import _host_allowed

    off = Settings(_env_file=None, APP_SECRET=SECRET, ALLOW_TEST_HOST="false")
    assert _host_allowed("testserver", off) is False
    assert _host_allowed("localhost", off) is True          # 本地回环仍放行
    on = Settings(_env_file=None, APP_SECRET=SECRET, ALLOW_TEST_HOST="true")
    assert _host_allowed("testserver", on) is True


def test_at_limit_returns_false_for_global_key():
    """全局键不参与淘汰降级判定（它始终保留，也不需要「已限流」标记）。"""
    from src.auth.throttle import LoginThrottle

    throttle = LoginThrottle()
    assert throttle._at_limit(("g", "")) is False

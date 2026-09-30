"""Qoder / CodeArts 两条新渠道的共享接线（M0）。

这两条渠道的大部分逻辑各自在 `src/provider/qoder` / `src/provider/codearts`
里单测（见 test_qoder.py / test_codearts.py）。本文件只守住**跨切面接线**：
新增的 Settings 字段、端点白名单校验、以及热更最小间隔——漏一处就会出现
「.env 里设了不生效」或「热更改了不管用」这类静默失效。
"""

from __future__ import annotations

import pytest

from src.config import (
    Settings,
    validate_codearts_endpoint_allowed,
    validate_qoder_endpoint_allowed,
)
from src.runtime_settings import HOT_BY_KEY, RuntimeSettings
from tests.conftest import SECRET


class _MemoryStore:
    """最小 SettingsStore：只实现读写三动作。"""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.rows: dict[str, str] = dict(initial or {})

    def load(self) -> dict[str, str]:
        return dict(self.rows)

    def set(self, key: str, value: str, now: int | None = None) -> None:
        self.rows[key] = value

    def delete(self, key: str) -> None:
        self.rows.pop(key, None)


def _settings(**overrides) -> Settings:
    defaults = {"_env_file": None, "APP_SECRET": SECRET, "DATA_DIR": "./data"}
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


@pytest.fixture()
def admin_client(tmp_path):
    """带 admin 会话的 TestClient（局部定义：本文件不复用他处 fixture）。"""
    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token
    from src.main import build_app

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    client = TestClient(app)
    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    with client:
        yield app, client


# --------------------------------------------------------------- Settings 字段

def test_qoder_defaults_and_allowed_parsing():
    settings = _settings()
    assert settings.qoder_api_endpoint == "https://openapi.qoder.com.cn"
    assert settings.qoder_gateway_endpoint == "https://gateway.qoder.com.cn"
    # 白名单去空白并去掉结尾斜杠；国内版两个域名都在
    assert settings.qoder_allowed == (
        "https://openapi.qoder.com.cn",
        "https://gateway.qoder.com.cn",
        "https://openapi.qoder.sh",
        "https://api1.qoder.sh",
    )


def test_codearts_defaults_and_allowed_parsing():
    settings = _settings()
    assert settings.codearts_api_endpoint == (
        "https://snap-access.cn-north-4.myhuaweicloud.com")
    assert settings.codearts_allowed == (
        "https://snap-access.cn-north-4.myhuaweicloud.com",
        "https://sts.cn-north-4.myhuaweicloud.com",
        "https://opengw.developer.huaweicloud.com",
        "https://codearts.huaweicloud.com",
    )


def test_allowed_parsing_ignores_blank_entries():
    settings = _settings(qoder_allowed_endpoints=" https://a.example.com/ , , ")
    assert settings.qoder_allowed == ("https://a.example.com",)
    assert _settings(codearts_allowed_endpoints="").codearts_allowed == ()


# --------------------------------------------------------------- 端点白名单

def test_qoder_endpoint_whitelist():
    settings = _settings()
    assert validate_qoder_endpoint_allowed("https://openapi.qoder.com.cn", settings)
    # 尾斜杠归一后同等通过
    assert validate_qoder_endpoint_allowed("https://openapi.qoder.com.cn/", settings)
    assert validate_qoder_endpoint_allowed("https://api1.qoder.sh", settings)
    assert not validate_qoder_endpoint_allowed("https://evil.example.com", settings)


def test_codearts_endpoint_whitelist():
    settings = _settings()
    base = "https://snap-access.cn-north-4.myhuaweicloud.com"
    assert validate_codearts_endpoint_allowed(base, settings)
    assert validate_codearts_endpoint_allowed(base + "/", settings)
    assert validate_codearts_endpoint_allowed(
        "https://sts.cn-north-4.myhuaweicloud.com", settings)
    assert not validate_codearts_endpoint_allowed("https://evil.example.com", settings)


# --------------------------------------------------------------- 热更最小间隔

def test_qoder_codearts_min_intervals_are_hot():
    for key in ("qoder_chat_min_interval", "codearts_chat_min_interval"):
        assert key in HOT_BY_KEY
    runtime = RuntimeSettings(_settings(), _MemoryStore())
    # 默认 5s，独立于其它渠道
    assert runtime.qoder_chat_min_interval == 5
    assert runtime.codearts_chat_min_interval == 5
    runtime.set("qoder_chat_min_interval", 0)
    runtime.set("codearts_chat_min_interval", 2.5)
    assert runtime.qoder_chat_min_interval == 0
    assert runtime.codearts_chat_min_interval == 2.5


def test_qoder_codearts_min_intervals_reject_negative():
    from src.runtime_settings import InvalidSetting

    runtime = RuntimeSettings(_settings(), _MemoryStore())
    with pytest.raises(InvalidSetting):
        runtime.set("qoder_chat_min_interval", -1)
    with pytest.raises(InvalidSetting):
        runtime.set("codearts_chat_min_interval", -1)


# ----------------------------------------------------- main.py 端点装配校验

def test_qoder_endpoint_resolvers_accept_defaults_and_reject_evil():
    from src.main import _qoder_gateway, _qoder_host

    settings = _settings()
    assert _qoder_host(settings) == "https://openapi.qoder.com.cn"
    assert _qoder_gateway(settings) == "https://gateway.qoder.com.cn"

    bad_host = _settings(qoder_api_endpoint="https://evil.example.com")
    with pytest.raises(ValueError, match="QODER_ALLOWED_ENDPOINTS"):
        _qoder_host(bad_host)

    bad_gateway = _settings(qoder_gateway_endpoint="https://evil.example.com")
    with pytest.raises(ValueError, match="QODER_ALLOWED_ENDPOINTS"):
        _qoder_gateway(bad_gateway)


def test_codearts_endpoint_resolver_accepts_default_and_rejects_evil():
    from src.main import _codearts_endpoint

    assert _codearts_endpoint(_settings()) == (
        "https://snap-access.cn-north-4.myhuaweicloud.com")
    bad = _settings(codearts_api_endpoint="https://evil.example.com")
    with pytest.raises(ValueError, match="CODEARTS_ALLOWED_ENDPOINTS"):
        _codearts_endpoint(bad)


# --------------------------------------------------------- poll 轨道装配

def test_upstream_auth_registers_qoder_and_codearts_tracks():
    """四渠道都能 start/poll：Qoder 与 CodeArts 各挂一个 poll 轨道对象。"""
    from src.main import _upstream_auth
    from src.provider.codearts import CodeArtsProvider
    from src.provider.codearts.client import CodeArtsClient
    from src.provider.codearts.oauth import CodeArtsOAuth
    from src.provider.qoder import QoderProvider
    from src.provider.qoder.auth import QoderOAuth
    from src.provider.qoder.client import QoderClient

    registry = {
        "qoder": QoderProvider(client=QoderClient(
            host="https://openapi.qoder.com.cn",
            gateway="https://gateway.qoder.com.cn")),
        "codearts": CodeArtsProvider(client=CodeArtsClient(
            endpoint="https://snap-access.cn-north-4.myhuaweicloud.com")),
    }
    flows = _upstream_auth(registry, _settings())
    assert isinstance(flows["qoder"], QoderOAuth)
    assert isinstance(flows["codearts"], CodeArtsOAuth)
    # 未注册的渠道不产生轨道（防御：registry 缺 key 时不应抛）
    assert _upstream_auth({}, _settings()) == {}


async def test_codearts_oauth_start_poll_and_cancel_loop():
    """CodeArts poll 轨道：start 出 auth_url（flow=paste），poll 就绪后落库。"""
    import httpx

    from src.provider.codearts import auth as codearts_auth
    from src.provider.codearts.oauth import AuthStateStore, CodeArtsOAuth

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "/v1/login/ticket" in request.url.path:
            return httpx.Response(200, json={
                "user_name": "alice", "user_id": "u1",
                "refresh_token": "rt", "credentials": {
                    "access_key_id": "ak", "secret_access_key": "sk",
                    "security_token": "st", "expiration": "2030-01-01T00:00:00Z"}})
        raise AssertionError(f"unexpected {request.url}")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, timeout=None)
    oauth = CodeArtsOAuth(codearts_auth.LoginConfig(
        portal_host="https://portal.test"),
        client=client, store=AuthStateStore())
    try:
        session = await oauth.start("alice")
        # CodeArts 门户走回调通道，ticket 轮询对服务端无效 → flow=paste
        assert session.flow == "paste"
        assert session.auth_url.startswith("https://portal.test/authorize?")
        assert session.interval is None
        result = await oauth.poll(session.state, "alice")
        assert result is not None
        assert result.credential_data["access_key_id"] == "ak"
        assert result.credential_data["refresh_token"] == "rt"
        # 成功后 state 被消费，重用即报错
        with pytest.raises(ValueError):
            await oauth.poll(session.state, "alice")
    finally:
        await client.aclose()
        await oauth.aclose()
    assert any("/v1/login/ticket" in url for url in calls)


async def test_codearts_oauth_poll_pending_returns_none():
    """ticket 未就绪（无凭证）时 poll 返回 None，state 不被消费。"""
    import httpx

    from src.provider.codearts import auth as codearts_auth
    from src.provider.codearts.oauth import CodeArtsOAuth

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"status": "not_logged_in"}))
    client = httpx.AsyncClient(transport=transport, timeout=None)
    oauth = CodeArtsOAuth(codearts_auth.LoginConfig(portal_host="https://portal.test"),
                          client=client)
    try:
        session = await oauth.start("alice")
        assert await oauth.poll(session.state, "alice") is None
        # 归属校验：别的用户拿同一个 state 轮询 → 认不出
        assert oauth.store.owner(session.state, "bob") is False
        # cancel 消费 state
        assert oauth.store.cancel(session.state, "alice") is True
        assert oauth.store.owner(session.state, "alice") is False
    finally:
        await client.aclose()
        await oauth.aclose()


async def test_codearts_oauth_store_and_client_edge_branches():
    """store 过期清理 / 非归属消费，以及 client 惰性创建与竞争失败分支。"""
    import httpx

    from src.provider.codearts import auth as codearts_auth
    from src.provider.codearts.oauth import AuthStateStore, CodeArtsOAuth

    config = codearts_auth.LoginConfig(portal_host="https://portal.test")

    # cleanup：TTL 内保留，过期后删除
    store = AuthStateStore(ttl_seconds=10)
    session = codearts_auth.new_login_session(config, port=1)
    state = store.begin("alice", session, now=100)
    store.cleanup(now=105)
    assert store.owner(state, "alice") is True
    store.cleanup(now=111)
    assert store.owner(state, "alice") is False
    # consume 非归属者 → False
    assert store.consume("ghost", "alice") is False

    # 未注入 client：aclose 在 None 时直接返回；_http 惰性创建（不触网）
    lazy = CodeArtsOAuth(config)
    await lazy.aclose()
    assert lazy._http is not None
    await lazy.aclose()

    # consume 竞争失败（state 在轮询间隙被别处消费）→ 明确报错
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "user_name": "alice", "refresh_token": "rt", "credentials": {
            "access_key_id": "ak", "secret_access_key": "sk",
            "security_token": "st", "expiration": "2030-01-01T00:00:00Z"}}))
    client = httpx.AsyncClient(transport=transport, timeout=None)
    oauth = CodeArtsOAuth(config, client=client)
    try:
        started = await oauth.start("alice")
        oauth.store.consume = lambda *a, **k: False  # type: ignore[method-assign]
        with pytest.raises(ValueError, match="consumed concurrently"):
            await oauth.poll(started.state, "alice")
    finally:
        await client.aclose()
        await oauth.aclose()


# ------------------------------------------------- CodeArts 粘贴回调完成登录


def test_extract_authorization_code_accepts_url_query_and_bare_code():
    from src.provider.codearts.oauth import extract_authorization_code

    assert extract_authorization_code(
        "http://127.0.0.1:12800/oauth/callback?code=abc123&state=xyz") == "abc123"
    assert extract_authorization_code("code=abc123&state=xyz") == "abc123"
    assert extract_authorization_code("abc123") == "abc123"
    assert extract_authorization_code("") == ""
    assert extract_authorization_code("http://127.0.0.1/cb?state=only") == ""


async def test_codearts_oauth_complete_callback_exchanges_code():
    """粘贴回调链接 → exchange_code 换 token → 落库扁平凭证。"""
    import httpx

    from src.provider.codearts import auth as codearts_auth
    from src.provider.codearts.oauth import CodeArtsOAuth

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert request.url.path.endswith("/v1/oauth2/tokens")
        return httpx.Response(200, json={
            "user_name": "alice", "user_id": "u1", "refresh_token": "rt",
            "credentials": {"access_key_id": "ak", "secret_access_key": "sk",
                            "security_token": "st",
                            "expiration": "2030-01-01T00:00:00Z"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None)
    oauth = CodeArtsOAuth(codearts_auth.LoginConfig(portal_host="https://portal.test"),
                          client=client)
    try:
        started = await oauth.start("alice")
        result = await oauth.complete_callback(
            "http://127.0.0.1:12800/oauth/callback?code=THE_CODE&state=ignored",
            started.state, "alice")
        assert result.credential_data["access_key_id"] == "ak"
        assert result.credential_data["dpop_private_jwk"]
        # 已消费：再次完成报错
        with pytest.raises(ValueError, match="unknown or consumed"):
            await oauth.complete_callback("http://x/cb?code=c", started.state, "alice")
    finally:
        await client.aclose()
        await oauth.aclose()
    assert seen, "exchange_code should have hit the token endpoint"


async def test_codearts_oauth_complete_callback_missing_code():
    import httpx

    from src.provider.codearts import auth as codearts_auth
    from src.provider.codearts.oauth import CodeArtsOAuth

    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={})), timeout=None)
    oauth = CodeArtsOAuth(codearts_auth.LoginConfig(portal_host="https://portal.test"),
                          client=client)
    try:
        started = await oauth.start("alice")
        with pytest.raises(ValueError, match="missing the authorization code"):
            await oauth.complete_callback("http://127.0.0.1/cb?state=only",
                                          started.state, "alice")
    finally:
        await client.aclose()
        await oauth.aclose()


async def test_codearts_oauth_poll_ticket_error_becomes_protocol_violation():
    """ticket 通道被判无效（上游 400）→ 受控 ProtocolViolation（不是裸 500）。"""
    import httpx

    from src.provider.codearts import auth as codearts_auth
    from src.provider.codearts.oauth import CodeArtsOAuth

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error_code": "TM.00001001",
                                         "error_msg": "无效ticketId: deadbeef"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None)
    oauth = CodeArtsOAuth(codearts_auth.LoginConfig(portal_host="https://portal.test"),
                          client=client)
    try:
        started = await oauth.start("alice")
        with pytest.raises(ValueError, match="TM.00001001"):
            await oauth.poll(started.state, "alice")
    finally:
        await client.aclose()
        await oauth.aclose()


async def test_codearts_oauth_complete_callback_exchange_error_is_controlled():
    """exchange_code 被上游拒（400）→ 受控 ProtocolViolation（不是裸 500）。"""
    import httpx

    from src.provider.codearts import auth as codearts_auth
    from src.provider.codearts.oauth import CodeArtsOAuth

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=b'{"error_code":"STS5.1806","error_msg":"bad code"}')

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None)
    oauth = CodeArtsOAuth(codearts_auth.LoginConfig(portal_host="https://portal.test"),
                          client=client)
    try:
        started = await oauth.start("alice")
        with pytest.raises(ValueError, match="STS5.1806"):
            await oauth.complete_callback("http://127.0.0.1/cb?code=abc",
                                          started.state, "alice")
    finally:
        await client.aclose()
        await oauth.aclose()


def test_upstream_auth_complete_endpoint_registered_and_guarded(admin_client):
    """粘贴完成端点：非支持渠道 400，admin 可调用（用伪造 oauth 验证落库路径）。"""
    from src.provider.base import AuthResult

    app, client = admin_client

    class _FakeOAuth:
        def __init__(self):
            from src.provider.codebuddy.oauth import AuthStateStore

            self.store = AuthStateStore()
            self.state = self.store.begin("root", "upstream")

        async def complete_callback(self, url, state, username):
            assert url == "http://127.0.0.1/cb?code=abc"
            return AuthResult(credential_data={"access_key_id": "ak",
                                               "secret_access_key": "sk",
                                               "security_token": "st"},
                              nickname="alice")

    fake = _FakeOAuth()
    app.state.upstream_auth["codearts"] = fake
    app.state.services.upstream_auth["codearts"] = fake
    response = client.post("/api/auth/upstream/complete", json={
        "provider": "codearts", "state": fake.state, "url": "http://127.0.0.1/cb?code=abc"})
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    # 未注册该能力的渠道 → 400
    assert client.post("/api/auth/upstream/complete", json={
        "provider": "unknown", "state": "x", "url": "y"}).status_code == 400


def test_upstream_auth_violations_map_to_400(admin_client):
    """qoder/codearts 的 UpstreamProtocolViolation 必须映射成受控 400 而非 500。"""
    from src.provider.codearts.events import (
        UpstreamProtocolViolation as CodeArtsViolation,
    )
    from src.provider.qoder.events import (
        UpstreamProtocolViolation as QoderViolation,
    )

    app, client = admin_client

    class _QoderOAuth:
        store = None

        async def poll(self, state, username):
            raise QoderViolation("qoder bad state")

        async def complete_callback(self, url, state, username):
            raise QoderViolation("qoder bad callback")

    class _CodeArtsOAuth:
        store = None

        async def poll(self, state, username):
            raise CodeArtsViolation("codearts bad state")

        async def complete_callback(self, url, state, username):
            raise CodeArtsViolation("codearts bad callback")

    app.state.upstream_auth["qoder"] = _QoderOAuth()
    app.state.services.upstream_auth["qoder"] = app.state.upstream_auth["qoder"]
    app.state.upstream_auth["codearts"] = _CodeArtsOAuth()
    app.state.services.upstream_auth["codearts"] = app.state.upstream_auth["codearts"]

    qoder = client.post("/api/auth/upstream/poll",
                        json={"provider": "qoder", "state": "s"})
    assert qoder.status_code == 400
    codearts = client.post("/api/auth/upstream/complete", json={
        "provider": "codearts", "state": "s", "url": "u"})
    assert codearts.status_code == 400

"""CodeArts OAuth 轨道：轮询/取消、粘贴回调完成登录与身份回填。"""

from __future__ import annotations

import pytest


class _IdentityClient:
    """登录补身份的替身：caller_identity / current_user 各可配返回值或异常。"""

    def __init__(self, *, caller=("u1", "robbsluo", "d1"), caller_error=None,
                 current=("u1", "robbsluo", "d1"), current_error=None) -> None:
        self._caller = caller
        self._caller_error = caller_error
        self._current = current
        self._current_error = current_error
        self.calls: list[str] = []

    async def caller_identity(self, _credential):
        self.calls.append("caller_identity")
        if self._caller_error is not None:
            raise self._caller_error
        return self._caller

    async def current_user(self, _credential):
        self.calls.append("current_user")
        if self._current_error is not None:
            raise self._current_error
        return self._current

def _exchange_tokens(*, user_name="", user_id=""):
    return {"user_name": user_name, "user_id": user_id, "refresh_token": "rt",
            "credentials": {"access_key_id": "ak", "secret_access_key": "sk",
                            "security_token": "st",
                            "expiration": "2030-01-01T00:00:00Z"}}

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
    assert store.owner(state, "alice", now=105) is True
    store.cleanup(now=111)
    assert store.owner(state, "alice", now=111) is False
    # owner 惰性判过期（未触发 cleanup）
    lazy = AuthStateStore(ttl_seconds=10)
    stale = lazy.begin("alice", codearts_auth.new_login_session(config, port=1), now=0)
    assert lazy.owner(stale, "alice", now=10) is False
    assert stale not in lazy._entries
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

def test_parse_first_stage_callback_shapes():
    """issue #4：首次回调 secret+redirect 识别；其他形态一律不算。"""
    from src.provider.codearts.oauth import parse_first_stage_callback

    secret, redirect = parse_first_stage_callback(
        "http://127.0.0.1/oauth/callback?secret=" + "a" * 64
        + "&redirect=https%3A%2F%2Fportal.test%2Flogin%3FauthorizationAvailable%3Dfalse")
    assert secret == "a" * 64
    assert redirect == "https://portal.test/login?authorizationAvailable=false"
    # 带 code 的第二次回调不算
    assert parse_first_stage_callback(
        "http://127.0.0.1:12800/oauth/callback?code=c&state=s") == ("", "")
    # 缺 redirect / 缺 secret / 裸 code / 空串都不算
    assert parse_first_stage_callback("http://x/cb?secret=abc") == ("", "")
    assert parse_first_stage_callback("http://x/cb?redirect=https%3A%2F%2Fx") == ("", "")
    assert parse_first_stage_callback("barecode") == ("", "")
    assert parse_first_stage_callback("") == ("", "")

async def test_codearts_oauth_first_stage_callback_gives_guidance_and_portal_secret():
    """issue #4：粘回首次回调 → 可行动指引（不是光秃秃缺 code）+ 记下门户 secret。"""
    import httpx

    from src.provider.codearts import auth as codearts_auth
    from src.provider.codearts.oauth import CodeArtsOAuth

    login_page = "https://portal.test/login?authorizationAvailable=false"
    first_stage = ("http://127.0.0.1/oauth/callback?secret=" + "f" * 64
                   + "&redirect=" + login_page.replace(":", "%3A")
                   .replace("/", "%2F").replace("?", "%3F"))
    seen_secret: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_secret.append(request.url.params.get("secret", ""))
        return httpx.Response(400, json={"error_code": "TM.00001001",
                                         "error_msg": "无效ticketId"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None)
    oauth = CodeArtsOAuth(codearts_auth.LoginConfig(portal_host="https://portal.test"),
                          client=client)
    try:
        started = await oauth.start("alice")
        with pytest.raises(ValueError) as exc_info:
            await oauth.complete_callback(first_stage, started.state, "alice")
        message = str(exc_info.value)
        assert "首次回调" in message
        assert login_page in message              # 指引里带解码后的登录页 URL
        assert "code" in message
        # state 未被消费，且后续 poll 用门户 secret（不是本地生成的）
        poll_state = oauth.store.session_entry(started.state, "alice")
        assert poll_state is not None
        assert poll_state.portal_secret == "f" * 64
        with pytest.raises(ValueError, match="TM.00001001"):
            await oauth.poll(started.state, "alice")
        assert seen_secret[-1] == "f" * 64
    finally:
        await client.aclose()
        await oauth.aclose()

async def test_codearts_oauth_poll_uses_local_secret_without_first_stage():
    """未收到首次回调时 poll 仍用本地 secret（旧行为不变）。"""
    import httpx

    from src.provider.codearts import auth as codearts_auth
    from src.provider.codearts.oauth import CodeArtsOAuth

    seen_secret: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_secret.append(request.url.params.get("secret", ""))
        return httpx.Response(400, json={"error_code": "TM.00001001",
                                         "error_msg": "无效ticketId"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None)
    oauth = CodeArtsOAuth(codearts_auth.LoginConfig(portal_host="https://portal.test"),
                          client=client)
    try:
        started = await oauth.start("alice")
        entry = oauth.store.session_entry(started.state, "alice")
        assert entry is not None
        local_secret = entry.session.secret
        with pytest.raises(ValueError, match="TM.00001001"):
            await oauth.poll(started.state, "alice")
        assert seen_secret[-1] == local_secret
    finally:
        await client.aclose()
        await oauth.aclose()

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

async def test_codearts_oauth_complete_fills_identity_when_tokens_lack_name():
    """token 响应不带用户名 → 登录后用签名身份接口补 uid/user_name/nickname。"""
    import httpx

    from src.provider.codearts import auth as codearts_auth
    from src.provider.codearts.oauth import CodeArtsOAuth

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=_exchange_tokens()))
    client = httpx.AsyncClient(transport=transport, timeout=None)
    identity = _IdentityClient()
    oauth = CodeArtsOAuth(codearts_auth.LoginConfig(portal_host="https://portal.test"),
                          client=client, identity_client=identity)
    try:
        started = await oauth.start("alice")
        result = await oauth.complete_callback(
            "http://127.0.0.1:12800/oauth/callback?code=THE_CODE",
            started.state, "alice")
        assert result.nickname == "robbsluo"
        assert result.credential_data["uid"] == "u1"
        assert result.credential_data["user_name"] == "robbsluo"
        assert result.credential_data["nickname"] == "robbsluo"
    finally:
        await client.aclose()
        await oauth.aclose()
    assert identity.calls == ["caller_identity"]  # 第一个成功就不再试第二个

async def test_codearts_oauth_identity_falls_back_to_current_user():
    """caller_identity 按区域不可用 → 换 current_user；token 自带名则不覆盖。"""
    import httpx

    from src.provider.base import UpstreamHTTPError
    from src.provider.codearts import auth as codearts_auth
    from src.provider.codearts.oauth import CodeArtsOAuth

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=_exchange_tokens(
            user_name="alice", user_id="u0")))
    client = httpx.AsyncClient(transport=transport, timeout=None)
    identity = _IdentityClient(caller_error=UpstreamHTTPError(400, b"APIGW.0101"))
    oauth = CodeArtsOAuth(codearts_auth.LoginConfig(portal_host="https://portal.test"),
                          client=client, identity_client=identity)
    try:
        started = await oauth.start("alice")
        result = await oauth.complete_callback(
            "http://127.0.0.1:12800/oauth/callback?code=THE_CODE",
            started.state, "alice")
        # token 自带的身份优先，只补缺失项（身份接口的 u1 不覆盖 u0）
        assert result.nickname == "alice"
        assert result.credential_data["user_name"] == "alice"
        assert result.credential_data["uid"] == "u0"
    finally:
        await client.aclose()
        await oauth.aclose()
    assert identity.calls == ["caller_identity", "current_user"]

async def test_codearts_oauth_identity_failure_does_not_block_login():
    """两个身份接口都失败 → 登录照常成功，昵称为空（展示层回退 id 前缀）。"""
    import httpx

    from src.provider.base import UpstreamHTTPError
    from src.provider.codearts import auth as codearts_auth
    from src.provider.codearts.oauth import CodeArtsOAuth

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=_exchange_tokens()))
    client = httpx.AsyncClient(transport=transport, timeout=None)
    identity = _IdentityClient(caller_error=UpstreamHTTPError(400, b"no"),
                               current_error=UpstreamHTTPError(500, b"no"))
    oauth = CodeArtsOAuth(codearts_auth.LoginConfig(portal_host="https://portal.test"),
                          client=client, identity_client=identity)
    try:
        started = await oauth.start("alice")
        result = await oauth.complete_callback(
            "http://127.0.0.1:12800/oauth/callback?code=THE_CODE",
            started.state, "alice")
        assert result.nickname == ""
        assert result.credential_data["access_key_id"] == "ak"
    finally:
        await client.aclose()
        await oauth.aclose()

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
    # OAuth 落库必须写审计：否则「谁什么时候把号塞进池子」在审计页上查不到
    # （手工导入 `credential.import` 一直有，登录进来的号原来完全缺失）。
    credential_id = response.json()["credential_id"]
    rows = app.state.services.audit.query(action="credential.import", limit=10)
    assert any(row["target"] == credential_id
               and "OAuth" in (row["detail"] or "") for row in rows)
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

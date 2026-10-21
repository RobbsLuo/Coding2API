"""officeAce 登录闭环：state 登记、轮询取码、STS 换票、开通校验、刷新。"""

from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import parse_qs, unquote

import httpx
import pytest

from src.provider.base import AuthFlow, UpstreamProtocolViolation
from src.provider.codearts import dpop
from src.provider.officeace import events as officeace_events
from src.provider.officeace.credential import OfficeAceCredential
from src.provider.officeace.oauth import (
    AuthStateStore,
    LoginConfig,
    LoginSession,
    OfficeAceOAuth,
    TokenEndpointError,
    _sdk_date,
    pkce_pair,
)
from tests.provider.officeace._support import MOMENT, login_config

CLOUD = "https://cloud.test"
STS = "https://sts.test"


def _session(**over) -> LoginSession:
    base = {
        "state": "cloud-state",
        "code_verifier": "verifier",
        "code_challenge": "challenge",
        "auth_url": "https://auth.test/authorize",
        "dpop_private_jwk": dpop.new_private_jwk(),
        "created_at": MOMENT,
    }
    base.update(over)
    return LoginSession(**base)


def _tokens_response(**over) -> httpx.Response:
    payload = {
        "credentials": {"access_key_id": "AK2", "secret_access_key": "SK2",
                        "security_token": "TOK2", "project_id": "pid2",
                        "expiration": 1999999999},
        "refresh_token": "rt2", "user_id": "prin-2",
    }
    payload.update(over)
    return httpx.Response(200, json=payload)


def _permission_response(**over) -> httpx.Response:
    payload = {
        "account_id": "acct", "principal_id": "prin",
        "subscription": {"model_info": {
            "model_api_url_base": "gw.test",
            "model_auth_info": {"model_app_key": "K", "model_app_secret": "S"},
        }},
    }
    payload.update(over)
    return httpx.Response(200, json=payload)


def _flow(*, state: httpx.Response | None = None,
          code: httpx.Response | None = None,
          tokens: httpx.Response | None = None,
          permission: httpx.Response | None = None,
          seen: dict | None = None):
    """按路径分发的 handler：state / code / tokens / permission-validate。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen[request.url.path] = request
        path = request.url.path
        if path.endswith("/auth/state"):
            return state if state is not None else httpx.Response(
                200, json={"state": "cloud-state"})
        if path.endswith("/auth/code"):
            return code if code is not None else httpx.Response(
                200, json={"code": "the-code"})
        if path.endswith("/oauth2/tokens"):
            return tokens if tokens is not None else _tokens_response()
        return permission if permission is not None else _permission_response()
    return handler


def _oauth(handler, *, store: AuthStateStore | None = None,
           now: int = MOMENT, **kw) -> OfficeAceOAuth:
    kw.setdefault("config", login_config())
    transport = httpx.MockTransport(handler)
    return OfficeAceOAuth(
        client=httpx.AsyncClient(transport=transport, timeout=None),
        store=store, now=lambda: now, **kw)


async def _started(handler, **kw) -> tuple[OfficeAceOAuth, str]:
    oauth = _oauth(handler, **kw)
    session = await oauth.start("alice")
    return oauth, session.state


# ------------------------------------------------------------------ PKCE

def test_pkce_pair_derives_challenge():
    verifier, challenge = pkce_pair()
    assert len(verifier) >= 43
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_pkce_pair_accepts_fixed_verifier():
    assert pkce_pair("abc")[0] == "abc"


def test_login_config_urls():
    config = LoginConfig(cloud_base="https://cloud.test/", sts_host=STS)
    assert config.token_url == f"{STS}/v1/oauth2/tokens"
    assert config.redirect_uri == f"{CLOUD}/v1/claw/auth/callback"


def test_sdk_date_is_compact_utc():
    assert _sdk_date(MOMENT) == "20261021T013052Z"


# ------------------------------------------------------------------ store

def test_store_begin_and_owner():
    store = AuthStateStore()
    state = store.begin("alice", _session(), now=MOMENT)
    assert state != "cloud-state"           # 对外键是本地 reservation
    assert store.owner(state, "alice", now=MOMENT) is True
    assert store.owner(state, "bob", now=MOMENT) is False
    assert store.owner("nope", "alice", now=MOMENT) is False
    assert len(store) == 1


def test_store_expires_after_ttl():
    store = AuthStateStore(ttl_seconds=10)
    state = store.begin("alice", _session(), now=MOMENT)
    assert store.owner(state, "alice", now=MOMENT + 9) is True
    assert store.owner(state, "alice", now=MOMENT + 10) is False
    assert len(store) == 0


def test_store_session_entry_returns_reservation():
    store = AuthStateStore()
    state = store.begin("alice", _session(), now=MOMENT)
    entry = store.session_entry(state, "alice", now=MOMENT)
    assert entry is not None and entry.username == "alice"
    assert store.session_entry(state, "bob", now=MOMENT) is None


def test_store_touch_counts_polls_and_tolerates_missing():
    store = AuthStateStore()
    state = store.begin("alice", _session(), now=MOMENT)
    store.touch(state, now=MOMENT)
    store.touch(state, now=MOMENT)
    entry = store.session_entry(state, "alice", now=MOMENT)
    assert entry is not None and entry.session.polls == 2
    store.touch("missing", now=MOMENT)


def test_store_consume_is_single_use():
    store = AuthStateStore()
    state = store.begin("alice", _session(), now=MOMENT)
    assert store.consume(state, "alice", now=MOMENT) is True
    assert store.consume(state, "alice", now=MOMENT) is False


def test_store_cancel_aliases_consume():
    store = AuthStateStore()
    state = store.begin("alice", _session(), now=MOMENT)
    assert store.cancel(state, "alice", now=MOMENT) is True
    assert store.cancel(state, "alice", now=MOMENT) is False


def test_store_evicts_oldest_when_full():
    """不限量会让误点攒出几十条 pending 会话，等于拿用户账号压华为云。"""
    store = AuthStateStore(max_pending=2)
    first = store.begin("a", _session(created_at=MOMENT), now=MOMENT)
    store.begin("b", _session(created_at=MOMENT), now=MOMENT)
    store.begin("c", _session(created_at=MOMENT), now=MOMENT)
    assert len(store) == 2
    assert store.session_entry(first, "a", now=MOMENT) is None


def test_store_cleanup_drops_expired_entries():
    store = AuthStateStore(ttl_seconds=10)
    store.begin("a", _session(), now=MOMENT)
    store.cleanup(now=MOMENT + 100)
    assert len(store) == 0


# ------------------------------------------------------------------ 发起登录

async def test_start_returns_poll_session():
    oauth = _oauth(_flow())
    session = await oauth.start("alice")
    assert session.flow is AuthFlow.POLL
    assert session.interval == int(officeace_events.POLL_INTERVAL_SECONDS)
    assert session.callback_url is None
    assert "authui/login.html?service=" in session.auth_url
    # 内层被整段 percent-encode（service 参数），故解一层再断言
    assert "state=cloud-state" in unquote(session.auth_url)
    assert len(oauth.store) == 1
    await oauth.aclose()


async def test_start_posts_to_auth_state_endpoint():
    seen: dict = {}
    await _oauth(_flow(seen=seen)).start("alice")
    request = seen["/v1/claw/auth/state"]
    assert request.method == "POST"


async def test_start_force_login_wraps_logout():
    oauth = _oauth(_flow())
    session = await oauth.start("alice", force_login=True)
    assert "authui/logout?service=" in session.auth_url


async def test_start_rejects_http_error():
    oauth = _oauth(_flow(state=httpx.Response(500, content=b"boom")))
    with pytest.raises(UpstreamProtocolViolation) as caught:
        await oauth.start("alice")
    assert "申请登录状态" in caught.value.public_message


async def test_start_rejects_non_json():
    oauth = _oauth(_flow(state=httpx.Response(200, text="<html>")))
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.start("alice")


@pytest.mark.parametrize("body", [{}, {"state": ""}, {"state": 5}, [1]])
async def test_start_rejects_missing_state(body):
    oauth = _oauth(_flow(state=httpx.Response(200, json=body)))
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.start("alice")


# ------------------------------------------------------------------ 轮询

async def test_poll_returns_none_while_waiting():
    oauth, state = await _started(_flow(code=httpx.Response(
        officeace_events.CODE_NOT_READY, json={"code": "AgentArts.11000009"})))
    assert await oauth.poll(state, "alice") is None
    assert len(oauth.store) == 1
    await oauth.aclose()


async def test_poll_unknown_state_raises():
    oauth = _oauth(_flow())
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.poll("nope", "alice")


async def test_poll_rejects_foreign_owner():
    """归属校验是安全约束：别的管理员不能消费这条登录。"""
    oauth, state = await _started(_flow())
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.poll(state, "bob")


async def test_poll_404_raises_actionable_error():
    oauth, state = await _started(_flow(code=httpx.Response(404, content=b"nope")))
    with pytest.raises(UpstreamProtocolViolation) as caught:
        await oauth.poll(state, "alice")
    assert "重新发起登录" in str(caught.value)


@pytest.mark.parametrize("status", [400, 500, 503])
async def test_poll_transient_error_keeps_waiting(status):
    """云端偶发抖动不该变成「登录失败，请重来」。"""
    oauth, state = await _started(_flow(code=httpx.Response(status, content=b"x")))
    assert await oauth.poll(state, "alice") is None


async def test_poll_rejects_non_json_code_response():
    oauth, state = await _started(_flow(code=httpx.Response(200, text="<html>")))
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.poll(state, "alice")


async def test_poll_rejects_non_object_code_response():
    oauth, state = await _started(_flow(code=httpx.Response(200, json=[1])))
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.poll(state, "alice")


async def test_poll_rejects_error_envelope():
    oauth, state = await _started(_flow(code=httpx.Response(200, json={
        "error": "access_denied", "error_msg": "用户拒绝授权"})))
    with pytest.raises(UpstreamProtocolViolation) as caught:
        await oauth.poll(state, "alice")
    assert "授权被拒绝" in caught.value.public_message


async def test_poll_error_envelope_without_message():
    oauth, state = await _started(_flow(code=httpx.Response(
        200, json={"error": "denied"})))
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.poll(state, "alice")


async def test_poll_missing_code_keeps_waiting(caplog):
    oauth, state = await _started(_flow(code=httpx.Response(200, json={"ok": True})))
    with caplog.at_level("WARNING"):
        assert await oauth.poll(state, "alice") is None
    assert "缺少 code" in caplog.text


async def test_poll_accepts_authorization_code_alias():
    """两种写法都见过：`code` 与 `authorization_code`。"""
    oauth, state = await _started(_flow(code=httpx.Response(
        200, json={"authorization_code": "the-code"})))
    result = await oauth.poll(state, "alice")
    assert result is not None


async def test_poll_success_builds_credential():
    oauth, state = await _started(_flow())
    result = await oauth.poll(state, "alice")
    assert result is not None
    # 无 id_token / user_name 时昵称退到 account_id，而不是留空
    assert result.nickname == "acct"
    credential = OfficeAceCredential.from_dict(result.credential_data)
    assert credential.access_key_id == "AK2"
    assert credential.refresh_token == "rt2"
    # 聊天凭据来自开通校验
    assert credential.model_app_key == "K"
    assert credential.model_app_secret == "S"
    assert credential.model_base_url == "https://gw.test"
    assert credential.account_id == "acct"
    assert credential.principal_id == "prin"
    # DPoP 私钥必须落盘：没有它下一步刷新必失败
    assert credential.dpop_private_jwk
    # 一次性 state 已被消费
    assert len(oauth.store) == 0


async def test_poll_credentials_use_client_id_from_config():
    oauth, state = await _started(_flow(),
                                 config=login_config(client_id="custom-client"))
    result = await oauth.poll(state, "alice")
    assert result is not None
    assert OfficeAceCredential.from_dict(
        result.credential_data).client_id == "custom-client"


async def test_poll_unprovisioned_account_gives_actionable_message():
    """未开通（`subscription: null`）不是登录失败，必须给可行动的指引。

    还要点名是**哪个**账号没开通：浏览器里留着别的华为云会话时，用户根本
    不知道刚才授权的是哪个号。
    """
    oauth, state = await _started(_flow(permission=httpx.Response(
        200, json={"account_id": "a", "principal_urn": "iam::a:user:eve",
                   "subscription": None})))
    with pytest.raises(UpstreamProtocolViolation) as caught:
        await oauth.poll(state, "alice")
    assert "还没开通 OfficeAce 服务" in caught.value.public_message
    assert "eve" in caught.value.public_message
    # 失败后 state 未消费：用户可以补齐开通再重试
    assert len(oauth.store) == 1


async def test_poll_unprovisioned_without_identity_uses_generic_wording():
    """上游连身份都不给时不能拼出「华为云账号  还没开通」这种残句。"""
    oauth, state = await _started(_flow(permission=httpx.Response(
        200, json={"subscription": None})))
    with pytest.raises(UpstreamProtocolViolation) as caught:
        await oauth.poll(state, "alice")
    message = caught.value.public_message
    assert message.startswith("这个账号还没开通 OfficeAce 服务")
    assert "华为云账号" not in message


async def test_poll_missing_gateway_raises():
    oauth, state = await _started(_flow(permission=_permission_response(
        subscription={"model_info": {"model_auth_info": {
            "model_app_key": "K", "model_app_secret": "S"}}})))
    with pytest.raises(UpstreamProtocolViolation) as caught:
        await oauth.poll(state, "alice")
    assert "模型网关地址" in caught.value.public_message


async def test_poll_nickname_falls_back_to_user_name():
    payload = base64.urlsafe_b64encode(
        json.dumps({"preferred_username": "carol"}).encode()).decode().rstrip("=")
    oauth, state = await _started(_flow(tokens=_tokens_response(
        id_token=f"h.{payload}.s")))
    result = await oauth.poll(state, "alice")
    assert result is not None and result.nickname == "carol"


async def test_poll_nickname_from_principal_urn():
    """令牌里没名字时用开通校验的 `principal_urn`：否则昵称是一串账号 ID。"""
    oauth, state = await _started(_flow(permission=_permission_response(
        principal_urn="iam::acct:user:dave")))
    result = await oauth.poll(state, "alice")
    assert result is not None
    assert result.nickname == "dave"
    assert OfficeAceCredential.from_dict(
        result.credential_data).user_name == "dave"


async def test_poll_concurrent_consume_raises():
    """两路轮询同时拿到码时，后完成的那路不能再落一条重复凭证。"""
    class LosingStore(AuthStateStore):
        """模拟「查得到但消费时已被另一路抢走」的竞态窗口。"""

        def consume(self, auth_state, username, now=None):
            return False

    oauth = _oauth(_flow())
    session = await oauth.start("alice")
    oauth.store = LosingStore()
    oauth.store.begin("alice", _session(), now=MOMENT)
    with pytest.raises(UpstreamProtocolViolation) as caught:
        # 用新 store 里真实存在的 state 走完整流程
        state = next(iter(oauth.store._entries))
        await oauth.poll(state, "alice")
    assert "consumed concurrently" in str(caught.value)
    assert session.state


async def test_poll_honours_ttl(monkeypatch):
    """过期 state 不再轮询：否则十分钟后回来的人会拿到一条「幽灵凭证」。"""
    oauth, state = await _started(_flow(), store=AuthStateStore(ttl_seconds=10))
    oauth._now = lambda: MOMENT + 3600
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.poll(state, "alice")


# ------------------------------------------------------------------ 换票

async def test_exchange_code_posts_form_with_dpop():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return _tokens_response()

    oauth = _oauth(handler)
    tokens = await oauth.exchange_code("code1", code_verifier="v",
                                       dpop_private_jwk=dpop.new_private_jwk())
    assert tokens["refresh_token"] == "rt2"
    request = seen["request"]
    assert str(request.url) == f"{STS}/v1/oauth2/tokens"
    assert request.headers["content-type"].startswith(
        "application/x-www-form-urlencoded")
    assert request.headers["dpop"].count(".") == 2
    assert {k: v[0] for k, v in parse_qs(request.content.decode()).items()} == {
        "client_id": "pdp5_for_agentarts", "code": "code1",
        "code_verifier": "v", "grant_type": "authorization_code",
        "redirect_uri": f"{CLOUD}/v1/claw/auth/callback"}


async def test_token_endpoint_error_carries_status_and_body():
    oauth = _oauth(lambda request: httpx.Response(400, content=b'{"e":"x"}'))
    with pytest.raises(TokenEndpointError) as caught:
        await oauth.exchange_code("c", code_verifier="v",
                                  dpop_private_jwk=dpop.new_private_jwk())
    assert caught.value.status == 400
    assert b'"e":"x"' in caught.value.body
    assert "token endpoint http 400" in str(caught.value)


async def test_token_endpoint_rejects_non_json():
    oauth = _oauth(lambda request: httpx.Response(200, text="<html>"))
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.exchange_code("c", code_verifier="v",
                                  dpop_private_jwk=dpop.new_private_jwk())


async def test_token_endpoint_rejects_non_object():
    oauth = _oauth(lambda request: httpx.Response(200, json=[1]))
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.exchange_code("c", code_verifier="v",
                                  dpop_private_jwk=dpop.new_private_jwk())


# ------------------------------------------------------------------ 刷新

async def test_refresh_tokens_uses_binding_fields():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["form"] = {k: v[0] for k, v in parse_qs(
            request.content.decode()).items()}
        return _tokens_response()

    oauth = _oauth(handler)
    await oauth.refresh_tokens(refresh_token="old-rt",
                               dpop_private_jwk=dpop.new_private_jwk(),
                               client_id="cid")
    assert seen["form"] == {"client_id": "cid", "grant_type": "refresh_token",
                            "refresh_token": "old-rt"}


async def test_refresh_tokens_defaults_client_id():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["form"] = {k: v[0] for k, v in parse_qs(
            request.content.decode()).items()}
        return _tokens_response()

    oauth = _oauth(handler)
    await oauth.refresh_tokens(refresh_token="rt",
                               dpop_private_jwk=dpop.new_private_jwk())
    assert seen["form"]["client_id"] == "pdp5_for_agentarts"


async def test_refresh_tokens_requires_ticket_and_key():
    """缺私钥本地就拒绝：上游只会回 `InvalidDPoPHeader`，还白烧一张一次性票。"""
    oauth = _oauth(lambda request: _tokens_response())
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.refresh_tokens(refresh_token="", dpop_private_jwk={})
    with pytest.raises(UpstreamProtocolViolation) as caught:
        await oauth.refresh_tokens(refresh_token="rt", dpop_private_jwk={})
    assert "dpop_private_jwk" in str(caught.value)


# ------------------------------------------------------------------ 开通校验

async def test_fetch_model_info_signs_with_sdk_hmac():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return _permission_response()

    oauth = _oauth(handler)
    credential = OfficeAceCredential(access_key_id="AK", secret_access_key="SK",
                                     security_token="TOK", project_id="pid")
    info = await oauth.fetch_model_info(credential)
    assert info["app_key"] == "K"
    request = seen["request"]
    assert str(request.url) == f"{CLOUD}/v1/claw/client-permission-validate"
    assert request.headers["authorization"].startswith(
        "SDK-HMAC-SHA256 Access=AK,")
    assert request.headers["x-sdk-date"] == "20261021T013052Z"


async def test_fetch_model_info_http_error_is_actionable():
    oauth = _oauth(lambda request: httpx.Response(401, content=b"APIG.0303"))
    credential = OfficeAceCredential(access_key_id="AK", secret_access_key="SK")
    with pytest.raises(UpstreamProtocolViolation) as caught:
        await oauth.fetch_model_info(credential)
    assert "开通校验失败" in caught.value.public_message
    assert "APIG.0303" in str(caught.value)


async def test_fetch_model_info_rejects_non_json():
    oauth = _oauth(lambda request: httpx.Response(200, text="<html>"))
    credential = OfficeAceCredential(access_key_id="AK", secret_access_key="SK")
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.fetch_model_info(credential)


# ------------------------------------------------------------------ 资源

async def test_lazy_http_client_and_aclose():
    oauth = OfficeAceOAuth(config=login_config(), proxy=None)
    first = oauth._http
    assert oauth._http is first
    await oauth.aclose()
    assert first.is_closed


async def test_aclose_without_client_is_noop():
    await OfficeAceOAuth(config=login_config()).aclose()

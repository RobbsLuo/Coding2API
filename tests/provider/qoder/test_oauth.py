"""Qoder 设备码登录。"""

from __future__ import annotations

import base64
import hashlib

import httpx
import pytest

from src.provider.qoder import auth as qoder_auth
from src.provider.qoder import events as qoder_events
from src.provider.qoder.auth import (
    AuthStateStore,
    QoderOAuth,
    _fallback_machine_id,
    build_auth_url,
    machine_id_for,
    pkce_pair,
)
from src.provider.qoder.cosy import _pkcs7_unpad
from src.provider.qoder.credential import QoderCredential
from src.provider.qoder.events import UpstreamProtocolViolation


async def test_oauth_start_and_poll_success():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_DEVICE_POLL:
            assert request.url.params["verifier"]
            assert request.url.params["challenge_method"] == "S256"
            return httpx.Response(200, json={"token": "dt-1", "refresh_token": "drt-1",
                                             "expires_in": 3_600_000,
                                             "user_id": "from-device"})
        assert request.url.path == qoder_events.EP_USERINFO
        assert request.headers["Authorization"] == "Bearer dt-1"
        return httpx.Response(200, json={"id": "u-9", "name": "Nick",
                                         "organization_id": "org"})

    oauth = QoderOAuth("cn", client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=None), machine_id="m-1")
    session = await oauth.start("root")
    assert session.flow == "poll" and session.interval == 5
    assert session.auth_url is not None
    assert "qoder.cn/device/selectAccounts?" in session.auth_url
    assert "client_id=" in session.auth_url
    assert "redirect_uri=" not in session.auth_url
    assert "machine_id=m-1" in session.auth_url

    result = await oauth.poll(session.state, "root")
    assert result is not None
    assert result.credential_data["access_token"] == "dt-1"
    assert result.credential_data["uid"] == "u-9"
    assert result.credential_data["realm"] == "cn"
    assert result.credential_data["auth_source"] == "oauth"
    assert result.nickname == "Nick"
    # state 已消费：再次轮询必须抛错
    with pytest.raises(UpstreamProtocolViolation, match="unknown or consumed"):
        await oauth.poll(session.state, "root")
    await oauth.aclose()

async def test_oauth_poll_pending_and_owner_checks():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    store = AuthStateStore()
    oauth = QoderOAuth("cn", client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=None), store=store,
        machine_id="m")
    session = await oauth.start("root")
    assert await oauth.poll(session.state, "root") is None        # 404 = 待授权
    # 其他用户不能轮询/取消
    with pytest.raises(UpstreamProtocolViolation, match="unknown or consumed"):
        await oauth.poll(session.state, "other")
    assert store.cancel(session.state, "other") is False
    assert store.cancel(session.state, "root") is True
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.poll(session.state, "root")
    await oauth.aclose()

async def test_oauth_poll_pending_202_and_missing_token():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(202)

    oauth = QoderOAuth("intl", client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=None), machine_id="m")
    session = await oauth.start("root")
    # intl 不带 redirect_uri，nonce 为 32 位 hex
    assert "redirect_uri" not in session.auth_url
    assert await oauth.poll(session.state, "root") is None
    await oauth.aclose()

    def no_token(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "pending"})

    oauth2 = QoderOAuth("cn", client=httpx.AsyncClient(
        transport=httpx.MockTransport(no_token), timeout=None), machine_id="m")
    session2 = await oauth2.start("root")
    assert await oauth2.poll(session2.state, "root") is None
    await oauth2.aclose()

async def test_oauth_poll_errors_and_userinfo_failure():
    def bad_status(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    oauth = QoderOAuth("cn", client=httpx.AsyncClient(
        transport=httpx.MockTransport(bad_status), timeout=None), machine_id="m")
    session = await oauth.start("root")
    with pytest.raises(UpstreamProtocolViolation, match="device poll http 500"):
        await oauth.poll(session.state, "root")
    await oauth.aclose()

    def not_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="nope")

    oauth2 = QoderOAuth("cn", client=httpx.AsyncClient(
        transport=httpx.MockTransport(not_json), timeout=None), machine_id="m")
    session2 = await oauth2.start("root")
    with pytest.raises(UpstreamProtocolViolation, match="not JSON"):
        await oauth2.poll(session2.state, "root")
    await oauth2.aclose()

    def userinfo_down(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_DEVICE_POLL:
            return httpx.Response(200, json={"token": "dt-x"})
        if request.url.path == qoder_events.EP_USERINFO:
            raise httpx.ConnectError("down", request=request)
        raise AssertionError(request.url.path)

    oauth3 = QoderOAuth("cn", client=httpx.AsyncClient(
        transport=httpx.MockTransport(userinfo_down), timeout=None), machine_id="m")
    session3 = await oauth3.start("root")
    result = await oauth3.poll(session3.state, "root")
    assert result is not None
    assert result.credential_data["uid"] == "dt-x"[:16]      # token 前缀兜底
    await oauth3.aclose()

async def test_oauth_poll_survives_userinfo_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_DEVICE_POLL:
            return httpx.Response(200, json={"token": "dt-y", "user_id": "u-y"})
        return httpx.Response(500, content=b"nope")

    oauth = QoderOAuth("cn", client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=None), machine_id="m")
    session = await oauth.start("root")
    result = await oauth.poll(session.state, "root")
    assert result is not None and result.credential_data["uid"] == "u-y"
    await oauth.aclose()

def test_auth_store_ttl_and_cleanup():
    store = AuthStateStore(ttl_seconds=10)
    state = store.begin("root", verifier="v", nonce="n", realm="cn",
                        machine_id="m", now=1000)
    assert store.owner(state, "root", now=1000) is True
    assert store.reservation(state, "root", now=1000) is not None
    assert store.reservation(state, "other", now=1000) is None
    store.cleanup(now=1009)
    assert store.owner(state, "root", now=1009) is True
    store.cleanup(now=1010)
    assert store.owner(state, "root", now=1010) is False
    assert store.consume(state, "root", now=1010) is False
    # owner 惰性判过期（未触发 cleanup）
    lazy = AuthStateStore(ttl_seconds=10)
    stale = lazy.begin("root", verifier="v", nonce="n", realm="cn",
                       machine_id="m", now=0)
    assert lazy.owner(stale, "root", now=10) is False
    assert stale not in lazy._entries

def test_pkce_pair_shape_and_auth_url_realm_difference():
    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert "=" not in challenge
    assert challenge == base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
    fixed = pkce_pair("abc")
    assert fixed[0] == "abc"
    url = build_auth_url("cn", challenge="c", nonce="n", machine_id="m")
    assert "challenge=c" in url and "challenge_method=S256" in url
    assert "client_id=e883ade2" in url
    assert "redirect_uri" not in url
    intl = build_auth_url("intl", challenge="c", nonce="n", machine_id="m")
    assert "redirect_uri" not in intl
    assert "client_id=e883ade2" in intl

def test_machine_id_prefers_official_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    realm = "cn"
    path = tmp_path / qoder_events.get_realm_config(realm).domain / ".auth"
    path.mkdir(parents=True)
    (path / "machine_id").write_text("official-id\n", encoding="utf-8")
    assert machine_id_for(realm) == "official-id"

def test_machine_id_falls_back_to_stable_value(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    first = machine_id_for("cn")
    assert first and first == machine_id_for("cn")
    # 无 HOME 可读时也不抛
    monkeypatch.setenv("HOME", str(tmp_path / "missing"))
    assert machine_id_for("intl")

def test_machine_id_skips_empty_and_reads_second_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = qoder_events.get_realm_config("cn")
    # 第一候选存在但为空 → 继续看第二候选
    first = tmp_path / config.domain / ".auth"
    first.mkdir(parents=True)
    (first / "machine_id").write_text("   \n", encoding="utf-8")
    second = tmp_path / f".{config.domain}" / ".auth"
    second.mkdir(parents=True)
    (second / "machine_id").write_text("second-id\n", encoding="utf-8")
    assert machine_id_for("cn") == "second-id"

def test_fallback_machine_id_is_process_stable():
    value = _fallback_machine_id()
    assert value == _fallback_machine_id()
    # issue #3：官方 machine_id 是带横线 UUID 形态，回落值必须同形态
    parts = value.split("-")
    assert [len(part) for part in parts] == [8, 4, 4, 4, 12]
    assert all(ch in "0123456789abcdef-" for ch in value)

def test_auth_url_without_client_id_and_build_variants():
    # 显式构造一个不发送 client_id 的区域配置（覆盖分支）
    realm = "custom"
    config = qoder_events.RealmConfig(
        name="custom", openapi="https://o.test", gateway="https://g.test",
        website="https://w.test", client_id="cid", redirect_uri="r",
        domain="d.test", user_agent="ua", send_client_id=False)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setitem(qoder_events.REALM_CONFIGS, realm, config)
    try:
        url = build_auth_url(realm, challenge="c", nonce="n", machine_id="m")
        assert "client_id" not in url and "machine_id" not in url
    finally:
        monkeypatch.undo()

def test_auth_url_redirect_uri_switch():
    # issue #3 后所有内置区域都不发 redirect_uri；开关分支用注入配置覆盖
    # （上游参数若再变，改 RealmConfig 即可恢复）
    config = qoder_events.RealmConfig(
        name="legacy", openapi="https://o.test", gateway="https://g.test",
        website="https://w.test", client_id="cid", redirect_uri="app://cb",
        domain="d.test", user_agent="ua", send_redirect_uri=True)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setitem(qoder_events.REALM_CONFIGS, "legacy", config)
    try:
        url = build_auth_url("legacy", challenge="c", nonce="n", machine_id="m")
        assert "redirect_uri=app%3A%2F%2Fcb" in url
    finally:
        monkeypatch.undo()

async def test_oauth_lazy_http_and_aclose_without_client(monkeypatch):
    oauth = QoderOAuth("cn", machine_id="m")
    assert oauth._client is None                              # noqa: SLF001
    created: list[httpx.AsyncClient] = []

    class _Stub:
        def __init__(self, *args, **kwargs):
            created.append(self)

        async def aclose(self):
            created.append("closed")

    monkeypatch.setattr(qoder_auth, "build_client", _Stub)
    assert oauth._http is oauth._http                          # noqa: SLF001
    assert len(created) == 1
    await oauth.aclose()
    assert created[-1] == "closed"

async def test_oauth_aclose_noop_without_client():
    oauth = QoderOAuth("cn", machine_id="m")
    await oauth.aclose()                                      # 未创建过：不报错

async def test_oauth_poll_missing_token_and_concurrent_consume():
    def missing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token": "  "})      # 授权完成但无 token

    oauth = QoderOAuth("cn", client=httpx.AsyncClient(
        transport=httpx.MockTransport(missing), timeout=None), machine_id="m")
    session = await oauth.start("root")
    assert await oauth.poll(session.state, "root") is None
    await oauth.aclose()

    # 拿到 token 但 state 已被并发消费 → 抛协议违规
    def ok(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_DEVICE_POLL:
            return httpx.Response(200, json={"token": "dt-1", "user_id": "u"})
        return httpx.Response(200, json={"id": "u"})

    store = AuthStateStore()
    oauth2 = QoderOAuth("cn", client=httpx.AsyncClient(
        transport=httpx.MockTransport(ok), timeout=None), store=store,
        machine_id="m")
    session2 = await oauth2.start("root")
    real_consume = store.consume

    def racy(state: str, username: str) -> bool:
        real_consume(state, username)                         # 先被别处消费
        return real_consume(state, username)

    store.consume = racy                                      # type: ignore[method-assign]
    with pytest.raises(UpstreamProtocolViolation, match="concurrently"):
        await oauth2.poll(session2.state, "root")
    await oauth2.aclose()

async def test_oauth_userinfo_non_200_and_non_object(monkeypatch):
    def not_200(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_DEVICE_POLL:
            return httpx.Response(200, json={"token": "dt-z", "user_id": "u"})
        return httpx.Response(503, content=b"down")

    oauth = QoderOAuth("cn", client=httpx.AsyncClient(
        transport=httpx.MockTransport(not_200), timeout=None), machine_id="m")
    session = await oauth.start("root")
    result = await oauth.poll(session.state, "root")
    assert result is not None and result.credential_data["uid"] == "u"
    await oauth.aclose()

    def not_object(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_DEVICE_POLL:
            return httpx.Response(200, json={"token": "dt-z"})
        return httpx.Response(200, json=[1, 2])

    oauth2 = QoderOAuth("cn", client=httpx.AsyncClient(
        transport=httpx.MockTransport(not_object), timeout=None), machine_id="m")
    session2 = await oauth2.start("root")
    with pytest.raises(UpstreamProtocolViolation, match="not an object"):
        await oauth2.poll(session2.state, "root")
    await oauth2.aclose()

def test_pkcs7_unpad_rejects_bad_input():
    with pytest.raises(ValueError, match="block aligned"):
        _pkcs7_unpad(b"")
    with pytest.raises(ValueError, match="block aligned"):
        _pkcs7_unpad(b"short")
    with pytest.raises(ValueError, match="bad PKCS7 padding"):
        _pkcs7_unpad(b"A" * 15 + b"\x00")                    # pad=0 非法
    with pytest.raises(ValueError, match="bad PKCS7 padding"):
        _pkcs7_unpad(b"A" * 15 + b"\x05")                    # 尾部不足 pad

async def test_oauth_fetch_userinfo_without_token_returns_empty():
    oauth = QoderOAuth("cn", machine_id="m")
    assert await oauth._fetch_userinfo(QoderCredential()) == {}   # noqa: SLF001

async def test_oauth_poll_rejects_empty_token_after_device(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_DEVICE_POLL:
            return httpx.Response(200, json={"token": "dt-1", "user_id": "u"})
        return httpx.Response(200, json={"id": "u"})

    oauth = QoderOAuth("cn", client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=None), machine_id="m")
    session = await oauth.start("root")
    # 模拟上游返回的 token 无法归一（deviceToken 形状漂移）→ 防御兜底
    monkeypatch.setattr(qoder_auth, "credential_from_device",
                        lambda *a, **k: QoderCredential())
    with pytest.raises(UpstreamProtocolViolation, match="missing token"):
        await oauth.poll(session.state, "root")
    await oauth.aclose()

"""TRAE 回调登录闭环与 /authorize。"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest

from src.config import Settings
from src.main import build_app
from src.provider.trae.callback import login_trace_id
from src.provider.trae.client import TraeProvider
from tests.helpers.constants import SECRET


async def test_poll_pending_returns_status_pending(admin_client):
    """轮询 pending → {"status": "pending"}（main 237-238）。"""
    app, client = admin_client
    oauth = app.state.upstream_auth["codebuddy"]

    async def fake_poll(_state, _username):
        return None

    oauth.poll = fake_poll
    body = client.post("/api/auth/upstream/poll",
                       json={"provider": "codebuddy", "state": "any"}).json()
    assert body == {"status": "pending"}

def test_trae_start_auth_builds_login_url_with_public_callback():
    from src.api.admin_auth import resolve_public_callback_url

    settings = Settings(_env_file=None, APP_SECRET=SECRET, PUBLIC_BASE_URL="https://gw.example")
    provider = TraeProvider()
    session = provider.start_auth(resolve_public_callback_url(settings))

    assert session.flow == "callback"
    assert session.callback_url == "https://gw.example/authorize"
    assert "auth_callback_url=https%3A%2F%2Fgw.example%2Fauthorize" in session.auth_url
    machine_id, _, device_id = session.state.partition(":")
    assert len(machine_id) == 32 and len(device_id) == 32

async def test_trae_complete_callback_exchanges_token():
    """回调链接必须真的换 token，而不是只存 refreshToken。"""
    import httpx as _httpx

    from src.provider.trae.client import TraeClient

    def handler(request: _httpx.Request) -> _httpx.Response:
        if request.url.path.endswith("ExchangeToken"):
            return _httpx.Response(200, json={"Result": {
                "Token": "ACCESS", "RefreshToken": "RT2", "TokenExpireAt": 1_800_000_000_000}})
        return _httpx.Response(200, json={"Result": {"UserID": "uid-9", "ScreenName": "昵称"}})

    transport = _httpx.MockTransport(handler)
    provider = TraeProvider(client=TraeClient(
        stream_client=_httpx.AsyncClient(transport=transport, timeout=None),
        short_client=_httpx.AsyncClient(transport=transport, timeout=None)))

    session = provider.start_auth("https://gw.example/authorize")
    url = ("https://gw.example/authorize?refreshToken=RT1&userInfo="
           "%7B%22uid%22%3A%22%22%7D")
    data = await provider.complete_callback(url, session.state)

    assert data["accessToken"] == "ACCESS"
    assert data["refreshToken"] == "RT2"
    assert data["expiresAt"] == 1_800_000_000
    assert data["uid"] == "uid-9"          # 回调没给 uid 时回退 GetUserInfo
    assert data["nickname"] == "昵称"
    assert data["machineId"] == session.state.partition(":")[0]

async def test_trae_complete_callback_rejects_bad_state():
    from src.provider.trae.events import UpstreamProtocolViolation

    provider = TraeProvider()
    with pytest.raises(UpstreamProtocolViolation):
        await provider.complete_callback("https://x/authorize?refreshToken=RT", "no-colon")

def test_authorize_completes_trae_login_end_to_end(tmp_path):
    """完整闭环：start → 浏览器回调 → 凭证入库 → 立即探测。"""
    import httpx as _httpx

    from src.provider.trae.client import TraeClient

    def handler(request: _httpx.Request) -> _httpx.Response:
        if request.url.path.endswith("ExchangeToken"):
            return _httpx.Response(200, json={"Result": {"Token": "ACCESS",
                                                         "RefreshToken": "RT2"}})
        if request.url.endswith("ide_user_ent_usage"):
            return _httpx.Response(200, json={"user_entitlement_pack_list": [
                {"entitlement_base_info": {"quota": {"credits_limit": 100}},
                 "usage": {"credits_amount": 25}}]})
        return _httpx.Response(200, json={"Result": {"UserID": "u1", "ScreenName": "n"}})

    transport = _httpx.MockTransport(handler)
    trae = TraeProvider(client=TraeClient(
        stream_client=_httpx.AsyncClient(transport=transport, timeout=None),
        short_client=_httpx.AsyncClient(transport=transport, timeout=None)))

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"trae": trae})

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "root", "password": "rootpw"})
        started = client.post("/api/auth/upstream/start", json={"provider": "trae"}).json()

        callback = ("/authorize?refreshToken=RT1&userInfo=%7B%22uid%22%3A%22u1%22%7D"
                    f"&state={started['state']}"
                    f"&loginTraceID={login_trace_id(*started['state'].split(':'))}")
        response = client.get(callback)
        assert response.status_code == 200 and response.json()["captured"] is True

        listed = client.get("/api/credentials").json()["credentials"]
        assert len(listed) == 1 and listed[0]["provider"] == "trae"
        # 响应与列表都不得泄漏 token
        assert "ACCESS" not in response.text
        assert "data_enc" not in listed[0]

    # state 已消费，同一回调不可重放
    with TestClient(app) as replay:
        assert replay.get(callback).status_code == 400

def test_authorize_rejects_callback_without_token(tmp_path):
    """带 state 与正确 loginTraceID 但没有 refreshToken/userJwt 的回调必须拒绝。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "root", "password": "rootpw"})
        started = client.post("/api/auth/upstream/start", json={"provider": "trae"}).json()
        trace = login_trace_id(*started["state"].split(":"))
        response = client.get(f"/authorize?state={started['state']}&loginTraceID={trace}")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_authorize_rejects_callback_with_wrong_login_trace_id(tmp_path):
    """绑定校验：缺失 / 不匹配的 loginTraceID 一律拒绝，且不消费 pending。

    这是 `/authorize` 的跨站防护——攻击者能带上受害者会话 Cookie（顶层导航
    会带 SameSite=Lax），但拿不到发起那次登录的 trace id，因此无法用自己的
    refreshToken 顶掉别人进行中的登录。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "root", "password": "rootpw"})
        started = client.post("/api/auth/upstream/start", json={"provider": "trae"}).json()

        # 缺失
        missing = client.get(f"/authorize?refreshToken=RT&state={started['state']}")
        assert missing.status_code == 400
        assert missing.json()["error"]["message"] == "callback does not match the pending login"
        # 不匹配（攻击者自造的 trace）
        wrong = client.get(f"/authorize?refreshToken=RT&state={started['state']}"
                           "&loginTraceID=0000000000000000")
        assert wrong.status_code == 400
        assert wrong.json()["error"]["message"] == "callback does not match the pending login"
        # 被拒的回调不消费 pending：发起人仍能用正确 trace 走完流程
        assert "root" in app.state.pending_callbacks

def test_upstream_start_uses_poll_track_for_codebuddy(admin_client):
    """provider 在 upstream_auth 里时走 poll 轨道（main 275-276）。"""
    app, client = admin_client

    class FakeOAuth:
        def __init__(self) -> None:
            self.store = type("S", (), {"cancel": staticmethod(lambda *_: True)})()

        async def start(self, username):
            from src.provider.base import AuthSession

            return AuthSession(flow="poll", state="local-reservation",
                               auth_url="https://auth.example/x", interval=5)

    app.state.upstream_auth["codebuddy"] = FakeOAuth()
    body = client.post("/api/auth/upstream/start", json={"provider": "codebuddy"}).json()
    assert body["flow"] == "poll"
    assert body["state"] == "local-reservation"
    assert body["callback_url"] is None

async def test_complete_callback_does_not_use_refresh_token_as_access_token():
    """ExchangeToken 失败时不得把 refreshToken 当 accessToken 塞进池子。"""
    import httpx as _httpx

    from src.provider.trae.client import TraeClient
    from src.provider.trae.events import UpstreamProtocolViolation

    def handler(request: _httpx.Request) -> _httpx.Response:
        if request.url.path.endswith("ExchangeToken"):
            return _httpx.Response(400, content=b"bad refresh token")
        return _httpx.Response(200, json={"Result": {"UserID": "u", "ScreenName": "n"}})

    transport = _httpx.MockTransport(handler)
    provider = TraeProvider(client=TraeClient(
        stream_client=_httpx.AsyncClient(transport=transport, timeout=None),
        short_client=_httpx.AsyncClient(transport=transport, timeout=None)))

    session = provider.start_auth("https://gw.example/authorize")
    with pytest.raises(UpstreamProtocolViolation):
        await provider.complete_callback(
            "https://gw.example/authorize?refreshToken=RT", session.state)

async def test_complete_callback_raises_when_no_token_available():
    import httpx as _httpx

    from src.provider.trae.client import TraeClient
    from src.provider.trae.events import UpstreamProtocolViolation

    def handler(_request: _httpx.Request) -> _httpx.Response:
        return _httpx.Response(400, content=b"rejected")

    transport = _httpx.MockTransport(handler)
    provider = TraeProvider(client=TraeClient(
        stream_client=_httpx.AsyncClient(transport=transport, timeout=None),
        short_client=_httpx.AsyncClient(transport=transport, timeout=None)))
    session = provider.start_auth("https://gw.example/authorize")
    with pytest.raises(UpstreamProtocolViolation):
        await provider.complete_callback(
            "https://gw.example/authorize?refreshToken=RT", session.state)

def test_authorize_reports_invalid_credential(tmp_path):
    """回调结构损坏 → 400 invalid_credential（main 446-448）。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "root", "password": "rootpw"})
        client.post("/api/auth/upstream/start", json={"provider": "trae"})
        # 有 refreshToken 但状态串损坏 → complete_callback 抛协议违规
        app.state.pending_callbacks["root"] = (time.monotonic(), "broken")
        response = client.get("/authorize?refreshToken=RT&state=broken"
                              "&loginTraceID=broken")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_credential"

def test_authorize_accepts_real_trae_callback_without_state(tmp_path):
    """TRAE 回跳只带 refreshToken/userInfo，不会回传我们的 state。

    之前实现要求 state 匹配，导致真实登录必然 400。
    """
    import httpx as _httpx

    from src.provider.trae.client import TraeClient

    def handler(request: _httpx.Request) -> _httpx.Response:
        if request.url.path.endswith("ExchangeToken"):
            return _httpx.Response(200, json={"Result": {"Token": "ACCESS",
                                                         "RefreshToken": "RT2"}})
        return _httpx.Response(200, json={"Result": {"UserID": "u9",
                                                     "ScreenName": "真名"}})

    transport = _httpx.MockTransport(handler)
    trae = TraeProvider(client=TraeClient(
        stream_client=_httpx.AsyncClient(transport=transport, timeout=None),
        short_client=_httpx.AsyncClient(transport=transport, timeout=None)))

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"trae": trae})

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "root", "password": "rootpw"})
        started = client.post("/api/auth/upstream/start", json={"provider": "trae"}).json()

        # 与真实回调一致：不带 state 参数，但带 TRAE 回显的 loginTraceID
        trace = login_trace_id(*started["state"].split(":"))
        callback = ("/authorize?refreshToken=RT1&userInfo="
                    "%7B%22uid%22%3A%22uid-9%22%2C%22nickname%22%3A%22%E7%9C%9F%E5%90%8D%22%7D"
                    f"&loginTraceID={trace}")
        response = client.get(callback)
        assert response.status_code == 200, response.text

        listed = client.get("/api/credentials").json()["credentials"]
        assert len(listed) == 1 and listed[0]["provider"] == "trae"
        assert listed[0]["nickname"] == "真名"
        # machine/device id 来自 pending state，与登录 URL 一致
        assert started["state"].partition(":")[0] in started["auth_url"]

    # 回调成功后 pending 已清空：同一链接重放必须拒绝
    with TestClient(app) as replay:
        assert replay.get(callback).status_code == 400

def test_authorize_without_pending_login_still_rejected(tmp_path):
    """没有进行中的登录时，带 refreshToken 的回调依然拒绝（防乱塞池子）。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    app = build_app(settings)
    with TestClient(app) as client:
        response = client.get("/authorize?refreshToken=RT")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"

def test_authorize_rejects_revoked_session(tmp_path, monkeypatch):
    """签名有效但发起人已禁用：不得完成兑换（与 principal_from_request 同口径）。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "root", "password": "rootpw"})
        started = client.post("/api/auth/upstream/start",
                              json={"provider": "trae"}).json()
        # Cookie 仍有效（签名/过期都 OK），但发起人已被禁用
        monkeypatch.setattr(app.state.services.users, "is_active", lambda _u: False)
        response = client.get(f"/authorize?refreshToken=RT&state={started['state']}")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_dump_request_bodies_writes_file(tmp_path):
    """诊断开关：dump_request_bodies=True 时 /v1 请求体落盘（main 291-293）。"""
    import json as _json

    from fastapi.testclient import TestClient

    from src.config import Settings
    from src.main import build_app

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        dump_request_bodies=True)

    class P:
        id = "p1"

        async def list_models(self, _data):
            from src.provider.base import Model

            return [Model(id="m")]

        def import_credential(self, raw):  # pragma: no cover
            return raw

    app = build_app(settings, providers={"p1": P()})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        r = client.post("/v1/chat/completions",
                        headers={"Authorization": f"Bearer {key}"},
                        json={"model": "m", "stream": False,
                              "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code in (200, 400, 503)  # body 是否合法不影响 dump
        dumps = list((tmp_path / "dumps").glob("*.json"))
        assert dumps, "应产生 dump 文件"
        body = _json.loads(dumps[0].read_text(encoding="utf-8"))
        assert body["model"] == "m"

def test_authorize_without_trae_provider_is_rejected(tmp_path):
    """registry 里没有 trae 渠道时，/authorize 直接 400（不崩）。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    app = build_app(settings, providers={})
    with TestClient(app) as client:
        response = client.get("/authorize?refreshToken=RT")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


# ---------------------------------------------------------------- 回调可发现性
# issue #21：「授权完成后不回调，也没报错」。两个成因都在这里固定：
# 回调基址指向浏览器打不开的主机（默认 127.0.0.1）、回调落在另一条入口而
# 浏览器不带管理台 Cookie；以及浏览器只拿到 JSON 看不出发生了什么。


def _request(host: str, *, scheme: str = "http",
             xfp: str | None = None) -> StarletteRequest:
    """构造一个最小 ASGI Request（只用到 host / scheme / 转发头）。"""
    headers = [(b"host", host.encode())]
    if xfp is not None:
        headers.append((b"x-forwarded-proto", xfp.encode()))
    return StarletteRequest({
        "type": "http", "method": "GET", "path": "/api/auth/upstream/start",
        "query_string": b"", "root_path": "", "scheme": scheme,
        "server": (host.split(":", 1)[0], 8000), "headers": headers,
    })


def test_resolve_callback_url_uses_request_origin_for_unreachable_base():
    """PUBLIC_BASE_URL 还是默认回环时，回调改用手上这条入口的 origin。

    默认值 http://127.0.0.1:8000 在远程/容器部署里指向**用户自己的机器**，
    浏览器跳过去连不上，授权完成也回不来。
    """
    from src.api.admin_auth import resolve_public_callback_url

    settings = Settings(_env_file=None, APP_SECRET=SECRET,
                        PUBLIC_BASE_URL="http://127.0.0.1:8000")
    # 从 LAN 地址打开管理台 → 回调用 LAN 地址
    assert resolve_public_callback_url(settings, _request("192.168.1.5:8000")) == \
        "http://192.168.1.5:8000/authorize"
    # 隧道入口：采信 X-Forwarded-Proto
    assert resolve_public_callback_url(
        settings, _request("ai.example.com", xfp="https")) == \
        "https://ai.example.com/authorize"
    # 非法 XFP 回落 socket 层协议
    assert resolve_public_callback_url(
        settings, _request("ai.example.com", xfp="ftp")) == \
        "http://ai.example.com/authorize"
    # 通配监听同样不可达
    wildcard = Settings(_env_file=None, APP_SECRET=SECRET,
                        PUBLIC_BASE_URL="http://0.0.0.0:8000")
    assert resolve_public_callback_url(wildcard, _request("10.0.0.2:8000")) == \
        "http://10.0.0.2:8000/authorize"
    # localhost 字面量同理
    named = Settings(_env_file=None, APP_SECRET=SECRET,
                     PUBLIC_BASE_URL="http://localhost:8000")
    assert resolve_public_callback_url(named, _request("10.0.0.2:8000")) == \
        "http://10.0.0.2:8000/authorize"


def test_resolve_callback_url_keeps_configured_base():
    """本机访问、或显式配了可达基址时，不为请求 origin 让路。"""
    from src.api.admin_auth import resolve_public_callback_url

    loopback = Settings(_env_file=None, APP_SECRET=SECRET,
                        PUBLIC_BASE_URL="http://127.0.0.1:8000/")
    assert resolve_public_callback_url(loopback, _request("127.0.0.1:8000")) == \
        "http://127.0.0.1:8000/authorize"
    assert resolve_public_callback_url(loopback, _request("[::1]:8000")) == \
        "http://127.0.0.1:8000/authorize"
    explicit = Settings(_env_file=None, APP_SECRET=SECRET,
                        PUBLIC_BASE_URL="https://gw.example")
    assert resolve_public_callback_url(explicit, _request("192.168.1.5:8000")) == \
        "https://gw.example/authorize"
    # 非回环 IP 基址：本来就是浏览器可达的，不为请求 origin 让路
    lan_base = Settings(_env_file=None, APP_SECRET=SECRET,
                        PUBLIC_BASE_URL="http://10.0.0.9:8000")
    assert resolve_public_callback_url(lan_base, _request("192.168.1.5:8000")) == \
        "http://10.0.0.9:8000/authorize"


def _mock_trae() -> TraeProvider:
    import httpx as _httpx

    from src.provider.trae.client import TraeClient

    def handler(request: _httpx.Request) -> _httpx.Response:
        if request.url.path.endswith("ExchangeToken"):
            return _httpx.Response(200, json={"Result": {"Token": "ACCESS",
                                                         "RefreshToken": "RT2"}})
        return _httpx.Response(200, json={"Result": {"UserID": "u1", "ScreenName": "n"}})

    transport = _httpx.MockTransport(handler)
    return TraeProvider(client=TraeClient(
        stream_client=_httpx.AsyncClient(transport=transport, timeout=None),
        short_client=_httpx.AsyncClient(transport=transport, timeout=None)))


def test_authorize_binds_callback_by_trace_without_session_cookie(tmp_path):
    """回调落在另一条入口时不带管理台 Cookie，靠 loginTraceID 仍能完成。

    同一实例常有 LAN 与隧道两条入口：在 LAN 发起、隧道回调时浏览器不会带
    LAN 域的会话 Cookie。trace 只出现在发起那次 auth_url 里，足以钉死登录。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"trae": _mock_trae()})

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "root", "password": "rootpw"})
        started = client.post("/api/auth/upstream/start", json={"provider": "trae"}).json()
        trace = login_trace_id(*started["state"].split(":"))

        client.cookies.clear()          # 换一条入口：浏览器没有管理台会话
        response = client.get(f"/authorize?refreshToken=RT1&loginTraceID={trace}")
        assert response.status_code == 200, response.text
        assert response.json()["captured"] is True
        assert "root" not in app.state.pending_callbacks

    listed = app.state.services.credentials.list_all()
    assert len(listed) == 1
    assert listed[0]["added_by"] == "root"      # 归属仍是发起登录的管理员


def test_authorize_ignores_malformed_pending_entries_when_matching_trace(tmp_path):
    """pending 里的 state 损坏（缺 device_id）时按 trace 反查直接跳过。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"trae": _mock_trae()})
    with TestClient(app) as client:
        app.state.pending_callbacks["root"] = (time.monotonic(), "broken")
        response = client.get("/authorize?refreshToken=RT&loginTraceID=abc")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_authorize_rejects_expired_pending_login(tmp_path):
    """pending 超过 TTL：作废并清槽，提示重新发起（不再无限期挂着）。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"trae": _mock_trae()})
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "root", "password": "rootpw"})
        started = client.post("/api/auth/upstream/start", json={"provider": "trae"}).json()
        trace = login_trace_id(*started["state"].split(":"))
        app.state.pending_callbacks["root"] = (time.monotonic() - 601, started["state"])
        response = client.get(f"/authorize?refreshToken=RT&loginTraceID={trace}")
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "pending TRAE login expired, start a new one"
    assert "root" not in app.state.pending_callbacks


def test_authorize_returns_html_page_for_browser_navigation(tmp_path):
    """浏览器顶层导航（Accept: text/html）拿到说明页，不是一坨 JSON。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"trae": _mock_trae()})

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "root", "password": "rootpw"})
        started = client.post("/api/auth/upstream/start", json={"provider": "trae"}).json()
        trace = login_trace_id(*started["state"].split(":"))
        response = client.get(
            f"/authorize?refreshToken=RT1&loginTraceID={trace}",
            headers={"accept": "text/html,application/xhtml+xml"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "TRAE 登录成功" in response.text
    assert "可以关闭此页面回到管理台" in response.text


def test_authorize_browser_failure_page_states_the_reason(tmp_path):
    """失败也要有中文原因——「没回调也没报错」的另一半在这里补齐。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    app = build_app(settings)
    with TestClient(app) as client:
        # 没有任何登录进行中（且请求不带 trace）
        response = client.get("/authorize?refreshToken=RT",
                              headers={"accept": "text/html"})
        assert response.status_code == 400
        assert response.headers["content-type"].startswith("text/html")
        assert "没有进行中的 TRAE 登录" in response.text

        # 结构化失败：JSON 契约不变
        json_response = client.get("/authorize?refreshToken=RT")
        assert json_response.json()["error"]["message"] == "no pending TRAE login in progress"

"""内置检索工具：provider 层解析 / 业务码映射 / 两个出口端点。"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from src.config import Settings
from src.main import build_app
from src.provider import base
from src.provider.codebuddy.tools import (
    EP_SEARCH,
    EP_WEBFETCH,
    ToolRejected,
    _classify_code,
    _envelope,
)
from tests.helpers.constants import SECRET

# ------------------------------------------------------------------ 夹具

@pytest.fixture(autouse=True)
def _allow_fetch_hosts(monkeypatch):
    """默认放行抓取目标：真实复核会做 DNS，用例里的占位域名解析必然失败。

    SSRF 真值与分支由 `test_fetch_*` 显式覆盖（含公网放行与内网拦截）。
    """
    monkeypatch.setattr("src.api.tools.blocked_webhook_host", lambda url: False)

# ------------------------------------------------------------------ 纯函数

@pytest.mark.parametrize(("code", "status"), [
    (11101, 400), (11128, 400), (11135, 400),
    (14018, 429), (1005, 429),
    (11140, 403),
    (99999, 502), ("not-a-number", 502), (None, 502),
])
def test_classify_code(code, status):
    assert _classify_code(code) == status


def test_envelope_unwraps_data_and_raises_on_business_code():
    assert _envelope({"code": 0, "data": {"results": []}}) == {"results": []}
    # 无 data 时返回原对象（部分端点直接平铺结果）
    assert _envelope({"code": 0, "results": []}) == {"code": 0, "results": []}
    with pytest.raises(ToolRejected) as caught:
        _envelope({"code": 11128, "msg": "illegal api invocation"})
    assert caught.value.status == 400
    assert caught.value.message == "illegal api invocation"
    assert caught.value.code == 11128


def test_envelope_uses_default_message_when_missing():
    with pytest.raises(ToolRejected, match="upstream rejected"):
        _envelope({"code": 99999})


# ------------------------------------------------------------------ client

def _client(handler) -> object:
    from src.provider.codebuddy.client import CodeBuddyClient

    return CodeBuddyClient(
        endpoint="https://copilot.tencent.com",
        stream_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        short_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _credential():
    from src.provider.codebuddy.credential import CodeBuddyCredential

    return CodeBuddyCredential(bearer_token="t", user_id="u")


async def test_search_sends_expected_body_and_normalizes_results():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == EP_SEARCH
        seen.update(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer t"
        return httpx.Response(200, json={"code": 0, "data": {
            "provider": "bing",
            "results": [{"title": "T", "url": "https://a", "snippet": "S"},
                        "junk"],
            "total_results": 2,
            "response_time_ms": 123}})

    client = _client(handler)
    result = await client.tools.search(
        _credential(), query="q", max_results=99,
        blocked_domains=["bad.com"], allowed_domains=["ok.com"],
        freshness="pw")

    assert seen["type"] == "text2text"
    assert seen["query"] == "q -site:bad.com"          # 官方 CLI 的拼法
    assert seen["max_results"] == 20                   # 夹到上限
    assert seen["allowed_domains"] == ["ok.com"]
    assert seen["freshness"] == "pw"
    assert result["provider"] == "bing"
    assert len(result["results"]) == 1                 # 非 dict 项被滤掉
    assert result["total_results"] == 2
    assert result["response_time_ms"] == 123


async def test_search_omits_optional_keys_when_absent():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"code": 0, "data": {"results": []}})

    await _client(handler).tools.search(_credential(), query="q", max_results=0)
    assert "allowed_domains" not in seen
    assert "freshness" not in seen
    assert seen["max_results"] == 1                    # 下限 1
    assert seen["query"] == "q"                        # 无 blocked 不加 site:


async def test_fetch_sends_expected_body_and_returns_text():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == EP_WEBFETCH
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"code": 0, "data": {
            "url": "https://a", "content": "# hi", "content_type": "text/html"}})

    result = await _client(handler).tools.fetch(
        _credential(), url="https://a", prompt="summarize", model="glm-5.2")
    assert seen["format"] == "markdown"
    assert seen["prompt"] == "summarize"
    assert seen["model_name"] == "glm-5.2"
    assert seen["timeout"] > 0 and seen["max_length"] > 0
    assert result == {"url": "https://a", "content": "# hi",
                      "content_type": "text/html"}


async def test_fetch_clamps_max_length_and_handles_missing_content():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"code": 0, "data": {}})

    result = await _client(handler).tools.fetch(
        _credential(), url="https://a", max_length=10 ** 9)
    assert seen["max_length"] == 200_000
    assert result["content"] == ""
    assert result["content_type"] == "text/plain"
    assert result["url"] == "https://a"                # 上游没回 url 时回落入参


@pytest.mark.parametrize(("status", "expected"), [
    (401, 403), (403, 403), (429, 429), (500, 502), (404, 502),
])
async def test_post_maps_http_status(status, expected):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=b'nope')

    with pytest.raises(ToolRejected) as caught:
        await _client(handler).tools.search(_credential(), query="q")
    assert caught.value.status == expected


async def test_post_raises_on_transport_error_and_bad_json():
    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(base.UpstreamTransportError):
        await _client(boom).tools.search(_credential(), query="q")

    def not_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>")

    with pytest.raises(base.UpstreamProtocolViolation):
        await _client(not_json).tools.search(_credential(), query="q")

    def not_object(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2])

    with pytest.raises(base.UpstreamProtocolViolation):
        await _client(not_object).tools.search(_credential(), query="q")


async def test_post_fills_response_time_when_upstream_omits_it():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"results": []}})

    result = await _client(handler).tools.search(_credential(), query="q")
    assert result["response_time_ms"] >= 0


# ------------------------------------------------------------------ 端点

class _ToolProvider:
    """实现 search_web / fetch_url 的渠道替身。"""

    id = "codebuddy"

    def __init__(self, *, reject_403: bool = False) -> None:
        self.reject_403 = reject_403
        self.calls: list[str] = []

    async def search_web(self, credential_data, *, query, max_results=5,
                         allowed_domains=None, blocked_domains=None,
                         freshness=None):
        self.calls.append(credential_data.get("bearer_token", "?"))
        if self.reject_403:
            raise ToolRejected(403, "credential rejected")
        return {"provider": "p", "results": [{"title": query}],
                "total_results": 1, "response_time_ms": max_results}

    async def fetch_url(self, credential_data, *, url, prompt="",
                        max_length=None, timeout=None):
        self.calls.append(url)
        return {"url": url, "content": f"{prompt}|{max_length}",
                "content_type": "text/html"}


def _app(tmp_path, provider):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"codebuddy": provider})
    app.state.credentials.add(provider="codebuddy", credential_data={"bearer_token": "t1"})
    return app


def _auth(app) -> dict:
    return {"Authorization": f"Bearer {app.state.api_keys.create('root')['api_key']}"}


def test_endpoint_search_and_fetch(tmp_path):
    provider = _ToolProvider()
    app = _app(tmp_path, provider)
    with TestClient(app) as client:
        headers = _auth(app)
        searched = client.post("/v1/tools/search", headers=headers, json={
            "query": "hello", "max_results": 3})
        fetched = client.post("/v1/fetch", headers=headers, json={
            "url": "https://a", "prompt": "sum", "max_length": 100})
    assert searched.status_code == 200
    assert searched.json()["results"] == [{"title": "hello"}]
    assert fetched.status_code == 200
    assert fetched.json()["content"] == "sum|100"


@pytest.mark.parametrize(("path", "body", "match"), [
    ("/v1/tools/search", {}, "query must be a non-empty string"),
    ("/v1/tools/search", {"query": "   "}, "query must be a non-empty string"),
    ("/v1/tools/search", {"query": "x" * 1001}, "too long"),
    ("/v1/tools/search", {"query": "q", "max_results": "5"},
     "max_results must be an integer"),
    ("/v1/tools/search", {"query": "q", "allowed_domains": "x"},
     "must be an array of strings"),
    ("/v1/tools/search", {"query": "q", "freshness": 1},
     "freshness must be a string"),
    ("/v1/fetch", {}, "url must be a non-empty string"),
    ("/v1/fetch", {"url": "ftp://a"}, "must be an http"),
    ("/v1/fetch", {"url": "https://a", "prompt": 1}, "prompt must be a string"),
    ("/v1/fetch", {"url": "https://a", "max_length": 0},
     "max_length must be a positive integer"),
])
def test_endpoint_rejects_bad_input(tmp_path, path, body, match):
    app = _app(tmp_path, _ToolProvider())
    with TestClient(app) as client:
        response = client.post(path, headers=_auth(app), json=body)
    assert response.status_code == 400
    assert match in response.text


def test_endpoint_rejects_non_object_body(tmp_path):
    app = _app(tmp_path, _ToolProvider())
    with TestClient(app) as client:
        response = client.post("/v1/tools/search", headers=_auth(app), json=[1])
    assert response.status_code == 400
    assert "JSON object" in response.text


@pytest.mark.parametrize(("status", "code"), [
    (400, "invalid_request"), (429, "rate_limit_exceeded"),
    (403, "credential_rejected"), (502, "upstream_error"),
])
def test_endpoint_maps_tool_rejection(tmp_path, status, code):
    class Rejecting(_ToolProvider):
        async def search_web(self, credential_data, **kwargs):
            raise ToolRejected(status, "nope")

    app = _app(tmp_path, Rejecting())
    with TestClient(app) as client:
        response = client.post("/v1/tools/search", headers=_auth(app),
                               json={"query": "q"})
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_endpoint_requires_api_key(tmp_path):
    app = _app(tmp_path, _ToolProvider())
    with TestClient(app) as client:
        response = client.post("/v1/tools/search", json={"query": "q"})
    assert response.status_code == 401


def test_endpoint_503_when_no_credentials(tmp_path):
    provider = _ToolProvider()
    settings = Settings(_env_file=None, APP_SECRET=SECRET,
                        DATA_DIR=str(tmp_path), ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"codebuddy": provider})
    # 只注册 provider、不加凭证 → pick_aux 返回空
    app.state.credentials.delete_all_for_provider("codebuddy") if hasattr(
        app.state.credentials, "delete_all_for_provider") else None
    with TestClient(app) as client:
        response = client.post("/v1/tools/search", headers=_auth(app),
                               json={"query": "q"})
    assert response.status_code == 503


def test_endpoint_400_when_no_provider_supports_tools(tmp_path):
    class Plain:
        id = "trae"

        async def stream_chat(self, *args, **kwargs):  # pragma: no cover
            yield None

    settings = Settings(_env_file=None, APP_SECRET=SECRET,
                        DATA_DIR=str(tmp_path), ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"trae": Plain()})
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    with TestClient(app) as client:
        response = client.post("/v1/tools/search", headers=_auth(app),
                               json={"query": "q"})
    assert response.status_code == 400
    assert "no provider supports" in response.text


# ------------------------------------------------------------------ 覆盖补丁

def test_optional_domains_drops_blank_entries_and_returns_none():
    from src.api.tools import _optional_domains

    assert _optional_domains({"d": [" a ", "  "]}, "d") == ["a"]
    assert _optional_domains({"d": ["   "]}, "d") is None
    assert _optional_domains({}, "d") is None


def test_endpoint_fetch_maps_rejection(tmp_path):
    class Rejecting(_ToolProvider):
        async def fetch_url(self, credential_data, **kwargs):
            raise ToolRejected(502, "upstream exploded")

    app = _app(tmp_path, Rejecting())
    with TestClient(app) as client:
        response = client.post("/v1/fetch", headers=_auth(app),
                               json={"url": "https://a"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"


def test_endpoint_fetch_rejects_bad_body_and_max_length_type(tmp_path):
    app = _app(tmp_path, _ToolProvider())
    with TestClient(app) as client:
        headers = _auth(app)
        non_object = client.post("/v1/fetch", headers=headers, json=[1])
        bad_length = client.post("/v1/fetch", headers=headers,
                                 json={"url": "https://a", "max_length": "9"})
    assert non_object.status_code == 400
    assert "JSON object" in non_object.text
    assert bad_length.status_code == 400
    assert "max_length must be a positive integer" in bad_length.text


def test_endpoint_403_falls_through_to_next_credential(tmp_path):
    """403 = 这个号不可用 → 换下一个号；两个都 403 才把最后一次拒绝对外。"""
    provider = _ToolProvider(reject_403=True)
    app = _app(tmp_path, provider)
    app.state.credentials.add(provider="codebuddy", credential_data={"bearer_token": "t2"})
    with TestClient(app) as client:
        response = client.post("/v1/tools/search", headers=_auth(app),
                               json={"query": "q"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "credential_rejected"
    # 两把号都试过（顺序由调度器按健康度/加入时间定，这里只断言覆盖全部）
    assert sorted(provider.calls) == ["t1", "t2"]


def test_pick_aux_skips_undecryptable_credentials(tmp_path):
    """解不开的凭证不进候选（与聊天选号同一口径）。"""
    from src.engine.executor import Executor, ExecutorDeps
    from src.engine.scheduler import Scheduler

    credentials, db = _repo_pair(tmp_path)
    good = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    bad = credentials.add(provider="trae", credential_data={"accessToken": "b"})
    db.connect().execute("UPDATE credentials SET data_enc = x'00' WHERE id = ?", (bad,))
    db.connect().commit()
    executor = Executor(ExecutorDeps(providers={}, credentials=credentials,
                                     scheduler=Scheduler(), default_model="m"))
    picked = executor.pick_aux(("trae",))
    assert [credential_id for credential_id, _ in picked] == [good]
    db.close()


def test_pick_aux_breaks_when_scheduler_selects_nothing(tmp_path):
    """调度器返回 None 就停（防御分支：候选都选不出来时不再空转）。"""
    from src.engine.executor import Executor, ExecutorDeps

    credentials, db = _repo_pair(tmp_path)
    credentials.add(provider="trae", credential_data={"accessToken": "a"})

    class NoPick:
        def select(self, candidates, tried, now):
            return None

    executor = Executor(ExecutorDeps(providers={}, credentials=credentials,
                                     scheduler=NoPick(), default_model="m"))
    assert executor.pick_aux(("trae",)) == []
    db.close()


def test_pick_aux_returns_empty_for_unknown_provider(tmp_path):
    from src.engine.executor import Executor, ExecutorDeps
    from src.engine.scheduler import Scheduler

    credentials, db = _repo_pair(tmp_path)
    executor = Executor(ExecutorDeps(providers={}, credentials=credentials,
                                     scheduler=Scheduler(), default_model="m"))
    assert executor.pick_aux(("nope",)) == []
    db.close()


def _repo_pair(tmp_path):
    from src.db.conn import Database
    from src.db.crypto import CredentialCipher
    from src.db.migrate import apply_schema
    from src.db.repo import CredentialRepository

    db = Database(tmp_path / "tools.sqlite3")
    apply_schema(db.connect())
    return CredentialRepository(db, CredentialCipher(SECRET)), db


def test_client_tools_property_is_cached():
    client = _client(lambda _request: httpx.Response(200, json={"code": 0}))
    assert client.tools is client.tools


async def test_provider_search_web_and_fetch_url_go_through_pacer(repo):
    """provider 层的两个方法：走 pacer + 转调 client.tools（含 intl 头族分支）。"""
    credentials, db = _repo_pair(__import__("pathlib").Path(
        __import__("tempfile").mkdtemp()))

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == EP_SEARCH:
            return httpx.Response(200, json={"code": 0, "data": {"results": []}})
        return httpx.Response(200, json={"code": 0, "data": {"content": "c"}})

    from src.provider.codebuddy.client import CodeBuddyClient, CodeBuddyProvider

    client = CodeBuddyClient(
        endpoint="https://copilot.tencent.com",
        stream_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        short_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    provider = CodeBuddyProvider(client=client)

    searched = await provider.search_web({"bearer_token": "t"}, query="q")
    fetched = await provider.fetch_url({"bearer_token": "t"}, url="https://a")
    assert searched["results"] == []
    assert fetched["content"] == "c"
    assert seen == [EP_SEARCH, EP_WEBFETCH]
    db.close()


# ------------------------------------------------------- 配额准入 / SSRF / 文案

def _quota_app(tmp_path, provider, *, user_quota=None):
    """单独建一个可按 Key/用户配额配置的 app（配额要写库，不能共用 `_app`）。"""
    app = _app(tmp_path, provider)
    if user_quota is not None:
        app.state.user_repo.set_policy("root", allowed_models="", quota=user_quota)
    return app


def _key_headers(app, quota: str = "") -> dict:
    return {"Authorization": f"Bearer {app.state.api_keys.create('root', quota=quota)['api_key']}"}


def test_tools_search_applies_key_rpm_quota(tmp_path):
    """0 配额外挂：超 RPM 必须 429，且不再打到上游。"""
    provider = _ToolProvider()
    app = _quota_app(tmp_path, provider)
    with TestClient(app) as client:
        headers = _key_headers(app, quota=json.dumps({"rpm": 1}))
        first = client.post("/v1/tools/search", headers=headers, json={"query": "q"})
        blocked = client.post("/v1/tools/search", headers=headers, json={"query": "q"})
    assert first.status_code == 200
    assert blocked.status_code == 429
    assert blocked.json()["error"]["type"] == "quota_exceeded"
    assert "requests per minute" in blocked.json()["error"]["message"]
    assert int(blocked.headers["Retry-After"]) >= 1
    assert len(provider.calls) == 1


def test_fetch_applies_user_rpm_quota(tmp_path):
    """用户级配额同样生效：换一把 Key 也绕不过（用户维度聚合）。"""
    app = _quota_app(tmp_path, _ToolProvider(),
                     user_quota=json.dumps({"rpm": 1}))
    with TestClient(app) as client:
        first = client.post("/v1/fetch", headers=_auth(app), json={"url": "https://a"})
        second_headers = _key_headers(app)
        blocked = client.post("/v1/fetch", headers=second_headers,
                              json={"url": "https://a"})
    assert first.status_code == 200
    assert blocked.status_code == 429
    assert blocked.json()["error"]["message"].startswith("user quota exceeded")


def test_tools_quota_lease_released_after_each_request(tmp_path):
    """并发租约必须归还：否则 max_concurrency=1 的 Key 自己就把自己锁死。"""
    app = _quota_app(tmp_path, _ToolProvider())
    created = app.state.api_keys.create("root", quota=json.dumps({"max_concurrency": 1}))
    headers = {"Authorization": f"Bearer {created['api_key']}"}
    with TestClient(app) as client:
        for _ in range(3):
            assert client.post("/v1/tools/search", headers=headers,
                               json={"query": "q"}).status_code == 200
    assert app.state.services.key_quota.inflight(created["id"]) == 0


def test_tools_quota_user_rejection_releases_key_slot(tmp_path):
    """用户级拒绝时 Key 级的在途名额要立刻归还（两层口径不分叉）。"""
    from src.key_quota import KeyQuota

    app = _quota_app(tmp_path, _ToolProvider(),
                     user_quota=json.dumps({"max_concurrency": 1}))
    created = app.state.api_keys.create("root", quota=json.dumps({"max_concurrency": 5}))
    app.state.services.user_quota.admit("root", KeyQuota(max_concurrency=1))
    with TestClient(app) as client:
        blocked = client.post(
            "/v1/tools/search",
            headers={"Authorization": f"Bearer {created['api_key']}"},
            json={"query": "q"})
    assert blocked.status_code == 429
    assert app.state.services.key_quota.inflight(created["id"]) == 0


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x",
    "http://169.254.169.254/latest/meta-data",
    "http://100.100.100.200/x",
])
def test_fetch_rejects_private_addresses(tmp_path, monkeypatch, url):
    """用户可控 URL 的信任边界：内网/保留地址一律 400，不打上游。"""
    monkeypatch.setattr("src.api.tools.blocked_webhook_host", lambda _url: True)
    provider = _ToolProvider()
    app = _app(tmp_path, provider)
    with TestClient(app) as client:
        response = client.post("/v1/fetch", headers=_auth(app), json={"url": url})
    assert response.status_code == 400
    assert "private, loopback or reserved" in response.text
    assert provider.calls == []


def test_fetch_allows_public_url(tmp_path, monkeypatch):
    """公网地址照常放行（不误伤正常抓取）。"""
    seen: list[str] = []
    monkeypatch.setattr("src.api.tools.blocked_webhook_host",
                        lambda url: seen.append(url) or False)
    app = _app(tmp_path, _ToolProvider())
    with TestClient(app) as client:
        response = client.post("/v1/fetch", headers=_auth(app),
                               json={"url": "https://example.com/a"})
    assert response.status_code == 200
    assert seen == ["https://example.com/a"]


def test_tool_rejection_does_not_echo_upstream_body(tmp_path, caplog):
    """上游拒绝正文只进日志：对外只给受控分类，避免内部 schema/请求回显外泄。"""
    class Leaky(_ToolProvider):
        async def search_web(self, credential_data, **kwargs):
            raise ToolRejected(502, 'secret_body query="user input" token=abc')

    app = _app(tmp_path, Leaky())
    with TestClient(app) as client, caplog.at_level("WARNING"):
        response = client.post("/v1/tools/search", headers=_auth(app),
                               json={"query": "q"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"
    assert "secret_body" not in response.text
    assert response.json()["error"]["message"] == "upstream error 502"
    assert "secret_body" in caplog.text

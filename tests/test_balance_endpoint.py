"""GET /v1/user/balance（余额查询端点）。

数据源是凭证池的额度探测缓存（credentials.quota_*），
覆盖：API Key 鉴权、可用性聚合、禁用/未探测凭证排除、
池为空或从未探测的 unknown 语义。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.config import Settings
from src.main import build_app
from src.provider.base import Quota
from tests.conftest import SECRET

PATH = "/v1/user/balance"


@pytest.fixture()
def settings(tmp_path):
    return Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                    ADMIN_USERNAMES="root")


def _client(settings):
    app = build_app(settings)
    return app, TestClient(app)


def _login(client):
    client.cookies.set("coding2api_session", create_session_token("root", SECRET))


def _key(client) -> str:
    return client.post("/api/api-keys", json={"name": "t"}).json()["api_key"]


def _client_with_key(settings):
    """建 app + 登录会话创建 API Key，返回 (app, client, key)；cookie 随后清掉。"""
    app, client = _client(settings)
    _login(client)
    with client as c:
        key = _key(c)
    client.cookies.clear()
    return app, client, key


def _seed_credentials(app, rows):
    """批量添加凭证并写回探测额度。rows: (provider, remaining, total, enabled)。"""
    credentials = app.state.credentials
    for provider, remaining, total, enabled in rows:
        credential_id = credentials.add(
            provider=provider, credential_data={"bearer_token": "t"})
        if not enabled:
            credentials.set_enabled(credential_id, False)
        if total is not None:
            credentials.save_quota(
                credential_id,
                Quota(remaining=remaining, total=total, probed_at=int(time.time())))


def _balance(client, key):
    return client.get(PATH, headers={"Authorization": f"Bearer {key}"}).json()


def test_balance_returns_pool_total(settings):
    app, client, key = _client_with_key(settings)
    _seed_credentials(app, [("codebuddy", 30.0, 100.0, True)])
    with client as c:
        response = c.get(PATH, headers={"Authorization": f"Bearer {key}"})
        assert response.status_code == 200
        body = response.json()
        assert body["is_available"] is True
        assert body["balance_infos"][0]["total_balance"] == "30.00"
        assert body["balance_infos"][0]["currency"] == "credits"


def test_balance_requires_api_key(settings):
    _app, client = _client(settings)
    with client as c:
        assert c.get(PATH).status_code == 401
        assert c.get(PATH, headers={"Authorization": "Bearer sk-wrong"}).status_code == 401


def test_balance_aggregates_pool_and_excludes_disabled(settings):
    app, client, key = _client_with_key(settings)
    _seed_credentials(app, [
        ("codebuddy", 30.0, 100.0, True),
        ("codebuddy", 20.0, 50.0, True),
        ("trae", 10.0, 40.0, True),
        ("trae", 999.0, 999.0, False),      # 用户软关闭 → 不计
    ])
    with client as c:
        body = _balance(c, key)
    assert body["is_available"] is True
    assert body["balance_infos"][0]["total_balance"] == "60.00"
    providers = {entry["provider"]: entry for entry in body["providers"]}
    assert providers["codebuddy"] == {"provider": "codebuddy", "remaining": 50.0,
                                      "total": 150.0, "credentials": 2}
    assert providers["trae"]["total"] == 40.0
    assert providers["trae"]["credentials"] == 1


def test_balance_unknown_when_no_quota_probed(settings):
    """从未探测成功 → unknown 而不是 0（与健康度三态一致）。"""
    app, client, key = _client_with_key(settings)
    _seed_credentials(app, [("codebuddy", None, None, True)])
    with client as c:
        body = _balance(c, key)
    assert body["is_available"] is False
    assert body["balance_known"] is False
    assert body["balance_infos"][0]["total_balance"] == "0.00"
    assert body["providers"] == []


def test_balance_exhausted_pool_is_not_available(settings):
    app, client, key = _client_with_key(settings)
    _seed_credentials(app, [("codebuddy", 0.0, 100.0, True)])
    with client as c:
        body = _balance(c, key)
    assert body["is_available"] is False
    assert body["balance_known"] is True
    assert body["balance_infos"][0]["total_balance"] == "0.00"


def test_balance_empty_pool_is_unknown(settings):
    """空池同样返回 unknown，客户端显示不可用而不是 0。"""
    _app, client, key = _client_with_key(settings)
    with client as c:
        body = _balance(c, key)
    assert body["is_available"] is False
    assert body["balance_known"] is False


def test_balance_only_v1_prefix(settings):
    """/user/balance（无 v1 前缀）不再提供：落回前端 SPA，不是余额响应；
    未匹配的 /v1 路径返回 JSON 404 而不是 index.html。"""
    _app, client, key = _client_with_key(settings)
    with client as c:
        response = c.get("/user/balance", headers={"Authorization": f"Bearer {key}"})
        assert "balance_infos" not in response.text      # 落回 index.html 而非余额 JSON
        response = c.get("/v1/user/nope", headers={"Authorization": f"Bearer {key}"})
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "invalid_request"


def test_balance_read_endpoint_needs_no_csrf_header(settings):
    """GET 读端点对 API Key 客户端天然无 CSRF 要求（cookie 不参与鉴权）。"""
    app, client, key = _client_with_key(settings)
    _seed_credentials(app, [("trae", 5.0, 10.0, True)])
    with client as c:
        assert c.get(PATH, headers={"Authorization": f"Bearer {key}"}).status_code == 200

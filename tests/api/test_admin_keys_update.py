"""管理台「编辑 API Key」：按字段增量更新策略、清空、校验、审计与即时生效。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.audit.actions import ACTION_API_KEY_UPDATE, ACTION_LABELS, ACTIONS
from src.db.key_update import update_api_key_fields
from src.main import build_app
from tests.helpers.auth import isolated_settings, set_session_cookie


@pytest.fixture()
def app(tmp_path):
    return build_app(isolated_settings(tmp_path))


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        set_session_cookie(test_client, "root")
        yield test_client


def _create(client, **payload) -> dict:
    response = client.post("/api/api-keys", json={"name": "k", **payload})
    assert response.status_code == 200, response.text
    return response.json()


def _stored(app, username: str = "root", key_id: str | None = None) -> dict:
    rows = app.state.api_keys.list_for(username)
    return next(row for row in rows if key_id is None or row["id"] == key_id)


def test_update_single_field_keeps_others(client, app):
    created = _create(client, provider_binding="trae", allowed_models="glm-*")
    response = client.put(f"/api/api-keys/{created['id']}", json={"name": "renamed"})
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "changed": ["name"]}
    row = _stored(app, key_id=created["id"])
    assert row["name"] == "renamed"
    # 没提供的字段保持原值（不是被清空）
    assert row["provider_binding"] == "trae"
    assert row["allowed_models"] == "glm-*"


def test_update_multiple_fields_normalizes_like_create(client, app):
    created = _create(client)
    response = client.put(f"/api/api-keys/{created['id']}", json={
        "provider_binding": " TRAE ",
        "allowed_ips": "10.0.0.1",
        "allowed_models": " glm-* , kimi-k3 ",
        "expires_at": 4102444800,
    })
    assert response.status_code == 200, response.text
    assert response.json()["changed"] == [
        "provider_binding", "allowed_ips", "allowed_models", "expires_at"]
    row = _stored(app, key_id=created["id"])
    assert row["provider_binding"] == "trae"
    assert row["allowed_ips"] == "10.0.0.1/32"
    assert row["allowed_models"] == "glm-*,kimi-k3"
    assert row["expires_at"] == 4102444800


def test_update_clears_fields_with_empty_values(client, app):
    created = _create(client, provider_binding="trae", allowed_ips="10.0.0.1",
                      allowed_models="glm-*", expires_at=4102444800)
    response = client.put(f"/api/api-keys/{created['id']}", json={
        "name": "", "provider_binding": "", "allowed_ips": "",
        "allowed_models": "", "expires_at": None,
    })
    assert response.status_code == 200, response.text
    row = _stored(app, key_id=created["id"])
    assert (row["name"], row["provider_binding"], row["allowed_ips"],
            row["allowed_models"], row["expires_at"]) == ("", "", "", "", None)
    detail = app.state.audit.query(action=ACTION_API_KEY_UPDATE)[0]["detail"]
    # 空值必须给出语义化文案，否则「来源 IP 」读不出是「不限制」
    assert "名称 k → (未命名)" in detail
    assert "绑定渠道 trae → 自动" in detail
    assert "来源 IP 10.0.0.1/32 → 不限制" in detail
    assert "模型白名单 glm-* → 不限制" in detail
    assert "到期 4102444800 → 永不过期" in detail


@pytest.mark.parametrize("field", ["quota", "api_key", "id", "username"])
def test_update_rejects_non_editable_fields(client, app, field):
    created = _create(client)
    response = client.put(f"/api/api-keys/{created['id']}", json={field: "x"})
    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert "不可编辑的字段" in message
    # 配额有专用端点：错误文案要指过去，否则用户以为 PUT 改配额成功了
    assert "quota" in message


@pytest.mark.parametrize(("payload", "needle"), [
    ({"provider_binding": "ghost"}, "provider_binding"),
    ({"allowed_ips": "not-an-ip"}, "非法 IP/CIDR"),
    ({"allowed_models": "x" * 200}, "过长"),
    ({"expires_at": "soon"}, "expires_at"),
    ({"expires_at": 0}, "expires_at"),
    ({"expires_at": True}, "expires_at"),
])
def test_update_rejects_invalid_values(client, app, payload, needle):
    created = _create(client, provider_binding="trae")
    response = client.put(f"/api/api-keys/{created['id']}", json=payload)
    assert response.status_code == 400
    assert needle in response.json()["error"]["message"]
    # 校验失败不落库（校验全部在写之前）
    assert _stored(app, key_id=created["id"])["provider_binding"] == "trae"


def test_update_missing_key_is_400(client):
    response = client.put("/api/api-keys/key_ghost", json={"name": "x"})
    assert response.status_code == 400
    assert "not found" in response.json()["error"]["message"]


def test_update_cannot_touch_other_users_key(client, app):
    other = app.state.api_keys.create("alice", "theirs")
    response = client.put(f"/api/api-keys/{other['id']}", json={"name": "hijacked"})
    assert response.status_code == 400
    assert "not found" in response.json()["error"]["message"]
    assert _stored(app, "alice", other["id"])["name"] == "theirs"


def test_update_noop_does_not_write_or_audit(client, app):
    created = _create(client, name="same")
    response = client.put(f"/api/api-keys/{created['id']}", json={"name": "same"})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "changed": []}
    # 空请求体（没有任何可编辑字段）= 无事发生，同样不写审计
    assert client.put(f"/api/api-keys/{created['id']}",
                      json={}).json() == {"ok": True, "changed": []}
    assert app.state.audit.query(action=ACTION_API_KEY_UPDATE) == []


def test_update_audits_diff_without_plaintext(client, app):
    created = _create(client, name="old")
    response = client.put(f"/api/api-keys/{created['id']}",
                          json={"name": "new", "allowed_models": "glm-*"})
    assert response.status_code == 200
    events = app.state.audit.query(action=ACTION_API_KEY_UPDATE)
    assert len(events) == 1
    assert events[0]["target"] == created["id"]
    assert events[0]["detail"] == "名称 old → new；模型白名单 不限制 → glm-*"
    # 明文只在创建响应里出现一次：审计行绝不能带它
    assert created["api_key"] not in events[0]["detail"]


def test_update_takes_effect_on_next_request(client, app):
    """改白名单后下一个 /v1 请求立即生效（鉴权每次现读，无进程内策略缓存）。"""
    created = _create(client, name="immediate")
    headers = {"Authorization": f"Bearer {created['api_key']}"}
    payload = {"model": "kimi-k3", "messages": [{"role": "user", "content": "hi"}]}

    # 空白名单 = 不限制：请求不会被白名单拒绝（后续可能因上游不可用失败）
    assert "not allowed" not in client.post("/v1/chat/completions", headers=headers,
                                            json=payload).text

    assert client.put(f"/api/api-keys/{created['id']}",
                      json={"allowed_models": "glm-*"}).status_code == 200
    blocked = client.post("/v1/chat/completions", headers=headers, json=payload)
    assert blocked.status_code == 400
    assert "not allowed" in blocked.json()["error"]["message"]

    # 清空白名单同样立即生效
    assert client.put(f"/api/api-keys/{created['id']}",
                      json={"allowed_models": ""}).status_code == 200
    assert "not allowed" not in client.post("/v1/chat/completions", headers=headers,
                                            json=payload).text


def test_update_requires_session(app):
    with TestClient(app) as http:      # 不带 Cookie
        created = app.state.api_keys.create("root")
        assert http.put(f"/api/api-keys/{created['id']}",
                        json={"name": "x"}).status_code == 401


def test_update_rejects_cross_origin_write(client):
    created = _create(client)
    response = client.put(f"/api/api-keys/{created['id']}", json={"name": "x"},
                          headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_update_race_guard_returns_400(client, monkeypatch):
    """SELECT 到 UPDATE 之间被并发删除：按「不存在」返回，而不是假装成功。"""
    created = _create(client)
    monkeypatch.setattr("src.api.admin_keys.update_api_key_fields",
                        lambda *args, **kwargs: False)
    response = client.put(f"/api/api-keys/{created['id']}", json={"name": "x"})
    assert response.status_code == 400
    assert "not found" in response.json()["error"]["message"]


def test_update_repository_rejects_unknown_column(app):
    """仓储层白名单是 SQL 拼接前的最后一道防线（API 层已先拦）。"""
    created = app.state.api_keys.create("root")
    with pytest.raises(ValueError, match="不可编辑的列"):
        update_api_key_fields(app.state.api_keys, created["id"], "root",
                              {"key_digest": "x"})
    # 空改动不写库
    assert update_api_key_fields(app.state.api_keys, created["id"], "root", {}) is True


def test_update_action_registered_with_label():
    assert ACTION_API_KEY_UPDATE in ACTIONS
    assert ACTION_LABELS[ACTION_API_KEY_UPDATE] == "编辑 API Key"

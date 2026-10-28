"""kill switch（全站暂停）+ 关键写操作的审计留痕。

守两条：
1. **暂停面**：`gateway_paused` 打开后所有 API Key 鉴权的 /v1 端点返 503
   （`type=service_paused`），而**管理台照常可用**——否则打开开关就再也关不掉。
2. **留痕面**：发 / 删 API Key、导出明细这些「造凭据 / 搬数据」的动作必须进审计，
   否则事后回答不了「谁在什么时候做了什么」。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.audit.actions import (
    ACTION_API_KEY_CREATE,
    ACTION_API_KEY_DELETE,
    ACTION_DATA_EXPORT,
    ACTION_SETTINGS_UPDATE,
)
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


def _make_key(client, **payload) -> dict:
    response = client.post("/api/api-keys", json={"name": "k", **payload})
    assert response.status_code == 200, response.text
    return response.json()


# ------------------------------------------------------------------ 暂停面


def test_gateway_pause_returns_503_on_v1_but_keeps_admin_console(client, app):
    """/v1 一律 503，管理台（会话鉴权）仍可用 —— 一键止血 + 还能关上它。"""
    created = _make_key(client)
    headers = {"Authorization": f"Bearer {created['api_key']}"}
    assert client.get("/v1/models", headers=headers).status_code == 200

    paused = client.put("/api/settings", json={"values": {"gateway_paused": "true",
                                                          "gateway_pause_reason": "上游事故"}})
    assert paused.status_code == 200, paused.text
    blocked = client.get("/v1/models", headers=headers)
    assert blocked.status_code == 503
    assert blocked.json()["error"]["code"] == "service_paused"
    # 原因只进日志/审计，不回显给调用方
    assert "上游事故" not in blocked.text
    # 管理台仍可读可写：这是「能自己把开关关掉」的前提
    assert client.get("/api/api-keys").status_code == 200
    assert client.get("/api/settings").status_code == 200

    resumed = client.put("/api/settings", json={"values": {"gateway_paused": "false"}})
    assert resumed.status_code == 200
    assert client.get("/v1/models", headers=headers).status_code == 200


def test_gateway_pause_blocks_anthropic_entry_too(client):
    """Anthropic 入口（x-api-key）同样被暂停面覆盖：它走同一个鉴权依赖。"""
    created = _make_key(client)
    client.put("/api/settings", json={"values": {"gateway_paused": "true"}})
    blocked = client.post("/v1/messages", headers={"x-api-key": created["api_key"]},
                          json={"model": "glm-5.2", "max_tokens": 1,
                                "messages": [{"role": "user", "content": "hi"}]})
    assert blocked.status_code == 503
    assert blocked.json()["error"]["code"] == "service_paused"


def test_gateway_pause_state_is_audited_and_logged(client, app, caplog):
    """开关动作进审计（settings.update 带 key）并单独记一条 WARNING。"""
    with caplog.at_level("WARNING"):
        client.put("/api/settings", json={"values": {"gateway_paused": "true",
                                                     "gateway_pause_reason": "演练"}})
    assert [event["action"] for event in app.state.audit.query()] == [
        ACTION_SETTINGS_UPDATE]
    assert "gateway_paused" in app.state.audit.query()[0]["detail"]
    assert any("网关暂停" in record.getMessage() for record in caplog.records)


def test_dump_toggle_is_audited_and_warned(client, app, caplog):
    """请求体落盘开关是热更项：改动进审计 + 单独 WARNING（排查完没关是事故）。"""
    with caplog.at_level("WARNING"):
        response = client.put("/api/settings",
                              json={"values": {"dump_request_bodies": "true"}})
    assert response.status_code == 200
    assert "dump_request_bodies" in app.state.audit.query(
        action=ACTION_SETTINGS_UPDATE)[0]["detail"]
    assert any("请求体诊断落盘" in record.getMessage() for record in caplog.records)
    # 生效值随响应下发（管理台据此回显），关掉后不再 dump
    assert client.put("/api/settings",
                      json={"values": {"dump_request_bodies": "false"}}).status_code == 200


# ------------------------------------------------------------------ 留痕面


def test_api_key_create_and_delete_are_audited(client, app):
    """发 Key / 删 Key 必须留痕：明文只回显一次，事后只能靠审计行追责。"""
    created = _make_key(client, allowed_models="glm-5.3*",
                        provider_binding="trae", allowed_ips="10.0.0.1",
                        expires_at=4102444800, quota={"rpm": 5})
    events = app.state.audit.query(action=ACTION_API_KEY_CREATE)
    assert events[0]["target"] == created["id"]
    detail = events[0]["detail"]
    assert "模型白名单 glm-5.3*" in detail and "绑定渠道 trae" in detail
    assert "来源 IP 10.0.0.1" in detail and "RPM 5" in detail
    assert detail.endswith("永不过期") or "到期 4102444800" in detail

    # 删掉的 Key 名字要留在审计里（删完就查不到名字了）
    with_key_name = _make_key(client, )
    client.delete(f"/api/api-keys/{with_key_name['id']}")
    deletes = app.state.audit.query(action=ACTION_API_KEY_DELETE)
    assert deletes[0]["target"] == with_key_name["id"]
    assert deletes[0]["detail"] == "名称 k"

    # 不存在的 Key / 别人的 Key：400 且不落审计（没有发生任何删除）
    assert client.delete("/api/api-keys/key_nope").status_code == 400
    assert len(app.state.audit.query(action=ACTION_API_KEY_DELETE)) == 1


def test_create_key_without_policy_is_audited_as_unlimited(client, app):
    """.env 式默认（无白名单/IP/到期）也要有一条可读明细，不能只留个 id。"""
    _make_key(client)
    detail = app.state.audit.query(action=ACTION_API_KEY_CREATE)[0]["detail"]
    assert detail == "名称 k；永不过期；清除配额"


def test_export_events_csv_is_audited(client, app):
    """整份明细导出留痕（谁把多少行拉走了）。"""
    response = client.get("/api/stats/events?format=csv")
    assert response.status_code == 200
    events = app.state.audit.query(action=ACTION_DATA_EXPORT)
    assert events[0]["target"] == "usage_events"
    assert events[0]["detail"] == "CSV 明细 0 行"


def test_export_audit_marks_truncation(client, app, monkeypatch):
    """截断也要写进明细（否则导出方以为拿到的是全部）。"""
    from src.api import admin_stats
    from src.stats.query import StatsQuery

    monkeypatch.setattr(StatsQuery, "export_events",
                        lambda self, **kwargs: [{"ts": 1, "model": "m"},
                                                {"ts": 2, "model": "m"}])
    monkeypatch.setattr(admin_stats, "EXPORT_MAX_ROWS", 1)
    assert client.get("/api/stats/events?format=csv").status_code == 200
    assert app.state.audit.query(action=ACTION_DATA_EXPORT)[0]["detail"] \
        == "CSV 明细 1 行（已截断）"
"""Qoder 网页 Cookie 录入端点：写入/清除、校验、审计、导出不泄漏。"""

from __future__ import annotations

from src.audit.actions import ACTION_CREDENTIAL_WEB_COOKIE


def _add_qoder(app):
    return app.state.credentials.add(
        provider="qoder",
        credential_data={"access_token": "dt-1", "uid": "u-1", "auth_source": "oauth"})


def _add_codebuddy(app):
    return app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t"})


def test_set_and_clear_web_cookie_round_trip(admin_client):
    app, client = admin_client
    credential_id = _add_qoder(app)

    response = client.put(f"/api/credentials/{credential_id}/web-cookie",
                          json={"cookie": "  qoder_session_cookie=abc; other=1  "})
    assert response.status_code == 200 and response.json() == {"ok": True}
    # 落库的是 trim 后的值；其余凭证字段保持原样
    data = app.state.credentials.credential_data(credential_id)
    assert data["web_cookie"] == "qoder_session_cookie=abc; other=1"
    assert data["access_token"] == "dt-1" and data["uid"] == "u-1"
    # 返回体不回显 Cookie
    assert "abc" not in response.text

    events = app.state.audit.query(action=ACTION_CREDENTIAL_WEB_COOKIE)
    assert len(events) == 1
    assert events[0]["target"] == credential_id
    assert events[0]["detail"] == "已设置"
    assert "abc" not in str(events[0])

    # 空串 = 清除，探测回退汇总口径
    cleared = client.put(f"/api/credentials/{credential_id}/web-cookie",
                         json={"cookie": ""})
    assert cleared.status_code == 200
    assert app.state.credentials.credential_data(credential_id)["web_cookie"] == ""
    details = [e["detail"] for e in
               app.state.audit.query(action=ACTION_CREDENTIAL_WEB_COOKIE)]
    assert "已清除" in details


def test_web_cookie_validation_and_capability(admin_client):
    app, client = admin_client
    qoder = _add_qoder(app)
    codebuddy = _add_codebuddy(app)

    # 非字符串 / 超长都 400（不做 Cookie 语法校验，上游结构会变）
    assert client.put(f"/api/credentials/{qoder}/web-cookie",
                      json={"cookie": 1}).status_code == 400
    assert client.put(f"/api/credentials/{qoder}/web-cookie",
                      json={"cookie": "x" * 10_001}).status_code == 400
    # 缺失凭证 / 渠道不支持该能力
    assert client.put("/api/credentials/ghost/web-cookie",
                      json={"cookie": "sid=1"}).status_code == 400
    assert client.put(f"/api/credentials/{codebuddy}/web-cookie",
                      json={"cookie": "sid=1"}).status_code == 400


def test_export_omits_web_cookie(admin_client):
    """导出件本来就有明文 token，不该再多一份浏览器会话凭据。"""
    app, client = admin_client
    credential_id = _add_qoder(app)
    client.put(f"/api/credentials/{credential_id}/web-cookie",
               json={"cookie": "qoder_session_cookie=abc"})

    exported = client.get(f"/api/credentials/{credential_id}/export").json()
    assert "web_cookie" not in exported["credential"]
    # 落库值仍在（导出只是脱这项）
    assert app.state.credentials.credential_data(credential_id)["web_cookie"] == (
        "qoder_session_cookie=abc")

def _add_codearts(app):
    return app.state.credentials.add(
        provider="codearts",
        credential_data={"access_key_id": "AK", "secret_access_key": "SK"})


def test_codearts_web_cookie_and_export_omits_it(admin_client):
    """CodeArts 同样支持网页 Cookie；导出件不得带浏览器会话凭据。"""
    app, client = admin_client
    credential_id = _add_codearts(app)

    response = client.put(f"/api/credentials/{credential_id}/web-cookie",
                          json={"cookie": "  devclouddevuibjJ_SESSION_ID=abc  "})
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert "abc" not in response.text
    assert app.state.credentials.credential_data(credential_id)["web_cookie"] == (
        "devclouddevuibjJ_SESSION_ID=abc")

    exported = client.get(f"/api/credentials/{credential_id}/export").json()
    assert "web_cookie" not in exported["credential"]
    assert exported["credential"]["access_key_id"] == "AK"


def test_list_exposes_web_cookie_state_and_setting_resets_it(admin_client):
    """管理台提示走列表字段；重新粘贴后立即清掉旧标记（下轮探测再定）。"""
    import time

    from src.provider.base import Quota

    app, client = admin_client
    credential_id = _add_codearts(app)
    app.state.credentials.save_quota(
        credential_id,
        Quota(remaining=1.0, total=2.0, probed_at=int(time.time()),
              web_cookie_state="expired"))

    rows = client.get("/api/credentials").json()["credentials"]
    row = next(item for item in rows if item["id"] == credential_id)
    assert row["web_cookie_state"] == "expired"

    response = client.put(f"/api/credentials/{credential_id}/web-cookie",
                          json={"cookie": "sid=new"})
    assert response.status_code == 200
    assert app.state.credentials.credential_data(credential_id)["web_cookie"] == "sid=new"
    rows = client.get("/api/credentials").json()["credentials"]
    assert next(item for item in rows if item["id"] == credential_id)[
        "web_cookie_state"] is None

"""小浣熊「账号密码」设置端点：写入/清除、校验、审计、导出不泄漏。"""

from __future__ import annotations

from src.audit.actions import ACTION_CREDENTIAL_LOGIN_SECRET


def _add_raccoon(app, *, credential=None):
    return app.state.credentials.add(
        provider="raccoon",
        credential_data=credential or {"access_token": "tok", "uid": "u-1"})


def _add_codebuddy(app):
    return app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t"})


def test_set_and_clear_login_secret_round_trip(admin_client):
    app, client = admin_client
    credential_id = _add_raccoon(app)

    response = client.put(f"/api/credentials/{credential_id}/login-secret",
                          json={"phone": "  +86 139-0000-0001 ", "password": "Abcd123456"})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "login_secret": 1}
    # 落库：手机号 trim，密码原样；其余凭证字段不动
    data = app.state.credentials.credential_data(credential_id)
    assert data["phone"] == "+86 139-0000-0001"
    assert data["password"] == "Abcd123456"
    assert data["access_token"] == "tok" and data["uid"] == "u-1"
    # 返回体与审计都不回显密码
    assert "Abcd123456" not in response.text
    events = app.state.audit.query(action=ACTION_CREDENTIAL_LOGIN_SECRET)
    assert len(events) == 1 and events[0]["detail"] == "已保存"
    assert "Abcd123456" not in str(events[0])
    # 列表页只拿到「已保存」标记，不需要解密
    row = next(item for item in client.get("/api/credentials").json()["credentials"]
               if item["id"] == credential_id)
    assert row["login_secret"] == 1

    cleared = client.put(f"/api/credentials/{credential_id}/login-secret",
                         json={"phone": "", "password": ""})
    assert cleared.status_code == 200
    assert cleared.json() == {"ok": True, "login_secret": 0}
    data = app.state.credentials.credential_data(credential_id)
    assert data["phone"] == "" and data["password"] == ""
    assert app.state.credentials.list_all()[0]["login_secret"] is None
    details = [e["detail"] for e in
               app.state.audit.query(action=ACTION_CREDENTIAL_LOGIN_SECRET)]
    assert "已清除" in details


def test_login_secret_validation_and_capability(admin_client):
    app, client = admin_client
    raccoon = _add_raccoon(app)
    codebuddy = _add_codebuddy(app)

    # 非字符串 / 只填一半 / 超长都 400
    assert client.put(f"/api/credentials/{raccoon}/login-secret",
                      json={"phone": 1, "password": "Abcd123456"}).status_code == 400
    assert client.put(f"/api/credentials/{raccoon}/login-secret",
                      json={"phone": "139", "password": ""}).status_code == 400
    assert client.put(f"/api/credentials/{raccoon}/login-secret",
                      json={"phone": "", "password": "Abcd123456"}).status_code == 400
    assert client.put(f"/api/credentials/{raccoon}/login-secret",
                      json={"phone": "1" * 33, "password": "Abcd123456"}).status_code == 400
    assert client.put(f"/api/credentials/{raccoon}/login-secret",
                      json={"phone": "139", "password": "x" * 129}).status_code == 400
    # 缺失凭证 / 渠道不支持该能力
    assert client.put("/api/credentials/ghost/login-secret",
                      json={"phone": "139", "password": "Abcd123456"}).status_code == 400
    assert client.put(f"/api/credentials/{codebuddy}/login-secret",
                      json={"phone": "139", "password": "Abcd123456"}).status_code == 400


def test_export_omits_login_password(admin_client):
    """导出件有明文 token，不该再多一份能改密的账号密码。"""
    app, client = admin_client
    credential_id = _add_raccoon(app)
    client.put(f"/api/credentials/{credential_id}/login-secret",
               json={"phone": "13900000001", "password": "Abcd123456"})

    exported = client.get(f"/api/credentials/{credential_id}/export").json()
    assert "password" not in exported["credential"]
    # 手机号保留（单独存在无法登录，便于核对是哪个号）
    assert exported["credential"]["phone"] == "13900000001"
    # 原凭证仍完整
    assert app.state.credentials.credential_data(credential_id)["password"] == (
        "Abcd123456")

def test_set_login_secret_holds_edit_lock(admin_client):
    """账号密码写入同样走编辑锁（与 web-cookie 同一根因）。"""
    app, client = admin_client
    credential_id = _add_raccoon(app)
    lock = app.state.credentials.edit_lock(credential_id)

    seen: list[bool] = []
    original_save = app.state.credentials.save_credential_data

    def spy(*args, **kwargs):
        seen.append(lock.locked())
        return original_save(*args, **kwargs)

    app.state.credentials.save_credential_data = spy
    try:
        response = client.put(f"/api/credentials/{credential_id}/login-secret",
                              json={"phone": "13800000000", "password": "pw"})
    finally:
        app.state.credentials.save_credential_data = original_save
    assert response.status_code == 200
    assert seen == [True]
    assert lock.locked() is False

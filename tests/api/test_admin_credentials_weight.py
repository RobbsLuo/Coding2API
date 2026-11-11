"""凭证调度权重端点：设置/清除、校验、审计、列表回显。

权重只影响选号顺序（`Scheduler.select` 同健康档内重排），不改变可调度性，
因此端点不做任何缓存失效。
"""

from __future__ import annotations

from src.audit.actions import ACTION_CREDENTIAL_WEIGHT


def _add_credential(app):
    return app.state.credentials.add(
        provider="trae", credential_data={"accessToken": "t-1"})


def _weight_of(app, credential_id):
    return next(row for row in app.state.credentials.list_all()
                if row["id"] == credential_id)["routing_weight"]


def test_set_and_clear_weight(admin_client):
    app, client = admin_client
    credential_id = _add_credential(app)
    assert _weight_of(app, credential_id) is None

    response = client.put(f"/api/credentials/{credential_id}/weight",
                          json={"weight": 42})
    assert response.status_code == 200
    assert response.json() == {"routing_weight": 42}
    assert _weight_of(app, credential_id) == 42

    events = app.state.audit.query(action=ACTION_CREDENTIAL_WEIGHT)
    assert len(events) == 1
    assert events[0]["target"] == credential_id
    assert events[0]["detail"] == "42"

    # null = 恢复默认
    cleared = client.put(f"/api/credentials/{credential_id}/weight",
                         json={"weight": None})
    assert cleared.status_code == 200
    assert cleared.json() == {"routing_weight": None}
    assert _weight_of(app, credential_id) is None
    assert sorted(e["detail"] for e in
                  app.state.audit.query(action=ACTION_CREDENTIAL_WEIGHT)) == sorted(
                      ["42", "（默认）"])


def test_negative_weight_is_allowed(admin_client):
    """负数 = 尽量靠后（备用号），与 0（默认）区分。"""
    app, client = admin_client
    credential_id = _add_credential(app)
    assert client.put(f"/api/credentials/{credential_id}/weight",
                      json={"weight": -100}).status_code == 200
    assert _weight_of(app, credential_id) == -100


def test_weight_validation(admin_client):
    app, client = admin_client
    credential_id = _add_credential(app)
    url = f"/api/credentials/{credential_id}/weight"

    # 布尔是 int 的子类，必须单独拒掉（{"weight": true} 不能当 1）
    assert client.put(url, json={"weight": True}).status_code == 400
    assert client.put(url, json={"weight": "5"}).status_code == 400
    assert client.put(url, json={"weight": 1001}).status_code == 400
    assert client.put(url, json={"weight": -1001}).status_code == 400
    # 缺失凭证
    assert client.put("/api/credentials/ghost/weight",
                      json={"weight": 1}).status_code == 400
    # 失败的请求不写审计、不改库
    assert _weight_of(app, credential_id) is None
    assert app.state.audit.query(action=ACTION_CREDENTIAL_WEIGHT) == []

"""访问冻结管理端点：operator+ 列表 / 解冻 + 审计。"""

from __future__ import annotations

from src.auth.rbac import ROLE_ADMIN, ROLE_VIEWER


def _seed(app):
    app.state.access_freezes.record_hit(subject="alice", provider="trae",
                                        reason="code=11128", now=10)
    app.state.access_freezes.record_hit(subject="alice", provider="trae",
                                        reason="code=11128", now=20)
    app.state.access_freezes.record_hit(subject="bob", provider="codebuddy",
                                        reason="code=11128", now=30)


def test_list_access_freezes_aggregates_and_marks_frozen(admin_client):
    app, client = admin_client
    _seed(app)
    app.state.runtime_settings.set("access_freeze_threshold", 2)
    try:
        payload = client.get("/api/access-freezes").json()
        assert payload["threshold"] == 2
        assert payload["total"] == 2
        rows = {row["subject"]: row for row in payload["entries"]}
        assert rows["alice"]["hits"] == 2 and rows["alice"]["frozen"] is True
        assert rows["bob"]["hits"] == 1 and rows["bob"]["frozen"] is False

        filtered = client.get("/api/access-freezes?subject=alice").json()
        assert [row["provider"] for row in filtered["entries"]] == ["trae"]
        filtered = client.get("/api/access-freezes?provider=codebuddy").json()
        assert [row["subject"] for row in filtered["entries"]] == ["bob"]
        paged = client.get("/api/access-freezes?limit=1&offset=1").json()
        assert len(paged["entries"]) == 1 and paged["total"] == 2
    finally:
        app.state.runtime_settings.reset("access_freeze_threshold")


def test_unfreeze_removes_hits_and_writes_audit(admin_client):
    app, client = admin_client
    _seed(app)
    response = client.post("/api/access-freezes/unfreeze",
                           json={"subject": "alice", "provider": "trae"})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "removed": 2}
    assert app.state.access_freezes.count(subject="alice", provider="trae") == 0
    assert app.state.access_freezes.count(subject="bob", provider="codebuddy") == 1
    events = app.state.audit.query(action="access_freeze.unfreeze")
    assert events[0]["target"] == "alice@trae"
    assert "2 条" in events[0]["detail"]


def test_unfreeze_requires_subject_and_provider(admin_client):
    _app, client = admin_client
    response = client.post("/api/access-freezes/unfreeze", json={"subject": "alice"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_list_requires_operator(admin_client):
    app, client = admin_client
    app.state.user_repo.create("viewer", "x", role=ROLE_VIEWER)
    from tests.helpers.auth import set_session_cookie
    set_session_cookie(client, "viewer")
    response = client.get("/api/access-freezes")
    assert response.status_code == 403


def test_unfreeze_is_idempotent(admin_client):
    app, client = admin_client
    response = client.post("/api/access-freezes/unfreeze",
                           json={"subject": "ghost", "provider": "trae"})
    assert response.status_code == 200
    assert response.json()["removed"] == 0
    events = app.state.audit.query(action="access_freeze.unfreeze")
    assert events[0]["target"] == "ghost@trae"


def test_admin_role_can_list(admin_client):
    app, client = admin_client
    app.state.user_repo.create("admin2", "x", role=ROLE_ADMIN)
    from tests.helpers.auth import set_session_cookie
    set_session_cookie(client, "admin2")
    assert client.get("/api/access-freezes").status_code == 200
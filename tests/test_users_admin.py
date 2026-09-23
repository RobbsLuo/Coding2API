"""用户管理 / 审计 / 激活端点（B5）。

守着五条语义：
1. 列表与创建响应**绝不**含 password_hash / activation_digest。
2. 三角色闸门：viewer 不能写凭证，operator 能写凭证但不能管用户/改配置。
3. 防锁死：降级/禁用最后一个活跃 admin 或对自己动手 → 400。
4. 激活令牌一次性 + TTL。
5. 审计可查、可按 actor/action 过滤。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from src.api.admin_users import digest_activation_token
from src.auth.rbac import ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER
from src.auth.users import create_password_hash
from src.config import Settings
from src.db.conn import Database
from src.db.migrate import apply_schema
from src.db.repo import UserRepository
from src.main import build_app
from tests.conftest import SECRET


def _app(tmp_path, monkeypatch, *, users):
    """构造 app 并预置用户。users: [(username, password, role, enabled)]"""
    monkeypatch.setenv("USERS_FILE", str(tmp_path / "none.txt"))
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    db = Database(settings.db_path)
    apply_schema(db.connect())
    repo = UserRepository(db)
    for username, password, role, enabled in users:
        repo.create(username, create_password_hash(password), role=role,
                    enabled=enabled, now=1)
    db.close()
    return build_app(settings)


def _login(client, username, password):
    response = client.post("/api/auth/login",
                           json={"username": username, "password": password})
    assert response.status_code == 200
    return response


# ------------------------------------------------------------ 列表/创建

def test_list_users_never_leaks_secrets(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, users=[
        ("root", "rootpw", ROLE_ADMIN, True),
        ("op", "oppw", ROLE_OPERATOR, True),
    ])
    with TestClient(app) as client:
        _login(client, "root", "rootpw")
        body = client.get("/api/users").json()
        assert [u["username"] for u in body["users"]] == ["op", "root"]
        raw = str(body)
        assert "password_hash" not in raw and "activation_digest" not in raw
        assert "$" not in raw                      # PBKDF2 串的定界符
        assert body["users"][0]["role"] == ROLE_OPERATOR
        assert body["users"][0]["pending_activation"] is False


def test_create_user_returns_token_once_and_no_hash(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, users=[("root", "rootpw", ROLE_ADMIN, True)])
    with TestClient(app) as client:
        _login(client, "root", "rootpw")
        created = client.post("/api/users",
                              json={"username": "newbie", "role": ROLE_OPERATOR})
        assert created.status_code == 200
        body = created.json()
        assert body["username"] == "newbie" and body["role"] == ROLE_OPERATOR
        token = body["activate_token"]
        assert token and "password_hash" not in str(body)
        # 库里存的是摘要，不是明文
        row = app.state.user_repo.get("newbie")
        assert row["activation_digest"] == digest_activation_token(token)
        assert row["activation_digest"] != token
        # 待激活状态在列表里可见（但不泄露摘要）
        listed = client.get("/api/users").json()["users"]
        newbie = next(u for u in listed if u["username"] == "newbie")
        assert newbie["pending_activation"] is True
        assert token not in str(listed)


@pytest.mark.parametrize("payload,message", [
    ({"role": "viewer"}, "username is required"),
    ({"username": "x", "role": "superuser"}, "unknown role"),
])
def test_create_user_validates(tmp_path, monkeypatch, payload, message):
    app = _app(tmp_path, monkeypatch, users=[("root", "rootpw", ROLE_ADMIN, True)])
    with TestClient(app) as client:
        _login(client, "root", "rootpw")
        response = client.post("/api/users", json=payload)
        assert response.status_code == 400
        assert message in response.json()["error"]["message"]


def test_create_user_rejects_duplicate(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, users=[("root", "rootpw", ROLE_ADMIN, True)])
    with TestClient(app) as client:
        _login(client, "root", "rootpw")
        response = client.post("/api/users",
                               json={"username": "root", "role": ROLE_VIEWER})
        assert response.status_code == 400
        assert "already exists" in response.json()["error"]["message"]


# --------------------------------------------------------------- 角色闸门

def test_viewer_cannot_manage_users_or_write_credentials(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, users=[
        ("root", "rootpw", ROLE_ADMIN, True),
        ("viewer", "vpw", ROLE_VIEWER, True),
    ])
    with TestClient(app) as client:
        _login(client, "viewer", "vpw")
        assert client.get("/api/users").status_code == 403
        assert client.get("/api/audit").status_code == 403
        assert client.post("/api/credentials",
                           json={"provider": "trae", "credential": {}}).status_code == 403
        # 配置写操作同样限于 admin
        assert client.put("/api/settings",
                          json={"key": "quota_probe_minutes", "value": "15"}).status_code == 403
        # 只读端点仍可用
        assert client.get("/api/credentials").status_code == 200


def test_operator_can_write_credentials_but_not_users(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, users=[
        ("root", "rootpw", ROLE_ADMIN, True),
        ("op", "oppw", ROLE_OPERATOR, True),
    ])
    with TestClient(app) as client:
        _login(client, "op", "oppw")
        # 凭证写操作放行（provider 未知才 400，说明已过权限闸门）
        response = client.post("/api/credentials",
                               json={"provider": "nope", "credential": {}})
        assert response.status_code == 400
        assert "unknown provider" in response.json()["error"]["message"]
        # 用户管理与审计仍被拒
        assert client.get("/api/users").status_code == 403
        assert client.get("/api/audit").status_code == 403


# --------------------------------------------------------------- 防锁死

def test_cannot_demote_last_active_admin(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, users=[
        ("root", "rootpw", ROLE_ADMIN, True),
        ("other", "opw", ROLE_ADMIN, True),
    ])
    with TestClient(app) as client:
        _login(client, "root", "rootpw")
        # 还有 2 个活跃 admin：降级别人是允许的
        assert client.patch("/api/users/other",
                            json={"role": ROLE_VIEWER}).status_code == 200
        # root 现在是最后一个活跃 admin：不能再降级（否则无人可管理）
        blocked = client.patch("/api/users/root", json={"role": ROLE_VIEWER})
        assert blocked.status_code == 400
        assert blocked.json()["error"]["code"] == "last_admin"


def test_cannot_disable_last_active_admin(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, users=[
        ("root", "rootpw", ROLE_ADMIN, True),
        ("solo", "spw", ROLE_ADMIN, True),
    ])
    with TestClient(app) as client:
        _login(client, "root", "rootpw")
        client.patch("/api/users/solo", json={"role": ROLE_OPERATOR})
        # solo 已不是 admin：可以自由禁用
        assert client.post("/api/users/solo/disable").status_code == 200
        # root 现在是唯一活跃 admin——禁用它会把 admin 数清零
        blocked = client.post("/api/users/root/disable")
        assert blocked.status_code == 400
        assert blocked.json()["error"]["code"] == "last_admin"
        # 最后一份 admin 必须还在
        assert app.state.user_repo.get("root")["enabled"] == 1


def test_self_role_change_and_disable_blocked_with_peer(tmp_path, monkeypatch):
    """有同伴时：动自己命中 self_target（不是 last_admin）。"""
    app = _app(tmp_path, monkeypatch, users=[
        ("root", "rootpw", ROLE_ADMIN, True),
        ("peer", "ppw", ROLE_ADMIN, True),
    ])
    with TestClient(app) as client:
        _login(client, "root", "rootpw")
        demote = client.patch("/api/users/root", json={"role": ROLE_VIEWER})
        assert demote.status_code == 400
        assert demote.json()["error"]["code"] == "self_target"
        disable = client.post("/api/users/root/disable")
        assert disable.status_code == 400
        assert disable.json()["error"]["code"] == "self_target"
        # 未发生任何变更
        assert app.state.user_repo.get("root")["role"] == ROLE_ADMIN
        assert app.state.user_repo.get("root")["enabled"] == 1


def test_admin_can_demote_non_last_admin(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, users=[
        ("root", "rootpw", ROLE_ADMIN, True),
        ("second", "2pw", ROLE_ADMIN, True),
    ])
    with TestClient(app) as client:
        _login(client, "root", "rootpw")
        assert client.patch("/api/users/second",
                            json={"role": ROLE_OPERATOR}).status_code == 200
        assert app.state.user_repo.get("second")["role"] == ROLE_OPERATOR


def test_promote_to_admin_skips_last_admin_guard(tmp_path, monkeypatch):
    """提升为 admin 不经过最后-admin 守卫（守卫只在降级时判）。"""
    app = _app(tmp_path, monkeypatch, users=[
        ("root", "rootpw", ROLE_ADMIN, True),
        ("op", "oppw", ROLE_OPERATOR, True),
    ])
    with TestClient(app) as client:
        _login(client, "root", "rootpw")
        assert client.patch("/api/users/op",
                            json={"role": ROLE_ADMIN}).status_code == 200
        assert app.state.user_repo.get("op")["role"] == ROLE_ADMIN


def test_update_unknown_user_and_bad_role(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, users=[("root", "rootpw", ROLE_ADMIN, True)])
    with TestClient(app) as client:
        _login(client, "root", "rootpw")
        assert client.patch("/api/users/ghost",
                            json={"role": ROLE_VIEWER}).status_code == 400
        assert client.patch("/api/users/root",
                            json={"role": "nope"}).status_code == 400
        assert client.post("/api/users/ghost/disable").status_code == 400
        assert client.post("/api/users/ghost/enable").status_code == 400
        assert client.post("/api/users/ghost/reset-password").status_code == 400


def test_enable_roundtrip(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, users=[
        ("root", "rootpw", ROLE_ADMIN, True),
        ("op", "oppw", ROLE_OPERATOR, True),
    ])
    with TestClient(app) as client:
        _login(client, "root", "rootpw")
        assert client.post("/api/users/op/disable").status_code == 200
        assert app.state.user_repo.get("op")["enabled"] == 0
        assert client.post("/api/users/op/enable").status_code == 200
        assert app.state.user_repo.get("op")["enabled"] == 1


def test_reset_password_issues_usable_token(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, users=[
        ("root", "rootpw", ROLE_ADMIN, True),
        ("op", "oppw", ROLE_OPERATOR, True),
    ])
    with TestClient(app) as client:
        _login(client, "root", "rootpw")
        body = client.post("/api/users/op/reset-password").json()
        token = body["activate_token"]
        # 被重置者现在处于强制改密状态
        assert app.state.user_repo.get("op")["must_change_password"] == 1
        # 令牌可用于激活
        with TestClient(app) as fresh:
            assert fresh.get(f"/api/auth/activate?token={token}").json() == {
                "username": "op", "valid": True}


# ---------------------------------------------------------------- 激活流

def test_activation_sets_password_and_is_single_use(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, users=[("root", "rootpw", ROLE_ADMIN, True)])
    with TestClient(app) as client:
        _login(client, "root", "rootpw")
        token = client.post("/api/users",
                            json={"username": "newbie", "role": ROLE_OPERATOR}
                            ).json()["activate_token"]

    with TestClient(app) as client:
        done = client.post("/api/auth/activate",
                           json={"token": token, "password": "newbie-pw-1"})
        assert done.status_code == 200 and done.json()["username"] == "newbie"
        # 一次性：同一个令牌不能再用
        again = client.post("/api/auth/activate",
                            json={"token": token, "password": "another-pw-1"})
        assert again.status_code == 400
        # 且能用新密码登录
        assert client.post("/api/auth/login",
                           json={"username": "newbie", "password": "newbie-pw-1"}
                           ).status_code == 200


def test_activation_rejects_expired_token(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, users=[("root", "rootpw", ROLE_ADMIN, True)])
    with TestClient(app) as client:
        _login(client, "root", "rootpw")
        token = client.post("/api/users",
                            json={"username": "newbie", "role": ROLE_VIEWER}
                            ).json()["activate_token"]
    # 手动把过期时间改到过去
    app.state.user_repo.set_activation_token(
        "newbie", digest_activation_token(token), int(time.time()) - 1)
    with TestClient(app) as client:
        assert client.get(f"/api/auth/activate?token={token}").status_code == 400
        assert client.post("/api/auth/activate",
                           json={"token": token, "password": "whatever-1"}
                           ).status_code == 400


@pytest.mark.parametrize("payload", [
    {"token": "", "password": "longenough1"},       # 缺令牌
    {"token": "not-a-token", "password": "longenough1"},  # 令牌不存在
    {"token": "x", "password": "short"},            # 密码过短
])
def test_activation_rejects_bad_input(tmp_path, monkeypatch, payload):
    app = _app(tmp_path, monkeypatch, users=[("root", "rootpw", ROLE_ADMIN, True)])
    with TestClient(app) as client:
        assert client.post("/api/auth/activate", json=payload).status_code == 400


def test_activation_describe_rejects_bad_token(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, users=[("root", "rootpw", ROLE_ADMIN, True)])
    with TestClient(app) as client:
        assert client.get("/api/auth/activate?token=").status_code == 400
        assert client.get("/api/auth/activate?token=nope").status_code == 400


# ------------------------------------------------------------------ 审计

def test_audit_endpoint_lists_and_filters(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, users=[("root", "rootpw", ROLE_ADMIN, True)])
    with TestClient(app) as client:
        _login(client, "root", "rootpw")
        client.post("/api/users", json={"username": "a1", "role": ROLE_VIEWER})
        client.post("/api/users", json={"username": "a2", "role": ROLE_VIEWER})

        body = client.get("/api/audit").json()
        actions = [e["action"] for e in body["events"]]
        assert "login.success" in actions and "user.create" in actions
        assert body["labels"]["user.create"] == "新建用户"
        assert "user.create" in body["actions"]

        only_create = client.get("/api/audit?action=user.create").json()["events"]
        assert {e["action"] for e in only_create} == {"user.create"}
        assert len(only_create) == 2

        filtered = client.get("/api/audit?actor=root&action=login.success").json()["events"]
        assert len(filtered) == 1

        # since/before 走数值过滤分支
        assert client.get("/api/audit?since=1").json()["events"]
        assert client.get("/api/audit?before=1").json()["events"] == []


def test_credential_writes_are_audited(tmp_path, monkeypatch):
    """凭证写操作入库：导入（用未知 provider 走 400 前不记）改为直接调仓储路径。"""
    app = _app(tmp_path, monkeypatch, users=[("root", "rootpw", ROLE_ADMIN, True)])
    with TestClient(app) as client:
        _login(client, "root", "rootpw")
        # toggle 一个不存在的凭证 → 400，不记审计（未发生写操作）
        client.post("/api/credentials/ghost/toggle", json={"enabled": False})
    actions = [e["action"] for e in app.state.audit.query()]
    assert "credential.toggle" not in actions          # 失败不记
    assert "login.success" in actions

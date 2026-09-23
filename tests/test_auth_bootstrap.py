"""账号身份层（B5）：DbUserStore、bootstrap、三角色 RBAC、会话 epoch、强制改密。

这一组守着四条容易退化的语义：
1. 角色/启用状态从 DB **每请求现读**——改完立即生效，不靠重新登录。
2. epoch 不匹配即失效；缺 ep 的老 Cookie 按 0 放行（升级不能踢光所有人）。
3. 首登改密只放行三个端点，`/api/auth/upstream/*` 也被拦住。
4. 引导三层：导入 → 提权 → 无活跃 admin 时兑回或报错。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.auth.bootstrap import bootstrap_users
from src.auth.rbac import (
    ROLE_ADMIN,
    ROLE_OPERATOR,
    ROLE_VIEWER,
    ForbiddenError,
    LastAdminError,
    Principal,
    SelfTargetError,
    require_admin,
    require_operator,
)
from src.auth.session import create_session_token, verify_session_token
from src.auth.users import (
    DbUserStore,
    UsersFileError,
    UsersFileStore,
    create_password_hash,
)
from src.config import Settings
from src.db.conn import Database
from src.db.migrate import apply_schema
from src.db.repo import UserRepository
from src.main import build_app
from tests.conftest import SECRET


class _Config:
    """最小 config 桩：bootstrap 只用到 admin_set 一个属性。"""

    def __init__(self, admins=()) -> None:
        self.admin_set = frozenset(admins)


def _repo(tmp_path):
    db = Database(tmp_path / "b.sqlite3")
    apply_schema(db.connect())
    return db, UserRepository(db)


def _file(tmp_path, rows):
    path = tmp_path / "users.txt"
    path.write_text("".join(f"{u}:{h}\n" for u, h in rows), encoding="utf-8")
    return UsersFileStore(path)


# ----------------------------------------------------------------- rbac

def test_is_operator_covers_admin_and_operator():
    assert Principal("a", is_admin=True, role=ROLE_ADMIN).is_operator is True
    assert Principal("b", role=ROLE_OPERATOR).is_operator is True
    assert Principal("c", role=ROLE_VIEWER).is_operator is False
    # 位置参数老构造（is_admin 在第 2 位）仍然算 operator
    assert Principal("d", True).is_operator is True


def test_require_operator_raises_for_viewer():
    assert require_operator(Principal("op", role=ROLE_OPERATOR)).username == "op"
    with pytest.raises(ForbiddenError):
        require_operator(Principal("v", role=ROLE_VIEWER))


def test_last_admin_and_self_target_errors_exist():
    """两个防锁死异常是独立类型（端点据此回 400 而非 403）。"""
    assert issubclass(LastAdminError, Exception)
    assert issubclass(SelfTargetError, Exception)


def test_require_admin_accepts_legacy_positional():
    assert require_admin(Principal("root", True)).username == "root"
    with pytest.raises(ForbiddenError):
        require_admin(Principal("u", False))


# -------------------------------------------------------------- session

def test_session_embeds_and_returns_epoch():
    token = create_session_token("alice", "sec", epoch=7, issued_at=1000, ttl_seconds=60)
    assert verify_session_token(token, "sec", now=1030) == ("alice", 7)


def test_session_token_without_epoch_defaults_to_zero():
    """老 Cookie 没有 ep 字段：按 0 放行，不能判非法（升级平滑）。"""
    import base64
    import json

    from src.auth import session as session_mod

    raw = json.dumps({"u": "alice", "iat": 1000, "exp": 2000},
                     separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    legacy = f"{payload_b64}.{session_mod._sign(payload_b64, 'sec')}"
    assert verify_session_token(legacy, "sec", now=1500) == ("alice", 0)


@pytest.mark.parametrize("epoch", ["7", True, -1])
def test_session_rejects_invalid_epoch(epoch):
    """显式写了非法 ep 才拒绝：字符串/bool/负数。"""
    import base64
    import json

    from src.auth import session as session_mod

    raw = json.dumps({"u": "alice", "ep": epoch, "iat": 1000, "exp": 2000},
                     separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    token = f"{payload_b64}.{session_mod._sign(payload_b64, 'sec')}"
    assert verify_session_token(token, "sec", now=1500) is None


# ---------------------------------------------------------- DbUserStore

def test_db_user_store_reads(tmp_path):
    db, repo = _repo(tmp_path)
    repo.create("alice", create_password_hash("pw"), role=ROLE_OPERATOR, now=1)
    store = DbUserStore(repo)
    assert store.verify("alice", "pw") is True
    assert store.verify("alice", "no") is False
    assert store.verify("ghost", "pw") is False
    assert store.has("alice") is True and store.has("ghost") is False
    assert store.is_active("alice") is True and store.is_active("ghost") is False
    assert store.role_of("alice") == ROLE_OPERATOR and store.role_of("ghost") is None
    assert store.session_epoch("alice") == 0 and store.session_epoch("ghost") is None
    assert store.must_change_password("alice") is False
    assert store.must_change_password("ghost") is False
    assert store.list_usernames() == ("alice",)
    store.validate()
    db.close()


def test_db_user_store_rejects_disabled(tmp_path):
    db, repo = _repo(tmp_path)
    repo.create("alice", create_password_hash("pw"), enabled=False, now=1)
    store = DbUserStore(repo)
    assert store.verify("alice", "pw") is False
    assert store.has("alice") is True          # 存在
    assert store.is_active("alice") is False   # 但不可用
    db.close()


def test_db_user_store_reports_must_change(tmp_path):
    db, repo = _repo(tmp_path)
    repo.create("alice", create_password_hash("pw"), must_change_password=True, now=1)
    assert DbUserStore(repo).must_change_password("alice") is True
    db.close()


def test_db_user_store_validate_requires_users(tmp_path):
    db, repo = _repo(tmp_path)
    with pytest.raises(UsersFileError):
        DbUserStore(repo).validate()
    db.close()


# ------------------------------------------------------------- bootstrap

def test_bootstrap_imports_viewer_then_promotes_env_admin(tmp_path):
    db, repo = _repo(tmp_path)
    store = _file(tmp_path, [("root", create_password_hash("a")),
                             ("alice", create_password_hash("b"))])
    logs: list[str] = []
    bootstrap_users(_Config(["root"]), repo, file_store=store,
                    log=lambda msg, *args: logs.append(msg % args))

    assert repo.get("root")["role"] == ROLE_ADMIN
    assert repo.get("alice")["role"] == ROLE_VIEWER
    assert any("导入 2 个用户" in line for line in logs)
    assert any("提权为 admin" in line for line in logs)
    db.close()


def test_bootstrap_is_idempotent_and_keeps_managed_changes(tmp_path):
    """重复引导不得把管理台改过的密码/角色冲回文件值。"""
    db, repo = _repo(tmp_path)
    store = _file(tmp_path, [("root", create_password_hash("a")),
                             ("alice", create_password_hash("b"))])
    bootstrap_users(_Config(["root"]), repo, file_store=store)
    repo.set_password("alice", create_password_hash("changed"))
    repo.update_role("alice", ROLE_OPERATOR)

    bootstrap_users(_Config(["root"]), repo, file_store=store)
    assert repo.get("alice")["role"] == ROLE_OPERATOR
    assert repo.get("alice")["password_hash"] != create_password_hash("b")
    db.close()


def test_bootstrap_without_file_uses_existing_db(tmp_path):
    """没有 users.txt 时只要库里有用户就正常（纯 DB 部署）。"""
    db, repo = _repo(tmp_path)
    repo.create("root", create_password_hash("a"), role=ROLE_ADMIN, now=1)
    bootstrap_users(_Config(), repo, file_store=None)
    db.close()


def test_bootstrap_fails_when_no_users_anywhere(tmp_path):
    db, repo = _repo(tmp_path)
    with pytest.raises(UsersFileError, match="no authentication users available"):
        bootstrap_users(_Config(), repo, file_store=None)
    db.close()


def test_bootstrap_promotes_when_no_active_admin(tmp_path):
    """库里有用户但无活跃 admin：兑回 env 指名者。"""
    db, repo = _repo(tmp_path)
    repo.create("root", create_password_hash("a"), role=ROLE_VIEWER, now=1)
    bootstrap_users(_Config(["root"]), repo)
    assert repo.get("root")["role"] == ROLE_ADMIN
    db.close()


def test_bootstrap_ignores_env_name_without_user(tmp_path):
    """env 指了个库里没有的用户：不报错，也不算有 admin。"""
    db, repo = _repo(tmp_path)
    repo.create("alice", create_password_hash("a"), role=ROLE_ADMIN, now=1)
    bootstrap_users(_Config(["ghost"]), repo)
    assert repo.get("alice")["role"] == ROLE_ADMIN
    db.close()


def test_bootstrap_fails_when_only_admin_candidate_is_disabled(tmp_path):
    """唯一候选被禁用：不自动启用（那等于绕过有意的禁用），走恢复路径报错。"""
    db, repo = _repo(tmp_path)
    repo.create("root", create_password_hash("a"), role=ROLE_VIEWER,
                enabled=False, now=1)
    with pytest.raises(UsersFileError, match="no active admin account"):
        bootstrap_users(_Config(["root"]), repo)
    assert repo.get("root")["enabled"] == 0
    db.close()


# ------------------------------------------------- 端到端：角色与 epoch

def test_role_read_from_db_not_cookie(tmp_path, monkeypatch):
    """改角色后旧 Cookie 立即失去 admin（角色不进签名 Cookie）。"""
    monkeypatch.setenv("USERS_FILE", str(tmp_path / "none.txt"))
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    db = Database(settings.db_path)
    apply_schema(db.connect())
    repo = UserRepository(db)
    repo.create("root", create_password_hash("rootpw"), role=ROLE_ADMIN, now=1)
    token = create_session_token("root", SECRET, epoch=0)

    app = build_app(settings)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", token)
        assert client.get("/api/auth/session").json()["role"] == ROLE_ADMIN
        # 降级后同一个 Cookie（epoch 会被 bump，故这里重新签发以只测角色）
        repo.update_role("root", ROLE_VIEWER, bump_epoch=False)
        body = client.get("/api/auth/session").json()
        assert body["role"] == ROLE_VIEWER and body["is_admin"] is False
    db.close()


def test_session_rejected_when_epoch_stale(tmp_path, monkeypatch):
    """改密/改角色后 bump epoch，旧 Cookie 立刻 401（用户仍启用，排除了禁用路径）。"""
    monkeypatch.setenv("USERS_FILE", str(tmp_path / "none.txt"))
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    db = Database(settings.db_path)
    apply_schema(db.connect())
    repo = UserRepository(db)
    repo.create("root", create_password_hash("rootpw"), role=ROLE_ADMIN, now=1)
    old_token = create_session_token("root", SECRET, epoch=0)

    app = build_app(settings)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", old_token)
        assert client.get("/api/auth/session").status_code == 200
        repo.update_role("root", ROLE_OPERATOR)      # epoch 0 → 1，root 仍启用
        assert client.get("/api/auth/session").status_code == 401
    db.close()


def test_session_rejected_when_user_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("USERS_FILE", str(tmp_path / "none.txt"))
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    db = Database(settings.db_path)
    apply_schema(db.connect())
    repo = UserRepository(db)
    repo.create("root", create_password_hash("rootpw"), role=ROLE_ADMIN, now=1)
    token = create_session_token("root", SECRET, epoch=1)

    app = build_app(settings)
    repo.set_enabled("root", False)              # epoch → 1
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", token)
        assert client.get("/api/auth/session").status_code == 401
    db.close()


def test_api_key_rejected_when_user_disabled(tmp_path, monkeypatch):
    """禁用用户后其 API Key 立即失效（is_active 而非 has）。"""
    monkeypatch.setenv("USERS_FILE", str(tmp_path / "none.txt"))
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    db = Database(settings.db_path)
    apply_schema(db.connect())
    repo = UserRepository(db)
    repo.create("root", create_password_hash("rootpw"), role=ROLE_ADMIN, now=1)

    app = build_app(settings)
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        assert client.get("/v1/models", headers={"Authorization": f"Bearer {key}"}
                          ).status_code == 200
        repo.set_enabled("root", False)
        assert client.get("/v1/models", headers={"Authorization": f"Bearer {key}"}
                          ).status_code == 401
    db.close()


# ------------------------------------------------------- 首登强制改密

def test_password_change_gate_blocks_everything_but_allowlist(tmp_path, monkeypatch):
    """must_change_password=1：只放行三个端点，上游建凭证也被拦。"""
    monkeypatch.setenv("USERS_FILE", str(tmp_path / "none.txt"))
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    db = Database(settings.db_path)
    apply_schema(db.connect())
    repo = UserRepository(db)
    repo.create("root", create_password_hash("rootpw"), role=ROLE_ADMIN,
                must_change_password=True, now=1)
    token = create_session_token("root", SECRET, epoch=0)

    app = build_app(settings)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", token)
        # 放行：看会话
        assert client.get("/api/auth/session").status_code == 200
        # 拦截：普通读端点
        blocked = client.get("/api/credentials")
        assert blocked.status_code == 403
        assert blocked.json()["error"]["code"] == "password_change_required"
        # 拦截：上游建凭证（前缀通配的写法会错误放行这三个）
        assert client.post("/api/auth/upstream/start",
                           json={"provider": "trae"}).status_code == 403
        # 放行：登出
        assert client.post("/api/auth/logout").status_code == 200
    db.close()


def test_change_password_flow_clears_flag_and_reissues_cookie(tmp_path, monkeypatch):
    """改密成功：清标志、换新 Cookie（否则 bump epoch 会把自己也登出）。"""
    monkeypatch.setenv("USERS_FILE", str(tmp_path / "none.txt"))
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    db = Database(settings.db_path)
    apply_schema(db.connect())
    repo = UserRepository(db)
    repo.create("root", create_password_hash("rootpw"), role=ROLE_ADMIN,
                must_change_password=True, now=1)
    token = create_session_token("root", SECRET, epoch=0)

    app = build_app(settings)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", token)
        changed = client.post("/api/auth/password",
                              json={"current_password": "rootpw",
                                    "new_password": "brand-new-pw"})
        assert changed.status_code == 200
        # 换发的 Cookie 已生效：不再被强制改密拦截
        assert client.get("/api/credentials").status_code == 200
        assert client.get("/api/auth/session").json()["must_change_password"] is False
    db.close()


def test_change_password_rejects_wrong_current(tmp_path, monkeypatch):
    monkeypatch.setenv("USERS_FILE", str(tmp_path / "none.txt"))
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    db = Database(settings.db_path)
    apply_schema(db.connect())
    UserRepository(db).create("root", create_password_hash("rootpw"),
                              role=ROLE_ADMIN, now=1)
    token = create_session_token("root", SECRET, epoch=0)

    app = build_app(settings)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", token)
        response = client.post("/api/auth/password",
                               json={"current_password": "wrong",
                                     "new_password": "brand-new-pw"})
        assert response.status_code == 400
    db.close()


def test_change_password_rejects_short_new_password(tmp_path, monkeypatch):
    monkeypatch.setenv("USERS_FILE", str(tmp_path / "none.txt"))
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    db = Database(settings.db_path)
    apply_schema(db.connect())
    UserRepository(db).create("root", create_password_hash("rootpw"),
                              role=ROLE_ADMIN, now=1)
    token = create_session_token("root", SECRET, epoch=0)

    app = build_app(settings)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", token)
        response = client.post("/api/auth/password",
                               json={"current_password": "rootpw", "new_password": "short"})
        assert response.status_code == 400
    db.close()


def test_login_response_exposes_role_and_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("USERS_FILE", str(tmp_path / "none.txt"))
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    db = Database(settings.db_path)
    apply_schema(db.connect())
    UserRepository(db).create("root", create_password_hash("rootpw"),
                              role=ROLE_ADMIN, now=1)
    UserRepository(db).create("op", create_password_hash("oppw"),
                              role=ROLE_OPERATOR, must_change_password=True, now=1)

    app = build_app(settings)
    with TestClient(app) as client:
        body = client.post("/api/auth/login",
                           json={"username": "op", "password": "oppw"}).json()
        assert body == {"username": "op", "is_admin": False, "role": ROLE_OPERATOR,
                        "must_change_password": True}
    db.close()


def test_login_failure_is_audited_without_password(tmp_path, monkeypatch):
    """失败登录入库（ok=0），且**绝不**记录密码明文。"""
    monkeypatch.setenv("USERS_FILE", str(tmp_path / "none.txt"))
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    db = Database(settings.db_path)
    apply_schema(db.connect())
    UserRepository(db).create("root", create_password_hash("rootpw"),
                              role=ROLE_ADMIN, now=1)

    app = build_app(settings)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "root", "password": "hunter2"})
    rows = app.state.audit.query()
    assert len(rows) == 1
    assert rows[0]["action"] == "login.failure" and rows[0]["ok"] == 0
    assert "hunter2" not in str(rows[0])
    db.close()


def test_login_success_is_audited(tmp_path, monkeypatch):
    monkeypatch.setenv("USERS_FILE", str(tmp_path / "none.txt"))
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    db = Database(settings.db_path)
    apply_schema(db.connect())
    UserRepository(db).create("root", create_password_hash("rootpw"),
                              role=ROLE_ADMIN, now=1)

    app = build_app(settings)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "root", "password": "rootpw"})
    rows = app.state.audit.query()
    assert rows[0]["action"] == "login.success" and rows[0]["ok"] == 1
    db.close()

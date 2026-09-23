"""账号数据层（B5）：users / audit_events 表与两个仓储。

不构造真实 PBKDF2 哈希（单次 600k 迭代约 50ms，仓储层不校验格式），
直接传占位串——密码学正确性由 tests/test_m0_skeleton.py 的 users 单测守着。
"""

from __future__ import annotations

from src.db.conn import Database
from src.db.migrate import SCHEMA_VERSION, apply_schema
from src.db.repo import AuditRepository, UserRepository


def _open(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    return db, UserRepository(db), AuditRepository(db)


# ------------------------------------------------------------------- users

def test_create_and_read_back(tmp_path):
    db, users, _ = _open(tmp_path)
    users.create("alice", "h1", role="operator", created_by="root", now=100)
    row = users.get("alice")
    assert row is not None
    assert (row["username"], row["password_hash"], row["role"]) == ("alice", "h1", "operator")
    assert row["enabled"] == 1
    assert row["must_change_password"] == 0
    assert row["session_epoch"] == 0
    assert (row["created_at"], row["updated_at"], row["created_by"]) == (100, 100, "root")
    assert row["activation_digest"] is None
    assert users.get("ghost") is None
    db.close()


def test_create_with_disabled_and_must_change(tmp_path):
    """两个布尔入参的 True 分支：落库为 1。"""
    db, users, _ = _open(tmp_path)
    users.create("alice", "h1", enabled=False, must_change_password=True, now=1)
    row = users.get("alice")
    assert row["enabled"] == 0 and row["must_change_password"] == 1
    db.close()


def test_list_helpers(tmp_path):
    db, users, _ = _open(tmp_path)
    users.create("carol", "h", now=1)
    users.create("alice", "h", now=1)
    users.create("bob", "h", now=1)
    assert users.list_usernames() == ("alice", "bob", "carol")
    assert [row["username"] for row in users.list_all()] == ["alice", "bob", "carol"]
    db.close()


def test_upsert_imported_is_idempotent_and_never_overwrites(tmp_path):
    db, users, _ = _open(tmp_path)
    assert users.upsert_imported("alice", "from_file", now=1) is True
    # 已存在：不覆盖（用户可能已改过密码/角色），返回 False
    users.update_role("alice", "admin")
    assert users.upsert_imported("alice", "from_file", now=2) is False
    assert users.get("alice")["password_hash"] == "from_file"
    assert users.get("alice")["role"] == "admin"
    # 导入的行一律 viewer 且无建号者
    assert users.get("alice")["created_by"] is None
    db.close()


def test_update_role_bumps_epoch_by_default(tmp_path):
    db, users, _ = _open(tmp_path)
    users.create("alice", "h", now=1)
    assert users.update_role("alice", "admin", now=5) is True
    row = users.get("alice")
    assert row["role"] == "admin"
    assert row["session_epoch"] == 1            # 降级/升级都要踢旧会话
    assert row["updated_at"] == 5
    db.close()


def test_update_role_can_skip_epoch_bump(tmp_path):
    db, users, _ = _open(tmp_path)
    users.create("alice", "h", now=1)
    users.update_role("alice", "viewer", bump_epoch=False, now=2)
    assert users.get("alice")["session_epoch"] == 0
    db.close()


def test_update_role_missing_user_returns_false(tmp_path):
    db, users, _ = _open(tmp_path)
    assert users.update_role("ghost", "admin") is False
    assert users.set_enabled("ghost", False) is False
    assert users.set_password("ghost", "h") is False
    assert users.set_activation_token("ghost", "d", 999) is False
    assert users.delete("ghost") is False
    db.close()


def test_set_enabled_toggles_and_bumps_epoch(tmp_path):
    db, users, _ = _open(tmp_path)
    users.create("alice", "h", now=1)
    assert users.set_enabled("alice", False, now=2) is True
    row = users.get("alice")
    assert row["enabled"] == 0 and row["session_epoch"] == 1
    users.set_enabled("alice", True, now=3)
    row = users.get("alice")
    assert row["enabled"] == 1 and row["session_epoch"] == 2
    db.close()


def test_set_password_clears_activation_and_bumps_epoch(tmp_path):
    db, users, _ = _open(tmp_path)
    users.create("alice", "old", must_change_password=True, now=1)
    users.set_activation_token("alice", "digest", 9999, now=2)
    assert users.get("alice")["activation_digest"] == "digest"

    assert users.set_password("alice", "new", now=3) is True
    row = users.get("alice")
    assert (row["password_hash"], row["must_change_password"]) == ("new", 0)
    assert row["session_epoch"] == 1
    # 一次性令牌用掉即清，且过期时间一起清
    assert row["activation_digest"] is None and row["activation_expires_at"] is None
    db.close()


def test_set_password_keeps_must_change_flag(tmp_path):
    """admin 重置为临时密码：标志位为 True。"""
    db, users, _ = _open(tmp_path)
    users.create("alice", "old", now=1)
    users.set_password("alice", "temp", must_change_password=True, now=2)
    assert users.get("alice")["must_change_password"] == 1
    db.close()


def test_activation_token_roundtrip(tmp_path):
    db, users, _ = _open(tmp_path)
    users.create("alice", "h", now=1)
    assert users.set_activation_token("alice", "digest", 9999, now=2) is True
    row = users.find_by_activation("digest")
    assert row is not None and row["username"] == "alice"
    assert users.find_by_activation("nope") is None
    db.close()


def test_delete_removes_row(tmp_path):
    db, users, _ = _open(tmp_path)
    users.create("alice", "h", now=1)
    assert users.delete("alice") is True
    assert users.get("alice") is None
    db.close()


def test_count_active_admins(tmp_path):
    db, users, _ = _open(tmp_path)
    assert users.count_active_admins() == 0
    users.create("root", "h", role="admin", now=1)
    users.create("former", "h", role="admin", now=1)
    users.create("op", "h", role="operator", now=1)
    assert users.count_active_admins() == 2
    # 禁用后不再计入；operator 从来不算
    users.set_enabled("former", False)
    assert users.count_active_admins() == 1
    db.close()


# ------------------------------------------------------------------- audit

def test_audit_record_and_query_desc(tmp_path):
    db, _, audit = _open(tmp_path)
    audit.record(actor="root", action="login.success", ip="1.2.3.4", now=100)
    audit.record(actor="root", action="user.create", target="alice", ok=True, now=200)
    audit.record(actor="alice", action="login.failure", ok=False, now=300)

    rows = audit.query()
    assert [row["action"] for row in rows] == [
        "login.failure", "user.create", "login.success"]     # ts 倒序
    assert rows[2]["ip"] == "1.2.3.4"
    assert rows[0]["ok"] == 0
    assert rows[1]["target"] == "alice"
    assert rows[1]["detail"] == ""
    db.close()


def test_audit_query_filters(tmp_path):
    db, _, audit = _open(tmp_path)
    audit.record(actor="root", action="user.create", now=100)
    audit.record(actor="root", action="login.success", now=200)
    audit.record(actor="alice", action="login.success", now=300)

    assert len(audit.query(actor="root")) == 2
    assert len(audit.query(action="login.success")) == 2
    assert len(audit.query(actor="root", action="login.success")) == 1
    assert [r["ts"] for r in audit.query(since=200)] == [300, 200]
    assert [r["ts"] for r in audit.query(before=300)] == [200, 100]
    # 组合过滤走同一条 WHERE 分支
    assert [r["ts"] for r in audit.query(actor="root", since=150)] == [200]
    db.close()


def test_audit_query_clamps_paging(tmp_path):
    """limit 收敛到 [1,500]，offset 非负——防止一次拉爆前端。"""
    db, _, audit = _open(tmp_path)
    audit.record(actor="root", action="login.success", now=1)
    assert len(audit.query(limit=0)) == 1              # 下界抬到 1
    assert len(audit.query(limit=99999)) == 1          # 上界压到 500，行数不受影响
    assert audit.query(offset=-5) == audit.query(offset=0)
    db.close()


def test_audit_prune(tmp_path):
    db, _, audit = _open(tmp_path)
    audit.record(actor="root", action="login.success", now=0)
    audit.record(actor="root", action="login.success", now=100 * 86400)
    # 保留 90 天，cutoff = 100*86400 - 90*86400 > 0 → 第一条被清
    assert audit.prune(keep_days=90, now=100 * 86400) == 1
    assert [r["ts"] for r in audit.query()] == [100 * 86400]
    db.close()


# ------------------------------------------------------------- schema v14

def test_schema_v14_adds_users_and_audit_tables(tmp_path):
    db, _, _ = _open(tmp_path)
    tables = {row[0] for row in db.connect().execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"users", "audit_events"} <= tables
    assert db.connect().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert SCHEMA_VERSION == 14
    db.close()

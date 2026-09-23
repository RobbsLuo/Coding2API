"""scripts/create_user.py 的建/删/列表测试（B5 CLI 恢复路径）。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from src.auth.users import verify_password
from src.db.conn import Database
from src.db.repo import AuditRepository, UserRepository

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "create_user.py"


def _load():
    spec = importlib.util.spec_from_file_location("create_user", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


SCRIPT_MODULE = _load()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "coding2api.sqlite3"


def repos(db_path):
    database = Database(db_path)
    SCRIPT_MODULE.apply_schema(database.connect())
    return UserRepository(database), AuditRepository(database)


def test_create_user_with_explicit_password(db_path, capsys):
    assert SCRIPT_MODULE.main(["alice", "--role", "admin", "--password", "pw",
                               "--db", str(db_path)]) == 0
    users, audit = repos(db_path)
    row = users.get("alice")
    assert row is not None and row["role"] == "admin" and row["enabled"] == 1
    assert verify_password("pw", row["password_hash"])
    # 建号写审计，actor 标为 cli
    events = audit.query(action="user.create")
    assert events and events[0]["actor"] == "cli" and events[0]["target"] == "alice"


def test_create_user_defaults_to_viewer(db_path):
    assert SCRIPT_MODULE.main(["bob", "--password", "pw", "--db", str(db_path)]) == 0
    users, _audit = repos(db_path)
    assert users.get("bob")["role"] == "viewer"


def test_create_duplicate_fails(db_path, capsys):
    SCRIPT_MODULE.main(["alice", "--password", "pw", "--db", str(db_path)])
    assert SCRIPT_MODULE.main(["alice", "--password", "pw2", "--db", str(db_path)]) == 1
    assert "已存在" in capsys.readouterr().err


def test_list_users_includes_imported_marker(db_path, capsys):
    users, _audit = repos(db_path)
    users.create("ghost", "hash", role="viewer", created_by=None)
    assert SCRIPT_MODULE.main(["--list", "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert "ghost" in out and "引导导入" in out and "viewer" in out


def test_list_empty(db_path, capsys):
    assert SCRIPT_MODULE.main(["--list", "--db", str(db_path)]) == 0
    assert "（无用户）" in capsys.readouterr().out


def test_delete_requires_force(db_path):
    SCRIPT_MODULE.main(["alice", "--password", "pw", "--db", str(db_path)])
    with pytest.raises(SystemExit):
        SCRIPT_MODULE.main(["alice", "--delete", "--db", str(db_path)])
    users, _audit = repos(db_path)
    assert users.get("alice") is not None


def test_delete_removes_and_audits(db_path, capsys):
    SCRIPT_MODULE.main(["alice", "--role", "admin", "--password", "pw",
                        "--db", str(db_path)])
    SCRIPT_MODULE.main(["bob", "--role", "admin", "--password", "pw",
                        "--db", str(db_path)])
    assert SCRIPT_MODULE.main(["alice", "--delete", "--force",
                               "--db", str(db_path)]) == 0
    users, audit = repos(db_path)
    assert users.get("alice") is None
    assert audit.query(action="user.delete")[0]["target"] == "alice"
    assert "无主数据" in capsys.readouterr().out


def test_delete_missing_user_fails(db_path, capsys):
    assert SCRIPT_MODULE.main(["ghost", "--delete", "--force",
                               "--db", str(db_path)]) == 1
    assert "不存在" in capsys.readouterr().err


def test_delete_last_active_admin_refused(db_path, capsys):
    SCRIPT_MODULE.main(["solo", "--role", "admin", "--password", "pw",
                        "--db", str(db_path)])
    assert SCRIPT_MODULE.main(["solo", "--delete", "--force",
                               "--db", str(db_path)]) == 1
    assert "最后一个活跃管理员" in capsys.readouterr().err
    users, _audit = repos(db_path)
    assert users.get("solo") is not None


def test_delete_disabled_admin_allowed(db_path):
    """被禁用的 admin 不参与 count_active_admins，可以硬删。"""
    SCRIPT_MODULE.main(["live", "--role", "admin", "--password", "pw",
                        "--db", str(db_path)])
    SCRIPT_MODULE.main(["dead", "--role", "admin", "--password", "pw",
                        "--db", str(db_path)])
    users, _audit = repos(db_path)
    users.set_enabled("dead", False)
    assert SCRIPT_MODULE.main(["dead", "--delete", "--force",
                               "--db", str(db_path)]) == 0
    assert users.get("dead") is None


def test_interactive_password_mismatch_exits(db_path, monkeypatch):
    answers = iter(["pw1", "pw2"])
    monkeypatch.setattr(SCRIPT_MODULE.getpass, "getpass", lambda *_: next(answers))
    with pytest.raises(SystemExit):
        SCRIPT_MODULE.main(["alice", "--db", str(db_path)])


def test_interactive_password_accepted(db_path, monkeypatch):
    answers = iter(["pw", "pw"])
    monkeypatch.setattr(SCRIPT_MODULE.getpass, "getpass", lambda *_: next(answers))
    assert SCRIPT_MODULE.main(["alice", "--db", str(db_path)]) == 0
    users, _audit = repos(db_path)
    assert verify_password("pw", users.get("alice")["password_hash"])


def test_empty_password_rejected(db_path):
    with pytest.raises(SystemExit):
        SCRIPT_MODULE.main(["alice", "--password", "", "--db", str(db_path)])


@pytest.mark.parametrize("argv", [[], ["  "]])
def test_missing_or_blank_username_rejected(argv, db_path):
    with pytest.raises(SystemExit):
        SCRIPT_MODULE.main([*argv, "--password", "pw", "--db", str(db_path)])


def test_resolve_db_path_defaults_to_settings(tmp_path, monkeypatch):
    # APP_SECRET 必须显式给：CI 无 .env，只设 DATA_DIR 会让 Settings 构造失败
    monkeypatch.setenv("APP_SECRET", "test-secret-0123456789")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "custom"))
    path = SCRIPT_MODULE.resolve_db_path(None)
    assert path == tmp_path / "custom" / "coding2api.sqlite3"


def test_resolve_db_path_explicit_wins(tmp_path, monkeypatch):
    """--db 优先，完全不碰 Settings（无 APP_SECRET 也能跑）。"""
    monkeypatch.delenv("APP_SECRET", raising=False)
    explicit = tmp_path / "elsewhere.sqlite3"
    assert SCRIPT_MODULE.resolve_db_path(str(explicit)) == explicit

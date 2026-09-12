"""M0 补充测试：把 session/users/config/db 拉到 100%。"""

from __future__ import annotations

import base64
import json
import sqlite3
import threading

import pytest

from src.auth import session as session_mod
from src.auth.users import UsersFileError, UsersFileStore, create_password_hash
from src.config import load_settings
from src.db.conn import Database
from src.db.migrate import apply_schema

# ------------------------------------------------------------------ session

def _forge(payload: object, secret: str) -> str:
    """用合法签名包裹任意 payload，用于走完 payload 校验分支。"""
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{payload_b64}.{session_mod._sign(payload_b64, secret)}"


def test_session_rejects_non_json_payload():
    token = _forge.__wrapped__ if False else None  # noqa: F841
    raw = b"\xff\xfe not json"
    payload_b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    token = f"{payload_b64}.{session_mod._sign(payload_b64, 'sec')}"
    assert session_mod.verify_session_token(token, "sec", now=1) is None


def test_session_rejects_non_dict_payload():
    assert session_mod.verify_session_token(_forge([1, 2], "sec"), "sec", now=1) is None


@pytest.mark.parametrize("payload", [
    {"exp": 10},                      # 缺 username
    {"u": "", "exp": 10},             # 空 username
    {"u": 5, "exp": 10},              # username 非字符串
    {"u": "a"},                       # 缺 exp
    {"u": "a", "exp": "10"},          # exp 非 int
    {"u": "a", "exp": True},          # exp 是 bool
])
def test_session_rejects_bad_claims(payload):
    assert session_mod.verify_session_token(_forge(payload, "sec"), "sec", now=1) is None


def test_session_uses_wall_clock_when_now_omitted():
    token = session_mod.create_session_token("a", "sec")
    assert session_mod.verify_session_token(token, "sec") == "a"


# -------------------------------------------------------------------- users

def test_password_hash_rejects_bool_iterations():
    with pytest.raises(ValueError):
        create_password_hash("p", iterations=True)


def test_users_file_rejects_blank_username(tmp_path):
    path = tmp_path / "users.txt"
    path.write_text(":hash\n", encoding="utf-8")
    with pytest.raises(UsersFileError):
        UsersFileStore(path).validate()


def test_users_file_skips_blank_lines_only(tmp_path):
    path = tmp_path / "users.txt"
    path.write_text("\n\n# c\n", encoding="utf-8")
    with pytest.raises(UsersFileError):
        UsersFileStore(path).validate()


def test_users_file_reload_on_mtime_change(tmp_path):
    path = tmp_path / "users.txt"
    path.write_text(f"a:{create_password_hash('x', iterations=600_000)}\n", encoding="utf-8")
    store = UsersFileStore(path)
    assert store.list_usernames() == ("a",)
    import os
    import time

    time.sleep(0.01)
    path.write_text(f"b:{create_password_hash('y', iterations=600_000)}\n", encoding="utf-8")
    os.utime(path, (time.time() + 1, time.time() + 1))
    assert store.list_usernames() == ("b",)


# ------------------------------------------------------------------- config

def test_load_settings_defaults_reads_env(monkeypatch):
    monkeypatch.setenv("APP_SECRET", "from-env")
    s = load_settings()
    assert s.app_secret == "from-env"


def test_load_settings_explicit_env_dict():
    s = load_settings({"APP_SECRET": "s", "PORT": "9999", "DEFAULT_MODEL": "glm-5.3"})
    assert s.port == 9999 and s.default_model == "glm-5.3"


# ----------------------------------------------------------------------- db

@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "t.sqlite3")
    apply_schema(database.connect())
    yield database
    database.close()


def test_database_applies_pragmas(db):
    conn = db.connect()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_database_creates_parent_dir(tmp_path):
    target = tmp_path / "nested" / "deep" / "x.sqlite3"
    database = Database(target)
    apply_schema(database.connect())
    assert target.exists()
    database.close()


def test_database_reuses_thread_local_connection(db):
    assert db.connect() is db.connect()


def test_database_schema_is_idempotent(db):
    apply_schema(db.connect())
    apply_schema(db.connect())
    tables = {r["name"] for r in db.connect().execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"api_keys", "credentials", "usage_events", "usage_hourly"} <= tables
    # checkins / model_cache 已删除（零引用：签到去重走内存态，模型列表是 TTL 缓存）
    assert "checkins" not in tables and "model_cache" not in tables


def test_database_connection_is_per_thread(db):
    seen: list[int] = []

    def worker():
        conn = db.connect()
        seen.append(id(conn))
        conn.close()

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert seen and seen[0] != id(db.connect())


def test_credentials_table_accepts_encrypted_blob(db):
    conn = db.connect()
    conn.execute(
        "INSERT INTO credentials (id, provider, data_enc, created_at) VALUES (?,?,?,?)",
        ("c1", "trae", b"\x00\x01", 1),
    )
    conn.commit()
    row = conn.execute("SELECT provider, display_health FROM ("
                       "SELECT provider, health AS display_health FROM credentials)").fetchone()
    assert row["provider"] == "trae"
    assert row["display_health"] is None          # NULL = unknown


def test_usage_events_credit_nullable(db):
    conn = db.connect()
    conn.execute(
        "INSERT INTO usage_events (id, ts, username, provider, model, ok, credit) "
        "VALUES (?,?,?,?,?,?,?)",
        ("e1", 1, "alice", "trae", "glm-5.2", 1, None),
    )
    conn.commit()
    assert conn.execute("SELECT credit FROM usage_events").fetchone()["credit"] is None


def test_close_is_safe_when_never_connected(tmp_path):
    Database(tmp_path / "never.sqlite3").close()      # 不抛异常


def test_sqlite_row_factory_returns_rows(db):
    conn: sqlite3.Connection = db.connect()
    assert isinstance(conn.row_factory, type) or conn.row_factory is sqlite3.Row


def test_database_skips_mkdir_for_bare_filename(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    database = Database("bare.sqlite3")      # parent 为 "" → 不建目录
    apply_schema(database.connect())
    assert (tmp_path / "bare.sqlite3").exists()
    database.close()

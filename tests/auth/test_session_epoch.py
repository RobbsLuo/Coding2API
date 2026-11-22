"""会话 epoch 作废入口：`UserRepository.bump_session_epoch` 与 `DbUserStore` 薄封装。

登出必须走 bump（Cookie 无状态，删浏览器那份挡不住被复制的 Cookie），
这里直接打仓储层覆盖「用户存在 / 不存在」两条返回分支。
"""

from __future__ import annotations

from src.auth.users import DbUserStore
from src.db.conn import Database
from src.db.migrate import apply_schema
from src.db.repo import UserRepository


def _repo(tmp_path) -> tuple[UserRepository, Database]:
    db = Database(tmp_path / "epoch.sqlite3")
    apply_schema(db.connect())
    return UserRepository(db), db


def test_bump_session_epoch_increments_existing_user(tmp_path):
    repo, db = _repo(tmp_path)
    repo.create("root", "pbkdf2_sha256$600000$c2FsdA==$ZGlnZXN0", role="admin", now=1)
    try:
        assert repo.bump_session_epoch("root", now=2) is True
        assert repo.get("root")["session_epoch"] == 1
        assert repo.get("root")["updated_at"] == 2
    finally:
        db.close()


def test_bump_session_epoch_unknown_user_returns_false(tmp_path):
    repo, db = _repo(tmp_path)
    try:
        assert repo.bump_session_epoch("ghost") is False
    finally:
        db.close()


def test_db_user_store_bump_is_thin_wrapper(tmp_path):
    """DbUserStore 只转发；用户不存在时不抛（登出路径必须永远成功）。"""
    repo, db = _repo(tmp_path)
    repo.create("root", "pbkdf2_sha256$600000$c2FsdA==$ZGlnZXN0", role="admin", now=1)
    store = DbUserStore(repo)
    try:
        store.bump_session_epoch("root")
        assert store.session_epoch("root") == 1
        store.bump_session_epoch("ghost")
    finally:
        db.close()

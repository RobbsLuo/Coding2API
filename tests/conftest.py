"""全局测试夹具。

用户文件是启动必需项（PROPOSAL §5），所有 build_app 调用都需要它存在。
PBKDF2 迭代次数是安全下限（600k），单次哈希约 50ms，因此用户文件在
整个测试会话内只生成一次——否则每个测试都重建文件会把测试拖慢 30 倍。
"""

from __future__ import annotations

import pytest

from src.auth.users import create_password_hash

USERS = (("root", "rootpw"), ("guest", "guestpw"), ("alice", "alicepw"))

# 测试用 APP_SECRET：必须满足 CredentialCipher 的最短长度校验（>=16）
SECRET = "test-secret-0123456789"


@pytest.fixture(scope="session")
def users_file_path(tmp_path_factory):
    directory = tmp_path_factory.mktemp("users")
    path = directory / "users.txt"
    rows = [f"{name}:{create_password_hash(password, iterations=600_000)}"
            for name, password in USERS]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def users_file(users_file_path, monkeypatch):
    monkeypatch.setenv("USERS_FILE", str(users_file_path))
    return users_file_path

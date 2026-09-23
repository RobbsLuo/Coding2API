"""全局测试夹具。

用户来源（B5 起）：启动时从 users.txt 一次性导入 SQLite。为保持「整个测试
会话只哈希一次」的既有优化，三用户的 users.txt 仍按 session 生成一次，每个
测试只是换一个 DATA_DIR，于是每个测试都要重新导入一次——所以这里同时给出
admin 的引导配置。

ADMIN_USERNAMES 用 monkeypatch 全局设成 root：B5 里它只在引导期生效（把已存在
的用户提权为 admin），对未显式传 ADMIN_USERNAMES 的 Settings 也一律可见。
实测这样全量跑仍是 1335 passed / 100% 覆盖：既有断言里只有 root 被判为 admin，
而没有任何测试断言 root 不是 admin（guest/alice 的 is_admin 断言不受影响）。
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
    # root 是引导期 admin：B5 的鉴权从 DB 现读角色，不再看 env；
    # 这里提供 env 是为了让每个测试的库在 bootstrap 时把 root 提权。
    monkeypatch.setenv("ADMIN_USERNAMES", "root")
    return users_file_path

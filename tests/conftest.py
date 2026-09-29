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


@pytest.fixture(autouse=True)
def zen_models_offline(monkeypatch, request):
    """除 test_zen 外，一律不真连 Zen 上游。

    任何 `build_app()` 的启动预热都会调 `ZenClient.fetch_models`，而它现在会
    对免费候选逐个发探活请求（真跑一次模型推理）；不拦住的话每个用到真实装配
    的用例都会发十几次外网请求，又慢又依赖外网。Zen 的真实逻辑（含探活各分支）
    由 `tests/test_zen.py` 用 MockTransport 全覆盖，这里只给它一个固定的小名单。
    """
    if request.node.path.name == "test_zen.py":
        return
    from src.provider.zen.client import Model, ZenClient

    async def fake_fetch_models(self) -> list[Model]:
        return [Model(id="offline-free", name="opencode")]

    monkeypatch.setattr(ZenClient, "fetch_models", fake_fetch_models)

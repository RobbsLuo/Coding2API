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
def data_dir(tmp_path, monkeypatch):
    """把「没显式传 DATA_DIR」的 Settings 隔离到 tmp_path。

    模型目录快照（Q53：`DATA_DIR/model_catalog.json`）是**默认值 `./data` 也会
    真写盘**的一处产物。不隔离的话，任何用默认 DATA_DIR 建 app 的用例会往仓库
    `data/` 写一份测试版模型表，覆盖生产快照——表现为本地服务重启后只恢复了测试
    里那两三个渠道（实测踩过：快照里只剩 codebuddy + trae）。显式传 DATA_DIR 的
    用例（含全部 build_app 用例）不受影响：pydantic 显式参数优先于 env。
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def zen_models_offline(monkeypatch, request):
    """除 test_zen / test_kilo 外，一律不真连 Zen / Kilo 上游。

    任何 `build_app()` 的启动预热都会调 `ZenClient.fetch_models`（它会逐个
    探活免费模型，真跑一次推理）与 `KiloClient.fetch_models`（真打外网清单）；
    不拦住的话每个用到真实装配的用例都会发外网请求，又慢又依赖外网。两条渠道
    的真实逻辑由 `tests/test_zen.py` / `tests/test_kilo.py` 用 MockTransport
    全覆盖，这里只给它们各一个固定的小名单。
    """
    name = request.node.path.name
    from src.provider.base import Model as _Model

    if name != "test_zen.py":
        from src.provider.zen.client import ZenClient

        async def fake_zen_models(self) -> list[_Model]:
            return [_Model(id="offline-free", name="opencode")]

        monkeypatch.setattr(ZenClient, "fetch_models", fake_zen_models)
    if name != "test_kilo.py":
        from src.provider.kilo.client import KiloClient

        async def fake_kilo_models(self) -> list[_Model]:
            return [_Model(id="kilo-offline/free", name="Kilo (offline)")]

        monkeypatch.setattr(KiloClient, "fetch_models", fake_kilo_models)


@pytest.fixture(autouse=True)
def price_catalog_offline(monkeypatch):
    """除 test_pricing 外，一律不真连 models.dev。

    `build_app()` 在**没有落盘价表快照**时会后台补拉一次
    （`_warm_price_table`），每个用默认空 DATA_DIR 建 app 的用例都会发外网请求。
    真实抓取逻辑由 `tests/test_pricing.py` 用 MockTransport 覆盖；这里给一个空表
    （等价「上游不可用」，费用显示 —），既不打外网也不污染既有断言。
    """
    from src import main

    async def fake_fetch(_url):
        return {}

    monkeypatch.setattr(main, "fetch_prices", fake_fetch)

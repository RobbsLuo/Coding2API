"""全局测试夹具。

用户来源：启动时从 users.txt 一次性导入 SQLite。为保持「整个测试
会话只哈希一次」的既有优化，三用户的 users.txt 仍按 session 生成一次，每个
测试只是换一个 DATA_DIR，于是每个测试都要重新导入一次——所以这里同时给出
admin 的引导配置。

ADMIN_USERNAMES 用 monkeypatch 全局设成 root： 里它只在引导期生效（把已存在
的用户提权为 admin），对未显式传 ADMIN_USERNAMES 的 Settings 也一律可见。
实测这样全量跑仍是 1335 passed / 100% 覆盖：既有断言里只有 root 被判为 admin，
而没有任何测试断言 root 不是 admin（guest/alice 的 is_admin 断言不受影响）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.auth.users import create_password_hash
from src.config import Settings
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository, GrowthRepository
from src.main import build_app
from tests.helpers.constants import SECRET, USERS
from tests.helpers.startup import wait_for_startup

# 常量迁到 tests/helpers/constants.py；此处 re-export 以兼容既有
# `from tests.conftest import SECRET` 调用点（新代码请直接 import helpers）。

__all__ = ["SECRET", "USERS"]


@pytest.fixture()
def repo(tmp_path):
    """`(CredentialRepository, Database)` 临时库夹具。

    13 个文件曾各写一份逐字节等价的实现；收敛到这里后，测试模块直接以
    形参 `repo` 注入即可。调用方负责在用例结束时关闭（夹具负责）。
    """
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    yield CredentialRepository(db, CredentialCipher(SECRET)), db
    db.close()


@pytest.fixture()
def db(tmp_path):
    """建表后的裸 `Database` 临时库夹具（5 处逐字节等价的副本收敛到此）。"""
    database = Database(tmp_path / "t.sqlite3")
    apply_schema(database.connect())
    yield database
    database.close()


@pytest.fixture()
def dual_repo(tmp_path):
    """`(CredentialRepository, Database)`，文件名固定 `dual.sqlite3`。

    4 个文件曾各写一份逐字节等价的实现（含双上游用例），收敛到这里。
    """
    db = Database(tmp_path / "dual.sqlite3")
    apply_schema(db.connect())
    yield CredentialRepository(db, CredentialCipher(SECRET)), db
    db.close()


@pytest.fixture()
def growth_repo(tmp_path):
    """`(CredentialRepository, GrowthRepository)` 临时库夹具。

    3 个文件（api / provider / tasks）曾各写一份逐字节等价的实现，收敛到这里。
    """
    db = Database(tmp_path / "growth.sqlite3")
    apply_schema(db.connect())
    yield CredentialRepository(db, CredentialCipher(SECRET)), GrowthRepository(db)
    db.close()


@pytest.fixture()
def admin_client(tmp_path):
    """`(app, client)`：带 admin 会话 Cookie 的 TestClient。

    5 个文件曾各写一份逐字节等价的实现，收敛到这里；需要替身 provider 或
    自定义 Settings 的文件仍保留自己的局部 `admin_client`（会覆盖本夹具）。

    **等启动任务跑完再交出**：`start()` 的首轮 retention 会删过期行，与测试体
    并发时会让「1970 年时间戳」的测试数据随机消失（本地绿、CI 红）。见
    `tests/helpers/startup.py`。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    client = TestClient(app)
    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    with client:
        wait_for_startup(app, client)
        yield app, client


@pytest.fixture()
def client(tmp_path):
    """带 admin 会话 Cookie 的 TestClient（只需 client、不需要 app 的用例用）。

    `tests/api/test_health.py` / `tests/auth/test_api_key_policy.py` 曾各写一份
    逐字节等价的实现；需要 `(app, client)` 的用例用 `admin_client`。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as test_client:
        test_client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        wait_for_startup(app, test_client)
        yield test_client


@pytest.fixture(autouse=True)
def reset_model_cache():
    """按测试清空 CodeBuddy 的模块级模型缓存。

    15 个测试模块曾各写一份逐字节等价的 autouse 夹具；收敛成一份全局夹具。
    `tests/provider/codebuddy/test_client_quota.py` 另需重置 `_DAYS_BASE`
    基准时刻，保留其局部同名夹具（它会覆盖本夹具，语义等价）。
    """
    from src.provider.codebuddy.client import _MODEL_CACHE

    _MODEL_CACHE.clear()
    yield
    _MODEL_CACHE.clear()


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
    # root 是引导期 admin：鉴权从 DB 现读角色，不再看 env；
    # 这里提供 env 是为了让每个测试的库在 bootstrap 时把 root 提权。
    monkeypatch.setenv("ADMIN_USERNAMES", "root")
    # TestClient 默认 Host=testserver：该主机名在生产默认不放行
    # （防 DNS rebinding），测试显式开启。
    monkeypatch.setenv("ALLOW_TEST_HOST", "true")
    return users_file_path


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """把「没显式传 DATA_DIR」的 Settings 隔离到 tmp_path。

    模型目录快照（`DATA_DIR/model_catalog.json`）是**默认值 `./data` 也会
    真写盘**的一处产物。不隔离的话，任何用默认 DATA_DIR 建 app 的用例会往仓库
    `data/` 写一份测试版模型表，覆盖生产快照——表现为本地服务重启后只恢复了测试
    里那两三个渠道（实测踩过：快照里只剩 codebuddy + trae）。显式传 DATA_DIR 的
    用例（含全部 build_app 用例）不受影响：pydantic 显式参数优先于 env。
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def zen_models_offline(monkeypatch, request):
    """除标了 `real_upstream("zen"/"kilo")` 的文件外，一律不真连 Zen / Kilo 上游。

    任何 `build_app()` 的启动预热都会调 `ZenClient.fetch_models`（它会逐个
    探活免费模型，真跑一次推理）与 `KiloClient.fetch_models`（真打外网清单）；
    不拦住的话每个用到真实装配的用例都会发外网请求，又慢又依赖外网。两条渠道
    的真实逻辑由 `tests/provider/zen/` / `tests/provider/kilo/` 用 MockTransport
    全覆盖，这里只给它们各一个固定的小名单。

    豁免按 **marker** 而非文件名判定：文件重命名 / 下沉后不会失效（曾按
    `request.node.path.name` 判 `test_zen.py`，文件一动就静默真连外网）。
    """
    from src.provider.base import Model as _Model

    real = _real_upstream(request)
    if "zen" not in real:
        from src.provider.zen.client import ZenClient

        # 签名必须与 `ZenClient.fetch_models(api_key="")` 一致：少一个参数时
        # `ZenProvider.list_models` 调用会 TypeError，被服务层记成「拉取失败」
        # 而静默跳过 zen——测试里曾经一直是这样（实测 2026-11-25）。
        async def fake_zen_models(self, _api_key: str = "") -> list[_Model]:
            return [_Model(id="offline-free", name="opencode")]

        monkeypatch.setattr(ZenClient, "fetch_models", fake_zen_models)
    if "kilo" not in real:
        from src.provider.kilo.client import KiloClient

        async def fake_kilo_models(self) -> list[_Model]:
            return [_Model(id="kilo-offline/free", name="Kilo (offline)")]

        monkeypatch.setattr(KiloClient, "fetch_models", fake_kilo_models)


@pytest.fixture(autouse=True)
def price_catalog_offline(monkeypatch, request):
    """除标了 `real_upstream("openrouter")` 的文件外，一律不真连 OpenRouter。

    `build_app()` 在**没有落盘模型目录快照**时会后台补拉一次
    （`_warm_model_catalog`），每个用默认空 DATA_DIR 建 app 的用例都会发外网请求。
    真实抓取逻辑由 `tests/provider/test_benchmarks.py` 用 MockTransport 覆盖；这里给一个空
    原始表（等价「上游不可用」，成本与分数显示 —），既不打外网也不污染既有断言。
    """
    if "openrouter" in _real_upstream(request):
        return

    from src import main

    async def fake_fetch(_url):
        return {}

    monkeypatch.setattr(main, "fetch_openrouter_models", fake_fetch)


@pytest.fixture(autouse=True)
def upstream_status_offline(monkeypatch):
    """一律不真连状态源：把任务用的 `fetch_all` 换成「什么都抓不到」。

    `build_app()` 启动时会后台预热一次上游状态页（`_warm_upstream_status`），
    每个用真实装配的用例都会打 8 个外部端点。这里掐的是**任务侧**的抓取入口
    （`src.tasks.upstream_status.fetch_all`），返回空快照——不打外网、也不凭空
    编造状态。真实抓取 / 解析由 `tests/test_upstream_status.py`（直接调
    `src.upstream_status.fetch_all`，走 MockTransport）与
    `tests/tasks/test_upstream_status.py`（自己的 autouse 夹具换回真实现）覆盖。
    """
    from src.tasks import upstream_status as task_module

    async def fake_fetch_all(specs, *, timeout, client=None):
        return ()

    monkeypatch.setattr(task_module, "fetch_all", fake_fetch_all)


def _real_upstream(request) -> set[str]:
    """本测试所属文件声明的 `real_upstream` 豁免项集合（如 {"zen"}）。"""
    marker = request.node.get_closest_marker("real_upstream")
    return set(marker.args) if marker else set()

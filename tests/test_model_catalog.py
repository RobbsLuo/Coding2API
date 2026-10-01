"""模型目录的落盘快照 + 逐渠道增量 publish（`api/model_catalog.py`）。

两条行为，各对应一个真故障：

1. **落盘/回灌**：启动到预热跑完之间别名表是空的，扁平名请求无法收窄候选，
   会真实打一轮不认该模型的上游（实测 CodeBuddy 11102 / TRAE 4001）；
   某渠道拉取失败时进程内缓存是唯一兜底，重启后连兜底都没了。
2. **增量 publish**：别名表原先等「全部渠道拉完」才更新，被最慢的 zen 探活
   （逐个免费模型真发推理，12–15s）拖住；改成拉完一条就 publish。
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from src.api.model_catalog import (
    CATALOG_FILENAME,
    MAX_AGE_SECONDS,
    catalog_path,
    load_catalog,
    save_catalog,
)
from src.api.models import (
    list_models,
    merged_entries,
    publish_aliases,
    restore_model_catalog,
)
from src.config import Settings
from src.main import _restore_model_list, build_app
from src.provider.base import Model
from tests.conftest import SECRET


@pytest.fixture()
def settings(tmp_path):
    return Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                    ADMIN_USERNAMES="root")


def _catalog(stub) -> Model:
    return stub


class _StubProvider:
    """最小 provider：只提供 list_models（模型列表链路只需要它）。"""

    def __init__(self, provider_id: str, models: list[Model], *, delay: float = 0.0):
        self.id = provider_id
        self._models = models
        self._delay = delay
        self.calls = 0

    async def list_models(self, credential_data):
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        return list(self._models)

    def import_credential(self, raw):  # pragma: no cover - 由凭证导入调用
        return raw


# --------------------------------------------------------------- 落盘 / 读回


def test_save_and_load_roundtrip(tmp_path):
    """写盘再读回：模型表与全部元数据字段原样恢复。"""
    tables = {"kilo": {"stealth/space-bunny-alpha": Model(
        id="stealth/space-bunny-alpha", name="Space Bunny Alpha", credit_rate=0.0,
        max_input_tokens=131072, supports_tool_call=True)}}
    save_catalog(str(tmp_path), tables)

    loaded = load_catalog(str(tmp_path))
    assert list(loaded) == ["kilo"]
    model = loaded["kilo"]["stealth/space-bunny-alpha"]
    assert model == Model(id="stealth/space-bunny-alpha", name="Space Bunny Alpha",
                          credit_rate=0.0, max_input_tokens=131072,
                          supports_tool_call=True)


def test_save_skips_empty_tables_and_creates_dir(tmp_path):
    """空表不落盘（否则「该渠道此刻真的没有模型」会被当成有缓存）；目录自动建。"""
    target = tmp_path / "nested" / "data"
    save_catalog(str(target), {"kilo": {}, "zen": {"a-free": Model(id="a-free")}})
    payload = json.loads((target / CATALOG_FILENAME).read_text(encoding="utf-8"))
    assert list(payload["providers"]) == ["zen"]


def test_save_failure_is_logged_not_raised(tmp_path, caplog):
    """写盘失败（这里是 data_dir 指向一个已存在的文件）只记日志。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a dir")
    with caplog.at_level("WARNING"):
        save_catalog(str(blocker), {"kilo": {"m": Model(id="m")}})
    assert any("模型目录落盘失败" in r.getMessage() for r in caplog.records)


def test_load_missing_file_returns_empty(tmp_path):
    assert load_catalog(str(tmp_path)) == {}


def test_load_broken_json_returns_empty(tmp_path, caplog):
    """损坏的 JSON 只丢缓存，绝不让服务起不来。"""
    (tmp_path / CATALOG_FILENAME).write_text("{not json", encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert load_catalog(str(tmp_path)) == {}
    assert any("模型目录读取失败" in r.getMessage() for r in caplog.records)


def test_load_version_mismatch_is_ignored(tmp_path, caplog):
    """未来版本/非 dict 载荷：不猜，直接忽略。"""
    path = tmp_path / CATALOG_FILENAME
    path.write_text(json.dumps({"version": 999, "providers": {"kilo": {}}}),
                    encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert load_catalog(str(tmp_path)) == {}
    assert any("模型目录版本不匹配" in r.getMessage() for r in caplog.records)

    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert load_catalog(str(tmp_path)) == {}


def test_load_drops_broken_records_but_keeps_good_ones(tmp_path):
    """逐渠道宽容：坏记录只丢自己那条渠道，其余照常用（kilo 归属最需要它）。"""
    payload = {
        "version": 1,
        "providers": {
            "kilo": {"saved_at": time.time(), "models": [{"id": "m", "unknown": 1}]},
            "trae": {"saved_at": time.time(), "models": "not-a-list"},
            "zen": {"saved_at": time.time(), "models": [{"no_id": True}, "junk",
                                                       {"id": ""}]},
            "qoder": {"saved_at": "not-a-number", "models": [{"id": "q"}]},
            "codearts": {"saved_at": time.time() - MAX_AGE_SECONDS - 1,
                         "models": [{"id": "old"}]},
            "trae2": "not-a-record",
        },
    }
    (tmp_path / CATALOG_FILENAME).write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_catalog(str(tmp_path))
    assert list(loaded) == ["kilo"]
    # 未知字段被忽略，老快照缺的字段走 dataclass 默认值
    assert loaded["kilo"]["m"] == Model(id="m")
    # providers 不是 dict（旧版本/手改文件）时整体退化为空
    (tmp_path / CATALOG_FILENAME).write_text(
        json.dumps({"version": 1, "providers": 7}), encoding="utf-8")
    assert load_catalog(str(tmp_path)) == {}


def test_catalog_path_joins_data_dir():
    assert catalog_path("/data") == f"/data/{CATALOG_FILENAME}"


# --------------------------------------------------- 启动回灌 + 增量 publish


def _app_with(settings, providers, credential_providers=("kilo",)):
    app = build_app(settings, providers=providers)
    for provider_id in credential_providers:
        app.state.credentials.add(provider=provider_id, credential_data={"accessToken": "a"})
    return app


def test_restore_publishes_aliases_without_touching_upstream(settings):
    """回灌是纯本地动作：kilo 独有模型立刻有归属，一次上游请求都不发。"""
    save_catalog(settings.data_dir, {"kilo": {"stealth/space-bunny-alpha": Model(
        id="stealth/space-bunny-alpha", name="Space Bunny Alpha", credit_rate=0.0)}})
    provider = _StubProvider("kilo", [])
    app = _app_with(settings, {"kilo": provider})
    services = app.state.services

    assert restore_model_catalog(services) == 1
    assert "stealth/space-bunny-alpha" in services.model_aliases["kilo"]
    assert provider.calls == 0


def test_startup_keeps_restored_aliases_when_warmup_fails(settings):
    """预热（上游全挂）失败也不影响已恢复的别名表——这正是落盘的意义。"""

    class Broken(_StubProvider):
        async def list_models(self, credential_data):
            self.calls += 1
            raise RuntimeError("upstream down")

    save_catalog(settings.data_dir, {"kilo": {"kilo-only/free": Model(
        id="kilo-only/free", name="Kilo Only", credit_rate=0.0)}})
    provider = Broken("kilo", [])
    app = _app_with(settings, {"kilo": provider})
    with TestClient(app):
        services = app.state.services
        assert services.model_aliases["kilo"]["kilo-only/free"] == "kilo-only/free"
        assert provider.calls == 1              # 预热确实打过上游并失败


def test_startup_skips_unregistered_and_credential_less_channels(settings, caplog):
    """未注册的渠道、以及没有可用凭证的渠道都不恢复（暂停的渠道不该复活）。"""
    save_catalog(settings.data_dir, {
        "kilo": {"kilo-m": Model(id="kilo-m")},
        "trae": {"trae-m": Model(id="trae-m")},
    })
    app = _app_with(settings, {"kilo": _StubProvider("kilo", [])},
                    credential_providers=())
    with caplog.at_level("INFO"), TestClient(app):
        assert app.state.services.model_list_cache == {}
        assert app.state.services.model_aliases == {}
    assert not any("恢复模型目录" in r.getMessage() for r in caplog.records)


def test_restore_seeds_zen_probe_cache(settings):
    """恢复出的 zen 免费集回填判活缓存：重启后预热不再逐个真发探活。"""

    class _Seeded:
        id = "zen"
        client = None
        seeded: list = []

        async def list_models(self, credential_data):  # pragma: no cover - 不该被调
            raise AssertionError("restore 阶段不该打上游")

        def import_credential(self, raw):  # pragma: no cover - 由凭证导入调用
            return raw

    provider = _Seeded()
    seeded: list[Model] = []

    class _Client:
        def seed_models_cache(self, models):
            seeded.extend(models)

    provider.client = _Client()
    save_catalog(settings.data_dir, {"zen": {"big-pickle-free": Model(
        id="big-pickle-free", name="Big Pickle", credit_rate=0.0)}})
    app = _app_with(settings, {"zen": provider}, credential_providers=("zen",))
    with TestClient(app):
        assert [m.id for m in seeded] == ["big-pickle-free"]


def test_aliases_published_per_provider_before_slow_channel_finishes(settings):
    """慢渠道未完成时，已拉好的渠道别名已生效（不等 zen 探活十几秒）。"""
    slow_started = asyncio.Event()
    release = asyncio.Event()

    class Slow(_StubProvider):
        async def list_models(self, credential_data):
            slow_started.set()
            await release.wait()
            return [Model(id="zen-only-free")]

    fast = _StubProvider("kilo", [Model(id="stealth/space-bunny-alpha")])
    app = build_app(settings, providers={"kilo": fast, "zen": Slow("zen", [])})
    for provider_id in ("kilo", "zen"):
        app.state.credentials.add(provider=provider_id, credential_data={"accessToken": "a"})

    async def scenario():
        task = asyncio.create_task(_list_models(app.state.services))
        await slow_started.wait()
        # kilo 已拉完、zen 仍在拉：别名表此刻就必须已经有 kilo 的归属
        aliases = app.state.services.model_aliases
        assert "stealth/space-bunny-alpha" in aliases["kilo"]
        assert "zen" not in aliases
        release.set()
        await task

    asyncio.run(scenario())


async def _list_models(services):
    from src.api.models import list_models

    return await list_models(services, force=True)


def test_publish_aliases_skips_channel_without_credentials(settings):
    """`merged_entries` 与调度同口径：没有可用凭证的渠道不进别名表。"""
    app = _app_with(settings, {"kilo": _StubProvider("kilo", [])},
                    credential_providers=("kilo",))
    services = app.state.services
    services.model_list_cache["kilo"] = {"kilo-m": Model(id="kilo-m")}
    services.model_list_cache["trae"] = {"trae-m": Model(id="trae-m")}
    services.model_list_cache["zen"] = {}          # 空表：跳过

    assert [e["providers"] for e in merged_entries(services, {"kilo", "zen"})] == [{"kilo"}]
    publish_aliases(services, {"kilo", "zen"})
    assert list(services.model_aliases) == ["kilo"]


def test_restore_failure_is_logged_not_raised(settings, caplog):
    """回灌期间的任何异常（这里是仓储炸了）只记日志，服务照常启动。"""
    from src.api.deps import Services

    app = build_app(settings, providers={})
    app.state.credentials.candidates = (
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("db gone")))
    services: Services = app.state.services
    with caplog.at_level("WARNING"):
        _restore_model_list(services)
    assert any("恢复落盘模型目录失败" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------- 后台兜底刷新循环


def test_lifespan_wires_periodic_model_catalog_refresh(settings):
    """装配了兜底刷新循环：管理台能看到它，默认周期 30 分钟（对齐 zen 判活 TTL）。"""
    app = _app_with(settings, {"kilo": _StubProvider("kilo", [Model(id="kilo-m")])})
    with TestClient(app):
        status = {item["key"]: item for item in app.state.task_runner.task_status()}
    assert status["model_catalog"]["interval_seconds"] == 1800
    assert status["model_catalog"]["enabled"] is True


@pytest.mark.asyncio
async def test_periodic_refresh_reports_only_model_count(settings):
    """一轮刷新只把条目数报进运行态——整份列表有几百条，管理台渲染不动。"""
    provider = _StubProvider("kilo", [Model(id="kilo-m"), Model(id="kilo-n")])
    app = _app_with(settings, {"kilo": provider})
    async with app.router.lifespan_context(app):
        runner = app.state.task_runner
        assert await runner._guarded(runner._model_catalog(), "模型目录刷新",
                                     key="model_catalog")
        run = runner.status.get("model_catalog")
        assert run.ok is True and run.report == {"models": 2}
        # 走的是同一条 list_models 路径：缓存已更新、别名表已 publish
        assert set(app.state.services.model_aliases["kilo"]) >= {"kilo-m", "kilo-n"}


def test_build_runner_without_refresh_keeps_task_hidden(settings, tmp_path):
    """不注入刷新协程时不装配这条循环（老调用方/测试保持原行为）。"""
    from src.config import Settings
    from src.db.conn import Database
    from src.db.crypto import CredentialCipher
    from src.db.migrate import apply_schema
    from src.db.repo import CredentialRepository
    from src.tasks.runner import build_runner

    db = Database(str(tmp_path / "c.sqlite3"))
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    config = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                      MODEL_CATALOG_MINUTES=1)
    runner = build_runner(credentials, {}, None, config)
    keys = {item["key"] for item in runner.task_status()}
    assert "model_catalog" not in keys
    # 下限 5 分钟：配 1 分钟也只按 5 分钟跑（更密只是白打各渠道 /models）
    assert runner._model_catalog_interval == 300


def test_list_models_serializes_concurrent_callers(settings):
    """HTTP 出口与后台刷新并发时串行化：同一条渠道只打一次上游。"""
    provider = _StubProvider("kilo", [Model(id="kilo-m")])
    app = _app_with(settings, {"kilo": provider})
    services = app.state.services

    async def scenario():
        await asyncio.gather(list_models(services), list_models(services))

    asyncio.run(scenario())
    assert provider.calls == 1
    assert "kilo-m" in services.model_aliases["kilo"]
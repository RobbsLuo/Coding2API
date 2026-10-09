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
import logging
import pathlib
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
    MODEL_LIST_TTL_SECONDS,
    _build_response,
    list_models,
    merged_entries,
    publish_aliases,
    restore_model_catalog,
    serve_models,
)
from src.auth.session import create_session_token
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
    saved_at, table = loaded["kilo"]
    assert 0 <= time.time() - saved_at < 5          # 新鲜度一起带回（TTL 播种要用）
    assert table["stealth/space-bunny-alpha"] == Model(
        id="stealth/space-bunny-alpha", name="Space Bunny Alpha",
        credit_rate=0.0, max_input_tokens=131072, supports_tool_call=True)


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
    """未来版本：不猜，直接忽略。"""
    path = tmp_path / CATALOG_FILENAME
    path.write_text(json.dumps({"version": 999, "providers": {"kilo": {}}}),
                    encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert load_catalog(str(tmp_path)) == {}
    assert any("模型目录版本不匹配" in r.getMessage() for r in caplog.records)


def test_load_skips_expired_snapshot(tmp_path):
    """超龄快照整条丢弃：停机很久的部署不该拿几天前的目录发请求。"""
    payload = {"version": 1, "providers": {"kilo": {
        "saved_at": time.time() - MAX_AGE_SECONDS - 1,
        "models": [{"id": "old"}]}}}
    (tmp_path / CATALOG_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    assert load_catalog(str(tmp_path)) == {}


def test_load_tolerates_unknown_fields_and_bad_items(tmp_path):
    """条目级宽容（免得一个坏模型带走整表）；文件级损坏则整体丢弃。"""
    payload = {"version": 1, "providers": {"kilo": {
        "saved_at": time.time(),
        "models": [{"id": "m", "unknown": 1}, {"no_id": True}, "junk"]}}}
    (tmp_path / CATALOG_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    assert load_catalog(str(tmp_path))["kilo"][1] == {"m": Model(id="m")}

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


def test_startup_uses_snapshot_even_when_upstream_is_down(settings):
    """快照新鲜时预热压根不打上游：上游挂着也不影响别名表（落盘的意义）。"""

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
        assert provider.calls == 0              # 快照年龄在 TTL 内，预热不重拉


def test_startup_refetches_snapshot_older_than_ttl(settings):
    """快照超 TTL 则照常重拉（kilo 实测一次 10–22s，refresh 后别名跟着更新）。"""
    payload = {"version": 1, "providers": {"kilo": {
        "saved_at": time.time() - MODEL_LIST_TTL_SECONDS - 1,
        "models": [{"id": "old-free"}]}}}
    (pathlib.Path(settings.data_dir) / CATALOG_FILENAME).write_text(
        json.dumps(payload), encoding="utf-8")

    provider = _StubProvider("kilo", [Model(id="kilo-only/free", name="Kilo Only",
                                            credit_rate=0.0)])
    app = _app_with(settings, {"kilo": provider})
    with TestClient(app):
        aliases = app.state.services.model_aliases
        assert provider.calls == 1
        assert "old-free" not in aliases.get("kilo", {})
        assert "kilo-only/free" in aliases["kilo"]


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
    return await list_models(services)


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
        assert await runner._guarded(runner._model_catalog(), "渠道模型列表刷新",
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


# --------------------------------------- stale-while-revalidate（Playground 出口）


def test_playground_serves_stale_list_and_refreshes_in_background(settings):
    """TTL 过期但仍有缓存时：立刻回旧列表，后台异步刷新（不卡请求）。

    回归点：此前 TTL 一过 HTTP 出口就同步重拉全部渠道，zen 探活十几秒全压在
    Playground 打开请求上。
    """
    provider = _StubProvider("kilo", [Model(id="kilo-m")])
    app = _app_with(settings, {"kilo": provider})
    services = app.state.services
    # 先拉一次填充缓存，再模拟 TTL 过期
    asyncio.run(list_models(services))
    assert provider.calls == 1
    services.model_list_fetched_at.clear()

    async def scenario():
        response = await serve_models(services)   # 立刻返回旧列表
        assert response["data"][0]["id"] == "kilo-m"
        # 后台任务已排队（此刻可能还没跑完）
        assert services.pending_model_refreshes
        await asyncio.gather(*list(services.pending_model_refreshes))

    asyncio.run(scenario())
    assert provider.calls == 2                              # 后台补齐了一次
    assert not services.model_refreshing                    # 刷新完释放
    assert not services.model_refresh_tasks


def test_playground_blocks_once_when_channel_has_no_cache(settings):
    """某渠道无任何缓存（冷启动无快照 / 新接入）时同步等它，列表不缺模型。"""
    provider = _StubProvider("kilo", [Model(id="kilo-m")])
    app = _app_with(settings, {"kilo": provider})
    services = app.state.services
    assert services.model_list_cache == {}

    response = asyncio.run(serve_models(services))
    assert [m["id"] for m in response["data"]] == ["kilo-m"]
    assert provider.calls == 1
    assert not services.model_refreshing


def test_playground_does_not_reschedule_while_refresh_in_flight(settings):
    """刷新任务在途时，后续请求不再重复排队（`model_refreshing` 去重）。"""
    release = asyncio.Event()

    class Slow(_StubProvider):
        async def list_models(self, credential_data):
            self.calls += 1
            await release.wait()
            return list(self._models)

    provider = Slow("kilo", [Model(id="kilo-m")])
    app = _app_with(settings, {"kilo": provider})
    services = app.state.services
    services.model_list_cache["kilo"] = {"kilo-m": Model(id="kilo-m")}
    services.model_list_fetched_at.pop("kilo", None)        # 过期

    async def scenario():
        await serve_models(services)
        first = len(services.pending_model_refreshes)
        await serve_models(services)              # 在途，去重
        assert len(services.pending_model_refreshes) == first
        release.set()
        await asyncio.gather(*list(services.pending_model_refreshes))

    asyncio.run(scenario())
    assert provider.calls == 1


def test_playground_background_refresh_survives_upstream_failure(settings):
    """后台刷新抛错只记日志，不影响本次已返回的旧列表。"""
    class Broken(_StubProvider):
        async def list_models(self, credential_data):
            self.calls += 1
            raise RuntimeError("upstream down")

    provider = Broken("kilo", [])
    app = _app_with(settings, {"kilo": provider})
    services = app.state.services
    services.model_list_cache["kilo"] = {"kilo-m": Model(id="kilo-m")}
    services.model_list_fetched_at.pop("kilo", None)

    async def scenario():
        response = await serve_models(services)
        assert response["data"][0]["id"] == "kilo-m"
        await asyncio.gather(*list(services.pending_model_refreshes),
                             return_exceptions=True)

    asyncio.run(scenario())
    assert provider.calls == 1
    assert not services.model_refreshing                    # 异常也释放去重标记


def test_playground_background_refresh_logs_unexpected_error(settings, monkeypatch,
                                                            caplog):
    """后台刷新线程内出现未预期异常（如合并/publish 抛错）也只记日志。"""
    from src.api import models as models_module

    provider = _StubProvider("kilo", [Model(id="kilo-m")])
    app = _app_with(settings, {"kilo": provider})
    services = app.state.services
    services.model_list_cache["kilo"] = {"kilo-m": Model(id="kilo-m")}
    services.model_list_fetched_at.pop("kilo", None)

    async def boom(_services, _connected):
        raise RuntimeError("merge exploded")

    monkeypatch.setattr(models_module, "_refresh_providers", boom)

    with caplog.at_level(logging.WARNING):
        async def scenario():
            await serve_models(services)
            await asyncio.gather(*list(services.pending_model_refreshes),
                                 return_exceptions=True)

        asyncio.run(scenario())

    assert "后台模型列表刷新失败" in caplog.text
    assert not services.model_refreshing                    # 异常也释放去重标记


def test_list_response_filters_by_api_key_model_allowlist(settings):
    """Key 级模型白名单在 `_build_response` 里现滤（列表与实际可用一致）。"""
    provider = _StubProvider("kilo", [Model(id="glm-m"), Model(id="kilo-m")])
    app = _app_with(settings, {"kilo": provider})
    services = app.state.services
    asyncio.run(list_models(services))            # 先填缓存（不碰上游的出口需要它）

    full = _build_response(services, {"kilo"})
    assert [m["id"] for m in full["data"]] == ["glm-m", "kilo-m"]

    filtered = _build_response(services, {"kilo"}, allowed_models="glm-*")
    assert [m["id"] for m in filtered["data"]] == ["glm-m"]


def test_schedule_refresh_noop_without_stale_channels(settings):
    """没有过期渠道时不排后台任务（`_schedule_refresh` 早退分支）。"""
    provider = _StubProvider("kilo", [Model(id="kilo-m")])
    app = _app_with(settings, {"kilo": provider})
    services = app.state.services

    async def scenario():
        await list_models(services)                         # 缓存新鲜
        await serve_models(services)
        assert services.pending_model_refreshes == []

    asyncio.run(scenario())
    assert provider.calls == 1


def test_lifespan_cancels_inflight_background_refresh(settings):
    """关闭时取消在途的后台刷新，不把上游请求（zen 探活十几秒）带出事件循环。"""
    class Slow:
        id = "kilo"

        async def list_models(self, credential_data):
            await asyncio.sleep(60)                 # 永不自然返回：靠取消退场
            return [Model(id="kilo-m")]             # pragma: no cover - 取消先到

        def import_credential(self, raw):           # pragma: no cover - 未调用
            return raw

    app = build_app(settings, providers={"kilo": Slow()})
    app.state.credentials.add(provider="kilo", credential_data={"accessToken": "a"})
    services = app.state.services
    # 预置新鲜缓存：预热不碰上游，把「在途刷新」留给本用例显式触发。
    services.model_list_cache["kilo"] = {"kilo-m": Model(id="kilo-m")}
    services.model_list_fetched_at["kilo"] = time.monotonic()

    with TestClient(app) as client:
        services.model_list_fetched_at.pop("kilo", None)     # 令其过期
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        assert client.get("/api/playground/models").json()["data"]
        assert services.model_refresh_tasks              # 后台刷新已排队

    # 退出 with → lifespan 关闭：任务被取消并摘除（否则 await 会挂 60s）
    assert services.model_refresh_tasks == set()
    assert services.pending_model_refreshes == []
"""上游状态页抓取任务：一轮 = 并发抓全部源 → 原子替换内存快照。

任务层只守着两件事：**开关语义**（关着时 no-op、不覆盖运行态）与**永不抛异常**
（单源失败 / 全源失败都只进 report）。四种解析器的真值由
`tests/test_upstream_status.py` 覆盖，这里用假源与 MockTransport 打通链路。
"""

from __future__ import annotations

import httpx
import pytest

from src.tasks.upstream_status import UpstreamStatusTask
from src.upstream_status import (
    LEVEL_DEGRADED,
    LEVEL_OPERATIONAL,
    LEVEL_UNKNOWN,
    SOURCE_SPECS,
    SourceSpec,
    UpstreamStatusStore,
    spec_by_key,
)


@pytest.fixture(autouse=True)
def _real_fetch_all(monkeypatch):
    """本文件要真跑抓取链路：把全局 conftest 的「空抓取」桩换回真实现。

    全局桩掐的是 `src.tasks.upstream_status.fetch_all`；这里从
    `src.upstream_status` 取真函数装回去，各用例再用 MockTransport 接管网络。
    """
    from src.tasks import upstream_status as task_module
    from src.upstream_status import fetch_all as real_fetch_all

    monkeypatch.setattr(task_module, "fetch_all", real_fetch_all)

KILO = "kilo"
MOONSHOT = "moonshot"

# 最小 Statuspage 载荷：解析器要的字段齐全即可
STATUSPAGE = {"status": {"indicator": "none"}, "components": [], "incidents": []}
STATUSPAGE_DEGRADED = {"status": {"indicator": "minor"}, "components": [],
                       "incidents": []}


def _specs(*keys: str) -> tuple[SourceSpec, ...]:
    found = [spec_by_key(key) for key in keys]
    assert all(item is not None for item in found)
    return tuple(item for item in found if item is not None)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _task(store: UpstreamStatusStore, specs, handler, **kwargs) -> UpstreamStatusTask:
    return UpstreamStatusTask(store, specs=specs, client_factory=lambda: _client(handler),
                              **kwargs)


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=STATUSPAGE)


async def test_run_once_updates_snapshot_and_reports_counts():
    store = UpstreamStatusStore()
    task = _task(store, _specs(KILO, MOONSHOT), _ok)

    report = await task.run_once()

    assert report == {"sources": 2, "abnormal": 0, "failed": 0}
    assert [item.spec.key for item in store.snapshot()] == [KILO, MOONSHOT]
    assert store.fetched_at is not None


async def test_run_once_counts_abnormal_and_failed_separately():
    """抓取失败同时算「异常」与「失败」：页面要分开渲染这两类。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "status.kilo.ai":
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json=STATUSPAGE_DEGRADED)

    store = UpstreamStatusStore()
    task = _task(store, _specs(KILO, MOONSHOT), handler)

    report = await task.run_once()

    assert report == {"sources": 2, "abnormal": 2, "failed": 1}
    levels = {item.spec.key: item.level for item in store.snapshot()}
    assert levels == {KILO: LEVEL_UNKNOWN, MOONSHOT: LEVEL_DEGRADED}


async def test_run_once_never_raises_when_every_source_fails():
    """全源失败也只是 report：任务失败会触发「连续失败」告警，而断网不值得叫人。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    store = UpstreamStatusStore()
    task = _task(store, _specs(KILO, MOONSHOT), handler)

    report = await task.run_once()

    assert report == {"sources": 2, "abnormal": 2, "failed": 2}
    assert all(item.level == LEVEL_UNKNOWN for item in store.snapshot())


async def test_run_once_is_noop_when_disabled():
    """关闭时返回 None（no-op，不覆盖上次运行态），且不打上游。"""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=STATUSPAGE)

    store = UpstreamStatusStore()
    task = _task(store, _specs(KILO), handler, enabled=lambda: False)

    assert await task.run_once() is None
    assert calls == []
    assert store.snapshot() == () and store.fetched_at is None


async def test_run_once_reads_enabled_each_round():
    """开关每轮现读：关掉再打开不必重启，与 alert / activity 同一热更范式。"""
    flag = {"on": False}
    store = UpstreamStatusStore()
    task = _task(store, _specs(KILO), _ok, enabled=lambda: flag["on"])

    assert await task.run_once() is None
    flag["on"] = True
    assert await task.run_once() == {"sources": 1, "abnormal": 0, "failed": 0}


async def test_run_once_owns_client_when_no_factory(monkeypatch):
    """未注入 client_factory 时走 fetch_all 的自建 client 分支（不打真网）。"""
    from src import upstream_status as module

    seen: list[object] = []

    async def fake_fetch_all(specs, *, timeout, client=None):
        seen.append(client)
        assert client is None
        return tuple(
            module.SourceStatus(spec=spec, level=LEVEL_OPERATIONAL, fetched_at=1)
            for spec in specs)

    monkeypatch.setattr("src.tasks.upstream_status.fetch_all", fake_fetch_all)
    store = UpstreamStatusStore()
    task = UpstreamStatusTask(store, specs=_specs(KILO))

    report = await task.run_once()

    assert report == {"sources": 1, "abnormal": 0, "failed": 0}
    assert seen == [None]


async def test_run_once_defaults_to_every_source(monkeypatch):
    """不传 specs 时抓全量（生产路径就是这样装配的）。"""
    async def fake_fetch_all(specs, *, timeout, client=None):
        assert tuple(specs) == SOURCE_SPECS
        assert timeout == 10.0
        return ()

    monkeypatch.setattr("src.tasks.upstream_status.fetch_all", fake_fetch_all)
    task = UpstreamStatusTask(UpstreamStatusStore())

    assert await task.run_once() == {"sources": 0, "abnormal": 0, "failed": 0}


async def test_run_once_passes_timeout():
    seen: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout", {}).get("read", 0.0))
        return httpx.Response(200, json=STATUSPAGE)

    store = UpstreamStatusStore()
    task = _task(store, _specs(KILO), handler, timeout=3.5)

    await task.run_once()

    assert seen == [3.5]


async def test_warm_upstream_status_logs_failure(caplog):
    """启动预热失败只记日志、不阻断服务（与 `_warm_model_catalog` 同一口径）。"""
    from src.main import _warm_upstream_status

    class Boom:
        async def run_once(self):
            raise RuntimeError("network down")

    with caplog.at_level("WARNING"):
        await _warm_upstream_status(Boom())
    assert any("启动预热上游状态页失败" in r.getMessage() for r in caplog.records)


def test_shutdown_cancels_pending_upstream_warmup(tmp_path, monkeypatch):
    """预热还在等上游时关闭：必须取消，否则 in-flight 请求会被带出事件循环。"""
    import asyncio

    from fastapi.testclient import TestClient

    from src.config import Settings
    from src.main import build_app
    from tests.helpers.constants import SECRET

    async def slow_run_once(self):
        await asyncio.sleep(3600)

    monkeypatch.setattr(UpstreamStatusTask, "run_once", slow_run_once)
    app = build_app(Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path)))

    with TestClient(app):
        warmup = app.state.upstream_warmup_task
        assert not warmup.done()        # 仍卡在「等上游」

    assert warmup.cancelled() or warmup.done()


def test_shutdown_cancels_all_pending_warmups(tmp_path, monkeypatch):
    """关闭时三个后台预热（模型列表 / 价表 / 上游状态）都要取消。

    三个都 monkeypatch 成挂住，是为了让**取消分支确定性覆盖**：此前只挂上游
    状态页，模型/价表预热靠「关闭时正好还没跑完」才走到取消块——CI（无 `.env`、
    无外网）里它们先跑完，`main` 的取消分支变成未覆盖，100% 门槛随机红
    （实测 2026-11-02 CI 报 `src/main.py 717-719` 缺失）。
    """
    import asyncio

    from fastapi.testclient import TestClient

    from src.config import Settings
    from src.main import build_app
    from tests.helpers.constants import SECRET

    async def hang(*_args, **_kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr("src.main._warm_model_list", hang)
    monkeypatch.setattr("src.main._warm_model_catalog", hang)
    monkeypatch.setattr(UpstreamStatusTask, "run_once", hang)
    app = build_app(Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path)))

    with TestClient(app):
        warmups = [app.state.model_warmup_task, app.state.catalog_warmup_task,
                   app.state.upstream_warmup_task]
        assert all(not task.done() for task in warmups)

    assert all(task.cancelled() or task.done() for task in warmups)


@pytest.mark.parametrize("level", [LEVEL_OPERATIONAL, LEVEL_DEGRADED])
async def test_run_once_counts_operational_as_normal(level):
    """operational 之外才算异常——包括维护与未知。"""
    payload = {"status": {"indicator": "none" if level == LEVEL_OPERATIONAL
                          else "minor"},
               "components": [], "incidents": []}
    store = UpstreamStatusStore()
    task = _task(store, _specs(KILO), lambda request: httpx.Response(200, json=payload))

    report = await task.run_once()

    assert report["abnormal"] == (0 if level == LEVEL_OPERATIONAL else 1)

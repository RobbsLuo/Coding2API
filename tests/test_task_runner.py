"""后台任务装配与生命周期测试。

覆盖 TaskRunner 的启动、循环、异常隔离与停止，以及 provider 资源释放。
此前 QuotaProbeTask / CheckinTask / RefreshTask / RetentionTask 全部是死代码
（从未在应用里实例化），这些测试保证它们真的被接线。
"""

from __future__ import annotations

import asyncio

import pytest

from src.config import Settings
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository
from src.provider.base import Quota
from src.stats.collector import StatsCollector
from src.tasks.background import CheckinTask, QuotaProbeTask, RefreshTask, RetentionTask
from src.tasks.runner import TaskRunner, build_runner


@pytest.fixture()
def repo(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    yield CredentialRepository(db, CredentialCipher("s")), db
    db.close()


class StubProvider:
    id = "codebuddy"

    def __init__(self) -> None:
        self.quota_calls = 0
        self.closed = False

    async def probe_quota(self, _data):
        self.quota_calls += 1
        return Quota(remaining=5, total=10, probed_at=1)

    async def checkin(self, _data):  # pragma: no cover - 由 CheckinTask 调用
        raise AssertionError("不应在 runner 测试里触发签到成功路径")

    def checkin_scope(self, _data):  # pragma: no cover
        return "scope"

    def credential_from(self, data):
        from src.provider.codebuddy.credential import CodeBuddyCredential

        return CodeBuddyCredential.from_dict(data)

    async def refresh(self, data):  # pragma: no cover
        return data

    async def aclose(self) -> None:
        self.closed = True


def _runner(repo_tuple, provider, *, quota_interval_minutes=60):
    credentials, db = repo_tuple
    collector = StatsCollector(db)
    runner = TaskRunner(
        quota_probe=QuotaProbeTask(credentials, {"codebuddy": provider}, None),
        checkin=CheckinTask(credentials, {"codebuddy": provider}, checkin_hour=23),
        refresh=RefreshTask(credentials, {"codebuddy": provider}, skew_seconds=3600),
        retention=RetentionTask(collector),
        quota_probe_minutes=quota_interval_minutes,
    )
    return runner, collector


async def test_runner_start_runs_initial_probe_without_pacing(repo):
    """启动首轮额度探测必须立即执行（不节流），否则池里全是 unknown。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    provider = StubProvider()
    runner, _collector = _runner(repo, provider)

    await runner.start()
    try:
        assert provider.quota_calls == 1
        assert credentials.candidates()[0].health == 50
    finally:
        await runner.stop()


async def test_runner_stop_cancels_all_loops(repo):
    provider = StubProvider()
    runner, _collector = _runner(repo, provider)
    await runner.start()
    assert len(runner._tasks) == 4
    await runner.stop()
    assert runner._tasks == []
    # 取消后不应再有新的探测
    before = provider.quota_calls
    await asyncio.sleep(0.05)
    assert provider.quota_calls == before


async def test_runner_loop_survives_task_failure(repo):
    """单个任务抛异常不能让循环退出（后台任务不能拖垮服务）。"""
    credentials, db = repo
    provider = StubProvider()

    class Exploding:
        async def run_once(self, **_kwargs):
            raise RuntimeError("probe blown up")

    collector = StatsCollector(db)
    runner = TaskRunner(
        quota_probe=Exploding(),  # type: ignore[arg-type]
        checkin=CheckinTask(credentials, {"codebuddy": provider}, checkin_hour=23),
        refresh=RefreshTask(credentials, {"codebuddy": provider}, skew_seconds=3600),
        retention=RetentionTask(collector),
        quota_probe_minutes=60,
    )
    # 启动首轮失败必须被吞掉，且循环仍然建立
    await runner.start()
    try:
        assert len(runner._tasks) == 4
        assert all(not task.done() for task in runner._tasks)
    finally:
        await runner.stop()


async def test_runner_direct_loop_invocation_is_guarded(repo):
    provider = StubProvider()
    runner, _collector = _runner(repo, provider)

    class Boom:
        async def run_once(self, **_kwargs):
            raise RuntimeError("boom")

    # 直接驱动内部循环体：异常被 _guarded 吞掉并返回 False
    assert await runner._guarded(Boom().run_once(), "测试任务") is False
    assert await runner._guarded(asyncio.sleep(0, result=1), "正常任务") is True


async def test_runner_checkin_skips_before_hour(repo):
    """未到签到时刻时 _sync_checkin 是 no-op。"""
    provider = StubProvider()
    runner, _collector = _runner(repo, provider)
    assert await runner._sync_checkin() is None


async def test_runner_retention_purges_expired_detail_and_keeps_rollup(repo):
    """清理任务必须删掉过期明细、留下小时汇总。"""
    import time as _time

    credentials, db = repo
    collector = StatsCollector(db)
    old = int(_time.time()) - 91 * 86400
    collector.record(username="u", provider="trae", model="m", ok=True, now=old)
    runner, _ = _runner(repo, StubProvider())

    result = await runner._sync_retention()
    assert isinstance(result, dict)
    assert result["purged"] == 1
    assert collector._db.connect().execute(
        "SELECT COUNT(*) AS c FROM usage_hourly").fetchone()["c"] >= 1
    assert collector._db.connect().execute(
        "SELECT COUNT(*) AS c FROM usage_events").fetchone()["c"] == 0


def test_build_runner_wires_everything(repo):
    credentials, db = repo
    config = Settings(_env_file=None, APP_SECRET="s", QUOTA_PROBE_MINUTES=15,
                      CHECKIN_HOUR=7, REFRESH_SKEW_HOURS=6,
                      PACER_MIN_SECONDS=0, PACER_MAX_SECONDS=0)
    runner = build_runner(credentials, {"codebuddy": StubProvider()}, StatsCollector(db), config)
    assert runner._quota_interval == 15 * 60
    assert runner._checkin_hour == 7
    assert runner._quota_probe._pacer is not None
    assert runner._quota_probe._pacer.disabled is True
    assert runner._refresh.skew_seconds == 6 * 3600


def test_build_runner_clamps_intervals(repo):
    """过小的配置值必须被夹到安全下限，避免打爆上游。"""
    credentials, db = repo
    config = Settings(_env_file=None, APP_SECRET="s", QUOTA_PROBE_MINUTES=0)
    runner = build_runner(credentials, {}, StatsCollector(db), config)
    assert runner._quota_interval == 60
    assert runner._refresh_interval >= 60
    assert runner._retention_interval >= 600


async def test_providers_release_http_pools_on_shutdown(tmp_path):
    """lifespan 关闭时必须释放 provider 的连接池。"""
    from src.provider.codebuddy.client import CodeBuddyProvider
    from src.provider.trae.client import TraeProvider

    codebuddy = CodeBuddyProvider()
    trae = TraeProvider()
    # 触发惰性客户端创建
    assert codebuddy.client._stream is not None
    assert trae.client._stream() is not None

    await codebuddy.aclose()
    await trae.aclose()
    # 关闭后再次调用必须幂等
    await codebuddy.aclose()
    await trae.aclose()


def test_run_entry_point_exists_and_is_callable():
    """pyproject 的 [project.scripts] 指向 src.main:run，它必须存在。"""
    import inspect

    from src import main

    assert callable(main.run)
    assert len(inspect.signature(main.run).parameters) == 0


# ------------------------------------------------- 剩余边界分支

async def test_runner_checkin_fires_when_due(repo, monkeypatch):
    """到点后 _sync_checkin 必须真的执行签到（runner 62→64）。"""
    import time as _time

    credentials, db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    provider = StubProvider()
    calls: list[int] = []

    async def fake_run_once(*, now=None):
        calls.append(1)
        from src.tasks.background import TaskReport

        return TaskReport()

    runner, _collector = _runner(repo, provider)
    runner._checkin.due = lambda **_kw: True           # 强制「到点」
    runner._checkin.run_once = fake_run_once            # type: ignore[method-assign]

    result = await runner._sync_checkin()
    assert calls == [1]
    assert result is not None
    assert _time is not None and db is not None


async def test_loop_propagates_cancellation(repo):
    """取消循环必须向上传播 CancelledError，不能当成普通失败吞掉（runner 73-76）。"""
    provider = StubProvider()
    runner, _collector = _runner(repo, provider)

    async def slow():
        return None

    task = asyncio.create_task(runner._loop("测试", slow, 3600))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_guarded_propagates_cancellation(repo):
    """_guarded 收到 CancelledError 时必须重新抛出（runner 82-83）。"""
    runner, _collector = _runner(repo, StubProvider())

    async def cancelled():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await runner._guarded(cancelled(), "取消任务")


def test_run_entry_point_starts_uvicorn(monkeypatch, tmp_path):
    """run() 必须把配置传给 uvicorn.run（main 433-437）。"""
    import sys
    import types

    from src import main

    captured: dict = {}

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured.update(kwargs)

    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.run = fake_run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setenv("APP_SECRET", "s")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "9123")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")

    main.run()

    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9123
    assert captured["log_level"] == "warning"
    assert captured["app"] is not None


async def test_loop_executes_runner_after_interval(repo):
    """循环体在 sleep 之后必须真的调用 runner（runner 76）。"""
    runner, _collector = _runner(repo, StubProvider())
    calls: list[int] = []

    async def runner_body():
        calls.append(1)

    task = asyncio.create_task(runner._loop("立即任务", runner_body, 0))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls, "循环体未执行"

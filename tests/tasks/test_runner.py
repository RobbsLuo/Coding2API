"""后台任务装配与生命周期测试。

覆盖 TaskRunner 的启动、循环、异常隔离与停止，以及 provider 资源释放。
此前 QuotaProbeTask / CheckinTask / RefreshTask / RetentionTask 全部是死代码
（从未在应用里实例化），这些测试保证它们真的被接线。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from src.config import Settings
from src.provider.base import Quota
from src.stats.collector import StatsCollector
from src.tasks.checkin import CheckinTask
from src.tasks.quota_probe import QuotaProbeTask
from src.tasks.refresh import RefreshTask
from src.tasks.retention import RetentionTask
from src.tasks.runner import TaskRunner, build_runner
from tests.helpers.constants import SECRET


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
        checkin=CheckinTask(credentials, {"codebuddy": provider}),
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


async def test_runner_start_refreshes_expiring_credentials(repo):
    """启动首轮刷新必须立即执行。

    CodeArts 的一次性 refresh_token 现在只由 RefreshTask 轮转（额度探测不再
    保活）。循环体是先睡后跑，若重启时凭证已落在到期窗口而不能抢先刷，就会
    带病服务到下一轮（60 分钟），期间聊天 401 会被硬禁用。
    """
    import time as _time

    credentials, db = repo
    credentials.add(provider="codebuddy", credential_data={
        "bearer_token": "old", "refresh_token": "RT", "auth_source": "oauth",
        "expires_at": int(_time.time()) + 10})
    provider = TaskProvider()
    collector = StatsCollector(db)
    runner = TaskRunner(
        quota_probe=QuotaProbeTask(credentials, {"codebuddy": provider}, None),
        checkin=CheckinTask(credentials, {"codebuddy": provider}),
        refresh=RefreshTask(credentials, {"codebuddy": provider}, skew_seconds=3600),
        retention=RetentionTask(collector),
        quota_probe_minutes=60,
    )
    await runner.start()
    try:
        assert provider.refresh_calls == 1
        assert credentials.credential_data(
            credentials.candidates()[0].credential_id)["bearer_token"] == "NEW"
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
        checkin=CheckinTask(credentials, {"codebuddy": provider}),
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
    """未到签到时刻（due()=False）时 _sync_checkin 是 no-op。"""
    provider = StubProvider()
    runner, _collector = _runner(repo, provider)
    runner._checkin.due = lambda **kwargs: False   # 固定"未到点"，避免真实时钟依赖
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
    config = Settings(_env_file=None, APP_SECRET=SECRET, QUOTA_PROBE_MINUTES=15,
                      REFRESH_SKEW_HOURS=6, REFRESH_INTERVAL_MINUTES=45,
                      PACER_MIN_SECONDS=0, PACER_MAX_SECONDS=0)
    runner = build_runner(credentials, {"codebuddy": StubProvider()}, StatsCollector(db), config)
    assert runner._quota_interval == 15 * 60
    assert runner._quota_probe._pacer is not None
    assert runner._quota_probe._pacer.disabled is True
    assert runner._refresh.skew_seconds == 6 * 3600
    # 预刷新轮询周期从配置接线（默认 30min）；短寿命渠道靠它保证窗口不漏
    assert runner._refresh_interval == 45 * 60


def test_build_runner_clamps_intervals(repo):
    """过小的配置值必须被夹到安全下限，避免打爆上游。"""
    credentials, db = repo
    config = Settings(_env_file=None, APP_SECRET=SECRET, QUOTA_PROBE_MINUTES=0,
                      REFRESH_INTERVAL_MINUTES=0)
    runner = build_runner(credentials, {}, StatsCollector(db), config)
    assert runner._quota_interval == 60
    assert runner._refresh_interval >= 60
    assert runner._retention_interval >= 60
    assert runner._backup_interval >= 3600      # 备份下限 60 分钟


def test_build_runner_wires_backup_task(repo, tmp_path):
    """backup 传入时接线：周期从配置读，对象原样挂上。"""
    from src.tasks.backup import BackupTask

    credentials, db = repo
    config = Settings(_env_file=None, APP_SECRET=SECRET,
                      BACKUP_INTERVAL_MINUTES=120, BACKUP_KEEP=3)
    task = BackupTask(tmp_path / "x.sqlite3", keep=lambda: config.backup_keep)
    runner = build_runner(credentials, {}, StatsCollector(db), config, backup=task)
    assert runner._backup is task
    assert runner._backup_interval == 120 * 60
    assert runner._backup._keep() == 3


async def test_runner_backup_loop_runs_in_thread(repo, tmp_path):
    """备份循环体把同步磁盘 IO 放进线程池（不能卡事件循环）。"""
    from src.tasks.backup import BackupTask

    credentials, _db = repo
    provider = StubProvider()
    db_path = tmp_path / "coding2api.sqlite3"
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    runner = TaskRunner(
        quota_probe=QuotaProbeTask(credentials, {"codebuddy": provider}, None),
        checkin=CheckinTask(credentials, {"codebuddy": provider}),
        refresh=RefreshTask(credentials, {"codebuddy": provider}, skew_seconds=3600),
        retention=RetentionTask(StatsCollector(_db)),
        backup=BackupTask(db_path),
    )
    report = await runner._sync_backup()
    assert report["backup"].startswith("coding2api-")
    assert (tmp_path / "backups").is_dir()


async def test_runner_backup_without_task_raises(repo):
    """未装配 backup 时 _sync_backup 报错（不会被静默当 no-op）。"""
    runner, _ = _runner(repo, StubProvider())
    with pytest.raises(RuntimeError, match="backup task not configured"):
        await runner._sync_backup()


async def test_runner_starts_backup_loop_when_assembled(repo, tmp_path):
    """装配 backup 后必须真的多起一条循环。"""
    from src.tasks.backup import BackupTask

    credentials, _db = repo
    provider = StubProvider()
    runner = TaskRunner(
        quota_probe=QuotaProbeTask(credentials, {"codebuddy": provider}, None),
        checkin=CheckinTask(credentials, {"codebuddy": provider}),
        refresh=RefreshTask(credentials, {"codebuddy": provider}, skew_seconds=3600),
        retention=RetentionTask(StatsCollector(_db)),
        backup=BackupTask(tmp_path / "x.sqlite3"),
    )
    await runner.start()
    try:
        assert len(runner._tasks) == 5
    finally:
        await runner.stop()


def test_build_runner_wires_upstream_status(repo):
    """upstream_status 传入时接线：周期从配置读，开关与对象原样挂上。"""
    from src.tasks.upstream_status import UpstreamStatusTask
    from src.upstream_status import UpstreamStatusStore

    credentials, db = repo
    config = Settings(_env_file=None, APP_SECRET=SECRET,
                      UPSTREAM_STATUS_MINUTES=7, UPSTREAM_STATUS_ENABLED=False)
    task = UpstreamStatusTask(UpstreamStatusStore(),
                              enabled=lambda: config.upstream_status_enabled)
    runner = build_runner(credentials, {}, StatsCollector(db), config,
                          upstream_status=task)

    assert runner._upstream_status is task
    assert runner._upstream_status_interval == 7 * 60
    assert runner._task_enabled("upstream_status") is False
    assert "upstream_status" in {spec.key for spec in runner._loop_specs()}
    assert runner._interval_seconds("upstream_status") == 7 * 60


def test_upstream_status_interval_has_floor(repo):
    """周期下限 1 分钟（与热更项 minimum 对齐）。"""
    credentials, db = repo
    config = Settings(_env_file=None, APP_SECRET=SECRET, UPSTREAM_STATUS_MINUTES=0)
    runner = build_runner(credentials, {}, StatsCollector(db), config)

    assert runner._upstream_status_interval == 60


def test_upstream_status_not_assembled_by_default(repo):
    """老调用方不传 upstream_status 时整条循环不出现（保持原行为）。"""
    credentials, db = repo
    runner = build_runner(credentials, {}, StatsCollector(db),
                          Settings(_env_file=None, APP_SECRET=SECRET))

    assert runner._upstream_status is None
    assert "upstream_status" not in {spec.key for spec in runner._loop_specs()}
    assert "upstream_status" not in {item["key"] for item in runner.task_status()}


async def test_runner_upstream_status_without_task_raises(repo):
    """未装配时 _sync_upstream_status 报错（不会被静默当 no-op）。"""
    runner, _ = _runner(repo, StubProvider())
    with pytest.raises(RuntimeError, match="upstream status task not configured"):
        await runner._sync_upstream_status()


async def test_runner_upstream_status_loop_runs_task(repo):
    """循环体委托给任务对象；任务关闭时 no-op 不入账（页面显示「未运行」）。"""
    from src.tasks.upstream_status import UpstreamStatusTask
    from src.upstream_status import UpstreamStatusStore, spec_by_key

    credentials, _db = repo
    provider = StubProvider()
    flag = {"on": False}
    store = UpstreamStatusStore()
    spec = spec_by_key("kilo")
    assert spec is not None
    runner = TaskRunner(
        quota_probe=QuotaProbeTask(credentials, {"codebuddy": provider}, None),
        checkin=CheckinTask(credentials, {"codebuddy": provider}),
        refresh=RefreshTask(credentials, {"codebuddy": provider}, skew_seconds=3600),
        retention=RetentionTask(StatsCollector(_db)),
        upstream_status=UpstreamStatusTask(store, specs=(spec,),
                                           enabled=lambda: flag["on"]),
    )

    # 关着：返回 None，不覆盖运行态
    assert await runner._sync_upstream_status() is None
    assert runner.status.runs("upstream_status") == 0

    flag["on"] = True
    ok = await runner._guarded(runner._sync_upstream_status(), "上游状态页",
                               key="upstream_status")
    assert ok is True
    assert runner.status.runs("upstream_status") == 1
    assert runner.status.get("upstream_status").report == {
        "sources": 0, "abnormal": 0, "failed": 0}


async def test_runner_starts_upstream_status_loop_when_assembled(repo):
    """装配后必须真的多起一条循环。"""
    from src.tasks.upstream_status import UpstreamStatusTask
    from src.upstream_status import UpstreamStatusStore

    credentials, _db = repo
    provider = StubProvider()
    runner = TaskRunner(
        quota_probe=QuotaProbeTask(credentials, {"codebuddy": provider}, None),
        checkin=CheckinTask(credentials, {"codebuddy": provider}),
        refresh=RefreshTask(credentials, {"codebuddy": provider}, skew_seconds=3600),
        retention=RetentionTask(StatsCollector(_db)),
        upstream_status=UpstreamStatusTask(UpstreamStatusStore(), specs=()),
    )
    await runner.start()
    try:
        assert len(runner._tasks) == 5
        assert "upstream_status" in {item["key"] for item in runner.task_status()}
    finally:
        await runner.stop()


async def test_providers_release_http_pools_on_shutdown(tmp_path):
    """lifespan 关闭时必须释放 provider 的连接池。"""
    from src.provider.codebuddy.client import CodeBuddyProvider
    from src.provider.trae.client import TraeProvider

    codebuddy = CodeBuddyProvider()
    trae = TraeProvider()
    # 触发惰性客户端创建
    assert codebuddy.client._stream is not None
    assert trae.client._stream is not None

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


def test_main_reexports_match_webapp_definitions():
    """拆分后 main 的兼容别名必须指向真实定义（同一对象）。

    拆分 main.py 时最隐蔽的坑：把函数移到 src/webapp/ 但保留 main 的别名，
    测试里 `monkeypatch.setattr("src.main.x", ...)` 仍会“成功”——
    只是再也影响不到 webapp 内部的调用，测试静默地测了空气。
    这里断言别名与真实定义同一，patch 目标才有意义。
    """
    from src import main
    from src.webapp import limits, static

    assert main._frontend_dist is static.frontend_dist
    assert main._api_not_found is static.api_not_found
    assert main._API_PREFIXES is static._API_PREFIXES
    assert main.LOGIN_BODY_LIMIT == limits.LOGIN_BODY_LIMIT
    assert main.DEFAULT_BODY_LIMIT == limits.DEFAULT_BODY_LIMIT
    assert main._body_limit("/api/auth/login") == limits.LOGIN_BODY_LIMIT
    assert main._body_limit("/v1/chat/completions") == limits.DEFAULT_BODY_LIMIT
    assert main._host_allowed is not None


def test_webapp_static_patch_targets_are_the_used_globals():
    """SPA 路由必须读 webapp.static 的全局，patch 那里才生效。"""
    from src.webapp import static

    # 真实定义处必须同时拥有函数与它依赖的两个常量
    for name in ("frontend_dist", "api_not_found", "register_spa_routes"):
        assert callable(getattr(static, name))
    for name in ("_PROJECT_ROOT", "_CONTAINER_DIST"):
        assert getattr(static, name) is not None


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
        from src.tasks import TaskReport

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
    monkeypatch.setenv("APP_SECRET", SECRET)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "9123")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")

    main.run()

    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9123
    assert captured["log_level"] == "warning"
    assert captured["app"] is not None
    # uvicorn 自带 access log 与 RequestLogMiddleware 重复（且无时间戳 /
    # request_id），必须关掉
    assert captured["access_log"] is False
    # 停机排空：不给 timeout_graceful_shutdown 就是无限等（长流卡住直到 SIGKILL）
    assert captured["timeout_graceful_shutdown"] == main.GRACEFUL_SHUTDOWN_SECONDS


def test_run_releases_instance_lock_after_uvicorn_returns(monkeypatch, tmp_path):
    """uvicorn 退出后必须释放锁，否则重启永远起不来。"""
    import sys
    import types

    from src import main
    from src.instance_lock import LOCK_NAME, InstanceLock

    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.run = lambda app, **kwargs: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setenv("APP_SECRET", SECRET)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    main.run()

    # 能再次取到锁 = 上次确实释放了
    lock = InstanceLock(tmp_path)
    lock.acquire()
    lock.release()
    assert (tmp_path / LOCK_NAME).is_file()


def test_run_aborts_when_another_instance_holds_lock(monkeypatch, tmp_path, capsys):
    """已有实例持锁时 run() 干净返回，绝不进 build_app（避免双写库）。

    这是崩溃循环的止损点：若这里以非零退出，launchd（SuccessfulExit=false）/
    systemd（Restart=on-failure）会把「另一个实例在跑」当成崩溃每 10s 重拉，
    实测刷过 2377 条 `[Errno 48]`。所以必须退出码 0。
    """
    import sys
    import types

    from src import main
    from src.instance_lock import InstanceLock

    started: list[int] = []

    def fake_run(app, **kwargs):
        started.append(1)

    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.run = fake_run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setenv("APP_SECRET", SECRET)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    holder = InstanceLock(tmp_path)
    holder.acquire()
    try:
        main.run()          # 不得抛 SystemExit（非零退出会触发重拉循环）
    finally:
        holder.release()

    assert started == [], "取锁失败时不得启动 uvicorn"
    err = capsys.readouterr().err
    assert "启动中止" in err
    assert "另一个实例" in err


def test_run_releases_lock_when_build_app_raises(monkeypatch, tmp_path):
    """build_app 抛异常也要释放锁（finally 覆盖，不是只在正常路径）。"""
    import sys
    import types

    from src import main
    from src.instance_lock import InstanceLock

    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.run = lambda app, **kwargs: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setenv("APP_SECRET", SECRET)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    def boom(_config):
        raise RuntimeError("组装失败")

    monkeypatch.setattr(main, "build_app", boom)

    with pytest.raises(RuntimeError):
        main.run()

    lock = InstanceLock(tmp_path)
    lock.acquire()
    lock.release()


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


async def test_loop_interval_getter_failure_falls_back(repo, caplog):
    """间隔取值器抛错不能终止循环：记一条 warning 后退回 60 秒继续（runner 230-235）。"""
    runner, _collector = _runner(repo, StubProvider())

    def broken_interval() -> float:
        raise ValueError("配置读取失败")

    async def runner_body():
        return None

    with caplog.at_level("WARNING"):
        task = asyncio.create_task(runner._loop("坏间隔", runner_body, broken_interval))
        await asyncio.sleep(0.02)
        # 循环没被异常终止：仍挂在 sleep 里（退回的 60 秒），而非已结束
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert any("读取间隔失败" in r.getMessage() for r in caplog.records)


async def test_checkin_task_soft_failure_retries_same_day(repo, monkeypatch):
    """claim 软失败（ok=False）→ 计入 failed 且当日不封账，下轮还能重试。"""
    import time as _time

    from src.provider.base import CheckinResult

    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    provider = StubProvider()

    async def failing_checkin(_data):
        return CheckinResult(ok=False, code=9074, message="当前参与用户太多，请稍后再试")

    provider.checkin = failing_checkin
    runner, _collector = _runner(repo, provider)
    task = runner._checkin
    task._done_scopes.clear()
    now = _time.localtime()
    report = await task.run_once(now=now)
    assert report.failed == 1 and report.succeeded == 0
    assert task._day_key(now) not in task._done_scopes   # 未封账 → 下轮重试

    # 下一轮成功 → 封账
    async def ok_checkin(_data):
        return CheckinResult(ok=True, credit=200, code=0)

    provider.checkin = ok_checkin
    report2 = await task.run_once(now=now)
    assert report2.succeeded == 1 and report2.failed == 0
    assert any(key.startswith(f"{task._day_key(now)}:") for key in task._done_scopes)


async def test_checkin_seal_until_shortens_day_and_prunes_stale_days(repo):
    """渠道给了 `seal_until`（签到周期 ≠ 自然日，如 Qoder 每日 10:00 换窗口）：
    封账只到那一刻，到点后同一天仍会再跑一轮；跨天的封账记录被清掉。"""
    from src.provider.base import CheckinResult

    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    provider = StubProvider()
    calls: list[int] = []

    async def sealed_checkin(_data):
        # 封到 10:05（比当日结束早得多）：模拟 Qoder 的签到窗口结束时刻
        calls.append(1)
        return CheckinResult(ok=True, already_checked_in=True,
                             message="今日已签到", seal_until=window_end)

    midnight = time.struct_time((2026, 10, 8, 0, 5, 0, 0, 1, -1))
    window_end = int(time.mktime((2026, 10, 8, 10, 5, 0, 0, 1, -1)))
    provider.checkin = sealed_checkin
    task = CheckinTask(credentials, {"codebuddy": provider})

    assert (await task.run_once(now=midnight)).succeeded == 1
    done_key = f"{task._day_key(midnight)}:scope"
    assert task._done_scopes[done_key] == window_end
    # 封账期内不再调上游
    assert (await task.run_once(now=midnight)).skipped == 1
    assert len(calls) == 1
    # 窗口结束（10:05）后：同一天自动再跑一轮，而不是等人工
    after = time.struct_time((2026, 10, 8, 10, 15, 0, 0, 1, -1))
    assert (await task.run_once(now=after)).succeeded == 1
    assert len(calls) == 2
    # 跨天：昨天的封账记录被清掉（done_key 以日期开头，否则长驻进程无上限增长）
    task._done_scopes["2026-10-07:scope"] = window_end
    await task.run_once(now=after)
    assert "2026-10-07:scope" not in task._done_scopes


async def test_checkin_seal_expired_entry_is_not_treated_as_sealed(repo):
    """封账到期后条目仍在 dict 里（当天不 prune），但不再算封账——否则
    `seal_until` 一过期该作用域会被永久跳过。"""
    from src.provider.base import CheckinResult

    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    provider = StubProvider()
    calls: list[int] = []

    async def checkin(_data):
        calls.append(1)
        return CheckinResult(ok=True, credit=1, code=0,
                             seal_until=int(time.mktime((2026, 10, 8, 0, 10, 0, 0, 1, -1))))

    provider.checkin = checkin
    task = CheckinTask(credentials, {"codebuddy": provider})
    before = time.struct_time((2026, 10, 8, 0, 5, 0, 0, 1, -1))
    await task.run_once(now=before)
    after = time.struct_time((2026, 10, 8, 0, 20, 0, 0, 1, -1))
    assert (await task.run_once(now=after)).succeeded == 1
    assert len(calls) == 2


def test_checkin_day_end_is_next_local_midnight():
    """默认封账截止 = 当日 24:00（渠道不给 seal_until 时的兜底）。"""
    task = CheckinTask.__new__(CheckinTask)
    now = time.struct_time((2026, 10, 8, 23, 30, 0, 0, 1, -1))
    assert task._day_end(now) == int(time.mktime((2026, 10, 9, 0, 0, 0, 0, 1, -1)))


class RecordingPacer:
    """记录 wait_turn 调用次数的 Pacer 替身。"""

    def __init__(self) -> None:
        self.turns = 0

    async def wait_turn(self) -> None:
        self.turns += 1


class TaskProvider(StubProvider):
    """可成功签到/刷新的最小 provider 桩。"""

    def __init__(self) -> None:
        self.refresh_calls = 0
        self.checkin_calls = 0

    async def checkin(self, _data):
        from src.provider.base import CheckinResult

        self.checkin_calls += 1
        return CheckinResult(ok=True)

    async def refresh(self, data):
        self.refresh_calls += 1
        return {**data, "bearer_token": "NEW"}


async def test_refresh_and_checkin_pace_upstream_calls(repo):
    """refresh/checkin 与 quota_probe/growth 共用 Pacer：每次触上游前 wait_turn，
    不绕开全局频率风控对策。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={
        "bearer_token": "old", "refresh_token": "RT", "auth_source": "oauth",
        "expires_at": 1_000_100})
    pacer = RecordingPacer()
    provider = TaskProvider()
    refresh = RefreshTask(credentials, {"codebuddy": provider}, skew_seconds=3600,
                          now=lambda: 1_000_000, pacer=pacer)
    checkin = CheckinTask(credentials, {"codebuddy": provider}, pacer=pacer)

    refresh_report = await refresh.run_once()
    checkin_report = await checkin.run_once()

    assert refresh_report.succeeded == 1 and provider.refresh_calls == 1
    assert checkin_report.succeeded == 1 and provider.checkin_calls == 1
    assert pacer.turns == 2


async def test_sync_helpers_raise_when_optional_tasks_unwired(repo):
    """可选任务未装配时同步入口显式报错（不依赖 assert，-O 下也生效）。

    `_sync_growth/_sync_activity/_sync_alert` 只在对应任务装配后才会被调度；
    未装配时直接调用（测试/误接线）必须抛 RuntimeError，而不是把 None 当对象用。
    """
    runner, _ = _runner(repo, StubProvider())
    for sync in (runner._sync_growth, runner._sync_activity, runner._sync_alert):
        with pytest.raises(RuntimeError, match="not configured"):
            await sync()


class _Store:
    """最小 SettingsStore：只实现读写，避免测试依赖数据库。"""

    def __init__(self) -> None:
        self.rows: dict[str, str] = {}

    def load(self) -> dict[str, str]:
        return dict(self.rows)

    def set(self, key: str, value: str, now: int | None = None) -> None:
        self.rows[key] = value

    def delete(self, key: str) -> None:
        self.rows.pop(key, None)


def _runtime(**overrides):
    from src.runtime_settings import RuntimeSettings

    base = Settings(_env_file=None, APP_SECRET=SECRET, **overrides)
    store = _Store()
    return RuntimeSettings(base, store), store


def test_checkin_interval_follows_schedule_config(repo):
    """签到时点是否配置决定醒转周期：留空 10 分钟，配了则 1 分钟。

    抖动=0 时 due() 窗口只有「恰好那一分钟」，10 分钟周期下醒来时刻几乎必然
    错开，结果是一天一次都不签——比旧的全天轮询更糟。
    """
    credentials, db = repo
    config, store = _runtime()
    runner = build_runner(credentials, {}, StatsCollector(db), config)
    assert runner._interval_seconds("checkin") == 600.0

    store.set("checkin_times", "09:37")
    config.reload()
    assert runner._checkin.has_times() is True
    assert runner._interval_seconds("checkin") == 60.0

    # 全是坏段等同留空：退回旧行为，不让一条笔误锁死签到
    store.set("checkin_times", "25:00,bad")
    config.reload()
    assert runner._checkin.has_times() is False
    assert runner._interval_seconds("checkin") == 600.0


def test_build_runner_wires_checkin_schedule(repo):
    """时点与抖动从热更项接线（零参 callable，改动立刻生效）。"""
    credentials, db = repo
    config, store = _runtime(CHECKIN_TIMES="09:37,21:10", CHECKIN_JITTER_MINUTES=5)
    runner = build_runner(credentials, {}, StatsCollector(db), config)

    assert runner._checkin._times() == "09:37,21:10"
    assert runner._checkin._jitter() == 5
    store.set("checkin_times", "")
    config.reload()
    assert runner._checkin._times() == ""


def test_build_runner_wires_model_probe(repo):
    """model_probe 传入时接线：周期从配置读，循环与间隔都挂上。"""
    from src.db.repo import ModelProbeRepository
    from src.tasks.model_probe import ModelProbeTask

    credentials, db = repo
    config = Settings(_env_file=None, APP_SECRET=SECRET, MODEL_PROBE_MINUTES=90)
    task = ModelProbeTask(credentials, {}, ModelProbeRepository(db))
    runner = build_runner(credentials, {}, StatsCollector(db), config,
                          model_probe=task,
                          model_probe_minutes=lambda: config.model_probe_minutes)

    assert runner._model_probe is task
    assert runner._model_probe_interval == 90 * 60
    assert "model_probe" in {spec.key for spec in runner._loop_specs()}
    assert runner._interval_seconds("model_probe") == 90 * 60
    assert "model_probe" in {item["key"] for item in runner.task_status()}


def test_model_probe_interval_floor(repo):
    """周期下限 1 分钟：填 0（关闭）时循环仍在，只是每轮判成 no-op。"""
    credentials, db = repo
    runner = build_runner(credentials, {}, StatsCollector(db),
                          Settings(_env_file=None, APP_SECRET=SECRET,
                                   MODEL_PROBE_MINUTES=0))

    assert runner._model_probe_interval == 60


def test_model_probe_not_assembled_by_default(repo):
    """老调用方不传 model_probe 时整条循环不出现（保持原行为）。"""
    credentials, db = repo
    runner = build_runner(credentials, {}, StatsCollector(db),
                          Settings(_env_file=None, APP_SECRET=SECRET))

    assert runner._model_probe is None
    assert "model_probe" not in {spec.key for spec in runner._loop_specs()}
    assert "model_probe" not in {item["key"] for item in runner.task_status()}


async def test_runner_model_probe_without_task_raises(repo):
    """未装配时 _sync_model_probe 报错（不会被静默当 no-op）。"""
    runner, _ = _runner(repo, StubProvider())
    with pytest.raises(RuntimeError, match="model probe task not configured"):
        await runner._sync_model_probe()


async def test_runner_model_probe_disabled_is_noop(repo):
    """周期 0（默认）= 关闭：返回 None 且不记运行态（页面显示「未运行」）。"""
    from src.db.repo import ModelProbeRepository
    from src.tasks.model_probe import ModelProbeTask

    credentials, db = repo
    provider = StubProvider()
    runner = TaskRunner(
        quota_probe=QuotaProbeTask(credentials, {"codebuddy": provider}, None),
        checkin=CheckinTask(credentials, {"codebuddy": provider}),
        refresh=RefreshTask(credentials, {"codebuddy": provider}, skew_seconds=3600),
        retention=RetentionTask(StatsCollector(db)),
        model_probe=ModelProbeTask(credentials, {"codebuddy": provider},
                                   ModelProbeRepository(db)),
        model_probe_minutes=lambda: 0,
    )

    assert await runner._sync_model_probe() is None
    assert runner.status.runs("model_probe") == 0


async def test_runner_model_probe_runs_task_when_enabled(repo):
    """周期 > 0 时委托给任务对象（无负缓存行 → 空报告也算跑过一轮）。"""
    from src.db.repo import ModelProbeRepository
    from src.tasks.model_probe import ModelProbeTask

    credentials, db = repo
    provider = StubProvider()
    runner = TaskRunner(
        quota_probe=QuotaProbeTask(credentials, {"codebuddy": provider}, None),
        checkin=CheckinTask(credentials, {"codebuddy": provider}),
        refresh=RefreshTask(credentials, {"codebuddy": provider}, skew_seconds=3600),
        retention=RetentionTask(StatsCollector(db)),
        model_probe=ModelProbeTask(credentials, {"codebuddy": provider},
                                   ModelProbeRepository(db)),
        model_probe_minutes=lambda: 60,
    )

    ok = await runner._guarded(runner._sync_model_probe(), "模型探活",
                               key="model_probe")

    assert ok is True
    assert runner.status.runs("model_probe") == 1
    assert runner.status.get("model_probe").report == {
        "attempted": 0, "succeeded": 0, "failed": 0, "skipped": 0}

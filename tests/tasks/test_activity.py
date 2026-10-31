"""活跃上报后台任务（去重、失败重试、时段窗口与 runner 接线）。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.config import Settings
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository, GrowthRepository
from src.provider.codebuddy.activity import ActivityResult
from src.tasks.activity import ActivityTask
from tests.helpers.constants import SECRET


class _FakeProvider:
    id = "codebuddy"

    def __init__(self, *, ok: bool = True, scope: str = "s1") -> None:
        self._ok, self._scope = ok, scope
        self.calls = 0

    def activity_scope(self, _data: dict) -> str:
        return self._scope

    async def activity(self, _data: dict):
        self.calls += 1
        return ActivityResult(ok=self._ok, message="" if self._ok else "失败原因")

class _NoActivityProvider:
    id = "trae"

class _DispatchProvider:
    """按 token 分派失败模式（验证两条失败分支互不干扰）。"""

    id = "codebuddy"

    def activity_scope(self, data: dict) -> str:
        return data["bearer_token"]

    async def activity(self, data: dict):
        if data["bearer_token"] == "a":          # 第一个：正常返回 ok=False
            return ActivityResult(ok=False, message="失败原因")
        raise RuntimeError("boom")               # 第二个：抛异常

@pytest.fixture
def activity_repo(tmp_path):
    db = Database(tmp_path / "activity.sqlite3")
    apply_schema(db.connect())
    return CredentialRepository(db, CredentialCipher(SECRET))

def _add(repo, *, provider="codebuddy", token="t", disabled=False, enabled=True):
    credential_id = repo.add(provider=provider,
                             credential_data={"bearer_token": token})
    if not enabled:
        repo.set_enabled(credential_id, False)
    if disabled:
        from src.engine.scheduler import Candidate, Scheduler
        from src.provider.base import ErrKind

        repo.save_error(credential_id, Scheduler().note_error(
            Candidate(credential_id=credential_id, provider=provider),
            ErrKind.DEAD, 0))
    return credential_id

def _cn(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 21, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai"))

def _only_id(repo) -> str:
    return repo.list_all()[0]["id"]

class _NullCollector:
    """RetentionTask 需要的最小 collector 接口（本用例不触发清理）。"""

    def purge_before(self, _cutoff):  # pragma: no cover - 本用例不调用
        return 0

async def test_activity_task_reports_once_and_closes_day(activity_repo):
    _add(activity_repo)
    provider = _FakeProvider()
    task = ActivityTask(activity_repo, {"codebuddy": provider}, hour=10)
    first = await task.run_once()
    assert (first.attempted, first.succeeded) == (1, 1)
    # 当日再跑：已封账，不再调上游
    second = await task.run_once()
    assert second.attempted == 0 and second.skipped == 1
    assert provider.calls == 1

async def test_activity_task_skips_unsupported_and_disabled(activity_repo):
    _add(activity_repo, token="a")
    _add(activity_repo, token="b", disabled=True)
    _add(activity_repo, provider="trae", token="c")       # 无 activity 能力
    provider = _FakeProvider()
    task = ActivityTask(activity_repo,
                        {"codebuddy": provider, "trae": _NoActivityProvider()}, hour=10)
    report = await task.run_once()
    assert report.succeeded == 1
    assert report.skipped == 2
    assert provider.calls == 1

async def test_activity_task_skips_soft_disabled_credentials(activity_repo):
    """`enabled=0`（用户软暂停）不参与活跃上报：暂停不能只停调度。

    与 checkin / refresh / quota_probe / growth 同口径。此前只判 `disabled`，
    于是「暂停」的号仍被拿去打卡。
    """
    _add(activity_repo, token="a")
    _add(activity_repo, token="b", enabled=False)
    provider = _FakeProvider()
    task = ActivityTask(activity_repo, {"codebuddy": provider}, hour=10)
    report = await task.run_once()
    assert report.attempted == 1 and report.skipped == 1
    assert provider.calls == 1


async def test_activity_task_skips_missing_provider_and_data():
    """provider 不认识该渠道、或凭证数据缺失 → 跳过（不调上游）。"""
    class _Repo:
        def candidates(self):
            from src.engine.scheduler import Candidate

            return [Candidate(credential_id="c1", provider="unknown"),
                    Candidate(credential_id="c2", provider="codebuddy")]

        def credential_data(self, credential_id):
            return None

    provider = _FakeProvider()
    task = ActivityTask(_Repo(), {"codebuddy": provider}, hour=10)
    report = await task.run_once()
    assert report.skipped == 2 and provider.calls == 0

async def test_activity_task_dedupes_scope_within_run(activity_repo):
    """同账号多凭证共享一次（同一轮 scope 去重；失败不封账故走到 seen 分支）。"""
    _add(activity_repo, token="a")
    _add(activity_repo, token="b")
    provider = _FakeProvider(ok=False, scope="same")
    task = ActivityTask(activity_repo, {"codebuddy": provider}, hour=10)
    report = await task.run_once()
    assert (report.attempted, report.failed, report.skipped) == (1, 1, 1)
    assert provider.calls == 1

async def test_activity_task_empty_scope_falls_back_to_credential_id(activity_repo):
    """身份未知（空 scope）时绝不能共享，否则第二个账号被永久跳过。"""
    _add(activity_repo, token="a")
    _add(activity_repo, token="b")
    provider = _FakeProvider(scope="")
    task = ActivityTask(activity_repo, {"codebuddy": provider}, hour=10)
    report = await task.run_once()
    assert report.attempted == 2 and provider.calls == 2

async def test_activity_task_failure_retries_and_does_not_abort(activity_repo):
    """ok=False 与抛异常都算失败、不封账（下轮重试），且互不打断。"""
    _add(activity_repo, token="a")
    _add(activity_repo, token="b")
    task = ActivityTask(activity_repo, {"codebuddy": _DispatchProvider()}, hour=10)
    report = await task.run_once()
    assert report.attempted == 2 and report.failed == 2
    again = await task.run_once()
    assert again.attempted == 2

async def test_activity_task_records_growth_event_on_success(activity_repo):
    _add(activity_repo)
    events = GrowthRepository(activity_repo._db)  # noqa: SLF001
    provider = _FakeProvider()
    task = ActivityTask(activity_repo, {"codebuddy": provider}, events=events, hour=10)
    await task.run_once()
    latest = events.latest_for(_only_id(activity_repo))
    assert latest is not None and latest["ok"] == 1
    assert "活跃上报" in latest["report"]

def test_activity_task_due_only_in_configured_hour():
    task = ActivityTask.__new__(ActivityTask)
    task._hour = 10
    task._now = _cn(10, 5)
    assert task.due() is True
    task._now = _cn(9, 59)
    assert task.due() is False
    task._now = _cn(11, 0)
    assert task.due() is False

def test_activity_task_day_key_is_cn_local():
    task = ActivityTask.__new__(ActivityTask)
    task._now = _cn(0, 30)
    assert task._day_key() == "2026-09-21"

async def test_activity_task_prunes_stale_done_scopes(activity_repo):
    """跨天后清掉前一天的封账记录：长驻进程否则每天每账号各攒一条（无上限）。"""
    _add(activity_repo, token="a")
    task = ActivityTask(activity_repo, {"codebuddy": _FakeProvider()},
                        hour=10, now=_cn(10, 5))
    task._done_scopes = {"2026-09-20:s1", "2026-09-21:s1", "2026-09-19:s1"}

    await task.run_once()

    # 非当天两条被清掉；当天那条保留（本轮已成功会重新写入同键）
    assert task._done_scopes == {"2026-09-21:s1"}

def test_build_runner_activity_toggle(tmp_path):
    """活跃上报恒建对象，开关只决定跑不跑（开关可热更）。

    以前是「关闭就不装配」，那样管理台把开关打开后必须重启才生效；现在
    对象总在，`_sync_activity` 每轮现读开关。
    """
    from src.tasks.runner import build_runner

    db = Database(str(tmp_path / "runner.sqlite3"))
    apply_schema(db.connect())
    repo = CredentialRepository(db, CredentialCipher(SECRET))

    class _Collector:
        pass

    off = build_runner(repo, {}, _Collector(), Settings(
        _env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path)))
    assert off._activity is not None            # noqa: SLF001 - 恒建对象
    assert off._activity_enabled() is False     # noqa: SLF001 - 默认关闭
    on = build_runner(repo, {}, _Collector(), Settings(
        _env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
        ACTIVITY_REPORT_ENABLED="true", ACTIVITY_REPORT_HOUR="7"))
    assert on._activity is not None             # noqa: SLF001
    assert on._activity_enabled() is True       # noqa: SLF001
    assert on._activity._hour == 7              # noqa: SLF001

async def test_runner_sync_activity_skips_when_disabled(activity_repo):
    """开关关闭时 _sync_activity 是 no-op，即使正处在配置小时窗口内。"""
    from src.tasks.activity import ActivityTask as _ActivityTask
    from src.tasks.checkin import CheckinTask
    from src.tasks.quota_probe import QuotaProbeTask
    from src.tasks.refresh import RefreshTask
    from src.tasks.retention import RetentionTask
    from src.tasks.runner import TaskRunner

    _add(activity_repo)
    provider = _FakeProvider()
    activity = _ActivityTask(activity_repo, {"codebuddy": provider}, hour=10)
    activity._now = _cn(10, 5)                  # 窗口内
    runner = TaskRunner(
        quota_probe=QuotaProbeTask(activity_repo, {"codebuddy": provider}, None),
        checkin=CheckinTask(activity_repo, {"codebuddy": provider}),
        activity=activity,
        refresh=RefreshTask(activity_repo, {"codebuddy": provider}, skew_seconds=3600),
        retention=RetentionTask(_NullCollector()),
        activity_enabled=lambda: False,
    )
    assert await runner._sync_activity() is None      # noqa: SLF001 - 开关关
    assert provider.calls == 0

async def test_runner_sync_activity_respects_hour_window(activity_repo):
    """_sync_activity 只在配置小时窗口内真的跑一轮，窗口外是 no-op。"""
    from src.tasks.activity import ActivityTask as _ActivityTask
    from src.tasks.checkin import CheckinTask
    from src.tasks.quota_probe import QuotaProbeTask
    from src.tasks.refresh import RefreshTask
    from src.tasks.retention import RetentionTask
    from src.tasks.runner import TaskRunner

    _add(activity_repo)
    provider = _FakeProvider()
    activity = _ActivityTask(activity_repo, {"codebuddy": provider}, hour=10)
    activity._now = _cn(9, 0)              # 窗口外
    runner = TaskRunner(
        quota_probe=QuotaProbeTask(activity_repo, {"codebuddy": provider}, None),
        checkin=CheckinTask(activity_repo, {"codebuddy": provider}),
        activity=activity,
        refresh=RefreshTask(activity_repo, {"codebuddy": provider}, skew_seconds=3600),
        retention=RetentionTask(_NullCollector()),
    )
    assert await runner._sync_activity() is None      # noqa: SLF001 - 窗口外
    assert provider.calls == 0
    activity._now = _cn(10, 0)                        # 窗口内
    await runner._sync_activity()                     # noqa: SLF001
    assert provider.calls == 1

async def test_runner_start_includes_activity_loop(activity_repo):
    """装配了 activity 时，start 要多起一条「活跃上报」循环。"""
    from src.tasks.activity import ActivityTask as _ActivityTask
    from src.tasks.checkin import CheckinTask
    from src.tasks.quota_probe import QuotaProbeTask
    from src.tasks.refresh import RefreshTask
    from src.tasks.retention import RetentionTask
    from src.tasks.runner import TaskRunner

    provider = _FakeProvider()
    runner = TaskRunner(
        quota_probe=QuotaProbeTask(activity_repo, {"codebuddy": provider}, None),
        checkin=CheckinTask(activity_repo, {"codebuddy": provider}),
        activity=_ActivityTask(activity_repo, {"codebuddy": provider}, hour=10),
        refresh=RefreshTask(activity_repo, {"codebuddy": provider}, skew_seconds=3600),
        retention=RetentionTask(_NullCollector()),
    )
    await runner.start()
    try:
        # 额度/刷新/清理/签到/活跃上报
        assert len(runner._tasks) == 5      # noqa: SLF001
    finally:
        await runner.stop()

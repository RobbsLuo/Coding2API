"""后台任务运行态（B4）：进程内 last-run 记录 + /api/tasks 端点。

这一组守着两条容易退化的语义：
1. **只记真实执行**：签到未到点 / 活跃上报未启用是 no-op，不能覆盖上一次
   结果——否则页面显示「签到刚刚跑过」，而当天其实一次都没签。
2. **周期与开关是当前生效值**：改完热更立刻反映在任务卡片上，不是装配快照。
"""

from __future__ import annotations

import pytest

from src.auth.session import create_session_token
from src.config import Settings
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository
from src.provider.base import Quota
from src.stats.collector import StatsCollector
from src.tasks import TaskReport
from src.tasks.activity import ActivityTask
from src.tasks.checkin import CheckinTask
from src.tasks.quota_probe import QuotaProbeTask
from src.tasks.refresh import RefreshTask
from src.tasks.retention import RetentionTask
from src.tasks.runner import TaskRunner, build_runner
from src.tasks.status import TASK_BY_KEY, TASK_SPECS, TaskStatusStore
from tests.conftest import SECRET


@pytest.fixture()
def repo(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    yield CredentialRepository(db, CredentialCipher(SECRET)), db
    db.close()


class StubProvider:
    id = "codebuddy"

    async def probe_quota(self, _data):
        return Quota(remaining=5, total=10, probed_at=1)

    async def checkin(self, _data):  # pragma: no cover - 不在本组触发
        raise AssertionError("不应触发签到")

    def checkin_scope(self, _data):  # pragma: no cover
        return "scope"

    def credential_from(self, data):  # pragma: no cover
        from src.provider.codebuddy.credential import CodeBuddyCredential

        return CodeBuddyCredential.from_dict(data)

    async def refresh(self, data):  # pragma: no cover
        return data


def _runner(repo_tuple, provider, *, status=None, quota_minutes=60):
    credentials, db = repo_tuple
    return TaskRunner(
        quota_probe=QuotaProbeTask(credentials, {"codebuddy": provider}, None),
        checkin=CheckinTask(credentials, {"codebuddy": provider}),
        refresh=RefreshTask(credentials, {"codebuddy": provider}, skew_seconds=3600),
        retention=RetentionTask(StatsCollector(db)),
        quota_probe_minutes=quota_minutes,
        status=status,
    )


async def _noop() -> None:
    return None


# ------------------------------------------------------------ TaskStatusStore

def test_store_records_only_last_run_and_counts():
    store = TaskStatusStore(now=lambda: 111.0)
    store.record("quota_probe", started_at=100.0, ok=True,
                 report={"attempted": 1}, error=None)
    store.record("quota_probe", started_at=105.0, ok=False,
                 report=None, error="boom")

    run = store.get("quota_probe")
    assert run is not None
    assert run.started_at == 105.0 and run.finished_at == 111.0
    assert run.ok is False and run.error == "boom" and run.report is None
    assert store.runs("quota_probe") == 2          # 只涨不重置
    assert store.get("never") is None and store.runs("never") == 0


def test_store_defaults_to_wall_clock():
    """未注入时钟时用真实时间（否则页面的「距今多久」会退化成常量）。"""
    store = TaskStatusStore()
    store.record("quota_probe", started_at=1.0, ok=True, report=None, error=None)
    assert store.get("quota_probe").finished_at > 1_000_000_000


# --------------------------------------------------------- 记录语义（TaskRunner）

async def test_guarded_without_key_does_not_touch_status(repo):
    """未标 key 的调用不写运行态（一次性/测试路径）。"""
    runner = _runner(repo, StubProvider())
    assert await runner._guarded(_noop(), "无 key") is True
    assert runner.status.get("quota_probe") is None
    assert runner.status.runs("quota_probe") == 0


async def test_guarded_records_real_run_and_skips_noop(repo):
    """核心语义：真实执行入账，返回 None 的 no-op 不覆盖上一次结果。"""
    store = TaskStatusStore(now=lambda: 1000.0)
    runner = _runner(repo, StubProvider(), status=store)

    async def real():
        return TaskReport(attempted=2, succeeded=1, failed=1)

    assert await runner._guarded(real(), "额度探测", key="quota_probe") is True
    first = store.get("quota_probe")
    assert first is not None and first.ok is True
    assert first.report == {"attempted": 2, "succeeded": 1, "failed": 1, "skipped": 0}
    assert store.runs("quota_probe") == 1

    # 第二轮是 no-op：不覆盖、不计数
    assert await runner._guarded(_noop(), "额度探测", key="quota_probe") is True
    assert store.get("quota_probe") == first
    assert store.runs("quota_probe") == 1


async def test_guarded_records_failure_and_dict_report(repo):
    """失败要记 error；清理任务返回 dict 也要能落成报告。"""
    store = TaskStatusStore(now=lambda: 5.0)
    runner = _runner(repo, StubProvider(), status=store)

    async def boom():
        raise RuntimeError("上游 500")

    assert await runner._guarded(boom(), "明细清理", key="retention") is False
    run = store.get("retention")
    assert run is not None and run.ok is False and run.error == "上游 500"

    async def dict_report():
        return {"rolled_up": 3, "purged": 1}

    assert await runner._guarded(dict_report(), "明细清理", key="retention") is True
    assert store.get("retention").report == {"rolled_up": 3, "purged": 1}


async def test_guarded_falls_back_for_opaque_result(repo):
    """既非 TaskReport 也非 dict 的返回值要兜成可 JSON 化的摘要。"""
    store = TaskStatusStore()
    runner = _runner(repo, StubProvider(), status=store)

    async def opaque():
        return object()

    await runner._guarded(opaque(), "额度探测", key="quota_probe")
    report = store.get("quota_probe").report
    assert report is not None and report["result"].startswith("<object object")


def test_as_report_prefers_dict_and_falls_back():
    """`as_dict()` 存在就用它；返回非 dict 时继续走 dict / repr 兜底。"""
    from src.tasks.runner import _as_report

    assert _as_report(TaskReport(attempted=1)) == {
        "attempted": 1, "succeeded": 0, "failed": 0, "skipped": 0}
    assert _as_report({"custom": 1}) == {"custom": 1}

    class Lying:
        def as_dict(self):
            return ["not", "a", "dict"]

    assert _as_report(Lying())["result"].startswith("<")


# ------------------------------------------------------------ task_status 快照

def test_task_status_lists_assembled_tasks_with_intervals(repo):
    runner = _runner(repo, StubProvider())
    status = runner.task_status()

    # 未装配 growth / activity（构造时没传）→ 不出现在清单里
    assert [item["key"] for item in status] == [
        "quota_probe", "refresh", "checkin", "retention"]
    by_key = {item["key"]: item for item in status}
    assert by_key["quota_probe"]["interval_seconds"] == 3600.0
    assert by_key["checkin"]["interval_seconds"] == 600.0
    assert by_key["retention"]["interval_seconds"] == 300.0
    assert by_key["quota_probe"]["enabled"] is True
    assert by_key["quota_probe"]["last_started_at"] is None
    assert by_key["quota_probe"]["last_ok"] is None
    assert by_key["quota_probe"]["last_report"] is None
    assert by_key["quota_probe"]["last_error"] is None
    assert by_key["quota_probe"]["runs"] == 0


def test_task_status_reflects_hot_interval_changes(repo):
    """周期必须现读：热更后同一 runner 的 task_status 立刻反映新值。"""
    current = {"minutes": 60}

    def minutes() -> int:
        return current["minutes"]

    credentials, db = repo
    runner = TaskRunner(
        quota_probe=QuotaProbeTask(credentials, {"codebuddy": StubProvider()}, None),
        checkin=CheckinTask(credentials, {"codebuddy": StubProvider()}),
        refresh=RefreshTask(credentials, {"codebuddy": StubProvider()}, skew_seconds=1),
        retention=RetentionTask(StatsCollector(db)),
        quota_probe_minutes=minutes,
    )
    assert runner.task_status()[0]["interval_seconds"] == 3600.0
    current["minutes"] = 15
    assert runner.task_status()[0]["interval_seconds"] == 900.0


def test_task_status_reports_activity_interval_and_toggle(repo):
    """活跃上报：窗口周期固定 10 分钟，enabled 是开关的当前值。"""
    credentials, db = repo
    enabled = {"on": False}

    def is_on() -> bool:
        return enabled["on"]

    runner = TaskRunner(
        quota_probe=QuotaProbeTask(credentials, {"codebuddy": StubProvider()}, None),
        checkin=CheckinTask(credentials, {"codebuddy": StubProvider()}),
        refresh=RefreshTask(credentials, {"codebuddy": StubProvider()}, skew_seconds=1),
        retention=RetentionTask(StatsCollector(db)),
        activity=ActivityTask(credentials, {"codebuddy": StubProvider()}),
        activity_enabled=is_on,
    )

    def activity_item():
        return next(i for i in runner.task_status() if i["key"] == "activity")

    assert activity_item()["interval_seconds"] == 600.0
    assert activity_item()["enabled"] is False
    enabled["on"] = True
    assert activity_item()["enabled"] is True


def test_task_status_includes_growth_when_assembled(repo):
    credentials, db = repo
    runner = TaskRunner(
        quota_probe=QuotaProbeTask(credentials, {"codebuddy": StubProvider()}, None),
        checkin=CheckinTask(credentials, {"codebuddy": StubProvider()}),
        refresh=RefreshTask(credentials, {"codebuddy": StubProvider()}, skew_seconds=1),
        retention=RetentionTask(StatsCollector(db)),
        growth=object(),
        growth_interval_minutes=10,
    )
    growth = next(i for i in runner.task_status() if i["key"] == "growth")
    assert growth["interval_seconds"] == 600.0     # 下限 5 分钟，10 分钟生效


def test_runner_reuses_injected_status_store(repo):
    store = TaskStatusStore()
    runner = _runner(repo, StubProvider(), status=store)
    assert runner.status is store


def test_task_specs_are_unique_and_named():
    assert len({spec.key for spec in TASK_SPECS}) == len(TASK_SPECS)
    for spec in TASK_SPECS:
        assert spec.name and spec.description
        assert TASK_BY_KEY[spec.key] is spec


def test_every_hot_setting_task_owner_is_a_real_task():
    """配置项的 task 归属必须是真实任务 key，否则页面会归到不存在的卡片。"""
    from src.runtime_settings import HOT_SETTINGS

    for setting in HOT_SETTINGS:
        if setting.task is not None:
            assert setting.task in TASK_BY_KEY, setting.key


# ------------------------------------------------------------- /api/tasks 端点

@pytest.fixture()
def admin_app(tmp_path):
    from fastapi.testclient import TestClient

    from src.main import build_app

    settings = Settings(_env_file=None, APP_SECRET=SECRET, ADMIN_USERNAMES="root",
                        DATA_DIR=str(tmp_path))
    app = build_app(settings)
    client = TestClient(app)
    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    with client:
        yield app, client


def test_tasks_requires_admin(admin_app):
    _app, client = admin_app
    client.cookies.set("coding2api_session", create_session_token("guest", SECRET))
    assert client.get("/api/tasks").status_code == 403


def test_tasks_returns_runtime_snapshot(admin_app):
    """端点返回已装配任务的运行态 + server_time；启动首轮探测已入账。"""
    _app, client = admin_app
    body = client.get("/api/tasks").json()
    assert isinstance(body["server_time"], int) and body["server_time"] > 0
    keys = [item["key"] for item in body["tasks"]]
    assert "quota_probe" in keys and "retention" in keys
    probe = next(item for item in body["tasks"] if item["key"] == "quota_probe")
    assert probe["name"] == "额度探测"
    assert probe["interval_seconds"] == 3600.0
    assert probe["last_ok"] is True            # 启动首轮探测（空池）也是真实执行
    assert probe["last_report"] == {
        "attempted": 0, "succeeded": 0, "failed": 0, "skipped": 0}
    assert probe["runs"] == 1
    assert "description" in probe


def test_tasks_endpoint_without_runner_returns_empty_list(tmp_path):
    """app.state 上还没有 runner（未进 lifespan）时不能 500。"""
    from fastapi.testclient import TestClient

    from src.main import build_app

    app = build_app(Settings(_env_file=None, APP_SECRET=SECRET,
                             ADMIN_USERNAMES="root", DATA_DIR=str(tmp_path)))
    client = TestClient(app)
    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    response = client.get("/api/tasks")   # 不进 with：lifespan 未运行
    assert response.status_code == 200
    assert response.json()["tasks"] == []


def test_settings_snapshot_carries_task_owner(tmp_path):
    """任务归属随 /api/settings 下发：前端据此把配置归到任务卡片。"""
    from fastapi.testclient import TestClient

    from src.main import build_app

    app = build_app(Settings(_env_file=None, APP_SECRET=SECRET,
                             ADMIN_USERNAMES="root", DATA_DIR=str(tmp_path)))
    client = TestClient(app)
    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    by_key = {item["key"]: item for item in client.get("/api/settings").json()["settings"]}
    assert by_key["quota_probe_minutes"]["task"] == "quota_probe"
    assert by_key["growth_interval_minutes"]["task"] == "growth"
    assert by_key["activity_report_enabled"]["task"] == "activity"
    assert by_key["default_model"]["task"] is None


def test_build_runner_accepts_shared_status_store(repo):
    """生产装配传共享 store，端点才能读到 runner 写下的运行态。"""
    credentials, db = repo
    store = TaskStatusStore()
    config = Settings(_env_file=None, APP_SECRET=SECRET)
    runner = build_runner(credentials, {}, StatsCollector(db), config, status=store)
    assert runner.status is store

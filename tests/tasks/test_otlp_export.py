"""OTLP 导出后台任务（`src/tasks/otlp_export.py`）与 TaskRunner 接线。"""

from __future__ import annotations

from src.config import Settings
from src.observability.otlp import OtlpExporter
from src.stats.collector import StatsCollector
from src.tasks.checkin import CheckinTask
from src.tasks.otlp_export import OtlpExportTask
from src.tasks.quota_probe import QuotaProbeTask
from src.tasks.refresh import RefreshTask
from src.tasks.retention import RetentionTask
from src.tasks.runner import TaskRunner, build_runner
from tests.helpers.auth import settings_with


class _Post:
    async def __call__(self, url, payload, headers):
        self.calls = getattr(self, "calls", []) + [(url, payload, headers)]
        return type("R", (), {"status_code": 200})()


def _exporter(endpoint: str = "http://127.0.0.1:4318") -> OtlpExporter:
    return OtlpExporter(endpoint, snapshot=lambda: {"requests": [("trae", "m", 1, 1)]},
                        post=_Post(), now=lambda: 1)


async def test_task_is_noop_when_endpoint_not_configured():
    """端点为空 = 关闭：返回 None（归档为「未运行」），零出站。"""
    post = _Post()
    task = OtlpExportTask(OtlpExporter("", snapshot=lambda: {}, post=post))
    assert await task.run_once() is None
    assert getattr(post, "calls", []) == []


async def test_task_reports_export_result():
    task = OtlpExportTask(_exporter())
    assert await task.run_once() == {"exported": True, "failed": 0}


async def test_task_reports_failure_without_raising():
    async def boom(url, payload, headers):
        raise RuntimeError("collector down")

    task = OtlpExportTask(OtlpExporter("http://127.0.0.1:4318",
                                       snapshot=lambda: {}, post=boom))
    # 失败只落到 report：任务抛异常会触发「任务连续失败」告警，而采集端连不上
    # 不等于本服务坏了。
    assert await task.run_once() == {"exported": False, "failed": 1}


def _runner(repo, *, otlp_export=None, otlp_export_seconds=60,
            otlp_export_enabled=None):
    credentials, db = repo
    collector = StatsCollector(db)
    return TaskRunner(
        quota_probe=QuotaProbeTask(credentials, {}, None),
        checkin=CheckinTask(credentials, {}),
        refresh=RefreshTask(credentials, {}, skew_seconds=3600),
        retention=RetentionTask(collector),
        otlp_export=otlp_export,
        otlp_export_seconds=otlp_export_seconds,
        otlp_export_enabled=otlp_export_enabled,
    )


def test_runner_lists_otlp_task_only_when_assembled(repo):
    """未装配时不出现在任务清单（页面上不该有「从不失败」的幽灵任务）。"""
    plain = _runner(repo)
    assert "otlp_export" not in {item["key"] for item in plain.task_status()}

    wired = _runner(repo, otlp_export=OtlpExportTask(_exporter()))
    items = {item["key"]: item for item in wired.task_status()}
    assert "otlp_export" in items
    assert items["otlp_export"]["interval_seconds"] == 60.0
    assert items["otlp_export"]["enabled"] is True


def test_runner_reports_otlp_disabled_when_endpoint_missing(repo):
    """端点为空时任务卡片显示「未启用」，而不是「启用但从不运行」。"""
    task = OtlpExportTask(OtlpExporter("", snapshot=lambda: {}))
    runner = _runner(repo, otlp_export=task,
                     otlp_export_enabled=lambda: task._exporter.enabled)
    items = {item["key"]: item for item in runner.task_status()}
    assert items["otlp_export"]["enabled"] is False


def test_runner_otlp_interval_has_floor_of_15_seconds(repo):
    """周期写错成 0 / 5 也钳到 15 秒，避免循环忙等。"""
    runner = _runner(repo, otlp_export=OtlpExportTask(_exporter()),
                     otlp_export_seconds=0)
    assert runner._otlp_export_interval == 15.0
    runner = _runner(repo, otlp_export=OtlpExportTask(_exporter()),
                     otlp_export_seconds=90)
    assert runner._otlp_export_interval == 90.0


async def test_runner_runs_otlp_export_manually(repo):
    """手动触发走同一条 `_loop_specs` 清单（与定时循环单一来源）。"""
    task = OtlpExportTask(_exporter())
    runner = _runner(repo, otlp_export=task)
    result = await runner.run_task("otlp_export")
    assert result["ran"] is True
    assert result["last_ok"] is True
    assert result["last_report"] == {"exported": True, "failed": 0}


async def test_manual_run_of_disabled_exporter_is_noop(repo):
    """端点未配置时手动触发也是 no-op（记「未运行」，不覆盖上次结果）。"""
    task = OtlpExportTask(OtlpExporter("", snapshot=lambda: {}))
    runner = _runner(repo, otlp_export=task)
    result = await runner.run_task("otlp_export")
    assert result["ran"] is False
    assert result["runs"] == 0


def test_build_runner_wires_otlp_task_and_hot_period(monkeypatch):
    """`build_runner` 把任务与周期取值器都接上（周期热更现读）。"""
    settings = settings_with(OTLP_EXPORT_SECONDS="120")
    assert isinstance(settings, Settings)
    staged: dict[str, object] = {}

    def fake_task_runner(**kwargs):
        staged.update(kwargs)
        return kwargs

    monkeypatch.setattr("src.tasks.runner.TaskRunner", fake_task_runner)
    task = OtlpExportTask(_exporter())
    result = build_runner(object(), {}, object(), settings, otlp_export=task)
    assert result["otlp_export"] is task
    # 周期是取值器而不是快照：管理台改 OTLP_EXPORT_SECONDS 后下一轮立即生效
    assert staged["otlp_export_seconds"]() == 120

"""后台任务调度：把额度探测、签到、token 预刷新、明细清理接进应用生命周期。

设计要点（PROPOSAL §8 / TECHNICAL §6）：
- 两类任务共用一个全局随机间隔节流器（跨所有用户）
- 后台任务失败只记日志，绝不影响聊天请求
- 启动时先做一轮额度探测（不节流），避免调度器拿到全 unknown 的池
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..config import live
from .activity import ActivityTask
from .checkin import CheckinTask
from .growth import GrowthTask
from .pacer import Pacer
from .quota_probe import QuotaProbeTask
from .refresh import RefreshTask
from .retention import RetentionTask
from .status import TASK_SPECS, TaskStatusStore

logger = logging.getLogger(__name__)


class TaskRunner:
    """周期任务循环。每类任务一个 asyncio 任务，异常互不影响。"""

    def __init__(
        self,
        *,
        quota_probe: QuotaProbeTask,
    checkin: CheckinTask,
    growth: GrowthTask | None = None,
    activity: ActivityTask | None = None,
    refresh: RefreshTask,
    retention: RetentionTask,
    quota_probe_minutes: int | Callable[[], int] = 60,
    growth_interval_minutes: int | Callable[[], int] = 60,
    refresh_interval_minutes: int | Callable[[], int] = 60,
    retention_interval_minutes: int | Callable[[], int] = 5,
    activity_enabled: Callable[[], bool] | None = None,
    status: TaskStatusStore | None = None,
) -> None:
        self._quota_probe = quota_probe
        self._checkin = checkin
        self._growth = growth
        self._activity = activity
        self._refresh = refresh
        self._retention = retention
        # 周期可热更（B3.2）：存取值器，每轮 sleep 前读当前值（否则改配置
        # 要等到下一次重启才生效）。下限与业务语义同前，不变。
        self._quota_probe_minutes = live(quota_probe_minutes)
        self._growth_minutes = live(growth_interval_minutes)
        self._refresh_minutes = live(refresh_interval_minutes)
        self._retention_minutes = live(retention_interval_minutes)
        # 活跃上报是否启用也可热更：装配时恒建对象（构造成本为零），
        # 每轮由 _sync_activity 问一次，关着时是 no-op。
        self._activity_enabled = activity_enabled or (lambda: activity is not None)
        # 活跃上报：每 10 分钟醒一次看时点（due() 只在配置小时窗口内放行），
        # 而不是整点只醒一次——服务恰在整点重启会整天漏报
        self._activity_interval = 600
        self._tasks: list[asyncio.Task[None]] = []
        # 进程内运行态（管理台「任务与配置」页）：共享实例由 build_app 注入，
        # 未注入时自建（测试与一次性装配直接构造 TaskRunner）。
        self.status = status or TaskStatusStore()

    @property
    def _quota_interval(self) -> float:
        return max(60, int(self._quota_probe_minutes()) * 60)

    @property
    def _growth_interval(self) -> float:
        # 成长中心请求量比签到大一个量级（一轮 7 类领取），间隔下限设 5 分钟：
        # 比这更密只会撞上游风控，而 Buddy 旅行最快 1 小时才回来
        return max(300, int(self._growth_minutes()) * 60)

    @property
    def _refresh_interval(self) -> float:
        return max(60, int(self._refresh_minutes()) * 60)

    @property
    def _retention_interval(self) -> float:
        return max(60, int(self._retention_minutes()) * 60)

    async def start(self) -> None:
        """启动所有周期任务；首轮额度探测立即执行（不节流）。"""
        await self._guarded(self._quota_probe.run_once(apply_pacing=False),
                            "启动额度探测", key="quota_probe")
        loops: list[tuple[str, str, Callable[[], Awaitable[object]],
                          Callable[[], float]]] = [
            ("quota_probe", "额度探测", lambda: self._quota_probe.run_once(),
             lambda: self._quota_interval),
            ("refresh", "token 预刷新", self._refresh.run_once,
             lambda: self._refresh_interval),
            ("retention", "明细清理", self._sync_retention,
             lambda: self._retention_interval),
            ("checkin", "每日签到", self._sync_checkin, lambda: 600.0),  # 全天每 10 分钟签到一次
        ]
        if self._growth is not None:
            loops.append(("growth", "成长中心", self._sync_growth,
                          lambda: self._growth_interval))
        if self._activity is not None:
            loops.append(("activity", "活跃上报", self._sync_activity,
                          lambda: float(self._activity_interval)))
        for key, name, runner, interval in loops:
            self._tasks.append(asyncio.create_task(
                self._loop(name, runner, interval, key=key)))

    async def _sync_growth(self) -> object:
        """成长中心一轮：领礼物 / 派 Buddy / 任务 / 补登 / 兑换 / 抽奖 / 盲盒。"""
        assert self._growth is not None
        return await self._growth.run_once()

    async def _sync_activity(self) -> object:
        """活跃上报：仅启用且处于配置小时窗口内才执行，成功凭证当日封账。

        「是否启用」每轮现读（B3.2）：关掉再打开不需要重启，也不必重建循环。
        """
        assert self._activity is not None
        if not self._activity_enabled():
            return None
        if not self._activity.due():
            return None
        return await self._activity.run_once()

    async def _sync_checkin(self) -> object:
        """全天每 10 分钟一轮；成功凭证当日封账（run_once 内跳过），失败凭证持续重试。"""
        if not self._checkin.due():
            return None
        return await self._checkin.run_once()

    async def _sync_retention(self) -> object:
        return self._retention.run_once()

    async def _loop(self, name: str, runner: Callable[[], Awaitable[object]],
                    interval: float | Callable[[], float],
                    key: str | None = None) -> None:
        """周期循环：间隔在每次 sleep 前重新求值（B3.2 热更周期）。

        传标量等价于固定周期（测试与一次性任务仍这么用）。
        """
        delay = live(interval)
        while True:
            try:
                await asyncio.sleep(delay())
            except asyncio.CancelledError:
                raise
            await self._guarded(runner(), name, key=key)

    async def _guarded(self, awaitable: Awaitable[object], name: str,
                       key: str | None = None) -> bool:
        """跑一轮并吞掉异常（后台任务不能拖垮服务）。

        `key` 给出时把「真实执行」记进运行态：返回 None 表示本轮 no-op
        （签到未到点 / 活跃上报未启用），不覆盖上一次结果——否则页面会显示
        「签到刚刚跑过」，而当天其实一次都没签。
        """
        started = time.time()
        try:
            result = await awaitable
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 后台任务不能拖垮服务
            logger.warning("后台任务「%s」失败: %s", name, error)
            if key is not None:
                self.status.record(key, started_at=started, ok=False,
                                   report=None, error=str(error))
            return False
        if key is not None and result is not None:
            self.status.record(key, started_at=started, ok=True,
                               report=_as_report(result), error=None)
        return True

    def task_status(self) -> list[dict[str, Any]]:
        """管理台「任务与配置」页：每个已装配任务的上次真跑 + 当前周期/开关。

        周期与开关是**当前生效值**（热更后立刻反映），不是装配时的快照。
        """
        items: list[dict[str, Any]] = []
        for spec in TASK_SPECS:
            if spec.key == "growth" and self._growth is None:
                continue
            if spec.key == "activity" and self._activity is None:
                continue
            run = self.status.get(spec.key)
            items.append({
                "key": spec.key,
                "name": spec.name,
                "description": spec.description,
                "interval_seconds": self._interval_seconds(spec.key),
                "enabled": self._task_enabled(spec.key),
                "runs": self.status.runs(spec.key),
                "last_started_at": run.started_at if run is not None else None,
                "last_finished_at": run.finished_at if run is not None else None,
                "last_ok": run.ok if run is not None else None,
                "last_report": run.report if run is not None else None,
                "last_error": run.error if run is not None else None,
            })
        return items

    def _interval_seconds(self, key: str) -> float:
        if key == "quota_probe":
            return self._quota_interval
        if key == "refresh":
            return self._refresh_interval
        if key == "checkin":
            return 600.0            # 全天每 10 分钟检查一次，与 _loop 装配一致
        if key == "growth":
            return self._growth_interval
        if key == "activity":
            return float(self._activity_interval)
        return self._retention_interval

    def _task_enabled(self, key: str) -> bool:
        if key == "activity":
            return bool(self._activity_enabled())
        return True

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()


def _as_report(result: object) -> dict[str, Any]:
    """任务返回值 → JSON 可序列化摘要。

    `TaskReport` 有 `as_dict()`；清理任务直接返回 dict；其它形态（含 None 之外
    的自定义对象）只记 `{"result": repr}`，保证运行态一定能落成 JSON。
    """
    as_dict = getattr(result, "as_dict", None)
    if callable(as_dict):
        report = as_dict()
        if isinstance(report, dict):
            return report
    if isinstance(result, dict):
        return result
    return {"result": repr(result)}


def build_runner(credentials, providers: dict, stats_collector, config,
                 growth_events=None, credit_events=None,
                 status: TaskStatusStore | None = None) -> TaskRunner:
    """按配置装配后台任务（Pacer 由两个 provider 共享）。

    growth_events 为 None 时（老调用方/测试）不装配成长中心任务：没有落库目标
    就跑起来只会把结果丢掉；生产路径（main.py）总是传入。

    credit_events 同理：为 None 时保留流水清理不启用（表仍会随探测增长，
    但不影响功能；生产路径总是传入）。

    B3.2 热更：`config` 既可以是启动期快照 `Settings`，也可以是
    `RuntimeSettings` 覆盖层。装配时所有「可热更项」必须传**零参 lambda**，
    后台循环每轮读当前值——否则改周期/开关都要等重启，热更就名存实亡。
    注意不能写成 `live(config.x)`：那会立刻求值一次再包成常量，
    对 `Settings` 无害、对覆盖层则等于没热更。
    """
    pacer = Pacer(lambda: config.pacer_min_seconds,
                  lambda: config.pacer_max_seconds)
    growth = None
    if growth_events is not None:
        growth = GrowthTask(credentials, providers, growth_events,
                            allow_irreversible=lambda: config.growth_irreversible_actions,
                            pacer=pacer)
    # 活跃上报恒建对象（构造成本为零），是否真的跑由每条循环现读的开关决定：
    # 只有对象先存在，管理台才能把默认关闭的它热开到不需要重启。
    activity = ActivityTask(credentials, providers, events=growth_events,
                            hour=lambda: config.activity_report_hour)
    return TaskRunner(
        quota_probe=QuotaProbeTask(credentials, providers, pacer),
        checkin=CheckinTask(credentials, providers),
        growth=growth,
        activity=activity,
        refresh=RefreshTask(credentials, providers, skew_seconds=config.refresh_skew_hours * 3600,
                            now=lambda: int(time.time())),
        retention=RetentionTask(stats_collector, credentials=credentials,
                                credit_events=credit_events),
        quota_probe_minutes=lambda: config.quota_probe_minutes,
        growth_interval_minutes=lambda: config.growth_interval_minutes,
        activity_enabled=lambda: config.activity_report_enabled,
        status=status,
    )

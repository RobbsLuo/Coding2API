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

from .checkin import CheckinTask
from .growth import GrowthTask
from .pacer import Pacer
from .quota_probe import QuotaProbeTask
from .refresh import RefreshTask
from .retention import RetentionTask

logger = logging.getLogger(__name__)


class TaskRunner:
    """周期任务循环。每类任务一个 asyncio 任务，异常互不影响。"""

    def __init__(
        self,
        *,
        quota_probe: QuotaProbeTask,
    checkin: CheckinTask,
    growth: GrowthTask | None = None,
    refresh: RefreshTask,
    retention: RetentionTask,
    quota_probe_minutes: int = 60,
    growth_interval_minutes: int = 60,
    refresh_interval_minutes: int = 60,
    retention_interval_minutes: int = 5,
) -> None:
        self._quota_probe = quota_probe
        self._checkin = checkin
        self._growth = growth
        self._refresh = refresh
        self._retention = retention
        self._quota_interval = max(60, quota_probe_minutes * 60)
        # 成长中心请求量比签到大一个量级（一轮 7 类领取），间隔下限设 5 分钟：
        # 比这更密只会撞上游风控，而 Buddy 旅行最快 1 小时才回来
        self._growth_interval = max(300, growth_interval_minutes * 60)
        self._refresh_interval = max(60, refresh_interval_minutes * 60)
        self._retention_interval = max(60, retention_interval_minutes * 60)
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        """启动所有周期任务；首轮额度探测立即执行（不节流）。"""
        await self._guarded(self._quota_probe.run_once(apply_pacing=False),
                            "启动额度探测")
        loops: list[tuple[str, Callable[[], Awaitable[object]], float]] = [
            ("额度探测", lambda: self._quota_probe.run_once(), self._quota_interval),
            ("token 预刷新", self._refresh.run_once, self._refresh_interval),
            ("明细清理", self._sync_retention, self._retention_interval),
            ("每日签到", self._sync_checkin, 600),  # 全天每 10 分钟签到一次（成功凭证当日封账）
        ]
        if self._growth is not None:
            loops.append(("成长中心", self._sync_growth, self._growth_interval))
        for name, runner, interval in loops:
            self._tasks.append(asyncio.create_task(self._loop(name, runner, interval)))

    async def _sync_growth(self) -> object:
        """成长中心一轮：领礼物 / 派 Buddy / 任务 / 补登 / 兑换 / 抽奖 / 盲盒。"""
        assert self._growth is not None
        return await self._growth.run_once()

    async def _sync_checkin(self) -> object:
        """全天每 10 分钟一轮；成功凭证当日封账（run_once 内跳过），失败凭证持续重试。"""
        if not self._checkin.due():
            return None
        return await self._checkin.run_once()

    async def _sync_retention(self) -> object:
        return self._retention.run_once()

    async def _loop(self, name: str, runner: Callable[[], Awaitable[object]],
                    interval: float) -> None:
        while True:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            await self._guarded(runner(), name)

    async def _guarded(self, awaitable: Awaitable[object], name: str) -> bool:
        try:
            await awaitable
            return True
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 后台任务不能拖垮服务
            logger.warning("后台任务「%s」失败: %s", name, error)
            return False

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()


def build_runner(credentials, providers: dict, stats_collector, config,
                 growth_events=None) -> TaskRunner:
    """按配置装配后台任务（Pacer 由两个 provider 共享）。

    growth_events 为 None 时（老调用方/测试）不装配成长中心任务：没有落库目标
    就跑起来只会把结果丢掉；生产路径（main.py）总是传入。
    """
    pacer = Pacer(config.pacer_min_seconds, config.pacer_max_seconds)
    growth = None
    if growth_events is not None:
        growth = GrowthTask(credentials, providers, growth_events,
                            allow_irreversible=config.growth_irreversible_actions,
                            pacer=pacer)
    return TaskRunner(
        quota_probe=QuotaProbeTask(credentials, providers, pacer),
        checkin=CheckinTask(credentials, providers),
        growth=growth,
        refresh=RefreshTask(credentials, providers, skew_seconds=config.refresh_skew_hours * 3600,
                            now=lambda: int(time.time())),
        retention=RetentionTask(stats_collector, credentials=credentials),
        quota_probe_minutes=config.quota_probe_minutes,
        growth_interval_minutes=config.growth_interval_minutes,
    )

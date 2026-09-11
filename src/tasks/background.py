"""后台任务：全局节流器、额度探测、签到、token 预刷新、明细清理。

节流器是全局的（跨所有用户），两类任务互不节流（PROPOSAL §8 的 CB 语义）。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..db.repo import CredentialRepository
from ..provider.base import Quota
from ..stats.collector import StatsCollector

logger = logging.getLogger(__name__)


class Pacer:
    """全局随机间隔节流器：相邻上游请求之间至少间隔 min..max 秒。"""

    def __init__(self, min_seconds: float, max_seconds: float,
                 *, sleep: Callable[[float], Awaitable[None]] | None = None,
                 now: Callable[[], float] | None = None) -> None:
        if min_seconds < 0 or max_seconds < 0:
            raise ValueError("pacer bounds must be non-negative")
        if min_seconds > max_seconds:
            raise ValueError("pacer min must not exceed max")
        self._min = min_seconds
        self._max = max_seconds
        self._sleep = sleep or asyncio.sleep
        self._now = now or time.monotonic
        self._last_started: float | None = None
        self._lock = asyncio.Lock()
        self._random = random.Random(0)          # 确定性：测试可复现

    @property
    def disabled(self) -> bool:
        return self._min == 0 and self._max == 0

    def next_interval(self) -> float:
        if self.disabled:
            return 0.0
        if self._min == self._max:
            return self._min
        return self._random.uniform(self._min, self._max)

    async def wait_turn(self) -> None:
        """取得一个节流 turn；只有即将真正调用上游时才应调用。"""
        if self.disabled:
            return
        async with self._lock:
            interval = self.next_interval()
            if self._last_started is not None:
                elapsed = self._now() - self._last_started
                remaining = interval - elapsed
                if remaining > 0:
                    await self._sleep(remaining)
            self._last_started = self._now()


@dataclass
class TaskReport:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"attempted": self.attempted, "succeeded": self.succeeded,
                "failed": self.failed, "skipped": self.skipped}


class QuotaProbeTask:
    """周期额度探测 → 写回 credentials.quota_* 与 health。"""

    def __init__(self, credentials: CredentialRepository, providers: dict,
                 pacer: Pacer | None = None) -> None:
        self._credentials = credentials
        self._providers = providers
        self._pacer = pacer

    async def run_once(self, *, apply_pacing: bool = True) -> TaskReport:
        report = TaskReport()
        for candidate in self._credentials.candidates():
            provider = self._providers.get(candidate.provider)
            if provider is None:
                report.skipped += 1
                continue
            data = self._credentials.credential_data(candidate.credential_id)
            if data is None:
                report.skipped += 1
                continue
            report.attempted += 1
            if apply_pacing and self._pacer is not None:
                await self._pacer.wait_turn()
            try:
                quota = await provider.probe_quota(data)
            except Exception as error:  # noqa: BLE001 - 探测失败不能中断整批
                logger.warning("quota probe failed for %s: %s", candidate.credential_id, error)
                # 探测失败 → health 置 NULL（unknown），绝不当作 0 分
                self._credentials.mark_probe_failed(candidate.credential_id)
                report.failed += 1
                continue
            self._apply(candidate.credential_id, quota)
            report.succeeded += 1
        return report

    def _apply(self, credential_id: str, quota: Quota) -> None:
        self._credentials.save_quota(credential_id, quota)


class CheckinTask:
    """每日签到：按「endpoint + X-User-Id」隔离，同账号多凭证共享一次。"""

    def __init__(self, credentials: CredentialRepository, providers: dict,
                 *, checkin_hour: int = 9, on_success: Callable[[str], None] | None = None) -> None:
        self._credentials = credentials
        self._providers = providers
        self.checkin_hour = checkin_hour
        self._on_success = on_success
        self._done_scopes: set[str] = set()

    def due(self, *, now: time.struct_time | None = None) -> bool:
        """09:30 之后当日首次视为到期（服务器本地时区）。"""
        current = now or time.localtime()
        if current.tm_hour < self.checkin_hour:
            return False
        return self._day_key(current) not in self._done_scopes

    def _day_key(self, now: time.struct_time) -> str:
        return f"{now.tm_year:04d}-{now.tm_mon:02d}-{now.tm_mday:02d}"

    async def run_once(self, *, now: time.struct_time | None = None) -> TaskReport:
        current = now or time.localtime()
        report = TaskReport()
        seen: set[str] = set()
        for candidate in self._credentials.candidates():
            if candidate.disabled:
                report.skipped += 1
                continue
            provider = self._providers.get(candidate.provider)
            checkin_scope = getattr(provider, "checkin_scope", None)
            data = self._credentials.credential_data(candidate.credential_id)
            if provider is None or data is None or checkin_scope is None:
                report.skipped += 1
                continue
            scope = checkin_scope(data)
            if scope in seen:
                report.skipped += 1                      # 同上游账号只签一次
                continue
            seen.add(scope)
            report.attempted += 1
            try:
                result = await provider.checkin(data)
            except Exception as error:  # noqa: BLE001
                logger.warning("checkin failed for %s: %s", candidate.credential_id, error)
                report.failed += 1
                continue
            if result.ok:
                report.succeeded += 1
                if self._on_success is not None:
                    self._on_success(candidate.credential_id)
            else:
                report.failed += 1
                logger.warning("checkin failed for %s: %s",
                               candidate.credential_id, result.message or "未知原因")
        # 只有全部成功才当日封账；有失败时留着，下轮循环重试
        if report.failed == 0:
            self._done_scopes.add(self._day_key(current))
        return report


class RefreshTask:
    """token 预刷新：只处理进入 refresh_skew 窗口的凭证。"""

    def __init__(self, credentials: CredentialRepository, providers: dict,
                 *, skew_seconds: int, now: Callable[[], int] | None = None) -> None:
        self._credentials = credentials
        self._providers = providers
        self.skew_seconds = skew_seconds
        self._now = now or (lambda: int(time.time()))

    async def run_once(self) -> TaskReport:
        report = TaskReport()
        current = self._now()
        for candidate in self._credentials.candidates():
            if candidate.disabled:
                report.skipped += 1
                continue
            provider = self._providers.get(candidate.provider)
            data = self._credentials.credential_data(candidate.credential_id)
            if provider is None or data is None:
                report.skipped += 1
                continue
            if not _needs_refresh(provider, data, self.skew_seconds, current):
                report.skipped += 1
                continue
            report.attempted += 1
            try:
                refreshed = await provider.refresh(data)
            except Exception as error:  # noqa: BLE001
                logger.warning("refresh failed for %s: %s", candidate.credential_id, error)
                report.failed += 1
                continue
            # 先持久化轮换后的 token，再同步账号（AGENTS.md 顺序约束）
            self._credentials.save_credential_data(candidate.credential_id, refreshed)
            report.succeeded += 1
        return report


def _needs_refresh(provider, data: dict, skew_seconds: int, now: int) -> bool:
    builder = getattr(provider, "credential_from", None)
    if builder is None:
        return False
    credential = builder(data)
    needs = getattr(credential, "needs_refresh", None)
    return bool(needs(skew_seconds, now)) if callable(needs) else False


class RetentionTask:
    """明细 90 天清理 + 小时汇总（永久保留）。"""

    def __init__(self, collector: StatsCollector, *, retention_days: int = 90) -> None:
        self._collector = collector
        self.retention_days = retention_days

    def run_once(self) -> dict[str, int]:
        rolled = self._collector.rollup_hourly()
        purged = self._collector.purge_expired(self.retention_days)
        return {"rolled_up": rolled, "purged": purged}

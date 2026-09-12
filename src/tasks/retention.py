"""usage_events 明细 90 天清理 + 小时汇总（永久保留）。"""

from __future__ import annotations

from ..stats.collector import StatsCollector


class RetentionTask:
    """明细 90 天清理 + 小时汇总（永久保留）。"""

    def __init__(self, collector: StatsCollector, *, retention_days: int = 90) -> None:
        self._collector = collector
        self.retention_days = retention_days

    def run_once(self) -> dict[str, int]:
        rolled = self._collector.rollup_hourly()
        purged = self._collector.purge_expired(self.retention_days)
        return {"rolled_up": rolled, "purged": purged}

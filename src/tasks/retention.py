"""usage_events 明细 90 天清理 + 小时汇总（永久保留）。"""

from __future__ import annotations

from ..stats.collector import StatsCollector


class RetentionTask:
    """明细 90 天清理 + 小时汇总（永久保留）。

    `credentials` 非 None 时顺带清理已过期的 (凭证, 模型) 冷却行——
    那些行只在限流/负缓存期间存在，不回收到期行会无限累积（管理台
    每次列表都要整表扫一遍）。
    """

    def __init__(self, collector: StatsCollector, *, retention_days: int = 90,
                 credentials=None) -> None:
        self._collector = collector
        self.retention_days = retention_days
        self._credentials = credentials

    def run_once(self) -> dict[str, int]:
        rolled = self._collector.rollup_hourly()
        purged = self._collector.purge_expired(self.retention_days)
        expired_coolings = (
            self._credentials.purge_expired_model_cooldowns()
            if self._credentials is not None else 0)
        return {"rolled_up": rolled, "purged": purged,
                "expired_coolings": expired_coolings}

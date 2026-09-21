"""usage_events 明细 90 天清理 + 小时汇总（永久保留）。"""

from __future__ import annotations

from ..stats.collector import StatsCollector


class RetentionTask:
    """明细 90 天清理 + 小时汇总（永久保留）。

    `credentials` 非 None 时顺带清理已过期的 (凭证, 模型) 冷却行——
    那些行只在限流/负缓存期间存在，不回收到期行会无限累积（管理台
    每次列表都要整表扫一遍）。

    `credit_events` 非 None 时同样按保留期清理积分流水：它每轮探测都可能
    落一行，不回收会长期增长（读侧有 LIMIT，但表本身会一直变大）。
    """

    def __init__(self, collector: StatsCollector, *, retention_days: int = 90,
                 credentials=None, credit_events=None) -> None:
        self._collector = collector
        self.retention_days = retention_days
        self._credentials = credentials
        self._credit_events = credit_events

    def run_once(self) -> dict[str, int]:
        rolled = self._collector.rollup_hourly()
        purged = self._collector.purge_expired(self.retention_days)
        expired_coolings = (
            self._credentials.purge_expired_model_cooldowns()
            if self._credentials is not None else 0)
        purged_credit_events = (
            self._credit_events.prune(keep_days=self.retention_days)
            if self._credit_events is not None else 0)
        return {"rolled_up": rolled, "purged": purged,
                "expired_coolings": expired_coolings,
                "purged_credit_events": purged_credit_events}

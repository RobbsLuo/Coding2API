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

    `audit` 非 None 时按同一保留期清理审计流水：登录/写操作每笔一行，
    不回收同样会无界增长（README「审计」承诺保留期与请求明细一致）。

    `alerts` 非 None 时按同一保留期清理运维告警：告警是周期性评估的产物，
    长期无界增长会让「站内告警记录」越拉越慢。
    """

    def __init__(self, collector: StatsCollector, *, retention_days: int = 90,
                 credentials=None, credit_events=None, audit=None,
                 alerts=None) -> None:
        self._collector = collector
        self.retention_days = retention_days
        self._credentials = credentials
        self._credit_events = credit_events
        self._audit = audit
        self._alerts = alerts

    def run_once(self) -> dict[str, int]:
        rolled = self._collector.rollup_hourly()
        purged = self._collector.purge_expired(self.retention_days)
        expired_coolings = (
            self._credentials.purge_expired_model_cooldowns()
            if self._credentials is not None else 0)
        purged_credit_events = (
            self._credit_events.prune(keep_days=self.retention_days)
            if self._credit_events is not None else 0)
        purged_audit = (
            self._audit.prune(keep_days=self.retention_days)
            if self._audit is not None else 0)
        purged_alerts = (
            self._alerts.prune(keep_days=self.retention_days)
            if self._alerts is not None else 0)
        return {"rolled_up": rolled, "purged": purged,
                "expired_coolings": expired_coolings,
                "purged_credit_events": purged_credit_events,
                "purged_audit": purged_audit,
                "purged_alerts": purged_alerts}

"""usage_events 明细 90 天清理 + 小时汇总（永久保留）。"""

from __future__ import annotations

from ..stats.collector import StatsCollector


class RetentionTask:
    """明细 90 天清理 + 小时汇总（永久保留）。

    `credentials` 非 None 时顺带清理已过期的冷却行——模型级
    `credential_model_cooldowns` 整行删除、账号级 `credentials.cooling_until`
    置 NULL。那些值只在限流/负缓存期间有意义，不回收到期值会无限累积
    （管理台每次列表都要整表扫一遍，且陈旧值会让「冷却」排序失真）。

    `credit_events` 非 None 时同样按保留期清理积分流水：它每轮探测都可能
    落一行，不回收会长期增长（读侧有 LIMIT，但表本身会一直变大）。

    `audit` 非 None 时按同一保留期清理审计流水：登录/写操作每笔一行，
    不回收同样会无界增长（README「审计」承诺保留期与请求明细一致）。

    `alerts` 非 None 时按同一保留期清理运维告警：告警是周期性评估的产物，
    长期无界增长会让「站内告警记录」越拉越慢。

    `growth_events` 非 None 时按同一保留期清理成长中心运行记录：每轮探测
    都可能落一行，不回收同样无界增长。
    """

    def __init__(self, collector: StatsCollector, *, retention_days: int = 90,
                 credentials=None, credit_events=None, audit=None,
                 alerts=None, growth_events=None) -> None:
        self._collector = collector
        self.retention_days = retention_days
        self._credentials = credentials
        self._credit_events = credit_events
        self._audit = audit
        self._alerts = alerts
        self._growth_events = growth_events

    def run_once(self) -> dict[str, int]:
        """同步执行一轮（CPU/IO 密集，调用方负责放到线程池，见）。"""
        rolled = self._collector.rollup_hourly()
        purged = self._collector.purge_expired(self.retention_days)
        expired_coolings = (
            self._credentials.purge_expired_model_cooldowns()
            if self._credentials is not None else 0)
        expired_account_coolings = (
            self._credentials.purge_expired_account_cooldowns()
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
        purged_growth = (
            self._growth_events.prune(keep_days=self.retention_days)
            if self._growth_events is not None else 0)
        return {"rolled_up": rolled, "purged": purged,
                "expired_coolings": expired_coolings,
                "expired_account_coolings": expired_account_coolings,
                "purged_credit_events": purged_credit_events,
                "purged_audit": purged_audit,
                "purged_alerts": purged_alerts,
                "purged_growth_events": purged_growth}

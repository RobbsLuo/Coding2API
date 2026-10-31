"""usage_events 明细 90 天清理 + 小时汇总（永久保留）。"""

from __future__ import annotations

import time

from ..stats.collector import StatsCollector

# 周期 rollup 只重算最近窗口（秒）。
#
# `record()` 已经在同一事务里增量维护 `usage_hourly`，周期任务的重算只是
# 对账。此前每 5 分钟做一次**全表** GROUP BY（最多 90 天明细）：事务期间持有
# SQLite 写锁，期间的 `record()` 撞上 busy_timeout 后异常被上层吞掉 → 这段
# 时间的用量直接丢失。窗口取 2 小时且按整点对齐：边界小时必须整小时都进入
# 重算，否则 REPLACE 会把该小时覆盖成「只剩窗口内那部分」的偏小值。
# 历史全量重算走 `scripts/rollup_hourly.py`（诊断/修复用，不在周期路径上）。
ROLLUP_OVERLAP_SECONDS = 2 * 3600


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

    @staticmethod
    def rollup_since(now: int | None = None) -> int:
        """本轮 rollup 的起点（整点对齐，见 `ROLLUP_OVERLAP_SECONDS`）。"""
        raw = int(now if now is not None else time.time()) - ROLLUP_OVERLAP_SECONDS
        if raw <= 0:
            # 早期时间戳（测试/时钟异常）：退回全量重算，绝不漏数据。
            return 0
        return (raw // 3600) * 3600

    def run_once(self, *, now: int | None = None) -> dict[str, int]:
        """同步执行一轮（CPU/IO 密集，调用方负责放到线程池，见）。"""
        rolled = self._collector.rollup_hourly(since=self.rollup_since(now))
        # now 同样传播给清理：调用方注入 now（测试/手动触发）时清理必须跟随，
        # 否则这里会用真实时间删掉测试里的早期明细——测试输出依赖系统时钟。
        purged = self._collector.purge_expired(self.retention_days, now=now)
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

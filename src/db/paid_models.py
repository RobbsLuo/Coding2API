"""实测扣费反推「目录说免费、其实收费」的模型（免费优先档的校准数据源）。

**为什么需要它**：`credit_rate == 0` 来自模型目录（上游 `/v3/config` 的
`credits: "x0 credits"`），而目录只是**上游自报**。上游改计费策略时常常不改
目录——实测 2026-11-03 21:59 起 `codebuddy_intl/deepseek-v4.1-flash` 目录仍
写 x0，每次请求却实扣 0.01–7.39 积分（`credit_estimated=0`，即上游真值）。
调度器把 x0 当「免费渠道整体排前」（`scheduler._free_first_rank`），于是这一
条目录脏数据压过了 36h / 7 天两级到期窗口：officeace 三条凭证 15.8 小时内
到期的 100 积分包整夜闲置，流量全打到实际在扣费的 intl 上。

**判据只认硬证据**：`usage_events` 里 `credit > 0 AND credit_estimated = 0`
的行——上游明确报了一个正数扣费，不是本服务按价表推算的（TRAE 的推算值
`credit_estimated=1` 不参与，否则价表一调就可能把真免费模型误判成收费）。

**按 (渠道, 归一键) 聚合**：`usage_events.model` 存的是**对外**模型名，目录
存的是**上游原始 id**，两者大小写与 free 尾缀写法不同（`space-bunny-free`
vs `space-bunny`），故两边都过 `normalize_model_key` 再比。
"""

from __future__ import annotations

import time
from collections.abc import Callable

from ..provider.naming import normalize_model_key

# 观测窗口：只认最近这段时间内的实扣。太短（分钟级）会让校准在低峰期掉回
# 免费档（夜里没人用就没有观测行）；太长会让「上游改回免费」之后仍被当收费
# 晾着。24h 覆盖一整个用量周期，且与「免费额度」的日界同量级。
OBSERVATION_WINDOW_SECONDS = 24 * 3600
# 快照 TTL：选号是热路径，每次请求都做一次 `GROUP BY provider, model` 虽只
# 2–7ms（17 万行），但没必要。计费策略是小时级变化的，10 分钟足够新鲜。
SNAPSHOT_TTL_SECONDS = 600.0


class PaidModelRepository:
    """`usage_events` 里「上游明确扣过费」的 (渠道, 模型) → 实测均值。

    返回的均值供调度端覆盖目录里的 x0：它不参与排序比较（排序只区分
    `== 0` 与 `!= 0`），但把真实倍率回填比填一个哨兵值诚实——排查时
    「这个模型被当成 x0.14」比「被当成非零」有用得多。
    """

    def __init__(self, db, *, window: int = OBSERVATION_WINDOW_SECONDS,
                 ttl: float = SNAPSHOT_TTL_SECONDS,
                 now_fn: Callable[[], float] = time.monotonic) -> None:
        self._db = db
        self._window = window
        self._ttl = ttl
        self._now = now_fn
        self._snapshot: tuple[float, dict[tuple[str, str], float]] | None = None

    def invalidate(self) -> None:
        """丢弃快照（测试与「刚写完一条明细」的调用方用）。"""
        self._snapshot = None

    def observed_rates(self) -> dict[tuple[str, str], float]:
        """`{(provider, 归一键): 平均每次请求积分}`；窗口内无实扣时为空表。

        快照按 TTL 复用：过期才重新聚合。窗口起点用 `time.time()`（与
        `usage_events.ts` 同一时钟），TTL 用单调钟（不受系统改时间影响）。
        """
        now = self._now()
        snapshot = self._snapshot
        if snapshot is not None and now - snapshot[0] < self._ttl:
            return snapshot[1]
        rates = self._query()
        self._snapshot = (now, rates)
        return rates

    def _query(self) -> dict[tuple[str, str], float]:
        since = int(time.time()) - self._window
        rows = self._db.connect().execute(
            "SELECT provider, model, COALESCE(SUM(credit), 0) AS total, "
            "COUNT(*) AS count FROM usage_events "
            "WHERE ts >= ? AND credit > 0 AND credit_estimated = 0 "
            "GROUP BY provider, model", (since,)).fetchall()
        # 同一归一键可能来自多个原始写法（大小写 / free 尾缀）：累加后再平均，
        # 否则后到的写法会覆盖先到的，均值取决于 SQL 的返回顺序。
        buckets: dict[tuple[str, str], tuple[float, int]] = {}
        for row in rows:
            key = (row["provider"], normalize_model_key(row["model"]))
            total, count = buckets.get(key, (0.0, 0))
            buckets[key] = (total + float(row["total"] or 0.0),
                            count + int(row["count"] or 0))
        return {key: total / count
                for key, (total, count) in buckets.items() if count > 0}

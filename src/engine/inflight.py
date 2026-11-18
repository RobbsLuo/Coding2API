"""凭证在途租约：单账号并发请求数上限（进程内计数）。

同类项目（`workbuddy2api` 的 `pool.max_in_flight`、`Qoder-2API-Go` 的
`QODER_MAX_CONCURRENCY`）用「占满的号不再参与选号」来保护单账号：一个账号
被并发打进上游时先排限流/封号，而多配几个号本来就是为了分摊。本项目把这条
护栏做成**全局**开关（`MAX_INFLIGHT_PER_CREDENTIAL`，默认 0 = 不限），
选号前过滤 + 选中后登记、请求结束释放。**计数与上限解耦**：即便上限为 0
（不限并发），租约仍照常登记——在途数是调度器的「最少连接」负载键
（`Candidate.inflight`），并发请求靠它摊到空闲号，而不是全部压到排序确定的
同一个号上。

**为什么独立于 `tasks/pacer.py` 的 `max_concurrency`**：pacer 的桶是按渠道
分的、且只覆盖走 pacer 的渠道；这里是**跨渠道的凭证级**上限，语义是「同一个
账号别被我方并发压垮」，与渠道节流正交。

**为什么同步（不用 asyncio.Lock）**：`acquire` / `release` / `count` 只在
executor 的事件循环里被调用，且「选号 → 登记」之间没有任何 `await`（选号、
压缩、创建上游迭代器都是同步的），单线程下不会交错，普通 dict 就够。
引入异步锁反而要求 `_selectable` 变成 async，把一个纯过滤函数染上异步。

**租约 TTL 兜底**：流式长请求理论上靠 `finally` 释放；但生成器被 GC、异常
路径漏走释放等情况下租约会滞留，慢慢把号占满。超过 `LEASE_TTL_SECONDS` 的
租约在计数时视为过期并回收——宁可偶尔多放一个请求进来，也不能让号被永久
占住（那等于静默把凭证踢出池子）。
"""

from __future__ import annotations

import time
from collections.abc import Callable

__all__ = ["LEASE_TTL_SECONDS", "InflightGuard", "InflightTracker"]

# 租约兜底存活时间（秒）：正常路径由 executor 的 finally 释放，这里只兜
# 「漏释放」。取 600s 与 `UPSTREAM_COMPLETE_TIMEOUT_SECONDS` 同量级——
# 比任何正常请求都长，又不会久到号被占死。
LEASE_TTL_SECONDS = 600.0


class InflightTracker:
    """`credential_id → 活跃租约时刻列表` 的进程内计数。

    每个租约只记**开始时刻**，不做请求标识绑定：同一凭证的多个租约彼此等价，
    释放任意一个即可（先入先出）。这样调用方只需在请求两侧各调一次，不必
    在轮换/回退的多层 `finally` 里传递租约句柄。
    """

    def __init__(self, *, ttl: float = LEASE_TTL_SECONDS,
                 now: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._now = now
        self._leases: dict[str, list[float]] = {}

    def _live(self, credential_id: str, now: float) -> list[float]:
        """**原地**回收过期租约并返回活跃列表（空则顺手删键，避免无界增长）。"""
        leases = self._leases.get(credential_id)
        if not leases:
            return []
        live = [started for started in leases if now - started < self._ttl]
        if live:
            self._leases[credential_id] = live
        else:
            self._leases.pop(credential_id, None)
        return live

    def count(self, credential_id: str) -> int:
        """当前活跃租约数（先回收过期租约）。"""
        return len(self._live(credential_id, self._now()))

    def total(self) -> int:
        """全池活跃租约总数（`/metrics` 的在途 gauge）。

        顺带回收全部过期租约：这个方法是唯一会遍历所有凭证的入口，正好
        承担「久不访问的凭证也要清理」的职责，避免 `_leases` 无界增长。
        """
        now = self._now()
        return sum(len(self._live(cid, now)) for cid in list(self._leases))

    def acquire(self, credential_id: str) -> None:
        """登记一次在途：顺手回收过期租约，避免「漏释放攒满后永占」。"""
        now = self._now()
        live = self._live(credential_id, now)
        live.append(now)
        self._leases[credential_id] = live

    def release(self, credential_id: str) -> None:
        """释放一个租约（先进先出）；没有租约时静默忽略（幂等）。"""
        leases = self._leases.get(credential_id)
        if not leases:
            return
        leases.pop()
        if not leases:
            self._leases.pop(credential_id, None)


class InflightGuard:
    """选号前的在途过滤 + 选中后的租约登记。

    上限从 `limit_source()` 现读（热更）；≤0 表示**不限并发**（`blocked` 恒
    False），但**仍照常登记租约**——在途计数是调度器的「最少连接」负载键
    （`Candidate.inflight`），关掉上限时它照样要能反映实时负载，否则并发请求
    会全部落到排序确定的同一个号上（见 `Scheduler.select`）。租约登记是纯
    内存字典操作，成本可忽略。
    """

    def __init__(self, limit_source: Callable[[], int],
                 *, tracker: InflightTracker | None = None) -> None:
        self._limit_source = limit_source
        self._tracker = tracker or InflightTracker()

    @property
    def limit(self) -> int:
        """当前上限；≤0 归一为 0（不限）。"""
        value = int(self._limit_source())
        return value if value > 0 else 0

    def count(self, credential_id: str) -> int:
        return self._tracker.count(credential_id)

    def total(self) -> int:
        """全池在途租约总数（`/metrics` 用）。"""
        return self._tracker.total()

    def blocked(self, credential_id: str) -> bool:
        """该凭证的在途数是否已达上限（不限时为 False）。"""
        limit = self.limit
        if limit <= 0:
            return False
        return self._tracker.count(credential_id) >= limit

    def acquire(self, credential_id: str) -> None:
        self._tracker.acquire(credential_id)

    def release(self, credential_id: str) -> None:
        # 不限时也登记过，这里必须对称释放；tracker 幂等，多余释放零成本
        self._tracker.release(credential_id)

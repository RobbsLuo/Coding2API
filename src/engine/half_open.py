"""冷却到期半开探测：到期后先放行**一个**请求，成功再全量回池。

原先冷却一到期，`Candidate.is_selectable` 立刻为真，所有并发请求会同时压到
同一个刚解冻的号上。上游刚解除的往往只是「限流窗口过了」——账号本身仍然
脆弱，一整波并发会把它再次打进限流，形成「冷却 60s → 到期 → 再撞 → 再冷却」
的锯齿。

半开把恢复过程分成两段：

- **探测中**：只允许 1 个在途请求打这个号；其余请求看见它被占用就换别的号
  （或在该号是唯一候选时按「无可用」返回，由客户端重试）。
- **已恢复**：探测请求成功 → 本进程内记住「这个 key 已经探通」，后续请求
  直接放行，不再逐个排队。失败的话上游会写一条新的冷却（`note_error`），
  探测位立即释放，下次到期再探。

**为什么是进程内集合**：冷却值本身在库里，但「谁正在探测」是瞬时状态。
单进程部署下内存即准；多进程各自探一次也不过是 1 次/进程，仍远低于全量
回池。重启丢失只会让每个号多探一次。

**TTL 兜底**：探测请求若在 `finally` 之前被 GC（生成器泄漏），探测位会滞留。
超过 `PROBE_TTL_SECONDS` 的占用视为过期——宁可偶尔多放一个请求，也不能让
号被永久卡在「探测中」。
"""

from __future__ import annotations

import time
from collections.abc import Callable

__all__ = ["PROBE_TTL_SECONDS", "HalfOpenGuard"]

# 探测位兜底存活时间（秒）：与请求整体超时同量级，正常路径必然更早释放。
PROBE_TTL_SECONDS = 600.0
# 「已探通」结论的存活时间（秒）：过了这段时间重新按半开恢复。探测成功只
# 证明「这一刻上游接受」，不能永久免除半开——冷却过 2h 的号探通一次就永久
# 免检，等于把半开关掉了。取 120s 覆盖「到期瞬间的并发洪峰」这一核心场景，
# 又不会把刚恢复的号长期排除在保护之外。
RECOVERED_TTL_SECONDS = 120.0


class HalfOpenGuard:
    """`(credential_id, scope)` 探测位：同一冷却作用域同时只有一个探测。

    `scope` 为空串表示账号级冷却；非空表示模型级冷却（用上游原始模型名，
    与 `credential_model_cooldowns` 的键一致）——两者是独立的作用域，
    账号级探测进行中不该挡住另一个模型的探测。
    """

    def __init__(self, *, ttl: float = PROBE_TTL_SECONDS,
                 recovered_ttl: float = RECOVERED_TTL_SECONDS,
                 now: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._recovered_ttl = recovered_ttl
        self._now = now
        # key → 探测开始时刻
        self._probing: dict[tuple[str, str], float] = {}
        # 已探通的 key → 探通时刻（见 RECOVERED_TTL_SECONDS）
        self._recovered: dict[tuple[str, str], float] = {}

    def _live(self, key: tuple[str, str]) -> bool:
        started = self._probing.get(key)
        if started is None:
            return False
        if self._now() - started >= self._ttl:
            self._probing.pop(key, None)
            return False
        return True

    def acquired(self, key: tuple[str, str]) -> bool:
        """是否已有探测在途（含未过 TTL 的占用）。"""
        return self._live(key)

    def recovered(self, key: tuple[str, str]) -> bool:
        """本进程内是否已探通该作用域（探通后不再逐个排队）。"""
        at = self._recovered.get(key)
        if at is None:
            return False
        if self._now() - at >= self._recovered_ttl:
            self._recovered.pop(key, None)
            return False
        return True

    def acquire(self, key: tuple[str, str]) -> bool:
        """占用探测位；已占用返回 False（调用方应换号或视为无可用）。"""
        if self._live(key):
            return False
        self._probing[key] = self._now()
        return True

    def confirm(self, key: tuple[str, str]) -> None:
        """探测成功：标记已恢复并释放探测位。"""
        self._probing.pop(key, None)
        self._recovered[key] = self._now()

    def release(self, key: tuple[str, str]) -> None:
        """探测失败/中断：只释放探测位，不标记恢复（上游已写新冷却）。"""
        self._probing.pop(key, None)

    def reset(self, credential_id: str, scope: str) -> None:
        """该作用域又记了一次错误：清掉「已恢复」结论，下次到期重新半开。"""
        self._recovered.pop((credential_id, scope), None)

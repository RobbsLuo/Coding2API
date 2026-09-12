"""全局随机间隔节流器（PACER_MIN/MAX_SECONDS，PROPOSAL §8）。"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable


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

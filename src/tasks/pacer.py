"""全局随机间隔节流器（PACER_MIN/MAX_SECONDS，PROPOSAL §8）。"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable

from ..config import live


class Pacer:
    """全局随机间隔节流器：相邻上游请求之间至少间隔 min..max 秒。"""

    def __init__(self, min_seconds: float, max_seconds: float,
                 *, sleep: Callable[[float], Awaitable[None]] | None = None,
                 now: Callable[[], float] | None = None) -> None:
        # 上下限可热更（B3.2）：存取值器，每次计间隔读当前值。校验必须放在
        # 读取时（而不是只在构造时）：覆盖层把下限改到上限之上时，构造成员
        # 早已完成，只能在真正用时拒绝，否则会算出负间隔。
        self._min = live(min_seconds)
        self._max = live(max_seconds)
        self._validate()                     # 构造时也校验一次：尽早失败
        self._sleep = sleep or asyncio.sleep
        self._now = now or time.monotonic
        self._last_started: float | None = None
        self._lock = asyncio.Lock()
        self._random = random.Random(0)          # 确定性：测试可复现

    def _validate(self) -> tuple[float, float]:
        low, high = self.min_seconds, self.max_seconds
        if low < 0 or high < 0:
            raise ValueError("pacer bounds must be non-negative")
        if low > high:
            raise ValueError("pacer min must not exceed max")
        return low, high

    @property
    def min_seconds(self) -> float:
        return float(self._min())

    @property
    def max_seconds(self) -> float:
        return float(self._max())

    @property
    def disabled(self) -> bool:
        return self.min_seconds == 0 and self.max_seconds == 0

    def next_interval(self) -> float:
        low, high = self._validate()
        if low == 0 and high == 0:
            return 0.0
        if low == high:
            return low
        return self._random.uniform(low, high)

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

"""上游请求节流器（PACER_MIN/MAX_SECONDS，PROPOSAL §8）。

两种模式，差别只在「同一时刻允不允许并发」：

- **严格模式**（默认）：相邻请求之间至少间隔 min..max 秒，单锁串行化。
  后台任务（签到/刷新/探测/成长）在各自循环里一次一个，用它把跨任务、
  跨凭证的请求也错开，避免同一上游被并发打爆。
- **并发模式**（`allow_concurrent=True`，聊天）：按桶（渠道 + 凭证身份）
  计数在途请求。同桶已有请求在途时，新请求直接放行——这正是「同渠道同模型
  并发不并行」的修复点：旧实现用一把全局锁把所有相邻请求排队，第 2、3 个
  实测被推到 +5s、+10s。只有当桶里没有在途请求、且距上次请求开始不足最小
  间隔时才补足等待，用来错开「一个接一个」的顺序连发。按凭证分桶而非全局
  单桶：一个账号在途不该拖住另一个账号（频率风控是账号级的）。
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from collections.abc import Awaitable, Callable

from ..config import live


def stable_key(provider: str, identity: str | None) -> str:
    """聊天节流桶键：渠道 + 身份摘要；身份缺失时退化为「该渠道单桶」。

    用摘要而非原文，避免把凭证 token 直接塞进节流器内部字典；同一渠道的
    不同账号落到不同桶、互不阻塞。
    """
    if not identity:
        return provider
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"{provider}:{digest}"


class Pacer:
    """上游请求节流器（严格 / 并发两种模式，见模块 docstring）。"""

    def __init__(self, min_seconds: float, max_seconds: float, *,
                 allow_concurrent: bool = False,
                 sleep: Callable[[float], Awaitable[None]] | None = None,
                 now: Callable[[], float] | None = None) -> None:
        # 上下限可热更（B3.2）：存取值器，每次计间隔读当前值。校验必须放在
        # 读取时（而不是只在构造时）：覆盖层把下限改到上限之上时，构造成员
        # 早已完成，只能在真正用时拒绝，否则会算出负间隔。
        self._min = live(min_seconds)
        self._max = live(max_seconds)
        self._validate()                     # 构造时也校验一次：尽早失败
        self._sleep = sleep or asyncio.sleep
        self._now = now or time.monotonic
        self._random = random.Random(0)          # 确定性：测试可复现
        self._allow_concurrent = allow_concurrent
        self._lock = asyncio.Lock()
        # 严格模式：全局单桶
        self._last_started: float | None = None
        # 并发模式：桶 → 上次（预留）开始时刻 / 在途计数
        self._bucket_started: dict[str, float] = {}
        self._inflight: dict[str, int] = {}

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

    @property
    def allow_concurrent(self) -> bool:
        return self._allow_concurrent

    def next_interval(self) -> float:
        low, high = self._validate()
        if low == 0 and high == 0:
            return 0.0
        if low == high:
            return low
        return self._random.uniform(low, high)

    async def wait_turn(self, key: str | None = None) -> None:
        """取得一个节流 turn；只有即将真正调用上游时才应调用。

        并发模式下返回即视为「已占用一个在途名额」，调用方必须在请求结束后
        用同一 key 调 `release` 归还，否则该桶会被当成永远有请求在途而失去
        节流（见模块 docstring）。
        """
        if self.disabled:
            return
        if not self._allow_concurrent:
            async with self._lock:
                interval = self.next_interval()
                if self._last_started is not None:
                    elapsed = self._now() - self._last_started
                    remaining = interval - elapsed
                    if remaining > 0:
                        await self._sleep(remaining)
                self._last_started = self._now()
            return
        bucket = key or ""
        async with self._lock:
            if self._inflight.get(bucket, 0) > 0:
                # 同桶已有请求在途：并发放行，不再排队
                self._inflight[bucket] += 1
                return
            current = self._now()
            interval = self.next_interval()
            started = self._bucket_started.get(bucket)
            remaining = 0.0
            if started is not None:
                remaining = interval - (current - started)
                if remaining < 0:
                    remaining = 0.0
            # 预留开始时刻：睡醒后才是真正开始，后续同桶请求据此计算间隔
            self._bucket_started[bucket] = current + remaining
            self._inflight[bucket] = 1
        if remaining > 0:
            await self._sleep(remaining)

    def release(self, key: str | None = None) -> None:
        """并发模式归还一个在途名额；严格模式与多余的 release 都是空操作。"""
        if not self._allow_concurrent:
            return
        bucket = key or ""
        count = self._inflight.get(bucket, 0)
        if count > 1:
            self._inflight[bucket] = count - 1
        elif count == 1:
            del self._inflight[bucket]

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

并发模式还可叠加**按桶在途上限**（`max_concurrency`）：达到上限时新请求
不直接放行，而是等到有请求 `release` 让出名额。上游对「并发会话数」有硬
上限（CodeArts 每账号 3），无上限放行会把第 4 个起全部打成 400 并发超限。
0/None 表示不限（保持旧行为）。

**`max_concurrency > 0` 时 `release` 必须与 `wait_turn` 严格配对。**
provider 在 `stream_chat` 的 `finally` 里归还名额，而 `async for ... break`
不会关闭 async generator（CPython 只在耗尽 / 显式 `aclose()` / GC 的
asyncgen finalizer 时才跑 `finally`）。于是流内错误换号重试会让名额推迟归还
甚至永久丢失：桶停在满载，新请求在 `wait_turn` 无限阻塞——表现为
「用了三次就限制」而非「并发三」。因此所有提前结束消费上游流的地方都必须
显式关闭它，见 `provider.base.aclose_stream`。
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
                 max_concurrency: int | Callable[[], int] = 0,
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
        # 按桶在途上限（0 = 不限）：CodeArts 每账号并发会话数有限，超限即 400。
        # 存取值器而非快照：上限可热更。用同步的 Event.set 唤醒等待者而非
        # 信号量——信号量创建后无法安全改容，热更上限时会给错名额。
        if not callable(max_concurrency) and max_concurrency < 0:
            raise ValueError("pacer max_concurrency must be non-negative")
        self._max_concurrency = live(max_concurrency)
        self._lock = asyncio.Lock()
        # 严格模式：全局单桶
        self._last_started: float | None = None
        # 并发模式：桶 → 上次（预留）开始时刻 / 在途计数
        self._bucket_started: dict[str, float] = {}
        self._inflight: dict[str, int] = {}
        # 并发模式 + 上限：名额让出时置位，挂起的请求醒来重查是否有空位
        self._wake = asyncio.Event()

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

    @property
    def max_concurrency(self) -> int:
        """当前在途上限；0/负 = 不限（每次现读，支持热更）。"""
        limit = int(self._max_concurrency())
        return limit if limit > 0 else 0

    def next_interval(self) -> float:
        low, high = self._validate()
        if low == 0 and high == 0:
            return 0.0
        if low == high:
            return low
        return self._random.uniform(low, high)

    def _capped(self) -> bool:
        """并发模式且配了在途上限——只有这种组合需要排队等名额。"""
        return self._allow_concurrent and self.max_concurrency > 0

    def _reserve(self, bucket: str) -> float:
        """**调用方须持 `_lock`**：登记一次在途并返回本次需补足的间隔秒数。

        同桶已有在途 → 并发放行、无需补间隔（返回 0）；否则预留开始时刻
        （睡醒后才是真正开始），后续同桶请求据此算间隔。
        """
        if self._inflight.get(bucket, 0) > 0:
            self._inflight[bucket] += 1
            return 0.0
        current = self._now()
        interval = self.next_interval()
        started = self._bucket_started.get(bucket)
        remaining = 0.0
        if started is not None:
            remaining = interval - (current - started)
            if remaining < 0:
                remaining = 0.0
        self._bucket_started[bucket] = current + remaining
        self._inflight[bucket] = 1
        return remaining

    async def wait_turn(self, key: str | None = None) -> None:
        """取得一个节流 turn；只有即将真正调用上游时才应调用。

        并发模式下返回即视为「已占用一个在途名额」，调用方必须在请求结束后
        用同一 key 调 `release` 归还，否则该桶会被当成永远有请求在途而失去
        节流（见模块 docstring）。配了 `max_concurrency` 时，名额满会让新
        请求在此挂起，直到有 `release` 让位。
        """
        if self.disabled and not self._capped():
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
        remaining = 0.0
        reserved = False
        try:
            while True:
                async with self._lock:
                    limit = self.max_concurrency
                    if not limit or self._inflight.get(bucket, 0) < limit:
                        remaining = self._reserve(bucket)
                        reserved = True
                        break
                    # 名额已满：清事件后到锁外等待，release 会 set 唤醒。
                    self._wake.clear()
                await self._wake.wait()
            if remaining > 0:
                await self._sleep(remaining)
        except BaseException:
            # 未成功「占用」就退出（取消 / 间隔校验异常）：把名额还回去，
            # 否则该桶的名额会永久少一个（最终把渠道卡死）。
            if reserved:
                self.release(bucket)
            raise

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
        else:
            return                            # 多余释放：不虚增名额
        self._wake.set()                      # 唤醒可能正等这个名额的请求

"""登录限流：全局 / IP / 用户名三级固定窗口 + PBKDF2 并发上限（PROPOSAL §8）。

威胁模型：登录接口可被脚本爆破；PBKDF2（600k 迭代）是 CPU 密集操作，
并发请求能打满默认线程池（asyncio.to_thread 默认 min(32, cpu+4) 线程）。

对策：三级失败窗口把爆破频率压到可用性以下；信号量限制同时进行的
哈希数，防 CPU 耗尽。窗口是进程内存态，单实例部署够用。
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field


class ThrottledError(Exception):
    """触发限流：上层映射 429，不向客户端透露命中哪一级。"""


@dataclass(frozen=True)
class ThrottleLimits:
    window_seconds: float = 60.0
    max_global: int = 40  # 60s 内全局限流失败次数
    max_per_ip: int = 8  # 60s 内单 IP 失败次数
    max_per_user: int = 5  # 60s 内单用户名失败次数


@dataclass
class LoginThrottle:
    """固定窗口计数（deque 时间戳）。同一事件循环内使用，无跨线程竞争。"""

    limits: ThrottleLimits = field(default_factory=ThrottleLimits)
    _events: dict[tuple[str, str], deque[float]] = field(
        default_factory=lambda: defaultdict(deque), init=False
    )
    # PBKDF2 并发上限：verify 是 CPU 密集同步调用，用信号量限制同时哈希数。
    # Python 3.10+ 信号量在首次 await 时绑定当前运行循环，无需显式传 loop。
    _pbkdf2: asyncio.Semaphore = field(init=False)

    def __post_init__(self) -> None:
        self._pbkdf2 = asyncio.Semaphore(4)

    def _prune(self, key: tuple[str, str], now: float) -> None:
        queue = self._events[key]
        cutoff = now - self.limits.window_seconds
        while queue and queue[0] <= cutoff:
            queue.popleft()

    def check(self, *, ip: str, username: str) -> None:
        """任一窗口超限即抛 ThrottledError；调用方应在校验密码前调用。"""
        now = time.monotonic()
        self._prune(("g", ""), now)
        self._prune(("i", ip), now)
        self._prune(("u", username), now)
        if len(self._events[("g", "")]) >= self.limits.max_global:
            raise ThrottledError()
        if len(self._events[("i", ip)]) >= self.limits.max_per_ip:
            raise ThrottledError()
        if username and len(self._events[("u", username)]) >= self.limits.max_per_user:
            raise ThrottledError()

    def record_failure(self, *, ip: str, username: str) -> None:
        now = time.monotonic()
        self._events[("g", "")].append(now)
        self._events[("i", ip)].append(now)
        if username:
            self._events[("u", username)].append(now)

    def record_success(self, *, username: str) -> None:
        """成功登录清空该用户名的失败计数，不误伤同 IP 他人。"""
        if username:
            self._events.pop(("u", username), None)

    async def verify(self, func, *args):
        """限并发地执行同步 verify（asyncio.to_thread 线程池 + 信号量）。"""
        async with self._pbkdf2:
            return await asyncio.to_thread(func, *args)

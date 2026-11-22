"""登录限流：IP / 用户名两级固定窗口 + PBKDF2 并发上限（PROPOSAL §6）。

威胁模型：登录接口可被脚本爆破；PBKDF2（600k 迭代）是 CPU 密集操作，
并发请求能打满默认线程池（asyncio.to_thread 默认 min(32, cpu+4) 线程）。

对策：两级失败窗口把爆破频率压到可用性以下；信号量限制同时进行的
哈希数，防 CPU 耗尽。**刻意不做全局拒绝**（见 `check` 的说明）：全局窗口
跨来源共享，按它拒绝等于给未认证攻击者一个「一次打满、全站登不上」的开关。
窗口是进程内存态，单实例部署够用。
"""

from __future__ import annotations

import asyncio
import ipaddress
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field


def _is_loopback(ip: str) -> bool:
    """本机回环地址（含 IPv6 `::1`）；解析不了按「非本机」处理。"""
    try:
        return ipaddress.ip_address((ip or "").strip()).is_loopback
    except ValueError:
        return False


class ThrottledError(Exception):
    """触发限流：上层映射 429，不向客户端透露命中哪一级。"""


@dataclass(frozen=True)
class ThrottleLimits:
    window_seconds: float = 60.0
    # 全局窗口只做观测与 CPU 预算参考，**不再用于拒绝**：未认证攻击者能把
    # 它打满，一旦拒绝就是「任何人都登不上管理台」的远程 DoS。CPU 消耗已由
    # `_pbkdf2` 信号量（并发 4）兜住，爆破频率由 IP / 用户名两级窗口限制。
    max_global: int = 40  # 60s 内全局失败次数（仅观测）
    max_per_ip: int = 8  # 60s 内单 IP 失败次数
    max_per_user: int = 5  # 60s 内单用户名失败次数

@dataclass
class LoginThrottle:
    """固定窗口计数（deque 时间戳）。同一事件循环内使用，无跨线程竞争。"""

    limits: ThrottleLimits = field(default_factory=ThrottleLimits)
    _events: dict[tuple[str, str], deque[float]] = field(
        default_factory=lambda: defaultdict(deque), init=False
    )
    # 失败键数量上限：用户名由客户端任意填写，不设界等于内存 DoS 入口。
    _MAX_KEYS: int = field(default=4096, init=False, repr=False)
    # PBKDF2 并发上限：verify 是 CPU 密集同步调用，用信号量限制同时哈希数。
    # Python 3.10+ 信号量在首次 await 时绑定当前运行循环，无需显式传 loop。
    _pbkdf2: asyncio.Semaphore = field(init=False)

    def __post_init__(self) -> None:
        self._pbkdf2 = asyncio.Semaphore(4)

    def _prune(self, key: tuple[str, str], now: float) -> None:
        queue = self._events.get(key)
        if not queue:
            return
        cutoff = now - self.limits.window_seconds
        while queue and queue[0] <= cutoff:
            queue.popleft()

    def _evict_over_capacity(self, now: float) -> None:
        """失败键数量超上限时清理，避免用户名维度无界增长（内存 DoS）。

        攻击者用任意用户名反复失败会为每个名字建一个 deque；`record_failure`
        是唯一的开键入口，这里在开键后清一次：先删整个窗口都没再命中的键，
        仍超上限再按「最近一次失败时间」淘汰最旧的（全局键始终保留）。

        **已触发限流的键排最后淘汰**：否则攻击者只要先用目标用户名撞满
        `max_per_user`，再灌一批唯一用户名把容量挤爆，就能通过淘汰把目标
        计数清零、继续爆破。把这类键压到候选末尾后，挤掉它需要先攒满
        `_MAX_KEYS` 个同样被限流的用户名，在 per-IP 窗口下不可达。
        """
        if len(self._events) <= self._MAX_KEYS:
            return
        cutoff = now - self.limits.window_seconds
        stale = [k for k, q in self._events.items()
                 if k != ("g", "") and (not q or q[-1] <= cutoff)]
        for key in stale:
            del self._events[key]
        excess = len(self._events) - self._MAX_KEYS
        if excess <= 0:
            return
        candidates = [k for k in self._events if k != ("g", "")]
        candidates.sort(key=lambda k: (self._at_limit(k),
                                       self._events[k][-1] if self._events[k] else 0.0))
        for key in candidates[:excess]:
            del self._events[key]

    def _at_limit(self, key: tuple[str, str], *, forwarded: bool = False) -> bool:
        """该键是否已触发限流（淘汰时降级为最后候选）。

        `forwarded` 与 `_ip_blocked` 同口径：回环键只有在「带 XFF、正常计数」
        时才算已限流。淘汰路径没有请求上下文，取默认值 False（本机直连的
        回环桶永不用于拒绝，淘汰它不损失任何防护）。
        """
        kind, name = key
        if kind == "u":
            return bool(name) and len(self._events[key]) >= self.limits.max_per_user
        if kind == "i":
            return self._ip_blocked(name, forwarded=forwarded)
        return False

    def check(self, *, ip: str, username: str, forwarded: bool = False) -> None:
        """IP / 用户名任一窗口超限即抛 ThrottledError；调用方应在校验密码前调用。

        `forwarded`：请求带了 `X-Forwarded-For`（说明有反代，真实来源不是本机），
        此时回环对端不再豁免 IP 窗口。

        **不按全局窗口拒绝**：该窗口由所有来源共享，未认证攻击者每人贡献几条
        就能把它打满，一旦据此拒绝，攻击者即刻让全站登录返回 429（管理台是
        唯一的止血入口，等于把修复通道也一起关掉）。全局计数仅保留用于观测。
        """
        now = time.monotonic()
        self._prune(("g", ""), now)
        self._prune(("i", ip), now)
        self._prune(("u", username), now)
        if self._ip_blocked(ip, forwarded=forwarded):
            raise ThrottledError()
        if username and len(self._events.get(("u", username)) or ()) >= self.limits.max_per_user:
            raise ThrottledError()

    def check_transport_windows(self, *, ip: str, forwarded: bool = False) -> None:
        """只查 IP 窗口（哈希前调用）。

        威胁模型：PBKDF2-600k 是 CPU 密集操作，攻击者用无效尝试就能占满
        线程池。IP 窗口完全由攻击者自己触发，因此在哈希前检查，把「单来源
        无限烧 CPU」压到 max_per_ip/窗口；用户名窗口故意不在此处检查，因为
        它可能被第三方（他人输错自己的用户名）抬高，放在哈希后能保证
        「密码正确就一定放行」。

        全局窗口同理不在哈希前拒绝：它跨来源共享，按它拒绝等于给未认证攻击者
        一个「一次打满、全站登不上」的开关。
        """
        now = time.monotonic()
        self._prune(("g", ""), now)
        self._prune(("i", ip), now)
        if self._ip_blocked(ip, forwarded=forwarded):
            raise ThrottledError()

    def _ip_blocked(self, ip: str, forwarded: bool = False) -> bool:
        """该 IP 的失败窗口是否已满。

        **回环豁免只针对本机直连**：反代/隧道部署下若 XFF 未被采信，所有请求
        的对端都是同一个 127.0.0.1，限它等于把所有人塞进同一个桶。但带了
        `X-Forwarded-For` 就说明前面有反代、真实来源不是本机（docker-compose
        端口绑 127.0.0.1 + TRUST_PROXY=false 时 peer 恒为回环），此时必须
        正常计数，否则 IP 窗口完全失效、只剩用户名窗口可跨名撞库。
        """
        if _is_loopback(ip) and not forwarded:
            return False
        return len(self._events.get(("i", ip)) or ()) >= self.limits.max_per_ip

    def record_failure(self, *, ip: str, username: str) -> None:
        now = time.monotonic()
        self._events[("g", "")].append(now)
        self._events[("i", ip)].append(now)
        if username:
            self._events[("u", username)].append(now)
        self._evict_over_capacity(now)

    def record_success(self, *, username: str) -> None:
        """成功登录清空该用户名的失败计数，不误伤同 IP 他人。"""
        if username:
            self._events.pop(("u", username), None)

    async def verify(self, func, *args):
        """限并发地执行同步 verify（asyncio.to_thread 线程池 + 信号量）。"""
        async with self._pbkdf2:
            return await asyncio.to_thread(func, *args)

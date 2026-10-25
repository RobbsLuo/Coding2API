"""OAuth pending 登录会话的共享登记表。

四个渠道（CodeBuddy / Qoder / CodeArts / OfficeAce）的 `AuthStateStore` 曾各写
一份，其中 `owner` / `consume` / `cancel` / `cleanup` 逐字相同。差异只在
`begin` 的载荷（上游 state / verifier+nonce / LoginSession）与取值方法名，
故基类只做「登记 + 归属校验 + 消费 + 过期清理」，载荷由泛型承载。

对外 `state` 一律是**本地生成的 reservation**，不是上游签发的 state：上游
state 会写进授权链接（浏览器可见），拿它当会话键等于让人凭 URL 消费别人的
登录。
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass


@dataclass(slots=True)
class Reservation[T]:
    """一条 pending 登录：归属人 + 创建时刻 + 渠道私有载荷。"""

    username: str
    created_at: int
    payload: T


class AuthStateStore[T]:
    """state 的归属校验、消费与过期清理。

    `max_pending` 非 None 时在 `begin` 里顶掉最老的会话：攒了太多没登完的
    就拒绝会让人以为功能坏了，而每条 pending 会话都在轮询上游，留一堆只是
    浪费（OfficeAce 的实测需求）。
    """

    def __init__(self, ttl_seconds: int, *,
                 max_pending: int | None = None) -> None:
        self._ttl = ttl_seconds
        self._max_pending = max_pending
        self._entries: dict[str, Reservation[T]] = {}

    def begin(self, username: str, payload: T, now: int | None = None) -> str:
        self.cleanup(now)
        if self._max_pending is not None:
            while len(self._entries) >= self._max_pending:
                oldest = min(self._entries,
                             key=lambda key: self._entries[key].created_at)
                del self._entries[oldest]
        reservation = secrets.token_urlsafe(18)
        self._entries[reservation] = Reservation(
            username=username,
            created_at=int(now if now is not None else time.time()),
            payload=payload)
        return reservation

    def owner(self, auth_state: str, username: str, now: int | None = None) -> bool:
        """归属校验，**含 TTL**：过期 reservation 不得再轮询/重放。

        `cleanup()` 只在 `begin()` 时触发；若 owner 不判过期，一个过期的
        state 在下次 begin 之前会一直可被轮询（每次轮询都打上游）。
        """
        entry = self._entries.get(auth_state)
        if entry is None or entry.username != username:
            return False
        current = int(now if now is not None else time.time())
        if current - entry.created_at >= self._ttl:
            del self._entries[auth_state]          # 惰性清理，避免字典堆积
            return False
        return True

    def payload(self, auth_state: str, username: str,
                now: int | None = None) -> T | None:
        """归属校验通过才返回载荷（调用方据此轮询上游 / 取私钥）。"""
        if not self.owner(auth_state, username, now):
            return None
        return self._entries[auth_state].payload

    def reservation(self, auth_state: str, username: str,
                    now: int | None = None) -> Reservation[T] | None:
        """归属校验通过才返回整条登记（含归属人 / 创建时刻 / 载荷）。"""
        if not self.owner(auth_state, username, now):
            return None
        return self._entries[auth_state]

    def consume(self, auth_state: str, username: str, now: int | None = None) -> bool:
        """消费：成功后该 state 不可再次轮询或重放。"""
        if not self.owner(auth_state, username, now):
            return False
        del self._entries[auth_state]
        return True

    def cancel(self, auth_state: str, username: str, now: int | None = None) -> bool:
        return self.consume(auth_state, username, now)

    def cleanup(self, now: int | None = None) -> None:
        current = int(now if now is not None else time.time())
        for key in [k for k, v in self._entries.items()
                    if current - v.created_at >= self._ttl]:
            del self._entries[key]

    def __len__(self) -> int:
        return len(self._entries)


def pkce_pair(verifier: str | None = None) -> tuple[str, str]:
    """PKCE `(code_verifier, code_challenge)`；challenge = BASE64URL(SHA256(v))。"""
    value = verifier or secrets.token_urlsafe(32)
    digest = hashlib.sha256(value.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return value, challenge

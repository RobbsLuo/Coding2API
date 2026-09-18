"""会话粘性：同一对话的多轮请求固定使用同一凭证。

OpenAI Chat Completions 本身无会话概念，但客户端（agent/CLI）的「对话」
表现为消息数组的前缀延续：下一轮的 messages 以上一轮的完整 messages
为前缀再追加。据此以增量前缀指纹做匹配，命中则请求固定复用原凭证，
不再按到期积分/健康度重排——对话进行中换号会触发上游风控并丢掉
上游侧的提示词缓存。

指纹链里掺入用户名，防止不同 API 用户的相同消息数组串到同一凭证。
TTL 内没有后续轮次即视为对话结束，条目过期；条目数有上限，超量先淘汰
过期条目、再按最旧淘汰。纯内存即可：重启后丢粘性只影响一轮选号。
"""

from __future__ import annotations

import hashlib
import json
import time


def _message_digest(message: object) -> bytes:
    """单条消息的规范哈希：键序归一，杜绝 JSON 键序差异造成假失配。"""
    text = json.dumps(message, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).digest()


class ConversationAffinity:
    """前缀指纹 → 凭证的粘性表（单事件循环内使用，无需加锁）。"""

    def __init__(self, *, ttl_seconds: int, max_entries: int = 512) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        # fingerprint_hex → (credential_id, expires_at)；dict 保持插入序，供淘汰
        self._entries: dict[str, tuple[str, float]] = {}

    def pin_for(self, messages: list[dict], username: str, *,
                now: float | None = None) -> str | None:
        """最长前缀匹配的粘性凭证；无匹配（含禁用/过期）返回 None。

        逐条累积指纹并在每一步查表：上一轮请求存的指纹正是本轮某个前缀
        的指纹，最长匹配者即本对话最近一轮实际使用的凭证。
        """
        if self.ttl_seconds <= 0 or not messages:
            return None
        current = now if now is not None else time.monotonic()
        pinned: str | None = None
        chain = hashlib.sha256(username.encode("utf-8")).digest()
        for message in messages:
            chain = hashlib.sha256(chain + _message_digest(message)).digest()
            entry = self._entries.get(chain.hex())
            if entry is None:
                continue
            credential_id, expires_at = entry
            if expires_at <= current:
                del self._entries[chain.hex()]
                continue
            pinned = credential_id
            self._touch(chain.hex(), credential_id, current)
        return pinned

    def remember(self, messages: list[dict], username: str, credential_id: str, *,
                 now: float | None = None) -> None:
        """记录本轮请求的完整指纹 → 实际服务的凭证（成功后调用）。"""
        if self.ttl_seconds <= 0 or not messages:
            return
        current = now if now is not None else time.monotonic()
        chain = hashlib.sha256(username.encode("utf-8")).digest()
        for message in messages:
            chain = hashlib.sha256(chain + _message_digest(message)).digest()
        self._touch(chain.hex(), credential_id, current)
        self._evict(current)

    def _touch(self, key: str, credential_id: str, now: float) -> None:
        """写入/刷新条目并移到末尾（最近使用优先保留）。"""
        self._entries.pop(key, None)
        self._entries[key] = (credential_id, now + self.ttl_seconds)

    def _evict(self, now: float) -> None:
        """超上限时先清过期条目，仍超则按最旧淘汰。"""
        if len(self._entries) <= self.max_entries:
            return
        for key in [k for k, (_, exp) in self._entries.items() if exp <= now]:
            del self._entries[key]
        while len(self._entries) > self.max_entries:
            self._entries.pop(next(iter(self._entries)))

    def __len__(self) -> int:
        return len(self._entries)

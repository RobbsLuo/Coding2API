"""会话粘性：同一对话的多轮请求固定使用同一凭证。

OpenAI Chat Completions 本身无会话概念，客户端（agent/CLI）的「对话」有
两种可识别形态，按可靠性依次取用（B1.5）：

1. **请求体显式会话标识**：`conversation_id` / `conversationId` /
   `prompt_cache_key`（`metadata` 对象内或请求体顶层）。客户端直接给出
   会话身份，最可靠——即便消息数组被裁剪/压缩也能粘住。
2. **回落：消息数组的增量前缀指纹**：下一轮的 messages 以上一轮的完整
   messages 为前缀再追加，据此匹配。仅当请求体**没有** `user_id`（顶层或
   `metadata` 内）时才使用——同一用户的并行对话消息前缀可能相同，派生
   兜底键会把它们误钉到同一凭证。

**键名来源与核实状态**（本机 71 份真实请求 dump 来自 PI 客户端，
七个顶层键为 model/messages/tools/stream/stream_options/store/
reasoning_effort，**不含任何会话标识键**，故后两项无法用本地流量核实）：

- `prompt_cache_key`：OpenAI 官方顶层参数，**已核实**（chat/completions
  与 responses 均支持）
- `metadata.user_id`：Anthropic Messages API 官方字段，**已核实**
- `conversation_id` / `conversationId` / 顶层 `user_id`：非 OpenAI/Anthropic
  标准键，属客户端惯用约定，**未在真实流量中观测到**；作为网关兼容性
  探测一并接受（命中即用，未命中无害）

键里掺入用户名，防止不同 API 用户的相同标识/消息数组串到同一凭证。
TTL 内没有后续轮次即视为对话结束，条目过期；条目数有上限，超量先淘汰
过期条目、再按最旧淘汰。纯内存即可：重启后丢粘性只影响一轮选号。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

# 请求体里的显式会话标识键名，按优先级排列（前两个等价，客户端实现不一）
_EXPLICIT_KEYS: tuple[str, ...] = ("conversation_id", "conversationId", "prompt_cache_key")


def _nonempty_str(value: object) -> str | None:
    """取非空字符串（去首尾空白）；其他类型一律视为未提供。"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _containers(raw: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """查键顺序：先 metadata 对象，再请求体顶层。"""
    metadata = raw.get("metadata")
    if isinstance(metadata, dict):
        return (metadata, raw)
    return (raw,)


def explicit_key(raw: dict[str, Any]) -> str | None:
    """请求体显式会话标识 → 归一后的键；未提供返回 None。

    优先级：`conversation_id` > `conversationId` > `prompt_cache_key`；
    同键名先查 `metadata` 对象再查请求体顶层。返回值带键名前缀，
    避免不同键名的同值串号（如 `conversation_id:abc` 与 `abc`）。
    """
    for name in _EXPLICIT_KEYS:
        for container in _containers(raw):
            value = _nonempty_str(container.get(name))
            if value is not None:
                return f"{name}:{value}"
    return None


def has_user_id(raw: dict[str, Any]) -> bool:
    """请求体是否显式带 `user_id`（顶层或 metadata 内）。

    有则客户端能自己区分并行对话，消息前缀兜底键不再派生（会误钉）。
    """
    return any(_nonempty_str(container.get("user_id")) is not None
               for container in _containers(raw))


def _message_digest(message: object) -> bytes:
    """单条消息的规范哈希：键序归一，杜绝 JSON 键序差异造成假失配。"""
    text = json.dumps(message, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).digest()


class ConversationAffinity:
    """会话键 → 凭证的粘性表（单事件循环内使用，无需加锁）。"""

    def __init__(self, *, ttl_seconds: int, max_entries: int = 512) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        # fingerprint_hex → (credential_id, expires_at)；dict 保持插入序，供淘汰
        self._entries: dict[str, tuple[str, float]] = {}

    # ------------------------------------------------------------ 对外接口

    def pin_for(self, raw: dict[str, Any], username: str, *,
                now: float | None = None) -> str | None:
        """本对话已粘的凭证；无匹配（含禁用/过期）返回 None。

        显式标识直接单键命中；否则退回最长前缀匹配：上一轮请求存的指纹
        正是本轮某个前缀的指纹，最长匹配者即本对话最近一轮实际使用的凭证。
        """
        if self.ttl_seconds <= 0 or not isinstance(raw, dict):
            return None
        current = now if now is not None else time.monotonic()
        explicit = explicit_key(raw)
        if explicit is not None:
            return self._lookup(self._explicit_digest(explicit, username), current)
        messages = raw.get("messages")
        if has_user_id(raw) or not isinstance(messages, list) or not messages:
            return None
        return self._pin_chain(messages, username, current)

    def remember(self, raw: dict[str, Any], username: str, credential_id: str, *,
                 now: float | None = None) -> None:
        """记录本轮请求 → 实际服务的凭证（成功后调用）。"""
        if self.ttl_seconds <= 0 or not isinstance(raw, dict):
            return
        current = now if now is not None else time.monotonic()
        explicit = explicit_key(raw)
        if explicit is not None:
            self._touch(self._explicit_digest(explicit, username), credential_id, current)
            self._evict(current)
            return
        messages = raw.get("messages")
        if has_user_id(raw) or not isinstance(messages, list) or not messages:
            return
        # 只存最终链键（一条对话一个条目）；pin_for 扫描前缀找最长匹配，
        # 逐条全存会成倍占用条目并改变淘汰行为
        digests = self._chain_digests(messages, username)
        self._touch(digests[-1], credential_id, current)
        self._evict(current)

    # -------------------------------------------------------------- 内部

    @staticmethod
    def _explicit_digest(explicit: str, username: str) -> str:
        """显式标识的条目键：掺入用户名防跨用户串号。"""
        text = f"explicit|{username}|{explicit}"
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _chain_digests(self, messages: list[Any], username: str) -> list[str]:
        """逐条累积指纹：每个前缀一个键，供下一轮做最长前缀匹配。"""
        chain = hashlib.sha256(username.encode("utf-8")).digest()
        digests: list[str] = []
        for message in messages:
            chain = hashlib.sha256(chain + _message_digest(message)).digest()
            digests.append(chain.hex())
        return digests

    def _lookup(self, key: str, current: float) -> str | None:
        """单键命中（显式标识用）：过期即删并返回 None。"""
        entry = self._entries.get(key)
        if entry is None:
            return None
        credential_id, expires_at = entry
        if expires_at <= current:
            del self._entries[key]
            return None
        self._touch(key, credential_id, current)
        return credential_id

    def _pin_chain(self, messages: list[Any], username: str,
                   current: float) -> str | None:
        """最长前缀匹配（消息前缀兜底用）。"""
        pinned: str | None = None
        for key in self._chain_digests(messages, username):
            credential_id = self._lookup(key, current)
            if credential_id is not None:
                pinned = credential_id
        return pinned

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

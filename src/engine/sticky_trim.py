"""粘性裁剪：让压缩后的前缀在多轮之间保持稳定。

**问题**：客户端每轮重发完整历史（实测一次 453 条 / 估算 1.88M token，而
qoder 窗口只有 180K）时，`compress_messages` 每轮都按当轮预算重算「保留最近
N 条」。多一轮消息，窗口边界就前移一条，发给上游的前缀从第 2 条消息起就变
了——上游前缀缓存只能命中 system 段。实测同一凭证 32 条请求里 21 条
`reusable=1`（只有 system 可复用），单号命中率 25%，而此刻同渠道其他形态
的会话在 90%+。

**做法**：只在**超预算**时裁，且一次裁到 `budget × low_watermark`（默认
0.6，留 40% 余量），把「保留窗口的起点」（正文消息的绝对序号）与该条消息
的摘要一起记住。之后每轮只要：

1. 起点消息内容没变（摘要一致），
2. 从该起点裁出来的总量仍在预算内，

就直接复用同一起点——前缀逐字相同，缓存正常命中；直到新增消息把总量顶回
预算，才再裁一次并下移起点。裁剪从「每轮一次」变成「每几十轮一次」。

**安全性**：起点必须仍是消息组的边界（`assistant.tool_calls` 与其 tool 结果
不能被拆开），摘要对不上或分组变了就丢弃记忆重新裁——宁可多裁一次，也不把
一段错的上下文发给上游。未超限时整份发出，记忆一并清除。

前提是保留窗口**连续**（`compress.trim_to_budget` 已改成「装不下就停」）：
带洞的保留集合即使记住起点也复用不了，因为下一轮重算时洞的位置会变。

状态是进程内 LRU（按「渠道 + 模型 + 会话身份」）。重启后丢记忆只意味着多裁
一次，不影响正确性。
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from .affinity import resolve_explicit
from .compress import (
    DEFAULT_MIN_KEEP_MESSAGES,
    DEFAULT_RESERVE_FOR_OUTPUT,
    DEFAULT_SAFETY_RATIO,
    _group_atomic,
    estimate_messages,
    trim_to_budget,
)

# 裁剪目标：预算的 60%。留出的余量决定「裁一次能撑多少轮」——余量越大，
# 两次裁剪间隔越长、缓存命中越稳，但一次性丢掉的旧上下文也越多。
DEFAULT_LOW_WATERMARK = 0.6
# 同时在跑的会话数上限（LRU）：超出后淘汰最久未用的会话记忆
_DEFAULT_CAPACITY = 256


def _digest(value: Any) -> str:
    """内容摘要（键排序紧凑 JSON）：只用于判断「还是不是那条消息」。"""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8"),
                          usedforsecurity=False).hexdigest()


def conversation_key(provider_id: str, model: str, raw: dict[str, Any]) -> str:
    """会话身份：显式会话标识（body/头）与头几条消息摘要一起做键。

    撞键的后果只是「白裁一次」——起点摘要校验会失败并重裁，不会把 A 会话的
    上下文发给 B；所以这里宁可比需要的更细。
    """
    explicit = resolve_explicit(raw) or ""
    messages = raw.get("messages")
    head = _digest(messages[:3]) if isinstance(messages, list) and messages else ""
    return f"{provider_id}|{model}|{explicit}|{head}"


@dataclass(frozen=True, slots=True)
class _Boundary:
    """上一次裁剪保留窗口的起点。"""

    start: int          # 正文消息（去掉 system）中的绝对序号
    digest: str         # 起点那条消息的摘要


@dataclass(frozen=True, slots=True)
class TrimOutcome:
    """一次压缩的结果。"""

    messages: list[dict[str, Any]]
    trimmed: bool       # 本轮是否真的重新裁过；False = 原样或复用旧边界
    reason: str = ""    # 为什么（没）裁：诊断用，见 _reuse / compress 的分支注释


class StickyTrimmer:
    """按会话记住裁剪边界，避免每轮都改写前缀。"""

    def __init__(self, *, capacity: int = _DEFAULT_CAPACITY,
                 low_watermark: float = DEFAULT_LOW_WATERMARK) -> None:
        if capacity < 1:
            raise ValueError("sticky trim capacity must be >= 1")
        if not 0.0 < low_watermark <= 1.0:
            raise ValueError("sticky trim low_watermark must be in (0, 1]")
        self._capacity = capacity
        self._low_watermark = low_watermark
        self._seen: OrderedDict[str, _Boundary] = OrderedDict()

    def compress(
        self,
        key: str,
        messages: list[dict[str, Any]],
        *,
        max_input_tokens: int,
        reserve_for_output: float = DEFAULT_RESERVE_FOR_OUTPUT,
        min_keep_messages: int = DEFAULT_MIN_KEEP_MESSAGES,
        safety_ratio: float = DEFAULT_SAFETY_RATIO,
    ) -> TrimOutcome:
        """压缩入参消息；未超预算时原样返回（零拷贝，并清掉旧边界）。"""
        if max_input_tokens <= 0:
            self._seen.pop(key, None)
            return TrimOutcome(messages, trimmed=False, reason="no_limit")
        budget = max_input_tokens * safety_ratio - reserve_for_output
        if budget <= 0:
            self._seen.pop(key, None)
            return TrimOutcome(messages, trimmed=False, reason="no_budget")
        if estimate_messages(messages) <= budget:
            # 整份都发得下：上一次的起点已无意义，清掉免得历史缩短后误用
            self._seen.pop(key, None)
            return TrimOutcome(messages, trimmed=False, reason="fits")

        body = [m for m in messages if m.get("role") != "system"]
        boundary = self._seen.get(key)
        if boundary is not None:
            reused, reason = self._reuse(messages, body, boundary, budget)
            if reused is not None:
                self._touch(key, boundary)
                return TrimOutcome(reused, trimmed=False, reason="reused")
        else:
            reason = "no_memory"

        # 需要（重新）裁：一次裁到低水位，给后续若干轮留出前缀稳定空间
        trimmed, start = trim_to_budget(
            messages, budget=budget * self._low_watermark,
            min_keep_messages=min_keep_messages)
        self._remember(key, body, start)
        return TrimOutcome(trimmed, trimmed=True, reason=reason)

    # -------------------------------------------------------------- 内部

    def _reuse(self, messages: list[dict[str, Any]], body: list[dict[str, Any]],
               boundary: _Boundary, budget: float
               ) -> tuple[list[dict[str, Any]] | None, str]:
        """沿用旧起点：起点消息未变、仍在组边界、且裁后不超预算才算命中。"""
        start = boundary.start
        if start >= len(body):
            return None, "start_out_of_range"
        if _digest(body[start]) != boundary.digest:
            return None, "digest_changed"
        if start not in _group_starts(body):
            return None, "not_group_start"
        candidate = [*(m for m in messages if m.get("role") == "system"),
                     *body[start:]]
        if estimate_messages(candidate) > budget:
            return None, "over_budget"
        return candidate, "reused"

    def _remember(self, key: str, body: list[dict[str, Any]], start: int) -> None:
        if start < len(body):
            boundary = _Boundary(start=start, digest=_digest(body[start]))
        else:
            # 没有正文可留（或全部被截）：不记起点，下一轮重新裁
            self._seen.pop(key, None)
            return
        self._seen.pop(key, None)
        self._seen[key] = boundary
        if len(self._seen) > self._capacity:
            self._seen.popitem(last=False)

    def _touch(self, key: str, boundary: _Boundary) -> None:
        self._seen.pop(key, None)
        self._seen[key] = boundary

    def __len__(self) -> int:
        return len(self._seen)


def _group_starts(body: list[dict[str, Any]]) -> set[int]:
    """正文各消息组的起始序号（`assistant.tool_calls` + 其后 tool 为一组）。"""
    starts: set[int] = set()
    cursor = 0
    for group in _group_atomic(body):
        starts.add(cursor)
        cursor += len(group)
    return starts

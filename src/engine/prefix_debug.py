"""前缀缓存诊断：记录每次请求的载荷指纹，定位「命中率为什么掉」。

为什么需要它：把单号命中率拆到单请求后能看到两种形态——连续轮次命中 ~99%，
以及每次只命中一段固定长度（如 14208/14464）的请求。后者说明前缀在会话正文
处就分叉了，但网关只落 `input_tokens` / `cached_tokens` 两个数字，看不到**在
哪条消息**分叉——上游前缀缓存按 token 对齐、按消息边界推理并不成立。

只产出**摘要**（sha256 前 16 位）与条数，不落正文：日志里不该出现用户代码。
开关由运行态 `prefix_debug_enabled` 控制（默认关），调用方先判开关再调用，
关闭时本模块一次都不执行。
"""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Sequence
from typing import Any

_DIGEST_LEN = 16
# 同一进程里同时在跑的会话数远小于这个值；LRU 只是防止凭证反复增删时无界增长
_DEFAULT_CAPACITY = 64
# 缺省 tools 的固定摘要：不参与比较的哨兵值，避免和真实摘要撞车。
# 与 `_EMPTY` **必须不同**：两者同值时日志里分不清「本次没带 tools」与
# 「没有 system 消息」（前者 tools_changed 恒 false 会被静默吞掉）。
_NO_TOOLS = "no-tools"
# 无 system / 无分叉点时的占位
_EMPTY = "-"


def _digest(value: Any) -> str:
    """稳定摘要：键排序 + 紧凑分隔符 ⇒ 同内容同摘要，跨进程可比。"""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), default=str)
    # usedforsecurity=False：这里不是密码学用途，只是内容指纹
    return hashlib.sha256(raw.encode("utf-8"),
                          usedforsecurity=False).hexdigest()[:_DIGEST_LEN]


def message_digests(messages: Any) -> tuple[str, ...]:
    """逐条消息摘要；非 list 形状（畸形请求）视为无消息。"""
    if not isinstance(messages, list):
        return ()
    return tuple(_digest(message) for message in messages)


def tools_digest(payload: dict[str, Any]) -> str:
    """tools 定义的摘要。缺省 / 空列表都记哨兵值：两者对上游等价。"""
    tools = payload.get("tools")
    return _digest(tools) if tools else _NO_TOOLS


def longest_common_prefix(previous: Sequence[str], current: Sequence[str]) -> int:
    """两条指纹序列从头部起连续相同的条数。"""
    limit = min(len(previous), len(current))
    index = 0
    while index < limit and previous[index] == current[index]:
        index += 1
    return index


class PrefixDebugTracker:
    """按 key（凭证 + 模型）保存上一条请求的指纹，进程内有界 LRU。"""

    def __init__(self, capacity: int = _DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("prefix debug capacity must be >= 1")
        self._capacity = capacity
        self._seen: OrderedDict[str, tuple[tuple[str, ...], str]] = OrderedDict()

    def observe(self, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        """记录本次载荷并返回与上一次（同 key）的比较结果。

        首次见到该 key 时 `lcp` / `reusable` / `split` 均为 None：没有基准就
        不猜「命中多少」，避免把「第一条」误报成「前缀完全变了」。
        """
        current = message_digests(payload.get("messages"))
        tools = tools_digest(payload)
        previous = self._seen.get(key)
        self._seen.pop(key, None)
        self._seen[key] = (current, tools)
        if len(self._seen) > self._capacity:
            self._seen.popitem(last=False)

        report: dict[str, Any] = {
            "messages": len(current),
            "system": current[0] if current else _EMPTY,
            "tools": tools,
        }
        if previous is None:
            report.update(lcp=None, reusable=None, tools_changed=None, split=None)
            return report
        lcp = longest_common_prefix(previous[0], current)
        tools_changed = previous[1] != tools
        # tools 定义排在 system 之后：它变了 ⇒ system 之后的 token 全部失效，
        # 可复用前缀塌到 system 为止（system 本身变了则连 1 条都不剩）
        reusable = min(lcp, 1) if tools_changed else lcp
        report.update(
            lcp=lcp,
            reusable=reusable,
            tools_changed=tools_changed,
            # 分叉点那条消息的摘要：同一条反复出现在这里 ⇒ 就是它在每轮改写
            split=current[lcp] if lcp < len(current) else _EMPTY,
        )
        return report

"""前缀缓存诊断（`src/engine/prefix_debug.py` + `build_prefix_observer` 接线）。

只验摘要与比较逻辑：同内容同摘要、分叉点定位、tools 变化塌到 system、
按凭证隔离、LRU 有界。日志文本只断言关键字与字段值，不锁整行格式。
"""

from __future__ import annotations

import pytest

from src.api.context import build_prefix_observer
from src.engine.prefix_debug import (
    PrefixDebugTracker,
    longest_common_prefix,
    message_digests,
    tools_digest,
)


def _msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


class _Settings:
    def __init__(self, *, enabled=True):
        self.prefix_debug_enabled = enabled


# ------------------------------------------------------------------ 摘要

def test_message_digests_are_stable_and_shape_safe():
    messages = [_msg("system", "s"), _msg("user", "u")]
    assert message_digests(messages) == message_digests(list(messages))
    assert len(message_digests(messages)) == 2
    # 同内容不同对象 ⇒ 同摘要（序列化键排序）
    assert message_digests([{"content": "x", "role": "user"}]) == \
        message_digests([{"role": "user", "content": "x"}])
    # 内容变了 ⇒ 摘要变
    assert message_digests([_msg("user", "a")]) != message_digests([_msg("user", "b")])
    # 非 list 形状（畸形请求）视为无消息
    assert message_digests(None) == ()
    assert message_digests({"role": "user"}) == ()


def test_tools_digest_treats_missing_and_empty_alike():
    assert tools_digest({}) == "-"
    assert tools_digest({"tools": []}) == "-"
    assert tools_digest({"tools": None}) == "-"
    defined = tools_digest({"tools": [{"type": "function", "function": {"name": "f"}}]})
    assert defined != "-"
    assert defined == tools_digest(
        {"tools": [{"function": {"name": "f"}, "type": "function"}]})


def test_longest_common_prefix_counts_leading_identical_entries():
    assert longest_common_prefix((), ()) == 0
    assert longest_common_prefix(("a",), ()) == 0
    assert longest_common_prefix(("a", "b"), ("a", "c")) == 1
    assert longest_common_prefix(("a", "b"), ("a", "b", "c")) == 2
    assert longest_common_prefix(("a", "b", "c"), ("a", "b", "c")) == 3


# ------------------------------------------------------------------ 追踪器

def test_first_request_has_no_baseline():
    tracker = PrefixDebugTracker()
    report = tracker.observe("c1", {"messages": [_msg("system", "s"), _msg("user", "u")]})
    assert report["messages"] == 2
    assert report["system"] == message_digests([_msg("system", "s")])[0]
    assert report["tools"] == "-"
    # 没有上一条就没有基准：一律 None，不把「第一条」报成「前缀全变」
    assert report["lcp"] is None
    assert report["reusable"] is None
    assert report["tools_changed"] is None
    assert report["split"] is None
    # 空消息列表也不炸，system 记哨兵值
    assert tracker.observe("c2", {})["system"] == "-"
    assert tracker.observe("c2", {"messages": []})["lcp"] == 0


def test_identical_prefix_reports_full_reuse():
    tracker = PrefixDebugTracker()
    payload = {"messages": [_msg("system", "s"), _msg("user", "u")],
               "tools": [{"type": "function"}]}
    tracker.observe("c1", payload)
    report = tracker.observe("c1", {"messages": payload["messages"],
                                    "tools": payload["tools"]})
    assert report["lcp"] == 2
    assert report["reusable"] == 2
    assert report["tools_changed"] is False
    # 没有分叉点（全部复用）也给哨兵值
    assert report["split"] == "-"


def test_divergent_message_is_located():
    tracker = PrefixDebugTracker()
    tracker.observe("c1", {"messages": [_msg("system", "s"), _msg("user", "u1")]})
    report = tracker.observe("c1", {"messages": [_msg("system", "s"),
                                                 _msg("user", "u2"),
                                                 _msg("assistant", "a")]})
    assert report["lcp"] == 1
    assert report["reusable"] == 1
    assert report["tools_changed"] is False
    assert report["split"] == message_digests([_msg("user", "u2")])[0]


def test_tools_change_collapses_reuse_to_system():
    """tools 定义排在 system 之后：它变了 ⇒ 正文一条都复用不了。"""
    tracker = PrefixDebugTracker()
    messages = [_msg("system", "s"), _msg("user", "u")]
    tracker.observe("c1", {"messages": messages, "tools": [{"n": 1}]})
    changed = tracker.observe("c1", {"messages": messages, "tools": [{"n": 2}]})
    assert changed["tools_changed"] is True
    assert changed["lcp"] == 2            # 消息本身没变，但 tools 变了
    assert changed["reusable"] == 1       # 可复用塌到 system
    # system 也变了（lcp 0）⇒ 一条都不剩
    tracker.observe("c1", {"messages": [{"role": "system", "content": "s2"}],
                           "tools": [{"n": 3}]})
    zero = tracker.observe("c1", {"messages": [_msg("user", "u")]})
    assert zero["lcp"] == 0
    assert zero["tools_changed"] is True
    assert zero["reusable"] == 0


def test_baselines_are_per_key():
    tracker = PrefixDebugTracker()
    tracker.observe("a", {"messages": [_msg("user", "x")]})
    # 另一个 key 首次见面：没有基准
    assert tracker.observe("b", {"messages": [_msg("user", "x")]})["lcp"] is None
    assert tracker.observe("a", {"messages": [_msg("user", "x")]})["lcp"] == 1


def test_tracker_is_bounded_and_evicts_oldest():
    tracker = PrefixDebugTracker(capacity=1)
    tracker.observe("a", {"messages": [_msg("user", "x")]})
    tracker.observe("b", {"messages": [_msg("user", "x")]})
    # a 已被 LRU 淘汰 ⇒ 重新变成「首次见到」
    assert tracker.observe("a", {"messages": [_msg("user", "x")]})["lcp"] is None


def test_tracker_rejects_non_positive_capacity():
    with pytest.raises(ValueError):
        PrefixDebugTracker(capacity=0)


# ------------------------------------------------------------------ observer

def test_observer_is_noop_when_disabled(caplog):
    settings = _Settings(enabled=False)
    observer = build_prefix_observer(settings)
    with caplog.at_level("INFO"):
        observer("c1", "qoder", "m", {"messages": [_msg("user", "u")]})
    assert "前缀诊断" not in caplog.text


def test_observer_logs_report_when_enabled(caplog):
    settings = _Settings(enabled=True)
    observer = build_prefix_observer(settings)
    payload = {"messages": [_msg("system", "s"), _msg("user", "u")]}
    with caplog.at_level("INFO"):
        observer("cred_1", "qoder", "deepseek-v4.1-flash", payload)
        observer("cred_1", "qoder", "deepseek-v4.1-flash",
                 {"messages": [*payload["messages"], _msg("assistant", "a")]})
    lines = [line for line in caplog.text.splitlines() if "前缀诊断 " in line]
    assert len(lines) == 2
    assert "provider=qoder" in lines[0]
    assert "model=deepseek-v4.1-flash" in lines[0]
    assert "cred=cred_1" in lines[0]
    assert "msgs=2" in lines[0]
    assert "lcp=None" in lines[0]            # 第一条：没有基准
    assert "reusable=2" in lines[1]          # 第二条：前两条消息可复用
    assert "tools_changed=False" in lines[1]


def test_observer_key_isolated_by_model(caplog):
    """同一凭证换模型：模板/参数不同，不复用基准。"""
    settings = _Settings(enabled=True)
    observer = build_prefix_observer(settings)
    payload = {"messages": [_msg("user", "u")]}
    with caplog.at_level("INFO"):
        observer("cred_1", "qoder", "model-a", payload)
        observer("cred_1", "qoder", "model-b", payload)
    lines = [line for line in caplog.text.splitlines() if "前缀诊断 " in line]
    assert len(lines) == 2
    assert "lcp=None" in lines[1]

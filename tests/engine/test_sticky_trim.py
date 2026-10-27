"""粘性裁剪（`src/engine/sticky_trim.py`）。

重点是「跨轮前缀不变」：同一会话追加消息后再压，前缀必须逐字相同（否则上游
前缀缓存还是只能命中 system 段）。其余覆盖失效分支：起点消息被改写、起点不再
是消息组边界、新增消息把总量顶回预算、未超预算时清记忆、LRU 淘汰。
"""

from __future__ import annotations

import pytest

from src.engine.compress import estimate_messages, trim_to_budget
from src.engine.sticky_trim import (
    StickyTrimmer,
    _Boundary,
    _digest,
    conversation_key,
)


def _sys(text: str = "sys") -> dict:
    return {"role": "system", "content": text}


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _assistant(text: str) -> dict:
    return {"role": "assistant", "content": text}


def _tool(call_id: str = "t1") -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": "result"}


def _settings(**overrides):
    """压缩参数（与 RuntimeSettings 同名的关键字），默认预算很小便于触发裁剪。"""
    options = {"max_input_tokens": 400, "reserve_for_output": 0,
               "min_keep_messages": 1, "safety_ratio": 1.0}
    options.update(overrides)
    return options


def _history(count: int, *, size: int = 400) -> list[dict]:
    """system + count 条等长 user 消息（每条 0.25*size token）。"""
    return [_sys(), *(_user(f"{index}-" + "x" * size) for index in range(count))]


# ------------------------------------------------------------------ 键与摘要

def test_conversation_key_uses_explicit_id_and_head_digest():
    raw = {"conversation_id": "abc", "messages": [_sys(), _user("u")]}
    key = conversation_key("qoder", "m", raw)
    assert key.startswith("qoder|m|conversation_id:abc|")
    # 同一个会话同一份头：键稳定
    assert key == conversation_key("qoder", "m", dict(raw))
    # 头几条消息不同 ⇒ 不同键（显式 id 相同也不共用边界记忆）
    other = {"conversation_id": "abc", "messages": [_sys("other"), _user("u")]}
    assert conversation_key("qoder", "m", other) != key
    # 没有显式 id：仍靠头摘要区分；没有 messages 也不炸
    assert "|m||" in conversation_key("qoder", "m", {"messages": [_sys()]})
    assert conversation_key("qoder", "m", {}) == conversation_key(
        "qoder", "m", {"messages": "not-a-list"})


# ------------------------------------------------------------------ 基本契约

def test_under_budget_is_noop_and_forgets_boundary():
    trimmer = StickyTrimmer()
    messages = _history(2)
    outcome = trimmer.compress("k", messages, **_settings(max_input_tokens=100000))
    assert outcome.messages is messages
    assert outcome.trimmed is False
    assert len(trimmer) == 0


def test_non_positive_limit_or_budget_is_noop():
    trimmer = StickyTrimmer()
    messages = _history(3)
    assert trimmer.compress("k", messages, **_settings(max_input_tokens=0)).messages is messages
    # safety_ratio 让预算 ≤ 0：不裁
    zero = trimmer.compress("k", messages, **_settings(max_input_tokens=10,
                                                       reserve_for_output=100))
    assert zero.messages is messages
    assert zero.trimmed is False


def test_invalid_construction_is_rejected():
    with pytest.raises(ValueError):
        StickyTrimmer(capacity=0)
    with pytest.raises(ValueError):
        StickyTrimmer(low_watermark=0.0)
    with pytest.raises(ValueError):
        StickyTrimmer(low_watermark=1.5)


# ------------------------------------------------------------------ 粘性核心

def test_trim_then_keep_prefix_stable_across_turns():
    """裁一次之后，后续追加消息不再改前缀——这是命中率回来的前提。"""
    trimmer = StickyTrimmer()
    options = _settings(max_input_tokens=4000)
    first = trimmer.compress("k", _history(60), **options)
    assert first.trimmed is True
    assert first.messages[0] == _sys()
    kept = len(first.messages)

    second = trimmer.compress("k", _history(61), **options)
    assert second.trimmed is False                 # 复用旧边界，不重裁
    assert second.messages[:kept] == first.messages  # 前缀逐字相同
    assert len(second.messages) == kept + 1

    third = trimmer.compress("k", _history(62), **options)
    assert third.trimmed is False
    assert third.messages[:kept] == first.messages


def test_retrim_only_when_appended_messages_exceed_budget():
    trimmer = StickyTrimmer()
    options = _settings(max_input_tokens=4000)
    before = trimmer.compress("k", _history(60), **options)
    kept_before = len(before.messages)
    # 一次追加足够多：候选超预算 ⇒ 重裁，窗口起点下移
    after = trimmer.compress("k", _history(120), **options)
    assert after.trimmed is True
    assert len(after.messages) < kept_before + 60
    assert after.messages[0] == _sys()


def test_rewritten_history_invalidates_boundary():
    trimmer = StickyTrimmer()
    options = _settings(max_input_tokens=4000)
    first = trimmer.compress("k", _history(60), **options)
    boundary_start = trimmer._seen["k"].start
    rewritten = _history(60)
    # 改掉保留窗口起点那条消息（客户端压缩/摘要改写）：摘要对不上 ⇒ 重裁
    assert rewritten[boundary_start + 1] == first.messages[1]
    rewritten[boundary_start + 1] = _user("changed-" + "x" * 400)
    outcome = trimmer.compress("k", rewritten, **options)
    assert outcome.trimmed is True


def test_boundary_that_is_no_longer_a_group_start_is_not_reused():
    """起点落在 tool 组中间（历史被改写）时不能复用，否则会切断 tool 配对。"""
    trimmer = StickyTrimmer()
    messages = [_sys(), _user("a" * 400), _assistant("saw"), _tool(), _user("z" * 400)]
    body = messages[1:]
    # 起点 = 2（tool 消息）在「assistant 无 tool_calls」时是组起点
    boundary = _Boundary(start=2, digest=_digest(body[2]))
    reused, reason = trimmer._reuse(messages, body, boundary, budget=10000)
    assert reused is not None and reason == "reused"
    # assistant 加上 tool_calls 后，tool 与它同组 ⇒ 序号 2 不再是组起点
    messages[2] = {"role": "assistant", "content": "saw",
                   "tool_calls": [{"id": "t1", "function": {"name": "f",
                                                           "arguments": ""}}]}
    body = messages[1:]
    assert trimmer._reuse(messages, body, boundary, budget=10000)[1] == "not_group_start"
    # 起点越界（历史变短）同样不复用
    assert trimmer._reuse(messages, body[:1], boundary, budget=10000)[1] == \
        "start_out_of_range"


def test_reuse_rejected_when_candidate_exceeds_budget():
    trimmer = StickyTrimmer()
    trimmer.compress("k", _history(20), **_settings())
    messages = _history(20)
    boundary = _Boundary(start=0, digest=_digest(messages[1]))
    # 预算小到连「从起点起全部保留」都装不下 ⇒ 必须重裁
    assert trimmer._reuse(messages, messages[1:], boundary, budget=10)[1] == "over_budget"


def test_no_body_means_no_memory():
    trimmer = StickyTrimmer()
    outcome = trimmer.compress("k", [_sys("s" * 4000)], **_settings())
    assert outcome.trimmed is True
    assert len(trimmer) == 0
    # 没有记忆 ⇒ 下一轮仍然重裁（不误用起点）
    assert trimmer.compress("k", [_sys("s" * 4000)], **_settings()).trimmed is True


def test_memory_is_bounded_lru():
    trimmer = StickyTrimmer(capacity=1)
    trimmer.compress("a", _history(20), **_settings())
    trimmer.compress("b", _history(20), **_settings())
    assert len(trimmer) == 1
    # a 的记忆被淘汰 ⇒ 重新裁一次
    assert trimmer.compress("a", _history(20), **_settings()).trimmed is True


# ------------------------------------------------------- trim_to_budget 起点

def test_trim_to_budget_reports_first_kept_body_index():
    messages = [_sys(), _user("a" * 400), _user("b" * 400), _user("c" * 400)]
    trimmed, start = trim_to_budget(messages, budget=1, min_keep_messages=1)
    assert trimmed == [_sys(), messages[-1]]
    assert start == 2                     # body 中第 2 条（0 基）是保留窗口起点
    # 工具组整体保留，起点落在组首而不是组内
    grouped = [_sys(), _user("x" * 400),
               {"role": "assistant", "content": "",
                "tool_calls": [{"id": "t1", "function": {"name": "f",
                                                        "arguments": ""}}]},
               _tool()]
    trimmed2, start2 = trim_to_budget(grouped, budget=3, min_keep_messages=2)
    assert start2 == 1                    # assistant(tool_calls)+tool 同组，起点为 1
    assert trimmed2[1].get("tool_calls") is not None
    assert estimate_messages(trimmed2) <= estimate_messages(grouped)

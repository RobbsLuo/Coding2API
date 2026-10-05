"""P0-2：上下文压缩（`src/engine/compress.py` + `src/api/context.py`）。

纯函数直测为主：估算口径、按上限裁剪、system 保留、tool_calls 成组、
单条超限截断。末尾一节覆盖 API 层接线（模型目录查上限 + 开关）。
"""

from __future__ import annotations

from src.api.context import apply_context_compression, context_window_for
from src.compat.openai.request import ChatRequest
from src.engine.compress import (
    _content_text,
    _truncate_content,
    compress_messages,
    estimate_messages,
    estimate_tokens,
)
from src.provider.base import Model


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _system(text: str) -> dict:
    return {"role": "system", "content": text}


# --------------------------------------------------------------- token 估算

def test_estimate_tokens_weights_by_character_class():
    assert estimate_tokens("") == 0
    # 中文 0.55 / 数字 0.33 / 其他 0.25
    assert estimate_tokens("中") == 0.55
    assert estimate_tokens("1") == 0.33
    assert estimate_tokens("a") == 0.25
    assert estimate_tokens("中1a") == 0.55 + 0.33 + 0.25


def test_estimate_tokens_covers_fullwidth_and_cjk_punctuation():
    # 3000 段（CJK 标点）与 FF00 段（全角）同样按中文计权
    assert estimate_tokens("、") == 0.55
    assert estimate_tokens("Ａ") == 0.55


def test_content_text_handles_parts_and_placeholders():
    assert _content_text("x") == "x"
    assert _content_text(None) == ""
    assert _content_text(5) == "5"
    # 非字符串 text / 非文本 part 各按 100 字符占位
    assert _content_text([{"text": "a"}, "b", {"image": 1}, {"text": 2}]) == "a" + "b" + "x" * 200
    # 既不是 dict 也不是 str 的条目直接跳过，不占位
    assert _content_text([1, "a"]) == "a"


def test_estimate_messages_counts_reasoning_tools_and_ids():
    messages = [{
        "role": "assistant", "content": "a", "reasoning_content": "bb",
        "tool_call_id": "t1",
        "tool_calls": [{"function": {"name": "f", "arguments": "x"}}]}]
    # content 'a' + reasoning 'bb' + name 'f' + args 'x' + tool_call_id 't1'(0.33+0.25)
    expected = 0.25 + 0.5 + 0.25 + 0.25 + 0.58
    assert estimate_messages(messages) == expected


def test_estimate_messages_tolerates_non_dict_tool_calls():
    assert estimate_messages([{"role": "assistant", "tool_calls": [None, 5]}]) == 0


def test_truncate_content_short_is_unchanged():
    assert _truncate_content("short") == "short"
    # part 数组即使总长很短也原样返回（不降级）
    parts = [{"type": "text", "text": "x"}]
    assert _truncate_content(parts) is parts


# --------------------------------------------------------------- 裁剪

def test_compress_noop_when_under_limit():
    messages = [_user("hi")]
    assert compress_messages(messages, max_input_tokens=1000) is messages


def test_compress_noop_for_zero_or_negative_limit():
    messages = [_user("x" * 1000)]
    assert compress_messages(messages, max_input_tokens=0) is messages
    assert compress_messages(messages, max_input_tokens=-5) is messages


def test_compress_noop_when_budget_non_positive():
    # safety_ratio 极小 / reserve 很大都可能让预算 ≤ 0 → 不裁剪
    messages = [_user("x" * 100)]
    assert compress_messages(messages, max_input_tokens=10, reserve_for_output=100) is messages


def test_compress_keeps_system_and_recent_drops_oldest():
    messages = [
        _system("sys"),
        _user("old " * 200),
        _user("mid " * 200),
        _user("new"),
    ]
    result = compress_messages(messages, max_input_tokens=200, reserve_for_output=0,
                               min_keep_messages=1, safety_ratio=1.0)
    assert result[0] == _system("sys")          # system 永远保留
    assert result[-1] == _user("new")           # 最近一条永远保留
    assert len(result) < len(messages)          # 老消息被丢
    assert _user("old " * 200) not in result


def test_compress_backfill_keeps_older_small_message_when_it_fits():
    # 最近一条很大但不至于超限，前面有一条小消息也应能留下
    messages = [_user("small"), _user("x" * 40)]
    result = compress_messages(messages, max_input_tokens=20, reserve_for_output=0,
                               min_keep_messages=1, safety_ratio=1.0)
    assert result == messages                    # 两条都在预算内 → 原样
    tighter = compress_messages(messages, max_input_tokens=11, reserve_for_output=0,
                                min_keep_messages=1, safety_ratio=1.0)
    assert tighter == [_user("x" * 40)]          # 只能容下最近一条


def test_compress_keeps_at_least_min_keep_messages_even_when_over_budget():
    messages = [_user("a" * 100), _user("b" * 100), _user("c" * 100)]
    result = compress_messages(messages, max_input_tokens=1, reserve_for_output=0,
                               min_keep_messages=2, safety_ratio=1.0)
    # 最近两条无条件保留（哪怕超预算），最老一条被丢
    assert result == messages[-2:]


def test_compress_keeps_tool_calls_and_results_together():
    messages = [
        _user("old " * 300),
        {"role": "assistant", "tool_calls": [
            {"id": "t1", "function": {"name": "f", "arguments": ""}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "res"},
        _user("new"),
    ]
    result = compress_messages(messages, max_input_tokens=60, reserve_for_output=0,
                               min_keep_messages=1, safety_ratio=1.0)
    # assistant(tool_calls) 与其 tool 结果要么都在、要么都不在
    has_call = any(m.get("tool_calls") for m in result)
    has_result = any(m.get("role") == "tool" for m in result)
    assert has_call == has_result


def test_compress_truncates_oversized_single_message():
    huge = "x" * 100_000
    result = compress_messages([_system("s"), _user(huge)],
                               max_input_tokens=200, reserve_for_output=0,
                               min_keep_messages=1, safety_ratio=1.0)
    # 单条自己就超限：内容被截断并带标记，而不是整条丢
    body = result[-1]["content"]
    assert len(body) < len(huge)
    assert "内容过长已截断" in body


def test_compress_truncates_part_array_content():
    huge_parts = [{"type": "text", "text": "x" * 100_000}]
    result = compress_messages([_user(huge_parts)],
                               max_input_tokens=200, reserve_for_output=0,
                               min_keep_messages=1, safety_ratio=1.0)
    assert isinstance(result[-1]["content"], str)
    assert "内容过长已截断" in result[-1]["content"]


# --------------------------------------------------------- 目录查上限 / 接线

class _ServicesStub:
    def __init__(self, *, cache=None, aliases=None, settings=None):
        self.model_list_cache = cache or {}
        self.model_aliases = aliases or {}
        self.settings = settings


class _SettingsStub:
    def __init__(self, *, enabled=True, reserve=0, min_keep=4, ratio=1.0):
        self.context_compress_enabled = enabled
        self.context_compress_reserve_tokens = reserve
        self.context_compress_min_keep_messages = min_keep
        self.context_compress_safety_ratio = ratio


def test_context_window_looks_up_by_normalized_and_raw_name():
    services = _ServicesStub(
        cache={"codebuddy": {"glm-5.2": Model(id="glm-5.2", max_input_tokens=8000)}},
        aliases={"codebuddy": {"glm-5.2": "GLM-5.2-raw"}},
        settings=_SettingsStub())
    assert context_window_for(services, "glm-5.2") == 8000
    assert context_window_for(services, "glm-5.2@codebuddy") == 8000


def test_context_window_looks_up_by_raw_id_when_alias_points_to_it():
    services = _ServicesStub(
        cache={"codebuddy": {"glm-5.2-raw": Model(id="x", max_input_tokens=4096)}},
        aliases={"codebuddy": {"glm-5.2": "glm-5.2-raw"}},
        settings=_SettingsStub())
    assert context_window_for(services, "glm-5.2") == 4096


def test_context_window_takes_minimum_across_providers():
    services = _ServicesStub(cache={
        "codebuddy": {"m": Model(id="m", max_input_tokens=10000)},
        "trae": {"m": Model(id="m", max_input_tokens=4000)}},
        aliases={"codebuddy": {"m": "m"}, "trae": {"m": "m"}},
        settings=_SettingsStub())
    assert context_window_for(services, "m") == 4000


def test_context_window_ignores_unknown_and_invalid_values():
    services = _ServicesStub(cache={
        "codebuddy": {"m": Model(id="m", max_input_tokens=0),
                      "n": Model(id="n", max_input_tokens=None)}},
        aliases={"codebuddy": {"m": "m", "n": "n"}},
        settings=_SettingsStub())
    assert context_window_for(services, "m") is None
    assert context_window_for(services, "n") is None
    assert context_window_for(services, "missing") is None
    assert context_window_for(services, "") is None


def test_apply_compression_disabled_is_noop():
    services = _ServicesStub(
        cache={"codebuddy": {"m": Model(id="m", max_input_tokens=1)}},
        aliases={"codebuddy": {"m": "m"}}, settings=_SettingsStub(enabled=False))
    request = ChatRequest(model="m", messages=[_user("x" * 1000)],
                          stream=False, raw={"messages": [_user("x" * 1000)]})
    apply_context_compression(services, request)
    assert request.messages == [_user("x" * 1000)]


def test_apply_compression_skips_when_window_unknown_or_messages_missing():
    services = _ServicesStub(settings=_SettingsStub())
    request = ChatRequest(model="m", messages=[_user("x")], stream=False,
                          raw={"messages": [_user("x")]})
    apply_context_compression(services, request)
    assert request.messages == [_user("x")]
    # 有上限但 raw 里没有 messages（非 chat 形状）：安全跳过
    services2 = _ServicesStub(
        cache={"codebuddy": {"m": Model(id="m", max_input_tokens=1)}},
        aliases={"codebuddy": {"m": "m"}}, settings=_SettingsStub())
    request2 = ChatRequest(model="m", messages=[], stream=False, raw={})
    apply_context_compression(services2, request2)
    assert request2.raw == {}


def test_apply_compression_under_budget_is_noop():
    messages = [_user("short")]
    services = _ServicesStub(
        cache={"codebuddy": {"m": Model(id="m", max_input_tokens=100000)}},
        aliases={"codebuddy": {"m": "m"}}, settings=_SettingsStub())
    request = ChatRequest(model="m", messages=messages, stream=False,
                          raw={"messages": messages})
    apply_context_compression(services, request)
    assert request.messages is messages


def test_apply_compression_shrinks_and_updates_both_views():
    messages = [_user("old " * 500), _user("new")]
    services = _ServicesStub(
        cache={"codebuddy": {"m": Model(id="m", max_input_tokens=50)}},
        aliases={"codebuddy": {"m": "m"}}, settings=_SettingsStub(min_keep=1))
    request = ChatRequest(model="m", messages=messages, stream=False,
                          raw={"messages": messages})
    apply_context_compression(services, request)
    assert request.messages is request.raw["messages"]
    assert request.messages == [_user("new")]        # 老消息被裁掉

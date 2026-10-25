"""共享 OpenAI chunk 解析原语：usage 合并后的并集语义。

7 个渠道原先各写一份 `_usage`，抽到 `usage_from_openai` 后必须保证
「details 优先、顶层兜底」的并集行为不丢——尤其顶层 `reasoning_tokens`
（TRAE / Zen / Kilo / Qoder 实测会给）不能因 details 缺失而归 None。
"""

from __future__ import annotations

import pytest

from src.provider.openai_chunk import details_int, usage_from_openai


def test_reasoning_tokens_falls_back_to_top_level():
    """details 缺失时顶层 reasoning_tokens 必须保留（TRAE 等渠道的真实形状）。"""
    usage = usage_from_openai({"prompt_tokens": 1, "reasoning_tokens": 148})
    assert usage.reasoning_tokens == 148


def test_details_win_over_top_level():
    """details 与顶层同时存在时以 details 为准（CB / OfficeAce / CodeArts 实测）。"""
    usage = usage_from_openai({
        "prompt_tokens": 1,
        "reasoning_tokens": 9,
        "completion_tokens_details": {"reasoning_tokens": 100},
    })
    assert usage.reasoning_tokens == 100


@pytest.mark.parametrize("bad", [True, "5", None, 1.5])
def test_details_rejects_non_int(bad):
    """details 里的非法值（bool / 字符串 / 浮点）回落顶层，不当成 0。"""
    usage = usage_from_openai({
        "completion_tokens_details": {"reasoning_tokens": bad},
        "reasoning_tokens": 7,
    })
    assert usage.reasoning_tokens == 7


def test_credit_only_when_channel_reports_it():
    """`credit=True` 的渠道才解析 credit；其余渠道忽略该字段。"""
    assert usage_from_openai({"credit": 1.5}, credit=True).credit == 1.5
    assert usage_from_openai({"credit": 1.5}).credit is None
    # bool 不是有效 credit（True 会被当成 1.0）
    assert usage_from_openai({"credit": True}, credit=True).credit is None


def test_cached_fallbacks_tried_in_order():
    """details → 顶层 → 渠道私有兜底键，逐级回落；0 是有效值不算缺失。"""
    assert usage_from_openai(
        {"cache_read_input_tokens": 7},
        cached_fallbacks=("cache_read_input_tokens",)).cached_tokens == 7
    # 顶层 cached_tokens 优先于私有兜底键
    assert usage_from_openai(
        {"cached_tokens": 2, "cache_read_input_tokens": 5},
        cached_fallbacks=("cache_read_input_tokens",)).cached_tokens == 2
    # 0 必须保留（TRAE 未命中报 0）
    assert usage_from_openai(
        {"cache_read_input_tokens": 0},
        cached_fallbacks=("cache_read_input_tokens",)).cached_tokens == 0
    assert usage_from_openai({}).cached_tokens is None


def test_details_int_handles_non_dict_details():
    """details 不是对象时直接回落顶层，不崩。"""
    assert details_int({"prompt_tokens_details": "nope", "cached_tokens": 3},
                       "prompt_tokens_details", "cached_tokens") == 3
    assert details_int({"prompt_tokens_details": None, "cached_tokens": 3},
                       "prompt_tokens_details", "cached_tokens") == 3

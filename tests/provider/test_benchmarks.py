"""OpenRouter 目录：model_match 候选键/命中与价表构建。"""

from __future__ import annotations

import pytest

from src.benchmarks import BenchmarkTable, build_price_table
from src.model_match import build_table, lookup, match_keys

pytestmark = pytest.mark.real_upstream("openrouter")

# --------------------------------------------------------------- model_match

def test_match_keys_expands_alias_and_suffix():
    """显式规则各产一枚键：doubao→seed、剥 -official。"""
    keys = match_keys("Doubao-Seed-2.1-Turbo")
    assert "doubao-seed-2.1-turbo" in keys
    assert "seed-2.1-turbo" in keys

    keys = match_keys("DeepSeek-V4-Pro-Official")
    assert "deepseek-v4-pro-official" in keys
    assert "deepseek-v4-pro" in keys

def test_match_keys_ignores_empty_and_normalises():
    """空写法不入键；噪声标记与命名空间仍由 normalize_model_key 削掉。"""
    assert match_keys(None, "", "   ") == set()
    keys = match_keys("nvidia/nemotron-3-ultra:free")
    assert keys == {"nemotron-3-ultra"}

def test_match_keys_drops_stacked_suffix_once():
    """规则叠加一层（seed-official）时两枚键都要能查。"""
    keys = match_keys("Seed-2.0-Code-Official")
    assert "seed-2.0-code" in keys
    assert "doubao-seed-2.0-code" not in keys  # 输入没有 doubao 前缀，不加

def test_match_keys_maps_deepseek_flash_channel_codes():
    """渠道分发代号归到同一刊例价：`-0930`（快照日）/ `-sg`（国际站区域）。

    只登记这两个 id，**不做通用日期 / 区域剥尾**——OpenRouter 的
    `deepseek-v4.1-flash-1017` 是另一个价位的快照，剥尾会把两套价格混成一个。
    """
    assert "deepseek-v4.1-flash" in match_keys("deepseek-v4.1-flash-0930")
    assert "deepseek-v4.1-flash" in match_keys("deepseek-v4.1-flash-sg")
    assert "deepseek-v4.1-flash" not in match_keys("deepseek-v4.1-flash-1017")
    assert "deepseek-v4.1-flash" not in match_keys("deepseek-v4.1-flash-batch")


def test_match_keys_swaps_version_separator():
    """版本号点 / 连字符互换：`seed-2.1-turbo` 与 `seed-2-1-turbo` 都能查。

    只互换夹在数字之间的那一枚：词间的 `.` 不是版本号（`kimi.k3` 不会变成
    `kimi-k3`）。
    """
    keys = match_keys("Doubao-Seed-2.1-Turbo")
    assert {"seed-2.1-turbo", "seed-2-1-turbo"} <= keys
    assert {"glm-5.3", "glm-5-3"} <= match_keys("glm-5.3")
    assert match_keys("kimi.k3") == {"kimi.k3"}

def test_lookup_matches_across_separator_spelling():
    """一边写点、一边写连字符：能匹配到（上游实测混用两种写法）。"""
    table = {"seed-2-1-turbo": {"source": "openrouter"}}
    assert lookup(table, "Doubao-Seed-2.1-Turbo") == {"source": "openrouter"}
    # 两个键都在且值不同 → 歧义，拒配（宁可没有分数也不错配）
    ambiguous = {"seed-2-1-turbo": {"a": 1}, "seed-2.1-turbo": {"b": 2}}
    assert lookup(ambiguous, "Doubao-Seed-2.1-Turbo") is None

def test_lookup_returns_unique_hit_only():
    """命中唯一才返回；多条歧义 / 查不到一律 None（不猜、不取其一）。"""
    table: BenchmarkTable = {"glm-5.3": {"source": "openrouter"}}
    assert lookup(table, "GLM-5.3") == {"source": "openrouter"}
    assert lookup(table, "glm-5.3-flash") is None      # 前缀不算匹配
    assert lookup({}, "glm-5.3") is None
    # 同键两个不等值 → 该键无判别力
    ambiguous = {"seed-2.1-turbo": {"source": "a"},
                 "doubao-seed-2.1-turbo": {"source": "b"}}
    assert lookup(ambiguous, "Doubao-Seed-2.1-Turbo") is None

def test_lookup_returns_when_all_matches_agree():
    """同键多条但值等值（不是歧义）时返回该值。"""
    table = {"seed-2.1-turbo": {"source": "a"},
             "doubao-seed-2.1-turbo": {"source": "a"}}
    assert lookup(table, "Doubao-Seed-2.1-Turbo") == {"source": "a"}

def test_build_table_first_wins():
    """同一键被两个值登记时保留先到的那条（结果确定）。"""
    table = build_table([(["m"], 1), (["m"], 2)])
    assert table == {"m": 1}

# ------------------------------------------------------------ 价表构建

def test_build_price_table_converts_per_token_to_per_million():
    """上游按 USD/token 给字符串小数 → 转 USD/百万 token。cache_read 缺失按
    input 原价（不打折也不免费）；completion 缺失按 0。"""
    raw = {"data": [
        {"id": "z-ai/glm-5.3", "name": "Z.ai: GLM 5.3",
         "pricing": {"prompt": "0.000000039", "completion": "0.0000048",
                     "input_cache_read": "0.000000038"}},
        {"id": "ok/no-cache", "name": "No Cache",
         "pricing": {"prompt": "0.000001"}},
    ]}
    table = build_price_table(raw)
    assert table["glm-5.3"] == pytest.approx((0.039, 4.8, 0.038))
    # 展示名一路也能查；无缓存价 → 按 input 原价（1.0），无输出价 → 0
    assert table["no-cache"] == pytest.approx((1.0, 0.0, 1.0))

@pytest.mark.parametrize("raw", [None, [], "x", 5, {}, {"data": "not-a-list"}])
def test_build_price_table_non_dict_or_bad_data_is_empty(raw):
    assert build_price_table(raw) == {}

def test_build_price_table_skips_bad_entries():
    """结构异常一律跳过：非 dict 条目、无 id/name、pricing 非 dict、无输入价 /
    负数 / 布尔 / 非数值。"""
    raw = {"data": [
        {"id": "good", "pricing": {"prompt": 1, "completion": 2}},
        {"pricing": {"prompt": 1}},                       # 无 id/name → 跳过
        {"id": "nocost"},                                 # 无 pricing
        {"id": "costnotdict", "pricing": "x"},            # pricing 非 dict
        {"id": "noinput", "pricing": {"completion": 1}},  # 无输入价
        {"id": "nullinput", "pricing": {"prompt": None}},
        {"id": "neginput", "pricing": {"prompt": -1}},
        {"id": "boolinput", "pricing": {"prompt": True}},
        {"id": "notanumber", "pricing": {"prompt": "abc"}},
        {"id": "emptyinput", "pricing": {"prompt": "  "}},
        "not-a-dict",
        {"id": 5, "pricing": {"prompt": 1}},              # id 非串且无 name → 跳过
    ]}
    # 数值 prompt = 1（USD/token）→ 1e6 USD/百万 token
    table = build_price_table(raw)
    assert set(table) == {"good"}
    assert table["good"] == pytest.approx((1e6, 2e6, 1e6))

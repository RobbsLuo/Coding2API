"""OpenRouter 模型目录（价格 + 明细 + 能力分）与统一匹配。

覆盖四条主线：
1. `model_match`：候选键生成、`lookup` 的唯一命中/歧义拒绝/查不到、`build_table`
   的 first-wins；
2. `benchmarks`：三张表建表（价格 / 明细 / 能力分，结构异常逐条跳过、null 指数
   不带）、快照落盘/读回（版本不符、过旧、坏条目）、抓取（MockTransport 成功与
   HTTP 失败上抛）；
3. 后台刷新与启动预热：单一 `openrouter_catalog` 任务一次抓取换入三张表；
4. 端到端：`/v1/models`、`/api/playground/models`、`/api/model-catalog` 三处
   都把分数随条目透出，匹配不到就不带字段，表为空时行为与加字段前一致。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest

from src import main
from src.benchmarks import (
    CATALOG_FILENAME,
    CATALOG_VERSION,
    MAX_AGE_SECONDS,
    BenchmarkTable,
    build_benchmark_table,
    build_model_catalog,
    build_price_table,
    fetch_openrouter_models,
    load_benchmarks,
    load_catalog_snapshot,
    save_catalog,
)
from src.config import Settings
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository
from src.model_match import build_table, lookup, match_keys
from src.pricing import estimate_cost_usd

from .conftest import SECRET

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


# --------------------------------------------------------------- 模型目录构建

def test_build_model_catalog_captures_metadata():
    """同口径选条，保留 OpenRouter 的完整明细（名称 / 上下文 / 模态 / 能力 / 价格）。"""
    raw = {"data": [{
        "id": "z-ai/glm-5.3",
        "name": "Z.ai: GLM 5.3",
        "context_length": 1048576,
        "knowledge_cutoff": "2026-02-16",   # 上游给的就是日期字符串
        "architecture": {"input_modalities": ["text", "image", 7],
                         "output_modalities": ["text"]},
        "top_provider": {"max_completion_tokens": 131072},
        "supported_parameters": ["reasoning", "tools", "structured_outputs"],
        "pricing": {"prompt": "0.000001", "completion": "0.000005",
                    "input_cache_read": "0.0000001",
                    "input_cache_write": "0.00000125"},
    }]}
    catalog = build_model_catalog(raw)
    assert set(catalog) == {"z-ai/glm-5.3"}
    row = catalog["z-ai/glm-5.3"]
    assert row["id"] == "z-ai/glm-5.3"
    assert row["provider"] == "z-ai"          # id 的厂商前缀
    assert row["name"] == "Z.ai: GLM 5.3"
    assert row["knowledge"] == "2026-02-16"
    assert row["context"] == 1048576 and row["max_output"] == 131072
    assert row["input_modalities"] == ["text", "image"]     # 非串元素丢弃
    assert row["output_modalities"] == ["text"]
    assert row["attachment"] is True
    assert row["reasoning"] is True and row["tool_call"] is True
    assert row["structured_output"] is True
    assert row["input"] == pytest.approx(1.0) and row["output"] == pytest.approx(5.0)
    assert row["cache_read"] == pytest.approx(0.1)
    assert row["cache_write"] == pytest.approx(1.25)
    # 旧目录有、OpenRouter 没有的三列：给 None / False，不改前端契约
    assert row["family"] is None and row["release_date"] is None
    assert row["open_weights"] is False


def test_build_model_catalog_tolerates_missing_fields():
    """缺字段 / 类型不符一律收敛为 None / 空 / False；缓存读缺失按 input 原价。"""
    raw = {"data": [{
        "id": "m",
        "name": "",
        "context_length": True,              # 布尔 → None
        "architecture": None,                # 非 dict → 空模态
        "pricing": {"prompt": "0.000002"},   # 无 output / cache_read / cache_write
        "supported_parameters": "not-a-list",
    }]}
    row = build_model_catalog(raw)["m"]
    assert row["name"] is None
    assert row["context"] is None and row["max_output"] is None
    assert row["input_modalities"] == [] and row["output_modalities"] == []
    assert row["output"] == 0.0
    assert row["cache_read"] == pytest.approx(2.0)      # 缺失按 input 原价
    assert row["cache_write"] is None
    assert row["reasoning"] is False and row["structured_output"] is False
    assert row["family"] is None
    # top_provider / architecture 整体非 dict 的分支
    raw2 = {"data": [{"id": "m", "top_provider": "bad", "architecture": "bad",
                      "pricing": {"prompt": 1}}]}
    assert build_model_catalog(raw2)["m"]["max_output"] is None


@pytest.mark.parametrize("cutoff,expected", [
    ("2026-02-16", "2026-02-16"),          # 上游真实形态：日期字符串
    ("  2026-02-16  ", "2026-02-16"),      # 两端空白照收
    (1750000000, "2025-06-15"),            # 数值（epoch 秒）仍兼容
    ("2026/02/16", None),                  # 别的日期格式不猜
    ("2026-2-16", None),                   # 不补零
    ("20260216", None),
    ("", None),
    (None, None),
    (-1, None),
    (True, None),
    (1e30, None),                          # 溢出的 epoch → None，不抛
])
def test_build_model_catalog_knowledge_cutoff_shapes(cutoff, expected):
    """知识截止只认 `YYYY-MM-DD`（上游真实形态）；别的写法宁可显示 — 也不给错日期。"""
    raw = {"data": [{"id": "m", "knowledge_cutoff": cutoff,
                     "pricing": {"prompt": "0.000001"}}]}
    assert build_model_catalog(raw)["m"]["knowledge"] == expected


def test_build_model_catalog_skips_unpriced():
    """没有刊例价的条目不进目录（页面「成本」列无意义）。"""
    raw = {"data": [{"id": "m", "name": "M"}]}     # 无 pricing
    assert build_model_catalog(raw) == {}


def test_build_model_catalog_empty_on_bad_raw():
    assert build_model_catalog(None) == {}
    assert build_model_catalog({"data": "x"}) == {}


def test_build_model_catalog_provider_without_slash():
    """id 无 '/' 时 provider 列给空串（不臆造厂商）。"""
    raw = {"data": [{"id": "bagelmix", "name": "Bagel", "pricing": {"prompt": 1}}]}
    assert build_model_catalog(raw)["bagelmix"]["provider"] == ""


# --------------------------------------------------------------- 能力分构建

_OR_PAYLOAD = {
    "data": [
        {"id": "z-ai/glm-5.3", "name": "Z.ai: GLM 5.3",
         "pricing": {"prompt": "0.000001", "completion": "0.000002"},
         "benchmarks": {"artificial_analysis": {
             "intelligence_index": 44.8, "coding_index": 74.8,
             "agentic_index": 53.1}}},
        {"id": "moonshotai/kimi-k3", "name": "MoonshotAI: Kimi K3",
         "pricing": {"prompt": "0.0000005", "completion": "0.000001"},
         "benchmarks": {"artificial_analysis": {
             "intelligence_index": 43.6, "coding_index": 76.2,
             "agentic_index": None}}},
        {"id": "xai/grok-no-score", "name": "Grok",
         "pricing": {"prompt": "0.000002"},
         "benchmarks": {"artificial_analysis": {
             "intelligence_index": None, "coding_index": None,
             "agentic_index": None}}},
        {"id": "broken/no-benchmarks", "name": "Broken"},
    ],
}


def test_build_benchmark_table_indexes_by_match_keys():
    """三项指数入表；null 不带字段；完全无分数的模型不入表。"""
    table = build_benchmark_table(_OR_PAYLOAD)
    assert table["glm-5.3"] == {
        "source": "openrouter", "source_model": "z-ai/glm-5.3",
        "intelligence_index": 44.8, "coding_index": 74.8,
        "agentic_index": 53.1}
    # 展示名一路也能查（上游 id 带厂商前缀）
    assert table["kimi-k3"]["intelligence_index"] == 43.6
    assert "agentic_index" not in table["kimi-k3"]
    assert "grok-no-score" not in table


@pytest.mark.parametrize("raw", [
    None, [], "string", 42,
    {},
    {"data": "not-a-list"},
    {"data": [None, "x", 5]},
    {"data": [{"id": "a", "benchmarks": {"artificial_analysis": "bad"}}]},
    {"data": [{"id": "a", "benchmarks": "bad"}]},
    {"data": [{"benchmarks": {"artificial_analysis": {"intelligence_index": 1}}}]},
    {"data": [{"id": "a", "benchmarks": {"artificial_analysis": {
        "intelligence_index": True, "coding_index": -1, "agentic_index": "x"}}}]},
    {"data": [{"id": "a", "benchmarks": {"other_block": {"intelligence_index": 1}}}]},
])
def test_build_benchmark_table_tolerates_malformed(raw):
    """结构异常/非数值一律安静跳过，绝不让模型列表挂掉。"""
    assert build_benchmark_table(raw) == {}


def test_build_benchmark_table_rejects_nan_and_infinite():
    """NaN / inf 不是有效指数：不当字段收。"""
    table = build_benchmark_table({"data": [
        {"id": "a", "benchmarks": {"artificial_analysis": {
            "intelligence_index": float("nan"), "coding_index": float("inf"),
             "agentic_index": float("-inf")}}},
        {"id": "b", "benchmarks": {"artificial_analysis": {
            "intelligence_index": 0}}},
    ]})
    assert table == {"b": {"source": "openrouter", "source_model": "b",
                           "intelligence_index": 0}}


# --------------------------------------------------------------- 网络拉取

async def test_fetch_openrouter_models_parses_and_raises():
    """MockTransport 下成功返回 JSON；HTTP 错误向上抛（调用方降级）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_OR_PAYLOAD)

    raw = await fetch_openrouter_models(transport=httpx.MockTransport(handler))
    assert raw == _OR_PAYLOAD

    def failing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_openrouter_models(transport=httpx.MockTransport(failing))


async def test_fetch_openrouter_models_default_transport_branch(monkeypatch):
    """不注入 transport 时走真实 AsyncClient 构造（替身覆盖该分支）。"""
    import httpx

    class FakeClient:
        def __init__(self, **_kwargs):
            self.kwargs = _kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, url):
            return httpx.Response(200, json=_OR_PAYLOAD,
                                  request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    fetched = await fetch_openrouter_models("https://x")
    assert build_benchmark_table(fetched)["glm-5.3"]["coding_index"] == 74.8


# --------------------------------------------------------- 落盘快照（三表一体）

def test_save_and_load_catalog_roundtrip(tmp_path):
    """落盘 → 读回：三张表内容一致，且共用同一个保存时刻。"""
    prices = {"glm-5.3": (1.0, 2.0, 0.5)}
    benchmarks = build_benchmark_table(_OR_PAYLOAD)
    catalog = build_model_catalog({"data": [
        {"id": "z-ai/glm-5.3", "name": "GLM", "pricing": {"prompt": 1}}]})
    before = time.time()
    save_catalog(str(tmp_path), prices, benchmarks, catalog)
    loaded_prices, loaded_bench, loaded_cat, saved_at = load_catalog_snapshot(
        str(tmp_path))
    assert loaded_prices == prices
    assert loaded_bench == benchmarks
    assert loaded_cat == catalog
    assert saved_at is not None and before <= saved_at <= time.time()
    assert load_benchmarks(str(tmp_path)) == benchmarks


def test_catalog_path_filename(tmp_path):
    from src.benchmarks import catalog_path

    assert catalog_path(str(tmp_path)).endswith(CATALOG_FILENAME)


def test_load_catalog_missing_file_is_empty(tmp_path):
    assert load_catalog_snapshot(str(tmp_path)) == ({}, {}, {}, None)
    assert load_benchmarks(str(tmp_path)) == {}


def test_load_catalog_version_mismatch_is_empty(tmp_path, caplog):
    save_catalog(str(tmp_path), {"m": (1.0, 2.0, 0.5)}, {}, {})
    payload = json.loads((tmp_path / CATALOG_FILENAME).read_text(encoding="utf-8"))
    payload["version"] = CATALOG_VERSION + 1
    (tmp_path / CATALOG_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert load_catalog_snapshot(str(tmp_path)) == ({}, {}, {}, None)
    assert any("模型目录版本不匹配" in r.getMessage() for r in caplog.records)


def test_load_catalog_expired_is_empty(tmp_path):
    save_catalog(str(tmp_path), {"m": (1.0, 2.0, 0.5)}, {}, {})
    assert load_catalog_snapshot(str(tmp_path),
                                 now=time.time() + MAX_AGE_SECONDS + 10) == ({}, {}, {}, None)


def test_load_catalog_broken_json_is_empty(tmp_path, caplog):
    (tmp_path / CATALOG_FILENAME).write_text("{not json", encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert load_catalog_snapshot(str(tmp_path)) == ({}, {}, {}, None)
    assert any("模型目录读取失败" in r.getMessage() for r in caplog.records)


def test_load_catalog_saved_at_missing_is_error(tmp_path, caplog):
    payload = {"version": CATALOG_VERSION, "prices": {}, "benchmarks": {},
               "models": {}}
    (tmp_path / CATALOG_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert load_catalog_snapshot(str(tmp_path)) == ({}, {}, {}, None)
    assert any("模型目录读取失败" in r.getMessage() for r in caplog.records)


def test_load_catalog_skips_bad_prices(tmp_path):
    """坏价格行跳过而不拖垮整表：非 list / 长度不对 / 含非数值 / 负值 / 布尔。"""
    payload = {
        "version": CATALOG_VERSION, "saved_at": time.time(),
        "prices": {
            "good": [1, 2, 3],
            "notlist": "x",
            "wronglen": [1, 2],
            "hasnull": [1, None, 3],
            "neg": [1, -2, 3],
            "notanumber": [1, "x", 3],
            "boolval": [1, True, 3],
            "": [1, 2, 3],
        },
        "benchmarks": {}, "models": {},
    }
    (tmp_path / CATALOG_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    assert load_catalog_snapshot(str(tmp_path))[0] == {"good": (1.0, 2.0, 3.0)}


def test_load_catalog_skips_bad_benchmarks(tmp_path):
    """缺指数字段的记录 / 非 dict 值 / 空键一律不收。"""
    payload = {
        "version": CATALOG_VERSION, "saved_at": time.time(), "prices": {},
        "models": {},
        "benchmarks": {"good": {"source": "openrouter", "intelligence_index": 1.0},
                       "bad": {"source": "openrouter"},      # 缺指数字段
                       "worse": "not-a-dict",
                       "": {"intelligence_index": 2.0}},
    }
    (tmp_path / CATALOG_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    assert load_catalog_snapshot(str(tmp_path))[1] == {
        "good": {"source": "openrouter", "intelligence_index": 1.0}}


def test_load_catalog_skips_bad_models(tmp_path):
    """坏条目（非 dict）跳过而不拖垮整份目录。"""
    payload = {"version": CATALOG_VERSION, "saved_at": time.time(),
               "prices": {}, "benchmarks": {},
               "models": {"good": {"id": "good"}, "bad": "x", "": {"id": ""}}}
    (tmp_path / CATALOG_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    assert load_catalog_snapshot(str(tmp_path))[2] == {"good": {"id": "good"}}


def test_load_catalog_non_dict_sections_are_empty(tmp_path):
    """三块整体类型不对（非 dict）时各自退空，不抛。"""
    payload = {"version": CATALOG_VERSION, "saved_at": time.time(),
               "prices": [], "benchmarks": "x", "models": 5}
    (tmp_path / CATALOG_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    assert load_catalog_snapshot(str(tmp_path)) == ({}, {}, {}, payload["saved_at"])


def test_save_catalog_failure_is_logged(tmp_path, monkeypatch, caplog):
    """落盘失败只记日志，不抛（绝不影响模型列表）。"""
    import logging
    import os

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    save_catalog(str(tmp_path), {"m": (1.0, 2.0, 0.5)}, {}, {"m": {"id": "m"}})
    assert any("模型目录落盘失败" in r.getMessage()
               for r in caplog.records if r.levelno >= logging.WARNING)


# ------------------------------------------------------- 成本估算接价表

def test_estimate_cost_matches_across_openrouter_prefix():
    """价表由 OpenRouter 构建：`z-ai/glm-5.3` 能对上本项目键 `glm-5.3`。"""
    table = build_price_table({"data": [{
        "id": "z-ai/glm-5.3", "pricing": {"prompt": "0.000001",
                                          "completion": "0.000002"}}]})
    cost = estimate_cost_usd(table, "glm-5.3", input_tokens=1_000_000,
                             output_tokens=0, cached_tokens=0)
    assert cost == pytest.approx(1.0)


# ------------------------------------------------------------ 端到端注入

def _benchmarks_app(tmp_path, monkeypatch, *, table: BenchmarkTable | None,
                    raw: Any = _OR_PAYLOAD):
    """带真实装配的 app，并直接把能力分表塞进 app.state（不打上游）。

    `raw` 是 OpenRouter 抓取的替身返回：默认给带两个分数的正常表，传 `` 模拟
    「上游不可用」（此时启动预热与后台刷新都拿不到分数，表保持空）。
    """
    from src.main import build_app

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")

    async def fake_fetch(_url):
        return json.loads(json.dumps(raw))

    monkeypatch.setattr(main, "fetch_openrouter_models", fake_fetch)
    app = build_app(settings, providers={})
    # 就地换值而不是重新绑定：`build_app` 里的刷新协程持有的是**同一个 dict
    # 引用**，重新绑定会让它写到旧对象上（与生产「换引用会让路由读到旧表」
    # 同一个坑）。
    app.state.model_benchmarks.clear()
    app.state.model_benchmarks.update(table or {})
    return app


def _seed_models(services, *, models: dict) -> None:
    """把渠道模型表塞进缓存并 publish 别名表，随后即可打 /v1/models。

    在 lifespan **之后**调用：启动预热会重拉各渠道（`_warm_model_list`），
    早于它填的缓存会被覆盖。trae 是否「已连接」由 `credential_providers`
    按凭证判定，这里顺带给该渠道加一条凭证，与线上「接入渠道」等价。
    """
    from src.api import models as models_api
    from src.provider.base import Model

    services.credentials.add(provider="trae", credential_data={"bearer_token": "t"})
    services.model_list_cache["trae"] = {
        key: (value if isinstance(value, Model) else Model(id=key, name=value))
        for key, value in models.items()}
    models_api.publish_aliases(services, {"trae"})


def test_v1_models_and_playground_carry_benchmarks(tmp_path, monkeypatch):
    """/v1/models 与 /api/playground/models 都随条目透出分数（同一份表）。"""
    from fastapi.testclient import TestClient

    app = _benchmarks_app(tmp_path, monkeypatch, table=None)
    with TestClient(app):
        pass
    _seed_models(app.state.services, models={
        "glm-5.3": "GLM 5.3",
        "glm-5.3-flash": "GLM 5.3 Flash",
    })
    app.state.model_benchmarks.update(build_benchmark_table(_OR_PAYLOAD))
    with TestClient(app) as client:
        key = _create_api_key(client)
        data = client.get("/v1/models", headers={"Authorization": f"Bearer {key}"}).json()
        rows = {row["id"]: row for row in data["data"]}
        assert rows["glm-5.3"]["benchmarks"]["intelligence_index"] == 44.8
        # 前缀不算匹配：没有分数就不带该字段，而不是带一个错的值
        assert "benchmarks" not in rows["glm-5.3-flash"]


def test_models_endpoint_without_table_has_no_benchmarks(tmp_path, monkeypatch):
    """能力表为空（拉取失败 / 未装配）时，条目与加字段前完全一致。"""
    from fastapi.testclient import TestClient

    # 上游不可用（raw={}）→ 启动预热也拉不到，表保持空
    app = _benchmarks_app(tmp_path, monkeypatch, table={}, raw={})
    with TestClient(app):
        pass
    _seed_models(app.state.services, models={"glm-5.3": "GLM 5.3"})
    with TestClient(app) as client:
        key = _create_api_key(client)
        data = client.get("/v1/models", headers={"Authorization": f"Bearer {key}"}).json()
        assert all("benchmarks" not in row for row in data["data"])


def test_playground_models_endpoint_requires_session(tmp_path, monkeypatch):
    """未登录访问 playground 列表仍 401（不带任何分数）。"""
    from fastapi.testclient import TestClient

    app = _benchmarks_app(tmp_path, monkeypatch, table=None)
    with TestClient(app) as client:
        assert client.get("/api/playground/models").status_code == 401


def test_model_catalog_endpoint_merges_benchmarks(tmp_path, monkeypatch):
    """「模型列表」页按同一口径给行补分，无分不带字段。"""
    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token

    catalog = {
        "z-ai/glm-5.3": {"id": "z-ai/glm-5.3", "name": "GLM 5.3",
                         "provider": "z-ai", "input": 1.0, "output": 2.0,
                         "cache_read": 0.1},
        "other/model": {"id": "other/model", "name": "No Score", "provider": "p",
                        "input": 0.5, "output": 1.0, "cache_read": 0.05},
    }
    save_catalog(str(tmp_path), {}, {}, catalog)
    # raw={} → 启动预热拿不到上游数据、失败退出，落盘明细目录得以保留（否则预热会
    # 用替身数据把它覆盖掉）；能力分表由 snapshot/helper 提供。
    app = _benchmarks_app(tmp_path, monkeypatch,
                          table=build_benchmark_table(_OR_PAYLOAD), raw={})
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        payload = client.get("/api/model-catalog").json()
    rows = {row["id"]: row for row in payload["models"]}
    assert rows["z-ai/glm-5.3"]["benchmarks"]["coding_index"] == 74.8
    assert "benchmarks" not in rows["other/model"]
    assert payload["saved_at"] is not None


# ---------------------------------------------------- 后台刷新（单一任务三表）

async def test_refresh_openrouter_catalog_roundtrip(tmp_path, monkeypatch):
    """后台刷新一轮：拉 OpenRouter → 建三表 → 落盘 → 就地换入，回报条目数。"""
    from fastapi.testclient import TestClient

    app = _benchmarks_app(tmp_path, monkeypatch, table=None)
    with TestClient(app):
        pass
    async with app.router.lifespan_context(app):
        runner = app.state.task_runner
        assert await runner._guarded(runner._openrouter_catalog(),
                                     "模型目录刷新（OpenRouter）",
                                     key="openrouter_catalog")
        run = runner.status.get("openrouter_catalog")
        assert run.ok is True and run.report == {
            "models": len(app.state.price_table)}
    assert app.state.price_table["glm-5.3"] == pytest.approx((1.0, 2.0, 1.0))
    assert app.state.model_benchmarks["glm-5.3"]["intelligence_index"] == 44.8
    assert app.state.price_saved_at is not None
    # 明细目录也换入了（与价表同一次抓取）
    assert app.state.models_dev_catalog["z-ai/glm-5.3"]["context"] is None
    # 落盘快照与回灌一致（下次启动零上游请求）
    assert load_benchmarks(str(tmp_path)) == app.state.model_benchmarks


async def test_refresh_openrouter_catalog_rejects_empty(tmp_path, monkeypatch):
    """拉到空价表视为失败：不把已有的数据清空，只记运行态。"""
    from fastapi.testclient import TestClient

    app = _benchmarks_app(tmp_path, monkeypatch, table=None)
    app.state.price_table["glm-5.3"] = (1.0, 2.0, 1.0)
    app.state.model_benchmarks.update({"glm-5.3": {"source": "openrouter",
                                                   "intelligence_index": 1.0}})

    async def empty_fetch(_url):
        return {}

    monkeypatch.setattr(main, "fetch_openrouter_models", empty_fetch)
    with TestClient(app):
        pass
    async with app.router.lifespan_context(app):
        runner = app.state.task_runner
        ok = await runner._guarded(runner._openrouter_catalog(),
                                   "模型目录刷新（OpenRouter）",
                                   key="openrouter_catalog")
        assert ok is False
    assert app.state.price_table == {"glm-5.3": (1.0, 2.0, 1.0)}
    assert app.state.model_benchmarks == {"glm-5.3": {"source": "openrouter",
                                                      "intelligence_index": 1.0}}


async def test_startup_warm_model_catalog_only_when_missing(caplog):
    """启动预热：无落盘快照时才补拉一次，有快照直接跳过（不打上游）。"""
    calls: list[str] = []

    async def refresh():
        calls.append("ran")
        return {"models": 1}

    # 有表：直接返回，不拉
    await main._warm_model_catalog(refresh, {"m": (1.0, 2.0, 1.0)})
    assert calls == []

    # 无表：补拉一次
    await main._warm_model_catalog(refresh, {})
    assert calls == ["ran"]

    # 补拉失败只记日志，不抛
    async def failing():
        raise RuntimeError("boom")

    with caplog.at_level("WARNING"):
        await main._warm_model_catalog(failing, {})
    assert any("启动预热模型目录失败" in r.getMessage() for r in caplog.records)


# -------------------------------------------------- 应用装配：快照回灌 / 端点

class _PriceStub:
    """最小 provider：只满足启动预热对 list_models 的调用。"""

    id = "kilo"

    async def list_models(self, _data):
        return []


def _price_app(tmp_path, **env):
    from src.main import build_app

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root", **env)
    return build_app(settings, providers={"kilo": _PriceStub()})


def test_app_restores_catalog_snapshot_and_estimates_cost(tmp_path):
    """启动回灌落盘模型目录：新写入的明细立刻能估出成本（零上游请求）。"""
    from fastapi.testclient import TestClient

    save_catalog(str(tmp_path), {"glm-5.2": (1.0, 2.0, 0.1)}, {},
                 {"glm-5.2": {"id": "glm-5.2", "name": "GLM"}})
    app = _price_app(tmp_path)
    with TestClient(app):
        assert app.state.price_table == {"glm-5.2": (1.0, 2.0, 0.1)}
        assert app.state.models_dev_catalog == {
            "glm-5.2": {"id": "glm-5.2", "name": "GLM"}}
        app.state.stats_collector.record(
            username="u", provider="trae", model="glm-5.2", ok=True,
            input_tokens=1_000_000, output_tokens=0)
        row = app.state.stats_collector._db.connect().execute(
            "SELECT cost_usd FROM usage_events").fetchone()
    assert row["cost_usd"] == pytest.approx(1.0)


def test_app_restore_catalog_failure_is_logged(tmp_path, monkeypatch, caplog):
    """恢复快照抛异常只记日志，服务照常启动。"""
    from fastapi.testclient import TestClient

    def boom(_data_dir):
        raise RuntimeError("bad snapshot")

    monkeypatch.setattr(main, "load_catalog_snapshot", boom)
    app = _price_app(tmp_path)
    with caplog.at_level("WARNING"), TestClient(app):
        assert app.state.price_table == {}
        assert app.state.models_dev_catalog == {}
        assert app.state.price_saved_at is None
    assert any("恢复落盘模型目录失败" in r.getMessage() for r in caplog.records)


def test_openrouter_catalog_task_status_and_interval(tmp_path):
    from fastapi.testclient import TestClient

    app = _price_app(tmp_path)
    with TestClient(app):
        status = {item["key"]: item for item in app.state.task_runner.task_status()}
    assert status["openrouter_catalog"]["interval_seconds"] == 86400
    assert status["openrouter_catalog"]["enabled"] is True
    assert status["openrouter_catalog"]["last_ok"] is None


def test_build_runner_without_openrouter_catalog_keeps_task_hidden(tmp_path):
    """不注入刷新协程时不装配这条循环（老调用方/测试保持原行为）。"""
    from src.tasks.runner import build_runner

    db = Database(tmp_path / "c.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    config = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                      OPENROUTER_CATALOG_MINUTES=1)
    runner = build_runner(credentials, {}, None, config)
    keys = {item["key"] for item in runner.task_status()}
    assert "openrouter_catalog" not in keys
    assert runner._openrouter_catalog_interval == 3600      # 下限 60 分钟
    db.close()


def _catalog_client(app):
    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token

    client = TestClient(app)
    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    return client


def test_model_catalog_endpoint_lists_sorted_catalog(tmp_path):
    """带登录可读全量模型目录：按模型 id 升序，含汇率与快照时间。"""
    save_catalog(str(tmp_path), {}, {}, {
        "glm-5.2": {"id": "glm-5.2", "name": "GLM 5.2", "provider": "zhipuai",
                    "context": 200000, "input": 1.0, "output": 2.0,
                    "cache_read": 0.1},
        "a-model": {"id": "a-model", "name": None, "provider": "p",
                    "input": 0.5, "output": 1.5, "cache_read": 0.05},
    })
    app = _price_app(tmp_path, USD_CNY_RATE=7)
    with _catalog_client(app) as client:
        payload = client.get("/api/model-catalog").json()
        assert payload["count"] == 2
        assert payload["currency"] == "USD"
        assert payload["usd_cny_rate"] == 7
        assert payload["saved_at"] == app.state.price_saved_at
    assert [row["id"] for row in payload["models"]] == ["a-model", "glm-5.2"]
    assert payload["models"][1] == {
        "id": "glm-5.2", "name": "GLM 5.2", "provider": "zhipuai",
        "context": 200000, "input": 1.0, "output": 2.0, "cache_read": 0.1}


def test_model_catalog_endpoint_empty_table(tmp_path, monkeypatch):
    """无快照时回空表 + saved_at=None（页面显示空态，不是错误）。"""
    async def no_models(_url):
        return {}

    monkeypatch.setattr(main, "fetch_openrouter_models", no_models)
    app = _price_app(tmp_path)
    with _catalog_client(app) as client:
        payload = client.get("/api/model-catalog").json()
    assert payload["models"] == [] and payload["count"] == 0
    assert payload["saved_at"] is None


def test_model_catalog_endpoint_requires_session(tmp_path):
    """未登录一律 401：模型列表页也要经过会话鉴权。"""
    from fastapi.testclient import TestClient

    save_catalog(str(tmp_path), {"m": (1.0, 2.0, 0.5)}, {}, {})
    app = _price_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/model-catalog").status_code == 401


def test_shutdown_cancels_inflight_catalog_warmup(tmp_path, monkeypatch):
    """关机时模型目录预热若仍在飞，取消它，别把上游请求带出事件循环。"""
    from fastapi.testclient import TestClient

    started = asyncio.Event()
    cancelled = []

    async def hang(_url):
        started.set()
        try:
            await asyncio.Event().wait()        # 永不返回，直到被取消
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr(main, "fetch_openrouter_models", hang)
    app = _price_app(tmp_path)
    with TestClient(app):
        # 预热在后台线程的事件循环里跑，轮询等它进入抓取（避免时序竞态）
        deadline = time.monotonic() + 2
        while not started.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started.is_set()                 # 预热已进入抓取
    assert cancelled == [True]                  # 关机时被取消


# --------------------------------------------------------- 汇率热更

def test_usd_cny_rate_is_hot_setting(tmp_path):
    from src.db.repo import RuntimeSettingsRepository
    from src.runtime_settings import load_runtime_settings

    db = Database(tmp_path / "r.sqlite3")
    apply_schema(db.connect())
    config = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                      USD_CNY_RATE=7.0)
    runtime = load_runtime_settings(config, RuntimeSettingsRepository(db))
    assert runtime.usd_cny_rate == 7.0
    runtime.set("usd_cny_rate", 6.5)
    assert runtime.usd_cny_rate == 6.5
    runtime.reset("usd_cny_rate")
    assert runtime.usd_cny_rate == 7.0
    db.close()


def test_openrouter_catalog_minutes_has_floor():
    from src.runtime_settings import HOT_BY_KEY

    spec = HOT_BY_KEY["openrouter_catalog_minutes"]
    assert spec.task == "openrouter_catalog"
    assert spec.floor == 60
    assert spec.minimum == 60


def _create_api_key(client) -> str:
    """用 admin 会话造一把 API Key（/v1/models 走 API Key 鉴权）。"""
    from src.auth.session import create_session_token

    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    response = client.post("/api/api-keys", json={"name": "bench"}, headers={
        "X-Requested-With": "XMLHttpRequest"})
    assert response.status_code == 200, response.text
    return response.json()["api_key"]

"""能力排行（Artificial Analysis 指数，经 OpenRouter 公开接口）与统一匹配。

覆盖三条主线：
1. `model_match`：候选键生成、`lookup` 的唯一命中/歧义拒绝/查不到、`build_table`
   的 first-wins；
2. `benchmarks`：建表（结构异常逐条跳过、null 指数不带）、快照落盘/读回（版本
   不符、过旧、坏条目）、抓取（MockTransport 成功与 HTTP 失败上抛）；
3. 端到端：`/v1/models`、`/api/playground/models`、`/api/model-catalog` 三处
   都把分数随条目透出，匹配不到就不带字段，表为空时行为与加字段前一致。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from src import main
from src.benchmarks import (
    BENCHMARKS_VERSION,
    BenchmarkTable,
    build_benchmark_table,
    fetch_openrouter_models,
    load_benchmarks,
    load_benchmarks_snapshot,
    save_benchmarks,
)
from src.model_match import build_table, lookup, match_keys

from .conftest import SECRET  # noqa: F401 - 与其它用例共用常量

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


# --------------------------------------------------------------- benchmarks

_OR_PAYLOAD = {
    "data": [
        {"id": "z-ai/glm-5.3", "name": "Z.ai: GLM 5.3",
         "benchmarks": {"artificial_analysis": {
             "intelligence_index": 44.8, "coding_index": 74.8,
             "agentic_index": 53.1}}},
        {"id": "moonshotai/kimi-k3", "name": "MoonshotAI: Kimi K3",
         "benchmarks": {"artificial_analysis": {
             "intelligence_index": 43.6, "coding_index": 76.2,
             "agentic_index": None}}},
        {"id": "xai/grok-no-score", "name": "Grok",
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


async def test_build_benchmark_table_rejects_nan_and_infinite():
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


async def test_fetch_openrouter_models_parses_and_raises(monkeypatch):
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


def test_save_and_load_benchmarks_roundtrip(tmp_path):
    """落盘 → 读回：内容一致，且保存时刻可见。"""
    table = build_benchmark_table(_OR_PAYLOAD)
    save_benchmarks(str(tmp_path), table)
    assert load_benchmarks(str(tmp_path)) == table
    loaded, saved_at = load_benchmarks_snapshot(str(tmp_path))
    assert loaded == table and saved_at is not None


def test_load_benchmarks_degrades_on_bad_snapshot(tmp_path):
    """缺失 / 损坏 / 版本不符 / 过旧一律退化为空表（不抛）。"""
    assert load_benchmarks(str(tmp_path)) == {}          # 文件不存在
    path = tmp_path / "model_benchmarks.json"

    path.write_text("{ broken", encoding="utf-8")
    assert load_benchmarks(str(tmp_path)) == {}

    path.write_text(json.dumps({"version": BENCHMARKS_VERSION + 1,
                                "saved_at": 0, "models": {}}), encoding="utf-8")
    assert load_benchmarks(str(tmp_path)) == {}

    path.write_text(json.dumps({"version": BENCHMARKS_VERSION,
                                "saved_at": 0, "models": {"m": {"x": 1}}}),
                    encoding="utf-8")
    assert load_benchmarks(str(tmp_path)) == {}          # 过旧（7 天上限）

    path.write_text(json.dumps({"version": BENCHMARKS_VERSION,
                                "saved_at": 1e18, "models": "bad"}), encoding="utf-8")
    assert load_benchmarks(str(tmp_path)) == {}          # models 非 dict


def test_load_benchmarks_skips_broken_records(tmp_path):
    """坏条目跳过而不拖垮整张表（缺指数字段的记录不收）。"""
    import time

    payload = {"version": BENCHMARKS_VERSION, "saved_at": time.time(),
               "models": {"good": {"source": "openrouter",
                                   "intelligence_index": 1.0},
                          "bad": {"source": "openrouter"},   # 缺指数字段
                          "worse": "not-a-dict"}}
    (tmp_path / "model_benchmarks.json").write_text(
        json.dumps(payload), encoding="utf-8")
    assert load_benchmarks(str(tmp_path)) == {"good": payload["models"]["good"]}


def test_save_benchmarks_failure_is_logged(tmp_path, monkeypatch, caplog):
    """落盘失败只记日志，不抛（绝不影响模型列表）。"""
    import logging
    import os

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    save_benchmarks(str(tmp_path), {"m": {"source": "openrouter"}})
    assert any("能力排行落盘失败" in r.getMessage()
               for r in caplog.records if r.levelno >= logging.WARNING)


# ------------------------------------------------------------ 端到端注入

def _benchmarks_app(tmp_path, monkeypatch, *, table: BenchmarkTable | None,
                    raw: Any = _OR_PAYLOAD):
    """带真实装配的 app，并直接把能力分表塞进 app.state（不打上游）。

    `raw` 是 OpenRouter 抓取的替身返回：默认给带两个分数的正常表，传 `{}` 模拟
    「上游不可用」（此时启动预热与后台刷新都拿不到分数，表保持空）。
    """
    from src.config import Settings
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
    from src.pricing import save_model_catalog

    save_model_catalog(str(tmp_path), {
        "z-ai/glm-5.3": {"id": "z-ai/glm-5.3", "name": "GLM 5.3",
                         "provider": "z-ai", "input": 1.0, "output": 2.0,
                         "cache_read": 0.1},
        "other/model": {"id": "other/model", "name": "No Score", "provider": "p",
                        "input": 0.5, "output": 1.0, "cache_read": 0.05},
    })
    app = _benchmarks_app(tmp_path, monkeypatch, table=None)
    app.state.model_benchmarks.update(build_benchmark_table(_OR_PAYLOAD))
    app.state.benchmark_saved_at = 123.0
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        payload = client.get("/api/model-catalog").json()
    rows = {row["id"]: row for row in payload["models"]}
    assert rows["z-ai/glm-5.3"]["benchmarks"]["coding_index"] == 74.8
    assert "benchmarks" not in rows["other/model"]
    assert payload["benchmark_saved_at"] == 123.0


async def test_refresh_benchmark_catalog_roundtrip(tmp_path, monkeypatch):
    """后台刷新一轮：拉 OpenRouter → 落盘 → 就地换入，回报条目数。"""
    from fastapi.testclient import TestClient


    app = _benchmarks_app(tmp_path, monkeypatch, table=None)
    with TestClient(app):
        pass
    async with app.router.lifespan_context(app):
        runner = app.state.task_runner
        assert await runner._guarded(runner._benchmark_catalog(),
                                     "能力排行刷新（OpenRouter）",
                                     key="benchmark_catalog")
        run = runner.status.get("benchmark_catalog")
        assert run.ok is True and run.report == {"models": 3}
    assert app.state.model_benchmarks["glm-5.3"]["intelligence_index"] == 44.8
    assert app.state.benchmark_saved_at is not None
    # 落盘快照与回灌一致（下次启动零上游请求）
    assert load_benchmarks(str(tmp_path)) == app.state.model_benchmarks


async def test_refresh_benchmark_catalog_rejects_empty(tmp_path, monkeypatch):
    """拉到空表视为失败：不把已有的分数清空，只记运行态。"""
    from fastapi.testclient import TestClient

    app = _benchmarks_app(tmp_path, monkeypatch, table=None)
    app.state.model_benchmarks.update({"glm-5.3": {"source": "openrouter",
                                                   "intelligence_index": 1.0}})

    async def empty_fetch(_url):
        return {}

    monkeypatch.setattr(main, "fetch_openrouter_models", empty_fetch)
    with TestClient(app):
        pass
    async with app.router.lifespan_context(app):
        runner = app.state.task_runner
        ok = await runner._guarded(runner._benchmark_catalog(),
                                   "能力排行刷新（OpenRouter）",
                                   key="benchmark_catalog")
        assert ok is False
    assert app.state.model_benchmarks == {"glm-5.3": {"source": "openrouter",
                                                      "intelligence_index": 1.0}}


async def test_startup_warm_benchmark_table_only_when_missing(tmp_path, monkeypatch, caplog):
    """启动预热：无落盘快照时才补拉一次，有快照直接跳过（不打上游）。"""
    from src import main as main_module

    calls: list[str] = []

    async def refresh():
        calls.append("ran")
        return {"models": 1}

    # 有表：直接返回，不拉
    await main_module._warm_benchmark_table(refresh, {"m": {}})
    assert calls == []

    # 无表：补拉一次
    await main_module._warm_benchmark_table(refresh, {})
    assert calls == ["ran"]

    # 补拉失败只记日志，不抛
    async def failing():
        raise RuntimeError("boom")

    with caplog.at_level("WARNING"):
        await main_module._warm_benchmark_table(failing, {})
    assert any("启动预热能力分表失败" in r.getMessage() for r in caplog.records)


def _create_api_key(client) -> str:
    """用 admin 会话造一把 API Key（/v1/models 走 API Key 鉴权）。"""
    from src.auth.session import create_session_token

    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    response = client.post("/api/api-keys", json={"name": "bench"}, headers={
        "X-Requested-With": "XMLHttpRequest"})
    assert response.status_code == 200, response.text
    return response.json()["api_key"]

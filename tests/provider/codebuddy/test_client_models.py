"""CodeBuddy 客户端：动态模型列表拉取与缓存。"""

from __future__ import annotations

import time

import httpx
import pytest

from src.provider.codebuddy.client import (
    DEFAULT_MODELS,
    CodeBuddyCredential,
    CodeBuddyProvider,
    UpstreamHTTPError,
)
from src.provider.codebuddy.events import UpstreamProtocolViolation
from tests.provider.codebuddy._support import client as _client


async def test_fetch_models_parses_config_and_caches():
    """/v3/config 动态拉取：data.models[].id，按 token+endpoint 缓存。"""
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"code": 0, "data": {"models": [
            {"id": "glm-5.2"}, {"id": "glm-5.3"}, {"id": ""}, "junk",
            {"id": "glm-5.2"},
        ]}})

    client = _client(handler)
    models = await client.fetch_models(CodeBuddyCredential(bearer_token="t"))
    assert [m.id for m in models] == ["glm-5.2", "glm-5.3"]
    await client.fetch_models(CodeBuddyCredential(bearer_token="t"))
    assert calls["n"] == 1                      # 命中缓存

async def test_fetch_models_cache_is_bounded():
    """模型缓存有容量上限：先淘汰过期条目，仍满则整体清空（LOW）。"""
    from src.provider.codebuddy.client import (
        _MODEL_CACHE,
        MODEL_CACHE_MAX_ENTRIES,
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"models": [
            {"id": "glm-5.2"}]}})

    client = _client(handler)
    # 缓存满且含过期条目 → 淘汰过期后写入新条目，不清空
    _MODEL_CACHE.clear()
    _MODEL_CACHE[("stale", "e")] = (0.0, [])
    for i in range(MODEL_CACHE_MAX_ENTRIES - 1):
        _MODEL_CACHE[(f"fresh{i}", "e")] = (time.time(), [])
    assert len(_MODEL_CACHE) == MODEL_CACHE_MAX_ENTRIES
    await client.fetch_models(CodeBuddyCredential(bearer_token="t"))
    assert ("stale", "e") not in _MODEL_CACHE
    assert len(_MODEL_CACHE) == MODEL_CACHE_MAX_ENTRIES

    # 缓存满且全部新鲜 → 整体清空，只留本次写入
    _MODEL_CACHE.clear()
    for i in range(MODEL_CACHE_MAX_ENTRIES):
        _MODEL_CACHE[(f"fresh{i}", "e")] = (time.time(), [])
    await client.fetch_models(CodeBuddyCredential(bearer_token="t"))
    assert len(_MODEL_CACHE) == 1

async def test_fetch_models_parses_metadata():
    """模型元数据解析：倍率（credits 字符串）/ token 上限 / 支持性 / 推理档位。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"models": [
            {"id": "default", "name": "Default", "credits": "x2.00 credits",
             "maxInputTokens": 200000, "maxOutputTokens": 24000,
             "supportsImages": False, "supportsToolCall": True},
            {"id": "glm-5.3", "credits": "bad-format"},
            {"id": "kimi-k3", "supportsReasoning": True,
             "reasoning": {"effort": "medium", "summary": "auto"}},
            {"id": "no-effort", "supportsReasoning": False,
             "reasoning": {"summary": "auto"}},
            {"id": "bad-reasoning", "reasoning": "not-a-dict"},
            {"id": "bad-effort", "reasoning": {"effort": 7}},
        ]}})

    models = await _client(handler).fetch_models(CodeBuddyCredential(bearer_token="t"))
    assert models[0].credit_rate == 2.0
    assert models[0].max_input_tokens == 200000
    assert models[0].max_output_tokens == 24000
    assert models[0].supports_images is False
    assert models[0].supports_tool_call is True
    # 未提供推理元数据 → 留空
    assert models[0].supports_reasoning is None
    assert models[0].default_effort is None
    # 格式不符 → 留空，不影响条目
    assert models[1].credit_rate is None
    # reasoning.effort → 默认档位；supportsReasoning → 能力标志
    assert models[2].supports_reasoning is True
    assert models[2].default_effort == "medium"
    assert models[3].supports_reasoning is False
    assert models[3].default_effort is None
    # reasoning 非 dict / effort 非字符串 → 一律留空（不编造）
    assert models[4].default_effort is None
    assert models[5].default_effort is None

@pytest.mark.parametrize("body", [
    {"code": 1, "msg": "no"},
    {"code": 0, "data": {}},
    {"code": 0, "data": {"models": "no"}},
    {"code": 0, "data": {"models": []}},
    "not-json",
])
async def test_fetch_models_rejects_bad_responses(body):
    def handler(_request: httpx.Request) -> httpx.Response:
        if isinstance(body, str):
            return httpx.Response(200, content=body)
        return httpx.Response(200, json=body)

    with pytest.raises(UpstreamProtocolViolation):
        await _client(handler).fetch_models(CodeBuddyCredential(bearer_token="t"))

async def test_fetch_models_http_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b"unauthorized")

    with pytest.raises(UpstreamHTTPError):
        await _client(handler).fetch_models(CodeBuddyCredential(bearer_token="t"))

async def test_provider_list_models_falls_back_to_static_on_failure():
    """动态拉取失败 → 回退静态表，保证客户端始终有可用列表。"""
    provider = CodeBuddyProvider(client=_client(
        lambda _r: httpx.Response(500, content=b"down")))
    models = await provider.list_models({"bearer_token": "t"})
    assert [m.id for m in models] == list(DEFAULT_MODELS)

async def test_provider_list_models_uses_dynamic_when_available():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"models": [
            {"id": "glm-5.2"}, {"id": "glm-5.3"},
        ]}})

    provider = CodeBuddyProvider(client=_client(handler))
    models = await provider.list_models({"bearer_token": "t"})
    assert [m.id for m in models] == ["glm-5.2", "glm-5.3"]

async def test_fetch_models_rejects_non_object_config():
    """config 返回 JSON 数组 → 显式失败（client 173-174）。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2])

    with pytest.raises(UpstreamProtocolViolation):
        await _client(handler).fetch_models(CodeBuddyCredential(bearer_token="t"))


async def test_provider_list_models_log_includes_type_when_message_empty(caplog):
    """裸 `TimeoutError` 消息为空 → 回退日志仍要带类名，不能只剩「: 」。"""
    class _Client:
        async def fetch_models(self, _credential):
            raise TimeoutError()

    provider = CodeBuddyProvider(client=_Client())
    with caplog.at_level("WARNING", logger="src.provider.codebuddy.client"):
        models = await provider.list_models({"bearer_token": "t"})
    assert [m.id for m in models] == list(DEFAULT_MODELS)
    assert any("TimeoutError" in record.getMessage() for record in caplog.records)

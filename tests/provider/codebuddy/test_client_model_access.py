"""CodeBuddy 国际站模型套餐可用性探活（Zen 式列表过滤）。

上游 `/v3/config` 返回全量模型，套餐限制只在真正调用时以 400 / 11158
暴露；这里验证「逐个探活 + 保守判据 + 缓存」三层行为。
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from src.provider.base import Model
from src.provider.codebuddy.client import (
    _MODEL_ACCESS_CACHE,
    CodeBuddyCredential,
    CodeBuddyProvider,
)
from src.provider.codebuddy.intl import CodeBuddyIntlProvider
from tests.provider.codebuddy._support import client as _client


@pytest.fixture(autouse=True)
def _clear_access_cache():
    _MODEL_ACCESS_CACHE.clear()
    yield
    _MODEL_ACCESS_CACHE.clear()


def _body(request: httpx.Request) -> dict:
    return json.loads(request.content)


def _models(*ids: str) -> list[Model]:
    return [Model(id=model_id) for model_id in ids]


async def test_filter_available_models_keeps_ok_and_inconclusive():
    """只有 11158 / 11102 剔除；限流、5xx、认证失败都保留（不确定 ≠ 不可用）。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model_id = _body(request)["model"]
        calls.append(model_id)
        if model_id == "denied":
            return httpx.Response(400, json={"code": 11158, "msg": "not allowed"})
        if model_id == "missing":
            return httpx.Response(400, json={"code": 11102, "msg": "no model"})
        if model_id == "limited":
            return httpx.Response(429, json={"code": 14003})
        if model_id == "boom":
            return httpx.Response(500, content=b"down")
        return httpx.Response(200, content=b"data: {}\n\n")

    client = _client(handler)
    models = _models("ok", "denied", "missing", "limited", "boom")
    kept = await client.filter_available_models(
        CodeBuddyCredential(bearer_token="t"), models)

    assert [model.id for model in kept] == ["ok", "limited", "boom"]
    assert sorted(calls) == sorted(model.id for model in models)

    # 第二次命中缓存：不再打上游
    calls.clear()
    kept = await client.filter_available_models(
        CodeBuddyCredential(bearer_token="t"), models)
    assert [model.id for model in kept] == ["ok", "limited", "boom"]
    assert calls == []


async def test_filter_available_models_all_denied_falls_back_to_full_list():
    """全部被拒绝时保留全量：避免一次探活异常让整个渠道从列表里消失。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": 11158})

    client = _client(handler)
    models = _models("a", "b")
    kept = await client.filter_available_models(
        CodeBuddyCredential(bearer_token="t"), models)
    assert [model.id for model in kept] == ["a", "b"]


async def test_access_cache_is_per_account_not_per_token_prefix():
    """同一 realm 的 token 前缀相同；缓存必须按账号隔离，否则付费/免费号串味。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(_body(request)["model"])
        return httpx.Response(200)

    client = _client(handler)
    shared_prefix = "same-jwt-prefix-" * 4
    account_a = CodeBuddyCredential(
        bearer_token=shared_prefix + "a", nickname="account-a")
    account_b = CodeBuddyCredential(
        bearer_token=shared_prefix + "b", nickname="account-b")

    await client.filter_available_models(account_a, _models("m"))
    await client.filter_available_models(account_b, _models("m"))
    assert calls == ["m", "m"]

    # 各自命中自己的缓存，不再探活
    await client.filter_available_models(account_a, _models("m"))
    await client.filter_available_models(account_b, _models("m"))
    assert calls == ["m", "m"]
    assert ("account-a", client.endpoint) in _MODEL_ACCESS_CACHE
    assert ("account-b", client.endpoint) in _MODEL_ACCESS_CACHE


async def test_access_cache_falls_back_to_token_hash_without_nickname():
    """手动凭证没有 nickname：用整 token 的 SHA-256，不用会碰撞的前缀。"""
    client = _client(lambda _request: httpx.Response(200))
    credential = CodeBuddyCredential(bearer_token="manual-token")
    await client.filter_available_models(credential, _models("m"))

    keys = list(_MODEL_ACCESS_CACHE)
    assert len(keys) == 1
    assert keys[0][0] != "manual-token"
    assert keys[0][1] == client.endpoint


async def test_filter_available_models_without_token_skips_probe():
    """静态兜底可能拿到空凭证（渠道刚建）：没有 token 时不发探活请求。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200)

    client = _client(handler)
    models = _models("a")
    assert await client.filter_available_models(
        CodeBuddyCredential(), models) == models
    assert calls == []


async def test_filter_available_models_empty_input_skips_probe():
    """空列表直接返回，不发任何请求。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200)

    client = _client(handler)
    assert await client.filter_available_models(
        CodeBuddyCredential(bearer_token="t"), []) == []
    assert calls == []


async def test_probe_access_timeout_and_network_error_are_inconclusive(monkeypatch):
    """超时与连接错误都返回 None（保留模型），不能误判为不可用。"""
    monkeypatch.setattr(
        "src.provider.codebuddy.client.MODEL_ACCESS_PROBE_TIMEOUT", 0.01)

    async def slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200)

    client = _client(slow)
    assert await client._probe_model_access(
        CodeBuddyCredential(bearer_token="t"), "slow") is None

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    client = _client(broken)
    assert await client._probe_model_access(
        CodeBuddyCredential(bearer_token="t"), "broken") is None


async def test_probe_access_reads_denied_code_from_error_body():
    """400 的 error envelope 才决定剔除；非 11158/11102 仍返回 None。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": 9999}})

    client = _client(handler)
    assert await client._probe_model_access(
        CodeBuddyCredential(bearer_token="t"), "x") is None


async def test_access_cache_is_bounded(monkeypatch):
    """缓存满：先清过期，仍满则整体清空（与模型表缓存同策略）。"""
    monkeypatch.setattr(
        "src.provider.codebuddy.client.MODEL_ACCESS_CACHE_MAX_ENTRIES", 2)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    client = _client(handler)
    credential = CodeBuddyCredential(bearer_token="t")

    _MODEL_ACCESS_CACHE[("stale", "e")] = (0.0, {})
    _MODEL_ACCESS_CACHE[("fresh", "e")] = (time.time(), {})
    await client.filter_available_models(credential, _models("a"))
    assert ("stale", "e") not in _MODEL_ACCESS_CACHE
    assert ("fresh", "e") in _MODEL_ACCESS_CACHE

    # 全是新鲜条目 → 整体清空，只留本次写入
    _MODEL_ACCESS_CACHE.clear()
    _MODEL_ACCESS_CACHE[("fresh1", "e")] = (time.time(), {})
    _MODEL_ACCESS_CACHE[("fresh2", "e")] = (time.time(), {})
    await client.filter_available_models(credential, _models("a"))
    assert len(_MODEL_ACCESS_CACHE) == 1


async def test_intl_list_models_filters_config_by_probe():
    """国际站 list_models：先拉 /v3/config，再按探活结果过滤。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v3/config":
            return httpx.Response(200, json={"code": 0, "data": {"models": [
                {"id": "ok"}, {"id": "denied"},
            ]}})
        if _body(request)["model"] == "denied":
            return httpx.Response(400, json={"code": 11158})
        return httpx.Response(200, content=b"data: {}\n\n")

    provider = CodeBuddyIntlProvider(client=_client(handler))
    models = await provider.list_models({"bearer_token": "intl-token"})
    assert [model.id for model in models] == ["ok"]


async def test_cn_provider_does_not_probe_models():
    """国内站无套餐门禁：列表保持原样，额外探活请求一次都不发。"""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"code": 0, "data": {"models": [
            {"id": "glm-5.2"}, {"id": "glm-5.3"},
        ]}})

    provider = CodeBuddyProvider(client=_client(handler))
    models = await provider.list_models({"bearer_token": "cn-token"})
    assert [model.id for model in models] == ["glm-5.2", "glm-5.3"]
    assert paths == ["/v3/config"]

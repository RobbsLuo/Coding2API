"""小浣熊上游客户端：请求体、SSE、模型目录、积分余额。"""

from __future__ import annotations

import json

import httpx
import pytest

from src.provider.base import ErrKind, EventKind
from src.provider.raccoon.client import (
    EP_BALANCE,
    EP_BILLS,
    EP_CHAT,
    EP_HEARTBEAT,
    EP_MODELS,
    RaccoonClient,
    UpstreamHTTPError,
    UpstreamProtocolViolation,
    _model_from_item,
    prepare_body,
    request_headers,
)
from tests.helpers.http import chunk_payload as chunk
from tests.helpers.http import sse_text_done as _sse
from tests.provider.raccoon._support import client as _client


def test_prepare_body_forces_stream_and_deepcopies_messages():
    messages = [{"role": "user", "content": "hi"}]
    body = prepare_body({"messages": messages, "stream": False, "temperature": 0.5},
                        "sn-glm-5-3")
    assert body["stream"] is True
    assert body["model"] == "sn-glm-5-3"
    assert body["temperature"] == 0.5
    body["messages"][0]["content"] = "changed"
    assert messages[0]["content"] == "hi"


def test_request_headers_bearer_and_json_accept():
    headers = request_headers("tok")
    assert headers["Authorization"] == "Bearer tok"
    assert headers["Accept"] == "text/event-stream"
    assert "AsyncOpenAI" in headers["User-Agent"]
    assert request_headers("tok", accept="application/json")["Accept"] \
        == "application/json"


async def test_stream_chat_yields_named_events():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == EP_CHAT
        assert request.headers["Authorization"] == "Bearer tok"
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["model"] == "sn-glm-5-3"
        return httpx.Response(200, text=_sse(
            {"model": "sn-glm-5-3", **chunk({"reasoning_content": "r"})},
            {"model": "sn-glm-5-3", **chunk({"content": "hi"})},
            {"model": "sn-glm-5-3", **chunk({}, finish="stop")},
        ))

    events = [e async for e in _client(handler).stream_chat(
        {"messages": [{"role": "user", "content": "x"}]}, "sn-glm-5-3", "tok")]
    kinds = [e.kind for e in events]
    assert kinds == [EventKind.REASONING, EventKind.CONTENT, EventKind.FINISH]


async def test_stream_chat_rejects_model_echo_mismatch():
    """未知模型会静默回落默认模型 → 首帧回显不一致必须失败，而不是照样吐字。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse(
            {"model": "raccoon-090dd3", **chunk({"content": "wrong model"})}))

    with pytest.raises(UpstreamProtocolViolation, match="model echo mismatch"):
        [e async for e in _client(handler).stream_chat(
            {"messages": []}, "not-a-model", "tok")]


async def test_stream_chat_ignores_frames_without_model():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse(chunk({"content": "ok"})))

    events = [e async for e in _client(handler).stream_chat(
        {"messages": []}, "m", "tok")]
    assert [e.kind for e in events] == [EventKind.CONTENT]


async def test_stream_chat_raises_classified_http_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b'{"error":"slow down"}')

    with pytest.raises(UpstreamHTTPError) as caught:
        [e async for e in _client(handler).stream_chat({"messages": []}, "m", "t")]
    assert caught.value.kind() is ErrKind.SOFT


def test_upstream_http_error_exposes_status_and_kind():
    error = UpstreamHTTPError(404, b'{"error":"model not found"}')
    assert error.status == 404
    assert error.kind() is ErrKind.INVALID


def test_model_from_item_metadata():
    model = _model_from_item({
        "name": "sn-glm-5-3",
        "billing_effective_multiplier": 0,
        "tags": ["office", "reasoning", "code"],
    })
    assert model is not None
    assert model.id == "sn-glm-5-3"
    assert model.credit_rate == 0.0
    assert model.supports_tool_call is True
    assert model.supports_reasoning is True
    assert model.supports_images is False


def test_model_from_item_vision_and_missing_tags():
    vision = _model_from_item({"name": "sn-kimi-k3", "tags": ["vision"]})
    assert vision is not None and vision.supports_images is True
    plain = _model_from_item({"model_name": "raccoon-090dd3"})
    assert plain is not None and plain.id == "raccoon-090dd3"
    assert plain.supports_images is None and plain.supports_reasoning is None


def test_model_from_item_rejects_malformed():
    assert _model_from_item("junk") is None
    assert _model_from_item({}) is None
    assert _model_from_item({"name": 123}) is None
    assert _model_from_item({"name": "  "}) is None


def test_model_from_item_ignores_non_numeric_multiplier():
    for value in (True, "1", None):
        model = _model_from_item({"name": "m", "billing_effective_multiplier": value})
        assert model is not None and model.credit_rate is None
    numeric = _model_from_item({"name": "m", "billing_effective_multiplier": 0.75})
    assert numeric is not None and numeric.credit_rate == 0.75


async def test_fetch_models_flattens_categories_and_dedupes():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == EP_MODELS
        return httpx.Response(200, json={"code": 0, "data": {"categories": [
            {"type": "chat", "models": [
                {"name": "raccoon-090dd3", "tags": ["code"]},
                {"name": "SN-GLM-5-3", "tags": ["reasoning"]},
                "junk",
                {"no_name": True},
            ]},
            {"models": [{"name": "sn-glm-5-3", "tags": ["vision"]}]},  # 去重
            "junk-category",
            {"models": "not-a-list"},
        ]}})

    models = await _client(handler).fetch_models("tok")
    # 去重按小写 id，保留先出现的条目
    assert [m.id for m in models] == ["raccoon-090dd3", "SN-GLM-5-3"]
    assert models[1].supports_images is False
    assert models[1].supports_reasoning is True


async def test_fetch_models_errors():
    def http_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b'{"message":"unauthorized"}')

    with pytest.raises(UpstreamHTTPError):
        await _client(http_error).fetch_models("tok")

    def non_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    with pytest.raises(UpstreamProtocolViolation):
        await _client(non_json).fetch_models("tok")

    def scalar(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2])

    with pytest.raises(UpstreamProtocolViolation):
        await _client(scalar).fetch_models("tok")

    def no_categories(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {}})

    with pytest.raises(UpstreamProtocolViolation):
        await _client(no_categories).fetch_models("tok")

    def empty(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"categories": [
            {"models": ["junk"]}, {"models": [{"name": ""}]}]}})

    with pytest.raises(UpstreamProtocolViolation, match="no models"):
        await _client(empty).fetch_models("tok")


async def test_fetch_quota_reports_balance_and_pools():
    """余额 + 四个池的明细；流水给出逐笔到期阶梯。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_BALANCE:
            return httpx.Response(200, json={"code": 0, "data": {
                "available_points": 4800, "daily_points": 300,
                "reward_points": 4500, "monthly_points": 0, "topup_points": 0,
                "topup_frozen": False}})
        assert request.url.path == EP_BILLS
        return httpx.Response(200, json={"code": 0, "data": {"items": [
            {"biz_type": "daily_grant", "points": 300,
             "grant_at": "2026-11-04T00:00:00+08:00",
             "created_at": "2026-11-04T21:41:38+08:00",
             "expire_at": "2026-11-10T23:59:59+08:00"},
        ]}})

    quota = await _client(handler).fetch_quota("tok")
    assert quota.remaining == 4800
    assert quota.total == 4800
    assert quota.probe_failed is False
    # 四个池里只列出非 0 的（monthly / topup 都是 0）
    assert [p["name"] for p in quota.packages] == ["每日额度", "奖励积分"]
    assert quota.packages[0]["total"] == 300
    # 逐笔到期阶梯来自流水的 daily_grant
    assert quota.expiry_ladder is not None
    assert quota.expiry_ladder[0][1] == 300


async def test_fetch_quota_survives_bills_failure():
    """流水失败不该拖垮余额结论（阶梯只是调度排序的加分项）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_BALANCE:
            return httpx.Response(200, json={"data": {"available_points": 100}})
        return httpx.Response(500, content=b"boom")

    quota = await _client(handler).fetch_quota("tok")
    assert quota.remaining == 100
    assert quota.expiry_ladder is None


async def test_fetch_quota_zero_points_has_no_packages():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_BALANCE:
            return httpx.Response(200, json={"data": {"available_points": 0}})
        return httpx.Response(200, json={"data": {"items": []}})

    quota = await _client(handler).fetch_quota("tok")
    assert (quota.remaining, quota.total, quota.packages) == (0.0, 0.0, None)
    # 0/0 时 health_score 会判「已耗尽」，这正是想要的语义
    from src.provider.base import health_score
    assert health_score(quota) == -1


async def test_fetch_quota_errors():
    def http_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    with pytest.raises(UpstreamHTTPError):
        await _client(http_error).fetch_quota("tok")

    def non_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="nope")

    with pytest.raises(UpstreamProtocolViolation):
        await _client(non_json).fetch_quota("tok")

    def no_points(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {}})

    with pytest.raises(UpstreamProtocolViolation):
        await _client(no_points).fetch_quota("tok")

    def bool_points(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"available_points": True}})

    with pytest.raises(UpstreamProtocolViolation):
        await _client(bool_points).fetch_quota("tok")


async def test_client_lazy_pools_and_aclose():
    client = RaccoonClient()
    assert client._stream is client._stream      # noqa: SLF001 - 惰性复用
    assert client._short is client._short        # noqa: SLF001
    await client.aclose()

async def test_fetch_bills_returns_items():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == EP_BILLS
        assert request.url.params["type"] == "all"
        return httpx.Response(200, json={"data": {"items": [{"biz_type": "desktop"}]}})

    items = await _client(handler).fetch_bills("tok")
    assert items == [{"biz_type": "desktop"}]


async def test_fetch_bills_errors():
    def http_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b'{"message":"unauthorized"}')

    with pytest.raises(UpstreamHTTPError):
        await _client(http_error).fetch_bills("tok")

    def non_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="nope")

    with pytest.raises(UpstreamProtocolViolation):
        await _client(non_json).fetch_bills("tok")

    def no_items(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {}})

    with pytest.raises(UpstreamProtocolViolation):
        await _client(no_items).fetch_bills("tok")


async def test_heartbeat_sends_client_headers():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == EP_HEARTBEAT
        assert request.method == "POST"
        # 缺 x-client-device-id 上游回 100002
        assert request.headers["x-client-device-id"] == "dev-x"
        assert request.headers["x-client-platform"] == "web"
        return httpx.Response(200, json={"code": 0, "data": {
            "device_id": "dev-x", "trusted_device": True,
            "last_seen_at": "2026-11-04T23:20:49+08:00"}})

    payload = await _client(handler).heartbeat("tok", "dev-x")
    assert payload["trusted_device"] is True


async def test_heartbeat_errors():
    def http_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=b'{"code":100002}')

    with pytest.raises(UpstreamHTTPError):
        await _client(http_error).heartbeat("tok", "dev-x")

    def non_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="nope")

    with pytest.raises(UpstreamProtocolViolation):
        await _client(non_json).heartbeat("tok", "dev-x")

    def no_data(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0})

    # 没有 data 段不算失败：心跳只要 200 就成立
    assert await _client(no_data).heartbeat("tok", "dev-x") == {}

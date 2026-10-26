"""LobsterAI 上游客户端：请求体、对话流、模型目录、积分、签到、令牌。"""

from __future__ import annotations

import json

import httpx
import pytest

from src.provider.base import EventKind, UpstreamProtocolViolation
from src.provider.lobsterai import events as lobsterai_events
from src.provider.lobsterai.client import (
    LobsteraiClient,
    UpstreamHTTPError,
    _extract_version,
    _granted_credits,
    _iso_to_epoch,
    auth_headers,
    chat_headers,
    keyfrom_body,
    prepare_body,
    thinking_levels_of,
)
from tests.helpers.http import chunk_payload as chunk
from tests.helpers.http import sse_text_done
from tests.provider.lobsterai._support import MOMENT, envelope, version_response
from tests.provider.lobsterai._support import client as _client
from tests.provider.lobsterai._support import cred as _cred

# ------------------------------------------------------------------ 请求体

def test_prepare_body_forces_stream_and_deepcopies_messages():
    messages = [{"role": "user", "content": "hi"}]
    body = prepare_body({"messages": messages, "stream": False, "temperature": 0.5},
                        "m", _cred(), "2026.10.23")
    assert body["stream"] is True
    assert body["model"] == "m"
    assert body["temperature"] == 0.5
    body["messages"][0]["content"] = "changed"
    assert messages[0]["content"] == "hi"


def test_prepare_body_drops_none_tool_choice():
    body = prepare_body({"messages": [], "tool_choice": "none"}, "m", _cred(), "v")
    assert "tool_choice" not in body
    body = prepare_body({"messages": [], "tool_choice": {"type": "none"}},
                        "m", _cred(), "v")
    assert "tool_choice" not in body


def test_prepare_body_keeps_other_tool_choice_forms():
    body = prepare_body({"messages": [], "tool_choice": {"type": "function",
                                                        "function": {"name": "f"}}},
                        "m", _cred(), "v")
    assert body["tool_choice"] == {"type": "function", "function": {"name": "f"}}


def test_prepare_body_translates_thinking_level():
    """远端产品档位 → wire 档位（`max` → `xhigh`）。"""
    body = prepare_body({"messages": [], "reasoning_effort": "max"}, "m", _cred(), "v",
                        thinking_levels={"max": "xhigh", "off": "off"})
    assert body["reasoning_effort"] == "xhigh"


def test_prepare_body_drops_untranslatable_effort():
    """翻译不出就删掉：发一个上游不认的档位会直接 500。"""
    body = prepare_body({"messages": [], "reasoning_effort": "max"}, "m", _cred(), "v")
    assert "reasoning_effort" not in body
    body = prepare_body({"messages": [], "reasoning_effort": "max"}, "m", _cred(), "v",
                        thinking_levels={"high": "high"})
    assert "reasoning_effort" not in body


def test_prepare_body_includes_identity_payload():
    body = prepare_body({"messages": []}, "m", _cred(), "2026.10.23")
    assert body["uuid"] == "uuid-1"
    assert body["firstKeyfrom"] == "official"
    assert body["version"] == "2026.10.23"
    assert body["userId"] == "user-1"


def test_keyfrom_body_omits_missing_identity_fields():
    """缺失的 uuid / userId **不带该键**（空串可能被上游当非法值）。"""
    body = keyfrom_body(_cred(uuid="", user_id=""), "v")
    assert "uuid" not in body
    assert "userId" not in body


def test_keyfrom_body_latest_falls_back_to_first():
    body = keyfrom_body(_cred(latest_keyfrom=""), "v")
    assert body["latestKeyfrom"] == "official"


def test_keyfrom_body_falls_back_to_default_when_absent():
    """归因缺失时回落到 `official`：上游只接受 `a-z0-9_-` 的合法值，
    空串会被记成一个不存在的渠道。"""
    body = keyfrom_body(_cred(first_keyfrom="", latest_keyfrom=""), "v")
    assert body["firstKeyfrom"] == "official"
    assert body["latestKeyfrom"] == "official"


def test_chat_headers_carry_capabilities_and_version():
    headers = chat_headers(_cred(), "2026.10.23")
    assert headers["Authorization"] == "Bearer AT"
    assert headers["X-LobsterAI-Client-Capabilities"] == lobsterai_events.CLIENT_CAPABILITIES
    assert headers["X-LobsterAI-Client-Version"] == "2026.10.23"
    # 官方客户端每个请求都带账号模式头，缺了服务端当成上下文不完整
    assert headers["X-LobsterAI-Account-Mode"] == "personal"
    assert "X-Domain" not in headers


def test_client_capabilities_include_virtual_models():
    """三个能力都要：官方客户端的声明是 virtual-models-v1 + kimi-k3 + thinking。"""
    capabilities = lobsterai_events.CLIENT_CAPABILITIES.split(",")
    assert "virtual-models-v1" in capabilities
    assert "kimi-k3-agentic-v1" in capabilities
    assert "thinking-level-control-v1" in capabilities


def test_auth_headers_have_no_authorization():
    headers = auth_headers("v")
    assert "Authorization" not in headers
    assert headers["Content-Type"] == "application/json"


# ------------------------------------------------------------------ 对话流

async def test_stream_chat_yields_events():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == lobsterai_events.EP_CHAT
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["model"] == "m"
        return httpx.Response(200, text=sse_text_done(
            chunk({"role": "assistant", "content": "hi"}),
            chunk({"content": "!"}, finish="stop"),
        ))

    client = _client(handler)
    events = [event async for event in client.stream_chat(_cred(), {"messages": []}, "m")]
    assert any(event.kind is EventKind.CONTENT for event in events)
    await client.aclose()


async def test_stream_chat_surfaces_stream_error_frame():
    """上游把额度耗尽表达成 200 里的错误帧：必须变成 ERROR 事件（供引擎换号）。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse_text_done(
            {"error": {"code": 40201, "message": "免费额度已用完"}}))

    client = _client(handler)
    events = [event async for event in client.stream_chat(_cred(), {"messages": []}, "m")]
    assert [event.kind for event in events] == [EventKind.ERROR]
    assert events[0].error_code == 40201
    await client.aclose()


async def test_stream_chat_raises_on_http_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    client = _client(handler)
    with pytest.raises(UpstreamHTTPError):
        [event async for event in client.stream_chat(_cred(), {"messages": []}, "m")]
    await client.aclose()


# ------------------------------------------------------------------ 版本号

async def test_client_version_reads_update_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "version.test"
        return httpx.Response(200, json=version_response("2026.10.23"))

    client = _client(handler)
    assert await client.client_version() == "2026.10.23"
    await client.aclose()


async def test_client_version_falls_back_on_failure():
    from tests.helpers.http import provider_client

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = provider_client(LobsteraiClient, handler, api_base="https://api.test",
                              version_api="https://version.test/update")
    assert await client.client_version() == lobsterai_events.FALLBACK_CLIENT_VERSION
    await client.aclose()


async def test_client_version_falls_back_on_bad_payload():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"value": {"version": "not-a-version"}}})

    client = _client(handler)
    assert await client.client_version() == lobsterai_events.FALLBACK_CLIENT_VERSION
    await client.aclose()


async def test_client_version_caches_success():
    """成功结果缓存 1h：版本号变更频率极低，不必每轮签到都查一次。"""
    from tests.helpers.http import provider_client

    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=version_response("2026.10.23"))

    client = provider_client(LobsteraiClient, handler, api_base="https://api.test",
                              version_api="https://version.test/update")
    assert await client.client_version() == "2026.10.23"
    assert await client.client_version() == "2026.10.23"
    assert calls["n"] == 1
    await client.aclose()


async def test_client_version_survives_transport_error():
    from tests.helpers.http import provider_client

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = provider_client(LobsteraiClient, handler, api_base="https://api.test",
                              version_api="https://version.test/update")
    assert await client.client_version() == lobsterai_events.FALLBACK_CLIENT_VERSION
    await client.aclose()


async def test_client_version_survives_non_json_body():
    from tests.helpers.http import provider_client

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    client = provider_client(LobsteraiClient, handler, api_base="https://api.test",
                              version_api="https://version.test/update")
    assert await client.client_version() == lobsterai_events.FALLBACK_CLIENT_VERSION
    await client.aclose()


def test_extract_version_accepts_date_like_only():
    assert _extract_version({"data": {"value": {"version": "2026.10.23"}}}) == "2026.10.23"
    assert _extract_version({"data": {"value": {"version": "abc"}}}) == ""
    assert _extract_version({"data": {"value": {}}}) == ""
    assert _extract_version({"data": {}}) == ""
    assert _extract_version([]) == ""
    assert _extract_version({"data": {"value": {"version": "  "}}}) == ""
    assert _extract_version({"data": {"value": {"version": 20261023}}}) == ""


# ------------------------------------------------------------------ 模型目录

async def test_fetch_models_uses_bearer_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == lobsterai_events.EP_MODELS
        assert request.headers["Authorization"] == "Bearer AT"
        assert request.url.params["uuid"] == "uuid-1"
        return httpx.Response(200, json=envelope([
            {"modelId": "deepseek-flash", "modelName": "DeepSeek-V4.1-Flash",
             "contextWindow": 1000000, "costMultiplier": 0.05,
             "supportsImage": True, "thinkingConfig": {
                 "options": [{"level": "max", "openclawLevel": "xhigh"},
                             {"level": "off", "openclawLevel": "off"}]}},
        ]))

    client = _client(handler)
    models = await client.fetch_models(_cred())
    assert [model.id for model in models] == ["deepseek-flash"]
    assert models[0].credit_rate == 0.05
    assert models[0].max_input_tokens == 1000000
    assert models[0].supports_images is True
    assert models[0].supports_reasoning is True
    await client.aclose()


async def test_fetch_models_falls_back_to_public_catalog():
    """Bearer 目录不可用 → 公开目录（无需鉴权），模型清单不依赖登录态。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == lobsterai_events.EP_MODELS:
            return httpx.Response(401, content=b"unauthorized")
        assert request.url.path == lobsterai_events.EP_PRICING_CATALOG
        return httpx.Response(200, json=envelope({
            "textModels": [{"modelId": "glm-5.3", "modelName": "GLM-5.3"}],
            "imageModels": [{"modelId": "img"}],
        }))

    client = _client(handler)
    models = await client.fetch_models(_cred())
    assert [model.id for model in models] == ["glm-5.3"]
    await client.aclose()


async def test_fetch_models_falls_back_when_bearer_list_is_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == lobsterai_events.EP_MODELS:
            return httpx.Response(200, json=envelope([]))
        return httpx.Response(200, json=envelope({
            "textModels": [{"modelId": "glm-5.3"}]}))

    client = _client(handler)
    models = await client.fetch_models(_cred())
    assert [model.id for model in models] == ["glm-5.3"]
    await client.aclose()


async def test_public_catalog_failure_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == lobsterai_events.EP_MODELS:
            return httpx.Response(401, content=b"unauthorized")
        return httpx.Response(500, content=b"boom")

    client = _client(handler)
    with pytest.raises(UpstreamHTTPError):
        await client.fetch_models(_cred())
    await client.aclose()


async def test_public_catalog_without_text_models_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == lobsterai_events.EP_MODELS:
            return httpx.Response(401, content=b"unauthorized")
        return httpx.Response(200, json=envelope({"textModels": []}))

    client = _client(handler)
    with pytest.raises(UpstreamProtocolViolation):
        await client.fetch_models(_cred())
    await client.aclose()


def test_to_model_skips_entries_without_id():
    client = _client(lambda _r: httpx.Response(200))
    assert client._to_model({"modelName": "x"}) is None
    assert client._to_model({"modelId": 5}) is None


def test_to_model_leaves_missing_metadata_none():
    client = _client(lambda _r: httpx.Response(200))
    model = client._to_model({"modelId": "m"})
    assert model.credit_rate is None
    assert model.max_input_tokens is None
    assert model.max_output_tokens is None
    assert model.supports_images is None
    assert model.supports_tool_call is None
    assert model.supports_reasoning is False


def test_thinking_levels_of_reads_options():
    levels = thinking_levels_of([
        {"modelId": "m", "thinkingConfig": {"options": [
            {"level": "max", "openclawLevel": "xhigh"},
            {"level": "off", "openclawLevel": "off"}]}},
        {"modelId": "n"},
        {"modelId": "o", "thinkingConfig": {"options": "bad"}},
        {"modelId": "p", "thinkingConfig": {"options": [
            "junk",
            {"level": "high"},                      # 缺 wire 值
            {"level": "", "openclawLevel": "high"},  # 空档位名
        ]}},
        "junk",
    ])
    assert levels == {"m": {"max": "xhigh", "off": "off"}}


def test_thinking_levels_of_skips_empty_tables():
    """没有合法档位的模型不进表（查不到就删掉 reasoning_effort）。"""
    assert thinking_levels_of([
        {"modelId": "m", "thinkingConfig": {"options": []}}]) == {}


# ------------------------------------------------------------------ 积分

async def test_fetch_quota_reads_remaining_and_packages():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == lobsterai_events.EP_PROFILE_SUMMARY
        return httpx.Response(200, json=envelope({
            "totalCreditsRemaining": 5297.72,
            "creditItems": [
                {"type": "campaign", "label": "每日登录奖励",
                 "creditsRemaining": 100, "expiresAt": "2026-10-27T01:21:23"},
                {"type": "subscription", "label": "标准版",
                 "creditsRemaining": 5000, "expiresAt": "2026-11-26T01:21:23"},
                {"type": "free", "creditsRemaining": 0},
                "junk",
            ],
        }))

    client = _client(handler)
    quota = await client.fetch_quota(_cred())
    assert quota.remaining == 5297.72
    assert quota.total is None            # 上游没有「总量」这个概念
    assert quota.expiry_ladder is not None
    assert [pair[0] for pair in quota.expiry_ladder] == sorted(
        pair[0] for pair in quota.expiry_ladder)
    assert quota.packages is not None and len(quota.packages) == 3
    assert quota.probed_at == MOMENT
    await client.aclose()


async def test_fetch_quota_requires_total_remaining():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope({"creditItems": []}))

    client = _client(handler)
    with pytest.raises(UpstreamProtocolViolation):
        await client.fetch_quota(_cred())
    await client.aclose()


async def test_fetch_quota_skips_malformed_items():
    """creditItems 里的脏条目（缺余额 / 非数字）跳过，不让整次探测失败。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope({
            "totalCreditsRemaining": 10,
            "creditItems": [
                {"label": "no-balance"},
                {"label": "bad", "creditsRemaining": "x"},
                {"label": "ok", "creditsRemaining": 10},
                "junk",
            ],
        }))

    client = _client(handler)
    quota = await client.fetch_quota(_cred())
    assert quota.remaining == 10
    assert quota.packages is not None and len(quota.packages) == 1
    assert quota.packages[0]["name"] == "ok"
    # 没有到期日的包不进到期阶梯（阶梯只收「已知到期」的余额）
    assert quota.expiry_ladder is None
    await client.aclose()


async def test_fetch_quota_clamps_negative_remaining():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope({"totalCreditsRemaining": -5}))

    client = _client(handler)
    quota = await client.fetch_quota(_cred())
    assert quota.remaining == 0.0
    await client.aclose()


def test_iso_to_epoch_parses_and_rejects():
    assert _iso_to_epoch("2026-10-27T01:21:23") > 0
    assert _iso_to_epoch("2026-10-27 01:21:23") > 0
    assert _iso_to_epoch("2026-10-27T01:21:23Z") > 0
    assert _iso_to_epoch("not a date") == 0
    assert _iso_to_epoch("") == 0


# ------------------------------------------------------------------ 签到

def _slot(activity_code: str = "daily-check-in-evergreen-prod-20260814",
          revision: int = 1, state: str = "available") -> dict:
    return {"slotState": state, "activity": {
        "activityCode": activity_code, "configRevision": revision}}


def _context(claimed: bool = False, actions=("check_in",), reward=100) -> dict:
    return {"state": {"claimedToday": claimed, "rewardCredits": reward},
            "actions": list(actions)}


async def test_claim_checkin_claims_and_verifies():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path == lobsterai_events.EP_ACTIVITY_SLOT:
            assert request.url.params["placement"] == "desktop_sidebar"
            assert request.url.params["platform"] == "win32"
            return httpx.Response(200, json=envelope(_slot()))
        if path.endswith("/context"):
            return httpx.Response(200, json=envelope(
                _context(claimed=len(calls) > 3)))
        body = json.loads(request.content)
        assert body["idempotencyKey"].startswith("lobsterai-checkin-")
        assert body["configRevision"] == 1
        return httpx.Response(200, json=envelope(
            {"result": {"creditsGranted": 100}}))

    client = _client(handler)
    result = await client.claim_checkin(_cred())
    assert result.ok is True
    assert result.credit == 100
    assert result.seal_until is not None
    await client.aclose()


async def test_claim_checkin_reports_already_claimed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == lobsterai_events.EP_ACTIVITY_SLOT:
            return httpx.Response(200, json=envelope(_slot()))
        return httpx.Response(200, json=envelope(_context(claimed=True)))

    client = _client(handler)
    result = await client.claim_checkin(_cred())
    assert result.ok is True
    assert result.already_checked_in is True
    await client.aclose()


async def test_claim_checkin_without_activity():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope(_slot(state="empty")))

    client = _client(handler)
    result = await client.claim_checkin(_cred())
    assert result.ok is False
    assert "无可用签到活动" in result.message
    await client.aclose()


async def test_claim_checkin_ignores_slot_without_activity_code():
    """槽位可用但没有 activityCode → 同样按「无可用活动」处理。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope({
            "slotState": "available", "activity": {"configRevision": 1}}))

    client = _client(handler)
    result = await client.claim_checkin(_cred())
    assert result.ok is False
    await client.aclose()


async def test_claim_checkin_ignores_non_object_activity():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope({
            "slotState": "available", "activity": "junk"}))

    client = _client(handler)
    result = await client.claim_checkin(_cred())
    assert result.ok is False
    await client.aclose()


async def test_claim_checkin_tolerates_missing_config_revision():
    """configRevision 缺失时按 0 发（上游不认就报错，不会静默领错）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == lobsterai_events.EP_ACTIVITY_SLOT:
            return httpx.Response(200, json=envelope({
                "slotState": "available",
                "activity": {"activityCode": "c"}}))
        if request.url.path.endswith("/context"):
            return httpx.Response(200, json=envelope({
                "state": {"claimedToday": False}, "actions": ["check_in"]}))
        return httpx.Response(200, json=envelope(
            {"result": {"creditsGranted": 100}}))

    client = _client(handler)
    result = await client.claim_checkin(_cred())
    assert result.ok is False          # 复核时 claimedToday 没翻转
    await client.aclose()


async def test_claim_checkin_when_context_query_fails():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == lobsterai_events.EP_ACTIVITY_SLOT:
            return httpx.Response(200, json=envelope(_slot()))
        return httpx.Response(500, content=b"boom")

    client = _client(handler)
    result = await client.claim_checkin(_cred())
    assert result.ok is False
    assert "状态查询失败" in result.message
    await client.aclose()


async def test_claim_checkin_when_action_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == lobsterai_events.EP_ACTIVITY_SLOT:
            return httpx.Response(200, json=envelope(_slot()))
        return httpx.Response(200, json=envelope(_context(actions=())))

    client = _client(handler)
    result = await client.claim_checkin(_cred())
    assert result.ok is False
    assert "不提供签到动作" in result.message
    await client.aclose()


async def test_claim_checkin_fails_when_state_not_flipped():
    """上游静默拒绝（已签到状态没翻转）时**不算成功**。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == lobsterai_events.EP_ACTIVITY_SLOT:
            return httpx.Response(200, json=envelope(_slot()))
        if request.url.path.endswith("/context"):
            return httpx.Response(200, json=envelope(_context(claimed=False)))
        return httpx.Response(200, json=envelope({"result": {"creditsGranted": 100}}))

    client = _client(handler)
    result = await client.claim_checkin(_cred())
    assert result.ok is False
    assert "签到未生效" in result.message
    await client.aclose()


async def test_fetch_checkin_status_reports_claimed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == lobsterai_events.EP_ACTIVITY_SLOT:
            return httpx.Response(200, json=envelope(_slot()))
        return httpx.Response(200, json=envelope(_context(claimed=True)))

    client = _client(handler)
    status = await client.fetch_checkin_status(_cred())
    assert status["active"] is True
    assert status["today_checked_in"] is True
    assert status["today_credit"] == 100
    await client.aclose()


async def test_fetch_checkin_status_without_activity():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope(_slot(state="empty")))

    client = _client(handler)
    status = await client.fetch_checkin_status(_cred())
    assert status["active"] is False
    await client.aclose()


def test_granted_credits_reads_result_fields():
    assert _granted_credits({"result": {"creditsGranted": 50}}) == 50
    assert _granted_credits({"result": {"rewardCredits": 60}}) == 60
    assert _granted_credits({"result": {"credits": 70}}) == 70
    # 两个字段都没有 → 回落每日固定奖励（实测 100）
    assert _granted_credits({"result": {}}) == 100
    assert _granted_credits({}) == 100
    assert _granted_credits("junk") == 100


# ------------------------------------------------------------------ 令牌

async def test_exchange_code_returns_credential():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == lobsterai_events.EP_EXCHANGE
        assert "Authorization" not in request.headers
        body = json.loads(request.content)
        assert body["authCode"] == "CODE"
        assert body["uuid"] == "uuid-1"
        assert body["firstKeyfrom"] == "official"
        return httpx.Response(200, json=envelope({
            "accessToken": "AT2", "refreshToken": "RT2", "expiresIn": 3600,
            "user": {"id": "uid-2", "userId": "u2", "nickname": "bob"}}))

    client = _client(handler)
    credential = await client.exchange_code(
        "CODE", uuid="uuid-1", keyfrom="official")
    assert credential.access_token == "AT2"
    assert credential.refresh_token == "RT2"
    assert credential.uid == "uid-2"
    assert credential.user_id == "u2"
    assert credential.nickname == "bob"
    assert credential.uuid == "uuid-1"
    assert credential.first_keyfrom == "official"
    assert credential.expires_at == MOMENT + 3600
    await client.aclose()


async def test_exchange_code_rejects_missing_access_token():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope({"refreshToken": "RT"}))

    client = _client(handler)
    with pytest.raises(UpstreamProtocolViolation):
        await client.exchange_code("CODE", uuid="u", keyfrom="official")
    await client.aclose()


async def test_refresh_token_keeps_old_refresh_token_when_not_rotated():
    """上游可能不轮转 refresh_token：响应里没有就沿用旧值。"""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == lobsterai_events.EP_REFRESH
        body = json.loads(request.content)
        assert body["refreshToken"] == "RT"
        assert body["uuid"] == "uuid-1"
        return httpx.Response(200, json=envelope({
            "accessToken": "AT2", "expiresIn": 3600}))

    client = _client(handler)
    refreshed = await client.refresh_token(_cred())
    assert refreshed.access_token == "AT2"
    assert refreshed.refresh_token == "RT"
    assert refreshed.uuid == "uuid-1"
    assert refreshed.first_keyfrom == "official"
    assert refreshed.user_id == "user-1"
    assert refreshed.nickname == "alice"
    await client.aclose()


async def test_refresh_token_rotates_refresh_token():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope({
            "accessToken": "AT2", "refreshToken": "RT2", "expiresIn": 3600}))

    client = _client(handler)
    refreshed = await client.refresh_token(_cred())
    assert refreshed.refresh_token == "RT2"
    await client.aclose()


async def test_refresh_token_keeps_identity_when_response_omits_it():
    """响应不带身份字段时沿用旧值（丢了就永远续不了期）。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope({"accessToken": "AT2"}))

    client = _client(handler)
    refreshed = await client.refresh_token(_cred())
    assert refreshed.uuid == "uuid-1"
    assert refreshed.first_keyfrom == "official"
    assert refreshed.latest_keyfrom == "official"
    assert refreshed.user_id == "user-1"
    assert refreshed.nickname == "alice"
    await client.aclose()


async def test_refresh_token_keeps_user_id_and_nickname_when_response_has_none():
    """响应带了 user 但既没 userId 也没 nickname → 沿用旧值。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope({
            "accessToken": "AT2", "user": {"id": "u2"}}))

    client = _client(handler)
    refreshed = await client.refresh_token(_cred())
    assert refreshed.user_id == "user-1"
    assert refreshed.nickname == "alice"
    await client.aclose()


async def test_refresh_token_takes_user_id_and_nickname_from_response():
    """响应给了新的 userId / nickname → 用新值（账号改名 / 换绑的情况）。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope({
            "accessToken": "AT2", "user": {"id": "u2", "userId": "U2",
                                          "nickname": "bob"}}))

    client = _client(handler)
    refreshed = await client.refresh_token(_cred())
    assert refreshed.user_id == "U2"
    assert refreshed.nickname == "bob"
    await client.aclose()


async def test_refresh_token_without_expires_in_keeps_expiry_unknown():
    """响应不带 expiresIn 时到期未知（不猜本地 TTL）。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope({"accessToken": "AT2"}))

    client = _client(handler)
    refreshed = await client.refresh_token(_cred())
    assert refreshed.expires_at == 0
    await client.aclose()


async def test_exchange_code_falls_back_to_yid_for_user_id():
    """响应只给 yid 时用它当 userId（身份载荷要带）。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope({
            "accessToken": "AT2", "expiresIn": 3600,
            "user": {"yid": "Y1"}}))

    client = _client(handler)
    credential = await client.exchange_code("C", uuid="u", keyfrom="official")
    assert credential.user_id == "Y1"
    assert credential.nickname == ""
    await client.aclose()

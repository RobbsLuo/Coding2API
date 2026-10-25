"""officeAce 上游客户端：聊天流、模型目录、额度、签到、刷新。"""

from __future__ import annotations

import json

import httpx
import pytest

from src.engine.sse import SSEFrame
from src.provider.base import (
    ErrKind,
    EventKind,
    UpstreamProtocolViolation,
    UpstreamReloginRequired,
)
from src.provider.officeace import events as officeace_events
from src.provider.officeace.client import (
    DEFAULT_CHECKIN_NAME,
    OfficeAceClient,
    UpstreamHTTPError,
    _model_from_item,
    _next_beijing_midnight,
    _sdk_date,
    prepare_body,
    request_headers,
)
from src.provider.officeace.oauth import TokenEndpointError
from tests.helpers.http import chunk_payload as _chunk
from tests.helpers.http import sse_text_done
from tests.provider.officeace._support import MOMENT
from tests.provider.officeace._support import client as _client
from tests.provider.officeace._support import cred as _cred
from tests.provider.officeace._support import token_response as _token_response

GW = "https://gw.test/v2"


# ------------------------------------------------------------------ 请求体

def test_prepare_body_sets_stream_and_model():
    body, sent = prepare_body({"messages": [{"role": "user", "content": "hi"}],
                               "max_tokens": 64}, "glm-5.2")
    assert body["model"] == "glm-5.2" and body["stream"] is True
    assert sent == 1088 and body["max_tokens"] == 1088


def test_prepare_body_deep_copies_messages():
    """不能穿透回引擎持有的 request.raw（会话粘性指纹与续写都读它）。"""
    messages = [{"role": "user", "content": "hi"}]
    body, _ = prepare_body({"messages": messages}, "m")
    body["messages"][0]["content"] = "changed"
    assert messages[0]["content"] == "hi"


def test_prepare_body_normalises_max_completion_tokens():
    body, sent = prepare_body({"messages": [], "max_completion_tokens": 64}, "m")
    assert "max_completion_tokens" not in body
    assert sent == 1088 and body["max_tokens"] == 1088


def test_prepare_body_tiny_and_missing_max_tokens():
    """`< 16` 原样透传（精确请求）；没给就不补，交给上游默认。"""
    assert prepare_body({"messages": [], "max_tokens": 1}, "m")[1] == 1
    body, sent = prepare_body({"messages": []}, "m")
    assert sent is None and "max_tokens" not in body


def test_prepare_body_ignores_bogus_max_tokens():
    for value in (0, -5, True, "64", None):
        body, sent = prepare_body({"messages": [], "max_tokens": value}, "m")
        assert sent is None
        assert body.get("max_tokens") in (None, value)


def test_request_headers_carry_basic_auth():
    headers = request_headers(_cred())
    assert headers["Authorization"].startswith("Basic ")
    assert headers["Accept"] == "text/event-stream"


def test_sdk_date_format():
    """UTC 紧凑格式（签名与请求头必须同值）。"""
    assert _sdk_date(MOMENT) == "20261021T013052Z"
    assert _sdk_date() != ""


def test_next_beijing_midnight():
    """签到封账到下一个北京 0 点（= UTC 16:00）：次日新活动一出现就能再签。"""
    assert _next_beijing_midnight(MOMENT) == 1792598400
    # 恰好是北京 0 点时，封到**下一个** 0 点（不是立即解封，否则会连打上游）
    assert _next_beijing_midnight(1792598400) == 1792684800


# ------------------------------------------------------------------ TTL

def test_client_normalises_bases_and_defaults():
    client = OfficeAceClient(cloud_base="https://cloud.test/",
                            gateway="https://gw.test/")
    assert client.cloud_base == "https://cloud.test"
    assert client.gateway == "https://gw.test"
    assert client.login.cloud_base == "https://cloud.test"


def test_client_lazy_clients_and_aclose():
    client = OfficeAceClient(cloud_base="https://cloud.test",
                            gateway="https://gw.test")
    assert client._stream is client._stream
    assert client._short is client._short
    assert client._stream is not client._short


async def test_client_aclose_closes_both_pools():
    client = _client(lambda request: httpx.Response(200, json={}))
    stream = client._stream
    await client.aclose()
    assert stream.is_closed


async def test_client_aclose_without_pools_is_noop():
    await OfficeAceClient(cloud_base="https://cloud.test").aclose()


# ------------------------------------------------------------------ 聊天

async def _events(payload: dict, **kw):
    client = _client(lambda request: httpx.Response(
        200, text=sse_text_done(payload), headers={"content-type": "text/event-stream"}),
        **kw)
    return [event async for event in client.stream_chat(_cred(), {}, "m")]


async def test_stream_chat_yields_parsed_events():
    got = await _events(_chunk({"content": "你好"}))
    assert [event.kind for event in got] == [EventKind.CONTENT]
    assert got[0].content == "你好"


async def test_stream_chat_posts_to_gateway_with_basic_auth():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, text=sse_text_done(_chunk({"content": "x"})))

    client = _client(handler)
    async for _ in client.stream_chat(_cred(), {"messages": []}, "glm-5.2"):
        pass
    assert seen["url"] == f"{GW}/chat/completions"
    assert seen["auth"].startswith("Basic ")
    assert seen["body"]["stream"] is True and seen["body"]["model"] == "glm-5.2"


async def test_stream_chat_logs_max_tokens_adjustment(caplog):
    """预算被抬高/夹回时要留一行日志：正文为空的排查第一步就是这个值。"""
    client = _client(lambda request: httpx.Response(
        200, text=sse_text_done(_chunk({"content": "x"}))))
    with caplog.at_level("INFO"):
        async for _ in client.stream_chat(_cred(), {"messages": [], "max_tokens": 64},
                                          "glm-5.2"):
            pass
    assert "调整 max_tokens" in caplog.text


async def test_stream_chat_no_log_when_max_tokens_unchanged():
    """`< 16` 原样透传（探活/测通道的精确请求）不该刷日志。"""
    client = _client(lambda request: httpx.Response(
        200, text=sse_text_done(_chunk({"content": "x"}))))
    async for _ in client.stream_chat(_cred(), {"messages": [], "max_tokens": 1}, "m"):
        pass


async def test_stream_chat_requires_gateway():
    client = _client(lambda request: httpx.Response(200))
    with pytest.raises(UpstreamProtocolViolation) as caught:
        async for _ in client.stream_chat(_cred(model_base_url=""), {}, "m"):
            pass
    assert "网关地址" in caught.value.public_message


async def test_stream_chat_raises_on_http_error():
    client = _client(lambda request: httpx.Response(
        403, content=b'ModelArts.81004 no permission'))
    with pytest.raises(UpstreamHTTPError) as caught:
        async for _ in client.stream_chat(_cred(), {}, "m"):
            pass
    # 81004 必须判 BLOCKED（模型级），而不是按 403 判 DEAD 打死整条凭据
    assert caught.value.kind() is ErrKind.BLOCKED


async def test_stream_chat_pseudo_200_error_frame_is_yielded():
    """限流走伪 200：2xx 不代表好流，错误帧必须照常产出交引擎分类。"""
    got = await _events({"error": {"code": 81111, "message": "rate limited"}})
    assert len(got) == 1
    assert got[0].kind is EventKind.ERROR
    assert got[0].error_kind is ErrKind.MODEL


async def test_stream_chat_retry_hints_from_headers():
    client = _client(lambda request: httpx.Response(
        429, content=b"slow down", headers={"Retry-After": "7"}))
    with pytest.raises(UpstreamHTTPError) as caught:
        async for _ in client.stream_chat(_cred(), {}, "m"):
            pass
    assert caught.value.status == 429
    assert caught.value.retry_after_seconds == 7


# ------------------------------------------------------------------ 模型

def _models_handler(items, *, rates=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/model-token-rates"):
            return httpx.Response(200, json={"models": rates or []})
        return httpx.Response(200, json={"data": items})
    return handler


async def test_fetch_models_maps_metadata_and_rates():
    client = _client(_models_handler(
        [{"id": "glm-5.2", "name": " GLM-5.2 ", "context_length": 131072,
          "max_output_tokens": 8192, "supports_images": True,
          "tool_call": True, "supports_reasoning": True}],
        rates=[{"model_name": "glm-5.2", "cost_coefficient": 0.5}]))
    models = await client.fetch_models(_cred())
    assert len(models) == 1
    model = models[0]
    assert model.id == "glm-5.2" and model.name == "GLM-5.2"
    assert model.credit_rate == 0.5
    assert model.max_input_tokens == 131072
    assert model.max_output_tokens == 8192
    assert model.supports_images is True
    assert model.supports_tool_call is True
    assert model.supports_reasoning is True


async def test_fetch_models_requires_gateway():
    with pytest.raises(UpstreamProtocolViolation):
        await _client(lambda request: httpx.Response(200)).fetch_models(
            _cred(model_base_url=""))


async def test_fetch_models_raises_on_http_error():
    with pytest.raises(UpstreamHTTPError):
        await _client(lambda request: httpx.Response(401, content=b"nope")).fetch_models(
            _cred())


async def test_fetch_models_rejects_non_json():
    with pytest.raises(UpstreamProtocolViolation):
        await _client(lambda request: httpx.Response(200, text="<html>")).fetch_models(
            _cred())


async def test_fetch_models_rejects_empty_catalog():
    """空目录是异常而不是「这个号没模型」：直接失败，别落一个空快照。"""
    with pytest.raises(UpstreamProtocolViolation):
        await _client(_models_handler([])).fetch_models(_cred())


async def test_fetch_models_without_control_plane_skips_rates():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"data": [{"id": "m"}]})

    models = await _client(handler).fetch_models(
        _cred(access_key_id="", secret_access_key=""))
    assert [model.id for model in models] == ["m"]
    assert not any("model-token-rates" in path for path in calls)


async def test_rates_failure_keeps_previous_values():
    """费率是 best-effort：失败沿用上次，绝不让模型列表整体失败。"""
    state = {"fail": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/model-token-rates"):
            if state["fail"]:
                return httpx.Response(500, content=b"boom")
            return httpx.Response(200, json={"models": [
                {"model_name": "m", "cost_coefficient": 2.0}]})
        return httpx.Response(200, json={"data": [{"id": "m"}]})

    client = _client(handler)
    first = await client.fetch_models(_cred())
    assert first[0].credit_rate is None
    state["fail"] = False
    second = await client.fetch_models(_cred())
    assert second[0].credit_rate == 2.0
    state["fail"] = True
    third = await client.fetch_models(_cred())
    assert third[0].credit_rate == 2.0


async def test_rates_structure_error_keeps_previous():
    client = _client(lambda request: httpx.Response(200, json={"data": [{"id": "m"}]}))
    client._rates = {"m": 3.0}
    assert await client._fetch_rates_quietly(_cred()) == {"m": 3.0}


def test_model_from_item_tolerates_unknown_metadata():
    """上游没给可读名时用命名表美化（与其余渠道同口径）。"""
    model = _model_from_item({"id": "x"}, "x", None)
    assert model.name == "X"
    assert model.credit_rate is None
    assert model.max_input_tokens is None
    assert model.supports_images is None


def test_model_from_item_prefers_first_int_key():
    model = _model_from_item(
        {"id": "x", "context_length": "bad", "max_context_tokens": 7,
         "max_input_tokens": 9, "max_tokens": 3, "max_output_tokens": 5},
        "x", None)
    assert model.max_input_tokens == 7
    assert model.max_output_tokens == 5


def test_model_from_item_ignores_bool_metadata():
    model = _model_from_item({"id": "x", "context_length": True}, "x", None)
    assert model.max_input_tokens is None


# ------------------------------------------------------------------ 额度

def _subscription(**over) -> dict:
    base = {
        "subscribe_status": "SUBSCRIBED",
        "bonus_skus": [{"activity_id": "daily", "points": "1000",
                        "current_value": "0",
                        "expired_time": "2026-10-21T15:59:59Z",
                        "effective_time": "2026-10-21T01:00:00Z",
                        "cbc_resource_id": "bonus:x:daily"}],
    }
    base.update(over)
    return base


async def test_probe_quota_reads_points():
    client = _client(lambda request: httpx.Response(200, json=_subscription()))
    quota = await client.probe_quota(_cred())
    assert quota.remaining == 1000.0 and quota.total == 1000.0
    assert quota.probed_at == MOMENT
    assert quota.probe_failed is False
    assert quota.packages and quota.expiry_ladder


async def test_probe_quota_without_control_plane_is_probe_failed():
    """「探不到」≠「没额度」：绝不能显示成 0 被调度器当耗尽。"""
    quota = await _client(lambda request: httpx.Response(200)).probe_quota(
        _cred(access_key_id="", secret_access_key=""))
    assert quota.probe_failed is True
    assert quota.remaining is None


async def test_probe_quota_reraises_auth_errors():
    """401/403 要上抛：可能是 app key 坏了（DEAD），也可能是临时凭证过期。"""
    for status in (401, 403):
        with pytest.raises(UpstreamHTTPError):
            await _client(lambda request, s=status: httpx.Response(
                s, content=b"nope")).probe_quota(_cred())


async def test_probe_quota_soft_fails_on_other_errors():
    quota = await _client(lambda request: httpx.Response(
        500, content=b"boom")).probe_quota(_cred())
    assert quota.probe_failed is True


async def test_probe_quota_soft_fails_on_bad_structure():
    quota = await _client(lambda request: httpx.Response(
        200, text="<html>")).probe_quota(_cred())
    assert quota.probe_failed is True


async def test_probe_quota_soft_fails_on_structural_violation():
    quota = await _client(lambda request: httpx.Response(
        200, json="not-an-object")).probe_quota(_cred())
    assert quota.probe_failed is True


async def test_v11_request_is_signed():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["date"] = request.headers["x-sdk-date"]
        seen["sub"] = request.headers["x-subscription-type"]
        return httpx.Response(200, json=_subscription())

    await _client(handler).probe_quota(_cred())
    assert seen["auth"].startswith("V11-HMAC-SHA256 Credential=AK/")
    assert seen["date"] == "20261021T013052Z"
    assert seen["sub"] == "v2"


# ------------------------------------------------------------------ 签到状态

async def test_checkin_status_reports_claimed_today():
    payload = _subscription(bonus_skus=[
        {"activity_id": "daily", "activity_name": "每日签到有礼", "points": "1000",
         "current_value": "0", "expired_time": "2026-10-21T15:59:59Z",
         "effective_time": "2026-10-21T01:00:00Z",
         "cbc_resource_id": "bonus:x:daily"}])
    status = await _client(lambda request: httpx.Response(
        200, json=payload)).fetch_checkin_status(_cred())
    assert status == {"active": True, "today_checked_in": True,
                      "today_credit": 1000.0, "total_credits": 1000.0,
                      "activity_name": "每日签到有礼"}


async def test_checkin_status_falls_back_to_default_activity_name():
    """上游不给活动名时不能展示空白（签到按钮旁边会是一片空）。"""
    status = await _client(lambda request: httpx.Response(
        200, json=_subscription())).fetch_checkin_status(_cred())
    assert status["activity_name"] == "每日签到"


async def test_checkin_status_reports_not_claimed():
    payload = _subscription(bonus_skus=[
        {"activity_id": "d", "activity_name": "每日签到有礼", "points": "1000",
         "expired_time": "2026-10-21T15:59:59Z",
         "effective_time": "2026-10-20T01:00:00Z",
         "cbc_resource_id": "bonus:x:daily"}])
    status = await _client(lambda request: httpx.Response(
        200, json=payload)).fetch_checkin_status(_cred())
    assert status["active"] is True
    assert status["today_checked_in"] is False
    assert status["today_credit"] is None


async def test_checkin_status_inactive_without_daily():
    status = await _client(lambda request: httpx.Response(
        200, json={"bonus_skus": []})).fetch_checkin_status(_cred())
    assert status == {"active": False, "today_checked_in": False,
                      "today_credit": None,
                      "activity_name": DEFAULT_CHECKIN_NAME}


async def test_checkin_status_inactive_without_control_plane():
    status = await _client(lambda request: httpx.Response(200)).fetch_checkin_status(
        _cred(access_key_id="", secret_access_key=""))
    assert status["active"] is False


# ------------------------------------------------------------------ 签到

def _claim_handler(*, before: dict, after: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=after)
        return httpx.Response(200, json=before)
    return handler


async def test_claim_bonus_reports_gained_points():
    before = {"bonus_skus": []}
    after = {"bonus_skus": [{"activity_id": "daily", "points": "1000",
                             "current_value": "0"}]}
    result = await _client(_claim_handler(before=before, after=after)).claim_bonus(
        _cred())
    assert result.ok is True and result.credit == 1000.0
    assert result.seal_until == _next_beijing_midnight(MOMENT)
    assert "1000" in result.message


async def test_claim_bonus_detects_increment():
    """按活动逐条比对增量：老活动不动、只多一条新用户福利 → 只记 4000。"""
    before = {"bonus_skus": [{"activity_id": "daily", "points": "1000",
                              "current_value": "0"}]}
    after = {"bonus_skus": [{"activity_id": "daily", "points": "1000",
                             "current_value": "0"},
                            {"activity_id": "new-user", "points": "4000",
                             "current_value": "0"}]}
    result = await _client(_claim_handler(before=before, after=after)).claim_bonus(
        _cred())
    assert result.credit == 4000.0


async def test_claim_bonus_already_checked_in():
    payload = {"bonus_skus": [{"activity_id": "daily", "points": "1000",
                               "current_value": "0"}]}
    result = await _client(_claim_handler(before=payload, after=payload)).claim_bonus(
        _cred())
    assert result.ok is True and result.already_checked_in is True
    assert result.code == 0


async def test_claim_bonus_partial_consumption_is_not_already():
    """已用掉一部分时 `after <= before` 成立但「没新领」不成立，不能报已签到。"""
    before = {"bonus_skus": [{"activity_id": "daily", "points": "1000",
                              "current_value": "0"}]}
    after = {"bonus_skus": [{"activity_id": "daily", "points": "1000",
                             "current_value": "500"}]}
    result = await _client(_claim_handler(before=before, after=after)).claim_bonus(
        _cred())
    assert result.ok is True and result.already_checked_in is True


async def test_claim_bonus_no_activities_is_retryable():
    result = await _client(_claim_handler(before={"bonus_skus": []},
                                         after={"bonus_skus": []})).claim_bonus(
        _cred())
    assert result.ok is False
    assert "稍后重试" in result.message


async def test_claim_bonus_snapshot_failure_still_counts_gain():
    """快照失败按空表 → 增量退化为「全算新领」（宁可虚高也不漏记）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(500, content=b"boom")
        return httpx.Response(200, json={"bonus_skus": [
            {"activity_id": "daily", "points": "1000", "current_value": "0"}]})

    result = await _client(handler).claim_bonus(_cred())
    assert result.ok is True and result.credit == 1000.0


async def test_claim_bonus_requires_control_plane():
    result = await _client(lambda request: httpx.Response(200)).claim_bonus(
        _cred(access_key_id="", secret_access_key=""))
    assert result.ok is False
    assert "临时 AK/SK" in result.message


async def test_claim_bonus_posts_empty_object_with_content_type():
    """裸 POST / 空 body 会被判 500 `OfficeAce.11020001`：必须 {} + JSON Content-Type。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen["ctype"] = request.headers["content-type"]
            seen["body"] = request.content
            return httpx.Response(200, json={"bonus_skus": [
                {"activity_id": "a", "points": "1", "current_value": "0"}]})
        return httpx.Response(200, json={"bonus_skus": []})

    await _client(handler).claim_bonus(_cred())
    assert seen["body"] == b"{}"     # 空 JSON 对象，不是空 body
    assert seen["ctype"].startswith("application/json")


async def test_bonus_snapshot_swallows_structural_error():
    """快照吃协议异常：签到主流程不该被一次坏响应带崩。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="<html>")
        return httpx.Response(200, json={"bonus_skus": [
            {"activity_id": "a", "points": "5", "current_value": "0"}]})

    result = await _client(handler).claim_bonus(_cred())
    assert result.ok is True and result.credit == 5.0


# ------------------------------------------------------------------ 刷新

async def test_refresh_token_rotates_credentials():
    client = _client(lambda request: httpx.Response(200, json=_token_response()))
    refreshed = await client.refresh_token(_cred())
    assert refreshed.access_key_id == "AK2"
    assert refreshed.refresh_token == "rt2"
    assert refreshed.model_app_key == "AK"


async def test_refresh_token_terminal_error_becomes_relogin():
    client = _client(lambda request: httpx.Response(
        400, content=b'{"error":"invalid_grant"}'))
    with pytest.raises(UpstreamReloginRequired):
        await client.refresh_token(_cred())


async def test_refresh_token_retryable_error_propagates():
    client = _client(lambda request: httpx.Response(503, content=b"try later"))
    with pytest.raises(TokenEndpointError):
        await client.refresh_token(_cred())


async def test_refresh_token_non_json_raises_protocol_violation():
    client = _client(lambda request: httpx.Response(200, text="<html>"))
    with pytest.raises(UpstreamProtocolViolation):
        await client.refresh_token(_cred())


# ------------------------------------------------------------------ 签名辅助

async def test_v11_requires_control_plane():
    with pytest.raises(UpstreamProtocolViolation):
        await _client(lambda request: httpx.Response(200))._v11_json(
            "GET", "/x", _cred(access_key_id="", secret_access_key=""))


async def test_v11_rejects_non_object_response():
    with pytest.raises(UpstreamProtocolViolation):
        await _client(lambda request: httpx.Response(
            200, json=[1]))._v11_json("GET", "/x", _cred())


async def test_v11_error_includes_retry_hints():
    with pytest.raises(UpstreamHTTPError) as caught:
        await _client(lambda request: httpx.Response(
            429, content=b"x", headers={"Retry-After": "3"}))._v11_json(
                "GET", "/x", _cred())
    assert caught.value.retry_after_seconds == 3


def test_gateway_of_prefers_credential_and_strips_v2():
    """两条导入路径给的形状不同：`models.json` 的 api_base 带 `/v2`，
    `client-permission-validate` 下发的不带。不归一会拼出 `/v2/v2/...`。"""
    client = OfficeAceClient(cloud_base="https://cloud.test",
                            gateway="https://default.test")
    assert client._gateway_of(_cred()) == "https://gw.test"
    assert client._gateway_of(_cred(model_base_url="")) == "https://default.test"
    assert client._gateway_of(_cred(model_base_url="https://gw.test/")) == "https://gw.test"


def test_upstream_http_error_delegates_classification():
    error = UpstreamHTTPError(403, b"ModelArts.81004")
    assert error.kind() is ErrKind.BLOCKED


def test_summarize_helper_is_shared_with_events():
    """client 与 events 必须用同一份订阅解析口径（避免两处漂移）。"""
    assert officeace_events.summarize_subscription({})["remaining"] == 0.0


def test_frames_feed_parse_all_events():
    """`stream_chat` 走 `parse_all_events`：一帧多个事件不能只取代表事件。"""
    frame = SSEFrame(event="", data=json.dumps(_chunk(
        {"content": "尾"}, finish="stop", usage={"prompt_tokens": 1})))
    assert len(officeace_events.parse_all_events(frame)) == 3

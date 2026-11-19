"""小浣熊上游客户端：请求体、SSE、模型目录、积分余额。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import httpx
import pytest

from src.provider.base import ErrKind, EventKind
from src.provider.raccoon.activity import CST
from src.provider.raccoon.client import (
    EP_AGREEMENT_CHECK,
    EP_BALANCE,
    EP_BILLS,
    EP_BIND_DEVICE,
    EP_CHAT,
    EP_HEARTBEAT,
    EP_IAM_CONFIRM,
    EP_LOGIN,
    EP_METRICS,
    EP_MODELS,
    EP_OFFICE_SETTING,
    EP_ONLINE_ACTIVITY,
    EP_USER_INFO,
    LOGIN_UA,
    LoginRejected,
    RaccoonClient,
    UpstreamHTTPError,
    UpstreamProtocolViolation,
    _model_from_item,
    prepare_body,
    request_headers,
)
from src.provider.raccoon.login import NATION_CODE
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

    # 日期相对今天构造：固定日期会随时间流逝变成「已过期」，把用例变成定时
    # 炸弹（实测 2026-11-11 这条 expire_at=11-10 已过期，get_quota 正确过滤后
    # 明细退回无流水口径，断言全崩）。
    now = datetime.now(CST)
    granted = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0,
                                                microsecond=0)
    expires = (now + timedelta(days=3)).replace(hour=23, minute=59, second=59,
                                                microsecond=0)
    grant_at = granted.isoformat()
    expire_at = expires.isoformat()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_BALANCE:
            return httpx.Response(200, json={"code": 0, "data": {
                "available_points": 4800, "daily_points": 300,
                "reward_points": 4500, "monthly_points": 0, "topup_points": 0,
                "topup_frozen": False}})
        assert request.url.path == EP_BILLS
        # 到期阶梯只要发放：用 income 过滤，避免消费流水挤占窗口
        assert request.url.params["type"] == "income"
        return httpx.Response(200, json={"code": 0, "data": {"items": [
            {"biz_type": "daily_grant", "points": 300,
             "grant_at": grant_at,
             "created_at": (granted + timedelta(hours=21, minutes=41,
                                                seconds=38)).isoformat(),
             "expire_at": expire_at},
        ]}})

    quota = await _client(handler).fetch_quota("tok")
    assert quota.remaining == 4800
    assert quota.total == 4800
    assert quota.probe_failed is False
    # 明细按发放流水逐笔展开：每日积分带真实到期；奖励池没有发放流水 →
    # 补一条「流水未覆盖」的余额行，避免 4500 分从明细里消失。
    assert [p["name"] for p in quota.packages] == [
        f"每日积分 {granted:%m-%d}", "奖励积分（流水未覆盖）"]
    assert quota.packages[0]["total"] == 300
    assert quota.packages[0]["end"] == int(expires.timestamp())
    assert quota.packages[1]["total"] == 4500
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


async def test_fetch_bills_income_filter_passes_type():
    """签到用 `income`：上游服务端过滤只留发放，消费不会挤占条数窗口。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["type"] == "income"
        return httpx.Response(200, json={"data": {"items": [
            {"biz_type": "daily_grant", "points": 300}]}})

    items = await _client(handler).fetch_bills("tok", bill_type="income")
    assert items == [{"biz_type": "daily_grant", "points": 300}]


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


async def test_visit_gets_user_info_and_returns_data():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == EP_USER_INFO
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(200, json={"code": 0, "data": {"id": "1"}})

    assert await _client(handler).visit("tok") == {"id": "1"}


async def test_visit_errors_and_missing_data():
    def http_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b'{"code":401}')

    with pytest.raises(UpstreamHTTPError):
        await _client(http_error).visit("tok")

    def non_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="nope")

    with pytest.raises(UpstreamProtocolViolation):
        await _client(non_json).visit("tok")

    def no_data(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0})

    # 没有 data 段不算失败：200 即成立
    assert await _client(no_data).visit("tok") == {}


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


async def test_login_posts_encrypted_fields_and_confirms_iam():
    """登录：走 IAM 链路，phone/password 都是 AES-CFB 密文，且登录后必发 confirm。"""
    seen: dict[str, object] = {}
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == EP_IAM_CONFIRM:
            seen["confirm_headers"] = dict(request.headers)
            seen["confirm_body"] = json.loads(request.content)
            return httpx.Response(200, json={"code": 0, "message": "success"})
        seen["ua"] = request.headers["User-Agent"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"code": "OK", "data": {
            "access_token": "new-token", "refresh_token": "r"}})

    payload = await _client(handler).login("+86 139 0000 0001", "Abcd123456")

    # 顺序固定：先登录拿 token，再确认——反过来的话 confirm 没 token 可用
    assert paths == [EP_LOGIN, EP_IAM_CONFIRM]
    assert seen["ua"] == LOGIN_UA
    body = seen["body"]
    assert isinstance(body, dict)
    # IAM 接口的 nation_code 带 `+`（旧接口是 "86"）
    assert body["nation_code"] == NATION_CODE == "+86"
    # 字段是密文：能解回原值，但原文不出现在请求体里
    from src.provider.raccoon.login import decrypt_param

    assert decrypt_param(body["phone"]) == "13900000001"
    assert decrypt_param(body["password"]) == "Abcd123456"
    assert "Abcd123456" not in request_dump(seen)
    # confirm 用新 token，且带浏览器同源的 registration_attribution
    assert seen["confirm_headers"]["authorization"] == "Bearer new-token"
    assert seen["confirm_body"] == {
        "registration_attribution": {"registration_source": "web"}}
    assert payload["access_token"] == "new-token"


async def test_confirm_iam_raises_on_upstream_error():
    """confirm 失败 → 整次登录算失败，不能把「半张」会话落库。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=b'{"code":200901}')

    with pytest.raises(UpstreamHTTPError):
        await _client(handler).confirm_iam("tok")


async def test_confirm_iam_protocol_violations():
    def non_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="nope")

    with pytest.raises(UpstreamProtocolViolation):
        await _client(non_json).confirm_iam("tok")

    def no_body(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "a", "dict"])

    # 非 dict 的 2xx 体不算失败（幂等确认，只要 200 就成立）
    assert await _client(no_body).confirm_iam("tok") == {}


def request_dump(seen: dict[str, object]) -> str:
    return json.dumps(seen["body"], ensure_ascii=False)


async def test_login_rejects_short_password_locally_without_http_call():
    """密码短于上游下限：本地拦下，不发请求（省一次必然被拒的登录）。"""
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        calls["n"] += 1
        return httpx.Response(200, json={"data": {"access_token": "t"}})

    with pytest.raises(LoginRejected, match="8 位"):
        await _client(handler).login("13900000001", "Ab1234")
    assert calls["n"] == 0


async def test_login_rejects_bad_credentials_as_relogin_required():
    """账号密码错 / 参数格式错 → LoginRejected（重试无意义，别再撞风控）。"""
    for body in (b'{"code":"AUTH_ACCOUNT_OR_PASSWORD_INVALID"}',
                 b'{"code":"COMMON_INVALID_ARGUMENT","message":"phone required"}'):
        def handler(_request: httpx.Request, payload: bytes = body) -> httpx.Response:
            return httpx.Response(400, content=payload)

        with pytest.raises(LoginRejected):
            await _client(handler).login("13900000001", "Abcd123456")


async def test_login_other_errors_are_retryable():
    def http_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b'{"code":200903}')

    with pytest.raises(UpstreamHTTPError):
        await _client(http_error).login("13900000001", "Abcd123456")

    # 响应体里没有「账号密码错」标记 → 按可重试处理（不标需重登）
    def unknown_code(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=b'{"code":"IAM_SOMETHING_ELSE"}')

    with pytest.raises(UpstreamHTTPError):
        await _client(unknown_code).login("13900000001", "Abcd123456")


async def test_login_protocol_violations():
    def non_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="nope")

    with pytest.raises(UpstreamProtocolViolation):
        await _client(non_json).login("13900000001", "Abcd123456")

    def no_token(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "OK", "data": {"refresh_token": "r"}})

    with pytest.raises(UpstreamProtocolViolation, match="access_token"):
        await _client(no_token).login("13900000001", "Abcd123456")

    def blank_token(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "OK", "data": {"access_token": "  "}})

    with pytest.raises(UpstreamProtocolViolation, match="access_token"):
        await _client(blank_token).login("13900000001", "Abcd123456")


async def test_bind_device_posts_session_and_device_headers():
    """绑定：带 x-client-* 头族与浏览器同形的 body（登录后必须走这一步）。"""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["dev_header"] = request.headers.get("x-client-device-id")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"code": 0,
                                         "data": {"device_id": "dev-x"}})

    payload = await _client(handler).bind_device("tok", "dev-x")

    assert seen["path"] == EP_BIND_DEVICE
    assert seen["dev_header"] == "dev-x"
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["client_device_id"] == "dev-x"
    assert body["client_platform"] == "web"
    assert body["application"] == "web"
    assert payload == {"device_id": "dev-x"}


async def test_bind_device_failures_are_swallowed():
    """绑定失败不该把登录判负：返回 None（token 已能用、当天补发已触发）。"""
    def http_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"code": 999999})

    assert await _client(http_error).bind_device("tok", "dev-x") is None

    def non_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="nope")

    assert await _client(non_json).bind_device("tok", "dev-x") is None

    def no_data(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0})

    # 200 但没 data：按「绑上了但现在没内容」处理，不算失败
    assert await _client(no_data).bind_device("tok", "dev-x") == {}


async def test_bind_device_transport_error_returns_none():
    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    assert await _client(boom).bind_device("tok", "dev-x") is None


async def test_page_boot_runs_browser_startup_sequence():
    """页面启动：首页 → 启动接口 → 绑定 → 心跳 → 活动上报，顺序与浏览器一致。"""
    seen: list[str] = []
    headers: dict[str, dict[str, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        headers[request.url.path] = dict(request.headers)
        if request.url.path == EP_BIND_DEVICE:
            return httpx.Response(200, json={"code": 0,
                                             "data": {"device_id": "dev-server"}})
        if request.url.path == EP_HEARTBEAT:
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    bound = await _client(handler).page_boot("tok", "dev-x")

    assert seen == ["/work/home", EP_AGREEMENT_CHECK, EP_OFFICE_SETTING,
                    EP_ONLINE_ACTIVITY, EP_BIND_DEVICE, EP_HEARTBEAT, EP_METRICS]
    # 绑定响应里的服务端设备 id 会用于随后的心跳
    assert bound == {"device_id": "dev-server"}
    hb = headers[EP_HEARTBEAT]
    assert hb["x-client-device-id"] == "dev-server"
    assert hb["x-client-platform"] == "web"
    # 首页用 text/html + 会话 cookie（SSR 侧才认得出「浏览器在加载页面」）
    assert headers["/work/home"]["accept"] == "text/html"
    assert headers["/work/home"]["cookie"] == "xhx_token=tok"


async def test_page_boot_swallows_every_failure():
    """页面启动是尽力而为：任何一步挂了都不抛（签到判定只看流水）。"""
    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    assert await _client(boom).page_boot("tok", "dev-x") is None


async def test_page_boot_tolerates_bind_without_device_id():
    """绑定响应缺 device_id → 心跳退回派生 id（不因缺字段跳过心跳）。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"code": 0, "data": {}})

    await _client(handler).page_boot("tok", "dev-x")
    assert EP_HEARTBEAT in seen

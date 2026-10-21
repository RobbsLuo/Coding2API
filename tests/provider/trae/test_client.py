"""TRAE 客户端：请求体改写、模型/额度拉取、刷新与租户安全。"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from src.compat.openai.request import InvalidRequest, parse_chat_request
from src.engine.model_resolver import UnknownModelError, resolve
from src.provider.base import ErrKind, Event, EventKind, Model
from src.provider.trae import events as trae_events
from src.provider.trae.client import (
    STATIC_MODELS,
    TraeClient,
    TraeCredential,
    TraeProvider,
    UpstreamHTTPError,
)
from src.tasks.pacer import stable_key
from tests.provider.trae._support import fixture


def _client(handler, **kw) -> TraeClient:
    transport = httpx.MockTransport(handler)
    return TraeClient(
        stream_client=httpx.AsyncClient(transport=transport, timeout=None),
        short_client=httpx.AsyncClient(transport=transport, timeout=None), **kw)

def test_resolve_flat_auto_and_forced():
    assert resolve("glm-5.2", "d").providers == (
        "codebuddy", "codebuddy_intl", "trae", "zen", "kilo", "qoder",
        "codearts", "officeace")
    assert resolve("", "glm-5.2").model == "glm-5.2"
    assert resolve("auto", "glm-5.2").model == "glm-5.2"
    assert resolve(None, "glm-5.2").model == "glm-5.2"
    forced = resolve("glm-5.2@trae", "d")
    assert forced.providers == ("trae",) and forced.forced and forced.model == "glm-5.2"

@pytest.mark.parametrize("model", ["glm-5.2@nope", "@trae", "glm-5.2@"])
def test_resolve_rejects_bad_provider_suffix(model):
    with pytest.raises(UnknownModelError):
        resolve(model, "d")

def test_parse_chat_request_ok():
    request = parse_chat_request({"messages": [{"role": "user", "content": "hi"}],
                                  "model": "glm-5.2", "stream": True})
    assert request.stream is True and request.model == "glm-5.2"

@pytest.mark.parametrize("body", [
    "not-a-dict", {}, {"messages": []}, {"messages": ["x"]},
    {"messages": [{"content": "no role"}]}, {"messages": [{"role": "user"}], "model": 5},
    {"messages": [{"role": "user"}], "stream": "yes"},
])
def test_parse_chat_request_rejects_invalid(body):
    with pytest.raises(InvalidRequest):
        parse_chat_request(body)

async def test_client_stream_chat_yields_events():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/agent/v3/llm_utils_chat"
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    events = [e async for e in _client(handler).stream_chat(
        TraeCredential(access_token="a"), {"messages": []}, "glm-5.2")]
    assert [e.kind for e in events][-1] is EventKind.FINISH

async def test_client_stream_chat_raises_classified_http_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b'{"code":1005}')

    with pytest.raises(UpstreamHTTPError) as caught:
        [e async for e in _client(handler).stream_chat(
            TraeCredential(access_token="a"), {"messages": []}, "m")]
    assert caught.value.kind() is ErrKind.PLAN

async def test_client_fetch_models_and_quota():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("get_detail_param"):
            return httpx.Response(200, json={"config_info_list": [
                {"config_name": "glm-5.2", "display_config": {"display_name": "GLM"}}]})
        return httpx.Response(200, json={"user_entitlement_pack_list": [
            {"entitlement_base_info": {"quota": {"credits_limit": 100}},
             "usage": {"credits_amount": 30}}]})

    client = _client(handler)
    models = await client.fetch_models(TraeCredential(access_token="a"))
    quota = await client.fetch_quota(TraeCredential(access_token="a"))
    assert models == [Model(id="glm-5.2", name="GLM")]
    assert quota.remaining == 70 and quota.total == 100

async def test_client_fetch_models_metadata():
    """倍率 / 上下文窗口 / 推理能力透传。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"config_info_list": [
            {"config_name": "Doubao-Seed-Evolving",
             "display_config": {"display_name": "Seed-Evolving",
                                "model_capability": "reasoning_model"},
             "context_window_tokens": {"dev": 256000},
             "display_contact_config": json.dumps({
                 "consumption_rate": {"enable": True, "data": {"rate": 0.08}}})},
            {"config_name": "glm-5.2",
             "display_config": {"display_name": "GLM"},
             "display_contact_config": "{bad json",
             "context_window_tokens": "oops"},
            {"config_name": "kimi-k3",
             "display_config": {"display_name": "K3"},
             "display_contact_config": json.dumps({
                 "consumption_rate": {"enable": True, "data": {"rate": "oops"}}}),
             "reasoning_effort_config": {"support_thinking": False}},
        ]})

    models = await _client(handler).fetch_models(TraeCredential(access_token="a"))
    assert models[0].credit_rate == 0.08
    assert models[0].max_input_tokens == 256000
    assert models[0].supports_reasoning is True
    # TRAE 无档位清单可核实 → 不编造默认档位
    assert models[0].default_effort is None
    # 坏数据 → 字段留空，不影响条目
    assert models[1].credit_rate is None and models[1].max_input_tokens is None
    # 能力字段缺失 → 回落 reasoning_effort_config.support_thinking
    assert models[1].supports_reasoning is None
    assert models[2].supports_reasoning is False
    # rate 非数值同样留空
    assert models[2].credit_rate is None

async def test_trae_supports_reasoning_branches():
    """能力判定的优先级与回落：capability 优先，其次 support_thinking。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"config_info_list": [
            # capability 非空字符串 → 不以 support_thinking 为准
            {"config_name": "c1", "display_config": {"model_capability": "chat_model"},
             "reasoning_effort_config": {"support_thinking": True}},
            # capability 为空串 → 掉到 support_thinking
            {"config_name": "c2", "display_config": {"model_capability": ""},
             "reasoning_effort_config": {"support_thinking": True}},
            # display_config 非 dict → 掉到 support_thinking
            {"config_name": "c3", "display_config": "oops",
             "reasoning_effort_config": {"support_thinking": True}},
            # support_thinking 非布尔 → None
            {"config_name": "c4", "reasoning_effort_config": {"support_thinking": "yes"}},
            # reasoning_effort_config 非 dict → None
            {"config_name": "c5", "reasoning_effort_config": ["oops"]},
            # 全部缺失 → None
            {"config_name": "c6"},
        ]})

    models = await _client(handler).fetch_models(TraeCredential(access_token="a"))
    assert [m.supports_reasoning for m in models] == [False, True, True, None, None, None]

@pytest.mark.parametrize("payload", [
    {}, {"config_info_list": "no"}, {"config_info_list": []},
])
async def test_fetch_models_rejects_bad_shapes(payload):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with pytest.raises(trae_events.UpstreamProtocolViolation):
        await _client(handler).fetch_models(TraeCredential(access_token="a"))

async def test_fetch_quota_skips_zero_and_bad_packs():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"user_entitlement_pack_list": [
            "junk", {"entitlement_base_info": {"quota": {"credits_limit": 0}}},
            {"entitlement_base_info": {"quota": {"credits_limit": 10}}, "usage": {}},
        ]})

    quota = await _client(handler).fetch_quota(TraeCredential(access_token="a"))
    assert quota.total == 10 and quota.remaining == 10
    # 只落入有额度的那个包，且名称缺失时给空串（而不是 None）
    assert quota.packages == [{"name": "", "total": 10.0, "used": 0.0, "end": None}]

async def test_fetch_quota_collects_pack_details():
    """奖励积分列表：包名逐级回落，到期取 end_time/expire_time，已用缺失当 0。

    TRAE 的权益包**各自独立到期**（每月登录积分按月、签到奖励各有到期日），
    因此 expiry_ladder 也要填：阶梯只收「未过期 + 有余额」的包，与展示明细
    同源；无到期信息的包（无 end）不进阶梯，否则选号会失去可比口径。

    到期时刻按当前时钟相对计算：写死绝对 epoch 会随时间流逝突然过期
    （本用例曾用固定时间戳，次日即假失败）。
    """
    end_first = int(time.time()) + 30 * 86400
    end_second = int(time.time()) + 10 * 86400

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"user_entitlement_pack_list": [
            # 完整字段：package_extra.package_name 优先
            {"entitlement_base_info": {
                "quota": {"credits_limit": 2000},
                "end_time": end_first,
                "product_extra": {"package_extra": {"package_name": "福利积分"}}},
             "display_desc": "老用户福利", "usage": {}},
            # 无 package_name → 回落 group_name；无 end_time → 回落 expire_time
            {"entitlement_base_info": {"quota": {"credits_limit": 500}},
             "group_name": "每月登录积分", "expire_time": end_second,
             "usage": {"credits_amount": 76.564}},
            # 无 group_name → 回落 display_desc；无到期 → None
            {"entitlement_base_info": {"quota": {"credits_limit": 150},
                                      "product_extra": {"package_extra": {}}},
             "display_desc": "签到奖励", "usage": {"credits_amount": "bad"}},
        ]})

    quota = await _client(handler).fetch_quota(TraeCredential(access_token="a"))
    assert quota.total == 2650 and quota.remaining == 2650 - 76.564
    assert quota.packages == [
        {"name": "福利积分", "total": 2000.0, "used": 0.0, "end": end_first},
        {"name": "每月登录积分", "total": 500.0, "used": 76.564, "end": end_second},
        {"name": "签到奖励", "total": 150.0, "used": 0.0, "end": None},
    ]
    # 阶梯按包顺序收「未过期 + 有余额」：剩余 = 额度 - 已用；无到期的包不进
    assert quota.expiry_ladder == [(end_first, 2000.0), (end_second, 500.0 - 76.564)]

async def test_fetch_quota_expiry_ladder_filters_expired_and_empty_packs():
    """到期阶梯口径：已过期（积分作废）与已用完（不携带积分）的包都排除，
    与 CodeBuddy 一致；只收未过期且有余额的包。"""
    now = time.time()
    future = int(now) + 3600
    past = int(now) - 3600

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"user_entitlement_pack_list": [
            # 未过期 + 有余额 → 进阶梯
            {"entitlement_base_info": {"quota": {"credits_limit": 100},
                                       "end_time": future},
             "usage": {"credits_amount": 30}},
            # 未过期但已用完（剩余 0）→ 不进阶梯
            {"entitlement_base_info": {"quota": {"credits_limit": 100},
                                       "end_time": future},
             "usage": {"credits_amount": 100}},
            # 已过期但仍有余额（积分作废）→ 不进阶梯
            {"entitlement_base_info": {"quota": {"credits_limit": 100},
                                       "end_time": past},
             "usage": {"credits_amount": 10}},
        ]})

    quota = await _client(handler).fetch_quota(TraeCredential(access_token="a"))
    assert quota.expiry_ladder == [(future, 70.0)]
    # 展示明细仍保留全部三个包（含已过期有余额的浪费提醒）
    assert len(quota.packages) == 3

async def test_fetch_quota_rejects_missing_list():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    with pytest.raises(trae_events.UpstreamProtocolViolation):
        await _client(handler).fetch_quota(TraeCredential(access_token="a"))

async def test_refresh_token_normalizes_millisecond_expiry():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Result": {"Token": "new", "RefreshToken": "r2",
                                                    "TokenExpireAt": 1_786_847_930_141}})

    refreshed = await _client(handler).refresh_token(
        TraeCredential(access_token="a", refresh_token="r1", expires_at=1))
    assert refreshed.access_token == "new" and refreshed.refresh_token == "r2"
    assert refreshed.expires_at == 1_786_847_930

async def test_refresh_token_uses_duration_when_no_timestamp():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Result": {"Token": "new",
                                                    "TokenExpireDuration": 3600}})

    refreshed = await _client(handler).refresh_token(
        TraeCredential(access_token="a", refresh_token="r"))
    assert refreshed.expires_at > 0 and refreshed.refresh_token == "r"

async def test_refresh_token_failure_leaves_original_untouched():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Result": {}})

    original = TraeCredential(access_token="a", refresh_token="r")
    with pytest.raises(trae_events.UpstreamProtocolViolation):
        await _client(handler).refresh_token(original)
    assert original.access_token == "a" and original.refresh_token == "r"

async def test_get_user_info():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Result": {"UserID": "u1", "ScreenName": "n"}})

    assert await _client(handler).get_user_info(TraeCredential(access_token="a")) == ("u1", "n")

async def test_get_user_info_rejects_bad_shape():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    with pytest.raises(trae_events.UpstreamProtocolViolation):
        await _client(handler).get_user_info(TraeCredential(access_token="a"))

async def test_post_json_surfaces_http_and_non_json_errors():
    def failing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    with pytest.raises(UpstreamHTTPError):
        await _client(failing).fetch_quota(TraeCredential(access_token="a"))

    def not_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>")

    with pytest.raises(trae_events.UpstreamProtocolViolation):
        await _client(not_json).fetch_quota(TraeCredential(access_token="a"))

    def not_object(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2])

    with pytest.raises(trae_events.UpstreamProtocolViolation):
        await _client(not_object).fetch_quota(TraeCredential(access_token="a"))

async def test_client_lazy_and_close_paths():
    client = TraeClient()
    assert client._stream() is client._stream()
    assert client._short() is client._short()
    await client.aclose()

async def test_provider_import_classify_and_models():
    provider = TraeProvider()
    data = provider.import_credential({"accessToken": "a", "uid": "u"})
    assert data["uid"] == "u"
    assert provider.classify(429, b"") is ErrKind.SOFT
    models = await provider.list_models({})
    assert [m.id for m in models] == list(STATIC_MODELS)
    with pytest.raises(trae_events.UpstreamProtocolViolation):
        provider.import_credential({"accessToken": "a"})

def test_provider_import_rejects_foreign_api_host():
    """apiHost 是用户可控输入：非白名单立即拒绝导入，不落库（PROPOSAL §6）。"""
    provider = TraeProvider()
    with pytest.raises(trae_events.UpstreamProtocolViolation) as error:
        provider.import_credential(
            {"accessToken": "a", "uid": "u", "apiHost": "https://evil.example"})
    assert "not in the TRAE allowed upstream hosts" in str(error.value)
    # 官方白名单地址（含尾斜杠归一）正常放行
    for host in ("https://api.trae.com.cn", "https://api.trae.com.cn/", ""):
        assert provider.import_credential(
            {"accessToken": "a", "uid": "u", "apiHost": host})["uid"] == "u"

async def test_refresh_ignores_foreign_api_host_from_legacy_credential():
    """旧库里带任意 apiHost 的凭证：刷新退回官方地址，绝不把 refreshToken 发出去。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"Result": {"Token": "t2", "RefreshToken": "r2"}})

    provider = TraeProvider(client=_client(handler))
    refreshed = await provider.refresh(
        {"accessToken": "a", "refreshToken": "r", "uid": "u",
         "apiHost": "https://evil.example"})
    assert refreshed["accessToken"] == "t2"
    assert all(url.startswith("https://api.trae.com.cn/") for url in seen)

async def test_user_info_ignores_foreign_api_host():
    """GetUserInfo 走同一个白名单：伪造 apiHost 不会收到 accessToken。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"Result": {"UserID": "u1", "ScreenName": "n"}})

    client = _client(handler)
    provider = TraeProvider(client=client)
    uid, nickname = await provider.client.get_user_info(
        TraeCredential(access_token="a", api_host="https://evil.example"))
    assert (uid, nickname) == ("u1", "n")
    assert all(url.startswith("https://api.trae.com.cn/") for url in seen)

async def test_provider_model_failure_negative_cache():
    """拉取失败后 5 分钟内不再打上游（静态表兜底），缓存过期后恢复拉取。"""
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500)

    provider = TraeProvider(client=_client(handler))
    first = await provider.list_models({"accessToken": "a", "uid": "u"})
    assert calls["n"] == 1
    assert [m.id for m in first] == list(STATIC_MODELS)
    # 负缓存生效：第二次不请求上游，仍回静态表
    second = await provider.list_models({"accessToken": "a", "uid": "u"})
    assert calls["n"] == 1
    assert [m.id for m in second] == list(STATIC_MODELS)
    # 模拟缓存过期：恢复动态拉取
    provider._dynamic_models_blocked_until = 0
    await provider.list_models({"accessToken": "a", "uid": "u"})
    assert calls["n"] == 2

async def test_provider_model_success_clears_negative_cache():
    """动态拉取成功后清除负缓存，后续请求恢复直连上游。"""
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500)
        return httpx.Response(200, json={"config_info_list": [
            {"config_name": "glm-5.2",
             "display_config": {"display_name": "GLM"}}]})

    provider = TraeProvider(client=_client(handler))
    await provider.list_models({"accessToken": "a", "uid": "u"})
    assert provider._dynamic_models_blocked_until is not None
    # 缓存过期后重试成功 → 清除负缓存
    provider._dynamic_models_blocked_until = 0
    models = await provider.list_models({"accessToken": "a", "uid": "u"})
    assert any(m.id == "glm-5.2" for m in models)
    assert provider._dynamic_models_blocked_until is None
    # 负缓存已清除：再次调用重新拉取（成功路径）
    before = calls["n"]
    await provider.list_models({"accessToken": "a", "uid": "u"})
    assert calls["n"] == before + 1

async def test_provider_stream_and_refresh_via_client():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    provider = TraeProvider(client=_client(handler))
    events = [e async for e in provider.stream_chat({"accessToken": "a"}, {}, "m")]
    assert events[-1].kind is EventKind.FINISH

async def test_trae_pacer_wait_and_disable():
    """TRAE stream_chat 前等待 pacer；None 不等待（与 CB 对称）。"""
    import time as _time

    from src.provider.trae.client import TraeProvider

    waited = []

    class FakePacer:
        async def wait_turn(self, key=None):
            waited.append((key, _time.monotonic()))

        def release(self, key=None):
            pass

    class FakeClient:
        async def stream_chat(self, cred, payload, model):
            yield Event(kind=EventKind.CONTENT, content="ok")

    provider = TraeProvider(client=FakeClient(), pacer=FakePacer())
    events = [e async for e in provider.stream_chat({"accessToken": "a", "uid": "u1"}, {}, "m")]
    assert events and waited
    # 桶键按账号身份派生，而不是空键
    assert waited[0][0] == stable_key("trae", "u1")

    provider2 = TraeProvider(client=FakeClient(), pacer=None)
    waited.clear()
    _ = [e async for e in provider2.stream_chat({"accessToken": "a"}, {}, "m")]
    assert not waited

def test_trae_usage_cached_tokens():
    """TRAE usage：details 路径 + 顶层兜底 + TRAE 官方字段 + 缺省 None。"""
    from src.provider.trae.events import SSEFrame

    frame = SSEFrame(event="token_usage", data=(
        '{"prompt_tokens":9,'
        '"prompt_tokens_details":{"cached_tokens":6}}'))
    assert trae_events.parse_frame(frame).usage.cached_tokens == 6

    frame = SSEFrame(event="token_usage", data='{"prompt_tokens":9,"cached_tokens":2}')
    assert trae_events.parse_frame(frame).usage.cached_tokens == 2

    # TRAE 官方字段：命中>0 与未命中=0 都要保留（0 是有效值不是缺失）
    frame = SSEFrame(event="token_usage", data=(
        '{"prompt_tokens":13,"completion_tokens":159,'
        '"cache_read_input_tokens":7,"reasoning_tokens":148}'))
    assert trae_events.parse_frame(frame).usage.cached_tokens == 7
    frame = SSEFrame(event="token_usage", data=(
        '{"prompt_tokens":13,"cache_read_input_tokens":0}'))
    assert trae_events.parse_frame(frame).usage.cached_tokens == 0

    # OpenAI 惯例字段优先于 TRAE 字段
    frame = SSEFrame(event="token_usage", data=(
        '{"prompt_tokens":9,"cached_tokens":2,"cache_read_input_tokens":5}'))
    assert trae_events.parse_frame(frame).usage.cached_tokens == 2

    frame = SSEFrame(event="token_usage", data='{"prompt_tokens":9}')
    assert trae_events.parse_frame(frame).usage.cached_tokens is None

async def test_client_stream_chat_fills_estimated_credit():
    """端到端：上游 token_usage 无积分 → 客户端流出来的 USAGE 带推算值 + 标记。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=(
            'event:token_usage\n'
            'data:{"prompt_tokens":21,"completion_tokens":142,"reasoning_tokens":135}\n\n'
            'event:done\ndata:{"finish_reason":"stop"}\n\n'))

    events = [e async for e in _client(handler).stream_chat(
        TraeCredential(access_token="a"), {"messages": []}, "glm-5.2")]
    usage = next(e.usage for e in events if e.kind is EventKind.USAGE)
    assert usage.credit is not None
    assert usage.credit_estimated is True

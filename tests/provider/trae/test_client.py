"""TRAE 客户端：请求体改写、模型/额度拉取、刷新与租户安全。"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from src.compat.openai.request import InvalidRequest, parse_chat_request
from src.engine.model_resolver import UnknownModelError, resolve
from src.provider import base
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
        "codearts", "officeace", "raccoon", "lingxi", "generic")
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

async def test_client_fetch_models_context_window_prefers_max():
    """上下文窗口取 `max`（真实上限），缺失/非法/不大于 dev 时回落 `dev`。

    只读 `dev` 会让 1M 上下文的模型报 200k，客户端据此提前压缩上下文。
    """
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"config_info_list": [
            {"config_name": "glm-5.3",
             "context_window_tokens": {"dev": 200000, "max": 1000000}},
            {"config_name": "mimo",
             "context_window_tokens": {"dev": 200000, "max": 981504}},
            # max 不大于 dev → 没有信息量，按 dev
            {"config_name": "flat",
             "context_window_tokens": {"dev": 200000, "max": 200000}},
            # max 非法 → 回落 dev
            {"config_name": "bad-max",
             "context_window_tokens": {"dev": 128000, "max": "1M"}},
            # 只有 max
            {"config_name": "max-only",
             "context_window_tokens": {"max": 400000}},
            # max 为 bool（int 子类，必须排除）
            {"config_name": "bool-max",
             "context_window_tokens": {"dev": 64000, "max": True}},
            # max 非正
            {"config_name": "zero-max",
             "context_window_tokens": {"dev": 64000, "max": 0}},
        ]})

    models = await _client(handler).fetch_models(TraeCredential(access_token="a"))
    assert [m.max_input_tokens for m in models] == [
        1000000, 981504, 200000, 128000, 400000, 64000, 64000]


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

async def test_fetch_models_accepts_body_far_above_error_body_limit(caplog):
    """模型清单是**正常业务数据**，不该被错误体那档 256KB 卡住（issue #56）。

    实测上游 54 条 config / 535,796 字节且不带 `content-length`；原先复用
    `BOUNDED_BODY_LIMIT` 让动态拉取 100% 失败、静默回退静态表。
    """
    configs = [{"config_name": f"m{i}"} for i in range(20)]
    # 用填充字段把 body 撑到 256KB 以上（真实上游是 54 条富配置）
    configs[0]["blob"] = "x" * (base.BOUNDED_BODY_LIMIT + 1024)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"config_info_list": configs})

    models = await _client(handler).fetch_models(TraeCredential(access_token="a"))
    assert len(models) == 20

async def test_fetch_models_rejects_body_above_models_limit():
    """放宽到 `MODELS_BODY_LIMIT` 仍是有界的：超过它就按协议违规早失败。"""
    from src.provider.trae.client import MODELS_BODY_LIMIT

    def huge(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b'{"blob": "' + b"x" * MODELS_BODY_LIMIT + b'"}')

    with pytest.raises(trae_events.UpstreamProtocolViolation) as caught:
        await _client(huge).fetch_models(TraeCredential(access_token="a"))
    assert "oversized" in caught.value.public_message

async def test_fetch_models_filters_invisible_and_unavailable():
    """按上游权威字段过滤：内部模型 + 本账号身份无权的会员模型。

    `is_invisible_to_user` 是客户端是否展示的权威位；`identity_list` 决定该
    模型对哪些身份开放（不含 `0` 的免费账号必回 1005）。两者都不该靠黑名单兜
    ——黑名单是全局且用户可热更的（issue #56 追加）。
    """
    def contact(identities):
        return json.dumps({"access": {"data": {"identity_list": identities}}})

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"config_info_list": [
            {"config_name": "glm-5.4"},
            # 内部模型：客户端不展示
            {"config_name": "computer_use_subagent", "is_invisible_to_user": True},
            # 免费档不可用：白名单不含 0
            {"config_name": "kimi-k3.1-pro", "display_contact_config": contact([1, 2, 3, 100])},
            # 白名单含 0：免费档可用
            {"config_name": "glm-5.3", "display_contact_config": contact([0, 5, 1])},
            # 未声明身份：不过滤（形状缺失按老行为）
            {"config_name": "qwen3.8-max", "display_contact_config": ""},
        ]})

    excluded: set[str] = set()
    models = await _client(handler).fetch_models(
        TraeCredential(access_token="a"), identities=frozenset({0}),
        excluded=excluded)
    assert [m.id for m in models] == ["glm-5.4", "glm-5.3", "qwen3.8-max"]
    assert excluded == {"computer_use_subagent", "kimi-k3.1-pro"}

async def test_fetch_models_identity_filter_passes_when_account_has_identity():
    """会员账号（身份含 1/2/3）看到会员模型：过滤只看交集，不写死免费档。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"config_info_list": [
            {"config_name": "kimi-k3.1-pro",
             "display_contact_config": json.dumps(
                 {"access": {"data": {"identity_list": [1, 2, 3, 100]}}})},
        ]})

    models = await _client(handler).fetch_models(
        TraeCredential(access_token="a"), identities=frozenset({0, 2}))
    assert [m.id for m in models] == ["kimi-k3.1-pro"]

async def test_fetch_models_without_identity_filter_keeps_everything():
    """不传 identities（老调用方）= 不做身份过滤，只滤内部模型。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"config_info_list": [
            {"config_name": "kimi-k3.1-pro",
             "display_contact_config": json.dumps(
                 {"access": {"data": {"identity_list": [1, 2, 3]}}})},
        ]})

    models = await _client(handler).fetch_models(TraeCredential(access_token="a"))
    assert [m.id for m in models] == ["kimi-k3.1-pro"]

@pytest.mark.parametrize("contact", [
    None, 123, "{bad json", json.dumps([]),
    json.dumps({"access": "no"}), json.dumps({"access": {"data": "no"}}),
    json.dumps({"access": {"data": {"identity_list": "no"}}}),
    json.dumps({"access": {"data": {"identity_list": ["0", True, None]}}}),
])
async def test_identity_list_malformed_shapes_do_not_filter(contact):
    """任何一层形状不符都返回空集（不过滤）：宁可多展示，也别把清单误滤空。"""
    from src.provider.trae.client import _identity_list

    assert _identity_list({"display_contact_config": contact}) == frozenset()

def test_identity_list_accepts_dict_contact_and_drops_non_int():
    """`display_contact_config` 在个别回包里是 dict 而非 JSON 字符串，也要认。"""
    from src.provider.trae.client import _identity_list

    assert _identity_list({"display_contact_config": {
        "access": {"data": {"identity_list": [0, True, "1", 5]}}}}) == frozenset({0, 5})

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


async def test_post_json_success_path_is_bounded():
    """成功路径同样有界：超大 body 按协议违规早失败，不把整段读进内存。"""
    def huge(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b'{"a": "' + b"x" * base.BOUNDED_BODY_LIMIT + b'"}')

    with pytest.raises(trae_events.UpstreamProtocolViolation) as caught:
        await _client(huge).fetch_quota(TraeCredential(access_token="a"))
    assert "oversized" in caught.value.public_message

async def test_client_lazy_and_close_paths():
    client = TraeClient()
    assert client._stream is client._stream
    assert client._short is client._short
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
            {"config_name": "glm-5.3",
             "display_config": {"display_name": "GLM"}}]})

    provider = TraeProvider(client=_client(handler))
    await provider.list_models({"accessToken": "a", "uid": "u"})
    assert provider._dynamic_models_blocked_until is not None
    # 缓存过期后重试成功 → 清除负缓存
    provider._dynamic_models_blocked_until = 0
    models = await provider.list_models({"accessToken": "a", "uid": "u"})
    assert any(m.id == "glm-5.3" for m in models)
    assert provider._dynamic_models_blocked_until is None
    # 负缓存已清除：再次调用重新拉取（成功路径）
    before = calls["n"]
    await provider.list_models({"accessToken": "a", "uid": "u"})
    assert calls["n"] == before + 1

async def test_provider_static_fallback_skips_excluded_models():
    """动态拉取成功但某模型被上游判为隐藏/无权 → 静态骨架不能再把它补回来。

    否则「修好动态拉取」反而多出几个一调用就 1005 的模型（issue #56 追加）。
    负缓存期间沿用上次结论：静态兜底同样干净。
    """
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"config_info_list": [
                {"config_name": "kimi-k3.1-pro",  # 属于 STATIC_MODELS
                 "display_contact_config": json.dumps(
                     {"access": {"data": {"identity_list": [1, 2, 3]}}})},
                {"config_name": "glm-5.3"},
            ]})
        return httpx.Response(500)

    provider = TraeProvider(client=_client(handler), identities=lambda: frozenset({0}))
    first = await provider.list_models({"accessToken": "a", "uid": "u"})
    assert "kimi-k3.1-pro" not in [m.id for m in first]
    assert "glm-5.3" in [m.id for m in first]
    # 第二次失败（负缓存）：静态骨架同样不含被身份过滤掉的条目
    provider._dynamic_models_blocked_until = 0
    second = await provider.list_models({"accessToken": "a", "uid": "u"})
    assert "kimi-k3.1-pro" not in [m.id for m in second]
    assert calls["n"] == 2

async def test_provider_identities_getter_is_read_per_fetch():
    """身份是热更项：每次拉取现读取值器，开通会员后下一轮即放出会员模型。"""
    identities = {"value": frozenset({0})}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"config_info_list": [
            {"config_name": "kimi-k3.1-pro",
             "display_contact_config": json.dumps(
                 {"access": {"data": {"identity_list": [1, 2, 3]}}})},
        ]})

    provider = TraeProvider(client=_client(handler),
                            identities=lambda: identities["value"])
    first = await provider.list_models({"accessToken": "a", "uid": "u"})
    assert "kimi-k3.1-pro" not in [m.id for m in first]
    # 模拟管理台把身份改成会员档（热更）
    identities["value"] = frozenset({0, 1, 2, 3})
    provider._dynamic_models_blocked_until = 0
    second = await provider.list_models({"accessToken": "a", "uid": "u"})
    assert "kimi-k3.1-pro" in [m.id for m in second]

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


async def test_client_stamps_metadata_model_hint_on_later_events():
    """extra_info 回显的真实模型名贴到后续 usage / finish 事件（观测用）。"""
    sse = (
        'event: metadata\ndata: {"model":""}\n\n'
        'event: output\ndata: {"response":"hi","reasoning_content":"","tool_calls":null}\n\n'
        'event: extra_info\ndata: {"model":"glm-5.2-real","input_token":1}\n\n'
        'event: token_usage\ndata: {"prompt_tokens":1,"completion_tokens":1}\n\n'
        'event: done\ndata: {"finish_reason":"stop"}\n\n'
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse)

    events = [e async for e in _client(handler).stream_chat(
        TraeCredential(access_token="a"), {"messages": []}, "glm-5.2")]
    content = next(e for e in events if e.kind is EventKind.CONTENT)
    usage = next(e for e in events if e.kind is EventKind.USAGE)
    finish = next(e for e in events if e.kind is EventKind.FINISH)
    # 内容帧在 extra_info 之前，拿不到提示
    assert content.model is None
    # extra_info 之后的事件都带上真实模型名
    assert usage.model == "glm-5.2-real"
    assert finish.model == "glm-5.2-real"


async def test_provider_model_failure_log_includes_type_when_message_empty(caplog):
    """裸 `TimeoutError` 消息为空 → 回退日志仍要带类名，不能只剩「: 」。"""
    class _Client:
        async def fetch_models(self, _credential, **_kwargs):
            raise TimeoutError()

    provider = TraeProvider(client=_Client())
    with caplog.at_level("WARNING", logger="src.provider.trae.client"):
        models = await provider.list_models({"accessToken": "a", "uid": "u"})
    assert [m.id for m in models] == list(STATIC_MODELS)
    assert any("TimeoutError" in record.getMessage() for record in caplog.records)

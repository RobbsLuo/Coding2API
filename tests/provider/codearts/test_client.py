"""CodeArts 上游客户端：聊天流、模型目录、额度与请求错误。"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from src.provider.base import ErrKind, EventKind, UpstreamReloginRequired
from src.provider.codearts import auth as codearts_auth
from src.provider.codearts import events as codearts_events
from src.provider.codearts.client import (
    CodeArtsClient,
    UpstreamHTTPError,
    _credit_rate,
    _flatten_content,
    _legacy_blocks,
    _models_from_items,
    _parse_ratio,
    chat_headers,
    derive_chat_id,
    new_chat_id,
    parse_credits,
    prepare_body,
    request_path,
    sdk_date,
    short_headers,
)
from tests.helpers.http import sse_text as _sse
from tests.provider.codearts._support import client as _client
from tests.provider.codearts._support import cred as _cred
from tests.provider.codearts._support import jwk as _jwk
from tests.provider.codearts._support import token_response as _token_response


def test_client_sdk_date_and_chat_id():
    assert sdk_date(0) == "19700101T000000Z"
    assert len(sdk_date()) == 16 and sdk_date().endswith("Z")
    assert len(new_chat_id()) == 32
    int(new_chat_id(), 16)

def test_client_derive_chat_id_stable_and_fallback():
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    assert derive_chat_id(payload) == derive_chat_id(payload)
    assert len(derive_chat_id(payload)) == 32
    cyclic: list = []
    cyclic.append(cyclic)                              # 不可序列化 → 退回随机
    assert len(derive_chat_id({"messages": cyclic})) == 32

def test_client_request_path_and_headers():
    assert request_path("https://h/v1/model/builtin?b=2&a=1") == (
        "/v1/model/builtin", "b=2&a=1")
    assert request_path("") == ("/", "")
    assert chat_headers(_cred(), trace_id="t")["x-auth-token"] == "TOK"
    assert short_headers("PromptCenter")["Agent-Type"] == "PromptCenter"
    assert "Agent-Type" not in short_headers()

def test_client_prepare_body_v2_and_legacy():
    messages = [{"role": "user", "content": "hi"}]
    body = prepare_body({"messages": messages, "stream": False, "temperature": 0.5,
                         "unknown": 1}, "m", chat_id="c")
    assert body["stream"] is True and body["model"] == "m"
    assert body["chat_id"] == "c" and body["prompt_cache_key"] == "c"
    assert body["tool_stream"] is True and body["temperature"] == 0.5
    assert "unknown" not in body
    body["messages"][0]["content"] = "changed"          # 深拷贝
    assert messages[0]["content"] == "hi"

    legacy = prepare_body({"messages": messages}, "m", chat_id="c", legacy=True,
                          user_id="u")
    assert legacy["task"] == "chat" and legacy["user_id"] == "u"
    assert legacy["messages"][0] == {"type": "text", "text": "hi"}
    assert "user_id" not in prepare_body({"messages": []}, "m", chat_id="c",
                                         legacy=True)

def test_client_legacy_blocks_and_flatten():
    assert _legacy_blocks("nope") == []
    blocks = _legacy_blocks([
        "junk",
        {"role": "assistant", "content": [{"text": "a"}, {"text": "b"}, "x"]},
        {"role": "user", "content": ""},
        {"role": "user", "content": "hi"},
    ])
    assert blocks == [{"type": "text", "text": "assistant: ab"},
                      {"type": "text", "text": "hi"}]
    assert _flatten_content("s") == "s"
    assert _flatten_content(1) == ""
    assert _flatten_content([None, {"text": 2}, {"text": "z"}]) == "z"
    assert _flatten_content([{"text": ""}]) == ""
    assert _flatten_content(None) == ""

def test_client_models_from_items_and_helpers():
    assert _models_from_items("nope") == []
    # credit[].ratio_display → credit_rate；无 credit 的条目 → None（不猜）。
    models = _models_from_items([
        "junk", {},
        {"model_id": "a", "model_name": "A", "contextWindow": 8000, "maxTokens": 100,
         "credit": [{"ratio_display": "0.7x", "ratio": "0.05"}]},
        {"modelId": "b", "display_name": "B", "max_input_tokens": 900},
    ])
    assert [m.id for m in models] == ["a", "b"]
    assert models[0].credit_rate == 0.7 and models[0].max_input_tokens == 8000
    assert models[0].max_output_tokens == 100
    assert models[1].credit_rate is None          # 无 credit → 不猜
    none_models = _models_from_items([
        {"model_id": "c", "name": 5, "context_window": True, "max_tokens": -1}])
    assert none_models[0].name == "c" and none_models[0].credit_rate is None
    assert none_models[0].max_input_tokens is None
    assert none_models[0].max_output_tokens is None

def test_client_credit_rate_and_ratio_parsing():
    # 多档取首条；缺 ratio_display 时继续找下一个可解析项
    assert _credit_rate([{"ratio": "0.05"}, {"ratio_display": "0.32x"}]) == 0.32
    assert _credit_rate([{"ratio_display": "0.7x"}]) == 0.7
    assert _credit_rate([1]) is None              # 非 dict 档位跳过
    assert _credit_rate("nope") is None
    assert _credit_rate([]) is None
    # 纯数字 / 无 x 后缀 / 布尔 / 非数字串 / 非法小数
    assert _parse_ratio("0.7") == 0.7
    assert _parse_ratio(0.5) == 0.5
    assert _parse_ratio("1.25x") == 1.25
    assert _parse_ratio(True) is None
    assert _parse_ratio("x0.7") is None
    assert _parse_ratio("0.7.1") is None
    assert _parse_ratio(None) is None

def test_client_parse_credits_real_shape():
    """积分账解析（2026-10-15 实测形状）：合计项进 remaining/total，分项进 packages。

    单位是积分本身，**不做 token 折算**——上游数值已是积分，套错单位会把
    13500 积分显示成 1.35。这是本渠道唯一的额度来源。
    """
    noon = 1_780_300_800
    data = {
        "metrics": [
            {"name": "usageDataChatMessages", "value": -1, "show": False},
            {"name": "usageTokenChatMessages", "value": 0,
             "usage_token_num": 3109, "package_token_amount": 1, "show": True},
            {"name": "usageTotalPackageCredit", "value": 0,
             "package_credit_amount": 13500.0, "package_credit_used": 1.28,
             "package_credit_remain": 13498.72, "show": True},
            {"name": "usageBasicPackageCredit", "value": 0,
             "package_credit_amount": 500.0, "package_credit_used": 0.0,
             "package_credit_remain": 500.0, "show": True},
            {"name": "usageOnDemandPackageCredit", "value": 0,
             "package_credit_amount": 0.0, "package_credit_used": 0.0,
             "package_credit_remain": 0.0, "show": True},
            {"name": "usageBonusPackageCredit", "value": 0,
             "package_credit_amount": 13000.0, "package_credit_used": 1.28,
             "package_credit_remain": 12998.72, "show": True},
        ],
        "start_date": "2026-10-01", "end_date": "2026-11-02",
        "package": {"package_name_cn": "体验版", "is_credit_package": True},
    }
    quota = parse_credits(data, now=noon)
    assert quota.probe_failed is False
    assert quota.remaining == pytest.approx(13498.72)   # 积分原值，不折 token
    assert quota.total == pytest.approx(13500.0)
    assert quota.probed_at == noon
    # 到期日：本地 0 点，且进到期阶梯（积分用不完同样作废）
    end = time.localtime(quota.cycle_end)
    assert (end.tm_year, end.tm_mon, end.tm_mday, end.tm_hour) == (2026, 11, 2, 0)
    assert quota.expiry_ladder == [(quota.cycle_end, pytest.approx(13498.72))]
    # 分项包带中文名与共用到期日；合计项本身不进 packages（否则重复计数）
    assert [p["name"] for p in quota.packages] == ["基础积分", "按需积分", "福利积分"]
    assert quota.packages[0] == {"name": "基础积分", "total": 500.0, "used": 0.0,
                                 "end": quota.cycle_end}
    # token 量级的 metric（usageTokenChatMessages）没有 credit 字段 → 不进 packages
    assert all(p["name"] != "usageTokenChatMessages" for p in quota.packages)


def test_client_parse_credits_unknown_and_edge_shapes():
    """缺失 / 畸形一律「未知」，绝不返回 remaining=0 被判已耗尽。"""
    assert parse_credits({}).probe_failed is True
    assert parse_credits({"metrics": "x"}).probe_failed is True
    assert parse_credits({"metrics": [{"name": "other"}]}).probe_failed is True
    # 合计项存在但数值全是 bool / 字符串 → 视为未提供
    assert parse_credits({"metrics": [
        {"name": "usageTotalPackageCredit", "package_credit_amount": True,
         "package_credit_remain": "x"}]}).probe_failed is True
    # 只有 amount 没有 remain：不猜剩余，但总量可用，且不判失败
    partial = parse_credits({"metrics": [
        {"name": "usageTotalPackageCredit", "package_credit_amount": 100.0}]})
    assert partial.probe_failed is False
    assert partial.remaining is None and partial.total == pytest.approx(100.0)
    # 无 end_date：可解析但无到期信息 → 不登记阶梯、cycle_end 为 None
    no_date = parse_credits({"metrics": [
        {"name": "usageTotalPackageCredit", "package_credit_amount": 10.0,
         "package_credit_remain": 3.0}]})
    assert no_date.cycle_end is None and no_date.expiry_ladder is None
    # 剩余为 0：不携带可消耗额度 → 不进到期排序（与每日池同口径）
    drained = parse_credits({"metrics": [
        {"name": "usageTotalPackageCredit", "package_credit_amount": 10.0,
         "package_credit_remain": 0.0}], "end_date": "2026-11-02"})
    assert drained.expiry_ladder is None and drained.cycle_end is not None
    # 畸形分项条目被跳过（非 dict / 名称非字符串 / 无任何数值）
    messy = parse_credits({"metrics": [
        "junk", {"name": 3, "package_credit_amount": 1.0},
        {"name": "usageBonusPackageCredit"},
        {"name": "usageTotalPackageCredit", "package_credit_amount": 5.0,
         "package_credit_remain": 4.0}]})
    assert messy.packages is None
    # 非法日期（月份 13）不崩、当无到期信息；未知 metric 名回落到原名
    bad_date = parse_credits({"metrics": [
        {"name": "usageNewPackageCredit", "package_credit_amount": 1.0,
         "package_credit_used": 0.0},
        {"name": "usageTotalPackageCredit", "package_credit_amount": 5.0,
         "package_credit_remain": 4.0}], "end_date": "2026-13-45"})
    assert bad_date.cycle_end is None
    assert [p["name"] for p in bad_date.packages] == ["usageNewPackageCredit"]
    # end_date 非字符串（上游换形状）同样当无到期信息
    assert parse_credits({"metrics": [
        {"name": "usageTotalPackageCredit", "package_credit_amount": 5.0}],
        "end_date": 20261102}).cycle_end is None
    # end_date 是字符串但非日期形状（正则不匹配）→ 当无到期信息
    assert parse_credits({"metrics": [
        {"name": "usageTotalPackageCredit", "package_credit_amount": 5.0}],
        "end_date": "长期有效"}).cycle_end is None


def test_client_upstream_error_kind():
    error = UpstreamHTTPError(401, b"")
    assert error.kind() is ErrKind.DEAD

def test_client_defaults_and_chat_url():
    client = CodeArtsClient(endpoint="https://snap.test/", sts_host="https://sts.test/")
    assert client.endpoint == "https://snap.test"
    assert client.sts_host == "https://sts.test"
    assert client.chat_url() == "https://snap.test" + codearts_events.EP_CHAT_V2
    legacy = CodeArtsClient(endpoint="https://snap.test", legacy=True,
                            login=codearts_auth.LoginConfig())
    assert legacy.chat_url() == "https://snap.test" + codearts_events.EP_CHAT
    assert legacy.login.client_id == codearts_events.CLIENT_ID

async def test_client_lazy_pools_and_aclose():
    client = CodeArtsClient(endpoint="https://snap.test")
    assert client._stream is client._stream          # noqa: SLF001
    assert client._short is client._short            # noqa: SLF001
    await client.aclose()
    await CodeArtsClient().aclose()                      # 两侧都没创建 → 不报错

async def test_client_stream_chat_openai_chunks_and_done():
    """v2 实测形状：标准 OpenAI chunk + `data:[DONE]`。"""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == codearts_events.EP_CHAT_V2
        assert request.url.host == "snap.test"
        body = json.loads(request.content)
        assert body["stream"] is True and body["messages"][0]["content"] == "x"
        assert body["tool_stream"] is True
        assert request.headers["Authorization"].startswith("SDK-HMAC-SHA256 ")
        return httpx.Response(200, text=_sse(
            {"choices": [{"index": 0, "delta": {"role": "assistant",
                                                "reasoning_content": ""},
                           "finish_reason": None}], "usage": None},
            {"choices": [{"index": 0, "delta": {"content": "",
                                                "reasoning_content": "想"},
                           "finish_reason": None}], "usage": None},
            {"choices": [{"index": 0, "delta": {"content": "好"},
                           "finish_reason": None}], "usage": None},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": None},
            {"choices": [], "usage": {"prompt_tokens": 35, "completion_tokens": 45}},
        ) + "data:[DONE]\n")

    events = [e async for e in _client(handler).stream_chat(
        _cred(), {"messages": [{"role": "user", "content": "x"}]}, "m")]
    assert [e.content for e in events if e.kind is EventKind.CONTENT] == ["好"]
    assert [e.content for e in events if e.kind is EventKind.REASONING] == ["想"]
    assert events[-2].kind is EventKind.USAGE
    assert events[-2].usage.input_tokens == 35
    assert events[-2].usage.output_tokens == 45

async def test_client_stream_chat_snapshot_semantics_and_done():
    """旧形状（累计 `text`）仍兼容：快照做差 + `{"text":"[DONE]"}` 结束。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse(
            {"text": "He"},                                  # 增量 He
            {"text": "Hello"},                               # 前缀 → 增量 llo
            {"text": "Hello"},                               # 无新内容 → 不产事件
            {"text": "Hi"},                                  # 非前缀 → 整段重发
            {"text": "[DONE]", "error_code": "0"},           # 正常结束
        ))

    events = [e async for e in _client(handler).stream_chat(
        _cred(), {"messages": [{"role": "user", "content": "x"}]}, "m")]
    assert [e.content for e in events if e.kind is EventKind.CONTENT] == [
        "He", "llo", "Hi"]
    assert [e.kind for e in events][-1] is EventKind.FINISH

async def test_client_stream_chat_embedded_error():
    """错误内嵌在 HTTP 200 流里：原始业务码进 error_message，分类进 error_kind。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse(
            {"text": "[DONE]", "error_code": "ChatAgent.00001001",
             "error_msg": "busy"}))

    events = [e async for e in _client(handler).stream_chat(
        _cred(), {"messages": []}, "m")]
    assert len(events) == 1 and events[0].kind is EventKind.ERROR
    assert events[0].error_message == "ChatAgent.00001001 busy"
    assert events[0].error_kind is ErrKind.OTHER

async def test_client_stream_chat_legacy_and_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == codearts_events.EP_CHAT
        body = json.loads(request.content)
        assert body["task"] == "chat"
        assert body["user_id"] == "alice"
        return httpx.Response(200, text=_sse({"text": "[DONE]"}))

    events = [e async for e in _client(handler, legacy=True).stream_chat(
        _cred(), {"messages": [{"role": "user", "content": "x"}]}, "m")]
    assert events[-1].kind is EventKind.FINISH

    def error_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    with pytest.raises(UpstreamHTTPError) as caught:
        [e async for e in _client(error_handler).stream_chat(_cred(), {}, "m")]
    assert caught.value.kind() is ErrKind.OTHER

async def test_client_fetch_models_builtin_only():
    """模型发现只走内置接口 `GET {endpoint}/v1/model/builtin`（Agent-Type）。"""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.host + request.url.path,
                      request.headers.get("Agent-Type", "")))
        assert request.method == "GET"
        return httpx.Response(200, json={"builtinModels": [
            "junk",
            {"model_id": "m1", "model_name": "M1",
             "credit": [{"ratio": "0.05"}, {"ratio_display": "0.7x"}]},
            {"name": "m2"},
            {"id": 7},                                             # 坏条目跳过
        ]})

    client = _client(handler)
    models = await client.fetch_models(_cred())
    assert [m.id for m in models] == ["m1", "m2"]
    assert models[0].credit_rate == 0.7
    assert calls == [("snap.test" + codearts_events.EP_MODEL_BUILTIN, "PromptCenter")]
    await client.aclose()

async def test_client_fetch_models_empty_raises_violation():
    """空列表（含 `builtinModels` 缺失/非列表）→ 协议违规，交由调用方退避。"""
    for body in ({}, {"builtinModels": []}, {"builtinModels": "nope"}):
        with pytest.raises(codearts_events.UpstreamProtocolViolation, match="empty list"):
            await _client(lambda _r, _b=body: httpx.Response(200, json=_b)).fetch_models(
                _cred())

async def test_client_fetch_models_propagates_upstream_failure():
    """内置接口失败直接上抛（不再吞掉后合并福利目录兜底）。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"builtin down")

    with pytest.raises(UpstreamHTTPError):
        await _client(handler).fetch_models(_cred())

async def test_client_identity_paths():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == codearts_events.EP_CALLER_IDENTITY:
            return httpx.Response(200, json={
                "principal_id": "pid", "account_id": "acc",
                "principal_urn": "urn:...:user:carol"})
        if request.url.path == "/snap-manager" + codearts_events.EP_CURRENT_USER:
            return httpx.Response(200, json={
                "user_id": "u", "user_name": "n", "domain_id": "d"})
        return httpx.Response(500, content=b"unexpected")

    client = _client(handler)
    assert await client.caller_identity(_cred()) == ("pid", "carol", "acc")
    assert await client.current_user(_cred()) == ("u", "n", "d")

    def urnless(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"principal_id": "p", "account_id": "a"})

    assert await _client(urnless).caller_identity(_cred()) == ("p", "", "a")

async def test_client_probe_quota_hits_only_statistics_endpoint():
    """额度只读一个端点：`/snap-manager/v1/statistics/plugin`，带 PromptCenter 头。"""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, request.headers.get("Agent-Type", "")))
        return httpx.Response(200, json={
            "metrics": [{"name": "usageTotalPackageCredit",
                         "package_credit_amount": 13500.0,
                         "package_credit_remain": 13498.72},
                        {"name": "usageBonusPackageCredit",
                         "package_credit_amount": 13000.0,
                         "package_credit_used": 1.28}],
            "end_date": "2026-11-02"})

    quota = await _client(handler).probe_quota(_cred())
    assert quota.remaining == pytest.approx(13498.72)
    assert quota.total == pytest.approx(13500.0)
    assert [p["name"] for p in quota.packages] == ["福利积分"]
    assert calls == [(codearts_events.EP_STATISTICS_PLUGIN, "PromptCenter")]

async def test_client_probe_quota_propagates_upstream_failure():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"down")

    with pytest.raises(UpstreamHTTPError):
        await _client(handler).probe_quota(_cred())

async def test_client_probe_quota_never_refreshes_even_in_window():
    """额度探测只读余额：即使凭证已在刷新窗口，也不得消费一次性 refresh_token。

    刷新唯一归 `RefreshTask`（先落库再同步）。这里若刷新，会与 RefreshTask 抢
    同一个一次性 token，后到的报 `the refresh token has been used` 且本处结果
    不落库——实测由此把渠道打成硬失效。
    """
    jwk = _jwk()
    sts_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sts_calls
        if request.url.host == "sts.test":
            sts_calls += 1
            return httpx.Response(200, json=_token_response())
        return httpx.Response(200, json={
            "metrics": [{"name": "usageTotalPackageCredit",
                         "package_credit_amount": 100.0,
                         "package_credit_remain": 7.0}]})

    client = _client(handler)
    expiring = _cred(refresh_token="rt", dpop_private_jwk=jwk,
                     expiration=int(time.time()) + 5)
    quota = await client.probe_quota(expiring)
    assert sts_calls == 0 and quota.remaining == pytest.approx(7.0)

async def test_client_refresh_token_rotates_via_sts():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "sts.test"
        assert request.headers["DPoP"]
        form = httpx.QueryParams(request.content.decode())
        assert form["grant_type"] == "refresh_token"
        assert form["refresh_token"] == "rt-old"
        return httpx.Response(200, json={
            "credentials": {"access_key_id": "AKN", "secret_access_key": "SKN",
                            "security_token": "TOKN"},
            "refresh_token": "rt-new", "user_id": "u1"})

    refreshed = await _client(handler).refresh_token(
        _cred(refresh_token="rt-old", dpop_private_jwk=_jwk()))
    assert refreshed.refresh_token == "rt-new"              # 必须轮转回写
    assert refreshed.access_key_id == "AKN"

async def test_client_refresh_token_spent_raises_relogin_required():
    """一次性票被消费 → 终态异常（不是可重试失败，RefreshTask 据此停手）。"""
    def burned(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=(
            b'{"error_code":"STS5.1806","error_msg":"invalid refresh token: '
            b"\'the refresh token has been used\'\"}"))

    with pytest.raises(UpstreamReloginRequired, match="需重新登录"):
        await _client(burned).refresh_token(
            _cred(refresh_token="rt-old", dpop_private_jwk=_jwk()))

async def test_client_refresh_token_retryable_failure_not_terminal():
    """5xx / 普通 4xx 是可重试的失败：原样上抛，不能升级成终态。"""
    def server_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"upstream busy")

    with pytest.raises(codearts_auth.TokenEndpointError):
        await _client(server_error).refresh_token(
            _cred(refresh_token="rt-old", dpop_private_jwk=_jwk()))

async def test_client_request_json_error_paths():
    def http_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=b"bad gateway")

    with pytest.raises(UpstreamHTTPError):
        await _client(http_error).caller_identity(_cred())

    def non_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="nope")

    with pytest.raises(codearts_events.UpstreamProtocolViolation, match="non-JSON"):
        await _client(non_json).caller_identity(_cred())

    # 对外文案不得带上游端点 URL（PROPOSAL §6「错误响应不回流上游正文」）：
    # 异常文本只进日志，public_message 才是回客户端的受控标识。
    def non_json_capture(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="nope")

    with pytest.raises(codearts_events.UpstreamProtocolViolation) as caught:
        await _client(non_json_capture).caller_identity(_cred())
    assert caught.value.public_message == "upstream returned a non-JSON response"
    assert "http" not in caught.value.public_message

    def non_object(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2])

    with pytest.raises(codearts_events.UpstreamProtocolViolation, match="shape"):
        await _client(non_object).caller_identity(_cred())

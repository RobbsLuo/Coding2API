"""officeAce 协议解析：SSE、错误分类、订阅/积分、模型目录。"""

from __future__ import annotations

import json

import pytest

from src.engine.sse import SSEFrame
from src.provider.base import ErrKind, EventKind, UpstreamProtocolViolation
from src.provider.officeace import events


def _frame(data: object) -> SSEFrame:
    return SSEFrame(event="", data=data if isinstance(data, str) else json.dumps(data))


def _chunk(delta: dict, *, finish: str | None = None,
           usage: dict | None = None) -> dict:
    payload: dict = {"choices": [{"index": 0, "delta": delta,
                                  "finish_reason": finish}]}
    if usage is not None:
        payload["usage"] = usage
    return payload


# ------------------------------------------------------------------ 需重登

def test_relogin_required_hits_named_markers():
    assert events.relogin_required(
        b'{"error":"The refresh token has been used"}') == "刷新令牌已失效，需重新登录"
    assert events.relogin_required(b"STS5.1806 invalid client id")
    assert events.relogin_required(b"InvalidDPoPHeader")


def test_relogin_required_ignores_retryable_bodies():
    """5xx / 网络类不许判成「不可自愈」：误判会永久废掉一条好凭证。"""
    assert events.relogin_required(b"Connection reset by peer") == ""
    assert events.relogin_required(b"") == ""


# ------------------------------------------------------------------ 状态码

@pytest.mark.parametrize("status, body, want", [
    (403, b'{"error_code":"ModelArts.81004"}', ErrKind.BLOCKED),
    (404, b'ModelArts.81009 model not found', ErrKind.BLOCKED),
    (429, b'ModelArts.81111 rate limited', ErrKind.MODEL),
    (200, b'ModelArts.81114', ErrKind.MODEL),
    (400, b'ModelArts.81113 ^C too large', ErrKind.REQUEST),
    (400, b'ModelArts.81001 invalid max_tokens', ErrKind.INVALID),
    (401, b'APIG.1009 app not found', ErrKind.DEAD),
    (401, b'AppKey or AppSecret is invalid', ErrKind.DEAD),
    (403, b'APIG.0303 app not found', ErrKind.DEAD),
    (402, b'payment required', ErrKind.CREDIT),
    (400, b'{"code":14018}', ErrKind.CREDIT),
    (400, b'Insufficient Balance', ErrKind.CREDIT),
    (429, b'too many requests', ErrKind.MODEL),
    (401, b'unknown', ErrKind.DEAD),
    (403, b'unknown', ErrKind.DEAD),
    (400, b'unknown', ErrKind.INVALID),
    (404, b'unknown', ErrKind.INVALID),
    (422, b'unknown', ErrKind.INVALID),
    (500, b'upstream exploded', ErrKind.OTHER),
])
def test_classify_status(status, body, want):
    assert events.classify_status(status, body) is want


def test_classify_status_model_reject_wins_over_403():
    """81004 走 403：若按状态码判会 DEAD，把整条凭据打死。"""
    assert events.classify_status(403, b'{"code":"ModelArts.81004"}') is ErrKind.BLOCKED


@pytest.mark.parametrize("code, message, want", [
    (None, "ModelArts.81004", ErrKind.BLOCKED),
    (None, "ModelArts.81009", ErrKind.BLOCKED),
    (None, "ModelArts.81111", ErrKind.MODEL),
    (None, "ModelArts.81112", ErrKind.MODEL),
    (None, "ModelArts.81114", ErrKind.MODEL),
    (None, "ModelArts.81113", ErrKind.REQUEST),
    (None, "ModelArts.81001", ErrKind.INVALID),
    (None, "AppKey or AppSecret is invalid", ErrKind.DEAD),
    (None, "points exhausted", ErrKind.CREDIT),
    (402, "", ErrKind.CREDIT),
    (429, "", ErrKind.MODEL),
    (401, "", ErrKind.DEAD),
    (403, "", ErrKind.DEAD),
    (400, "", ErrKind.INVALID),
    (404, "", ErrKind.INVALID),
    (422, "", ErrKind.INVALID),
    (500, "", ErrKind.OTHER),
    (None, "", ErrKind.OTHER),
])
def test_classify_error_code(code, message, want):
    assert events.classify_error_code(code, message) is want


# ------------------------------------------------------------------ SSE 帧

def test_parse_frame_skips_empty_and_done():
    assert events.parse_frame(SSEFrame(event="", data="")) is None
    assert events.parse_frame(SSEFrame(event="", data="  [DONE]  ")) is None


def test_parse_frame_rejects_broken_json():
    with pytest.raises(UpstreamProtocolViolation):
        events.parse_frame(SSEFrame(event="", data="{not json"))


def test_parse_frame_rejects_non_object():
    with pytest.raises(UpstreamProtocolViolation):
        events.parse_frame(SSEFrame(event="", data="[1,2]"))


def test_parse_frame_content():
    event = events.parse_frame(_frame(_chunk({"content": "hi"})))
    assert event is not None and event.kind is EventKind.CONTENT
    assert event.content == "hi"


def test_parse_frame_reasoning():
    event = events.parse_frame(_frame(_chunk({"reasoning_content": "想"})))
    assert event is not None and event.kind is EventKind.REASONING
    assert event.content == "想"


def test_parse_frame_tool_calls_filters_blank():
    """只有 name 与 arguments 都空的噪声分片才丢；带参数的必须保留。"""
    blank = {"index": 0, "function": {"name": "", "arguments": ""}}
    real = {"index": 1, "function": {"name": "search", "arguments": ""}}
    event = events.parse_frame(_frame(_chunk({"tool_calls": [blank, real]})))
    assert event is not None and event.kind is EventKind.TOOL_CALLS
    assert event.tool_calls == [real]


def test_parse_frame_blank_tool_calls_fall_through():
    """全是噪声分片时不能产出空 TOOL_CALLS；无其它内容则整帧无事件。"""
    blank = {"index": 0, "function": {"name": "", "arguments": None}}
    assert events.parse_frame(_frame(_chunk({"tool_calls": [blank, "x"]}))) is None
    event = events.parse_frame(_frame(_chunk({"tool_calls": [blank],
                                              "content": "正文"})))
    assert event is not None and event.kind is EventKind.CONTENT
    assert event.content == "正文"


def test_parse_frame_tool_calls_not_a_list_is_ignored():
    event = events.parse_frame(_frame(_chunk({"tool_calls": "x",
                                              "content": "正文"})))
    assert event is not None and event.kind is EventKind.CONTENT


def test_parse_frame_error_object():
    payload = {"error": {"code": 81111, "message": "rate limited"}}
    event = events.parse_frame(_frame(payload))
    assert event is not None and event.kind is EventKind.ERROR
    assert event.error_code == 81111
    assert event.error_kind is ErrKind.MODEL


def test_parse_frame_string_business_code():
    """伪 200 常把码写成字符串（`ModelArts.81004`）：必须并进分类文本。"""
    event = events.parse_frame(_frame({"error": {
        "code": "ModelArts.81004", "message": "no permission"}}))
    assert event is not None and event.kind is EventKind.ERROR
    assert event.error_code is None
    assert event.error_message == "ModelArts.81004 no permission"
    assert event.error_kind is ErrKind.BLOCKED


def test_parse_frame_error_string():
    event = events.parse_frame(_frame({"error": "boom"}))
    assert event is not None and event.kind is EventKind.ERROR
    assert event.error_message == "boom"


def test_parse_frame_usage_only():
    event = events.parse_frame(_frame(_chunk({}, usage={"prompt_tokens": 3})))
    assert event is not None and event.kind is EventKind.USAGE
    assert event.usage is not None and event.usage.input_tokens == 3


def test_parse_frame_finish_only():
    event = events.parse_frame(_frame(_chunk({}, finish="stop")))
    assert event is not None and event.kind is EventKind.FINISH
    assert event.finish_reason == "stop"


def test_parse_frame_no_choices_and_nothing_else():
    assert events.parse_frame(_frame({"id": "x"})) is None


def test_parse_all_events_adds_usage_and_finish():
    """合并帧（正文 + finish + usage）必须拆全，否则客户端收不到结束信号。"""
    payload = _chunk({"content": "尾"}, finish="stop",
                     usage={"prompt_tokens": 1, "completion_tokens": 2})
    got = events.parse_all_events(_frame(payload))
    assert [e.kind for e in got] == [EventKind.CONTENT, EventKind.USAGE,
                                     EventKind.FINISH]


def test_parse_all_events_does_not_duplicate():
    payload = _chunk({}, usage={"prompt_tokens": 1})
    payload["usage"] = {"prompt_tokens": 1}
    got = events.parse_all_events(_frame(payload))
    assert [e.kind for e in got].count(EventKind.USAGE) == 1


def test_parse_all_events_empty_frame():
    assert events.parse_all_events(SSEFrame(event="", data="")) == []


def test_parse_all_events_finish_already_present():
    """代表性事件本身就是 FINISH 时不再补一条。"""
    got = events.parse_all_events(_frame(_chunk({}, finish="length")))
    assert [e.kind for e in got] == [EventKind.FINISH]


# ------------------------------------------------------------------ usage

def test_usage_reads_nested_details():
    usage = events._usage({
        "prompt_tokens": 10, "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 4},
        "completion_tokens_details": {"reasoning_tokens": 7},
    })
    assert (usage.input_tokens, usage.output_tokens) == (10, 20)
    assert (usage.cached_tokens, usage.reasoning_tokens) == (4, 7)


def test_usage_falls_back_to_flat_fields():
    usage = events._usage({"cached_tokens": 5, "reasoning_tokens": 6,
                           "prompt_tokens": "x", "completion_tokens": True})
    assert usage.cached_tokens == 5 and usage.reasoning_tokens == 6
    assert usage.input_tokens is None and usage.output_tokens is None


def test_usage_ignores_bool_inside_details():
    """bool 是 int 子类：`True` 不能被当成 token 数。"""
    usage = events._usage({
        "prompt_tokens_details": {"cached_tokens": True},
        "completion_tokens_details": {"reasoning_tokens": False},
        "cached_tokens": 3, "reasoning_tokens": 4,
    })
    assert usage.cached_tokens == 3 and usage.reasoning_tokens == 4


# ------------------------------------------------------------------ 数值

@pytest.mark.parametrize("value, want", [
    (True, 0.0), (False, 0.0), (5, 5.0), (2.5, 2.5),
    (" 4000 ", 4000.0), ("oops", 0.0), (None, 0.0), ([], 0.0),
])
def test_number(value, want):
    assert events._number(value) == want


@pytest.mark.parametrize("value, want", [
    ("2026-10-21T15:59:59Z", 1792598399),
    ("2026-10-21T23:59:59+08:00", 1792598399),
    ("", 0), (None, 0), (123, 0), ("not-a-date", 0),
])
def test_epoch_from_iso(value, want):
    assert events.epoch_from_iso(value) == want


def test_beijing_day_uses_utc8():
    # 2026-10-21T16:30:00Z = 北京时间 2026-10-22 00:30
    assert events.beijing_day(1792600200) == "2026-10-22"
    assert events.beijing_day(1792598399) == "2026-10-21"


# ------------------------------------------------------------------ 订阅

def _subscription(**over) -> dict:
    base = {
        "subscribe_status": "SUBSCRIBED",
        "domain_id": "dom",
        "skus": [{"sku_name": "专业版", "quotas": [
            {"sku_attr_code": "officeace_points", "sku_value": "4000",
             "current_value": "1000",
             "expired_time": "2026-12-31T15:59:59Z"},
        ]}],
        "bonus_skus": [
            {"activity_id": "new-user", "activity_name": "新用户福利",
             "points": "4000", "current_value": "0",
             "expired_time": "2026-12-31T15:59:59Z",
             "effective_time": "2026-10-20T00:00:00Z",
             "cbc_resource_id": "bonus:x:bonus-new-user-2026"},
            {"activity_id": "daily", "activity_name": "每日签到有礼",
             "points": "1000", "current_value": "0",
             "expired_time": "2026-10-21T15:59:59Z",
             "effective_time": "2026-10-21T01:00:00Z",
             "cbc_resource_id": "bonus:x:daily"},
        ],
    }
    base.update(over)
    return base


def test_summarize_subscription_sums_both_sources():
    summary = events.summarize_subscription(_subscription(), now=1792546252)
    assert summary["total"] == 9000.0
    assert summary["remaining"] == 8000.0
    assert summary["subscribe_status"] == "SUBSCRIBED"
    assert summary["domain_id"] == "dom"
    assert [p["name"] for p in summary["packages"]] == ["专业版", "新用户福利", "每日签到有礼"]
    assert summary["expiry_ladder"] == sorted(summary["expiry_ladder"])
    assert summary["cycle_end"] == events.epoch_from_iso("2026-10-21T15:59:59Z")


def test_summarize_subscription_skips_expired_entries():
    payload = _subscription(skus=[{"quotas": [
        {"sku_attr_code": "officeace_points", "sku_value": "100",
         "expired_time": "2026-01-01T00:00:00Z"}]}],
        bonus_skus=[{"activity_id": "old", "points": "50",
                     "expired_time": "2026-01-01T00:00:00Z"}])
    summary = events.summarize_subscription(payload, now=1792546252)
    assert summary["total"] == 0.0 and summary["packages"] == []


def test_summarize_subscription_tolerates_garbage():
    """上游改版/字段缺失不许抛错：额度列宁可显示 0，也不能让探测整轮失败。"""
    summary = events.summarize_subscription("not-a-dict", now=1792546252)
    assert summary["remaining"] == 0.0 and summary["packages"] == []
    assert summary["daily"] is None and summary["cycle_end"] is None

    payload = {"skus": [None, {"quotas": "x", "sku_name": None},
                        {"quotas": [None, {"sku_attr_code": "other"}]}],
               "bonus_skus": [None, {"points": "10"}]}
    summary = events.summarize_subscription(payload, now=1792546252)
    # 没有 activity_id 的 bonus 仍计入总额（只是进不了 bonus_remaining）
    assert summary["total"] == 10.0
    assert summary["packages"] == [{"name": "奖励积分", "total": 10.0,
                                    "used": 0.0, "end": 0}]


def test_summarize_subscription_negative_balance_clamps_to_zero():
    """已用 > 总额（上游结算延迟）时剩余夹到 0，且不进到期阶梯。"""
    payload = {"skus": [], "bonus_skus": [
        {"activity_id": "a", "points": "10", "current_value": "30",
         "expired_time": "2026-12-31T00:00:00Z"}]}
    summary = events.summarize_subscription(payload, now=1792546252)
    assert summary["remaining"] == 0.0
    assert summary["expiry_ladder"] == []
    assert summary["cycle_end"] is None


def test_summarize_subscription_no_ladder_means_no_cycle_end():
    payload = _subscription(
        skus=[{"quotas": [{"sku_attr_code": "officeace_points",
                           "sku_value": "100", "current_value": "100"}]}],
        bonus_skus=[])
    summary = events.summarize_subscription(payload, now=1792546252)
    assert summary["remaining"] == 0.0
    # 无到期时间 → 不进 ladder → cycle_end 为 None（不是 0）
    assert summary["cycle_end"] is None


def test_summarize_subscription_daily_marks_claimed_today():
    summary = events.summarize_subscription(_subscription(), now=1792546252)
    daily = summary["daily"]
    assert daily is not None
    assert daily["claimed"] is True
    assert daily["credit"] == 1000.0
    assert daily["activity_id"] == "daily"


def test_summarize_subscription_daily_not_claimed_yesterday():
    payload = _subscription(bonus_skus=[{
        "activity_id": "daily", "activity_name": "每日签到有礼",
        "points": "1000", "current_value": "0",
        "expired_time": "2026-10-21T15:59:59Z",
        "effective_time": "2026-10-20T01:00:00Z",
        "cbc_resource_id": "bonus:x:daily"}])
    daily = events.summarize_subscription(payload, now=1792546252)["daily"]
    assert daily is not None and daily["claimed"] is False


def test_daily_activity_skips_non_daily_and_expired():
    now = 1792546252
    assert events._daily_activity([None, {"activity_name": "新用户福利"}], now) is None
    expired = {"activity_name": "每日签到有礼",
               "expired_time": "2026-10-20T15:59:59Z"}
    assert events._daily_activity([expired], now) is None


def test_daily_activity_recognises_resource_marker():
    now = 1792546252
    bonus = {"cbc_resource_id": "bonus:x:DAILY", "points": "100",
             "expired_time": "2026-10-21T15:59:59Z"}
    daily = events._daily_activity([bonus], now)
    assert daily is not None
    assert daily["activity_name"] == "每日签到"
    assert daily["claimed"] is False


def test_bonus_remaining_maps_activity_to_points():
    got = events.bonus_remaining(_subscription())
    assert got == {"new-user": 4000.0, "daily": 1000.0}
    assert events.bonus_remaining("x") == {}
    assert events.bonus_remaining({"bonus_skus": [None, {}, {"points": "1"}]}) == {}


# ------------------------------------------------------------------ max_tokens

@pytest.mark.parametrize("model, want", [
    ("glm-5.2", 131072), ("GLM-5", 131072), ("qwen3-max", 65536),
    ("deepseek-v4.1-flash", 393216), ("unknown-model", events.DEFAULT_MAX_TOKENS),
    ("", events.DEFAULT_MAX_TOKENS),
])
def test_max_tokens_cap(model, want):
    assert events.max_tokens_cap(model) == want


@pytest.mark.parametrize("value, model, want", [
    (1, "glm-5.2", 1), (15, "glm-5.2", 15), (16, "glm-5.2", 1040),
    (64, "glm-5.2", 1088), (131072, "glm-5.2", 131072),
    (392192, "deepseek-v4.1-flash", 393216),
])
def test_clamp_max_tokens(value, model, want):
    assert events.clamp_max_tokens(value, model) == want


# ------------------------------------------------------------------ 模型

def test_model_id_prefers_id_then_aliases():
    assert events.model_id_from_item({"id": " a "}) == "a"
    assert events.model_id_from_item({"model": "b"}) == "b"
    assert events.model_id_from_item({"model_name": "c"}) == "c"
    assert events.model_id_from_item({"id": "  ", "model": "b"}) == "b"
    assert events.model_id_from_item({}) == ""


def test_parse_models_accepts_both_shapes():
    assert events.parse_models({"data": [{"id": "a"}, {"nope": 1}]}) == [{"id": "a"}]
    assert events.parse_models({"models": [{"id": "b"}]}) == [{"id": "b"}]


def test_parse_models_rejects_missing_list():
    with pytest.raises(UpstreamProtocolViolation):
        events.parse_models({"object": "list"})
    with pytest.raises(UpstreamProtocolViolation):
        events.parse_models("nope")


def test_parse_rates_normalises_names():
    got = events.parse_rates({"models": [
        {"model_name": " kimi-k3\n", "cost_coefficient": 1.5},
        {"model_name": "", "cost_coefficient": 1},
        {"model_name": 5, "cost_coefficient": 1},
        {"model_name": "bad", "cost_coefficient": True},
        {"model_name": "bad2", "cost_coefficient": "1"},
        None, {"model_name": "no-rate"},
    ]})
    assert got == {"kimi-k3": 1.5}


def test_parse_rates_tolerates_non_dict():
    assert events.parse_rates(None) == {}


def test_parse_model_info_reads_subscription_block():
    payload = {
        "account_id": "acct", "principal_id": "prin",
        "subscription": {"model_info": {
            "model_api_url_base": "gw.test/v2",
            "model_auth_info": {"model_app_key": "K", "model_app_secret": "S"},
        }},
    }
    assert events.parse_model_info(payload) == {
        "account_id": "acct", "principal_id": "prin", "user_name": "",
        "app_key": "K", "app_secret": "S", "base_url": "https://gw.test/v2",
    }


def test_parse_model_info_reads_top_level_block():
    payload = {"model_info": {"model_api_url_base": "https://gw.test/v2/"}}
    info = events.parse_model_info(payload)
    assert info["base_url"] == "https://gw.test/v2"
    assert info["app_key"] == "" and info["app_secret"] == ""


def test_parse_model_info_unprovisioned_account():
    """未开通：`subscription: null`（实测三账号全如此）→ 各字段空。"""
    info = events.parse_model_info({"account_id": "a", "subscription": None,
                                    "model_info": {}})
    assert info == {"account_id": "a", "principal_id": "", "user_name": "",
                    "app_key": "", "app_secret": "", "base_url": ""}


def test_parse_model_info_tolerates_garbage():
    assert events.parse_model_info("x")["app_key"] == ""
    assert events.parse_model_info({"model_info": "x"})["base_url"] == ""
    assert events.parse_model_info(
        {"subscription": {"model_info": {"model_auth_info": "x"}}})["base_url"] == ""


def test_parse_model_info_reads_user_name_from_principal_urn():
    """用户名只在 `principal_urn` 末尾，接口没有独立字段（2026-10-21 实测）。"""
    info = events.parse_model_info(
        {"principal_urn": "iam::acct:user:robbsluo"})
    assert info["user_name"] == "robbsluo"


def test_parse_model_info_prefers_explicit_user_name():
    for key in ("user_name", "userName"):
        info = events.parse_model_info(
            {key: " carol ", "principal_urn": "iam::acct:user:ignored"})
        assert info["user_name"] == "carol"


def test_parse_model_info_urn_without_user_marker_is_blank():
    """URM 形态变了也不能炸：认不出就留空，昵称退到账号 ID。"""
    assert events.parse_model_info({"principal_urn": "iam::acct"})["user_name"] == ""
    assert events.parse_model_info(
        {"principal_urn": 123})["user_name"] == ""
    assert events.parse_model_info(
        {"user_name": "  "})["user_name"] == ""


# ------------------------------------------------------------------ 授权 URL

def test_build_authorize_url_wraps_login_page():
    url = events.build_authorize_url(
        auth_base="https://auth.test", client_id="cid", state="st",
        code_challenge="ch", redirect_uri="https://cloud.test/cb")
    assert url.startswith("https://auth.test/authui/login.html?service=")
    from urllib.parse import parse_qs, unquote

    inner = unquote(url.split("service=", 1)[1])
    assert inner.startswith("https://auth.test/authui/v1/oauth2/authorize?")
    query = parse_qs(inner.split("?", 1)[1])
    assert query == {
        "client_id": ["cid"], "code_challenge": ["ch"],
        "code_challenge_method": ["SHA-256"], "state": ["st"],
        "scope": ["openid"], "redirect_uri": ["https://cloud.test/cb"],
        "response_type": ["code"],
    }


def test_build_switch_account_url_logs_out_first():
    url = events.build_switch_account_url(
        auth_base="https://auth.test", authorize_url="https://auth.test/a?b=1")
    assert url == "https://auth.test/authui/logout?service=https%3A%2F%2Fauth.test%2Fa%3Fb%3D1"

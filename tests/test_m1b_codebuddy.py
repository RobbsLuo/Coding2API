"""M1b 契约测试：CodeBuddy provider（fixture 驱动）。

fixture 从 codebuddy2api 的 tests/test_stream_service.py 提取（真实 SSE 结构）。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from src.config import Settings
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository
from src.engine.executor import Executor, ExecutorDeps, NoHealthyCredential
from src.engine.scheduler import Scheduler
from src.engine.sse import parse_frames
from src.main import build_app
from src.provider.base import ErrKind, Event, EventKind, Usage
from src.provider.codebuddy import events as cb_events
from src.provider.codebuddy.client import (
    DEFAULT_MODELS,
    CodeBuddyClient,
    CodeBuddyCredential,
    CodeBuddyProvider,
    UpstreamHTTPError,
    _cycle_end_epoch,
    build_headers,
    parse_credential,
)
from src.provider.codebuddy.events import UpstreamProtocolViolation
from src.provider.codebuddy.headers import QUOTA_RANGE_END, encode_department, host_of
from tests.conftest import SECRET

FIXTURES = Path(__file__).parent.parent / "src" / "provider" / "fixtures" / "codebuddy"


def _quota_body(accounts: object) -> dict:
    """包出上游真实的个人版额度响应结构（data.Response.Data.Accounts）。"""
    return {"code": 0, "msg": "OK",
            "data": {"Response": {"Data": {"Accounts": accounts},
                                  "RequestId": "<redacted>"}}}


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _clear_model_cache():
    """模块级模型缓存必须按测试清空，否则用例间互相污染。"""
    from src.provider.codebuddy.client import _MODEL_CACHE

    _MODEL_CACHE.clear()
    yield
    _MODEL_CACHE.clear()


def _client(handler) -> CodeBuddyClient:
    transport = httpx.MockTransport(handler)
    return CodeBuddyClient(
        stream_client=httpx.AsyncClient(transport=transport, timeout=None),
        short_client=httpx.AsyncClient(transport=transport, timeout=None))


# --------------------------------------------------------------- 请求头

def test_host_and_domain_derived_from_same_endpoint():
    headers = build_headers(CodeBuddyCredential(bearer_token="t"), "https://copilot.tencent.com")
    assert headers["Host"] == headers["X-Domain"] == "copilot.tencent.com"


def test_department_encoded_as_utf8_percent():
    headers = build_headers(
        CodeBuddyCredential(bearer_token="t", enterprise_id="e",
                            department_full_name="技术部/平台组"),
        "https://copilot.tencent.com")
    encoded = "%E6%8A%80%E6%9C%AF%E9%83%A8%2F%E5%B9%B3%E5%8F%B0%E7%BB%84"
    assert headers["X-Department-Info"] == encoded
    assert headers["X-Enterprise-Id"] == "e"


def test_account_uid_takes_priority_over_user_id():
    headers = build_headers(
        CodeBuddyCredential(bearer_token="t", user_id="user", account_uid="acct"),
        "https://copilot.tencent.com")
    assert headers["X-User-Id"] == "acct"


def test_quota_only_headers_drop_enterprise_context():
    """手动凭证的额度探测只切接口，不发企业上下文头（AGENTS.md 约束）。"""
    headers = build_headers(
        CodeBuddyCredential(bearer_token="t", enterprise_id="e",
                            department_full_name="技术部", user_id="u"),
        "https://copilot.tencent.com", quota_only=True)
    assert "X-Enterprise-Id" not in headers
    assert "X-Department-Info" not in headers
    assert "X-User-Id" not in headers


def test_host_of_and_encode_department_helpers():
    assert host_of("https://www.codebuddy.ai") == "www.codebuddy.ai"
    assert encode_department("") == ""


# --------------------------------------------------------------- 凭证解析

def test_parse_credential_bearer_only():
    credential = parse_credential({"token": "abc"})
    assert credential.bearer_token == "abc"
    assert credential.auth_source == "manual"
    assert credential.is_oauth is False


@pytest.mark.parametrize("raw", [
    b"{broken", b"[1,2]", b"{}", b'{"token":""}', b'{"token":"   "}',
])
def test_parse_credential_rejects_malformed(raw):
    with pytest.raises(UpstreamProtocolViolation):
        parse_credential(raw)


def test_parse_credential_accepts_all_token_key_names():
    for key in ("bearer_token", "access_token", "token"):
        assert parse_credential({key: "v"}).bearer_token == "v"


def test_auth_source_normalization():
    assert parse_credential({"token": "t", "auth_source": "oauth"}).auth_source == "oauth"
    assert parse_credential({"token": "t", "auth_source": "hacked"}).auth_source == "unknown"
    assert CodeBuddyCredential.from_dict({"auth_source": "manual"}).auth_source == "manual"


def test_quota_probe_mode_normalization():
    enterprise = CodeBuddyCredential.from_dict({"quota_probe_mode": "enterprise"})
    assert enterprise.quota_probe_mode == "enterprise"
    junk = CodeBuddyCredential.from_dict({"quota_probe_mode": "junk"})
    assert junk.quota_probe_mode == "personal"


def test_bearer_only_credential_never_needs_refresh():
    """手动 bearer-only 凭证不得进入 OAuth 刷新流程（AGENTS.md 约束）。"""
    manual = CodeBuddyCredential(bearer_token="t", refresh_token="r", expires_at=1)
    assert manual.needs_refresh(86400, now=10_000) is False
    oauth = CodeBuddyCredential(bearer_token="t", refresh_token="r", expires_at=100,
                                auth_source="oauth")
    assert oauth.needs_refresh(86400, now=10_000) is True
    assert oauth.needs_refresh(1, now=1) is False


def test_credential_dict_roundtrip():
    original = CodeBuddyCredential(bearer_token="t", user_id="u", account_uid="a",
                                   enterprise_id="e", auth_source="oauth",
                                   quota_probe_mode="enterprise", nickname="n")
    assert CodeBuddyCredential.from_dict(original.to_dict()) == original


# ------------------------------------------------------------- 事件映射

def test_basic_fixture_maps_to_events():
    events = [e for e in (cb_events.parse_frame(f)
                          for f in parse_frames(fixture("chat-basic.sse"))) if e]
    assert [e.kind for e in events] == [EventKind.CONTENT, EventKind.CONTENT, EventKind.USAGE]
    assert events[0].content == "你好"
    assert events[1].content == "，世界"
    assert events[2].usage.input_tokens == 11


def test_done_marker_and_empty_frame_yield_nothing():
    assert cb_events.parse_frame(cb_events.SSEFrame(event="", data="[DONE]")) is None
    assert cb_events.parse_frame(cb_events.SSEFrame(event="", data="")) is None


def test_parse_all_events_emits_usage_and_finish_together():
    """收尾帧同时带 finish_reason 与 usage → 两个事件都产出。"""
    frame = cb_events.SSEFrame(
        event="",
        data='{"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3}}')
    kinds = [e.kind for e in cb_events.parse_all_events(frame)]
    assert kinds == [EventKind.USAGE, EventKind.FINISH]


def test_parse_all_events_keeps_reasoning_then_content_priority():
    """reasoning 与 content 同帧时按 content 优先（单事件语义）。"""
    frame = cb_events.SSEFrame(
        event="", data='{"choices":[{"delta":{"reasoning_content":"r","content":"c"}}]}')
    events = cb_events.parse_all_events(frame)
    assert [e.kind for e in events] == [EventKind.CONTENT]


def test_reasoning_fixture_events():
    events = [e for e in (cb_events.parse_frame(f)
                          for f in parse_frames(fixture("chat-reasoning.sse"))) if e]
    assert [e.kind for e in events] == [EventKind.REASONING, EventKind.REASONING,
                                        EventKind.CONTENT]
    assert events[0].content == "我"


def test_tool_calls_fixture_keeps_upstream_ids():
    events = [e for e in (cb_events.parse_frame(f)
                          for f in parse_frames(fixture("tool-calls.sse"))) if e]
    assert all(e.kind is EventKind.TOOL_CALLS for e in events)
    assert events[0].tool_calls[0]["id"] == "call_1"
    assert "id" not in events[1].tool_calls[0]          # 分片不重生成 id


def test_tool_call_list_with_non_dict_entries_is_filtered():
    frame = cb_events.SSEFrame(
        event="", data='{"choices":[{"delta":{"tool_calls":[1,{"id":"ok",'
                       '"function":{"name":"f","arguments":"{}"}}]}}]}')
    assert cb_events.parse_frame(frame).tool_calls == [
        {"id": "ok", "function": {"name": "f", "arguments": "{}"}}]


def test_tool_call_without_name_is_dropped():
    """空名 tool_call（上游噪声）被丢弃，不转发给客户端。"""
    frame = cb_events.SSEFrame(
        event="", data='{"choices":[{"delta":{"tool_calls":'
                       '[{"id":"x","function":{"arguments":"{}"}}]}}]}')
    assert cb_events.parse_frame(frame) is None


@pytest.mark.parametrize("data", ["{broken", "[1,2]", '{"choices":"no"}',
                                  '{"choices":[5]}'])
def test_malformed_frames_raise(data):
    with pytest.raises(UpstreamProtocolViolation):
        cb_events.parse_frame(cb_events.SSEFrame(event="", data=data))


def test_choices_empty_array_is_tolerated():
    frame = cb_events.SSEFrame(event="", data='{"choices":[],"usage":{"prompt_tokens":3}}')
    assert cb_events.parse_frame(frame).usage.input_tokens == 3


def test_finish_only_frame():
    frame = cb_events.SSEFrame(
        event="", data='{"choices":[{"delta":{},"finish_reason":"length"}]}')
    assert cb_events.parse_frame(frame).finish_reason == "length"


def test_parse_credit_rate_malformed_returns_none():
    """credit_rate 解析：非数值/无数字等坏格式走异常路径返回 None。"""
    from src.provider.codebuddy.client import _parse_credit_rate

    assert _parse_credit_rate("x") is None            # IndexError
    assert _parse_credit_rate("xabc") is None         # ValueError
    assert _parse_credit_rate("x0.25 credits") == 0.25


def test_usage_credit_is_optional():
    frame = cb_events.SSEFrame(event="", data='{"usage":{"credit":0.25,"prompt_tokens":1}}')
    usage = cb_events.parse_frame(frame).usage
    assert usage.credit == 0.25


def test_usage_cached_tokens_from_details_and_top_level():
    """缓存命中：优先 prompt_tokens_details.cached_tokens，顶层 cached_tokens 兜底。"""
    frame = cb_events.SSEFrame(event="", data=(
        '{"usage":{"prompt_tokens":10,"prompt_tokens_details":{"cached_tokens":7}}}'))
    usage = cb_events.parse_frame(frame).usage
    assert usage.cached_tokens == 7

    frame = cb_events.SSEFrame(event="", data='{"usage":{"prompt_tokens":10,"cached_tokens":4}}')
    assert cb_events.parse_frame(frame).usage.cached_tokens == 4

    frame = cb_events.SSEFrame(event="", data='{"usage":{"prompt_tokens":10}}')
    assert cb_events.parse_frame(frame).usage.cached_tokens is None


def test_usage_ignores_boolean_and_non_numeric_values():
    frame = cb_events.SSEFrame(
        event="", data='{"usage":{"prompt_tokens":true,"credit":true,"completion_tokens":"x"}}')
    usage = cb_events.parse_frame(frame).usage
    assert usage.input_tokens is None and usage.output_tokens is None and usage.credit is None


# ----------------------------------------------------------- 错误分类

@pytest.mark.parametrize(("status", "expected"), [
    (401, ErrKind.DEAD), (403, ErrKind.DEAD), (404, ErrKind.SOFT), (429, ErrKind.SOFT),
    (500, ErrKind.OTHER), (400, ErrKind.INVALID), (200, ErrKind.OTHER),
    (402, ErrKind.CREDIT),
])
def test_classify_status(status, expected):
    assert cb_events.classify_status(status) is expected


def test_classify_body_markers():
    assert cb_events.classify_status(400, fixture("error-1005.json").encode()) is ErrKind.PLAN
    assert cb_events.classify_status(400, b'{"code": 1005, "plan": "x"}') is ErrKind.PLAN
    assert cb_events.classify_status(401, fixture("error-401.json").encode()) is ErrKind.DEAD
    assert cb_events.classify_error_code(1005) is ErrKind.PLAN
    assert cb_events.classify_error_code(500) is ErrKind.OTHER


@pytest.mark.parametrize(("status", "body", "expected"), [
    # 余额不足：402 或 body 里的 14018（credits exhausted）
    (402, b'{"code": 14018, "msg": "credits exhausted"}', ErrKind.CREDIT),
    (429, b'{"code": 14018}', ErrKind.CREDIT),
    # 权益耗尽（1005）与余额不足是两种语义：前者 12h，后者等签到
    (400, b'{"code": 1005}', ErrKind.PLAN),
    # 模型级限流：429 + 6004 只冷却触发模型，不是账号级 SOFT
    (429, b'{"code": 6004, "msg": "model quota exceeded"}', ErrKind.MODEL),
    # 「该后端无此模型」：(账号, 模型) 负缓存
    (400, b'{"code": 11102}', ErrKind.BLOCKED),
    (404, b'{"code": 11102}', ErrKind.BLOCKED),
    # 请求级错误：不罚号，仍换号（请求体坏 / 上下文超限 / 渠道风控 / 图片无效）
    (400, b'{"code": 11101}', ErrKind.REQUEST),
    (400, b'{"code": 500, "msg": "Unmarshal chat params failed"}', ErrKind.REQUEST),
    (400, b'{"code": 11115, "msg": "prompt is too long"}', ErrKind.REQUEST),
    # 渠道风控（瞬时，窗口内自愈）：曾误落 INVALID → 零重试直接 400，实测误伤
    (400, b'{"code": 11128, "msg": "Illegal API invocation from an unapproved channel"}',
     ErrKind.REQUEST),
    (400, b'{"code": 11135, "msg": "Invalid image data"}', ErrKind.REQUEST),
    # 裸数字不误伤：111020 不是 11102（要求带 "code": 键值形态）
    (400, b'{"trace": 1110201}', ErrKind.INVALID),
    (400, b'{"code": 111020}', ErrKind.INVALID),
    # 嵌套信封：业务码在内层，按集合成员判断不漏
    (400, b'{"code": 0, "data": {"code": 11102}}', ErrKind.BLOCKED),
    (404, b"not json at all", ErrKind.SOFT),
    (500, b'{"code": 9999}', ErrKind.OTHER),
])
def test_classify_status_business_codes(status, body, expected):
    assert cb_events.classify_status(status, body) is expected


@pytest.mark.parametrize(("code", "expected"), [
    (14018, ErrKind.CREDIT), (6004, ErrKind.MODEL), (11102, ErrKind.BLOCKED),
    (11101, ErrKind.REQUEST), (11115, ErrKind.REQUEST), (11128, ErrKind.REQUEST),
    (11135, ErrKind.REQUEST),
    (None, ErrKind.OTHER),
])
def test_classify_error_code_business_codes(code, expected):
    assert cb_events.classify_error_code(code) is expected


# ------------------------------------------------------------- 客户端

async def test_stream_chat_yields_events_from_fixture():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/chat/completions"
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    events = [e async for e in _client(handler).stream_chat(
        CodeBuddyCredential(bearer_token="t"), {"messages": []}, "glm-5.2")]
    # 收尾帧同时带 finish_reason 与 usage → 两个事件都产出
    assert [e.kind for e in events] == [EventKind.CONTENT, EventKind.CONTENT,
                                        EventKind.USAGE, EventKind.FINISH]


async def test_stream_chat_raises_classified_http_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b"rate limited")

    with pytest.raises(UpstreamHTTPError) as caught:
        [e async for e in _client(handler).stream_chat(
            CodeBuddyCredential(bearer_token="t"), {}, "m")]
    assert caught.value.kind() is ErrKind.SOFT


async def test_stream_chat_forces_stream_true_even_if_client_sent_false():
    """上游只有流式；客户端 stream=false 也必须强制 true（AGENTS.md 约束）。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    [e async for e in _client(handler).stream_chat(
        CodeBuddyCredential(bearer_token="t"), {"stream": False}, "glm-5.2")]
    assert seen["stream"] is True and seen["model"] == "glm-5.2"


async def test_fetch_personal_quota_parses_real_nested_response():
    """真实响应是 data.Response.Data.Accounts，且 *Precise 是字符串。

    用抓取的真实结构做契约测试：层级读错或忽略字符串都会让有效凭证
    被误判成「未探测到额度」。
    """
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=fixture("quota-personal.json"))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    # fixture 是脱敏后的真实响应：两个 Status=0 的套餐（累加 5500 / 5267.5），
    # 但 TotalDosage 是脱敏前的官方汇总值 9070——优先取官方口径，不逐包累加。
    assert quota.total == 5500.0
    assert quota.remaining == 9070.0
    assert quota.cycle_end is not None
    assert quota.probe_failed is False


@pytest.mark.parametrize(("body", "expected"), [
    ({"data": None}, None),                                # data 非 dict
    ({"data": {"Response": {"Data": {"TotalDosage": 9070}}}}, 9070.0),
    ({"data": {"Response": {"Data": {"TotalDosage": "3344.5"}}}}, 3344.5),
    ({"data": {"Response": {"Data": {}}}}, None),         # 缺失 → 回退累加
    ({"data": {"Response": {"Data": {"TotalDosage": "x"}}}}, None),  # 非数字
])
def test_total_dosage_extracts_official_remaining(body, expected):
    from src.provider.codebuddy.client import _total_dosage

    assert _total_dosage(body) == expected


async def test_fetch_personal_quota_prefers_precise_over_plain():
    """Precise 优先，且能解析字符串；被禁用的套餐（Status!=0）要跳过。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        accounts = [
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "60", "CycleCapacitySize": 999,
             "CycleEndTime": "2026-12-31 23:59:59"},
            {"Status": 0, "CycleCapacitySize": 50, "CycleCapacityRemain": 10},
            {"Status": 1, "CycleCapacitySizePrecise": "777",
             "CycleCapacityRemainPrecise": "777"},
            "junk",
        ]
        return httpx.Response(200, json=_quota_body(accounts))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert quota.total == 150.0      # 100(Precise 字符串) + 50(回退非 Precise)
    assert quota.remaining == 70.0
    assert quota.cycle_end is not None


def _days(offset: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + offset * 3600))


async def test_fetch_personal_quota_cycle_end_is_earliest_not_expired():
    """账号常有几十个套餐各自独立到期（每日 100 积分 × N），cycle_end 取最早的那个。

    上游会把已过期套餐一起返回（EndTimeRangeBegin 过滤的是套餐有效期，不是积分周期），
    直接取最小值会得到过去的时间，调度器的到期偏好就永远不触发。
    """
    def handler(_request: httpx.Request) -> httpx.Response:
        accounts = [
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "100", "CycleEndTime": _days(400)},
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "100", "CycleEndTime": _days(2)},
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "64", "CycleEndTime": _days(-1)},
        ]
        return httpx.Response(200, json=_quota_body(accounts))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert quota.cycle_end == _cycle_end_epoch(_days(2))
    assert quota.cycle_end < _cycle_end_epoch(_days(400))
    assert quota.cycle_end > time.time()          # 已过期套餐不参与
    assert quota.total == 300.0
    # 到期阶梯保留套餐顺序，只收未过期的包
    assert quota.expiry_ladder == [(_cycle_end_epoch(_days(400)), 100.0),
                                   (_cycle_end_epoch(_days(2)), 100.0)]


async def test_fetch_personal_quota_expiry_ladder_filters_packages():
    """到期阶梯只收「未过期 + 有余额」的包：状态异常、零容量、已用完、
    时间格式不合法的套餐都排除；total/remaining 仍按原口径累加。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        accounts = [
            {"Status": 1, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "100", "CycleEndTime": _days(1)},
            {"Status": 0, "CycleCapacitySizePrecise": "0",
             "CycleCapacityRemainPrecise": "0", "CycleEndTime": _days(1)},
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "0", "CycleEndTime": _days(1)},
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "100", "CycleEndTime": "not-a-date"},
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "40", "CycleEndTime": _days(3)},
        ]
        return httpx.Response(200, json=_quota_body(accounts))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert quota.expiry_ladder == [(_cycle_end_epoch(_days(3)), 40.0)]
    assert quota.total == 300.0
    assert quota.remaining == 140.0
    assert quota.cycle_end == _cycle_end_epoch(_days(3))


async def test_fetch_personal_quota_packages_include_names_and_usage():
    """展示明细比调度阶梯宽：已用完但仍有效的包要显示（管理员想知道哪包空了），
    但「已过期且已用完」的历史残留不堆进列表。与 ladder 互不影响。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        accounts = [
            # 未过期 + 有余额 → 进明细，也进阶梯
            {"Status": 0, "PackageName": "CodeBuddy个人体验版",
             "CycleCapacitySizePrecise": "500", "CycleCapacityRemainPrecise": "100",
             "CycleEndTime": _days(2)},
            # 未过期但已用完 → 只进明细（展示“已用完”），不进阶梯
            {"Status": 0, "PackageName": "已用完的包",
             "CycleCapacitySizePrecise": "100", "CycleCapacityRemainPrecise": "0",
             "CycleEndTime": _days(3)},
            # 已过期但还有余额 → 只进明细（展示“浪费了”），不进阶梯
            {"Status": 0, "PackageName": "已过期有余额",
             "CycleCapacitySizePrecise": "100", "CycleCapacityRemainPrecise": "50",
             "CycleEndTime": _days(-1)},
            # 已过期 + 已用完 → 两边都不进（历史残留）
            {"Status": 0, "PackageName": "历史残留",
             "CycleCapacitySizePrecise": "100", "CycleCapacityRemainPrecise": "0",
             "CycleEndTime": _days(-2)},
            # 无到期时间但还有余额 → 进明细，end 为 None
            {"Status": 0, "PackageName": "无到期",
             "CycleCapacitySizePrecise": "100", "CycleCapacityRemainPrecise": "10"},
        ]
        return httpx.Response(200, json=_quota_body(accounts))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert [item["name"] for item in quota.packages] == [
        "CodeBuddy个人体验版", "已用完的包", "已过期有余额", "无到期"]
    assert quota.packages[0] == {"name": "CodeBuddy个人体验版", "total": 500.0,
                                 "used": 400.0, "end": _cycle_end_epoch(_days(2))}
    assert quota.packages[1]["used"] == 100.0      # 已用完
    assert quota.packages[3]["end"] is None        # 无到期信息
    # 调度阶梯不受展示口径影响：已过期的包不进阶梯（只收未过期 + 有余额）
    assert quota.expiry_ladder == [(_cycle_end_epoch(_days(2)), 100.0)]


async def test_fetch_personal_quota_cycle_end_none_when_all_packages_expired():
    """套餐全部过期（积分已作废）时不给出过去的到期点。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        accounts = [
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "100", "CycleEndTime": _days(-2)},
            {"Status": 0, "CycleCapacitySizePrecise": "100",
             "CycleCapacityRemainPrecise": "100", "CycleEndTime": _days(-48)},
        ]
        return httpx.Response(200, json=_quota_body(accounts))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert quota.cycle_end is None
    assert quota.total == 200.0
    assert quota.expiry_ladder == []


@pytest.mark.parametrize("accounts", [None, []])
async def test_fetch_quota_accepts_null_accounts_as_no_personal_quota(accounts):
    """个人版 Accounts: null / [] 表示探测成功但没有额度（AGENTS.md 约束）。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_quota_body(accounts))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert quota.total == 0 and quota.remaining == 0
    assert quota.probe_failed is False


async def test_fetch_personal_quota_omits_product_code():
    """回归：探测请求不带 ProductCode。

    上游对部分账号已拒绝 "codebuddy" 产品码（InvalidParameterValue:
    productCode:param format error），缺省时才返回全部套餐。
    """
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.read()))
        return httpx.Response(200, json=_quota_body([]))

    await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert bodies == [{"PageNumber": 1, "PageSize": 200, "Status": [0, 3],
                       "PackageEndTimeRangeBegin": bodies[0]["PackageEndTimeRangeBegin"],
                       "PackageEndTimeRangeEnd": QUOTA_RANGE_END}]


@pytest.mark.parametrize("message", ["[productCode:param format error] ", None])
async def test_fetch_quota_response_error_is_probe_failure(message):
    """上游业务层错误（Response.Error）必须抛探测失败，不能当成 0 额度
    把还有积分的凭证误标成「已耗尽」。"""
    error = {"Code": "InvalidParameterValue", "Message": message}
    body = {"code": 0, "msg": "OK",
            "data": {"Response": {"Data": {"Accounts": None,
                                          "TotalDosage": 0},
                                  "Error": error}}}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(UpstreamProtocolViolation, match="InvalidParameterValue"):
        await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))


@pytest.mark.parametrize("body", [
    {},                                     # 完全没有 data
    {"data": None},
    {"data": {}},                           # data 里没有 Accounts
    {"data": {"Response": {}}},
    {"data": {"Response": {"Data": {}}}},
    {"data": {"Accounts": "no"}},           # 类型错误
])
async def test_fetch_quota_missing_accounts_fails(body):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(UpstreamProtocolViolation):
        await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))


async def test_fetch_enterprise_quota_requires_oauth():
    """企业额度接口只对 OAuth 凭证开放；手动凭证走个人版。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("get-enterprise-user-usage"):
            return httpx.Response(200, json={"credit": 30, "limitNum": 100})
        return httpx.Response(200, json=_quota_body([]))

    oauth = CodeBuddyCredential(bearer_token="t", auth_source="oauth",
                                enterprise_id="e", quota_probe_mode="enterprise")
    quota = await _client(handler).fetch_quota(oauth)
    assert quota.total == 100 and quota.remaining == 70
    assert calls[-1].endswith("get-enterprise-user-usage")

    manual = CodeBuddyCredential(bearer_token="t", quota_probe_mode="enterprise")
    await _client(handler).fetch_quota(manual)
    assert calls[-1].endswith("get-user-resource")


async def test_fetch_enterprise_quota_rejects_missing_limit():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"credit": 5})

    oauth = CodeBuddyCredential(bearer_token="t", auth_source="oauth",
                                quota_probe_mode="enterprise")
    with pytest.raises(UpstreamProtocolViolation):
        await _client(handler).fetch_quota(oauth)


async def test_post_json_error_paths():
    def failing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    with pytest.raises(UpstreamHTTPError):
        await _client(failing).fetch_quota(CodeBuddyCredential(bearer_token="t"))

    def not_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>")

    with pytest.raises(UpstreamProtocolViolation):
        await _client(not_json).fetch_quota(CodeBuddyCredential(bearer_token="t"))

    def not_object(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1])

    with pytest.raises(UpstreamProtocolViolation):
        await _client(not_object).fetch_quota(CodeBuddyCredential(bearer_token="t"))


async def test_client_lazy_properties_and_close():
    client = CodeBuddyClient()
    assert client._stream is client._stream
    assert client._short is client._short
    await client.aclose()
    await client.aclose()


def test_provider_import_classify_and_models():
    provider = CodeBuddyProvider()
    data = provider.import_credential({"token": "abc"})
    assert data["bearer_token"] == "abc" and data["auth_source"] == "manual"
    assert provider.classify(429, b"") is ErrKind.SOFT
    assert provider.host() == "copilot.tencent.com"
    with pytest.raises(UpstreamProtocolViolation):
        provider.import_credential({})


async def test_provider_stream_and_probe_delegate():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    provider = CodeBuddyProvider(client=_client(handler))
    events = [e async for e in provider.stream_chat({"bearer_token": "t"}, {}, "m")]
    assert events[-1].kind is EventKind.FINISH
    assert any(e.kind is EventKind.USAGE for e in events)


# ------------------------------------------------ 双 provider 端到端（Q25=A）

@pytest.fixture()
def dual_repo(tmp_path):
    db = Database(tmp_path / "dual.sqlite3")
    apply_schema(db.connect())
    yield CredentialRepository(db, CredentialCipher(SECRET)), db
    db.close()


class DualProvider:
    """同一脚本双 provider：验证调度层对两个上游行为一致。"""

    def __init__(self, provider_id: str, script) -> None:
        self.id = provider_id
        self.script = script
        self.calls = 0

    async def stream_chat(self, _credential_data, _payload, _model):
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for item in self.script[index]:
            if isinstance(item, Exception):
                raise item
            yield item

    def list_models(self, _credential_data):
        return []


GOOD = [Event(kind=EventKind.CONTENT, content="ok"),
        Event(kind=EventKind.FINISH, finish_reason="stop")]


def _dual_executor(credentials, trae_script, cb_script, **kw):
    trae = DualProvider("trae", trae_script)
    codebuddy = DualProvider("codebuddy", cb_script)
    executor = Executor(ExecutorDeps(
        providers={"trae": trae, "codebuddy": codebuddy}, credentials=credentials,
        scheduler=Scheduler(**kw), default_model="glm-5.2"))
    return executor, trae, codebuddy


def _request(model="glm-5.2"):
    from src.compat.openai.request import parse_chat_request

    return parse_chat_request({"messages": [{"role": "user", "content": "hi"}],
                               "model": model})


class Boom(Exception):
    def __init__(self, kind: ErrKind, message: str = "upstream error") -> None:
        super().__init__(message)
        self._kind = kind

    def kind(self) -> ErrKind:
        return self._kind


# ------------------------------------------------- 截断续写装配（B1.4）

@pytest.mark.asyncio
async def test_executor_auto_continues_on_length_same_credential(dual_repo):
    """finish_reason=length → 同凭证续写；executor 只看到一条更长的事件流。"""
    credentials, db = dual_repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    truncated = [Event(kind=EventKind.CONTENT, content="part1"),
                 Event(kind=EventKind.USAGE, usage=Usage(input_tokens=7,
                                                         output_tokens=3)),
                 Event(kind=EventKind.FINISH, finish_reason="length")]
    completed = [Event(kind=EventKind.CONTENT, content="part2"),
                 Event(kind=EventKind.USAGE, usage=Usage(input_tokens=2,
                                                         output_tokens=4)),
                 Event(kind=EventKind.FINISH, finish_reason="stop")]
    provider = DualProvider("codebuddy", [truncated, completed])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": provider}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2", max_auto_continues=10))
    result = await executor.complete(_request("glm-5.2"))
    assert result["choices"][0]["message"]["content"] == "part1part2"
    assert result["choices"][0]["finish_reason"] == "stop"
    assert provider.calls == 2                       # 同凭证两次请求
    # 累计用量：input 7+2、output 3+4
    assert result["usage"]["prompt_tokens"] == 9
    assert result["usage"]["completion_tokens"] == 7
    # 只用一个凭证，未发生轮换
    assert len(credentials.candidates(["codebuddy"])) == 1
    db.close()


@pytest.mark.asyncio
async def test_executor_auto_continue_disabled(dual_repo):
    """max_auto_continues=0（默认）：length 原样透传，不续写。"""
    credentials, db = dual_repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    truncated = [Event(kind=EventKind.CONTENT, content="part1"),
                 Event(kind=EventKind.FINISH, finish_reason="length")]
    provider = DualProvider("codebuddy", [truncated])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": provider}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2"))
    result = await executor.complete(_request("glm-5.2"))
    assert result["choices"][0]["message"]["content"] == "part1"
    assert result["choices"][0]["finish_reason"] == "length"
    assert provider.calls == 1
    db.close()


@pytest.mark.asyncio
async def test_executor_stream_auto_continue_emits_single_finish(dual_repo):
    """流式续写：客户端只看到一条流、一个 finish_reason 与一条 [DONE]。"""
    credentials, db = dual_repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    truncated = [Event(kind=EventKind.CONTENT, content="a"),
                 Event(kind=EventKind.FINISH, finish_reason="length")]
    completed = [Event(kind=EventKind.CONTENT, content="b"),
                 Event(kind=EventKind.FINISH, finish_reason="stop")]
    provider = DualProvider("codebuddy", [truncated, completed])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": provider}, credentials=credentials,
        scheduler=Scheduler(), default_model="glm-5.2", max_auto_continues=10))
    frames = [f async for f in executor.stream(_request("glm-5.2"), username="u")]
    joined = b"".join(frames).decode()
    assert joined.count("data: [DONE]") == 1
    assert '"finish_reason": "stop"' in joined
    assert '"finish_reason": "length"' not in joined
    assert provider.calls == 2
    db.close()


async def test_dual_provider_same_model_routes_and_fails_over(dual_repo):
    """同一扁平模型名下，一个上游失败 → 自动落到另一个（Q21=C 的核心价值）。

    用 pin 固定首个上游，避免依赖同分时的字母序挑选。
    """
    credentials, db = dual_repo
    trae_id = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    credentials.set_pinned(trae_id)          # 固定先走 trae
    executor, trae, codebuddy = _dual_executor(
        credentials, [[Boom(ErrKind.SOFT)]], [GOOD])
    result = await executor.complete(_request())
    assert result["choices"][0]["message"]["content"] == "ok"
    assert trae.calls == 1 and codebuddy.calls == 1
    assert credentials.candidates(["trae"])[0].cooling_until is not None
    assert credentials.candidates(["codebuddy"])[0].cooling_until is None
    db.close()


async def test_dual_provider_forced_suffix_targets_single_upstream(dual_repo):
    """model@codebuddy 强制只走 codebuddy，不落到 trae。"""
    credentials, db = dual_repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    executor, trae, codebuddy = _dual_executor(credentials, [GOOD], [GOOD])
    await executor.complete(_request("glm-5.2@codebuddy"))
    assert codebuddy.calls == 1 and trae.calls == 0
    db.close()


async def test_dual_provider_error_classification_is_consistent(dual_repo):
    """两个 provider 的错误分类都落到同一冷却语义（Q25=A 验证点）。"""
    credentials, db = dual_repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    executor, _trae, _codebuddy = _dual_executor(
        credentials, [[Boom(ErrKind.PLAN)]], [[Boom(ErrKind.PLAN)]])
    with pytest.raises(NoHealthyCredential):
        await executor.complete(_request())
    for candidate in credentials.candidates():
        assert candidate.cooling_until is not None, candidate
    db.close()


async def test_dual_provider_no_credential_when_both_registered(dual_repo):
    credentials, db = dual_repo
    executor, _trae, _codebuddy = _dual_executor(credentials, [GOOD], [GOOD])
    with pytest.raises(NoHealthyCredential):
        await executor.complete(_request())
    db.close()


# ------------------------------------------------------------------- API

def test_default_registry_contains_both_providers(tmp_path):
    """双上游注册后 /v1/models 与 playground 模型一致，动态拉取失败回退静态表。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class FailingCB:
        id = "codebuddy"

        def list_models(self, _data):
            raise RuntimeError("dynamic fetch unavailable")

        def import_credential(self, raw):
            return raw

    class TraeStub:
        id = "trae"

        async def list_models(self, _data):
            from src.provider.base import Model

            return [Model(id="glm-5.2")]

        def import_credential(self, raw):  # pragma: no cover
            return raw

    app = build_app(settings, providers={
        "trae": TraeStub(),
        "codebuddy": FailingCB(),
    })
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = client.get("/v1/models",
                          headers={"Authorization": f"Bearer {key}"}).json()["data"]
    by_id = {item["id"]: item["providers"] for item in data}
    # CB 的 list_models 抛错 → 该上游被跳过（不拖垮整个列表），TRAE 正常返回
    assert by_id["glm-5.2"] == ["trae"]


def test_models_cache_used_when_fetch_fails(tmp_path):
    """拉取成功后写入缓存；之后失败时用缓存兜底，/v1/models 保持完整。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class FailingCB:
        id = "codebuddy"

        def list_models(self, _data):
            raise RuntimeError("dynamic fetch unavailable")

        def import_credential(self, raw):
            return raw

    class FlakyTrae:
        id = "trae"
        fail = False

        async def list_models(self, _data):
            from src.provider.base import Model

            if self.fail:
                raise RuntimeError("upstream down")
            return [Model(id="glm-5.2"), Model(id="DeepSeek-V4-Flash")]

        def import_credential(self, raw):
            return raw

    trae = FlakyTrae()
    app = build_app(settings, providers={"trae": trae, "codebuddy": FailingCB()})
    key = app.state.api_keys.create("root")["api_key"]
    auth = {"Authorization": f"Bearer {key}"}
    with TestClient(app) as client:
        # 第一次：拉取成功，缓存写入
        first = client.get("/v1/models", headers=auth).json()["data"]
        assert {item["id"] for item in first} == {"glm-5.2", "DeepSeek-V4-Flash"}
        cached_keys = app.state.services.model_list_cache["trae"].keys()
        assert cached_keys == {"glm-5.2", "deepseek-v4-flash"}

        # 第二次：拉取失败 → 用缓存兜底，列表不缺模型
        trae.fail = True
        second = client.get("/v1/models", headers=auth).json()["data"]
        assert {item["id"] for item in second} == {"glm-5.2", "DeepSeek-V4-Flash"}
        assert {item["id"] for item in second} and all(
            item["providers"] == ["trae"] for item in second)

        # 恢复后重新拉取成功，缓存刷新
        trae.fail = False
        third = client.get("/v1/models", headers=auth).json()["data"]
        assert {item["id"] for item in third} == {"glm-5.2", "DeepSeek-V4-Flash"}


def test_models_blocklist_filters_noise_and_old(tmp_path):
    """默认黑名单滤非用户模型（custom_model_*/subagent/summary/file_search_agent/
    default/hunyuan-image-*），MODEL_BLOCKLIST 覆盖后可再滤老模型；
    直连指定不受列表过滤影响。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class NoisyProvider:
        id = "trae"

        async def list_models(self, _data):
            from src.provider.base import Model

            return [Model(id="glm-5.2"), Model(id="kimi-k2.6"),
                    Model(id="custom_model_claude"),
                    Model(id="explore_sub_agent_v13"), Model(id="summary"),
                    Model(id="file_search_agent"), Model(id="default"),
                    Model(id="hunyuan-image-alpha")]

        def import_credential(self, raw):
            return raw

    app = build_app(settings, providers={"trae": NoisyProvider()})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        ids = {item["id"] for item in client.get(
            "/v1/models", headers={"Authorization": f"Bearer {key}"}).json()["data"]}
    # 默认黑名单：噪音全滤（实测内部/不可用模型），老模型默认保留（是否滤由配置决定）
    assert "custom_model_claude" not in ids
    assert "explore_sub_agent_v13" not in ids
    assert "summary" not in ids
    assert "file_search_agent" not in ids
    assert "default" not in ids
    assert "hunyuan-image-alpha" not in ids
    assert {"glm-5.2", "kimi-k2.6"} <= ids

    # 覆盖黑名单：额外滤老模型（完全替换语义，需重写噪音规则）
    settings2 = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                         MODEL_BLOCKLIST="custom_model_*,*sub*agent*,summary,kimi-k2.6")
    app2 = build_app(settings2, providers={"trae": NoisyProvider()})
    key2 = app2.state.api_keys.create("root")["api_key"]
    with TestClient(app2) as client:
        ids2 = {item["id"] for item in client.get(
            "/v1/models", headers={"Authorization": f"Bearer {key2}"}).json()["data"]}
    assert "kimi-k2.6" not in ids2 and "glm-5.2" in ids2

    # 覆盖黑名单：额外滤老模型（完全替换语义，需重写噪音规则）
    settings2 = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                         MODEL_BLOCKLIST="custom_model_*,*sub*agent*,summary,kimi-k2.6")
    app2 = build_app(settings2, providers={"trae": NoisyProvider()})
    key2 = app2.state.api_keys.create("root")["api_key"]
    with TestClient(app2) as client:
        ids2 = {item["id"] for item in client.get(
            "/v1/models", headers={"Authorization": f"Bearer {key2}"}).json()["data"]}
    assert "kimi-k2.6" not in ids2 and "glm-5.2" in ids2


def test_models_by_provider_rates_when_dual_upstream(tmp_path):
    """双上游同名模型倍率不同时，响应带 by_provider 按渠道给倍率。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Stub:
        def __init__(self, pid: str, rate: float):
            self.id = pid
            self._rate = rate

        async def list_models(self, _data):
            from src.provider.base import Model

            return [Model(id="glm-5.2", credit_rate=self._rate)]

        def import_credential(self, raw):
            return raw

    app = build_app(settings, providers={
        "codebuddy": Stub("codebuddy", 0.29), "trae": Stub("trae", 0.17)})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        item = next(m for m in client.get("/v1/models", headers={
            "Authorization": f"Bearer {key}"}).json()["data"]
            if m["id"] == "glm-5.2")
    assert item["providers"] == ["codebuddy", "trae"]
    assert item["credit_rate"] == 0.29          # 合并值：先到先填
    assert item["by_provider"] == {"codebuddy": {"credit_rate": 0.29},
                                   "trae": {"credit_rate": 0.17}}


def test_models_passthrough_reasoning_metadata(tmp_path):
    """B1.6：supports_reasoning / default_effort 随条目透传，且逐字段补缺。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return self._models

        def import_credential(self, raw):
            return raw

    from src.provider.base import Model

    app = build_app(settings, providers={
        # trae 只给能力标志，codebuddy 补上默认档位（先到先填、后到补 None）
        "trae": Stub("trae", [Model(id="glm-5.3", supports_reasoning=True)]),
        "codebuddy": Stub("codebuddy", [Model(id="glm-5.3", supports_reasoning=True,
                                              default_effort="medium")]),
    })
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        item = next(m for m in client.get("/v1/models", headers={
            "Authorization": f"Bearer {key}"}).json()["data"]
            if m["id"] == "glm-5.3")
    assert item["supports_reasoning"] is True
    assert item["default_effort"] == "medium"


def test_codebuddy_import_via_api(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    from src.auth.session import create_session_token

    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        created = client.post("/api/credentials", json={
            "provider": "codebuddy", "credential": {"token": "abc"}})
        assert created.status_code == 200
        listed = client.get("/api/credentials").json()["credentials"]
        assert listed[0]["provider"] == "codebuddy"
        bad = client.post("/api/credentials", json={"provider": "codebuddy",
                                                    "credential": {}})
        assert bad.status_code == 400


# ---------------------------------------------------- 覆盖率收尾

async def test_fetch_quota_skips_packages_with_zero_total():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_quota_body([
            {"Status": 0, "CycleCapacitySizePrecise": "0", "CycleCapacityRemainPrecise": "5"},
            {"Status": 0, "CycleCapacitySizePrecise": "10",
             "CycleCapacityRemainPrecise": "10"},
        ]))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert quota.total == 10 and quota.remaining == 10


async def test_fetch_quota_tolerates_bad_cycle_end_format():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_quota_body([
            {"Status": 0, "CycleCapacitySizePrecise": "5",
             "CycleCapacityRemainPrecise": "5", "CycleEndTime": "not-a-date"},
        ]))

    quota = await _client(handler).fetch_quota(CodeBuddyCredential(bearer_token="t"))
    assert quota.cycle_end is None


def test_cycle_end_accepts_iso_separator():
    from src.provider.codebuddy.client import _cycle_end_epoch

    assert _cycle_end_epoch("2026-12-31T23:59:59") is not None
    assert _cycle_end_epoch(None) is None
    assert _cycle_end_epoch("") is None


async def test_provider_probe_quota_delegates():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=fixture("quota-personal.json"))

    provider = CodeBuddyProvider(client=_client(handler))
    quota = await provider.probe_quota({"bearer_token": "t"})
    assert quota.total == 5500.0


def test_auth_start_headers_mark_anonymous():
    from src.provider.codebuddy.headers import auth_start_headers

    headers = auth_start_headers("copilot.tencent.com")
    assert headers["X-No-Authorization"] == "true"
    assert "Authorization" not in headers


def test_parse_all_events_returns_empty_for_done_and_blank():
    assert cb_events.parse_all_events(cb_events.SSEFrame(event="", data="[DONE]")) == []
    assert cb_events.parse_all_events(cb_events.SSEFrame(event="", data="")) == []


@pytest.mark.parametrize("data", ["{broken", "[1,2]"])
def test_parse_all_events_raises_on_malformed(data):
    with pytest.raises(UpstreamProtocolViolation):
        cb_events.parse_all_events(cb_events.SSEFrame(event="", data=data))


def test_parse_all_events_with_tool_calls_does_not_duplicate_finish():
    """带 tool_calls 的收尾帧：工具事件 + finish，且 usage 不重复。"""
    frame = cb_events.SSEFrame(event="", data=(
        '{"choices":[{"delta":{"tool_calls":[{"id":"c","function":'
        '{"name":"f","arguments":"{}"}}]},"finish_reason":"tool_calls"}],'
        '"usage":{"prompt_tokens":2}}'))
    kinds = [e.kind for e in cb_events.parse_all_events(frame)]
    assert kinds == [EventKind.TOOL_CALLS, EventKind.USAGE, EventKind.FINISH]


def test_tool_calls_list_of_only_non_dicts_falls_through():
    """tool_calls 全是非 dict → 不算工具事件，继续走 content（events 42→45）。"""
    frame = cb_events.SSEFrame(
        event="", data='{"choices":[{"delta":{"tool_calls":[1,2],"content":"c"}}]}')
    assert cb_events.parse_frame(frame).kind is EventKind.CONTENT


def test_content_non_string_falls_through_to_reasoning():
    """content 非字符串 → 继续看 reasoning（events 46→下一分支）。"""
    frame = cb_events.SSEFrame(
        event="", data='{"choices":[{"delta":{"content":5,"reasoning_content":"r"}}]}')
    assert cb_events.parse_frame(frame).kind is EventKind.REASONING


def test_reasoning_non_string_is_ignored():
    frame = cb_events.SSEFrame(
        event="", data='{"choices":[{"delta":{"reasoning_content":5}}]}')
    assert cb_events.parse_frame(frame) is None


def test_finish_reason_non_string_is_ignored():
    frame = cb_events.SSEFrame(
        event="", data='{"choices":[{"delta":{},"finish_reason":5}]}')
    assert cb_events.parse_frame(frame) is None


def test_choices_missing_returns_none_choice():
    """没有 choices 键 → 不报错，按无 choice 处理。"""
    frame = cb_events.SSEFrame(event="", data='{"usage":{"prompt_tokens":1}}')
    assert cb_events.parse_frame(frame).kind is EventKind.USAGE


async def test_aclose_when_only_short_client_was_created():
    """只创建 short 客户端时，aclose 跳过 stream（client 159→158）。"""
    client = CodeBuddyClient()
    assert client._short is not None    # 只实例化 short
    await client.aclose()


# --------------------------------------------- _number 的字符串解析

@pytest.mark.parametrize(("value", "expected"), [
    (500, 500.0),
    (500.5, 500.5),
    ("500", 500.0),          # 上游 Precise 字段是字符串
    (" 4767.50000158 ", 4767.50000158),
    ("", None),              # 空字符串
    ("   ", None),
    ("not-a-number", None),  # 无法解析
    (None, None),
    ([], None),
    (True, None),            # 布尔不算数字
    (False, None),
])
def test_number_parses_strings_and_rejects_junk(value, expected):
    """额度解析必须能处理字符串型 Precise，否则整批额度会被算成 0。"""
    from src.provider.codebuddy.client import _number

    result = _number(value)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


# ------------------------------------- CodeBuddy 动态模型列表

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


# ------------------------------------- 400 = INVALID：不冷却凭证

async def test_http_400_does_not_cooldown_or_rotate(dual_repo):
    """上游 400（模型不存在等）→ 抛 InvalidRequest；凭证不冷却、不轮换。"""
    from src.compat.openai.request import InvalidRequest

    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"code": 1002, "msg": "model not found"})

    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "t1"})
    provider = CodeBuddyProvider(client=_client(handler))
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": provider}, credentials=repo, scheduler=Scheduler(),
        default_model="qwen3.8"))

    with pytest.raises(InvalidRequest) as exc_info:
        await executor.complete(_request("qwen3.8"), username="u")
    assert "qwen3.8" in str(exc_info.value)
    assert calls["n"] == 1                      # 未轮换重试
    # 凭证零错误记录、无冷却、未禁用
    row = db.connect().execute(
        "SELECT err_count, cooling_until, disabled FROM credentials").fetchone()
    assert tuple(row) == (0, None, 0)


async def test_http_400_stream_yields_invalid_request_frame(dual_repo):
    """流式路径：400 → invalid_request 错误帧；凭证不冷却、不轮换。"""
    import json as _json

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": 1002, "msg": "model not found"})

    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "t1"})
    provider = CodeBuddyProvider(client=_client(handler))
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": provider}, credentials=repo, scheduler=Scheduler(),
        default_model="qwen3.8"))

    frames = [f async for f in executor.stream(_request("qwen3.8"), username="u")]
    assert len(frames) == 1                     # 错误帧（内含 [DONE]）
    payload = _json.loads(frames[0].split(b"\n\n")[0].removeprefix(b"data: "))
    assert payload["error"]["type"] == "api_error"
    assert payload["error"]["code"] == "invalid_request"
    assert "qwen3.8" in payload["error"]["message"]
    row = db.connect().execute(
        "SELECT err_count, cooling_until FROM credentials").fetchone()
    assert tuple(row) == (0, None)


def test_error_type_for_invalid():
    """INVALID → invalid_request（executor 290-291）。"""
    from src.engine.executor import _error_type_for

    assert _error_type_for(ErrKind.INVALID) == "invalid_request"


# ------------------------------------- INVALID 跳过上游：双上游回退

class _RejectProvider:
    """可脚本化的 provider：按调用次序抛指定异常或返回事件。"""

    def __init__(self, provider_id: str, outcomes: list) -> None:
        self.id = provider_id
        self.outcomes = outcomes
        self.calls = 0

    async def stream_chat(self, _credential_data, _payload, _model):
        index = min(self.calls, len(self.outcomes) - 1)
        self.calls += 1
        item = self.outcomes[index]
        if isinstance(item, Exception):
            raise item
        for event in item:
            yield event

    def list_models(self, _credential_data):
        return []


def _http_400() -> Exception:
    from src.provider.codebuddy.client import UpstreamHTTPError

    return UpstreamHTTPError(400, b'{"code":1002,"msg":"model not found"}')


# 真实上游 400 响应体（渠道风控；实测见 TECHNICAL.md §3.2）
_CB_11128_BODY = (
    b'{"code":11128,"msg":"Illegal API invocation from an unapproved channel"}')


async def test_invalid_skips_provider_and_falls_back_to_next(dual_repo):
    """CB 400（不认识模型）→ 跳过 CB，TRAE 成功接住；凭证零冷却。"""
    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "trae"})
    # pin CB 保证它先被选中，从而触发「400 → 跳过 → TRAE 接住」路径
    db.connect().execute("UPDATE credentials SET pinned = 1 WHERE provider = 'codebuddy'")
    cb = _RejectProvider("codebuddy", [_http_400()])
    trae = _RejectProvider("trae", [GOOD])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "trae": trae}, credentials=repo,
        scheduler=Scheduler(), default_model="qwen3.8-max"))

    result = await executor.complete(_request("qwen3.8-max"), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert cb.calls == 1 and trae.calls == 1
    assert tuple(db.connect().execute(
        "SELECT err_count, cooling_until, disabled FROM credentials").fetchone()) \
        is not None
    rows = [tuple(r) for r in db.connect().execute(
        "SELECT err_count, cooling_until, disabled FROM credentials")]
    assert rows == [(0, None, 0), (0, None, 0)]     # 谁都没被冷却


async def test_all_upstreams_reject_yields_400(dual_repo):
    """两个上游都 400 → InvalidRequest（400 语义），凭证零冷却。"""
    from src.compat.openai.request import InvalidRequest

    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "trae"})
    cb = _RejectProvider("codebuddy", [_http_400()])
    trae = _RejectProvider("trae", [_http_400()])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "trae": trae}, credentials=repo,
        scheduler=Scheduler(), default_model="qwen3.8-max"))

    with pytest.raises(InvalidRequest) as exc_info:
        await executor.complete(_request("qwen3.8-max"), username="u")
    assert "qwen3.8-max" in str(exc_info.value)
    rows = [tuple(r) for r in db.connect().execute(
        "SELECT err_count, cooling_until, disabled FROM credentials")]
    assert rows == [(0, None, 0), (0, None, 0)]


# ------------------------------------- 独有模型目录收窄：不白打无关上游

async def test_flat_exclusive_model_skips_other_upstream(dual_repo):
    """CodeBuddy 独有模型（目录可证归属）→ 只打 CB；TRAE 零调用、零统计记录。"""
    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "trae"})
    # pin TRAE：旧逻辑会先选中它并把请求真实打到 TRAE（留下无效请求记录）
    db.connect().execute("UPDATE credentials SET pinned = 1 WHERE provider = 'trae'")
    cb = _RejectProvider("codebuddy", [GOOD])
    trae = _RejectProvider("trae", [GOOD])
    records: list[dict] = []

    class _Stats:
        @staticmethod
        def record(**fields):
            records.append(fields)

    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "trae": trae}, credentials=repo,
        scheduler=Scheduler(), default_model="cb-only", stats=_Stats(),
        # "ghost" 未注册：顺带覆盖目录里混入未知上游的过滤分支
        model_aliases={"codebuddy": {"cb-only": "cb-only"},
                       "ghost": {"cb-only": "cb-only"}}))

    result = await executor.complete(_request("cb-only"), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert trae.calls == 0 and cb.calls == 1
    assert [(r["provider"], r["ok"]) for r in records] == [("codebuddy", True)]


async def test_forced_provider_bypasses_catalog_narrowing(dual_repo):
    """@provider 强制指定不过滤：目录里只有 CB 登记也照样打 TRAE。"""
    from src.compat.openai.request import InvalidRequest

    repo, _db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "trae"})
    cb = _RejectProvider("codebuddy", [GOOD])
    trae = _RejectProvider("trae", [_http_400()])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "trae": trae}, credentials=repo,
        scheduler=Scheduler(), default_model="cb-only",
        model_aliases={"codebuddy": {"cb-only": "cb-only"}}))

    with pytest.raises(InvalidRequest):
        await executor.complete(_request("cb-only@trae"), username="u")
    assert trae.calls == 1 and cb.calls == 0


async def test_narrowing_disabled_without_catalog(dual_repo):
    """目录未就绪（model_aliases 缺省/为空）→ 保持双上游参与的原行为。"""
    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "trae"})
    db.connect().execute("UPDATE credentials SET pinned = 1 WHERE provider = 'trae'")
    cb = _RejectProvider("codebuddy", [GOOD])
    trae = _RejectProvider("trae", [_http_400()])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "trae": trae}, credentials=repo,
        scheduler=Scheduler(), default_model="qwen3.8-max"))

    result = await executor.complete(_request("qwen3.8-max"), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert trae.calls == 1 and cb.calls == 1


async def test_narrowing_keeps_candidates_when_model_unknown_to_catalog(dual_repo):
    """目录就绪但没有已注册上游登记该模型 → 不过滤（黑名单滤掉仍可直连）。"""
    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "trae"})
    db.connect().execute("UPDATE credentials SET pinned = 1 WHERE provider = 'trae'")
    cb = _RejectProvider("codebuddy", [GOOD])
    trae = _RejectProvider("trae", [_http_400()])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "trae": trae}, credentials=repo,
        scheduler=Scheduler(), default_model="qwen3.8-max",
        model_aliases={"codebuddy": {"glm-5.2": "glm-5.2"},
                       "trae": {"GLM-5.2": "GLM-5.2"}}))

    result = await executor.complete(_request("qwen3.8-max"), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert trae.calls == 1 and cb.calls == 1


async def test_stream_rotate_exhausted_with_invalid_last_error(dual_repo):
    """流式 + 轮换耗尽时最后错误是 INVALID → invalid_request 帧（155-159）。"""
    import json as _json

    repo, _db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    cb = _RejectProvider("codebuddy", [_http_400()])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb}, credentials=repo,
        scheduler=Scheduler(max_rotate=1), default_model="qwen3.8"))

    frames = [f async for f in executor.stream(_request("qwen3.8"), username="u")]
    payload = _json.loads(frames[0].split(b"\n\n")[0].removeprefix(b"data: "))
    assert payload["error"]["code"] == "invalid_request"
    assert "qwen3.8" in payload["error"]["message"]


async def test_complete_rotate_exhausted_with_invalid_last_error(dual_repo):
    """非流式 + 轮换耗尽 + 最后错误 INVALID → 400（executor 238）。"""
    from src.compat.openai.request import InvalidRequest

    repo, _db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    cb = _RejectProvider("codebuddy", [_http_400()])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb}, credentials=repo,
        scheduler=Scheduler(max_rotate=1), default_model="qwen3.8"))

    with pytest.raises(InvalidRequest):
        await executor.complete(_request("qwen3.8"), username="u")


async def test_executor_invalid_request_includes_suggestions(dual_repo):
    """全部上游 400 → 400 文案附相近模型建议。"""
    from src.compat.openai.request import InvalidRequest

    repo, _db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "tr"})
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": _RejectProvider("codebuddy", [_http_400()]),
                   "trae": _RejectProvider("trae", [_http_400()])},
        credentials=repo, scheduler=Scheduler(), default_model="qwen3.8-max",
        model_suggestions=lambda name: ["qwen-3.7-plus"]))

    with pytest.raises(InvalidRequest) as exc_info:
        await executor.complete(_request("qwen3.8-max"), username="u")
    assert "qwen-3.7-plus" in str(exc_info.value)


def test_chat_headers_carry_channel_identity():
    """聊天头必须带完整渠道特征（缺失触发 11128 渠道风控）。"""
    from src.provider.codebuddy.headers import generate_headers

    headers = generate_headers(
        endpoint="https://copilot.tencent.com", bearer_token="t",
        user_id="u1", enterprise_id="ent1")
    # 会话链路（每请求随机）
    assert headers["X-Conversation-ID"] != headers["X-Request-ID"]
    assert len(headers["X-Conversation-Request-ID"]) == 32
    # 渠道身份
    assert headers["X-Agent-Intent"] == "craft"
    assert headers["X-Agent-Purpose"] == "conversation"
    assert headers["X-IDE-Type"] == headers["X-IDE-Name"] == "CLI"
    assert headers["X-CodeBuddy-Request"] == "1"
    assert headers["X-Private-Data"] == "false"
    assert headers["X-Requested-With"] == "XMLHttpRequest"
    # OpenAI JS SDK 指纹
    assert headers["x-stainless-lang"] == "js"
    assert headers["x-stainless-runtime"] == "node"
    assert headers["x-stainless-retry-count"] == "0"
    # 企业租户
    assert headers["X-Tenant-Id"] == "ent1"


def test_quota_only_headers_carry_full_identity():
    """quota_only 只切换 URL；头部与聊天一致（原实现不区分，防渠道校验）。"""
    from src.provider.codebuddy.headers import generate_headers

    headers = generate_headers(
        endpoint="https://copilot.tencent.com", bearer_token="t", quota_only=True)
    assert "X-Conversation-ID" in headers
    assert headers["X-Agent-Intent"] == "craft"


def test_stainless_fingerprint_variants():
    """x-stainless-arch/os 的平台分支（headers 64-79）。"""
    from unittest import mock

    from src.provider.codebuddy import headers as h

    with mock.patch.object(h.platform, "machine", return_value="x86_64"):
        assert h._stainless_arch() == "x64"
    with mock.patch.object(h.platform, "machine", return_value="aarch64"):
        assert h._stainless_arch() == "arm64"
    with mock.patch.object(h.platform, "machine", return_value="riscv"):
        assert h._stainless_arch() == "riscv"
    with mock.patch.object(h.platform, "system", return_value="Darwin"):
        assert h._stainless_os() == "MacOS"
    with mock.patch.object(h.platform, "system", return_value="Windows"):
        assert h._stainless_os() == "Windows"
    with mock.patch.object(h.platform, "system", return_value="Linux"):
        assert h._stainless_os() == "Linux"
    with mock.patch.object(h.platform, "system", return_value="SunOS"):
        assert h._stainless_os() == "SunOS"


async def test_chat_pacer_throttles_and_can_be_disabled():
    """CB 聊天节流：请求前等待；None/关闭时不等待。"""
    import time as _time

    from src.provider.codebuddy.client import CodeBuddyProvider

    waited = []

    class FakePacer:
        async def wait_turn(self):
            waited.append(_time.monotonic())

    async def stream_ok(cred, payload, model):
        yield GOOD[0]

    class FakeClient:
        async def stream_chat(self, cred, payload, model):
            async for ev in stream_ok(cred, payload, model):
                yield ev

    provider = CodeBuddyProvider(client=FakeClient(), pacer=FakePacer())
    events = [e async for e in provider.stream_chat({"bearer_token": "t"}, {}, "m")]
    assert events and waited

    provider2 = CodeBuddyProvider(client=FakeClient(), pacer=None)
    waited.clear()
    _ = [e async for e in provider2.stream_chat({"bearer_token": "t"}, {}, "m")]
    assert not waited


async def test_stream_inner_error_other_is_logged_and_counted(dual_repo, caplog):
    """流内非 4001/1005 错误 → 记冷却并打日志（可观测性回归）。"""
    import logging

    from src.provider.base import Event

    repo, _db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    calls = {"n": 0}

    async def gen(_cred, _payload, _model):
        calls["n"] += 1
        yield Event(kind=EventKind.ERROR, error_code=41291, error_message="rate limited")

    class P:
        id = "codebuddy"
        stream_chat = staticmethod(gen)

    executor = Executor(ExecutorDeps(
        providers={"codebuddy": P()}, credentials=repo,
        scheduler=Scheduler(max_rotate=1), default_model="m"))
    frames = [f async for f in executor.stream(_request("m"), username="u")]
    assert any(b"error" in f for f in frames)
    assert calls["n"] == 1
    assert any("流内错误" in r.getMessage() for r in caplog.records
               if r.levelno == logging.WARNING)


async def test_stream_inner_4001_yields_invalid_frame(dual_repo, caplog):
    """流内 4001 → INVALID：跳过上游 + invalid_request 帧。"""
    import json as _json
    import logging

    from src.provider.base import Event

    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    calls = {"n": 0}

    async def gen(_cred, _payload, _model):
        calls["n"] += 1
        yield Event(kind=EventKind.ERROR, error_code=4001, error_message="param invalid")

    class P:
        id = "codebuddy"
        stream_chat = staticmethod(gen)

    executor = Executor(ExecutorDeps(
        providers={"codebuddy": P()}, credentials=repo,
        scheduler=Scheduler(max_rotate=1), default_model="m",
        model_suggestions=lambda _n: ["alt-model"]))

    with caplog.at_level(logging.WARNING):
        frames = [f async for f in executor.stream(_request("m"), username="u")]
    payload = _json.loads(frames[0].split(b"\n\n")[0].removeprefix(b"data: "))
    assert payload["error"]["code"] == "invalid_request"
    assert "alt-model" in payload["error"]["message"]
    assert calls["n"] == 1                              # 不轮换
    row = db.connect().execute(
        "SELECT err_count, cooling_until FROM credentials").fetchone()
    assert tuple(row) == (0, None)                      # 不冷却
    assert any("流内拒绝模型" in r.getMessage() for r in caplog.records)


async def test_stream_chat_body_carries_cli_signature_fields():
    """官方 CLI 特征字段：enable_thinking / stream_options.include_usage。"""
    import json as _json

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    captured: dict = {}

    def capture_handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(request.read())
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    provider = CodeBuddyProvider(client=_client(capture_handler))
    payload = {"messages": [{"role": "user", "content": "hi"}],
               "stream_options": {"include_usage": False}}
    events = [e async for e in provider.stream_chat({"bearer_token": "t"}, payload, "m")]

    body = captured["body"]
    assert body["enable_thinking"] is True          # 缺失触发 11128 渠道风控
    assert body["stream_options"]["include_usage"] is True
    assert body["stream"] is True
    assert body["model"] == "m"
    assert events[-1].kind is EventKind.FINISH


async def test_trae_4001_falls_through_to_codebuddy(dual_repo, caplog):
    """TRAE 流内 4001 → 跳过 TRAE → CB 成功接住；TRAE 凭证不被冷却。"""

    from src.provider.base import Event

    class Trae4001:
        id = "trae"
        calls = 0

        async def stream_chat(self, _cred, _payload, _model):
            self.calls += 1
            yield Event(kind=EventKind.ERROR, error_code=4001, error_message="param")

    class CbOk:
        id = "codebuddy"
        calls = 0

        async def stream_chat(self, _cred, _payload, _model):
            self.calls += 1
            yield Event(kind=EventKind.CONTENT, content="ok")
            yield Event(kind=EventKind.FINISH, finish_reason="stop")

    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "tr"})
    db.connect().execute("UPDATE credentials SET pinned = 1 WHERE provider = 'trae'")
    trae, cb = Trae4001(), CbOk()
    executor = Executor(ExecutorDeps(
        providers={"trae": trae, "codebuddy": cb}, credentials=repo,
        scheduler=Scheduler(), default_model="m"))

    result = await executor.complete(_request("m"), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert trae.calls == 1 and cb.calls == 1
    rows = [tuple(r) for r in db.connect().execute(
        "SELECT err_count, cooling_until FROM credentials ORDER BY provider")]
    assert rows == [(0, None), (0, None)]       # TRAE 4001 不冷却


async def test_stream_chat_normalizes_developer_role():
    """PI 的 developer 角色归一为 system（腾讯实测 developer → 11128）。"""
    import json as _json

    captured: dict = {}

    def capture_handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(request.read())
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    provider = CodeBuddyProvider(client=_client(capture_handler))
    payload = {"messages": [
        {"role": "developer", "content": "You are PI."},
        {"role": "user", "content": "hi"},
    ]}
    _ = [e async for e in provider.stream_chat({"bearer_token": "t"}, payload, "m")]
    roles = [m["role"] for m in captured["body"]["messages"]]
    assert roles == ["system", "user"]


def test_blank_noise_tool_call_is_dropped_but_argument_shards_kept():
    """噪声（无 name 且空 arguments）丢弃；正常分片（带实际 arguments）保留。"""
    body_obj = {
        "choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {}},
            {"index": 0, "function": {"arguments": "{}"}},
            {"index": 0, "id": "c1", "type": "function",
             "function": {"name": "bash", "arguments": ""}},
            {"index": 0, "function": {"arguments": '{"x":1}'}},
        ]}}],
    }
    frame = cb_events.SSEFrame(event="", data=json.dumps(body_obj))
    event = cb_events.parse_frame(frame)
    assert event is not None and event.kind is EventKind.TOOL_CALLS
    kept = event.tool_calls
    assert len(kept) == 2                                  # 两个噪声被丢
    assert kept[0]["function"]["name"] == "bash"           # 首块保留
    assert kept[1]["function"]["arguments"] == '{"x":1}'   # 分片保留

def test_clean_history_tool_calls_removes_dirty_and_orphans(tmp_path):
    """历史清理：空名剔除、空占位丢弃、悬空 tool 成对清理。"""
    from src.provider.codebuddy.client import _clean_history_tool_calls

    body = {"messages": [
        {"role": "system", "content": "s"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "bad", "type": "function",
                         "function": {"arguments": "{}"}},
                        {"id": "good", "type": "function",
                         "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "bad", "content": "x"},
        {"role": "tool", "tool_call_id": "good", "content": "y"},
        {"role": "assistant", "content": None, "tool_calls": []},
        {"role": "user", "content": "hi"},
    ]}
    _clean_history_tool_calls(body)
    # bad 被剔 → 其 tool 消息删；good 保留 → 其 tool 消息保留；
    # 空占位 assistant 丢弃
    roles = [(m["role"], m.get("tool_call_id", "")) for m in body["messages"]]
    assert roles == [
        ("system", ""), ("assistant", ""), ("tool", "good"), ("user", "")]

    # 边界：非 list messages 原样返回；非 dict 消息与非 dict tc 跳过
    body2 = {"messages": "nope"}
    _clean_history_tool_calls(body2)
    assert body2["messages"] == "nope"

    body3 = {"messages": [
        "junk",
        {"role": "assistant", "content": None, "tool_calls": ["junk", {"id": "bad"}]},
        {"role": "tool", "tool_call_id": "bad", "content": "x"},
    ]}
    _clean_history_tool_calls(body3)
    # 非dict消息保留、非dict tc 剔除；assistant 带 content 保留；悬空 tool 清理
    assert [(m if isinstance(m, str) else m["role"]) for m in body3["messages"]] == ["junk"]

    # assistant 有实际 content → tool_calls 剔除后消息本身保留
    body4 = {"messages": [
        {"role": "assistant", "content": "文本",
         "tool_calls": [{"id": "bad", "type": "function",
                         "function": {"arguments": "{}"}}]},
    ]}
    _clean_history_tool_calls(body4)
    assert body4["messages"][0]["role"] == "assistant"
    assert body4["messages"][0]["content"] == "文本"
    assert "tool_calls" not in body4["messages"][0]


# ------------------------------------- 11128 内容风控：伪装客户端指纹中和

def test_sanitize_channel_markers_neutralizes_prompt_roles_only():
    """system/assistant 正文替换为占位符；user/tool 与非正文位置不动。"""
    from src.provider.codebuddy.client import CHANNEL_MARKERS, sanitize_channel_markers

    marker = CHANNEL_MARKERS[0]
    body = {"messages": [
        {"role": "system", "content": f"前缀 {marker} 后缀"},
        {"role": "assistant", "content": marker},
        {"role": "user", "content": marker},                      # user 不动
        {"role": "tool", "tool_call_id": "t", "content": marker},  # tool 不动
        {"role": "assistant", "tool_calls": [{"id": "c", "type": "function",
                                             "function": {"name": "f",
                                                          "arguments": f'"{marker}"'}}]},
    ]}
    hits = sanitize_channel_markers(body)
    assert hits == 2
    assert body["messages"][0]["content"] == "前缀 [external-client-identity] 后缀"
    assert body["messages"][1]["content"] == "[external-client-identity]"
    assert body["messages"][2]["content"] == marker
    assert body["messages"][3]["content"] == marker
    args = body["messages"][4]["tool_calls"][0]["function"]["arguments"]
    assert marker in args                          # tool_calls 参数不动


def test_sanitize_channel_markers_neutralizes_text_blocks():
    """content 为文本块列表时逐块中和（2026-09-21 实测块形态指纹同样 11128）。"""
    from src.provider.codebuddy.client import CHANNEL_MARKERS, sanitize_channel_markers

    marker = CHANNEL_MARKERS[0]
    body = {"messages": [
        {"role": "system", "content": [
            {"type": "text", "text": f"前缀 {marker} 后缀"},
            {"type": "text", "text": "无指纹"},
            {"type": "image_url", "image_url": {"url": marker}},  # 非文本块不动
            {"type": "text", "text": 123},                        # 非 str 不动
            "junk",                                               # 非 dict 不动
        ]},
        {"role": "assistant", "content": [
            {"type": "text", "text": marker},
            {"type": "text", "text": f"{marker} x{marker}"},
        ]},
        {"role": "user", "content": [{"type": "text", "text": marker}]},  # user 不动
    ]}
    assert sanitize_channel_markers(body) == 4
    blocks = body["messages"][0]["content"]
    assert blocks[0]["text"] == "前缀 [external-client-identity] 后缀"
    assert blocks[1]["text"] == "无指纹"
    assert blocks[2]["image_url"]["url"] == marker
    assert blocks[3]["text"] == 123
    assert blocks[4] == "junk"
    assistant_blocks = body["messages"][1]["content"]
    assert assistant_blocks[0]["text"] == "[external-client-identity]"
    assert assistant_blocks[1]["text"] == "[external-client-identity] x[external-client-identity]"
    assert body["messages"][2]["content"][0]["text"] == marker


def test_sanitize_channel_markers_counts_repeats_and_skips_noise():
    """同条消息多指纹多出处计数；非 list/非 dict/非 str content 安全跳过。"""
    from src.provider.codebuddy.client import CHANNEL_MARKERS, sanitize_channel_markers

    a, b = CHANNEL_MARKERS[1], CHANNEL_MARKERS[2]
    body = {"messages": [
        "junk",
        {"role": "assistant", "content": None},
        {"role": "assistant"},
        {"role": "assistant", "content": ""},
        {"role": "assistant", "content": {"list": "content"}},
        {"role": "assistant", "content": f"{a} 中间 {b} 结尾 {a}"},
    ]}
    assert sanitize_channel_markers(body) == 3
    assert body["messages"][5]["content"] == (
        "[external-client-identity] 中间 [external-client-identity] 结尾 "
        "[external-client-identity]")

    assert sanitize_channel_markers({}) == 0
    assert sanitize_channel_markers({"messages": "nope"}) == 0


async def test_stream_chat_sanitizes_markers_and_warns(caplog):
    """默认开启：出站正文被中和并告警；可关。"""
    import json as _json

    marker = "Main branch (you will usually use this for PRs):"

    def fresh_payload() -> dict:
        return {"messages": [
            {"role": "system", "content": "s"},
            {"role": "assistant", "content": f"见 {marker}"},
            {"role": "user", "content": "hi"},
        ]}

    def capture_handler(request: httpx.Request) -> httpx.Response:
        capture_handler.body = _json.loads(request.read())  # type: ignore[attr-defined]
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    with caplog.at_level(logging.WARNING):
        [e async for e in _client(capture_handler).stream_chat(
            CodeBuddyCredential(bearer_token="t"), fresh_payload(), "m")]
        body = capture_handler.body  # type: ignore[attr-defined]
    assert body["messages"][1]["content"] == "见 [external-client-identity]"
    assert body["messages"][2]["content"] == "hi"     # user 原样
    assert any("伪装客户端指纹" in r.getMessage() for r in caplog.records)

    def passthrough_handler(request: httpx.Request) -> httpx.Response:
        passthrough_handler.body = _json.loads(request.read())  # type: ignore[attr-defined]
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    client = _client(passthrough_handler)
    client.sanitize_markers = False
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        [e async for e in client.stream_chat(
            CodeBuddyCredential(bearer_token="t"), fresh_payload(), "m")]
        body = passthrough_handler.body  # type: ignore[attr-defined]
    assert body["messages"][1]["content"] == f"见 {marker}"   # 关闭后透传
    assert not any("伪装客户端指纹" in r.getMessage() for r in caplog.records)


# ------------------------------------------- 模型级冷却的端到端行为（B1.1）

def _model_cooldown_rows(db):
    return [tuple(row) for row in db.connect().execute(
        "SELECT credential_id, model, hits, reason FROM credential_model_cooldowns"
    ).fetchall()]


async def test_stream_6004_cools_only_that_model(dual_repo):
    """429 + 6004：只写 (凭证, 模型) 条目，账号级冷却保持为空。"""
    from src.provider.base import Event

    repo, db = dual_repo
    cred_id = repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})

    async def gen(_cred, _payload, _model):
        yield Event(kind=EventKind.ERROR, error_code=6004, error_message="model quota")

    class P:
        id = "codebuddy"
        stream_chat = staticmethod(gen)

    executor = Executor(ExecutorDeps(
        providers={"codebuddy": P()}, credentials=repo,
        scheduler=Scheduler(max_rotate=1), default_model="glm-5.2"))
    frames = [f async for f in executor.stream(_request(), username="u")]
    assert any(b"error" in f for f in frames)

    account = db.connect().execute(
        "SELECT cooling_until, err_count FROM credentials").fetchone()
    assert tuple(account) == (None, 0)                   # 账号未被冷却
    assert _model_cooldown_rows(db) == [(cred_id, "glm-5.2", 1, "model")]


async def test_stream_11102_blocks_account_model_pair(dual_repo):
    """11102「该后端无此模型」→ negative cache，且换号能继续服务。"""
    from src.provider.base import Event

    repo, db = dual_repo
    blocked_id = repo.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    healthy_id = repo.add(provider="codebuddy", credential_data={"bearer_token": "h"})
    # 让被负缓存的凭证先被选中（pin 优先）
    db.connect().execute("UPDATE credentials SET pinned = 1 WHERE id = ?", (blocked_id,))
    db.connect().commit()

    class P:
        id = "codebuddy"

        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, cred, _payload, _model):
            self.calls += 1
            if cred["bearer_token"] == "b":
                yield Event(kind=EventKind.ERROR, error_code=11102,
                            error_message="no such model")
                return
            yield Event(kind=EventKind.CONTENT, content="ok")
            yield Event(kind=EventKind.FINISH, finish_reason="stop")

    provider = P()
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": provider}, credentials=repo,
        scheduler=Scheduler(), default_model="glm-5.2"))
    frames = [f async for f in executor.stream(_request(), username="u")]
    assert any(b"ok" in f for f in frames)                # 第二个凭证接住
    assert _model_cooldown_rows(db) == [(blocked_id, "glm-5.2", 1, "blocked")]
    # 该凭证的其他模型不受影响
    assert repo.candidates()[0].model_cooldowns is not None
    assert repo.candidates()[1].model_cooldowns is None
    assert healthy_id != blocked_id


async def test_stream_11101_request_error_does_not_touch_credential(dual_repo):
    """请求级错误（11101 请求体坏）：不冷却、不累计，但仍换号重试。

    err_count 预置为 2（阈值 3）：若请求级错误被当成普通错误累计，
    第三次就会触发熔断冷却，健康凭证被踢出池——这正是要防的误伤。
    """
    from src.engine.scheduler import ErrorOutcome
    from src.provider.base import Event

    repo, db = dual_repo
    for token in ("a", "b", "c"):
        cred_id = repo.add(provider="codebuddy", credential_data={"bearer_token": token})
        repo.save_error(cred_id, ErrorOutcome(err_count=2))

    calls = {"n": 0}

    class P:
        id = "codebuddy"

        async def stream_chat(self, _cred, _payload, _model):
            calls["n"] += 1
            yield Event(kind=EventKind.ERROR, error_code=11101,
                        error_message="Unmarshal chat params failed")

    executor = Executor(ExecutorDeps(
        providers={"codebuddy": P()}, credentials=repo,
        scheduler=Scheduler(max_rotate=3), default_model="glm-5.2"))
    frames = [f async for f in executor.stream(_request(), username="u")]
    assert any(b"error" in f for f in frames)
    assert calls["n"] == 3                               # 换号重试到上限，不是原地放弃

    rows = db.connect().execute(
        "SELECT cooling_until, err_count FROM credentials").fetchall()
    assert [tuple(row) for row in rows] == [(None, 2)] * 3   # 原样保留：没冷却也没累计
    assert _model_cooldown_rows(db) == []


async def test_http_400_11128_rotates_instead_of_skipping_provider(dual_repo):
    """渠道风控 11128（400）→ REQUEST：换号重试，不是跳过上游直接 400。

    修复前 11128 落 INVALID：_skip_provider 把该上游全部凭证塞进 tried，
    should_rotate 立即为假，第一条请求就以 invalid_request 400 收尾（零重试）。
    回归证据：真实日志里同凭证同时刻换模型即成功、同 (凭证, 模型) 秒级交替
    成功/失败，属于瞬时风控而非模型/凭证问题。
    """
    repo, db = dual_repo
    first = repo.add(provider="codebuddy", credential_data={"bearer_token": "a"})
    repo.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    db.connect().execute("UPDATE credentials SET pinned = 1 WHERE id = ?", (first,))

    class P:
        id = "codebuddy"

        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, _cred, _payload, _model):
            self.calls += 1
            if self.calls == 1:
                raise UpstreamHTTPError(400, _CB_11128_BODY)
            for event in GOOD:
                yield event

    provider = P()
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": provider}, credentials=repo,
        scheduler=Scheduler(max_rotate=3), default_model="glm-5.2"))

    result = await executor.complete(_request(), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert provider.calls == 2                     # 换到第二个凭证重试成功

    rows = db.connect().execute(
        "SELECT cooling_until, err_count, disabled FROM credentials").fetchall()
    assert [tuple(row) for row in rows] == [(None, 0, 0)] * 2   # 两个凭证都零惩罚
    assert _model_cooldown_rows(db) == []


async def test_complete_402_cools_until_next_credit_reset(dual_repo):
    """402 余额不足：非流式路径冷到次日签到时刻（不是固定 12h）。"""
    from src.engine.scheduler import next_credit_reset

    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})

    class PaymentRequired(Exception):
        def kind(self):
            return ErrKind.CREDIT

    executor, _trae, codebuddy = _dual_executor(repo, GOOD, [[PaymentRequired()]])
    with pytest.raises(NoHealthyCredential):
        await executor.complete(_request())

    cooling_until = db.connect().execute(
        "SELECT cooling_until FROM credentials").fetchone()["cooling_until"]
    assert cooling_until == next_credit_reset(int(time.time()))

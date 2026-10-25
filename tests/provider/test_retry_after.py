"""A2：上游 `Retry-After` 解析（整秒 / HTTP-date / 文本 / 缺头 / 非法）。

纯函数测试：注入固定 `now`，不 sleep、不访问网络。另含一个端到端用例
（`429 + Retry-After: 7200` → 冷却约 2h），走 MockTransport 校验各渠道的
raise 点确实填了 `retry_at`。
"""

from __future__ import annotations

import email.utils
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.provider.base import (
    ErrKind,
    UpstreamHTTPError,
    parse_reset_at,
    parse_retry_after,
    parse_retry_after_seconds,
    retry_hints,
)

NOW = 1_700_000_000


# ------------------------------------------------------------------ 整秒


@pytest.mark.parametrize("value, expected", [
    ("0", 0),
    ("60", 60),
    ("7200", 7200),
    ("  90  ", 90),
])
def test_integer_seconds(value, expected):
    assert parse_retry_after({"Retry-After": value}, NOW) == NOW + expected
    assert parse_retry_after_seconds({"Retry-After": value}, NOW) == expected


def test_header_lookup_is_case_insensitive():
    """httpx.Headers 大小写不敏感；普通 dict 也要能命中（各渠道测试都用 dict）。"""
    headers = httpx.Headers({"retry-after": "42"})
    assert parse_retry_after(headers, NOW) == NOW + 42
    assert parse_retry_after({"RETRY-AFTER": "42"}, NOW) == NOW + 42


# --------------------------------------------------------------- HTTP-date


def test_http_date_is_converted_to_epoch():
    moment = NOW + 3600
    value = email.utils.formatdate(moment, usegmt=True)
    assert parse_retry_after({"Retry-After": value}, NOW) == moment


def test_http_date_without_timezone_is_utc():
    """RFC 1123 之外的历史格式（无时区）按 UTC 解释，不按本机时区。"""
    value = time.strftime("%a, %d %b %Y %H:%M:%S", time.gmtime(NOW + 120))
    assert parse_retry_after({"Retry-After": value}, NOW) == NOW + 120


def test_past_http_date_is_rejected():
    """过去时刻返回 None：交给调度器回落本地常量（A2 双端钳制）。"""
    value = email.utils.formatdate(NOW - 3600, usegmt=True)
    assert parse_retry_after({"Retry-After": value}, NOW) is None


# ----------------------------------------------------------------- 文本格式


@pytest.mark.parametrize("value, expected", [
    ("Try again in 17h 59m", 17 * 3600 + 59 * 60),
    ("try again in 30s", 30),
    ("Try again in 2 hours", 7200),
    ("Try again in 1 hour, 30 min", 5400),
    ("Try again in 45 minutes and 30 seconds", 45 * 60 + 30),
    ("in 5m", 300),
    ("17h 59m", 17 * 3600 + 59 * 60),
    ("Try again in 1.5h", 5400),
])
def test_text_forms(value, expected):
    assert parse_retry_after_seconds({"Retry-After": value}, NOW) == expected


# --------------------------------------------------------------- 缺头 / 非法


@pytest.mark.parametrize("headers", [
    {},
    {"Retry-After": ""},
    {"Retry-After": "   "},
    {"Retry-After": "soon"},
    {"Retry-After": "17 fortnights"},
    {"Retry-After": "-60"},
    {"Retry-After": "1.2.3"},
    {"Retry-After": "Try again later"},
    {"Retry-After": "0x10"},
    {"Retry-After": "17h junk"},
    {"Retry-After": "17h x 59m"},                   # token 间隔不是空白/逗号/and
    {"Retry-After": "9" * 400 + "h"},               # 溢出成 inf
    {"X-Other": "60"},
    None,
])
def test_invalid_or_missing_headers_return_none(headers):
    assert parse_retry_after(headers, NOW) is None
    assert parse_retry_after_seconds(headers, NOW) is None


def test_retry_hints_shape():
    """无头时两值都是 None：`**retry_hints(...)` 与旧构造逐字节等价。"""
    assert retry_hints({}, NOW) == {"retry_at": None, "retry_after_seconds": None}
    assert retry_hints({"Retry-After": "120"}, NOW) == {
        "retry_at": NOW + 120, "retry_after_seconds": 120.0}
    # now 缺省读当前时刻（只校验「在将来」，不依赖具体秒数）
    hints = retry_hints({"Retry-After": "60"})
    assert hints["retry_at"] is not None
    assert hints["retry_at"] > int(time.time())


# ------------------------------------------------------------------- 异常字段


def test_upstream_http_error_defaults_to_none():
    error = UpstreamHTTPError(429, b"")
    assert error.retry_at is None and error.retry_after_seconds is None


def test_upstream_http_error_accepts_hints():
    error = UpstreamHTTPError(429, b"", retry_at=NOW + 60, retry_after_seconds=60.0)
    assert error.retry_at == NOW + 60
    assert error.retry_after_seconds == 60.0


# --------------------------------------------------- 各渠道 raise 点端到端填值


def _kilo_client(handler):
    from src.provider.kilo.client import KiloClient

    transport = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None)
    return KiloClient(host="https://kilo.test", stream_client=transport,
                      short_client=transport)


async def test_kilo_stream_fills_retry_at_from_header():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b'{"error":"slow down"}',
                              headers={"Retry-After": "7200"})

    before = int(time.time())
    with pytest.raises(UpstreamHTTPError) as caught:
        [event async for event in _kilo_client(handler).stream_chat({"messages": []}, "m")]
    error = caught.value
    assert error.retry_after_seconds == 7200.0
    assert error.retry_at is not None
    assert before + 7200 <= error.retry_at <= int(time.time()) + 7200


async def test_kilo_stream_without_header_keeps_none():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b'{"error":"slow down"}')

    with pytest.raises(UpstreamHTTPError) as caught:
        [event async for event in _kilo_client(handler).stream_chat({"messages": []}, "m")]
    assert caught.value.retry_at is None
    assert caught.value.retry_after_seconds is None


@pytest.mark.parametrize("module_path, factory", [
    ("src.provider.codebuddy.client", "CodeBuddyProvider"),
    ("src.provider.trae.client", "TraeProvider"),
    ("src.provider.zen.client", "ZenProvider"),
])
async def test_chat_raise_points_fill_retry_at(module_path, factory):
    """三个走同一 SSE 形状的渠道：非 2xx 的 raise 点都带上了 retry 提示。"""
    import importlib

    module = importlib.import_module(module_path)
    provider_class = getattr(module, factory)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b"", headers={"Retry-After": "120"})

    transport = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None)
    client = module.__dict__[factory.replace("Provider", "Client")](
        stream_client=transport, short_client=transport)
    provider = provider_class(client=client)
    with pytest.raises(UpstreamHTTPError) as caught:
        [event async for event in provider.stream_chat(
            _credential_for(module_path), {"messages": []}, "m")]
    assert caught.value.retry_after_seconds == 120.0


def _credential_for(module_path: str):
    """各渠道 `stream_chat` 需要的凭证形状（只填它读取的字段）。"""
    if "trae" in module_path:
        return {"accessToken": "t"}
    return {"bearer_token": "t"}


# ------------------------------------------------- 响应体里的重置时刻（A2b）
#
# CodeBuddy 国际站 `429 + 6004` 不回 `Retry-After` 头，恢复时刻只在正文：
# `... your usage will reset at 2026-10-25 22:34:25 UTC+8, alternatively ...`。
# 不解析它就永远回落 600s 基数：实测同一号一天被爆 42 次，每次都「冷却到期
# → 重试 → 又 429」。

# 2026-10-25 22:34:25 UTC+8 = 14:34:25Z
RESET_MOMENT = 1_792_938_865
RESET_BODY = (
    b'{"code":6004,"msg":"usage exceeds frequency limit, but don\'t worry, your '
    b'usage will reset at 2026-10-25 22:34:25 UTC+8, alternatively, you can '
    b'switch to the other models to continue using it.","requestId":"8967ac37"}'
)


def test_parse_reset_at_from_real_body():
    assert parse_reset_at(RESET_BODY, NOW) == RESET_MOMENT
    # 距现在多少秒 = 绝对时刻 - now（供诊断，不参与钳制）
    assert retry_hints({}, NOW, body=RESET_BODY)["retry_after_seconds"] == (
        RESET_MOMENT - NOW)


def test_parse_reset_at_accepts_str_and_other_offsets():
    """str 入参、`UTC+08:00` 写法、负偏移都要能解析。"""
    text = "usage will reset at 2026-10-25 22:34:25 UTC+08:00"
    assert parse_reset_at(text, NOW) == RESET_MOMENT
    # UTC-5：同一墙钟时刻在 UTC-5 是 5 小时之后
    negative = "reset at 2026-10-25 22:34:25 UTC-5"
    assert parse_reset_at(negative, NOW) == RESET_MOMENT + 13 * 3600


def test_parse_reset_at_rejects_past_and_garbage():
    """过去时刻 / 无偏移 / 脏日期一律 None：交给调度器回落本地常量。"""
    assert parse_reset_at("reset at 2020-01-01 00:00:00 UTC+8", NOW) is None
    # 无 UTC 偏移：无法判断时区，不猜
    assert parse_reset_at("reset at 2026-10-25 22:34:25", NOW) is None
    # 非法日期（2 月 30 日）
    assert parse_reset_at("reset at 2026-02-30 00:00:00 UTC+8", NOW) is None
    assert parse_reset_at("", NOW) is None
    assert parse_reset_at(b"", NOW) is None
    assert parse_reset_at(b"no reset word here 2026-10-25 22:34:25 UTC+8", NOW) is None


def test_retry_hints_prefers_header_over_body():
    """头是协议标准位，正文只是文案：两者都有时以头为准。"""
    hints = retry_hints({"Retry-After": "120"}, NOW, body=RESET_BODY)
    assert hints == {"retry_at": NOW + 120, "retry_after_seconds": 120.0}


def test_retry_hints_falls_back_to_body_without_header():
    hints = retry_hints({}, NOW, body=RESET_BODY)
    assert hints["retry_at"] == RESET_MOMENT
    # body 不含重置时刻时仍是 None（行为与历史版本一致）
    assert retry_hints({}, NOW, body=b'{"code":6004}') == {
        "retry_at": None, "retry_after_seconds": None}
    # 不传 body 与历史签名逐字节等价
    assert retry_hints({}, NOW) == {"retry_at": None, "retry_after_seconds": None}


def _reset_body_at(moment: int) -> bytes:
    """按 `moment` 生成一份真实形状的正文（重置时刻写成 UTC+8 墙钟）。

    端到端用例必须用**相对当前时刻**的正文。写死某个日期的话，跑到那天之后
    `parse_reset_at` 会把「过去的时刻」判成 None（这是它的正确行为，见
    `test_parse_reset_at_rejects_past_and_garbage`），用例就变成隔天必红——
    实测 2026-10-25 22:34:25 那条写死正文在 22:35 之后即失败。
    """
    stamp = datetime.fromtimestamp(moment, timezone(timedelta(hours=8)))
    return (
        b'{"code":6004,"msg":"usage exceeds frequency limit, but don\'t worry, your '
        b'usage will reset at '
        + stamp.strftime("%Y-%m-%d %H:%M:%S").encode()
        + b' UTC+8, alternatively, you can switch to the other models to '
        b'continue using it.","requestId":"8967ac37"}'
    )


async def test_codebuddy_stream_fills_retry_at_from_body():
    """CodeBuddy 的 raise 点把 body 传给了 retry_hints：429+6004 冷却到重置时刻。"""
    from src.provider.codebuddy.client import CodeBuddyClient, CodeBuddyCredential

    reset_at = int(time.time()) + 3600
    body = _reset_body_at(reset_at)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=body)

    transport = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None)
    client = CodeBuddyClient(stream_client=transport, short_client=transport)
    before = int(time.time())
    with pytest.raises(UpstreamHTTPError) as caught:
        [event async for event in client.stream_chat(
            CodeBuddyCredential(bearer_token="t"), {"messages": []},
            "deepseek-v4.1-flash")]
    assert caught.value.retry_at == reset_at
    assert caught.value.kind() is ErrKind.MODEL
    assert caught.value.retry_at > before

"""CodeArts 逐行 SSE 事件解析与错误分类。"""

from __future__ import annotations

import json

import pytest

from src.provider.base import ErrKind, EventKind, Usage
from src.provider.codearts import events as codearts_events


async def _lines(chunks: list[bytes]):
    async def gen():
        for chunk in chunks:
            yield chunk
    async for line in codearts_events.iter_data_lines(gen()):
        yield line

def _v2_usage(usage: dict) -> Usage:
    """喂一帧标准 OpenAI usage 对象（v2 路径），取回解析结果。"""
    snapshot = codearts_events.TextSnapshot()
    payload = {"choices": [], "usage": usage}
    return codearts_events.parse_line(json.dumps(payload), snapshot)[0].usage

def test_events_text_snapshot_replace_semantics():
    snapshot = codearts_events.TextSnapshot()
    assert snapshot.delta("He") == "He"
    assert snapshot.delta("Hello") == "llo"
    assert snapshot.delta("Hello") == ""            # 无变化
    assert snapshot.delta("Hi") == "Hi"             # 修正/回退 → 整段重发

async def test_events_iter_data_lines_handles_prefixes_and_tail():
    chunks = [
        b"event: message\n",            # 无 data 语义 → 跳过
        b"data: {\"a\": 1}\n",
        b"\n",
        b"{\"b\": 2}\n",                # 实测部分行没有 data: 前缀
        b"data: ",                      # 空载荷 → 跳过
        b'{"c":3}',                     # 末行无换行
    ]
    lines = [line async for line in _lines(chunks)]
    assert lines == ['{"a": 1}', '{"b": 2}', '{"c":3}']

async def test_events_iter_data_lines_multibyte_split_across_chunks():
    """UTF-8 多字节字符被块边界切断不能变替换字符。"""
    raw = "data: {\"text\": \"中文\"}\n".encode()
    lines = [line async for line in _lines([raw[:12], raw[12:]])]
    assert lines == ['{"text": "中文"}']

async def test_events_iter_data_lines_rejects_oversized_line(monkeypatch):
    """CodeArts 行缓冲同样有上限，上游不吐换行时中止该流。"""
    from src.engine import sse

    monkeypatch.setattr(codearts_events, "MAX_DATA_LINE_CHARS", 8)

    async def gen():
        yield b"data: " + b"x" * 100

    with pytest.raises(sse.SSEFrameTooLarge, match="CodeArts data line"):
        [line async for line in codearts_events.iter_data_lines(gen())]

def test_events_parse_line_content_finish_reasoning_usage():
    snapshot = codearts_events.TextSnapshot()
    assert codearts_events.parse_line('{"text": "Hi"}', snapshot)[0].content == "Hi"
    done = codearts_events.parse_line('{"text": "[DONE]"}', snapshot)
    assert done[0].kind is EventKind.FINISH
    delta = codearts_events.parse_line(
        '{"delta": {"content": "c", "reasoning_content": "r"}}', snapshot)
    assert [e.kind for e in delta] == [EventKind.CONTENT, EventKind.REASONING]
    # delta 里空串 / 非字符串：不产事件
    assert codearts_events.parse_line(
        '{"delta": {"content": "", "reasoning_content": 5}}', snapshot) == []
    usage = codearts_events.parse_line(
        '{"prompt_tokens": 3, "completion_tokens": 4}', snapshot)
    assert usage[0].kind is EventKind.USAGE and usage[0].usage.input_tokens == 3

def test_events_parse_line_openai_chunk_shape():
    """v2 端点实测：标准 OpenAI chunk（delta + finish_reason + usage）。"""
    snapshot = codearts_events.TextSnapshot()
    delta = codearts_events.parse_line(
        '{"choices":[{"index":0,"delta":{"reasoning_content":"r"},'
        '"finish_reason":null}],"usage":null}', snapshot)
    assert [e.kind for e in delta] == [EventKind.REASONING]
    assert delta[0].content == "r"

    merged = codearts_events.parse_line(
        '{"choices":[{"delta":{"content":"好"},"finish_reason":"stop"}]}', snapshot)
    assert [e.kind for e in merged] == [EventKind.CONTENT, EventKind.FINISH]
    assert merged[0].content == "好" and merged[1].finish_reason == "stop"

    # 收尾帧 choices 为空、usage 单独给出 → 仍要保留 usage
    tail = codearts_events.parse_line(
        '{"choices":[],"usage":{"prompt_tokens":35,"completion_tokens":45}}', snapshot)
    assert [e.kind for e in tail] == [EventKind.USAGE]
    assert tail[0].usage.input_tokens == 35 and tail[0].usage.output_tokens == 45

    # 空 delta / 非对象 choice / 空 tool_calls：不产内容事件
    assert codearts_events.parse_line(
        '{"choices":[{"delta":{"content":""}}]}', snapshot) == []
    assert codearts_events.parse_line(
        '{"choices":[5,{"delta":null}]}', snapshot) == []
    assert codearts_events.parse_line(
        '{"choices":[{"delta":{"tool_calls":[]}}]}', snapshot) == []
    tool = codearts_events.parse_line(
        '{"choices":[{"delta":{"tool_calls":[{"id":"t"}]}}]}', snapshot)
    assert tool[0].kind is EventKind.TOOL_CALLS and tool[0].tool_calls == [{"id": "t"}]

def test_events_parse_line_done_sentinel():
    """`data:[DONE]` 是流结束哨兵（非 JSON），必须识别而不是当坏 JSON 抛错。"""
    snapshot = codearts_events.TextSnapshot()
    events = codearts_events.parse_line("[DONE]", snapshot)
    assert len(events) == 1
    assert events[0].kind is EventKind.FINISH and events[0].finish_reason == "stop"
    # `iter_data_lines` 要原样把它吐出来（不能被当成无载荷行丢掉）
    assert codearts_events._data_payload("data:[DONE]") == "[DONE]"  # noqa: SLF001

def test_events_parse_line_embedded_error_code():
    snapshot = codearts_events.TextSnapshot()
    events = codearts_events.parse_line(
        '{"text": "[DONE]", "error_code": "ChatAgent.00001001", "error_msg": "busy"}',
        snapshot)
    assert len(events) == 1 and events[0].kind is EventKind.ERROR
    assert events[0].error_message == "ChatAgent.00001001 busy"
    # 无 error_msg：只留原始码
    only_code = codearts_events.parse_line(
        '{"error_code": "ChatAgent.00001001"}', snapshot)
    assert only_code[0].error_message == "ChatAgent.00001001"

def test_events_parse_line_bad_payload_raises():
    snapshot = codearts_events.TextSnapshot()
    with pytest.raises(codearts_events.UpstreamProtocolViolation, match="unparsable"):
        codearts_events.parse_line("{not json", snapshot)
    with pytest.raises(codearts_events.UpstreamProtocolViolation, match="not an object"):
        codearts_events.parse_line("[1, 2]", snapshot)

def test_events_usage_tolerates_bad_types():
    snapshot = codearts_events.TextSnapshot()
    assert codearts_events.parse_line('{"prompt_tokens": true}', snapshot) == []
    usage = codearts_events.parse_line('{"prompt_tokens": 1}', snapshot)[0]
    assert usage.usage.input_tokens == 1 and usage.usage.output_tokens is None

def test_usage_object_reads_details_fields():
    """上游实测把 cached/reasoning 放在 details 里（TECHNICAL §5.4）。"""
    usage = _v2_usage({
        "prompt_tokens": 35, "completion_tokens": 45,
        "prompt_tokens_details": {"cached_tokens": 12},
        "completion_tokens_details": {"reasoning_tokens": 30},
    })
    assert (usage.input_tokens, usage.output_tokens) == (35, 45)
    assert usage.cached_tokens == 12
    assert usage.reasoning_tokens == 30

def test_usage_object_details_zero_is_a_reported_value():
    """未命中时上游报 0：0 是有效值，必须与「未上报」区分开。"""
    usage = _v2_usage({
        "prompt_tokens": 35,
        "prompt_tokens_details": {"cached_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 0},
    })
    assert usage.cached_tokens == 0
    assert usage.reasoning_tokens == 0

def test_usage_object_invalid_details_falls_back_to_top_level():
    """details 值非法（bool / 字符串）时回落顶层同名字段。"""
    usage = _v2_usage({
        "prompt_tokens": 35,
        "cached_tokens": 7,
        "reasoning_tokens": True,              # bool 非法且顶层无 → None
        "prompt_tokens_details": {"cached_tokens": "12"},
        "completion_tokens_details": {"reasoning_tokens": False},
    })
    assert usage.cached_tokens == 7              # 字符串非法 → 顶层 7
    assert usage.reasoning_tokens is None

def test_usage_object_missing_details_keeps_none():
    """details 整缺：无上报就诚实为 None，不拿 0 冒充。"""
    usage = _v2_usage({"prompt_tokens": 35, "completion_tokens": 45,
                       "prompt_tokens_details": "not-an-object"})
    assert usage.cached_tokens is None
    assert usage.reasoning_tokens is None

def test_events_classify_error_code_branches():
    classify = codearts_events.classify_error_code
    assert classify(None) is ErrKind.OTHER
    assert classify("0") is ErrKind.OTHER
    assert classify("   ") is ErrKind.OTHER
    assert classify("InferHub.002002009.404") is ErrKind.BLOCKED
    assert classify("InferHub.4004.200 benefit not found") is ErrKind.BLOCKED
    assert classify("TM.00001041") is ErrKind.CONCURRENCY
    assert classify("tpm limit") is ErrKind.MODEL
    assert classify("1005 quota") is ErrKind.PLAN
    assert classify("APIG.0301 decrypt token fail") is ErrKind.DEAD
    assert classify("ChatAgent.99999999") is ErrKind.OTHER

def test_events_classify_request_marker_is_not_credential_fault():
    """`InferHub.001001020.406 not multimodal` 是请求侧错误（实测原文）。

    判成 OTHER 会让正常用户带图请求 3 次后把整条渠道拉进 10min 冷却；
    REQUEST 不冷却、不累计、不落库，仍换号重试。
    """
    classify = codearts_events.classify_error_code
    assert classify("InferHub.001001020.406") is ErrKind.REQUEST
    assert classify(
        "InferHub.001001020.406 The request model is not multimodal"
    ) is ErrKind.REQUEST
    assert classify("The request model is not multimodal") is ErrKind.REQUEST

def test_events_relogin_required_markers():
    """令牌端点终态判定：只有点名的一次性票据失效类错误才算不可自愈。"""
    burned = codearts_events.relogin_required(
        b'{"error_code":"STS5.1806","error_msg":"invalid refresh token: '
        b"\'the refresh token has been used\'\"}")
    assert burned == "刷新令牌已失效，需重新登录"
    for body in (b"invalid client id", b"InvalidDPoPHeader", b"invalid_grant"):
        assert codearts_events.relogin_required(body)
    # 可重试的失败不得判终态：否则一次偶发 5xx 会永久废掉一条好凭证
    for body in (b"", b"internal server error", b"too many requests"):
        assert codearts_events.relogin_required(body) == ""

def test_events_classify_status_sts_expired_is_soft():
    """`APIG.0602`（STS 临时凭证过期）走短冷却，不落 INVALID 也不硬禁用。

    INVALID 会让客户端收到 `model not available on any configured upstream`
    （实测 84 条 APIG.0602 全走这条），DEAD 会硬禁用从而断掉 RefreshTask
    唯一的自愈路径——只有短冷却才对：既挡住对废凭证的连续打，又保留接管权。
    """
    classify = codearts_events.classify_status
    body = b'{"error_msg":"Bad request: the security token has expired","error_code":"APIG.0602"}'
    assert classify(400, body) is ErrKind.SOFT
    assert classify(400, b"the security token has expired") is ErrKind.SOFT
    # 普通 400 仍是 INVALID（不回归原有语义）
    assert classify(400, b"bad model param") is ErrKind.INVALID

def test_events_classify_status_branches():
    classify = codearts_events.classify_status
    assert classify(402, b"") is ErrKind.CREDIT
    assert classify(200, b'{"code": 14018}') is ErrKind.CREDIT
    assert classify(200, b'{"code": 1005}') is ErrKind.PLAN
    assert classify(400, b'{"code": 11102}') is ErrKind.BLOCKED
    assert classify(404, b"model is not registered") is ErrKind.BLOCKED
    assert classify(401, b"") is ErrKind.DEAD
    assert classify(404, b"") is ErrKind.SOFT
    assert classify(429, b"00001041") is ErrKind.CONCURRENCY
    assert classify(429, b"slow down") is ErrKind.SOFT
    # 并发会话超限实测走 HTTP 400（不是 429）：必须判成可重试的 CONCURRENCY，
    # 否则落 INVALID → 换号也没用、还不冷却（77% 失败率主因之一）；
    # 且不能套 MODEL 的翻倍退避（一次打满锁 10min 太久，在途排空即恢复）。
    throttle_body = ('{"error_code":"TM.00001041",'
                     '"error_msg":"并发会话数已达上限(3个)，请关闭部分会话后重试。"}'
                     ).encode()
    assert classify(400, throttle_body) is ErrKind.CONCURRENCY
    assert classify(400, b"rate limit exceeded") is ErrKind.MODEL
    assert classify(400, b"bad request") is ErrKind.INVALID
    assert classify(500, b"boom") is ErrKind.OTHER

def test_events_parse_line_skips_structural_noise():
    """心跳 / 被截断的裸括号行跳过，而不是让整条响应以 unparsable 失败。"""
    snapshot = codearts_events.TextSnapshot()
    assert codearts_events.parse_line("{", snapshot) == []
    assert codearts_events.parse_line("  [  ", snapshot) == []
    assert codearts_events.parse_line("", snapshot) == []
    # 噪声跳过不吞真载荷
    assert codearts_events.parse_line('{"text": "hi"}', snapshot)[0].content == "hi"

async def test_events_iter_data_lines_skips_non_data_lines_only():
    async def gen():
        yield b"event: ping\n"
        yield b": comment\n"
    assert [line async for line in codearts_events.iter_data_lines(gen())] == []


def test_parse_line_attaches_upstream_reported_model():
    """CodeArts 载荷带 model 时挂到事件上（观测用，不改语义）。"""
    snapshot = codearts_events.TextSnapshot()
    events = codearts_events.parse_line(json.dumps({
        "model": "DeepSeek-V4.1",
        "choices": [{"delta": {"content": "hi"}}],
    }), snapshot)
    assert events[0].content == "hi"
    assert events[0].model == "DeepSeek-V4.1"
    # 不报模型时保持 None
    plain = codearts_events.parse_line('{"text": "hi"}', snapshot)
    assert plain[0].model is None

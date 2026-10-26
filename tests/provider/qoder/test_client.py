"""Qoder 上游客户端：请求体、推理流、模型发现、额度与刷新。"""

from __future__ import annotations

import hashlib
import json
import time

import httpx
import pytest

from src.provider.base import ErrKind, EventKind
from src.provider.qoder import client as qoder_client
from src.provider.qoder import events as qoder_events
from src.provider.qoder.client import (
    QoderClient,
    UpstreamHTTPError,
    build_chat_body,
    build_openapi_headers,
    realm_for,
    session_dead,
)
from src.provider.qoder.cosy import derive_id, qoder_decode, qoder_encode, sign_path
from src.provider.qoder.events import UpstreamProtocolViolation
from tests.provider.qoder._support import OPENAPI, cred, envelope, handler_for, inner, sse


def test_build_chat_body_maps_fields_and_is_stream_only():
    payload = {"messages": [{"role": "user", "content": "hi"}], "stream": False,
               "temperature": 0.5, "max_tokens": 128, "reasoning_effort": "high",
               "tools": [{"type": "function"}], "top_p": 0.9}
    body = build_chat_body(payload, "qmodel_38max")
    assert body["stream"] is True and body["model"] == "qmodel_38max"
    assert body["agent_id"] == "agent_common"
    assert body["parameters"] == {"max_tokens": 128, "reasoning_effort": "high"}
    assert body["tools"] == [{"type": "function"}] and body["top_p"] == 0.9
    assert body["messages"] == payload["messages"]
    # 深拷贝：改写上游 body 不影响引擎持有的请求原文
    body["messages"][0]["content"] = "changed"
    assert payload["messages"][0]["content"] == "hi"

def test_build_chat_body_empty_and_alternate_effort_shapes():
    body = build_chat_body({}, "m")
    assert body["messages"] == [] and "parameters" not in body
    nested = build_chat_body({"reasoning": {"effort": "low"},
                              "max_completion_tokens": 10}, "m")
    assert nested["parameters"] == {"max_tokens": 10, "reasoning_effort": "low"}
    assert "parameters" not in build_chat_body({"max_tokens": 0, "tools": None}, "m")

def test_build_chat_body_keeps_well_formed_tool_sequence(caplog):
    """user → assistant(tool_calls) → tool 是合法序列，原样转发且不记 warning。"""
    messages = [
        {"role": "user", "content": "查天气"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_a", "type": "function",
             "function": {"name": "weather", "arguments": ""}}]},
        {"role": "tool", "tool_call_id": "call_a", "content": "晴"},
    ]
    with caplog.at_level("WARNING", logger=qoder_client.__name__):
        body = build_chat_body({"messages": messages}, "m")
    assert body["messages"] == messages
    assert "孤立 tool" not in caplog.text

def test_build_chat_body_drops_orphan_tool_and_keeps_order(caplog):
    """前面没有带 tool_calls 的 assistant：孤立 tool 丢弃，其余顺序不变。"""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "call_a", "content": "孤儿结果"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "再来"},
    ]
    with caplog.at_level("WARNING", logger=qoder_client.__name__):
        body = build_chat_body({"messages": messages}, "m")
    assert body["messages"] == [messages[0], messages[2], messages[3]]
    assert any("孤立 tool 消息" in r.getMessage() and "1" in r.getMessage()
               for r in caplog.records)

def test_build_chat_body_drops_tool_with_unmatched_id():
    """assistant 声明 id=a，tool 回 id=b：id 对不上也算孤立。"""
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_a", "type": "function", "function": {"name": "f"}}]},
        {"role": "tool", "tool_call_id": "call_b", "content": "错配"},
        {"role": "tool", "content": "缺 tool_call_id"},
        {"role": "tool", "tool_call_id": "call_a", "content": "对上了"},
    ]
    body = build_chat_body({"messages": messages}, "m")
    assert body["messages"] == [messages[0], messages[3]]

def test_build_chat_body_keeps_parallel_tool_results():
    """并行 tool_calls 的多条 tool 结果都跟在同一个 assistant 后，全部保留。"""
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "f"}},
            {"id": "b", "type": "function", "function": {"name": "g"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "1"},
        {"role": "tool", "tool_call_id": "b", "content": "2"},
    ]
    body = build_chat_body({"messages": messages}, "m")
    assert body["messages"] == messages

def test_build_chat_body_unions_consecutive_assistant_tool_calls():
    """`/v1/responses` 的并行 function_call 拆成多条 assistant，声明需并集。"""
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "f"}}]},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "b", "type": "function", "function": {"name": "g"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "1"},
        {"role": "tool", "tool_call_id": "b", "content": "2"},
    ]
    body = build_chat_body({"messages": messages}, "m")
    assert body["messages"] == messages

def test_build_chat_body_normalizes_null_assistant_content():
    """带 tool_calls 的 assistant 的 `content: null` 归一为空串。

    上游把 null content 的 assistant 当成空消息跳过，其后的 tool 结果就成了
    孤儿，并以 400「Messages with role 'tool' must be a response to a preceding
    message with 'tool_calls'」拒绝——报文指向 tool 配对，真实原因是 null
    content（2026-10-23 实测：只把 null 换成 "" 即从 400 变 200）。
    """
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "reasoning_content": "想想",
         "tool_calls": [{"id": "a", "type": "function", "function": {"name": "f"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "结果"},
    ]
    body = build_chat_body({"messages": messages}, "m")
    assert body["messages"][1]["content"] == ""
    assert body["messages"][1]["reasoning_content"] == "想想"
    assert body["messages"][2] == messages[2]
    # 入参不被改写（build_chat_body 深拷贝）
    assert messages[1]["content"] is None

def test_build_chat_body_normalizes_null_tool_content(caplog):
    """tool 消息的 null content 同样归一，且计入日志条数。"""
    messages = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "f"}}]},
        {"role": "tool", "tool_call_id": "a", "content": None},
        {"role": "assistant", "content": None},
    ]
    with caplog.at_level("INFO", logger=qoder_client.__name__):
        body = build_chat_body({"messages": messages}, "m")
    assert [m["content"] for m in body["messages"]] == ["", "", ""]
    assert any("null content" in r.getMessage() and "3" in r.getMessage()
               for r in caplog.records)

def test_build_chat_body_leaves_other_roles_content_untouched():
    """只归一 assistant/tool：user/system 的 null 原样交给上游裁决。"""
    messages = [
        {"role": "system", "content": None},
        {"role": "user", "content": None},
        {"role": "assistant", "content": "有正文"},
        {"role": "junk", "content": None},
    ]
    body = build_chat_body({"messages": messages}, "m")
    assert [m["content"] for m in body["messages"]] == [None, None, "有正文", None]

def test_normalize_null_content_skips_non_dict_entries():
    """畸形历史（非 dict 消息）跳过，不改写也不计数。"""
    messages = ["junk", {"role": "assistant", "content": None}]
    assert qoder_client._normalize_null_content(messages) == 1
    assert messages == ["junk", {"role": "assistant", "content": ""}]

def test_build_chat_body_plain_assistant_resets_declared_ids():
    """中间夹了一条不带 tool_calls 的 assistant：它打断了配对，其后的 tool 孤立。"""
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "f"}}]},
        {"role": "assistant", "content": "我先说一句"},
        {"role": "tool", "tool_call_id": "a", "content": "晚到的结果"},
    ]
    body = build_chat_body({"messages": messages}, "m")
    assert body["messages"] == [messages[0], messages[1]]

def test_build_chat_body_tool_after_empty_tool_calls_is_orphan():
    """`tool_calls: []` 的 assistant 没有声明任何 id，其后的 tool 视为孤立。"""
    messages = [
        {"role": "assistant", "content": "无调用", "tool_calls": []},
        {"role": "tool", "tool_call_id": "call_a", "content": "孤儿"},
    ]
    body = build_chat_body({"messages": messages}, "m")
    assert body["messages"] == [messages[0]]

def test_drop_orphan_tool_messages_tolerates_junk_entries():
    """畸形历史（非 dict 消息、畸形 tool_calls 元素）不参与匹配，但原样保留。"""
    messages = [
        "junk",
        {"role": "assistant", "content": "", "tool_calls": [
            "junk", {"id": 1}, {"id": ""}, {"id": "call_a"}]},
        {"role": "tool", "tool_call_id": "call_a", "content": "ok"},
        {"role": "tool", "tool_call_id": "call_b", "content": "孤儿"},
        {"role": "tool", "content": "缺 tool_call_id"},
        {"role": "tool", "tool_call_id": ["unhashable"], "content": "畸形 id"},
    ]
    kept, dropped = qoder_client._drop_orphan_tool_messages(messages)
    assert kept == [messages[0], messages[1], messages[2]]
    assert dropped == 3

def test_build_chat_body_passes_through_non_list_messages():
    """畸形 payload（messages 非数组）不做结构假设，原样交给上游裁决。"""
    body = build_chat_body({"messages": {"role": "tool"}}, "m")
    assert body["messages"] == {"role": "tool"}

def test_openapi_headers_use_stable_device_ids():
    headers = build_openapi_headers(cred())
    assert headers["Authorization"] == "Bearer dt-token-1"
    assert headers["X-Machine-ID"] == derive_id("u-1", "machine")
    assert headers["X-Session-ID"] == derive_id("u-1", "session")
    assert headers["User-Agent"] == qoder_events.CLIENT_UA
    assert headers["Origin"].startswith("https://qoder.cn")

def test_realm_for_and_session_dead_helpers():
    assert realm_for(cred()) == "cn"
    assert realm_for(cred(realm="", domain="qoder.sh")) == "intl"
    assert realm_for(cred(realm="", domain="")) == "cn"
    assert session_dead('{"error":"TOKEN_EXPIRE"}') is True
    assert session_dead("Offline user session not found") is True
    assert session_dead("12153") is True
    assert session_dead("ok") is False

async def test_stream_chat_decodes_envelope_and_done():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/algo/api/v2/service/pro/sse/agent_chat_generation"
        assert request.headers["authorization"].startswith("Bearer COSY.")
        assert request.headers["x-model-key"] == "qmodel_38max"
        # 请求体是自定义 Base64，解码后是带 stream=true 的 JSON
        body = json.loads(qoder_decode(request.content.decode("ascii")))
        assert body["stream"] is True and body["model"] == "qmodel_38max"
        return httpx.Response(200, text=sse(
            inner({"role": "assistant", "reasoning_content": "why"}),
            inner({"content": "hi"}),
            inner({"content": "!"}, finish="stop",
                  usage={"prompt_tokens": 3, "completion_tokens": 2}),
            inner({}, choices=[], usage={"prompt_tokens": 5}),
        ))

    events = [e async for e in handler_for(handler).stream_chat(
        cred(), {"messages": [{"role": "user", "content": "x"}]}, "qmodel_38max")]
    kinds = [e.kind for e in events]
    # 与 codebuddy/zen 的 parse_all_events 同序：同一帧先 usage 后 finish
    assert kinds == [EventKind.REASONING, EventKind.CONTENT, EventKind.CONTENT,
                     EventKind.USAGE, EventKind.FINISH, EventKind.USAGE]
    assert events[1].content == "hi"
    assert events[3].usage.input_tokens == 3 and events[3].usage.output_tokens == 2
    assert events[4].finish_reason == "stop"

async def test_stream_chat_inner_error_frame_is_classified():
    """`statusCodeValue != 200` 是流内错误（HTTP 仍 200）。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=(
            envelope(inner({"content": "part"}))
            + envelope("upstream boom", status=418)
            + envelope("[DONE]")))

    events = [e async for e in handler_for(handler).stream_chat(
        cred(), {"messages": []}, "m")]
    assert [e.kind for e in events] == [EventKind.CONTENT, EventKind.ERROR]
    assert events[1].error_code == 418
    assert events[1].error_kind is ErrKind.SOFT

async def test_stream_chat_queue_envelope_yields_queue_event():
    """排队信封（403 + `isQueued`）必须归 QUEUE。

    按状态码分类会把 403 当事务级失败硬禁用凭证，而排队恰恰说明号是好的；
    上游点名的等待秒数要原样带给 executor（原号重开）。
    """
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=envelope(
            {"isQueued": True, "retryAfterSeconds": 6}, status=403))

    events = [e async for e in handler_for(handler).stream_chat(cred(), {}, "m")]
    assert len(events) == 1
    assert events[0].kind is EventKind.ERROR
    assert events[0].error_kind is ErrKind.QUEUE
    assert events[0].error_code == 403
    assert events[0].retry_after_seconds == 6


async def test_inner_error_message_keeps_nested_details():
    """错误正文放宽到 2000：200 字符会截在转义的 details 中间，根因丢失。"""
    detail = json.dumps({"error": {"message": "Messages with role 'tool' must be a "
                                             "response to a preceding message with "
                                             "'tool_calls'", "padding": "y" * 1400}})
    body = json.dumps({"code": "400", "details": detail})
    assert 200 < len(body) < 2000

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=envelope(body, status=400))

    events = [e async for e in handler_for(handler).stream_chat(cred(), {}, "m")]
    assert events[0].error_message == body
    assert "preceding message with 'tool_calls'" in events[0].error_message

async def test_stream_chat_top_level_done_and_blank_frames():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="data: \n\ndata: [DONE]\n\n")

    events = [e async for e in handler_for(handler).stream_chat(cred(), {}, "m")]
    assert events == []

async def test_stream_chat_skips_keepalive_null_body():
    """上游保活帧 `body:"null"`（外层 200）必须跳过，不能打断正常输出。

    实测（2026-10-26 16:29-16:53）：上游在流中发
    `{"headers":…,"body":"null","statusCodeValue":200,"statusCode":"OK"}`。
    早先把它当「内层不是对象」→ 抛 `UpstreamProtocolViolation` → 整条流
    判失败（连续 13 次「流式响应失败: Qoder inner chunk is not an object」），
    而模型其实已在正常输出。这正是「静默出错」：客户端只看到回复被截断。
    """
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=(
            envelope(inner({"content": "part"}))
            + envelope("null")
            + envelope("null")
            + envelope(inner({"content": "more"}))
            + envelope("[DONE]")))

    events = [e async for e in handler_for(handler).stream_chat(cred(), {}, "m")]
    assert [e.kind for e in events] == [EventKind.CONTENT, EventKind.CONTENT]
    assert "".join(e.content or "" for e in events) == "partmore"

async def test_stream_chat_keeps_content_after_empty_object_body():
    """空对象 / 空数组内层同样是保活：跳过但不终止流。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=(
            envelope("{}")
            + envelope("[]")
            + envelope(inner({"content": "ok"}))
            + envelope("[DONE]")))

    events = [e async for e in handler_for(handler).stream_chat(cred(), {}, "m")]
    assert [e.kind for e in events] == [EventKind.CONTENT]
    assert events[0].content == "ok"

def test_is_keepalive_body_shape_table():
    """保活判据用结构而非字符串前缀：非空对象 / 坏 JSON 都不是保活。"""
    assert qoder_events.is_keepalive_body("null") is True
    assert qoder_events.is_keepalive_body("{}") is True
    assert qoder_events.is_keepalive_body("[]") is True
    assert qoder_events.is_keepalive_body("  null  ") is True
    assert qoder_events.is_keepalive_body('{"choices":[]}') is False
    assert qoder_events.is_keepalive_body('{"code":"403"}') is False
    assert qoder_events.is_keepalive_body("nope") is False
    assert qoder_events.is_keepalive_body('"text"') is False
    assert qoder_events.is_keepalive_body("0") is False

async def test_stream_chat_queue_envelope_from_real_upstream_frame():
    """上游真实排队帧（2026-10-26 抓取）：排队标记在**两层序列化字符串**里。

    早先 `parse_queue_hint` 只认 dict/list，漏判 → 403 被 `classify_error_code`
    判成 DEAD → **好凭证被硬禁用**。这里守两件事：归 QUEUE、且拿到上游点名的
    等待秒数（最内层的 `retryAfterSeconds:30`，不是兜底 5s）。
    """
    inner_payload = json.dumps({
        "isQueued": True, "modelKey": "kmodel_latest", "queueCount": 454,
        "queueType": "p3", "retryAfterSeconds": 30, "serviceAvailable": True,
        "subCode": "RESOURCE_LIMIT", "waitTime": 205})
    body = json.dumps({
        "code": "403",
        "message": json.dumps({
            "code": "10605",
            "message": inner_payload,
            "subCode": "RESOURCE_LIMIT"})})

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=envelope(body, status=403))

    events = [e async for e in handler_for(handler).stream_chat(cred(), {}, "m")]
    assert len(events) == 1
    assert events[0].kind is EventKind.ERROR
    assert events[0].error_kind is ErrKind.QUEUE
    assert events[0].error_code == 403
    # 上游点名 30s：不是兜底值 5s（兜底只用于「上游没给秒数」）
    assert events[0].retry_after_seconds == 30.0

def test_classify_status_recognizes_serialized_queue_envelope():
    """HTTP 层（`classify_status`）同样要认出串了两层的排队报文。"""
    inner_payload = json.dumps({"isQueued": True, "retryAfterSeconds": 30,
                                "subCode": "RESOURCE_LIMIT"})
    body = json.dumps({"code": "403",
                       "message": json.dumps({"code": "10605",
                                              "message": inner_payload})})
    # 排队优先于状态码：403 不是硬失败
    assert qoder_events.classify_status(403, body.encode()) is ErrKind.QUEUE
    # 排队报文绝不能落到 DEAD（会把可自愈的凭证打进冷宫）
    assert qoder_events.classify_error_code(403, body) is not ErrKind.DEAD

async def test_stream_chat_http_error_is_classified():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b"slow")

    with pytest.raises(UpstreamHTTPError) as caught:
        [e async for e in handler_for(handler).stream_chat(cred(), {}, "m")]
    assert caught.value.kind() is ErrKind.MODEL

async def test_stream_chat_falls_back_to_second_gateway_on_transport_error():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "gw1.test":
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, text=sse(inner({"content": "ok"}, finish="stop")))

    client = handler_for(handler, gateway_fallbacks=("https://gw1.test",
                                                     "https://gw2.test"))
    events = [e async for e in client.stream_chat(cred(), {}, "m")]
    assert [e.kind for e in events] == [EventKind.CONTENT, EventKind.FINISH]
    assert seen == ["gw1.test", "gw2.test"]
    await client.aclose()

async def test_stream_chat_reraises_transport_error_on_last_gateway():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = handler_for(handler, gateway_fallbacks=("https://gw1.test",))
    with pytest.raises(httpx.ConnectError):
        [e async for e in client.stream_chat(cred(), {}, "m")]
    await client.aclose()

async def test_decoded_envelope_protocol_violation_propagates():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="data: {not json}\n\n")

    with pytest.raises(UpstreamProtocolViolation):
        [e async for e in handler_for(handler).stream_chat(cred(), {}, "m")]

async def test_fetch_models_signs_get_and_sends_no_body():
    """模型清单端点只接受 GET：带 COSY 签名头、请求体为空。

    实测（2026-09-30）带头 POST 会被上游 400「Request method 'POST' not
    supported」拒绝，故断言方法为 GET 且无请求体。
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["body"] = request.content
        seen["auth"] = request.headers["authorization"]
        seen["key"] = request.headers["cosy-key"]
        seen["date"] = request.headers["cosy-date"]
        seen["path"] = request.url.path
        assert "x-model-key" not in request.headers       # 清单请求不带 x-model-key
        return httpx.Response(200, json={"chat": [
            {"key": "qmodel_38max", "display_name": "Qwen3.8-Max",
             "is_vl": True, "supportsToolCall": True, "is_reasoning": True,
             "max_input_tokens": 180000},
            {"key": ""},                                    # 空 key 跳过
            "junk",                                         # 非 dict 跳过
            {"key": "dmodel", "display_name": "DeepSeek"},
        ]})

    client = handler_for(handler)
    models = await client.fetch_models(cred())
    sign_body = qoder_encode(qoder_events.MODELS_SIGN_PLAIN)
    assert seen["method"] == "GET"
    assert seen["body"] == b""                           # GET 无请求体
    assert seen["key"] == client.sessions.get(cred()).cosy_key
    expected = hashlib.md5("\n".join([
        seen["auth"].split(".")[1], seen["key"], seen["date"], sign_body,
        sign_path(seen["path"])]).encode()).hexdigest()
    assert seen["auth"] == f"Bearer COSY.{seen['auth'].split('.')[1]}.{expected}"
    assert [m.id for m in models] == ["qmodel_38max", "dmodel"]
    assert models[0].name == "Qwen3.8-Max" and models[0].supports_images is True
    assert models[0].max_input_tokens == 180000
    # 缓存：同区域 10 分钟内不再打上游
    called = {"n": 0}

    def counting(_request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"chat": [{"key": "m2"}]})

    client2 = handler_for(counting)
    client2._model_cache["cn"] = (time.time(), models)   # noqa: SLF001
    assert await client2.fetch_models(cred()) == models
    assert called["n"] == 0
    await client.aclose()
    await client2.aclose()

async def test_fetch_models_errors_and_host_fallback():
    def http_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"forbidden")

    with pytest.raises(UpstreamHTTPError) as caught:
        await handler_for(http_error).fetch_models(cred())
    assert caught.value.kind() is ErrKind.SOFT

    def not_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    with pytest.raises(UpstreamProtocolViolation, match="model list request failed"):
        await handler_for(not_json).fetch_models(cred())

    def not_object(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2])

    with pytest.raises(UpstreamProtocolViolation, match="not an object"):
        await handler_for(not_object).fetch_models(cred())

    def no_models(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"chat": []})

    with pytest.raises(UpstreamProtocolViolation, match="no usable models"):
        await handler_for(no_models).fetch_models(cred())

    # 第一台主机传输失败 → 换备用主机
    def flaky(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gw1.test":
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"chat": [{"key": "m"}]})

    client = handler_for(flaky, gateway_fallbacks=("https://gw1.test",
                                                   "https://gw2.test"))
    assert [m.id for m in await client.fetch_models(cred())] == ["m"]
    await client.aclose()

    # 所有主机都传输失败 → UpstreamTransportError（可翻译成网络不可达），
    # 而不是协议违规（「响应格式不符」会误导用户去查上游改版）
    def all_down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    from src.provider.base import UpstreamTransportError

    with pytest.raises(UpstreamTransportError):
        await handler_for(all_down, gateway_fallbacks=("https://gw1.test",
                                                       "https://gw2.test")).fetch_models(cred())

async def test_probe_quota_aggregates_base_and_addon():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == qoder_events.EP_QUOTA
        assert request.headers["Authorization"] == "Bearer dt-token-1"
        return httpx.Response(200, json={
            "userQuota": {"total": 100, "used": 40, "remaining": 60},
            "addOnQuota": {"total": 50, "used": 10, "remaining": 40},
            "expiresAt": 2000000000})

    quota = await handler_for(handler).probe_quota(cred())
    assert quota.total == 150 and quota.remaining == 100
    assert quota.cycle_end == 2000000000
    assert [p["name"] for p in quota.packages] == ["基础额度", "赠送额度"]

async def test_probe_quota_http_error_and_bad_shape():
    def http_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b'{"error":"TOKEN_EXPIRE"}')

    with pytest.raises(UpstreamHTTPError) as caught:
        await handler_for(http_error).probe_quota(cred())
    assert caught.value.kind() is ErrKind.DEAD

    def bad_shape(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="nope")

    with pytest.raises(UpstreamProtocolViolation):
        await handler_for(bad_shape).probe_quota(cred())

async def test_refresh_token_rotates_and_preserves_identity():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == qoder_events.EP_DEVICE_REFRESH
        assert "Authorization" not in request.headers     # 刷新不带旧 token
        assert json.loads(request.content) == {"refresh_token": "drt-1"}
        return httpx.Response(200, json={"token": "dt-2", "refresh_token": "drt-2",
                                         "expires_in": 3_600_000})

    client = handler_for(handler)
    session = client.sessions.get(cred())
    refreshed = await client.refresh_token(cred())
    assert refreshed["access_token"] == "dt-2"
    assert refreshed["refresh_token"] == "drt-2"
    assert refreshed["uid"] == "u-1" and refreshed["nickname"] == "Tester"
    assert refreshed["expires_at"] > 0
    assert client.sessions.get(cred(access_token="dt-2")) is not session
    await client.aclose()

async def test_refresh_token_requires_refresh_token_and_result():
    def no_token(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"refresh_token": "drt-x"})

    with pytest.raises(UpstreamProtocolViolation, match="missing refresh_token"):
        await handler_for(no_token).refresh_token(cred(refresh_token=""))

    def empty(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"refresh_token": "drt-x"})

    with pytest.raises(UpstreamProtocolViolation, match="returned no token"):
        await handler_for(empty).refresh_token(cred(access_token=""))

    def dead(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b"TOKEN_EXPIRE")

    with pytest.raises(UpstreamHTTPError) as caught:
        await handler_for(dead).refresh_token(cred())
    assert caught.value.status == 401

async def test_fetch_userinfo():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == qoder_events.EP_USERINFO
        return httpx.Response(200, json={"id": "u-1", "name": "Tester"})

    assert (await handler_for(handler).fetch_userinfo(cred()))["name"] == "Tester"

def test_classify_error_code_full_table():
    assert qoder_events.classify_error_code(400) is ErrKind.INVALID
    assert qoder_events.classify_error_code(404) is ErrKind.INVALID
    assert qoder_events.classify_error_code(422) is ErrKind.INVALID
    assert qoder_events.classify_error_code(999) is ErrKind.OTHER
    assert qoder_events.classify_error_code(None) is ErrKind.OTHER

def test_node_failure_400_is_model_scoped_transient():
    """上游把自身节点故障包成 400：归模型级瞬时故障，不能当「模型不存在」。"""
    body = ('{"code":"400","message":"[FAIL]node:oa_qwen-plus-main '
            'msg:Execution failed: null"}')
    assert qoder_events.is_node_failure(body) is True
    assert qoder_events.is_node_failure("model not found") is False

    # 字符串与 bytes 两种入参都支持（信封走 str，HTTP body 走 bytes）
    assert qoder_events.classify_error_code(400, body) is ErrKind.MODEL
    assert qoder_events.classify_error_code(400, body.encode()) is ErrKind.MODEL
    assert qoder_events.classify_error_code(404, body) is ErrKind.MODEL
    # 普通 400（无节点故障标记）仍是 INVALID
    assert qoder_events.classify_error_code(400, "bad request") is ErrKind.INVALID
    assert qoder_events.classify_status(400, body.encode()) is ErrKind.MODEL

def test_classify_status_queue_hint_wins_over_403():
    """排队标记优先于状态码：403 不再当硬失败（会把健康凭证打进冷宫）。"""
    body = b'{"isQueued": true, "retryAfterSeconds": 9}'
    assert qoder_events.classify_status(403, body) is ErrKind.QUEUE
    # 没有排队标记的 403 仍是 SOFT：实测多为瞬时/风控抖动，可自愈
    assert qoder_events.classify_status(403, b"") is ErrKind.SOFT


def test_to_status_variants():
    assert qoder_events._to_status(None) == 200
    assert qoder_events._to_status(True) == 502
    assert qoder_events._to_status(418) == 418
    assert qoder_events._to_status("429") == 429
    assert qoder_events._to_status("nope") == 502
    assert qoder_events._to_status([1]) == 502

def test_gateway_candidates_deduplicates():
    config = qoder_events.RealmConfig(
        name="dup", openapi="o", gateway="https://g", website="w", client_id="c",
        redirect_uri="r", domain="d", user_agent="ua",
        gateway_fallbacks=("https://g", "https://h"))
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setitem(qoder_events.REALM_CONFIGS, "dup", config)
    try:
        assert qoder_events.gateway_candidates("dup") == ["https://g", "https://h"]
    finally:
        monkeypatch.undo()

def test_clean_delta_strips_noise_and_blank_calls():
    cleaned = qoder_events.clean_delta({
        "content": "x", "extra_fields": {}, "refusal": None,
        "reasoning_content": "", "tool_calls": [],
        "function_call": {"name": "", "arguments": ""}})
    assert cleaned == {"content": "x"}
    # 有实际参数/名称的调用必须保留
    kept = qoder_events.clean_delta({
        "function_call": {"name": "f", "arguments": "{}"},
        "tool_calls": [{"id": "1", "function": {"name": "f", "arguments": "{}"}}]})
    assert "function_call" in kept and "tool_calls" in kept

def test_blank_function_call_and_tool_calls_helpers():
    assert qoder_events._is_blank_function_call(None) is True
    assert qoder_events._is_blank_function_call("x") is False
    assert qoder_events._is_blank_function_call({"name": "f", "arguments": ""}) is False
    assert qoder_events._is_blank_function_call({"arguments": {}}) is True
    # _tool_calls：旧式 function_call + 过滤空调用
    calls = qoder_events._tool_calls({
        "tool_calls": ["junk", {"function": {"name": "", "arguments": ""}},
                       {"id": "1", "function": {"name": "f", "arguments": "{}"}}],
        "function_call": {"name": "g", "arguments": "{}"}})
    assert [c["function"]["name"] for c in calls] == ["f", "g"]

def test_first_choice_error_paths():
    # first_choice 已抽到 provider/openai_chunk（多 provider 共用），经 qoder 命名空间仍可直达
    assert qoder_events.first_choice({}) is None
    with pytest.raises(UpstreamProtocolViolation, match="not an array"):
        qoder_events.first_choice({"choices": "x"})
    assert qoder_events.first_choice({"choices": []}) is None
    with pytest.raises(UpstreamProtocolViolation, match="choices\\[0\\]"):
        qoder_events.first_choice({"choices": [1]})

def test_parse_models_requires_chat_list():
    with pytest.raises(UpstreamProtocolViolation, match="missing chat list"):
        qoder_events.parse_models({"chat": "x"})

def test_parse_models_maps_price_factor_to_credit_rate():
    """`price_factor` 即官方「Credit 消耗倍率」，映射为 credit_rate。

    来源：docs.qoder.com/zh/cli/model 的「Credit 消耗倍率」表脚注明说
    「表中倍率来自当前服务端模型列表的 price_factor」。免费模型上游给
    0.0（显示「免费」）；缺失或非数值时留 None，不编造。
    """
    models = qoder_events.parse_models({"chat": [
        {"key": "qmodel_38max", "display_name": "Qwen3.8-Max", "price_factor": 0.2},
        {"key": "qfmodel", "display_name": "Qwen3.8-Flash", "price_factor": 0.0},
        {"key": "nofactor"},                          # 无 price_factor → None
        {"key": "badfactor", "price_factor": "x"},    # 非数值 → None
        {"key": "boolfactor", "price_factor": True},  # 布尔不算数值 → None
    ]})
    by_id = {m.id: m for m in models}
    assert by_id["qmodel_38max"].credit_rate == 0.2
    assert by_id["qfmodel"].credit_rate == 0.0        # 免费
    assert by_id["nofactor"].credit_rate is None
    assert by_id["badfactor"].credit_rate is None
    assert by_id["boolfactor"].credit_rate is None

def test_usage_cached_tokens_fallback():
    usage = qoder_events._usage({"prompt_tokens": 5, "cached_tokens": 2})
    assert usage.cached_tokens == 2 and usage.input_tokens == 5
    # details 里的 cached_tokens 优先；非 int 回落到顶层
    usage2 = qoder_events._usage({"prompt_tokens_details": {"cached_tokens": True},
                                  "cached_tokens": 7})
    assert usage2.cached_tokens == 7

def test_opt_helpers_edges():
    assert qoder_events._opt_float(True) is None
    assert qoder_events._opt_float(1) == 1.0
    assert qoder_events._opt_epoch(True) is None
    assert qoder_events._opt_epoch(0) is None
    assert qoder_events._opt_epoch(2000000000) == 2000000000
    assert qoder_events._opt_bool(1) is None and qoder_events._opt_bool(True) is True

def test_client_gateway_hosts_and_fallbacks(monkeypatch):
    client = QoderClient(host=OPENAPI, gateway="https://api1.qoder.sh")
    assert client._gateway_hosts(cred(realm="intl")) == [        # noqa: SLF001
        "https://api1.qoder.sh", "https://api2.qoder.sh", "https://api3.qoder.sh"]
    # client gateway 覆盖区域主选 → 替换首项
    override = QoderClient(host=OPENAPI, gateway="https://custom.test")
    assert override._gateway_hosts(cred(realm="intl"))[0] == "https://custom.test"  # noqa: SLF001
    # 显式 fallbacks 覆盖一切
    forced = QoderClient(host=OPENAPI, gateway="g",
                         gateway_fallbacks=("https://only.test",))
    assert forced._gateway_hosts(cred()) == ["https://only.test"]   # noqa: SLF001
    # 区域无候选主机时回落 client.gateway（防御兜底）
    monkeypatch.setattr(qoder_client, "gateway_candidates", lambda realm: [])
    assert forced._gateway_hosts(cred()) == ["https://only.test"]   # noqa: SLF001
    bare = QoderClient(host=OPENAPI, gateway="https://solo.test")
    assert bare._gateway_hosts(cred()) == ["https://solo.test"]     # noqa: SLF001

async def test_client_parse_json_response_non_object_and_error():
    def non_object(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2])

    with pytest.raises(UpstreamProtocolViolation, match="unexpected response shape"):
        await handler_for(non_object).fetch_userinfo(cred())

def test_client_opt_helpers():
    assert qoder_client._opt_int(True) is None
    assert qoder_client._opt_int(3) == 3
    assert qoder_client._opt_float(True) is None
    assert qoder_client._opt_float(2) == 2.0

async def test_stream_host_switches_on_second_gateway_failure():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host in ("gw1.test", "gw2.test"):
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, text=sse(inner({"content": "ok"}, finish="stop")))

    client = handler_for(handler, gateway_fallbacks=("https://gw1.test",
                                                     "https://gw2.test",
                                                     "https://gw3.test"))
    events = [e async for e in client.stream_chat(cred(), {}, "m")]
    assert [e.kind for e in events] == [EventKind.CONTENT, EventKind.FINISH]
    assert seen == ["gw1.test", "gw2.test", "gw3.test"]
    await client.aclose()

def test_parse_inner_chunk_emits_tool_calls():
    events = qoder_events.parse_inner_chunk(json.dumps({
        "choices": [{"delta": {"tool_calls": [
            {"id": "1", "function": {"name": "f", "arguments": "{}"}}]}}]}))
    assert [e.kind for e in events] == [EventKind.TOOL_CALLS]
    assert events[0].tool_calls[0]["function"]["name"] == "f"

def test_usage_cached_tokens_from_details_preferred():
    usage = qoder_events._usage({"prompt_tokens_details": {"cached_tokens": 4}})
    assert usage.cached_tokens == 4

async def test_stream_chat_succeeds_on_middle_gateway():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "gw1.test":
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, text=sse(inner({"content": "ok"}, finish="stop")))

    client = handler_for(handler, gateway_fallbacks=("https://gw1.test",
                                                     "https://gw2.test",
                                                     "https://gw3.test"))
    events = [e async for e in client.stream_chat(cred(), {}, "m")]
    assert [e.kind for e in events] == [EventKind.CONTENT, EventKind.FINISH]
    assert seen == ["gw1.test", "gw2.test"]                   # 中途主机成功即止
    await client.aclose()

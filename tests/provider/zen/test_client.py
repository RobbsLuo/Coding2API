"""Zen 上游客户端：聊天流、模型发现、额度与惰性连接。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from src.provider.base import ErrKind, EventKind
from src.provider.zen import events as zen_events
from src.provider.zen.client import EP_CHAT, EP_MODELS, UpstreamHTTPError, ZenClient
from tests.helpers.http import chunk_payload as chunk
from tests.helpers.http import sse_text_done as _sse
from tests.provider.zen._support import SESSION_RE
from tests.provider.zen._support import client as _client

pytestmark = pytest.mark.real_upstream("zen")


async def test_stream_chat_yields_named_events():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == EP_CHAT
        assert request.headers["User-Agent"].startswith("opencode/")
        assert SESSION_RE.match(request.headers["x-opencode-session"])
        body = json.loads(request.content)
        assert body["stream"] is True
        names = [t["function"]["name"] for t in body["tools"]]
        assert names == ["bash", "read"]
        return httpx.Response(200, text=_sse(
            chunk({"role": "assistant", "reasoning_content": "r"}),
            chunk({"content": "hi"}),
            chunk({"content": "!"}, finish="stop"),
            chunk({}, choices=[], usage={"prompt_tokens": 1, "completion_tokens": 1}),
        ))

    events = [e async for e in _client(handler).stream_chat(
        {"messages": [{"role": "user", "content": "x"}]}, "big-pickle")]
    kinds = [e.kind for e in events]
    assert kinds == [EventKind.REASONING, EventKind.CONTENT, EventKind.CONTENT,
                     EventKind.FINISH, EventKind.USAGE]

async def test_stream_chat_drops_injected_tool_calls_and_downgrades_finish():
    """模型调用了我们伪造的 bash → 丢弃该 tool_call，finish 收敛为 stop。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse(
            chunk({"tool_calls": [
                {"index": 0, "id": "c1", "type": "function",
                 "function": {"name": "bash", "arguments": ""}}]},
                finish="tool_calls"),
            chunk({}, choices=[], usage={"prompt_tokens": 1}),
        ))

    events = [e async for e in _client(handler).stream_chat({"messages": []}, "m")]
    assert all(e.kind is not EventKind.TOOL_CALLS for e in events)
    finishes = [e for e in events if e.kind is EventKind.FINISH]
    assert finishes and finishes[0].finish_reason == "stop"

async def test_stream_chat_rewrites_injected_bash_to_synonym():
    """客户端有 shell 工具时：模型调伪 bash → 改写成 shell，finish 保持 tool_calls。"""
    shell_tool = {"type": "function",
                  "function": {"name": "shell",
                               "description": "Run a shell command",
                               "parameters": {"type": "object",
                                              "properties": {"command": {"type": "string"}},
                                              "required": ["command"]}}}
    sent: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, text=_sse(
            chunk({"tool_calls": [
                {"index": 0, "id": "c1", "type": "function",
                 "function": {"name": "bash",
                              "arguments": "{\"command\":\"pwd\"}"}}],
                "content": ""}, finish="tool_calls"),
            chunk({}, choices=[], usage={"prompt_tokens": 1}),
        ))

    events = [e async for e in _client(handler).stream_chat(
        {"messages": [], "tools": [shell_tool]}, "m")]
    # 上游请求里注入的 bash 带上了 shell 的 schema（供模型生成匹配参数）
    by_name = {t["function"]["name"]: t["function"] for t in sent["tools"]}
    assert by_name["bash"]["description"] == "Run a shell command"
    # 出口：tool_call 被改写成客户端真实工具，finish 不降级
    tool_events = [e for e in events if e.kind is EventKind.TOOL_CALLS]
    call = tool_events[0].tool_calls[0]
    assert call["function"]["name"] == "shell"
    assert call["function"]["arguments"] == "{\"command\":\"pwd\"}"
    finishes = [e for e in events if e.kind is EventKind.FINISH]
    assert finishes[0].finish_reason == "tool_calls"

async def test_stream_chat_keeps_real_tool_calls():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse(
            chunk({"tool_calls": [
                {"index": 0, "id": "c1", "type": "function",
                 "function": {"name": "get_weather", "arguments": ""}}]},
                finish="tool_calls"),
        ))

    events = [e async for e in _client(handler).stream_chat({"messages": []}, "m")]
    tool_events = [e for e in events if e.kind is EventKind.TOOL_CALLS]
    assert tool_events and tool_events[0].tool_calls[0]["function"]["name"] == "get_weather"
    finishes = [e for e in events if e.kind is EventKind.FINISH]
    assert finishes[0].finish_reason == "tool_calls"

async def test_stream_chat_raises_classified_http_error(monkeypatch):
    """403 抖动重试用尽后仍上抛，分类保持 REQUEST（重试不改错误语义）。"""
    from src.provider.zen import client as zen_client

    monkeypatch.setattr(zen_client, "GATE_403_RETRY_DELAY", 0.0)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(403, content=b'{"error":"forbidden"}')

    with pytest.raises(UpstreamHTTPError) as caught:
        [e async for e in _client(handler).stream_chat({"messages": []}, "m")]
    assert caught.value.kind() is ErrKind.REQUEST
    # 首次 + 重试一次（GATE_403_RETRIES=1）后放弃
    assert len(calls) == zen_client.GATE_403_RETRIES + 1

async def test_stream_chat_retries_gate_403_then_succeeds(monkeypatch):
    """门禁 403 抖动：退避后换新 session 重发，第二次 200 → 对下游完全不可见。"""
    from src.provider.zen import client as zen_client

    monkeypatch.setattr(zen_client, "GATE_403_RETRY_DELAY", 0.0)
    sessions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sessions.append(request.headers["x-opencode-session"])
        if len(sessions) == 1:
            return httpx.Response(403, content=b'{"error":"FreeTierError"}')
        return httpx.Response(200, text=_sse(
            chunk({"content": "hi"}, finish="stop"),
            chunk({}, choices=[], usage={"prompt_tokens": 1, "completion_tokens": 1}),
        ))

    events = [e async for e in _client(handler).stream_chat({"messages": []}, "m")]
    assert len(sessions) == 2
    # 重试走新 session（门禁只校验格式，但换 session 是重试语义的一部分）
    assert sessions[0] != sessions[1]
    assert all(SESSION_RE.match(s) for s in sessions)
    assert [e.kind for e in events] == [EventKind.CONTENT, EventKind.FINISH,
                                        EventKind.USAGE]

async def test_stream_chat_non_403_http_error_is_not_retried():
    """只对 403 重试：其余状态码一次上抛，不增加额外往返。"""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500, content=b'{"error":"boom"}')

    with pytest.raises(UpstreamHTTPError) as caught:
        [e async for e in _client(handler).stream_chat({"messages": []}, "m")]
    assert caught.value.kind() is ErrKind.OTHER
    assert len(calls) == 1

async def test_fetch_models_keeps_only_live_free_candidates():
    """后缀收窄 + 探活：非后缀、探活非 2xx、畸形条目一律不出现在结果里。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_MODELS:
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": "big-pickle", "owned_by": "opencode"},  # 无 -free 后缀 → 隐藏
                {"id": "alpha-free", "owned_by": "opencode"},  # 探活 200 → 保留
                {"id": "beta-free", "owned_by": "opencode"},   # 探活 401（付费/需 key）→ 剔除
                {"id": "GAMMA-FREE"},                          # 大写后缀也认，探活 200 → 保留
                {"id": "delta-free"},                          # 探活 400（已下线）→ 剔除
                {"id": "no-owner-free"},                       # owned_by 缺失也照样派生出名字
                "junk",                                        # 非 dict 跳过
                {"id": ""},                                    # 空 id 跳过
                {"id": 123},                                   # 非字符串 id 跳过
            ]})
        assert request.url.path == EP_CHAT
        body = json.loads(request.content)
        assert body["stream"] is True
        assert [t["function"]["name"] for t in body["tools"]] == ["bash", "read"]
        status = {"alpha-free": 200, "GAMMA-FREE": 200, "no-owner-free": 200,
                  "beta-free": 401, "delta-free": 400}[body["model"]]
        return httpx.Response(status, text=_sse(chunk({"content": "x"}, finish="stop")))

    models = await _client(handler).fetch_models()
    assert [m.id for m in models] == ["alpha-free", "GAMMA-FREE", "no-owner-free"]
    # 展示名从 id 派生，不用上游 `owned_by`（恒为 "opencode"，会让所有模型同名）
    assert [m.name for m in models] == ["Alpha", "Gamma", "No Owner"]
    # 免费层显式标 0 倍率（列表 UI 显示 x0，排序时也排最省的一档）
    assert all(m.credit_rate == 0.0 for m in models)

def test_pretty_model_name_derives_readable_name():
    """展示名派生规则：去免费后缀 + 品牌/缩写按官方写法 + 数字段原样。"""
    from src.provider.zen.client import pretty_model_name

    assert pretty_model_name("longcat-2.5-preview-free") == "LongCat 2.5 Preview"
    assert pretty_model_name("mimo-v2.6-flash-free") == "MiMo V2.6 Flash"
    assert pretty_model_name("nemotron-3.5-lightning-free") == "Nemotron 3.5 Lightning"
    assert pretty_model_name("deepseek-v4.1-flash-free") == "DeepSeek V4.1 Flash"
    assert pretty_model_name("glm-5-free") == "GLM 5"
    assert pretty_model_name("qwen3.8-max") == "Qwen3.8 Max"     # 字母+数字同段
    assert pretty_model_name("space-bunny-free") == "Space Bunny"
    assert pretty_model_name("kimi-k2.7-code-free") == "Kimi K2.7 Code"
    assert pretty_model_name("qmodel_vl_free", free_suffix="_free") == "Qmodel VL"
    assert pretty_model_name("big-pickle") == "Big Pickle"       # 无后缀：付费档也走这条
    assert pretty_model_name("-free") == ""                      # 去掉后缀后为空 → 前端回退 id
    assert pretty_model_name("gpt-5", free_suffix="") == "GPT 5"  # 后缀置空：按原 id 切

def test_pretty_model_name_plain_numeric_segment():
    """纯数字/符号段（无字母部分）原样保留，不被 title-case 破坏。

    分段美化逻辑与其余渠道共用（`provider.naming`），zen 只留薄封装。
    """
    from src.provider.naming import _pretty_segment

    assert _pretty_segment("2.5") == "2.5"
    assert _pretty_segment("3") == "3"

async def test_fetch_models_custom_suffix_and_probe_network_error():
    """后缀可覆盖；探活连接异常按不可用处理。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_MODELS:
            return httpx.Response(200, json={"data": [
                {"id": "m1-free"}, {"id": "m2-lite"}]})
        raise httpx.ConnectError("boom")

    with pytest.raises(zen_events.UpstreamProtocolViolation, match="no free model"):
        await _client(handler, free_suffix="-lite").fetch_models()

async def test_fetch_models_probe_timeout_is_unavailable(monkeypatch):
    """探活首字超时按不可用：整体套 `asyncio.timeout`，不拖垮模型列表。"""
    from src.provider.zen import client as zen_client

    monkeypatch.setattr(zen_client, "PROBE_TIMEOUT", 0.01)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_CHAT:
            await asyncio.sleep(1)   # 首字过慢，超过 PROBE_TIMEOUT
        return httpx.Response(200, json={"data": [{"id": "slow-free"}]})

    with pytest.raises(zen_events.UpstreamProtocolViolation, match="no free model"):
        await _client(handler).fetch_models()

async def test_fetch_models_reuses_probe_result_within_ttl():
    """判活结果在 models_cache_ttl 内复用：服务层每 300s 重拉列表不会重探上游。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == EP_MODELS:
            return httpx.Response(200, json={"data": [
                {"id": "a-free", "owned_by": "opencode"}]})
        assert request.url.path == EP_CHAT
        return httpx.Response(200, text="")

    client = _client(handler)
    first = await client.fetch_models()
    assert [m.id for m in first] == ["a-free"]
    upstream_calls = len(calls)              # 一次清单 + 一次探活
    assert upstream_calls == 2

    second = await client.fetch_models()
    assert [m.id for m in second] == ["a-free"]
    assert len(calls) == upstream_calls      # 命中判活缓存，没再打上游

async def test_fetch_models_cache_expiry_reprobes():
    """超过 models_cache_ttl（此处设为 0）后重新拉清单并重探。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == EP_MODELS:
            return httpx.Response(200, json={"data": [{"id": "a-free"}]})
        return httpx.Response(200, text="")

    client = _client(handler, models_cache_ttl=0)
    await client.fetch_models()
    assert len(calls) == 2
    await client.fetch_models()
    assert len(calls) == 4

async def test_fetch_models_errors():
    def http_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=b"bad gateway")

    with pytest.raises(UpstreamHTTPError):
        await _client(http_error).fetch_models()

    def non_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    with pytest.raises(zen_events.UpstreamProtocolViolation):
        await _client(non_json).fetch_models()

    # JSON 合法但不是对象（无 data 可取）
    def json_scalar(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2])

    with pytest.raises(zen_events.UpstreamProtocolViolation):
        await _client(json_scalar).fetch_models()

    def no_list(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": "list"})

    with pytest.raises(zen_events.UpstreamProtocolViolation):
        await _client(no_list).fetch_models()

    # 有 data 但没有任何 -free 候选（含非 dict 噪声）
    def no_candidates(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": ["junk", {"id": "gpt"}]})

    with pytest.raises(zen_events.UpstreamProtocolViolation, match="no free candidates"):
        await _client(no_candidates).fetch_models()

    # 候选都有，但探活全军覆没（上游整体故障）→ 交给上层缓存兜底
    def all_dead(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_MODELS:
            return httpx.Response(200, json={"data": [{"id": "a-free"},
                                                      {"id": "b-free"}]})
        return httpx.Response(500, text="down")

    with pytest.raises(zen_events.UpstreamProtocolViolation, match="liveness probe"):
        await _client(all_dead).fetch_models()

async def test_client_lazy_clients_and_aclose():
    """未注入 client 时惰性创建；aclose 释放两侧连接池。"""
    client = ZenClient()
    assert client._stream is client._stream      # noqa: SLF001 - 惰性复用同一实例
    assert client._short is client._short        # noqa: SLF001
    await client.aclose()

    # 都没创建过：aclose 不报错（两个分支都为 None）
    await ZenClient().aclose()

async def test_client_host_trailing_slash_stripped():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == EP_CHAT
        return httpx.Response(200, text=_sse(chunk({"content": "x"}, finish="stop")))

    client = _client(handler, host="https://opencode.ai/")
    assert client.host == "https://opencode.ai"
    _ = [e async for e in client.stream_chat({"messages": []}, "m")]
    await client.aclose()

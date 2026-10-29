"""CodeBuddy 客户端：状态分类、聊天流、池生命周期与错误分类。"""

from __future__ import annotations

import json

import httpx
import pytest

from src.provider.base import ErrKind, EventKind
from src.provider.codebuddy import events as cb_events
from src.provider.codebuddy.client import (
    CodeBuddyClient,
    CodeBuddyCredential,
    CodeBuddyProvider,
    UpstreamHTTPError,
)
from src.provider.codebuddy.events import UpstreamProtocolViolation
from tests.provider.codebuddy._support import client as _client
from tests.provider.codebuddy._support import fixture


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
    # 「套餐不允许」：对**当前凭证**与 11102 等价（重试无意义、换号才有用）
    (400, b'{"code": 11158, "msg": "model [gpt-6-luna] is not allowed for user '
          b'type [personal_free_user]"}', ErrKind.BLOCKED),
    (404, b'{"code": 11158}', ErrKind.BLOCKED),
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
    (400, b'{"code": 111580}', ErrKind.INVALID),
    # 嵌套信封：业务码在内层，按集合成员判断不漏
    (400, b'{"code": 0, "data": {"code": 11102}}', ErrKind.BLOCKED),
    (404, b"not json at all", ErrKind.SOFT),
    (500, b'{"code": 9999}', ErrKind.OTHER),
])
def test_classify_status_business_codes(status, body, expected):
    assert cb_events.classify_status(status, body) is expected

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

async def test_provider_probe_quota_delegates():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=fixture("quota-personal.json"))

    provider = CodeBuddyProvider(client=_client(handler))
    quota = await provider.probe_quota({"bearer_token": "t"})
    assert quota.total == 5500.0

async def test_aclose_when_only_short_client_was_created():
    """只创建 short 客户端时，aclose 跳过 stream（client 159→158）。"""
    client = CodeBuddyClient()
    assert client._short is not None    # 只实例化 short
    await client.aclose()

def test_error_type_for_invalid():
    """INVALID → invalid_request（executor 290-291）。"""
    from src.engine.executor import _error_type_for

    assert _error_type_for(ErrKind.INVALID) == "invalid_request"


# ------------------------------------------- 国际站指纹（2026-10-29）

def test_conversation_seed_prefers_explicit_then_first_message():
    from src.provider.codebuddy.client import conversation_seed

    explicit = conversation_seed({
        "prompt_cache_key": "p1",
        "messages": [{"role": "user", "content": "hi"}]})
    assert explicit == "prompt_cache_key:p1"

    seeded = conversation_seed({"messages": [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "同一个问题"}]})
    assert seeded is not None and seeded.startswith("first:")
    # 多轮追加后首条用户消息不变 → 仍是同一个会话种子（换号/重试不换 id）
    grown = conversation_seed({"messages": [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "同一个问题"},
        {"role": "assistant", "content": "答"},
        {"role": "user", "content": "追问"}]})
    assert grown == seeded

    assert conversation_seed({}) is None
    assert conversation_seed({"messages": "not a list"}) is None
    # 脏消息（非 dict）跳过，继续找第一条可用的
    assert conversation_seed({"messages": ["junk", {"role": "user", "content": "x"}]}
                             ).startswith("first:")
    assert conversation_seed({"messages": [{"role": "system", "content": "s"}]}) is None


async def test_stream_chat_intl_gzips_body_and_keeps_conversation_id():
    """官方 CLI 形态：gzip 体 + 同一会话复用 conversation id、每轮换 request id。"""
    import gzip

    from tests.helpers.http import provider_client

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    client = provider_client(CodeBuddyClient, handler, intl=True, gzip_body=True)
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    for _ in range(2):
        [event async for event in client.stream_chat(
            CodeBuddyCredential(bearer_token="t"), payload, "glm-5.2")]

    first, second = seen
    assert first.headers["content-encoding"] == "gzip"
    assert json.loads(gzip.decompress(first.content))["stream"] is True
    assert first.headers["x-conversation-id"] == second.headers["x-conversation-id"]
    assert first.headers["x-request-id"] != second.headers["x-request-id"]
    assert first.headers["x-model-id"] == "glm-5.2"
    assert first.headers["user-agent"].startswith("CLI/2.178.0")


async def test_stream_chat_cn_keeps_plain_json_body():
    """国内站不受国际指纹改动影响：明文 JSON、无 Content-Encoding。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["encoding"] = request.headers.get("content-encoding")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    [event async for event in _client(handler).stream_chat(
        CodeBuddyCredential(bearer_token="t"), {"messages": []}, "glm-5.2")]
    assert seen["encoding"] is None
    assert seen["body"]["model"] == "glm-5.2"

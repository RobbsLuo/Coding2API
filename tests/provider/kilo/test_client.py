"""Kilo 上游客户端：请求体、聊天流、模型发现与惰性连接。"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from src.provider.base import ErrKind, Event, EventKind
from src.provider.kilo import events as kilo_events
from src.provider.kilo.client import (
    EP_CHAT,
    EP_MODELS,
    KiloClient,
    UpstreamHTTPError,
    _model_from_item,
    prepare_body,
    request_headers,
)
from tests.helpers.http import chunk_payload as chunk
from tests.helpers.http import sse_text_done as _sse
from tests.provider.kilo._support import client as _client

pytestmark = pytest.mark.real_upstream("kilo")


def test_prepare_body_forces_stream_and_deepcopies_messages():
    messages = [{"role": "user", "content": "hi"}]
    body = prepare_body({"messages": messages, "stream": False, "temperature": 0.5},
                        "stealth/space-bunny-alpha")
    assert body["stream"] is True
    assert body["model"] == "stealth/space-bunny-alpha"
    assert body["temperature"] == 0.5
    # 深拷贝：改写上游 body 不影响引擎持有的原始请求体
    body["messages"][0]["content"] = "changed"
    assert messages[0]["content"] == "hi"

def test_request_headers_are_anonymous():
    headers = request_headers()
    assert headers["Accept"] == "text/event-stream"
    assert headers["Content-Type"] == "application/json"
    # 不带 Authorization：Kilo 把任何 Authorization 头当真实凭证校验，
    # 带占位符反而 401（与 Zen 相反）
    assert "Authorization" not in headers

async def test_stream_chat_yields_named_events():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == EP_CHAT
        assert "Authorization" not in request.headers
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["model"] == "m"
        return httpx.Response(200, text=_sse(
            chunk({"role": "assistant", "reasoning": "r"}),
            chunk({"content": "hi"}),
            chunk({"content": "!"}, finish="stop"),
            chunk({}, choices=[], usage={"prompt_tokens": 1, "completion_tokens": 1}),
        ))

    events = [e async for e in _client(handler).stream_chat(
        {"messages": [{"role": "user", "content": "x"}]}, "m")]
    kinds = [e.kind for e in events]
    assert kinds == [EventKind.REASONING, EventKind.CONTENT, EventKind.CONTENT,
                     EventKind.FINISH, EventKind.USAGE]

async def test_stream_chat_raises_classified_http_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b'{"error":"slow down"}')

    with pytest.raises(UpstreamHTTPError) as caught:
        [e async for e in _client(handler).stream_chat({"messages": []}, "m")]
    assert caught.value.kind() is ErrKind.MODEL

def test_upstream_http_error_exposes_status_and_kind():
    error = UpstreamHTTPError(404, b'{"error":"model not found"}')
    assert error.status == 404
    assert error.kind() is ErrKind.INVALID

def test_model_from_item_passes_through_metadata():
    item = {
        "id": "stealth/space-bunny-alpha",
        "name": "Space Bunny Alpha",
        "isFree": True,
        "context_length": 131072,
        "top_provider": {"max_completion_tokens": 8192,
                         "context_length": 65536},
        "architecture": {"input_modalities": ["text", "image"]},
        "supported_parameters": ["tools", "reasoning", "temperature"],
    }
    model = _model_from_item(item, item["id"])
    assert model.id == "stealth/space-bunny-alpha"
    assert model.name == "Space Bunny Alpha"
    assert model.credit_rate == 0.0
    assert model.max_input_tokens == 131072          # 顶层优先
    assert model.max_output_tokens == 8192
    assert model.supports_images is True
    assert model.supports_tool_call is True
    assert model.supports_reasoning is True

def test_model_from_item_falls_back_to_top_provider_context():
    model = _model_from_item(
        {"id": "m", "top_provider": {"context_length": 4096}}, "m")
    assert model.max_input_tokens == 4096
    assert model.name == ""

def test_model_from_item_handles_missing_metadata():
    """元数据缺失/类型不对 → 各字段 None，不崩。"""
    model = _model_from_item(
        {"id": "m", "name": 123, "context_length": True,
         "top_provider": "junk", "architecture": "junk",
         "supported_parameters": "junk"}, "m")
    assert model.name == ""                          # 非字符串 name
    assert model.max_input_tokens is None            # bool 不算 int
    assert model.max_output_tokens is None
    assert model.supports_images is None
    assert model.supports_tool_call is None
    assert model.supports_reasoning is None

def test_model_from_item_modalities_and_params_without_capabilities():
    model = _model_from_item(
        {"id": "m", "architecture": {"input_modalities": ["text"]},
         "supported_parameters": ["temperature"]}, "m")
    assert model.supports_images is False
    assert model.supports_tool_call is False
    assert model.supports_reasoning is False

async def test_fetch_models_keeps_only_isfree_models():
    """只保留 isFree=true；非免费、畸形条目一律不出现在结果里。"""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == EP_MODELS
        return httpx.Response(200, json={"object": "list", "data": [
            {"id": "kilo-auto/free", "name": "Auto", "isFree": True},
            {"id": "gpt-4o", "isFree": False},          # 付费 → 隐藏
            {"id": "no-flag"},                          # 无 isFree → 隐藏
            {"id": "isFree-truthy", "isFree": 1},       # 非布尔 True → 隐藏
            "junk",                                     # 非 dict 跳过
            {"id": ""},                                 # 空 id 跳过
            {"id": 123},                                # 非字符串 id 跳过
            {"id": "openrouter/free", "name": "OR", "isFree": True},
        ]})

    models = await _client(handler).fetch_models()
    assert [m.id for m in models] == ["kilo-auto/free", "openrouter/free"]
    # 免费层显式标 0 倍率（列表 UI 显示 x0，排序时也排最省的一档）
    assert all(m.credit_rate == 0.0 for m in models)

async def test_fetch_models_no_probe_request_sent():
    """不做探活：只打一次 /models，不向 /chat/completions 发任何请求。"""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"data": [{"id": "a/free", "isFree": True}]})

    models = await _client(handler).fetch_models()
    assert [m.id for m in models] == ["a/free"]
    assert paths == [EP_MODELS]

async def test_fetch_models_errors():
    def http_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=b"bad gateway")

    with pytest.raises(UpstreamHTTPError):
        await _client(http_error).fetch_models()

    def non_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    with pytest.raises(kilo_events.UpstreamProtocolViolation):
        await _client(non_json).fetch_models()

    # JSON 合法但不是对象（无 data 可取）
    def json_scalar(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2])

    with pytest.raises(kilo_events.UpstreamProtocolViolation):
        await _client(json_scalar).fetch_models()

    def no_list(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": "list"})

    with pytest.raises(kilo_events.UpstreamProtocolViolation):
        await _client(no_list).fetch_models()

    # 有 data 但没有任何 isFree 模型
    def no_free(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": ["junk", {"id": "gpt-4o"}]})

    with pytest.raises(kilo_events.UpstreamProtocolViolation, match="no free models"):
        await _client(no_free).fetch_models()

async def test_client_lazy_clients_and_aclose():
    """未注入 client 时惰性创建；aclose 释放两侧连接池。"""
    client = KiloClient()
    assert client._stream is client._stream      # noqa: SLF001 - 惰性复用同一实例
    assert client._short is client._short        # noqa: SLF001
    await client.aclose()

    # 都没创建过：aclose 不报错（两个分支都为 None）
    await KiloClient().aclose()

async def test_client_host_trailing_slash_stripped():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/gateway" + EP_CHAT
        return httpx.Response(200, text=_sse(chunk({"content": "x"}, finish="stop")))

    client = _client(handler, host="https://kilo.test/api/gateway/")
    assert client.host == "https://kilo.test/api/gateway"
    _ = [e async for e in client.stream_chat({"messages": []}, "m")]
    await client.aclose()

async def test_stream_429_cools_only_that_model(tmp_path):
    """上游 429（OpenRouter 共享池点名模型）只冷却该模型，不连累整条 kilo 渠道。

    回归：429 曾判账号级 SOFT，单虚拟凭证下会把整条渠道冷却 60s，客户端
    收到 `all credentials unavailable`。改为模型级后账号级冷却保持为空，
    同渠道其他免费模型仍可选。
    """
    from src.compat.openai.request import parse_chat_request
    from src.db.conn import Database
    from src.db.crypto import CredentialCipher
    from src.db.migrate import apply_schema
    from src.db.repo import CredentialRepository
    from src.engine.executor import Executor, ExecutorDeps
    from src.engine.scheduler import Scheduler
    from tests.helpers.constants import SECRET

    db = Database(tmp_path / "kilo429.sqlite3")
    apply_schema(db.connect())
    repo = CredentialRepository(db, CredentialCipher(SECRET))
    repo.add(provider="kilo", credential_data={})

    async def gen(_cred, _payload, model):
        if model == "qwen/qwen3.8-27b:free":
            raise UpstreamHTTPError(429, b'{"error":{"code":429}}')
        yield Event(kind=EventKind.CONTENT, content="ok")
        yield Event(kind=EventKind.FINISH, finish_reason="stop")

    class P:
        id = "kilo"
        stream_chat = staticmethod(gen)

    executor = Executor(ExecutorDeps(
        providers={"kilo": P()}, credentials=repo,
        scheduler=Scheduler(max_rotate=1), default_model="qwen/qwen3.8-27b:free"))

    request = parse_chat_request(
        {"messages": [{"role": "user", "content": "hi"}]})   # model 缺省 → default
    frames = [f async for f in executor.stream(request, username="u")]
    assert any(b"no_healthy_credential" in f for f in frames)

    account = db.connect().execute(
        "SELECT cooling_until, err_count FROM credentials").fetchone()
    assert tuple(account) == (None, 0)                   # 账号未被冷却
    cooldowns = [tuple(row) for row in db.connect().execute(
        "SELECT model, hits, reason FROM credential_model_cooldowns").fetchall()]
    assert cooldowns == [("qwen/qwen3.8-27b:free", 1, "model")]

    # 同渠道其他模型仍可选（模型级冷却只锁触发模型）
    now = int(time.time())
    row = repo.candidates(["kilo"])[0]
    assert row.is_selectable(now, "stealth/space-bunny-alpha")
    assert not row.is_selectable(now, "qwen/qwen3.8-27b:free")
    db.close()

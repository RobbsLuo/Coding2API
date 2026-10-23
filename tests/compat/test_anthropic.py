"""Anthropic 出口：/v1/messages 端点契约（流式/非流式/工具/鉴权/400）。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from src.config import Settings
from src.main import build_app
from src.provider.base import Event, EventKind, Quota, Usage
from tests.compat._support import build_compat_app
from tests.helpers.constants import SECRET


def _events(text: str) -> list[tuple[str, dict]]:
    """SSE 文本 → [(event_name, data_dict), ...]。"""
    out: list[tuple[str, dict]] = []
    for block in text.split("\n\n"):
        lines = [line for line in block.split("\n") if line]
        name = ""
        data = ""
        for line in lines:
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if name and data:
            out.append((name, json.loads(data)))
    return out

class _Provider:
    id = "codebuddy"

    def __init__(self, scenario: str = "text") -> None:
        self.scenario = scenario
        self.last_payload: dict = {}
        self.calls = 0

    async def stream_chat(self, credential_data, payload, model):
        self.calls += 1
        self.last_payload = payload
        if self.scenario == "tools":
            yield Event(kind=EventKind.TOOL_CALLS, tool_calls=[
                {"index": 0, "id": "call_9", "type": "function",
                 "function": {"name": "f", "arguments": '{"a":1}'}}])
            yield Event(kind=EventKind.FINISH, finish_reason="tool_calls")
            return
        if self.scenario == "cache":
            yield Event(kind=EventKind.CONTENT, content="cached")
            yield Event(kind=EventKind.USAGE, usage=Usage(
                input_tokens=10, output_tokens=3, cached_tokens=8))
            yield Event(kind=EventKind.FINISH, finish_reason="stop")
            return
        yield Event(kind=EventKind.CONTENT, content="hi there")
        yield Event(kind=EventKind.USAGE, usage=Usage(input_tokens=2, output_tokens=3))
        yield Event(kind=EventKind.FINISH, finish_reason="stop")

    async def probe_quota(self, credential_data):
        return Quota(remaining=10, total=10)

    async def list_models(self, credential_data):
        return []

    async def refresh(self, credential_data):
        return credential_data

    async def aclose(self):
        return None

def _app(tmp_path, provider=None):
    return build_compat_app(tmp_path, provider or _Provider())

def _auth(app) -> dict:
    return {"x-api-key": app.state.api_keys.create("root")["api_key"]}

def _messages(**overrides) -> dict:
    body = {"model": "glm-5.2@codebuddy", "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}]}
    body.update(overrides)
    return body

def test_endpoint_stream_events(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/v1/messages", headers=_auth(app),
                               json=_messages(stream=True))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _events(response.text)
    names = [name for name, _ in events]
    assert names[0] == "message_start"
    assert names[-1] == "message_stop"
    assert any(delta["delta"]["type"] == "text_delta"
               for name, delta in events if name == "content_block_delta")

def test_endpoint_stream_tool_use(tmp_path):
    app = _app(tmp_path, _Provider("tools"))
    with TestClient(app) as client:
        response = client.post("/v1/messages", headers=_auth(app),
                               json=_messages(stream=True, tools=[
                                   {"name": "f", "input_schema": {"type": "object"}}]))
    assert "tool_use" in response.text
    assert "input_json_delta" in response.text
    assert '"stop_reason": "tool_use"' in response.text

def test_endpoint_non_stream(tmp_path):
    provider = _Provider()
    app = _app(tmp_path, provider)
    with TestClient(app) as client:
        response = client.post("/v1/messages", headers=_auth(app), json=_messages())
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "message"
    assert body["content"] == [{"type": "text", "text": "hi there"}]
    assert body["usage"] == {"input_tokens": 2, "output_tokens": 3}
    assert provider.last_payload["messages"][0] == {"role": "user", "content": "hi"}

def test_endpoint_stream_keepalive_uses_ping_event(tmp_path, monkeypatch):
    """空闲心跳走 `event: ping`（不是注释帧）：只解析 event 行的客户端才收得到。

    这是端到端契约：`dispatch_compat` 必须把 translator 的心跳帧交给
    `with_keepalive`，否则翻译器上的 `keepalive()` 永远不会被调用。
    """
    import asyncio

    from src.api import streaming

    monkeypatch.setattr(streaming, "KEEPALIVE_SECONDS", 0.01)

    class SlowProvider(_Provider):
        async def stream_chat(self, credential_data, payload, model):
            await asyncio.sleep(0.08)
            async for event in super().stream_chat(credential_data, payload, model):
                yield event

    app = _app(tmp_path, SlowProvider())
    with TestClient(app) as client:
        response = client.post("/v1/messages", headers=_auth(app),
                               json=_messages(stream=True))
    assert response.status_code == 200
    assert "event: ping" in response.text
    assert ": keepalive" not in response.text
    # ping 必须是合法 JSON 事件，不能只有事件名
    assert any(name == "ping" and data == {"type": "ping"}
               for name, data in _events(response.text))


def test_endpoint_stream_reports_cache_read(tmp_path):
    app = _app(tmp_path, _Provider("cache"))
    with TestClient(app) as client:
        response = client.post("/v1/messages", headers=_auth(app),
                               json=_messages(stream=True))
    events = _events(response.text)
    usage = [data["usage"] for name, data in events if name == "message_delta"][0]
    assert usage == {"input_tokens": 2, "output_tokens": 3,
                     "cache_read_input_tokens": 8}

def test_endpoint_non_stream_reports_cache_read(tmp_path):
    app = _app(tmp_path, _Provider("cache"))
    with TestClient(app) as client:
        response = client.post("/v1/messages", headers=_auth(app), json=_messages())
    assert response.json()["usage"] == {"input_tokens": 2, "output_tokens": 3,
                                        "cache_read_input_tokens": 8}

def test_endpoint_accepts_bearer_fallback(tmp_path):
    app = _app(tmp_path)
    token = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        response = client.post("/v1/messages",
                               headers={"Authorization": f"Bearer {token}"},
                               json=_messages())
    assert response.status_code == 200

def test_endpoint_requires_api_key(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        assert client.post("/v1/messages", json=_messages()).status_code == 401

def test_endpoint_rejects_unsupported_block(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/v1/messages", headers=_auth(app), json=_messages(
            messages=[{"role": "user", "content": [{"type": "image"}]}]))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"

def test_endpoint_unknown_provider_400(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/v1/messages", headers=_auth(app),
                               json=_messages(model="glm-5.2@nope", stream=True))
    assert response.status_code == 400
    assert "unknown provider" in response.json()["error"]["message"]

def test_endpoint_no_healthy_credential_503(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"codebuddy": _Provider()})
    with TestClient(app) as client:
        response = client.post("/v1/messages", headers=_auth(app), json=_messages())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "no_healthy_credential"

def test_endpoint_model_allowlist_blocks(tmp_path):
    app = _app(tmp_path)
    token = app.state.api_keys.create("root", allowed_models="glm-*")["api_key"]
    headers = {"x-api-key": token}
    with TestClient(app) as client:
        blocked = client.post("/v1/messages", headers=headers,
                              json=_messages(model="kimi-k3@codebuddy"))
        allowed = client.post("/v1/messages", headers=headers, json=_messages())
    assert blocked.status_code == 400
    assert "not allowed" in blocked.json()["error"]["message"]
    assert allowed.status_code == 200

def test_endpoint_count_tokens(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/v1/messages/count_tokens", headers=_auth(app),
                               json=_messages())
    assert response.status_code == 200
    assert response.json()["input_tokens"] > 0

def test_endpoint_count_tokens_respects_allowlist(tmp_path):
    app = _app(tmp_path)
    token = app.state.api_keys.create("root", allowed_models="glm-*")["api_key"]
    with TestClient(app) as client:
        response = client.post("/v1/messages/count_tokens", headers={"x-api-key": token},
                               json=_messages(model="kimi-k3@codebuddy"))
    assert response.status_code == 400

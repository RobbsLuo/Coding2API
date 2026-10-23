"""Responses 出口：/v1/responses 端点契约（流式/非流式/400/鉴权）。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from src.config import Settings
from src.main import build_app
from src.provider.base import ErrKind, Event, EventKind, Quota, Usage
from tests.compat._support import build_compat_app
from tests.helpers.constants import SECRET


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
        yield Event(kind=EventKind.CONTENT, content="hi there")
        yield Event(kind=EventKind.USAGE,
                    usage=Usage(input_tokens=2, output_tokens=3))
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
    return {"Authorization": f"Bearer {app.state.api_keys.create('root')['api_key']}"}

def test_endpoint_stream_events(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/v1/responses", headers=_auth(app), json={
            "model": "glm-5.2@codebuddy", "instructions": "sys", "input": "hi",
            "stream": True, "max_output_tokens": 32,
        })
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    names = [line[7:] for line in response.text.split("\n") if line.startswith("event: ")]
    assert names[0] == "response.created"
    assert names[-1] == "response.completed"
    assert "response.output_text.delta" in names

def test_endpoint_stream_keepalive_stays_comment(tmp_path, monkeypatch):
    """Responses 出口的心跳必须保持注释帧：该协议没有 ping 事件类型。"""
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
        response = client.post("/v1/responses", headers=_auth(app), json={
            "model": "glm-5.2@codebuddy", "input": "hi", "stream": True,
        })
    assert response.status_code == 200
    assert ": keepalive" in response.text
    assert "event: ping" not in response.text


def test_endpoint_non_stream(tmp_path):
    provider = _Provider()
    app = _app(tmp_path, provider)
    with TestClient(app) as client:
        response = client.post("/v1/responses", headers=_auth(app),
                               json={"model": "glm-5.2@codebuddy", "input": "hi"})
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["output"][0]["content"][0]["text"] == "hi there"
    assert body["usage"]["total_tokens"] == 5
    assert provider.last_payload["messages"] == [{"role": "user", "content": "hi"}]

def test_endpoint_tool_call_stream(tmp_path):
    provider = _Provider("tools")
    app = _app(tmp_path, provider)
    with TestClient(app) as client:
        response = client.post("/v1/responses", headers=_auth(app), json={
            "model": "glm-5.2@codebuddy", "input": "hi", "stream": True,
            "tools": [{"type": "function", "name": "f",
                       "parameters": {"type": "object"}}],
        })
    assert "response.function_call_arguments.done" in response.text
    item = json.loads([line[6:] for line in response.text.split("\n")
                       if line.startswith("data: ") and '"function_call"' in line][0])
    assert item["item"]["name"] == "f"

def test_endpoint_unsupported_param_400(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/v1/responses", headers=_auth(app),
                               json={"model": "glm-5.2@codebuddy", "input": "hi",
                                     "store": True})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert "store=true" in response.json()["error"]["message"]

def test_endpoint_unknown_provider_400(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/v1/responses", headers=_auth(app),
                               json={"model": "glm-5.2@nope", "input": "hi",
                                     "stream": True})
    assert response.status_code == 400
    assert "unknown provider" in response.json()["error"]["message"]

def test_endpoint_requires_api_key(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/v1/responses", json={"model": "m", "input": "hi"})
    assert response.status_code == 401

def test_endpoint_no_healthy_credential_returns_503(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"codebuddy": _Provider()})
    with TestClient(app) as client:
        response = client.post("/v1/responses", headers=_auth(app),
                               json={"model": "glm-5.2@codebuddy", "input": "hi"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "no_healthy_credential"

def test_stream_error_frame_prefers_translator_then_falls_back():
    """`_stream_error_frame`：translator 有 error_frame 就用它，否则 chat 兜底。"""
    from src.engine.executor import _stream_error_frame

    class _WithFrame:
        def error_frame(self, message, code):
            return f"custom:{code}".encode()

    class _WithoutFrame:
        pass

    assert _stream_error_frame(_WithFrame(), "m", "c") == b"custom:c"
    fallback = _stream_error_frame(_WithoutFrame(), "m", "c")
    assert b'"code": "c"' in fallback
    assert fallback.endswith(b"data: [DONE]\n\n")
    assert _stream_error_frame(None, "m", "c") == fallback

def test_endpoint_malformed_json_400(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/v1/responses", headers=_auth(app),
                               content=b"{not json",
                               headers_extra=None) if False else client.post(
            "/v1/responses", content=b"{not json",
            headers={**_auth(app), "Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"

def test_stream_midway_rejection_emits_failed_event(tmp_path):
    """流内 4001（模型不属于该上游）：400 语义以 response.failed 事件表达。"""

    class Rejecting(_Provider):
        async def stream_chat(self, credential_data, payload, model):
            yield Event(kind=EventKind.ERROR, error_code=4001,
                        error_message="model not available",
                        error_kind=ErrKind.INVALID)

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"codebuddy": Rejecting()})
    app.state.credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    with TestClient(app) as client:
        response = client.post("/v1/responses", headers=_auth(app),
                               json={"model": "glm-5.2@codebuddy", "input": "hi",
                                     "stream": True})
    assert response.status_code == 200
    assert "response.failed" in response.text
    assert '"invalid_request"' in response.text

def test_endpoint_model_alias_and_session_indicator(tmp_path):
    """prompt_cache_key 透传到上游，供会话粘性使用。"""
    provider = _Provider()
    app = _app(tmp_path, provider)
    with TestClient(app) as client:
        client.post("/v1/responses", headers=_auth(app), json={
            "model": "glm-5.2@codebuddy", "input": "hi",
            "prompt_cache_key": "thread-1"})
    assert provider.last_payload["prompt_cache_key"] == "thread-1"

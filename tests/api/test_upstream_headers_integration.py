"""响应头端到端：正常 / 流式 / 错误 / 未处理 500 都带（开关开）或不带（关）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.engine.executor import Executor
from src.main import build_app
from src.provider.base import ErrKind
from tests.helpers.auth import isolated_settings
from tests.helpers.events import GOOD
from tests.helpers.providers import ScriptedProvider


class _UpstreamError(Exception):
    def kind(self) -> ErrKind:
        return ErrKind.SOFT


def _app(tmp_path, *, expose: bool, provider=None):
    settings = isolated_settings(tmp_path, expose_upstream_headers=expose,
                                 short_cooldown_wait_seconds=0)
    app = build_app(settings, providers={"trae": provider or ScriptedProvider([GOOD])})
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    key = app.state.api_keys.create("root")["api_key"]
    return app, key


def _post(client, key, *, stream: bool = False):
    return client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": "glm-5.2", "stream": stream,
              "messages": [{"role": "user", "content": "hi"}]})


def test_headers_on_normal_and_stream_when_enabled(tmp_path):
    app, key = _app(tmp_path, expose=True)
    with TestClient(app) as client:
        normal = _post(client, key)
        assert normal.status_code == 200
        assert normal.headers["x-upstream-attempts"] == "1"
        assert normal.headers["x-upstream-degraded"] == "false"
        stream = _post(client, key, stream=True)
        assert stream.status_code == 200
        assert stream.headers["x-upstream-attempts"] == "1"
        assert stream.headers["x-upstream-degraded"] == "false"


def test_headers_on_error_and_unhandled_500_when_enabled(tmp_path, monkeypatch):
    app, key = _app(tmp_path, expose=True,
                    provider=ScriptedProvider([[_UpstreamError()]]))
    with TestClient(app) as client:
        failed = _post(client, key)
        assert failed.status_code == 503
        assert failed.headers["x-upstream-attempts"] >= "1"
        assert "x-upstream-degraded" in failed.headers

    async def _boom(_self, *_args, **_kwargs):
        raise RuntimeError("handler exploded")

    monkeypatch.setattr(Executor, "complete", _boom)
    with TestClient(app, raise_server_exceptions=False) as client:
        crashed = _post(client, key)
    assert crashed.status_code == 500
    assert crashed.json()["error"]["code"] == "internal_error"
    assert "x-upstream-attempts" in crashed.headers
    assert "x-upstream-degraded" in crashed.headers


def test_headers_absent_when_disabled(tmp_path):
    app, key = _app(tmp_path, expose=False)
    with TestClient(app) as client:
        normal = _post(client, key)
        stream = _post(client, key, stream=True)
    for response in (normal, stream):
        assert response.status_code == 200
        assert "x-upstream-attempts" not in response.headers
        assert "x-upstream-degraded" not in response.headers
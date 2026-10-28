"""Playground 会话端点（无需 API Key）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.config import Settings
from src.main import build_app
from tests.helpers.constants import SECRET
from tests.helpers.events import GOOD_EVENTS


class _PlaygroundProvider:
    id = "trae"

    def __init__(self, script=None) -> None:
        # script 是「按尝试次数」分组的段：每段是一批 Event（或一个异常）
        self.script = script or []
        self.calls = 0

    async def stream_chat(self, _data, _payload, _model):
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        segment = self.script[index]
        if isinstance(segment, Exception):
            raise segment
        for item in segment:
            if isinstance(item, Exception):
                raise item
            yield item

    async def list_models(self, _data):
        from src.provider.base import Model

        return [Model(id="glm-5.3"), Model(id="trae-only")]

    def import_credential(self, raw):  # pragma: no cover - 非本测试路径
        return raw

def _playground_app(tmp_path, script=None):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"trae": _PlaygroundProvider(script)})
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    return app

def test_playground_models_uses_session(tmp_path):
    app = _playground_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/playground/models").status_code == 401
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        body = client.get("/api/playground/models").json()
    assert [m["id"] for m in body["data"]] == ["glm-5.3", "trae-only"]
    assert body["data"][0]["providers"] == ["trae"]

def test_playground_chat_requires_session(tmp_path):
    app = _playground_app(tmp_path, [GOOD_EVENTS])
    with TestClient(app) as client:
        response = client.post("/api/playground/chat/completions",
                               json={"messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 401

def test_playground_chat_works_without_api_key(tmp_path):
    app = _playground_app(tmp_path, [GOOD_EVENTS])
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        response = client.post("/api/playground/chat/completions",
                               json={"messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "hi"

def test_playground_chat_stream(tmp_path):
    app = _playground_app(tmp_path, [GOOD_EVENTS])
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        response = client.post("/api/playground/chat/completions",
                               json={"messages": [{"role": "user", "content": "hi"}],
                                     "stream": True})
    assert "text/event-stream" in response.headers["content-type"]
    assert "data: [DONE]" in response.text

def test_playground_attributes_usage_to_session_user(tmp_path):
    app = _playground_app(tmp_path, [GOOD_EVENTS])
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("alice", SECRET))
        client.post("/api/playground/chat/completions",
                    json={"messages": [{"role": "user", "content": "hi"}]})
    overview = app.state.stats_query.overview(username="alice")
    assert overview["requests"] == 1 and overview["ok_count"] == 1

def test_playground_invalid_request_returns_400(tmp_path):
    app = _playground_app(tmp_path, [GOOD_EVENTS])
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        response = client.post("/api/playground/chat/completions", json={"messages": []})
    assert response.status_code == 400

def test_v1_models_and_playground_models_are_consistent(tmp_path):
    app = _playground_app(tmp_path)
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        v1 = client.get("/v1/models", headers={"Authorization": f"Bearer {key}"}).json()
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        playground = client.get("/api/playground/models").json()
    assert v1 == playground


# ----------------------------------------------- 按人判定策略（白名单 + 配额）

def _policy_app(tmp_path, *, models="", quota=None):
    """预置 root 的用户级策略（仅约束 Playground 会话直连）。"""
    app = _playground_app(tmp_path, [GOOD_EVENTS])
    app.state.user_repo.set_policy("root", allowed_models=models,
                                   quota=quota or "")
    return app


def _post_chat(client, model=None):
    body = {"messages": [{"role": "user", "content": "hi"}]}
    if model:
        body["model"] = model
    return client.post("/api/playground/chat/completions", json=body)


def test_playground_rejects_model_outside_user_allowlist(tmp_path):
    """用户白名单不通过 → 400（与 /v1 的 Key 白名单同一判据、同一文案主体）。

    缺这一步时任何登录用户（含 viewer）都能直调任意模型，把 Key 的模型白名单
    整体绕过——Playground 走 Cookie，不经过 Key 鉴权。
    """
    app = _policy_app(tmp_path, models="glm-5.3")
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        denied = _post_chat(client, "trae-only")
        assert denied.status_code == 400
        assert "not allowed for this user" in denied.json()["error"]["message"]
        assert _post_chat(client, "glm-5.3").status_code == 200


def test_playground_applies_user_daily_quota(tmp_path):
    """用户配额超限 → 429 `quota_exceeded`（带 Retry-After），不消耗上游调用。"""
    app = _policy_app(tmp_path, quota='{"daily_requests": 1}')
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        assert _post_chat(client, "glm-5.3").status_code == 200
        limited = _post_chat(client, "glm-5.3")
        assert limited.status_code == 429
        assert limited.json()["error"]["type"] == "quota_exceeded"
        assert limited.json()["error"]["message"].startswith("user quota exceeded")
        assert int(limited.headers["Retry-After"]) > 0
    # 只有第一次真的打到了上游
    assert app.state.services.executor is not None
    provider = app.state.services.registry["trae"]
    assert provider.calls == 1


def test_playground_stream_releases_concurrency_lease(tmp_path):
    """流式收尾必须归还并发名额：否则第二个请求永远撞 429。"""
    app = _policy_app(tmp_path, quota='{"max_concurrency": 1}')
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        first = client.post("/api/playground/chat/completions",
                            json={"messages": [{"role": "user", "content": "hi"}],
                                  "stream": True})
        assert first.status_code == 200
        assert "data:" in first.text
        # 名额已归还：第二个请求不该因为 max_concurrency 被拒
        assert _post_chat(client, "glm-5.3").status_code == 200


def test_playground_preflight_failure_releases_lease(tmp_path, monkeypatch):
    """流式前置校验失败（模型不属于任何已注册上游）也要归还名额。"""
    from src.compat.openai.request import InvalidRequest
    from src.engine.executor import Executor as _Executor

    app = _policy_app(tmp_path, quota='{"max_concurrency": 1}')

    def boom(self, request, provider_binding=None):
        raise InvalidRequest("unknown provider for model")

    monkeypatch.setattr(_Executor, "preflight", boom)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        bad = client.post("/api/playground/chat/completions",
                          json={"messages": [{"role": "user", "content": "hi"}],
                                "stream": True})
        assert bad.status_code == 400
        monkeypatch.undo()
        assert _post_chat(client, "glm-5.3").status_code == 200

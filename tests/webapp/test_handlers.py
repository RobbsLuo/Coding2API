"""异常 → HTTP 响应映射（`src/webapp/handlers.py`）的端到端行为。

重点覆盖「客户端自己断开」这一以前会落 500 + 全量 ASGI Traceback 的路径：
注册的异常处理器必须真的被 FastAPI 的 handler 查找命中（499 而不是冒泡）。
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from src.main import build_app
from tests.helpers.auth import isolated_settings


def test_client_disconnect_during_body_returns_499(tmp_path, monkeypatch):
    """读请求体时客户端断开：499 + client_disconnected，而非 500。

    真实的 `ClientDisconnect` 由 starlette 在 `request.body()` 内抛出（对端
    在 body 传完前关闭连接）；这里让 `read_json_body` 直接抛同一种异常，
    验证的是**handler 注册与匹配链路**，不是 starlette 的读体实现。
    """
    settings = isolated_settings(tmp_path)
    app = build_app(settings)
    key = app.state.api_keys.create("root")["api_key"]

    async def _disconnected(_request):
        raise ClientDisconnect()

    monkeypatch.setattr("src.api.endpoints.read_json_body", _disconnected)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "X-Request-ID": "req-disconnect"},
            json={"messages": [{"role": "user"}]})
    assert response.status_code == 499
    assert response.json()["error"]["code"] == "client_disconnected"
    assert response.json()["error"]["status"] == 499
    assert response.headers["x-request-id"] == "req-disconnect"

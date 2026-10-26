"""异常 → HTTP 响应映射（`src/webapp/handlers.py`）的端到端行为。

重点覆盖「客户端自己断开」这一以前会落 500 + 全量 ASGI Traceback 的路径：
注册的异常处理器必须真的被 FastAPI 的 handler 查找命中（499 而不是冒泡）。
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from src.db.crypto import CredentialDecryptError
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


def test_credential_decrypt_error_returns_actionable_500(tmp_path, monkeypatch):
    """凭证解密失败 → 500 + 可行动提示（不是裸 traceback）。

    解密失败本身已在仓储层被收成「跳过该凭证」（见
    `CredentialRepository.credential_data`），故这条路径只剩**其它**解密
    调用点（如直接用 cipher 的代码）冒泡到处理器时的兜底映射——处理器必须
    仍在，否则异常会冒泡成默认 500 + 全量 ASGI Traceback。
    """
    settings = isolated_settings(tmp_path)
    app = build_app(settings)
    key = app.state.api_keys.create("root")["api_key"]

    async def _boom(_request):
        raise CredentialDecryptError("credential decryption failed")

    monkeypatch.setattr("src.api.endpoints.read_json_body", _boom)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "X-Request-ID": "req-decrypt"},
            json={"messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "credential_decrypt_failed"
    assert response.headers["x-request-id"] == "req-decrypt"

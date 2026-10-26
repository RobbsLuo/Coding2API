"""LobsterAI 端到端接线：渠道装配、凭证导入、登录轨道、路由。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.config import Settings
from src.main import build_app
from src.provider.base import UpstreamProtocolViolation
from src.provider.lobsterai import LobsteraiProvider
from src.provider.lobsterai.client import LobsteraiClient
from tests.helpers.constants import SECRET

CREDENTIAL = {
    "accessToken": "AT", "refreshToken": "RT", "uuid": "U",
    "firstKeyfrom": "official", "latestKeyfrom": "official", "uid": "uid-1",
    "nickname": "alice",
}


def _settings(tmp_path) -> Settings:
    return Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                    ADMIN_USERNAMES="root")


async def test_build_app_registers_lobsterai_provider(tmp_path):
    """新渠道必须进 registry：否则永远不会被选号。"""
    app = build_app(_settings(tmp_path))
    providers = app.state.executor._deps.providers
    assert "lobsterai" in providers
    assert isinstance(providers["lobsterai"], LobsteraiProvider)
    assert isinstance(providers["lobsterai"].client, LobsteraiClient)
    for provider in providers.values():
        closer = getattr(provider, "aclose", None)
        if callable(closer):
            await closer()


def test_credential_import_requires_identity_triple(tmp_path):
    """管理台导入：缺身份三元组当场 400，不留一条永远续不了期的凭证。"""
    app = build_app(_settings(tmp_path))
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        missing = client.post("/api/credentials", json={
            "provider": "lobsterai",
            "credential": {**CREDENTIAL, "uuid": ""}})
        assert missing.status_code == 400
        ok = client.post("/api/credentials", json={
            "provider": "lobsterai", "credential": CREDENTIAL})
        assert ok.status_code == 200
        stored = app.state.credentials.candidates(["lobsterai"])
        assert len(stored) == 1
        data = app.state.credentials.credential_data(stored[0].credential_id)
        assert data["accessToken"] == "AT"


def test_upstream_auth_start_uses_paste_track(tmp_path):
    """门户只接受回环回调 → paste 轨道（用户把地址栏链接粘回来）。"""
    app = build_app(_settings(tmp_path))
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        response = client.post("/api/auth/upstream/start",
                               json={"provider": "lobsterai"})
        assert response.status_code == 200
        body = response.json()
        assert body["flow"] == "paste"
        assert "source=electron" in body["auth_url"]
        # pending 登记按发起人分键
        assert app.state.pending_callbacks["root"][1] == body["state"]


def test_upstream_auth_complete_exchanges_code(tmp_path, monkeypatch):
    """粘贴回调 → 换 token → 落库（不回传 token 到响应体）。"""
    app = build_app(_settings(tmp_path))

    async def fake_exchange(self, code, *, uuid, keyfrom):
        assert code == "CODE"
        assert uuid and keyfrom == "official"
        from src.provider.lobsterai.credential import LobsteraiCredential

        return LobsteraiCredential.from_dict({
            "accessToken": "AT2", "refreshToken": "RT2", "uuid": uuid,
            "firstKeyfrom": keyfrom, "latestKeyfrom": keyfrom,
            "uid": "uid-9", "nickname": "bob"})

    monkeypatch.setattr(LobsteraiClient, "exchange_code", fake_exchange)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        started = client.post("/api/auth/upstream/start",
                              json={"provider": "lobsterai"}).json()
        state = started["state"]
        # state 形如 uuid:keyfrom:upstream_state
        uuid, keyfrom, upstream_state = state.split(":", 2)
        assert uuid and keyfrom == "official" and upstream_state
        response = client.post("/api/auth/upstream/complete", json={
            "provider": "lobsterai", "state": state,
            "url": f"http://127.0.0.1:52641/auth/callback?code=CODE&state={upstream_state}"})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "success"
        assert "AT2" not in json.dumps(body)
        stored = app.state.credentials.candidates(["lobsterai"])
        assert len(stored) == 1
        data = app.state.credentials.credential_data(stored[0].credential_id)
        assert data["accessToken"] == "AT2"


def test_upstream_auth_complete_rejects_mismatched_state(tmp_path, monkeypatch):
    """粘贴的回调不属于当前这次登录 → 400（别把另一次登录的凭证贴进来）。"""
    app = build_app(_settings(tmp_path))
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        started = client.post("/api/auth/upstream/start",
                              json={"provider": "lobsterai"}).json()
        response = client.post("/api/auth/upstream/complete", json={
            "provider": "lobsterai", "state": started["state"],
            "url": "http://127.0.0.1:52641/auth/callback?code=C&state=OTHER"})
        assert response.status_code == 400


def test_model_alias_route_targets_lobsterai(tmp_path):
    """`模型@lobsterai` 强制路由到该渠道（KNOWN_PROVIDERS 认它）。"""
    from src.engine.model_resolver import resolve

    target = resolve("some-model@lobsterai", "glm-5.2")
    assert target.providers == ("lobsterai",)
    assert target.forced


def test_import_credential_rejects_non_object():
    with pytest.raises(UpstreamProtocolViolation):
        LobsteraiProvider().import_credential(None)  # type: ignore[arg-type]

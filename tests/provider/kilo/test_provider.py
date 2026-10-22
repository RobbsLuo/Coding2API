"""KiloProvider 委托、端点白名单与启动种子。"""

from __future__ import annotations

import httpx
import pytest

from src.provider.base import ErrKind, EventKind
from src.provider.kilo.client import (
    EP_MODELS,
    KILO_HOST,
    KiloClient,
    KiloProvider,
    Model,
    UpstreamHTTPError,
)
from tests.helpers.http import chunk_payload as chunk
from tests.helpers.http import sse_text_done as _sse
from tests.provider.kilo._support import client as _client

pytestmark = pytest.mark.real_upstream("kilo")


async def test_provider_probe_quota_is_unknown_not_exhausted():
    quota = await KiloProvider().probe_quota({})
    assert quota.probe_failed is True
    assert quota.total is None and quota.remaining is None
    assert quota.probed_at is not None

def test_provider_import_credential_is_empty():
    assert KiloProvider().import_credential({"anything": 1}) == {}

def test_provider_classify_delegates():
    assert KiloProvider().classify(429, b"") is ErrKind.MODEL

def test_provider_default_client_and_id():
    provider = KiloProvider()
    assert provider.id == "kilo"
    assert provider.client.host == KILO_HOST

async def test_provider_list_models_delegates():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == EP_MODELS
        return httpx.Response(200, json={"data": [{"id": "m/free", "isFree": True}]})

    provider = KiloProvider(client=_client(handler))
    models = await provider.list_models({})
    assert [m.id for m in models] == ["m/free"]
    await provider.aclose()

async def test_provider_stream_chat_with_and_without_pacer():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse(chunk({"content": "hi"}, finish="stop")))

    class Pacer:
        def __init__(self) -> None:
            self.turns = 0
            self.released = 0

        async def wait_turn(self, key=None) -> None:
            self.turns += 1

        def release(self, key=None) -> None:
            self.released += 1

    pacer = Pacer()
    provider = KiloProvider(client=_client(handler), pacer=pacer)
    events = [e async for e in provider.stream_chat({}, {"messages": []}, "m")]
    assert any(e.kind is EventKind.CONTENT for e in events)
    assert pacer.turns == 1 and pacer.released == 1     # 并发模式必须配对归还
    await provider.aclose()

    plain = KiloProvider(client=_client(handler))
    _ = [e async for e in plain.stream_chat({}, {"messages": []}, "m")]
    await plain.aclose()

async def test_provider_stream_chat_releases_pacer_on_error():
    """上游抛错也要归还名额，否则该桶被当成永远在途而失去节流。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    class Pacer:
        def __init__(self) -> None:
            self.released = 0

        async def wait_turn(self, key=None) -> None:
            pass

        def release(self, key=None) -> None:
            self.released += 1

    pacer = Pacer()
    provider = KiloProvider(client=_client(handler), pacer=pacer)
    with pytest.raises(UpstreamHTTPError):
        [e async for e in provider.stream_chat({}, {"messages": []}, "m")]
    assert pacer.released == 1
    await provider.aclose()

def test_kilo_endpoint_rejects_unallowlisted_host():
    from src.config import Settings
    from src.main import _kilo_endpoint
    from tests.helpers.constants import SECRET

    settings = Settings(_env_file=None, APP_SECRET=SECRET,
                        KILO_API_ENDPOINT="https://evil.example.com")
    with pytest.raises(ValueError, match="KILO_ALLOWED_ENDPOINTS"):
        _kilo_endpoint(settings)

def test_kilo_endpoint_accepts_default():
    from src.config import Settings
    from src.main import _kilo_endpoint
    from tests.helpers.constants import SECRET

    settings = Settings(_env_file=None, APP_SECRET=SECRET)
    assert _kilo_endpoint(settings) == KILO_HOST

def test_kilo_allowed_parses_comma_separated_endpoints():
    from src.config import Settings, validate_kilo_endpoint_allowed
    from tests.helpers.constants import SECRET

    settings = Settings(_env_file=None, APP_SECRET=SECRET,
                        KILO_ALLOWED_ENDPOINTS="https://api.kilo.ai/api/gateway/, https://alt.example.com")
    assert settings.kilo_allowed == ("https://api.kilo.ai/api/gateway",
                                     "https://alt.example.com")
    assert validate_kilo_endpoint_allowed("https://alt.example.com/", settings) is True
    assert validate_kilo_endpoint_allowed("https://nope.example.com", settings) is False

def test_seed_kilo_credential_is_idempotent(tmp_path):
    from src.db.conn import Database
    from src.db.crypto import CredentialCipher
    from src.db.migrate import apply_schema
    from src.db.repo import CredentialRepository
    from src.main import _seed_kilo_credential
    from tests.helpers.constants import SECRET

    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    repo = CredentialRepository(db, CredentialCipher(SECRET))

    _seed_kilo_credential(repo)
    _seed_kilo_credential(repo)                      # 已有 → 不再补
    rows = repo.candidates(["kilo"])
    assert len(rows) == 1
    assert repo.credential_data(rows[0].credential_id) == {}
    db.close()

async def test_build_app_seeds_kilo_and_resolves_forced_route(tmp_path):
    """端到端：默认装配种子 kilo 凭证，`模型@kilo` 能路由到 kilo 渠道。"""
    from src.config import Settings
    from src.engine.model_resolver import resolve
    from src.main import build_app
    from tests.helpers.constants import SECRET

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    assert "kilo" in app.state.executor._deps.providers
    rows = app.state.credentials.candidates(["kilo"])
    assert len(rows) == 1 and rows[0].health is None      # 未知而非耗尽
    target = resolve("some-model@kilo", "glm-5.2")
    assert target.providers == ("kilo",) and target.forced
    app.state.executor._deps.providers["kilo"].client          # noqa: B018 - 存在即可
    for provider in app.state.executor._deps.providers.values():
        closer = getattr(provider, "aclose", None)
        if callable(closer):
            await closer()

def test_kilo_credential_can_be_restored_after_delete(tmp_path, monkeypatch):
    """管理台删除 kilo 虚拟凭证后，可用「添加 Kilo」从 UI 补回（无需重启）。"""
    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token
    from src.config import Settings
    from src.main import build_app
    from tests.helpers.constants import SECRET

    async def fake_fetch_models(self):
        return [Model(id="offline/free", name="Kilo")]

    # TestClient 进 lifespan 会预热模型列表（真连上游），这里保持离线。
    monkeypatch.setattr(KiloClient, "fetch_models", fake_fetch_models)
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        credentials = app.state.credentials
        seeded = credentials.candidates(["kilo"])
        assert len(seeded) == 1
        assert credentials.delete(seeded[0].credential_id)          # 模拟管理台删除
        assert credentials.candidates(["kilo"]) == []

        client.cookies.set("coding2api_session",
                           create_session_token("root", SECRET))
        created = client.post("/api/credentials",
                              json={"provider": "kilo", "credential": {},
                                    "nickname": "Kilo"})
        assert created.status_code == 200
        credential_id = created.json()["id"]
        restored = credentials.candidates(["kilo"])
        assert [row.credential_id for row in restored] == [credential_id]
        assert credentials.credential_data(credential_id) == {}

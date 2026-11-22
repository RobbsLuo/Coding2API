"""ZenProvider 委托、端点白名单与启动种子。"""

from __future__ import annotations

import httpx
import pytest

from src.provider.base import ErrKind, EventKind
from src.provider.zen import events as zen_events
from src.provider.zen.client import EP_MODELS, MIN_OPENCODE_VERSION, Model, ZenClient, ZenProvider
from tests.helpers.http import chunk_payload as chunk
from tests.helpers.http import sse_text_done as _sse
from tests.provider.zen._support import client as _client

pytestmark = pytest.mark.real_upstream("zen")


async def test_provider_probe_quota_is_unknown_not_exhausted():
    quota = await ZenProvider().probe_quota({})
    assert quota.probe_failed is True
    assert quota.total is None and quota.remaining is None
    assert quota.probed_at is not None

def test_provider_import_credential_is_empty():
    assert ZenProvider().import_credential({"anything": 1}) == {}

def test_provider_import_credential_api_key_variants():
    provider = ZenProvider()
    assert provider.import_credential({"api_key": " sk-zen "}) == {"api_key": "sk-zen"}
    assert provider.import_credential({"apiKey": "sk-zen"}) == {"api_key": "sk-zen"}
    assert provider.import_credential({"token": "sk-zen"}) == {"api_key": "sk-zen"}
    assert provider.import_credential({"access_token": "sk-zen"}) == {"api_key": "sk-zen"}
    assert provider.import_credential("sk-zen") == {"api_key": "sk-zen"}
    # 空 / 非字符串 / 非 dict 一律回落匿名档（凭证行只是占位）
    assert provider.import_credential({"api_key": "   "}) == {}
    assert provider.import_credential({"api_key": 42}) == {}
    assert provider.import_credential([]) == {}

async def test_provider_passes_api_key_to_client():
    """带 Key 的凭证 → 清单与对话都走付费根 + Bearer。"""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, request.headers["Authorization"]))
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gpt-6-luna"}]})
        return httpx.Response(200, text=_sse(chunk({"content": "x"}, finish="stop")))

    provider = ZenProvider(client=_client(handler))
    models = await provider.list_models({"api_key": "sk-zen"})
    assert [m.id for m in models] == ["gpt-6-luna"]
    _ = [e async for e in provider.stream_chat(
        {"api_key": "sk-zen"}, {"messages": []}, "gpt-6-luna")]
    assert calls == [("/inference/v1/models", "Bearer sk-zen"),
                     ("/inference/openai/v1/chat/completions", "Bearer sk-zen")]
    await provider.aclose()

async def test_provider_blank_key_falls_back_to_anonymous():
    """坏数据（None / 空白 / 非字符串）不能被发成 `Bearer None`。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={"data": []})

    provider = ZenProvider(client=_client(handler))
    for data in ({"api_key": None}, {"api_key": "  "}, {"api_key": 7}, {}):
        with pytest.raises(zen_events.UpstreamProtocolViolation):
            await provider.list_models(data)
    assert seen == ["Bearer public"] * 4
    await provider.aclose()

def test_provider_classify_delegates():
    assert ZenProvider().classify(429, b"") is ErrKind.SOFT

async def test_provider_list_models_delegates():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_MODELS:
            return httpx.Response(200, json={"data": [{"id": "m1-free"}]})
        return httpx.Response(200, text=_sse(chunk({"content": "x"}, finish="stop")))

    provider = ZenProvider(client=_client(handler))
    models = await provider.list_models({})
    assert [m.id for m in models] == ["m1-free"]
    await provider.aclose()

async def test_provider_stream_chat_with_and_without_pacer():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse(chunk({"content": "hi"}, finish="stop")))

    class Pacer:
        def __init__(self) -> None:
            self.turns = 0

        async def wait_turn(self, key=None) -> None:
            self.turns += 1

        def release(self, key=None) -> None:
            pass

    pacer = Pacer()
    provider = ZenProvider(client=_client(handler), pacer=pacer)
    events = [e async for e in provider.stream_chat({}, {"messages": []}, "m")]
    assert any(e.kind is EventKind.CONTENT for e in events)
    assert pacer.turns == 1
    await provider.aclose()

    plain = ZenProvider(client=_client(handler))
    _ = [e async for e in plain.stream_chat({}, {"messages": []}, "m")]
    await plain.aclose()

def test_provider_default_client_uses_min_version():
    provider = ZenProvider()
    assert provider.client.version == MIN_OPENCODE_VERSION
    assert provider.id == "zen"

def test_zen_endpoint_rejects_unallowlisted_host():
    from src.config import Settings
    from src.main import _zen_endpoint
    from tests.helpers.constants import SECRET

    settings = Settings(_env_file=None, APP_SECRET=SECRET,
                        ZEN_API_ENDPOINT="https://evil.example.com")
    with pytest.raises(ValueError, match="ZEN_ALLOWED_ENDPOINTS"):
        _zen_endpoint(settings)

def test_zen_endpoint_accepts_trailing_slash():
    from src.config import Settings
    from src.main import _zen_endpoint
    from tests.helpers.constants import SECRET

    settings = Settings(_env_file=None, APP_SECRET=SECRET,
                        ZEN_API_ENDPOINT="https://opencode.ai/")
    assert _zen_endpoint(settings) == "https://opencode.ai/"

def test_seed_zen_credential_is_idempotent(tmp_path):
    from src.db.conn import Database
    from src.db.crypto import CredentialCipher
    from src.db.migrate import apply_schema
    from src.db.repo import CredentialRepository
    from src.main import _seed_zen_credential
    from tests.helpers.constants import SECRET

    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    repo = CredentialRepository(db, CredentialCipher(SECRET))

    _seed_zen_credential(repo)
    _seed_zen_credential(repo)                      # 已有 → 不再补
    rows = repo.candidates(["zen"])
    assert len(rows) == 1
    assert repo.credential_data(rows[0].credential_id) == {}
    db.close()

async def test_build_app_seeds_zen_and_resolves_forced_route(tmp_path):
    """端到端：默认装配种子 zen 凭证，`模型@zen` 能路由到 zen 渠道。"""
    from src.config import Settings
    from src.engine.model_resolver import resolve
    from src.main import build_app
    from tests.helpers.constants import SECRET

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    assert "zen" in app.state.executor._deps.providers
    rows = app.state.credentials.candidates(["zen"])
    assert len(rows) == 1 and rows[0].health is None      # 未知而非耗尽
    target = resolve("big-pickle@zen", "glm-5.2")
    assert target.providers == ("zen",) and target.forced
    app.state.executor._deps.providers["zen"].client          # noqa: B018 - 存在即可
    for provider in app.state.executor._deps.providers.values():
        closer = getattr(provider, "aclose", None)
        if callable(closer):
            await closer()

def test_zen_credential_can_be_restored_after_delete(tmp_path, monkeypatch):
    """管理台删除 zen 虚拟凭证后，可用「添加 OpenCode Zen」从 UI 补回（无需重启）。

    回归背景：种子只在 build_app 跑一次，用户删掉凭证后 /v1/models 就再也
    没有 zen；前端「登录渠道账号」面板提供的一键补回走通用导入端点
    （provider=zen 时 import_credential 忽略入参、落空对象），这条链路必须稳定。
    """
    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token
    from src.config import Settings
    from src.main import build_app
    from tests.helpers.constants import SECRET

    async def fake_fetch_models(self, api_key=""):
        return [Model(id="offline-free", name="Offline")]

    # TestClient 进 lifespan 会预热模型列表（真连上游），这里保持离线。
    monkeypatch.setattr(ZenClient, "fetch_models", fake_fetch_models)
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        credentials = app.state.credentials
        seeded = credentials.candidates(["zen"])
        assert len(seeded) == 1
        assert credentials.delete(seeded[0].credential_id)          # 模拟管理台删除
        assert credentials.candidates(["zen"]) == []

        client.cookies.set("coding2api_session",
                           create_session_token("root", SECRET))
        created = client.post("/api/credentials",
                              json={"provider": "zen", "credential": {},
                                    "nickname": "OpenCode Zen"})
        assert created.status_code == 200
        credential_id = created.json()["id"]
        restored = credentials.candidates(["zen"])
        assert [row.credential_id for row in restored] == [credential_id]
        assert credentials.credential_data(credential_id) == {}

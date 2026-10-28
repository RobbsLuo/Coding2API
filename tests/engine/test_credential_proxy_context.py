"""凭证上下文 → 出口代理：engine 设置、provider 读取、轮换不串号。"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from src.engine.executor import Executor, ExecutorDeps
from src.engine.scheduler import Scheduler
from src.provider.base import Event, EventKind
from src.provider.lazy_clients import LazyClients
from src.provider.proxy import CredentialProxyPool, set_proxy_pool
from src.request_context import get_credential_id


class _RecordingProvider:
    """记录每次 stream_chat 迭代时上下文里的凭证 id。"""

    id = "stub"

    def __init__(self) -> None:
        self.seen: list[str | None] = []

    async def stream_chat(self, credential_data, payload, model) -> AsyncIterator[Event]:
        self.seen.append(get_credential_id())
        yield Event(kind=EventKind.CONTENT, content="ok")


def _executor(provider) -> Executor:
    return Executor(ExecutorDeps(providers={provider.id: provider},
                                 credentials=object(), scheduler=Scheduler(),
                                 default_model="m"))


async def _drain(source) -> None:
    async for _ in source:
        pass


async def test_stream_source_scopes_credential_id():
    provider = _RecordingProvider()
    executor = _executor(provider)
    await _drain(executor._stream_source("stub", {}, {}, "m", "cred_1"))
    assert provider.seen == ["cred_1"]


async def test_credential_switch_does_not_leak_previous_id():
    """换号重试：每次调用都重设上下文，不会沿用上一个凭证的出口。"""
    provider = _RecordingProvider()
    executor = _executor(provider)
    await _drain(executor._stream_source("stub", {}, {}, "m", "cred_1"))
    await _drain(executor._stream_source("stub", {}, {}, "m", "cred_2"))
    assert provider.seen == ["cred_1", "cred_2"]


async def test_missing_credential_id_clears_context():
    provider = _RecordingProvider()
    executor = _executor(provider)
    await _drain(executor._stream_source("stub", {}, {}, "m", None))
    assert provider.seen == [None]


def test_provider_pool_follows_context_credential(monkeypatch):
    """端到端：同一凭证 → 同一出口池；不同凭证可命中不同出口。"""
    set_proxy_pool(CredentialProxyPool(("http://a:1", "http://b:2")))
    used: list[str | None] = []

    def fake(**kwargs):
        used.append(kwargs.get("proxy"))
        return httpx.AsyncClient()

    monkeypatch.setattr("src.provider.lazy_clients.build_client", fake)

    class _Client(LazyClients):
        def __init__(self) -> None:
            self._stream_client = None
            self._short_client = None
            self._proxy = None

    client = _Client()
    from src.request_context import credential_scope

    with credential_scope("cred_a"):
        first = client._stream
        assert client._stream is first
    assert used and used[0] in ("http://a:1", "http://b:2")
    set_proxy_pool(CredentialProxyPool())


@pytest.fixture(autouse=True)
def _reset_pool():
    yield
    set_proxy_pool(CredentialProxyPool())

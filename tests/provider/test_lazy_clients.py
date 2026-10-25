"""渠道客户端共享的懒建连接池。

7 家原先各写一份「两个懒建 client + aclose」，差异只有 property/方法两种写法。
这里直接测共享实现：懒建、复用、按需释放。
"""

from __future__ import annotations

import httpx
import pytest

from src.provider.lazy_clients import LazyClients


class _Client(LazyClients):
    def __init__(self, *, stream=None, short=None, proxy=None) -> None:
        self._stream_client = stream
        self._short_client = short
        self._proxy = proxy


def test_pools_are_lazy_and_reused():
    client = _Client()
    assert client._stream_client is None and client._short_client is None

    stream = client._stream
    short = client._short

    assert client._stream is stream          # 复用同一实例，不重建
    assert client._short is short
    assert stream is not short               # 两条独立的池


def test_injected_clients_are_used_as_is():
    """注入的 client（测试与复用场景）不能被替换掉。"""
    given = httpx.AsyncClient()
    client = _Client(stream=given)
    assert client._stream is given


def test_proxy_is_forwarded_to_both_pools(monkeypatch):
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        return httpx.AsyncClient()

    monkeypatch.setattr("src.provider.lazy_clients.build_client", fake)
    client = _Client(proxy="socks5://127.0.0.1:1080")

    assert client._stream is not None
    assert client._short is not None
    assert calls[0]["proxy"] == "socks5://127.0.0.1:1080"
    assert calls[1]["proxy"] == "socks5://127.0.0.1:1080"
    # 两条池用不同的超时（流式读无上限，短请求整体 30s）
    assert calls[0]["timeout"].read is None
    assert calls[1]["timeout"].read == 30.0


async def test_aclose_skips_pools_never_created():
    """只建了 stream 时，aclose 不能因 short 为 None 而崩（懒建的意义）。"""
    client = _Client()
    assert client._stream is not None
    assert client._short_client is None
    await client.aclose()


async def test_aclose_is_idempotent():
    client = _Client()
    stream, short = client._stream, client._short
    await client.aclose()
    assert stream.is_closed and short.is_closed
    # 再关一次不该抛（客户端已 closed 时 aclose 是 no-op）
    await client.aclose()


async def test_aclose_with_no_pools_is_noop():
    client = _Client()
    await client.aclose()          # 一条都没建过


@pytest.mark.parametrize("attr", ["_stream", "_short"])
def test_pools_ignore_environment_proxy(attr):
    """`trust_env=False` 是既有约定：不吃 HTTP_PROXY，避免劫持带 Token 的请求。"""
    client = _Client()
    assert getattr(client, attr).trust_env is False

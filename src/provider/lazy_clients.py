"""渠道客户端的懒建 HTTP 连接池。

7 个渠道各写一份「两个懒建 client + aclose」，差异只有 `@property` 与方法
两种写法。统一成 property：调用点一律 `self._stream` / `self._short`。

为什么懒建：绝大多数请求只用其中一条池（聊天走 stream、探活走 short），
两个都预建会白占一份连接池；而未建的那条在 `aclose()` 里跳过即可。

**按凭证分池**（出口 IP 粘性）：`CREDENTIAL_PROXY_POOL` 非空时，同一凭证的
请求固定走同一个出口代理。`httpx` 的代理是连接池级参数，所以每条出口各建
一份池（`_pools[(流/短, 代理)]`）——不能在同一 client 上按请求换代理。
上下文里的凭证 id 由 engine 层设置（`request_context.get_credential_id`）。
未配置池时行为与之前逐字节一致（始终只有一条池）。
"""

from __future__ import annotations

import httpx

from ..request_context import get_credential_id
from .proxy import SHORT_TIMEOUT, STREAM_TIMEOUT, build_client, credential_proxy


class LazyClients:
    """`_stream` / `_short` 两条按需建立的连接池 + 统一释放。

    子类在 `__init__` 里设置 `_stream_client` / `_short_client` / `_proxy`
    （前三项为 None 表示「按需自建」）。显式注入的 client（测试替身）永远优先，
    且不参与按凭证分池——替身本来就不出网。

    `_pools` 由本类懒初始化（子类普遍不调 `super().__init__`，不能依赖构造）。
    """

    _stream_client: httpx.AsyncClient | None
    _short_client: httpx.AsyncClient | None
    _proxy: str | None

    def _effective_proxy(self) -> str | None:
        """当前请求的出口：凭证池分配优先，其次渠道级 `PROVIDER_PROXIES`。"""
        return credential_proxy(get_credential_id()) or self._proxy

    def _pooled(self, timeout: httpx.Timeout, kind: str) -> httpx.AsyncClient:
        proxy = self._effective_proxy()
        pools: dict[tuple[str, str | None], httpx.AsyncClient] | None = getattr(
            self, "_pools", None)
        if pools is None:
            pools = {}
            self._pools = pools
        key = (kind, proxy)
        client = pools.get(key)
        if client is None:
            client = build_client(timeout=timeout, proxy=proxy)
            pools[key] = client
        return client

    @property
    def _stream(self) -> httpx.AsyncClient:
        """流式请求池：读无上限（SSE 长连接），写 60s（大 payload 也要发完）。"""
        if self._stream_client is not None:
            return self._stream_client
        return self._pooled(STREAM_TIMEOUT, "stream")

    @property
    def _short(self) -> httpx.AsyncClient:
        """短请求池（探活 / 额度 / 模型列表）：整体 30s。"""
        if self._short_client is not None:
            return self._short_client
        return self._pooled(SHORT_TIMEOUT, "short")

    async def aclose(self) -> None:
        """释放全部池（默认池 + 各出口池）；从未建立的跳过。"""
        clients = [self._stream_client, self._short_client]
        clients.extend(getattr(self, "_pools", {}).values())
        for client in clients:
            if client is not None:
                await client.aclose()

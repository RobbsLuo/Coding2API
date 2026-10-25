"""渠道客户端的懒建 HTTP 连接池。

7 个渠道各写一份「两个懒建 client + aclose」，差异只有 `@property` 与方法
两种写法。统一成 property：调用点一律 `self._stream` / `self._short`。

为什么懒建：绝大多数请求只用其中一条池（聊天走 stream、探活走 short），
两个都预建会白占一份连接池；而未建的那条在 `aclose()` 里跳过即可。
"""

from __future__ import annotations

import httpx

from .proxy import SHORT_TIMEOUT, STREAM_TIMEOUT, build_client


class LazyClients:
    """`_stream` / `_short` 两条按需建立的连接池 + 统一释放。

    子类在 `__init__` 里设置 `_stream_client` / `_short_client` / `_proxy`
    （前三项为 None 表示「按需自建」）。
    """

    _stream_client: httpx.AsyncClient | None
    _short_client: httpx.AsyncClient | None
    _proxy: str | None

    @property
    def _stream(self) -> httpx.AsyncClient:
        """流式请求池：读无上限（SSE 长连接），写 60s（大 payload 也要发完）。"""
        if self._stream_client is None:
            self._stream_client = build_client(timeout=STREAM_TIMEOUT,
                                               proxy=self._proxy)
        return self._stream_client

    @property
    def _short(self) -> httpx.AsyncClient:
        """短请求池（探活 / 额度 / 模型列表）：整体 30s。"""
        if self._short_client is None:
            self._short_client = build_client(timeout=SHORT_TIMEOUT,
                                              proxy=self._proxy)
        return self._short_client

    async def aclose(self) -> None:
        """释放两条池；从未建立的跳过（懒建的意义就在这）。"""
        for client in (self._stream_client, self._short_client):
            if client is not None:
                await client.aclose()

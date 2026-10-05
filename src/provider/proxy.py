"""按渠道出站代理（P1-6）。

`PROVIDER_PROXIES` 是**启动期**配置：代理作用于连接池（`httpx.AsyncClient`），
运行中改值需要重建连接池，故不做热更（与上游端点同类）。格式：

    PROVIDER_PROXIES="trae=http://127.0.0.1:7890;codebuddy=socks5://127.0.0.1:1080"

留空 = 全部直连（默认，行为不变）。协议支持 http / https / socks5 / socks5h
（SOCKS 需 `httpx[socks]`，见 pyproject）。
"""

from __future__ import annotations

from collections.abc import Iterable

import httpx

PROXY_SCHEMES: tuple[str, ...] = ("http", "https", "socks5", "socks5h")


def parse_provider_proxies(raw: str, known: Iterable[str]) -> dict[str, str]:
    """`PROVIDER_PROXIES` 文本 → `{渠道: 代理 URL}`。

    严格解析（启动期配置，宁可启动失败也不静默走直连——代理常带合规/隐私
    意图，静默直连比报错更糟）：未知渠道、非法协议、缺 `=`、空 URL 一律
    `ValueError`；只忽略空段（容忍结尾 `;`）。渠道名小写、重名后者覆盖。
    """
    allowed = frozenset(known)
    result: dict[str, str] = {}
    for segment in raw.split(";"):
        segment = segment.strip()
        if not segment:
            continue
        provider, sep, url = segment.partition("=")
        provider = provider.strip().lower()
        url = url.strip()
        if not sep or not provider or not url:
            raise ValueError(
                f"PROVIDER_PROXIES 段格式非法（应为 渠道=代理URL）: {segment!r}")
        if provider not in allowed:
            raise ValueError(
                f"PROVIDER_PROXIES 未知渠道 {provider!r}"
                f"（可选: {', '.join(sorted(allowed))}）")
        scheme = url.split("://", 1)[0].lower() if "://" in url else ""
        if scheme not in PROXY_SCHEMES:
            raise ValueError(
                f"PROVIDER_PROXIES 代理协议不支持: {url!r}"
                f"（可选: {', '.join(PROXY_SCHEMES)}）")
        result[provider] = url
    return result


def build_client(*, timeout: httpx.Timeout, proxy: str | None = None) -> httpx.AsyncClient:
    """构造上游 HTTP 客户端：显式按渠道代理优先，永远忽略环境代理。

    `trust_env=False` 是既有约定——不吃 `HTTP_PROXY` 等环境变量，避免部署环境
    的全局代理意外劫持带 Token 的上游请求；按渠道代理只经 `proxy=` 显式生效。
    """
    return httpx.AsyncClient(timeout=timeout, trust_env=False, proxy=proxy)
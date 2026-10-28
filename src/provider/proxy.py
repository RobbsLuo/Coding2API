"""按渠道出站代理。

`PROVIDER_PROXIES` 是**启动期**配置：代理作用于连接池（`httpx.AsyncClient`），
运行中改值需要重建连接池，故不做热更（与上游端点同类）。格式：

    PROVIDER_PROXIES="trae=http://127.0.0.1:7890;codebuddy=socks5://127.0.0.1:1080"

留空 = 全部直连（默认，行为不变）。协议支持 http / https / socks5 / socks5h
（SOCKS 需 `httpx[socks]`，见 pyproject）。
"""

from __future__ import annotations

import hashlib
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


def parse_proxy_pool(raw: str) -> tuple[str, ...]:
    """`CREDENTIAL_PROXY_POOL` 文本 → 代理 URL 元组（启动期配置）。

    格式与 `PROVIDER_PROXIES` 一致（分号分隔、严格协议校验、忽略空段），
    但没有 `渠道=` 前缀：这是一个**全局出口池**，按凭证 id 稳定哈希取桶。
    重复项按首次出现去重（池子写重了不会让某出口被抽中两次）。
    """
    urls: list[str] = []
    for segment in raw.split(";"):
        segment = segment.strip()
        if not segment:
            continue
        scheme = segment.split("://", 1)[0].lower() if "://" in segment else ""
        if scheme not in PROXY_SCHEMES:
            raise ValueError(
                f"CREDENTIAL_PROXY_POOL 代理协议不支持: {segment!r}"
                f"（可选: {', '.join(PROXY_SCHEMES)}）")
        if segment not in urls:
            urls.append(segment)
    return tuple(urls)


class CredentialProxyPool:
    """按凭证 id 稳定分配出口（「一号一出口」）。

    分配用 `sha256(credential_id)` 取模而不是哈希表轮转：同一凭证在**重启后**
    仍落在同一出口——出口稳定性正是上游风控在看的信号，进程内轮转会让每次
    重启都换 IP，等于没做。池子增删成员会让部分凭证换桶，这是可接受的运维
    代价（改池子本身就是要换出口）。

    空池 / 无凭证 id（虚拟凭证、后台无上下文）→ None，调用方回落渠道级代理。
    """

    def __init__(self, urls: Iterable[str] = ()) -> None:
        self._urls = tuple(urls)

    def __bool__(self) -> bool:
        return bool(self._urls)

    def __len__(self) -> int:
        return len(self._urls)

    def for_credential(self, credential_id: str | None) -> str | None:
        if not self._urls or not credential_id:
            return None
        digest = hashlib.sha256(credential_id.encode("utf-8")).digest()
        index = int.from_bytes(digest[:8], "big") % len(self._urls)
        return self._urls[index]


# 启动期全局池：provider 构造时不逐个传参（8 个渠道 × 构造点太多），
# 与 `PROVIDER_PROXIES` 同生命周期——改值必须重启。
_POOL = CredentialProxyPool()


def set_proxy_pool(pool: CredentialProxyPool) -> None:
    """装配全局出口池（`build_app` 启动时调用一次；测试可直接替换）。"""
    global _POOL
    _POOL = pool


def get_proxy_pool() -> CredentialProxyPool:
    return _POOL


def credential_proxy(credential_id: str | None) -> str | None:
    """凭证 → 出口代理 URL；未配置池或虚拟凭证返回 None。"""
    return _POOL.for_credential(credential_id)


def build_client(*, timeout: httpx.Timeout, proxy: str | None = None) -> httpx.AsyncClient:
    """构造上游 HTTP 客户端：显式按渠道代理优先，永远忽略环境代理。

    `trust_env=False` 是既有约定——不吃 `HTTP_PROXY` 等环境变量，避免部署环境
    的全局代理意外劫持带 Token 的上游请求；按渠道代理只经 `proxy=` 显式生效。
    """
    return httpx.AsyncClient(timeout=timeout, trust_env=False, proxy=proxy)


# 流式请求：读无上限（SSE 长连接），写 60s（大 payload 也要发完），连接/取池各 10s。
STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=60.0, pool=10.0)
# 短请求（探活 / 额度 / 模型列表）：整体 30s。
SHORT_TIMEOUT = httpx.Timeout(30.0)
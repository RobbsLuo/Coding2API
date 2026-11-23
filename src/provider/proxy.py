"""按渠道出站代理。

`PROVIDER_PROXIES` 是**启动期**配置：代理作用于连接池（`httpx.AsyncClient`），
运行中改值需要重建连接池，故不做热更（与上游端点同类）。格式：

    PROVIDER_PROXIES="trae=http://127.0.0.1:7890;codebuddy=socks5://127.0.0.1:1080"

留空 = 全部直连（默认，行为不变）。协议支持 http / https / socks5 / socks5h
（SOCKS 需 `httpx[socks]`，见 pyproject）。

**出口熔断**：坏代理的代价是「每个请求 × 每个账号各撞一次超时」，而且每次
超时都会给代理后面的健康账号写一条冷却。`EgressBreaker` 按代理 URL 记连续
失败数，打开后经该代理的请求**在发往上游之前**直接拒绝（`EgressUnavailable`，
**不写凭证冷却**）——见 `EgressUnavailable` / `EgressBreaker` 的注释。异常
自带 `code="egress_unavailable"` + `retry_after`（`Retry-After` 头）；响应
信封待 `webapp/handlers.py` 接线，本层不碰 HTTP。
"""

from __future__ import annotations

import hashlib
import math
import ssl
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import httpx

from .base import ErrKind

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


def build_client(*, timeout: httpx.Timeout, proxy: str | None = None,
                 breaker: EgressBreaker | None = None) -> httpx.AsyncClient:
    """构造上游 HTTP 客户端：显式按渠道代理优先，永远忽略环境代理。

    `trust_env=False` 是既有约定——不吃 `HTTP_PROXY` 等环境变量，避免部署环境
    的全局代理意外劫持带 Token 的上游请求；按渠道代理只经 `proxy=` 显式生效。

    客户端按代理 URL 挂到 `EgressBreaker` 上（`proxy` 为 None 即直连，不参与
    熔断——直连没有「哪个出口坏了」这回事）。`breaker` 只在测试里显式传。
    """
    return _EgressGuardedClient(
        timeout=timeout,
        trust_env=False,
        proxy=proxy,
        limits=UPSTREAM_LIMITS,
        breaker=breaker or get_egress_breaker(),
        egress_proxy=proxy,
    )


# ---------------------------------------------------------------- 出口熔断

# 连续失败 → 打开时长：首次 30s，之后按连续失败数翻倍，封顶 5min。
# 为什么不一开就 5min：坏代理常常是「进程重启中 / 抖动」，首次短冷却让恢复
# 足够快；真坏（连续探测失败）才升级到 5min，避免每 30s 往死代理上打一枪。
EGRESS_FAILURE_COOLDOWN_SECONDS = 30.0
EGRESS_MAX_COOLDOWN_SECONDS = 300.0

# 「出口挂了」的异常形态：代理拒绝凭证（407 由 httpcore 抛 `ProxyError`）、
# 连不上（`ConnectError` 含 DNS / TCP / TLS 握手）、连接超时。
# **不含** `ReadTimeout` / `WriteTimeout`：读超时可能是上游慢而不是出口坏，
# 拿它熔断会把「模型在思考」误判成「代理挂了」。
_EGRESS_FAILURE_TYPES: tuple[type[BaseException], ...] = (
    httpx.ProxyError,
    httpx.ConnectError,
    httpx.ConnectTimeout,
    ssl.SSLError,
)


def _retry_after(seconds: float) -> int:
    """`Retry-After` 秒数：HTTP 头只认整数秒，向上取整且至少 1。"""
    return max(1, math.ceil(seconds))


class EgressUnavailable(httpx.ProxyError):
    """出口代理已被熔断：请求在**发往上游之前**就被拒绝。

    继承 `httpx.ProxyError` 是有意的：引擎的 `_classify` 靠
    `httpx.ProxyError` 认出「本机代理通道不可用」并归 `NETWORK`，而
    `NETWORK ∈ NO_PENALTY_KINDS`——不冷却、不累计、不落错写库。**这是本类
    存在的全部意义**：坏出口不能顺手把代理后面一整池健康账号写成冷却。

    `code` / `retry_after` 是 HTTP 出口的契约（`egress_unavailable` +
    `Retry-After`），响应信封在 `webapp/handlers.py`，不在本层。
    """

    code = "egress_unavailable"

    def __init__(self, proxy: str, retry_after: int) -> None:
        self.proxy = proxy
        self.retry_after = retry_after
        super().__init__(f"egress proxy unavailable: {proxy}")

    def kind(self) -> ErrKind:
        return ErrKind.NETWORK


@dataclass(slots=True)
class _EgressState:
    """单个出口的熔断状态（进程内，不落库）。"""

    failures: int = 0
    open_until: float = 0.0
    # 半开探测位：到期后只放 1 个请求过去，其余继续拒绝
    probing: bool = False


class EgressBreaker:
    """进程内出口熔断器：按代理 URL 记「连续失败数 + 到期时间」。

    **为什么不需要锁**：全部状态只在协程里读写，且每个方法都是纯同步函数
    （`check` / `record_failure` / `record_success` 内部没有 `await`）。
    asyncio 是单线程协作式调度，同步代码不会在执行中途被切走，「读-判-写」
    天然原子。若将来把调用点挪进 `run_in_executor` 或线程，必须补锁。

    **状态不落库**：出口是启动期配置（改 `PROVIDER_PROXIES` /
    `CREDENTIAL_PROXY_POOL` 都要重启），进程内状态随重启清零正是想要的
    ——重启后第一枪重新探测，不必人工清冷却。

    **半开**：`open_until` 到期后只放 1 个真实请求（`probing` 标记）去探，
    成功即清空状态恢复、失败按连续失败数指数续期。探测请求本身挂着不返回时
    熔断保持打开（fail-closed）：客户端自有超时（30s / 60s），不会永久卡死。
    """

    def __init__(self, *, cooldown: float = EGRESS_FAILURE_COOLDOWN_SECONDS,
                 max_cooldown: float = EGRESS_MAX_COOLDOWN_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._cooldown = cooldown
        self._max_cooldown = max_cooldown
        # `time.monotonic` 而不是 `time.time`：墙钟被 NTP 回拨时，用 epoch
        # 判到期会让熔断「永不到期」或「立刻到期」，monotonic 不受影响。
        self._clock = clock
        self._states: dict[str, _EgressState] = {}

    def check(self, proxy: str) -> None:
        """放行或拒绝：打开中（或探测已被占）抛 `EgressUnavailable`。"""
        state = self._states.get(proxy)
        if state is None:
            return
        now = self._clock()
        if now < state.open_until:
            raise EgressUnavailable(proxy, _retry_after(state.open_until - now))
        if state.probing:
            # 已有 1 个探测在途：其余请求继续快速失败（Retry-After 至少 1s）
            raise EgressUnavailable(proxy, 1)
        state.probing = True

    def record_success(self, proxy: str) -> None:
        """出口可用：清空状态（连续失败数归零，下次失败从 30s 重新计）。"""
        self._states.pop(proxy, None)

    def record_failure(self, proxy: str) -> float:
        """出口失败：打开/续期并返回本次冷却秒数（供 `Retry-After`）。"""
        state = self._states.setdefault(proxy, _EgressState())
        state.failures += 1
        state.probing = False
        # 指数上限先夹住再乘：长期挂掉的出口失败数可以到几千，直接
        # `2 ** (failures - 1)` 会 OverflowError（float 转换溢出）而不是封顶。
        backoff = min(self._cooldown * 2 ** min(state.failures - 1, 16),
                      self._max_cooldown)
        state.open_until = self._clock() + backoff
        return backoff

    def open_proxies(self) -> dict[str, float]:
        """当前打开的出口 → 距恢复秒数（管理端点 / 告警读取用）。"""
        now = self._clock()
        return {proxy: state.open_until - now
                for proxy, state in self._states.items()
                if now < state.open_until}


class _EgressGuardedClient(httpx.AsyncClient):
    """带出口熔断的 `httpx.AsyncClient`：发请求前问熔断器，失败后回写状态。

    挂在 `send()` 上是唯一不漏的做法：`get` / `post` / `stream` / 重定向
    全部收敛到它，而 8 个渠道的请求点各写一遍必然漏（与引擎 `_classify`
    「放在唯一错误出口」同一纪律）。

    只覆盖「拿到响应头之前」的失败：`send()` 返回后流内读失败（死连接
    只发不收）不在此列——那属于连接池层面的回收，见 `UPSTREAM_LIMITS`。
    """

    def __init__(self, *, breaker: EgressBreaker,
                 egress_proxy: str | None = None,
                 **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._egress_breaker = breaker
        self._egress_proxy = egress_proxy

    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        proxy = self._egress_proxy
        if proxy is None:
            # 直连：没有「哪个出口坏了」，不进熔断器
            return await super().send(request, **kwargs)
        breaker = self._egress_breaker
        breaker.check(proxy)          # 打开中 → 发往上游之前就抛
        try:
            response = await super().send(request, **kwargs)
        except _EGRESS_FAILURE_TYPES:
            breaker.record_failure(proxy)
            raise
        except BaseException:
            # 非出口故障（读/写超时、取消、协议错）：请求已经发到上游、出口
            # 是通的。必须回写成功——否则半开探测被取消后 `probing` 永久占位，
            # 该出口会一直 503（fail-closed 卡死）。
            breaker.record_success(proxy)
            raise
        if response.status_code == 407:
            # 代理自身拒绝：这次失败是出口的问题，不是凭证的。必须转成
            # `EgressUnavailable`，否则 407 会被渠道分类成 `OTHER` 并给
            # 一个完全健康的凭证写冷却。
            await response.aclose()
            raise EgressUnavailable(proxy, _retry_after(
                breaker.record_failure(proxy)))
        # 任何响应（含 4xx/5xx）都证明出口是通的：上游的错误不该熔断出口
        breaker.record_success(proxy)
        return response


# 启动期全局熔断器：与 `_POOL` 同生命周期（改配置要重启），管理端点经
# `get_egress_breaker()` 读取，测试用显式 `breaker=` 隔离。
_BREAKER = EgressBreaker()


def get_egress_breaker() -> EgressBreaker:
    return _BREAKER


# ------------------------------------------------- 上游连接池（死连接回收）
#
# C5「HTTP/2 PING 空闲探测」在 httpx 里**没有公开入口**：`keepalive_expiry`
# 只作用于 HTTP/1.1（httpcore `http11._expire_at`），HTTP/2 连接既没有到期
# 时间也没有 PING 开关；开 `http2=True` 还要额外装 `h2`（本仓未装，见
# pyproject），而多数上游端点本来也走 1.1。故只取 httpx 暴露的**唯一**杠杆：
# 把空闲连接的存活上限从默认 5s 压到 3s——网络中断/代理静默丢包后，「只发
# 不收」的老连接在池里最多活 3s 就被丢弃，下个请求重新建连，不会把 30s~60s
# 的非流式请求挂到总超时。
#
# 取舍：TTL 越短越早赶走死连接，但并发下会牺牲连接复用（每次重连多一次
# TCP+TLS 甚至代理 CONNECT）。3s 是折中：比一个「思考停顿」更短，突发流量
# 仍能复用热连接。**它替代不了 PING**——连接在请求中途死掉仍然靠超时兜底，
# 真正的心跳探测需要自建 HTTP/2 传输层（见交付说明的降级建议）。
UPSTREAM_KEEPALIVE_EXPIRY_SECONDS = 3.0
UPSTREAM_MAX_CONNECTIONS = 100
UPSTREAM_MAX_KEEPALIVE_CONNECTIONS = 20
UPSTREAM_LIMITS = httpx.Limits(
    max_connections=UPSTREAM_MAX_CONNECTIONS,
    max_keepalive_connections=UPSTREAM_MAX_KEEPALIVE_CONNECTIONS,
    keepalive_expiry=UPSTREAM_KEEPALIVE_EXPIRY_SECONDS,
)


# 流式请求：读无上限（SSE 长连接），写 60s（大 payload 也要发完），连接/取池各 10s。
STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=60.0, pool=10.0)
# 短请求（探活 / 额度 / 模型列表）：整体 30s。
SHORT_TIMEOUT = httpx.Timeout(30.0)

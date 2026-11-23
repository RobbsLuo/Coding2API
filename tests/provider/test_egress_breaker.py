"""出口熔断（C1）：坏代理只产生 1 次真实失败，后续快速失败且不冷却凭证。

覆盖三层：

* `EgressBreaker` 状态机（打开 / 半开 / 恢复 / 指数续期）
* `_EgressGuardedClient` 的挂载点（`send` 之前拒绝、407、读超时不算出口失败）
* `build_client` 的接线与连接池参数（C5 的死连接回收）

`EgressUnavailable` 的契约单独断言：必须是 `httpx.ProxyError` 且 `kind()` 为
`NETWORK`——那是「不写凭证冷却」的唯一实现（见 `NO_PENALTY_KINDS`）。
"""

from __future__ import annotations

import httpx
import pytest

from src.provider.base import NO_PENALTY_KINDS, ErrKind
from src.provider.proxy import (
    EGRESS_FAILURE_COOLDOWN_SECONDS,
    EGRESS_MAX_COOLDOWN_SECONDS,
    UPSTREAM_KEEPALIVE_EXPIRY_SECONDS,
    EgressBreaker,
    EgressUnavailable,
    _EgressGuardedClient,
    _retry_after,
    build_client,
    get_egress_breaker,
)

PROXY = "http://127.0.0.1:7890"
UPSTREAM = "https://api.example.com/v1/models"


class _Clock:
    """可推进的 monotonic 时钟（避免测试里真睡 30s）。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _client(handler, *, breaker: EgressBreaker,
            proxy: str | None = PROXY) -> _EgressGuardedClient:
    """被测客户端：`transport` 用 MockTransport，`egress_proxy` 是熔断 key。"""
    return _EgressGuardedClient(
        transport=httpx.MockTransport(handler),
        breaker=breaker,
        egress_proxy=proxy,
    )


# --------------------------------------------------------- 熔断器状态机


def test_unknown_proxy_is_not_open():
    """没见过失败的出口直接放行（直连/首次请求的常态路径）。"""
    breaker = EgressBreaker(clock=_Clock())
    breaker.check(PROXY)                    # 不抛
    assert breaker.open_proxies() == {}


def test_failure_opens_with_cooldown_retry_after():
    clock = _Clock()
    breaker = EgressBreaker(clock=clock)
    backoff = breaker.record_failure(PROXY)

    assert backoff == EGRESS_FAILURE_COOLDOWN_SECONDS
    assert breaker.open_proxies() == {PROXY: EGRESS_FAILURE_COOLDOWN_SECONDS}
    with pytest.raises(EgressUnavailable) as info:
        breaker.check(PROXY)
    assert info.value.retry_after == int(EGRESS_FAILURE_COOLDOWN_SECONDS)
    assert info.value.proxy == PROXY


def test_consecutive_failures_back_off_exponentially_up_to_cap():
    """连续探测失败 → 30/60/120/240/300 封顶，不让每次探测都 30s 一枪。"""
    clock = _Clock()
    breaker = EgressBreaker(clock=clock)
    backoffs = []
    for _ in range(6):
        backoffs.append(breaker.record_failure(PROXY))
        clock.now += 1_000.0                # 越过本次冷却，模拟「到期又失败」
    assert backoffs == [30.0, 60.0, 120.0, 240.0, 300.0, 300.0]
    assert backoffs[-1] == EGRESS_MAX_COOLDOWN_SECONDS


def test_long_dead_proxy_caps_without_overflow():
    """连续失败几千次也只是封顶 5min：指数项先夹住，不能溢出成 OverflowError。"""
    breaker = EgressBreaker(clock=_Clock())
    backoff = 0.0
    for _ in range(10_000):
        backoff = breaker.record_failure(PROXY)
    assert backoff == EGRESS_MAX_COOLDOWN_SECONDS


def test_half_open_releases_exactly_one_probe():
    """到期后只放 1 个探测；探测在途时其余请求继续快速失败。"""
    clock = _Clock()
    breaker = EgressBreaker(clock=clock)
    breaker.record_failure(PROXY)

    clock.now += EGRESS_FAILURE_COOLDOWN_SECONDS
    breaker.check(PROXY)                    # 探测位：放行
    with pytest.raises(EgressUnavailable) as info:
        breaker.check(PROXY)                # 第二个请求被挡
    assert info.value.retry_after == 1


def test_probe_success_closes_breaker():
    clock = _Clock()
    breaker = EgressBreaker(clock=clock)
    breaker.record_failure(PROXY)
    clock.now += EGRESS_FAILURE_COOLDOWN_SECONDS
    breaker.check(PROXY)

    breaker.record_success(PROXY)           # 探测通过 → 恢复
    assert breaker.open_proxies() == {}
    breaker.check(PROXY)
    # 恢复后重新计数：下次失败从 30s 起步，而不是延续之前的翻倍
    assert breaker.record_failure(PROXY) == EGRESS_FAILURE_COOLDOWN_SECONDS


def test_retry_after_rounds_up_and_never_zero():
    assert _retry_after(0.2) == 1
    assert _retry_after(30.0) == 30
    assert _retry_after(30.4) == 31


def test_egress_unavailable_contract_keeps_credentials_healthy():
    """`egress_unavailable` 必须落在「零惩罚」分类，否则坏代理会写坏整池凭证。"""
    error = EgressUnavailable(PROXY, 30)
    assert isinstance(error, httpx.ProxyError)      # 引擎 _is_network_error 认它
    assert error.kind() is ErrKind.NETWORK
    assert ErrKind.NETWORK in NO_PENALTY_KINDS      # → 不冷却、不累计、不落库
    assert error.code == "egress_unavailable"
    assert error.retry_after == 30


def test_wrapped_egress_failure_still_classifies_as_network():
    """渠道把传输异常包成 `UpstreamTransportError` 后，引擎仍须归 NETWORK。

    9 个渠道里有 4 个会 `raise UpstreamTransportError(...) from error`；
    引擎的 `_is_network_error` 看一层 `__cause__`，所以包装不能把出口故障
    洗成 SOFT（那会给健康凭证写 60s 冷却，正是本功能要消灭的雪崩）。
    """
    from src.engine.executor import _classify
    from src.provider.base import UpstreamTransportError

    try:
        raise EgressUnavailable(PROXY, 30)
    except EgressUnavailable as error:
        assert _classify(error) is ErrKind.NETWORK
        try:
            raise UpstreamTransportError("wrapped") from error
        except UpstreamTransportError as wrapped:
            assert _classify(wrapped) is ErrKind.NETWORK


# ------------------------------------------------- 客户端挂载点（真实请求路径）


async def test_bad_proxy_costs_one_real_failure_then_fast_fails():
    """坏代理：1 次真实失败 + N 次发往上游之前的快速失败。"""
    clock = _Clock()
    breaker = EgressBreaker(clock=clock)
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ConnectError("proxy unreachable", request=request)

    client = _client(handler, breaker=breaker)
    try:
        with pytest.raises(httpx.ConnectError):
            await client.get(UPSTREAM)       # 唯一的真实失败
        for _ in range(5):
            with pytest.raises(EgressUnavailable) as info:
                await client.get(UPSTREAM)
            assert info.value.retry_after >= 1
    finally:
        await client.aclose()

    assert len(attempts) == 1               # 熔断期间没有再往上游发请求
    assert breaker.open_proxies() == {PROXY: EGRESS_FAILURE_COOLDOWN_SECONDS}


async def test_probe_request_recovers_breaker():
    """到期后的第一个真实请求即探测：成功 → 熔断关闭、后续请求正常。"""
    clock = _Clock()
    breaker = EgressBreaker(clock=clock)
    state = {"fail": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["fail"]:
            raise httpx.ConnectTimeout("connect timeout", request=request)
        return httpx.Response(200, json={"data": []})

    client = _client(handler, breaker=breaker)
    try:
        with pytest.raises(httpx.ConnectTimeout):
            await client.get(UPSTREAM)
        clock.now += EGRESS_FAILURE_COOLDOWN_SECONDS
        state["fail"] = False
        response = await client.get(UPSTREAM)   # 探测成功
        assert response.status_code == 200
        assert breaker.open_proxies() == {}
        assert (await client.get(UPSTREAM)).status_code == 200
    finally:
        await client.aclose()


async def test_probe_failure_extends_cooldown():
    """探测也失败 → 按连续失败数续期（60s），不是重新从 30s 起。"""
    clock = _Clock()
    breaker = EgressBreaker(clock=clock)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("still down", request=request)

    client = _client(handler, breaker=breaker)
    try:
        with pytest.raises(httpx.ConnectError):
            await client.get(UPSTREAM)
        clock.now += EGRESS_FAILURE_COOLDOWN_SECONDS
        with pytest.raises(httpx.ConnectError):
            await client.get(UPSTREAM)       # 探测失败
    finally:
        await client.aclose()
    assert breaker.open_proxies() == {PROXY: 2 * EGRESS_FAILURE_COOLDOWN_SECONDS}


async def test_proxy_407_rejects_without_classifying_credential():
    """代理自己回 407：转成 egress_unavailable，绝不让渠道按 OTHER 冷却凭证。"""
    breaker = EgressBreaker(clock=_Clock())
    seen: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(407, content=b"proxy auth required")
        seen.append(response)
        return response

    client = _client(handler, breaker=breaker)
    try:
        with pytest.raises(EgressUnavailable) as info:
            await client.get(UPSTREAM)
        assert info.value.retry_after >= 1
    finally:
        await client.aclose()

    assert seen[0].is_closed                 # 响应已释放，连接能回池
    assert breaker.open_proxies() == {PROXY: EGRESS_FAILURE_COOLDOWN_SECONDS}


@pytest.mark.parametrize("error", [
    httpx.ReadTimeout("read timeout"),
    httpx.WriteTimeout("write timeout"),
])
async def test_upstream_timeouts_do_not_trip_breaker(error):
    """读/写超时是「上游慢」，不是出口坏——拿它熔断会误杀健康代理。"""
    breaker = EgressBreaker(clock=_Clock())

    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    client = _client(handler, breaker=breaker)
    try:
        with pytest.raises(type(error)):
            await client.get(UPSTREAM)
    finally:
        await client.aclose()
    assert breaker.open_proxies() == {}


async def test_aborted_probe_releases_slot_instead_of_locking_out():
    """探测请求被取消/读超时：出口证明是通的，必须归还探测位。

    否则 `probing` 永久为 True，该出口每次 `check` 都 503（fail-closed 卡死，
    只能重启进程恢复）。
    """
    clock = _Clock()
    breaker = EgressBreaker(clock=clock)
    state = {"error": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["error"]:
            raise httpx.ReadTimeout("probe aborted", request=request)
        return httpx.Response(200, json={"data": []})

    client = _client(handler, breaker=breaker)
    try:
        with pytest.raises(httpx.ReadTimeout):
            await client.get(UPSTREAM)
        # 出口坏了 → 打开；到期后探测被取消 → 状态清空、恢复放行
        breaker.record_failure(PROXY)
        clock.now += EGRESS_FAILURE_COOLDOWN_SECONDS
        with pytest.raises(httpx.ReadTimeout):
            await client.get(UPSTREAM)
        state["error"] = False
        assert (await client.get(UPSTREAM)).status_code == 200
    finally:
        await client.aclose()
    assert breaker.open_proxies() == {}


async def test_upstream_error_response_keeps_breaker_closed():
    """5xx 响应证明出口是通的：上游的错不该熔断出口。"""
    breaker = EgressBreaker(clock=_Clock())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"upstream busy")

    client = _client(handler, breaker=breaker)
    try:
        response = await client.get(UPSTREAM)
        assert response.status_code == 503
    finally:
        await client.aclose()
    assert breaker.open_proxies() == {}


async def test_direct_connection_never_consults_breaker():
    """直连没有「哪个出口坏了」：即使熔断器里有人打开也不拦。"""
    breaker = EgressBreaker(clock=_Clock())
    breaker.record_failure(PROXY)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    client = _client(handler, breaker=breaker, proxy=None)
    try:
        assert (await client.get(UPSTREAM)).status_code == 200
        # 直连成功也不该清掉别的出口的状态（key 是代理 URL）
        assert breaker.open_proxies() == {PROXY: EGRESS_FAILURE_COOLDOWN_SECONDS}
    finally:
        await client.aclose()


# --------------------------------------------- build_client 接线与连接池参数


async def test_build_client_wires_breaker_and_proxy():
    breaker = EgressBreaker(clock=_Clock())
    proxied = build_client(timeout=httpx.Timeout(5.0), proxy=PROXY, breaker=breaker)
    direct = build_client(timeout=httpx.Timeout(5.0))
    try:
        assert proxied._egress_proxy == PROXY
        assert proxied._egress_breaker is breaker
        assert direct._egress_proxy is None
        # 未显式传 breaker → 全局实例（管理端点读同一个）
        assert direct._egress_breaker is get_egress_breaker()
    finally:
        await proxied.aclose()
        await direct.aclose()


async def test_build_client_bounds_connection_reuse_for_dead_sockets():
    """C5 降级实现：空闲连接 3s 回收，死连接最多久留 3s 就被丢弃重建。

    httpx 不暴露 HTTP/2 PING 空闲阈值（`keepalive_expiry` 只作用于 HTTP/1.1），
    故只断言它唯一能配的那个参数；同时钉住 `http2` 保持关闭（未装 `h2`）。
    """
    direct = build_client(timeout=httpx.Timeout(5.0))
    proxied = build_client(timeout=httpx.Timeout(5.0), proxy=PROXY)

    def _pool(client: httpx.AsyncClient):
        if client._mounts:
            return next(iter(client._mounts.values()))._pool
        return client._transport._pool

    try:
        for client in (direct, proxied):
            pool = _pool(client)
            assert pool._keepalive_expiry == UPSTREAM_KEEPALIVE_EXPIRY_SECONDS
            assert pool._http2 is False
        assert type(_pool(proxied)).__name__ == "AsyncHTTPProxy"
    finally:
        await direct.aclose()
        await proxied.aclose()

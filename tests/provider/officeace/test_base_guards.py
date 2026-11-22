"""`base` 的两个出口守卫：`ensure_public_host`（SSRF 出口校验）与
`read_json_bounded`（成功路径有界读）。

放在 officeace 目录下：这两条都是为了 officeAce 的「长期 Basic 密钥 + 可写
网关地址」与「短接口成功路径无上限」两组缺陷补的，就近归属便于回归时找到。
"""

from __future__ import annotations

import httpx
import pytest

from src.provider import base
from src.provider.base import (
    UpstreamProtocolViolation,
    ensure_public_host,
    read_json_bounded,
)

#: 任意公网地址（example.com 的历史 A 记录）。
PUBLIC = "93.184.216.34"

#: 目录级 autouse 夹具会把 `base._resolve_addresses` 换成替身；本模块在 import
#: 期先存一份原实现，供「验证解析入口本身」的用例还原。
_REAL_RESOLVE = base._resolve_addresses


def _resolve_to(monkeypatch, *addresses: str) -> list[str]:
    """把解析入口换成固定结果，并返回调用记录（用于验证缓存）。"""
    calls: list[str] = []

    async def fake(host: str) -> list[str]:
        calls.append(host)
        return list(addresses)

    monkeypatch.setattr(base, "_resolve_addresses", fake)
    return calls


# ---------------------------------------------------------- ensure_public_host

async def test_ensure_public_host_accepts_public_address(monkeypatch):
    _resolve_to(monkeypatch, PUBLIC)
    await ensure_public_host("gw.test")          # 不抛即通过


async def test_ensure_public_host_rejects_private_a_record(monkeypatch):
    """域名解析到 10.x 是最典型的 SSRF 跳板：导入期判不出来，出口必须拦。"""
    _resolve_to(monkeypatch, "10.1.2.3")
    with pytest.raises(UpstreamProtocolViolation) as caught:
        await ensure_public_host("gw.test")
    assert caught.value.public_message


async def test_ensure_public_host_rejects_mixed_records(monkeypatch):
    """一公一私也必须拒：任一结果命中黑名单即阻断（否则按解析顺序随机绕过）。"""
    _resolve_to(monkeypatch, PUBLIC, "169.254.169.254")
    with pytest.raises(UpstreamProtocolViolation):
        await ensure_public_host("gw.test")


async def test_ensure_public_host_rejects_metadata_ip(monkeypatch):
    """云元数据地址单独一条：它是这条 SSRF 链最值钱的目标。"""
    _resolve_to(monkeypatch, "169.254.169.254")
    with pytest.raises(UpstreamProtocolViolation):
        await ensure_public_host("gw.test")


async def test_ensure_public_host_rejects_cgnat(monkeypatch):
    """CGNAT 100.64.0.0/10 在部分云内可达，`is_private` 却不覆盖它。"""
    _resolve_to(monkeypatch, "100.100.100.200")
    with pytest.raises(UpstreamProtocolViolation):
        await ensure_public_host("gw.test")


async def test_ensure_public_host_rejects_ipv6_link_local_with_scope(monkeypatch):
    """IPv6 链路本地带 `%en0` 后缀：先剥后缀再判，不能因 ValueError 放过。"""
    _resolve_to(monkeypatch, "fe80::1%en0")
    with pytest.raises(UpstreamProtocolViolation):
        await ensure_public_host("gw.test")


async def test_ensure_public_host_rejects_non_global(monkeypatch):
    """保留段（基准测试 198.18.0.0/15）非 global，必须拦。"""
    _resolve_to(monkeypatch, "198.18.0.1")
    with pytest.raises(UpstreamProtocolViolation):
        await ensure_public_host("gw.test")


async def test_ensure_public_host_rejects_malformed_address(monkeypatch):
    """畸形 DNS 应答（不是 IP）按阻断处理，不赌它是公网。"""
    _resolve_to(monkeypatch, "not-an-ip")
    with pytest.raises(UpstreamProtocolViolation):
        await ensure_public_host("gw.test")


async def test_ensure_public_host_rejects_empty_answer(monkeypatch):
    """解析成功但零结果：不能读成「干净的公网主机」。"""
    _resolve_to(monkeypatch)
    with pytest.raises(UpstreamProtocolViolation):
        await ensure_public_host("gw.test")


async def test_ensure_public_host_rejects_unresolvable(monkeypatch):
    """解析失败（OSError）按阻断处理：宁可这次失败，也不把密钥发给未知名字。"""
    async def boom(_host: str) -> list[str]:
        raise OSError("Name or service not known")

    monkeypatch.setattr(base, "_resolve_addresses", boom)
    with pytest.raises(UpstreamProtocolViolation) as caught:
        await ensure_public_host("gw.test")
    assert caught.value.public_message


async def test_ensure_public_host_rejects_empty_host():
    with pytest.raises(UpstreamProtocolViolation):
        await ensure_public_host("  ")


async def test_ensure_public_host_caches_successful_lookup(monkeypatch):
    """TTL 内命中缓存：不会每次聊天都查一次 DNS。"""
    calls = _resolve_to(monkeypatch, PUBLIC)
    await ensure_public_host("gw.test")
    await ensure_public_host("GW.TEST  ")       # 归一后同键
    assert calls == ["gw.test"]


async def test_ensure_public_host_does_not_cache_failure(monkeypatch):
    """失败不缓存：一次 DNS 抖动不能把合法域名锁死 60s。"""
    calls = _resolve_to(monkeypatch, "10.1.2.3")
    with pytest.raises(UpstreamProtocolViolation):
        await ensure_public_host("gw.test")
    with pytest.raises(UpstreamProtocolViolation):
        await ensure_public_host("gw.test")
    assert calls == ["gw.test", "gw.test"]


async def test_ensure_public_host_expires_cache(monkeypatch):
    """超过 TTL 后重新解析（DNS 变更能跟上）。"""
    calls = _resolve_to(monkeypatch, PUBLIC)
    monkeypatch.setattr(base, "PUBLIC_HOST_CACHE_TTL", -1.0)
    await ensure_public_host("gw.test")
    await ensure_public_host("gw.test")
    assert calls == ["gw.test", "gw.test"]


async def test_resolve_addresses_uses_loop_getaddrinfo(monkeypatch):
    """真实解析入口走 `loop.getaddrinfo`（线程池执行，不阻塞事件循环）。"""
    seen: list[tuple[str, None]] = []

    class _Loop:
        async def getaddrinfo(self, host, port):
            seen.append((host, port))
            return [(2, 1, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(base.asyncio, "get_running_loop", lambda: _Loop())
    assert await _REAL_RESOLVE("gw.test") == ["93.184.216.34"]
    assert seen == [("gw.test", None)]


# ---------------------------------------------------------- read_json_bounded

def _response(body: bytes) -> httpx.Response:
    return httpx.Response(200, content=body)


async def test_read_json_bounded_parses_normal_body():
    assert await read_json_bounded(_response(b'{"a": 1}'), what="/x") == {"a": 1}


async def test_read_json_bounded_rejects_non_json():
    with pytest.raises(UpstreamProtocolViolation):
        await read_json_bounded(_response(b"<html>"), what="/x")


async def test_read_json_bounded_rejects_oversized_body():
    """超限按协议违规早失败，而不是先分配整段 body 再解析。"""
    with pytest.raises(UpstreamProtocolViolation) as caught:
        await read_json_bounded(_response(b"x" * 32), what="/x", limit=16)
    assert caught.value.public_message == "upstream returned an oversized response"


async def test_read_json_bounded_accepts_body_exactly_at_limit():
    """恰好等于上限是完整响应，不能被误判超限。"""
    body = b'{"a": 1}'
    assert await read_json_bounded(
        _response(body), what="/x", limit=len(body)) == {"a": 1}

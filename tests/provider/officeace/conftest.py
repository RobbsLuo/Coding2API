"""officeAce 测试目录夹具：出口 DNS 校验默认放行 + 清 TTL 缓存。

`OfficeAceClient._gateway_of` 现在会解析域名并逐个结果过 IP 黑名单
（`base.ensure_public_host`）。测试用的 `gw.test` / `default.test` 并不存在，
真实解析必然失败并被按阻断处理，于是所有既有用例都会变成「网关地址无法
解析」。这里把解析入口换成固定公网地址。

同时清掉进程内 TTL 缓存：否则「上一个用例把 gw.test 判成公网并缓存」会让
「私网 A 记录 → 拒绝」的用例命中缓存而假通过（顺序相关的假绿最难查）。
"""

from __future__ import annotations

import pytest

from src.provider import base

#: 任意公网地址（example.com 的历史 A 记录），只用于让校验通过。
PUBLIC_ADDRESS = "93.184.216.34"


@pytest.fixture(autouse=True)
def public_gateway_dns(monkeypatch):
    """默认把域名解析成公网地址；需要验证拒绝的用例自行再 patch 本函数。"""

    async def fake_resolve(_host: str) -> list[str]:
        return [PUBLIC_ADDRESS]

    monkeypatch.setattr(base, "_resolve_addresses", fake_resolve)
    base._public_host_checked.clear()
    yield
    base._public_host_checked.clear()

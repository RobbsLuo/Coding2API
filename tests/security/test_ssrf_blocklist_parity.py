"""SSRF 黑名单的两份实现必须给出同一结论。

`src/runtime_settings.py` 的 `_is_blocked_webhook_host`（告警 webhook 用）与
`src/provider/base.py` 的 `_is_blocked_host_address`（上游网关出口校验用）
是**刻意复制的两份**判定：协议层不能反向依赖配置层（见后者的 docstring）。

复制必然带来漂移风险——某天一边补了新的保留网段、另一边忘了同步，就会留下
「告警 webhook 挡住了、但上游网关没挡住」的缺口，而且这种缺口不会被任何功能
测试发现。这里用同一组地址把两份判定钉在一起：任何一边单独改动都会立刻变红。
"""

from __future__ import annotations

import ipaddress

import pytest

from src import runtime_settings
from src.provider import base

#: 覆盖两份实现各自关心的每一类地址：私网三段、回环（v4/v6）、链路本地
#: （含云元数据与 IPv6 作用域后缀形态）、保留/文档段、组播、未指定、CGNAT、
#: 以及「非 global 但 is_private=False」的基准测试段。
BLOCKED = [
    "10.0.0.1",
    "172.16.0.1",
    "192.168.1.1",
    "127.0.0.1",
    "::1",
    "169.254.169.254",          # 云元数据
    "fe80::1",                  # IPv6 链路本地
    "fe80::1%en0",              # 带作用域后缀（getaddrinfo 原样返回的形态）
    "100.100.100.200",          # CGNAT（RFC 6598），部分云厂商内可达
    "198.18.0.1",               # 基准测试段：is_private=False 但非 global
    "203.0.113.7",              # 文档段：同上
    "224.0.0.1",                # 组播
    "0.0.0.0",                  # 未指定
]

ALLOWED = [
    "1.1.1.1",
    "8.8.8.8",
    "93.184.216.34",
    "2606:4700:4700::1111",
]


@pytest.mark.parametrize("address", BLOCKED)
def test_both_implementations_block(address):
    """内网/回环/保留地址：两份实现都必须阻断。"""
    assert base._is_blocked_host_address(address) is True
    assert runtime_settings._is_blocked_webhook_host(address) is True


@pytest.mark.parametrize("address", ALLOWED)
def test_both_implementations_allow(address):
    """公网地址：两份实现都必须放行（否则会把正常上游打成不可用）。"""
    assert base._is_blocked_host_address(address) is False
    assert runtime_settings._is_blocked_webhook_host(address) is False


def test_both_implementations_agree_on_malformed_input():
    """非 IP 文本：两份实现都要给出**同一**结论。

    `base` 侧把畸形解析结果当阻断（宁可错杀），`runtime_settings` 侧把
    「不是 IP」读成「是域名，交给投递前复核」放行——两者语义不同但都在各自
    位置正确。这里只断言它们各自稳定，不要求结论相同，避免把有意的差异
    误判成漂移。
    """
    assert base._is_blocked_host_address("not-an-ip") is True
    assert runtime_settings._is_blocked_webhook_host("not-an-ip") is False


def test_cgnat_network_constants_match():
    """两份实现里的 CGNAT 网段常量必须一致（最容易被单边改动的一处）。"""
    assert base._CGNAT_SHARED == runtime_settings._CGNAT_SHARED
    assert ipaddress.ip_network("100.64.0.0/10") == base._CGNAT_SHARED

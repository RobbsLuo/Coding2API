"""API Key 出口访问控制（B3.5）：来源 IP 白名单解析与判定。

放在 auth/ 而不是 api/：这里全是与框架无关的纯函数（输入输出都是字符串），
deps 只负责取「对端地址」和「转发头」两个原始值，策略本身不碰 Request。

两条刻意的保守设计：

- **默认不信 X-Forwarded-For**。该头由客户端可写，仅在显式开启 TRUST_PROXY
  时采信，且只取最后一个条目——它是紧邻本服务的那个代理实际看到的地址。
  多于一层反代时最后一个条目仍是「最近一跳」而非真实客户端，因此 TRUST_PROXY
  只适用于「本服务前面恰好一层受信反代」的部署。
- **写入口校验、读路径容错**。IP 白名单在写入时就解析成合法网络段；读取时
  再遇到解析不了的条目就丢弃，若整份白名单没有一条可解析，判定为**拒绝**
  （安全策略宁可拒绝，也不能因为一条脏数据变成敞开）。
"""

from __future__ import annotations

import ipaddress

# 单条 Key 的白名单条目数上限：读取路径每次请求都要遍历，且这是给
# 「自己用的几个出口 IP」设计的，不是给网段清单用的。（单条长度由
# ipaddress 解析天然有界，无需再设字符总量上限。）
MAX_IP_ENTRIES = 32


def split_entries(raw: str | None) -> list[str]:
    """逗号分隔文本 → 去空白后的非空条目列表。"""
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def normalize_allowed_ips(raw: str | None) -> str:
    """校验并规范化 IP/CIDR 白名单；非法条目抛 ValueError（消息可直接回给用户）。

    规范化后以逗号拼接存储：写成 `10.0.0.1` 会存成 `10.0.0.1/32`，
    这样读取路径不必再区分「单个地址」与「网段」两种写法。
    """
    entries = split_entries(raw)
    if len(entries) > MAX_IP_ENTRIES:
        raise ValueError(f"allowed_ips 最多 {MAX_IP_ENTRIES} 条")
    normalized: list[str] = []
    for entry in entries:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError as error:
            raise ValueError(f"allowed_ips 含非法 IP/CIDR: {entry!r}") from error
        text = str(network)
        if text not in normalized:
            normalized.append(text)
    return ",".join(normalized)


def _parse_networks(raw: str | None) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """读取路径的宽松解析：解析不了的条目直接丢弃（判定为不匹配）。"""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in split_entries(raw):
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            continue
    return networks


def ip_allowed(client_ip: str, allowed_ips: str | None) -> bool:
    """来源 IP 是否在白名单内；白名单为空 = 不限制（放行）。

    白名单**非空但一条都解析不出来**时判为拒绝（fail closed）：写入口已做
    校验，走到这里只可能是手工改库写坏了数据——安全策略宁可拒绝，也不能
    因为一条脏数据变成敞开。IPv4 与 IPv6 不能互相比对（`ip_address('::1') in
    network('10.0.0.0/8')` 返回 False 而非抛错），因此混合配置下要么命中同族
    网段，要么被拒。
    """
    raw_entries = split_entries(allowed_ips)
    if not raw_entries:
        return True
    networks = _parse_networks(allowed_ips)
    if not networks:
        return False
    address_text = (client_ip or "").strip()
    if not address_text:
        return False                    # 拿不到对端地址：不能默认放行
    try:
        address = ipaddress.ip_address(address_text)
    except ValueError:
        return False
    return any(address in network for network in networks)


def client_ip(peer: str | None, forwarded_for: str | None, *,
              trust_proxy: bool) -> str:
    """解析请求来源 IP。

    TRUST_PROXY 关闭（默认）时只看 TCP 对端地址——X-Forwarded-For 由客户端
    任意填写，信它就等于白名单形同虚设。开启后取 XFF 的**最后一个**条目：
    `$proxy_add_x_forwarded_for` 语义下那是紧邻的受信代理实际看到的地址，
    而第一个条目是客户端自己写的、不可信。
    """
    if trust_proxy:
        entries = [item.strip() for item in (forwarded_for or "").split(",") if item.strip()]
        if entries:
            return entries[-1]
    return (peer or "").strip()

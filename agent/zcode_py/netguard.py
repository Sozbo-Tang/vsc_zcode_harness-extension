"""出网 host 校验（SSRF 防护，PLAN §9.1）。

webfetch / websearch（以及后续 MCP 的 HTTP 请求）发请求前必须通过
:meth:`validate_url`：仅 http/https；解析 hostname 全部地址后逐一比对
拒绝清单——localhost、环回、私有、链路本地、保留、组播、CGNAT、唯一本地
等（IPv4 + IPv6）。

十进制/十六进制/缺段 IP 变体（``http://2130706433/``、``http://0x7f000001/``、
``http://127.1/``）由 OS 解析器统一归一：``socket.getaddrinfo`` 把它们全部
解析成真实 IP（本机实测：三者均得 127.0.0.1），故"先解析、后按地址段拒绝"
天然覆盖所有变体，无需自己穷举字符串形态。IPv4-mapped IPv6 字面量
（``http://[::ffff:127.0.0.1]/``）按映射出的 IPv4 地址比对拒绝段——双栈
连接实际打到映射地址，等价于直接写该 IPv4。

已知局限（记录在案，不假装解决）：
- DNS rebinding TOCTOU：校验与实际连接是两次解析，理论上存在竞态。彻底
  防护需把连接钉死在校验过的 IP（会破坏 TLS SNI/虚拟主机），M2 不做。
- 校验只保证"目标不是内网"，不保证内容可信。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

ALLOWED_SCHEMES = ("http", "https")

# 拒绝地址段（IPv4 + IPv6）：环回/私有/链路本地/保留/组播/CGNAT/唯一本地等
DENIED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    # IPv4
    ipaddress.ip_network("0.0.0.0/8"),        # "本网络"段
    ipaddress.ip_network("10.0.0.0/8"),       # RFC1918 私有
    ipaddress.ip_network("100.64.0.0/10"),    # CGNAT（云元数据常客）
    ipaddress.ip_network("127.0.0.0/8"),      # 环回
    ipaddress.ip_network("169.254.0.0/16"),   # 链路本地（AWS/GCP 元数据 169.254.169.254）
    ipaddress.ip_network("172.16.0.0/12"),    # RFC1918 私有
    ipaddress.ip_network("192.0.0.0/24"),     # IETF 协议指派
    ipaddress.ip_network("192.0.2.0/24"),     # TEST-NET-1
    ipaddress.ip_network("192.88.99.0/24"),   # 6to4 中继任播（已弃用）
    ipaddress.ip_network("192.168.0.0/16"),   # RFC1918 私有
    ipaddress.ip_network("198.18.0.0/15"),    # 基准测试
    ipaddress.ip_network("198.51.100.0/24"),  # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),   # TEST-NET-3
    ipaddress.ip_network("224.0.0.0/4"),      # 组播
    ipaddress.ip_network("240.0.0.0/4"),      # 保留（含 255.255.255.255 广播）
    # IPv6
    ipaddress.ip_network("::/128"),           # 未指定
    ipaddress.ip_network("::1/128"),          # 环回
    ipaddress.ip_network("fc00::/7"),         # 唯一本地（ULA）
    ipaddress.ip_network("fe80::/10"),        # 链路本地
    ipaddress.ip_network("ff00::/8"),         # 组播
    ipaddress.ip_network("2001:db8::/32"),    # 文档示例段
    ipaddress.ip_network("64:ff9b::/96"),     # NAT64（会映射到 IPv4 内网）
)


class NetguardError(ValueError):
    """URL 不允许出网（协议/主机/地址段任一不过）。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _denied_reason(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    for net in DENIED_NETWORKS:
        if address in net:
            return f"{address} is in denied range {net}"
    # IPv4-mapped IPv6（::ffff:a.b.c.d）：双栈连接实际打到映射的 IPv4，
    # 必须按映射地址再比对一次（字面量 [::ffff:127.0.0.1] 与 DNS 解析出
    # 的映射地址共用此路径）；映射到公网地址的放行——与直接写该 IPv4 等价
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        for net in DENIED_NETWORKS:
            if mapped in net:
                return f"{address} (ipv4-mapped {mapped}) is in denied range {net}"
    return None


def _extract_host(url: str) -> tuple[str, int, str]:
    """拆 URL → (scheme, port, hostname)。hostname 为 IPv6 字面量时去掉方括号。"""
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise NetguardError(
            f"scheme {parts.scheme!r} not allowed (only http/https, PLAN §9.1)"
        )
    hostname = parts.hostname
    if not hostname:
        raise NetguardError("URL has no host")
    port = parts.port or (443 if scheme == "https" else 80)
    return scheme, port, hostname


def validate_host(hostname: str) -> None:
    """校验单个 hostname：字面量 IP 直接比对；域名经 getaddrinfo 解析后
    对**每个**解析地址做地址段比对。任何地址落入拒绝段即拒绝。"""
    # 显式 localhost（解析前先拦，避免依赖解析器的本地映射行为）
    if hostname.lower() in ("localhost", "localhost.localdomain"):
        raise NetguardError("localhost is not allowed")
    # 字面量 IP（含 [::1] 已去括号）
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        reason = _denied_reason(addr)
        if reason:
            raise NetguardError(reason)
        return
    # 域名 / 十进制·十六进制·缺段 IP 变体：解析后按真实地址判定
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise NetguardError(f"cannot resolve host {hostname!r}: {exc}") from exc
    addresses: set[str] = set()
    for info in infos:
        sockaddr = info[4]
        addresses.add(sockaddr[0])
    if not addresses:
        raise NetguardError(f"host {hostname!r} resolved to no addresses")
    for text in sorted(addresses):
        try:
            addr = ipaddress.ip_address(text)
        except ValueError:
            raise NetguardError(f"unparseable resolved address {text!r} for {hostname!r}")
        reason = _denied_reason(addr)
        if reason:
            raise NetguardError(reason)


def validate_url(url: str) -> str:
    """出网前校验（webfetch/websearch 共用）。通过则返回规范化的 URL。"""
    if not isinstance(url, str) or not url.strip():
        raise NetguardError("empty URL")
    url = url.strip()
    _scheme, _port, hostname = _extract_host(url)
    validate_host(hostname)
    return url


__all__ = [
    "ALLOWED_SCHEMES",
    "DENIED_NETWORKS",
    "NetguardError",
    "validate_host",
    "validate_url",
]

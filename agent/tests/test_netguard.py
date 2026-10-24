"""SSRF host 校验测试（PLAN §9.1：localhost/环回/私有/保留 + IPv6 + 十进制变体）。

域名解析用 monkeypatch 假 getaddrinfo（离线可测、可控地址组合）；
字面量 IP 与十进制/十六进制/缺段变体走真实路径（OS 解析器本地归一，
无 DNS 依赖——见 netguard.py docstring 实测记录）。
"""

from __future__ import annotations

import socket

import pytest

from zcode_py.netguard import NetguardError, validate_url


# ---------------------------------------------------------------------------
# 协议与结构
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "file:///etc/passwd",
        "gopher://localhost",
        "javascript:alert(1)",
    ],
)
def test_non_http_schemes_rejected(url):
    with pytest.raises(NetguardError) as ei:
        validate_url(url)
    assert "scheme" in ei.value.reason


def test_empty_url_rejected():
    with pytest.raises(NetguardError):
        validate_url("")
    with pytest.raises(NetguardError):
        validate_url("not a url at all")


def test_url_without_host_rejected():
    with pytest.raises(NetguardError):
        validate_url("http://")


# ---------------------------------------------------------------------------
# 字面量 IP（不经 DNS）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.1.2.3",          # 环回段任意位
        "0.0.0.0",
        "10.0.0.5",
        "10.255.255.255",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.1",
        "169.254.169.254",    # 云元数据
        "100.64.0.1",         # CGNAT
        "224.0.0.1",          # 组播
        "240.0.0.1",          # 保留
        "255.255.255.255",
        "192.0.2.10",         # TEST-NET
        "[::1]",
        "[fe80::1]",
        "[fc00::1234]",
        "[fd12:3456:789a::1]",
        "[ff02::1]",
        "[2001:db8::1]",
        "[::]",               # 未指定
        "[64:ff9b::192.168.0.1]",  # NAT64 映射到内网
        # IPv4-mapped IPv6：双栈连接实际打到映射的 IPv4（实测审计确认）
        "[::ffff:127.0.0.1]",      # → 环回
        "[::ffff:10.0.0.5]",       # → RFC1918 私有
        "[::ffff:169.254.169.254]",  # → 链路本地（云元数据）
        "[::ffff:192.168.1.1]",    # → RFC1918 私有
        "[::ffff:0.0.0.0]",        # → "本网络"段
    ],
)
def test_literal_denied_hosts(host):
    with pytest.raises(NetguardError):
        validate_url(f"http://{host}/path")


@pytest.mark.parametrize(
    "host",
    [
        "8.8.8.8",
        "1.1.1.1",
        "93.184.216.34",
        "[2606:2800:220:1:248:1893:25c8:1946]",
        "[::ffff:8.8.8.8]",  # 映射到公网：与直接写该 IPv4 等价，放行
    ],
)
def test_literal_public_hosts_pass(host):
    assert validate_url(f"https://{host}/") == f"https://{host}/"


def test_localhost_name_rejected():
    with pytest.raises(NetguardError) as ei:
        validate_url("http://localhost:3000/api")
    assert "localhost" in ei.value.reason


# ---------------------------------------------------------------------------
# 十进制 / 十六进制 / 缺段 IP 变体（OS 解析器本地归一，无需网络）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "variant",
    [
        "2130706433",   # 127.0.0.1 的十进制
        "0x7f000001",   # 十六进制
        "0x7f.1",       # 混合
        "127.1",        # 缺段
    ],
)
def test_decimal_ip_variants_rejected(variant):
    """十进制/十六进制/缺段变体由 OS 解析器归一成真实 IP 后按段拒绝。

    注：前导零八进制（0177.0.0.1）各平台解析不一致（macOS 按十进制解析为
    公网 177.0.0.1，实际连接也是该地址——「先解析后校验」策略下行为自洽；
    Linux 按八进制解析为 127.0.0.1 会被拒），故不做跨平台断言。
    """
    with pytest.raises(NetguardError):
        validate_url(f"http://{variant}/")


# ---------------------------------------------------------------------------
# 域名解析（monkeypatch getaddrinfo）
# ---------------------------------------------------------------------------


def _fake_resolver(addresses):
    def resolve(host, *args, **kwargs):
        infos = []
        for addr in addresses:
            family = socket.AF_INET6 if ":" in addr else socket.AF_INET
            socktype = socket.SOCK_STREAM
            infos.append((family, socktype, 6, "", (addr, 0)))
        return infos

    return resolve


def test_public_domain_passes(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolver(["93.184.216.34"]))
    assert validate_url("https://example.com/x") == "https://example.com/x"


def test_private_resolution_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolver(["10.1.2.3"]))
    with pytest.raises(NetguardError) as ei:
        validate_url("http://internal.corp/api")
    assert "10.1.2.3" in ei.value.reason


def test_mixed_resolution_any_private_rejected(monkeypatch):
    """A 记录一个公网一个内网：任一地址落拒绝段即拒绝。"""
    monkeypatch.setattr(
        socket, "getaddrinfo", _fake_resolver(["93.184.216.34", "192.168.0.10"])
    )
    with pytest.raises(NetguardError):
        validate_url("http://mixed.example.com/")


def test_ipv6_resolution_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolver(["fe80::abcd"]))
    with pytest.raises(NetguardError):
        validate_url("http://ipv6.example.com/")


def test_ipv4_mapped_resolution_rejected(monkeypatch):
    """域名解析出 IPv4-mapped IPv6：按映射出的 IPv4 比对拒绝段。"""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolver(["::ffff:10.0.0.5"]))
    with pytest.raises(NetguardError) as ei:
        validate_url("http://mapped.example.com/")
    assert "10.0.0.5" in ei.value.reason


def test_unresolvable_host_rejected(monkeypatch):
    def boom(host, *args, **kwargs):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(NetguardError) as ei:
        validate_url("http://ghost.invalid/")
    assert "cannot resolve" in ei.value.reason

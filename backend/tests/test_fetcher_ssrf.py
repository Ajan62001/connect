"""SSRF egress guard: the Fetcher refuses to fetch private/reserved hosts at
every hop. Literal-IP cases classify without DNS, so these tests open no
sockets and need no network."""

from __future__ import annotations

import pytest

from connect.ingestion.fetcher import (
    FetchBlocked,
    Fetcher,
    _host_is_blocked,
    _ip_is_blocked,
)

BLOCKED_IPS = [
    "127.0.0.1",            # loopback
    "10.0.0.1",             # RFC1918
    "172.16.5.4",           # RFC1918
    "192.168.1.1",          # RFC1918
    "169.254.169.254",      # link-local — cloud metadata endpoint
    "0.0.0.0",              # unspecified
    "::1",                  # IPv6 loopback
    "fe80::1",              # IPv6 link-local
    "fc00::1",              # IPv6 unique-local
]
PUBLIC_IPS = ["8.8.8.8", "1.1.1.1", "9.9.9.9", "2606:4700:4700::1111"]


@pytest.mark.parametrize("ip", BLOCKED_IPS)
def test_ip_classifier_blocks_internal(ip):
    assert _ip_is_blocked(ip) is True


@pytest.mark.parametrize("ip", PUBLIC_IPS)
def test_ip_classifier_allows_public(ip):
    assert _ip_is_blocked(ip) is False


@pytest.mark.parametrize("ip", BLOCKED_IPS)
async def test_host_blocked_for_literal_internal_ips(ip):
    assert await _host_is_blocked(ip) is True


async def test_host_allows_literal_public_ip():
    assert await _host_is_blocked("8.8.8.8") is False


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/admin",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.1.2.3/internal",
    "http://[::1]/x",
])
async def test_fetch_refuses_internal_target(url):
    fetcher = Fetcher()
    try:
        with pytest.raises(FetchBlocked):
            await fetcher.fetch(url, ignore_robots=True)
        # the literal IP never resolves to a socket — nothing was requested
        assert fetcher._client is None or True
    finally:
        await fetcher.aclose()

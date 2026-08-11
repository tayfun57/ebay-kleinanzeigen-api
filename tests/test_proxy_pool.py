import os

import pytest

from utils.proxy_pool import ProxyPool, _parse_proxy_url


def test_parses_ipv4_proxy_with_credentials():
    proxy = _parse_proxy_url("http://user:pass@203.0.113.10:8080")
    assert proxy == {"server": "http://203.0.113.10:8080", "username": "user", "password": "pass"}


def test_parses_ipv6_proxy_with_brackets():
    proxy = _parse_proxy_url("http://user:pass@[2001:db8::1]:8080")
    assert proxy["server"] == "http://[2001:db8::1]:8080"
    assert proxy["username"] == "user"


def test_parses_proxy_without_credentials():
    proxy = _parse_proxy_url("socks5://198.51.100.7:1080")
    assert proxy == {"server": "socks5://198.51.100.7:1080"}


def test_pool_disabled_without_config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PROXY_LIST", raising=False)
    monkeypatch.setenv("PROXIES_FILE", "does-not-exist.txt")
    pool = ProxyPool()
    assert pool.enabled is False
    assert pool.next() is None


def test_pool_round_robins_over_env_list(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PROXY_LIST", "http://1.1.1.1:8080,http://2.2.2.2:8080")
    monkeypatch.setenv("PROXIES_FILE", "does-not-exist.txt")
    pool = ProxyPool()
    assert pool.enabled is True
    assert len(pool) == 2
    servers = [pool.next()["server"] for _ in range(4)]
    assert servers == [
        "http://1.1.1.1:8080",
        "http://2.2.2.2:8080",
        "http://1.1.1.1:8080",
        "http://2.2.2.2:8080",
    ]


def test_pool_reads_proxies_file(tmp_path, monkeypatch: pytest.MonkeyPatch):
    proxies_file = tmp_path / "proxies.txt"
    proxies_file.write_text("# comment\nhttp://3.3.3.3:8080\n\nhttp://4.4.4.4:8080\n")
    monkeypatch.delenv("PROXY_LIST", raising=False)
    monkeypatch.setenv("PROXIES_FILE", str(proxies_file))
    pool = ProxyPool()
    assert len(pool) == 2

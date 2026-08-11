"""
Round-robin pool of outbound proxies for Playwright browser contexts.

Configure via either:
  - PROXY_LIST env var: comma- or newline-separated proxy URLs
      e.g. "http://user:pass@1.2.3.4:8080,http://user:pass@[2001:db8::1]:8080"
  - PROXIES_FILE env var (default "proxies.txt"): one proxy URL per line,
    "#"-prefixed lines are treated as comments

Both IPv4 and IPv6 proxy hosts are supported. If no proxies are configured,
the pool is a no-op and contexts are created without a proxy (unchanged
behavior).
"""

import ipaddress
import itertools
import os
from typing import Optional
from urllib.parse import urlsplit


def _format_server(scheme: str, hostname: str, port: Optional[int]) -> str:
    try:
        ipaddress.IPv6Address(hostname)
        host = f"[{hostname}]"
    except ValueError:
        host = hostname
    return f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}"


def _parse_proxy_url(url: str) -> dict:
    parts = urlsplit(url)
    proxy = {"server": _format_server(parts.scheme or "http", parts.hostname, parts.port)}
    if parts.username:
        proxy["username"] = parts.username
    if parts.password:
        proxy["password"] = parts.password
    return proxy


def _load_proxy_urls() -> list[str]:
    urls: list[str] = []

    raw_list = os.environ.get("PROXY_LIST", "")
    if raw_list:
        urls.extend(p.strip() for p in raw_list.replace("\n", ",").split(",") if p.strip())

    proxies_file = os.environ.get("PROXIES_FILE", "proxies.txt")
    if os.path.exists(proxies_file):
        with open(proxies_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)

    return urls


class ProxyPool:
    def __init__(self):
        self._proxies = [_parse_proxy_url(u) for u in _load_proxy_urls()]
        self._cycle = itertools.cycle(self._proxies) if self._proxies else None

    @property
    def enabled(self) -> bool:
        return self._cycle is not None

    def __len__(self) -> int:
        return len(self._proxies)

    def next(self) -> Optional[dict]:
        """Returns the next proxy dict (Playwright new_context(proxy=...) shape),
        or None if no proxies are configured."""
        if self._cycle is None:
            return None
        return next(self._cycle)

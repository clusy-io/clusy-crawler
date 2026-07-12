from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from collections import OrderedDict
from urllib.parse import urlparse

import httpx

from app.config import settings


class _DNSCache:
    """TTL-based DNS cache to avoid repeated getaddrinfo calls."""

    def __init__(self, ttl: int = 300) -> None:
        self._cache: dict[str, tuple[str, float]] = {}
        self._ttl = ttl
        self._lock = asyncio.Lock()

    async def resolve(self, host: str) -> str:
        now = time.monotonic()
        if host in self._cache:
            ip, expires = self._cache[host]
            if now < expires:
                return ip

        async with self._lock:
            if host in self._cache:
                ip, expires = self._cache[host]
                if now < expires:
                    return ip

            loop = asyncio.get_running_loop()
            info = await loop.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
            ip = info[0][4][0] if info else host
            self._cache[host] = (ip, now + self._ttl)
            return ip


_dns_cache = _DNSCache()


async def resolve_host(host: str) -> str:
    return await _dns_cache.resolve(host)


class _ConnectionWarmer:
    """Pre-warm TCP+TLS connections to frequently-hit domains.

    On first access to a new domain, the TCP handshake + TLS negotiation
    costs 100-300ms. By eagerly establishing connections, we eliminate
    this cold-start penalty for subsequent requests to the same domain.
    """

    def __init__(self, max_warm: int = 20) -> None:
        self._warmed: OrderedDict[str, float] = OrderedDict()
        self._max = max_warm
        self._lock = asyncio.Lock()

    async def warm(self, url: str, client: httpx.AsyncClient) -> None:
        domain = urlparse(url).netloc
        if not domain:
            return

        async with self._lock:
            if domain in self._warmed:
                self._warmed.move_to_end(domain)
                return
            # Evict oldest if at capacity
            while len(self._warmed) >= self._max:
                self._warmed.popitem(last=False)
            self._warmed[domain] = time.monotonic()

        # Trigger a HEAD request to establish the connection pool entry.
        # httpx will keep this connection alive for keepalive_expiry seconds.
        # HEAD may fail; the TCP+TLS handshake still completes.
        with contextlib.suppress(Exception):
            await client.head(
                f"{urlparse(url).scheme}://{domain}/",
                timeout=httpx.Timeout(connect=5.0, read=3.0, write=3.0, pool=1.0),
            )


_warmer = _ConnectionWarmer()


async def warm_connection(url: str) -> None:
    await _warmer.warm(url, get_http_client())


_lock = asyncio.Lock()
_client: httpx.AsyncClient | None = None


def _build_limits() -> httpx.Limits:
    return httpx.Limits(
        max_keepalive_connections=settings.http_max_keepalive_connections,
        max_connections=settings.http_max_connections,
        keepalive_expiry=30.0,
    )


def get_http_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.http_connect_timeout_s,
                read=settings.http_timeout_s,
                write=10.0,
                pool=5.0,
            ),
            proxy=settings.http_proxy or None,
            limits=_build_limits(),
            headers={
                "User-Agent": settings.http_user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                # Accept-Encoding is intentionally NOT set here: httpx advertises
                # exactly the codecs it can decode (gzip/deflate + br/zstd when the
                # brotli/zstandard packages are installed). Hardcoding `br` while the
                # decoder is absent yields undecodable bytes on Brotli-serving sites.
                "Accept-Language": "en-US,en;q=0.9",
            },
            follow_redirects=True,
            http2=True,
        )
    return _client


async def aclose_http_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# Adaptive timeout tracking — per-domain response time history
_domain_latency: dict[str, list[float]] = {}


def record_latency(url: str, ms: float) -> None:
    domain = urlparse(url).netloc
    if not domain:
        return
    if domain not in _domain_latency:
        _domain_latency[domain] = []
    history = _domain_latency[domain]
    history.append(ms)
    if len(history) > 50:
        history.pop(0)


def adaptive_timeout(url: str) -> float:
    """Return a good timeout for this URL based on past performance."""
    domain = urlparse(url).netloc
    history = _domain_latency.get(domain, [])
    if len(history) >= 3:
        p95 = sorted(history)[int(len(history) * 0.95)]
        return max(settings.http_timeout_s, min(p95 * 3, 30.0))
    return settings.http_timeout_s

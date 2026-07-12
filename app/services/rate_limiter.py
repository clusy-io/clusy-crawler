from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from aiolimiter import AsyncLimiter

from app.config import settings


class DomainRateLimiter:
    def __init__(self) -> None:
        self._limiters: dict[str, AsyncLimiter] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, domain: str) -> asyncio.Lock:
        if domain not in self._locks:
            self._locks[domain] = asyncio.Lock()
        return self._locks[domain]

    async def acquire(self, url: str) -> None:
        domain = _extract_domain(url)
        limiter = self._limiters.get(domain)
        if limiter is None:
            limiter = await self._create_limiter(domain)
        await limiter.acquire()

    async def _create_limiter(self, domain: str) -> AsyncLimiter:
        lock = self._get_lock(domain)
        async with lock:
            if domain in self._limiters:
                return self._limiters[domain]
            limiter = AsyncLimiter(
                max_rate=settings.rate_limit_requests_per_second,
                time_period=1.0,
            )
            self._limiters[domain] = limiter
            return limiter

    def evict_idle(self, max_entries: int = 500) -> None:
        if len(self._limiters) > max_entries:
            excess = len(self._limiters) - max_entries
            keys = list(self._limiters.keys())[:excess]
            for key in keys:
                self._limiters.pop(key, None)
                self._locks.pop(key, None)


def _extract_domain(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return host.lower()


_rate_limiter: DomainRateLimiter | None = None


def get_rate_limiter() -> DomainRateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = DomainRateLimiter()
    return _rate_limiter

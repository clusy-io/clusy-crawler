from __future__ import annotations

import asyncio
import ipaddress
import time
from collections import OrderedDict
from urllib.parse import urlparse

from aiolimiter import AsyncLimiter

from app.config import settings


class DomainRateLimiter:
    def __init__(self) -> None:
        self._limiters: OrderedDict[str, AsyncLimiter] = OrderedDict()
        self._last_used: dict[str, float] = {}
        self._in_use: dict[str, int] = {}
        self._overflow_limiter: AsyncLimiter | None = None
        self._registry_lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def acquire(self, url: str) -> None:
        self._ensure_loop()
        domain = _extract_domain(url)
        async with self._registry_lock:
            limiter = self._limiters.get(domain)
            dedicated = limiter is not None
            if limiter is None:
                max_entries = settings.rate_limit_max_domains
                # Reuse a slot only after its previous bucket is guaranteed to
                # be completely replenished. Otherwise LRU churn could grant a
                # fresh burst to a recently limited domain.
                self._evict_to_limit(max_entries - 1)
                if len(self._limiters) < max_entries:
                    limiter = self._create_limiter(domain)
                    dedicated = True
                else:
                    # Preserve the hard registry bound without resetting rate
                    # state: excess domains share one conservative bucket.
                    limiter = self._get_overflow_limiter()
            else:
                self._limiters.move_to_end(domain)
            if dedicated:
                self._last_used[domain] = time.monotonic()
                self._in_use[domain] = self._in_use.get(domain, 0) + 1
        acquired = False
        try:
            await limiter.acquire()
            acquired = True
        finally:
            if dedicated:
                async with self._registry_lock:
                    if acquired:
                        self._last_used[domain] = time.monotonic()
                    remaining = self._in_use.get(domain, 1) - 1
                    if remaining > 0:
                        self._in_use[domain] = remaining
                    else:
                        self._in_use.pop(domain, None)
                    self._evict_to_limit(settings.rate_limit_max_domains)

    def _ensure_loop(self) -> None:
        """Discard loop-bound limiters when a new event loop takes ownership."""
        loop = asyncio.get_running_loop()
        if self._loop is loop:
            return
        self._loop = loop
        self._limiters.clear()
        self._last_used.clear()
        self._in_use.clear()
        self._overflow_limiter = None
        self._registry_lock = asyncio.Lock()

    def _create_limiter(self, domain: str) -> AsyncLimiter:
        limiter = self._new_limiter()
        self._limiters[domain] = limiter
        return limiter

    def _get_overflow_limiter(self) -> AsyncLimiter:
        if self._overflow_limiter is None:
            self._overflow_limiter = self._new_limiter()
        return self._overflow_limiter

    @staticmethod
    def _new_limiter() -> AsyncLimiter:
        # AsyncLimiter's max_rate is also its initial bucket capacity. Express
        # the configured burst explicitly while preserving the long-run RPS.
        steady_rate = max(settings.rate_limit_requests_per_second, 0.001)
        burst = max(settings.rate_limit_burst, 1)
        return AsyncLimiter(
            max_rate=burst,
            time_period=burst / steady_rate,
        )

    def _evict_to_limit(self, max_entries: int) -> None:
        while len(self._limiters) > max_entries:
            now = time.monotonic()
            refill_period = max(settings.rate_limit_burst, 1) / max(
                settings.rate_limit_requests_per_second,
                0.001,
            )
            domain = next(
                (
                    candidate
                    for candidate in self._limiters
                    if self._in_use.get(candidate, 0) == 0
                    and now - self._last_used.get(candidate, now) >= refill_period
                ),
                None,
            )
            # A temporary overflow is safer than resetting a live or depleted
            # bucket. New domains use the shared overflow limiter until a
            # dedicated bucket is safe to discard.
            if domain is None:
                break
            self._limiters.pop(domain, None)
            self._last_used.pop(domain, None)

    def evict_idle(self, max_entries: int = 500) -> None:
        self._evict_to_limit(max_entries)


def _extract_domain(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").rstrip(".")
    try:
        return ipaddress.ip_address(host).compressed.lower()
    except ValueError:
        pass
    try:
        return host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return host.lower()


_rate_limiter: DomainRateLimiter | None = None


def get_rate_limiter() -> DomainRateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = DomainRateLimiter()
    return _rate_limiter

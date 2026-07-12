from __future__ import annotations

import asyncio

from app.services.rate_limiter import DomainRateLimiter


class TestDomainRateLimiter:
    def test_different_domains_isolated(self):
        limiter = DomainRateLimiter()

        async def exercise():
            await limiter.acquire("https://example.com/a")
            await limiter.acquire("https://other.org/b")

        asyncio.run(exercise())

    def test_same_domain_uses_same_limiter(self):
        limiter = DomainRateLimiter()
        assert len(limiter._limiters) == 0

        async def exercise():
            await limiter.acquire("https://example.com/page1")
            await limiter.acquire("https://example.com/page2")

        asyncio.run(exercise())
        assert len(limiter._limiters) == 1

    def test_domain_extraction(self):
        from app.services.rate_limiter import _extract_domain

        assert _extract_domain("https://example.com/path") == "example.com"
        assert _extract_domain("http://sub.example.co.uk/path") == "sub.example.co.uk"
        assert _extract_domain("https://foo.com:8080/x") == "foo.com"

    def test_evict_idle_clears_old_entries(self):
        limiter = DomainRateLimiter()

        async def exercise():
            for i in range(10):
                await limiter.acquire(f"https://domain{i}.com/page")

        asyncio.run(exercise())
        assert len(limiter._limiters) == 10
        limiter.evict_idle(max_entries=5)
        assert len(limiter._limiters) == 5

from __future__ import annotations

import asyncio

from app.config import settings
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
        assert _extract_domain("https://EXAMPLE.com./path") == "example.com"
        assert _extract_domain("http://sub.example.co.uk/path") == "sub.example.co.uk"
        assert _extract_domain("https://foo.com:8080/x") == "foo.com"
        assert _extract_domain("https://bücher.example/x") == "xn--bcher-kva.example"
        assert _extract_domain("https://xn--bcher-kva.example/x") == (
            "xn--bcher-kva.example"
        )

    def test_evict_idle_clears_old_entries(self):
        limiter = DomainRateLimiter()

        async def exercise():
            for i in range(10):
                await limiter.acquire(f"https://domain{i}.com/page")

        asyncio.run(exercise())
        assert len(limiter._limiters) == 10
        # Simulate enough idle time for every bucket to be fully replenished.
        limiter._last_used = dict.fromkeys(limiter._last_used, 0.0)
        limiter.evict_idle(max_entries=5)
        assert len(limiter._limiters) == 5

    def test_configures_burst_capacity_and_steady_rate(self, monkeypatch):
        monkeypatch.setattr(settings, "rate_limit_requests_per_second", 2.0)
        monkeypatch.setattr(settings, "rate_limit_burst", 5)
        limiter = DomainRateLimiter()

        async def exercise():
            await limiter.acquire("https://example.com/one")

        asyncio.run(exercise())
        domain_limiter = limiter._limiters["example.com"]
        assert domain_limiter.max_rate == 5
        assert domain_limiter.time_period == 2.5

    def test_registry_is_bounded_lru(self, monkeypatch):
        monkeypatch.setattr(settings, "rate_limit_max_domains", 3)
        monkeypatch.setattr(settings, "rate_limit_requests_per_second", 1000.0)
        limiter = DomainRateLimiter()

        async def exercise():
            for i in range(10):
                await limiter.acquire(f"https://domain{i}.example/page")

        asyncio.run(exercise())
        assert len(limiter._limiters) == 3
        assert limiter._overflow_limiter is not None

    def test_domain_churn_cannot_reset_a_depleted_bucket(self, monkeypatch):
        monkeypatch.setattr(settings, "rate_limit_max_domains", 1)
        monkeypatch.setattr(settings, "rate_limit_requests_per_second", 20.0)
        monkeypatch.setattr(settings, "rate_limit_burst", 1)
        limiter = DomainRateLimiter()

        async def exercise():
            await limiter.acquire("https://example.com/one")
            await limiter.acquire("https://churn.example/one")
            started = asyncio.get_running_loop().time()
            await limiter.acquire("https://example.com/two")
            return asyncio.get_running_loop().time() - started

        elapsed = asyncio.run(exercise())
        assert elapsed >= 0.035

    def test_lru_never_evicts_a_limiter_with_live_waiters(self, monkeypatch):
        monkeypatch.setattr(settings, "rate_limit_max_domains", 1)
        limiter = DomainRateLimiter()

        class BlockingLimiter:
            def __init__(self):
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def acquire(self):
                self.started.set()
                await self.release.wait()

        class ImmediateLimiter:
            async def acquire(self):
                return None

        blocking = BlockingLimiter()

        def create(domain):
            created = blocking if domain == "busy.example" else ImmediateLimiter()
            limiter._limiters[domain] = created
            return created

        monkeypatch.setattr(limiter, "_create_limiter", create)

        async def exercise():
            waiting = asyncio.create_task(limiter.acquire("https://busy.example/a"))
            await blocking.started.wait()
            await limiter.acquire("https://other.example/b")
            assert limiter._limiters["busy.example"] is blocking
            assert "other.example" not in limiter._limiters
            blocking.release.set()
            await waiting

        asyncio.run(exercise())

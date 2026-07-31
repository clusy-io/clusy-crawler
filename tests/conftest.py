from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture(autouse=True)
def _patch_cache(monkeypatch):
    from app.cache import RedisCache

    class _NoopCache(RedisCache):
        async def get(self, key):  # type: ignore
            return None

        async def set(self, key, value, ttl=None):  # type: ignore
            pass

    monkeypatch.setattr("app.cache._cache", _NoopCache())


@pytest.fixture(autouse=True)
def _patch_rate_limiter(monkeypatch):
    from app.services.rate_limiter import DomainRateLimiter

    class _NoopRateLimiter(DomainRateLimiter):
        async def acquire(self, url: str) -> None:  # type: ignore
            pass

    monkeypatch.setattr("app.services.rate_limiter._rate_limiter", _NoopRateLimiter())


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch):
    monkeypatch.setattr("app.config.settings.max_concurrent_tasks", 10)
    monkeypatch.setattr("app.config.settings.js_render_mode", "never")
    monkeypatch.setattr("app.config.settings.playwright_enabled", False)

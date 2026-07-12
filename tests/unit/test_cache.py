from __future__ import annotations

import asyncio

from app.cache import RedisCache, make_cache_key


class TestCacheKey:
    def test_same_url_same_key(self):
        key1 = make_cache_key("https://example.com", False, None)
        key2 = make_cache_key("https://example.com", False, None)
        assert key1 == key2

    def test_different_js_render_different_key(self):
        key1 = make_cache_key("https://example.com", True, None)
        key2 = make_cache_key("https://example.com", False, None)
        assert key1 != key2

    def test_starts_with_prefix(self):
        key = make_cache_key("https://example.com", False, None)
        assert key.startswith("crawler:")


class TestRedisCache:
    def test_noop_when_no_redis_url(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.redis_url", "")
        cache = RedisCache()

        async def exercise():
            ok = await cache._ensure_client()
            assert ok is False
            result = await cache.get("foo")
            assert result is None
            await cache.set("foo", b"bar")

        asyncio.run(exercise())

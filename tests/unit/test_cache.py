from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from app.cache import CACHE_SCHEMA_VERSION, RedisCache, make_cache_key
from app.config import Settings, settings
from app.models.requests import CrawlRequest


class TestCacheKey:
    def test_same_url_same_key(self):
        key1 = make_cache_key("https://example.com", False, None)
        key2 = make_cache_key("https://example.com", False, None)
        assert key1 == key2

    def test_extraction_profiles_do_not_share_cache_entries(self):
        balanced = make_cache_key(
            "https://example.com",
            False,
            None,
            extraction_profile="balanced",
        )
        article = make_cache_key(
            "https://example.com",
            False,
            None,
            extraction_profile="article_body",
        )
        adaptive = make_cache_key(
            "https://example.com",
            False,
            None,
            extraction_profile="adaptive",
        )
        quality = make_cache_key(
            "https://example.com",
            False,
            None,
            extraction_profile="quality",
        )
        assert len({balanced, article, adaptive, quality}) == 4

    def test_different_js_render_different_key(self):
        key1 = make_cache_key("https://example.com", True, None)
        key2 = make_cache_key("https://example.com", False, None)
        assert key1 != key2

    def test_conditional_auto_render_has_distinct_key(self):
        key1 = make_cache_key("https://example.com", False, None)
        key2 = make_cache_key(
            "https://example.com",
            False,
            None,
            auto_render=True,
        )
        assert key1 != key2

    def test_starts_with_prefix(self):
        key = make_cache_key("https://example.com", False, None)
        assert key.startswith(f"crawler:{CACHE_SCHEMA_VERSION}:")

    def test_output_affecting_threshold_changes_key(self):
        key1 = make_cache_key(
            "https://example.com",
            False,
            None,
            word_count_threshold=10,
        )
        key2 = make_cache_key(
            "https://example.com",
            False,
            None,
            word_count_threshold=100,
        )
        assert key1 != key2

    def test_javascript_runtime_setting_changes_key(self, monkeypatch):
        key1 = make_cache_key("https://example.com", True, None)
        monkeypatch.setattr(
            "app.config.settings.playwright_java_script_enabled",
            False,
        )
        key2 = make_cache_key("https://example.com", True, None)
        assert key1 != key2

    def test_native_backend_revision_changes_key(self, monkeypatch):
        key1 = make_cache_key("https://example.com", False, None)
        monkeypatch.setattr("clusy_native.backend_version", lambda: "future-backend")
        key2 = make_cache_key("https://example.com", False, None)
        assert key1 != key2

    def test_quality_model_and_endpoint_change_key(self, monkeypatch):
        monkeypatch.setattr(settings, "quality_extraction_base_url", "https://one.invalid/v1")
        monkeypatch.setattr(settings, "quality_extraction_model", "model-a")
        first = make_cache_key(
            "https://example.com",
            False,
            None,
            extraction_profile="quality",
        )
        monkeypatch.setattr(settings, "quality_extraction_base_url", "https://two.invalid/v1")
        monkeypatch.setattr(settings, "quality_extraction_model", "model-b")
        second = make_cache_key(
            "https://example.com",
            False,
            None,
            extraction_profile="quality",
        )
        monkeypatch.setattr(
            settings,
            "quality_extraction_prompt_profile",
            "mineru_compact",
        )
        compact = make_cache_key(
            "https://example.com",
            False,
            None,
            extraction_profile="quality",
        )

        assert first != second
        assert second != compact

    def test_adaptive_model_router_revision_and_thresholds_change_key(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(settings, "quality_extraction_model", "model-a")
        first = make_cache_key(
            "https://example.com",
            False,
            None,
            extraction_profile="adaptive",
        )

        monkeypatch.setattr(settings, "quality_extraction_model", "model-b")
        model_changed = make_cache_key(
            "https://example.com",
            False,
            None,
            extraction_profile="adaptive",
        )

        monkeypatch.setattr(settings, "adaptive_extraction_min_confidence", 0.9)
        threshold_changed = make_cache_key(
            "https://example.com",
            False,
            None,
            extraction_profile="adaptive",
        )

        monkeypatch.setattr(
            "app.services.extractor.ADAPTIVE_ROUTER_REVISION",
            "adaptive-future",
        )
        revision_changed = make_cache_key(
            "https://example.com",
            False,
            None,
            extraction_profile="adaptive",
        )

        monkeypatch.setattr(
            "app.services.quality_extractor.MINERU_HTML_REVISION",
            "quality-future",
        )
        quality_revision_changed = make_cache_key(
            "https://example.com",
            False,
            None,
            extraction_profile="adaptive",
        )

        assert len(
            {
                first,
                model_changed,
                threshold_changed,
                revision_changed,
                quality_revision_changed,
            }
        ) == 5


def test_adaptive_profile_and_settings_validation() -> None:
    request = CrawlRequest(urls=["https://example.com"], extraction_profile="adaptive")
    assert request.extraction_profile == "adaptive"

    with pytest.raises(ValidationError):
        CrawlRequest(urls=["https://example.com"], extraction_profile="unknown")
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            adaptive_extraction_risky_page_types="listing,unknown",
        )
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            adaptive_extraction_structural_score_threshold=0,
        )
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            quality_extraction_prompt_profile="unsupported",
        )


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

    @pytest.mark.anyio
    async def test_redis_outage_is_singleflight_and_cooled_down(
        self,
        monkeypatch,
    ):
        import redis.asyncio as aioredis

        calls = 0

        class FailedClient:
            async def ping(self):
                raise OSError("black hole")

            async def aclose(self):
                pass

        def fake_from_url(*args, **kwargs):
            nonlocal calls
            calls += 1
            return FailedClient()

        monkeypatch.setattr(settings, "redis_url", "redis://secret@cache.invalid/0")
        monkeypatch.setattr(settings, "cache_failure_cooldown_s", 30.0)
        monkeypatch.setattr(aioredis, "from_url", fake_from_url)
        cache = RedisCache()

        results = await asyncio.gather(*[cache._ensure_client() for _ in range(50)])
        assert results == [False] * 50
        assert calls == 1

        assert await cache._ensure_client() is False
        assert calls == 1

    @pytest.mark.anyio
    async def test_oversized_entry_is_not_sent_to_redis(self, monkeypatch):
        cache = RedisCache()
        monkeypatch.setattr(settings, "cache_max_entry_bytes", 4)

        async def should_not_connect():
            raise AssertionError("oversized cache values must be rejected first")

        monkeypatch.setattr(cache, "_ensure_client", should_not_connect)
        await cache.set("key", b"12345")

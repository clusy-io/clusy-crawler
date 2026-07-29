from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from app.cache import (
    ADAPTIVE_OUTPUT_SETTING_NAMES,
    CACHE_SCHEMA_VERSION,
    CORE_OUTPUT_SETTING_NAMES,
    FETCH_OUTPUT_SETTING_NAMES,
    QUALITY_OUTPUT_SETTING_NAMES,
    RENDER_OUTPUT_SETTING_NAMES,
    SCHOLARLY_OUTPUT_SETTING_NAMES,
    RedisCache,
    _cache_semantics_payload,
    make_cache_key,
)
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

    @pytest.mark.parametrize(
        "field_name",
        (
            CORE_OUTPUT_SETTING_NAMES
            + FETCH_OUTPUT_SETTING_NAMES
            + RENDER_OUTPUT_SETTING_NAMES
            + SCHOLARLY_OUTPUT_SETTING_NAMES
        ),
    )
    def test_every_declared_balanced_runtime_setting_partitions_cache(
        self,
        monkeypatch,
        field_name,
    ):
        first = make_cache_key("https://example.com", False, None)
        current = getattr(settings, field_name)
        if isinstance(current, bool):
            replacement = not current
        elif isinstance(current, str):
            replacement = current + "-changed"
        else:
            replacement = current + 1
        monkeypatch.setattr(settings, field_name, replacement)

        assert make_cache_key("https://example.com", False, None) != first

    @pytest.mark.parametrize("field_name", QUALITY_OUTPUT_SETTING_NAMES)
    def test_every_declared_quality_setting_partitions_assisted_cache(
        self,
        monkeypatch,
        field_name,
    ):
        first = make_cache_key(
            "https://example.com",
            False,
            None,
            extraction_profile="quality",
        )
        current = getattr(settings, field_name)
        if isinstance(current, bool):
            replacement = not current
        elif isinstance(current, str):
            replacement = current + "-changed"
        else:
            replacement = current + 1
        monkeypatch.setattr(settings, field_name, replacement)

        assert (
            make_cache_key(
                "https://example.com",
                False,
                None,
                extraction_profile="quality",
            )
            != first
        )

    @pytest.mark.parametrize("field_name", ADAPTIVE_OUTPUT_SETTING_NAMES)
    def test_every_declared_adaptive_setting_partitions_adaptive_cache(
        self,
        monkeypatch,
        field_name,
    ):
        first = make_cache_key(
            "https://example.com",
            False,
            None,
            extraction_profile="adaptive",
        )
        current = getattr(settings, field_name)
        if isinstance(current, bool):
            replacement = not current
        elif isinstance(current, str):
            replacement = current + "-changed"
        else:
            replacement = current + 1
        monkeypatch.setattr(settings, field_name, replacement)

        assert (
            make_cache_key(
                "https://example.com",
                False,
                None,
                extraction_profile="adaptive",
            )
            != first
        )

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

    @pytest.mark.parametrize("profile", ["quality", "adaptive"])
    def test_enabling_quality_credentials_partitions_disabled_cache(
        self,
        monkeypatch,
        profile,
    ):
        monkeypatch.setattr(
            settings,
            "quality_extraction_base_url",
            "https://quality.invalid/v1",
        )
        monkeypatch.setattr(settings, "quality_extraction_model", "model-a")
        monkeypatch.setattr(settings, "quality_extraction_api_key", "")
        disabled = make_cache_key(
            "https://example.com",
            False,
            None,
            extraction_profile=profile,
        )

        monkeypatch.setattr(
            settings,
            "quality_extraction_api_key",
            "configured-but-never-hashed",
        )
        enabled = make_cache_key(
            "https://example.com",
            False,
            None,
            extraction_profile=profile,
        )

        assert disabled != enabled

    @pytest.mark.parametrize("profile", ["quality", "adaptive"])
    def test_quality_dependency_availability_partitions_assisted_cache(
        self,
        monkeypatch,
        profile,
    ):
        monkeypatch.setattr(
            "app.services.quality_extractor.quality_dependency_available",
            lambda: False,
        )
        unavailable = make_cache_key(
            "https://example.com",
            False,
            None,
            extraction_profile=profile,
        )
        monkeypatch.setattr(
            "app.services.quality_extractor.quality_dependency_available",
            lambda: True,
        )
        available = make_cache_key(
            "https://example.com",
            False,
            None,
            extraction_profile=profile,
        )

        assert unavailable != available

    @pytest.mark.parametrize(
        ("field_name", "profile", "first_value", "second_value"),
        [
            (
                "http_proxy",
                "balanced",
                "https://tenant-a:secret-a@proxy.invalid:8443?pool=one",
                "https://tenant-b:secret-b@proxy.invalid:8443?pool=two",
            ),
            (
                "playwright_proxy",
                "balanced",
                "https://tenant-a:secret-a@browser.invalid:8443?pool=one",
                "https://tenant-b:secret-b@browser.invalid:8443?pool=two",
            ),
            (
                "elsevier_api_key",
                "balanced",
                "elsevier-key-one",
                "elsevier-key-two",
            ),
            (
                "ieee_api_key",
                "balanced",
                "ieee-key-one",
                "ieee-key-two",
            ),
            (
                "quality_extraction_api_key",
                "quality",
                "quality-key-one",
                "quality-key-two",
            ),
            (
                "quality_extraction_base_url",
                "quality",
                "https://quality.invalid/v1?deployment=one",
                "https://quality.invalid/v1?deployment=two",
            ),
        ],
    )
    def test_sensitive_serving_identity_rotation_partitions_cache(
        self,
        monkeypatch,
        field_name,
        profile,
        first_value,
        second_value,
    ):
        monkeypatch.setattr(settings, field_name, first_value)
        first = make_cache_key(
            "https://example.com",
            False,
            None,
            extraction_profile=profile,
        )
        monkeypatch.setattr(settings, field_name, second_value)
        second = make_cache_key(
            "https://example.com",
            False,
            None,
            extraction_profile=profile,
        )

        assert first != second

    def test_sensitive_identity_binds_exact_raw_whitespace(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(
            settings,
            "serving_fingerprint_key",
            "independent-fingerprint-secret-" + ("f" * 32),
        )
        monkeypatch.setattr(
            settings,
            "http_proxy",
            "https://proxy.invalid:8443",
        )
        exact = make_cache_key("https://example.com", False, None)
        monkeypatch.setattr(
            settings,
            "http_proxy",
            " https://proxy.invalid:8443 ",
        )
        padded = make_cache_key("https://example.com", False, None)

        assert exact != padded

    def test_cache_semantics_snapshot_contains_no_plaintext_secrets(
        self,
        monkeypatch,
    ):
        secrets = {
            "serving_fingerprint_key": (
                "independent-fingerprint-secret-" + ("f" * 32)
            ),
            "http_proxy": "https://proxy-user:proxy-secret@proxy.invalid:8443",
            "playwright_proxy": (
                "https://browser-user:browser-secret@browser.invalid:8443"
            ),
            "elsevier_api_key": "elsevier-secret",
            "ieee_api_key": "ieee-secret",
            "quality_extraction_base_url": (
                "https://quality.invalid/v1?token=endpoint-secret"
            ),
            "quality_extraction_api_key": "quality-secret",
            "quality_extraction_model": "model-a",
        }
        for field_name, value in secrets.items():
            monkeypatch.setattr(settings, field_name, value)

        snapshot = _cache_semantics_payload(
            extraction_profile="quality",
            native_backend="native-test",
            pipeline_revision="pipeline-test",
            quality_revision="quality-test",
            adaptive_revision="",
        )
        serialized = json.dumps(snapshot, sort_keys=True)

        for secret in (
            "proxy-secret",
            "browser-secret",
            "elsevier-secret",
            "ieee-secret",
            "endpoint-secret",
            "quality-secret",
            "independent-fingerprint-secret",
        ):
            assert secret not in serialized

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
            settings,
            "adaptive_extraction_structure_loss_threshold",
            2,
        )
        structure_loss_changed = make_cache_key(
            "https://example.com",
            False,
            None,
            extraction_profile="adaptive",
        )

        monkeypatch.setattr(
            settings,
            "adaptive_extraction_candidate_disagreement_threshold",
            0.8,
        )
        disagreement_changed = make_cache_key(
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
                structure_loss_changed,
                disagreement_changed,
                revision_changed,
                quality_revision_changed,
            }
        ) == 7


def test_adaptive_profile_and_settings_validation() -> None:
    request = CrawlRequest(urls=["https://example.com"], extraction_profile="adaptive")
    assert request.extraction_profile == "adaptive"
    service_settings = Settings(
        _env_file=None,
        adaptive_extraction_risky_page_types="service",
    )
    assert service_settings.adaptive_extraction_risky_page_types == "service"

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
            adaptive_extraction_structure_loss_threshold=0,
        )
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            adaptive_extraction_candidate_disagreement_threshold=1.1,
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

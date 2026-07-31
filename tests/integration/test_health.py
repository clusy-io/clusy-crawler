from __future__ import annotations

import json
from importlib.metadata import version as distribution_version

import pytest

from app.config import settings
from app.routers import health as health_module
from app.version import SERVICE_VERSION


class TestHealth:
    @pytest.mark.anyio
    async def test_health_ok(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    @pytest.mark.anyio
    async def test_ready(self, client):
        resp = await client.get("/health/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ready", "degraded")
        assert "checks" in data
        assert "http_client" in data["checks"]
        assert data["checks"]["native_extractor"] == "ok"

    @pytest.mark.anyio
    async def test_version(self, client):
        resp = await client.get("/health/version")
        assert resp.status_code == 200
        data = resp.json()
        assert data["service_version"] == SERVICE_VERSION
        assert "python_version" in data
        assert data["native_extractor_version"].startswith("rs-trafilatura")
        assert "trafilatura_version" in data
        assert data["playwright_version"] == distribution_version("playwright")
        assert "environment" in data
        assert data["pipeline_revision"] == "clusy-extraction-v2"
        assert data["adaptive_router_revision"] == "adaptive-v2"
        assert data["image_digest"] == "unknown"
        assert len(data["config_fingerprint"]) == 64
        assert set(data["config_fingerprint"]) <= set("0123456789abcdef")
        assert data["config_fingerprint_scheme"] == "hmac-sha256-v1"
        assert isinstance(data["quality_backend_configured"], bool)
        assert isinstance(data["quality_dependency_available"], bool)
        assert isinstance(data["quality_backend_enabled"], bool)
        assert data["quality_backend_revision"] == ""
        assert data["quality_source_selection_schema"] == (
            "quality-source-selection.v0"
        )
        assert isinstance(data["playwright_enabled"], bool)
        assert data["crawl_store_in_cache_supported"] is True
        assert data["crawl_cache_policy_revision"] == "crawl-cache-policy.v1"
        assert data["crawl_service_identity_schema"] == "crawl-service-identity.v1"

    @pytest.mark.anyio
    async def test_version_sha_uses_validated_settings_not_live_environment(
        self,
        client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("GIT_SHA", "aaaaaaaaaaaaaaaa")
        monkeypatch.setattr(settings, "git_sha", "bbbbbbbbbbbbbbbb")

        resp = await client.get("/health/version")

        assert resp.status_code == 200
        assert resp.json()["sha"] == "bbbbbbbbbbbbbbbb"

    def test_serving_config_payload_is_comprehensive_and_secret_free(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(
            settings,
            "quality_extraction_base_url",
            "https://user:endpoint-secret@quality.invalid/v1?token=query-secret",
        )
        monkeypatch.setattr(
            settings,
            "quality_extraction_api_key",
            "quality-key-secret",
        )
        monkeypatch.setattr(
            settings,
            "serving_fingerprint_key",
            "independent-fingerprint-secret-" + ("f" * 32),
        )
        monkeypatch.setattr(settings, "quality_extraction_model", "model-a")
        monkeypatch.setattr(
            settings,
            "redis_url",
            "redis://:redis-secret@cache.invalid/3",
        )
        monkeypatch.setattr(
            settings,
            "http_proxy",
            "https://proxy-user:proxy-secret@proxy.invalid:8443",
        )

        payload = health_module._serving_config_payload()
        serialized = json.dumps(payload, sort_keys=True)

        assert payload["quality_backend_configured"] is True
        assert isinstance(payload["quality_dependency_available"], bool)
        assert payload["quality_api_key_present"] is True
        assert payload["redis_configured"] is True
        assert len(str(payload["quality_endpoint_sha256"])) == 64
        assert len(str(payload["redis_endpoint_sha256"])) == 64
        assert len(str(payload["http_proxy_endpoint_sha256"])) == 64
        for secret in (
            "endpoint-secret",
            "query-secret",
            "quality-key-secret",
            "independent-fingerprint-secret",
            "redis-secret",
            "proxy-secret",
        ):
            assert secret not in serialized
        assert health_module._endpoint_identity_sha256(
            "https://first:secret@quality.invalid/v1?token=one"
        ) == health_module._endpoint_identity_sha256(
            "https://second:different@quality.invalid/v1?token=two"
        )

    @pytest.mark.anyio
    async def test_configured_quality_lane_requires_local_dependency_for_readiness(
        self,
        client,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(
            settings,
            "quality_extraction_base_url",
            "https://quality.invalid/v1",
        )
        monkeypatch.setattr(settings, "quality_extraction_api_key", "configured")
        monkeypatch.setattr(settings, "quality_extraction_model", "model-a")
        monkeypatch.setattr(
            health_module,
            "quality_dependency_available",
            lambda: False,
        )

        response = await client.get("/health/ready")
        version_response = await client.get("/health/version")

        assert response.status_code == 503
        assert response.json()["checks"]["quality_backend"] == "error"
        assert version_response.json()["quality_backend_configured"] is True
        assert version_response.json()["quality_dependency_available"] is False
        assert version_response.json()["quality_backend_enabled"] is False

    @pytest.mark.parametrize(
        "field_name",
        [
            "http_timeout_s",
            "max_concurrent_tasks",
            "playwright_timeout_s",
            "quality_extraction_timeout_s",
            "cache_ttl_s",
        ],
    )
    def test_output_or_latency_setting_changes_serving_fingerprint(
        self,
        monkeypatch: pytest.MonkeyPatch,
        field_name: str,
    ):
        before = health_module.serving_config_fingerprint()
        current = getattr(settings, field_name)
        replacement = current + 1
        monkeypatch.setattr(settings, field_name, replacement)

        assert health_module.serving_config_fingerprint() != before

    def test_endpoint_identity_and_key_presence_change_serving_fingerprint(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "quality_extraction_base_url", "")
        monkeypatch.setattr(settings, "quality_extraction_api_key", "")
        monkeypatch.setattr(settings, "quality_extraction_model", "model-a")
        disabled = health_module.serving_config_fingerprint()

        monkeypatch.setattr(
            settings,
            "quality_extraction_base_url",
            "https://quality.invalid/v1",
        )
        endpoint_changed = health_module.serving_config_fingerprint()
        monkeypatch.setattr(settings, "quality_extraction_api_key", "configured")
        enabled = health_module.serving_config_fingerprint()

        assert len({disabled, endpoint_changed, enabled}) == 3

    def test_exact_sensitive_semantics_change_public_hmac_fingerprint(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(
            settings,
            "serving_fingerprint_key",
            "independent-fingerprint-secret-" + ("f" * 32),
        )
        monkeypatch.setattr(settings, "crawler_api_token", "bearer-one")
        monkeypatch.setattr(
            settings,
            "http_proxy",
            "https://tenant-a:password-a@proxy.invalid:8443?pool=one",
        )
        first = health_module.serving_config_fingerprint()

        monkeypatch.setattr(
            settings,
            "http_proxy",
            "https://tenant-b:password-b@proxy.invalid:8443?pool=two",
        )
        proxy_rotated = health_module.serving_config_fingerprint()
        monkeypatch.setattr(
            settings,
            "quality_extraction_api_key",
            "quality-key-one",
        )
        quality_key_one = health_module.serving_config_fingerprint()
        monkeypatch.setattr(
            settings,
            "quality_extraction_api_key",
            "quality-key-two",
        )
        quality_key_two = health_module.serving_config_fingerprint()

        assert len(
            {
                first,
                proxy_rotated,
                quality_key_one,
                quality_key_two,
            }
        ) == 4

    def test_bearer_token_is_message_not_fingerprint_verifier(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(
            settings,
            "serving_fingerprint_key",
            "independent-fingerprint-secret-" + ("f" * 32),
        )
        monkeypatch.setattr(settings, "crawler_api_token", "weak-token-one")
        verifier_one = health_module._serving_fingerprint_hmac_key()
        fingerprint_one = health_module.serving_config_fingerprint()

        monkeypatch.setattr(settings, "crawler_api_token", "weak-token-two")
        verifier_two = health_module._serving_fingerprint_hmac_key()
        fingerprint_two = health_module.serving_config_fingerprint()

        assert verifier_one == verifier_two
        assert fingerprint_one != fingerprint_two

    def test_image_digest_changes_serving_fingerprint(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        before = health_module.serving_config_fingerprint()
        monkeypatch.setattr(settings, "image_digest", "sha256:" + ("a" * 64))

        assert health_module.serving_config_fingerprint() != before

    def test_cors_semantics_change_serving_fingerprint(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "cors_allow_origins", "")
        before = health_module.serving_config_fingerprint()
        monkeypatch.setattr(
            settings,
            "cors_allow_origins",
            "https://platform.example.test",
        )

        assert health_module.serving_config_fingerprint() != before

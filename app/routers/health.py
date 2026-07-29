from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from urllib.parse import urlsplit

import trafilatura
from fastapi import APIRouter, Response

from app.cache import CACHE_SCHEMA_VERSION, get_cache
from app.config import settings
from app.services.extractor import (
    ADAPTIVE_ROUTER_REVISION,
    PIPELINE_REVISION,
    native_backend_version,
)
from app.services.quality_extractor import quality_dependency_available
from app.services.rendering.manager import render_manager_is_ready as renderer_is_ready

router = APIRouter(tags=["health"])
_LOCAL_FINGERPRINT_HMAC_KEY = secrets.token_bytes(32)
CONFIG_FINGERPRINT_SCHEME = "hmac-sha256-v1"

_SERVING_CONFIG_FIELDS = (
    # Browser-facing service access semantics.
    "cors_allow_origins",
    # HTTP fetch/retry behavior.
    "http_timeout_s",
    "http_connect_timeout_s",
    "http_total_timeout_s",
    "http_max_keepalive_connections",
    "http_max_connections",
    "http_user_agent",
    "http_max_attempts",
    "http_retry_max_delay_s",
    # Recursive robots and map budgets.
    "robots_timeout_s",
    "robots_max_redirects",
    "robots_max_body_bytes",
    "robots_max_url_length",
    "robots_max_rules",
    "robots_max_records",
    "robots_max_line_chars",
    "robots_max_concurrency",
    "robots_cache_max_entries",
    "robots_cache_ttl_s",
    "robots_unavailable_cache_ttl_s",
    "robots_error_cache_ttl_s",
    "map_timeout_s",
    "map_max_download_bytes",
    "map_max_concurrency",
    # Extraction selection and worker capacity.
    "adaptive_timeout_enabled",
    "connection_warming_enabled",
    "parallel_extraction_enabled",
    "extraction_merge_mode",
    "native_extraction_enabled",
    "native_extraction_min_confidence",
    "max_concurrent_extractions",
    "max_concurrent_tasks",
    "max_concurrent_pages",
    "max_domains_per_request",
    "default_max_pages",
    "max_pending_requests",
    "max_request_body_bytes",
    "crawl_request_timeout_s",
    "max_response_output_bytes",
    "rate_limit_requests_per_second",
    "rate_limit_burst",
    "rate_limit_max_domains",
    "extract_max_text_length",
    # Playwright behavior.
    "playwright_enabled",
    "playwright_timeout_s",
    "playwright_java_script_enabled",
    "js_render_mode",
    "playwright_disable_sandbox",
    "playwright_max_html_bytes",
    # Optional quality lane and adaptive router.
    "quality_extraction_model",
    "quality_extraction_backend_revision",
    "quality_extraction_prompt_profile",
    "quality_extraction_timeout_s",
    "quality_extraction_capacity_timeout_s",
    "quality_extraction_shutdown_timeout_s",
    "quality_extraction_max_input_chars",
    "quality_extraction_max_concurrency",
    "quality_extraction_failure_threshold",
    "quality_extraction_cooldown_s",
    "adaptive_extraction_min_confidence",
    "adaptive_extraction_structural_score_threshold",
    "adaptive_extraction_structure_loss_threshold",
    "adaptive_extraction_candidate_disagreement_threshold",
    "adaptive_extraction_max_scan_chars",
    "adaptive_extraction_risky_page_types",
    # Scholarly fallback behavior.
    "scholarly_metadata_enabled",
    "scholarly_metadata_timeout_s",
    "scholarly_metadata_max_concurrency",
    "scholarly_metadata_max_response_bytes",
    "academic_pdf_fallback_timeout_s",
    # Cache behavior.
    "cache_ttl_s",
    "cache_connect_timeout_s",
    "cache_operation_timeout_s",
    "cache_failure_cooldown_s",
    "cache_max_entry_bytes",
    # Structured JSON extraction behavior.
    "extraction_model",
    "extraction_max_tokens",
    "extraction_max_input_chars",
    "structured_extraction_max_concurrency",
    "structured_extraction_timeout_s",
    "crawl4ai_compat",
)


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(response: Response) -> dict[str, object]:
    checks: dict[str, str] = {"http_client": "ok"}
    checks["native_extractor"] = "ok" if native_backend_version() != "unavailable" else "error"

    if settings.redis_url:
        checks["redis"] = "ok" if await get_cache().healthcheck() else "error"

    if settings.playwright_enabled:
        checks["playwright"] = "ok" if renderer_is_ready() else "error"

    if settings.quality_backend_configured():
        checks["quality_backend"] = (
            "ok" if quality_dependency_available() else "error"
        )

    all_ok = all(v == "ok" for v in checks.values())
    status = "ready" if all_ok else "degraded"
    if not all_ok:
        response.status_code = 503

    return {"status": status, "checks": checks}


def _endpoint_identity_sha256(value: str) -> str:
    """Hash a configured endpoint identity without embedding its plaintext."""
    normalized = value.strip()
    if not normalized:
        return ""
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError:
        # Invalid endpoint strings all fail before useful serving work. Keep a
        # configured marker without deriving an identifier from credentials.
        identity: dict[str, object] = {"configured": True, "valid": False}
    else:
        query_keys = sorted(
            part.partition("=")[0]
            for part in parsed.query.split("&")
            if part.partition("=")[0]
        )
        identity = {
            "scheme": parsed.scheme.casefold(),
            "host": (parsed.hostname or "").casefold(),
            "port": port,
            "path": parsed.path.rstrip("/") or "/",
            # Query parameter names can select an API surface; values and
            # fragments commonly carry credentials and are deliberately absent.
            "query_keys": query_keys,
        }
    encoded = json.dumps(
        identity,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _serving_config_payload() -> dict[str, object]:
    """Return an auditable, secret-free snapshot of serving semantics."""
    payload: dict[str, object] = {
        "pipeline_revision": PIPELINE_REVISION,
        "adaptive_router_revision": ADAPTIVE_ROUTER_REVISION,
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "native_backend": native_backend_version(),
        "environment": settings.environment,
        "git_sha": settings.git_sha,
        "image_digest": settings.image_digest,
        **{
            field_name: getattr(settings, field_name)
            for field_name in _SERVING_CONFIG_FIELDS
        },
        # Endpoint values can carry credentials or private topology. Bind their
        # identities without placing plaintext endpoints in this snapshot.
        "http_proxy_endpoint_sha256": _endpoint_identity_sha256(
            settings.http_proxy
        ),
        "playwright_proxy_endpoint_sha256": _endpoint_identity_sha256(
            settings.playwright_proxy
        ),
        "quality_endpoint_sha256": _endpoint_identity_sha256(
            settings.quality_extraction_base_url
        ),
        "redis_endpoint_sha256": _endpoint_identity_sha256(settings.redis_url),
        # Credential plaintext never enters this public audit payload. Presence
        # is semantic; exact values are bound only by the private HMAC input
        # below so rotations also change the fingerprint.
        "quality_api_key_present": bool(settings.quality_extraction_api_key),
        "quality_backend_configured": settings.quality_backend_configured(),
        "quality_dependency_available": quality_dependency_available(),
        "redis_configured": bool(settings.redis_url.strip()),
        "crawler_api_token_present": bool(settings.crawler_api_token),
        "serving_fingerprint_key_present": bool(
            settings.serving_fingerprint_key
        ),
        "elsevier_api_key_present": bool(settings.elsevier_api_key),
        "ieee_api_key_present": bool(settings.ieee_api_key),
        "anthropic_api_key_present": bool(settings.anthropic_api_key),
    }
    return payload


def _exact_serving_config_bytes() -> bytes:
    """Canonical exact semantics used only as private HMAC input.

    Sensitive values never leave this module as a response, log, or persisted
    record. HMAC binds rotations and endpoint query/credential changes while
    the public audit payload above remains safely redacted.
    """
    payload = {
        "redacted": _serving_config_payload(),
        "sensitive": {
            "http_proxy": settings.http_proxy,
            "playwright_proxy": settings.playwright_proxy,
            "quality_extraction_base_url": settings.quality_extraction_base_url,
            "quality_extraction_api_key": settings.quality_extraction_api_key,
            "redis_url": settings.redis_url,
            "elsevier_api_key": settings.elsevier_api_key,
            "ieee_api_key": settings.ieee_api_key,
            "anthropic_api_key": settings.anthropic_api_key,
            # Authentication is serving semantics but never HMAC key material.
            "crawler_api_token": settings.crawler_api_token,
        },
    }
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _serving_fingerprint_hmac_key() -> bytes:
    if settings.serving_fingerprint_key:
        return hashlib.sha256(
            b"clusy-serving-fingerprint-v1\0"
            + settings.serving_fingerprint_key.encode()
        ).digest()
    # Production requires an independent fingerprint key. The process-local
    # key is a safe, deliberately non-portable fallback for local/test
    # instances with no durable secret configured.
    return _LOCAL_FINGERPRINT_HMAC_KEY


def serving_config_fingerprint() -> str:
    """HMAC exact serving semantics for benchmark/deployment binding."""
    return hmac.new(
        _serving_fingerprint_hmac_key(),
        _exact_serving_config_bytes(),
        hashlib.sha256,
    ).hexdigest()


@router.get("/health/version")
async def version() -> dict[str, str | bool]:
    quality_configured = settings.quality_backend_configured()
    quality_available = quality_dependency_available()
    return {
        "sha": settings.git_sha,
        "image_digest": settings.image_digest,
        "environment": settings.environment,
        "python_version": sys.version,
        "native_extractor_version": native_backend_version(),
        "trafilatura_version": trafilatura.__version__,
        "playwright_version": _playwright_version(),
        "pipeline_revision": PIPELINE_REVISION,
        "adaptive_router_revision": ADAPTIVE_ROUTER_REVISION,
        "config_fingerprint": serving_config_fingerprint(),
        "config_fingerprint_scheme": CONFIG_FINGERPRINT_SCHEME,
        "quality_backend_configured": quality_configured,
        "quality_dependency_available": quality_available,
        "quality_backend_enabled": quality_configured and quality_available,
        "quality_backend_revision": (
            settings.quality_extraction_backend_revision
        ),
        "playwright_enabled": settings.playwright_enabled,
    }


def _playwright_version() -> str:
    try:
        # Playwright does not expose a stable public ``__version__`` attribute.
        # Distribution metadata is the supported packaging-level source and
        # remains available even when the browser driver is not running.
        return distribution_version("playwright")
    except PackageNotFoundError:
        return "not installed"

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import random
import time
from dataclasses import dataclass

import structlog

from app.config import settings

logger = structlog.get_logger()

# Bump this whenever the serialized crawl-result contract or extraction
# semantics change. Keeping the version in both the readable prefix and the
# hashed payload prevents a rolling deployment from serving results produced by
# an incompatible crawler revision.
CACHE_SCHEMA_VERSION = "v11"

# These are the runtime settings that can change the canonical crawl result
# while the source revision (GIT_SHA) remains constant. Keep the declaration
# centralized: the key builder and parameterized regression tests consume the
# same groups, so adding a serving knob requires an explicit cache decision.
CORE_OUTPUT_SETTING_NAMES = (
    "image_digest",
    "parallel_extraction_enabled",
    "extraction_merge_mode",
    "native_extraction_enabled",
    "native_extraction_min_confidence",
    "max_concurrent_extractions",
    "extract_max_text_length",
)
FETCH_OUTPUT_SETTING_NAMES = (
    "http_timeout_s",
    "http_connect_timeout_s",
    "http_total_timeout_s",
    "http_max_keepalive_connections",
    "http_max_connections",
    "http_user_agent",
    "http_max_attempts",
    "http_retry_max_delay_s",
    "rate_limit_requests_per_second",
    "rate_limit_burst",
)
RENDER_OUTPUT_SETTING_NAMES = (
    "playwright_enabled",
    "playwright_timeout_s",
    "playwright_java_script_enabled",
    "js_render_mode",
    "playwright_disable_sandbox",
    "playwright_max_html_bytes",
    "max_concurrent_pages",
)
SCHOLARLY_OUTPUT_SETTING_NAMES = (
    "scholarly_metadata_enabled",
    "scholarly_metadata_timeout_s",
    "scholarly_metadata_max_concurrency",
    "scholarly_metadata_max_response_bytes",
    "academic_pdf_fallback_timeout_s",
)
QUALITY_OUTPUT_SETTING_NAMES = (
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
)
ADAPTIVE_OUTPUT_SETTING_NAMES = (
    "adaptive_extraction_min_confidence",
    "adaptive_extraction_structural_score_threshold",
    "adaptive_extraction_structure_loss_threshold",
    "adaptive_extraction_candidate_disagreement_threshold",
    "adaptive_extraction_max_scan_chars",
    "adaptive_extraction_risky_page_types",
)


@dataclass
class CacheEntry:
    data: bytes
    ttl: int


class RedisCache:
    def __init__(self) -> None:
        self._redis = None
        self._connect_lock = asyncio.Lock()
        self._retry_after = 0.0
        self._failure_count = 0

    async def _mark_unavailable(self) -> None:
        client = self._redis
        self._redis = None
        self._failure_count += 1
        cooldown = min(
            settings.cache_failure_cooldown_s * (2 ** (self._failure_count - 1)),
            60.0,
        )
        self._retry_after = time.monotonic() + cooldown
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()

    async def _ensure_client(self) -> bool:
        if not settings.redis_url:
            return False
        if self._redis is not None:
            return True
        if time.monotonic() < self._retry_after:
            return False
        async with self._connect_lock:
            if self._redis is not None:
                return True
            if time.monotonic() < self._retry_after:
                return False
            candidate = None
            try:
                import redis.asyncio as aioredis

                # redis.asyncio.from_url ships without type annotations.
                candidate = aioredis.from_url(  # type: ignore[no-untyped-call]
                    settings.redis_url,
                    socket_timeout=settings.cache_operation_timeout_s,
                    socket_connect_timeout=settings.cache_connect_timeout_s,
                )
                async with asyncio.timeout(settings.cache_connect_timeout_s):
                    await candidate.ping()
                self._redis = candidate
                self._failure_count = 0
                self._retry_after = 0.0
                logger.info("redis_cache_connected")
                return True
            except Exception:
                logger.warning("redis_cache_unavailable")
                if candidate is not None:
                    with contextlib.suppress(Exception):
                        await candidate.aclose()
                self._failure_count += 1
                cooldown = min(
                    settings.cache_failure_cooldown_s
                    * (2 ** (self._failure_count - 1)),
                    60.0,
                )
                self._retry_after = time.monotonic() + cooldown
                return False

    def write_available(self) -> bool:
        """Return whether a cache write is not known to be unavailable.

        This advisory gate deliberately does not connect or ping: `set` remains
        the authoritative, concurrency-safe operation.  That preserves the
        existing connection/recovery ordering while allowing callers to skip
        expensive value construction when Redis is disabled or inside an
        explicit failure cooldown.  A concurrent failure after this check can
        only cause redundant value construction; `set` still fails closed.
        """
        if not settings.redis_url:
            return False
        return self._redis is not None or time.monotonic() >= self._retry_after

    async def get(self, key: str) -> bytes | None:
        if not await self._ensure_client():
            return None
        assert self._redis is not None
        try:
            async with asyncio.timeout(settings.cache_operation_timeout_s):
                value: bytes | None = await self._redis.get(key)
            return value
        except Exception:
            await self._mark_unavailable()
            logger.warning("redis_cache_read_failed")
            return None

    async def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        if len(value) > settings.cache_max_entry_bytes:
            logger.info("redis_cache_entry_skipped", bytes=len(value))
            return
        if not await self._ensure_client():
            return
        assert self._redis is not None
        ttl = ttl or settings.cache_ttl_s
        # Small jitter prevents a deployment's hot keys expiring in lockstep.
        ttl = max(1, round(ttl * random.uniform(0.95, 1.05)))
        try:
            async with asyncio.timeout(settings.cache_operation_timeout_s):
                await self._redis.setex(key, ttl, value)
        except Exception:
            await self._mark_unavailable()
            logger.warning("redis_cache_write_failed")

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None
        self._retry_after = 0.0
        self._failure_count = 0

    async def healthcheck(self) -> bool:
        if not settings.redis_url:
            return True
        if not await self._ensure_client():
            return False
        assert self._redis is not None
        try:
            async with asyncio.timeout(settings.cache_operation_timeout_s):
                return bool(await self._redis.ping())
        except Exception:
            await self._mark_unavailable()
            return False


_cache: RedisCache | None = None


def get_cache() -> RedisCache:
    global _cache
    if _cache is None:
        _cache = RedisCache()
    return _cache


def _settings_snapshot(names: tuple[str, ...]) -> dict[str, object]:
    return {name: getattr(settings, name) for name in names}


def _private_cache_identity(value: str, *, purpose: str) -> str:
    """Digest sensitive config used only inside the final private cache key.

    The raw value and this intermediate identity are never exposed through an
    API, log, or standalone cache record. Production uses the independent
    serving-fingerprint secret as an HMAC key; local instances without it fall
    back to a domain-separated digest that remains nested inside the final
    cache hash.
    """
    if value == "":
        return ""
    message = f"{purpose}\0{value}".encode()
    if settings.serving_fingerprint_key:
        key = hashlib.sha256(
            b"clusy-private-cache-identity-v1\0"
            + settings.serving_fingerprint_key.encode()
        ).digest()
        return hmac.new(key, message, hashlib.sha256).hexdigest()
    return hashlib.sha256(message).hexdigest()


def _cache_semantics_payload(
    *,
    extraction_profile: str,
    native_backend: str,
    pipeline_revision: str,
    quality_revision: str,
    adaptive_revision: str,
) -> dict[str, object]:
    """Build the secret-free runtime semantics bound by a crawl cache key."""
    assisted_profile = extraction_profile in {"adaptive", "quality"}
    quality_dependency_ready = False
    if assisted_profile:
        from app.services.quality_extractor import quality_dependency_available

        quality_dependency_ready = quality_dependency_available()
    return {
        "pipeline_revision": pipeline_revision,
        "native_backend": native_backend,
        "core": _settings_snapshot(CORE_OUTPUT_SETTING_NAMES),
        "fetch": {
            **_settings_snapshot(FETCH_OUTPUT_SETTING_NAMES),
            # Full proxy identity is private-key material: credentials and
            # query routing can select egress geography and origin content.
            "http_proxy_sha256": _private_cache_identity(
                settings.http_proxy,
                purpose="http-proxy",
            ),
        },
        "render": {
            **_settings_snapshot(RENDER_OUTPUT_SETTING_NAMES),
            "playwright_proxy_sha256": _private_cache_identity(
                settings.playwright_proxy,
                purpose="playwright-proxy",
            ),
        },
        "scholarly": {
            **_settings_snapshot(SCHOLARLY_OUTPUT_SETTING_NAMES),
            # Provider credentials can change entitlement, provider response,
            # and fallback selection even at the same source revision.
            "elsevier_api_key_sha256": _private_cache_identity(
                settings.elsevier_api_key,
                purpose="elsevier-api-key",
            ),
            "ieee_api_key_sha256": _private_cache_identity(
                settings.ieee_api_key,
                purpose="ieee-api-key",
            ),
        },
        "quality": (
            {
                **_settings_snapshot(QUALITY_OUTPUT_SETTING_NAMES),
                "backend_configured": settings.quality_backend_configured(),
                "dependency_available": quality_dependency_ready,
                "base_url_sha256": _private_cache_identity(
                    settings.quality_extraction_base_url,
                    purpose="quality-base-url",
                ),
                "api_key_sha256": _private_cache_identity(
                    settings.quality_extraction_api_key,
                    purpose="quality-api-key",
                ),
                "revision": quality_revision,
            }
            if assisted_profile
            else None
        ),
        "adaptive": (
            {
                **_settings_snapshot(ADAPTIVE_OUTPUT_SETTING_NAMES),
                "revision": adaptive_revision,
            }
            if extraction_profile == "adaptive"
            else None
        ),
    }


def make_cache_key(
    url: str,
    js_render: bool,
    wait_for_selector: str | None,
    *,
    word_count_threshold: int = 10,
    auto_render: bool = False,
    extraction_profile: str = "balanced",
) -> str:
    """Build a key for every input/configuration value that can change output.

    Output projection (markdown/html/links) is intentionally absent: the crawl
    pipeline caches one canonical result containing markdown, metadata, and
    links, then projects that result for each caller. Raw HTML remains
    intentionally uncached.
    """
    try:
        from clusy_native import backend_version

        native_backend = backend_version()
    except (ImportError, RuntimeError):
        native_backend = "unavailable"

    quality_revision = ""
    adaptive_revision = ""
    if extraction_profile in {"adaptive", "quality"}:
        from app.services.quality_extractor import MINERU_HTML_REVISION

        quality_revision = MINERU_HTML_REVISION
    from app.services.extractor import PIPELINE_REVISION

    if extraction_profile == "adaptive":
        from app.services.extractor import ADAPTIVE_ROUTER_REVISION

        adaptive_revision = ADAPTIVE_ROUTER_REVISION

    payload = json.dumps(
        {
            "v": CACHE_SCHEMA_VERSION,
            "build": settings.git_sha,
            "u": url,
            "js": js_render,
            "auto_js": auto_render,
            "sel": wait_for_selector or "",
            "words": word_count_threshold,
            "profile": extraction_profile,
            "semantics": _cache_semantics_payload(
                extraction_profile=extraction_profile,
                native_backend=native_backend,
                pipeline_revision=PIPELINE_REVISION,
                quality_revision=quality_revision,
                adaptive_revision=adaptive_revision,
            ),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return f"crawler:{CACHE_SCHEMA_VERSION}:{digest}"

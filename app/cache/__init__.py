from __future__ import annotations

import asyncio
import contextlib
import hashlib
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
CACHE_SCHEMA_VERSION = "v7"


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

    assisted_profile = extraction_profile in {"adaptive", "quality"}
    quality_revision = ""
    adaptive_revision = ""
    if assisted_profile:
        from app.services.quality_extractor import MINERU_HTML_REVISION

        quality_revision = MINERU_HTML_REVISION
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
            "quality_base_url": (
                settings.quality_extraction_base_url
                if assisted_profile
                else ""
            ),
            "quality_model": (
                settings.quality_extraction_model
                if assisted_profile
                else ""
            ),
            "quality_prompt_profile": (
                settings.quality_extraction_prompt_profile
                if assisted_profile
                else ""
            ),
            "quality_max_input": (
                settings.quality_extraction_max_input_chars
                if assisted_profile
                else None
            ),
            "quality_revision": quality_revision,
            "adaptive_revision": adaptive_revision,
            "adaptive_min_confidence": (
                settings.adaptive_extraction_min_confidence
                if extraction_profile == "adaptive"
                else None
            ),
            "adaptive_structural_score": (
                settings.adaptive_extraction_structural_score_threshold
                if extraction_profile == "adaptive"
                else None
            ),
            "adaptive_max_scan_chars": (
                settings.adaptive_extraction_max_scan_chars
                if extraction_profile == "adaptive"
                else None
            ),
            "adaptive_risky_page_types": (
                settings.adaptive_extraction_risky_page_types
                if extraction_profile == "adaptive"
                else ""
            ),
            "render_mode": settings.js_render_mode,
            "playwright": settings.playwright_enabled,
            "javascript": settings.playwright_java_script_enabled,
            "parallel": settings.parallel_extraction_enabled,
            "merge": settings.extraction_merge_mode,
            "native": getattr(settings, "native_extraction_enabled", False),
            "native_backend": native_backend,
            "native_confidence": getattr(
                settings,
                "native_extraction_min_confidence",
                None,
            ),
            "max_text": settings.extract_max_text_length,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return f"crawler:{CACHE_SCHEMA_VERSION}:{digest}"

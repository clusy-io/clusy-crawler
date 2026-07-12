from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import structlog

from app.config import settings

logger = structlog.get_logger()


@dataclass
class CacheEntry:
    data: bytes
    ttl: int


class RedisCache:
    _redis = None

    async def _ensure_client(self) -> bool:
        if not settings.redis_url:
            return False
        if self._redis is not None:
            return True
        try:
            import redis.asyncio as aioredis

            # redis.asyncio.from_url ships without type annotations.
            self._redis = aioredis.from_url(  # type: ignore[no-untyped-call]
                settings.redis_url,
                socket_timeout=3,
                socket_connect_timeout=3,
            )
            await self._redis.ping()
            logger.info("redis_cache_connected", url=settings.redis_url)
            return True
        except Exception:
            logger.warning("redis_cache_unavailable")
            self._redis = None
            return False

    async def get(self, key: str) -> bytes | None:
        if not await self._ensure_client():
            return None
        assert self._redis is not None
        try:
            value: bytes | None = await self._redis.get(key)
            return value
        except Exception:
            return None

    async def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        if not await self._ensure_client():
            return
        assert self._redis is not None
        ttl = ttl or settings.cache_ttl_s
        try:
            await self._redis.setex(key, ttl, value)
        except Exception as e:
            logger.warning("redis_cache_write_failed", error=str(e))

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None


_cache: RedisCache | None = None


def get_cache() -> RedisCache:
    global _cache
    if _cache is None:
        _cache = RedisCache()
    return _cache


def make_cache_key(url: str, js_render: bool, wait_for_selector: str | None) -> str:
    payload = json.dumps(
        {"u": url, "js": js_render, "sel": wait_for_selector or ""},
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return f"crawler:{digest}"

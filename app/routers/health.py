from __future__ import annotations

import os
import sys
from typing import cast

import trafilatura
from fastapi import APIRouter

from app.config import settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready() -> dict[str, object]:
    checks: dict[str, str] = {"http_client": "ok"}

    if settings.redis_url:
        try:
            import redis.asyncio as aioredis

            # redis.asyncio.from_url ships without type annotations.
            r = aioredis.from_url(settings.redis_url)  # type: ignore[no-untyped-call]
            await r.ping()
            await r.aclose()
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "error"

    if settings.playwright_enabled:
        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                await browser.close()
            checks["playwright"] = "ok"
        except Exception:
            checks["playwright"] = "error"

    all_ok = all(v == "ok" for v in checks.values())
    status = "ready" if all_ok else "degraded"

    return {"status": status, "checks": checks}


@router.get("/health/version")
async def version() -> dict[str, str]:
    sha = os.getenv("GIT_SHA", "unknown")
    return {
        "sha": sha,
        "environment": settings.environment,
        "python_version": sys.version,
        "trafilatura_version": trafilatura.__version__,
        "playwright_version": _playwright_version(),
    }


def _playwright_version() -> str:
    try:
        # Private playwright module; `version` is absent in some releases, hence
        # the ImportError guard. cast keeps the return typed without a runtime op.
        from playwright._impl._api_structures import version  # type: ignore[attr-defined]

        return cast("str", version)
    except ImportError:
        return "not installed"

from __future__ import annotations

import os
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version

import trafilatura
from fastapi import APIRouter, Response

from app.cache import get_cache
from app.config import settings
from app.services.extractor import native_backend_version
from app.services.renderer import renderer_is_ready

router = APIRouter(tags=["health"])


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

    all_ok = all(v == "ok" for v in checks.values())
    status = "ready" if all_ok else "degraded"
    if not all_ok:
        response.status_code = 503

    return {"status": status, "checks": checks}


@router.get("/health/version")
async def version() -> dict[str, str]:
    sha = os.getenv("GIT_SHA", "unknown")
    return {
        "sha": sha,
        "environment": settings.environment,
        "python_version": sys.version,
        "native_extractor_version": native_backend_version(),
        "trafilatura_version": trafilatura.__version__,
        "playwright_version": _playwright_version(),
    }


def _playwright_version() -> str:
    try:
        # Playwright does not expose a stable public ``__version__`` attribute.
        # Distribution metadata is the supported packaging-level source and
        # remains available even when the browser driver is not running.
        return distribution_version("playwright")
    except PackageNotFoundError:
        return "not installed"

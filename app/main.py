from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.cache import get_cache
from app.config import settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
from app.lib.http_client import aclose_http_client
from app.lib.logging import configure_logging
from app.middleware.auth import AuthMiddleware
from app.middleware.resource_limits import ResourceLimitMiddleware
from app.routers import crawl, extract, health
from app.routers import map as map_router
from app.services.crawler import shutdown_crawler, start_crawler
from app.services.quality_extractor import close_quality_extractor
from app.services.rendering.manager import (
    start_render_manager as start_renderer,
)
from app.services.rendering.manager import (
    stop_render_manager as stop_renderer,
)
from app.services.robots import close_robots_policy
from app.services.structured import close_structured_client
from app.version import SERVICE_VERSION

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    logger.info(
        "crawler_starting",
        environment=settings.environment,
        port=settings.crawler_port,
    )
    try:
        start_crawler()
        if settings.playwright_enabled:
            try:
                await start_renderer()
            except Exception as exc:
                logger.error(
                    "playwright_startup_failed",
                    error_type=type(exc).__name__,
                )
                if settings.environment == "prod":
                    raise
        yield
    finally:
        try:
            await shutdown_crawler()
        except Exception as drain_error:
            logger.warning(
                "crawler_shutdown_cleanup_failed",
                error_type=type(drain_error).__name__,
            )
        cleanup_steps = (
            ("renderer", stop_renderer()),
            ("quality_extractor", close_quality_extractor()),
            ("structured_client", close_structured_client()),
            ("robots_policy", close_robots_policy()),
            ("cache", get_cache().close()),
            ("http_client", aclose_http_client()),
        )
        cleanup_results = await asyncio.gather(
            *(cleanup for _, cleanup in cleanup_steps),
            return_exceptions=True,
        )
        for (component, _), cleanup_result in zip(
            cleanup_steps,
            cleanup_results,
            strict=True,
        ):
            if isinstance(cleanup_result, Exception):
                logger.warning(
                    "crawler_shutdown_cleanup_failed",
                    component=component,
                    error_type=type(cleanup_result).__name__,
                )
        logger.info("crawler_shutdown")


app = FastAPI(
    title="Clusy Crawler",
    version=SERVICE_VERSION,
    lifespan=lifespan,
)

# Middleware
app.add_middleware(
    ResourceLimitMiddleware,
    max_body_bytes=settings.max_request_body_bytes,
    max_active_requests=settings.max_pending_requests,
    request_timeout_s=settings.crawl_request_timeout_s,
)
# Starlette makes the last-added middleware outermost. Authenticate before
# reading or admitting request bodies so unauthenticated slow clients cannot
# consume the crawler's bounded request slots.
app.add_middleware(AuthMiddleware)
# CORS is OFF by default (no browser origin allowed). Opt in explicitly via
# CORS_ALLOW_ORIGINS when a browser client must call the service directly.
_cors_origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

# Routers
app.include_router(health.router)
app.include_router(crawl.router)
app.include_router(extract.router)
app.include_router(map_router.router)


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    route = getattr(request.scope.get("route"), "path", "(unmatched)")
    logger.error(
        "unhandled_exception",
        error_type=type(exc).__name__,
        route=route,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "status": "error"},
    )

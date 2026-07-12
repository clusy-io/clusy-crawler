from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
from app.lib.http_client import aclose_http_client
from app.lib.logging import configure_logging
from app.middleware.auth import AuthMiddleware
from app.routers import crawl, extract, health
from app.routers import map as map_router

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    logger.info(
        "crawler_starting",
        environment=settings.environment,
        port=settings.crawler_port,
    )
    yield
    await aclose_http_client()
    logger.info("crawler_shutdown")


app = FastAPI(
    title="Clusy Crawler",
    version="0.1.0",
    lifespan=lifespan,
)

# Middleware
app.add_middleware(AuthMiddleware)
# CORS is OFF by default (no browser origin allowed) — this is an internal
# service-to-service API. Opt in explicitly via CORS_ALLOW_ORIGINS.
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
    logger.error("unhandled_exception", error=str(exc), path=str(request.url))
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "status": "error"},
    )

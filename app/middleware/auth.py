from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from fastapi import Request, Response
    from starlette.middleware.base import RequestResponseEndpoint

from app.config import settings

UNAUTHENTICATED_ROUTES = {"/health", "/health/ready", "/health/version", "/docs", "/openapi.json"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.url.path in UNAUTHENTICATED_ROUTES:
            return await call_next(request)

        if not settings.crawler_api_token:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid Authorization header", "status": "error"},
            )

        token = auth_header.removeprefix("Bearer ")
        if not secrets.compare_digest(token, settings.crawler_api_token):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid API token", "status": "error"},
            )

        return await call_next(request)

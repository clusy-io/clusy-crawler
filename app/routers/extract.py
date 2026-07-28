from __future__ import annotations

import structlog
from fastapi import APIRouter

from app.models.requests import HTMLRequest, MDRequest
from app.models.responses import HTMLResponse, MDResponse
from app.services.crawler import crawl_urls

logger = structlog.get_logger()

router = APIRouter(tags=["extract"])


@router.post("/md")
async def extract_markdown(req: MDRequest) -> MDResponse:
    # Preserve the distinction between an omitted option (use the configured
    # auto policy) and an explicit false (never render this request).
    js_render_option = req.options.get("js_render")
    js_render = js_render_option if isinstance(js_render_option, bool) else None
    wait_for = req.options.get("wait_for_selector")

    results = await crawl_urls(
        urls=[req.url],
        js_render=js_render,
        wait_for_selector=wait_for,
        word_count_threshold=req.word_count_threshold,
        extraction_profile=req.extraction_profile,
    )

    result = results[0]
    if result.error:
        return MDResponse(status="error", markdown=result.error, metadata=result.metadata)

    return MDResponse(status="ok", markdown=result.markdown, metadata=result.metadata)


@router.post("/html")
async def extract_html(req: HTMLRequest) -> HTMLResponse:
    # Reuse the canonical crawl path so this compatibility endpoint shares the
    # same crawl semaphore, cancellation semantics, and response-output budget
    # as /crawl and /md. Requesting HTML already forces a live crawl because raw
    # HTML is intentionally excluded from Redis.
    results = await crawl_urls(
        urls=[req.url],
        js_render=req.js_render,
        formats=["html"],
    )
    result = results[0]

    if result.error:
        return HTMLResponse(status="error", html=result.error, metadata=result.metadata)

    return HTMLResponse(status="ok", html=result.html or "", metadata=result.metadata)

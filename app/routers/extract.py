from __future__ import annotations

import structlog
from fastapi import APIRouter

from app.models.requests import HTMLRequest, MDRequest
from app.models.responses import HTMLResponse, MDResponse
from app.services import fetcher as fetcher_module
from app.services.crawler import crawl_urls

logger = structlog.get_logger()

router = APIRouter(tags=["extract"])


@router.post("/md")
async def extract_markdown(req: MDRequest) -> MDResponse:
    js_render = req.options.get("js_render", False)
    wait_for = req.options.get("wait_for_selector")

    results = await crawl_urls(
        urls=[req.url],
        js_render=js_render,
        wait_for_selector=wait_for,
        word_count_threshold=req.word_count_threshold,
    )

    result = results[0]
    if result.error:
        return MDResponse(status="error", markdown=result.error, metadata=result.metadata)

    return MDResponse(status="ok", markdown=result.markdown, metadata=result.metadata)


@router.post("/html")
async def extract_html(req: HTMLRequest) -> HTMLResponse:
    result = await fetcher_module.fetch_url(req.url, js_render=req.js_render)

    if result.error:
        return HTMLResponse(status="error", html=result.error, metadata=result.metadata)

    return HTMLResponse(status="ok", html=result.html, metadata=result.metadata)

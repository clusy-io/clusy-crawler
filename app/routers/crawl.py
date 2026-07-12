from __future__ import annotations

import time

import structlog
from fastapi import APIRouter

from app.models.requests import CrawlRequest
from app.models.responses import CrawlResponse
from app.services.crawler import crawl_urls

logger = structlog.get_logger()

router = APIRouter(tags=["crawl"])


@router.post("/crawl")
async def crawl(req: CrawlRequest) -> CrawlResponse:
    start = time.monotonic()
    results = await crawl_urls(
        urls=req.urls,
        js_render=req.js_render,
        wait_for_selector=req.wait_for_selector,
        word_count_threshold=req.word_count_threshold,
        formats=req.formats,
        max_age=req.max_age,
        json_schema=req.json_schema,
        extraction_prompt=req.extraction_prompt,
    )
    elapsed_ms = (time.monotonic() - start) * 1000

    return CrawlResponse(
        status="ok",
        results=results,
        total_time_ms=round(elapsed_ms, 1),
        total_pages=len(req.urls),
    )

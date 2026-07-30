from __future__ import annotations

import time

import structlog
from fastapi import APIRouter

from app.config import settings
from app.models.requests import CrawlRequest
from app.models.responses import CrawlResponse, ServiceIdentityReceipt
from app.routers.health import serving_config_fingerprint
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
        extraction_profile=req.extraction_profile,
        formats=[str(output_format) for output_format in req.formats],
        max_age=req.max_age,
        store_in_cache=req.store_in_cache,
        json_schema=req.json_schema,
        extraction_prompt=req.extraction_prompt,
        max_depth=req.max_depth,
        allow_subdomains=req.allow_subdomains,
        max_pages=req.max_pages,
        priority=req.priority,
    )
    elapsed_ms = (time.monotonic() - start) * 1000

    return CrawlResponse(
        status="ok",
        results=results,
        total_time_ms=round(elapsed_ms, 1),
        total_pages=len(results) if req.max_depth > 0 else len(req.urls),
        service_identity=ServiceIdentityReceipt(
            revision=settings.git_sha,
            config_fingerprint=serving_config_fingerprint(),
            image_digest=settings.image_digest,
        ),
    )

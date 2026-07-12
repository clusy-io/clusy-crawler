from __future__ import annotations

import asyncio
import re
import time as time_module
from typing import Any, cast

import structlog

from app.cache import get_cache, make_cache_key
from app.config import settings
from app.lib.http_client import record_latency
from app.models.responses import CrawlResult, ExtractionMetadata
from app.services import fetcher as fetcher_module
from app.services.extractor import extract_content_async
from app.services.rate_limiter import get_rate_limiter

logger = structlog.get_logger()

# Below this word count a "static" fetch is treated as essentially empty and we
# escalate to a JS render. Kept low so genuinely short-but-complete pages (e.g.
# example.com) are NOT needlessly rendered — true JS shells are caught earlier
# by needs_js_rendering()/bot-block detection, not by this floor.
SPARSE_WORD_FLOOR = 15

_crawl_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _crawl_semaphore
    if _crawl_semaphore is None:
        _crawl_semaphore = asyncio.Semaphore(settings.max_concurrent_tasks)
    return _crawl_semaphore


async def _crawl_single_url(
    url: str,
    js_render: bool | None = None,
    wait_for_selector: str | None = None,
    word_count_threshold: int = 10,
    formats: list[str] | None = None,
    max_age: int | None = None,
) -> CrawlResult:
    import orjson

    decide_js = js_render if js_render is not None else False
    formats = formats or ["markdown"]

    # Auto-force JS rendering for known bot-wall / SPA domains
    if not decide_js and FORCE_JS_DOMAINS.search(url):
        decide_js = True
        logger.info("force_js_rendering", url=url)

    cache = get_cache()
    cache_key = make_cache_key(url, decide_js, wait_for_selector)
    # max_age == 0 bypasses the cache entirely (always re-crawl). "html" output
    # is never cached (too large), so those requests always take the live path.
    if max_age != 0 and "html" not in formats:
        cached = await cache.get(cache_key)
        if cached is not None:
            envelope = orjson.loads(cached)
            cached_at = envelope.get("t", 0)
            age = time_module.time() - cached_at
            # Serve only if fresh enough for this request's freshness bar.
            if max_age is None or age <= max_age:
                result = CrawlResult(**envelope["r"])
                result.cached = True
                return _project_formats(result, formats)

    rate_limiter = get_rate_limiter()
    await rate_limiter.acquire(url)

    t_start = time_module.monotonic()

    sem = _get_semaphore()
    async with sem:
        loop = asyncio.get_running_loop()

        fetch_result = await fetcher_module.fetch_url(
            url, js_render=decide_js, wait_for_selector=wait_for_selector,
        )

        if fetch_result.error:
            return CrawlResult(url=url, error=fetch_result.error)

        # Record latency for adaptive timeout learning
        if fetch_result.latency_ms > 0:
            record_latency(url, fetch_result.latency_ms)
        else:
            record_latency(url, (time_module.monotonic() - t_start) * 1000)

        # PDF / academic content extraction (pypdfium2 is blocking → offload)
        is_pdf = "application/pdf" in fetch_result.content_type.lower()
        if is_pdf and fetch_result.raw_bytes:
            from app.services.academic import extract_pdf

            paper = await loop.run_in_executor(
                None, extract_pdf, fetch_result.raw_bytes, url
            )
            return CrawlResult(
                url=url,
                markdown=paper.to_markdown(),
                metadata=ExtractionMetadata(
                    title=paper.title,
                    source_url=url,
                    content_type=fetch_result.content_type,
                    status_code=fetch_result.status_code,
                    word_count=paper.word_count,
                    rendered=False,
                    extraction_strategy="pypdfium2+academic",
                ),
            )

        # Auto PDF fallback: arXiv abstract pages → fetch PDF directly
        is_arxiv_abs = "arxiv.org/abs/" in url
        if is_arxiv_abs and not fetch_result.raw_bytes:
            pdf_url = url.replace("arxiv.org/abs/", "arxiv.org/pdf/") + ".pdf"
            logger.info("arxiv_pdf_fallback", url=url, pdf_url=pdf_url)
            pdf_result = await fetcher_module.fetch_url(pdf_url)
            if not pdf_result.error and pdf_result.raw_bytes:
                from app.services.academic import extract_pdf

                paper = await loop.run_in_executor(
                    None, extract_pdf, pdf_result.raw_bytes, pdf_url
                )
                return CrawlResult(
                    url=url,
                    markdown=paper.to_markdown(),
                    metadata=ExtractionMetadata(
                        title=paper.title,
                        source_url=url,
                        content_type="application/pdf",
                        status_code=200,
                        word_count=paper.word_count,
                        rendered=False,
                        extraction_strategy="arxiv-pdf-fallback",
                    ),
                )

        # Conditional JS rendering — extract ONCE, escalate only when needed.
        #
        # The old path ran a full synchronous extraction just to compute a word
        # count, then extracted AGAIN — double work on every page, on the event
        # loop. Here we check cheap regex signals first; if the static HTML is a
        # bot wall or SPA shell we skip extraction and render directly. Otherwise
        # we extract once and only re-render when the page yielded ~nothing.
        extraction = None
        rendered = decide_js
        if (
            not decide_js
            and settings.js_render_mode == "conditional"
            and settings.playwright_enabled
        ):
            from app.services.renderer import needs_js_rendering

            escalate = False
            trigger = ""
            if _detect_bot_block(fetch_result.html):
                escalate, trigger = True, "bot_block"
            elif needs_js_rendering(fetch_result.html, url):
                escalate, trigger = True, "js_detected"
            else:
                extraction = await extract_content_async(fetch_result.html, url)
                if extraction.word_count < max(word_count_threshold, SPARSE_WORD_FLOOR):
                    escalate, trigger = True, "sparse"

            if escalate:
                logger.info("escalating_to_playwright", url=url, trigger=trigger)
                pw_result = await fetcher_module.fetch_url(
                    url, js_render=True, wait_for_selector=wait_for_selector,
                )
                if not pw_result.error and pw_result.html:
                    pw_extraction = await extract_content_async(pw_result.html, url)
                    # Rendering can regress (interstitials, lazy content) — keep
                    # whichever extraction actually captured more content.
                    if extraction is None or pw_extraction.word_count >= extraction.word_count:
                        fetch_result = pw_result
                        extraction = pw_extraction
                        rendered = True

        if extraction is None:
            extraction = await extract_content_async(fetch_result.html, url)

        # An empty extraction is a failed crawl, not a silent empty success —
        # surface it (e.g. a 401/403 bot wall that even the render couldn't pass)
        # so the caller knows the page was blocked rather than genuinely empty.
        if extraction.word_count == 0:
            return CrawlResult(
                url=url,
                error=f"no content extracted (HTTP {fetch_result.status_code}"
                f"{', rendered' if rendered else ''}) — page may be blocked",
            )

        # Academic structure detection (lxml walk is blocking → offload)
        if _is_academic_content(fetch_result.html, url) and extraction.word_count > 200:
            from app.services.academic import extract_long_html

            paper = await loop.run_in_executor(
                None, extract_long_html, fetch_result.html, url
            )
            if paper.title or paper.abstract or len(paper.sections) > 3:
                return CrawlResult(
                    url=url,
                    markdown=paper.to_markdown(),
                    metadata=ExtractionMetadata(
                        title=paper.title,
                        description=paper.abstract[:500] if paper.abstract else "",
                        source_url=url,
                        content_type=fetch_result.content_type,
                        status_code=fetch_result.status_code,
                        word_count=paper.word_count,
                        rendered=rendered,
                        extraction_strategy="academic-html",
                    ),
                )

        metadata = ExtractionMetadata(
            title=extraction.title,
            description=extraction.description,
            language=extraction.language,
            source_url=url,
            content_type=fetch_result.content_type,
            status_code=fetch_result.status_code,
            word_count=extraction.word_count,
            rendered=rendered,
            extraction_strategy=extraction.strategy,
        )

        result = CrawlResult(url=url, markdown=extraction.text, metadata=metadata)
        if "links" in formats:
            result.links = _extract_links(fetch_result.html, url)

        # Cache markdown + metadata (+ links) with a timestamp for max_age checks.
        # HTML is intentionally NOT cached — it can be up to 10MB/entry.
        await cache.set(
            cache_key,
            orjson.dumps({"t": time_module.time(), "r": result.model_dump()}),
        )

        if "html" in formats:
            result.html = fetch_result.html
        return _project_formats(result, formats)


# Domains that ALWAYS need JS rendering — bot walls, SPAs, etc.
FORCE_JS_DOMAINS = re.compile(
    r"acm\.org|springer\.com|ieee\.org|sciencedirect\.com|"
    r"nature\.com/articles|cell\.com|nejm\.org|thelancet\.com|"
    r"medium\.com|substack\.com",
    re.IGNORECASE,
)


ACADEMIC_TRIGGERS = re.compile(
    r"arxiv\.org|acm\.org|springer\.com|ieee\.org|sciencedirect\.com|"
    r"nature\.com|sci-hub|pubmed|doi\.org|researchgate|semanticscholar|"
    r"neurips|icml|iclr|aclweb|aaai|ijcai|cvpr|eccv|iccv|"
    r"/pdf/|/paper/|/abstract/",
    re.IGNORECASE,
)

LONG_CONTENT_TRIGGERS = re.compile(
    r"eprint|preprint|manuscript|dissertation|thesis|technical.report|"
    r"white.paper|proceedings|journal|conference",
    re.IGNORECASE,
)


BOT_BLOCK_SIGNATURES = re.compile(
    r"just a moment|checking your browser|cf-browser-verification|"
    r"enable javascript|please enable cookies|attention required|"
    r"captcha|ddos-guard|_cf_chl_opt|cloudflare|"
    r"access denied|request blocked|security check|"
    r"browser verification|human verification",
    re.IGNORECASE,
)


def _detect_bot_block(html: str) -> bool:
    """Detect if we hit a bot-detection wall (Cloudflare, DDoS-Guard, etc.)."""
    stripped = html[:3000].lower()
    if BOT_BLOCK_SIGNATURES.search(stripped):
        return True
    # Very short HTML with no content = likely bot block
    visible = re.sub(r"<[^>]+>", " ", stripped)
    visible = re.sub(r"\s+", " ", visible).strip()
    return len(html) < 500 and len(visible) < 100


def _is_academic_content(html: str, url: str) -> bool:
    """Route to the structured academic-paper extractor ONLY for genuine papers.

    The path is meant for arXiv/ACM/IEEE-style paper pages and LaTeX source. The
    old heuristic also fired on any page containing "references"/"citation"/"doi"
    — which every Wikipedia article has — sending 600KB encyclopedic pages into
    the slow paper parser (and lower-quality output). Require a real signal: an
    academic URL or LaTeX markup.
    """
    if ACADEMIC_TRIGGERS.search(url):
        return True
    lower = html[:50000].lower()
    return bool(re.search(r"\\begin\{|\\cite\{|\\ref\{|\\usepackage", lower))


def _extract_links(html: str, base_url: str) -> list[str]:
    """Collect de-duplicated absolute http(s) links from the page."""
    from urllib.parse import urljoin, urlparse

    seen: set[str] = set()
    out: list[str] = []
    for m in re.finditer(r'<a\b[^>]*\bhref=["\']([^"\'#]+)["\']', html, re.IGNORECASE):
        href = m.group(1).strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "data:")):
            continue
        absolute = urljoin(base_url, href)
        if urlparse(absolute).scheme not in ("http", "https"):
            continue
        if absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
        if len(out) >= 1000:
            break
    return out


def _project_formats(result: CrawlResult, formats: list[str]) -> CrawlResult:
    """Drop optional fields the caller did not request."""
    if "html" not in formats:
        result.html = None
    if "links" not in formats:
        result.links = None
    return result


async def crawl_urls(
    urls: list[str],
    js_render: bool | None = None,
    wait_for_selector: str | None = None,
    word_count_threshold: int = 10,
    formats: list[str] | None = None,
    max_age: int | None = None,
    json_schema: dict[str, Any] | None = None,
    extraction_prompt: str | None = None,
) -> list[CrawlResult]:
    formats = formats or ["markdown"]
    tasks = [
        _crawl_single_url(
            u, js_render, wait_for_selector, word_count_threshold, formats, max_age
        )
        for u in urls
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    out: list[CrawlResult] = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            out.append(CrawlResult(url=urls[i], error=str(r)))
        else:
            # gather(return_exceptions=True) yields CrawlResult on the non-Exception
            # path; the union also admits BaseException, which the isinstance check
            # above deliberately does not catch (only Exception), so narrow here.
            out.append(cast("CrawlResult", r))

    # Structured-extraction pass (concurrent). Runs on each result's final
    # markdown so it covers the PDF, academic, and normal paths uniformly.
    if "json" in formats and (json_schema or extraction_prompt):
        from app.services.structured import extract_structured

        async def _extract(res: CrawlResult) -> None:
            if res.error or not res.markdown:
                return
            res.extracted = await extract_structured(
                res.markdown, json_schema, extraction_prompt
            )

        await asyncio.gather(*[_extract(r) for r in out])

    return out

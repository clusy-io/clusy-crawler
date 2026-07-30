from __future__ import annotations

import asyncio
import re
import time as time_module
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import urlparse

import structlog

from app.cache import get_cache, make_cache_key
from app.config import settings
from app.models.responses import (
    CACHE_POLICY_REVISION,
    CrawlResult,
    ExtractionMetadata,
)
from app.services import academic as academic_module
from app.services import fetcher as fetcher_module
from app.services import scholarly_metadata as scholarly_metadata_module
from app.services.document_policy import (
    DocumentPolicyBlockReason,
    DocumentPolicyDecision,
    DocumentPolicyDeniedError,
    enforce_document_policy,
)
from app.services.extractor import (
    PIPELINE_REVISION,
    ExtractionProfile,
    ExtractionResult,
    extract_content_async,
)
from app.services.frontier import (
    CrawlFrontier,
    FrontierConfig,
    StaleLeaseError,
    TerminalReason,
    UrlCanonicalizationError,
)
from app.services.github import (
    GitHubPageKind,
    classify_github_url,
    find_blob_raw_url,
    wrap_github_source,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.services.academic import AcademicPaper
    from app.services.document_policy import DocumentPolicyCallback
    from app.services.frontier import FrontierLease

logger = structlog.get_logger()

# Below this word count a "static" fetch is treated as essentially empty and we
# escalate to a JS render. Kept low so genuinely short-but-complete pages (e.g.
# example.com) are NOT needlessly rendered — true JS shells are caught earlier
# by needs_js_rendering()/bot-block detection, not by this floor.
SPARSE_WORD_FLOOR = 15

_crawl_semaphore: asyncio.Semaphore | None = None
_academic_parser_semaphore: asyncio.Semaphore | None = None
_academic_parser_loop: asyncio.AbstractEventLoop | None = None
_GITHUB_SESSION_ROOTS = frozenset(
    {
        "account",
        "login",
        "notifications",
        "sessions",
        "settings",
        "signup",
    }
)


@dataclass
class _Flight:
    task: asyncio.Task[CrawlResult]
    waiters: int = 0


_singleflight_tasks: dict[str, _Flight] = {}
_singleflight_lock: asyncio.Lock | None = None
_singleflight_loop: asyncio.AbstractEventLoop | None = None
_accepting_crawls = True


def _get_semaphore() -> asyncio.Semaphore:
    global _crawl_semaphore
    if _crawl_semaphore is None:
        _crawl_semaphore = asyncio.Semaphore(settings.max_concurrent_tasks)
    return _crawl_semaphore


def _get_academic_parser_semaphore() -> asyncio.Semaphore:
    global _academic_parser_semaphore, _academic_parser_loop
    loop = asyncio.get_running_loop()
    if _academic_parser_semaphore is None or _academic_parser_loop is not loop:
        _academic_parser_semaphore = asyncio.Semaphore(max(1, settings.max_concurrent_extractions))
        _academic_parser_loop = loop
    return _academic_parser_semaphore


def _get_singleflight_lock() -> asyncio.Lock:
    """Return a loop-local lock for the in-process duplicate-work registry."""
    global _singleflight_lock, _singleflight_loop, _singleflight_tasks
    loop = asyncio.get_running_loop()
    if _singleflight_lock is None or _singleflight_loop is not loop:
        _singleflight_lock = asyncio.Lock()
        _singleflight_loop = loop
        _singleflight_tasks = {}
    return _singleflight_lock


async def _read_cached_result(
    cache: Any,
    cache_key: str,
    max_age: int | None,
) -> CrawlResult | None:
    """Read and validate one cache envelope, treating corruption as a miss."""
    import orjson

    lookup_started = time_module.perf_counter()
    try:
        cached = await cache.get(cache_key)
    except Exception as e:
        logger.warning("cache_read_failed", error_type=type(e).__name__)
        return None
    if cached is None:
        return None

    try:
        envelope = orjson.loads(cached)
        cached_at = envelope["t"]
        age = time_module.time() - float(cached_at)
        if max_age is not None and age > max_age:
            return None
        result = CrawlResult(**envelope["r"])
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        logger.warning(
            "cache_entry_invalid",
            cache_key=cache_key,
            error_type=type(e).__name__,
        )
        return None

    result.cached = True
    if result.metadata is not None:
        lookup_ms = round(
            (time_module.perf_counter() - lookup_started) * 1000,
            3,
        )
        result.metadata.cache_status = "hit"
        result.metadata.cache_age_ms = round(max(0.0, age * 1000), 3)
        result.metadata.cache_lookup_ms = lookup_ms
        # Timings persisted with the source crawl describe a different
        # request. A cache hit reports only its own lookup wall time and never
        # presents the original fetch/render/extraction as current work.
        result.metadata.stage_timings_ms = {
            "queue": 0.0,
            "fetch": 0.0,
            "render": 0.0,
            "extraction": 0.0,
            "total": lookup_ms,
        }
    return result


def _apply_cache_policy_receipt(
    result: CrawlResult,
    *,
    cache_read_permitted: bool,
    cache_write_permitted: bool,
) -> CrawlResult:
    """Bind the response to the effective persistent-result cache policy."""
    metadata = result.metadata
    if metadata is None:
        return result
    metadata.cache_policy = "default" if cache_write_permitted else "no_store"
    metadata.cache_read_permitted = cache_read_permitted
    metadata.cache_write_permitted = cache_write_permitted
    metadata.cache_policy_revision = CACHE_POLICY_REVISION
    return result


async def _store_cached_result(cache: Any, cache_key: str, result: CrawlResult) -> None:
    """Persist the canonical cacheable projection of a successful crawl."""
    if result.error:
        return

    # Redis exposes a conservative, concurrency-safe readiness gate so the
    # disabled and failure-cooldown paths do not pay for a deep Pydantic copy
    # and JSON encoding that cannot be stored.  Test and embedding caches that
    # predate the gate retain their existing eager-set behavior.
    write_available = getattr(cache, "write_available", None)
    if write_available is not None:
        try:
            if not write_available():
                return
        except Exception as e:
            # Cache availability must never determine crawl success.  A broken
            # readiness implementation fails closed and never constructs the
            # cache projection.
            logger.warning(
                "cache_write_readiness_failed",
                error_type=type(e).__name__,
            )
            return

    import orjson

    cached_result = result.model_copy(deep=True)
    # HTML can be many megabytes. It remains available to concurrent
    # singleflight waiters but is intentionally excluded from Redis.
    cached_result.html = None
    cached_result.cached = False
    if cached_result.metadata is not None:
        # Request-local telemetry is reconstructed on every cache hit.
        cached_result.metadata.stage_timings_ms = {}
        cached_result.metadata.cache_status = "live"
        cached_result.metadata.cache_age_ms = None
        cached_result.metadata.cache_lookup_ms = None
    try:
        await cache.set(
            cache_key,
            orjson.dumps({"t": time_module.time(), "r": cached_result.model_dump()}),
        )
    except Exception as e:
        # Cache availability must never determine crawl success.
        logger.warning("cache_write_failed", error_type=type(e).__name__)


def _result_is_stable_for_cache(result: CrawlResult) -> bool:
    """Reject temporary quality fallbacks while caching stable V2 outputs."""
    metadata = result.metadata
    if metadata is None:
        return True
    assisted = (
        metadata.model_assisted
        or metadata.extraction_route == "quality_model"
        or metadata.extraction_strategy.startswith("mineru-")
    )
    if assisted:
        if not metadata.quality_succeeded:
            # Fail closed if an adapter returns a model strategy but forgets
            # the explicit V2 success provenance.
            return False
        from app.services.source_selection_receipt_v0 import (
            SOURCE_SELECTION_RECEIPT_V0_SCHEMA,
        )

        if not (
            metadata.source_selection_replay_verified
            and metadata.source_selection_schema
            == SOURCE_SELECTION_RECEIPT_V0_SCHEMA
            and len(metadata.source_selection_receipt_sha256) == 64
            and all(
                character in "0123456789abcdef"
                for character in metadata.source_selection_receipt_sha256
            )
            and metadata.source_selection_item_count >= 1
            and 1
            <= metadata.source_selection_selected_count
            <= metadata.source_selection_item_count
        ):
            # Model-assisted Markdown is cacheable only when it came from a
            # complete, independently replayed source-pointer selection.
            return False
        # Temperature-zero is insufficient identity by itself. Persist model
        # output only when the operator binds the exact immutable backend build;
        # the revision is also part of the cache key and health fingerprint.
        return bool(settings.quality_extraction_backend_revision.strip())
    return not (metadata.quality_attempted and not metadata.quality_succeeded)


def _specialist_route(metadata: ExtractionMetadata) -> tuple[str, list[str]]:
    """Return explicit provenance for non-general extraction paths."""
    strategy = metadata.extraction_strategy
    if strategy.startswith("academic-metadata-"):
        return (
            "scholarly_metadata_fallback",
            ["publisher_full_text_unavailable", "metadata_only"],
        )
    if strategy == "pypdfium2+academic":
        return ("academic_pdf", ["direct_pdf"])
    if strategy == "academic-html+pdf":
        return (
            "academic_pdf_fallback",
            ["academic_landing_detected", "pdf_full_text_selected"],
        )
    if strategy == "academic-html":
        return ("academic_html", ["academic_full_text_detected"])
    if strategy == "academic-landing":
        return ("academic_landing", ["full_text_unavailable"])
    if strategy.startswith("github-"):
        return ("github_source", ["github_source_specialist"])
    if metadata.content_scope == "source":
        return ("raw_source", ["non_html_source"])
    return ("specialist", [strategy or "specialist_output"])


def _finalize_live_success(
    result: CrawlResult,
    *,
    pipeline_started: float,
    queue_elapsed_ms: float,
    fetch_elapsed_ms: float,
    render_elapsed_ms: float,
) -> CrawlResult:
    """Attach uniform request-local provenance to every successful result."""
    metadata = result.metadata
    if result.error or metadata is None:
        return result

    metadata.pipeline_revision = metadata.pipeline_revision or PIPELINE_REVISION
    if not metadata.extraction_route:
        route, reasons = _specialist_route(metadata)
        metadata.extraction_route = route
        metadata.route_reasons = reasons
    if metadata.completeness_coverage == "unassessed":
        # Specialist extractors do not compare their output against an
        # independently assessed source corpus. Keep the numeric compatibility
        # score conservative and state that only output was observed.
        metadata.completeness_score = 0.0
        metadata.completeness_coverage = "output_only"
        metadata.source_coverage_score = None
        metadata.output_grounding_score = None
        if "source_completeness_unassessed" not in metadata.completeness_reasons:
            metadata.completeness_reasons.append(
                "source_completeness_unassessed"
            )

    total_elapsed_ms = max(
        0.0,
        (time_module.perf_counter() - pipeline_started) * 1000,
    )
    # The fetcher reports actual static/browser provenance. Clamp aggregate
    # durations to the enclosing request wall: a specialist may overlap
    # independent fetches, and summing their individual latency observations
    # must never claim more wall time than the request consumed. Proportional
    # scaling preserves the fetch/render split without preferring either lane.
    bounded_queue_ms = min(max(0.0, queue_elapsed_ms), total_elapsed_ms)
    available_work_ms = max(0.0, total_elapsed_ms - bounded_queue_ms)
    observed_fetch_ms = max(0.0, fetch_elapsed_ms)
    observed_render_ms = max(0.0, render_elapsed_ms)
    observed_io_ms = observed_fetch_ms + observed_render_ms
    if observed_io_ms > available_work_ms and observed_io_ms > 0:
        scale = available_work_ms / observed_io_ms
        bounded_fetch_ms = observed_fetch_ms * scale
        bounded_render_ms = observed_render_ms * scale
        extraction_elapsed_ms = 0.0
    else:
        bounded_fetch_ms = observed_fetch_ms
        bounded_render_ms = observed_render_ms
        extraction_elapsed_ms = available_work_ms - observed_io_ms
    metadata.stage_timings_ms = {
        "queue": round(bounded_queue_ms, 3),
        "fetch": round(bounded_fetch_ms, 3),
        "render": round(bounded_render_ms, 3),
        "extraction": round(extraction_elapsed_ms, 3),
        "total": round(total_elapsed_ms, 3),
    }
    metadata.cache_status = "live"
    metadata.cache_age_ms = None
    metadata.cache_lookup_ms = None
    return result


async def _crawl_single_url(
    url: str,
    js_render: bool | None = None,
    wait_for_selector: str | None = None,
    word_count_threshold: int = 10,
    formats: list[str] | None = None,
    max_age: int | None = None,
    extraction_profile: ExtractionProfile = "balanced",
    document_policy: DocumentPolicyCallback | None = None,
    store_in_cache: bool = True,
) -> CrawlResult:
    if not _accepting_crawls:
        return CrawlResult(url=url, error="crawler is shutting down")

    decide_js, auto_render = _resolve_js_policy(url, js_render)
    formats = formats or ["markdown"]

    cache_key = make_cache_key(
        url,
        decide_js,
        wait_for_selector,
        word_count_threshold=word_count_threshold,
        auto_render=auto_render,
        extraction_profile=extraction_profile,
    )
    # V2 caches deterministic adaptive fast paths and accepted temperature-zero
    # quality outputs under a fully versioned key. A fallback caused by a
    # temporary quality failure is filtered before storage below.
    #
    # A flat cache envelope binds only the terminal CrawlResult. It does not
    # carry the redirect chain or prove that every hop passed the recursive
    # scope and robots policy. Fail closed for policy-aware crawls: bypass both
    # cache reads and writes until a versioned envelope can authenticate that
    # complete provenance. Flat crawl cache behavior remains unchanged.
    cache_allowed = document_policy is None
    cache_read_permitted = cache_allowed and max_age != 0 and "html" not in formats
    cache_write_permitted = cache_allowed and store_in_cache
    cache = (
        get_cache()
        if cache_read_permitted or cache_write_permitted
        else None
    )
    # max_age == 0 bypasses the cache entirely (always re-crawl). "html" output
    # is never cached (too large), so those requests always take the live path.
    if cache_read_permitted:
        cached_result = await _read_cached_result(cache, cache_key, max_age)
        if cached_result is not None:
            effective_cached_url = (
                cached_result.metadata.source_url
                if cached_result.metadata is not None
                and cached_result.metadata.source_url
                else url
            )
            if document_policy is not None:
                await enforce_document_policy(document_policy, effective_cached_url)
            _apply_cache_policy_receipt(
                cached_result,
                cache_read_permitted=cache_read_permitted,
                cache_write_permitted=cache_write_permitted,
            )
            return _project_formats(cached_result, formats)

    # Coalesce simultaneous cache misses/refreshes for the same effective crawl.
    # Every waiter receives a deep copy so per-request format projection cannot
    # mutate the shared canonical result. Policy-aware work uses a per-callback
    # partition so it can never join a flat flight whose redirects/navigation
    # were created without the recursive scope and robots gate.
    flight_key = (
        f"{cache_key}:cache-policy:"
        f"r{int(cache_read_permitted)}:w{int(cache_write_permitted)}"
    )
    if document_policy is not None:
        flight_key = f"{flight_key}:document-policy:{id(document_policy)}"
    lock = _get_singleflight_lock()
    async with lock:
        flight = _singleflight_tasks.get(flight_key)
        if flight is None:
            task = asyncio.create_task(
                _crawl_uncached_and_cache(
                    cache_key=cache_key,
                    flight_key=flight_key,
                    cache=cache,
                    url=url,
                    decide_js=decide_js,
                    auto_render=auto_render,
                    wait_for_selector=wait_for_selector,
                    word_count_threshold=word_count_threshold,
                    extraction_profile=extraction_profile,
                    document_policy=document_policy,
                    cache_read_permitted=cache_read_permitted,
                    cache_write_permitted=cache_write_permitted,
                    lock=lock,
                )
            )
            flight = _Flight(task=task)
            _singleflight_tasks[flight_key] = flight
        flight.waiters += 1

    try:
        canonical = await asyncio.shield(flight.task)
        result = canonical.model_copy(deep=True)
        result.cached = False
        return _project_formats(result, formats)
    finally:
        await _release_flight_waiter(flight_key, flight, lock)


async def _release_flight_waiter(
    cache_key: str,
    flight: _Flight,
    lock: asyncio.Lock,
) -> None:
    task_to_drain: asyncio.Task[CrawlResult] | None = None
    async with lock:
        if _singleflight_tasks.get(cache_key) is not flight:
            return
        flight.waiters = max(0, flight.waiters - 1)
        if flight.waiters == 0 and not flight.task.done():
            # No client can observe or benefit from the work any longer.
            flight.task.cancel()
            task_to_drain = flight.task
    if task_to_drain is not None:
        # Cancellation must not strand a singleflight task after its last
        # observer goes away. The flight retires its registry entry in its own
        # finally block, so drain only after releasing the registry lock.
        await asyncio.gather(task_to_drain, return_exceptions=True)


async def _crawl_uncached_and_cache(
    *,
    cache_key: str,
    flight_key: str,
    cache: Any,
    url: str,
    decide_js: bool,
    auto_render: bool,
    wait_for_selector: str | None,
    word_count_threshold: int,
    extraction_profile: ExtractionProfile,
    document_policy: DocumentPolicyCallback | None,
    cache_read_permitted: bool,
    cache_write_permitted: bool,
    lock: asyncio.Lock,
) -> CrawlResult:
    """Run one canonical live crawl, cache it, and retire its flight entry."""
    try:
        result = await _crawl_uncached(
            url=url,
            decide_js=decide_js,
            auto_render=auto_render,
            wait_for_selector=wait_for_selector,
            word_count_threshold=word_count_threshold,
            extraction_profile=extraction_profile,
            document_policy=document_policy,
        )
        _apply_cache_policy_receipt(
            result,
            cache_read_permitted=cache_read_permitted,
            cache_write_permitted=cache_write_permitted,
        )
        if cache_write_permitted and _result_is_stable_for_cache(result):
            await _store_cached_result(cache, cache_key, result)
        return result
    finally:
        current = asyncio.current_task()
        async with lock:
            flight = _singleflight_tasks.get(flight_key)
            if flight is not None and flight.task is current:
                _singleflight_tasks.pop(flight_key, None)


async def _crawl_uncached(
    *,
    url: str,
    decide_js: bool,
    auto_render: bool,
    wait_for_selector: str | None,
    word_count_threshold: int,
    extraction_profile: ExtractionProfile,
    document_policy: DocumentPolicyCallback | None = None,
) -> CrawlResult:
    """Run the live crawl and return a canonical, format-complete result."""

    pipeline_started = time_module.perf_counter()
    fetch_elapsed_ms = 0.0
    render_elapsed_ms = 0.0

    async def run_extraction(
        html_content: str,
        target_url: str,
    ) -> ExtractionResult:
        return await extract_content_async(
            html_content,
            target_url,
            extraction_profile,
        )

    def record_fetch(result: fetcher_module.FetchResult) -> None:
        nonlocal fetch_elapsed_ms, render_elapsed_ms
        fetch_elapsed_ms += max(0.0, result.fetch_latency_ms)
        render_elapsed_ms += max(0.0, result.render_latency_ms)

    def finalize(result: CrawlResult) -> CrawlResult:
        return _finalize_live_success(
            result,
            pipeline_started=pipeline_started,
            queue_elapsed_ms=queue_elapsed_ms,
            fetch_elapsed_ms=fetch_elapsed_ms,
            render_elapsed_ms=render_elapsed_ms,
        )

    sem = _get_semaphore()
    async with sem:
        queue_elapsed_ms = (time_module.perf_counter() - pipeline_started) * 1000
        loop = asyncio.get_running_loop()

        if document_policy is None:
            fetch_result = await fetcher_module.fetch_url(
                url,
                js_render=decide_js,
                wait_for_selector=wait_for_selector,
            )
        else:
            fetch_result = await fetcher_module.fetch_url(
                url,
                js_render=decide_js,
                wait_for_selector=wait_for_selector,
                document_policy=document_policy,
            )
        record_fetch(fetch_result)

        if fetch_result.error:
            metadata_fallback = await _try_scholarly_metadata_fallback(
                requested_url=url,
                effective_url=fetch_result.final_url or url,
                trusted_title=fetch_result.title,
                rendered=fetch_result.rendered,
                origin_status_code=fetch_result.status_code,
                origin_error=fetch_result.error,
            )
            if metadata_fallback is not None:
                return finalize(metadata_fallback)
            return CrawlResult(url=url, error=fetch_result.error)

        effective_url = fetch_result.final_url or url
        if _is_github_session_page(effective_url):
            return CrawlResult(
                url=url,
                error=(
                    "GitHub account/session pages are not public repository "
                    "content and require unsupported authentication"
                ),
            )

        # Direct PDFs never pass through the HTML extractors.  Malformed,
        # encrypted, and scanned PDFs are reported as one result-level error
        # instead of escaping the task and cancelling the entire crawl batch.
        is_pdf = "application/pdf" in fetch_result.content_type.lower()
        if is_pdf and fetch_result.raw_bytes:
            paper = await _extract_pdf_safely(
                fetch_result.raw_bytes,
                effective_url,
                loop,
            )
            if paper is None:
                return CrawlResult(
                    url=url,
                    error="PDF was fetched but no machine-readable text could be extracted",
                )
            paper = await _enrich_direct_pdf_metadata(
                paper,
                effective_url,
                loop,
                document_policy=document_policy,
                record_fetch=record_fetch,
            )
            landing_url = getattr(paper, "canonical_url", "")
            return finalize(
                _academic_result(
                    requested_url=url,
                    paper=paper,
                    source_url=effective_url,
                    content_type=fetch_result.content_type,
                    status_code=fetch_result.status_code,
                    rendered=fetch_result.rendered,
                    strategy="pypdfium2+academic",
                    html=None,
                    links=(
                        [landing_url]
                        if landing_url and landing_url != effective_url
                        else []
                    ),
                )
            )

        # Direct raw files and GitHub blob pages bypass article extraction.
        # GitHub supplies the canonical Raw control, so refs containing slashes
        # never need to be guessed from the /blob/ path.
        github_source = await _try_github_source(
            requested_url=url,
            fetch_result=fetch_result,
            effective_url=effective_url,
            document_policy=document_policy,
            record_fetch=record_fetch,
        )
        if github_source is not None:
            return finalize(github_source)

        if not _is_html_content_type(fetch_result.content_type):
            return finalize(
                _source_result(
                    requested_url=url,
                    content=fetch_result.html,
                    source_url=effective_url,
                    content_type=fetch_result.content_type,
                    status_code=fetch_result.status_code,
                    rendered=fetch_result.rendered,
                    html=fetch_result.html,
                    strategy="source-text",
                )
            )

        # Parse trustworthy public landing-page metadata first.  PMC/JATS and
        # publisher full-text HTML are normally cleaner than PDF text and return
        # immediately.  Abstract-only records (notably arXiv /abs) retain their
        # metadata as a fallback while advertised PDF candidates are tried.
        academic_result = await _crawl_academic_html(
            requested_url=url,
            fetch_result=fetch_result,
            effective_url=effective_url,
            loop=loop,
            document_policy=document_policy,
            record_fetch=record_fetch,
        )
        if academic_result is not None:
            return finalize(academic_result)

        # Conditional JS rendering — extract ONCE, escalate only when needed.
        #
        # The old path ran a full synchronous extraction just to compute a word
        # count, then extracted AGAIN — double work on every page, on the event
        # loop. Here we check cheap regex signals first; if the static HTML is a
        # bot wall or SPA shell we skip extraction and render directly. Otherwise
        # we extract once and only re-render when the page yielded ~nothing.
        extraction = None
        rendered = fetch_result.rendered
        if auto_render and settings.playwright_enabled and settings.playwright_java_script_enabled:
            from app.services.renderer import needs_js_rendering

            escalate = False
            trigger = ""
            if _detect_bot_block(fetch_result.html):
                escalate, trigger = True, "bot_block"
            elif needs_js_rendering(fetch_result.html, effective_url):
                escalate, trigger = True, "js_detected"
            else:
                extraction = await run_extraction(
                    fetch_result.html,
                    effective_url,
                )
                if extraction.word_count < max(word_count_threshold, SPARSE_WORD_FLOOR):
                    escalate, trigger = True, "sparse"

            if escalate:
                logger.info(
                    "escalating_to_playwright",
                    host=_log_host(url),
                    trigger=trigger,
                )
                if document_policy is None:
                    pw_result = await fetcher_module.fetch_url(
                        url,
                        js_render=True,
                        wait_for_selector=wait_for_selector,
                    )
                else:
                    pw_result = await fetcher_module.fetch_url(
                        url,
                        js_render=True,
                        wait_for_selector=wait_for_selector,
                        document_policy=document_policy,
                    )
                record_fetch(pw_result)
                if not pw_result.error and pw_result.html:
                    pw_effective_url = pw_result.final_url or url
                    pw_extraction = await run_extraction(
                        pw_result.html,
                        pw_effective_url,
                    )
                    # Rendering can regress (interstitials, lazy content) — keep
                    # whichever extraction actually captured more content.
                    if extraction is None or pw_extraction.word_count >= extraction.word_count:
                        fetch_result = pw_result
                        extraction = pw_extraction
                        effective_url = pw_effective_url
                        rendered = pw_result.rendered

        if rendered:
            rendered_academic_result = await _crawl_academic_html(
                requested_url=url,
                fetch_result=fetch_result,
                effective_url=effective_url,
                loop=loop,
                document_policy=document_policy,
                record_fetch=record_fetch,
            )
            if rendered_academic_result is not None:
                return finalize(rendered_academic_result)

        if BOT_BLOCK_SIGNATURES.search(fetch_result.html[:3000]):
            metadata_fallback = await _try_scholarly_metadata_fallback(
                requested_url=url,
                effective_url=effective_url,
                trusted_title=fetch_result.title,
                trusted_html=fetch_result.html,
                rendered=rendered,
                origin_status_code=fetch_result.status_code,
                origin_error=("bot or client challenge blocked the publisher page"),
            )
            if metadata_fallback is not None:
                return finalize(metadata_fallback)
            return CrawlResult(
                url=url,
                error=f"bot or client challenge blocked content (HTTP "
                f"{fetch_result.status_code}{', rendered' if rendered else ''})",
            )

        if extraction is None:
            extraction = await run_extraction(
                fetch_result.html,
                effective_url,
            )

        # An empty extraction is a failed crawl, not a silent empty success —
        # surface it (e.g. a 401/403 bot wall that even the render couldn't pass)
        # so the caller knows the page was blocked rather than genuinely empty.
        if extraction.word_count <= SPARSE_WORD_FLOOR:
            metadata_fallback = await _try_scholarly_metadata_fallback(
                requested_url=url,
                effective_url=effective_url,
                trusted_title=fetch_result.title or extraction.title,
                trusted_html=fetch_result.html,
                rendered=rendered,
                origin_status_code=fetch_result.status_code,
                origin_error="publisher page returned only sparse content",
            )
            if metadata_fallback is not None:
                return finalize(metadata_fallback)

        if extraction.word_count == 0:
            return CrawlResult(
                url=url,
                error=f"no content extracted (HTTP {fetch_result.status_code}"
                f"{', rendered' if rendered else ''}) — page may be blocked",
            )

        metadata = ExtractionMetadata(
            title=extraction.title,
            description=extraction.description,
            language=extraction.language,
            source_url=effective_url,
            content_type=fetch_result.content_type,
            status_code=fetch_result.status_code,
            word_count=extraction.word_count,
            rendered=rendered,
            extraction_strategy=extraction.strategy,
            truncated=extraction.truncated,
            truncation_reason=extraction.truncation_reason,
            origin_status_code=fetch_result.status_code,
            pipeline_revision=extraction.pipeline_revision,
            extraction_route=extraction.route,
            route_reasons=list(extraction.route_reasons),
            model_assisted=extraction.model_assisted,
            quality_attempted=extraction.quality_attempted,
            quality_succeeded=extraction.quality_succeeded,
            candidate_count=extraction.candidate_count,
            candidate_disagreement=extraction.candidate_disagreement,
            completeness_score=extraction.completeness_score,
            completeness_coverage=extraction.completeness_coverage,
            source_coverage_score=extraction.source_coverage_score,
            output_grounding_score=extraction.output_grounding_score,
            completeness_reasons=list(extraction.completeness_reasons),
            source_selection_schema=extraction.source_selection_schema,
            source_selection_receipt_sha256=(
                extraction.source_selection_receipt_sha256
            ),
            source_selection_item_count=extraction.source_selection_item_count,
            source_selection_selected_count=(
                extraction.source_selection_selected_count
            ),
            source_selection_replay_verified=(
                extraction.source_selection_replay_verified
            ),
        )

        return finalize(
            CrawlResult(
                url=url,
                markdown=extraction.text,
                html=fetch_result.html,
                links=_extract_links(fetch_result.html, effective_url),
                metadata=metadata,
            )
        )


async def _crawl_academic_html(
    *,
    requested_url: str,
    fetch_result: fetcher_module.FetchResult,
    effective_url: str,
    loop: asyncio.AbstractEventLoop,
    document_policy: DocumentPolicyCallback | None = None,
    record_fetch: Callable[[fetcher_module.FetchResult], None] | None = None,
) -> CrawlResult | None:
    if not _is_academic_content(fetch_result.html, effective_url) or _detect_bot_block(
        fetch_result.html
    ):
        return None
    landing_paper = await _extract_academic_html_safely(
        fetch_result.html,
        effective_url,
        loop,
    )
    if landing_paper is None:
        return None

    landing_links = _extract_links(fetch_result.html, effective_url)
    if _academic_html_is_full_text(landing_paper, effective_url):
        return _academic_result(
            requested_url=requested_url,
            paper=landing_paper,
            source_url=effective_url,
            content_type=fetch_result.content_type,
            status_code=fetch_result.status_code,
            rendered=fetch_result.rendered,
            strategy="academic-html",
            html=fetch_result.html,
            links=landing_links,
            origin_status_code=fetch_result.status_code,
        )

    pdf_match = await _try_academic_pdf_candidates(
        html=fetch_result.html,
        landing_url=effective_url,
        landing_paper=landing_paper,
        loop=loop,
        document_policy=document_policy,
        record_fetch=record_fetch,
    )
    if pdf_match is not None:
        pdf_paper, pdf_url, pdf_status = pdf_match
        return _academic_result(
            requested_url=requested_url,
            paper=pdf_paper,
            source_url=landing_paper.canonical_url or effective_url,
            content_type="application/pdf",
            status_code=pdf_status,
            rendered=fetch_result.rendered,
            strategy="academic-html+pdf",
            html=fetch_result.html,
            links=_append_unique(landing_links, pdf_url),
            origin_status_code=fetch_result.status_code,
        )

    # A publisher may block its PDF while still exposing a useful
    # title/author/abstract record.  Return that record rather than losing it to
    # a generic extractor or surfacing a false failure.
    return _academic_result(
        requested_url=requested_url,
        paper=landing_paper,
        source_url=landing_paper.canonical_url or effective_url,
        content_type=fetch_result.content_type,
        status_code=fetch_result.status_code,
        rendered=fetch_result.rendered,
        strategy="academic-landing",
        html=fetch_result.html,
        links=landing_links,
        origin_status_code=fetch_result.status_code,
    )


async def _extract_pdf_safely(
    contents: bytes,
    source_url: str,
    loop: asyncio.AbstractEventLoop,
) -> AcademicPaper | None:
    try:
        paper = cast(
            "AcademicPaper",
            await _run_executor_holding_cancellation(
                loop,
                academic_module.extract_pdf,
                contents,
                source_url,
            ),
        )
    except Exception as exc:
        logger.warning(
            "academic_pdf_extraction_failed",
            host=_log_host(source_url),
            error_type=type(exc).__name__,
        )
        return None
    full_text = getattr(paper, "full_text", None)
    if getattr(paper, "word_count", 0) <= 0 or (full_text is not None and not full_text.strip()):
        logger.info("academic_pdf_has_no_text", host=_log_host(source_url))
        return None
    return paper


async def _extract_academic_html_safely(
    html: str,
    source_url: str,
    loop: asyncio.AbstractEventLoop,
) -> AcademicPaper | None:
    try:
        paper = cast(
            "AcademicPaper",
            await _run_executor_holding_cancellation(
                loop,
                academic_module.extract_long_html,
                html,
                source_url,
            ),
        )
    except Exception as exc:
        logger.warning(
            "academic_html_extraction_failed",
            host=_log_host(source_url),
            error_type=type(exc).__name__,
        )
        return None
    if len(paper.abstract.strip()) < 40 and paper.word_count < 30 and not paper.sections:
        return None
    return paper


def _academic_html_is_full_text(paper: AcademicPaper, url: str) -> bool:
    source = academic_module.classify_academic_url(url)
    lower_path = url.lower().split("?", 1)[0].split("#", 1)[0]
    if source == "pubmed" or (source == "arxiv" and "/abs/" in lower_path):
        return False
    if source == "pmc":
        return paper.word_count >= 200
    abstract_words = len(getattr(paper, "abstract", "").split())
    if source is None and paper.word_count >= max(300, abstract_words * 2):
        return True
    return paper.word_count >= 1000 or (paper.word_count >= 400 and len(paper.sections) >= 2)


def _merge_academic_papers(
    pdf_paper: AcademicPaper,
    landing_paper: AcademicPaper,
    pdf_url: str,
) -> AcademicPaper:
    # Highwire/JATS metadata is usually more reliable than heuristics over PDF
    # page one.  Keep the PDF body but prefer structured landing-page fields.
    for field_name in (
        "title",
        "authors",
        "abstract",
        "language",
        "doi",
        "journal",
        "publication_date",
        "license",
        "pmid",
        "pmcid",
        "arxiv_id",
        "canonical_url",
    ):
        landing_value = getattr(landing_paper, field_name)
        if landing_value:
            setattr(pdf_paper, field_name, landing_value)
    pdf_paper.pdf_url = pdf_url
    return pdf_paper


async def _enrich_direct_pdf_metadata(
    pdf_paper: AcademicPaper,
    pdf_url: str,
    loop: asyncio.AbstractEventLoop,
    *,
    document_policy: DocumentPolicyCallback | None = None,
    record_fetch: Callable[[fetcher_module.FetchResult], None] | None = None,
) -> AcademicPaper:
    """Merge authoritative arXiv landing metadata into a direct PDF result.

    Embedded PDF title/author fields are frequently producer names, copyright
    notices, or stale build metadata. arXiv's canonical ``/abs`` page exposes
    structured citation fields and is cheap to fetch, so prefer it while
    retaining the already-decoded PDF body. Failure is deliberately soft.
    """
    if academic_module.classify_academic_url(pdf_url) != "arxiv":
        return pdf_paper
    landing_url = academic_module.canonicalize_academic_url(pdf_url)
    if not landing_url or landing_url == pdf_url:
        return pdf_paper
    try:
        async with asyncio.timeout(settings.academic_pdf_fallback_timeout_s):
            if document_policy is None:
                landing_result = await fetcher_module.fetch_url(
                    landing_url,
                    js_render=False,
                )
            else:
                landing_result = await fetcher_module.fetch_url(
                    landing_url,
                    js_render=False,
                    document_policy=document_policy,
                )
    except (TimeoutError, DocumentPolicyDeniedError) as exc:
        logger.info(
            "academic_pdf_metadata_enrichment_skipped",
            host=_log_host(landing_url),
            reason=(
                "landing_policy_denied"
                if isinstance(exc, DocumentPolicyDeniedError)
                else "landing_timeout"
            ),
        )
        return pdf_paper
    if record_fetch is not None:
        record_fetch(landing_result)
    if (
        landing_result.error
        or not landing_result.html
        or not _is_html_content_type(landing_result.content_type)
        or _detect_bot_block(landing_result.html)
    ):
        logger.info(
            "academic_pdf_metadata_enrichment_skipped",
            host=_log_host(landing_url),
            reason="landing_fetch_failed",
        )
        return pdf_paper
    landing_paper = await _extract_academic_html_safely(
        landing_result.html,
        landing_result.final_url or landing_url,
        loop,
    )
    if landing_paper is None:
        logger.info(
            "academic_pdf_metadata_enrichment_skipped",
            host=_log_host(landing_url),
            reason="landing_parse_failed",
        )
        return pdf_paper
    return _merge_academic_papers(pdf_paper, landing_paper, pdf_url)


async def _try_academic_pdf_candidates(
    *,
    html: str,
    landing_url: str,
    landing_paper: AcademicPaper,
    loop: asyncio.AbstractEventLoop,
    document_policy: DocumentPolicyCallback | None = None,
    record_fetch: Callable[[fetcher_module.FetchResult], None] | None = None,
) -> tuple[AcademicPaper, str, int] | None:
    # Cap fallback fan-out: publisher pages sometimes advertise supplementary
    # PDFs alongside the article.  Candidate ordering puts citation_pdf_url and
    # typed <link> entries first, followed by explicit PDF anchors.
    candidates = academic_module.academic_pdf_candidates(html, landing_url)[:3]
    try:
        async with asyncio.timeout(settings.academic_pdf_fallback_timeout_s):
            for candidate in candidates:
                try:
                    if document_policy is None:
                        pdf_result = await fetcher_module.fetch_url(
                            candidate,
                            js_render=False,
                        )
                    else:
                        pdf_result = await fetcher_module.fetch_url(
                            candidate,
                            js_render=False,
                            document_policy=document_policy,
                        )
                except DocumentPolicyDeniedError:
                    # The landing page is already a usable extraction. A
                    # same-site or robots denial on an optional PDF candidate
                    # must degrade to it, not fail the leased root document.
                    logger.info(
                        "academic_pdf_candidate_skipped",
                        host=_log_host(candidate),
                        reason="document_policy_denied",
                    )
                    continue
                if record_fetch is not None:
                    record_fetch(pdf_result)
                if (
                    pdf_result.error
                    or not pdf_result.raw_bytes
                    or "application/pdf" not in pdf_result.content_type.lower()
                ):
                    logger.info(
                        "academic_pdf_candidate_skipped",
                        host=_log_host(candidate),
                        reason="fetch_failed" if pdf_result.error else "not_pdf",
                    )
                    continue
                resolved_url = pdf_result.final_url or candidate
                pdf_paper = await _extract_pdf_safely(
                    pdf_result.raw_bytes,
                    resolved_url,
                    loop,
                )
                if pdf_paper is None:
                    continue
                return (
                    _merge_academic_papers(pdf_paper, landing_paper, resolved_url),
                    resolved_url,
                    pdf_result.status_code,
                )
    except TimeoutError:
        logger.info(
            "academic_pdf_fallback_timeout",
            host=_log_host(landing_url),
            candidates=len(candidates),
            timeout_s=settings.academic_pdf_fallback_timeout_s,
        )
        return None
    return None


def _append_unique(links: list[str], url: str) -> list[str]:
    return links if url in links else [*links, url]


async def _try_github_source(
    *,
    requested_url: str,
    fetch_result: fetcher_module.FetchResult,
    effective_url: str,
    document_policy: DocumentPolicyCallback | None = None,
    record_fetch: Callable[[fetcher_module.FetchResult], None] | None = None,
) -> CrawlResult | None:
    github_url = classify_github_url(effective_url)
    if github_url is None:
        return None

    source_result = fetch_result
    source_url = effective_url
    raw_link = ""
    if github_url.kind == GitHubPageKind.BLOB and _is_html_content_type(fetch_result.content_type):
        raw_link = find_blob_raw_url(fetch_result.html, effective_url) or ""
        if not raw_link:
            return None
        try:
            if document_policy is None:
                candidate = await fetcher_module.fetch_url(raw_link, js_render=False)
            else:
                candidate = await fetcher_module.fetch_url(
                    raw_link,
                    js_render=False,
                    document_policy=document_policy,
                )
        except DocumentPolicyDeniedError:
            # ``raw.githubusercontent.com`` is commonly outside an exact-host
            # recursive scope. Keep the already-fetched GitHub HTML available
            # to the normal extractor instead of terminating the whole page.
            logger.info(
                "github_raw_fallback",
                host=_log_host(raw_link),
                reason="document_policy_denied",
            )
            return None
        if record_fetch is not None:
            record_fetch(candidate)
        if (
            candidate.error
            or not candidate.html
            or "application/pdf" in candidate.content_type.lower()
        ):
            logger.info(
                "github_raw_fallback",
                host=_log_host(raw_link),
                reason="fetch_failed" if candidate.error else "unsupported_type",
            )
            return None
        source_result = candidate
        source_url = candidate.final_url or raw_link
    elif github_url.kind != GitHubPageKind.RAW:
        return None

    wrapped = wrap_github_source(
        source_result.html,
        source_url,
        source_result.content_type,
    )
    if wrapped is None:
        return None
    uncapped_markdown = wrapped.text
    markdown = _cap_text(uncapped_markdown)
    cap_truncated = markdown != uncapped_markdown
    extractor_truncated = getattr(wrapped, "truncated", False)
    truncation_reason = getattr(wrapped, "truncation_reason", "")
    if cap_truncated:
        truncation_reason = _join_truncation_reasons(
            truncation_reason,
            "configured text limit",
        )
    return CrawlResult(
        url=requested_url,
        markdown=markdown,
        html=source_result.html,
        links=[raw_link] if raw_link else [],
        metadata=ExtractionMetadata(
            title=wrapped.title,
            language=wrapped.language,
            source_url=source_url,
            content_type=source_result.content_type,
            status_code=source_result.status_code,
            word_count=_count_output_words(markdown),
            rendered=False,
            extraction_strategy=wrapped.strategy,
            content_scope="source",
            truncated=cap_truncated or extractor_truncated,
            truncation_reason=truncation_reason,
            origin_status_code=source_result.status_code,
        ),
    )


def _is_html_content_type(content_type: str) -> bool:
    return content_type.partition(";")[0].strip().lower() in {
        "text/html",
        "application/xhtml+xml",
    }


def _is_github_session_page(url: str) -> bool:
    github_url = classify_github_url(url)
    if github_url is None or github_url.kind != GitHubPageKind.ROOT:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    segments = [segment.casefold() for segment in parsed.path.split("/") if segment]
    return bool(segments and segments[0] in _GITHUB_SESSION_ROOTS)


def _source_result(
    *,
    requested_url: str,
    content: str,
    source_url: str,
    content_type: str,
    status_code: int,
    rendered: bool,
    html: str | None,
    strategy: str,
) -> CrawlResult:
    wrapped = wrap_github_source(content, source_url, content_type)
    if wrapped is None:
        return CrawlResult(url=requested_url, error="text source is empty")
    uncapped_markdown = wrapped.text
    markdown = _cap_text(uncapped_markdown)
    cap_truncated = markdown != uncapped_markdown
    extractor_truncated = getattr(wrapped, "truncated", False)
    truncation_reason = getattr(wrapped, "truncation_reason", "")
    if cap_truncated:
        truncation_reason = _join_truncation_reasons(
            truncation_reason,
            "configured text limit",
        )
    return CrawlResult(
        url=requested_url,
        markdown=markdown,
        html=html,
        links=[],
        metadata=ExtractionMetadata(
            title=wrapped.title,
            language=wrapped.language,
            source_url=source_url,
            content_type=content_type,
            status_code=status_code,
            word_count=_count_output_words(markdown),
            rendered=rendered,
            extraction_strategy=strategy,
            content_scope="source",
            truncated=cap_truncated or extractor_truncated,
            truncation_reason=truncation_reason,
            origin_status_code=status_code,
        ),
    )


def _academic_result(
    *,
    requested_url: str,
    paper: AcademicPaper,
    source_url: str,
    content_type: str,
    status_code: int,
    rendered: bool,
    strategy: str,
    html: str | None,
    links: list[str],
    origin_status_code: int | None = None,
    origin_error: str = "",
) -> CrawlResult:
    canonical_url = getattr(
        paper,
        "canonical_url",
        "",
    ) or academic_module.canonicalize_academic_url(source_url)
    abstract = getattr(paper, "abstract", "")
    uncapped_markdown = paper.to_markdown()
    markdown = _cap_text(uncapped_markdown)
    cap_truncated = markdown != uncapped_markdown
    paper_truncated = getattr(paper, "truncated", False)
    truncation_reason = getattr(paper, "truncation_reason", "")
    if cap_truncated:
        truncation_reason = _join_truncation_reasons(
            truncation_reason,
            "configured text limit",
        )
    content_scope: Literal["full_text", "landing", "metadata_only"]
    if strategy.startswith("academic-metadata-"):
        content_scope = "metadata_only"
    elif strategy == "academic-landing":
        content_scope = "landing"
    else:
        content_scope = "full_text"
    return CrawlResult(
        url=requested_url,
        markdown=markdown,
        html=html,
        links=links,
        metadata=ExtractionMetadata(
            title=paper.title,
            description=abstract[:500] if abstract else "",
            language=getattr(paper, "language", ""),
            source_url=source_url,
            content_type=content_type,
            status_code=status_code,
            word_count=_count_output_words(markdown),
            rendered=rendered,
            extraction_strategy=strategy,
            authors=getattr(paper, "authors", []),
            doi=getattr(paper, "doi", ""),
            pmid=getattr(paper, "pmid", ""),
            pmcid=getattr(paper, "pmcid", ""),
            arxiv_id=getattr(paper, "arxiv_id", ""),
            journal=getattr(paper, "journal", ""),
            published_at=getattr(paper, "publication_date", ""),
            canonical_url=canonical_url,
            license=getattr(paper, "license", ""),
            content_scope=content_scope,
            truncated=cap_truncated or paper_truncated,
            truncation_reason=truncation_reason,
            origin_status_code=(status_code if origin_status_code is None else origin_status_code),
            origin_error=origin_error,
        ),
    )


async def _try_scholarly_metadata_fallback(
    *,
    requested_url: str,
    effective_url: str,
    trusted_title: str,
    trusted_html: str = "",
    rendered: bool,
    origin_status_code: int,
    origin_error: str,
) -> CrawlResult | None:
    """Try metadata APIs only for a strictly recognized publisher identifier."""
    trusted_doi = academic_module.extract_academic_doi(
        effective_url,
        trusted_html,
    )
    if not trusted_doi and effective_url != requested_url:
        trusted_doi = academic_module.extract_academic_doi(
            requested_url,
            trusted_html,
        )
    lookup_url = requested_url
    if (
        scholarly_metadata_module.classify_publisher_target(
            lookup_url,
            trusted_doi=trusted_doi,
        )
        is None
    ):
        lookup_url = effective_url
    if (
        scholarly_metadata_module.classify_publisher_target(
            lookup_url,
            trusted_doi=trusted_doi,
        )
        is None
    ):
        return None

    lookup = await scholarly_metadata_module.lookup_publisher_metadata(
        lookup_url,
        # Static titles can be attacker-controlled or generic. Crossref title
        # search is an IEEE-only last resort, so expose a title only after a
        # successful isolated-browser render.
        trusted_title=trusted_title if rendered else "",
        trusted_doi=trusted_doi,
    )
    if lookup is None:
        return None

    canonical_url = lookup.paper.canonical_url or academic_module.canonicalize_academic_url(
        lookup_url
    )
    links = [canonical_url] if canonical_url else []
    result = _academic_result(
        requested_url=requested_url,
        paper=lookup.paper,
        source_url=canonical_url or lookup_url,
        content_type="application/json",
        status_code=200,
        rendered=rendered,
        strategy=lookup.strategy,
        html=None,
        links=links,
        origin_status_code=origin_status_code,
        origin_error=origin_error,
    )
    notice = (
        "> **Metadata-only record:** the publisher page and article full text "
        "were not retrieved.\n\n"
    )
    uncapped_markdown = notice + result.markdown
    result.markdown = _cap_text(uncapped_markdown)
    if result.metadata is not None:
        result.metadata.word_count = _count_output_words(result.markdown)
        if result.markdown != uncapped_markdown:
            _mark_truncated(result.metadata, "configured text limit")
    return result


# Domains that ALWAYS need JS rendering — bot walls, SPAs, etc.
FORCE_JS_DOMAINS = re.compile(
    r"acm\.org|springer\.com|ieee\.org|sciencedirect\.com|"
    r"nature\.com/articles|cell\.com|nejm\.org|thelancet\.com|"
    r"medium\.com|substack\.com",
    re.IGNORECASE,
)


def _resolve_js_policy(url: str, requested: bool | None) -> tuple[bool, bool]:
    """Resolve explicit request intent and the configured automatic policy.

    The optional API field is meaningful: either explicit boolean always wins.
    Only an omitted value consults ``JS_RENDER_MODE`` and known dynamic domains.
    The second return value enables conditional post-fetch escalation.
    """
    if requested is not None:
        return requested, False

    if settings.js_render_mode == "force":
        return True, False
    if settings.js_render_mode == "never":
        return False, False
    if FORCE_JS_DOMAINS.search(url):
        logger.info(
            "force_js_rendering",
            host=_log_host(url),
            reason="known_dynamic_domain",
        )
        return True, False
    return False, True


BOT_BLOCK_SIGNATURES = re.compile(
    r"just a moment|checking your browser|cf-browser-verification|"
    r"enable javascript|please enable cookies|attention required|"
    r"captcha|ddos-guard|_cf_chl_opt|cloudflare|"
    r"access denied|request blocked|security check|"
    r"browser verification|human verification|client challenge",
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
    source = academic_module.classify_academic_url(url)
    if source in {"arxiv", "pubmed", "pmc", "doi"}:
        return True
    if source == "journal" and "xplGlobal.document.metadata" in html:
        return True
    lower = html[:100000].lower()
    meta_tags = re.findall(r"<meta\b[^>]*>", lower)
    has_citation_title = any("citation_title" in tag for tag in meta_tags)
    has_scholarly_metadata = any(
        any(
            key in tag
            for key in (
                "citation_author",
                "citation_doi",
                "citation_journal_title",
                "citation_pdf_url",
            )
        )
        for tag in meta_tags
    )
    if has_citation_title and has_scholarly_metadata:
        return True
    if re.search(
        r"""["']@type["']\s*:\s*["'](?:scholarlyarticle|medicalscholarlyarticle)["']""",
        lower,
    ):
        return True
    return bool(re.search(r"\\begin\{|\\cite\{|\\ref\{|\\usepackage", lower))


def _extract_links(html: str, base_url: str) -> list[str]:
    """Collect de-duplicated absolute http(s) links from the page."""
    from urllib.parse import urljoin, urlparse

    seen: set[str] = set()
    out: list[str] = []
    output_chars = 0
    for m in re.finditer(r'<a\b[^>]*\bhref=["\']([^"\'#]+)["\']', html, re.IGNORECASE):
        href = m.group(1).strip()
        if (
            not href
            or len(href) > 4096
            or href.startswith(("javascript:", "mailto:", "tel:", "data:"))
        ):
            continue
        absolute = urljoin(base_url, href)
        if urlparse(absolute).scheme not in ("http", "https"):
            continue
        if absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
            output_chars += len(absolute)
            if output_chars > 256_000:
                out.pop()
                break
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


@dataclass
class _LiveRecursiveTask:
    lease: FrontierLease
    task: asyncio.Task[_RecursivePageOutcome]


@dataclass
class _RecursivePageOutcome:
    result: CrawlResult
    terminal_reason: TerminalReason | None = None


@dataclass
class _RecursiveDocumentPolicy:
    """Scope and robots gate shared by every document fetch for one lease."""

    frontier: CrawlFrontier

    async def __call__(self, url: str) -> DocumentPolicyDecision:
        if not self.frontier.is_in_scope(url):
            return DocumentPolicyDecision(
                allowed=False,
                reason=DocumentPolicyBlockReason.OFF_SITE,
                error="recursive document redirect leaves the configured crawl scope",
            )

        # Import lazily so flat max_depth=0 work never constructs the robots
        # service or executes recursive policy code.
        from app.services.robots import get_robots_policy, robots_blocked_error

        try:
            robots_decision = await get_robots_policy().check(url)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "recursive_robots_policy_failed",
                error_type=type(exc).__name__,
            )
            return DocumentPolicyDecision(
                allowed=False,
                reason=DocumentPolicyBlockReason.ROBOTS_DISALLOWED,
                error=(
                    "robots.txt policy check failed; recursive crawling "
                    "is denied by policy"
                ),
            )
        if not robots_decision.allowed:
            return DocumentPolicyDecision(
                allowed=False,
                reason=DocumentPolicyBlockReason.ROBOTS_DISALLOWED,
                error=robots_blocked_error(robots_decision),
            )
        return DocumentPolicyDecision(allowed=True)


def _log_recursive_frontier_metrics(
    frontier: CrawlFrontier,
    *,
    outcome: Literal["success", "cancelled", "error"],
    error_type: str = "",
) -> None:
    """Emit one bounded, URL-free recursive-job frontier snapshot."""

    metrics = frontier.metrics()
    logger.info(
        "recursive_crawl_frontier_finished",
        outcome=outcome,
        error_type=error_type,
        admitted=metrics.admitted,
        rejected=metrics.rejected,
        duplicates=metrics.duplicates,
        claimed=metrics.claimed,
        retries_scheduled=metrics.retries_scheduled,
        pending=metrics.pending,
        in_flight=metrics.in_flight,
        terminal=metrics.terminal,
        rejection_reasons={
            reason.value: count for reason, count in metrics.rejection_reasons.items() if count > 0
        },
        terminal_reasons={
            reason.value: count for reason, count in metrics.terminal_reasons.items() if count > 0
        },
        robots_disallowed=metrics.terminal_reasons.get(
            TerminalReason.ROBOTS_DISALLOWED,
            0,
        ),
    )


class _RecursiveCrawlJob:
    """Bounded owner for one opt-in recursive crawl.

    The owner checks the loop-local robots policy before every leased page
    fetch, retains links long enough to discover children, and retires results
    in claim order so completion timing cannot reorder the response or the next
    discovery wave. Completed network tasks release capacity even while an
    older claim is still running; those completed tasks form a bounded reorder
    buffer. The historical flat path never constructs this owner.
    """

    def __init__(
        self,
        *,
        urls: list[str],
        max_depth: int,
        allow_subdomains: bool,
        max_pages: int,
        priority: int,
        js_render: bool | None,
        wait_for_selector: str | None,
        word_count_threshold: int,
        extraction_profile: ExtractionProfile,
        formats: list[str],
        max_age: int | None,
        store_in_cache: bool,
    ) -> None:
        self._priority = priority
        self._js_render = js_render
        self._wait_for_selector = wait_for_selector
        self._word_count_threshold = word_count_threshold
        self._extraction_profile = extraction_profile
        self._formats = formats
        self._internal_formats = list(formats)
        if "links" not in self._internal_formats:
            self._internal_formats.append("links")
        self._max_age = max_age
        self._store_in_cache = store_in_cache
        self._concurrency = max(
            1,
            min(max_pages, settings.max_concurrent_tasks),
        )
        self._frontier = CrawlFrontier(
            urls,
            config=FrontierConfig(
                max_depth=max_depth,
                max_urls=max_pages,
                max_urls_per_host=max_pages,
                max_fetch_attempts=max_pages,
                max_fetch_attempts_per_host=max_pages,
                max_attempts_per_url=1,
                allow_subdomains=allow_subdomains,
                # _crawl_single_url already applies the shared per-domain rate
                # limiter. Keep frontier delay at zero to avoid double-throttling.
                host_delay_s=0,
            ),
            seed_priority=priority,
        )
        self._live: dict[int, _LiveRecursiveTask] = {}
        self._claim_order = 0
        self._results: list[CrawlResult] = []

    @property
    def frontier(self) -> CrawlFrontier:
        """Expose state for diagnostics and focused owner tests."""
        return self._frontier

    async def run(self) -> list[CrawlResult]:
        try:
            while True:
                self._fill_capacity()
                if not self._live:
                    _log_recursive_frontier_metrics(
                        self._frontier,
                        outcome="success",
                    )
                    return self._results
                await self._retire_ready_prefix()
                self._fill_capacity()
                if not self._live:
                    continue
                running = [
                    item.task for item in self._live.values() if not item.task.done()
                ]
                if running:
                    # Wake on any completion rather than awaiting the oldest
                    # claim. A fast page can immediately free a network slot
                    # while its response waits for ordered retirement.
                    await asyncio.wait(
                        running,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
        except BaseException as exc:
            await self._cancel_and_drain()
            _log_recursive_frontier_metrics(
                self._frontier,
                outcome=("cancelled" if isinstance(exc, asyncio.CancelledError) else "error"),
                error_type=type(exc).__name__,
            )
            raise

    def _fill_capacity(self) -> None:
        loop = asyncio.get_running_loop()
        active = sum(not item.task.done() for item in self._live.values())
        while active < self._concurrency:
            lease = self._frontier.claim(now=loop.time())
            if lease is None:
                return
            order = self._claim_order
            self._claim_order += 1
            task = asyncio.create_task(
                self._crawl_leased_url(lease),
                name=f"recursive-crawl-page-{order}",
            )
            self._live[order] = _LiveRecursiveTask(lease=lease, task=task)
            active += 1

    async def _crawl_leased_url(
        self,
        lease: FrontierLease,
    ) -> _RecursivePageOutcome:
        document_policy = _RecursiveDocumentPolicy(self._frontier)
        try:
            await enforce_document_policy(document_policy, lease.url)
            cache_options: dict[str, bool] = {}
            if not self._store_in_cache:
                # Preserve the historical call surface for embedders and test
                # doubles unless the caller opted into the new policy.
                cache_options["store_in_cache"] = False
            result = await _crawl_single_url(
                url=lease.url,
                js_render=self._js_render,
                wait_for_selector=self._wait_for_selector,
                word_count_threshold=self._word_count_threshold,
                formats=self._internal_formats,
                max_age=self._max_age,
                extraction_profile=self._extraction_profile,
                document_policy=document_policy,
                **cache_options,
            )
        except asyncio.CancelledError:
            raise
        except DocumentPolicyDeniedError as exc:
            terminal_reason = (
                TerminalReason.ROBOTS_DISALLOWED
                if exc.decision.reason is DocumentPolicyBlockReason.ROBOTS_DISALLOWED
                else TerminalReason.CONTENT_REJECTED
            )
            return _RecursivePageOutcome(
                result=CrawlResult(
                    url=lease.url,
                    error=str(exc),
                ),
                terminal_reason=terminal_reason,
            )
        return _RecursivePageOutcome(result=result)

    async def _retire_ready_prefix(self) -> None:
        """Commit the contiguous completed prefix in deterministic order."""
        while self._live:
            order = min(self._live)
            live = self._live[order]
            if not live.task.done():
                return
            await self._retire_completed(order, live)

    async def _retire_completed(
        self,
        order: int,
        live: _LiveRecursiveTask,
    ) -> None:
        terminal_reason: TerminalReason | None
        try:
            outcome = await live.task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "recursive_crawl_task_failed",
                host=_log_host(live.lease.url),
                error_type=type(exc).__name__,
            )
            result = CrawlResult(
                url=live.lease.url,
                error=f"crawl failed ({type(exc).__name__})",
            )
            terminal_reason = TerminalReason.PERMANENT_FAILURE
        else:
            result = outcome.result
            terminal_reason = outcome.terminal_reason

        now = asyncio.get_running_loop().time()
        if terminal_reason is not None:
            self._frontier.fail(
                live.lease,
                now=now,
                retryable=False,
                terminal_reason=terminal_reason,
            )
        elif result.error:
            self._frontier.fail(
                live.lease,
                now=now,
                retryable=False,
                terminal_reason=TerminalReason.PERMANENT_FAILURE,
            )
        else:
            self._frontier.succeed(live.lease)
            await self._admit_discovered_links(result, live.lease)

        self._results.append(_project_formats(result, self._formats))
        self._live.pop(order)

    async def _admit_discovered_links(
        self,
        result: CrawlResult,
        lease: FrontierLease,
    ) -> None:
        now = asyncio.get_running_loop().time()
        for index, link in enumerate(result.links or ()):
            self._frontier.admit(
                link,
                depth=lease.depth + 1,
                priority=self._priority,
                parent_url=lease.url,
                ready_at=now,
            )
            # Real extraction caps links at 1,000, but yielding also keeps the
            # owner responsive if a test double or future adapter returns more.
            if index % 128 == 127:
                await asyncio.sleep(0)

    async def _cancel_and_drain(self) -> None:
        live = list(self._live.values())
        for item in live:
            item.task.cancel()
        if live:
            await asyncio.gather(
                *(item.task for item in live),
                return_exceptions=True,
            )

        now = asyncio.get_running_loop().time()
        for item in live:
            # A task can complete between cancellation and draining. Its lease
            # may already have been terminalized by ordered retirement.
            with suppress(StaleLeaseError):
                self._frontier.fail(
                    item.lease,
                    now=now,
                    retryable=False,
                    terminal_reason=TerminalReason.CANCELLED,
                )
        self._live.clear()
        self._frontier.cancel_pending()


async def _crawl_urls_recursive(
    *,
    urls: list[str],
    max_depth: int,
    allow_subdomains: bool,
    max_pages: int,
    priority: int,
    js_render: bool | None,
    wait_for_selector: str | None,
    word_count_threshold: int,
    extraction_profile: ExtractionProfile,
    formats: list[str],
    max_age: int | None,
    store_in_cache: bool,
) -> list[CrawlResult]:
    if max_pages < len(urls):
        raise ValueError("max_pages must be at least the number of seed URLs")
    try:
        job = _RecursiveCrawlJob(
            urls=urls,
            max_depth=max_depth,
            allow_subdomains=allow_subdomains,
            max_pages=max_pages,
            priority=priority,
            js_render=js_render,
            wait_for_selector=wait_for_selector,
            word_count_threshold=word_count_threshold,
            extraction_profile=extraction_profile,
            formats=formats,
            max_age=max_age,
            store_in_cache=store_in_cache,
        )
    except (UrlCanonicalizationError, ValueError):
        return [
            CrawlResult(
                url=url,
                error="recursive crawl seed rejected by URL policy",
            )
            for url in urls
        ]
    return await job.run()


async def crawl_urls(
    urls: list[str],
    js_render: bool | None = None,
    wait_for_selector: str | None = None,
    word_count_threshold: int = 10,
    extraction_profile: ExtractionProfile = "balanced",
    formats: list[str] | None = None,
    max_age: int | None = None,
    store_in_cache: bool = True,
    json_schema: dict[str, Any] | None = None,
    extraction_prompt: str | None = None,
    max_depth: int = 0,
    allow_subdomains: bool = False,
    max_pages: int = settings.default_max_pages,
    priority: int = 10,
) -> list[CrawlResult]:
    formats = formats or ["markdown"]
    if max_depth > 0:
        out = await _crawl_urls_recursive(
            urls=urls,
            max_depth=max_depth,
            allow_subdomains=allow_subdomains,
            max_pages=max_pages,
            priority=priority,
            js_render=js_render,
            wait_for_selector=wait_for_selector,
            word_count_threshold=word_count_threshold,
            extraction_profile=extraction_profile,
            formats=formats,
            max_age=max_age,
            store_in_cache=store_in_cache,
        )
    else:
        tasks = [
            _crawl_single_url(
                u,
                js_render,
                wait_for_selector,
                word_count_threshold,
                formats,
                max_age,
                extraction_profile,
                store_in_cache=store_in_cache,
            )
            for u in urls
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        out = []
        for i, r in enumerate(results):
            if isinstance(r, asyncio.CancelledError):
                raise r
            if isinstance(r, Exception):
                logger.warning(
                    "crawl_task_failed",
                    host=_log_host(urls[i]),
                    error_type=type(r).__name__,
                )
                out.append(
                    CrawlResult(
                        url=urls[i],
                        error=f"crawl failed ({type(r).__name__})",
                    )
                )
            else:
                # gather(return_exceptions=True) yields CrawlResult on the
                # non-Exception path; the union also admits BaseException,
                # which the isinstance check above deliberately does not catch.
                out.append(cast("CrawlResult", r))

    # Structured-extraction pass (concurrent). Runs on each result's final
    # markdown so it covers the PDF, academic, and normal paths uniformly.
    if "json" in formats and (json_schema or extraction_prompt):
        from app.services.structured import extract_structured

        async def _extract(res: CrawlResult) -> None:
            if res.error or not res.markdown:
                return
            res.extracted = await extract_structured(res.markdown, json_schema, extraction_prompt)

        await asyncio.gather(*[_extract(r) for r in out])

    _apply_response_budget(out)
    return out


async def _run_executor_holding_cancellation(
    loop: asyncio.AbstractEventLoop,
    function: Any,
    *args: Any,
) -> Any:
    """Return cancellation promptly while a live parser retains CPU capacity."""
    semaphore = _get_academic_parser_semaphore()
    await semaphore.acquire()
    try:
        worker = loop.run_in_executor(None, function, *args)
    except BaseException:
        semaphore.release()
        raise

    def release_when_worker_finishes(done: asyncio.Future[Any]) -> None:
        if not done.cancelled():
            done.exception()
        semaphore.release()

    worker.add_done_callback(release_when_worker_finishes)
    return await asyncio.shield(worker)


def _cap_text(text: str) -> str:
    limit = settings.extract_max_text_length
    if len(text) <= limit:
        return text
    suffix = "\n\n[content truncated at configured limit]"
    if limit <= len(suffix):
        return suffix[:limit]
    content_limit = max(0, limit - len(suffix))
    boundary = max(
        text.rfind("\n", 0, content_limit),
        text.rfind(" ", 0, content_limit),
    )
    if boundary < content_limit // 2:
        boundary = content_limit
    return text[:boundary].rstrip() + suffix


def _join_truncation_reasons(current: str, added: str) -> str:
    reasons = [part.strip() for part in current.split(";") if part.strip()]
    if added and added not in reasons:
        reasons.append(added)
    return "; ".join(reasons)[:500]


def _mark_truncated(metadata: ExtractionMetadata, reason: str) -> None:
    metadata.truncated = True
    metadata.truncation_reason = _join_truncation_reasons(
        metadata.truncation_reason,
        reason,
    )


def _count_output_words(text: str) -> int:
    return len(re.findall(r"\b\w+\b|[\u3400-\u9fff]", text, re.UNICODE))


def _log_host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "(invalid)").lower()
    except ValueError:
        return "(invalid)"


def _apply_response_budget(results: list[CrawlResult]) -> None:
    import orjson

    for result in results:
        uncapped_markdown = result.markdown
        result.markdown = _cap_text(uncapped_markdown)
        if result.metadata is not None and result.markdown != uncapped_markdown:
            _mark_truncated(result.metadata, "configured text limit")

    def serialized_size() -> int:
        # Mirror the complete /crawl response, including metadata, errors,
        # escaped JSON strings, result envelopes, and a worst-case configured
        # request duration. This is intentionally conservative for /md and
        # /html, whose response models contain fewer fields.
        payload = {
            "status": "ok",
            "results": [result.model_dump(mode="json") for result in results],
            "total_time_ms": settings.crawl_request_timeout_s * 1000,
            "total_pages": len(results),
        }
        return len(orjson.dumps(payload))

    limit = settings.max_response_output_bytes
    if serialized_size() <= limit:
        return

    for result in reversed(results):
        result.markdown = ""
        result.html = None
        result.links = None
        result.extracted = None
        result.metadata = None
        result.cached = False
        result.error = "response output budget exceeded"
        if serialized_size() <= limit:
            return

    # A request URL may itself be several KiB. If retaining all fifty original
    # URLs would exceed the response limit, preserve result cardinality and the
    # explicit error while dropping only those echoed inputs.
    for result in reversed(results):
        result.url = ""
        if serialized_size() <= limit:
            return


def start_crawler() -> None:
    global _accepting_crawls
    _accepting_crawls = True


async def shutdown_crawler() -> None:
    """Stop admission, cancel active singleflights, and drain their cleanup."""
    global _accepting_crawls
    _accepting_crawls = False
    lock = _get_singleflight_lock()
    async with lock:
        tasks = [flight.task for flight in _singleflight_tasks.values()]
        for task in tasks:
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

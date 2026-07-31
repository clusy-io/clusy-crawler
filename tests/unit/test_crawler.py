from __future__ import annotations

import asyncio
import threading

import orjson
import pytest

from app.config import settings
from app.models.responses import CrawlResult, ExtractionMetadata
from app.services.crawler import (
    _apply_response_budget,
    _crawl_single_url,
    _get_semaphore,
    _resolve_js_policy,
    _run_executor_holding_cancellation,
    crawl_urls,
)
from app.services.document_policy import (
    DocumentPolicyBlockReason,
    DocumentPolicyDecision,
    DocumentPolicyDeniedError,
)
from app.services.extractor import ExtractionResult


class TestCrawler:
    @pytest.mark.anyio
    async def test_cancelled_academic_parser_returns_but_retains_cpu_permit(
        self,
        monkeypatch,
    ):
        from app.services import crawler as crawler_module

        started = threading.Event()
        release = threading.Event()
        semaphore = asyncio.Semaphore(1)

        def worker():
            started.set()
            release.wait(timeout=1)
            return "done"

        monkeypatch.setattr(crawler_module, "_academic_parser_semaphore", semaphore)
        monkeypatch.setattr(
            crawler_module,
            "_academic_parser_loop",
            asyncio.get_running_loop(),
        )
        task = asyncio.create_task(
            _run_executor_holding_cancellation(
                asyncio.get_running_loop(),
                worker,
            )
        )
        assert await asyncio.to_thread(started.wait, 0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.1)
        assert semaphore.locked()
        release.set()
        await asyncio.sleep(0.02)
        assert not semaphore.locked()

    @pytest.mark.anyio
    async def test_crawl_urls_propagates_cancelled_work(self, monkeypatch):
        from app.services import crawler as crawler_module

        async def cancelled(*_args, **_kwargs):
            raise asyncio.CancelledError

        monkeypatch.setattr(crawler_module, "_crawl_single_url", cancelled)

        with pytest.raises(asyncio.CancelledError):
            await crawl_urls(["https://example.com"])

    def test_response_budget_counts_json_escaping_metadata_and_envelope(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(settings, "max_response_output_bytes", 1024)
        result = CrawlResult(
            url="https://example.com/private",
            markdown="\\" * 900,
            metadata=ExtractionMetadata(title="x" * 5000),
        )

        results = [result]
        _apply_response_budget(results)
        encoded = orjson.dumps(
            {
                "status": "ok",
                "results": [item.model_dump(mode="json") for item in results],
                "total_time_ms": settings.crawl_request_timeout_s * 1000,
                "total_pages": 1,
            }
        )

        assert len(encoded) <= settings.max_response_output_bytes
        assert result.error == "response output budget exceeded"
        assert result.metadata is None

    def test_configured_text_cap_is_explicit_in_metadata(self, monkeypatch):
        monkeypatch.setattr(settings, "extract_max_text_length", 96)
        monkeypatch.setattr(settings, "max_response_output_bytes", 64 * 1024)
        result = CrawlResult(
            url="https://example.com/large",
            markdown=("bounded paragraph content " * 20).strip(),
            metadata=ExtractionMetadata(word_count=60),
        )

        results = [result]
        _apply_response_budget(results)

        assert len(result.markdown) <= settings.extract_max_text_length
        assert result.markdown.endswith("[content truncated at configured limit]")
        assert result.metadata is not None
        assert result.metadata.truncated is True
        assert result.metadata.truncation_reason == "configured text limit"

    @pytest.mark.parametrize(
        ("mode", "requested", "url", "expected"),
        [
            ("force", False, "https://medium.com/post", (False, False)),
            ("never", True, "https://example.com/", (True, False)),
            ("force", None, "https://example.com/", (True, False)),
            ("never", None, "https://medium.com/post", (False, False)),
            ("conditional", None, "https://medium.com/post", (True, False)),
            ("conditional", None, "https://example.com/", (False, True)),
        ],
    )
    def test_js_policy_precedence(
        self,
        monkeypatch,
        mode,
        requested,
        url,
        expected,
    ):
        monkeypatch.setattr(settings, "js_render_mode", mode)
        assert _resolve_js_policy(url, requested) == expected

    @pytest.mark.anyio
    async def test_crawl_single_error_on_bad_url(self):
        result = await _crawl_single_url("http://127.0.0.1/forbidden")
        assert result.error is not None

    @pytest.mark.anyio
    async def test_crawl_multiple_urls(self, monkeypatch):
        from app.services import fetcher

        fake_html = (
            "<html><head><title>Test</title></head>"
            "<body><p>Hello world test content here for extraction.</p></body></html>"
        )

        async def mock_fetch(url, js_render=False, wait_for_selector=None):
            from app.services.fetcher import FetchResult

            return FetchResult(
                html=fake_html,
                status_code=200,
                content_type="text/html",
                title="Test Page",
            )

        monkeypatch.setattr(fetcher, "fetch_url", mock_fetch)
        monkeypatch.setattr(settings, "js_render_mode", "never")

        results = await crawl_urls(["https://example.com", "https://example.org"])
        assert len(results) == 2
        for r in results:
            assert r.error is None
            assert r.markdown
            assert r.metadata is not None
            assert r.metadata.origin_status_code == 200

    @pytest.mark.anyio
    async def test_crawl_exception_returned_as_error(self, monkeypatch):
        from app.services import fetcher

        async def mock_fetch(url, js_render=False, wait_for_selector=None):
            raise RuntimeError("boom")

        monkeypatch.setattr(fetcher, "fetch_url", mock_fetch)

        results = await crawl_urls(["https://example.com"])
        assert len(results) == 1
        assert results[0].error is not None
        assert results[0].error == "crawl failed (RuntimeError)"

    @pytest.mark.anyio
    async def test_final_url_and_actual_render_state_propagate(self, monkeypatch):
        from app.services import fetcher

        async def mock_fetch(url, js_render=False, wait_for_selector=None):
            from app.services.fetcher import FetchResult

            assert js_render is True
            return FetchResult(
                html='<html><body><a href="source">source</a></body></html>',
                status_code=200,
                content_type="text/html",
                final_url="https://canonical.example/articles/one/",
                # Simulate browser failure followed by a successful static
                # fallback: request intent must not be reported as rendering.
                rendered=False,
            )

        async def mock_extract(html, url, extraction_profile="balanced"):
            assert url == "https://canonical.example/articles/one/"
            assert extraction_profile == "article_body"
            return ExtractionResult(
                text="final content from the canonical page",
                word_count=7,
                strategy="test",
            )

        monkeypatch.setattr(fetcher, "fetch_url", mock_fetch)
        monkeypatch.setattr(
            "app.services.crawler.extract_content_async",
            mock_extract,
        )

        result = await _crawl_single_url(
            "https://example.com/redirect",
            js_render=True,
            formats=["markdown", "links"],
            max_age=0,
            extraction_profile="article_body",
        )

        assert result.error is None
        assert result.metadata is not None
        assert result.metadata.source_url == "https://canonical.example/articles/one/"
        assert result.metadata.rendered is False
        assert result.metadata.origin_status_code == 200
        assert result.links == ["https://canonical.example/articles/one/source"]

    @pytest.mark.anyio
    async def test_direct_arxiv_pdf_prefers_abs_page_metadata(self, monkeypatch):
        from app.services import crawler as crawler_module
        from app.services.academic import AcademicPaper, Section
        from app.services.fetcher import FetchResult

        pdf_url = "https://arxiv.org/pdf/1706.03762"
        abs_url = "https://arxiv.org/abs/1706.03762"
        calls: list[str] = []

        async def fetch(url, js_render=False, wait_for_selector=None):
            calls.append(url)
            await asyncio.sleep(0.002)
            if url == pdf_url:
                return FetchResult(
                    raw_bytes=b"%PDF-test",
                    status_code=200,
                    content_type="application/pdf",
                    final_url=pdf_url,
                    fetch_latency_ms=1.25,
                )
            assert url == abs_url
            return FetchResult(
                html=(
                    "<html><body><article>"
                    + ("structured landing metadata " * 30)
                    + "</article></body></html>"
                ),
                status_code=200,
                content_type="text/html",
                final_url=abs_url,
            )

        def extract_pdf(_contents, _url):
            return AcademicPaper(
                title="Google PDF Producer Notice",
                authors=["Google Brain"],
                full_text="decoded PDF body",
                sections=[Section(heading="Body", content="decoded PDF body")],
                word_count=3,
                arxiv_id="1706.03762",
                canonical_url=abs_url,
            )

        async def parse_landing(_html, _url, _loop):
            return AcademicPaper(
                title="Attention Is All You Need",
                authors=["Ashish Vaswani", "Noam Shazeer"],
                publication_date="2017-06-12",
                arxiv_id="1706.03762",
                canonical_url=abs_url,
            )

        monkeypatch.setattr(crawler_module.fetcher_module, "fetch_url", fetch)
        monkeypatch.setattr(crawler_module.academic_module, "extract_pdf", extract_pdf)
        monkeypatch.setattr(
            crawler_module,
            "_extract_academic_html_safely",
            parse_landing,
        )
        monkeypatch.setattr(crawler_module, "_crawl_semaphore", None)

        result = await crawler_module._crawl_uncached(
            url=pdf_url,
            decide_js=False,
            auto_render=False,
            wait_for_selector=None,
            word_count_threshold=10,
            extraction_profile="balanced",
        )

        assert calls == [pdf_url, abs_url]
        assert result.error is None
        assert result.metadata is not None
        assert result.metadata.title == "Attention Is All You Need"
        assert result.metadata.authors == ["Ashish Vaswani", "Noam Shazeer"]
        assert result.metadata.published_at == "2017-06-12"
        assert result.metadata.extraction_strategy == "pypdfium2+academic"
        assert result.metadata.origin_status_code == 200
        assert result.metadata.pipeline_revision == "clusy-extraction-v2"
        assert result.metadata.extraction_route == "academic_pdf"
        assert result.metadata.route_reasons == ["direct_pdf"]
        assert result.metadata.completeness_score == 0.0
        assert result.metadata.completeness_coverage == "output_only"
        assert result.metadata.source_coverage_score is None
        assert result.metadata.output_grounding_score is None
        assert result.metadata.cache_status == "live"
        assert set(result.metadata.stage_timings_ms) == {
            "queue",
            "fetch",
            "render",
            "extraction",
            "total",
        }
        assert result.metadata.stage_timings_ms["fetch"] == 1.25
        assert result.links == [abs_url]
        assert "decoded PDF body" in result.markdown

    @pytest.mark.anyio
    async def test_github_login_page_is_an_honest_unsupported_result(
        self,
        monkeypatch,
    ):
        from app.services import crawler as crawler_module
        from app.services.fetcher import FetchResult

        async def fetch(url, js_render=False, wait_for_selector=None):
            return FetchResult(
                html="<html><body>Sign in to GitHub</body></html>",
                status_code=200,
                content_type="text/html",
                final_url=url,
            )

        async def must_not_extract(*_args, **_kwargs):
            raise AssertionError("account chrome must not enter content extraction")

        monkeypatch.setattr(crawler_module.fetcher_module, "fetch_url", fetch)
        monkeypatch.setattr(crawler_module, "extract_content_async", must_not_extract)
        monkeypatch.setattr(crawler_module, "_crawl_semaphore", None)

        result = await crawler_module._crawl_uncached(
            url="https://github.com/login",
            decide_js=False,
            auto_render=True,
            wait_for_selector=None,
            word_count_threshold=10,
            extraction_profile="balanced",
        )

        assert result.markdown == ""
        assert result.metadata is None
        assert result.error is not None
        assert "unsupported authentication" in result.error

    @pytest.mark.anyio
    async def test_structured_html_doi_reaches_exact_metadata_fallback(
        self,
        monkeypatch,
    ):
        from app.services import crawler as crawler_module
        from app.services.academic import AcademicPaper
        from app.services.scholarly_metadata import ScholarlyMetadataResult

        doi = "10.1002/example.12345"
        captured: dict[str, str] = {}

        async def lookup(url, *, trusted_title="", trusted_doi=""):
            captured["url"] = url
            captured["trusted_title"] = trusted_title
            captured["trusted_doi"] = trusted_doi
            return ScholarlyMetadataResult(
                AcademicPaper(
                    title="Exact structured DOI record",
                    doi=doi,
                    canonical_url=f"https://doi.org/{doi}",
                ),
                "academic-metadata-crossref",
            )

        monkeypatch.setattr(
            crawler_module.scholarly_metadata_module,
            "lookup_publisher_metadata",
            lookup,
        )

        result = await crawler_module._try_scholarly_metadata_fallback(
            requested_url="https://onlinelibrary.wiley.com/article/opaque-id",
            effective_url="https://onlinelibrary.wiley.com/article/opaque-id",
            trusted_title="",
            trusted_html=(
                "<html><head>"
                f'<meta name="citation_doi" content="{doi}">'
                "</head></html>"
            ),
            rendered=False,
            origin_status_code=403,
            origin_error="HTTP 403",
        )

        assert result is not None
        assert captured["trusted_doi"] == doi
        assert result.metadata is not None
        assert result.metadata.doi == doi
        assert result.metadata.content_scope == "metadata_only"
        assert result.metadata.origin_status_code == 403

    def test_semaphore_caps_concurrency(self):
        sem = _get_semaphore()
        assert sem._value == settings.max_concurrent_tasks


@pytest.mark.anyio
@pytest.mark.parametrize("max_age", [None, 0])
async def test_recursive_policy_bypasses_flat_cache_for_enabled_and_disabled_modes(
    monkeypatch,
    max_age,
):
    from app.services import crawler as crawler_module

    requested = "https://example.com/start"
    cached_final = "https://outside.example/final"
    cached = CrawlResult(
        url=requested,
        markdown="cached content",
        metadata=ExtractionMetadata(source_url=cached_final),
    )
    cache_calls: list[str] = []

    class FakeCache:
        async def get(self, _key):
            cache_calls.append("get")
            return orjson.dumps(
                {
                    "t": 9_999_999_999,
                    "r": cached.model_dump(),
                }
            )

        async def set(self, _key, _value):
            cache_calls.append("set")

    policy_calls: list[str] = []

    async def document_policy(url):
        policy_calls.append(url)
        return DocumentPolicyDecision(allowed=True)

    live_calls: list[str] = []

    async def live_crawl(**kwargs):
        live_calls.append(kwargs["url"])
        decision = await kwargs["document_policy"](kwargs["url"])
        assert decision.allowed
        return CrawlResult(
            url=kwargs["url"],
            markdown="live content",
            metadata=ExtractionMetadata(source_url=kwargs["url"]),
        )

    monkeypatch.setattr(crawler_module, "get_cache", lambda: FakeCache())
    monkeypatch.setattr(crawler_module, "_crawl_uncached", live_crawl)

    result = await crawler_module._crawl_single_url(
        requested,
        max_age=max_age,
        document_policy=document_policy,
    )

    assert result.markdown == "live content"
    assert result.cached is False
    assert live_calls == [requested]
    assert policy_calls == [requested]
    assert cache_calls == []


@pytest.mark.anyio
async def test_recursive_policy_requests_keep_singleflight_while_bypassing_cache(
    monkeypatch,
):
    from app.services import crawler as crawler_module

    url = "https://recursive-singleflight.example/page"
    started = asyncio.Event()
    release = asyncio.Event()
    uncached_calls = 0

    def cache_must_not_be_resolved():
        raise AssertionError("policy-aware crawl must not resolve the flat cache")

    async def fake_uncached(**kwargs):
        nonlocal uncached_calls
        uncached_calls += 1
        assert kwargs["document_policy"] is document_policy
        started.set()
        await release.wait()
        return CrawlResult(url=url, markdown="live content")

    async def document_policy(_url):
        return DocumentPolicyDecision(allowed=True)

    monkeypatch.setattr(crawler_module, "get_cache", cache_must_not_be_resolved)
    monkeypatch.setattr(crawler_module, "_crawl_uncached", fake_uncached)
    monkeypatch.setattr(crawler_module, "_singleflight_tasks", {})
    monkeypatch.setattr(crawler_module, "_singleflight_lock", None)
    monkeypatch.setattr(crawler_module, "_singleflight_loop", None)

    first = asyncio.create_task(
        crawler_module._crawl_single_url(
            url,
            document_policy=document_policy,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    second = asyncio.create_task(
        crawler_module._crawl_single_url(
            url,
            document_policy=document_policy,
        )
    )
    await asyncio.sleep(0)
    release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert uncached_calls == 1
    assert first_result.markdown == second_result.markdown == "live content"
    assert first_result is not second_result
    assert first_result.cached is second_result.cached is False


@pytest.mark.anyio
async def test_recursive_policy_flight_never_joins_concurrent_flat_flight(monkeypatch):
    from app.services import crawler as crawler_module

    url = "https://flight-partition.example/page"
    both_started = asyncio.Event()
    release = asyncio.Event()
    policy_modes: list[bool] = []

    class FakeCache:
        async def get(self, _key):
            return None

        async def set(self, _key, _value):
            pass

    async def fake_uncached(**kwargs):
        policy_modes.append(kwargs["document_policy"] is not None)
        if len(policy_modes) == 2:
            both_started.set()
        await release.wait()
        return CrawlResult(url=url, markdown="content")

    async def document_policy(_url):
        return DocumentPolicyDecision(allowed=True)

    monkeypatch.setattr(crawler_module, "get_cache", lambda: FakeCache())
    monkeypatch.setattr(crawler_module, "_crawl_uncached", fake_uncached)

    flat = asyncio.create_task(
        crawler_module._crawl_single_url(
            url,
            max_age=0,
        )
    )
    await asyncio.sleep(0)
    recursive = asyncio.create_task(
        crawler_module._crawl_single_url(
            url,
            max_age=0,
            document_policy=document_policy,
        )
    )
    await asyncio.wait_for(both_started.wait(), timeout=1)
    release.set()
    await asyncio.gather(flat, recursive)

    assert sorted(policy_modes) == [False, True]


@pytest.mark.anyio
@pytest.mark.parametrize("max_age", [None, 0])
async def test_unconfigured_redis_skips_cache_projection_and_serialization(
    monkeypatch,
    max_age,
):
    import redis.asyncio as aioredis

    from app.cache import RedisCache
    from app.services import crawler as crawler_module

    url = "https://cache-disabled.example/page"
    cache = RedisCache()

    async def fake_uncached(**kwargs):
        return CrawlResult(
            url=kwargs["url"],
            markdown="live content",
            metadata=ExtractionMetadata(source_url=kwargs["url"]),
        )

    def serialization_must_not_run(*_args, **_kwargs):
        raise AssertionError("disabled Redis must not serialize a cache value")

    def connection_must_not_run(*_args, **_kwargs):
        raise AssertionError("disabled Redis must not construct a client")

    monkeypatch.setattr(settings, "redis_url", "")
    monkeypatch.setattr(crawler_module, "get_cache", lambda: cache)
    monkeypatch.setattr(crawler_module, "_crawl_uncached", fake_uncached)
    monkeypatch.setattr(orjson, "dumps", serialization_must_not_run)
    monkeypatch.setattr(aioredis, "from_url", connection_must_not_run)

    result = await crawler_module._crawl_single_url(url, max_age=max_age)

    assert result.markdown == "live content"
    assert result.cached is False


@pytest.mark.anyio
async def test_available_cache_preserves_projection_and_write(monkeypatch):
    from app.services import crawler as crawler_module

    result = CrawlResult(
        url="https://cache-enabled.example/page",
        markdown="live content",
        html="<html>not persisted</html>",
        metadata=ExtractionMetadata(
            source_url="https://cache-enabled.example/page",
            stage_timings_ms={"total": 12.5},
        ),
    )
    gated_stored: list[bytes] = []
    legacy_stored: list[bytes] = []

    class AvailableCache:
        def write_available(self):
            return True

        async def set(self, _key, value):
            gated_stored.append(value)

    class LegacyCache:
        async def set(self, _key, value):
            legacy_stored.append(value)

    monkeypatch.setattr(crawler_module.time_module, "time", lambda: 123.5)
    await crawler_module._store_cached_result(LegacyCache(), "key", result)
    await crawler_module._store_cached_result(AvailableCache(), "key", result)

    assert len(legacy_stored) == len(gated_stored) == 1
    assert gated_stored[0] == legacy_stored[0]
    envelope = orjson.loads(gated_stored[0])
    cached = envelope["r"]
    assert cached["markdown"] == "live content"
    assert cached["html"] is None
    assert cached["cached"] is False
    assert cached["metadata"]["stage_timings_ms"] == {}
    assert cached["metadata"]["cache_status"] == "live"
    assert result.html == "<html>not persisted</html>"
    assert result.metadata is not None
    assert result.metadata.stage_timings_ms == {"total": 12.5}


@pytest.mark.anyio
@pytest.mark.parametrize("readiness_mode", ["unavailable", "error"])
async def test_unavailable_cache_readiness_fails_closed_before_projection(
    monkeypatch,
    readiness_mode,
):
    from app.services import crawler as crawler_module

    class UnavailableCache:
        def write_available(self):
            if readiness_mode == "error":
                raise OSError("readiness failed")
            return False

        async def set(self, _key, _value):
            raise AssertionError("unavailable cache must not receive a value")

    def projection_must_not_run(self, *, deep=False, update=None):
        raise AssertionError("unavailable cache must not project a cache value")

    monkeypatch.setattr(CrawlResult, "model_copy", projection_must_not_run)

    await crawler_module._store_cached_result(
        UnavailableCache(),
        "key",
        CrawlResult(url="https://cache-unavailable.example/page", markdown="live"),
    )


@pytest.mark.anyio
async def test_cache_write_cancellation_is_not_swallowed():
    from app.services import crawler as crawler_module

    class CancellingCache:
        def write_available(self):
            return True

        async def set(self, _key, _value):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await crawler_module._store_cached_result(
            CancellingCache(),
            "key",
            CrawlResult(url="https://cache-cancel.example/page", markdown="live"),
        )


@pytest.mark.anyio
async def test_optional_github_raw_policy_denial_falls_back_to_fetched_html(
    monkeypatch,
):
    from app.services import crawler as crawler_module
    from app.services.fetcher import FetchResult

    blob_url = "https://github.com/acme/repo/blob/main/src/example.py"
    raw_url = "https://raw.githubusercontent.com/acme/repo/main/src/example.py"
    fetched_blob = FetchResult(
        html=(
            "<html><body>"
            f'<a data-testid="raw-button" href="{raw_url}">Raw</a>'
            "<article>usable server-rendered blob page</article>"
            "</body></html>"
        ),
        status_code=200,
        content_type="text/html",
        final_url=blob_url,
    )

    async def denied_fetch(*_args, **_kwargs):
        raise DocumentPolicyDeniedError(
            DocumentPolicyDecision(
                allowed=False,
                reason=DocumentPolicyBlockReason.OFF_SITE,
                error="raw host outside recursive scope",
            )
        )

    async def allow(_url):
        return DocumentPolicyDecision(allowed=True)

    monkeypatch.setattr(crawler_module.fetcher_module, "fetch_url", denied_fetch)

    result = await crawler_module._try_github_source(
        requested_url=blob_url,
        fetch_result=fetched_blob,
        effective_url=blob_url,
        document_policy=allow,
    )

    assert result is None


@pytest.mark.anyio
async def test_optional_academic_pdf_policy_denial_keeps_landing_fallback(
    monkeypatch,
):
    from app.services import crawler as crawler_module
    from app.services.academic import AcademicPaper

    landing_url = "https://papers.example/article/one"
    pdf_url = "https://cdn.example/article/one.pdf"

    async def denied_fetch(*_args, **_kwargs):
        raise DocumentPolicyDeniedError(
            DocumentPolicyDecision(
                allowed=False,
                reason=DocumentPolicyBlockReason.OFF_SITE,
                error="PDF host outside recursive scope",
            )
        )

    async def allow(_url):
        return DocumentPolicyDecision(allowed=True)

    monkeypatch.setattr(
        crawler_module.academic_module,
        "academic_pdf_candidates",
        lambda _html, _url: [pdf_url],
    )
    monkeypatch.setattr(crawler_module.fetcher_module, "fetch_url", denied_fetch)

    result = await crawler_module._try_academic_pdf_candidates(
        html="<html><body>landing</body></html>",
        landing_url=landing_url,
        landing_paper=AcademicPaper(
            title="Useful landing record",
            abstract="A useful abstract remains available after policy denial.",
        ),
        loop=asyncio.get_running_loop(),
        document_policy=allow,
    )

    assert result is None


# V2 withholds failed attempts and unversioned model output from Redis.
def test_quality_cacheability_requires_success_and_backend_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import crawler as crawler_module

    fallback = CrawlResult(
        url="https://example.test",
        markdown="deterministic fallback",
        metadata=ExtractionMetadata(
            model_assisted=True,
            quality_attempted=True,
            quality_succeeded=False,
        ),
    )
    accepted = fallback.model_copy(deep=True)
    assert accepted.metadata is not None
    accepted.metadata.quality_succeeded = True

    assert crawler_module._result_is_stable_for_cache(fallback) is False
    monkeypatch.setattr(settings, "quality_extraction_backend_revision", "")
    assert crawler_module._result_is_stable_for_cache(accepted) is False
    monkeypatch.setattr(
        settings,
        "quality_extraction_backend_revision",
        "model-build@sha256:abc123",
    )
    assert crawler_module._result_is_stable_for_cache(accepted) is False
    accepted.metadata.source_selection_schema = "quality-source-selection.v0"
    accepted.metadata.source_selection_item_count = 2
    accepted.metadata.source_selection_selected_count = 1
    accepted.metadata.source_selection_replay_verified = True
    accepted.metadata.source_selection_receipt_sha256 = "g" * 64
    assert crawler_module._result_is_stable_for_cache(accepted) is False
    accepted.metadata.source_selection_receipt_sha256 = "a" * 64
    assert crawler_module._result_is_stable_for_cache(accepted) is True


def test_completeness_score_preserves_numeric_response_schema() -> None:
    metadata = ExtractionMetadata()
    schema = ExtractionMetadata.model_json_schema()

    assert metadata.completeness_score == 0.0
    assert isinstance(metadata.completeness_score, float)
    assert schema["properties"]["completeness_score"]["type"] == "number"


@pytest.mark.anyio
async def test_forced_playwright_wall_time_is_attributed_to_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import crawler as crawler_module
    from app.services.fetcher import FetchResult

    observed_js_render: list[bool] = []

    async def rendered_fetch(
        _url: str,
        js_render: bool = False,
        wait_for_selector: str | None = None,
    ) -> FetchResult:
        del wait_for_selector
        observed_js_render.append(js_render)
        await asyncio.sleep(0.002)
        return FetchResult(
            html="<html><body><main>Rendered source</main></body></html>",
            status_code=200,
            content_type="text/html",
            rendered=True,
            render_latency_ms=2.0,
        )

    async def extract(*_args: object, **_kwargs: object) -> ExtractionResult:
        text = " ".join(["rendered"] * 30)
        return ExtractionResult(
            text=text,
            word_count=30,
            strategy="trafilatura",
            completeness_score=1.0,
            completeness_coverage="source_full",
        )

    monkeypatch.setattr(crawler_module.fetcher_module, "fetch_url", rendered_fetch)
    monkeypatch.setattr(crawler_module, "extract_content_async", extract)
    monkeypatch.setattr(crawler_module, "_crawl_semaphore", None)

    result = await crawler_module._crawl_uncached(
        url="https://example.test/rendered",
        decide_js=True,
        auto_render=False,
        wait_for_selector=None,
        word_count_threshold=10,
        extraction_profile="balanced",
    )

    assert observed_js_render == [True]
    assert result.metadata is not None
    timings = result.metadata.stage_timings_ms
    assert timings["fetch"] == 0
    assert timings["render"] > 0
    assert timings["total"] >= timings["render"]


@pytest.mark.anyio
async def test_forced_js_disabled_is_attributed_to_fetch_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import crawler as crawler_module
    from app.services.fetcher import FetchResult

    async def static_fetch(
        _url: str,
        js_render: bool = False,
        wait_for_selector: str | None = None,
    ) -> FetchResult:
        del wait_for_selector
        assert js_render is True
        await asyncio.sleep(0.002)
        return FetchResult(
            html="<html><body><main>Static fallback source</main></body></html>",
            status_code=200,
            content_type="text/html",
            rendered=False,
            fetch_latency_ms=1.5,
        )

    async def extract(*_args: object, **_kwargs: object) -> ExtractionResult:
        return ExtractionResult(
            text=" ".join(["static"] * 30),
            word_count=30,
            strategy="trafilatura",
        )

    monkeypatch.setattr(crawler_module.fetcher_module, "fetch_url", static_fetch)
    monkeypatch.setattr(crawler_module, "extract_content_async", extract)
    monkeypatch.setattr(crawler_module, "_crawl_semaphore", None)

    result = await crawler_module._crawl_uncached(
        url="https://example.test/static-fallback",
        decide_js=True,
        auto_render=False,
        wait_for_selector=None,
        word_count_threshold=10,
        extraction_profile="balanced",
    )

    assert result.metadata is not None
    timings = result.metadata.stage_timings_ms
    assert timings["fetch"] == 1.5
    assert timings["render"] == 0
    assert result.metadata.rendered is False


@pytest.mark.anyio
async def test_conditional_browser_static_fallback_accumulates_each_stage_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import crawler as crawler_module
    from app.services.fetcher import FetchResult

    calls: list[bool] = []

    async def fetch(
        _url: str,
        js_render: bool = False,
        wait_for_selector: str | None = None,
    ) -> FetchResult:
        del wait_for_selector
        calls.append(js_render)
        if not js_render:
            await asyncio.sleep(0.002)
            return FetchResult(
                html="<html><body>shell</body></html>",
                status_code=200,
                content_type="text/html",
                fetch_latency_ms=1.0,
            )
        # Browser navigation failed inside fetch_url, then its one static
        # fallback returned richer HTML. Provenance must preserve both stages.
        await asyncio.sleep(0.006)
        return FetchResult(
            html="<html><body>rich static fallback</body></html>",
            status_code=200,
            content_type="text/html",
            rendered=False,
            fetch_latency_ms=2.0,
            render_latency_ms=3.0,
        )

    async def extract(
        html: str,
        *_args: object,
        **_kwargs: object,
    ) -> ExtractionResult:
        if "shell" in html and "fallback" not in html:
            return ExtractionResult(
                text="shell",
                word_count=1,
                strategy="trafilatura",
            )
        return ExtractionResult(
            text=" ".join(["fallback"] * 30),
            word_count=30,
            strategy="trafilatura",
        )

    monkeypatch.setattr(crawler_module.fetcher_module, "fetch_url", fetch)
    monkeypatch.setattr(crawler_module, "extract_content_async", extract)
    monkeypatch.setattr(crawler_module, "_crawl_semaphore", None)
    monkeypatch.setattr(settings, "playwright_enabled", True)
    monkeypatch.setattr(settings, "playwright_java_script_enabled", True)

    result = await crawler_module._crawl_uncached(
        url="https://example.test/conditional",
        decide_js=False,
        auto_render=True,
        wait_for_selector=None,
        word_count_threshold=10,
        extraction_profile="balanced",
    )

    assert calls == [False, True]
    assert result.metadata is not None
    timings = result.metadata.stage_timings_ms
    assert timings["fetch"] == 3.0
    assert timings["render"] == 3.0
    assert timings["total"] >= 6.0
    assert (
        timings["queue"]
        + timings["fetch"]
        + timings["render"]
        + timings["extraction"]
    ) == pytest.approx(timings["total"], abs=0.004)
    assert result.metadata.rendered is False


@pytest.mark.anyio
async def test_raw_source_success_has_uniform_live_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import crawler as crawler_module
    from app.services.fetcher import FetchResult

    async def fetch(*_args: object, **_kwargs: object) -> FetchResult:
        await asyncio.sleep(0.002)
        return FetchResult(
            html="plain source line\n" * 20,
            status_code=200,
            content_type="text/plain",
            final_url="https://example.test/source.txt",
            fetch_latency_ms=0.5,
        )

    monkeypatch.setattr(crawler_module.fetcher_module, "fetch_url", fetch)
    monkeypatch.setattr(crawler_module, "_crawl_semaphore", None)

    result = await crawler_module._crawl_uncached(
        url="https://example.test/source.txt",
        decide_js=False,
        auto_render=False,
        wait_for_selector=None,
        word_count_threshold=10,
        extraction_profile="balanced",
    )

    assert result.error is None
    assert result.metadata is not None
    assert result.metadata.pipeline_revision == "clusy-extraction-v2"
    assert result.metadata.extraction_route == "raw_source"
    assert result.metadata.route_reasons == ["non_html_source"]
    assert result.metadata.completeness_coverage == "output_only"
    assert result.metadata.source_coverage_score is None
    assert result.metadata.output_grounding_score is None
    assert result.metadata.cache_status == "live"
    assert result.metadata.stage_timings_ms["fetch"] == 0.5


@pytest.mark.anyio
async def test_github_raw_success_has_specialist_route_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import crawler as crawler_module
    from app.services.fetcher import FetchResult

    raw_url = "https://raw.githubusercontent.com/acme/repo/main/example.py"

    async def fetch(*_args: object, **_kwargs: object) -> FetchResult:
        await asyncio.sleep(0.002)
        return FetchResult(
            html="def example():\n    return 'source'\n" * 10,
            status_code=200,
            content_type="text/plain",
            final_url=raw_url,
            fetch_latency_ms=0.75,
        )

    monkeypatch.setattr(crawler_module.fetcher_module, "fetch_url", fetch)
    monkeypatch.setattr(crawler_module, "_crawl_semaphore", None)

    result = await crawler_module._crawl_uncached(
        url=raw_url,
        decide_js=False,
        auto_render=False,
        wait_for_selector=None,
        word_count_threshold=10,
        extraction_profile="balanced",
    )

    assert result.error is None
    assert result.metadata is not None
    assert result.metadata.pipeline_revision == "clusy-extraction-v2"
    assert result.metadata.extraction_route == "github_source"
    assert result.metadata.route_reasons == ["github_source_specialist"]
    assert result.metadata.completeness_coverage == "output_only"
    assert result.metadata.stage_timings_ms["fetch"] == 0.75


@pytest.mark.anyio
async def test_academic_html_success_has_specialist_route_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import crawler as crawler_module
    from app.services.academic import AcademicPaper
    from app.services.fetcher import FetchResult

    url = "https://papers.example.test/article/one"

    async def fetch(*_args: object, **_kwargs: object) -> FetchResult:
        await asyncio.sleep(0.002)
        return FetchResult(
            html="<html><body><article>paper body</article></body></html>",
            status_code=200,
            content_type="text/html",
            final_url=url,
            fetch_latency_ms=0.625,
        )

    async def academic_html(**_kwargs: object) -> CrawlResult:
        return crawler_module._academic_result(
            requested_url=url,
            paper=AcademicPaper(
                title="Structured Academic Page",
                abstract="A structured abstract with authoritative metadata.",
                full_text="Structured full text from the academic HTML source.",
                word_count=8,
            ),
            source_url=url,
            content_type="text/html",
            status_code=200,
            rendered=False,
            strategy="academic-html",
            html="<html><body><article>paper body</article></body></html>",
            links=[],
        )

    monkeypatch.setattr(crawler_module.fetcher_module, "fetch_url", fetch)
    monkeypatch.setattr(crawler_module, "_crawl_academic_html", academic_html)
    monkeypatch.setattr(crawler_module, "_crawl_semaphore", None)

    result = await crawler_module._crawl_uncached(
        url=url,
        decide_js=False,
        auto_render=False,
        wait_for_selector=None,
        word_count_threshold=10,
        extraction_profile="balanced",
    )

    assert result.error is None
    assert result.metadata is not None
    assert result.metadata.pipeline_revision == "clusy-extraction-v2"
    assert result.metadata.extraction_route == "academic_html"
    assert result.metadata.route_reasons == ["academic_full_text_detected"]
    assert result.metadata.completeness_score == 0.0
    assert result.metadata.completeness_coverage == "output_only"
    assert result.metadata.stage_timings_ms["fetch"] == 0.625


def test_parallel_specialist_latency_is_bounded_by_request_wall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import crawler as crawler_module

    monkeypatch.setattr(
        crawler_module.time_module,
        "perf_counter",
        lambda: 10.010,
    )
    result = CrawlResult(
        url="https://example.test/parallel-specialist",
        markdown="specialist output",
        metadata=ExtractionMetadata(
            extraction_strategy="source-text",
            content_scope="source",
        ),
    )

    finalized = crawler_module._finalize_live_success(
        result,
        pipeline_started=10.0,
        queue_elapsed_ms=1.0,
        # Simulate two overlapping specialist fetch observations plus a render.
        fetch_elapsed_ms=20.0,
        render_elapsed_ms=10.0,
    )

    assert finalized.metadata is not None
    timings = finalized.metadata.stage_timings_ms
    assert timings["total"] == pytest.approx(10.0, abs=0.001)
    assert timings["queue"] == pytest.approx(1.0, abs=0.001)
    assert timings["fetch"] == pytest.approx(6.0, abs=0.001)
    assert timings["render"] == pytest.approx(3.0, abs=0.001)
    assert timings["extraction"] == 0
    assert sum(
        timings[name]
        for name in ("queue", "fetch", "render", "extraction")
    ) == pytest.approx(timings["total"], abs=0.004)

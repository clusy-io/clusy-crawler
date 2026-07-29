from __future__ import annotations

import asyncio
import time

import orjson
import pytest

from app.cache import make_cache_key
from app.config import settings
from app.models.responses import CrawlResult
from app.services import crawler as crawler_mod
from app.services import fetcher as fetcher_mod
from app.services.crawler import _extract_links, _project_formats
from app.services.fetcher import FetchResult
from app.services.site_map import _same_site

# ── link extraction ─────────────────────────────────────────────────


def test_extract_links_absolutizes_and_filters():
    html = """
    <a href="/docs/intro">a</a>
    <a href="https://other.com/x">b</a>
    <a href="mailto:x@y.com">c</a>
    <a href="javascript:void(0)">d</a>
    <a href="/docs/intro">dup</a>
    """
    links = _extract_links(html, "https://example.com/page")
    assert "https://example.com/docs/intro" in links
    assert "https://other.com/x" in links
    assert all(not link.startswith(("mailto:", "javascript:")) for link in links)
    # de-duplicated
    assert links.count("https://example.com/docs/intro") == 1


# ── format projection ───────────────────────────────────────────────


def test_project_formats_drops_unrequested_fields():
    r = CrawlResult(url="u", markdown="m", html="<html>", links=["a"])
    _project_formats(r, ["markdown"])
    assert r.html is None and r.links is None

    r2 = CrawlResult(url="u", markdown="m", html="<html>", links=["a"])
    _project_formats(r2, ["markdown", "html", "links"])
    assert r2.html == "<html>" and r2.links == ["a"]


# ── same-site map filter ─────────────────────────────────────────────


def test_same_site():
    assert _same_site("example.com", "https://example.com/a")
    assert _same_site("example.com", "https://docs.example.com/a")
    assert not _same_site("example.com", "https://evil.com/a")


# ── max_age cache behavior ───────────────────────────────────────────


class _MemCache:
    """Minimal in-memory cache that actually stores values (unlike the Noop)."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ttl=None):
        self.store[key] = value


@pytest.fixture
def mem_cache(monkeypatch):
    cache = _MemCache()
    monkeypatch.setattr("app.cache._cache", cache)
    return cache


@pytest.fixture
def stub_fetch(monkeypatch):
    calls = {"n": 0}

    async def _fetch(url, js_render=False, wait_for_selector=None):
        calls["n"] += 1
        return FetchResult(
            html="<html><head><title>T</title></head><body>"
            + "<p>"
            + " ".join(["word"] * 80)
            + '</p><a href="/source">source</a></body></html>',
            status_code=200,
            content_type="text/html",
        )

    monkeypatch.setattr(fetcher_mod, "fetch_url", _fetch)
    monkeypatch.setattr("app.config.settings.js_render_mode", "never")
    monkeypatch.setattr(crawler_mod, "_crawl_semaphore", None)
    return calls


async def test_second_call_is_cached(mem_cache, stub_fetch):
    r1 = (await crawler_mod.crawl_urls(["https://ex.com/a"]))[0]
    assert r1.cached is False
    assert r1.metadata is not None
    assert r1.metadata.cache_status == "live"
    r2 = (await crawler_mod.crawl_urls(["https://ex.com/a"]))[0]
    assert r2.cached is True
    assert r2.metadata is not None
    assert r2.metadata.cache_status == "hit"
    assert r2.metadata.cache_age_ms is not None
    assert r2.metadata.cache_lookup_ms is not None
    assert stub_fetch["n"] == 1  # second served from cache, no re-fetch


async def test_cache_hit_replaces_persisted_live_stage_timings(mem_cache, stub_fetch):
    await crawler_mod.crawl_urls(["https://ex.com/cache-timing"])
    key = next(iter(mem_cache.store))
    envelope = orjson.loads(mem_cache.store[key])
    assert envelope["r"]["metadata"]["stage_timings_ms"] == {}
    envelope["r"]["metadata"]["stage_timings_ms"] = {
        "queue": 101.0,
        "fetch": 102.0,
        "render": 103.0,
        "extraction": 104.0,
        "total": 999.0,
    }
    mem_cache.store[key] = orjson.dumps(envelope)

    result = (
        await crawler_mod.crawl_urls(["https://ex.com/cache-timing"])
    )[0]

    assert result.cached is True
    assert result.metadata is not None
    assert result.metadata.cache_status == "hit"
    assert result.metadata.stage_timings_ms["queue"] == 0
    assert result.metadata.stage_timings_ms["fetch"] == 0
    assert result.metadata.stage_timings_ms["render"] == 0
    assert result.metadata.stage_timings_ms["extraction"] == 0
    assert result.metadata.stage_timings_ms["total"] == (
        result.metadata.cache_lookup_ms
    )


async def test_max_age_zero_bypasses_cache(mem_cache, stub_fetch):
    await crawler_mod.crawl_urls(["https://ex.com/a"])
    r2 = (await crawler_mod.crawl_urls(["https://ex.com/a"], max_age=0))[0]
    assert r2.cached is False
    assert stub_fetch["n"] == 2  # forced re-fetch


async def test_stale_entry_triggers_recrawl(mem_cache, stub_fetch):
    await crawler_mod.crawl_urls(["https://ex.com/a"])
    # Age the stored entry well past the freshness bar.
    key = next(iter(mem_cache.store))
    env = orjson.loads(mem_cache.store[key])
    env["t"] = time.time() - 9999
    mem_cache.store[key] = orjson.dumps(env)
    r = (await crawler_mod.crawl_urls(["https://ex.com/a"], max_age=10))[0]
    assert r.cached is False
    assert stub_fetch["n"] == 2  # stale → re-crawled


@pytest.mark.parametrize("profile", ["adaptive", "quality"])
async def test_verified_model_assisted_outputs_use_versioned_cache(
    profile,
    mem_cache,
    stub_fetch,
    monkeypatch,
):
    from app.services.extractor import ExtractionResult

    async def quality_extract(*_args, **_kwargs):
        return ExtractionResult(
            text="# Model output\n\nFresh content",
            word_count=5,
            strategy="mineru-html-v1.1-openai",
            route="quality_model",
            model_assisted=True,
            quality_attempted=True,
            quality_succeeded=True,
        )

    monkeypatch.setattr(crawler_mod, "extract_content_async", quality_extract)
    monkeypatch.setattr(
        settings,
        "quality_extraction_backend_revision",
        "model-build@sha256:abc123",
    )

    first = (
        await crawler_mod.crawl_urls(
            [f"https://ex.com/{profile}"],
            extraction_profile=profile,
        )
    )[0]
    second = (
        await crawler_mod.crawl_urls(
            [f"https://ex.com/{profile}"],
            extraction_profile=profile,
        )
    )[0]

    assert first.cached is False
    assert second.cached is True
    assert stub_fetch["n"] == 1
    assert len(mem_cache.store) == 1


async def test_unversioned_model_assisted_output_is_not_persisted(
    mem_cache,
    stub_fetch,
    monkeypatch,
):
    from app.services.extractor import ExtractionResult

    async def quality_extract(*_args, **_kwargs):
        return ExtractionResult(
            text="# Unversioned model output",
            word_count=4,
            strategy="mineru-html-v1.1-openai",
            route="quality_model",
            model_assisted=True,
            quality_attempted=True,
            quality_succeeded=True,
        )

    monkeypatch.setattr(crawler_mod, "extract_content_async", quality_extract)
    monkeypatch.setattr(settings, "quality_extraction_backend_revision", "")

    first = (
        await crawler_mod.crawl_urls(
            ["https://ex.com/unversioned-quality"],
            extraction_profile="quality",
        )
    )[0]
    second = (
        await crawler_mod.crawl_urls(
            ["https://ex.com/unversioned-quality"],
            extraction_profile="quality",
        )
    )[0]

    assert first.cached is False
    assert second.cached is False
    assert stub_fetch["n"] == 2
    assert mem_cache.store == {}


async def test_cached_canonical_result_can_project_links(mem_cache, stub_fetch):
    first = (await crawler_mod.crawl_urls(["https://ex.com/a"]))[0]
    assert first.links is None
    cached_envelope = orjson.loads(next(iter(mem_cache.store.values())))
    assert cached_envelope["r"]["html"] is None
    assert cached_envelope["r"]["links"] == ["https://ex.com/source"]

    second = (
        await crawler_mod.crawl_urls(
            ["https://ex.com/a"],
            formats=["markdown", "links"],
        )
    )[0]

    assert second.cached is True
    assert second.links == ["https://ex.com/source"]
    assert stub_fetch["n"] == 1


async def test_corrupt_cache_entry_is_treated_as_miss(mem_cache, stub_fetch):
    url = "https://ex.com/corrupt"
    key = make_cache_key(url, False, None, word_count_threshold=10)
    mem_cache.store[key] = b"{definitely-not-json"

    result = (await crawler_mod.crawl_urls([url]))[0]

    assert result.error is None
    assert result.cached is False
    assert stub_fetch["n"] == 1
    # The successful live crawl replaces the invalid entry.
    assert orjson.loads(mem_cache.store[key])["r"]["markdown"]


async def test_simultaneous_identical_crawls_are_singleflight(mem_cache, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = {"n": 0}
    html = (
        "<html><head><title>T</title></head><body>"
        "<p>" + " ".join(["word"] * 80) + '</p><a href="/source">source</a></body></html>'
    )

    async def blocking_fetch(url, js_render=False, wait_for_selector=None):
        calls["n"] += 1
        started.set()
        await release.wait()
        return FetchResult(
            html=html,
            status_code=200,
            content_type="text/html",
        )

    monkeypatch.setattr(fetcher_mod, "fetch_url", blocking_fetch)
    monkeypatch.setattr("app.config.settings.js_render_mode", "never")
    monkeypatch.setattr(crawler_mod, "_crawl_semaphore", None)
    monkeypatch.setattr(crawler_mod, "_singleflight_tasks", {})
    monkeypatch.setattr(crawler_mod, "_singleflight_lock", None)
    monkeypatch.setattr(crawler_mod, "_singleflight_loop", None)

    tasks = [
        asyncio.create_task(
            crawler_mod._crawl_single_url(
                "https://singleflight.example/a",
                formats=["markdown", "links"] if i == 0 else ["markdown"],
                max_age=0,
            )
        )
        for i in range(8)
    ]
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*tasks)

    assert calls["n"] == 1
    assert results[0].links == ["https://singleflight.example/source"]
    assert all(result.markdown for result in results)
    assert all(result.links is None for result in results[1:])


async def test_cancelled_singleflight_waiter_does_not_cancel_other_waiters(
    mem_cache,
    monkeypatch,
):
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocking_fetch(url, js_render=False, wait_for_selector=None):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return FetchResult(
            html="<html><body><main>" + ("useful content " * 40) + "</main></body></html>",
            status_code=200,
            content_type="text/html",
        )

    monkeypatch.setattr(fetcher_mod, "fetch_url", blocking_fetch)
    monkeypatch.setattr(crawler_mod, "_singleflight_tasks", {})
    monkeypatch.setattr(crawler_mod, "_singleflight_lock", None)
    monkeypatch.setattr(crawler_mod, "_singleflight_loop", None)
    monkeypatch.setattr(crawler_mod, "_accepting_crawls", True)

    first = asyncio.create_task(
        crawler_mod._crawl_single_url(
            "https://singleflight.example/cancel-one",
            max_age=0,
        )
    )
    second = asyncio.create_task(
        crawler_mod._crawl_single_url(
            "https://singleflight.example/cancel-one",
            max_age=0,
        )
    )
    await started.wait()
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not cancelled.is_set()

    release.set()
    result = await second
    assert result.error is None
    assert result.markdown


async def test_last_cancelled_singleflight_waiter_cancels_underlying_work(
    mem_cache,
    monkeypatch,
):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocking_fetch(url, js_render=False, wait_for_selector=None):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("unreachable")

    monkeypatch.setattr(fetcher_mod, "fetch_url", blocking_fetch)
    monkeypatch.setattr(crawler_mod, "_singleflight_tasks", {})
    monkeypatch.setattr(crawler_mod, "_singleflight_lock", None)
    monkeypatch.setattr(crawler_mod, "_singleflight_loop", None)
    monkeypatch.setattr(crawler_mod, "_accepting_crawls", True)

    waiter = asyncio.create_task(
        crawler_mod._crawl_single_url(
            "https://singleflight.example/cancel-last",
            max_age=0,
        )
    )
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.wait_for(cancelled.wait(), timeout=1)


async def test_pdf_result_is_cached(mem_cache, monkeypatch):
    calls = {"fetch": 0, "extract": 0}

    async def fetch_pdf(url, js_render=False, wait_for_selector=None):
        calls["fetch"] += 1
        return FetchResult(
            status_code=200,
            content_type="application/pdf",
            raw_bytes=b"%PDF-fake",
        )

    class Paper:
        title = "Cached Paper"
        word_count = 42

        def to_markdown(self):
            return "# Cached Paper\n\nBody"

    def extract_pdf(contents, url):
        calls["extract"] += 1
        return Paper()

    monkeypatch.setattr(fetcher_mod, "fetch_url", fetch_pdf)
    monkeypatch.setattr("app.services.academic.extract_pdf", extract_pdf)
    monkeypatch.setattr("app.config.settings.js_render_mode", "never")
    monkeypatch.setattr(crawler_mod, "_crawl_semaphore", None)

    first = (await crawler_mod.crawl_urls(["https://ex.com/paper.pdf"]))[0]
    second = (
        await crawler_mod.crawl_urls(
            ["https://ex.com/paper.pdf"],
            formats=["markdown", "links"],
        )
    )[0]

    assert first.cached is False
    assert second.cached is True
    assert second.links == []
    assert second.metadata is not None
    assert second.metadata.extraction_strategy == "pypdfium2+academic"
    assert calls == {"fetch": 1, "extract": 1}


async def test_academic_html_result_is_cached_with_links(mem_cache, monkeypatch):
    from app.services.extractor import ExtractionResult

    calls = {"fetch": 0, "academic": 0}
    html = (
        '<html><head><meta name="citation_title" content="Structured Paper">'
        '<meta name="citation_author" content="Researcher"></head>'
        "<body><article><p>"
        + " ".join(["research"] * 300)
        + '</p><a href="/dataset">dataset</a></article></body></html>'
    )

    async def fetch_paper(url, js_render=False, wait_for_selector=None):
        calls["fetch"] += 1
        return FetchResult(
            html=html,
            status_code=200,
            content_type="text/html",
        )

    async def extract_page(html_content, url, extraction_profile="balanced"):
        assert extraction_profile == "balanced"
        return ExtractionResult(text="research " * 300, word_count=300, strategy="test")

    class Paper:
        title = "Structured Paper"
        abstract = "Abstract"
        sections = []
        word_count = 300

        def to_markdown(self):
            return "# Structured Paper\n\nBody"

    def extract_long_html(html_content, url):
        calls["academic"] += 1
        return Paper()

    monkeypatch.setattr(fetcher_mod, "fetch_url", fetch_paper)
    monkeypatch.setattr(crawler_mod, "extract_content_async", extract_page)
    monkeypatch.setattr("app.services.academic.extract_long_html", extract_long_html)
    monkeypatch.setattr("app.config.settings.js_render_mode", "never")
    monkeypatch.setattr(crawler_mod, "_crawl_semaphore", None)

    url = "https://ex.com/paper/123"
    first = (await crawler_mod.crawl_urls([url]))[0]
    second = (
        await crawler_mod.crawl_urls(
            [url],
            formats=["markdown", "links"],
        )
    )[0]

    assert first.cached is False
    assert second.cached is True
    assert second.links == ["https://ex.com/dataset"]
    assert second.metadata is not None
    assert second.metadata.extraction_strategy == "academic-html"
    assert calls == {"fetch": 1, "academic": 1}


# ── structured (LLM) extraction ─────────────────────────────────────


def test_ensure_strict_recurses():
    from app.services.structured import _ensure_strict

    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "tags": {
                "type": "array",
                "items": {"type": "object", "properties": {"name": {"type": "string"}}},
            },
        },
    }
    strict = _ensure_strict(schema)
    assert strict["additionalProperties"] is False
    assert strict["properties"]["tags"]["items"]["additionalProperties"] is False


@pytest.fixture
def reset_llm_client(monkeypatch):
    import app.services.structured as s

    monkeypatch.setattr(s, "_client", None)
    monkeypatch.setattr(s, "_client_init", False)
    return s


async def test_extract_structured_no_key_is_graceful(reset_llm_client, monkeypatch):
    monkeypatch.setattr("app.config.settings.anthropic_api_key", "")
    out = await reset_llm_client.extract_structured("some content", {"type": "object"})
    assert "error" in out and "ANTHROPIC_API_KEY" in out["error"]


async def test_extract_structured_parses_schema_output(reset_llm_client, monkeypatch):
    captured = {}

    class _Block:
        type = "text"
        text = '{"title": "Hello", "price": 9.99}'

    class _Resp:
        content = [_Block()]

    class _Messages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return _Resp()

    class _FakeClient:
        messages = _Messages()

    monkeypatch.setattr(reset_llm_client, "_get_client", lambda: _FakeClient())
    out = await reset_llm_client.extract_structured(
        "Hello costs $9.99", {"type": "object", "properties": {"title": {"type": "string"}}}
    )
    assert out == {"title": "Hello", "price": 9.99}
    # schema was passed through as a constrained output format
    assert captured["output_config"]["format"]["type"] == "json_schema"


async def test_structured_queue_wait_is_inside_total_deadline(
    reset_llm_client,
    monkeypatch,
):
    class _Messages:
        async def create(self, **_kwargs):
            raise AssertionError("capacity was never acquired")

    class _FakeClient:
        messages = _Messages()

    monkeypatch.setattr(reset_llm_client, "_get_client", lambda: _FakeClient())
    monkeypatch.setattr("app.config.settings.structured_extraction_timeout_s", 0.01)
    monkeypatch.setattr(reset_llm_client, "_semaphore", asyncio.Semaphore(0))
    monkeypatch.setattr(reset_llm_client, "_semaphore_loop", asyncio.get_running_loop())

    out = await asyncio.wait_for(
        reset_llm_client.extract_structured("content", {"type": "object"}),
        timeout=0.1,
    )

    assert out == {"error": "structured extraction timed out"}


async def test_crawl_urls_wires_extraction(monkeypatch, stub_fetch):
    async def fake_extract(content, schema, prompt):
        return {"ok": True, "len": len(content)}

    monkeypatch.setattr("app.services.structured.extract_structured", fake_extract)
    results = await crawler_mod.crawl_urls(
        ["https://ex.com/a"],
        formats=["markdown", "json"],
        json_schema={"type": "object"},
    )
    assert results[0].extracted == {"ok": True, "len": len(results[0].markdown)}


async def test_crawl_urls_skips_extraction_without_format(monkeypatch, stub_fetch):
    called = {"n": 0}

    async def fake_extract(content, schema, prompt):
        called["n"] += 1
        return {}

    monkeypatch.setattr("app.services.structured.extract_structured", fake_extract)
    results = await crawler_mod.crawl_urls(
        ["https://ex.com/a"],
        json_schema={"type": "object"},  # no "json" in formats
    )
    assert results[0].extracted is None
    assert called["n"] == 0

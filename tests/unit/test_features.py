from __future__ import annotations

import time

import orjson
import pytest

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
            + "<p>" + " ".join(["word"] * 80) + "</p></body></html>",
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
    r2 = (await crawler_mod.crawl_urls(["https://ex.com/a"]))[0]
    assert r2.cached is True
    assert stub_fetch["n"] == 1  # second served from cache, no re-fetch


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


# ── structured (LLM) extraction ─────────────────────────────────────


def test_ensure_strict_recurses():
    from app.services.structured import _ensure_strict

    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "object",
                     "properties": {"name": {"type": "string"}}}},
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


async def test_crawl_urls_wires_extraction(monkeypatch, stub_fetch):
    async def fake_extract(content, schema, prompt):
        return {"ok": True, "len": len(content)}

    monkeypatch.setattr(
        "app.services.structured.extract_structured", fake_extract
    )
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
        ["https://ex.com/a"], json_schema={"type": "object"}  # no "json" in formats
    )
    assert results[0].extracted is None
    assert called["n"] == 0

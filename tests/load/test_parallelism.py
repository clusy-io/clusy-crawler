from __future__ import annotations

import asyncio
import time

import pytest

from app.config import settings


class TestParallelism:
    @pytest.mark.anyio
    async def test_semaphore_respected(self, monkeypatch):
        """
        Verify that the crawl semaphore limits in-flight requests.
        Submit 2x max_concurrent_tasks URLs; count how many fetch concurrently.
        """
        from app.services import crawler, fetcher

        in_flight = 0
        max_seen = 0
        lock = asyncio.Lock()

        async def mock_fetch(url, js_render=False, wait_for_selector=None):
            from app.services.fetcher import FetchResult

            nonlocal in_flight, max_seen
            async with lock:
                in_flight += 1
                max_seen = max(max_seen, in_flight)
            await asyncio.sleep(0.05)
            async with lock:
                in_flight -= 1
            return FetchResult(
                html="<html><body><p>Test content here for extraction.</p></body></html>",
                status_code=200,
                content_type="text/html",
            )

        monkeypatch.setattr(fetcher, "fetch_url", mock_fetch)
        monkeypatch.setattr(settings, "max_concurrent_tasks", 5)
        monkeypatch.setattr(settings, "js_render_mode", "never")

        # Reset semaphore for the new limit
        monkeypatch.setattr(crawler, "_crawl_semaphore", None)

        urls = [f"https://example.com/page/{i}" for i in range(15)]
        results = await crawler.crawl_urls(urls)

        assert len(results) == 15
        assert max_seen <= 5  # Semaphore caps in-flight
        assert max_seen >= 1  # At least some concurrency happened

    @pytest.mark.anyio
    async def test_fast_concurrent_fetching(self, monkeypatch):
        """Submit 10 URLs concurrently and ensure total time < sequential time."""
        from app.services import crawler, fetcher

        async def mock_fetch(url, js_render=False, wait_for_selector=None):
            from app.services.fetcher import FetchResult

            await asyncio.sleep(0.1)
            return FetchResult(
                html="<html><body><p>Test content here for extraction.</p></body></html>",
                status_code=200,
                content_type="text/html",
            )

        monkeypatch.setattr(fetcher, "fetch_url", mock_fetch)
        monkeypatch.setattr(settings, "max_concurrent_tasks", 10)
        monkeypatch.setattr(settings, "js_render_mode", "never")
        monkeypatch.setattr(crawler, "_crawl_semaphore", None)

        urls = [f"https://example.com/page/{i}" for i in range(10)]
        start = time.monotonic()
        results = await crawler.crawl_urls(urls)
        elapsed = time.monotonic() - start

        assert len(results) == 10
        assert elapsed < 0.5  # With 0.1s per fetch × 10, sequential = 1s, parallel < 0.2s

from __future__ import annotations

import pytest

from app.config import settings
from app.services.crawler import _crawl_single_url, _get_semaphore, crawl_urls


class TestCrawler:
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

    @pytest.mark.anyio
    async def test_crawl_exception_returned_as_error(self, monkeypatch):
        from app.services import fetcher

        async def mock_fetch(url, js_render=False, wait_for_selector=None):
            raise RuntimeError("boom")

        monkeypatch.setattr(fetcher, "fetch_url", mock_fetch)

        results = await crawl_urls(["https://example.com"])
        assert len(results) == 1
        assert results[0].error is not None
        assert "boom" in results[0].error

    def test_semaphore_caps_concurrency(self):
        sem = _get_semaphore()
        assert sem._value == settings.max_concurrent_tasks

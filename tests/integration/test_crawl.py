from __future__ import annotations

import pytest


class TestCrawlEndpoint:
    @pytest.mark.anyio
    async def test_crawl_requires_urls(self, client):
        resp = await client.post("/crawl", json={})
        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_crawl_private_ip_rejected(self, client):
        resp = await client.post("/crawl", json={"urls": ["http://127.0.0.1/test"]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["results"][0]["error"] is not None

    @pytest.mark.anyio
    async def test_crawl_empty_urls_rejected(self, client):
        resp = await client.post("/crawl", json={"urls": []})
        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_crawl_with_mocked_fetch(self, client, monkeypatch):
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

        resp = await client.post(
            "/crawl",
            json={"urls": ["https://example.com"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert len(data["results"]) == 1
        r = data["results"][0]
        assert r["error"] is None
        assert "Hello world" in r["markdown"]
        assert r["metadata"] is not None

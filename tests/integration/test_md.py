from __future__ import annotations

import pytest


class TestMDEndpoint:
    @pytest.mark.anyio
    async def test_md_missing_url(self, client):
        resp = await client.post("/md", json={})
        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_md_with_mocked_fetch(self, client, monkeypatch):
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
            "/md",
            json={"url": "https://example.com", "options": {"js_render": False}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "Hello world" in data["markdown"]


class TestHTMLEndpoint:
    @pytest.mark.anyio
    async def test_html_missing_url(self, client):
        resp = await client.post("/html", json={})
        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_html_with_mocked_fetch(self, client, monkeypatch):
        from app.services import fetcher

        async def mock_fetch(url, js_render=False, wait_for_selector=None):
            from app.services.fetcher import FetchResult

            return FetchResult(
                html="<html><body>raw html</body></html>",
                status_code=200,
                content_type="text/html",
            )

        monkeypatch.setattr(fetcher, "fetch_url", mock_fetch)

        resp = await client.post("/html", json={"url": "https://example.com"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "raw html" in data["html"]

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.main import app


class TestAuthMiddleware:
    @pytest.mark.anyio
    async def test_auth_bypassed_when_token_empty(self, monkeypatch):
        monkeypatch.setattr(settings, "crawler_api_token", "")
        transport = ASGITransport(app=app)
        client = AsyncClient(transport=transport, base_url="http://test")
        resp = await client.post("/crawl", json={"urls": ["https://example.com"]})
        assert resp.status_code != 401

    @pytest.mark.anyio
    async def test_auth_required_when_token_set(self, monkeypatch):
        monkeypatch.setattr(settings, "crawler_api_token", "secret")
        transport = ASGITransport(app=app)
        client = AsyncClient(transport=transport, base_url="http://test")
        resp = await client.post("/crawl", json={"urls": ["https://example.com"]})
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_auth_valid_token(self, monkeypatch):
        monkeypatch.setattr(settings, "crawler_api_token", "secret")
        transport = ASGITransport(app=app)
        client = AsyncClient(transport=transport, base_url="http://test")
        resp = await client.post(
            "/crawl",
            json={"urls": ["https://example.com"]},
            headers={"Authorization": "Bearer secret"},
        )
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_auth_invalid_token(self, monkeypatch):
        monkeypatch.setattr(settings, "crawler_api_token", "secret")
        transport = ASGITransport(app=app)
        client = AsyncClient(transport=transport, base_url="http://test")
        resp = await client.post(
            "/crawl",
            json={"urls": ["https://example.com"]},
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_health_unauthenticated(self, monkeypatch):
        monkeypatch.setattr(settings, "crawler_api_token", "secret")
        transport = ASGITransport(app=app)
        client = AsyncClient(transport=transport, base_url="http://test")
        resp = await client.get("/health")
        assert resp.status_code == 200

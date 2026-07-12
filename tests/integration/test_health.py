from __future__ import annotations

import pytest


class TestHealth:
    @pytest.mark.anyio
    async def test_health_ok(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    @pytest.mark.anyio
    async def test_ready(self, client):
        resp = await client.get("/health/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ready", "degraded")
        assert "checks" in data
        assert "http_client" in data["checks"]

    @pytest.mark.anyio
    async def test_version(self, client):
        resp = await client.get("/health/version")
        assert resp.status_code == 200
        data = resp.json()
        assert "python_version" in data
        assert "trafilatura_version" in data
        assert "environment" in data

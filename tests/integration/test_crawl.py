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
        metadata = r["metadata"]
        assert metadata["pipeline_revision"] == "clusy-extraction-v2"
        assert metadata["extraction_route"]
        assert isinstance(metadata["completeness_score"], (int, float))
        assert 0 <= metadata["completeness_score"] <= 1
        assert metadata["completeness_coverage"] in {
            "unassessed",
            "output_only",
            "source_full",
            "source_prefix",
        }
        if metadata["completeness_coverage"] in {"source_full", "source_prefix"}:
            assert metadata["source_coverage_score"] is not None
            assert metadata["output_grounding_score"] is not None
        assert metadata["cache_status"] == "live"
        assert set(metadata["stage_timings_ms"]) == {
            "queue",
            "fetch",
            "render",
            "extraction",
            "total",
        }
        assert metadata["stage_timings_ms"]["total"] >= 0
        assert metadata["stage_timings_ms"]["total"] >= (
            metadata["stage_timings_ms"]["queue"]
            + metadata["stage_timings_ms"]["fetch"]
            + metadata["stage_timings_ms"]["render"]
            + metadata["stage_timings_ms"]["extraction"]
        ) - 0.01

    @pytest.mark.anyio
    async def test_crawl_cold_no_store_returns_effective_policy_receipt(
        self,
        client,
        monkeypatch,
    ):
        from app.services import crawler, fetcher
        from app.services.fetcher import FetchResult

        async def mock_fetch(url, js_render=False, wait_for_selector=None):
            return FetchResult(
                html=(
                    "<html><body><article>"
                    + " ".join(["content"] * 40)
                    + "</article></body></html>"
                ),
                status_code=200,
                content_type="text/html",
            )

        def cache_must_not_be_resolved():
            raise AssertionError("cold no-store request resolved persistent cache")

        monkeypatch.setattr(fetcher, "fetch_url", mock_fetch)
        monkeypatch.setattr(crawler, "get_cache", cache_must_not_be_resolved)

        response = await client.post(
            "/crawl",
            json={
                "urls": ["https://example.com/no-store"],
                "max_age": 0,
                "store_in_cache": False,
            },
        )

        assert response.status_code == 200
        result = response.json()["results"][0]
        assert result["error"] is None
        assert result["metadata"]["cache_policy"] == "no_store"
        assert result["metadata"]["cache_read_permitted"] is False
        assert result["metadata"]["cache_write_permitted"] is False
        assert result["metadata"]["cache_policy_revision"] == "crawl-cache-policy.v1"
        identity = response.json()["service_identity"]
        assert identity["schema_version"] == "crawl-service-identity.v1"
        assert identity["revision"]
        assert len(identity["config_fingerprint"]) == 64
        assert identity["image_digest"]
        version = (await client.get("/health/version")).json()
        assert identity["revision"] == version["sha"]
        assert identity["config_fingerprint"] == version["config_fingerprint"]
        assert identity["image_digest"] == version["image_digest"]

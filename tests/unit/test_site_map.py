from __future__ import annotations

import gzip
import time

import httpx

from app.services import site_map


def test_same_site_does_not_promote_tenant_to_parent_domain():
    assert site_map._same_site("user.github.io", "https://user.github.io/page")
    assert site_map._same_site("user.github.io", "https://docs.user.github.io/page")
    assert not site_map._same_site("user.github.io", "https://github.io/attacker")
    assert not site_map._same_site("user.github.io", "https://other.github.io/attacker")


async def test_map_site_traverses_indexes_and_contains_sitemap_hosts(monkeypatch):
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        documents = {
            "/robots.txt": (
                "Sitemap: https://example.com/sitemap-index.xml\n"
                "Sitemap: https://outside.test/attacker.xml\n"
            ),
            "/sitemap-index.xml": (
                "<sitemapindex>"
                "<loc>https://example.com/news.xml</loc>"
                "<loc>https://outside.test/nested.xml</loc>"
                "</sitemapindex>"
            ),
            "/news.xml": (
                "<urlset>"
                "<loc>https://example.com/a</loc>"
                "<loc>https://example.com/b?x=1&amp;y=2</loc>"
                "</urlset>"
            ),
            "/": (
                '<a href="/from-home">home</a>'
                '<a href="https://outside.test/not-allowed">outside</a>'
            ),
        }
        return httpx.Response(200, text=documents.get(request.url.path, ""))

    async def public(_url: str) -> str | None:
        return None

    async def not_private(_host: str) -> bool:
        return False

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(site_map, "get_http_client", lambda: client)
        monkeypatch.setattr(site_map, "validate_public_url", public)
        monkeypatch.setattr(site_map, "_is_private_host", not_private)

        links = await site_map.map_site("https://example.com", limit=10)

    assert links == [
        "https://example.com/a",
        "https://example.com/b?x=1&y=2",
        "https://example.com/from-home",
    ]
    assert not any("outside.test" in url for url in requested)


async def test_map_fetch_rejects_oversized_gzip_after_decompression(monkeypatch):
    compressed = gzip.compress(b"x" * (site_map._MAX_DISCOVERY_BYTES + 1))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=compressed)

    async def public(_url: str) -> str | None:
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(site_map, "validate_public_url", public)
        assert await site_map._get("https://example.com/sitemap.xml.gz", client) is None


async def test_map_redirect_cannot_escape_allowed_site(monkeypatch):
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "https://outside.test/steal.xml"},
        )

    async def public(_url: str) -> str | None:
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(site_map, "validate_public_url", public)
        result = await site_map._get(
            "https://example.com/sitemap.xml",
            client,
            allowed_host="example.com",
        )

    assert result is None
    assert requested == ["https://example.com/sitemap.xml"]


async def test_map_aggregate_download_budget_is_enforced(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"123456")

    async def public(_url: str) -> str | None:
        return None

    budget = site_map._DiscoveryBudget(
        remaining_bytes=5,
        deadline=time.monotonic() + 1,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(site_map, "validate_public_url", public)
        result = await site_map._get(
            "https://example.com/sitemap.xml",
            client,
            allowed_host="example.com",
            budget=budget,
        )

    assert result is None
    assert budget.remaining_bytes == 0

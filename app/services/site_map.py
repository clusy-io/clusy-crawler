"""Site URL discovery — the /map capability (Firecrawl parity).

Discovers a site's URLs cheaply (no rendering, no extraction) by reading
robots.txt → sitemap(s), then supplementing with same-domain links from the
homepage. Uses the shared HTTP client directly because sitemaps are XML and
fetcher.fetch_url() only accepts HTML/PDF.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import httpx
import structlog

from app.lib.http_client import get_http_client
from app.services.fetcher import _is_private_host, validate_public_url

logger = structlog.get_logger()

_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_HREF = re.compile(r'<a\b[^>]*\bhref=["\']([^"\'#]+)["\']', re.IGNORECASE)
_MAX_SITEMAPS = 50  # cap how many sitemap files we fetch per request


async def _get(url: str, client: httpx.AsyncClient) -> str | None:
    # SSRF guard: sitemap URLs come from robots.txt `Sitemap:` lines and
    # `<loc>` tags — both attacker-controllable — so every one is validated
    # before fetching, and redirects are not auto-followed (a 3xx to an
    # internal address would otherwise bypass the check).
    if await validate_public_url(url):
        return None
    try:
        r = await client.get(
            url,
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0),
            follow_redirects=False,
        )
        if r.status_code == 200:
            return r.text
    except Exception:
        return None
    return None


async def _discover_sitemaps(root: str, client: httpx.AsyncClient) -> list[str]:
    sitemaps: list[str] = []
    robots = await _get(urljoin(root, "/robots.txt"), client)
    if robots:
        for line in robots.splitlines():
            if line.lower().startswith("sitemap:"):
                sitemaps.append(line.split(":", 1)[1].strip())
    if not sitemaps:
        sitemaps.append(urljoin(root, "/sitemap.xml"))
    return sitemaps


def _same_site(host: str, candidate: str) -> bool:
    h = (urlparse(candidate).hostname or "").lower()
    if not h:
        return False
    return h == host or h.endswith("." + host) or host.endswith("." + h)


async def map_site(url: str, limit: int = 1000, search: str | None = None) -> list[str]:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return []
    if await _is_private_host(parsed.hostname):
        return []

    root = f"{parsed.scheme}://{parsed.netloc}"
    host = parsed.hostname.lower()
    client = get_http_client()

    found: list[str] = []
    seen: set[str] = set()
    needle = search.lower() if search else None

    def add(candidate: str) -> None:
        if candidate in seen or not _same_site(host, candidate):
            return
        if needle and needle not in candidate.lower():
            return
        seen.add(candidate)
        found.append(candidate)

    # 1. Sitemaps, recursing one level into sitemap-index files.
    to_parse = await _discover_sitemaps(root, client)
    fetched = 0
    while to_parse and len(found) < limit and fetched < _MAX_SITEMAPS:
        sm = to_parse.pop(0)
        fetched += 1
        xml = await _get(sm, client)
        if not xml:
            continue
        is_index = "<sitemapindex" in xml.lower()
        for loc in _LOC.findall(xml):
            if is_index or loc.lower().endswith((".xml", ".xml.gz")):
                if len(to_parse) + fetched < _MAX_SITEMAPS:
                    to_parse.append(loc)
            else:
                add(loc)
                if len(found) >= limit:
                    break

    # 2. Supplement with homepage links if the sitemap was thin/absent.
    if len(found) < limit:
        home = await _get(root, client)
        if home:
            for m in _HREF.finditer(home):
                add(urljoin(root, m.group(1).strip()))
                if len(found) >= limit:
                    break

    logger.info("map_site", url=url, discovered=len(found), sitemaps=fetched)
    return found[:limit]

"""Site URL discovery for the /map endpoint.

Discovers a site's URLs without rendering or content extraction by reading
robots.txt → sitemap(s), then supplementing with same-site links from the
homepage. Uses the shared HTTP client directly because sitemaps are XML and
fetcher.fetch_url() only accepts HTML/PDF.
"""

from __future__ import annotations

import asyncio
import re
import time
import zlib
from dataclasses import dataclass, field
from html import unescape
from urllib.parse import urljoin, urlparse

import httpx
import structlog

from app.config import settings
from app.lib.http_client import get_http_client
from app.services.fetcher import (
    _decode_html,
    _is_private_host,
    _response_peer_error,
    validate_public_url,
)

logger = structlog.get_logger()

_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_HREF = re.compile(r'<a\b[^>]*\bhref=["\']([^"\'#]+)["\']', re.IGNORECASE)
_XML_ENCODING = re.compile(rb"<\?xml[^>]+\bencoding=[\"']([^\"']+)", re.IGNORECASE)
_MAX_SITEMAPS = 50  # cap how many sitemap files we fetch per request
_SITEMAP_CONCURRENCY = 8
_MAX_DISCOVERY_BYTES = 10 * 1024 * 1024
_MAX_REDIRECTS = 5
_map_semaphore: asyncio.Semaphore | None = None
_map_semaphore_loop: asyncio.AbstractEventLoop | None = None


@dataclass
class _DiscoveryBudget:
    remaining_bytes: int
    deadline: float
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def consume(self, count: int) -> bool:
        if time.monotonic() >= self.deadline:
            return False
        async with self._lock:
            if count > self.remaining_bytes:
                self.remaining_bytes = 0
                return False
            self.remaining_bytes -= count
            return True


def _get_map_semaphore() -> asyncio.Semaphore:
    global _map_semaphore, _map_semaphore_loop
    loop = asyncio.get_running_loop()
    if _map_semaphore is None or _map_semaphore_loop is not loop:
        _map_semaphore = asyncio.Semaphore(settings.map_max_concurrency)
        _map_semaphore_loop = loop
    return _map_semaphore


def _decompress_gzip_bounded(body: bytes) -> bytes | None:
    """Inflate a sitemap without ever materializing more than the size cap."""
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    output = bytearray()
    pending = body
    try:
        while pending:
            chunk = decoder.decompress(
                pending,
                _MAX_DISCOVERY_BYTES - len(output) + 1,
            )
            output.extend(chunk)
            if len(output) > _MAX_DISCOVERY_BYTES:
                return None
            pending = decoder.unconsumed_tail
        output.extend(decoder.flush(_MAX_DISCOVERY_BYTES - len(output) + 1))
    except zlib.error:
        return None
    if not decoder.eof or len(output) > _MAX_DISCOVERY_BYTES:
        return None
    return bytes(output)


async def _get(
    url: str,
    client: httpx.AsyncClient,
    *,
    allowed_host: str | None = None,
    budget: _DiscoveryBudget | None = None,
) -> str | None:
    """Fetch one bounded text resource with redirect and peer validation."""
    current = url
    for _hop in range(_MAX_REDIRECTS + 1):
        # Sitemap URLs and redirect locations are attacker-controlled. Validate
        # every hop, then also inspect the connected peer when httpx exposes it.
        if (
            (allowed_host is not None and not _same_site(allowed_host, current))
            or await validate_public_url(current)
        ):
            return None
        try:
            from app.services.rate_limiter import get_rate_limiter

            await get_rate_limiter().acquire(current)
            async with client.stream(
                "GET",
                current,
                timeout=httpx.Timeout(
                    connect=5.0,
                    read=15.0,
                    write=10.0,
                    pool=5.0,
                ),
                follow_redirects=False,
            ) as response:
                if _response_peer_error(response):
                    return None

                location = response.headers.get("location")
                if response.is_redirect and location:
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    return None

                declared = response.headers.get("content-length")
                if declared:
                    try:
                        if int(declared) > _MAX_DISCOVERY_BYTES:
                            return None
                    except ValueError:
                        pass

                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_DISCOVERY_BYTES:
                        return None
                    if budget is not None and not await budget.consume(len(chunk)):
                        return None
                    chunks.append(chunk)
                body = b"".join(chunks)
                content_type = response.headers.get("content-type", "")
        except (httpx.HTTPError, OSError, ValueError):
            return None
        break
    else:
        return None

    if body.startswith(b"\x1f\x8b"):
        decompressed = _decompress_gzip_bounded(body)
        if decompressed is None:
            return None
        if (
            budget is not None
            and len(decompressed) > len(body)
            and not await budget.consume(len(decompressed) - len(body))
        ):
            return None
        body = decompressed

    xml_encoding = _XML_ENCODING.search(body[:512])
    if xml_encoding and "charset=" not in content_type.lower():
        label = xml_encoding.group(1).decode("ascii", "ignore")
        content_type = f"{content_type}; charset={label}"
    return _decode_html(body, content_type)


async def _discover_sitemaps(
    root: str,
    host: str,
    client: httpx.AsyncClient,
    budget: _DiscoveryBudget,
) -> list[str]:
    sitemaps: list[str] = []
    robots = await _get(
        urljoin(root, "/robots.txt"),
        client,
        allowed_host=host,
        budget=budget,
    )
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
    normalized_host = host.lower().rstrip(".")
    if h == normalized_host or h.endswith("." + normalized_host):
        return True
    # Treat only the conventional www/non-www pair as equivalent. The old
    # arbitrary parent-domain rule made user.github.io equivalent to github.io.
    return (
        normalized_host.startswith("www.")
        and h == normalized_host.removeprefix("www.")
    )


async def map_site(url: str, limit: int = 1000, search: str | None = None) -> list[str]:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return []
    if await _is_private_host(parsed.hostname):
        return []

    sem = _get_map_semaphore()
    async with sem:
        try:
            async with asyncio.timeout(settings.map_timeout_s):
                return await _map_site_bounded(url, limit, search)
        except TimeoutError:
            logger.warning(
                "map_site_deadline_exceeded",
                host=parsed.hostname.lower(),
            )
            return []


async def _map_site_bounded(
    url: str,
    limit: int,
    search: str | None,
) -> list[str]:
    parsed_url = urlparse(url)
    root = f"{parsed_url.scheme}://{parsed_url.netloc}"
    host = (parsed_url.hostname or "").lower()
    client = get_http_client()
    budget = _DiscoveryBudget(
        remaining_bytes=settings.map_max_download_bytes,
        deadline=time.monotonic() + settings.map_timeout_s,
    )

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

    # 1. Sitemaps, recursively traversing bounded index batches.
    to_parse = await _discover_sitemaps(root, host, client, budget)
    seen_sitemaps: set[str] = set()
    fetched = 0
    while to_parse and len(found) < limit and fetched < _MAX_SITEMAPS:
        batch: list[str] = []
        while (
            to_parse and len(batch) < _SITEMAP_CONCURRENCY and fetched + len(batch) < _MAX_SITEMAPS
        ):
            candidate = urljoin(root, unescape(to_parse.pop(0).strip()))
            if candidate in seen_sitemaps or not _same_site(host, candidate):
                continue
            seen_sitemaps.add(candidate)
            batch.append(candidate)
        if not batch:
            continue

        documents = await asyncio.gather(
            *(
                _get(
                    item,
                    client,
                    allowed_host=host,
                    budget=budget,
                )
                for item in batch
            )
        )
        fetched += len(batch)
        for sitemap_url, xml in zip(batch, documents, strict=True):
            if not xml:
                continue
            is_index = "<sitemapindex" in xml.lower()
            for raw_loc in _LOC.findall(xml):
                loc = urljoin(sitemap_url, unescape(raw_loc.strip()))
                if is_index or urlparse(loc).path.lower().endswith((".xml", ".xml.gz")):
                    if len(to_parse) + fetched < _MAX_SITEMAPS:
                        to_parse.append(loc)
                else:
                    add(loc)
                    if len(found) >= limit:
                        break
            if len(found) >= limit:
                break

    # 2. Supplement with homepage links if the sitemap was thin/absent.
    if len(found) < limit:
        home = await _get(
            root,
            client,
            allowed_host=host,
            budget=budget,
        )
        if home:
            for m in _HREF.finditer(home):
                add(urljoin(root, m.group(1).strip()))
                if len(found) >= limit:
                    break

    logger.info("map_site", host=host, discovered=len(found), sitemaps=fetched)
    return found[:limit]

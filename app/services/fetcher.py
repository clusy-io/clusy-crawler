from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import settings
from app.lib.http_client import get_http_client

if TYPE_CHECKING:
    from app.models.responses import ExtractionMetadata

logger = structlog.get_logger()

ALLOWED_SCHEMES = frozenset({"http", "https"})

# Redirects are followed manually (see fetch_url) so every hop can be
# re-validated against the SSRF guard; httpx's own auto-follow is disabled per
# request. This caps how many Location hops we chase before giving up.
_MAX_REDIRECTS = 5

# Hard ceiling on the DECOMPRESSED response body. httpx's aiter_bytes yields
# content-decoded bytes, so counting them here caps the post-Brotli/gzip size —
# closing the decompression-bomb DoS (an 800-byte body can inflate to >500 MB).
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MB


@dataclass
class FetchResult:
    html: str = ""
    error: str | None = None
    status_code: int = 0
    content_type: str = ""
    title: str = ""
    metadata: ExtractionMetadata | None = None
    latency_ms: float = 0.0
    bytes_downloaded: int = 0
    raw_bytes: bytes | None = None  # For PDF/binary content


def _ip_is_blocked(ip_str: str) -> bool:
    """True if an IP literal is anything other than a routable public address.

    Blocks private (RFC1918), loopback, link-local (incl. the 169.254.169.254
    cloud-metadata endpoint), unique-local IPv6, multicast, reserved, and the
    unspecified address (0.0.0.0 / ::). IPv4-mapped IPv6 (``::ffff:a.b.c.d``) is
    unwrapped first so ``::ffff:169.254.169.254`` cannot slip through.
    """
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable → treat as unsafe
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


async def _resolve_all(host: str) -> list[str]:
    """Resolve a host to every A/AAAA address (IP literals resolve to self)."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    # Deduplicate while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in seen:
            seen.add(ip)
            out.append(ip)
    return out


async def validate_public_url(url: str) -> str | None:
    """Return an error string if the URL is unsafe to fetch, else None.

    A URL is safe only when its scheme is http(s) and EVERY address the host
    resolves to is a routable public IP. Validating all resolved addresses
    (not just the first) blocks the multi-record bypass, and re-running this on
    every redirect hop blocks the "public URL 302s to 169.254.169.254" attack.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        return f"blocked scheme: {parsed.scheme or '(none)'!r}"
    host = parsed.hostname
    if not host:
        return "URL has no host"
    try:
        ips = await _resolve_all(host)
    except (socket.gaierror, OSError, UnicodeError) as e:
        return f"DNS resolution failed for {host}: {e}"
    if not ips:
        return f"{host} resolved to no addresses"
    for ip in ips:
        if _ip_is_blocked(ip):
            return f"{host} resolves to non-public address {ip}"
    return None


def _is_private_ip(host: str) -> bool:
    """True only if `host` is an IP literal in a non-public range.

    Returns False for hostnames — those must be resolved (via
    :func:`validate_public_url`) before they can be judged.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return _ip_is_blocked(host)


# Backwards-compatible helper still imported by site_map.py.
async def _is_private_host(host: str) -> bool:
    """True if the host resolves to any non-public address (or fails to resolve)."""
    if not host:
        return True
    try:
        ips = await _resolve_all(host)
    except (socket.gaierror, OSError, UnicodeError):
        return True
    if not ips:
        return True
    return any(_ip_is_blocked(ip) for ip in ips)


@retry(
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
)
async def _stream_one(url: str, client: httpx.AsyncClient) -> tuple[int, dict[str, str], bytes]:
    """Fetch a single URL (no auto-redirect) and return status, headers, body.

    Streams the response and stops reading once the decompressed body exceeds
    _MAX_RESPONSE_BYTES, so an oversized or decompression-bomb response cannot
    exhaust memory. Redirects are NOT followed here — the caller re-validates
    the Location and loops.
    """
    chunks: list[bytes] = []
    total = 0
    async with client.stream("GET", url, follow_redirects=False) as resp:
        # Reject obviously-oversized bodies before downloading them.
        declared = resp.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > _MAX_RESPONSE_BYTES:
                    await resp.aclose()
                    return resp.status_code, dict(resp.headers), b""
            except ValueError:
                pass
        if not resp.is_redirect:
            async for chunk in resp.aiter_bytes():
                remaining = _MAX_RESPONSE_BYTES - total
                if remaining <= 0:
                    break
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]
                chunks.append(chunk)
                total += len(chunk)
        return resp.status_code, dict(resp.headers), b"".join(chunks)


async def fetch_url(
    url: str,
    js_render: bool = False,
    wait_for_selector: str | None = None,
) -> FetchResult:
    client = get_http_client()
    t_start = time.monotonic()

    current = url
    status_code = 0
    headers: dict[str, str] = {}
    body = b""

    for _hop in range(_MAX_REDIRECTS + 1):
        err = await validate_public_url(current)
        if err:
            return FetchResult(error=f"SSRF blocked: {err}")
        try:
            status_code, headers, body = await _stream_one(current, client)
        except httpx.TimeoutException:
            return FetchResult(error="Request timed out")
        except httpx.ConnectError as e:
            return FetchResult(error=f"Connection failed: {e}")
        except Exception as e:
            return FetchResult(error=f"Fetch error: {e}")

        if 300 <= status_code < 400 and "location" in {k.lower() for k in headers}:
            location = next(v for k, v in headers.items() if k.lower() == "location")
            current = urljoin(current, location)
            continue
        break
    else:
        return FetchResult(error=f"Too many redirects (>{_MAX_REDIRECTS})")

    fetch_ms = (time.monotonic() - t_start) * 1000
    content_type = headers.get("content-type", "").lower()

    # PDF handling — return raw bytes (already size-capped) for academic extraction.
    if "application/pdf" in content_type:
        return FetchResult(
            html="",
            status_code=status_code,
            content_type=content_type,
            raw_bytes=body,
            bytes_downloaded=len(body),
            latency_ms=round(fetch_ms, 1),
        )

    if "text/html" not in content_type and "application/xhtml" not in content_type:
        return FetchResult(error=f"Not an HTML or PDF page (content-type: {content_type})")

    html = body.decode("utf-8", errors="replace")

    result = FetchResult(
        html=html,
        status_code=status_code,
        content_type=content_type,
        bytes_downloaded=len(html),
        latency_ms=round(fetch_ms, 1),
    )

    # JS rendering: re-fetch with Playwright for full JS execution. The renderer
    # re-validates the URL against the SSRF guard and enforces same-origin policy.
    if js_render and settings.playwright_enabled:
        try:
            from app.services.renderer import get_renderer

            renderer = get_renderer()
            rendered = await renderer.render(url, wait_for_selector)
            if rendered.html and len(rendered.html) > 100:
                result.html = rendered.html
                result.title = rendered.title
                result.latency_ms = rendered.latency_ms
                result.bytes_downloaded = len(rendered.html)
        except Exception as e:
            logger.warning("js_render_failed", url=url, error=str(e))

    return result

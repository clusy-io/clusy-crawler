from __future__ import annotations

import asyncio
import codecs
import ipaddress
import random
import re
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, cast
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
import structlog

from app.config import settings
from app.lib.http_client import get_http_client
from app.services.document_policy import (
    DocumentPolicyDeniedError,
    enforce_document_policy,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.models.responses import ExtractionMetadata
    from app.services.document_policy import DocumentPolicyCallback

logger = structlog.get_logger()

ALLOWED_SCHEMES = frozenset({"http", "https"})

# Redirects are followed manually (see fetch_url) so every hop can be
# re-validated against the SSRF guard; httpx's own auto-follow is disabled per
# request. This caps how many Location hops we chase before giving up.
_MAX_REDIRECTS = 5
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

# Keep every URL handed to DNS, httpx, Chromium, document-policy callbacks, and
# downstream source serializers within the same public request-model budget.
# Redirect Location values are attacker controlled, so this guard is repeated
# after urljoin instead of relying only on the next loop's SSRF validation.
_MAX_FETCH_URL_CHARS = 4096

# Hard ceiling on the DECOMPRESSED response body. httpx's aiter_bytes yields
# content-decoded bytes, so counting them here caps the post-Brotli/gzip size —
# closing the decompression-bomb DoS (an 800-byte body can inflate to >500 MB).
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MB

_PROXY_ENV_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)

_CHARSET_PARAMETER_RE = re.compile(
    r"""charset\s*=\s*(?:"([^"]+)"|'([^']+)'|([^;\s"']+))""",
    re.IGNORECASE,
)
_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_HTML_PREFIX_RE = re.compile(
    r"^(?:"
    r"<!--|<!doctype\s+html(?:\s|>)|"
    r"<(?:html|head|body|title|base|link|meta|style|script|noscript|"
    r"main|article|section|nav|header|footer|address|blockquote|div|"
    r"p|h[1-6]|pre|table|form|fieldset|iframe|a|br)(?:\s|/?>)"
    r")",
    re.IGNORECASE,
)

_HTML_ENCODING_ALIASES = {
    # WHATWG treats these legacy labels as Windows-1252 for HTML.
    "ascii": "windows-1252",
    "iso-8859-1": "windows-1252",
    "iso8859-1": "windows-1252",
    "latin-1": "windows-1252",
    "latin1": "windows-1252",
    "us-ascii": "windows-1252",
}
_SUPPORTED_TEXT_MIMES = frozenset(
    {
        "application/json",
        "application/ld+json",
        "application/toml",
        "application/x-httpd-php",
        "application/x-javascript",
        "application/x-ndjson",
        "application/x-sh",
        "application/x-yaml",
        "application/xml",
        "application/yaml",
        "text/csv",
        "text/markdown",
        "text/plain",
        "text/tab-separated-values",
        "text/x-markdown",
        "text/xml",
    }
)
_NON_TEXT_CODECS = frozenset(
    {
        "base64",
        "bz2",
        "hex",
        "quopri",
        "raw-unicode-escape",
        "rot-13",
        "unicode-escape",
        "uu",
        "zlib",
    }
)


@dataclass
class FetchResult:
    html: str = ""
    error: str | None = None
    status_code: int = 0
    content_type: str = ""
    title: str = ""
    metadata: ExtractionMetadata | None = None
    # End-to-end fetch_url wall time retained for compatibility. The explicit
    # provenance fields below partition actual static-fetch and browser-render
    # work so callers never infer execution from requested js_render intent.
    latency_ms: float = 0.0
    fetch_latency_ms: float = 0.0
    render_latency_ms: float = 0.0
    bytes_downloaded: int = 0  # Content-decoded response-body bytes.
    raw_bytes: bytes | None = None  # For PDF/binary content
    final_url: str = ""
    rendered: bool = False


class _ResponseTooLargeError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        *,
        bytes_read: int = 0,
        declared_bytes: int | None = None,
    ) -> None:
        self.status_code = status_code
        self.bytes_read = bytes_read
        self.declared_bytes = declared_bytes
        size = declared_bytes if declared_bytes is not None else bytes_read
        super().__init__(
            f"Response too large: {size} bytes exceeds {_MAX_RESPONSE_BYTES}-byte limit"
        )


class _UnsafePeerAddressError(RuntimeError):
    pass


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
    # ``is_global`` also rejects shared-address space (100.64/10), benchmarking
    # ranges, and other special-purpose networks that are neither RFC1918
    # ``private`` nor Internet-routable.
    return not addr.is_global


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
    length_error = _url_length_error(url)
    if length_error is not None:
        return length_error
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


def _url_length_error(url: object) -> str | None:
    if type(url) is not str:
        return "URL must be an exact string"
    if len(url) > _MAX_FETCH_URL_CHARS:
        return f"URL exceeds {_MAX_FETCH_URL_CHARS}-character limit"
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


def _header_value(headers: dict[str, str], name: str) -> str:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return ""


def _safe_log_url(url: str) -> str:
    """Return only the URL origin; paths can contain bearer or signed tokens."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return urlunparse((parsed.scheme, host, "", "", "", ""))
    except ValueError:
        return "(invalid URL)"


def _proxy_may_be_in_use() -> bool:
    """Whether the app client could be connected to a proxy, not the origin.

    A proxied response's ``server_addr`` is commonly the proxy address. Treating
    that address as the origin peer would reject perfectly safe requests when a
    local/corporate proxy is used, so peer validation is deliberately skipped in
    that case. The app's client only supports the configured proxy and httpx's
    standard environment proxies.
    """
    return bool(settings.http_proxy)


def _response_peer_error(response: httpx.Response) -> str | None:
    """Return an SSRF error when httpx exposes a non-public direct peer IP."""
    if _proxy_may_be_in_use():
        return None

    stream = response.extensions.get("network_stream")
    get_extra_info = getattr(stream, "get_extra_info", None)
    if not callable(get_extra_info):
        return None

    try:
        peer = cast("Callable[[str], object]", get_extra_info)("server_addr")
    except (OSError, RuntimeError, TypeError, ValueError):
        return None

    if isinstance(peer, (tuple, list)) and peer:
        peer_host = peer[0]
    elif isinstance(peer, str):
        peer_host = peer
    else:
        return None
    if not isinstance(peer_host, str):
        return None

    # Some custom transports expose a hostname or Unix-socket path rather than
    # an IP literal. Only enforce this check when an actual peer IP is available.
    try:
        ipaddress.ip_address(peer_host)
    except ValueError:
        return None
    if _ip_is_blocked(peer_host):
        return f"connected to non-public peer address {peer_host}"
    return None


def validate_response_peer(response: httpx.Response) -> str | None:
    """Return an SSRF error when a direct response exposes an unsafe peer.

    Policy-side fetchers such as ``robots.txt`` must perform the same
    post-connect rebinding check as normal page fetches.  Keeping this tiny
    public wrapper avoids duplicating the proxy-boundary and network-stream
    handling outside this module.
    """

    return _response_peer_error(response)


def _charset_from_content_type(content_type: str) -> str | None:
    match = _CHARSET_PARAMETER_RE.search(content_type)
    if not match:
        return None
    return next((group.strip() for group in match.groups() if group), None)


def _charset_from_meta(body: bytes) -> str | None:
    # HTML encoding declarations must occur near the start of the document.
    # Latin-1 is used only as a one-byte view so the ASCII meta syntax survives
    # regardless of the eventual document encoding.
    prefix = body[:4096].decode("latin-1", errors="ignore").replace("\x00", "")
    for tag_match in _META_TAG_RE.finditer(prefix):
        match = _CHARSET_PARAMETER_RE.search(tag_match.group(0))
        if match:
            return next((group.strip() for group in match.groups() if group), None)
    return None


def _bom_encoding(body: bytes) -> str | None:
    # UTF-32 BOMs start with the same bytes as UTF-16 BOMs, so check them first.
    if body.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return "utf-32"
    if body.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    if body.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    return None


def _normalise_encoding(label: str | None) -> str | None:
    if not label:
        return None
    cleaned = label.strip().lower().replace("_", "-")
    cleaned = _HTML_ENCODING_ALIASES.get(cleaned, cleaned)
    try:
        codec = codecs.lookup(cleaned)
    except LookupError:
        return None
    canonical = codec.name.replace("_", "-")
    if canonical in _NON_TEXT_CODECS:
        return None
    return canonical


def _decode_html(body: bytes, declared_content_type: str) -> str:
    """Decode HTML using BOM, HTTP charset, meta charset, then safe fallbacks."""
    bom = _bom_encoding(body)
    if bom:
        # A Unicode signature is authoritative and these codecs consume it.
        return body.decode(bom, errors="replace")

    candidates = (
        _charset_from_content_type(declared_content_type),
        _charset_from_meta(body),
        "utf-8",
        "windows-1252",
        "latin-1",
    )
    seen: set[str] = set()
    for label in candidates:
        encoding = _normalise_encoding(label)
        if encoding is None or encoding in seen:
            continue
        seen.add(encoding)
        try:
            return body.decode(encoding, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue

    # Latin-1 maps all byte values, so this is defensive rather than reachable.
    return body.decode("latin-1", errors="replace")


def _text_prefix(body: bytes) -> str:
    bom = _bom_encoding(body)
    if bom:
        text = body[:4096].decode(bom, errors="ignore")
    else:
        # Removing NULs lets us recognize BOM-less UTF-16 ASCII markup without
        # trying to guess endianness for the complete document.
        text = body[:4096].decode("latin-1", errors="ignore").replace("\x00", "")
    text = text.lstrip("\ufeff\x00\t\n\r\f ")
    text = re.sub(r"^<\?xml\b[^>]*>\s*", "", text, flags=re.IGNORECASE)
    return text


def _looks_like_pdf(body: bytes) -> bool:
    prefix = body[:1024]
    marker = prefix.find(b"%PDF-")
    return marker >= 0 and not prefix[:marker].strip(b"\x00\t\n\r\f ")


def _looks_like_html(body: bytes) -> bool:
    return bool(_HTML_PREFIX_RE.match(_text_prefix(body)))


def _effective_content_type(declared_content_type: str, body: bytes) -> str | None:
    declared = declared_content_type.strip().lower()
    mime = declared.partition(";")[0].strip()
    declared_html = mime in {"text/html", "application/xhtml+xml"}
    declared_pdf = mime == "application/pdf"
    declared_text = mime in _SUPPORTED_TEXT_MIMES or (
        mime.startswith("text/")
        and mime not in {"text/event-stream", "text/html"}
    )

    # Strong body signatures correct missing and incorrectly labelled headers.
    if _looks_like_pdf(body):
        return declared if declared_pdf else "application/pdf"
    if _looks_like_html(body):
        return declared if declared_html else "text/html"

    # Some valid HTML fragments do not contain a document-level signature, and
    # not every PDF producer places its marker predictably. Trust an explicit,
    # supported media type when sniffing is inconclusive.
    if declared_html or declared_pdf:
        return declared
    if declared_text and not _looks_binary(body):
        return declared
    return None


def _looks_binary(body: bytes) -> bool:
    sample = body[:8192]
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    control = sum(
        byte < 9 or (13 < byte < 32)
        for byte in sample
    )
    return control / len(sample) > 0.05


def _url_looks_like_pdf(url: str) -> bool:
    """Avoid sending obvious PDF downloads through Chromium's PDF viewer."""
    return urlparse(url).path.lower().rstrip("/").endswith(".pdf")


async def _try_direct_render(
    url: str,
    wait_for_selector: str | None,
    document_policy: DocumentPolicyCallback | None = None,
) -> FetchResult | None:
    """Render an HTML URL without first downloading it with httpx.

    Returns ``None`` when browser navigation failed or the response was not
    HTML, allowing the caller to fall back to the bounded static fetch path.
    """
    try:
        from app.services.rendering.manager import get_render_manager

        if document_policy is None:
            rendered = await get_render_manager().render(url, wait_for_selector)
        else:
            await enforce_document_policy(document_policy, url)
            rendered = await get_render_manager().render(
                url,
                wait_for_selector,
                document_policy=document_policy,
            )
        if document_policy is not None and rendered.final_url:
            # The renderer route gate checks before every real document
            # request. This postcondition also protects custom renderer
            # adapters that return a different final URL.
            await enforce_document_policy(document_policy, rendered.final_url)
    except DocumentPolicyDeniedError:
        raise
    except Exception as e:
        logger.warning(
            "js_render_failed",
            url=_safe_log_url(url),
            error_type=type(e).__name__,
        )
        return None

    final_url = rendered.final_url or url
    final_url_error = _url_length_error(final_url)
    if final_url_error is not None:
        return FetchResult(
            error=final_url_error,
            status_code=rendered.status_code,
            latency_ms=rendered.latency_ms,
            render_latency_ms=rendered.latency_ms,
            final_url=final_url,
            rendered=True,
        )

    content_type = rendered.content_type.strip().lower()
    mime = content_type.partition(";")[0]
    if (
        not rendered.rendered
        or not 200 <= rendered.status_code < 300
        or len(rendered.html) <= 100
        or (mime and mime not in {"text/html", "application/xhtml+xml"})
    ):
        logger.info(
            "js_render_falling_back_to_static",
            url=_safe_log_url(url),
            status_code=rendered.status_code,
            content_type=content_type,
        )
        return None

    downloaded_bytes = len(rendered.html.encode("utf-8"))
    if downloaded_bytes > _MAX_RESPONSE_BYTES:
        return FetchResult(
            error=(
                f"Response too large: {downloaded_bytes} bytes exceeds "
                f"{_MAX_RESPONSE_BYTES}-byte limit"
            ),
            status_code=rendered.status_code,
            content_type=content_type or "text/html",
            latency_ms=rendered.latency_ms,
            render_latency_ms=rendered.latency_ms,
            bytes_downloaded=downloaded_bytes,
            final_url=final_url,
            rendered=True,
        )

    return FetchResult(
        html=rendered.html,
        status_code=rendered.status_code,
        content_type=content_type or "text/html",
        title=rendered.title,
        latency_ms=rendered.latency_ms,
        render_latency_ms=rendered.latency_ms,
        bytes_downloaded=downloaded_bytes,
        final_url=final_url,
        rendered=True,
    )


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


async def _stream_one(url: str, client: httpx.AsyncClient) -> tuple[int, dict[str, str], bytes]:
    """Fetch a single URL (no auto-redirect) and return status, headers, body.

    Streams the response and fails explicitly if the decompressed body exceeds
    ``_MAX_RESPONSE_BYTES``. Redirects are NOT followed here — the caller
    re-validates the Location and loops.
    """
    chunks: list[bytes] = []
    total = 0
    async with client.stream("GET", url, follow_redirects=False) as resp:
        headers = dict(resp.headers)

        peer_error = _response_peer_error(resp)
        if peer_error:
            raise _UnsafePeerAddressError(peer_error)

        # Redirect bodies are irrelevant and can legitimately have surprising
        # Content-Length values. The caller only needs status + Location.
        if resp.is_redirect:
            return resp.status_code, headers, b""

        # Error bodies are never extraction input and can be arbitrarily large.
        # Retry/status handling only needs headers (notably Retry-After).
        if resp.status_code >= 400:
            return resp.status_code, headers, b""

        # Reject obviously-oversized bodies before downloading them.
        declared = resp.headers.get("content-length")
        if declared is not None:
            try:
                declared_bytes = int(declared)
                if declared_bytes > _MAX_RESPONSE_BYTES:
                    raise _ResponseTooLargeError(
                        resp.status_code,
                        declared_bytes=declared_bytes,
                    )
            except ValueError:
                pass

        async for chunk in resp.aiter_bytes():
            observed = total + len(chunk)
            if observed > _MAX_RESPONSE_BYTES:
                raise _ResponseTooLargeError(
                    resp.status_code,
                    bytes_read=observed,
                )
            chunks.append(chunk)
            total = observed
        return resp.status_code, headers, b"".join(chunks)


def _retry_delay_seconds(headers: dict[str, str], attempt: int) -> float:
    """Compute a bounded Retry-After or full-jitter exponential delay."""
    maximum = settings.http_retry_max_delay_s
    retry_after = _header_value(headers, "retry-after").strip()
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), maximum)
        except ValueError:
            try:
                parsed = parsedate_to_datetime(retry_after)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                remaining = (parsed - datetime.now(UTC)).total_seconds()
                return min(max(remaining, 0.0), maximum)
            except (OverflowError, TypeError, ValueError):
                pass
    ceiling = min(0.25 * (2 ** max(attempt - 1, 0)), maximum)
    return random.uniform(0.0, ceiling)


async def _stream_with_retries(
    url: str,
    client: httpx.AsyncClient,
    deadline: float,
) -> tuple[int, dict[str, str], bytes]:
    """Fetch one redirect hop within a shared wall-clock budget."""
    from app.services.rate_limiter import get_rate_limiter

    last_error: httpx.TransportError | None = None
    for attempt in range(1, settings.http_max_attempts + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise httpx.TimeoutException("total fetch deadline exceeded")

        try:
            async with asyncio.timeout(remaining):
                await get_rate_limiter().acquire(url)
                status, headers, body = await _stream_one(url, client)
        except TimeoutError as exc:
            raise httpx.TimeoutException("total fetch deadline exceeded") from exc
        except httpx.TransportError as exc:
            last_error = exc
            if attempt >= settings.http_max_attempts:
                raise
            delay = _retry_delay_seconds({}, attempt)
            logger.info(
                "http_fetch_retry",
                url=_safe_log_url(url),
                attempt=attempt,
                error_type=type(exc).__name__,
                delay_s=round(delay, 3),
            )
        else:
            if status not in _RETRYABLE_STATUSES or attempt >= settings.http_max_attempts:
                return status, headers, body
            delay = _retry_delay_seconds(headers, attempt)
            logger.info(
                "http_status_retry",
                url=_safe_log_url(url),
                status_code=status,
                attempt=attempt,
                delay_s=round(delay, 3),
            )

        remaining = deadline - time.monotonic()
        if delay >= remaining:
            raise httpx.TimeoutException("total fetch deadline exceeded")
        if delay:
            await asyncio.sleep(delay)

    if last_error is not None:
        raise last_error
    raise httpx.TimeoutException("fetch attempts exhausted")


async def fetch_url(
    url: str,
    js_render: bool = False,
    wait_for_selector: str | None = None,
    document_policy: DocumentPolicyCallback | None = None,
) -> FetchResult:
    t_start = time.monotonic()
    deadline = t_start + settings.http_total_timeout_s
    render_elapsed_ms = 0.0
    static_started: float | None = None

    def finish(result: FetchResult) -> FetchResult:
        """Attach mutually exclusive actual fetch/render wall-time provenance."""
        fetch_elapsed_ms = (
            0.0
            if static_started is None
            else max(0.0, (time.monotonic() - static_started) * 1000)
        )
        result.fetch_latency_ms = round(fetch_elapsed_ms, 3)
        result.render_latency_ms = round(render_elapsed_ms, 3)
        result.latency_ms = round(fetch_elapsed_ms + render_elapsed_ms, 1)
        return result

    initial_url_error = _url_length_error(url)
    if initial_url_error is not None:
        return finish(FetchResult(error=initial_url_error, final_url=url))

    # Forced/explicit JS requests go straight to Chromium. Previously they
    # downloaded every page once with httpx and then fetched it again in the
    # browser; conditional escalation downloaded it three times. Obvious PDFs
    # still use httpx because the crawler needs the original binary bytes.
    if (
        js_render
        and settings.playwright_enabled
        and settings.playwright_java_script_enabled
        and not _url_looks_like_pdf(url)
    ):
        render_started = time.monotonic()
        try:
            direct_result = await _try_direct_render(
                url,
                wait_for_selector,
                document_policy,
            )
        finally:
            render_elapsed_ms += (
                time.monotonic() - render_started
            ) * 1000
        if direct_result is not None:
            return finish(direct_result)

    static_started = time.monotonic()
    client = get_http_client()
    current = url
    status_code = 0
    headers: dict[str, str] = {}
    body = b""

    for _hop in range(_MAX_REDIRECTS + 1):
        err = await validate_public_url(current)
        if err:
            return finish(
                FetchResult(error=f"SSRF blocked: {err}", final_url=current)
            )
        if document_policy is not None:
            await enforce_document_policy(document_policy, current)
        try:
            status_code, headers, body = await _stream_with_retries(
                current,
                client,
                deadline,
            )
        except _UnsafePeerAddressError as e:
            return finish(
                FetchResult(
                    error=f"SSRF blocked: {e}",
                    final_url=current,
                )
            )
        except _ResponseTooLargeError as e:
            return finish(
                FetchResult(
                    error=str(e),
                    status_code=e.status_code,
                    bytes_downloaded=e.bytes_read,
                    final_url=current,
                )
            )
        except httpx.TimeoutException:
            return finish(
                FetchResult(error="Request timed out", final_url=current)
            )
        except httpx.ConnectError:
            return finish(
                FetchResult(error="Connection failed", final_url=current)
            )
        except httpx.TransportError as e:
            logger.warning(
                "http_transport_failed",
                url=_safe_log_url(current),
                error_type=type(e).__name__,
            )
            return finish(
                FetchResult(error="Network request failed", final_url=current)
            )
        except Exception as e:
            logger.warning(
                "http_fetch_failed",
                url=_safe_log_url(current),
                error_type=type(e).__name__,
            )
            return finish(FetchResult(error="Fetch failed", final_url=current))

        location = _header_value(headers, "location")
        if 300 <= status_code < 400 and location:
            current = urljoin(current, location)
            redirect_url_error = _url_length_error(current)
            if redirect_url_error is not None:
                return finish(
                    FetchResult(
                        error=redirect_url_error,
                        status_code=status_code,
                        final_url=current,
                    )
                )
            continue
        break
    else:
        return finish(
            FetchResult(
                error=f"Too many redirects (>{_MAX_REDIRECTS})",
                status_code=status_code,
                final_url=current,
            )
        )

    if not 200 <= status_code < 300:
        return finish(
            FetchResult(
                error=f"HTTP {status_code}",
                status_code=status_code,
                content_type=_header_value(
                    headers,
                    "content-type",
                ).strip().lower(),
                bytes_downloaded=len(body),
                final_url=current,
            )
        )

    declared_content_type = _header_value(headers, "content-type")
    content_type = _effective_content_type(declared_content_type, body)
    downloaded_bytes = len(body)

    if content_type is None:
        shown_type = declared_content_type.strip().lower() or "(missing)"
        return finish(
            FetchResult(
                error=(
                    "Not a supported HTML, text, or PDF page "
                    f"(content-type: {shown_type})"
                ),
                status_code=status_code,
                content_type=declared_content_type.strip().lower(),
                bytes_downloaded=downloaded_bytes,
                final_url=current,
            )
        )

    # PDF handling — return raw bytes (already size-capped) for academic extraction.
    if content_type.partition(";")[0] == "application/pdf":
        return finish(
            FetchResult(
                html="",
                status_code=status_code,
                content_type=content_type,
                raw_bytes=body,
                bytes_downloaded=downloaded_bytes,
                final_url=current,
            )
        )

    html = _decode_html(body, declared_content_type)

    return finish(
        FetchResult(
            html=html,
            status_code=status_code,
            content_type=content_type,
            bytes_downloaded=downloaded_bytes,
            final_url=current,
        )
    )

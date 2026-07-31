from __future__ import annotations

import httpx

from app.config import settings

_client: httpx.AsyncClient | None = None


def _build_limits() -> httpx.Limits:
    return httpx.Limits(
        max_keepalive_connections=settings.http_max_keepalive_connections,
        max_connections=settings.http_max_connections,
        keepalive_expiry=30.0,
    )


def get_http_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.http_connect_timeout_s,
                read=settings.http_timeout_s,
                write=10.0,
                pool=5.0,
            ),
            proxy=settings.http_proxy or None,
            limits=_build_limits(),
            headers={
                "User-Agent": settings.http_user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                # Accept-Encoding is intentionally NOT set here: httpx advertises
                # exactly the codecs it can decode (gzip/deflate + br/zstd when the
                # brotli/zstandard packages are installed). Hardcoding `br` while the
                # decoder is absent yields undecodable bytes on Brotli-serving sites.
                "Accept-Language": "en-US,en;q=0.9",
            },
            follow_redirects=True,
            http2=True,
            # Environment proxies are an implicit change in the network trust
            # boundary. Only the explicitly validated HTTP_PROXY setting is
            # honoured.
            trust_env=False,
        )
    return _client


async def aclose_http_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None

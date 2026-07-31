from __future__ import annotations

import asyncio

import httpx
import pytest

from app.middleware.resource_limits import ResourceLimitMiddleware


async def _ok_app(scope, receive, send):
    message = await receive()
    body = message.get("body", b"")
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


@pytest.mark.anyio
async def test_declared_oversized_request_is_rejected():
    app = ResourceLimitMiddleware(
        _ok_app,
        max_body_bytes=4,
        max_active_requests=1,
        request_timeout_s=1,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post("/", content=b"12345")

    assert response.status_code == 413


@pytest.mark.anyio
async def test_chunked_oversized_request_is_rejected():
    app = ResourceLimitMiddleware(
        _ok_app,
        max_body_bytes=4,
        max_active_requests=1,
        request_timeout_s=1,
    )

    async def chunks():
        yield b"123"
        yield b"45"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post("/", content=chunks())

    assert response.status_code == 413


@pytest.mark.anyio
async def test_admission_limit_returns_429_instead_of_unbounded_queue():
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_app(scope, receive, send):
        await receive()
        started.set()
        await release.wait()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-length", b"0")],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    app = ResourceLimitMiddleware(
        blocking_app,
        max_body_bytes=32,
        max_active_requests=1,
        request_timeout_s=2,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        first = asyncio.create_task(client.post("/", content=b"first"))
        await started.wait()
        second = await client.post("/", content=b"second")
        release.set()
        first_response = await first

    assert first_response.status_code == 200
    assert second.status_code == 429
    assert second.headers["retry-after"] == "1"


@pytest.mark.anyio
async def test_request_deadline_returns_504():
    never = asyncio.Event()

    async def hung_app(scope, receive, send):
        await receive()
        await never.wait()

    app = ResourceLimitMiddleware(
        hung_app,
        max_body_bytes=32,
        max_active_requests=1,
        request_timeout_s=0.01,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post("/", content=b"hello")

    assert response.status_code == 504


@pytest.mark.anyio
async def test_request_deadline_includes_slow_chunked_body_and_releases_admission():
    app = ResourceLimitMiddleware(
        _ok_app,
        max_body_bytes=32,
        max_active_requests=1,
        request_timeout_s=0.01,
    )

    async def stalled_chunks():
        yield b"partial"
        await asyncio.Event().wait()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await asyncio.wait_for(
            client.post("/", content=stalled_chunks()),
            timeout=0.2,
        )
        next_response = await client.post("/", content=b"next")

    assert response.status_code == 504
    assert next_response.status_code == 200
    assert next_response.content == b"next"

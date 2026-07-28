from __future__ import annotations

import asyncio

import pytest

from app.main import app, lifespan
from app.middleware.auth import AuthMiddleware
from app.middleware.resource_limits import ResourceLimitMiddleware


def test_auth_middleware_is_outside_resource_admission():
    middleware_classes = [middleware.cls for middleware in app.user_middleware]

    assert middleware_classes.index(AuthMiddleware) < middleware_classes.index(
        ResourceLimitMiddleware
    )


@pytest.mark.anyio
async def test_lifespan_closes_browser_cache_and_http(monkeypatch):
    calls: list[str] = []
    drain_started = asyncio.Event()
    allow_drain = asyncio.Event()
    drain_completed = False

    def require_drained(name: str) -> None:
        assert drain_completed, f"{name} closed before crawler work drained"
        calls.append(name)

    async def stop_renderer():
        require_drained("renderer")

    class FakeCache:
        async def close(self):
            require_drained("cache")

    async def close_http():
        require_drained("http")

    async def close_structured():
        require_drained("structured")

    async def close_quality():
        require_drained("quality")

    def start_crawler():
        calls.append("crawler_start")

    async def shutdown_crawler():
        nonlocal drain_completed
        calls.append("crawler_stop_started")
        drain_started.set()
        await allow_drain.wait()
        drain_completed = True
        calls.append("crawler_stop_completed")

    monkeypatch.setattr("app.main.stop_renderer", stop_renderer)
    monkeypatch.setattr("app.main.close_quality_extractor", close_quality)
    monkeypatch.setattr("app.main.close_structured_client", close_structured)
    monkeypatch.setattr("app.main.start_crawler", start_crawler)
    monkeypatch.setattr("app.main.shutdown_crawler", shutdown_crawler)
    monkeypatch.setattr("app.main.get_cache", FakeCache)
    monkeypatch.setattr("app.main.aclose_http_client", close_http)

    manager = lifespan(app)
    await manager.__aenter__()
    close_task = asyncio.create_task(manager.__aexit__(None, None, None))
    await asyncio.wait_for(drain_started.wait(), timeout=1)

    assert calls == ["crawler_start", "crawler_stop_started"]

    allow_drain.set()
    await asyncio.wait_for(close_task, timeout=1)

    assert calls[:3] == [
        "crawler_start",
        "crawler_stop_started",
        "crawler_stop_completed",
    ]
    assert sorted(calls[3:]) == [
        "cache",
        "http",
        "quality",
        "renderer",
        "structured",
    ]

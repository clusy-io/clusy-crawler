from __future__ import annotations

import pytest

from app.services.rendering.contract import (
    RenderBackendDisabledError,
    RenderResult,
)
from app.services.rendering.manager import RenderManager


@pytest.mark.anyio
async def test_disabled_manager_does_not_construct_backend() -> None:
    factory_calls = 0

    def backend_factory():
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("disabled rendering must not construct a backend")

    manager = RenderManager(
        enabled=lambda: False,
        backend_factory=backend_factory,
    )

    await manager.start()

    assert manager.kind == "disabled"
    assert manager.enabled is False
    assert manager.is_ready() is False
    assert factory_calls == 0
    with pytest.raises(
        RenderBackendDisabledError,
        match="browser rendering is disabled",
    ):
        await manager.render("https://example.com/")

    await manager.aclose()
    assert factory_calls == 0


@pytest.mark.anyio
async def test_local_manager_delegates_contract_and_lifecycle() -> None:
    expected = RenderResult(
        html="<html>rendered bytes</html>",
        title="Rendered",
        rendered=True,
        latency_ms=12.5,
        status_code=200,
        content_type="text/html; charset=utf-8",
        final_url="https://example.com/final",
    )
    events: list[object] = []
    ready = False

    async def document_policy(_url: str):
        return object()

    class FakeBackend:
        kind = "local"

        async def start(self) -> None:
            nonlocal ready
            events.append("start")
            ready = True

        async def render(
            self,
            url,
            wait_for_selector=None,
            timeout_ms=None,
            *,
            document_policy=None,
        ):
            events.append(
                (
                    "render",
                    url,
                    wait_for_selector,
                    timeout_ms,
                    document_policy,
                )
            )
            return expected

        def is_ready(self) -> bool:
            return ready

        async def aclose(self) -> None:
            nonlocal ready
            events.append("close")
            ready = False

    manager = RenderManager(
        enabled=lambda: True,
        backend_factory=FakeBackend,
    )

    assert manager.kind == "local"
    assert manager.is_ready() is False

    await manager.start()
    actual = await manager.render(
        "https://example.com/start",
        "#ready",
        1_500,
        document_policy=document_policy,
    )

    assert manager.is_ready() is True
    assert actual is expected
    assert events == [
        "start",
        (
            "render",
            "https://example.com/start",
            "#ready",
            1_500,
            document_policy,
        ),
    ]

    await manager.aclose()
    assert manager.is_ready() is False
    assert events[-1] == "close"

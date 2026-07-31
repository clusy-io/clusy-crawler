from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.rendering.contract import (
        DocumentPolicyCallback,
        RenderBackendKind,
        RenderResult,
    )


class LocalRenderBackend:
    """Adapter around the existing process-local Playwright renderer."""

    kind: RenderBackendKind = "local"

    async def start(self) -> None:
        from app.services import renderer

        await renderer.start_renderer()

    async def render(
        self,
        url: str,
        wait_for_selector: str | None = None,
        timeout_ms: int | None = None,
        *,
        document_policy: DocumentPolicyCallback | None = None,
    ) -> RenderResult:
        from app.services import renderer

        local_renderer = renderer.get_renderer()
        if timeout_ms is None and document_policy is None:
            return await local_renderer.render(url, wait_for_selector)
        if timeout_ms is None:
            return await local_renderer.render(
                url,
                wait_for_selector,
                document_policy=document_policy,
            )
        if document_policy is None:
            return await local_renderer.render(
                url,
                wait_for_selector,
                timeout_ms,
            )
        return await local_renderer.render(
            url,
            wait_for_selector,
            timeout_ms,
            document_policy=document_policy,
        )

    def is_ready(self) -> bool:
        from app.services import renderer

        return renderer.renderer_is_ready()

    async def aclose(self) -> None:
        from app.services import renderer

        await renderer.stop_renderer()

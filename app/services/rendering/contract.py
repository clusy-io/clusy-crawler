from __future__ import annotations

from typing import Literal, Protocol

from app.services.document_policy import (  # noqa: TC001
    DocumentPolicyCallback as DocumentPolicyCallback,
)

RenderBackendKind = Literal["disabled", "local"]


class RenderBackendDisabledError(RuntimeError):
    """Raised when rendering is requested while its backend is disabled."""


class RenderResult:
    """Backend-neutral rendering result.

    This intentionally retains the original mutable, slotted value semantics
    used by the local renderer.
    """

    __slots__ = (
        "html",
        "title",
        "rendered",
        "latency_ms",
        "status_code",
        "content_type",
        "final_url",
    )

    def __init__(
        self,
        html: str = "",
        title: str = "",
        rendered: bool = False,
        latency_ms: float = 0.0,
        status_code: int = 0,
        content_type: str = "",
        final_url: str = "",
    ) -> None:
        self.html = html
        self.title = title
        self.rendered = rendered
        self.latency_ms = latency_ms
        self.status_code = status_code
        self.content_type = content_type
        self.final_url = final_url


class RenderBackend(Protocol):
    """Contract implemented by concrete rendering backends."""

    kind: RenderBackendKind

    async def start(self) -> None: ...

    async def render(
        self,
        url: str,
        wait_for_selector: str | None = None,
        timeout_ms: int | None = None,
        *,
        document_policy: DocumentPolicyCallback | None = None,
    ) -> RenderResult: ...

    def is_ready(self) -> bool: ...

    async def aclose(self) -> None: ...

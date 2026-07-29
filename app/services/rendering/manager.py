from __future__ import annotations

from collections.abc import Callable

from app.config import settings
from app.services.rendering.contract import (
    DocumentPolicyCallback,
    RenderBackend,
    RenderBackendDisabledError,
    RenderBackendKind,
    RenderResult,
)

BackendFactory = Callable[[], RenderBackend]


def _local_backend_factory() -> RenderBackend:
    from app.services.rendering.local import LocalRenderBackend

    return LocalRenderBackend()


class RenderManager:
    """Owns backend selection and lifecycle without changing render semantics."""

    def __init__(
        self,
        *,
        enabled: Callable[[], bool] | None = None,
        backend_factory: BackendFactory = _local_backend_factory,
    ) -> None:
        self._enabled = enabled or (lambda: settings.playwright_enabled)
        self._backend_factory = backend_factory
        self._backend: RenderBackend | None = None

    @property
    def kind(self) -> RenderBackendKind:
        return "local" if self._enabled() else "disabled"

    @property
    def enabled(self) -> bool:
        return self._enabled()

    def _get_backend(self) -> RenderBackend:
        if not self.enabled:
            raise RenderBackendDisabledError("browser rendering is disabled")
        if self._backend is None:
            self._backend = self._backend_factory()
        return self._backend

    async def start(self) -> None:
        if self.enabled:
            await self._get_backend().start()

    async def render(
        self,
        url: str,
        wait_for_selector: str | None = None,
        timeout_ms: int | None = None,
        *,
        document_policy: DocumentPolicyCallback | None = None,
    ) -> RenderResult:
        return await self._get_backend().render(
            url,
            wait_for_selector,
            timeout_ms,
            document_policy=document_policy,
        )

    def is_ready(self) -> bool:
        if not self.enabled:
            return False
        return self._get_backend().is_ready()

    async def aclose(self) -> None:
        backend = self._backend
        self._backend = None
        if backend is not None:
            await backend.aclose()


_manager = RenderManager()


def get_render_manager() -> RenderManager:
    return _manager


async def start_render_manager() -> None:
    await _manager.start()


def render_manager_is_ready() -> bool:
    return _manager.is_ready()


async def stop_render_manager() -> None:
    await _manager.aclose()

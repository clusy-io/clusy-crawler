"""Backend-neutral browser rendering interfaces."""

from app.services.rendering.contract import (
    DocumentPolicyCallback,
    RenderBackend,
    RenderBackendDisabledError,
    RenderBackendKind,
    RenderResult,
)

__all__ = [
    "DocumentPolicyCallback",
    "RenderBackend",
    "RenderBackendDisabledError",
    "RenderBackendKind",
    "RenderResult",
]

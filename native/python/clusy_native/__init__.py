"""Native extraction primitives used by :mod:`app.services.extractor`."""

from ._native import (
    NativeDocumentBlocks,
    NativeExtraction,
    NativeSemanticBlock,
    backend_version,
    extract_document_blocks,
    extract_html,
)
from .document_ir import (
    DEFAULT_DOCUMENT_IR_LIMITS,
    DocumentIRLimits,
    extract_document_ir,
)

__all__ = [
    "DEFAULT_DOCUMENT_IR_LIMITS",
    "DocumentIRLimits",
    "NativeDocumentBlocks",
    "NativeExtraction",
    "NativeSemanticBlock",
    "backend_version",
    "extract_document_blocks",
    "extract_document_ir",
    "extract_html",
]

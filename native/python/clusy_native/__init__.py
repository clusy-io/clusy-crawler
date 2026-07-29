"""Native extraction primitives used by :mod:`app.services.extractor`."""

from ._native import (
    NativeDocumentBlocks,
    NativeDocumentIRV2,
    NativeExtraction,
    NativeIRElementV2,
    NativeIRListItemV2,
    NativeIRListV2,
    NativeIRMathV2,
    NativeIRSerializationV2,
    NativeIRTableCellV2,
    NativeIRTableV2,
    NativeIRTextRunV2,
    NativeSemanticBlock,
    backend_version,
    extract_document_blocks,
    extract_document_ir_v2_native,
    extract_html,
)
from .document_ir import (
    DEFAULT_DOCUMENT_IR_LIMITS,
    DocumentIRLimits,
    extract_document_ir,
)
from .document_ir_v2 import (
    DEFAULT_DOCUMENT_IR_V2_LIMITS,
    DocumentIRV2Limits,
    extract_document_ir_v2,
    reconstruct_document_ir_v2,
)

__all__ = [
    "DEFAULT_DOCUMENT_IR_LIMITS",
    "DEFAULT_DOCUMENT_IR_V2_LIMITS",
    "DocumentIRLimits",
    "DocumentIRV2Limits",
    "NativeDocumentBlocks",
    "NativeDocumentIRV2",
    "NativeExtraction",
    "NativeIRElementV2",
    "NativeIRListItemV2",
    "NativeIRListV2",
    "NativeIRMathV2",
    "NativeIRSerializationV2",
    "NativeSemanticBlock",
    "NativeIRTableCellV2",
    "NativeIRTableV2",
    "NativeIRTextRunV2",
    "backend_version",
    "extract_document_blocks",
    "extract_document_ir",
    "extract_document_ir_v2",
    "extract_document_ir_v2_native",
    "extract_html",
    "reconstruct_document_ir_v2",
]

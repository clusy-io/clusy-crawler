"""Typed façade for the source-backed ordered DOM IR v2.

V2 is additive: callers that need the benchmark-pinned v1 block interface can
continue to use :func:`extract_document_ir`. V2 exposes a complete ordered
element/text graph and typed structural relations for selectors that must
reconstruct content without asking a model to generate Markdown.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from ._native import (
    NativeDocumentIRV2,
    NativeIRSerializationV2,
    extract_document_ir_v2_native,
)


@dataclass(frozen=True, slots=True)
class DocumentIRV2Limits:
    """Caller-selectable resource limits, each capped again in native code."""

    max_input_bytes: int = 4 * 1024 * 1024
    max_nodes: int = 200_000
    max_elements: int = 100_000
    max_text_runs: int = 200_000
    max_depth: int = 256
    max_text_run_bytes: int = 256 * 1024
    max_total_text_bytes: int = 8 * 1024 * 1024
    max_math_bytes: int = 256 * 1024  # Total retained math source per document.
    max_table_columns: int = 1_024


DEFAULT_DOCUMENT_IR_V2_LIMITS = DocumentIRV2Limits()


def extract_document_ir_v2(
    html: str,
    *,
    limits: DocumentIRV2Limits = DEFAULT_DOCUMENT_IR_V2_LIMITS,
) -> NativeDocumentIRV2:
    """Parse *html* into the bounded source-backed v2 graph without the GIL."""

    return extract_document_ir_v2_native(
        html,
        max_input_bytes=limits.max_input_bytes,
        max_nodes=limits.max_nodes,
        max_elements=limits.max_elements,
        max_text_runs=limits.max_text_runs,
        max_depth=limits.max_depth,
        max_text_run_bytes=limits.max_text_run_bytes,
        max_total_text_bytes=limits.max_total_text_bytes,
        max_math_bytes=limits.max_math_bytes,
        max_table_columns=limits.max_table_columns,
    )


def reconstruct_document_ir_v2(
    document: NativeDocumentIRV2,
    *,
    selected_ids: Iterable[str] | None = None,
) -> NativeIRSerializationV2:
    """Deterministically serialize all content or an ID-selected subgraph.

    Selecting an element includes its retained subtree. Selecting a text run
    retains the minimum ancestor structure required to serialize that run.
    Unknown IDs are returned in ``missing_ids`` and never broaden selection.
    """

    selection = None if selected_ids is None else list(selected_ids)
    return document.reconstruct(selection)

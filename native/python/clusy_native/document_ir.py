"""Typed Python façade for the bounded native ordered-DOM IR.

This interface is deliberately independent from :func:`extract_html`. It is a
foundation for later block classification and structure-preserving
reconstruction, not a new extraction strategy.
"""

from dataclasses import dataclass

from ._native import NativeDocumentBlocks, extract_document_blocks


@dataclass(frozen=True, slots=True)
class DocumentIRLimits:
    """Caller-selectable limits, each subject to a native hard ceiling."""

    max_input_bytes: int = 4 * 1024 * 1024
    max_nodes: int = 100_000
    max_blocks: int = 4_096
    max_depth: int = 128
    max_block_text_bytes: int = 32 * 1024
    max_block_html_bytes: int = 64 * 1024
    max_total_text_bytes: int = 4 * 1024 * 1024
    max_total_html_bytes: int = 8 * 1024 * 1024


DEFAULT_DOCUMENT_IR_LIMITS = DocumentIRLimits()


def extract_document_ir(
    html: str,
    *,
    limits: DocumentIRLimits = DEFAULT_DOCUMENT_IR_LIMITS,
) -> NativeDocumentBlocks:
    """Parse *html* into bounded semantic blocks without holding the GIL."""

    return extract_document_blocks(
        html,
        max_input_bytes=limits.max_input_bytes,
        max_nodes=limits.max_nodes,
        max_blocks=limits.max_blocks,
        max_depth=limits.max_depth,
        max_block_text_bytes=limits.max_block_text_bytes,
        max_block_html_bytes=limits.max_block_html_bytes,
        max_total_text_bytes=limits.max_total_text_bytes,
        max_total_html_bytes=limits.max_total_html_bytes,
    )

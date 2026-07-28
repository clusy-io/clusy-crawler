from typing import Final

class NativeExtraction:
    text: Final[str]
    plain_text: Final[str]
    article_text: Final[str]
    title: Final[str]
    description: Final[str]
    language: Final[str]
    page_type: Final[str]
    word_count: Final[int]
    confidence: Final[float]
    strategy: Final[str]

class NativeSemanticBlock:
    id: Final[str]
    order: Final[int]
    parent_id: Final[str | None]
    tag: Final[str]
    role: Final[str]
    atomic: Final[bool]
    selectable: Final[bool]
    preserve_whitespace: Final[bool]
    text: Final[str]
    outer_html: Final[str]
    depth: Final[int]
    word_count: Final[int]
    text_bytes: Final[int]
    html_bytes: Final[int]
    link_count: Final[int]
    link_text_bytes: Final[int]
    descendant_element_count: Final[int]
    text_density: Final[float]
    link_density: Final[float]
    text_truncated: Final[bool]
    html_truncated: Final[bool]
    features_truncated: Final[bool]

class NativeDocumentBlocks:
    blocks: Final[list[NativeSemanticBlock]]
    block_count: Final[int]
    schema_version: Final[str]
    input_bytes: Final[int]
    parsed_bytes: Final[int]
    node_count: Final[int]
    removed_node_count: Final[int]
    parse_error_count: Final[int]
    stored_text_bytes: Final[int]
    stored_html_bytes: Final[int]
    input_truncated: Final[bool]
    nodes_truncated: Final[bool]
    depth_truncated: Final[bool]
    blocks_truncated: Final[bool]
    text_truncated_blocks: Final[int]
    html_truncated_blocks: Final[int]
    features_truncated_blocks: Final[int]
    truncated: Final[bool]
    truncation_reasons: Final[list[str]]
    max_input_bytes: Final[int]
    max_nodes: Final[int]
    max_blocks: Final[int]
    max_depth: Final[int]
    max_block_text_bytes: Final[int]
    max_block_html_bytes: Final[int]
    max_total_text_bytes: Final[int]
    max_total_html_bytes: Final[int]

def extract_html(html: str, url: str = "", article_body: bool = False) -> NativeExtraction: ...
def extract_document_blocks(
    html: str,
    max_input_bytes: int = 4 * 1024 * 1024,
    max_nodes: int = 100_000,
    max_blocks: int = 4_096,
    max_depth: int = 128,
    max_block_text_bytes: int = 32 * 1024,
    max_block_html_bytes: int = 64 * 1024,
    max_total_text_bytes: int = 4 * 1024 * 1024,
    max_total_html_bytes: int = 8 * 1024 * 1024,
) -> NativeDocumentBlocks: ...
def backend_version() -> str: ...

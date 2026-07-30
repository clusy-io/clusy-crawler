from typing import Final, Literal

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

class NativeIRElementV2:
    id: Final[str]
    order: Final[int]
    parent_id: Final[str | None]
    child_ids: Final[list[str]]
    text_run_ids: Final[list[str]]
    tag: Final[str]
    role: Final[str]
    path: Final[str]
    depth: Final[int]
    block: Final[bool]
    preserve_whitespace: Final[bool]
    implicit: Final[bool]
    source_start: Final[int | None]
    source_start_tag_end: Final[int | None]
    source_end: Final[int | None]
    source_span_reliable: Final[bool]
    heading_level: Final[int | None]
    href: Final[str | None]
    src: Final[str | None]
    alt: Final[str | None]
    language: Final[str | None]

class NativeIRTextRunV2:
    id: Final[str]
    order: Final[int]
    parent_id: Final[str]
    path: Final[str]
    text: Final[str]
    preserve_whitespace: Final[bool]
    original_bytes: Final[int]
    stored_bytes: Final[int]
    truncated: Final[bool]
    source_start: Final[int | None]
    source_end: Final[int | None]
    source_span_reliable: Final[bool]

class NativeIRTableV2:
    id: Final[str]
    node_id: Final[str]
    order: Final[int]
    row_count: Final[int]
    column_count: Final[int]
    cell_ids: Final[list[str]]
    grid_complete: Final[bool]

class NativeIRTableCellV2:
    id: Final[str]
    node_id: Final[str]
    table_id: Final[str]
    order: Final[int]
    row_index: Final[int]
    column_index: Final[int]
    row_span: Final[int]
    column_span: Final[int]
    row_group: Final[str]
    header: Final[bool]
    scope: Final[str]
    text_run_ids: Final[list[str]]
    grid_complete: Final[bool]

class NativeIRListV2:
    id: Final[str]
    node_id: Final[str]
    order: Final[int]
    kind: Final[str]
    depth: Final[int]
    start: Final[int | None]
    reversed: Final[bool]
    marker_type: Final[str]
    item_ids: Final[list[str]]

class NativeIRListItemV2:
    id: Final[str]
    node_id: Final[str]
    list_id: Final[str]
    order: Final[int]
    depth: Final[int]
    index: Final[int]
    kind: Final[str]
    ordinal: Final[int | None]
    explicit_value: Final[int | None]
    text_run_ids: Final[list[str]]

class NativeIRMathV2:
    id: Final[str]
    node_id: Final[str]
    order: Final[int]
    format: Final[str]
    display: Final[str]
    tex: Final[str | None]
    mathml: Final[str | None]
    source_markup: Final[str]
    alt_text: Final[str | None]
    source_backed: Final[bool]
    truncated: Final[bool]

class NativeIRSerializationV2:
    contract_version: Final[str]
    markdown: Final[str]
    selected_ids: Final[list[str]]
    missing_ids: Final[list[str]]
    deterministic: Final[bool]
    exact_code_whitespace: Final[bool]
    table_grid_complete: Final[bool]
    source_complete: Final[bool]
    truncated: Final[bool]
    truncation_reasons: Final[list[str]]

class NativeSelectionCertificateV0:
    encoded: Final[bytes]
    contract_version: Final[str]
    wire_version: Final[int]
    validation_scope: Final[Literal["full_document", "local_atomic"]]
    source_digest: Final[str]
    graph_digest: Final[str]
    output_digest: Final[str]
    output_bytes: Final[int]
    max_output_bytes: Final[int]
    selection_count: Final[int]
    selected_ids: Final[list[str]]
    certificate_digest: Final[str]

class NativeSelectionReceiptV0:
    contract_version: Final[str]
    wire_version: Final[int]
    validation_scope: Final[Literal["full_document", "local_atomic"]]
    certificate_digest: Final[str]
    source_digest: Final[str]
    graph_digest: Final[str]
    output_digest: Final[str]
    output_bytes: Final[int]
    certificate_output_limit_bytes: Final[int]
    verifier_output_limit_bytes: Final[int]
    selection_count: Final[int]
    selected_ids: Final[list[str]]
    verified: Final[bool]
    deterministic: Final[bool]

class NativeSelectionReplayV0:
    markdown: Final[str]
    receipt: Final[NativeSelectionReceiptV0]

class NativeTypedAtomicOverlayItemV0:
    certificate: Final[bytes]
    contract_version: Final[str]
    atom_kind: Final[str]
    selected_id: Final[str]
    source_order: Final[int]
    source_start: Final[int]
    source_end: Final[int]
    source_span_digest: Final[str]
    source_digest: Final[str]
    graph_digest: Final[str]
    output_digest: Final[str]
    certificate_digest: Final[str]
    markdown: Final[str]
    verified: Final[bool]
    deterministic: Final[bool]

class NativeLocalAtomicBatchItemV0:
    certificate: Final[bytes]
    contract_version: Final[str]
    validation_scope: Final[Literal["local_atomic"]]
    request_index: Final[int]
    selected_id: Final[str]
    atom_kind: Final[Literal["", "code", "table"]]
    accepted: Final[bool]
    reason: Final[str]
    source_order: Final[int | None]
    source_start: Final[int | None]
    source_end: Final[int | None]
    source_span_digest: Final[str]
    source_digest: Final[str]
    graph_digest: Final[str]
    output_digest: Final[str]
    certificate_digest: Final[str]
    markdown: Final[str]
    verified: Final[bool]
    deterministic: Final[bool]

class NativeDocumentIRV2:
    elements: Final[list[NativeIRElementV2]]
    text_runs: Final[list[NativeIRTextRunV2]]
    tables: Final[list[NativeIRTableV2]]
    table_cells: Final[list[NativeIRTableCellV2]]
    lists: Final[list[NativeIRListV2]]
    list_items: Final[list[NativeIRListItemV2]]
    math: Final[list[NativeIRMathV2]]
    element_count: Final[int]
    text_run_count: Final[int]
    table_count: Final[int]
    table_cell_count: Final[int]
    list_count: Final[int]
    list_item_count: Final[int]
    math_count: Final[int]
    schema_version: Final[str]
    serialization_contract: Final[str]
    source: Final[str]
    input_bytes: Final[int]
    parsed_bytes: Final[int]
    source_complete: Final[bool]
    node_count: Final[int]
    parse_error_count: Final[int]
    event_count: Final[int]
    mapped_element_count: Final[int]
    implicit_element_count: Final[int]
    unmapped_explicit_element_count: Final[int]
    source_mapping_complete: Final[bool]
    stored_text_bytes: Final[int]
    input_truncated: Final[bool]
    nodes_truncated: Final[bool]
    depth_truncated: Final[bool]
    elements_truncated: Final[bool]
    text_runs_truncated: Final[bool]
    text_truncated_runs: Final[int]
    table_grid_truncated: Final[bool]
    math_truncated_nodes: Final[int]
    truncated: Final[bool]
    truncation_reasons: Final[list[str]]
    root_ids: Final[list[str]]
    max_input_bytes: Final[int]
    max_nodes: Final[int]
    max_elements: Final[int]
    max_text_runs: Final[int]
    max_depth: Final[int]
    max_text_run_bytes: Final[int]
    max_total_text_bytes: Final[int]
    max_math_bytes: Final[int]
    max_table_columns: Final[int]
    def reconstruct(
        self, selected_ids: list[str] | None = None
    ) -> NativeIRSerializationV2: ...

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
def extract_document_ir_v2_native(
    html: str,
    max_input_bytes: int = 4 * 1024 * 1024,
    max_nodes: int = 200_000,
    max_elements: int = 100_000,
    max_text_runs: int = 200_000,
    max_depth: int = 256,
    max_text_run_bytes: int = 256 * 1024,
    max_total_text_bytes: int = 8 * 1024 * 1024,
    max_math_bytes: int = 256 * 1024,
    max_table_columns: int = 1_024,
) -> NativeDocumentIRV2: ...
def create_selection_certificate_v0_native(
    document: NativeDocumentIRV2,
    selected_ids: list[str],
    max_output_bytes: int = 4 * 1024 * 1024,
) -> NativeSelectionCertificateV0: ...
def create_local_atomic_selection_certificate_v0_native(
    document: NativeDocumentIRV2,
    selected_ids: list[str],
    max_output_bytes: int = 4 * 1024 * 1024,
) -> NativeSelectionCertificateV0: ...
def create_typed_atomic_overlay_batch_v0_native(
    document: NativeDocumentIRV2,
    selected_ids: list[str],
    max_output_bytes: int = 4 * 1024 * 1024,
) -> list[NativeTypedAtomicOverlayItemV0]: ...
def create_local_atomic_selection_batch_v0_native(
    document: NativeDocumentIRV2,
    selected_ids: list[str],
    max_output_bytes: int = 4 * 1024 * 1024,
    max_total_certificate_bytes: int = 8 * 1024 * 1024,
    max_total_output_bytes: int = 8 * 1024 * 1024,
) -> list[NativeLocalAtomicBatchItemV0]: ...
def decode_selection_certificate_v0_native(
    encoded: bytes,
) -> NativeSelectionCertificateV0: ...
def verify_typed_atomic_overlay_batch_v0_native(
    document: NativeDocumentIRV2,
    encoded_certificates: list[bytes],
    max_output_bytes: int = 4 * 1024 * 1024,
) -> list[NativeTypedAtomicOverlayItemV0]: ...
def verify_and_replay_local_atomic_selection_batch_v0_native(
    document: NativeDocumentIRV2,
    selected_ids: list[str],
    encoded_certificates: list[bytes],
    max_output_bytes: int = 4 * 1024 * 1024,
    max_total_certificate_bytes: int = 8 * 1024 * 1024,
    max_total_output_bytes: int = 8 * 1024 * 1024,
) -> list[NativeLocalAtomicBatchItemV0]: ...
def verify_and_replay_selection_certificate_v0_native(
    document: NativeDocumentIRV2,
    encoded: bytes,
    max_output_bytes: int = 4 * 1024 * 1024,
) -> NativeSelectionReplayV0: ...
def verify_and_replay_local_atomic_selection_certificate_v0_native(
    document: NativeDocumentIRV2,
    encoded: bytes,
    max_output_bytes: int = 4 * 1024 * 1024,
) -> NativeSelectionReplayV0: ...
def backend_version() -> str: ...

"""Bounded, label-free selection atoms over ``ordered-dom-ir.v2``.

The catalog is an opt-in representation surface. It does not classify content,
change crawler output, or serialize selected content. Each accepted atom is
anchored to one reliable text-run byte span, so atom spans are globally
non-overlapping. Typed structure membership and the deterministic v2
``selection_id`` are recorded separately because structure-closure spans may
legitimately nest. ``text_run_id`` is the narrow lexical replay pointer.
``selection_id`` is closure metadata whose typed replay remains subject to the
ledger's verified replay policy; it is not a complete downstream selection
decision.

Any incomplete source, truncated IR, unreliable ordered text map, invalid typed
relation, overlapping atom span, or catalog budget failure rejects the entire
catalog. Entity-decoded and repeated text uses the versioned ordered raw-source
mapper; the catalog never falls back to literal substring search. Callers can
therefore retain their existing deterministic fallback without interpreting a
partial atom set.

V1 deliberately keeps atoms lexical: several text runs inside one table cell,
list item, math node, or code block share the same ``selection_id``. A consumer
replays one lexical atom through ``text_run_id``. To reconstruct a typed unit,
it must group atoms, choose the required structure closure, and run the normal
verified replay policy; the catalog does not make that decision. Atom text,
element tag/path/depth, and learned features are not copied into this catalog;
they remain resolvable from the source ledger and are intentionally deferred
to a bounded downstream stage.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, replace
from typing import Final, Literal

from ._native import (
    NativeDocumentIRV2,
    NativeIRElementV2,
    NativeIRListItemV2,
    NativeIRListV2,
    NativeIRMathV2,
    NativeIRTableCellV2,
    NativeIRTableV2,
    NativeIRTextRunV2,
    NativeOrderedSourceTextMapV2,
    NativeOrderedSourceTextSpanV2,
)
from .document_ir_v2 import DocumentIRV2Limits, extract_document_ir_v2
from .source_text_mapper_v2 import (
    ORDERED_SOURCE_TEXT_MAP_V2_SCHEMA,
    ORDERED_SOURCE_TEXT_SPAN_V2_SCHEMA,
    OrderedSourceTextMapV2Limits,
    map_ordered_source_text_v2,
)

SELECTION_ATOM_CATALOG_V1_SCHEMA: Final = "selection-atom-catalog.v1"
SELECTION_ATOM_V1_SCHEMA: Final = "selection-atom.v1"

_MAX_SOURCE_BYTES: Final = 16 * 1024 * 1024
_MAX_ATOMS: Final = 200_000
_MAX_TOTAL_ATOM_SOURCE_BYTES: Final = 16 * 1024 * 1024
_MAX_ANCESTRY_STEPS: Final = 10_000_000
_MAX_IDENTIFIER_CHARS: Final = 4_096

SelectionAtomKindV1 = Literal["text", "code", "table_cell", "list_item", "math"]
_KIND_ORDER: Final[tuple[SelectionAtomKindV1, ...]] = (
    "text",
    "code",
    "table_cell",
    "list_item",
    "math",
)


def _bounded_int(name: str, value: int, maximum: int) -> None:
    if type(value) is not int or value <= 0 or value > maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")


@dataclass(frozen=True, slots=True)
class SelectionAtomCatalogV1Config:
    """Explicit opt-in and hard caller-lowerable catalog budgets."""

    enabled: bool = False
    max_source_bytes: int = 4 * 1024 * 1024
    max_atoms: int = 65_536
    max_total_atom_source_bytes: int = 4 * 1024 * 1024
    max_ancestry_steps: int = 2_000_000
    max_identifier_chars: int = 1_024

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be a bool")
        _bounded_int("max_source_bytes", self.max_source_bytes, _MAX_SOURCE_BYTES)
        _bounded_int("max_atoms", self.max_atoms, _MAX_ATOMS)
        _bounded_int(
            "max_total_atom_source_bytes",
            self.max_total_atom_source_bytes,
            _MAX_TOTAL_ATOM_SOURCE_BYTES,
        )
        _bounded_int(
            "max_ancestry_steps",
            self.max_ancestry_steps,
            _MAX_ANCESTRY_STEPS,
        )
        _bounded_int(
            "max_identifier_chars",
            self.max_identifier_chars,
            _MAX_IDENTIFIER_CHARS,
        )


DEFAULT_SELECTION_ATOM_CATALOG_V1_CONFIG = SelectionAtomCatalogV1Config()


@dataclass(frozen=True, slots=True)
class SelectionAtomV1:
    """One lexical replay pointer plus non-standalone typed closure metadata."""

    schema_version: str
    id: str
    order: int
    source_order: int
    kind: SelectionAtomKindV1
    source_start: int
    source_end: int
    source_bytes: int
    source_fragment_sha256: str
    text_run_id: str
    parent_id: str
    selection_id: str
    selection_source_start: int
    selection_source_end: int
    selection_source_fragment_sha256: str
    code_element_id: str | None
    code_language: str | None
    table_id: str | None
    table_cell_id: str | None
    table_row_index: int | None
    table_column_index: int | None
    table_row_span: int | None
    table_column_span: int | None
    table_header: bool | None
    list_id: str | None
    list_item_id: str | None
    list_depth: int | None
    list_index: int | None
    list_kind: str | None
    list_ordinal: int | None
    math_id: str | None
    math_format: str | None
    math_display: str | None
    preserve_whitespace: bool
    mapping_reliable: bool
    selection_mapping_reliable: bool
    source_backed: bool


@dataclass(frozen=True, slots=True)
class SelectionAtomCatalogV1:
    """All-or-nothing atom catalog with source and resource provenance."""

    schema_version: str
    enabled: bool
    accepted: bool
    reason: str
    document_schema_version: str
    source_digest: str
    config_digest: str
    catalog_digest: str
    source_text_map_schema_version: str
    source_text_map_reason: str
    source_text_map_digest: str
    source_text_map_transformed_span_count: int
    text_mapping_contract: str
    input_bytes: int
    parsed_bytes: int
    source_bytes: int
    source_complete: bool
    source_mapping_complete: bool
    parse_error_count: int
    ir_truncated: bool
    ir_truncation_reasons: tuple[str, ...]
    atoms: tuple[SelectionAtomV1, ...]
    candidate_text_run_count: int
    skipped_whitespace_text_run_count: int
    atom_source_bytes: int
    ancestry_steps: int
    kind_counts: tuple[tuple[SelectionAtomKindV1, int], ...]
    deterministic: bool
    digest_is_authentication: bool = False

    @property
    def atom_count(self) -> int:
        """Number of accepted atoms."""

        return len(self.atoms)


@dataclass(frozen=True, slots=True)
class _SourceSpan:
    start: int
    end: int
    digest: str


@dataclass(frozen=True, slots=True)
class _AtomDraft:
    source_order: int
    kind: SelectionAtomKindV1
    source_span: _SourceSpan
    text_run_id: str
    parent_id: str
    selection_id: str
    selection_span: _SourceSpan
    code_element_id: str | None
    code_language: str | None
    table_id: str | None
    table_cell_id: str | None
    table_row_index: int | None
    table_column_index: int | None
    table_row_span: int | None
    table_column_span: int | None
    table_header: bool | None
    list_id: str | None
    list_item_id: str | None
    list_depth: int | None
    list_index: int | None
    list_kind: str | None
    list_ordinal: int | None
    math_id: str | None
    math_format: str | None
    math_display: str | None
    preserve_whitespace: bool


@dataclass(frozen=True, slots=True)
class _DocumentProvenance:
    document_schema_version: str
    source_digest: str
    input_bytes: int
    parsed_bytes: int
    source_bytes: int
    source_complete: bool
    source_mapping_complete: bool
    parse_error_count: int
    ir_truncated: bool
    ir_truncation_reasons: tuple[str, ...]


class _CatalogRejectedError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def build_selection_atom_catalog_v1(
    html: str,
    *,
    config: SelectionAtomCatalogV1Config = DEFAULT_SELECTION_ATOM_CATALOG_V1_CONFIG,
) -> SelectionAtomCatalogV1:
    """Parse *html* once and build a disabled-by-default atom catalog."""

    config = _canonical_config(config)
    if type(html) is not str:
        raise TypeError("html must be a string")
    if not config.enabled:
        return _rejection(config, "disabled", enabled=False)
    try:
        source_bytes = html.encode("utf-8")
    except UnicodeEncodeError:
        return _rejection(config, "invalid_unicode")
    if len(source_bytes) > config.max_source_bytes:
        return _rejection(
            config,
            "source_byte_budget",
            input_bytes=len(source_bytes),
            source_bytes=len(source_bytes),
        )
    try:
        document = extract_document_ir_v2(
            html,
            limits=DocumentIRV2Limits(max_input_bytes=config.max_source_bytes),
        )
    except Exception:
        return _rejection(
            config,
            "ir_extraction_failure",
            input_bytes=len(source_bytes),
            source_bytes=len(source_bytes),
        )
    return build_selection_atom_catalog_from_document_v1(document, config=config)


def build_selection_atom_catalog_from_document_v1(
    document: NativeDocumentIRV2,
    *,
    config: SelectionAtomCatalogV1Config,
) -> SelectionAtomCatalogV1:
    """Build atoms from one already-parsed v2 source ledger."""

    config = _canonical_config(config)
    if type(document) is not NativeDocumentIRV2:
        raise TypeError("document must be a NativeDocumentIRV2")
    if not config.enabled:
        return _rejection(config, "disabled", enabled=False)

    provenance = _document_provenance(document)
    source = document.source
    try:
        source_bytes = source.encode("utf-8")
    except UnicodeEncodeError:
        return _provenance_rejection(config, "invalid_ir_unicode", provenance)
    source_digest = hashlib.sha256(source_bytes).hexdigest()
    provenance = replace(
        provenance,
        source_digest=source_digest,
        source_bytes=len(source_bytes),
    )

    if document.schema_version != "ordered-dom-ir.v2":
        return _provenance_rejection(config, "unsupported_ir_schema", provenance)
    if len(source_bytes) != document.parsed_bytes:
        return _provenance_rejection(config, "source_length_mismatch", provenance)
    if len(source_bytes) > config.max_source_bytes:
        return _provenance_rejection(config, "source_byte_budget", provenance)
    if not document.source_complete or document.input_truncated:
        return _provenance_rejection(config, "incomplete_source", provenance)
    if not document.source_mapping_complete:
        return _provenance_rejection(config, "incomplete_source_mapping", provenance)
    if (
        document.truncated
        or document.nodes_truncated
        or document.depth_truncated
        or document.elements_truncated
        or document.text_runs_truncated
        or document.text_truncated_runs != 0
        or document.table_grid_truncated
        or document.math_truncated_nodes != 0
    ):
        return _provenance_rejection(config, "truncated_ir", provenance)

    try:
        source_text_map = map_ordered_source_text_v2(
            document,
            limits=OrderedSourceTextMapV2Limits(
                max_source_bytes=config.max_source_bytes,
                max_raw_fragment_bytes=config.max_source_bytes,
                max_total_raw_bytes=config.max_source_bytes,
            ),
        )
    except Exception:
        return _provenance_rejection(
            config,
            "source_text_mapping_failure",
            provenance,
            source_text_map_schema_version=ORDERED_SOURCE_TEXT_MAP_V2_SCHEMA,
            source_text_map_reason="native_failure",
        )
    text_mapping_contract = ORDERED_SOURCE_TEXT_MAP_V2_SCHEMA
    if not source_text_map.accepted:
        return _provenance_rejection(
            config,
            _catalog_reason_for_source_map(source_text_map.reason),
            provenance,
            source_text_map_schema_version=source_text_map.schema_version,
            source_text_map_reason=source_text_map.reason,
        )

    try:
        certified_text_spans = _certified_text_spans(
            document,
            source_bytes,
            source_digest,
            source_text_map,
            config,
        )
        atoms, diagnostics = _build_atoms(
            document,
            source_bytes,
            source_digest,
            config,
            certified_text_spans,
        )
    except _CatalogRejectedError as error:
        return _provenance_rejection(
            config,
            error.reason,
            provenance,
            source_text_map_schema_version=source_text_map.schema_version,
            source_text_map_reason=source_text_map.reason,
            source_text_map_digest=source_text_map.map_digest,
            source_text_map_transformed_span_count=source_text_map.transformed_span_count,
            text_mapping_contract=text_mapping_contract,
        )

    kind_counter = Counter(atom.kind for atom in atoms)
    kind_counts = tuple((kind, kind_counter[kind]) for kind in _KIND_ORDER)
    catalog_digest = _hash_json(
        {
            "schema_version": SELECTION_ATOM_CATALOG_V1_SCHEMA,
            "source_digest": source_digest,
            "config_digest": _config_digest(config),
            "source_text_map_schema_version": source_text_map.schema_version,
            "source_text_map_reason": source_text_map.reason,
            "source_text_map_digest": source_text_map.map_digest,
            "text_mapping_contract": text_mapping_contract,
            "atoms": [_atom_digest_payload(atom) for atom in atoms],
            "diagnostics": diagnostics,
        }
    )
    return SelectionAtomCatalogV1(
        schema_version=SELECTION_ATOM_CATALOG_V1_SCHEMA,
        enabled=True,
        accepted=True,
        reason="accepted",
        document_schema_version=document.schema_version,
        source_digest=source_digest,
        config_digest=_config_digest(config),
        catalog_digest=catalog_digest,
        source_text_map_schema_version=source_text_map.schema_version,
        source_text_map_reason=source_text_map.reason,
        source_text_map_digest=source_text_map.map_digest,
        source_text_map_transformed_span_count=source_text_map.transformed_span_count,
        text_mapping_contract=text_mapping_contract,
        input_bytes=document.input_bytes,
        parsed_bytes=document.parsed_bytes,
        source_bytes=len(source_bytes),
        source_complete=document.source_complete,
        source_mapping_complete=document.source_mapping_complete,
        parse_error_count=document.parse_error_count,
        ir_truncated=document.truncated,
        ir_truncation_reasons=tuple(document.truncation_reasons),
        atoms=atoms,
        candidate_text_run_count=diagnostics["candidate_text_run_count"],
        skipped_whitespace_text_run_count=diagnostics["skipped_whitespace_text_run_count"],
        atom_source_bytes=diagnostics["atom_source_bytes"],
        ancestry_steps=diagnostics["ancestry_steps"],
        kind_counts=kind_counts,
        deterministic=True,
    )


def _build_atoms(
    document: NativeDocumentIRV2,
    source_bytes: bytes,
    source_digest: str,
    config: SelectionAtomCatalogV1Config,
    certified_text_spans: dict[str, _SourceSpan],
) -> tuple[tuple[SelectionAtomV1, ...], dict[str, int]]:
    elements = tuple(document.elements)
    text_runs = tuple(document.text_runs)
    elements_by_id: dict[str, NativeIRElementV2] = {}
    for element in elements:
        _bounded_identifier(element.id, config)
        if element.id in elements_by_id:
            raise _CatalogRejectedError("duplicate_element_id")
        elements_by_id[element.id] = element
    for element in elements:
        if element.parent_id is not None and element.parent_id not in elements_by_id:
            raise _CatalogRejectedError("unknown_element_parent")

    text_runs_by_id: dict[str, NativeIRTextRunV2] = {}
    for run in text_runs:
        _bounded_identifier(run.id, config)
        _bounded_identifier(run.parent_id, config)
        if run.id in text_runs_by_id:
            raise _CatalogRejectedError("duplicate_text_run_id")
        if run.parent_id not in elements_by_id:
            raise _CatalogRejectedError("unknown_text_parent")
        text_runs_by_id[run.id] = run

    tables_by_id = _tables_by_id(tuple(document.tables), elements_by_id, config)
    cells_by_run = _table_cells_by_run(
        tuple(document.table_cells),
        text_runs_by_id,
        elements_by_id,
        config,
        tables_by_id,
    )
    lists_by_id = _lists_by_id(tuple(document.lists), elements_by_id, config)
    items_by_run = _list_items_by_run(
        tuple(document.list_items),
        text_runs_by_id,
        elements_by_id,
        config,
        lists_by_id,
    )
    selection_spans: dict[str, _SourceSpan] = {}
    maths_by_node = _maths_by_node(
        tuple(document.math),
        elements_by_id,
        source_bytes,
        config,
        selection_spans,
    )

    ancestry_steps = 0

    def ancestors(parent_id: str) -> tuple[NativeIRElementV2, ...]:
        nonlocal ancestry_steps
        chain: list[NativeIRElementV2] = []
        seen: set[str] = set()
        current_id: str | None = parent_id
        while current_id is not None:
            if current_id in seen:
                raise _CatalogRejectedError("element_parent_cycle")
            seen.add(current_id)
            element = elements_by_id.get(current_id)
            if element is None:
                raise _CatalogRejectedError("unknown_element_parent")
            chain.append(element)
            ancestry_steps += 1
            if ancestry_steps > config.max_ancestry_steps:
                raise _CatalogRejectedError("ancestry_budget")
            current_id = element.parent_id
        return tuple(chain)

    drafts: list[_AtomDraft] = []
    skipped_whitespace = 0
    candidate_count = 0
    draft_atom_source_bytes = 0
    for run in sorted(text_runs, key=lambda item: (item.order, item.id)):
        if not run.text or (not run.text.strip() and not run.preserve_whitespace):
            skipped_whitespace += 1
            continue
        candidate_count += 1
        if candidate_count > config.max_atoms:
            raise _CatalogRejectedError("atom_budget")
        run_span = _validated_text_span(
            run,
            certified_text_spans,
        )
        lineage = ancestors(run.parent_id)
        lineage_ids = {element.id for element in lineage}

        cell = cells_by_run.get(run.id)
        if cell is not None:
            table = tables_by_id[cell.table_id]
            if cell.node_id not in lineage_ids or table.node_id not in lineage_ids:
                raise _CatalogRejectedError("table_cell_membership_mismatch")
        item = items_by_run.get(run.id)
        if item is not None:
            list_record = lists_by_id[item.list_id]
            if item.node_id not in lineage_ids or list_record.node_id not in lineage_ids:
                raise _CatalogRejectedError("list_item_membership_mismatch")
        math = next(
            (maths_by_node[element.id] for element in lineage if element.id in maths_by_node),
            None,
        )
        code_element, code_language = _code_context(lineage, config)

        kind: SelectionAtomKindV1
        selection_element: NativeIRElementV2 | None
        if math is not None:
            kind = "math"
            selection_element = elements_by_id[math.node_id]
        elif code_element is not None:
            kind = "code"
            selection_element = code_element
        elif cell is not None:
            kind = "table_cell"
            selection_element = elements_by_id[cell.node_id]
        elif item is not None:
            kind = "list_item"
            selection_element = elements_by_id[item.node_id]
        else:
            kind = "text"
            selection_element = None

        if selection_element is None:
            selection_id = run.id
            selection_span = run_span
        else:
            selection_id = selection_element.id
            selection_span = _cached_element_span(
                selection_element,
                source_bytes,
                selection_spans,
            )
        if not (
            selection_span.start <= run_span.start
            and run_span.start < run_span.end
            and run_span.end <= selection_span.end
        ):
            raise _CatalogRejectedError("selection_span_does_not_contain_text")
        _bounded_identifier(selection_id, config)
        draft_atom_source_bytes += run_span.end - run_span.start
        if draft_atom_source_bytes > config.max_total_atom_source_bytes:
            raise _CatalogRejectedError("atom_source_byte_budget")

        drafts.append(
            _AtomDraft(
                source_order=run.order,
                kind=kind,
                source_span=run_span,
                text_run_id=run.id,
                parent_id=run.parent_id,
                selection_id=selection_id,
                selection_span=selection_span,
                code_element_id=code_element.id if code_element is not None else None,
                code_language=code_language,
                table_id=cell.table_id if cell is not None else None,
                table_cell_id=cell.id if cell is not None else None,
                table_row_index=cell.row_index if cell is not None else None,
                table_column_index=cell.column_index if cell is not None else None,
                table_row_span=cell.row_span if cell is not None else None,
                table_column_span=cell.column_span if cell is not None else None,
                table_header=cell.header if cell is not None else None,
                list_id=item.list_id if item is not None else None,
                list_item_id=item.id if item is not None else None,
                list_depth=item.depth if item is not None else None,
                list_index=item.index if item is not None else None,
                list_kind=item.kind if item is not None else None,
                list_ordinal=item.ordinal if item is not None else None,
                math_id=math.id if math is not None else None,
                math_format=math.format if math is not None else None,
                math_display=math.display if math is not None else None,
                preserve_whitespace=run.preserve_whitespace,
            )
        )

    drafts.sort(
        key=lambda item: (
            item.source_span.start,
            item.source_span.end,
            item.source_order,
            item.text_run_id,
        )
    )
    previous_end = 0
    atoms: list[SelectionAtomV1] = []
    for order, draft in enumerate(drafts):
        if order and draft.source_span.start < previous_end:
            raise _CatalogRejectedError("overlapping_source_spans")
        previous_end = draft.source_span.end
        atom_id = _atom_id(source_digest, draft)
        atoms.append(
            SelectionAtomV1(
                schema_version=SELECTION_ATOM_V1_SCHEMA,
                id=atom_id,
                order=order,
                source_order=draft.source_order,
                kind=draft.kind,
                source_start=draft.source_span.start,
                source_end=draft.source_span.end,
                source_bytes=draft.source_span.end - draft.source_span.start,
                source_fragment_sha256=draft.source_span.digest,
                text_run_id=draft.text_run_id,
                parent_id=draft.parent_id,
                selection_id=draft.selection_id,
                selection_source_start=draft.selection_span.start,
                selection_source_end=draft.selection_span.end,
                selection_source_fragment_sha256=draft.selection_span.digest,
                code_element_id=draft.code_element_id,
                code_language=draft.code_language,
                table_id=draft.table_id,
                table_cell_id=draft.table_cell_id,
                table_row_index=draft.table_row_index,
                table_column_index=draft.table_column_index,
                table_row_span=draft.table_row_span,
                table_column_span=draft.table_column_span,
                table_header=draft.table_header,
                list_id=draft.list_id,
                list_item_id=draft.list_item_id,
                list_depth=draft.list_depth,
                list_index=draft.list_index,
                list_kind=draft.list_kind,
                list_ordinal=draft.list_ordinal,
                math_id=draft.math_id,
                math_format=draft.math_format,
                math_display=draft.math_display,
                preserve_whitespace=draft.preserve_whitespace,
                mapping_reliable=True,
                selection_mapping_reliable=True,
                source_backed=True,
            )
        )

    return tuple(atoms), {
        "candidate_text_run_count": candidate_count,
        "skipped_whitespace_text_run_count": skipped_whitespace,
        "atom_source_bytes": draft_atom_source_bytes,
        "ancestry_steps": ancestry_steps,
    }


def _tables_by_id(
    tables: tuple[NativeIRTableV2, ...],
    elements_by_id: dict[str, NativeIRElementV2],
    config: SelectionAtomCatalogV1Config,
) -> dict[str, NativeIRTableV2]:
    output: dict[str, NativeIRTableV2] = {}
    for table in tables:
        _bounded_identifier(table.id, config)
        _bounded_identifier(table.node_id, config)
        if table.id in output:
            raise _CatalogRejectedError("duplicate_table_id")
        element = elements_by_id.get(table.node_id)
        if element is None:
            raise _CatalogRejectedError("unknown_table_node")
        if element.tag != "table":
            raise _CatalogRejectedError("invalid_table_node")
        if not table.grid_complete:
            raise _CatalogRejectedError("incomplete_table_grid")
        output[table.id] = table
    return output


def _lists_by_id(
    lists: tuple[NativeIRListV2, ...],
    elements_by_id: dict[str, NativeIRElementV2],
    config: SelectionAtomCatalogV1Config,
) -> dict[str, NativeIRListV2]:
    output: dict[str, NativeIRListV2] = {}
    for list_record in lists:
        _bounded_identifier(list_record.id, config)
        _bounded_identifier(list_record.node_id, config)
        if list_record.id in output:
            raise _CatalogRejectedError("duplicate_list_id")
        element = elements_by_id.get(list_record.node_id)
        if element is None:
            raise _CatalogRejectedError("unknown_list_node")
        expected_kind = {
            "ol": "ordered",
            "ul": "unordered",
            "dl": "description",
        }.get(element.tag)
        if expected_kind is None or list_record.kind != expected_kind:
            raise _CatalogRejectedError("invalid_list_node")
        output[list_record.id] = list_record
    return output


def _table_cells_by_run(
    records: tuple[NativeIRTableCellV2, ...],
    text_runs_by_id: dict[str, NativeIRTextRunV2],
    elements_by_id: dict[str, NativeIRElementV2],
    config: SelectionAtomCatalogV1Config,
    tables_by_id: dict[str, NativeIRTableV2],
) -> dict[str, NativeIRTableCellV2]:
    output: dict[str, NativeIRTableCellV2] = {}
    seen_ids: set[str] = set()
    for record in records:
        _bounded_identifier(record.id, config)
        _bounded_identifier(record.node_id, config)
        if record.id in seen_ids:
            raise _CatalogRejectedError("duplicate_table_cell_id")
        seen_ids.add(record.id)
        element = elements_by_id.get(record.node_id)
        if element is None:
            raise _CatalogRejectedError("unknown_table_cell_node")
        if element.tag not in {"td", "th"} or record.header != (element.tag == "th"):
            raise _CatalogRejectedError("invalid_table_cell_node")
        _bounded_identifier(record.table_id, config)
        if record.table_id not in tables_by_id:
            raise _CatalogRejectedError("unknown_table_cell_parent")
        if not record.grid_complete:
            raise _CatalogRejectedError("incomplete_table_cell_grid")
        for run_id in record.text_run_ids:
            _bounded_identifier(run_id, config)
            if run_id not in text_runs_by_id:
                raise _CatalogRejectedError("unknown_table_cell_text_run")
            if run_id in output:
                raise _CatalogRejectedError("duplicate_table_cell_text_membership")
            output[run_id] = record
    return output


def _list_items_by_run(
    records: tuple[NativeIRListItemV2, ...],
    text_runs_by_id: dict[str, NativeIRTextRunV2],
    elements_by_id: dict[str, NativeIRElementV2],
    config: SelectionAtomCatalogV1Config,
    lists_by_id: dict[str, NativeIRListV2],
) -> dict[str, NativeIRListItemV2]:
    output: dict[str, NativeIRListItemV2] = {}
    seen_ids: set[str] = set()
    for record in records:
        _bounded_identifier(record.id, config)
        _bounded_identifier(record.node_id, config)
        if record.id in seen_ids:
            raise _CatalogRejectedError("duplicate_list_item_id")
        seen_ids.add(record.id)
        element = elements_by_id.get(record.node_id)
        if element is None:
            raise _CatalogRejectedError("unknown_list_item_node")
        expected_kind = {
            "li": "item",
            "dt": "term",
            "dd": "definition",
        }.get(element.tag)
        if expected_kind is None or record.kind != expected_kind:
            raise _CatalogRejectedError("invalid_list_item_node")
        _bounded_identifier(record.list_id, config)
        if record.list_id not in lists_by_id:
            raise _CatalogRejectedError("unknown_list_item_parent")
        for run_id in record.text_run_ids:
            _bounded_identifier(run_id, config)
            if run_id not in text_runs_by_id:
                raise _CatalogRejectedError("unknown_list_item_text_run")
            if run_id in output:
                raise _CatalogRejectedError("duplicate_list_item_text_membership")
            output[run_id] = record
    return output


def _maths_by_node(
    maths: tuple[NativeIRMathV2, ...],
    elements_by_id: dict[str, NativeIRElementV2],
    source_bytes: bytes,
    config: SelectionAtomCatalogV1Config,
    selection_spans: dict[str, _SourceSpan],
) -> dict[str, NativeIRMathV2]:
    output: dict[str, NativeIRMathV2] = {}
    ids: set[str] = set()
    for math in maths:
        _bounded_identifier(math.id, config)
        _bounded_identifier(math.node_id, config)
        if math.id in ids:
            raise _CatalogRejectedError("duplicate_math_id")
        ids.add(math.id)
        if math.node_id in output:
            raise _CatalogRejectedError("duplicate_math_node")
        element = elements_by_id.get(math.node_id)
        if element is None:
            raise _CatalogRejectedError("unknown_math_node")
        expected_format = {"math": "mathml", "script": "tex"}.get(element.tag)
        if (
            not math.source_backed
            or math.truncated
            or expected_format is None
            or math.format != expected_format
            or math.display not in {"inline", "block"}
        ):
            raise _CatalogRejectedError("unreliable_math_mapping")
        span = _cached_element_span(element, source_bytes, selection_spans)
        if source_bytes[span.start : span.end].decode("utf-8") != math.source_markup:
            raise _CatalogRejectedError("unreliable_math_mapping")
        output[math.node_id] = math
    return output


def _code_context(
    lineage: tuple[NativeIRElementV2, ...],
    config: SelectionAtomCatalogV1Config,
) -> tuple[NativeIRElementV2 | None, str | None]:
    owner: NativeIRElementV2 | None = None
    for element in reversed(lineage):
        if element.tag == "pre":
            owner = element
            break
    if owner is None:
        owner = next((element for element in lineage if element.tag == "code"), None)
    language = next(
        (
            element.language
            for element in lineage
            if element.tag in {"code", "pre"} and element.language
        ),
        None,
    )
    if language is not None:
        _bounded_identifier(language, config)
    return owner, language


def _certified_text_spans(
    document: NativeDocumentIRV2,
    source_bytes: bytes,
    source_digest: str,
    source_map: NativeOrderedSourceTextMapV2,
    config: SelectionAtomCatalogV1Config,
) -> dict[str, _SourceSpan]:
    text_runs = tuple(document.text_runs)
    runs_by_id = {run.id: run for run in text_runs}
    if (
        type(source_map) is not NativeOrderedSourceTextMapV2
        or source_map.schema_version != ORDERED_SOURCE_TEXT_MAP_V2_SCHEMA
        or source_map.document_schema_version != document.schema_version
        or not source_map.accepted
        or source_map.reason != "accepted"
        or source_map.source_digest != source_digest
        or not _is_sha256(source_map.source_digest)
        or not _is_sha256(source_map.map_digest)
        or source_map.input_bytes != document.input_bytes
        or source_map.parsed_bytes != document.parsed_bytes
        or source_map.source_complete != document.source_complete
        or source_map.source_mapping_complete != document.source_mapping_complete
        or source_map.document_truncated != document.truncated
        or len(runs_by_id) != len(text_runs)
        or source_map.candidate_text_run_count != len(text_runs)
        or source_map.mapped_text_run_count != len(source_map.spans)
        or source_map.mapped_text_run_count + source_map.skipped_dom_text_run_count
        != len(text_runs)
        or not source_map.deterministic
        or source_map.digest_is_authentication
    ):
        raise _CatalogRejectedError("unreliable_text_mapping")

    output: dict[str, _SourceSpan] = {}
    previous_end = 0
    transformed_count = 0
    tokenizer_error_count = 0
    for expected_order, span in enumerate(source_map.spans):
        if type(span) is not NativeOrderedSourceTextSpanV2:
            raise _CatalogRejectedError("unreliable_text_mapping")
        _bounded_identifier(span.text_run_id, config)
        _bounded_identifier(span.parent_id, config)
        run = runs_by_id.get(span.text_run_id)
        if (
            run is None
            or span.text_run_id in output
            or span.schema_version != ORDERED_SOURCE_TEXT_SPAN_V2_SCHEMA
            or span.order != expected_order
            or span.source_order != run.order
            or span.parent_id != run.parent_id
            or span.decoded_text != run.text
            or span.decoded_bytes != run.stored_bytes
            or span.decoded_bytes != len(run.text.encode("utf-8"))
            or span.decoded_text_sha256 != hashlib.sha256(run.text.encode("utf-8")).hexdigest()
            or span.raw_source_bytes != span.raw_source_end - span.raw_source_start
            or span.raw_source_start < previous_end
            or span.raw_fragment.encode("utf-8")
            != source_bytes[span.raw_source_start : span.raw_source_end]
            or span.raw_fragment_sha256
            != hashlib.sha256(span.raw_fragment.encode("utf-8")).hexdigest()
            or span.transformed != (span.raw_fragment != span.decoded_text)
            or not span.decode_verified
            or not _is_sha256(span.certificate_sha256)
            or span.digest_is_authentication
        ):
            raise _CatalogRejectedError("unreliable_text_mapping")
        source_span = _validated_span(
            span.raw_source_start,
            span.raw_source_end,
            source_bytes,
            "unreliable_text_mapping",
        )
        if source_span.digest != span.raw_fragment_sha256:
            raise _CatalogRejectedError("unreliable_text_mapping")
        output[span.text_run_id] = source_span
        previous_end = span.raw_source_end
        transformed_count += int(span.transformed)
        tokenizer_error_count += span.tokenizer_error_count

    skipped_runs = [run for run in text_runs if run.id not in output]
    if len(skipped_runs) != source_map.skipped_dom_text_run_count or any(
        run.preserve_whitespace
        or not run.text
        or not all(character in "\t\n\u000c\r " for character in run.text)
        for run in skipped_runs
    ):
        raise _CatalogRejectedError("unreliable_text_mapping")
    if (
        transformed_count != source_map.transformed_span_count
        or tokenizer_error_count != source_map.tokenizer_error_count
    ):
        raise _CatalogRejectedError("unreliable_text_mapping")
    return output


def _validated_text_span(
    run: NativeIRTextRunV2,
    certified_text_spans: dict[str, _SourceSpan],
) -> _SourceSpan:
    if (
        run.truncated
        or run.original_bytes != run.stored_bytes
        or run.stored_bytes != len(run.text.encode("utf-8"))
    ):
        raise _CatalogRejectedError("unreliable_text_mapping")
    span = certified_text_spans.get(run.id)
    if span is None:
        raise _CatalogRejectedError("unreliable_text_mapping")
    return span


def _validated_element_span(
    element: NativeIRElementV2,
    source_bytes: bytes,
) -> _SourceSpan:
    if (
        not element.source_span_reliable
        or element.source_start is None
        or element.source_end is None
    ):
        raise _CatalogRejectedError("unreliable_selection_mapping")
    return _validated_span(
        element.source_start,
        element.source_end,
        source_bytes,
        "unreliable_selection_mapping",
    )


def _cached_element_span(
    element: NativeIRElementV2,
    source_bytes: bytes,
    cache: dict[str, _SourceSpan],
) -> _SourceSpan:
    span = cache.get(element.id)
    if span is None:
        span = _validated_element_span(element, source_bytes)
        cache[element.id] = span
    return span


def _validated_span(
    start: int,
    end: int,
    source_bytes: bytes,
    reason: str,
) -> _SourceSpan:
    if (
        type(start) is not int
        or type(end) is not int
        or start < 0
        or end <= start
        or end > len(source_bytes)
    ):
        raise _CatalogRejectedError(reason)
    fragment = source_bytes[start:end]
    try:
        fragment.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _CatalogRejectedError(reason) from error
    return _SourceSpan(
        start=start,
        end=end,
        digest=hashlib.sha256(fragment).hexdigest(),
    )


def _atom_id(source_digest: str, draft: _AtomDraft) -> str:
    digest = _hash_json(
        {
            "schema_version": SELECTION_ATOM_V1_SCHEMA,
            "source_digest": source_digest,
            "source_order": draft.source_order,
            "source_start": draft.source_span.start,
            "source_end": draft.source_span.end,
            "kind": draft.kind,
            "text_run_id": draft.text_run_id,
            "selection_id": draft.selection_id,
            "code_element_id": draft.code_element_id,
            "code_language": draft.code_language,
            "table_id": draft.table_id,
            "table_cell_id": draft.table_cell_id,
            "table_row_index": draft.table_row_index,
            "table_column_index": draft.table_column_index,
            "table_row_span": draft.table_row_span,
            "table_column_span": draft.table_column_span,
            "table_header": draft.table_header,
            "list_id": draft.list_id,
            "list_item_id": draft.list_item_id,
            "list_depth": draft.list_depth,
            "list_index": draft.list_index,
            "list_kind": draft.list_kind,
            "list_ordinal": draft.list_ordinal,
            "math_id": draft.math_id,
            "math_format": draft.math_format,
            "math_display": draft.math_display,
        }
    )
    return f"atom-v1-{digest}"


def _atom_digest_payload(atom: SelectionAtomV1) -> dict[str, object]:
    return {
        "id": atom.id,
        "order": atom.order,
        "source_order": atom.source_order,
        "kind": atom.kind,
        "source_start": atom.source_start,
        "source_end": atom.source_end,
        "source_fragment_sha256": atom.source_fragment_sha256,
        "text_run_id": atom.text_run_id,
        "parent_id": atom.parent_id,
        "selection_id": atom.selection_id,
        "selection_source_start": atom.selection_source_start,
        "selection_source_end": atom.selection_source_end,
        "selection_source_fragment_sha256": atom.selection_source_fragment_sha256,
        "code_element_id": atom.code_element_id,
        "code_language": atom.code_language,
        "table_id": atom.table_id,
        "table_cell_id": atom.table_cell_id,
        "table_row_index": atom.table_row_index,
        "table_column_index": atom.table_column_index,
        "table_row_span": atom.table_row_span,
        "table_column_span": atom.table_column_span,
        "table_header": atom.table_header,
        "list_id": atom.list_id,
        "list_item_id": atom.list_item_id,
        "list_depth": atom.list_depth,
        "list_index": atom.list_index,
        "list_kind": atom.list_kind,
        "list_ordinal": atom.list_ordinal,
        "math_id": atom.math_id,
        "math_format": atom.math_format,
        "math_display": atom.math_display,
        "preserve_whitespace": atom.preserve_whitespace,
    }


def _bounded_identifier(value: str, config: SelectionAtomCatalogV1Config) -> None:
    if type(value) is not str or not value or len(value) > config.max_identifier_chars:
        raise _CatalogRejectedError("identifier_budget")


def _is_sha256(value: str) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _catalog_reason_for_source_map(reason: str) -> str:
    return {
        "incomplete_source": "incomplete_source",
        "incomplete_element_mapping": "incomplete_source_mapping",
        "source_byte_budget": "source_byte_budget",
        "truncated_document": "truncated_ir",
    }.get(reason, "unreliable_text_mapping")


def _canonical_config(config: SelectionAtomCatalogV1Config) -> SelectionAtomCatalogV1Config:
    if type(config) is not SelectionAtomCatalogV1Config:
        raise TypeError("config must be a SelectionAtomCatalogV1Config")
    return SelectionAtomCatalogV1Config(
        enabled=object.__getattribute__(config, "enabled"),
        max_source_bytes=object.__getattribute__(config, "max_source_bytes"),
        max_atoms=object.__getattribute__(config, "max_atoms"),
        max_total_atom_source_bytes=object.__getattribute__(
            config,
            "max_total_atom_source_bytes",
        ),
        max_ancestry_steps=object.__getattribute__(config, "max_ancestry_steps"),
        max_identifier_chars=object.__getattribute__(config, "max_identifier_chars"),
    )


def _config_digest(config: SelectionAtomCatalogV1Config) -> str:
    return _hash_json(
        {
            "enabled": config.enabled,
            "max_source_bytes": config.max_source_bytes,
            "max_atoms": config.max_atoms,
            "max_total_atom_source_bytes": config.max_total_atom_source_bytes,
            "max_ancestry_steps": config.max_ancestry_steps,
            "max_identifier_chars": config.max_identifier_chars,
        }
    )


def _document_provenance(document: NativeDocumentIRV2) -> _DocumentProvenance:
    return _DocumentProvenance(
        document_schema_version=document.schema_version,
        source_digest="",
        input_bytes=document.input_bytes,
        parsed_bytes=document.parsed_bytes,
        source_bytes=0,
        source_complete=document.source_complete,
        source_mapping_complete=document.source_mapping_complete,
        parse_error_count=document.parse_error_count,
        ir_truncated=document.truncated,
        ir_truncation_reasons=tuple(document.truncation_reasons),
    )


def _provenance_rejection(
    config: SelectionAtomCatalogV1Config,
    reason: str,
    provenance: _DocumentProvenance,
    *,
    source_text_map_schema_version: str = "",
    source_text_map_reason: str = "",
    source_text_map_digest: str = "",
    source_text_map_transformed_span_count: int = 0,
    text_mapping_contract: str = "",
) -> SelectionAtomCatalogV1:
    return _rejection(
        config,
        reason,
        document_schema_version=provenance.document_schema_version,
        source_digest=provenance.source_digest,
        source_text_map_schema_version=source_text_map_schema_version,
        source_text_map_reason=source_text_map_reason,
        source_text_map_digest=source_text_map_digest,
        source_text_map_transformed_span_count=source_text_map_transformed_span_count,
        text_mapping_contract=text_mapping_contract,
        input_bytes=provenance.input_bytes,
        parsed_bytes=provenance.parsed_bytes,
        source_bytes=provenance.source_bytes,
        source_complete=provenance.source_complete,
        source_mapping_complete=provenance.source_mapping_complete,
        parse_error_count=provenance.parse_error_count,
        ir_truncated=provenance.ir_truncated,
        ir_truncation_reasons=provenance.ir_truncation_reasons,
    )


def _rejection(
    config: SelectionAtomCatalogV1Config,
    reason: str,
    *,
    enabled: bool = True,
    document_schema_version: str = "",
    source_digest: str = "",
    source_text_map_schema_version: str = "",
    source_text_map_reason: str = "",
    source_text_map_digest: str = "",
    source_text_map_transformed_span_count: int = 0,
    text_mapping_contract: str = "",
    input_bytes: int = 0,
    parsed_bytes: int = 0,
    source_bytes: int = 0,
    source_complete: bool = False,
    source_mapping_complete: bool = False,
    parse_error_count: int = 0,
    ir_truncated: bool = False,
    ir_truncation_reasons: tuple[str, ...] = (),
) -> SelectionAtomCatalogV1:
    return SelectionAtomCatalogV1(
        schema_version=SELECTION_ATOM_CATALOG_V1_SCHEMA,
        enabled=enabled,
        accepted=False,
        reason=reason,
        document_schema_version=document_schema_version,
        source_digest=source_digest,
        config_digest=_config_digest(config),
        catalog_digest="",
        source_text_map_schema_version=source_text_map_schema_version,
        source_text_map_reason=source_text_map_reason,
        source_text_map_digest=source_text_map_digest,
        source_text_map_transformed_span_count=source_text_map_transformed_span_count,
        text_mapping_contract=text_mapping_contract,
        input_bytes=input_bytes,
        parsed_bytes=parsed_bytes,
        source_bytes=source_bytes,
        source_complete=source_complete,
        source_mapping_complete=source_mapping_complete,
        parse_error_count=parse_error_count,
        ir_truncated=ir_truncated,
        ir_truncation_reasons=ir_truncation_reasons,
        atoms=(),
        candidate_text_run_count=0,
        skipped_whitespace_text_run_count=0,
        atom_source_bytes=0,
        ancestry_steps=0,
        kind_counts=(),
        deterministic=True,
    )


def _hash_json(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

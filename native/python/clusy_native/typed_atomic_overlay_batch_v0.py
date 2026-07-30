"""Unwired phase-one batch primitive for exact typed structure overlays.

The production crawler does not import this module. Its default configuration
is disabled. When explicitly enabled, one bounded HTML parse is shared by all
code, table, list, and math atoms; native code then creates one unchanged
``selection-certificate.v0`` wire record per non-overlapping source span in a
single graph clone and batch-wide validation/digest phase.

The returned Markdown is deterministic native serialization of the selected
source graph. This API cannot generate content and is not a model surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ._native import (
    NativeDocumentIRV2,
    NativeIRElementV2,
    NativeTypedAtomicOverlayItemV0,
    create_typed_atomic_overlay_batch_v0_native,
    verify_typed_atomic_overlay_batch_v0_native,
)
from .document_ir_v2 import DocumentIRV2Limits, extract_document_ir_v2

TYPED_ATOMIC_OVERLAY_BATCH_V0_SCHEMA: Final = "typed-atomic-overlay-batch.v0"
TYPED_ATOMIC_OVERLAY_BATCH_V0_MAX_ATOMS: Final = 1_024
_MAX_SOURCE_BYTES: Final = 4 * 1024 * 1024
_MAX_OUTPUT_BYTES: Final = 16 * 1024 * 1024
_MAX_CERTIFICATE_BYTES: Final = 2 * 1024 * 1024
_MAX_TOTAL_CERTIFICATE_BYTES: Final = 8 * 1024 * 1024


def _bounded_int(name: str, value: int, maximum: int) -> None:
    if type(value) is not int or value <= 0 or value > maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")


@dataclass(frozen=True, slots=True)
class TypedAtomicOverlayBatchV0Config:
    """Hard-bounded, explicit opt-in for the research-only batch builder."""

    enabled: bool = False
    max_source_bytes: int = 4 * 1024 * 1024
    max_atoms: int = 256
    max_output_bytes_per_atom: int = 512 * 1024

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be a bool")
        _bounded_int("max_source_bytes", self.max_source_bytes, _MAX_SOURCE_BYTES)
        _bounded_int(
            "max_atoms",
            self.max_atoms,
            TYPED_ATOMIC_OVERLAY_BATCH_V0_MAX_ATOMS,
        )
        _bounded_int(
            "max_output_bytes_per_atom",
            self.max_output_bytes_per_atom,
            _MAX_OUTPUT_BYTES,
        )


DEFAULT_TYPED_ATOMIC_OVERLAY_BATCH_V0_CONFIG = TypedAtomicOverlayBatchV0Config()


@dataclass(frozen=True, slots=True)
class TypedAtomicOverlayBatchV0:
    """One all-or-nothing source-backed batch build."""

    schema_version: str
    enabled: bool
    accepted: bool
    reason: str
    source_digest: str
    graph_digest: str
    atom_ids: tuple[str, ...]
    atom_kinds: tuple[str, ...]
    items: tuple[NativeTypedAtomicOverlayItemV0, ...]
    parse_count: int
    graph_clone_count: int
    deterministic: bool
    digest_is_authentication: bool = False


@dataclass(frozen=True, slots=True)
class TypedAtomicOverlayBatchVerificationV0:
    """Fail-closed verification result for a stored batch."""

    schema_version: str
    verified: bool
    reason: str
    items: tuple[NativeTypedAtomicOverlayItemV0, ...]
    parse_count: int
    graph_clone_count: int
    deterministic: bool
    digest_is_authentication: bool = False


def build_typed_atomic_overlay_batch_v0(
    html: str,
    *,
    config: TypedAtomicOverlayBatchV0Config = (DEFAULT_TYPED_ATOMIC_OVERLAY_BATCH_V0_CONFIG),
) -> TypedAtomicOverlayBatchV0:
    """Parse once and certify all outermost exact typed atoms as one batch."""

    config = _canonical_config(config)
    if type(html) is not str:
        raise TypeError("html must be a string")
    if not config.enabled:
        return _batch_rejection(enabled=False, reason="disabled")
    try:
        source_bytes = html.encode("utf-8")
    except UnicodeEncodeError:
        return _batch_rejection(enabled=True, reason="invalid_unicode")
    if len(source_bytes) > config.max_source_bytes:
        return _batch_rejection(enabled=True, reason="source_byte_budget")

    try:
        document = extract_document_ir_v2(
            html,
            limits=DocumentIRV2Limits(max_input_bytes=config.max_source_bytes),
        )
    except Exception:
        return _batch_rejection(enabled=True, reason="ir_extraction_failure")
    if not _document_is_complete(document):
        return _batch_rejection(
            enabled=True,
            reason="incomplete_ir",
            parse_count=1,
        )

    try:
        atom_ids = _outermost_typed_atom_ids(document)
    except ValueError:
        return _batch_rejection(
            enabled=True,
            reason="invalid_ir_topology",
            parse_count=1,
        )
    if not atom_ids:
        return _batch_rejection(
            enabled=True,
            reason="no_typed_atoms",
            parse_count=1,
        )
    if len(atom_ids) > config.max_atoms:
        return _batch_rejection(
            enabled=True,
            reason="atom_budget",
            parse_count=1,
        )
    try:
        items = tuple(
            create_typed_atomic_overlay_batch_v0_native(
                document,
                list(atom_ids),
                max_output_bytes=config.max_output_bytes_per_atom,
            )
        )
    except (TypeError, ValueError):
        return _batch_rejection(
            enabled=True,
            reason="certificate_provenance_rejected",
            parse_count=1,
            graph_clone_count=1,
        )
    if len(items) != len(atom_ids):
        return _batch_rejection(
            enabled=True,
            reason="native_batch_cardinality",
            parse_count=1,
            graph_clone_count=1,
        )
    if tuple(item.selected_id for item in items) != atom_ids:
        return _batch_rejection(
            enabled=True,
            reason="native_batch_order",
            parse_count=1,
            graph_clone_count=1,
        )
    if not all(item.verified and item.deterministic for item in items):
        return _batch_rejection(
            enabled=True,
            reason="native_batch_unverified",
            parse_count=1,
            graph_clone_count=1,
        )
    return TypedAtomicOverlayBatchV0(
        schema_version=TYPED_ATOMIC_OVERLAY_BATCH_V0_SCHEMA,
        enabled=True,
        accepted=True,
        reason="accepted",
        source_digest=items[0].source_digest,
        graph_digest=items[0].graph_digest,
        atom_ids=atom_ids,
        atom_kinds=tuple(item.atom_kind for item in items),
        items=items,
        parse_count=1,
        graph_clone_count=1,
        deterministic=True,
    )


def verify_typed_atomic_overlay_batch_v0(
    html: str,
    batch: object,
    *,
    config: TypedAtomicOverlayBatchV0Config,
) -> TypedAtomicOverlayBatchVerificationV0:
    """Verify stored certificates with one fresh parse and one graph clone."""

    config = _canonical_config(config)
    if type(html) is not str:
        raise TypeError("html must be a string")
    if type(batch) is not TypedAtomicOverlayBatchV0:
        return _verification_rejection("invalid_batch_type")
    if not config.enabled:
        return _verification_rejection("disabled")
    if not batch.accepted or not batch.items:
        return _verification_rejection("batch_not_accepted")
    if len(batch.items) > config.max_atoms:
        return _verification_rejection("atom_budget")
    certificates = tuple(item.certificate for item in batch.items)
    try:
        items = verify_typed_atomic_overlay_certificates_v0(
            html,
            certificates,
            config=config,
        )
    except (TypeError, ValueError):
        return _verification_rejection(
            "certificate_provenance_rejected",
            parse_count=1,
            graph_clone_count=1,
        )
    expected = tuple(_item_identity(item) for item in batch.items)
    actual = tuple(_item_identity(item) for item in items)
    if actual != expected:
        return _verification_rejection(
            "batch_mismatch",
            parse_count=1,
            graph_clone_count=1,
        )
    return TypedAtomicOverlayBatchVerificationV0(
        schema_version=TYPED_ATOMIC_OVERLAY_BATCH_V0_SCHEMA,
        verified=True,
        reason="verified",
        items=items,
        parse_count=1,
        graph_clone_count=1,
        deterministic=True,
    )


def verify_typed_atomic_overlay_certificates_v0(
    html: str,
    certificates: tuple[bytes, ...] | list[bytes],
    *,
    config: TypedAtomicOverlayBatchV0Config,
) -> tuple[NativeTypedAtomicOverlayItemV0, ...]:
    """Low-level strict replay used by the batch verifier and tamper tests."""

    config = _canonical_config(config)
    if type(html) is not str:
        raise TypeError("html must be a string")
    if not config.enabled:
        raise ValueError("typed-atomic-overlay-batch.v0: disabled")
    try:
        source_bytes = html.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("typed-atomic-overlay-batch.v0: invalid Unicode") from error
    if len(source_bytes) > config.max_source_bytes:
        raise ValueError("typed-atomic-overlay-batch.v0: source byte budget")
    bounded_certificates = _bounded_certificates(certificates, config.max_atoms)
    document = extract_document_ir_v2(
        html,
        limits=DocumentIRV2Limits(max_input_bytes=config.max_source_bytes),
    )
    if not _document_is_complete(document):
        raise ValueError("typed-atomic-overlay-batch.v0: incomplete IR")
    return tuple(
        verify_typed_atomic_overlay_batch_v0_native(
            document,
            bounded_certificates,
            max_output_bytes=config.max_output_bytes_per_atom,
        )
    )


def _outermost_typed_atom_ids(document: NativeDocumentIRV2) -> tuple[str, ...]:
    elements = tuple(sorted(document.elements, key=lambda element: element.order))
    elements_by_id = {element.id: element for element in elements}
    math_node_ids = {math.node_id for math in document.math}
    typed_ids = {
        element.id for element in elements if _atom_kind(element, math_node_ids) is not None
    }
    atomic_barrier_ids = typed_ids | {element.id for element in elements if element.tag == "figure"}
    output: list[str] = []
    for element in elements:
        if element.id not in typed_ids:
            continue
        parent_id = element.parent_id
        seen: set[str] = set()
        nested = False
        while parent_id is not None:
            if parent_id in seen:
                raise ValueError("typed-atomic-overlay-batch.v0: IR parent cycle")
            seen.add(parent_id)
            if parent_id in atomic_barrier_ids:
                nested = True
                break
            parent = elements_by_id.get(parent_id)
            if parent is None:
                raise ValueError("typed-atomic-overlay-batch.v0: unknown IR parent")
            parent_id = parent.parent_id
        if not nested:
            output.append(element.id)
    return tuple(output)


def _atom_kind(
    element: NativeIRElementV2,
    math_node_ids: set[str],
) -> str | None:
    if element.tag in {"pre", "code"}:
        return "code"
    if element.tag == "table":
        return "table"
    if element.tag in {"ul", "ol", "dl"}:
        return "list"
    if element.id in math_node_ids:
        return "math"
    return None


def _document_is_complete(document: NativeDocumentIRV2) -> bool:
    return (
        document.schema_version == "ordered-dom-ir.v2"
        and document.source_complete
        and document.source_mapping_complete
        and document.parse_error_count == 0
        and not document.truncated
        and not document.input_truncated
        and not document.nodes_truncated
        and not document.depth_truncated
        and not document.elements_truncated
        and not document.text_runs_truncated
        and document.text_truncated_runs == 0
        and not document.table_grid_truncated
        and document.math_truncated_nodes == 0
    )


def _bounded_certificates(
    certificates: tuple[bytes, ...] | list[bytes],
    max_atoms: int,
) -> list[bytes]:
    if type(certificates) not in {tuple, list}:
        raise TypeError("certificates must be a tuple or list of bytes")
    if not certificates or len(certificates) > max_atoms:
        raise ValueError("typed-atomic-overlay-batch.v0: certificate count budget")
    output: list[bytes] = []
    total_bytes = 0
    for certificate in certificates:
        if type(certificate) is not bytes:
            raise TypeError("certificate must be bytes")
        if not certificate or len(certificate) > _MAX_CERTIFICATE_BYTES:
            raise ValueError("typed-atomic-overlay-batch.v0: certificate byte budget")
        total_bytes += len(certificate)
        if total_bytes > _MAX_TOTAL_CERTIFICATE_BYTES:
            raise ValueError("typed-atomic-overlay-batch.v0: aggregate certificate byte budget")
        output.append(certificate)
    return output


def _item_identity(item: NativeTypedAtomicOverlayItemV0) -> tuple[object, ...]:
    return (
        item.contract_version,
        item.atom_kind,
        item.selected_id,
        item.source_order,
        item.source_start,
        item.source_end,
        item.source_span_digest,
        item.source_digest,
        item.graph_digest,
        item.output_digest,
        item.certificate_digest,
        item.markdown,
        item.certificate,
        item.verified,
        item.deterministic,
    )


def _canonical_config(
    config: TypedAtomicOverlayBatchV0Config,
) -> TypedAtomicOverlayBatchV0Config:
    if type(config) is not TypedAtomicOverlayBatchV0Config:
        raise TypeError("config must be TypedAtomicOverlayBatchV0Config")
    return TypedAtomicOverlayBatchV0Config(
        enabled=object.__getattribute__(config, "enabled"),
        max_source_bytes=object.__getattribute__(config, "max_source_bytes"),
        max_atoms=object.__getattribute__(config, "max_atoms"),
        max_output_bytes_per_atom=object.__getattribute__(
            config,
            "max_output_bytes_per_atom",
        ),
    )


def _batch_rejection(
    *,
    enabled: bool,
    reason: str,
    parse_count: int = 0,
    graph_clone_count: int = 0,
) -> TypedAtomicOverlayBatchV0:
    return TypedAtomicOverlayBatchV0(
        schema_version=TYPED_ATOMIC_OVERLAY_BATCH_V0_SCHEMA,
        enabled=enabled,
        accepted=False,
        reason=reason,
        source_digest="",
        graph_digest="",
        atom_ids=(),
        atom_kinds=(),
        items=(),
        parse_count=parse_count,
        graph_clone_count=graph_clone_count,
        deterministic=True,
    )


def _verification_rejection(
    reason: str,
    *,
    parse_count: int = 0,
    graph_clone_count: int = 0,
) -> TypedAtomicOverlayBatchVerificationV0:
    return TypedAtomicOverlayBatchVerificationV0(
        schema_version=TYPED_ATOMIC_OVERLAY_BATCH_V0_SCHEMA,
        verified=False,
        reason=reason,
        items=(),
        parse_count=parse_count,
        graph_clone_count=graph_clone_count,
        deterministic=True,
    )

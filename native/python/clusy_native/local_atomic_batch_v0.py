"""Typed façade for the unwired local-atomic certificate batch bridge.

The bridge is intentionally limited to exact ``pre`` and ``table`` roots.
Each native call clones the immutable IR once, performs one batch-wide graph
validation and digest, then isolates source-provenance rejection per requested
atom. Accepted creation records contain canonical
``selection-certificate.v0`` bytes in the ``local_atomic`` scope. Verification
replays each accepted certificate exactly against a fresh immutable IR clone.

This module is research-only infrastructure for the default-disabled atomic
structure overlay. It is not imported by a production extraction route.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable

from ._native import (
    NativeDocumentIRV2,
    NativeLocalAtomicBatchItemV0,
    create_local_atomic_selection_batch_v0_native,
    verify_and_replay_local_atomic_selection_batch_v0_native,
)

LOCAL_ATOMIC_SELECTION_BATCH_V0_CONTRACT: Final = "local-atomic-selection-batch.v0"
LOCAL_ATOMIC_SELECTION_BATCH_V0_MAX_ATOMS: Final = 1_024
LOCAL_ATOMIC_SELECTION_BATCH_V0_MAX_OUTPUT_BYTES: Final = 16 * 1024 * 1024
LOCAL_ATOMIC_SELECTION_BATCH_V0_MAX_CERTIFICATE_BYTES: Final = 2 * 1024 * 1024
LOCAL_ATOMIC_SELECTION_BATCH_V0_MAX_TOTAL_CERTIFICATE_BYTES: Final = 8 * 1024 * 1024
LOCAL_ATOMIC_SELECTION_BATCH_V0_MAX_TOTAL_OUTPUT_BYTES: Final = 8 * 1024 * 1024
_MAX_ID_BYTES: Final = 256

type LocalAtomicBatchItemV0 = NativeLocalAtomicBatchItemV0


def create_local_atomic_selection_batch_v0(
    document: NativeDocumentIRV2,
    selected_ids: Iterable[str],
    *,
    max_output_bytes: int,
    max_total_certificate_bytes: int,
    max_total_output_bytes: int,
) -> tuple[LocalAtomicBatchItemV0, ...]:
    """Create independent, scope-bound certificates with one native clone."""

    bounded_ids = _bounded_ids(selected_ids)
    _validate_limits(
        max_output_bytes=max_output_bytes,
        max_total_certificate_bytes=max_total_certificate_bytes,
        max_total_output_bytes=max_total_output_bytes,
    )
    items = tuple(
        create_local_atomic_selection_batch_v0_native(
            document,
            bounded_ids,
            max_output_bytes=max_output_bytes,
            max_total_certificate_bytes=max_total_certificate_bytes,
            max_total_output_bytes=max_total_output_bytes,
        )
    )
    _validate_native_items(
        items,
        bounded_ids,
        require_verified=False,
        max_total_certificate_bytes=max_total_certificate_bytes,
        max_total_output_bytes=max_total_output_bytes,
    )
    return items


def verify_and_replay_local_atomic_selection_batch_v0(
    document: NativeDocumentIRV2,
    selected_ids: Iterable[str],
    certificates: Iterable[bytes],
    *,
    max_output_bytes: int,
    max_total_certificate_bytes: int,
    max_total_output_bytes: int,
) -> tuple[LocalAtomicBatchItemV0, ...]:
    """Verify paired IDs/certificates and replay each valid item exactly."""

    bounded_ids = _bounded_ids(selected_ids)
    bounded_certificates = _bounded_certificates(certificates, len(bounded_ids))
    _validate_limits(
        max_output_bytes=max_output_bytes,
        max_total_certificate_bytes=max_total_certificate_bytes,
        max_total_output_bytes=max_total_output_bytes,
    )
    items = tuple(
        verify_and_replay_local_atomic_selection_batch_v0_native(
            document,
            bounded_ids,
            bounded_certificates,
            max_output_bytes=max_output_bytes,
            max_total_certificate_bytes=max_total_certificate_bytes,
            max_total_output_bytes=max_total_output_bytes,
        )
    )
    _validate_native_items(
        items,
        bounded_ids,
        require_verified=True,
        max_total_certificate_bytes=max_total_certificate_bytes,
        max_total_output_bytes=max_total_output_bytes,
    )
    return items


def _bounded_ids(selected_ids: Iterable[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for selected_id in selected_ids:
        if len(output) == LOCAL_ATOMIC_SELECTION_BATCH_V0_MAX_ATOMS:
            raise ValueError("local-atomic-selection-batch.v0: too many selections")
        if (
            type(selected_id) is not str
            or not selected_id
            or len(selected_id.encode("utf-8")) > _MAX_ID_BYTES
            or not selected_id.isascii()
            or any(
                not (character.isalnum() or character in "-_")
                for character in selected_id
            )
        ):
            raise ValueError(
                "local-atomic-selection-batch.v0: selection ID is not canonical"
            )
        if selected_id in seen:
            raise ValueError(
                "local-atomic-selection-batch.v0: duplicate selection ID"
            )
        seen.add(selected_id)
        output.append(selected_id)
    if not output:
        raise ValueError("local-atomic-selection-batch.v0: empty selection batch")
    return output


def _bounded_certificates(
    certificates: Iterable[bytes],
    expected_count: int,
) -> list[bytes]:
    output: list[bytes] = []
    total_bytes = 0
    for certificate in certificates:
        if len(output) == LOCAL_ATOMIC_SELECTION_BATCH_V0_MAX_ATOMS:
            raise ValueError("local-atomic-selection-batch.v0: too many certificates")
        if (
            type(certificate) is not bytes
            or not certificate
            or len(certificate) > LOCAL_ATOMIC_SELECTION_BATCH_V0_MAX_CERTIFICATE_BYTES
        ):
            raise ValueError(
                "local-atomic-selection-batch.v0: certificate byte budget"
            )
        total_bytes += len(certificate)
        if total_bytes > LOCAL_ATOMIC_SELECTION_BATCH_V0_MAX_TOTAL_CERTIFICATE_BYTES:
            raise ValueError(
                "local-atomic-selection-batch.v0: aggregate certificate byte budget"
            )
        output.append(certificate)
    if len(output) != expected_count:
        raise ValueError(
            "local-atomic-selection-batch.v0: ID and certificate counts differ"
        )
    return output


def _validate_limits(
    *,
    max_output_bytes: int,
    max_total_certificate_bytes: int,
    max_total_output_bytes: int,
) -> None:
    _bounded_positive_int(
        "max_output_bytes",
        max_output_bytes,
        LOCAL_ATOMIC_SELECTION_BATCH_V0_MAX_OUTPUT_BYTES,
    )
    _bounded_positive_int(
        "max_total_certificate_bytes",
        max_total_certificate_bytes,
        LOCAL_ATOMIC_SELECTION_BATCH_V0_MAX_TOTAL_CERTIFICATE_BYTES,
    )
    _bounded_positive_int(
        "max_total_output_bytes",
        max_total_output_bytes,
        LOCAL_ATOMIC_SELECTION_BATCH_V0_MAX_TOTAL_OUTPUT_BYTES,
    )


def _bounded_positive_int(name: str, value: int, hard_maximum: int) -> None:
    if type(value) is not int or value <= 0 or value > hard_maximum:
        raise ValueError(f"{name} must be between 1 and {hard_maximum}")


def _validate_native_items(
    items: tuple[LocalAtomicBatchItemV0, ...],
    selected_ids: list[str],
    *,
    require_verified: bool,
    max_total_certificate_bytes: int,
    max_total_output_bytes: int,
) -> None:
    if len(items) != len(selected_ids):
        raise RuntimeError("local atomic native batch cardinality mismatch")
    total_certificate_bytes = 0
    total_output_bytes = 0
    for request_index, (item, selected_id) in enumerate(
        zip(items, selected_ids, strict=True)
    ):
        if (
            type(item) is not NativeLocalAtomicBatchItemV0
            or item.contract_version != LOCAL_ATOMIC_SELECTION_BATCH_V0_CONTRACT
            or item.validation_scope != "local_atomic"
            or item.request_index != request_index
            or item.selected_id != selected_id
            or item.atom_kind not in {"", "code", "table"}
            or not item.deterministic
        ):
            raise RuntimeError("local atomic native batch identity mismatch")
        if item.accepted:
            if (
                item.reason != "accepted"
                or not item.certificate
                or not item.markdown
                or item.source_order is None
                or item.source_start is None
                or item.source_end is None
                or item.source_start >= item.source_end
                or item.atom_kind not in {"code", "table"}
                or any(
                    len(digest) != 64
                    for digest in (
                        item.source_span_digest,
                        item.source_digest,
                        item.graph_digest,
                        item.output_digest,
                        item.certificate_digest,
                    )
                )
                or item.verified is not require_verified
            ):
                raise RuntimeError("local atomic native accepted item is malformed")
            total_certificate_bytes += len(item.certificate)
            total_output_bytes += len(item.markdown.encode("utf-8"))
        elif (
            item.reason == "accepted"
            or item.certificate
            or item.markdown
            or item.source_order is not None
            or item.source_start is not None
            or item.source_end is not None
            or item.source_span_digest
            or item.source_digest
            or item.graph_digest
            or item.output_digest
            or item.certificate_digest
            or item.verified
        ):
            raise RuntimeError("local atomic native rejected item retained payload")
    if total_certificate_bytes > max_total_certificate_bytes:
        raise RuntimeError("local atomic native aggregate certificate limit bypass")
    if total_output_bytes > max_total_output_bytes:
        raise RuntimeError("local atomic native aggregate output limit bypass")

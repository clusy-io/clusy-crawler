"""Typed, native-only façade for the unwired selection-certificate v0 scaffold.

The Python layer performs no certificate parsing, digesting, validation, or
serialization. Rust remains authoritative for creation, canonical decoding,
verification, and all-or-nothing replay.
"""

from collections.abc import Iterable

from ._native import (
    NativeDocumentIRV2,
    NativeSelectionCertificateV0,
    NativeSelectionReceiptV0,
    NativeSelectionReplayV0,
    create_selection_certificate_v0_native,
    decode_selection_certificate_v0_native,
    verify_and_replay_selection_certificate_v0_native,
)

DEFAULT_SELECTION_CERTIFICATE_V0_MAX_OUTPUT_BYTES = 4 * 1024 * 1024
SELECTION_CERTIFICATE_V0_MAX_SELECTIONS = 16_384
_SELECTION_CERTIFICATE_V0_MAX_ID_BYTES = 256

type SelectionCertificateV0 = NativeSelectionCertificateV0
type SelectionReceiptV0 = NativeSelectionReceiptV0
type SelectionReplayV0 = NativeSelectionReplayV0


def create_selection_certificate_v0(
    document: NativeDocumentIRV2,
    selected_ids: Iterable[str],
    *,
    max_output_bytes: int = DEFAULT_SELECTION_CERTIFICATE_V0_MAX_OUTPUT_BYTES,
) -> SelectionCertificateV0:
    """Create a local deterministic-replay identity, not a signature.

    The bounded Python collection prevents an untrusted iterable from being
    exhausted before the native hard limit is enforced. Native code remains
    authoritative for ID validation and certificate eligibility.
    """

    bounded_ids: list[str] = []
    for value in selected_ids:
        if len(bounded_ids) == SELECTION_CERTIFICATE_V0_MAX_SELECTIONS:
            raise ValueError("selection-certificate.v0: too many selected events")
        if (
            not isinstance(value, str)
            or not value
            or len(value) > _SELECTION_CERTIFICATE_V0_MAX_ID_BYTES
            or not value.isascii()
            or any(not (character.isalnum() or character in "-_") for character in value)
        ):
            raise ValueError("selection-certificate.v0: selection ID is not canonical")
        bounded_ids.append(value)
    return create_selection_certificate_v0_native(
        document,
        bounded_ids,
        max_output_bytes=max_output_bytes,
    )


def decode_selection_certificate_v0(encoded: bytes) -> SelectionCertificateV0:
    """Strictly decode one canonical certificate in native code."""

    return decode_selection_certificate_v0_native(encoded)


def verify_and_replay_selection_certificate_v0(
    document: NativeDocumentIRV2,
    certificate: SelectionCertificateV0 | bytes,
    *,
    max_output_bytes: int = DEFAULT_SELECTION_CERTIFICATE_V0_MAX_OUTPUT_BYTES,
) -> SelectionReplayV0:
    """Verify and replay against this local document; this is not authentication."""

    encoded = (
        certificate.encoded
        if isinstance(certificate, NativeSelectionCertificateV0)
        else certificate
    )
    return verify_and_replay_selection_certificate_v0_native(
        document,
        encoded,
        max_output_bytes=max_output_bytes,
    )

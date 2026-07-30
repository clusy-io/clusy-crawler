from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from clusy_native import (
    create_local_atomic_selection_batch_v0,
    create_local_atomic_selection_certificate_v0,
    create_selection_certificate_v0,
    decode_selection_certificate_v0,
    extract_document_ir_v2,
    verify_and_replay_local_atomic_selection_batch_v0,
    verify_and_replay_local_atomic_selection_certificate_v0,
)

_MAX_CERTIFICATES = 8 * 1024 * 1024
_MAX_OUTPUT = 8 * 1024 * 1024


def _document(body: str) -> str:
    return (
        "<!doctype html><html><head><title>local batch</title></head>"
        f"<body><main>{body}</main></body></html>"
    )


def _atomic_ids(html: str) -> tuple[object, tuple[str, ...]]:
    document = extract_document_ir_v2(html)
    ids = tuple(
        element.id
        for element in sorted(document.elements, key=lambda item: item.order)
        if element.tag in {"pre", "table"}
    )
    return document, ids


def _create(
    document: object,
    ids: tuple[str, ...],
    *,
    max_total_certificate_bytes: int = _MAX_CERTIFICATES,
    max_total_output_bytes: int = _MAX_OUTPUT,
) -> tuple[object, ...]:
    return create_local_atomic_selection_batch_v0(
        document,  # type: ignore[arg-type]
        ids,
        max_output_bytes=512 * 1024,
        max_total_certificate_bytes=max_total_certificate_bytes,
        max_total_output_bytes=max_total_output_bytes,
    )


def test_batch_bytes_and_markdown_are_exactly_legacy_local_atomic() -> None:
    html = _document(
        '<pre><code class="language-python">def answer():\n    return 42</code></pre>'
        "<table><thead><tr><th>Name</th><th>Score</th></tr></thead>"
        "<tbody><tr><td>Clusy</td><td>42</td></tr></tbody></table>"
    )
    document, ids = _atomic_ids(html)
    items = _create(document, ids)

    assert len(items) == 2
    for request_index, (selected_id, item) in enumerate(
        zip(ids, items, strict=True)
    ):
        legacy = create_local_atomic_selection_certificate_v0(
            document,  # type: ignore[arg-type]
            [selected_id],
            max_output_bytes=512 * 1024,
        )
        legacy_replay = verify_and_replay_local_atomic_selection_certificate_v0(
            document,  # type: ignore[arg-type]
            legacy,
            max_output_bytes=512 * 1024,
        )
        assert item.accepted
        assert not item.verified
        assert item.request_index == request_index
        assert item.validation_scope == "local_atomic"
        assert item.certificate == legacy.encoded
        assert item.markdown == legacy_replay.markdown
        assert decode_selection_certificate_v0(item.certificate).validation_scope == (
            "local_atomic"
        )

    replayed = verify_and_replay_local_atomic_selection_batch_v0(
        document,  # type: ignore[arg-type]
        ids,
        (item.certificate for item in items),
        max_output_bytes=512 * 1024,
        max_total_certificate_bytes=_MAX_CERTIFICATES,
        max_total_output_bytes=_MAX_OUTPUT,
    )
    assert all(item.accepted and item.verified for item in replayed)
    assert tuple((item.certificate, item.markdown) for item in replayed) == tuple(
        (item.certificate, item.markdown) for item in items
    )


def test_one_bad_source_atom_does_not_poison_valid_siblings() -> None:
    html = _document(
        "<pre>bad &amp; transformed</pre>"
        "<pre>good literal</pre>"
        "<table><tr><th>Name</th></tr><tr><td>Clusy</td></tr></table>"
    )
    document, ids = _atomic_ids(html)
    items = _create(document, ids)

    assert len(items) == 3
    assert not items[0].accepted
    assert items[0].reason == "certificate_provenance_rejected"
    assert items[0].certificate == b""
    assert items[1].accepted
    assert items[2].accepted


def test_tamper_scope_pairing_and_aggregate_limits_fail_closed_per_item() -> None:
    html = _document("<pre>first literal</pre><pre>second literal</pre>")
    document, ids = _atomic_ids(html)
    created = _create(document, ids)
    certificates = [item.certificate for item in created]

    tampered = bytearray(certificates[0])
    tampered[len(tampered) // 2] ^= 1
    replayed = verify_and_replay_local_atomic_selection_batch_v0(
        document,  # type: ignore[arg-type]
        ids,
        [bytes(tampered), certificates[1]],
        max_output_bytes=512 * 1024,
        max_total_certificate_bytes=_MAX_CERTIFICATES,
        max_total_output_bytes=_MAX_OUTPUT,
    )
    assert not replayed[0].accepted
    assert replayed[0].reason == "certificate_replay_rejected"
    assert replayed[1].accepted and replayed[1].verified

    swapped = verify_and_replay_local_atomic_selection_batch_v0(
        document,  # type: ignore[arg-type]
        ids,
        list(reversed(certificates)),
        max_output_bytes=512 * 1024,
        max_total_certificate_bytes=_MAX_CERTIFICATES,
        max_total_output_bytes=_MAX_OUTPUT,
    )
    assert not any(item.accepted for item in swapped)

    full_scope = create_selection_certificate_v0(
        document,  # type: ignore[arg-type]
        [ids[0]],
        max_output_bytes=512 * 1024,
    )
    wrong_scope = verify_and_replay_local_atomic_selection_batch_v0(
        document,  # type: ignore[arg-type]
        [ids[0]],
        [full_scope.encoded],
        max_output_bytes=512 * 1024,
        max_total_certificate_bytes=_MAX_CERTIFICATES,
        max_total_output_bytes=_MAX_OUTPUT,
    )
    assert not wrong_scope[0].accepted
    assert wrong_scope[0].reason == "certificate_replay_rejected"

    capped = _create(
        document,
        ids,
        max_total_certificate_bytes=len(certificates[0]),
    )
    assert capped[0].accepted
    assert not capped[1].accepted
    assert capped[1].reason == "aggregate_certificate_byte_budget"


def test_batch_boundary_rejects_duplicates_mismatched_counts_and_bad_limits() -> None:
    html = _document("<pre>literal</pre>")
    document, ids = _atomic_ids(html)

    with pytest.raises(ValueError, match="duplicate"):
        _create(document, (ids[0], ids[0]))
    with pytest.raises(ValueError, match="counts differ"):
        verify_and_replay_local_atomic_selection_batch_v0(
            document,  # type: ignore[arg-type]
            ids,
            [],
            max_output_bytes=512 * 1024,
            max_total_certificate_bytes=_MAX_CERTIFICATES,
            max_total_output_bytes=_MAX_OUTPUT,
        )
    with pytest.raises(ValueError, match="max_total_output_bytes"):
        create_local_atomic_selection_batch_v0(
            document,  # type: ignore[arg-type]
            ids,
            max_output_bytes=512 * 1024,
            max_total_certificate_bytes=_MAX_CERTIFICATES,
            max_total_output_bytes=0,
        )


def test_batch_is_deterministic_across_parallel_calls() -> None:
    html = _document(
        "".join(f"<pre>parallel literal {index}</pre>" for index in range(8))
    )
    document, ids = _atomic_ids(html)

    def build(_: int) -> tuple[tuple[object, ...], ...]:
        return tuple(
            (
                item.selected_id,
                item.accepted,
                item.reason,
                item.certificate,
                item.markdown,
                item.certificate_digest,
            )
            for item in _create(document, ids)
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        outputs = list(pool.map(build, range(12)))
    assert len(set(outputs)) == 1

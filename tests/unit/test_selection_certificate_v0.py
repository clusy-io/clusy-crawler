from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest
from clusy_native import (
    SELECTION_CERTIFICATE_V0_MAX_SELECTIONS,
    NativeDocumentIRV2,
    create_local_atomic_selection_certificate_v0,
    create_selection_certificate_v0,
    decode_selection_certificate_v0,
    extract_document_ir_v2,
    reconstruct_document_ir_v2,
    verify_and_replay_local_atomic_selection_certificate_v0,
    verify_and_replay_selection_certificate_v0,
)
from clusy_native._native import create_selection_certificate_v0_native

if TYPE_CHECKING:
    from collections.abc import Iterator


def _document(body: str) -> str:
    return (
        "<!doctype html><html><head><title>certificate fixture</title></head>"
        f"<body>{body}</body></html>"
    )


def _text_id(document: NativeDocumentIRV2, text: str) -> str:
    return next(run.id for run in document.text_runs if run.text == text)


def _tag_id(document: NativeDocumentIRV2, tag: str) -> str:
    return next(element.id for element in document.elements if element.tag == tag)


def _framed_digest(domain: bytes, value: bytes) -> str:
    payload = len(domain).to_bytes(8, "big") + domain + len(value).to_bytes(8, "big") + value
    return hashlib.sha256(payload).hexdigest()


def test_certificate_round_trip_binds_utf8_source_graph_and_atomic_output() -> None:
    html = _document("<main><p>你好 😀 café</p><p>second</p></main>")
    document = extract_document_ir_v2(html)
    selected_ids = [_text_id(document, "你好 😀 café"), _text_id(document, "second")]

    certificate = create_selection_certificate_v0(
        document,
        selected_ids,
        max_output_bytes=4_096,
    )
    decoded = decode_selection_certificate_v0(certificate.encoded)
    replay = verify_and_replay_selection_certificate_v0(
        document,
        decoded,
        max_output_bytes=4_096,
    )
    existing_serializer = reconstruct_document_ir_v2(document, selected_ids=selected_ids)

    assert certificate.contract_version == "selection-certificate.v0"
    assert certificate.wire_version == 0
    assert certificate.source_digest == _framed_digest(
        b"clusy-selection-certificate-source-v0",
        html.encode(),
    )
    assert decoded.certificate_digest == certificate.certificate_digest
    assert decoded.selected_ids == selected_ids
    assert replay.markdown == "你好 😀 café\n\nsecond"
    assert replay.markdown == existing_serializer.markdown
    assert replay.receipt.verified
    assert replay.receipt.deterministic
    assert replay.receipt.output_digest == certificate.output_digest
    assert replay.receipt.output_bytes == len(replay.markdown.encode())


def test_certificate_rejects_duplicate_order_overlap_unknown_and_cross_document() -> None:
    first = extract_document_ir_v2(_document("<main><p>one</p><p>two</p></main>"))
    one = _text_id(first, "one")
    two = _text_id(first, "two")

    with pytest.raises(ValueError, match="duplicate selection"):
        create_selection_certificate_v0(first, [one, one])
    with pytest.raises(ValueError, match="strict event order"):
        create_selection_certificate_v0(first, [two, one])
    with pytest.raises(ValueError, match="unknown selection"):
        create_selection_certificate_v0(first, ["unknown"])
    with pytest.raises(ValueError, match="ancestor-overlapping"):
        create_selection_certificate_v0(first, [_tag_id(first, "main"), one])

    certificate = create_selection_certificate_v0(first, [one])
    second = extract_document_ir_v2(_document("<main><p>changed</p><p>two</p></main>"))
    with pytest.raises(ValueError, match="source digest mismatch"):
        verify_and_replay_selection_certificate_v0(second, certificate)


def test_certificate_rejects_noncanonical_and_ambiguous_coordinates() -> None:
    entity = extract_document_ir_v2(_document("<p>&amp;</p>"))
    with pytest.raises(ValueError, match="tokenizer"):
        create_selection_certificate_v0(entity, [_text_id(entity, "&")])

    repeated = extract_document_ir_v2(_document("<p>same<em>x</em>same</p>"))
    with pytest.raises(ValueError, match="reliable complete source span"):
        create_selection_certificate_v0(repeated, [_text_id(repeated, "same")])

    fostered = extract_document_ir_v2(_document("<table>foster<tr><td>x</td></tr></table>"))
    with pytest.raises(ValueError, match="provenance|DOM ancestry"):
        create_selection_certificate_v0(fostered, [_text_id(fostered, "foster")])

    valid = extract_document_ir_v2(_document("<p>safe</p>"))
    certificate = create_selection_certificate_v0(valid, [_text_id(valid, "safe")])
    with pytest.raises(ValueError, match="trailing certificate bytes"):
        decode_selection_certificate_v0(certificate.encoded + b"\x00")

    for body, text in [
        ("<p>&#65;<!--A--></p>", "A"),
        ("<p>&copy;<!--©--></p>", "©"),
        ("<p>\r\n<!--\n--></p>", "\n"),
        ("<p>&#65;<span hidden>A</span></p>", "A"),
    ]:
        aliased = extract_document_ir_v2(_document(body))
        with pytest.raises(ValueError):
            create_selection_certificate_v0(
                aliased,
                [_text_id(aliased, text)],
            )


def test_certificate_selection_input_is_bounded_before_native_copy() -> None:
    document = extract_document_ir_v2(_document("<p>safe</p>"))
    selected_id = _text_id(document, "safe")
    consumed = 0

    def untrusted_ids() -> Iterator[str]:
        nonlocal consumed
        for _ in range(SELECTION_CERTIFICATE_V0_MAX_SELECTIONS + 100_000):
            consumed += 1
            yield selected_id

    with pytest.raises(ValueError, match="too many selected events"):
        create_selection_certificate_v0(document, untrusted_ids())
    assert consumed == SELECTION_CERTIFICATE_V0_MAX_SELECTIONS + 1

    oversized_id_consumed = 0

    def oversized_id() -> Iterator[str]:
        nonlocal oversized_id_consumed
        oversized_id_consumed += 1
        yield "x" * 1_000_000
        oversized_id_consumed += 1
        yield selected_id

    with pytest.raises(ValueError, match="not canonical"):
        create_selection_certificate_v0(document, oversized_id())
    assert oversized_id_consumed == 1

    with pytest.raises(ValueError, match="too many selected events"):
        create_selection_certificate_v0_native(
            document,
            [selected_id] * (SELECTION_CERTIFICATE_V0_MAX_SELECTIONS + 1),
        )
    with pytest.raises(ValueError, match="not canonical"):
        create_selection_certificate_v0_native(
            document,
            ["é" * 1_000_000],
        )
    with pytest.raises(ValueError, match="must not be empty"):
        create_selection_certificate_v0(document, [])


def test_certificate_atomic_structures_and_output_cap_fail_closed() -> None:
    document = extract_document_ir_v2(
        _document(
            "<table><tbody><tr><td>cell</td></tr></tbody></table><pre><code>abcdefghij</code></pre>"
        )
    )

    with pytest.raises(ValueError, match="whole structure"):
        create_selection_certificate_v0(document, [_text_id(document, "cell")])
    table = create_selection_certificate_v0(document, [_tag_id(document, "table")])
    assert "<table>" in verify_and_replay_selection_certificate_v0(document, table).markdown

    pre_id = _tag_id(document, "pre")
    with pytest.raises(ValueError, match="output limit"):
        create_selection_certificate_v0(document, [pre_id], max_output_bytes=5)
    pre = create_selection_certificate_v0(document, [pre_id], max_output_bytes=128)
    with pytest.raises(ValueError, match="output limit"):
        verify_and_replay_selection_certificate_v0(document, pre, max_output_bytes=5)


def test_certificate_is_deterministic_across_parallel_native_calls() -> None:
    html = _document("<main><p>parallel 😀</p></main>")

    def issue(_: int) -> tuple[bytes, str]:
        document = extract_document_ir_v2(html)
        certificate = create_selection_certificate_v0(
            document,
            [_text_id(document, "parallel 😀")],
        )
        replay = verify_and_replay_selection_certificate_v0(document, certificate)
        return certificate.encoded, replay.markdown

    with ThreadPoolExecutor(max_workers=4) as pool:
        outputs = list(pool.map(issue, range(16)))

    assert len(set(outputs)) == 1


def test_local_atomic_certificate_ignores_unrelated_parse_error_only() -> None:
    html = _document(
        "<div><span>broken outside</div>"
        '<pre><code class="language-python">def safe():\n  return 1</code></pre>'
    )
    document = extract_document_ir_v2(html)
    pre_id = _tag_id(document, "pre")

    assert document.parse_error_count > 0
    assert document.source_mapping_complete
    with pytest.raises(ValueError, match="HTML parse errors"):
        create_selection_certificate_v0(document, [pre_id])

    certificate = create_local_atomic_selection_certificate_v0(document, [pre_id])
    replay = verify_and_replay_local_atomic_selection_certificate_v0(
        document,
        certificate,
    )

    assert replay.receipt.verified
    assert replay.markdown == "```python\ndef safe():\n  return 1\n```"
    with pytest.raises(ValueError, match="HTML parse errors"):
        verify_and_replay_selection_certificate_v0(document, certificate)


def test_local_atomic_certificate_allows_only_standard_implicit_tbody() -> None:
    document = extract_document_ir_v2(
        _document(
            "<table>\n<tr><th>A</th><th>B</th></tr>\n"
            "<!-- inter-row -->\n<tr><td>1</td><td>2</td></tr>\n</table>"
        )
    )
    implicit = [element for element in document.elements if element.implicit]

    assert [(element.tag, element.parent_id) for element in implicit] == [
        ("tbody", _tag_id(document, "table")),
    ]
    certificate = create_local_atomic_selection_certificate_v0(
        document,
        [_tag_id(document, "table")],
    )
    replay = verify_and_replay_local_atomic_selection_certificate_v0(
        document,
        certificate,
    )

    assert replay.receipt.verified
    assert "A" in replay.markdown
    assert "1" in replay.markdown


@pytest.mark.parametrize(
    "body",
    [
        "<pre><code>broken inside</pre>",
        "<table><td>A</td><td>B</td></table>",
        "<table><tr><th>A</th><th>B</th></tr>noise"
        "<tr><td>1</td><td>2</td></tr></table>",
    ],
)
def test_local_atomic_certificate_rejects_repair_inside_selection(body: str) -> None:
    document = extract_document_ir_v2(_document(body))
    selected_tag = "pre" if body.startswith("<pre") else "table"

    with pytest.raises(ValueError, match="reliable|repair|tokenizer|source"):
        create_local_atomic_selection_certificate_v0(
            document,
            [_tag_id(document, selected_tag)],
        )

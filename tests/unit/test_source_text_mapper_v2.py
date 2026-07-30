from __future__ import annotations

import hashlib

import pytest
from clusy_native import (
    ORDERED_SOURCE_TEXT_MAP_V2_SCHEMA,
    ORDERED_SOURCE_TEXT_SPAN_V2_SCHEMA,
    DocumentIRV2Limits,
    OrderedSourceTextMapV2Limits,
    extract_document_ir_v2,
    map_ordered_source_text_v2,
    reconstruct_document_ir_v2,
)


def _raw_bytes(source: str, start: int, end: int) -> bytes:
    return source.encode("utf-8")[start:end]


def test_mapper_repairs_entity_and_repeated_sibling_mapping_failures() -> None:
    html = "<main><p>same &amp; 中文<em>x</em>same &amp; 中文</p></main>"
    document = extract_document_ir_v2(html)
    repeated = [run for run in document.text_runs if run.text == "same & 中文"]

    assert len(repeated) == 2
    assert all(not run.source_span_reliable for run in repeated)

    mapped = map_ordered_source_text_v2(document)

    assert mapped.accepted
    assert mapped.reason == "accepted"
    assert mapped.schema_version == ORDERED_SOURCE_TEXT_MAP_V2_SCHEMA
    assert mapped.mapped_text_run_count == document.text_run_count == 3
    repeated_spans = [span for span in mapped.spans if span.decoded_text == "same & 中文"]
    assert len(repeated_spans) == 2
    assert all(span.schema_version == ORDERED_SOURCE_TEXT_SPAN_V2_SCHEMA for span in mapped.spans)
    assert [span.raw_fragment for span in repeated_spans] == [
        "same &amp; 中文",
        "same &amp; 中文",
    ]
    assert repeated_spans[0].raw_source_end < repeated_spans[1].raw_source_start
    assert all(span.transform_kind == "html_character_reference" for span in repeated_spans)


def test_mapper_certifies_named_numeric_multibyte_and_raw_byte_offsets() -> None:
    html = "<main><p>前缀 &copy; &#169; &#x1F642; &amp; café</p></main>"
    document = extract_document_ir_v2(html)

    mapped = map_ordered_source_text_v2(document)

    assert mapped.accepted
    assert mapped.source_digest == hashlib.sha256(html.encode()).hexdigest()
    assert mapped.character_reference_span_count == 1
    [span] = mapped.spans
    assert span.decoded_text == "前缀 © © 🙂 & café"
    assert span.raw_fragment == "前缀 &copy; &#169; &#x1F642; &amp; café"
    raw = _raw_bytes(document.source, span.raw_source_start, span.raw_source_end)
    assert raw.decode() == span.raw_fragment
    assert span.raw_source_bytes == len(raw)
    assert span.raw_fragment_sha256 == hashlib.sha256(raw).hexdigest()
    assert span.decoded_text_sha256 == hashlib.sha256(span.decoded_text.encode()).hexdigest()
    assert len(span.certificate_sha256) == 64
    assert span.decode_verified
    assert not span.digest_is_authentication
    assert not mapped.digest_is_authentication


@pytest.mark.parametrize(
    ("raw", "decoded"),
    [
        ("&copy", "©"),
        ("&#0;", "\ufffd"),
        ("&#x110000;", "\ufffd"),
        ("&bogus;", "&bogus;"),
        ("&notit;", "¬it;"),
    ],
)
def test_mapper_certifies_adversarial_character_reference_recovery(
    raw: str,
    decoded: str,
) -> None:
    html = f"<main><p>{raw}</p></main>"

    mapped = map_ordered_source_text_v2(extract_document_ir_v2(html))

    assert mapped.accepted
    [span] = mapped.spans
    assert span.raw_fragment == raw
    assert span.decoded_text == decoded
    assert span.decode_verified
    assert span.tokenizer_error_count >= 1
    assert _raw_bytes(html, span.raw_source_start, span.raw_source_end).decode() == raw


def test_mapper_certifies_crlf_and_cr_newline_normalization() -> None:
    html = "<main><p>A\r\nB\rC</p></main>"

    mapped = map_ordered_source_text_v2(extract_document_ir_v2(html))

    assert mapped.accepted
    [span] = mapped.spans
    assert span.raw_fragment == "A\r\nB\rC"
    assert span.decoded_text == "A\nB\nC"
    assert span.transform_kind == "newline_normalization"
    assert span.transformed


def test_mapper_is_deterministic_and_does_not_mutate_ir_or_serialization() -> None:
    html = "<main><p>A &amp; B</p><p>A &amp; B</p></main>"
    document = extract_document_ir_v2(html)
    before_runs = [
        (run.id, run.text, run.source_start, run.source_end, run.source_span_reliable)
        for run in document.text_runs
    ]
    before_output = reconstruct_document_ir_v2(document).markdown

    first = map_ordered_source_text_v2(document)
    second = map_ordered_source_text_v2(document)
    rebuilt = map_ordered_source_text_v2(extract_document_ir_v2(html))

    assert first.accepted and second.accepted and rebuilt.accepted
    assert first.map_digest == second.map_digest == rebuilt.map_digest
    assert [span.certificate_sha256 for span in first.spans] == [
        span.certificate_sha256 for span in rebuilt.spans
    ]
    assert before_runs == [
        (run.id, run.text, run.source_start, run.source_end, run.source_span_reliable)
        for run in document.text_runs
    ]
    assert reconstruct_document_ir_v2(document).markdown == before_output


def test_mapper_handles_comments_and_skips_hidden_script_and_style_text() -> None:
    html = (
        "<main><p>A<!-- comment -->B</p>"
        "<script>window.hidden = '&amp;';</script>"
        "<style>.x::after { content: '&amp;' }</style>"
        "<p>shown</p></main>"
    )
    document = extract_document_ir_v2(html)

    mapped = map_ordered_source_text_v2(document)

    assert mapped.accepted
    assert [span.decoded_text for span in mapped.spans] == ["A", "B", "shown"]
    assert mapped.skipped_source_text_token_count == 2
    assert all("<script" not in span.raw_fragment for span in mapped.spans)
    assert all("<style" not in span.raw_fragment for span in mapped.spans)


def test_mapper_records_ignorable_document_edge_whitespace_omissions() -> None:
    html = "<!doctype html><html><body>\n<p>shown</p>\n</body></html>\n"
    document = extract_document_ir_v2(html)

    mapped = map_ordered_source_text_v2(document)

    assert mapped.accepted
    assert [span.decoded_text for span in mapped.spans if span.decoded_text.strip()] == ["shown"]
    assert mapped.mapped_text_run_count + mapped.skipped_dom_text_run_count == len(
        document.text_runs
    )
    assert mapped.skipped_dom_text_run_count == 1
    assert mapped.skipped_source_text_token_count >= 1


def test_mapper_accepts_exact_text_through_standard_implied_tbody_structure() -> None:
    html = "<table><tr><td>inside</td></tr></table>"
    document = extract_document_ir_v2(html)

    mapped = map_ordered_source_text_v2(document)

    assert any(element.tag == "tbody" and element.implicit for element in document.elements)
    assert mapped.accepted
    assert mapped.reason == "accepted"
    assert [span.decoded_text for span in mapped.spans] == ["inside"]


@pytest.mark.parametrize(
    "html",
    [
        "<main><p>one<div>two</div></p></main>",
        "<table>fostered<tr><td>inside</td></tr></table>",
        "<html><body><table>fostered<tr><td>inside</td></tr></table></body></html>",
        "<ul><li>one<li>two</ul>",
        "<main><b><i>crossed</b></i></main>",
    ],
)
def test_mapper_fails_closed_for_repairs_foster_parenting_and_reordering(html: str) -> None:
    mapped = map_ordered_source_text_v2(extract_document_ir_v2(html))

    assert not mapped.accepted
    assert mapped.reason in {
        "incomplete_element_mapping",
        "source_dom_mismatch",
        "tokenization_failure",
    }
    assert mapped.spans == []
    assert mapped.mapped_text_run_count == 0
    assert mapped.map_digest == ""


@pytest.mark.parametrize(
    ("limits", "reason"),
    [
        (OrderedSourceTextMapV2Limits(max_source_bytes=16), "source_byte_budget"),
        (OrderedSourceTextMapV2Limits(max_source_events=2), "source_event_budget"),
        (OrderedSourceTextMapV2Limits(max_text_runs=1), "text_run_budget"),
        (OrderedSourceTextMapV2Limits(max_raw_fragment_bytes=2), "raw_fragment_budget"),
        (OrderedSourceTextMapV2Limits(max_total_raw_bytes=5), "total_raw_byte_budget"),
        (OrderedSourceTextMapV2Limits(max_stack_depth=1), "stack_depth_budget"),
    ],
)
def test_mapper_budgets_reject_the_whole_map(
    limits: OrderedSourceTextMapV2Limits,
    reason: str,
) -> None:
    document = extract_document_ir_v2("<main><p>one</p><p>two</p></main>")

    mapped = map_ordered_source_text_v2(document, limits=limits)

    assert not mapped.accepted
    assert mapped.reason == reason
    assert mapped.spans == []
    assert mapped.map_digest == ""


def test_mapper_rejects_truncated_document_before_mapping() -> None:
    document = extract_document_ir_v2(
        "<main><p>" + ("é" * 128) + "</p></main>",
        limits=DocumentIRV2Limits(max_input_bytes=64),
    )

    mapped = map_ordered_source_text_v2(document)

    assert document.truncated
    assert not mapped.accepted
    assert mapped.reason in {"incomplete_source", "truncated_document"}
    assert mapped.document_truncated
    assert mapped.spans == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_source_bytes", 0),
        ("max_source_bytes", True),
        ("max_source_events", 1_000_001),
        ("max_text_runs", 500_001),
        ("max_raw_fragment_bytes", 16 * 1024 * 1024 + 1),
        ("max_total_raw_bytes", 16 * 1024 * 1024 + 1),
        ("max_stack_depth", 513),
    ],
)
def test_mapper_rejects_invalid_limits(field: str, value: int) -> None:
    values = {
        "max_source_bytes": 1_024,
        "max_source_events": 1_024,
        "max_text_runs": 1_024,
        "max_raw_fragment_bytes": 1_024,
        "max_total_raw_bytes": 2_048,
        "max_stack_depth": 32,
    }
    values[field] = value

    with pytest.raises(ValueError):
        OrderedSourceTextMapV2Limits(**values)


def test_generated_entity_multibyte_and_duplicate_runs_have_exact_certificates() -> None:
    atoms = [
        ("plain", "plain"),
        ("&amp;", "&"),
        ("&copy;", "©"),
        ("&#38;", "&"),
        ("&#x1F642;", "🙂"),
        ("中文", "中文"),
        ("é", "é"),
    ]
    for case in range(36):
        selected = [atoms[(case * 5 + offset * 3) % len(atoms)] for offset in range(9)]
        raw = " ".join(atom[0] for atom in selected)
        decoded = " ".join(atom[1] for atom in selected)
        html = f"<main><p>{raw}</p><em>separator</em><p>{raw}</p></main>"

        mapped = map_ordered_source_text_v2(extract_document_ir_v2(html))

        assert mapped.accepted
        duplicate_spans = [span for span in mapped.spans if span.decoded_text == decoded]
        assert len(duplicate_spans) == 2
        for span in duplicate_spans:
            raw_bytes = _raw_bytes(html, span.raw_source_start, span.raw_source_end)
            assert raw_bytes.decode() == raw
            assert span.raw_fragment_sha256 == hashlib.sha256(raw_bytes).hexdigest()
            assert span.decoded_text_sha256 == hashlib.sha256(decoded.encode()).hexdigest()

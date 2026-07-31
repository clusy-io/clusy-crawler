from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace

import pytest
from lxml import html as lxml_html

from app.services.source_selection_receipt_v0 import (
    SOURCE_SELECTION_RECEIPT_V0_SCHEMA,
    QualitySourceSelectionReceiptV0,
    SourceSelectionReceiptError,
    build_quality_source_selection_receipt_v0,
    verify_quality_source_selection_receipt_v0,
)


def _fixture() -> dict[str, object]:
    mapped = (
        "<html><body>"
        '<nav _item_id="1">Navigation</nav>'
        "<main>"
        '<h1 _item_id="2">Receipt title</h1>'
        "<br>"
        '<p _item_id="3">Selected source body.</p>'
        "</main>"
        '<footer _item_id="4">Footer</footer>'
        "</body></html>"
    )
    return {
        "raw_html": "<html><body>original source</body></html>",
        "raw_model_response": (
            '{"1":"other","2":"main","3":"main","4":"other"}'
        ),
        "response_format": "json",
        "simplified_html": mapped,
        "mapped_html": mapped,
        "item_labels": {
            "1": "other",
            "2": "main",
            "3": "main",
            "4": "other",
        },
        "selected_html": (
            "<html><body><main>"
            '<h1 _item_id="2">Receipt title</h1>'
            "<br>"
            '<p _item_id="3">Selected source body.</p>'
            "</main></body></html>"
        ),
        "upstream_revision": "73cf266690befd209cae7e6fdff9716d5b31a976",
        "prompt_profile": "openai_json",
    }


def test_builds_source_bound_independently_replayed_receipt() -> None:
    values = _fixture()

    receipt = build_quality_source_selection_receipt_v0(**values)

    assert receipt.schema_version == SOURCE_SELECTION_RECEIPT_V0_SCHEMA
    assert receipt.item_count == 4
    assert receipt.selected_count == 2
    assert receipt.selected_item_ids == ("2", "3")
    assert receipt.response_format == "json"
    assert receipt.model_response_sha256 == hashlib.sha256(
        str(values["raw_model_response"]).encode()
    ).hexdigest()
    assert receipt.replay_verified is True
    assert receipt.digest_is_authentication is False
    assert len(receipt.receipt_sha256) == 64
    mapped_root = lxml_html.fromstring(str(values["mapped_html"]).encode())
    mapped_canonical = lxml_html.tostring(
        mapped_root,
        encoding="utf-8",
        method="html",
    )
    assert receipt.mapped_html_sha256 == hashlib.sha256(mapped_canonical).hexdigest()
    assert receipt.mapped_html_sha256 != receipt.selected_html_sha256
    assert verify_quality_source_selection_receipt_v0(
        receipt,
        raw_html=values["raw_html"],
    )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        (
            "item_labels",
            {"1": "other", "2": "main", "3": "main"},
            "cover every",
        ),
        (
            "item_labels",
            {"1": "other", "2": "main", "3": "keep", "4": "other"},
            "canonical",
        ),
        (
            "selected_html",
            '<html><body><main><h1 _item_id="2">tampered</h1></main></body></html>',
            "replay",
        ),
        (
            "simplified_html",
            '<html><body><p _item_id="1">one</p><p _item_id="3">three</p></body></html>',
            "contiguous",
        ),
        (
            "mapped_html",
            '<html><body><p _item_id="1">one</p><p _item_id="1">again</p></body></html>',
            "duplicate",
        ),
    ],
)
def test_rejects_incomplete_ambiguous_or_tampered_selection(
    field: str,
    value: object,
    match: str,
) -> None:
    values = _fixture()
    values[field] = value

    with pytest.raises(SourceSelectionReceiptError, match=match):
        build_quality_source_selection_receipt_v0(**values)


def test_rejects_selection_without_main_items() -> None:
    values = _fixture()
    values["item_labels"] = {
        "1": "other",
        "2": "other",
        "3": "other",
        "4": "other",
    }
    values["raw_model_response"] = (
        '{"1":"other","2":"other","3":"other","4":"other"}'
    )

    with pytest.raises(SourceSelectionReceiptError, match="no main"):
        build_quality_source_selection_receipt_v0(**values)


def test_tail_wrapper_replay_preserves_text_and_drops_internal_tag() -> None:
    mapped = (
        "<html><body><section>"
        '<cc-alg-uc-text _item_id="1">direct source text</cc-alg-uc-text>'
        '<p _item_id="2">noise</p>'
        "</section></body></html>"
    )

    receipt = build_quality_source_selection_receipt_v0(
        raw_html="<section>direct source text<p>noise</p></section>",
        simplified_html=mapped,
        mapped_html=mapped,
        item_labels={"1": "main", "2": "other"},
        raw_model_response='{"1":"main","2":"other"}',
        response_format="json",
        selected_html="<html><body><section>direct source text</section></body></html>",
        upstream_revision="revision",
        prompt_profile="profile",
    )

    assert receipt.selected_item_ids == ("1",)
    assert receipt.replay_verified is True


def test_receipt_verifier_rejects_wrong_source_and_identity_tampering() -> None:
    values = _fixture()
    receipt = build_quality_source_selection_receipt_v0(**values)

    assert not verify_quality_source_selection_receipt_v0(
        receipt,
        raw_html="<html>different source</html>",
    )
    assert not verify_quality_source_selection_receipt_v0(
        replace(receipt, selected_count=1),
        raw_html=values["raw_html"],
    )
    assert not verify_quality_source_selection_receipt_v0(
        replace(receipt, receipt_sha256="0" * 64),
        raw_html=values["raw_html"],
    )
    assert not verify_quality_source_selection_receipt_v0(
        replace(receipt, response_format="yaml"),
        raw_html=values["raw_html"],
    )
    assert not verify_quality_source_selection_receipt_v0(
        object(),
        raw_html=values["raw_html"],
    )


def test_receipt_dataclass_cannot_claim_authentication() -> None:
    values = _fixture()
    receipt = build_quality_source_selection_receipt_v0(**values)
    forged = QualitySourceSelectionReceiptV0(
        **{
            field: getattr(receipt, field)
            for field in receipt.__dataclass_fields__
            if field != "digest_is_authentication"
        },
        digest_is_authentication=True,
    )

    assert not verify_quality_source_selection_receipt_v0(
        forged,
        raw_html=values["raw_html"],
    )


@pytest.mark.parametrize(
    ("raw_model_response", "response_format", "match"),
    [
        (
            '{"1":"other","2":"main","2":"other","3":"main","4":"other"}',
            "json",
            "duplicate",
        ),
        (
            '{"1":"other","2":"main","3":"main","4":"other"} trailing',
            "json",
            "malformed",
        ),
        (
            '{"1":"other","2":"main","3":"main"',
            "json",
            "malformed",
        ),
        (
            "1other3main2main4other",
            "compact",
            "prompt order",
        ),
        (
            "1other2main2main3main4other",
            "compact",
            "prompt order",
        ),
        (
            "1other2main3main4other trailing",
            "compact",
            "malformed",
        ),
    ],
)
def test_rejects_ambiguous_or_repaired_raw_model_response(
    raw_model_response: str,
    response_format: str,
    match: str,
) -> None:
    values = _fixture()
    values["raw_model_response"] = raw_model_response
    values["response_format"] = response_format

    with pytest.raises(SourceSelectionReceiptError, match=match):
        build_quality_source_selection_receipt_v0(**values)


def test_rejects_raw_and_parsed_label_mismatch() -> None:
    values = _fixture()
    values["raw_model_response"] = (
        '{"1":"other","2":"main","3":"other","4":"other"}'
    )

    with pytest.raises(SourceSelectionReceiptError, match="differ from parsed"):
        build_quality_source_selection_receipt_v0(**values)


def test_accepts_reordered_json_and_canonical_compact_response() -> None:
    values = _fixture()
    values["raw_model_response"] = (
        '{"4":"other","3":"main","2":"main","1":"other"}'
    )
    reordered = build_quality_source_selection_receipt_v0(**values)
    assert reordered.response_format == "json"

    values["raw_model_response"] = "1other2main3main4other"
    values["response_format"] = "compact"
    compact = build_quality_source_selection_receipt_v0(**values)
    assert compact.response_format == "compact"
    assert compact.selected_item_ids == ("2", "3")


@pytest.mark.parametrize(
    "raw_html",
    [
        (
            "<html><body><nav>Home</nav><article><h1>Title</h1>"
            "<p>Primary paragraph with <a href='https://example.test/?a=1&amp;b=2'>"
            "a link</a>.</p><p>Second primary paragraph.</p></article>"
            "<footer>Footer</footer></body></html>"
        ),
        (
            "<html><body><main><h2>Structured</h2>"
            "<pre><code>if ready:\\n    run()</code></pre>"
            "<table><tr><th>Name</th><th>Value</th></tr>"
            "<tr><td>alpha</td><td>1</td></tr></table>"
            "<ul><li>one</li><li>two</li><li>three</li></ul>"
            "</main></body></html>"
        ),
        (
            "<html><body><section>Leading direct text <strong>inline</strong>"
            " trailing direct text.<div><p>Nested block content.</p></div>"
            "Final tail text.</section></body></html>"
        ),
    ],
)
def test_independent_replay_matches_pinned_mineru_mapping(
    raw_html: str,
) -> None:
    simplify_module = pytest.importorskip("mineru_html.process.simplify_html")
    mapping_module = pytest.importorskip("mineru_html.process.map_to_main")

    simplified_html, mapped_html = simplify_module.simplify_html(raw_html)
    item_ids = tuple(
        re.findall(r'\s_item_id="([1-9][0-9]*)"', simplified_html)
    )
    assert item_ids
    # Exhaust every selection over the first eight blocks. This covers isolated
    # nodes, disjoint intervals, adjacent blocks, and the all-selected case
    # while keeping the optional upstream cross-check bounded.
    selectable = min(len(item_ids), 8)
    for mask in range(1, 1 << selectable):
        labels = {
            item_id: (
                "main"
                if index < selectable and mask & (1 << index)
                else "other"
            )
            for index, item_id in enumerate(item_ids)
        }
        selected_html = mapping_module.extract_main_html(mapped_html, labels)

        receipt = build_quality_source_selection_receipt_v0(
            raw_html=raw_html,
            raw_model_response=json.dumps(labels, separators=(",", ":")),
            response_format="json",
            simplified_html=simplified_html,
            mapped_html=mapped_html,
            item_labels=labels,
            selected_html=selected_html,
            upstream_revision="73cf266690befd209cae7e6fdff9716d5b31a976",
            prompt_profile="openai_json",
        )

        expected = tuple(
            item_id
            for index, item_id in enumerate(item_ids)
            if index < selectable and mask & (1 << index)
        )
        assert receipt.selected_item_ids == expected
        assert receipt.replay_verified is True

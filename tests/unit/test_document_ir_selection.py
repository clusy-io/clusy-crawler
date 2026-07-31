from __future__ import annotations

import json
from dataclasses import dataclass, replace

import pytest
from clusy_native import DocumentIRLimits, extract_document_ir

from app.services.document_ir_selection import (
    SELECTION_SCHEMA_VERSION,
    BlockSelection,
    ClassifierBudgetError,
    ClassifierInputLimits,
    InvalidBlockSelection,
    InvalidDocumentIR,
    ReconstructionLimits,
    SelectionLimits,
    build_classifier_input,
    parse_block_selection,
    prepare_document_ir,
    reconstruct_block_selection,
)

_COMPOUND_HTML = """
<main>
  <h1>Title</h1>
  <p>Before</p>
  <table class="layout">
    <tr>
      <td><p>noise</p><p>target</p></td>
      <td>other</td>
    </tr>
    <tr><td>wanted row</td><td>tail</td></tr>
  </table>
  <p>After</p>
  <ul class="items"><li>one</li><li><p>two nested</p></li></ul>
  <pre class="src"><code>x = 1
  y = 2</code></pre>
  <figure class="hero"><img src="x" alt="photo"><figcaption>caption</figcaption></figure>
</main>
"""


def _selection_response(
    source_digest: str,
    selected: list[object],
    **extra: object,
) -> str:
    value: dict[str, object] = {
        "schema_version": SELECTION_SCHEMA_VERSION,
        "source_digest": source_digest,
        "selected": selected,
    }
    value.update(extra)
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def test_classifier_input_is_stable_bounded_and_exposes_only_selectable_units() -> None:
    document = extract_document_ir(_COMPOUND_HTML)

    first = build_classifier_input(document)
    second = build_classifier_input(document)

    assert first == second
    assert first.payload_digest == second.payload_digest
    assert first.chars == len(first.payload)
    assert first.tokens == len(first.payload.encode())
    assert first.chars <= first.limits.max_chars
    assert first.tokens <= first.limits.max_tokens
    assert first.included_block_ids == tuple(
        block.id for block in document.blocks if block.selectable
    )
    assert first.serialization_container_ids == tuple(
        block.id for block in document.blocks if not block.selectable
    )
    assert first.coarse_selectable_container_ids == tuple(
        block.id for block in document.blocks if block.selectable and not block.atomic
    )

    lines = first.payload.splitlines()
    assert len(lines) == first.included_block_count + 1
    records = [json.loads(line) for line in lines[1:]]
    assert [record["i"] for record in records] == list(first.included_block_ids)
    target = next(record for record in records if record["i"] == "block-000008")
    assert target["q"] == "main/table/tbody/tr/td"
    assert target["x"] == "target"
    assert all(record["s"] is True for record in records)


def test_prepared_document_context_is_reusable_across_all_stages() -> None:
    document = extract_document_ir(_COMPOUND_HTML)
    prepared = prepare_document_ir(document)

    assert prepare_document_ir(prepared) is prepared
    classifier_input = build_classifier_input(prepared)
    response = _selection_response(classifier_input.source_digest, ["block-000008"])
    selection = parse_block_selection(
        response,
        classifier_input=classifier_input,
        document=prepared,
    )
    reconstructed = reconstruct_block_selection(prepared, selection)

    assert prepared.source_digest == classifier_input.source_digest
    assert prepared.source_block_count == document.block_count
    assert prepared.source_selectable_block_count == sum(
        block.selectable for block in document.blocks
    )
    assert "target" in reconstructed.html


def test_classifier_input_applies_block_text_char_and_token_budgets() -> None:
    document = extract_document_ir(_COMPOUND_HTML)
    full = build_classifier_input(document)
    header_chars = len(full.payload.splitlines()[0])
    limits = ClassifierInputLimits(
        max_chars=header_chars + 450,
        max_tokens=header_chars + 450,
        max_blocks=2,
        max_text_chars_per_block=3,
    )

    bounded = build_classifier_input(document, limits=limits)

    assert bounded.chars <= limits.max_chars
    assert bounded.tokens <= limits.max_tokens
    assert bounded.included_block_count <= limits.max_blocks
    assert bounded.omitted_block_count > 0
    assert bounded.truncated
    assert "max_blocks" in bounded.truncation_reasons or "max_chars" in bounded.truncation_reasons
    assert bounded.text_truncated_block_ids


def test_classifier_input_supports_an_exact_endpoint_token_counter() -> None:
    document = extract_document_ir(_COMPOUND_HTML)
    full = build_classifier_input(
        document,
        token_counter=len,
        token_accounting="unicode-codepoints",
    )
    limits = ClassifierInputLimits(
        max_chars=full.chars,
        max_tokens=len(full.payload.splitlines()[0]) + 300,
        max_blocks=512,
        max_text_chars_per_block=1_024,
    )

    bounded = build_classifier_input(
        document,
        limits=limits,
        token_counter=len,
        token_accounting="unicode-codepoints",
    )

    assert bounded.tokens == len(bounded.payload)
    assert bounded.tokens <= limits.max_tokens
    assert bounded.token_accounting == "unicode-codepoints"
    assert bounded.omitted_block_count > 0
    assert "max_tokens" in bounded.truncation_reasons


def test_classifier_header_and_invalid_token_counter_fail_closed() -> None:
    document = extract_document_ir(_COMPOUND_HTML)

    with pytest.raises(ClassifierBudgetError):
        build_classifier_input(
            document,
            limits=ClassifierInputLimits(
                max_chars=1,
                max_tokens=1,
                max_blocks=1,
                max_text_chars_per_block=1,
            ),
        )
    with pytest.raises(ValueError, match="token counter"):
        build_classifier_input(document, token_counter=lambda _: -1)


def test_valid_selection_reconstructs_partial_table_with_minimal_skeleton() -> None:
    document = extract_document_ir(_COMPOUND_HTML)
    classifier_input = build_classifier_input(document)
    response = _selection_response(
        classifier_input.source_digest,
        ["block-000008", "block-000011"],
    )

    selection = parse_block_selection(
        response,
        classifier_input=classifier_input,
        document=document,
    )
    reconstructed = reconstruct_block_selection(document, selection)

    assert selection.selected_ids == ("block-000008", "block-000011")
    assert reconstructed.emitted_ids == selection.selected_ids
    assert reconstructed.selected_complete
    assert reconstructed.complete
    assert reconstructed.html.startswith("<main>")
    assert '<table class="layout">' in reconstructed.html
    assert reconstructed.html.index("target") < reconstructed.html.index("wanted row")
    assert reconstructed.html.count("<tr>") == 2
    assert reconstructed.html.count("<td>") == 2
    assert "noise" not in reconstructed.html
    assert "other" not in reconstructed.html
    assert "tail" not in reconstructed.html
    assert "Before" not in reconstructed.html
    assert reconstructed.html.endswith("</main>")
    assert "block-000003" in reconstructed.wrapper_ids
    assert "block-000004" in reconstructed.wrapper_ids


def test_contiguous_range_expands_over_exposed_dom_order() -> None:
    document = extract_document_ir(_COMPOUND_HTML)
    classifier_input = build_classifier_input(document)
    response = _selection_response(
        classifier_input.source_digest,
        ["block-000008..block-000011"],
    )

    selection = parse_block_selection(
        response,
        classifier_input=classifier_input,
        document=document,
    )

    assert selection.selected_ids == (
        "block-000008",
        "block-000009",
        "block-000011",
    )
    assert selection.range_count == 1


def test_mixed_layout_cell_exposes_link_leaf_without_global_anchor_explosion() -> None:
    document = extract_document_ir(
        '<table><tr><td>noise <a href="/target">target</a> trailing noise</td></tr></table>'
    )
    classifier_input = build_classifier_input(document)
    selectable = [block for block in document.blocks if block.selectable]

    assert [(block.tag, block.text) for block in selectable] == [("a", "target")]
    assert not classifier_input.coarse_selectable_container_ids
    response = _selection_response(classifier_input.source_digest, [selectable[0].id])
    selection = parse_block_selection(
        response,
        classifier_input=classifier_input,
        document=document,
    )
    reconstructed = reconstruct_block_selection(document, selection)

    assert "<table>" in reconstructed.html
    assert "<tr>" in reconstructed.html
    assert "<td>" in reconstructed.html
    assert '<a href="/target">target</a>' in reconstructed.html
    assert "noise" not in reconstructed.html
    assert reconstructed.html.endswith("</table>")


def test_bare_inline_cell_reports_coarse_selection_ceiling() -> None:
    document = extract_document_ir(
        "<table><tr><td>noise target trailing noise</td></tr></table>"
    )
    classifier_input = build_classifier_input(document)
    selectable = [block for block in document.blocks if block.selectable]

    assert len(selectable) == 1
    assert selectable[0].tag == "td"
    assert not selectable[0].atomic
    assert classifier_input.coarse_selectable_container_ids == (selectable[0].id,)


@pytest.mark.parametrize(
    "selected",
    [
        ["not-a-block"],
        ["block-999999"],
        ["block-000003"],
        ["block-000011", "block-000008"],
        ["block-000008", "block-000008"],
        ["block-000008..block-000011", "block-000009"],
        ["block-000011..block-000008"],
        [123],
    ],
)
def test_invalid_ids_order_duplicates_and_ranges_fail_closed(selected: list[object]) -> None:
    document = extract_document_ir(_COMPOUND_HTML)
    classifier_input = build_classifier_input(document)
    response = _selection_response(classifier_input.source_digest, selected)

    with pytest.raises(InvalidBlockSelection):
        parse_block_selection(
            response,
            classifier_input=classifier_input,
            document=document,
        )


def test_strict_json_contract_and_response_limit_fail_closed() -> None:
    document = extract_document_ir(_COMPOUND_HTML)
    classifier_input = build_classifier_input(document)
    digest = classifier_input.source_digest
    invalid_responses = [
        "not json",
        "[]",
        _selection_response(digest, [], extra=True),
        json.dumps({"schema_version": SELECTION_SCHEMA_VERSION, "selected": []}),
        _selection_response("0" * 64, []),
        json.dumps(
            {
                "schema_version": SELECTION_SCHEMA_VERSION,
                "source_digest": digest,
                "selected": "block-000001",
            }
        ),
        (
            '{"schema_version":"ordered-dom-ir.selection.v1",'
            f'"source_digest":"{digest}","source_digest":"{digest}","selected":[]}}'
        ),
        (
            '{"schema_version":"ordered-dom-ir.selection.v1",'
            f'"source_digest":"{digest}","selected":[NaN]}}'
        ),
    ]

    for response in invalid_responses:
        with pytest.raises(InvalidBlockSelection):
            parse_block_selection(
                response,
                classifier_input=classifier_input,
                document=document,
            )

    oversized = " " * 1_001
    with pytest.raises(InvalidBlockSelection, match="max_response_chars"):
        parse_block_selection(
            oversized,
            classifier_input=classifier_input,
            document=document,
            limits=SelectionLimits(
                max_response_chars=1_000,
                max_items=10,
                max_selected_blocks=10,
            ),
        )


def test_selection_expansion_limit_and_unexposed_budget_ids_fail_closed() -> None:
    document = extract_document_ir(_COMPOUND_HTML)
    classifier_input = build_classifier_input(document)
    response = _selection_response(
        classifier_input.source_digest,
        ["block-000001", "block-000002"],
    )

    with pytest.raises(InvalidBlockSelection, match="too many"):
        parse_block_selection(
            response,
            classifier_input=classifier_input,
            document=document,
            limits=SelectionLimits(
                max_response_chars=10_000,
                max_items=10,
                max_selected_blocks=1,
            ),
        )

    header_chars = len(classifier_input.payload.splitlines()[0])
    tiny = build_classifier_input(
        document,
        limits=ClassifierInputLimits(
            max_chars=header_chars + 250,
            max_tokens=header_chars + 250,
            max_blocks=1,
            max_text_chars_per_block=10,
        ),
    )
    omitted_id = tiny.omitted_selectable_block_ids[0]
    omitted_response = _selection_response(tiny.source_digest, [omitted_id])
    with pytest.raises(InvalidBlockSelection, match="unexposed"):
        parse_block_selection(
            omitted_response,
            classifier_input=tiny,
            document=document,
        )


def test_reconstruction_output_limit_emits_only_complete_balanced_prefix() -> None:
    document = extract_document_ir(_COMPOUND_HTML)
    classifier_input = build_classifier_input(document)
    first_response = _selection_response(classifier_input.source_digest, ["block-000008"])
    first_selection = parse_block_selection(
        first_response,
        classifier_input=classifier_input,
        document=document,
    )
    first = reconstruct_block_selection(document, first_selection)
    two_response = _selection_response(
        classifier_input.source_digest,
        ["block-000008", "block-000011"],
    )
    two_selection = parse_block_selection(
        two_response,
        classifier_input=classifier_input,
        document=document,
    )

    bounded = reconstruct_block_selection(
        document,
        two_selection,
        limits=ReconstructionLimits(max_chars=first.chars, max_blocks=10),
    )

    assert bounded.emitted_ids == ("block-000008",)
    assert bounded.omitted_ids == ("block-000011",)
    assert bounded.html == first.html
    assert bounded.html.endswith("</main>")
    assert "max_chars" in bounded.truncation_reasons
    assert not bounded.selected_complete
    assert not bounded.complete


def test_reconstruction_refuses_truncated_selected_outer_html() -> None:
    document = extract_document_ir(
        "<main><p>" + ("long text " * 30) + "</p></main>",
        limits=DocumentIRLimits(
            max_input_bytes=10_000,
            max_nodes=1_000,
            max_blocks=100,
            max_depth=30,
            max_block_text_bytes=1_000,
            max_block_html_bytes=20,
            max_total_text_bytes=10_000,
            max_total_html_bytes=10_000,
        ),
    )
    classifier_input = build_classifier_input(document)
    selected_id = classifier_input.included_block_ids[0]
    response = _selection_response(classifier_input.source_digest, [selected_id])
    selection = parse_block_selection(
        response,
        classifier_input=classifier_input,
        document=document,
    )

    reconstructed = reconstruct_block_selection(document, selection)

    assert not reconstructed.html
    assert reconstructed.omitted_ids == (selected_id,)
    assert reconstructed.source_html_truncated_ids == (selected_id,)
    assert any(
        "source_block_html_truncated" in reason
        for reason in reconstructed.truncation_reasons
    )
    assert not reconstructed.complete


@dataclass(frozen=True, slots=True)
class _FakeBlock:
    id: str
    order: int
    parent_id: str | None = None
    tag: str = "div"
    role: str = "generic"
    atomic: bool = True
    selectable: bool = True
    preserve_whitespace: bool = False
    text: str = "text"
    outer_html: str = "<div>text</div>"
    depth: int = 1
    word_count: int = 1
    text_bytes: int = 100
    html_bytes: int = 100
    link_count: int = 0
    link_text_bytes: int = 0
    descendant_element_count: int = 0
    text_density: float = 0.25
    link_density: float = 0.0
    text_truncated: bool = False
    html_truncated: bool = False
    features_truncated: bool = False


@dataclass(frozen=True, slots=True)
class _FakeDocument:
    blocks: tuple[_FakeBlock, ...]
    schema_version: str = "ordered-dom-ir.v1"
    truncated: bool = False
    truncation_reasons: tuple[str, ...] = ()

    @property
    def block_count(self) -> int:
        return len(self.blocks)


def test_ancestor_descendant_overlap_fails_closed() -> None:
    document = _FakeDocument(
        blocks=(
            _FakeBlock(
                id="block-000000",
                order=0,
                atomic=False,
                selectable=True,
                outer_html="<div><p>child</p></div>",
            ),
            _FakeBlock(
                id="block-000001",
                order=1,
                parent_id="block-000000",
                tag="p",
                role="paragraph",
                outer_html="<p>child</p>",
                depth=2,
            ),
        )
    )
    classifier_input = build_classifier_input(document)
    response = _selection_response(
        classifier_input.source_digest,
        ["block-000000", "block-000001"],
    )

    with pytest.raises(InvalidBlockSelection, match="overlaps ancestor"):
        parse_block_selection(
            response,
            classifier_input=classifier_input,
            document=document,
        )


def test_invalid_source_ir_and_forged_selection_fail_closed() -> None:
    invalid_document = _FakeDocument(
        blocks=(
            _FakeBlock(id="block-000000", order=0),
            _FakeBlock(id="block-000002", order=2),
        )
    )
    with pytest.raises(InvalidDocumentIR):
        build_classifier_input(invalid_document)

    document = extract_document_ir(_COMPOUND_HTML)
    classifier_input = build_classifier_input(document)
    response = _selection_response(classifier_input.source_digest, ["block-000008"])
    selection = parse_block_selection(
        response,
        classifier_input=classifier_input,
        document=document,
    )
    forged = replace(
        selection,
        selected_ids=("block-000011", "block-000008"),
    )
    with pytest.raises(InvalidBlockSelection, match="strict DOM order"):
        reconstruct_block_selection(document, forged)

    forged_classifier = replace(classifier_input, payload_digest="0" * 64)
    with pytest.raises(InvalidBlockSelection, match="payload digest"):
        parse_block_selection(
            response,
            classifier_input=forged_classifier,
            document=document,
        )


def test_source_ir_truncation_is_preserved_in_reconstruction_provenance() -> None:
    document = _FakeDocument(
        blocks=(_FakeBlock(id="block-000000", order=0),),
        truncated=True,
        truncation_reasons=("input_bytes",),
    )
    classifier_input = build_classifier_input(document)
    response = _selection_response(classifier_input.source_digest, ["block-000000"])
    selection = parse_block_selection(
        response,
        classifier_input=classifier_input,
        document=document,
    )
    reconstructed = reconstruct_block_selection(document, selection)

    assert reconstructed.selected_complete
    assert not reconstructed.complete
    assert reconstructed.truncated
    assert reconstructed.source_ir_truncated
    assert reconstructed.source_ir_truncation_reasons == ("input_bytes",)
    assert "source_ir" in reconstructed.truncation_reasons


def test_reconstruction_rejects_forged_structural_container_selection() -> None:
    document = extract_document_ir(_COMPOUND_HTML)
    classifier_input = build_classifier_input(document)
    valid_response = _selection_response(classifier_input.source_digest, ["block-000008"])
    selection = parse_block_selection(
        valid_response,
        classifier_input=classifier_input,
        document=document,
    )
    forged = BlockSelection(
        schema_version=selection.schema_version,
        source_digest=selection.source_digest,
        classifier_payload_digest=selection.classifier_payload_digest,
        response_digest=selection.response_digest,
        selected_ids=("block-000003",),
        raw_item_count=1,
        range_count=0,
        response_chars=selection.response_chars,
    )

    with pytest.raises(InvalidBlockSelection, match="serialization-only"):
        reconstruct_block_selection(document, forged)

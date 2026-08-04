"""No-network production-chain smoke for the pinned quality image."""

from __future__ import annotations

import json
from importlib import import_module
from typing import Protocol, cast

from lxml import html as lxml_html

from app.services.quality_extractor import quality_dependency_available
from app.services.source_selection_receipt_v0 import (
    replay_quality_source_selection_from_derived_v0,
)
from app.services.source_serialization_receipt_v1 import (
    MINERU_HTML_PREPROCESSOR_CUTOFF_LENGTH,
    MINERU_HTML_REVISION,
    SOURCE_SERIALIZATION_RECEIPT_V1_MAX_LATEX_GROUP_DEPTH,
    SOURCE_SERIALIZATION_RECEIPT_V1_MAX_MATH_DELIMITER_TOKENS,
    SOURCE_SERIALIZATION_RECEIPT_V1_SCHEMA,
    SourceSerializationReceiptError,
    _preflight_serializer_structure,
    load_pinned_mineru_webkit_serializer_v1,
    mint_quality_source_serialization_v1,
    preflight_quality_source_input_v1,
    verify_quality_source_serialization_receipt_v1,
)


class _Preprocessor(Protocol):
    def __call__(self, html_str: str, cutoff_length: int = 500) -> tuple[str, str]: ...


def main() -> int:
    if not quality_dependency_available():
        raise RuntimeError("complete pinned quality capability is unavailable")
    raw_html = (
        "<html><body><article>"
        "<h1>Clusy quality runtime</h1>"
        "<p>This source-backed paragraph validates preprocessing, complete "
        "pointer replay, local serialization, and receipt authentication.</p>"
        "</article></body></html>"
    )
    source_url = "https://example.invalid/quality-runtime-smoke"
    preflight_quality_source_input_v1(raw_html)
    simplify_html = cast(
        "_Preprocessor",
        import_module("mineru_html.process.simplify_html").simplify_html,
    )
    simplified_html, mapped_html = simplify_html(
        raw_html,
        cutoff_length=MINERU_HTML_PREPROCESSOR_CUTOFF_LENGTH,
    )
    root = lxml_html.fromstring(simplified_html.encode("utf-8"))
    item_ids = tuple(str(value) for value in root.xpath("//*[@_item_id]/@_item_id"))
    if not item_ids:
        raise RuntimeError("pinned preprocessor returned no source item IDs")
    labels = {item_id: "main" for item_id in item_ids}
    raw_response = json.dumps(labels, ensure_ascii=False, separators=(",", ":"))
    selected = replay_quality_source_selection_from_derived_v0(
        simplified_html=simplified_html,
        mapped_html=mapped_html,
        selected_item_ids=item_ids,
    )
    minted = mint_quality_source_serialization_v1(
        raw_html=raw_html,
        source_url=source_url,
        raw_model_response=raw_response,
        response_format="json",
        simplified_html=simplified_html,
        mapped_html=mapped_html,
        item_labels=labels,
        selected_html=selected.selected_html,
        upstream_revision=MINERU_HTML_REVISION,
        prompt_profile="openai_json",
        max_output_chars=10_000,
    )
    if minted.receipt.schema_version != SOURCE_SERIALIZATION_RECEIPT_V1_SCHEMA:
        raise RuntimeError("quality smoke minted the wrong receipt schema")
    if not verify_quality_source_serialization_receipt_v1(
        minted.receipt,
        raw_html=raw_html,
        source_url=source_url,
        output_text=minted.text,
    ):
        raise RuntimeError("quality smoke receipt did not verify")
    if verify_quality_source_serialization_receipt_v1(
        minted.receipt,
        raw_html=raw_html,
        source_url=source_url,
        output_text=minted.text + " tampered",
    ):
        raise RuntimeError("quality smoke accepted tampered output")

    # MinerU-HTML uses cc-alg-uc-text internally for mixed inline/block source,
    # then both its mapper and Clusy replay remove that wrapper before the
    # serializer boundary. Exercise the real path so the selected DOM remains
    # closed to every caller-supplied cc* tag without rejecting normal prose.
    mixed_raw_html = (
        "<html><body><main>Lead <span>inline</span> tail"
        "<p>Block paragraph</p>after block</main></body></html>"
    )
    preflight_quality_source_input_v1(mixed_raw_html)
    mixed_simplified, mixed_mapped = simplify_html(
        mixed_raw_html,
        cutoff_length=MINERU_HTML_PREPROCESSOR_CUTOFF_LENGTH,
    )
    if "<cc-alg-uc-text" not in mixed_mapped:
        raise RuntimeError("pinned preprocessor mixed-content contract changed")
    mixed_root = lxml_html.fromstring(mixed_simplified.encode("utf-8"))
    mixed_ids = tuple(str(value) for value in mixed_root.xpath("//*[@_item_id]/@_item_id"))
    mixed_labels = {item_id: "main" for item_id in mixed_ids}
    mixed_selected = replay_quality_source_selection_from_derived_v0(
        simplified_html=mixed_simplified,
        mapped_html=mixed_mapped,
        selected_item_ids=mixed_ids,
    )
    if "<cc" in mixed_selected.selected_html:
        raise RuntimeError("trusted replay retained an internal cc* source tag")
    mixed_mint = mint_quality_source_serialization_v1(
        raw_html=mixed_raw_html,
        source_url=source_url,
        raw_model_response=json.dumps(
            mixed_labels,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        response_format="json",
        simplified_html=mixed_simplified,
        mapped_html=mixed_mapped,
        item_labels=mixed_labels,
        selected_html=mixed_selected.selected_html,
        upstream_revision=MINERU_HTML_REVISION,
        prompt_profile="openai_json",
        max_output_chars=10_000,
    )
    if not all(
        text in mixed_mint.text for text in ("Lead", "inline", "Block paragraph", "after block")
    ):
        raise RuntimeError("mixed-content replay lost source text")

    serializer = load_pinned_mineru_webkit_serializer_v1()
    conformance_cases = (
        "<html><body><main><h1>Title</h1><p>Useful article body.</p></main></body></html>",
        "<html><body><ul><li>one</li><li>two<ul><li>nested</li></ul></li></ul></body></html>",
        "<html><body><table><tr><th>A</th><th>B</th></tr>"
        "<tr><td>1</td><td>2</td></tr></table></body></html>",
        '<html><body><img src="relative.png" alt="diagram"></body></html>',
        "<html><body><pre><code>def f():\n    return 1</code></pre></body></html>",
        "<html><body><p>" + (">" * 1000) + "</p></body></html>",
    )
    conformance_cap = 100_000
    for html_case in conformance_cases:
        projected_chars = _preflight_serializer_structure(
            html_case,
            source_url=source_url,
            max_output_chars=conformance_cap,
        )
        serialized = serializer(
            main_html=html_case,
            url=source_url,
            output_format="mm_md",
        )
        if type(serialized) is not str or not (
            len(serialized.strip()) <= projected_chars <= conformance_cap
        ):
            raise RuntimeError("serializer output exceeded its structural projection")

    prefix = "https://example.invalid/"
    long_source_url = prefix + ("u" * (4096 - len(prefix)))
    rejected_cases = (
        '<svg width="100000" height="100000"><path d="M0 0"/></svg>',
        "<table><tr>"
        + ("<td>x</td>" * 1000)
        + "</tr>"
        + ("<tr><td>x</td></tr>" * 100)
        + "</table>",
        '<img src="relative.png">' * 129,
        '<img src="data:image/png;base64,' + ("A" * (520 * 1024)) + '">',
        '<math alttext="\\dpsint"><mi>x</mi></math>',
        '<span class="math">\\dpsint</span>',
        "<math>" + ("<msup><mi>x</mi><mn>2</mn></msup>" * 1000) + "</math>",
        "<code>x</code>" * 1414,
        "<p>"
        + ("$x$ " * ((SOURCE_SERIALIZATION_RECEIPT_V1_MAX_MATH_DELIMITER_TOKENS // 2) + 1))
        + "</p>",
        "<p>"
        + ("{" * (SOURCE_SERIALIZATION_RECEIPT_V1_MAX_LATEX_GROUP_DEPTH + 1))
        + "x"
        + ("}" * (SOURCE_SERIALIZATION_RECEIPT_V1_MAX_LATEX_GROUP_DEPTH + 1))
        + "</p>",
    )
    for index, html_case in enumerate(rejected_cases):
        try:
            _preflight_serializer_structure(
                html_case,
                source_url=long_source_url if index in {2, 3} else source_url,
                max_output_chars=8 * 1024 * 1024,
            )
        except SourceSerializationReceiptError:
            continue
        raise RuntimeError("serializer preflight admitted an adversarial structure")

    rejected_source_cases = (
        "<html><body>" + ("<div>x</div>" * 5001) + "</body></html>",
        '<html><body><main _item_id="1">caller value</main></body></html>',
    )
    for html_case in rejected_source_cases:
        try:
            preflight_quality_source_input_v1(html_case)
        except SourceSerializationReceiptError:
            continue
        raise RuntimeError("quality preprocessor preflight admitted an invalid source")
    print(
        json.dumps(
            {
                "item_count": minted.receipt.item_count,
                "mixed_item_count": mixed_mint.receipt.item_count,
                "output_chars": len(minted.text),
                "preflight_conformance_cases": len(conformance_cases),
                "preflight_rejected_cases": len(rejected_cases),
                "preprocessor_rejected_cases": len(rejected_source_cases),
                "receipt_sha256": minted.receipt.receipt_sha256,
                "schema": minted.receipt.schema_version,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

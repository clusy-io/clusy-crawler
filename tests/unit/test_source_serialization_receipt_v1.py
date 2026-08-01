from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from app.services import source_serialization_receipt_v1 as receipt_module
from app.services.source_serialization_receipt_v1 import (
    MINERU_HTML_DISTRIBUTION,
    MINERU_HTML_PREPROCESSOR_CUTOFF_LENGTH,
    MINERU_HTML_PREPROCESSOR_ENTRYPOINT,
    MINERU_HTML_REVISION,
    MINERU_HTML_VERSION,
    MINERU_WEBKIT_DISTRIBUTION,
    MINERU_WEBKIT_ENTRYPOINT,
    MINERU_WEBKIT_OUTPUT_FORMAT,
    MINERU_WEBKIT_VERSION,
    SOURCE_SERIALIZATION_RECEIPT_V1_SCHEMA,
    QualitySourceSerializationReceiptV1,
    SourceSerializationReceiptError,
    build_quality_source_serialization_receipt_v1,
    verify_quality_source_serialization_receipt_v1,
)


class _ExplosiveString(str):
    def __eq__(self, other: object) -> bool:
        del other
        raise AssertionError("hostile string reached equality")


class _BytesSubclass(bytes):
    pass


def _fixture() -> dict[str, object]:
    mapped = (
        "<html><body>"
        '<nav _item_id="1">Navigation</nav>'
        "<main>"
        '<h1 _item_id="2">Receipt title</h1>'
        '<p _item_id="3">Selected source body with enough useful words.</p>'
        "</main>"
        '<footer _item_id="4">Footer</footer>'
        "</body></html>"
    )
    return {
        "raw_html": "<html><body>original source</body></html>",
        "source_url": "https://example.test/article?view=full",
        "raw_model_response": ('{"1":"other","2":"main","3":"main","4":"other"}'),
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
            "<html>\n<body><main>"
            '<h1 _item_id="2">Receipt title</h1>'
            '<p _item_id="3">Selected source body with enough useful words.</p>'
            "</main></body>\n</html>"
        ),
        "output_text": "# Receipt title\n\nSelected source body with enough useful words.",
        "upstream_revision": "73cf266690befd209cae7e6fdff9716d5b31a976",
        "prompt_profile": "openai_json",
    }


def _single_item_fixture(
    body_html: str,
    *,
    source_url: str = "https://example.test/article",
    output_text: str = "bounded output",
) -> dict[str, object]:
    mapped = f'<html><body><main _item_id="1">{body_html}</main></body></html>'
    return {
        "raw_html": "<html><body>source replay fixture</body></html>",
        "source_url": source_url,
        "raw_model_response": '{"1":"main"}',
        "response_format": "json",
        "simplified_html": mapped,
        "mapped_html": mapped,
        "item_labels": {"1": "main"},
        "selected_html": mapped,
        "output_text": output_text,
        "upstream_revision": "73cf266690befd209cae7e6fdff9716d5b31a976",
        "prompt_profile": "openai_json",
    }


def _install_serializer(
    monkeypatch: pytest.MonkeyPatch,
    *,
    output: object,
    calls: list[dict[str, object]] | None = None,
    preprocess_values: dict[str, object] | None = None,
) -> None:
    values = _fixture() if preprocess_values is None else preprocess_values

    def preprocess(html_str: str, cutoff_length: int = 500) -> object:
        del html_str, cutoff_length
        return values["simplified_html"], values["mapped_html"]

    def serialize(
        *,
        main_html: str,
        url: str | None,
        output_format: str,
    ) -> object:
        if calls is not None:
            calls.append(
                {
                    "main_html": main_html,
                    "url": url,
                    "output_format": output_format,
                }
            )
        if isinstance(output, BaseException):
            raise output
        return output

    monkeypatch.setattr(
        receipt_module,
        "load_pinned_mineru_html_preprocessor_v1",
        lambda: preprocess,
    )
    monkeypatch.setattr(
        receipt_module,
        "load_pinned_mineru_webkit_serializer_v1",
        lambda: serialize,
    )


def _build(
    monkeypatch: pytest.MonkeyPatch,
    **overrides: object,
) -> QualitySourceSerializationReceiptV1:
    values = _fixture()
    values.update(overrides)
    _install_serializer(monkeypatch, output=values["output_text"])
    return build_quality_source_serialization_receipt_v1(**values)


def test_builds_closed_source_selection_serialization_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _fixture()
    calls: list[dict[str, object]] = []
    _install_serializer(
        monkeypatch,
        output=f"  {values['output_text']}\n",
        calls=calls,
    )

    receipt = build_quality_source_serialization_receipt_v1(**values)

    assert receipt.schema_version == SOURCE_SERIALIZATION_RECEIPT_V1_SCHEMA
    assert receipt.preprocessor_distribution == MINERU_HTML_DISTRIBUTION
    assert receipt.preprocessor_version == MINERU_HTML_VERSION
    assert receipt.preprocessor_revision == MINERU_HTML_REVISION
    assert receipt.preprocessor_entrypoint == MINERU_HTML_PREPROCESSOR_ENTRYPOINT
    assert receipt.preprocessor_cutoff_length == MINERU_HTML_PREPROCESSOR_CUTOFF_LENGTH
    assert receipt.model_prompt_version == "v2"
    assert receipt.serializer_distribution == MINERU_WEBKIT_DISTRIBUTION
    assert receipt.serializer_version == MINERU_WEBKIT_VERSION
    assert receipt.serializer_entrypoint == MINERU_WEBKIT_ENTRYPOINT
    assert receipt.serializer_output_format == MINERU_WEBKIT_OUTPUT_FORMAT
    assert (
        receipt.source_url_sha256 == hashlib.sha256(str(values["source_url"]).encode()).hexdigest()
    )
    output_encoded = str(values["output_text"]).encode()
    assert receipt.output_sha256 == hashlib.sha256(output_encoded).hexdigest()
    assert receipt.output_bytes == len(output_encoded)
    assert receipt.selection_receipt_sha256 == receipt.selection_receipt.receipt_sha256
    assert receipt.serializer_input_sha256 == (receipt.selection_receipt.selected_html_sha256)
    assert receipt.item_count == 4
    assert receipt.selected_count == 2
    assert receipt.source_derivation_replay_verified is True
    assert receipt.selection_replay_verified is True
    assert receipt.serialization_replay_verified is True
    assert receipt.replay_verified is True
    assert receipt.digest_is_authentication is False
    assert len(receipt.receipt_sha256) == 64
    assert calls == [
        {
            "main_html": (
                "<html><body><main>"
                '<h1 _item_id="2">Receipt title</h1>'
                '<p _item_id="3">Selected source body with enough useful words.</p>'
                "</main></body></html>"
            ),
            "url": values["source_url"],
            "output_format": "mm_md",
        }
    ]
    assert verify_quality_source_serialization_receipt_v1(
        receipt,
        raw_html=values["raw_html"],
        source_url=values["source_url"],
        output_text=values["output_text"],
    )


def test_empty_source_url_replays_as_none_but_binds_empty_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _fixture()
    values["source_url"] = ""
    calls: list[dict[str, object]] = []
    _install_serializer(monkeypatch, output=values["output_text"], calls=calls)

    receipt = build_quality_source_serialization_receipt_v1(**values)

    assert calls[0]["url"] is None
    assert receipt.source_url_sha256 == hashlib.sha256(b"").hexdigest()


def test_source_url_budget_matches_public_fetch_contract_before_serializer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _fixture()
    values["source_url"] = "https://example.test/" + ("a" * 4076)
    calls: list[dict[str, object]] = []
    _install_serializer(monkeypatch, output=values["output_text"], calls=calls)

    with pytest.raises(
        receipt_module.QualitySourceInputIneligibleError,
        match="source_url",
    ):
        build_quality_source_serialization_receipt_v1(**values)

    assert calls == []


def test_preflight_rejects_selected_text_above_output_budget_before_serializer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _fixture()
    values["output_text"] = "short"
    calls: list[dict[str, object]] = []
    _install_serializer(
        monkeypatch,
        output=values["output_text"],
        calls=calls,
    )

    with pytest.raises(
        receipt_module.QualitySourceInputIneligibleError,
        match="selected source text",
    ):
        build_quality_source_serialization_receipt_v1(
            **values,
            max_output_chars=32,
        )

    assert calls == []


def test_mint_self_preflights_raw_source_before_pinned_preprocessor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _fixture()
    values["raw_html"] = "<main>" + ("<span>x</span>" * 5000) + "</main>"
    preprocessor_calls = 0

    def forbidden_preprocessor(*_: object, **__: object) -> object:
        nonlocal preprocessor_calls
        preprocessor_calls += 1
        raise AssertionError("ineligible raw source reached the pinned preprocessor")

    monkeypatch.setattr(
        receipt_module,
        "load_pinned_mineru_html_preprocessor_v1",
        lambda: forbidden_preprocessor,
    )

    with pytest.raises(
        receipt_module.QualitySourceInputIneligibleError,
        match="node budget",
    ):
        receipt_module.mint_quality_source_serialization_v1(
            raw_html=values["raw_html"],
            source_url=values["source_url"],
            raw_model_response=values["raw_model_response"],
            response_format=values["response_format"],
            simplified_html=values["simplified_html"],
            mapped_html=values["mapped_html"],
            item_labels=values["item_labels"],
            selected_html=values["selected_html"],
            upstream_revision=values["upstream_revision"],
            prompt_profile=values["prompt_profile"],
        )

    assert preprocessor_calls == 0


def test_selected_dom_character_admission_is_input_ineligibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body_html = '<div class="' + ("a" * (769 * 1024)) + '">x</div>'
    values = _single_item_fixture(body_html)
    calls: list[dict[str, object]] = []
    _install_serializer(
        monkeypatch,
        output=values["output_text"],
        calls=calls,
        preprocess_values=values,
    )

    with pytest.raises(
        receipt_module.QualitySourceInputIneligibleError,
        match="selected source DOM",
    ):
        build_quality_source_serialization_receipt_v1(**values)

    assert calls == []


@pytest.mark.parametrize(
    ("body_html", "reason"),
    [
        (
            '<svg width="100000" height="100000"><path d="M0 0"/></svg>',
            "inline SVG",
        ),
        ("<script>window.MathJax = {tex: {}}</script>", "scripts"),
        ("<ccimage>caller-forged converter node</ccimage>", "internal converter"),
    ],
)
def test_structural_preflight_rejects_unsafe_active_or_internal_tags(
    monkeypatch: pytest.MonkeyPatch,
    body_html: str,
    reason: str,
) -> None:
    values = _single_item_fixture(body_html)
    calls: list[dict[str, object]] = []
    _install_serializer(
        monkeypatch,
        output=values["output_text"],
        calls=calls,
        preprocess_values=values,
    )

    with pytest.raises(SourceSerializationReceiptError, match=reason):
        build_quality_source_serialization_receipt_v1(**values)

    assert calls == []


def test_structural_preflight_rejects_sparse_table_grid_before_serializer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wide_row = "<tr>" + ("<td>x</td>" * 1000) + "</tr>"
    sparse_rows = "<tr><td>x</td></tr>" * 100
    values = _single_item_fixture(f"<table>{wide_row}{sparse_rows}</table>")
    calls: list[dict[str, object]] = []
    _install_serializer(
        monkeypatch,
        output=values["output_text"],
        calls=calls,
        preprocess_values=values,
    )

    with pytest.raises(SourceSerializationReceiptError, match="table.*grid"):
        build_quality_source_serialization_receipt_v1(**values)

    assert calls == []


def test_structural_preflight_rejects_long_base_image_expansion_before_serializer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = "https://example.test/"
    source_url = prefix + ("u" * (4096 - len(prefix)))
    values = _single_item_fixture(
        '<img src="relative.png">' * 129,
        source_url=source_url,
    )
    calls: list[dict[str, object]] = []
    _install_serializer(
        monkeypatch,
        output=values["output_text"],
        calls=calls,
        preprocess_values=values,
    )

    with pytest.raises(SourceSerializationReceiptError, match="image.*expansion"):
        build_quality_source_serialization_receipt_v1(**values)

    assert calls == []


def test_structural_preflight_rejects_large_data_image_with_no_visible_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_uri = "data:image/png;base64," + ("A" * (520 * 1024))
    values = _single_item_fixture(f'<img src="{data_uri}">')
    calls: list[dict[str, object]] = []
    _install_serializer(
        monkeypatch,
        output=values["output_text"],
        calls=calls,
        preprocess_values=values,
    )

    with pytest.raises(SourceSerializationReceiptError, match="image.*expansion"):
        build_quality_source_serialization_receipt_v1(**values)

    assert calls == []


def test_structural_preflight_rejects_nested_list_amplification_before_serializer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body_html = ("<ul>" * 33) + "item" + ("</ul>" * 33)
    values = _single_item_fixture(body_html)
    calls: list[dict[str, object]] = []
    _install_serializer(
        monkeypatch,
        output=values["output_text"],
        calls=calls,
        preprocess_values=values,
    )

    with pytest.raises(SourceSerializationReceiptError, match="list.*nesting"):
        build_quality_source_serialization_receipt_v1(**values)

    assert calls == []


def test_structural_preflight_rejects_mathml_before_serializer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    formula = "<math>" + ("<msup><mi>x</mi><mn>2</mn></msup>" * 1000) + "</math>"
    values = _single_item_fixture(formula)
    calls: list[dict[str, object]] = []
    _install_serializer(
        monkeypatch,
        output=values["output_text"],
        calls=calls,
        preprocess_values=values,
    )

    with pytest.raises(SourceSerializationReceiptError, match="MathML"):
        build_quality_source_serialization_receipt_v1(**values)

    assert calls == []


@pytest.mark.parametrize(
    "body_html",
    [
        '<span class="math">\\dpsint</span>',
        '<div class="katex">formula</div>',
        '<span data-tex="x^2">formula</span>',
        '<img src="formula-latex.png" alt="formula">',
    ],
)
def test_structural_preflight_rejects_formula_markup_before_serializer(
    monkeypatch: pytest.MonkeyPatch,
    body_html: str,
) -> None:
    values = _single_item_fixture(body_html)
    calls: list[dict[str, object]] = []
    _install_serializer(
        monkeypatch,
        output=values["output_text"],
        calls=calls,
        preprocess_values=values,
    )

    with pytest.raises(SourceSerializationReceiptError, match="formula markup"):
        build_quality_source_serialization_receipt_v1(**values)

    assert calls == []


def test_structural_preflight_rejects_plain_text_math_node_amplification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # MinerU-Webkit's default MathJax pass turns every matched delimiter pair
    # into a new DOM node. This compact source used to pass the linear
    # projection while causing superlinear insertion work inside the converter.
    body_html = (
        "<p>"
        + (
            "$x$ "
            * ((receipt_module.SOURCE_SERIALIZATION_RECEIPT_V1_MAX_MATH_DELIMITER_TOKENS // 2) + 1)
        )
        + "</p>"
    )
    values = _single_item_fixture(body_html)
    calls: list[dict[str, object]] = []
    _install_serializer(
        monkeypatch,
        output=values["output_text"],
        calls=calls,
        preprocess_values=values,
    )

    with pytest.raises(
        receipt_module.QualitySourceInputIneligibleError,
        match="math-delimiter",
    ):
        build_quality_source_serialization_receipt_v1(**values)

    assert calls == []


def test_math_and_latex_work_boundaries_are_closed_but_skip_code() -> None:
    preflight = receipt_module._preflight_serializer_structure
    delimiter_pairs = receipt_module.SOURCE_SERIALIZATION_RECEIPT_V1_MAX_MATH_DELIMITER_TOKENS // 2
    preflight(
        "<p>" + ("$x$ " * delimiter_pairs) + "</p>",
        source_url="",
        max_output_chars=500_000,
    )
    with pytest.raises(SourceSerializationReceiptError, match="math-delimiter"):
        preflight(
            "<p>" + ("$x$ " * (delimiter_pairs + 1)) + "</p>",
            source_url="",
            max_output_chars=500_000,
        )

    control_limit = receipt_module.SOURCE_SERIALIZATION_RECEIPT_V1_MAX_LATEX_CONTROL_TOKENS
    preflight(
        "<p>" + ("\\a" * control_limit) + "</p>",
        source_url="",
        max_output_chars=500_000,
    )
    with pytest.raises(SourceSerializationReceiptError, match="LaTeX-control"):
        preflight(
            "<p>" + ("\\a" * (control_limit + 1)) + "</p>",
            source_url="",
            max_output_chars=500_000,
        )

    group_limit = receipt_module.SOURCE_SERIALIZATION_RECEIPT_V1_MAX_LATEX_GROUP_DEPTH
    preflight(
        "<p>" + ("{" * group_limit) + "x" + ("}" * group_limit) + "</p>",
        source_url="",
        max_output_chars=500_000,
    )
    with pytest.raises(SourceSerializationReceiptError, match="group-depth"):
        preflight(
            "<p>" + ("{" * (group_limit + 1)) + "x" + ("}" * (group_limit + 1)) + "</p>",
            source_url="",
            max_output_chars=500_000,
        )

    # MathJax skips code/pre subtrees in the pinned converter; counting their
    # literal dollars and backslashes would reject ordinary source listings.
    preflight(
        "<pre><code>" + ("$x$ \\a " * 5000) + "</code></pre>",
        source_url="",
        max_output_chars=500_000,
    )


def test_projection_accounts_for_paragraph_entity_expansion() -> None:
    html = "<p>" + (">" * 100) + "</p>"
    projected = receipt_module._preflight_serializer_structure(
        html,
        source_url="",
        max_output_chars=525,
    )

    assert projected == 525
    with pytest.raises(SourceSerializationReceiptError, match="projected"):
        receipt_module._preflight_serializer_structure(
            html,
            source_url="",
            max_output_chars=524,
        )


@pytest.mark.parametrize(
    "source_url",
    [
        "https://mathinsight.org/article",
        "https://www.mathinsight.org/article",
        "https://MATHINSIGHT.ORG./article",
        "https://mathinsight.org.example.test/article",
        "https://example.test/mathinsight.org/article",
        "https://mathinsight.org@example.test/article",
    ],
)
def test_structural_preflight_rejects_host_specific_macro_transforms(
    monkeypatch: pytest.MonkeyPatch,
    source_url: str,
) -> None:
    values = _single_item_fixture("<p>ordinary text</p>", source_url=source_url)
    calls: list[dict[str, object]] = []
    _install_serializer(
        monkeypatch,
        output=values["output_text"],
        calls=calls,
        preprocess_values=values,
    )

    with pytest.raises(SourceSerializationReceiptError, match="source URL"):
        build_quality_source_serialization_receipt_v1(**values)

    assert calls == []


@pytest.mark.parametrize(
    "raw_html",
    [
        '<main _item_id="1">caller value</main>',
        '<main data-uid="caller">caller value</main>',
        "<cc-alg-uc-text>caller value</cc-alg-uc-text>",
    ],
)
def test_quality_preprocessor_preflight_rejects_reserved_source_contract(
    raw_html: str,
) -> None:
    with pytest.raises(
        receipt_module.QualitySourceInputIneligibleError,
        match="reserved quality",
    ):
        receipt_module.preflight_quality_source_input_v1(raw_html)


def test_quality_preprocessor_structural_boundaries_are_closed() -> None:
    preflight = receipt_module.preflight_quality_source_input_v1

    preflight("<main>" + ("<span></span>" * 4999) + "</main>")
    with pytest.raises(
        receipt_module.QualitySourceInputIneligibleError,
        match="node budget",
    ):
        preflight("<main>" + ("<span></span>" * 5000) + "</main>")

    preflight(("<div>" * 64) + "x" + ("</div>" * 64))
    with pytest.raises(
        receipt_module.QualitySourceInputIneligibleError,
        match="depth budget",
    ):
        preflight(("<div>" * 65) + "x" + ("</div>" * 65))

    fragments_at_limit = "<main>x" + ("<span>x</span>y" * 3999) + "<b>x</b></main>"
    preflight(fragments_at_limit)
    with pytest.raises(
        receipt_module.QualitySourceInputIneligibleError,
        match="text-fragment budget",
    ):
        preflight(fragments_at_limit.replace("</main>", "y</main>"))


def test_structural_preflight_rejects_code_scan_work_before_serializer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _single_item_fixture("<code>x</code>" * 1414)
    calls: list[dict[str, object]] = []
    _install_serializer(
        monkeypatch,
        output=values["output_text"],
        calls=calls,
        preprocess_values=values,
    )

    with pytest.raises(SourceSerializationReceiptError, match="code.*work"):
        build_quality_source_serialization_receipt_v1(**values)

    assert calls == []


def test_structural_preflight_work_boundaries_are_closed() -> None:
    preflight = receipt_module._preflight_serializer_structure

    preflight("<main>x</main>", source_url="", max_output_chars=33)
    with pytest.raises(SourceSerializationReceiptError, match="projected"):
        preflight("<main>x</main>", source_url="", max_output_chars=32)

    code_at_limit = "<main>" + ("<code>x</code>" * 1413) + "</main>"
    preflight(code_at_limit, source_url="", max_output_chars=8 * 1024 * 1024)
    with pytest.raises(SourceSerializationReceiptError, match="code.*work"):
        preflight(
            "<main>" + ("<code>x</code>" * 1414) + "</main>",
            source_url="",
            max_output_chars=8 * 1024 * 1024,
        )

    table_at_limit = (
        "<table>" + ("<tr><td>x</td></tr>" * 99) + "<tr>" + ("<td>x</td>" * 1000) + "</tr></table>"
    )
    preflight(table_at_limit, source_url="", max_output_chars=8 * 1024 * 1024)
    with pytest.raises(SourceSerializationReceiptError, match="table.*grid"):
        preflight(
            table_at_limit.replace("<table>", "<table><tr><td>x</td></tr>", 1),
            source_url="",
            max_output_chars=8 * 1024 * 1024,
        )

    with pytest.raises(SourceSerializationReceiptError, match="MathML"):
        preflight(
            '<math alttext="\\dpsint"><mi>x</mi></math>',
            source_url="https://example.test/",
            max_output_chars=79,
        )

    preflight("<ul>" * 32 + "x" + "</ul>" * 32, source_url="", max_output_chars=8 * 1024 * 1024)
    with pytest.raises(SourceSerializationReceiptError, match="list.*nesting"):
        preflight(
            "<ul>" * 33 + "x" + "</ul>" * 33,
            source_url="",
            max_output_chars=8 * 1024 * 1024,
        )


@pytest.mark.parametrize("limit", [0, True, 8 * 1024 * 1024 + 1])
def test_builder_rejects_invalid_serializer_output_budget(
    monkeypatch: pytest.MonkeyPatch,
    limit: object,
) -> None:
    values = _fixture()
    _install_serializer(monkeypatch, output=values["output_text"])

    with pytest.raises(SourceSerializationReceiptError, match="output budget"):
        build_quality_source_serialization_receipt_v1(
            **values,
            max_output_chars=limit,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "quality-source-selection.v0"),
        ("preprocessor_distribution", "mineru-html-fork"),
        ("preprocessor_version", "1.1.3"),
        ("preprocessor_revision", "0" * 40),
        ("preprocessor_entrypoint", "forged.preprocessor"),
        ("preprocessor_cutoff_length", 499),
        ("preprocessor_cutoff_length", True),
        ("model_prompt_version", "forged"),
        ("serializer_distribution", "mineru-webkit-fork"),
        ("serializer_version", "0.1.7"),
        ("serializer_entrypoint", "forged.converter"),
        ("serializer_output_format", "markdown"),
        ("source_url_sha256", "0" * 64),
        ("serializer_input_sha256", "0" * 64),
        ("replayed_selected_html", "<html><body>forged</body></html>"),
        ("output_sha256", "0" * 64),
        ("output_bytes", 1),
        ("selection_receipt_sha256", "0" * 64),
        ("source_derivation_replay_verified", False),
        ("source_derivation_replay_verified", 1),
        ("selection_replay_verified", False),
        ("selection_replay_verified", 1),
        ("serialization_replay_verified", False),
        ("serialization_replay_verified", 1),
        ("process_authentication_scope", "portable-signature"),
        ("process_authentication_mac", b""),
        ("process_authentication_mac", bytearray(32)),
        ("process_authentication_mac", _BytesSubclass(b"x" * 32)),
        ("receipt_sha256", "0" * 64),
        ("digest_is_authentication", True),
        ("digest_is_authentication", 0),
    ],
)
def test_verifier_rejects_every_mutated_top_level_field(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    values = _fixture()
    receipt = _build(monkeypatch)

    assert not verify_quality_source_serialization_receipt_v1(
        replace(receipt, **{field: value}),
        raw_html=values["raw_html"],
        source_url=values["source_url"],
        output_text=values["output_text"],
    )


def test_verifier_rejects_mutated_inputs_and_nested_v0_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _fixture()
    receipt = _build(monkeypatch)
    nested = replace(receipt.selection_receipt, selected_count=1)

    assert not verify_quality_source_serialization_receipt_v1(
        receipt,
        raw_html="<html>different</html>",
        source_url=values["source_url"],
        output_text=values["output_text"],
    )
    assert not verify_quality_source_serialization_receipt_v1(
        receipt,
        raw_html=values["raw_html"],
        source_url="https://example.test/different",
        output_text=values["output_text"],
    )
    assert not verify_quality_source_serialization_receipt_v1(
        receipt,
        raw_html=values["raw_html"],
        source_url=values["source_url"],
        output_text=f"{values['output_text']} forged",
    )
    assert not verify_quality_source_serialization_receipt_v1(
        replace(receipt, selection_receipt=nested),
        raw_html=values["raw_html"],
        source_url=values["source_url"],
        output_text=values["output_text"],
    )


def test_verifier_rejects_hostile_schema_without_running_equality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _fixture()
    receipt = _build(monkeypatch)

    assert not verify_quality_source_serialization_receipt_v1(
        replace(receipt, schema_version=_ExplosiveString(receipt.schema_version)),
        raw_html=values["raw_html"],
        source_url=values["source_url"],
        output_text=values["output_text"],
    )


def test_self_consistent_public_dataclass_forgery_cannot_earn_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonical hashes are identities; acceptance still reruns serialization."""

    values = _fixture()
    receipt = _build(monkeypatch)
    forged_output = "# Forged\n\ncaller supplied different but self-consistently hashed output text"
    encoded = forged_output.encode()
    draft = replace(
        receipt,
        output_sha256=hashlib.sha256(encoded).hexdigest(),
        output_bytes=len(encoded),
        receipt_sha256="0" * 64,
    )
    forged = replace(
        draft,
        receipt_sha256=receipt_module._sha256_json(
            receipt_module._receipt_identity(
                draft,
                draft.selection_receipt,
            )
        ),
    )

    assert not verify_quality_source_serialization_receipt_v1(
        forged,
        raw_html=values["raw_html"],
        source_url=values["source_url"],
        output_text=forged_output,
    )


def test_mint_rederives_selected_dom_from_raw_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Foreign upstream artifacts cannot pass the trusted derivation replay."""

    values = _fixture()
    _install_serializer(monkeypatch, output=values["output_text"])
    benign = (
        "<html><body>"
        '<nav _item_id="1">Benign navigation</nav>'
        "<main>"
        '<h1 _item_id="2">Benign title</h1>'
        '<p _item_id="3">Only benign source text exists on this page.</p>'
        "</main>"
        '<footer _item_id="4">Benign footer</footer>'
        "</body></html>"
    )

    def preprocess_benign(html_str: str, cutoff_length: int = 500) -> object:
        del html_str, cutoff_length
        return benign, benign

    monkeypatch.setattr(
        receipt_module,
        "load_pinned_mineru_html_preprocessor_v1",
        lambda: preprocess_benign,
    )

    with pytest.raises(SourceSerializationReceiptError, match="exact source-derived"):
        build_quality_source_serialization_receipt_v1(**values)


def test_receipt_identity_is_deterministic_and_binds_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _build(monkeypatch)
    second = _build(monkeypatch)
    changed_url = _build(
        monkeypatch,
        source_url="https://example.test/article?view=compact",
    )

    assert first == second
    assert first.receipt_sha256 == second.receipt_sha256
    assert first.receipt_sha256 != changed_url.receipt_sha256


def test_builder_rejects_mutated_or_non_text_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _fixture()
    _install_serializer(monkeypatch, output="different output")
    with pytest.raises(SourceSerializationReceiptError, match="differs"):
        build_quality_source_serialization_receipt_v1(**values)

    _install_serializer(monkeypatch, output=b"not exact text")
    with pytest.raises(SourceSerializationReceiptError, match="exact text"):
        build_quality_source_serialization_receipt_v1(**values)

    _install_serializer(monkeypatch, output=RuntimeError("hostile details"))
    with pytest.raises(SourceSerializationReceiptError, match="replay failed"):
        build_quality_source_serialization_receipt_v1(**values)


def test_builder_and_verifier_reject_noncanonical_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _fixture()
    _install_serializer(monkeypatch, output=values["output_text"])
    values["output_text"] = f" {values['output_text']} "

    with pytest.raises(SourceSerializationReceiptError, match="stripped"):
        build_quality_source_serialization_receipt_v1(**values)

    receipt = _build(monkeypatch)
    assert not verify_quality_source_serialization_receipt_v1(
        receipt,
        raw_html=_fixture()["raw_html"],
        source_url=_fixture()["source_url"],
        output_text=f" {receipt_module.MINERU_WEBKIT_OUTPUT_FORMAT} ",
    )


def test_verifier_uses_authenticated_mint_without_loading_mutable_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _fixture()
    receipt = _build(monkeypatch)

    def unavailable() -> object:
        raise SourceSerializationReceiptError("unavailable")

    monkeypatch.setattr(
        receipt_module,
        "load_pinned_mineru_webkit_serializer_v1",
        unavailable,
    )
    monkeypatch.setattr(
        receipt_module,
        "load_pinned_mineru_html_preprocessor_v1",
        unavailable,
    )
    assert verify_quality_source_serialization_receipt_v1(
        receipt,
        raw_html=values["raw_html"],
        source_url=values["source_url"],
        output_text=values["output_text"],
    )


def test_process_key_rotation_invalidates_outstanding_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _fixture()
    receipt = _build(monkeypatch)
    current_pid = receipt_module.os.getpid()
    monkeypatch.setattr(receipt_module.os, "getpid", lambda: current_pid + 1)

    assert not verify_quality_source_serialization_receipt_v1(
        receipt,
        raw_html=values["raw_html"],
        source_url=values["source_url"],
        output_text=values["output_text"],
    )


def test_process_mac_is_hidden_from_receipt_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    receipt = _build(monkeypatch)

    assert repr(receipt.process_authentication_mac) not in repr(receipt)


def test_locked_serializer_prevents_cross_request_state_corruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _fixture()
    active = 0
    maximum_active = 0
    state_lock = threading.Lock()

    def serialize(
        *,
        main_html: str,
        url: str | None,
        output_format: str,
    ) -> str:
        del main_html, url, output_format
        nonlocal active, maximum_active
        with state_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.01)
        with state_lock:
            active -= 1
        return str(values["output_text"])

    _install_serializer(monkeypatch, output=values["output_text"])
    monkeypatch.setattr(
        receipt_module,
        "load_pinned_mineru_webkit_serializer_v1",
        lambda: serialize,
    )

    def build(index: int) -> QualitySourceSerializationReceiptV1:
        return build_quality_source_serialization_receipt_v1(
            **{
                **values,
                "source_url": f"https://example.test/article/{index}",
            }
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(build, range(8)))

    assert len(receipts) == 8
    assert maximum_active == 1


def test_wrong_receipt_type_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    values = _fixture()
    _install_serializer(monkeypatch, output=values["output_text"])

    assert not verify_quality_source_serialization_receipt_v1(
        object(),
        raw_html=values["raw_html"],
        source_url=values["source_url"],
        output_text=values["output_text"],
    )

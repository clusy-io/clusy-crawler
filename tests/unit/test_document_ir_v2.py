from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from clusy_native import (
    DocumentIRV2Limits,
    extract_document_ir,
    extract_document_ir_v2,
    reconstruct_document_ir_v2,
)

_STRUCTURED_HTML = """\
<main id="content">
  <h2>V2 <em>ordered</em> IR</h2>
  <pre><code class="language-python">def answer():
    return 42</code></pre>
  <table>
    <tr><th rowspan="2" scope="row">Name</th><th colspan="2">Scores</th></tr>
    <tr><td>A</td><td>B</td></tr>
  </table>
  <ol start="3"><li>first</li><li value="8">second</li></ol>
  <math display="block" alttext="x squared">
    <semantics><msup><mi>x</mi><mn>2</mn></msup>
      <annotation encoding="application/x-tex">x^2</annotation>
    </semantics>
  </math>
</main>
"""


def test_v2_exposes_ordered_source_backed_graph_and_exact_code() -> None:
    result = extract_document_ir_v2(_STRUCTURED_HTML)

    assert result.schema_version == "ordered-dom-ir.v2"
    assert result.serialization_contract == "ordered-dom-ir.v2.markdown.1"
    assert result.event_count == result.element_count + result.text_run_count
    all_orders = sorted(
        [element.order for element in result.elements] + [text.order for text in result.text_runs]
    )
    assert all_orders == list(range(result.event_count))
    assert len({element.id for element in result.elements}) == result.element_count
    assert all(element.path.startswith("/") for element in result.elements)

    main = next(element for element in result.elements if element.tag == "main")
    assert result.source[main.source_start : main.source_end] == _STRUCTURED_HTML.rstrip("\n")
    assert main.source_span_reliable

    code = next(text for text in result.text_runs if "return 42" in text.text)
    assert code.text == "def answer():\n    return 42"
    assert code.preserve_whitespace
    assert not code.truncated


def test_v2_table_list_math_and_reconstruction_contract() -> None:
    result = extract_document_ir_v2(_STRUCTURED_HTML)

    assert result.table_count == 1
    assert result.tables[0].row_count == 2
    assert result.tables[0].column_count == 3
    assert [
        (
            cell.row_index,
            cell.column_index,
            cell.row_span,
            cell.column_span,
        )
        for cell in result.table_cells
    ] == [(0, 0, 2, 1), (0, 1, 1, 2), (1, 1, 1, 1), (1, 2, 1, 1)]
    assert [item.ordinal for item in result.list_items] == [3, 8]
    assert result.math_count == 1
    assert result.math[0].format == "mathml"
    assert result.math[0].tex == "x^2"
    assert result.math[0].alt_text == "x squared"

    first = reconstruct_document_ir_v2(result)
    second = reconstruct_document_ir_v2(result)
    assert first.markdown == second.markdown
    assert "```python\ndef answer():\n    return 42\n```" in first.markdown
    assert 'rowspan="2"' in first.markdown
    assert 'colspan="2"' in first.markdown
    assert "$$\nx^2\n$$" in first.markdown
    assert first.exact_code_whitespace
    assert first.table_grid_complete


def test_v2_selection_is_closed_under_ancestors_and_never_broadens_unknown_ids() -> None:
    result = extract_document_ir_v2("<main><p>keep <em>this</em></p><p>drop this</p></main>")
    selected = next(text for text in result.text_runs if text.text == "this")

    reconstructed = reconstruct_document_ir_v2(
        result,
        selected_ids=[selected.id, "node-does-not-exist"],
    )

    assert reconstructed.selected_ids == [selected.id]
    assert reconstructed.missing_ids == ["node-does-not-exist"]
    assert reconstructed.markdown == "*this*"
    assert "drop" not in reconstructed.markdown
    assert reconstruct_document_ir_v2(result, selected_ids=[]).markdown == ""


def test_v2_limit_provenance_is_utf8_safe_and_v1_is_unchanged() -> None:
    html = "<main><p>" + ("é" * 100) + "</p><table><tr><td colspan=99>x</td></tr></table></main>"
    limits = DocumentIRV2Limits(
        max_input_bytes=1_024,
        max_nodes=100,
        max_elements=50,
        max_text_runs=50,
        max_depth=20,
        max_text_run_bytes=9,
        max_total_text_bytes=9,
        max_math_bytes=128,
        max_table_columns=2,
    )

    result = extract_document_ir_v2(html, limits=limits)

    assert result.truncated
    assert result.text_truncated_runs >= 1
    assert result.table_grid_truncated
    assert {"text_bytes", "table_columns"} <= set(result.truncation_reasons)
    assert all(text.stored_bytes == len(text.text.encode()) for text in result.text_runs)
    assert all(text.text.encode().decode() == text.text for text in result.text_runs)

    v1 = extract_document_ir("<p>compatibility</p>")
    assert v1.schema_version == "ordered-dom-ir.v1"


def test_v2_math_budget_is_document_wide_and_explicit() -> None:
    limits = DocumentIRV2Limits(max_math_bytes=24)
    result = extract_document_ir_v2(
        "<main><math><mi>first</mi></math><math><mi>second</mi></math></main>",
        limits=limits,
    )

    assert sum(len(math.source_markup.encode()) for math in result.math) <= 24
    assert result.math_truncated_nodes > 0
    assert "math_markup" in result.truncation_reasons


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_input_bytes", 0),
        ("max_nodes", 0),
        ("max_elements", 300_000),
        ("max_depth", 600),
        ("max_table_columns", 5_000),
    ],
)
def test_v2_rejects_invalid_limits(field: str, value: int) -> None:
    values = {
        "max_input_bytes": 1_024,
        "max_nodes": 100,
        "max_elements": 100,
        "max_text_runs": 100,
        "max_depth": 20,
        "max_text_run_bytes": 1_024,
        "max_total_text_bytes": 2_048,
        "max_math_bytes": 1_024,
        "max_table_columns": 100,
    }
    values[field] = value

    with pytest.raises(ValueError):
        extract_document_ir_v2("<p>text</p>", limits=DocumentIRV2Limits(**values))


def test_v2_is_stable_across_parallel_native_calls() -> None:
    with ThreadPoolExecutor(max_workers=4) as pool:
        outputs = list(pool.map(lambda _: extract_document_ir_v2(_STRUCTURED_HTML), range(16)))

    expected = [(element.id, element.order, element.path) for element in outputs[0].elements]
    assert all(
        [(element.id, element.order, element.path) for element in output.elements] == expected
        for output in outputs
    )

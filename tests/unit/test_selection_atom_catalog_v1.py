from __future__ import annotations

import hashlib
import inspect
import random

import clusy_native.selection_atom_catalog_v1 as catalog_module
import pytest
from clusy_native import (
    SELECTION_ATOM_CATALOG_V1_SCHEMA,
    DocumentIRV2Limits,
    SelectionAtomCatalogV1Config,
    build_selection_atom_catalog_from_document_v1,
    build_selection_atom_catalog_v1,
    extract_document_ir_v2,
)

_STRUCTURED_HTML = """\
<!doctype html><html><body><main>
  <p>plain prose</p>
  <pre><code class="language-python">def answer():
    return 42</code></pre>
  <table><tr><td>cell alpha</td><td>cell beta</td></tr></table>
  <ol><li>first item</li><li>second item</li></ol>
  <math display="block"><semantics><mi>x</mi>
    <annotation encoding="application/x-tex">x^2</annotation>
  </semantics></math>
</main></body></html>
"""


def _enabled(**changes: int | bool) -> SelectionAtomCatalogV1Config:
    values: dict[str, int | bool] = {
        "enabled": True,
        "max_source_bytes": 4 * 1024 * 1024,
        "max_atoms": 65_536,
        "max_total_atom_source_bytes": 4 * 1024 * 1024,
        "max_ancestry_steps": 2_000_000,
        "max_identifier_chars": 1_024,
    }
    values.update(changes)
    return SelectionAtomCatalogV1Config(**values)  # type: ignore[arg-type]


def test_catalog_is_default_off_and_has_no_decision_or_reference_surface() -> None:
    result = build_selection_atom_catalog_v1(_STRUCTURED_HTML)

    assert result.schema_version == SELECTION_ATOM_CATALOG_V1_SCHEMA
    assert not result.enabled
    assert not result.accepted
    assert result.reason == "disabled"
    assert result.atoms == ()
    assert tuple(inspect.signature(build_selection_atom_catalog_v1).parameters) == (
        "html",
        "config",
    )
    assert tuple(inspect.signature(build_selection_atom_catalog_from_document_v1).parameters) == (
        "document",
        "config",
    )


def test_catalog_exposes_disjoint_typed_source_atoms_and_stable_closures() -> None:
    config = _enabled()
    first = build_selection_atom_catalog_v1(_STRUCTURED_HTML, config=config)
    second = build_selection_atom_catalog_v1(_STRUCTURED_HTML, config=config)
    document = extract_document_ir_v2(_STRUCTURED_HTML)

    assert first.accepted
    assert first.reason == "accepted"
    assert first.catalog_digest == second.catalog_digest
    assert first.atoms == second.atoms
    assert first.atom_count == first.candidate_text_run_count
    assert {atom.kind for atom in first.atoms} == {
        "text",
        "code",
        "table_cell",
        "list_item",
        "math",
    }
    assert [atom.order for atom in first.atoms] == list(range(first.atom_count))
    assert all(
        left.source_end <= right.source_start
        for left, right in zip(first.atoms, first.atoms[1:], strict=False)
    )
    assert first.atom_source_bytes == sum(atom.source_bytes for atom in first.atoms)
    assert first.atom_source_bytes <= first.source_bytes

    source_bytes = document.source.encode("utf-8")
    element_ids = {element.id for element in document.elements}
    text_runs = {run.id: run for run in document.text_runs}
    text_ids = set(text_runs)
    for atom in first.atoms:
        fragment = source_bytes[atom.source_start : atom.source_end]
        selection_fragment = source_bytes[atom.selection_source_start : atom.selection_source_end]
        assert fragment.decode() == text_runs[atom.text_run_id].text
        assert hashlib.sha256(fragment).hexdigest() == atom.source_fragment_sha256
        assert (
            hashlib.sha256(selection_fragment).hexdigest() == atom.selection_source_fragment_sha256
        )
        assert atom.selection_id in element_ids | text_ids
        assert atom.mapping_reliable
        assert atom.selection_mapping_reliable
        assert atom.source_backed
        assert (
            atom.selection_source_start
            <= atom.source_start
            < atom.source_end
            <= atom.selection_source_end
        )

    code = next(atom for atom in first.atoms if atom.kind == "code")
    cell = next(atom for atom in first.atoms if atom.kind == "table_cell")
    item = next(atom for atom in first.atoms if atom.kind == "list_item")
    math = next(atom for atom in first.atoms if atom.kind == "math")
    assert code.code_element_id == code.selection_id
    assert code.code_language == "python"
    assert cell.table_id and cell.table_cell_id and cell.selection_id
    assert (
        cell.table_row_index,
        cell.table_column_index,
        cell.table_row_span,
        cell.table_column_span,
        cell.table_header,
    ) == (0, 0, 1, 1, False)
    assert item.list_id and item.list_item_id and item.selection_id
    assert (item.list_depth, item.list_index, item.list_kind, item.list_ordinal) == (
        0,
        0,
        "item",
        1,
    )
    assert math.math_id and math.selection_id
    assert (math.math_format, math.math_display) == ("mathml", "block")


def test_nested_structure_uses_one_primary_kind_but_retains_all_memberships() -> None:
    html = """\
<!doctype html><html><body>
<table><tr><td><ol><li><code>nested code</code></li></ol></td></tr></table>
</body></html>
"""
    result = build_selection_atom_catalog_v1(html, config=_enabled())

    assert result.accepted
    atom = next(atom for atom in result.atoms if atom.kind == "code")
    assert atom.code_element_id == atom.selection_id
    assert atom.table_id is not None
    assert atom.table_cell_id is not None
    assert atom.list_id is not None
    assert atom.list_item_id is not None
    assert atom.math_id is None


def test_text_run_id_replays_every_kind_and_selection_id_is_closure_metadata() -> None:
    document = extract_document_ir_v2(_STRUCTURED_HTML)
    result = build_selection_atom_catalog_from_document_v1(
        document,
        config=_enabled(),
    )
    text_runs = {run.id: run for run in document.text_runs}

    assert result.accepted
    assert {atom.kind for atom in result.atoms} == {
        "text",
        "code",
        "table_cell",
        "list_item",
        "math",
    }
    for atom in result.atoms:
        replay = document.reconstruct([atom.text_run_id])
        assert replay.selected_ids == [atom.text_run_id]
        assert replay.markdown
        assert text_runs[atom.text_run_id].text in replay.markdown

    table_atom = next(atom for atom in result.atoms if atom.kind == "table_cell")
    assert table_atom.selection_id != table_atom.text_run_id
    assert "cell alpha" in document.reconstruct([table_atom.text_run_id]).markdown
    assert "cell alpha" in document.reconstruct([table_atom.selection_id]).markdown
    # `selection_id` identifies a typed closure owner, but does not decide which
    # grouped atoms or enclosing structure a downstream typed replay must retain.


def test_catalog_rejects_a_forged_selection_span_that_excludes_its_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = "<table><tr><td>cell</td></tr></table>"
    document = extract_document_ir_v2(html)
    forged_fragment = html.encode("utf-8")[:1]
    forged_span = catalog_module._SourceSpan(
        start=0,
        end=1,
        digest=hashlib.sha256(forged_fragment).hexdigest(),
    )
    monkeypatch.setattr(
        catalog_module,
        "_cached_element_span",
        lambda *_args, **_kwargs: forged_span,
    )

    result = build_selection_atom_catalog_from_document_v1(
        document,
        config=_enabled(),
    )

    assert not result.accepted
    assert result.reason == "selection_span_does_not_contain_text"
    assert result.atoms == ()


def test_utf8_and_entity_raw_byte_spans_are_exact() -> None:
    html = "<main><p>前缀</p><p>éclair</p></main>"
    result = build_selection_atom_catalog_v1(html, config=_enabled())

    assert result.accepted
    source = html.encode("utf-8")
    fragments = [source[atom.source_start : atom.source_end] for atom in result.atoms]
    assert fragments == ["前缀".encode(), "éclair".encode()]
    assert [atom.source_bytes for atom in result.atoms] == [6, 7]

    entity = build_selection_atom_catalog_v1(
        "<main><p>before &amp; after</p></main>",
        config=_enabled(),
    )
    assert entity.accepted
    [entity_atom] = entity.atoms
    entity_source = b"<main><p>before &amp; after</p></main>"
    assert entity_source[entity_atom.source_start : entity_atom.source_end] == b"before &amp; after"
    assert entity_atom.source_bytes == len(b"before &amp; after")
    assert entity.source_text_map_schema_version == "ordered-source-text-map.v2"
    assert entity.source_text_map_reason == "accepted"
    assert entity.source_text_map_transformed_span_count == 1
    assert entity.text_mapping_contract == "ordered-source-text-map.v2"


def test_lexical_atoms_share_typed_closure_ids_without_overlapping() -> None:
    html = """\
<main>
<table><tr><td>alpha <strong>beta</strong> gamma</td></tr></table>
<ol><li>one <em>two</em></li></ol>
<math><mi>x</mi><mo>+</mo><mi>y</mi></math>
</main>
"""
    result = build_selection_atom_catalog_v1(html, config=_enabled())

    assert result.accepted
    grouped = {
        kind: [atom for atom in result.atoms if atom.kind == kind]
        for kind in ("table_cell", "list_item", "math")
    }
    assert [len(grouped[kind]) for kind in grouped] == [3, 2, 3]
    for atoms in grouped.values():
        assert len({atom.selection_id for atom in atoms}) == 1
        assert all(
            left.source_end <= right.source_start
            for left, right in zip(atoms, atoms[1:], strict=False)
        )
    assert len({atom.table_cell_id for atom in grouped["table_cell"]}) == 1
    assert len({atom.list_item_id for atom in grouped["list_item"]}) == 1
    assert len({atom.math_id for atom in grouped["math"]}) == 1


def test_catalog_rejects_partial_mapping_and_reports_ir_truncation() -> None:
    repaired = build_selection_atom_catalog_v1(
        "<ul><li>one<li>two</ul>",
        config=_enabled(),
    )
    assert not repaired.accepted
    assert repaired.reason == "unreliable_text_mapping"
    assert repaired.atoms == ()

    html = "<main><p>" + ("content " * 100) + "</p></main>"
    document = extract_document_ir_v2(
        html,
        limits=DocumentIRV2Limits(max_input_bytes=128),
    )
    truncated = build_selection_atom_catalog_from_document_v1(
        document,
        config=_enabled(),
    )
    assert not truncated.accepted
    assert truncated.reason == "incomplete_source"
    assert truncated.ir_truncated
    assert "input_bytes" in truncated.ir_truncation_reasons
    assert truncated.atoms == ()


def test_ordered_mapper_certifies_repeated_sibling_text_without_guessing() -> None:
    html = "<main><p>same &amp; value<em>x</em>same &amp; value</p></main>"
    document = extract_document_ir_v2(html)
    repeated_runs = [run for run in document.text_runs if run.text == "same & value"]
    assert len(repeated_runs) == 2
    assert all(not run.source_span_reliable for run in repeated_runs)

    result = build_selection_atom_catalog_from_document_v1(
        document,
        config=_enabled(),
    )

    assert result.accepted
    repeated_atoms = [
        atom for atom in result.atoms if atom.text_run_id in {run.id for run in repeated_runs}
    ]
    assert len(repeated_atoms) == 2
    source = html.encode()
    assert [source[atom.source_start : atom.source_end] for atom in repeated_atoms] == [
        b"same &amp; value",
        b"same &amp; value",
    ]
    assert repeated_atoms[0].source_end < repeated_atoms[1].source_start
    assert result.source_text_map_transformed_span_count == 2


@pytest.mark.parametrize(
    "html",
    [
        "<main><p>one<div>two</div></p></main>",
        "<table>fostered<tr><td>inside</td></tr></table>",
        "<ul><li>one<li>two</ul>",
        "<main><b><i>crossed</b></i></main>",
    ],
)
def test_catalog_fails_closed_for_optional_end_and_parser_repairs(html: str) -> None:
    result = build_selection_atom_catalog_v1(html, config=_enabled())

    assert not result.accepted
    assert result.reason in {"incomplete_source_mapping", "unreliable_text_mapping"}
    assert result.atoms == ()
    assert result.catalog_digest == ""


@pytest.mark.parametrize(
    ("html", "changes", "reason"),
    [
        ("<p>content</p>", {"max_source_bytes": 8}, "source_byte_budget"),
        (
            "<main><p>one</p><p>two</p></main>",
            {"max_atoms": 1},
            "atom_budget",
        ),
        (
            "<main><p>alpha</p><p>beta</p></main>",
            {"max_total_atom_source_bytes": 5},
            "atom_source_byte_budget",
        ),
        (
            "<main><section><p>deep</p></section></main>",
            {"max_ancestry_steps": 1},
            "ancestry_budget",
        ),
        ("<p>content</p>", {"max_identifier_chars": 1}, "identifier_budget"),
    ],
)
def test_catalog_budgets_fail_closed_without_partial_atoms(
    html: str,
    changes: dict[str, int | bool],
    reason: str,
) -> None:
    result = build_selection_atom_catalog_v1(html, config=_enabled(**changes))

    assert not result.accepted
    assert result.reason == reason
    assert result.atoms == ()
    assert result.catalog_digest == ""


@pytest.mark.parametrize(
    ("html", "limits", "truncation_reason", "catalog_reason"),
    [
        (
            "<main><p>" + ("content " * 20) + "</p></main>",
            DocumentIRV2Limits(max_input_bytes=64),
            "input_bytes",
            "incomplete_source",
        ),
        (
            "<main><p>" + ("x" * 50) + "</p></main>",
            DocumentIRV2Limits(max_text_run_bytes=4),
            "text_bytes",
            "truncated_ir",
        ),
        (
            "<main><section><p>x</p></section></main>",
            DocumentIRV2Limits(max_depth=1),
            "dom_depth",
            "truncated_ir",
        ),
        (
            '<table><tr><td colspan="5">x</td></tr></table>',
            DocumentIRV2Limits(max_table_columns=2),
            "table_columns",
            "truncated_ir",
        ),
        (
            "<main><math><mi>first</mi></math><math><mi>second</mi></math></main>",
            DocumentIRV2Limits(max_math_bytes=24),
            "math_markup",
            "truncated_ir",
        ),
    ],
)
def test_every_native_truncation_family_is_rejected_with_provenance(
    html: str,
    limits: DocumentIRV2Limits,
    truncation_reason: str,
    catalog_reason: str,
) -> None:
    document = extract_document_ir_v2(html, limits=limits)
    result = build_selection_atom_catalog_from_document_v1(
        document,
        config=_enabled(),
    )

    assert not result.accepted
    assert result.reason == catalog_reason
    assert result.ir_truncated
    assert truncation_reason in result.ir_truncation_reasons
    assert result.atoms == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_source_bytes", 0),
        ("max_atoms", 0),
        ("max_total_atom_source_bytes", 0),
        ("max_ancestry_steps", 10_000_001),
        ("max_identifier_chars", 4_097),
    ],
)
def test_catalog_rejects_invalid_limits(field: str, value: int) -> None:
    values: dict[str, int | bool] = {
        "enabled": True,
        "max_source_bytes": 4 * 1024 * 1024,
        "max_atoms": 65_536,
        "max_total_atom_source_bytes": 4 * 1024 * 1024,
        "max_ancestry_steps": 2_000_000,
        "max_identifier_chars": 1_024,
    }
    values[field] = value
    with pytest.raises(ValueError):
        SelectionAtomCatalogV1Config(**values)  # type: ignore[arg-type]


def test_generated_well_formed_documents_are_stable_ordered_and_disjoint() -> None:
    rng = random.Random(0xC1A5)
    words = ("alpha", "beta", "gamma", "éclair", "中文", "delta")
    config = _enabled()

    for case_index in range(32):
        chosen = [rng.choice(words) for _ in range(8)]
        body = (
            f"<p>{chosen[0]} {chosen[1]}</p>"
            f"<pre><code>{chosen[2]}\\n  {chosen[3]}</code></pre>"
            f"<table><tr><td>{chosen[4]}</td><td>{chosen[5]}</td></tr></table>"
            f"<ol><li>{chosen[6]}</li></ol>"
            f"<math><mi>{chosen[7]}</mi></math>"
        )
        html = (
            f'<!doctype html><html><body><main data-case="{case_index}">{body}</main></body></html>'
        )
        first = build_selection_atom_catalog_v1(html, config=config)
        second = build_selection_atom_catalog_v1(html, config=config)

        assert first.accepted, (case_index, first.reason)
        assert first.catalog_digest == second.catalog_digest
        assert first.atoms == second.atoms
        assert len({atom.id for atom in first.atoms}) == first.atom_count
        assert all(
            left.source_end <= right.source_start
            for left, right in zip(first.atoms, first.atoms[1:], strict=False)
        )

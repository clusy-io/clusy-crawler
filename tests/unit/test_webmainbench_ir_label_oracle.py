from __future__ import annotations

import re
from collections import Counter
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from lxml import html as lxml_html

from bench.webmainbench_ir_label_oracle import (
    DIAGNOSTIC_SCHEMA_VERSION,
    LabelOracleError,
    OracleAggregate,
    OracleRecord,
    _fresh_wrapper_canonicalizer,
    analyze_record,
    assert_benchmark_only_isolation,
    build_label_profile,
    deterministic_indices,
    oracle_selected_units,
    parse_args,
    validate_args,
)

if TYPE_CHECKING:
    from pathlib import Path


def _canonicalize(value: str, url: str) -> str:
    del url
    if not value:
        return ""
    return " ".join(lxml_html.fromstring(value).text_content().split())


def _token_score(reference: str, prediction: str) -> dict[str, float]:
    reference_tokens = re.findall(r"\w+", reference.lower())
    prediction_tokens = re.findall(r"\w+", prediction.lower())
    overlap = sum(
        (Counter(reference_tokens) & Counter(prediction_tokens)).values()
    )
    precision = overlap / len(prediction_tokens) if prediction_tokens else 0.0
    recall = overlap / len(reference_tokens) if reference_tokens else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    return {"precision": precision, "recall": recall, "f1": f1}


def _record(
    html: str,
    main_html: str,
    *,
    reference: str = "target",
    table: str = "without",
) -> OracleRecord:
    return OracleRecord(
        dataset_index=7,
        track_id="synthetic-track",
        url="https://example.test/article",
        html=html,
        main_html=main_html,
        reference=reference,
        metadata={
            "level": "mid",
            "language": "en",
            "style": "Article",
            "table": table,
            "code": "without",
            "equation": "without",
        },
    )


def test_label_profile_requires_real_ground_truth_markers() -> None:
    with pytest.raises(LabelOracleError, match="missing required cc-select"):
        build_label_profile(
            '<p data-anno-uid="p">target</p>',
            context="hostile fixture",
            require_ground_truth_marker=True,
        )

    profile = build_label_profile(
        (
            '<p data-anno-uid="p">noise '
            '<marked-text cc-select="true" data-anno-uid="m">target</marked-text>'
            "</p>"
        ),
        context="labelled fixture",
        require_ground_truth_marker=True,
    )

    assert profile.marker_count == 1
    assert profile.uid_to_marker_ids["p"] == frozenset({0})
    assert profile.uid_to_marker_ids["m"] == frozenset({0})
    assert "p" in profile.marker_context_tags[0]
    assert "m" in profile.fully_selected_uids
    assert profile.selected_non_whitespace_chars == len("target")


def test_mixed_atomic_block_quantifies_unavoidable_noise() -> None:
    source = (
        '<html data-anno-uid="html"><body data-anno-uid="body">'
        '<p data-anno-uid="p">before '
        '<marked-text cc-select="true" data-anno-uid="marker">target</marked-text>'
        " after</p></body></html>"
    )
    ground_truth = (
        '<html data-anno-uid="html"><body data-anno-uid="body">'
        '<p data-anno-uid="p">'
        '<marked-text cc-select="true" data-anno-uid="marker">target</marked-text>'
        "</p></body></html>"
    )

    page = analyze_record(
        _record(source, ground_truth),
        canonicalize=_canonicalize,
        score=_token_score,
    )

    assert page["label_oracle"] is True
    assert page["claimable"] is False
    assert page["features"]["mixed_selected_block"] is True
    assert page["diagnostics"]["selected_blocks"] == 1
    assert page["diagnostics"]["mixed_selected_blocks"] == 1
    assert page["diagnostics"]["selected_noise_non_whitespace_chars"] == len(
        "beforeafter"
    )
    assert page["diagnostics"]["emitted_label_markers"] == 1
    assert page["score"]["recall"] == 1.0
    assert page["score"]["precision"] < 1.0


def test_direct_table_text_marker_exposes_structural_ceiling() -> None:
    source = (
        '<html data-anno-uid="html"><body data-anno-uid="body">'
        '<table data-anno-uid="table"><tr data-anno-uid="row">'
        '<td data-anno-uid="cell">noise '
        '<marked-text cc-select="true" data-anno-uid="marker">target</marked-text>'
        ' <a data-anno-uid="link" href="/more">other link</a>'
        "</td></tr></table></body></html>"
    )
    ground_truth = (
        '<html data-anno-uid="html"><body data-anno-uid="body">'
        '<table data-anno-uid="table"><tr data-anno-uid="row">'
        '<td data-anno-uid="cell">'
        '<marked-text cc-select="true" data-anno-uid="marker">target</marked-text>'
        "</td></tr></table></body></html>"
    )

    page = analyze_record(
        _record(source, ground_truth, table="with"),
        canonicalize=_canonicalize,
        score=_token_score,
    )

    assert page["features"]["ground_truth_table_markup"] is True
    assert page["features"]["zero_selected_blocks"] is True
    assert page["features"]["unrepresented_label_marker"] is True
    assert page["features"]["unselectable_label_marker"] is True
    assert page["features"]["unrepresented_table_or_list_marker"] is True
    assert page["features"]["incomplete_label_char_coverage"] is True
    assert page["diagnostics"]["selected_blocks"] == 0
    assert page["diagnostics"]["emitted_label_markers"] == 0
    assert page["score"]["f1"] == 0.0


def test_bounded_table_anchor_is_selected_with_minimum_skeleton() -> None:
    source = (
        '<html data-anno-uid="html"><body data-anno-uid="body">'
        '<table data-anno-uid="table"><tr data-anno-uid="row">'
        '<td data-anno-uid="cell">noise '
        '<a data-anno-uid="link" href="/target">'
        '<marked-text cc-select="true" data-anno-uid="marker">target</marked-text>'
        "</a> other</td></tr></table></body></html>"
    )
    ground_truth = (
        '<html data-anno-uid="html"><body data-anno-uid="body">'
        '<table data-anno-uid="table"><tr data-anno-uid="row">'
        '<td data-anno-uid="cell"><a data-anno-uid="link" href="/target">'
        '<marked-text cc-select="true" data-anno-uid="marker">target</marked-text>'
        "</a></td></tr></table></body></html>"
    )

    page = analyze_record(
        _record(source, ground_truth, table="with"),
        canonicalize=_canonicalize,
        score=_token_score,
    )

    assert page["features"]["selected_table_unit"] is True
    assert page["features"]["zero_selected_blocks"] is False
    assert page["features"]["unrepresented_label_marker"] is False
    assert page["features"]["unselectable_label_marker"] is False
    assert page["features"]["unrepresented_table_or_list_marker"] is False
    assert page["diagnostics"]["selected_tag_counts"] == {"a": 1}
    assert page["diagnostics"]["selected_noise_non_whitespace_chars"] == 0
    assert page["score"] == {"precision": 1.0, "recall": 1.0, "f1": 1.0}


def test_overlapping_selectable_ancestor_prefers_marker_complete_leaf() -> None:
    source = (
        '<table data-anno-uid="table"><tr data-anno-uid="row">'
        '<td data-anno-uid="cell"><center data-anno-uid="center">'
        '<marked-text cc-select="true" data-anno-uid="marker">target</marked-text>'
        "</center></td></tr></table>"
    )

    page = analyze_record(
        _record(source, source, table="with"),
        canonicalize=_canonicalize,
        score=_token_score,
    )

    assert page["features"]["overlapping_selectable_units"] is True
    assert page["diagnostics"]["raw_intersecting_selectable_blocks"] == 2
    assert page["diagnostics"]["selected_blocks"] == 1
    assert page["diagnostics"]["dropped_overlap_candidates"] == 1
    assert page["diagnostics"]["selected_tag_counts"] == {"center": 1}
    assert page["diagnostics"]["emitted_label_markers"] == 1
    assert page["score"] == {"precision": 1.0, "recall": 1.0, "f1": 1.0}


def test_overlap_keeps_ancestor_when_leaf_would_drop_labelled_text() -> None:
    source = (
        '<table data-anno-uid="table"><tr data-anno-uid="row">'
        '<td data-anno-uid="cell">'
        '<marked-text cc-select="true" data-anno-uid="marker">'
        'before<center data-anno-uid="center">target</center>'
        "</marked-text></td></tr></table>"
    )

    page = analyze_record(
        _record(source, source, reference="beforetarget", table="with"),
        canonicalize=_canonicalize,
        score=_token_score,
    )

    assert page["features"]["overlapping_selectable_units"] is True
    assert page["diagnostics"]["raw_intersecting_selectable_blocks"] == 2
    assert page["diagnostics"]["selected_blocks"] == 1
    assert page["diagnostics"]["selected_tag_counts"] == {"td": 1}
    assert page["diagnostics"]["emitted_label_non_whitespace_chars"] == len(
        "beforetarget"
    )
    assert page["score"] == {"precision": 1.0, "recall": 1.0, "f1": 1.0}


def test_duplicate_annotation_uids_are_isolated_and_reported() -> None:
    html = (
        '<div data-anno-uid="same">'
        '<marked-text cc-select="true" data-anno-uid="marker">target</marked-text>'
        '<p data-anno-uid="same">duplicate</p>'
        "</div>"
    )

    profile = build_label_profile(
        html,
        context="duplicate fixture",
        require_ground_truth_marker=True,
    )

    assert profile.duplicate_uids == ("same",)
    assert "same" not in profile.uid_to_marker_ids

    page = analyze_record(
        _record(html, html),
        canonicalize=_canonicalize,
        score=_token_score,
    )
    assert page["features"]["label_alignment_ambiguous"] is True
    assert page["diagnostics"]["source_duplicate_annotation_uids"] == 1


def test_ir_declared_missing_selectable_html_is_measured_not_parsed() -> None:
    source = (
        '<p data-anno-uid="p">'
        '<marked-text cc-select="true" data-anno-uid="marker">target</marked-text>'
        "</p>"
    )
    profile = build_label_profile(
        source,
        context="source",
        require_ground_truth_marker=True,
    )
    block = SimpleNamespace(
        id="block-000000",
        order=0,
        parent_id=None,
        tag="p",
        selectable=True,
        outer_html="",
        html_truncated=True,
    )
    document = SimpleNamespace(blocks=[block], truncated=True)

    selection = oracle_selected_units(document, profile)

    assert selection.candidates == ()
    assert selection.selected == ()
    assert selection.missing_source_html_blocks == 1
    assert selection.unparseable_source_html_blocks == 0


def test_diagnostic_is_deterministic_and_aggregate_keeps_label_warning() -> None:
    source = (
        '<p data-anno-uid="p">'
        '<marked-text cc-select="true" data-anno-uid="marker">target</marked-text>'
        "</p>"
    )
    record = _record(source, source)

    first = analyze_record(
        record,
        canonicalize=_canonicalize,
        score=_token_score,
    )
    second = analyze_record(
        record,
        canonicalize=_canonicalize,
        score=_token_score,
    )
    aggregate = OracleAggregate()
    aggregate.add(first)
    exported = aggregate.export()

    assert first == second
    assert first["schema_version"] == DIAGNOSTIC_SCHEMA_VERSION
    assert exported["score_ceiling"]["f1"] == 1.0
    assert exported["score_ceiling"]["pages"] == 1
    assert exported["ground_truth_recanonicalization"]["pages"] == 1
    assert exported["coverage"]["emitted_marker_recall"] == 1.0


def test_hash_ranked_sample_is_stable_unique_and_corpus_ordered() -> None:
    first = deterministic_indices(128, "fixed-seed")
    second = deterministic_indices(128, "fixed-seed")

    assert first == second
    assert len(first) == len(set(first)) == 128
    assert first == tuple(sorted(first))
    assert deterministic_indices(None, "ignored")[:3] == (0, 1, 2)


def test_official_canonicalizer_wrapper_state_never_leaks_between_calls() -> None:
    class StatefulWrapper:
        def __init__(self) -> None:
            self.used = False

        def __call__(self, html: str, url: str) -> str:
            del url
            if self.used:
                raise AssertionError("wrapper instance was reused")
            self.used = True
            return html

    canonicalize = _fresh_wrapper_canonicalizer(StatefulWrapper)

    assert canonicalize("first", "") == "first"
    assert canonicalize("second", "") == "second"


def test_acknowledgement_is_mandatory() -> None:
    args = parse_args(["--sample-size", "1"])
    with pytest.raises(LabelOracleError, match="acknowledge-label-oracle"):
        validate_args(args)

    validate_args(
        SimpleNamespace(
            acknowledge_label_oracle_not_claimable=True,
            sample_size=1,
            sample_seed="seed",
            progress_every=1,
        )
    )


def test_production_app_cannot_import_benchmark_modules(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "safe.py").write_text("VALUE = 1\n", encoding="utf-8")

    assert assert_benchmark_only_isolation(tmp_path)["passed"] is True

    (app / "unsafe.py").write_text(
        "from bench.webmainbench_ir_label_oracle import analyze_record\n",
        encoding="utf-8",
    )
    with pytest.raises(LabelOracleError, match="imports benchmark code"):
        assert_benchmark_only_isolation(tmp_path)


def test_current_production_tree_does_not_import_benchmark_code() -> None:
    assert assert_benchmark_only_isolation()["passed"] is True

from __future__ import annotations

import json
from dataclasses import asdict, replace
from typing import Any, cast

import pytest

from app.services.independent_lineage_text_medoid_v1 import (
    INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_GRAM_WIDTH,
    INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_MINIMUM_SUPPORT_PPM,
    INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_PROTOCOL_REVISION,
    INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_SIMILARITY,
    CandidateRoleV1,
    IndependentLineageCandidateV1,
    IndependentLineageTextMedoidV1Config,
    IndependentLineageTextMedoidV1Error,
    build_terminal_strategy_role_receipt_v1,
    select_independent_lineage_text_medoid_v1,
    verify_terminal_strategy_role_receipt_v1,
)


def _candidate(
    candidate_role: CandidateRoleV1,
    terminal_strategy: str,
    text: str,
) -> IndependentLineageCandidateV1:
    exact_text = str.__getitem__(text, slice(None))
    return IndependentLineageCandidateV1(
        text=exact_text,
        terminal_receipt=build_terminal_strategy_role_receipt_v1(
            text=exact_text,
            candidate_role=candidate_role,
            terminal_strategy=terminal_strategy,
        ),
    )


def _enabled(**changes: int | bool) -> IndependentLineageTextMedoidV1Config:
    return replace(
        IndependentLineageTextMedoidV1Config(enabled=True),
        **cast("Any", changes),
    )


def test_disabled_mode_inspects_nothing_and_returns_exact_fallback() -> None:
    class Hostile:
        def __getattribute__(self, _name: str) -> object:
            raise AssertionError("disabled selector inspected candidate input")

        def __iter__(self) -> object:
            raise AssertionError("disabled selector iterated candidate input")

    class Fallback(str):
        pass

    fallback = Fallback("exact fallback object")
    result = select_independent_lineage_text_medoid_v1(
        fallback,
        cast("Any", Hostile()),
    )

    assert not result.accepted
    assert result.reason == "disabled"
    assert result.output is fallback
    assert result.receipt.protocol_revision == (
        INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_PROTOCOL_REVISION
    )
    assert result.receipt.similarity == INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_SIMILARITY
    assert result.receipt.gram_width == INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_GRAM_WIDTH
    assert len(result.receipt.receipt_sha256) == 64


def test_terminal_receipt_binds_text_and_rejects_caller_chosen_role() -> None:
    text = "A terminal receipt binds this exact candidate text."
    receipt = build_terminal_strategy_role_receipt_v1(
        text=text,
        candidate_role="production_balanced",
        terminal_strategy="rs-trafilatura",
    )

    assert verify_terminal_strategy_role_receipt_v1(receipt, text=text)
    assert not verify_terminal_strategy_role_receipt_v1(receipt, text=text + " changed")
    assert not verify_terminal_strategy_role_receipt_v1(
        replace(receipt, text_sha256="0" * 64),
        text=text,
    )
    assert not hasattr(receipt, "selection_lineage")
    assert receipt.digest_is_authentication is False

    with pytest.raises(IndependentLineageTextMedoidV1Error, match="closed v1 policy"):
        build_terminal_strategy_role_receipt_v1(
            text=text,
            candidate_role="readability",
            terminal_strategy="rs-trafilatura",
        )


def test_selects_character_5gram_medoid_with_two_other_lineages() -> None:
    fallback = "alpha beta gamma"
    readability = "alpha beta gamma delta"
    semantic = "alpha beta gamma delta epsilon"
    candidates = (
        _candidate("production_balanced", "rs-trafilatura", fallback),
        _candidate("readability", "readability", readability),
        _candidate("semantic_main", "semantic_main", semantic),
    )

    result = select_independent_lineage_text_medoid_v1(
        fallback,
        candidates,
        config=_enabled(),
    )

    assert result.accepted
    assert result.output == readability
    assert result.receipt.selected_terminal_strategy == "readability"
    assert result.receipt.selected_selection_lineage == "readability"
    assert result.receipt.minimum_other_lineages == 2
    assert result.receipt.minimum_support_ppm == 100_000
    assert {support.selection_lineage for support in result.receipt.selected_supports} == {
        "dom_rendered_views",
        "trafilatura_derived",
    }
    assert all(support.passes_minimum_support for support in result.receipt.selected_supports)
    assert result.receipt.selected_minimum_support_ppm >= 100_000


def test_production_variants_share_one_lineage_and_cannot_form_quorum() -> None:
    class Fallback(str):
        pass

    fallback = Fallback("shared production extraction candidate")
    candidates = (
        _candidate("production_balanced", "rs-trafilatura", fallback),
        _candidate("production_article_body", "rs-trafilatura", fallback + " article"),
        _candidate("python_trafilatura", "trafilatura", fallback + " python"),
    )

    result = select_independent_lineage_text_medoid_v1(
        fallback,
        candidates,
        config=_enabled(),
    )

    assert not result.accepted
    assert result.reason == "insufficient_independent_lineage_support"
    assert result.output is fallback
    assert result.receipt.selection_lineage_count == 1
    assert result.receipt.selected_selection_lineage == "trafilatura_derived"


def test_production_role_uses_its_actual_terminal_strategy_for_lineage() -> None:
    fallback = "readability produced this production balanced output"
    candidates = (
        _candidate("production_balanced", "readability", fallback),
        _candidate("readability", "readability", fallback + " standalone"),
        _candidate("markdownify", "markdownify", fallback + " dom"),
    )

    result = select_independent_lineage_text_medoid_v1(
        fallback,
        candidates,
        config=_enabled(),
    )

    assert not result.accepted
    assert result.reason == "insufficient_independent_lineage_support"
    assert result.output == fallback
    assert result.receipt.selection_lineage_count == 2
    assert result.receipt.selected_selection_lineage == "readability"


@pytest.mark.parametrize(
    ("terminal_strategy", "selection_lineage"),
    [
        ("rs-trafilatura", "trafilatura_derived"),
        ("trafilatura", "trafilatura_derived"),
        ("readability", "readability"),
        ("markdownify", "dom_rendered_views"),
        ("raw_lxml", "dom_rendered_views"),
        ("documentation", "dom_rendered_views"),
        ("github-source", "dom_rendered_views"),
    ],
)
def test_actual_production_terminal_strategy_drives_fallback_lineage(
    terminal_strategy: str,
    selection_lineage: str,
) -> None:
    fallback = f"production output from {terminal_strategy}"
    candidate = _candidate("production_balanced", terminal_strategy, fallback)

    result = select_independent_lineage_text_medoid_v1(
        fallback,
        (candidate,),
        config=_enabled(),
    )

    assert not result.accepted
    assert result.receipt.selected_candidate_role == "production_balanced"
    assert result.receipt.selected_terminal_strategy == terminal_strategy
    assert result.receipt.selected_selection_lineage == selection_lineage


def test_unknown_or_mixed_terminal_strategy_has_no_independence_vote() -> None:
    with pytest.raises(IndependentLineageTextMedoidV1Error, match="closed v1 policy"):
        build_terminal_strategy_role_receipt_v1(
            text="mixed terminal output",
            candidate_role="production_balanced",
            terminal_strategy="union(trafilatura+readability)",
        )


def test_identical_texts_are_deduplicated_but_keep_independent_provenance() -> None:
    text = "identical source backed text shared by three terminal lineages"
    candidates = (
        _candidate("production_balanced", "rs-trafilatura", text),
        _candidate("readability", "readability", text),
        _candidate("markdownify", "markdownify", text),
    )

    result = select_independent_lineage_text_medoid_v1(
        text,
        candidates,
        config=_enabled(),
    )

    assert result.accepted
    assert result.output == text
    assert result.receipt.candidate_count == 3
    assert result.receipt.unique_text_count == 1
    assert result.receipt.duplicate_candidate_count == 2
    assert result.receipt.selectable_unique_text_count == 1
    assert result.receipt.selected_candidate_role == "production_balanced"
    assert result.receipt.selected_terminal_strategy == "rs-trafilatura"
    assert [support.support_ppm for support in result.receipt.selected_supports] == [
        1_000_000,
        1_000_000,
    ]


def test_support_only_dom_view_can_attest_but_cannot_be_selected() -> None:
    fallback = "abcdefghij"
    candidates = (
        _candidate("production_balanced", "rs-trafilatura", fallback),
        _candidate("readability", "readability", "abcdefghijXYZ"),
        _candidate("markdownify", "markdownify", "abcdefghijUVW"),
    )

    result = select_independent_lineage_text_medoid_v1(
        fallback,
        candidates,
        config=_enabled(),
    )

    assert result.accepted
    assert result.receipt.selected_candidate_role != "markdownify"
    assert result.receipt.selectable_unique_text_count == 2
    assert "dom_rendered_views" in {
        support.selection_lineage for support in result.receipt.selected_supports
    }


def test_zero_and_below_floor_support_fail_closed_to_exact_fallback() -> None:
    class Fallback(str):
        pass

    fallback = Fallback("ABCDE" + "x" * 100)
    candidates = (
        _candidate("production_balanced", "rs-trafilatura", fallback),
        _candidate("readability", "readability", "ABCDE" + "y" * 100),
        _candidate("semantic_main", "semantic_main", "ABCDE" + "z" * 100),
    )

    result = select_independent_lineage_text_medoid_v1(
        fallback,
        candidates,
        config=_enabled(),
    )

    assert not result.accepted
    assert result.reason == "insufficient_independent_lineage_support"
    assert result.output is fallback
    assert result.receipt.eligible_unique_text_count == 0
    assert result.receipt.minimum_support_ppm == (
        INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_MINIMUM_SUPPORT_PPM
    )


def test_support_floor_can_only_be_raised_and_is_bound_into_receipt() -> None:
    fallback = "alpha beta gamma"
    candidates = (
        _candidate("production_balanced", "rs-trafilatura", fallback),
        _candidate("readability", "readability", "alpha beta gamma delta"),
        _candidate(
            "semantic_main",
            "semantic_main",
            "alpha beta gamma delta epsilon",
        ),
    )
    default_result = select_independent_lineage_text_medoid_v1(
        fallback,
        candidates,
        config=_enabled(),
    )
    strict_result = select_independent_lineage_text_medoid_v1(
        fallback,
        candidates,
        config=_enabled(minimum_support_ppm=900_000),
    )

    assert default_result.accepted
    assert not strict_result.accepted
    assert strict_result.receipt.minimum_support_ppm == 900_000
    assert strict_result.receipt.config_sha256 != default_result.receipt.config_sha256
    assert strict_result.receipt.receipt_sha256 != default_result.receipt.receipt_sha256

    with pytest.raises(ValueError, match="safety floor"):
        IndependentLineageTextMedoidV1Config(minimum_support_ppm=99_999)


def test_ties_and_input_permutations_are_deterministic() -> None:
    fallback = "AAAAABBBBB"
    candidates = (
        _candidate("production_balanced", "rs-trafilatura", fallback),
        _candidate("readability", "readability", "AAAAACCCCC"),
        _candidate("semantic_main", "semantic_main", "AAAAADDDDD"),
    )
    config = _enabled()

    forward = select_independent_lineage_text_medoid_v1(
        fallback,
        candidates,
        config=config,
    )
    reverse = select_independent_lineage_text_medoid_v1(
        fallback,
        tuple(reversed(candidates)),
        config=config,
    )

    assert forward.accepted
    assert forward.receipt.selected_candidate_role == "production_balanced"
    assert forward.receipt.selected_terminal_strategy == "rs-trafilatura"
    assert reverse.output == forward.output
    assert reverse.receipt == forward.receipt


def test_tampered_receipt_and_resource_budgets_fail_closed() -> None:
    class Fallback(str):
        pass

    fallback = Fallback("abcdefghij")
    balanced = _candidate("production_balanced", "rs-trafilatura", fallback)
    tampered = IndependentLineageCandidateV1(
        text=balanced.text,
        terminal_receipt=replace(
            balanced.terminal_receipt,
            receipt_sha256="0" * 64,
        ),
    )
    invalid = select_independent_lineage_text_medoid_v1(
        fallback,
        (tampered,),
        config=_enabled(),
    )

    assert invalid.reason == "invalid_terminal_receipt"
    assert invalid.output is fallback

    bounded = select_independent_lineage_text_medoid_v1(
        fallback,
        (
            balanced,
            _candidate("readability", "readability", "abcdefghijk"),
        ),
        config=_enabled(max_candidate_bytes=10, max_total_candidate_bytes=20),
    )
    assert bounded.reason == "candidate_byte_budget"
    assert bounded.output is fallback

    work_bounded = select_independent_lineage_text_medoid_v1(
        fallback,
        (
            balanced,
            _candidate("readability", "readability", "abcdefghijY"),
            _candidate("semantic_main", "semantic_main", "abcdefghijZ"),
        ),
        config=_enabled(max_work=1),
    )
    assert work_bounded.reason == "work_budget"
    assert work_bounded.output is fallback


def test_decision_receipt_is_bounded_and_contains_no_candidate_text() -> None:
    fallback = "private candidate phrase 7f13c4e0d89a"
    candidates = (
        _candidate("production_balanced", "rs-trafilatura", fallback),
        _candidate("readability", "readability", fallback + " one"),
        _candidate("semantic_main", "semantic_main", fallback + " two"),
    )
    config = _enabled()

    result = select_independent_lineage_text_medoid_v1(
        fallback,
        candidates,
        config=config,
    )
    encoded = json.dumps(asdict(result.receipt), sort_keys=True).encode()

    assert result.accepted
    assert fallback.encode() not in encoded
    assert len(encoded) <= config.max_receipt_bytes
    assert result.receipt.digest_is_authentication is False

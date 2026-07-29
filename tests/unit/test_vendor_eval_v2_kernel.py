from __future__ import annotations

import hashlib
import json
import math
import operator
import os
import platform
import random
import runpy
import subprocess
import sys
import unicodedata
from collections.abc import Iterator, MutableMapping, Sequence
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, TypedDict, cast, overload

import pytest

import bench.vendor_eval_v2.kernel as kernel
from bench.vendor_eval_v2 import (
    ARTIFACT_STATUS,
    CLAIM_BOOTSTRAP_SAMPLES,
    CLAIM_BOOTSTRAP_SEED,
    CLAIM_MANIFEST,
    CLAIM_MANIFEST_SHA256,
    DRIFT_CANARY_MANIFEST,
    DRIFT_CANARY_MANIFEST_SHA256,
    INTERNAL_SKELETON_PROFILE,
    METRIC_REGISTRY,
    POSITION_BUCKETS,
    PROTOCOL_FAMILY,
    PROTOCOL_MANIFEST,
    PROTOCOL_MANIFEST_SHA256,
    PROTOCOL_VERSION,
    RUNTIME_BUILD_SHA256,
    SCORER_SOURCE_SHA256,
    SYNTHETIC_FIXTURE_SCHEMA,
    SYNTHETIC_FIXTURE_SHA256,
    SYNTHETIC_FIXTURE_SOURCE_SHA256,
    UNICODE_DATA_VERSION,
    UNICODE_RUNTIME_PROFILE,
    UTS39_CONFUSABLES_SOURCE_SHA256,
    UTS39_CONFUSABLES_VERSION,
    BootstrapResult,
    BudgetExceeded,
    ExampleScore,
    KernelError,
    KernelLimits,
    PairedObservation,
    best_window_f1,
    canonical_json_bytes,
    canonical_sha256,
    character_ngrams,
    character_ngrams_sequence,
    claim_manifest_document,
    confusable_internal_skeleton_diagnostic,
    drift_canary_manifest_document,
    normalize_visible_text,
    ordered_output_f1,
    paired_bootstrap,
    positional_output_f1,
    protocol_manifest_document,
    score_example,
    score_verified_fixture,
    whole_output_f1,
)

FIXTURES_PATH = Path(__file__).parents[2] / "bench" / "vendor_eval_v2" / "synthetic_fixtures.json"
EXTERNAL_VERIFIER_PATH = FIXTURES_PATH.with_name("external_verifier.py")
EXTERNAL_TRUST_ROOT_PATH = FIXTURES_PATH.with_name("external_trust_root.json")


class Fixture(TypedDict):
    candidate: str
    id: str
    lies: list[str]
    truth: str


def _fixtures() -> dict[str, Fixture]:
    document = cast(
        "dict[str, Any]",
        json.loads(FIXTURES_PATH.read_text(encoding="utf-8")),
    )
    assert document["fixture_schema"] == SYNTHETIC_FIXTURE_SCHEMA
    assert canonical_sha256(document) == SYNTHETIC_FIXTURE_SHA256
    fixtures = cast("list[Fixture]", document["fixtures"])
    return {fixture["id"]: fixture for fixture in fixtures}


def _score(fixture: Fixture) -> ExampleScore:
    verified = score_verified_fixture(fixture["id"])
    assert verified.fixture_id == fixture["id"]
    return verified.score


def test_synthetic_fixture_relations_cover_core_failure_modes() -> None:
    fixtures = _fixtures()
    exact = _score(fixtures["exact"])
    partial = _score(fixtures["partial"])
    reordered = _score(fixtures["reordered"])
    repeated = _score(fixtures["repeated"])
    boilerplate = _score(fixtures["boilerplate"])
    raw_style = _score(fixtures["raw-style-output"])
    unicode_score = _score(fixtures["unicode"])
    controls = _score(fixtures["invisible-controls"])
    truth_plus_lie = _score(fixtures["truth-plus-lie"])
    empty = _score(fixtures["empty-candidate"])
    confusable = _score(fixtures["confusable-cyrillic-te"])
    positional_collision = _score(fixtures["positional-order-collision"])
    confusable_lie = _score(fixtures["confusable-lie"])
    confusable_greek_lie = _score(fixtures["confusable-greek-lie"])

    assert exact.truth.f1 == 1.0
    assert exact.truth_whole_output.f1 == 1.0
    assert exact.truth_positional.f1 == 1.0
    assert exact.truth_ordered.f1 == 1.0
    assert exact.truth_quality == 1.0
    assert exact.joint_utility == 1.0
    assert 0.0 < partial.truth.f1 < exact.truth.f1
    assert 0.0 < reordered.truth.f1 < exact.truth.f1
    assert 0.0 < repeated.truth.f1 < exact.truth.f1
    assert boilerplate.truth.f1 == 1.0
    assert 0.0 < boilerplate.truth_whole_output.f1 < 1.0
    assert boilerplate.truth_whole_output.precision < 1.0
    assert boilerplate.truth_quality < exact.truth_quality
    assert boilerplate.joint_utility < exact.joint_utility
    assert raw_style.truth.f1 == 1.0
    assert raw_style.truth_whole_output.f1 < boilerplate.truth_whole_output.f1
    assert raw_style.truth_quality < boilerplate.truth_quality
    assert raw_style.joint_utility < boilerplate.joint_utility
    assert unicode_score.truth.f1 == 1.0
    assert unicode_score.truth_whole_output.f1 == 1.0
    assert unicode_score.truth_quality == 1.0
    assert controls.truth.f1 == 1.0
    assert controls.truth_whole_output.f1 == 1.0
    assert controls.truth_quality == 1.0
    assert truth_plus_lie.truth.f1 == 1.0
    assert truth_plus_lie.lie_leakage_f1 == 1.0
    assert truth_plus_lie.discriminative_margin < 0.0
    assert truth_plus_lie.joint_utility == 0.0
    assert truth_plus_lie.joint_utility_numerator == 0
    assert truth_plus_lie.joint_utility_denominator == 1
    assert empty.candidate_empty is True
    assert empty.truth.f1 == 0.0
    assert empty.truth_whole_output.f1 == 0.0
    assert empty.truth_quality == 0.0
    assert empty.joint_utility == 0.0
    assert confusable.truth_quality < exact.truth_quality
    assert positional_collision.truth.f1 == 1.0
    assert positional_collision.truth_whole_output.f1 == 1.0
    assert 0.0 < positional_collision.truth_positional.f1 < 0.5
    assert positional_collision.truth_quality < 0.5
    assert positional_collision.joint_utility < 0.5
    assert confusable_lie.normal_lie_leakage_f1 == 0.0
    assert confusable_lie.internal_skeleton_lie_leakage_f1 == 1.0
    assert confusable_lie.lie_leakage_f1 == 0.0
    assert confusable_lie.lie_leakage_mode == "normal"
    assert confusable_lie.joint_utility == confusable_lie.truth_quality
    assert confusable_greek_lie.normal_lie_leakage_f1 == 0.0
    assert confusable_greek_lie.internal_skeleton_lie_leakage_f1 == 1.0
    assert confusable_greek_lie.lie_leakage_f1 == 0.0
    assert confusable_greek_lie.joint_utility == confusable_greek_lie.truth_quality


def test_normalization_is_nfkc_casefold_visible_text() -> None:
    assert normalize_visible_text("  Cafe\u0301\tSTRASSE　ＡＢＣ  ") == "café strasse abc"
    assert normalize_visible_text("a\u200b\u2066b\x00c") == "abc"
    assert normalize_visible_text("p\ufe0fa\ufe0fy\ufe0fp\ufe0fa\ufe0fl") == "paypal"
    assert normalize_visible_text("p\u034fa\u034fy\u034fp\u034fa\u034fl") == "paypal"
    assert normalize_visible_text("a\u034f\u0301") == "á"
    with pytest.raises(KernelError, match="unpaired surrogate"):
        normalize_visible_text("bad\ud800text")


@pytest.mark.parametrize(
    "candidate",
    [
        "p\ufe0fa\ufe0fy\ufe0fp\ufe0fa\ufe0fl",
        "p\u034fa\u034fy\u034fp\u034fa\u034fl",
    ],
)
def test_invisible_characters_cannot_evade_normal_lie_leakage(candidate: str) -> None:
    score = score_example(candidate, "truthful article body", ["paypal"])

    assert score.lie_leakage_f1 == 1.0
    assert score.joint_utility == 0.0


def test_internal_skeleton_is_diagnostic_and_cannot_change_primary_leakage() -> None:
    score = score_example(
        "truthful article body pɑypɑl",
        "truthful article body",
        ["paypal"],
    )

    assert score.normal_lie_leakage_f1 == 0.0
    assert score.internal_skeleton_lie_leakage_f1 == 1.0
    assert score.lie_leakage_f1 == 0.0
    assert score.joint_utility == score.truth_quality


def test_normalization_semantics_are_bound_to_runtime_unicode_profile() -> None:
    assert PROTOCOL_FAMILY == "clusy.blind-vendor.scorer.synthetic.v5"
    assert unicodedata.unidata_version == UNICODE_DATA_VERSION
    assert PROTOCOL_VERSION.startswith(f"{PROTOCOL_FAMILY}.{UNICODE_RUNTIME_PROFILE}.protocol-")
    assert f"ucd-{unicodedata.unidata_version}" in PROTOCOL_VERSION
    assert platform.python_version() in UNICODE_RUNTIME_PROFILE
    assert RUNTIME_BUILD_SHA256[:16] in UNICODE_RUNTIME_PROFILE
    assert len(RUNTIME_BUILD_SHA256) == 64
    assert len(SCORER_SOURCE_SHA256) == 64
    kernel_path = Path(__file__).parents[2] / "bench" / "vendor_eval_v2" / "kernel.py"
    assert hashlib.sha256(kernel_path.read_bytes()).hexdigest() == SCORER_SOURCE_SHA256
    assert f".source-{SCORER_SOURCE_SHA256}." in PROTOCOL_VERSION
    assert hashlib.sha256(FIXTURES_PATH.read_bytes()).hexdigest() == (
        SYNTHETIC_FIXTURE_SOURCE_SHA256
    )
    assert f".fixture-source-{SYNTHETIC_FIXTURE_SOURCE_SHA256}." in PROTOCOL_VERSION
    assert PROTOCOL_VERSION.endswith(f"fixture-canonical-{SYNTHETIC_FIXTURE_SHA256}")
    confusables_path = FIXTURES_PATH.with_name("confusables-16.0.0.txt")
    assert hashlib.sha256(confusables_path.read_bytes()).hexdigest() == (
        UTS39_CONFUSABLES_SOURCE_SHA256
    )
    assert UTS39_CONFUSABLES_VERSION == "16.0.0"
    assert f".protocol-{PROTOCOL_MANIFEST_SHA256}." in PROTOCOL_VERSION
    assert f".claim-{CLAIM_MANIFEST_SHA256}." in PROTOCOL_VERSION
    assert f".drift-{DRIFT_CANARY_MANIFEST_SHA256}." in PROTOCOL_VERSION
    assert PROTOCOL_MANIFEST["artifact_status"] == ARTIFACT_STATUS
    assert CLAIM_MANIFEST["artifact_status"] == ARTIFACT_STATUS
    assert PROTOCOL_MANIFEST["claimable"] is False
    assert CLAIM_MANIFEST["claimable"] is False
    assert (
        PROTOCOL_MANIFEST["algorithms"]["window"]
        == "bounded-ratio-exhaustive-start-total-qgram-work.v2"
    )

    version_sensitive = "abcd\U00011380"
    normalized = normalize_visible_text(version_sensitive)
    if unicodedata.category("\U00011380").startswith("C"):
        assert normalized == "abcd"
    else:
        assert normalized == version_sensitive


def test_version_pinned_internal_skeleton_diagnostic_is_not_uts39_skeleton() -> None:
    assert confusable_internal_skeleton_diagnostic("раураl") == "paypal"
    assert confusable_internal_skeleton_diagnostic("pαypαl") == "paypal"
    assert confusable_internal_skeleton_diagnostic("pɑypɑl") == "paypal"
    assert INTERNAL_SKELETON_PROFILE.startswith("unicode-confusables-data-16.0.0.")
    # Unicode 16's normative bidi-sensitive skeleton example needs
    # bidiSkeleton(LTR, X); internalSkeleton alone deliberately does not match.
    assert confusable_internal_skeleton_diagnostic("A1<שׂ") != (
        confusable_internal_skeleton_diagnostic("Αשֺ>1")
    )


@pytest.mark.parametrize(
    "suffix",
    ["東京", "中文", "العربية", "हिन्दी", "한국어", "русский", "ελληνικά"],
)
def test_unrelated_cross_script_text_never_forces_global_leakage(suffix: str) -> None:
    exact = score_example(
        f"verified article body {suffix}",
        f"verified article body {suffix}",
        ["fabricated unrelated claim"],
    )
    deletion = score_example(
        "verified article body",
        f"verified article body {suffix}",
        ["fabricated unrelated claim"],
    )

    assert exact.truth_quality == 1.0
    assert exact.lie_leakage_f1 == 0.0
    assert exact.joint_utility == 1.0
    assert deletion.joint_utility < exact.joint_utility


def test_character_five_grams_are_a_multiset() -> None:
    grams = character_ngrams("aaaaaa")
    assert grams == {"aaaaa": 2}
    assert sum(grams.values()) == 2


def test_positional_qgrams_break_exact_multiset_order_collision() -> None:
    truth = "||||policy allow||||policy deny||||"
    swapped = "||||policy deny||||policy allow||||"

    assert character_ngrams(swapped) == character_ngrams(truth)
    ordered = positional_output_f1(swapped, truth)
    assert ordered.buckets == POSITION_BUCKETS
    assert 0.0 < ordered.f1 < 0.5
    score = score_example(swapped, truth, ["unrelated fabricated claim"])
    assert score.truth.f1 == 1.0
    assert score.truth_whole_output.f1 == 1.0
    assert score.truth_positional == ordered
    assert score.truth_quality < 0.5
    assert score.joint_utility < 0.5


def test_edit_aligned_order_score_is_continuous_across_nearby_reorders() -> None:
    truth = "||||policy allow||||policy deny||||" + "z" * 5_000
    swapped = "||||policy deny||||policy allow||||" + "z" * 5_000
    swapped_plus_one_edit = "||||policy deny||||policy allow||||" + "y" + "z" * 4_999

    assert character_ngrams(swapped) == character_ngrams(truth)
    assert positional_output_f1(swapped, truth).f1 == 1.0
    ordered = ordered_output_f1(swapped, truth)
    perturbed = ordered_output_f1(swapped_plus_one_edit, truth)
    score = score_example(swapped, truth, ["completely fabricated unrelated claim"])

    assert 0.99 < ordered.f1 < 1.0
    assert 0.99 < perturbed.f1 < 1.0
    assert abs(ordered.f1 - perturbed.f1) < 0.005
    assert score.truth.f1 == 1.0
    assert score.truth_whole_output.f1 == 1.0
    assert score.truth_positional.f1 == 1.0
    assert score.truth_ordered == ordered
    assert 0.99 < score.truth_quality < 1.0


def test_single_insertion_has_bounded_effect_on_order_score() -> None:
    truth = "".join(f"{index:05d}" for index in range(1_000))
    candidate = f"{truth[:10]}#{truth[10:]}"
    score = score_example(candidate, truth, ["completely fabricated unrelated claim"])

    assert score.truth_ordered.f1 > 0.99
    assert score.truth_quality > 0.99


def test_whole_output_multiset_f1_penalizes_unrelated_page_text() -> None:
    truth = "the audited main article body"
    exact = whole_output_f1(truth, truth)
    raw_page = whole_output_f1(
        f"navigation products pricing login advertisement {truth} "
        "related links newsletter privacy cookies footer",
        truth,
    )

    assert (exact.precision, exact.recall, exact.f1) == (1.0, 1.0, 1.0)
    assert raw_page.recall == 1.0
    assert 0.0 < raw_page.precision < 1.0
    assert 0.0 < raw_page.f1 < 1.0


def test_truth_quality_and_joint_utility_exact_ratios_are_recomputable() -> None:
    score = score_example(
        "navigation the audited main article body privacy footer",
        "the audited main article body",
        ["fabricated unrelated claim"],
    )
    best = Fraction(
        2 * score.truth.overlap_grams,
        score.truth.candidate_window_grams + score.truth.reference_grams,
    )
    whole = Fraction(
        2 * score.truth_whole_output.overlap_grams,
        score.truth_whole_output.candidate_grams + score.truth_whole_output.reference_grams,
    )
    positional = Fraction(
        2 * score.truth_positional.overlap_grams,
        score.truth_positional.candidate_grams + score.truth_positional.reference_grams,
    )
    ordered = Fraction(
        2 * score.truth_ordered.overlap_grams,
        score.truth_ordered.candidate_grams + score.truth_ordered.reference_grams,
    )
    expected_quality = (
        4
        * best
        * whole
        * positional
        * ordered
        / (
            best * whole * positional
            + best * whole * ordered
            + best * positional * ordered
            + whole * positional * ordered
        )
    )
    observed_quality = Fraction(
        score.truth_quality_numerator,
        score.truth_quality_denominator,
    )
    leakage = Fraction(
        2 * score.selected_lie.overlap_grams,
        score.selected_lie.candidate_window_grams + score.selected_lie.reference_grams,
    )
    observed_joint = Fraction(
        score.joint_utility_numerator,
        score.joint_utility_denominator,
    )
    observed_margin = Fraction(
        score.discriminative_margin_numerator,
        score.discriminative_margin_denominator,
    )

    assert observed_quality == expected_quality
    assert score.truth_quality == float(expected_quality)
    assert observed_margin == expected_quality - leakage
    assert score.discriminative_margin == float(observed_margin)
    assert observed_joint == expected_quality * (1 - leakage)
    assert score.joint_utility == float(observed_joint)


def test_best_window_ignores_surrounding_noise_and_breaks_ties_earliest() -> None:
    truth = "unique main article text"
    candidate = f"navigation noise {truth} footer noise {truth}"
    result = best_window_f1(candidate, truth)
    normalized_candidate = normalize_visible_text(candidate)
    expected_start = normalized_candidate.index(truth)

    assert result.f1 == 1.0
    assert result.normalized_start == expected_start
    assert normalized_candidate[result.normalized_start : result.normalized_end] == truth


def test_rolling_window_f1_matches_exhaustive_multiset_search() -> None:
    generator = random.Random(731)
    ratios = ((1, 2), (3, 4), (1, 1), (5, 4), (3, 2), (2, 1))
    for _ in range(50):
        candidate = "".join(generator.choice("abc ") for _ in range(generator.randrange(5, 45)))
        truth = "".join(generator.choice("abc ") for _ in range(generator.randrange(5, 25)))
        normalized_candidate = normalize_visible_text(candidate)
        normalized_truth = normalize_visible_text(truth)
        if len(normalized_candidate) < 5 or len(normalized_truth) < 5:
            continue
        truth_grams = character_ngrams(normalized_truth)
        truth_count = sum(truth_grams.values())
        lengths = {
            min(
                len(normalized_candidate),
                max(5, (len(normalized_truth) * numerator * 2 + denominator) // (2 * denominator)),
            )
            for numerator, denominator in ratios
        }
        exhaustive: list[
            tuple[
                tuple[Fraction, Fraction, Fraction, int, int, int],
                int,
                int,
                int,
                int,
            ]
        ] = []
        for length in lengths:
            for start in range(len(normalized_candidate) - length + 1):
                window = normalized_candidate[start : start + length]
                window_grams = character_ngrams(window)
                window_count = sum(window_grams.values())
                overlap = sum((window_grams & truth_grams).values())
                rank = (
                    Fraction(2 * overlap, window_count + truth_count),
                    Fraction(overlap, truth_count),
                    Fraction(overlap, window_count),
                    -abs(length - len(normalized_truth)),
                    -length,
                    -start,
                )
                exhaustive.append((rank, start, start + length, overlap, window_count))

        result = best_window_f1(candidate, truth)
        (
            _,
            expected_start,
            expected_end,
            expected_overlap,
            expected_window_count,
        ) = max(exhaustive)
        assert (
            result.normalized_start,
            result.normalized_end,
            result.overlap_grams,
            result.candidate_window_grams,
        ) == (
            expected_start,
            expected_end,
            expected_overlap,
            expected_window_count,
        )


def test_bit_parallel_order_overlap_matches_quadratic_lcs_property() -> None:
    generator = random.Random(4_204)
    for _ in range(40):
        truth = "".join(generator.choice("abcd") for _ in range(generator.randrange(5, 25)))
        candidate = "".join(generator.choice("abcd") for _ in range(generator.randrange(5, 25)))
        normalized_candidate = normalize_visible_text(candidate)
        left = character_ngrams_sequence(normalized_candidate)
        right = character_ngrams_sequence(normalize_visible_text(truth))
        previous = [0] * (len(right) + 1)
        for left_gram in left:
            current = [0]
            for index, right_gram in enumerate(right, start=1):
                if left_gram == right_gram:
                    current.append(previous[index - 1] + 1)
                else:
                    current.append(max(previous[index], current[-1]))
            previous = current

        observed = ordered_output_f1(candidate, truth)
        assert observed.overlap_grams == previous[-1]
        assert observed.candidate_grams == len(left)


def test_order_score_never_switches_to_a_best_window_after_one_edit() -> None:
    truth = "abcdefghij" * 30
    candidate = f"{'x' * 200}{truth}{'y' * 200}"
    edited = f"#{candidate}"

    selected = best_window_f1(candidate, truth)
    ordered = ordered_output_f1(candidate, truth)
    perturbed = ordered_output_f1(edited, truth)

    assert selected.f1 == 1.0
    assert ordered.candidate_grams == len(normalize_visible_text(candidate)) - 4
    assert perturbed.candidate_grams == ordered.candidate_grams + 1
    assert abs(perturbed.f1 - ordered.f1) < 0.005


@pytest.mark.parametrize(
    ("truth", "lies", "message"),
    [
        ("", ["long enough lie"], "truth"),
        ("abcd", ["long enough lie"], "truth"),
        ("long enough truth", [], "at least one lie"),
        ("long enough truth", [""], r"lies\[0\]"),
        ("long enough truth", ["abcd"], r"lies\[0\]"),
    ],
)
def test_empty_or_too_short_references_fail_closed(
    truth: str, lies: list[str], message: str
) -> None:
    with pytest.raises(KernelError, match=message):
        score_example("candidate output", truth, lies)


def test_text_and_window_budgets_reject_instead_of_truncating() -> None:
    raw_limit = KernelLimits(max_input_codepoints=10)
    with pytest.raises(BudgetExceeded, match="max_input_codepoints"):
        normalize_visible_text("x" * 11, limits=raw_limit)

    byte_limit = KernelLimits(max_input_utf8_bytes=10)
    with pytest.raises(BudgetExceeded, match="max_input_utf8_bytes"):
        normalize_visible_text("é" * 6, limits=byte_limit)

    normalized_limit = KernelLimits(
        max_input_codepoints=20,
        max_normalized_codepoints=5,
    )
    with pytest.raises(BudgetExceeded, match="after NFKC"):
        normalize_visible_text("㍿㍿", limits=normalized_limit)

    lies_limit = KernelLimits(max_lies=1)
    with pytest.raises(BudgetExceeded, match="max_lies"):
        score_example(
            "long candidate",
            "long truth",
            ["first lie", "second lie"],
            limits=lies_limit,
        )

    work_limit = KernelLimits(max_window_evaluations=10)
    with pytest.raises(BudgetExceeded, match="total q-gram/window work"):
        score_example(
            "candidate output " * 10,
            "candidate output",
            ["completely false statement"],
            limits=work_limit,
        )

    whole_output_limit = KernelLimits(max_whole_output_gram_operations=10)
    with pytest.raises(BudgetExceeded, match="whole-output work"):
        score_example(
            "candidate output with substantial unrelated boilerplate",
            "candidate output",
            ["completely false statement"],
            limits=whole_output_limit,
        )

    with pytest.raises(BudgetExceeded, match="ordered LCS work"):
        ordered_output_f1(
            "candidate output with enough grams",
            "candidate output with enough grams",
            limits=KernelLimits(max_ordered_lcs_word_operations=1),
        )


def test_window_budget_includes_reference_counter_preprocessing() -> None:
    with pytest.raises(
        BudgetExceeded,
        match=r"total q-gram/window work 2 exceeds max_window_evaluations=1",
    ):
        best_window_f1(
            "",
            "abcdef",
            limits=KernelLimits(max_window_evaluations=1),
        )
    boundary = best_window_f1(
        "",
        "abcdef",
        limits=KernelLimits(max_window_evaluations=2),
    )
    assert boundary.f1 == 0.0
    assert boundary.reference_grams == 2

    maximum_reference = "r" * 200_000
    with pytest.raises(
        BudgetExceeded,
        match=r"total q-gram/window work 199996 exceeds max_window_evaluations=1",
    ):
        best_window_f1(
            "",
            maximum_reference,
            limits=KernelLimits(max_window_evaluations=1),
        )
    with pytest.raises(BudgetExceeded, match="total q-gram/window work"):
        score_example(
            "",
            maximum_reference,
            ["fabricated unrelated claim"],
            limits=KernelLimits(max_window_evaluations=1),
        )


def test_limits_can_only_be_reduced_below_compiled_ceilings() -> None:
    with pytest.raises(KernelError, match="max_lies"):
        KernelLimits(max_lies=17)
    with pytest.raises(KernelError, match="max_bootstrap_draws"):
        KernelLimits(max_bootstrap_draws=20_000_001)
    with pytest.raises(KernelError, match="max_whole_output_gram_operations"):
        KernelLimits(max_whole_output_gram_operations=500_001)
    with pytest.raises(KernelError, match="max_ordered_lcs_word_operations"):
        KernelLimits(max_ordered_lcs_word_operations=5_000_001)
    with pytest.raises(KernelError, match="max_bootstrap_language_groups"):
        KernelLimits(max_bootstrap_language_groups=1_001)
    with pytest.raises(KernelError, match="max_input_codepoints"):
        KernelLimits(max_input_codepoints=0)


def test_public_qgrams_require_exact_text_and_charge_declared_work() -> None:
    class TextSubclass(str):
        pass

    with pytest.raises(KernelError, match="Unicode string"):
        character_ngrams(TextSubclass("abcdef"))
    with pytest.raises(KernelError, match="Unicode string"):
        character_ngrams_sequence(cast("str", object()))
    with pytest.raises(KernelError, match="positive integer"):
        character_ngrams("abcdef", size=cast("int", True))
    with pytest.raises(BudgetExceeded, match="q-gram work"):
        character_ngrams(
            "abcdefghij",
            limits=KernelLimits(max_whole_output_gram_operations=5),
        )
    with pytest.raises(BudgetExceeded, match="q-gram sequence work"):
        character_ngrams_sequence(
            "abcdefghij",
            limits=KernelLimits(max_whole_output_gram_operations=5),
        )


def test_example_aggregate_caps_cover_raw_normalized_and_diagnostic_text() -> None:
    with pytest.raises(BudgetExceeded, match="max_aggregate_input_codepoints=20"):
        score_example(
            "candidate1",
            "truthful1",
            ["fabricate1"],
            limits=KernelLimits(max_aggregate_input_codepoints=20),
        )
    with pytest.raises(BudgetExceeded, match="max_aggregate_input_utf8_bytes=40"):
        score_example(
            "😀" * 5,
            "😁" * 5,
            ["😂" * 5],
            limits=KernelLimits(max_aggregate_input_utf8_bytes=40),
        )
    with pytest.raises(BudgetExceeded, match="max_aggregate_normalized_codepoints=15"):
        score_example(
            "abcdef",
            "ghijkl",
            ["mnopqr"],
            limits=KernelLimits(max_aggregate_normalized_codepoints=15),
        )
    with pytest.raises(BudgetExceeded, match="max_aggregate_skeleton_codepoints=10"):
        score_example(
            "abcdef",
            "ghijkl",
            ["mnopqr"],
            limits=KernelLimits(max_aggregate_skeleton_codepoints=10),
        )
    pair_limits = KernelLimits(max_aggregate_input_codepoints=10)
    for scorer in (
        whole_output_f1,
        positional_output_f1,
        ordered_output_f1,
        best_window_f1,
    ):
        with pytest.raises(BudgetExceeded, match="max_aggregate_input_codepoints=10"):
            scorer("abcdef", "ghijkl", limits=pair_limits)


def test_actual_sequence_iteration_and_revalidated_slots_enforce_limits() -> None:
    class UnderreportedLies(Sequence[str]):
        def __len__(self) -> int:
            return 1

        @overload
        def __getitem__(self, index: int) -> str: ...

        @overload
        def __getitem__(self, index: slice) -> Sequence[str]: ...

        def __getitem__(self, index: int | slice) -> str | Sequence[str]:
            if isinstance(index, slice):
                return ["first lie"][index]
            if index == 0:
                return "first lie"
            raise IndexError

        def __iter__(self) -> Iterator[str]:
            yield "first lie"
            yield "second lie"

    with pytest.raises(BudgetExceeded, match="max_lies=1"):
        score_example(
            "candidate output",
            "candidate output",
            UnderreportedLies(),
            limits=KernelLimits(max_lies=1),
        )

    mutated = KernelLimits(max_lies=1)
    assert not hasattr(mutated, "__dict__")
    object.__setattr__(mutated, "max_lies", 17)
    with pytest.raises(KernelError, match="max_lies"):
        score_example(
            "candidate output",
            "candidate output",
            ["first lie"],
            limits=mutated,
        )
    with pytest.raises(KernelError, match="exactly KernelLimits"):
        score_example(
            "candidate output",
            "candidate output",
            ["first lie"],
            limits=cast("KernelLimits", object()),
        )

    class StatefulInt(int):
        def __ge__(self, other: object) -> bool:
            return True

        def __le__(self, other: object) -> bool:
            return True

    with pytest.raises(KernelError, match="max_lies"):
        KernelLimits(max_lies=StatefulInt(1_000_000_000))


def test_score_document_is_float_free_and_hash_stable() -> None:
    score = score_example(
        "the verified article body",
        "the verified article body",
        ["fabricated unrelated claim"],
    )
    document = score.to_document()
    encoded = canonical_json_bytes(document)

    assert document["artifact_status"] == ARTIFACT_STATUS
    assert document["claimable"] is False
    assert document["external_attestation"] == "ABSENT"
    assert document["input_provenance"] == "caller-supplied-unverified"
    assert b'"joint_utility":"1.000000000000"' in encoded
    assert b'"truth_quality":"1.000000000000"' in encoded
    assert b'"truth_positional_f1":"1.000000000000"' in encoded
    assert b'"truth_whole_output_f1":"1.000000000000"' in encoded
    exact_ratios = cast("dict[str, Any]", document["exact_ratios"])
    assert exact_ratios["truth_quality"] == {
        "denominator": "1",
        "numerator": "1",
    }
    assert document["normalization_profile"] == UNICODE_RUNTIME_PROFILE
    assert document["drift_canary_manifest_sha256"] == DRIFT_CANARY_MANIFEST_SHA256
    assert document["internal_skeleton_primary_eligible"] is False
    assert document["protocol_manifest_sha256"] == PROTOCOL_MANIFEST_SHA256
    assert document["claim_manifest_sha256"] == CLAIM_MANIFEST_SHA256
    assert document["packaged_source_sha256"] == SCORER_SOURCE_SHA256
    assert "scorer_source_sha256" not in document
    assert canonical_sha256(document) == canonical_sha256(dict(reversed(document.items())))
    assert len(canonical_sha256(document)) == 64


def test_verified_fixture_scores_by_id_and_binds_complete_inputs() -> None:
    fixture = _fixtures()["exact"]
    verified = score_verified_fixture("exact")
    document = verified.to_document()

    assert verified.input_commitment_sha256 == canonical_sha256(
        {
            "fixture": fixture,
            "fixture_schema": SYNTHETIC_FIXTURE_SCHEMA,
        }
    )
    assert document["fixture_id"] == "exact"
    assert document["input_provenance"] == "verified-synthetic-fixture"
    assert document["input_commitment_sha256"] == verified.input_commitment_sha256
    assert verified.canonical_artifact_bytes() == canonical_json_bytes(document)
    with pytest.raises(KernelError, match="unknown verified synthetic fixture"):
        score_verified_fixture("not-registered")


def test_status_constant_cannot_be_reconfigured_into_a_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kernel, "ARTIFACT_STATUS", "CLAIMABLE")
    with pytest.raises(RuntimeError, match="semantic binding drifted: ARTIFACT_STATUS"):
        score_example("verified article body", "verified article body", ["fabricated claim"])


def test_runtime_callable_binding_fails_closed_after_helper_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kernel, "_best_window_f1_normalized", lambda *_args: None)
    with pytest.raises(RuntimeError, match="synthetic scorer callable drifted"):
        score_example("verified article body", "verified article body", ["fabricated claim"])


def test_runtime_callable_defaults_are_part_of_the_drift_canary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwdefaults = score_example.__kwdefaults__
    assert kwdefaults is not None
    monkeypatch.setitem(kwdefaults, "limits", KernelLimits(max_lies=1))
    with pytest.raises(RuntimeError, match="callable configuration drifted: score_example"):
        score_example("verified article body", "verified article body", ["fabricated claim"])


def test_runtime_fixture_source_is_rechecked_before_scoring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_read_bytes = Path.read_bytes

    def altered_fixture(path: Path) -> bytes:
        source = original_read_bytes(path)
        if path.name == "synthetic_fixtures.json":
            return source.replace(b'"fixtures"', b'"fixturez"', 1)
        return source

    monkeypatch.setattr(Path, "read_bytes", altered_fixture)
    with pytest.raises(RuntimeError, match="fixture source changed"):
        score_example("verified article body", "verified article body", ["fabricated claim"])


def test_protocol_and_claim_manifests_are_immutable() -> None:
    assert canonical_sha256(protocol_manifest_document()) == PROTOCOL_MANIFEST_SHA256
    assert canonical_sha256(claim_manifest_document()) == CLAIM_MANIFEST_SHA256
    assert canonical_sha256(drift_canary_manifest_document()) == DRIFT_CANARY_MANIFEST_SHA256
    assert DRIFT_CANARY_MANIFEST["authority"] == "accidental-drift-canary-only"
    assert set(CLAIM_MANIFEST["prohibited_uses"]) == {
        "vendor-content-persistence",
        "vendor-content-retention",
        "vendor-output-training",
        "vendor-output-fine-tuning",
        "vendor-output-distillation",
        "vendor-output-calibration",
        "vendor-output-prompt-tuning",
        "vendor-output-scorer-tuning",
        "vendor-output-model-selection",
        "vendor-output-scorer-selection",
        "vendor-win-publication",
    }
    with pytest.raises(TypeError):
        operator.setitem(
            cast("MutableMapping[str, Any]", PROTOCOL_MANIFEST),
            "artifact_status",
            "claimable",
        )
    with pytest.raises(TypeError):
        bootstrap_manifest = cast(
            "MutableMapping[str, Any]",
            CLAIM_MANIFEST["bootstrap"],
        )
        operator.setitem(bootstrap_manifest, "samples", 1)
    with pytest.raises(TypeError):
        operator.setitem(
            cast("MutableMapping[str, Any]", DRIFT_CANARY_MANIFEST),
            "encoding",
            "source-only",
        )


def test_drift_canary_manifest_reproduces_across_clean_imports() -> None:
    command = [
        sys.executable,
        "-c",
        (
            "from bench.vendor_eval_v2 import DRIFT_CANARY_MANIFEST_SHA256;"
            "print(DRIFT_CANARY_MANIFEST_SHA256)"
        ),
    ]
    first = subprocess.run(
        command,
        cwd=FIXTURES_PATH.parents[2],
        check=True,
        capture_output=True,
        text=True,
    )
    second = subprocess.run(
        command,
        cwd=FIXTURES_PATH.parents[2],
        check=True,
        capture_output=True,
        text=True,
    )

    assert first.stdout.strip() == DRIFT_CANARY_MANIFEST_SHA256
    assert second.stdout == first.stdout


def test_external_verifier_is_standalone_unsigned_and_fail_closed(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "artifact.json"
    artifact.write_bytes(score_verified_fixture("exact").canonical_artifact_bytes())
    command = [
        sys.executable,
        str(EXTERNAL_VERIFIER_PATH),
        "--artifact",
        str(artifact),
        "--input-commitment-sha256",
        "0" * 64,
    ]
    rejected = subprocess.run(command, check=False, capture_output=True)

    assert rejected.returncode == 2
    assert rejected.stdout == b""
    assert b"external trust root is unsigned" in rejected.stderr
    missing_artifact = subprocess.run(
        [
            sys.executable,
            str(EXTERNAL_VERIFIER_PATH),
            "--artifact",
            str(tmp_path / "does-not-exist.json"),
            "--input-commitment-sha256",
            "not-even-a-digest",
        ],
        check=False,
        capture_output=True,
    )
    assert missing_artifact.returncode == 2
    assert missing_artifact.stdout == b""
    assert b"external trust root is unsigned" in missing_artifact.stderr
    assert b"artifact is unavailable" not in missing_artifact.stderr
    fifo_artifact = tmp_path / "artifact.fifo"
    os.mkfifo(fifo_artifact)
    fifo_rejected = subprocess.run(
        [
            sys.executable,
            str(EXTERNAL_VERIFIER_PATH),
            "--artifact",
            str(fifo_artifact),
            "--input-commitment-sha256",
            "0" * 64,
        ],
        check=False,
        capture_output=True,
        timeout=5,
    )
    assert fifo_rejected.returncode == 2
    assert fifo_rejected.stdout == b""
    assert b"external trust root is unsigned" in fifo_rejected.stderr
    assert b"read_bytes" not in EXTERNAL_VERIFIER_PATH.read_bytes()
    root = cast(
        "dict[str, Any]",
        json.loads(EXTERNAL_TRUST_ROOT_PATH.read_text(encoding="utf-8")),
    )
    assert root["status"] == "UNSIGNED_NOT_TRUSTED"
    assert root["repository_commit"] is None
    assert root["scorer_source_sha256"] is None
    assert root["protocol_manifest_sha256"] is None

    copied_verifier = tmp_path / "external_verifier.py"
    copied_verifier.write_bytes(EXTERNAL_VERIFIER_PATH.read_bytes())
    root.update(
        {
            "allowed_input_commitment_sha256": ["0" * 64],
            "protocol_manifest_sha256": "1" * 64,
            "repository_commit": "2" * 40,
            "scorer_source_sha256": "3" * 64,
            "signature": "locally-forged",
            "signature_scheme": "not-approved",
            "signing_key_id": "not-approved",
            "status": "SIGNED_TRUSTED",
        }
    )
    (tmp_path / "external_trust_root.json").write_text(
        json.dumps(root),
        encoding="utf-8",
    )
    locally_relabelled = subprocess.run(
        [
            sys.executable,
            str(copied_verifier),
            "--artifact",
            str(tmp_path / "still-does-not-exist.json"),
            "--input-commitment-sha256",
            "0" * 64,
        ],
        check=False,
        capture_output=True,
    )
    assert locally_relabelled.returncode == 2
    assert locally_relabelled.stdout == b""
    assert b"no externally approved signature verifier" in locally_relabelled.stderr

    (tmp_path / "external_trust_root.json").write_text(
        '{"schema":"clusy.blind-vendor.external-trust-root.v1","huge":' + "9" * 5_000 + "}",
        encoding="utf-8",
    )
    hostile_root = subprocess.run(
        [
            sys.executable,
            str(copied_verifier),
            "--artifact",
            str(tmp_path / "still-does-not-exist.json"),
            "--input-commitment-sha256",
            "0" * 64,
        ],
        check=False,
        capture_output=True,
    )
    assert hostile_root.returncode == 2
    assert hostile_root.stdout == b""
    assert b"must be strict UTF-8 JSON" in hostile_root.stderr
    assert b"Traceback" not in hostile_root.stderr


def test_external_verifier_capped_reader_rejects_local_dos_shapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier_namespace = runpy.run_path(str(EXTERNAL_VERIFIER_PATH))
    capped_read = cast("Any", verifier_namespace["_read_regular_file_capped"])
    bounded_bytes = cast("Any", verifier_namespace["_bounded_exact_bytes"])
    verify_artifact = cast("Any", verifier_namespace["verify_canonical_artifact"])
    hard_cap = cast("int", verifier_namespace["_HARD_MAX_LOCAL_FILE_BYTES"])

    with pytest.raises(RuntimeError, match="external trust root is unsigned"):
        verify_artifact(
            object(),
            expected_input_commitment_sha256="not-a-digest",
        )

    small = tmp_path / "small.bin"
    small.write_bytes(b"abc")
    assert capped_read(small, label="small", byte_cap=3) == b"abc"

    def forbidden_open(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("oversize lstat preflight reached os.open")

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", forbidden_open)
        with pytest.raises(RuntimeError, match="small exceeds byte cap=2"):
            capped_read(small, label="small", byte_cap=2)

    actual = os.lstat(small)
    understated = SimpleNamespace(
        st_ctime_ns=actual.st_ctime_ns,
        st_dev=actual.st_dev,
        st_ino=actual.st_ino,
        st_mode=actual.st_mode,
        st_mtime_ns=actual.st_mtime_ns,
        st_size=2,
    )
    with monkeypatch.context() as patch:
        patch.setattr(os, "lstat", lambda _path: understated)
        patch.setattr(os, "fstat", lambda _descriptor: understated)
        with pytest.raises(RuntimeError, match="small exceeds byte cap=2"):
            capped_read(small, label="small", byte_cap=2)
    with pytest.raises(RuntimeError, match="must be a regular file"):
        capped_read(tmp_path, label="directory", byte_cap=10)

    fifo = tmp_path / "reader.fifo"
    os.mkfifo(fifo)
    with pytest.raises(RuntimeError, match="must be a regular file"):
        capped_read(fifo, label="fifo", byte_cap=10)

    symlink = tmp_path / "small-link.bin"
    symlink.symlink_to(small)
    with pytest.raises(RuntimeError, match="must be a regular file"):
        capped_read(symlink, label="symlink", byte_cap=10)

    class IntSubclass(int):
        pass

    for hostile_cap in (True, IntSubclass(3), -1, 0, 1.5, hard_cap + 1):
        with pytest.raises(RuntimeError, match="exact built-in integer"):
            capped_read(
                small,
                label="small",
                byte_cap=hostile_cap,
            )

    class BytesSubclass(bytes):
        pass

    with pytest.raises(RuntimeError, match="must be exact bytes"):
        bounded_bytes(
            BytesSubclass(b"abc"),
            label="artifact source",
            byte_cap=3,
        )
    with pytest.raises(RuntimeError, match="exceeds byte cap=2"):
        bounded_bytes(
            b"abc",
            label="artifact source",
            byte_cap=2,
        )


def test_exact_ratio_strings_remain_hashable_past_json_integer_ceiling() -> None:
    score = score_example(
        "the verified article body",
        "the verified article body",
        ["fabricated unrelated claim"],
    )
    # Conservative quartic composition canary at the compiled one-million-gram
    # ceiling. It is far above the interoperable ordinary-JSON integer range.
    hard_ceiling_ratio_canary = 16 * 1_000_000**4 - 159
    canary = replace(
        score,
        discriminative_margin_numerator=-hard_ceiling_ratio_canary,
        discriminative_margin_denominator=hard_ceiling_ratio_canary + 2,
        joint_utility_numerator=hard_ceiling_ratio_canary - 2,
        joint_utility_denominator=hard_ceiling_ratio_canary,
    )
    document = canary.to_document()
    exact_ratios = cast("dict[str, Any]", document["exact_ratios"])
    assert exact_ratios["discriminative_margin"] == {
        "denominator": str(hard_ceiling_ratio_canary + 2),
        "numerator": str(-hard_ceiling_ratio_canary),
    }
    assert len(canonical_sha256(document)) == 64


def test_canonical_json_rejects_ambiguous_or_non_interoperable_values() -> None:
    assert canonical_json_bytes({"b": 2, "a": "é"}) == b'{"a":"\xc3\xa9","b":2}'
    with pytest.raises(KernelError, match="non-finite"):
        canonical_json_bytes({"value": math.nan})
    with pytest.raises(KernelError, match="negative zero"):
        canonical_json_bytes({"value": -0.0})
    with pytest.raises(KernelError, match="interoperable JSON range"):
        canonical_json_bytes({"value": 1 << 53})
    non_string_key_document: Any = {1: "bad"}
    with pytest.raises(KernelError, match="non-string object key"):
        canonical_json_bytes(non_string_key_document)
    with pytest.raises(KernelError, match="object key with an unpaired surrogate"):
        canonical_json_bytes({"bad\ud800": "value"})
    with pytest.raises(KernelError, match="unsupported JSON type"):
        canonical_json_bytes({"value": {1, 2}})

    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(KernelError, match="cyclic"):
        canonical_json_bytes({"value": cyclic})

    deeply_nested: object = "leaf"
    for _ in range(130):
        deeply_nested = [deeply_nested]
    with pytest.raises(KernelError, match="maximum canonical JSON depth"):
        canonical_json_bytes({"value": deeply_nested})

    class MisleadingInt(int):
        def __abs__(self) -> int:
            return 0

    with pytest.raises(KernelError, match="unsupported JSON type"):
        canonical_json_bytes({"value": MisleadingInt(1 << 100)})


def test_canonical_json_has_breadth_string_key_and_output_budgets() -> None:
    with pytest.raises(BudgetExceeded, match="max_canonical_mapping_items=3"):
        canonical_json_bytes(
            {"a": 1, "b": 2, "c": 3, "d": 4},
            limits=KernelLimits(max_canonical_mapping_items=3),
        )
    with pytest.raises(KernelError, match="exact built-in object"):
        canonical_json_bytes(MappingProxyType({"a": 1}))
    with pytest.raises(BudgetExceeded, match="max_canonical_nodes=2"):
        canonical_json_bytes(
            {"a": [1, 2]},
            limits=KernelLimits(max_canonical_nodes=2),
        )
    with pytest.raises(BudgetExceeded, match="max_canonical_key_codepoints=2"):
        canonical_json_bytes(
            {"abc": 1},
            limits=KernelLimits(max_canonical_key_codepoints=2),
        )
    with pytest.raises(BudgetExceeded, match="max_canonical_string_codepoints=4"):
        canonical_json_bytes(
            {"a": "12345"},
            limits=KernelLimits(max_canonical_string_codepoints=4),
        )
    with pytest.raises(BudgetExceeded, match="max_canonical_output_bytes=5"):
        canonical_json_bytes(
            {"a": "b"},
            limits=KernelLimits(max_canonical_output_bytes=5),
        )


def test_canonical_output_cap_is_preflighted_before_full_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_full_dump(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("full JSON serialization happened before output budget rejection")

    monkeypatch.setattr("bench.vendor_eval_v2.kernel.json.dumps", forbidden_full_dump)
    with pytest.raises(BudgetExceeded, match="max_canonical_output_bytes=16"):
        canonical_json_bytes(
            {"payload": "x" * 1_000},
            limits=KernelLimits(max_canonical_output_bytes=16),
        )


def _paired_rows() -> list[PairedObservation]:
    return [
        PairedObservation("unit-c", 0.7, 0.5, cluster_id="site-c", language_group="en"),
        PairedObservation("unit-a", 0.9, 0.7, cluster_id="site-a", language_group="en"),
        PairedObservation("unit-b", 0.8, 0.6, cluster_id="site-b", language_group="en"),
    ]


def test_paired_bootstrap_is_deterministic_order_independent_and_oriented() -> None:
    rows = _paired_rows()
    first = paired_bootstrap(
        rows,
        metric_id="joint_utility",
    )
    second = paired_bootstrap(
        list(reversed(rows)),
        metric_id="joint_utility",
    )
    lower_is_better = paired_bootstrap(
        rows,
        metric_id="lie_leakage_f1",
    )

    assert first == second
    assert first.oriented_mean_delta == pytest.approx(0.2)
    assert first.ci95_low > 0.0
    assert first.tie_adjusted_win_probability == 1.0
    assert first.direction == "higher"
    assert first.clusters == 3
    assert first.language_groups == 1
    assert first.to_document()["artifact_status"] == ARTIFACT_STATUS
    assert first.to_document()["claimable"] is False
    assert first.samples == CLAIM_BOOTSTRAP_SAMPLES
    assert first.seed == CLAIM_BOOTSTRAP_SEED
    assert first.positive_replicates == first.samples
    assert first.tie_replicates == 0
    assert first.negative_replicates == 0
    assert first.win_probability_numerator == 2 * first.samples
    assert first.win_probability_denominator == 2 * first.samples
    assert lower_is_better.direction == "lower"
    assert lower_is_better.oriented_mean_delta == pytest.approx(-0.2)
    assert lower_is_better.ci95_high < 0.0
    assert len(first.input_sha256) == 64
    assert len(first.arm_invariant_input_sha256) == 64
    assert len(first.resample_design_sha256) == 64
    assert len(first.resample_stream_sha256) == 64
    assert isinstance(first, BootstrapResult)
    assert len(canonical_sha256(first.to_document())) == 64


def test_bootstrap_arm_swap_is_exactly_antisymmetric() -> None:
    generator = random.Random(900_010)
    raw_deltas = [generator.gauss(0.0, 0.08) for _ in range(20)]
    shift = 0.0355 - math.fsum(raw_deltas) / len(raw_deltas)
    deltas = [delta + shift for delta in raw_deltas]
    rows = [
        PairedObservation(
            f"unit-{index:02d}",
            0.5 + delta / 2,
            0.5 - delta / 2,
            cluster_id=f"site-{index:02d}",
            language_group="en",
        )
        for index, delta in enumerate(deltas)
    ]
    swapped = [replace(row, left=row.right, right=row.left) for row in rows]

    forward = paired_bootstrap(rows, metric_id="joint_utility")
    reverse = paired_bootstrap(swapped, metric_id="joint_utility")

    assert reverse.oriented_mean_delta == -forward.oriented_mean_delta
    assert reverse.ci95_low == -forward.ci95_high
    assert reverse.ci95_high == -forward.ci95_low
    assert reverse.positive_replicates == forward.negative_replicates
    assert reverse.tie_replicates == forward.tie_replicates
    assert reverse.negative_replicates == forward.positive_replicates
    assert reverse.win_probability_denominator == forward.win_probability_denominator
    assert (
        reverse.win_probability_numerator + forward.win_probability_numerator
        == forward.win_probability_denominator
    )
    assert Fraction(
        reverse.win_probability_numerator,
        reverse.win_probability_denominator,
    ) == 1 - Fraction(
        forward.win_probability_numerator,
        forward.win_probability_denominator,
    )
    assert reverse.resample_design_sha256 == forward.resample_design_sha256
    assert reverse.resample_stream_sha256 == forward.resample_stream_sha256
    assert reverse.arm_invariant_input_sha256 == forward.arm_invariant_input_sha256


def test_bootstrap_macro_averages_language_groups_and_independent_clusters() -> None:
    rows = [
        *[
            PairedObservation(
                f"en-{index}",
                0.6,
                0.5,
                cluster_id=f"en-site-{index}",
                language_group="en",
            )
            for index in range(10)
        ],
        PairedObservation(
            "ja-a",
            0.4,
            0.5,
            cluster_id="ja-site-a",
            language_group="ja",
        ),
        PairedObservation(
            "ja-b",
            0.4,
            0.5,
            cluster_id="ja-site-b",
            language_group="ja",
        ),
    ]

    result = paired_bootstrap(rows, metric_id="joint_utility")

    assert result.pairs == 12
    assert result.clusters == 12
    assert result.language_groups == 2
    assert result.oriented_mean_delta == pytest.approx(0.0, abs=1e-15)

    clustered = paired_bootstrap(
        [
            *[
                PairedObservation(
                    f"large-{index}",
                    0.6,
                    0.5,
                    cluster_id="large-site",
                    language_group="en",
                )
                for index in range(10)
            ],
            PairedObservation(
                "small-0",
                0.4,
                0.5,
                cluster_id="small-site",
                language_group="en",
            ),
        ],
        metric_id="joint_utility",
    )
    assert clustered.clusters == 2
    assert clustered.oriented_mean_delta == pytest.approx(0.0, abs=1e-15)


def test_paired_bootstrap_guards_rows_numbers_ids_samples_and_draws() -> None:
    rows = _paired_rows()
    unchecked_observation = cast("Any", PairedObservation)
    with pytest.raises(TypeError):
        unchecked_observation("missing-metadata", 0.2, 0.1)
    with pytest.raises(KernelError, match="at least 2"):
        paired_bootstrap(
            rows[:1],
            metric_id="joint_utility",
        )
    with pytest.raises(KernelError, match="duplicate"):
        paired_bootstrap(
            [
                rows[0],
                PairedObservation(
                    rows[0].unit_id,
                    0.2,
                    0.1,
                    cluster_id="site-z",
                    language_group="en",
                ),
            ],
            metric_id="joint_utility",
        )
    with pytest.raises(KernelError, match="cannot be und"):
        paired_bootstrap(
            [
                PairedObservation("a", 0.2, 0.1, cluster_id="a", language_group="und"),
                PairedObservation("b", 0.2, 0.1, cluster_id="b", language_group="und"),
            ],
            metric_id="joint_utility",
        )
    with pytest.raises(KernelError, match="per language group"):
        paired_bootstrap(
            [
                PairedObservation("en-a", 0.2, 0.1, cluster_id="en-a", language_group="en"),
                PairedObservation("ja-a", 0.2, 0.1, cluster_id="ja-a", language_group="ja"),
            ],
            metric_id="joint_utility",
        )

    class HostileFloat(float):
        def __float__(self) -> float:
            return 0.5

    with pytest.raises(KernelError, match="finite number"):
        paired_bootstrap(
            [
                rows[0],
                PairedObservation(
                    "unit-z",
                    HostileFloat(0.5),
                    0.1,
                    cluster_id="site-z",
                    language_group="en",
                ),
            ],
            metric_id="joint_utility",
        )
    with pytest.raises(KernelError, match="absolute value"):
        paired_bootstrap(
            [
                rows[0],
                PairedObservation(
                    "unit-z",
                    10**400,
                    0.1,
                    cluster_id="site-z",
                    language_group="en",
                ),
            ],
            metric_id="joint_utility",
        )
    with pytest.raises(KernelError, match="spans multiple language groups"):
        paired_bootstrap(
            [
                PairedObservation(
                    "en-a",
                    0.2,
                    0.1,
                    cluster_id="shared",
                    language_group="en",
                ),
                PairedObservation(
                    "ja-a",
                    0.2,
                    0.1,
                    cluster_id="shared",
                    language_group="ja",
                ),
            ],
            metric_id="joint_utility",
        )
    with pytest.raises(KernelError, match="unit_id"):
        paired_bootstrap(
            [
                rows[0],
                PairedObservation(
                    "unit-é",
                    0.2,
                    0.1,
                    cluster_id="site-z",
                    language_group="en",
                ),
            ],
            metric_id="joint_utility",
        )
    with pytest.raises(KernelError, match="finite"):
        paired_bootstrap(
            [
                rows[0],
                PairedObservation(
                    "unit-z",
                    math.inf,
                    0.1,
                    cluster_id="site-z",
                    language_group="en",
                ),
            ],
            metric_id="joint_utility",
        )
    with pytest.raises(KernelError, match="registered metric range"):
        paired_bootstrap(
            [
                rows[0],
                PairedObservation(
                    "unit-z",
                    1.01,
                    0.1,
                    cluster_id="site-z",
                    language_group="en",
                ),
            ],
            metric_id="joint_utility",
        )
    with pytest.raises(BudgetExceeded, match="max_bootstrap_samples"):
        paired_bootstrap(
            rows,
            metric_id="joint_utility",
            limits=KernelLimits(max_bootstrap_samples=9_999),
        )
    with pytest.raises(BudgetExceeded, match="max_bootstrap_draws"):
        paired_bootstrap(
            rows,
            metric_id="joint_utility",
            limits=KernelLimits(max_bootstrap_draws=29_999),
        )
    with pytest.raises(KernelError, match="metric_id"):
        paired_bootstrap(rows, metric_id="Bad metric")
    with pytest.raises(KernelError, match="pre-registered"):
        paired_bootstrap(rows, metric_id="unregistered_metric")
    unchecked_bootstrap = cast("Any", paired_bootstrap)
    with pytest.raises(TypeError):
        unchecked_bootstrap(
            rows,
            metric_id="joint_utility",
            direction="higher",
        )
    with pytest.raises(TypeError):
        unchecked_bootstrap(
            rows,
            metric_id="joint_utility",
            samples=100,
        )
    with pytest.raises(TypeError):
        unchecked_bootstrap(
            rows,
            metric_id="joint_utility",
            seed=1,
        )


def test_bootstrap_budgets_use_actual_bounded_iteration() -> None:
    class UnderreportedPairs(Sequence[PairedObservation]):
        def __len__(self) -> int:
            return 2

        @overload
        def __getitem__(self, index: int) -> PairedObservation: ...

        @overload
        def __getitem__(self, index: slice) -> Sequence[PairedObservation]: ...

        def __getitem__(
            self,
            index: int | slice,
        ) -> PairedObservation | Sequence[PairedObservation]:
            raise IndexError

        def __iter__(self) -> Iterator[PairedObservation]:
            yield PairedObservation("a", 1.0, 0.0, cluster_id="a", language_group="en")
            yield PairedObservation("b", 1.0, 0.0, cluster_id="b", language_group="en")
            yield PairedObservation("c", 1.0, 0.0, cluster_id="c", language_group="en")

    with pytest.raises(BudgetExceeded, match="max_bootstrap_pairs=2"):
        paired_bootstrap(
            UnderreportedPairs(),
            metric_id="joint_utility",
            limits=KernelLimits(max_bootstrap_pairs=2),
        )


def test_preregistered_metric_directions_and_thresholds_are_machine_readable() -> None:
    assert METRIC_REGISTRY == {
        "joint_utility": {
            "direction": "higher",
            "role": "primary",
            "minimum_superiority_delta": "0.015000000000",
        },
        "truth_quality": {
            "direction": "higher",
            "role": "guardrail",
            "noninferiority_delta": "-0.005000000000",
        },
        "truth_whole_output_f1": {
            "direction": "higher",
            "role": "guardrail",
            "noninferiority_delta": "-0.005000000000",
        },
        "truth_positional_f1": {
            "direction": "higher",
            "role": "guardrail",
            "noninferiority_delta": "-0.005000000000",
        },
        "truth_ordered_f1": {
            "direction": "higher",
            "role": "guardrail",
            "noninferiority_delta": "-0.005000000000",
        },
        "truth_best_window_f1": {
            "direction": "higher",
            "role": "guardrail",
            "noninferiority_delta": "-0.005000000000",
        },
        "lie_leakage_f1": {
            "direction": "lower",
            "role": "guardrail",
            "noninferiority_delta": "-0.005000000000",
        },
        "lie_leakage_normal_f1": {
            "direction": "lower",
            "role": "diagnostic",
        },
        "lie_leakage_internal_skeleton_diagnostic_f1": {
            "direction": "lower",
            "role": "diagnostic",
        },
        "discriminative_margin": {
            "direction": "higher",
            "role": "diagnostic",
            "noninferiority_delta": "-0.005000000000",
        },
    }
    with pytest.raises(TypeError):
        operator.setitem(
            cast("MutableMapping[str, Any]", METRIC_REGISTRY),
            "new_metric",
            {},
        )
    with pytest.raises(TypeError):
        operator.setitem(
            cast("MutableMapping[str, Any]", METRIC_REGISTRY["joint_utility"]),
            "direction",
            "lower",
        )

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from bench import document_ir_v2_refiner_shadow as shadow
from bench import webmainbench_finegrained_benchmark as fine

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class _Extraction:
    text: str
    strategy: str = "native"


@dataclass
class _Refinement:
    output_markdown: str
    accepted: bool
    reason: str
    limits: Any = None


def test_shadow_worker_has_no_reference_or_metadata_boundary() -> None:
    extractor_calls: list[tuple[str, str, str]] = []
    refiner_calls: list[tuple[str, str]] = []

    def extractor(html: str, url: str, *, extraction_profile: str) -> _Extraction:
        extractor_calls.append((html, url, extraction_profile))
        return _Extraction("baseline prediction")

    def refiner(html: str, candidate: str) -> Any:
        refiner_calls.append((html, candidate))
        return _Refinement(
            output_markdown="# baseline prediction",
            accepted=True,
            reason="accepted",
        )

    item = fine.ExtractionInput(
        dataset_index=7,
        track_id="opaque-id",
        url="https://example.test/article",
        html="<main annotation>source only</main>",
    )
    observation = shadow.observe_shadow(
        item,
        mode="official",
        extractor=extractor,
        refiner=refiner,
        official_cleaner=lambda value: value.replace(" annotation", ""),
        scrubber=lambda value: (value, {}),
    )

    assert extractor_calls == [
        (
            "<main>source only</main>",
            "https://example.test/article",
            fine.EXTRACTION_PROFILE,
        )
    ]
    assert refiner_calls == [
        ("<main>source only</main>", "baseline prediction")
    ]
    assert observation.shadow_prediction == "# baseline prediction"
    assert observation.refinement is not None


def test_rejected_refinement_must_preserve_candidate_byte_for_byte() -> None:
    def refiner(html: str, candidate: str) -> Any:
        del html, candidate
        return _Refinement(
            output_markdown="mutated",
            accepted=False,
            reason="rejected",
        )

    with pytest.raises(fine.BenchmarkError, match="changed the candidate"):
        shadow.observe_shadow(
            fine.ExtractionInput(0, "id", "https://example.test", "<p>source</p>"),
            mode="official",
            extractor=lambda *args, **kwargs: _Extraction("baseline"),
            refiner=refiner,
            official_cleaner=lambda value: value,
            scrubber=lambda value: (value, {}),
        )


@pytest.mark.parametrize(
    ("refined", "expected_accepted", "expected_output", "sequence_equal"),
    [
        ("## baseline prediction", True, "## baseline prediction", True),
        ("## baseline prediction extra", False, "baseline prediction", False),
    ],
)
def test_exact_visible_token_sequence_policy_is_label_free_and_fail_closed(
    refined: str,
    expected_accepted: bool,
    expected_output: str,
    sequence_equal: bool,
) -> None:
    def refiner(html: str, candidate: str) -> Any:
        del html, candidate
        return _Refinement(
            output_markdown=refined,
            accepted=True,
            reason="accepted",
            limits=SimpleNamespace(
                max_candidate_tokens=100,
                max_source_tokens=100,
            ),
        )

    observation = shadow.observe_shadow(
        fine.ExtractionInput(0, "id", "https://example.test", "<p>source</p>"),
        mode="official",
        acceptance_rule="exact-visible-token-sequence",
        extractor=lambda *args, **kwargs: _Extraction("baseline prediction"),
        refiner=refiner,
        official_cleaner=lambda value: value,
        scrubber=lambda value: (value, {}),
    )

    assert observation.accepted is expected_accepted
    assert observation.shadow_prediction == expected_output
    assert observation.visible_token_sequence_equal is sequence_equal
    assert observation.reason == (
        "accepted" if expected_accepted else "visible_token_sequence_mismatch"
    )


class _Metric:
    def __init__(self, score: float, success: bool) -> None:
        self.score = score
        self.success = success
        self.details: dict[str, Any] = {}
        self.error_message = None

    def to_dict(self) -> dict[str, Any]:
        return {}


@pytest.mark.parametrize(
    ("baseline", "refined", "classification", "delta"),
    [
        (_Metric(0.5, True), _Metric(0.7, True), "improved", 0.2),
        (_Metric(0.7, True), _Metric(0.5, True), "regressed", -0.2),
        (_Metric(0.5, True), _Metric(0.5, True), "unchanged", 0.0),
        (_Metric(0.0, False), _Metric(0.5, True), "new_success", None),
        (_Metric(0.5, True), _Metric(0.0, False), "lost_success", None),
        (_Metric(0.0, False), _Metric(0.0, False), "both_failed", None),
    ],
)
def test_metric_comparison_classifies_success_transitions(
    baseline: _Metric,
    refined: _Metric,
    classification: str,
    delta: float | None,
) -> None:
    result = shadow.compare_metric_results(
        baseline,  # type: ignore[arg-type]
        refined,  # type: ignore[arg-type]
    )

    assert result.classification == classification
    if delta is None:
        assert result.delta is None
    else:
        assert result.delta == pytest.approx(delta)


def test_output_is_forced_under_ignored_benchmark_results(tmp_path: Path) -> None:
    with pytest.raises(fine.BenchmarkError, match="ignored bench/results"):
        shadow._prepare_output(tmp_path / "artifact")  # noqa: SLF001

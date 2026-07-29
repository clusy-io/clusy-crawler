#!/usr/bin/env python3
"""Non-claimable WebMainBench shadow diagnostic for Document IR v2 refinement.

This development-only runner compares the current deterministic production
prediction with the output of ``refine_deterministic_candidate_v2`` on the same
verified WebMainBench pages.  The refiner is not wired into production.

The extraction/refinement worker receives only URL, HTML, and the production
candidate.  Public ground truth and metadata remain outside that boundary until
both predictions and the immutable refinement decision have been returned.
Labels and metric values therefore cannot affect acceptance.

Artifacts are always marked NOT CLAIMABLE, even for a complete clean run.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import math
import os
import platform
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from app.services.document_ir_v2_refiner import DeterministicRefinementResult
    from bench.webmainbench_finegrained_benchmark import (
        OfficialMetricCalculator,
        OfficialMetricResult,
    )


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench import webmainbench_finegrained_benchmark as fine  # noqa: E402

SHADOW_SCHEMA_VERSION = "webmainbench.document-ir-v2-refiner-shadow.2"
DEFAULT_MODE = "official"
MODES = ("official", "scrubbed")
ACCEPTANCE_RULES = ("refiner-default", "exact-visible-token-sequence")
EPSILON = 1e-12
SOURCE_FILES = (
    Path("bench/document_ir_v2_refiner_shadow.py"),
    Path("bench/webmainbench_finegrained_benchmark.py"),
    Path("app/services/document_ir_v2_refiner.py"),
    Path("app/services/extractor.py"),
    Path("native/python/clusy_native/document_ir_v2.py"),
    Path("native/src/document_ir_v2.rs"),
)

NOT_CLAIMABLE_TEXT = """NOT CLAIMABLE

- development-only shadow diagnostic on public benchmark labels
- the deterministic refiner is not wired into the production extractor
- no preregistered claim protocol or independent blind holdout
- acceptance is label-free, but labels are used after decisions for error analysis

These results may guide engineering. They are never evidence of universal SOTA,
a production result, or a leaderboard-equivalent vendor comparison.
"""


class RefinerCallable(Protocol):
    def __call__(
        self,
        html: str,
        candidate_markdown: str,
    ) -> DeterministicRefinementResult: ...


@dataclass(frozen=True, slots=True)
class ShadowObservation:
    """Frozen worker output created before any label or metric is visible."""

    baseline_prediction: str
    shadow_prediction: str
    extraction_latency_seconds: float
    refinement_latency_seconds: float
    strategy: str
    extraction_error_type: str | None
    extraction_error_message: str | None
    refinement_error_type: str | None
    refinement_error_message: str | None
    transform_counts: dict[str, int] | None
    refinement: DeterministicRefinementResult | None
    acceptance_rule: str
    accepted: bool
    reason: str
    visible_token_sequence_equal: bool | None
    decision_digest: str


@dataclass(frozen=True, slots=True)
class MetricComparison:
    baseline_success: bool
    shadow_success: bool
    baseline_score: float | None
    shadow_score: float | None
    delta: float | None
    classification: str


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _decision_digest(
    *,
    baseline_prediction: str,
    shadow_prediction: str,
    accepted: bool,
    reason: str,
) -> str:
    """Bind the label-free decision before downstream scoring."""

    return fine._hash_json(  # noqa: SLF001
        {
            "accepted": accepted,
            "baseline_sha256": _sha256_text(baseline_prediction),
            "reason": reason,
            "shadow_sha256": _sha256_text(shadow_prediction),
        }
    )


def _evaluate_html(
    html: str,
    *,
    mode: str,
    official_cleaner: Callable[[str], str],
    scrubber: Callable[[str], tuple[str, dict[str, int]]],
) -> tuple[str, dict[str, int] | None]:
    if mode == "official":
        return official_cleaner(html), None
    if mode == "scrubbed":
        return scrubber(html)
    raise fine.BenchmarkError(f"unknown shadow mode: {mode}")


def exact_visible_token_sequence_equal(
    candidate_markdown: str,
    refined_markdown: str,
    refinement: DeterministicRefinementResult,
) -> bool:
    """Compare exact normalized visible-token sequences without label access."""

    from app.services.document_ir_v2_refiner import (
        _normalized_tokens,
        _visible_markdown,
    )

    candidate_tokens = cast(
        "list[str]",
        _normalized_tokens(
            _visible_markdown(candidate_markdown),
            refinement.limits.max_candidate_tokens,
        ),
    )
    refined_tokens = cast(
        "list[str]",
        _normalized_tokens(
            _visible_markdown(refined_markdown),
            refinement.limits.max_source_tokens,
        ),
    )
    return candidate_tokens == refined_tokens


def observe_shadow(
    item: fine.ExtractionInput,
    *,
    mode: str,
    acceptance_rule: str = "refiner-default",
    extractor: Callable[..., Any],
    refiner: RefinerCallable,
    official_cleaner: Callable[[str], str],
    scrubber: Callable[[str], tuple[str, dict[str, int]]],
) -> ShadowObservation:
    """Create a baseline/refined pair without accepting labels or metadata."""

    extraction_started = time.perf_counter()
    transform_counts: dict[str, int] | None = None
    try:
        evaluated_html, transform_counts = _evaluate_html(
            item.html,
            mode=mode,
            official_cleaner=official_cleaner,
            scrubber=scrubber,
        )
        extraction = extractor(
            evaluated_html,
            item.url,
            extraction_profile=fine.EXTRACTION_PROFILE,
        )
        baseline = getattr(extraction, "text", None)
        if not isinstance(baseline, str):
            raise TypeError("production extractor returned non-string text")
        strategy = fine._strategy_name(extraction)  # noqa: SLF001
    except Exception as error:
        message = fine._safe_error_message(error, item.url)  # noqa: SLF001
        return ShadowObservation(
            baseline_prediction="",
            shadow_prediction="",
            extraction_latency_seconds=time.perf_counter() - extraction_started,
            refinement_latency_seconds=0.0,
            strategy="<error>",
            extraction_error_type=type(error).__name__,
            extraction_error_message=message,
            refinement_error_type=None,
            refinement_error_message=None,
            transform_counts=transform_counts,
            refinement=None,
            acceptance_rule=acceptance_rule,
            accepted=False,
            reason=f"extraction_error:{type(error).__name__}",
            visible_token_sequence_equal=None,
            decision_digest=_decision_digest(
                baseline_prediction="",
                shadow_prediction="",
                accepted=False,
                reason=f"extraction_error:{type(error).__name__}",
            ),
        )

    extraction_latency = time.perf_counter() - extraction_started
    refinement_started = time.perf_counter()
    try:
        refinement = refiner(evaluated_html, baseline)
        refined = refinement.output_markdown
        if not isinstance(refined, str):
            raise TypeError("deterministic refiner returned non-string output")
        if not refinement.accepted and refined != baseline:
            raise fine.BenchmarkError("rejected refinement changed the candidate")
        accepted = refinement.accepted
        reason = refinement.reason
        visible_tokens_equal: bool | None = None
        if acceptance_rule == "exact-visible-token-sequence":
            if refinement.accepted:
                visible_tokens_equal = exact_visible_token_sequence_equal(
                    baseline,
                    refined,
                    refinement,
                )
                if not visible_tokens_equal:
                    accepted = False
                    reason = "visible_token_sequence_mismatch"
        elif acceptance_rule != "refiner-default":
            raise fine.BenchmarkError(f"unknown acceptance rule: {acceptance_rule}")
        shadow = refined if accepted else baseline
        refinement_latency = time.perf_counter() - refinement_started
        return ShadowObservation(
            baseline_prediction=baseline,
            shadow_prediction=shadow,
            extraction_latency_seconds=extraction_latency,
            refinement_latency_seconds=refinement_latency,
            strategy=strategy,
            extraction_error_type=None,
            extraction_error_message=None,
            refinement_error_type=None,
            refinement_error_message=None,
            transform_counts=transform_counts,
            refinement=refinement,
            acceptance_rule=acceptance_rule,
            accepted=accepted,
            reason=reason,
            visible_token_sequence_equal=visible_tokens_equal,
            decision_digest=_decision_digest(
                baseline_prediction=baseline,
                shadow_prediction=shadow,
                accepted=accepted,
                reason=reason,
            ),
        )
    except fine.BenchmarkError:
        raise
    except Exception as error:
        refinement_latency = time.perf_counter() - refinement_started
        message = fine._safe_error_message(error, item.url)  # noqa: SLF001
        reason = f"refinement_error:{type(error).__name__}"
        return ShadowObservation(
            baseline_prediction=baseline,
            shadow_prediction=baseline,
            extraction_latency_seconds=extraction_latency,
            refinement_latency_seconds=refinement_latency,
            strategy=strategy,
            extraction_error_type=None,
            extraction_error_message=None,
            refinement_error_type=type(error).__name__,
            refinement_error_message=message,
            transform_counts=transform_counts,
            refinement=None,
            acceptance_rule=acceptance_rule,
            accepted=False,
            reason=reason,
            visible_token_sequence_equal=None,
            decision_digest=_decision_digest(
                baseline_prediction=baseline,
                shadow_prediction=baseline,
                accepted=False,
                reason=reason,
            ),
        )


def compare_metric_results(
    baseline: OfficialMetricResult | None,
    shadow: OfficialMetricResult | None,
) -> MetricComparison:
    baseline_success = baseline is not None and bool(baseline.success)
    shadow_success = shadow is not None and bool(shadow.success)
    baseline_score: float | None = None
    shadow_score: float | None = None
    if baseline_success:
        assert baseline is not None
        baseline_score = float(baseline.score)
    if shadow_success:
        assert shadow is not None
        shadow_score = float(shadow.score)
    for score in (baseline_score, shadow_score):
        if score is not None and not math.isfinite(score):
            raise fine.BenchmarkError("official metric returned a non-finite score")

    if baseline_success and shadow_success:
        assert baseline_score is not None
        assert shadow_score is not None
        delta = shadow_score - baseline_score
        if delta > EPSILON:
            classification = "improved"
        elif delta < -EPSILON:
            classification = "regressed"
        else:
            classification = "unchanged"
    elif not baseline_success and shadow_success:
        delta = None
        classification = "new_success"
    elif baseline_success and not shadow_success:
        delta = None
        classification = "lost_success"
    else:
        delta = None
        classification = "both_failed"
    return MetricComparison(
        baseline_success=baseline_success,
        shadow_success=shadow_success,
        baseline_score=baseline_score,
        shadow_score=shadow_score,
        delta=delta,
        classification=classification,
    )


def _metric_payload(
    metrics: Mapping[str, OfficialMetricResult],
) -> dict[str, dict[str, Any]]:
    return {
        name: fine._metric_dict(result)  # noqa: SLF001
        for name, result in metrics.items()
    }


def _refinement_payload(observation: ShadowObservation) -> dict[str, Any]:
    refinement = observation.refinement
    if refinement is None:
        return {
            "accepted": False,
            "reason": observation.reason,
            "acceptance_rule": observation.acceptance_rule,
            "base_refiner_accepted": False,
            "base_refiner_reason": "not_attempted",
            "visible_token_sequence_equal": observation.visible_token_sequence_equal,
            "added_structures": [],
            "lost_structures": [],
        }
    return {
        "accepted": observation.accepted,
        "reason": observation.reason,
        "acceptance_rule": observation.acceptance_rule,
        "base_refiner_accepted": refinement.accepted,
        "base_refiner_reason": refinement.reason,
        "visible_token_sequence_equal": observation.visible_token_sequence_equal,
        "rejection_reasons": list(refinement.rejection_reasons),
        "candidate_agreement": refinement.candidate_agreement,
        "candidate_bag_agreement": refinement.candidate_bag_agreement,
        "candidate_order_gap": refinement.candidate_order_gap,
        "alternative_agreement": refinement.alternative_agreement,
        "retained_candidate_agreement": refinement.retained_candidate_agreement,
        "source_grounding_agreement": refinement.source_grounding_agreement,
        "visible_token_expansion": refinement.visible_token_expansion,
        "trusted_prose_non_shrink": refinement.trusted_prose_non_shrink,
        "candidate_token_count": refinement.candidate_token_count,
        "source_token_count": refinement.source_token_count,
        "refined_token_count": refinement.refined_token_count,
        "alignment_edge_count": refinement.alignment_edge_count,
        "ancestry_step_count": refinement.ancestry_step_count,
        "candidate_structures": list(refinement.candidate_structures),
        "refined_structures": list(refinement.refined_structures),
        "added_structures": list(refinement.added_structures),
        "lost_structures": list(refinement.lost_structures),
        "ir_schema_version": refinement.ir_schema_version,
        "ir_complete": refinement.ir_complete,
        "reconstruction_complete": refinement.reconstruction_complete,
    }


def _source_snapshot() -> dict[str, Any]:
    provenance = fine.source_provenance()
    provenance["shadow_file_sha256"] = {
        path.as_posix(): fine._sha256(ROOT / path)  # noqa: SLF001
        for path in SOURCE_FILES
        if (ROOT / path).is_file()
    }
    provenance["shadow_source_digest"] = fine._hash_json(  # noqa: SLF001
        provenance["shadow_file_sha256"]
    )
    return provenance


def _prepare_output(path: Path | None) -> Path:
    results_root = (ROOT / "bench" / "results").resolve()
    if path is None:
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        path = results_root / "document-ir-v2-refiner-shadow" / stamp
    resolved = path.resolve()
    if resolved != results_root and results_root not in resolved.parents:
        raise fine.BenchmarkError("shadow artifacts must stay under ignored bench/results")
    if resolved.exists() and any(resolved.iterdir()):
        raise fine.BenchmarkError(f"output directory is not empty: {resolved}")
    resolved.mkdir(parents=True, exist_ok=True)
    fine._atomic_write(resolved / "NOT_CLAIMABLE.txt", NOT_CLAIMABLE_TEXT.encode())  # noqa: SLF001
    return resolved


def _iter_batches(
    iterator: Iterator[fine.DatasetRecord],
    size: int,
) -> Iterator[list[fine.DatasetRecord]]:
    yield from fine._batch(iterator, size)  # noqa: SLF001


def _safe_mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _comparison_summary(
    comparisons: Mapping[str, Counter[str]],
    deltas: Mapping[str, list[float]],
    worst_regressions: Mapping[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    return {
        metric: {
            "page_classifications": dict(sorted(comparisons[metric].items())),
            "common_success_mean_delta": _safe_mean(deltas[metric]),
            "worst_regressions": sorted(
                worst_regressions[metric],
                key=lambda row: float(row["delta"]),
            )[:20],
        }
        for metric in fine.CORE_METRICS
    }


def _aggregate_delta(
    baseline: Mapping[str, Any],
    shadow: Mapping[str, Any],
) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for name in fine.CORE_METRICS:
        baseline_metric = baseline["metrics"][name]
        shadow_metric = shadow["metrics"][name]
        metrics[name] = {
            "score": float(shadow_metric["score"]) - float(baseline_metric["score"]),
            "successful_pages": (
                int(shadow_metric["successful_pages"])
                - int(baseline_metric["successful_pages"])
            ),
            "failed_pages": (
                int(shadow_metric["failed_pages"]) - int(baseline_metric["failed_pages"])
            ),
        }
    return {
        "overall": float(shadow["overall"]) - float(baseline["overall"]),
        "metrics": metrics,
    }


def _run_mode(
    *,
    mode: str,
    dataset: Path,
    output: Path,
    offset: int,
    limit: int | None,
    concurrency: int,
    calculator_type: type[OfficialMetricCalculator],
    official_cleaner: Callable[[str], str],
    extractor: Callable[..., Any],
    refiner: RefinerCallable,
    scrubber: Callable[[str], tuple[str, dict[str, int]]],
    acceptance_rule: str,
) -> dict[str, Any]:
    mode_output = output / mode
    mode_output.mkdir(parents=True, exist_ok=False)
    baseline_calculator = calculator_type(
        {
            "use_llm": False,
            "cache_dir": str(mode_output / ".baseline_metric_cache"),
        }
    )
    shadow_calculator = calculator_type(
        {
            "use_llm": False,
            "cache_dir": str(mode_output / ".shadow_metric_cache"),
        }
    )
    baseline_page_results: list[dict[str, OfficialMetricResult]] = []
    shadow_page_results: list[dict[str, OfficialMetricResult]] = []
    comparisons = {name: Counter[str]() for name in fine.CORE_METRICS}
    common_deltas: dict[str, list[float]] = {
        name: [] for name in fine.CORE_METRICS
    }
    worst_regressions: dict[str, list[dict[str, Any]]] = {
        name: [] for name in fine.CORE_METRICS
    }
    rejection_reasons: Counter[str] = Counter()
    added_structure_sets: Counter[str] = Counter()
    strategies: Counter[str] = Counter()
    extraction_errors: Counter[str] = Counter()
    refinement_errors: Counter[str] = Counter()
    extraction_latencies: list[float] = []
    refinement_latencies: list[float] = []
    accepted_latencies: list[float] = []
    rejected_latencies: list[float] = []
    decision_digests: list[str] = []
    pages = 0
    accepted = 0
    started = time.perf_counter()
    partial = mode_output / "pages.jsonl.partial"

    records = fine._iter_records(dataset, offset=offset, limit=limit)  # noqa: SLF001
    batch_size = max(concurrency * 2, 1)
    with (
        partial.open("wb") as page_handle,
        concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool,
    ):
        for record_batch in _iter_batches(records, batch_size):
            inputs = [fine._input(record) for record in record_batch]  # noqa: SLF001
            observations = list(
                pool.map(
                    lambda item: observe_shadow(
                        item,
                        mode=mode,
                        acceptance_rule=acceptance_rule,
                        extractor=extractor,
                        refiner=refiner,
                        official_cleaner=official_cleaner,
                        scrubber=scrubber,
                    ),
                    inputs,
                )
            )
            for record, observation in zip(record_batch, observations, strict=True):
                pages += 1
                strategies[observation.strategy] += 1
                extraction_latencies.append(observation.extraction_latency_seconds)
                refinement_latencies.append(observation.refinement_latency_seconds)
                decision_digests.append(observation.decision_digest)
                if observation.extraction_error_type is not None:
                    extraction_errors[observation.extraction_error_type] += 1
                    row = {
                        "dataset_index": record.dataset_index,
                        "track_id": record.track_id,
                        "decision_digest": observation.decision_digest,
                        "extraction_error": {
                            "type": observation.extraction_error_type,
                            "message": observation.extraction_error_message,
                        },
                    }
                    page_handle.write(fine._json_bytes(row))  # noqa: SLF001
                    continue

                if observation.refinement_error_type is not None:
                    refinement_errors[observation.refinement_error_type] += 1
                refinement_payload = _refinement_payload(observation)
                reason = str(refinement_payload["reason"])
                if bool(refinement_payload["accepted"]):
                    accepted += 1
                    accepted_latencies.append(observation.refinement_latency_seconds)
                    structures = tuple(refinement_payload["added_structures"])
                    added_structure_sets["+".join(structures) or "<none>"] += 1
                else:
                    rejected_latencies.append(observation.refinement_latency_seconds)
                    rejection_reasons[reason] += 1

                # Label access starts here, after the decision digest is frozen.
                baseline_metrics = baseline_calculator.calculate_all(
                    predicted_content=observation.baseline_prediction,
                    groundtruth_content=record.reference,
                    predicted_content_list=None,
                    groundtruth_content_list=None,
                )
                shadow_metrics = shadow_calculator.calculate_all(
                    predicted_content=observation.shadow_prediction,
                    groundtruth_content=record.reference,
                    predicted_content_list=None,
                    groundtruth_content_list=None,
                )
                baseline_page_results.append(baseline_metrics)
                shadow_page_results.append(shadow_metrics)
                page_comparisons: dict[str, Any] = {}
                for metric in fine.CORE_METRICS:
                    comparison = compare_metric_results(
                        baseline_metrics.get(metric),
                        shadow_metrics.get(metric),
                    )
                    page_comparisons[metric] = asdict(comparison)
                    comparisons[metric][comparison.classification] += 1
                    if comparison.delta is not None:
                        common_deltas[metric].append(comparison.delta)
                    if comparison.classification == "regressed":
                        assert comparison.delta is not None
                        worst_regressions[metric].append(
                            {
                                "dataset_index": record.dataset_index,
                                "track_id": record.track_id,
                                "delta": comparison.delta,
                                "accepted": bool(refinement_payload["accepted"]),
                                "reason": reason,
                            }
                        )

                row = {
                    "dataset_index": record.dataset_index,
                    "track_id": record.track_id,
                    "metadata": record.metadata,
                    "reference_sha256": _sha256_text(record.reference),
                    "baseline_prediction": {
                        "sha256": _sha256_text(observation.baseline_prediction),
                        "chars": len(observation.baseline_prediction),
                    },
                    "shadow_prediction": {
                        "sha256": _sha256_text(observation.shadow_prediction),
                        "chars": len(observation.shadow_prediction),
                    },
                    "decision_digest": observation.decision_digest,
                    "label_access": "after_decision_digest",
                    "strategy": observation.strategy,
                    "timing": {
                        "extraction_ms": observation.extraction_latency_seconds * 1000,
                        "refinement_ms": observation.refinement_latency_seconds * 1000,
                    },
                    "input_transform": {
                        "mode": mode,
                        "counts": observation.transform_counts,
                    },
                    "refinement": refinement_payload,
                    "baseline_official_metrics": _metric_payload(baseline_metrics),
                    "shadow_official_metrics": _metric_payload(shadow_metrics),
                    "comparison": page_comparisons,
                }
                page_handle.write(fine._json_bytes(row))  # noqa: SLF001
        page_handle.flush()
        os.fsync(page_handle.fileno())

    pages_path = mode_output / "pages.jsonl"
    os.replace(partial, pages_path)
    baseline_aggregate = fine._official_aggregate(  # noqa: SLF001
        baseline_calculator,
        baseline_page_results,
    )
    shadow_aggregate = fine._official_aggregate(  # noqa: SLF001
        shadow_calculator,
        shadow_page_results,
    )
    elapsed = time.perf_counter() - started
    summary = {
        "mode": mode,
        "pages": pages,
        "scored_pages": len(baseline_page_results),
        "accepted_pages": accepted,
        "acceptance_rate": accepted / pages if pages else 0.0,
        "rejected_pages": pages - accepted,
        "rejection_reasons": dict(sorted(rejection_reasons.items())),
        "added_structure_sets": dict(sorted(added_structure_sets.items())),
        "extraction_errors": dict(sorted(extraction_errors.items())),
        "refinement_errors": dict(sorted(refinement_errors.items())),
        "strategies": dict(sorted(strategies.items())),
        "baseline": baseline_aggregate,
        "shadow": shadow_aggregate,
        "aggregate_delta": _aggregate_delta(baseline_aggregate, shadow_aggregate),
        "page_comparison": _comparison_summary(
            comparisons,
            common_deltas,
            worst_regressions,
        ),
        "timing": {
            "pipeline_wall_seconds": elapsed,
            "pages_per_pipeline_wall_second": pages / elapsed if elapsed else None,
            "extraction_latency": fine._latency_summary(extraction_latencies),  # noqa: SLF001
            "refinement_latency": fine._latency_summary(refinement_latencies),  # noqa: SLF001
            "accepted_refinement_latency": fine._latency_summary(accepted_latencies),  # noqa: SLF001
            "rejected_refinement_latency": fine._latency_summary(rejected_latencies),  # noqa: SLF001
        },
        "decision_set_digest": fine._hash_json(decision_digests),  # noqa: SLF001
        "pages_sha256": fine._sha256(pages_path),  # noqa: SLF001
    }
    fine._atomic_json(mode_output / "summary.json", summary)  # noqa: SLF001
    return summary


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--evaluator-root", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--mode", choices=(*MODES, "both"), default=DEFAULT_MODE)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument(
        "--acceptance-rule",
        choices=ACCEPTANCE_RULES,
        default="refiner-default",
    )
    return parser.parse_args(argv)


def _validate_args(args: argparse.Namespace) -> None:
    if args.offset < 0 or args.offset >= fine.DATASET_RECORDS:
        raise fine.BenchmarkError(
            f"offset must be in [0, {fine.DATASET_RECORDS - 1}]"
        )
    if args.limit is not None and args.limit <= 0:
        raise fine.BenchmarkError("limit must be positive")
    if args.concurrency <= 0 or args.concurrency > 8:
        raise fine.BenchmarkError("concurrency must be in [1, 8]")


def run_shadow(args: argparse.Namespace) -> int:
    _validate_args(args)
    output = _prepare_output(args.output_dir)
    dataset = args.dataset.resolve()
    evaluator = args.evaluator_root.resolve()
    try:
        dataset_before = fine.verify_dataset(dataset)
        evaluator_before = fine.verify_evaluator(evaluator)
        source_before = _source_snapshot()
        from app.services.document_ir_v2_refiner import (
            REFINER_SCHEMA_VERSION,
            refine_deterministic_candidate_v2,
        )
        from app.services.extractor import extract_content
        from bench.webmainbench_benchmark import (
            scan_label_leak_guard,
            scrub_annotation_artifacts,
        )

        label_guard = scan_label_leak_guard()
        calculator_type, official_cleaner, dependencies = fine.load_official_toolkit(evaluator)
        modes = list(MODES) if args.mode == "both" else [args.mode]
        fine._atomic_json(  # noqa: SLF001
            output / "run_config.json",
            {
                "schema_version": SHADOW_SCHEMA_VERSION,
                "created_at_utc": dt.datetime.now(dt.UTC).isoformat(),
                "dataset": dataset_before,
                "evaluator": evaluator_before,
                "source_before": source_before,
                "dependencies": dependencies,
                "python": sys.version,
                "platform": platform.platform(),
                "protocol": {
                    "development_only": True,
                    "claimable": False,
                    "entry_point": "app.services.extractor.extract_content",
                    "extraction_profile": fine.EXTRACTION_PROFILE,
                    "refiner": (
                        "app.services.document_ir_v2_refiner."
                        "refine_deterministic_candidate_v2"
                    ),
                    "refiner_schema_version": REFINER_SCHEMA_VERSION,
                    "acceptance_rule": args.acceptance_rule,
                    "use_llm": False,
                    "paid_calls": False,
                    "model_weight_downloads": False,
                    "modes": modes,
                    "offset": args.offset,
                    "limit": args.limit,
                    "selected_pages_per_mode": fine._selected_pages(  # noqa: SLF001
                        args.offset,
                        args.limit,
                    ),
                    "concurrency": args.concurrency,
                    "label_boundary": (
                        "worker receives ExtractionInput only; ground truth and metadata "
                        "are first used after baseline/shadow predictions and the decision "
                        "digest are frozen"
                    ),
                    "acceptance_inputs": (
                        "transformed source HTML, deterministic baseline candidate, "
                        "fixed refiner limits, and the fixed diagnostic acceptance rule"
                    ),
                    "acceptance_excludes": (
                        "ground truth, metadata, official metrics, page deltas, and "
                        "aggregate scores"
                    ),
                },
                "claimability": {
                    "claimable": False,
                    "universal_or_blind_sota_claimable": False,
                    "production_result": False,
                    "reasons": [
                        "development-only shadow diagnostic",
                        "public labels used after decisions for error analysis",
                        "refiner not wired into production",
                        "not a preregistered independent blind holdout",
                    ],
                },
            },
        )
        fine._atomic_json(output / "label_leak_guard.json", label_guard)  # noqa: SLF001

        summaries: dict[str, Any] = {}
        for mode in modes:
            summaries[mode] = _run_mode(
                mode=mode,
                dataset=dataset,
                output=output,
                offset=args.offset,
                limit=args.limit,
                concurrency=args.concurrency,
                calculator_type=calculator_type,
                official_cleaner=official_cleaner,
                extractor=extract_content,
                refiner=refine_deterministic_candidate_v2,
                scrubber=scrub_annotation_artifacts,
                acceptance_rule=args.acceptance_rule,
            )

        dataset_after = fine.verify_dataset(dataset)
        evaluator_after = fine.verify_evaluator(evaluator)
        source_after = _source_snapshot()
        summary = {
            "schema_version": SHADOW_SCHEMA_VERSION,
            "completed_at_utc": dt.datetime.now(dt.UTC).isoformat(),
            "claimable": False,
            "production_wiring_changed": False,
            "acceptance_used_labels_or_metrics": False,
            "dataset": {
                "before": dataset_before,
                "after": dataset_after,
                "stable": dataset_before == dataset_after,
            },
            "evaluator": {
                "before": evaluator_before,
                "after": evaluator_after,
                "stable": evaluator_before == evaluator_after,
            },
            "source": {
                "before": source_before,
                "after": source_after,
                "stable": (
                    source_before["shadow_source_digest"]
                    == source_after["shadow_source_digest"]
                ),
            },
            "label_leak_guard": label_guard,
            "modes": summaries,
        }
        fine._atomic_json(output / "summary.json", summary)  # noqa: SLF001
        fine._atomic_json(  # noqa: SLF001
            output / "manifest.json",
            fine._artifact_manifest(output),  # noqa: SLF001
        )
        for mode, mode_summary in summaries.items():
            delta = mode_summary["aggregate_delta"]
            print(
                f"{mode}: pages={mode_summary['pages']} "
                f"accepted={mode_summary['accepted_pages']} "
                f"baseline={mode_summary['baseline']['overall']:.6f} "
                f"shadow={mode_summary['shadow']['overall']:.6f} "
                f"delta={delta['overall']:+.6f}"
            )
        print("NOT CLAIMABLE: development-only label-free-decision shadow diagnostic")
        print(f"artifacts: {output}")
        return 0
    except Exception:
        if output.exists():
            fine._atomic_write(  # noqa: SLF001
                output / "NOT_CLAIMABLE.txt",
                (
                    NOT_CLAIMABLE_TEXT
                    + "\nRUN INCOMPLETE\n\n"
                    + "No score from this incomplete artifact may be reported.\n"
                ).encode(),
            )
        raise


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run_shadow(args)
    except fine.BenchmarkError as error:
        print(f"shadow diagnostic error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("shadow diagnostic interrupted; artifact remains NOT CLAIMABLE", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

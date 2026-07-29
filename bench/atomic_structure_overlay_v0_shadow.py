#!/usr/bin/env python3
"""Pinned 545-page audit for the unwired exact atomic overlay v0.

Decisions are made twice: once from the official-cleaned, annotation-bearing
HTML and once after the repository's full annotation scrubber.  Acceptance and
output bytes must agree across tracks.  Ground truth and official metrics are
used only after both decisions and replay receipts are frozen.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import math
import os
import platform
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.atomic_structure_overlay_v0 import (  # noqa: E402
    AtomicStructureOverlayDecisionV0,
    AtomicStructureOverlayReplayV0,
    AtomicStructureOverlayV0Config,
    propose_atomic_structure_overlay_v0,
    verify_atomic_structure_overlay_v0,
)
from bench import webmainbench_finegrained_benchmark as fine  # noqa: E402
from bench.webmainbench_benchmark import (  # noqa: E402
    scrub_annotation_artifacts,
)

SCHEMA_VERSION = "webmainbench.atomic-structure-overlay-v0-shadow.2"
EXPECTED_PAGES = 545
BASELINE_SHA256 = "3d4fefffb7d809b703934ce212602d7f52e7c6d1986f884b5b638f36a9b312af"
MODES = ("official", "scrubbed")
QUALITY_THRESHOLDS = {
    "overall": 0.01,
    "code_edit": 0.03,
    "table_TEDS": 0.02,
    "text_edit": 0.0,
    "formula_edit": 0.0,
}


@dataclass(frozen=True, slots=True)
class DecisionInput:
    dataset_index: int
    track_id: str
    official_html: str
    scrubbed_html: str
    scrub_counts: dict[str, int]
    baseline_prediction: str


@dataclass(frozen=True, slots=True)
class ScoringRecord:
    dataset_index: int
    track_id: str
    reference: str
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TrackObservation:
    decision: AtomicStructureOverlayDecisionV0
    replay: AtomicStructureOverlayReplayV0
    latency_seconds: float


@dataclass(frozen=True, slots=True)
class PageObservation:
    dataset_index: int
    track_id: str
    official: TrackObservation
    scrubbed: TrackObservation


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _hash_json(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_json(path: Path, value: object) -> None:
    _atomic_write(path, _json_bytes(value))


def _prepare_output(path: Path) -> Path:
    output = path.resolve()
    if output.exists() and any(output.iterdir()):
        raise fine.BenchmarkError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    return output


def _load_baseline(path: Path) -> dict[int, tuple[str, str]]:
    if not path.is_file() or _sha256(path) != BASELINE_SHA256:
        raise fine.BenchmarkError("fixed baseline SHA-256 mismatch")
    rows: dict[int, tuple[str, str]] = {}
    with path.open("rb") as handle:
        for line_number, line in enumerate(handle, start=1):
            row = json.loads(line)
            index = row.get("dataset_index")
            track_id = row.get("track_id")
            prediction = row.get("prediction")
            if (
                type(index) is not int
                or type(track_id) is not str
                or type(prediction) is not str
                or index in rows
            ):
                raise fine.BenchmarkError(f"invalid baseline row {line_number}")
            rows[index] = (track_id, prediction)
    if set(rows) != set(range(EXPECTED_PAGES)):
        raise fine.BenchmarkError("fixed baseline is not exactly 545 aligned rows")
    return rows


def _load_pages(
    dataset: Path,
    baseline: Path,
    official_cleaner: Callable[[str], str],
) -> tuple[tuple[DecisionInput, ...], tuple[ScoringRecord, ...]]:
    predictions = _load_baseline(baseline)
    decisions: list[DecisionInput] = []
    scoring: list[ScoringRecord] = []
    records = fine._iter_records(dataset, offset=0, limit=None)  # noqa: SLF001
    for record in records:
        baseline_row = predictions.get(record.dataset_index)
        if baseline_row is None or baseline_row[0] != record.track_id:
            raise fine.BenchmarkError(
                f"dataset/baseline mismatch at row {record.dataset_index}"
            )
        official_html = official_cleaner(record.html)
        scrubbed_html, scrub_counts = scrub_annotation_artifacts(official_html)
        decisions.append(
            DecisionInput(
                dataset_index=record.dataset_index,
                track_id=record.track_id,
                official_html=official_html,
                scrubbed_html=scrubbed_html,
                scrub_counts=scrub_counts,
                baseline_prediction=baseline_row[1],
            )
        )
        scoring.append(
            ScoringRecord(
                dataset_index=record.dataset_index,
                track_id=record.track_id,
                reference=record.reference,
                metadata=record.metadata,
            )
        )
    if len(decisions) != EXPECTED_PAGES or len(scoring) != EXPECTED_PAGES:
        raise fine.BenchmarkError("audit requires exactly 545 dataset pages")
    return tuple(decisions), tuple(scoring)


def _observe_track(
    html: str,
    prediction: str,
    config: AtomicStructureOverlayV0Config,
) -> TrackObservation:
    started = time.perf_counter()
    decision = propose_atomic_structure_overlay_v0(
        html,
        prediction,
        config=config,
    )
    replay = verify_atomic_structure_overlay_v0(
        html,
        prediction,
        decision,
        config=config,
    )
    return TrackObservation(
        decision=decision,
        replay=replay,
        latency_seconds=time.perf_counter() - started,
    )


def _observe_page(
    page: DecisionInput,
    config: AtomicStructureOverlayV0Config,
) -> PageObservation:
    # Only transformed source and the fixed baseline prediction cross this
    # boundary. Reference and metadata remain in the caller until scoring.
    official = _observe_track(
        page.official_html,
        page.baseline_prediction,
        config,
    )
    scrubbed = _observe_track(
        page.scrubbed_html,
        page.baseline_prediction,
        config,
    )
    return PageObservation(
        dataset_index=page.dataset_index,
        track_id=page.track_id,
        official=official,
        scrubbed=scrubbed,
    )


def _accepted_kinds(decision: AtomicStructureOverlayDecisionV0) -> tuple[str, ...]:
    return tuple(
        proposal.atom_kind for proposal in decision.proposals if proposal.accepted
    )


def _parity_key(observation: TrackObservation) -> tuple[bool, str, tuple[str, ...]]:
    return (
        observation.decision.accepted,
        observation.decision.output_markdown,
        _accepted_kinds(observation.decision),
    )


def _metric_payload(
    metrics: Mapping[str, fine.OfficialMetricResult],
) -> dict[str, dict[str, Any]]:
    return {
        name: fine._metric_dict(result)  # noqa: SLF001
        for name, result in metrics.items()
    }


def _aggregate_delta(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "overall": float(candidate["overall"]) - float(baseline["overall"]),
        "metrics": {
            name: {
                "score": (
                    float(candidate["metrics"][name]["score"])
                    - float(baseline["metrics"][name]["score"])
                ),
                "successful_pages": (
                    int(candidate["metrics"][name]["successful_pages"])
                    - int(baseline["metrics"][name]["successful_pages"])
                ),
                "failed_pages": (
                    int(candidate["metrics"][name]["failed_pages"])
                    - int(baseline["metrics"][name]["failed_pages"])
                ),
            }
            for name in fine.CORE_METRICS
        },
    }


def _score(
    decision_inputs: tuple[DecisionInput, ...],
    scoring_records: tuple[ScoringRecord, ...],
    observations: tuple[PageObservation, ...],
    calculator_type: type[fine.OfficialMetricCalculator],
    output: Path,
) -> tuple[
    list[dict[str, fine.OfficialMetricResult]],
    list[dict[str, fine.OfficialMetricResult]],
    dict[str, Any],
    dict[str, Any],
]:
    baseline_calculator = calculator_type(
        {
            "use_llm": False,
            "cache_dir": str(output / ".baseline_metric_cache"),
        }
    )
    candidate_calculator = calculator_type(
        {
            "use_llm": False,
            "cache_dir": str(output / ".candidate_metric_cache"),
        }
    )
    baseline_results: list[dict[str, fine.OfficialMetricResult]] = []
    candidate_results: list[dict[str, fine.OfficialMetricResult]] = []
    for decision_input, scoring_record, observation in zip(
        decision_inputs,
        scoring_records,
        observations,
        strict=True,
    ):
        # This is the first label access after both decision records and replay
        # receipts have been frozen and cross-track parity has been checked.
        baseline_results.append(
            baseline_calculator.calculate_all(
                predicted_content=decision_input.baseline_prediction,
                groundtruth_content=scoring_record.reference,
                predicted_content_list=None,
                groundtruth_content_list=None,
            )
        )
        candidate_results.append(
            candidate_calculator.calculate_all(
                predicted_content=observation.scrubbed.decision.output_markdown,
                groundtruth_content=scoring_record.reference,
                predicted_content_list=None,
                groundtruth_content_list=None,
            )
        )
    return (
        baseline_results,
        candidate_results,
        fine._official_aggregate(  # noqa: SLF001
            baseline_calculator,
            baseline_results,
        ),
        fine._official_aggregate(  # noqa: SLF001
            candidate_calculator,
            candidate_results,
        ),
    )


def _proposal_payload(decision: AtomicStructureOverlayDecisionV0) -> list[dict[str, Any]]:
    return [
        {
            "proposal_id": proposal.proposal_id,
            "atom_kind": proposal.atom_kind,
            "accepted": proposal.accepted,
            "reason": proposal.reason,
            "selected_id": proposal.selected_id,
            "source_span": [proposal.source_span_start, proposal.source_span_end],
            "candidate_span": [
                proposal.candidate_span_start,
                proposal.candidate_span_end,
            ],
            "replacement_digest": proposal.replacement_digest,
            "patch_digest": proposal.patch_digest,
            "certificate_digest": proposal.certificate_digest,
            "visible_token_digest": proposal.visible_token_digest,
        }
        for proposal in decision.proposals
    ]


def _track_summary(
    mode: str,
    observations: tuple[PageObservation, ...],
    baseline_aggregate: Mapping[str, Any],
    candidate_aggregate: Mapping[str, Any],
    delta: Mapping[str, Any],
    pages_path: Path,
) -> dict[str, Any]:
    decisions = [
        observation.official.decision
        if mode == "official"
        else observation.scrubbed.decision
        for observation in observations
    ]
    replays = [
        observation.official.replay
        if mode == "official"
        else observation.scrubbed.replay
        for observation in observations
    ]
    latencies = [
        observation.official.latency_seconds
        if mode == "official"
        else observation.scrubbed.latency_seconds
        for observation in observations
    ]
    decision_reasons = Counter(decision.reason for decision in decisions)
    proposal_reasons: Counter[str] = Counter()
    accepted_kinds: Counter[str] = Counter()
    for decision in decisions:
        for proposal in decision.proposals:
            proposal_reasons[proposal.reason] += 1
            if proposal.accepted:
                accepted_kinds[proposal.atom_kind] += 1
    replay_failures = sum(
        not replay.verified
        or replay.output_markdown != decision.output_markdown
        for decision, replay in zip(decisions, replays, strict=True)
    )
    visible_token_failures = sum(
        not decision.visible_tokens_identical for decision in decisions
    )
    fallback_identity_failures = sum(
        not decision.accepted
        and decision.output_markdown != decision.candidate_markdown
        for decision in decisions
    )
    return {
        "mode": mode,
        "source_track": (
            "verified official cleaner; cc-select annotations may remain"
            if mode == "official"
            else "official cleaner followed by full annotation scrubber postcondition"
        ),
        "pages": len(decisions),
        "accepted_pages": sum(decision.accepted for decision in decisions),
        "accepted_proposals": sum(accepted_kinds.values()),
        "accepted_kinds": dict(sorted(accepted_kinds.items())),
        "decision_reasons": dict(sorted(decision_reasons.items())),
        "proposal_reasons": dict(sorted(proposal_reasons.items())),
        "audit_failures": {
            "replay": replay_failures,
            "visible_token_identity": visible_token_failures,
            "fallback_byte_identity": fallback_identity_failures,
        },
        "baseline": baseline_aggregate,
        "candidate": candidate_aggregate,
        "aggregate_delta": delta,
        "quality_shared_across_tracks": True,
        "timing": fine._latency_summary(latencies),  # noqa: SLF001
        "decision_set_digest": _hash_json(
            [decision.decision_digest for decision in decisions]
        ),
        "pages_sha256": _sha256(pages_path),
    }


def _quality_gates(
    mode_summaries: Mapping[str, Mapping[str, Any]],
    delta: Mapping[str, Any],
    parity_failures: int,
    decision_wall_seconds: float,
    maximum_decision_wall_seconds: float,
) -> dict[str, Any]:
    checks = {
        "exactly_545_pages_each_track": all(
            int(summary["pages"]) == EXPECTED_PAGES
            for summary in mode_summaries.values()
        ),
        "cross_track_acceptance_output_kind_parity": parity_failures == 0,
        "replay_100_percent": all(
            int(summary["audit_failures"]["replay"]) == 0
            for summary in mode_summaries.values()
        ),
        "global_visible_token_identity_100_percent": all(
            int(summary["audit_failures"]["visible_token_identity"]) == 0
            for summary in mode_summaries.values()
        ),
        "fallback_byte_identity_100_percent": all(
            int(summary["audit_failures"]["fallback_byte_identity"]) == 0
            for summary in mode_summaries.values()
        ),
        "nonzero_code_coverage": all(
            int(summary["accepted_kinds"].get("code", 0)) > 0
            for summary in mode_summaries.values()
        ),
        "nonzero_table_coverage": all(
            int(summary["accepted_kinds"].get("table", 0)) > 0
            for summary in mode_summaries.values()
        ),
        "decision_and_replay_wall_within_budget": (
            decision_wall_seconds <= maximum_decision_wall_seconds
        ),
        "overall_delta_at_least_0_01": (
            float(delta["overall"]) >= QUALITY_THRESHOLDS["overall"]
        ),
        "code_edit_delta_at_least_0_03": (
            float(delta["metrics"]["code_edit"]["score"])
            >= QUALITY_THRESHOLDS["code_edit"]
        ),
        "table_TEDS_delta_at_least_0_02": (
            float(delta["metrics"]["table_TEDS"]["score"])
            >= QUALITY_THRESHOLDS["table_TEDS"]
        ),
        "text_edit_non_regression": (
            float(delta["metrics"]["text_edit"]["score"])
            >= QUALITY_THRESHOLDS["text_edit"]
        ),
        "formula_edit_non_regression": (
            float(delta["metrics"]["formula_edit"]["score"])
            >= QUALITY_THRESHOLDS["formula_edit"]
        ),
    }
    return {
        "thresholds": QUALITY_THRESHOLDS,
        "performance_thresholds": {
            "maximum_dual_track_decision_and_replay_wall_seconds": (
                maximum_decision_wall_seconds
            ),
            "observed_dual_track_decision_and_replay_wall_seconds": (
                decision_wall_seconds
            ),
            "scope": (
                "545 pages, two source tracks, one decision plus one independent "
                "replay per track"
            ),
        },
        "checks": checks,
        "passed": all(checks.values()),
    }


def _artifact_manifest(output: Path) -> dict[str, dict[str, Any]]:
    manifest: dict[str, dict[str, Any]] = {}
    for path in sorted(output.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        relative = path.relative_to(output).as_posix()
        manifest[relative] = {
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
    return manifest


def run_audit(args: argparse.Namespace) -> dict[str, Any]:
    if args.concurrency <= 0 or args.concurrency > 8:
        raise fine.BenchmarkError("concurrency must be in [1, 8]")
    if (
        not math.isfinite(args.max_decision_wall_seconds)
        or args.max_decision_wall_seconds <= 0
    ):
        raise fine.BenchmarkError("max decision wall seconds must be positive")
    output = _prepare_output(args.output_dir)
    dataset = args.dataset.resolve()
    baseline = args.baseline.resolve()
    evaluator = args.evaluator_root.resolve()
    dataset_before = fine.verify_dataset(dataset)
    evaluator_before = fine.verify_evaluator(evaluator)
    calculator_type, official_cleaner, dependencies = fine.load_official_toolkit(
        evaluator
    )
    source_paths = {
        "module": ROOT / "app/services/atomic_structure_overlay_v0.py",
        "native_certificate": (
            ROOT / "native/src/document_ir_v2/selection_certificate_v0.rs"
        ),
        "runner": Path(__file__).resolve(),
    }
    source_before = {name: _sha256(path) for name, path in source_paths.items()}
    _atomic_json(
        output / "run_config.json",
        {
            "schema_version": SCHEMA_VERSION,
            "created_at_utc": dt.datetime.now(dt.UTC).isoformat(),
            "dataset": dataset_before,
            "fixed_baseline": {
                "path": str(baseline),
                "sha256": _sha256(baseline),
            },
            "evaluator": evaluator_before,
            "dependencies": dependencies,
            "python": sys.version,
            "platform": platform.platform(),
            "protocol": {
                "default_off": True,
                "production_wiring_changed": False,
                "modes": list(MODES),
                "concurrency": args.concurrency,
                "maximum_decision_and_replay_wall_seconds": (
                    args.max_decision_wall_seconds
                ),
                "use_llm": False,
                "model_calls": False,
                "paid_calls": False,
                "vendor_outputs_used": False,
                "decision_inputs": (
                    "transformed HTML, fixed baseline prediction, fixed overlay config"
                ),
                "decision_excludes": (
                    "ground truth, metadata, official metrics, vendor outputs, models"
                ),
                "official_track_annotation_status": (
                    "official cleaner is label-bearing because cc-select may remain"
                ),
                "scrubbed_track_annotation_status": (
                    "full scrubber postcondition removes known annotation signals"
                ),
            },
            "source_sha256": source_before,
        },
    )

    decision_inputs, scoring_records = _load_pages(
        dataset,
        baseline,
        official_cleaner,
    )
    config = AtomicStructureOverlayV0Config(enabled=True)
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=args.concurrency
    ) as pool:
        observations = tuple(
            pool.map(
                lambda decision_input: _observe_page(decision_input, config),
                decision_inputs,
            )
        )
    decision_seconds = time.perf_counter() - started
    parity_failures = [
        observation.dataset_index
        for observation in observations
        if _parity_key(observation.official) != _parity_key(observation.scrubbed)
    ]
    if parity_failures:
        raise fine.BenchmarkError(
            "official/scrubbed decision parity failed at pages "
            + ",".join(str(index) for index in parity_failures[:20])
        )

    score_started = time.perf_counter()
    (
        baseline_results,
        candidate_results,
        baseline_aggregate,
        candidate_aggregate,
    ) = _score(
        decision_inputs,
        scoring_records,
        observations,
        calculator_type,
        output,
    )
    scoring_seconds = time.perf_counter() - score_started
    delta = _aggregate_delta(baseline_aggregate, candidate_aggregate)

    for mode in MODES:
        mode_dir = output / mode
        mode_dir.mkdir()
        pages_path = mode_dir / "pages.jsonl"
        with pages_path.open("wb") as handle:
            for (
                decision_input,
                scoring_record,
                observation,
                baseline_metrics,
                candidate_metrics,
            ) in zip(
                decision_inputs,
                scoring_records,
                observations,
                baseline_results,
                candidate_results,
                strict=True,
            ):
                track = (
                    observation.official
                    if mode == "official"
                    else observation.scrubbed
                )
                row = {
                    "dataset_index": decision_input.dataset_index,
                    "track_id": decision_input.track_id,
                    "metadata": scoring_record.metadata,
                    "reference_sha256": _sha256_text(scoring_record.reference),
                    "baseline_prediction": {
                        "sha256": _sha256_text(
                            decision_input.baseline_prediction
                        ),
                        "chars": len(decision_input.baseline_prediction),
                    },
                    "prediction": track.decision.output_markdown,
                    "prediction_sha256": _sha256_text(
                        track.decision.output_markdown
                    ),
                    "decision": {
                        "accepted": track.decision.accepted,
                        "reason": track.decision.reason,
                        "decision_digest": track.decision.decision_digest,
                        "source_digest": track.decision.source_digest,
                        "input_digest": track.decision.input_digest,
                        "output_digest": track.decision.output_digest,
                        "config_digest": track.decision.config_digest,
                        "visible_tokens_identical": (
                            track.decision.visible_tokens_identical
                        ),
                        "proposals": _proposal_payload(track.decision),
                    },
                    "replay": {
                        "verified": track.replay.verified,
                        "reason": track.replay.reason,
                        "decision_digest": track.replay.decision_digest,
                        "output_digest": track.replay.output_digest,
                    },
                    "input_transform": {
                        "mode": mode,
                        "scrub_counts": (
                            decision_input.scrub_counts
                            if mode == "scrubbed"
                            else None
                        ),
                    },
                    "label_access": "after_dual_decision_replay_and_parity",
                    "baseline_official_metrics": _metric_payload(
                        baseline_metrics
                    ),
                    "candidate_official_metrics": _metric_payload(
                        candidate_metrics
                    ),
                }
                handle.write(_json_bytes(row))
            handle.flush()
            os.fsync(handle.fileno())

    mode_summaries = {
        mode: _track_summary(
            mode,
            observations,
            baseline_aggregate,
            candidate_aggregate,
            delta,
            output / mode / "pages.jsonl",
        )
        for mode in MODES
    }
    gates = _quality_gates(
        mode_summaries,
        delta,
        len(parity_failures),
        decision_seconds,
        args.max_decision_wall_seconds,
    )
    for mode, mode_summary in mode_summaries.items():
        _atomic_json(output / mode / "summary.json", mode_summary)

    dataset_after = fine.verify_dataset(dataset)
    evaluator_after = fine.verify_evaluator(evaluator)
    source_after = {name: _sha256(path) for name, path in source_paths.items()}
    summary = {
        "schema_version": SCHEMA_VERSION,
        "completed_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "claimable_sota_or_vendor_evidence": False,
        "production_wiring_changed": False,
        "go_for_545_shadow": gates["passed"],
        "go_for_production": False,
        "decision_used_labels_or_metrics": False,
        "official_source_track_is_label_free": False,
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
        "fixed_baseline": {
            "path": str(baseline),
            "sha256": _sha256(baseline),
            "stable": _sha256(baseline) == BASELINE_SHA256,
        },
        "source": {
            "before": source_before,
            "after": source_after,
            "stable": source_before == source_after,
        },
        "cross_track": {
            "parity_definition": (
                "accepted flag, exact output bytes, accepted proposal-kind sequence"
            ),
            "parity_failures": parity_failures,
            "quality_scored_once_after_exact_prediction_parity": True,
        },
        "modes": mode_summaries,
        "quality_gates": gates,
        "timing": {
            "decision_and_replay_wall_seconds": decision_seconds,
            "official_scoring_wall_seconds": scoring_seconds,
            "total_wall_seconds": decision_seconds + scoring_seconds,
        },
        "limitations": [
            "development-only unwired shadow component",
            "public benchmark labels score frozen decisions",
            "not an independent blind holdout",
            "no SOTA or vendor-comparison claim is supported",
        ],
    }
    if not all(
        math.isfinite(float(delta["metrics"][name]["score"]))
        for name in fine.CORE_METRICS
    ):
        raise fine.BenchmarkError("quality delta is non-finite")
    _atomic_json(output / "summary.json", summary)
    _atomic_json(output / "manifest.json", _artifact_manifest(output))
    return summary


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--evaluator-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--max-decision-wall-seconds",
        type=float,
        default=180.0,
        help=(
            "Dedicated-host wall budget for 545 pages, two tracks, and "
            "decision plus independent replay"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = run_audit(args)
    except fine.BenchmarkError as error:
        print(f"atomic overlay audit error: {error}", file=sys.stderr)
        return 2
    gates = summary["quality_gates"]
    delta = summary["modes"]["scrubbed"]["aggregate_delta"]
    print(
        "545-page audit "
        f"go={summary['go_for_545_shadow']} "
        f"accepted={summary['modes']['scrubbed']['accepted_pages']} "
        f"overall={delta['overall']:+.6f} "
        f"code={delta['metrics']['code_edit']['score']:+.6f} "
        f"table_TEDS={delta['metrics']['table_TEDS']['score']:+.6f}"
    )
    print(f"artifacts: {args.output_dir.resolve()}")
    return 0 if gates["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

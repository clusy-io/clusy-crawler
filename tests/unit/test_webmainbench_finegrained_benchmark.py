from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

from bench import webmainbench_finegrained_benchmark as benchmark

if TYPE_CHECKING:
    from pathlib import Path


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> bytes:
    content = b"".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        for row in rows
    )
    path.write_bytes(content)
    return content


def _dataset_row(identifier: str = "page-1") -> dict[str, Any]:
    return {
        "track_id": identifier,
        "url": "https://example.test/article",
        "html": "<main cc-select='true'>Body</main>",
        "groundtruth_content": "Body",
        "meta": {
            "language": "en",
            "style": "Article",
            "level": "simple",
            "table": [],
            "code": [],
            "equation": [],
        },
    }


def test_verify_dataset_checks_exact_hash_size_count_and_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "fine.jsonl"
    content = _write_jsonl(path, [_dataset_row()])
    monkeypatch.setattr(benchmark, "DATASET_BYTES", len(content))
    monkeypatch.setattr(benchmark, "DATASET_SHA256", hashlib.sha256(content).hexdigest())
    monkeypatch.setattr(benchmark, "DATASET_RECORDS", 1)

    result = benchmark.verify_dataset(path)

    assert result["records"] == 1
    assert result["sha256"] == hashlib.sha256(content).hexdigest()


def test_verify_dataset_rejects_duplicate_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "fine.jsonl"
    content = _write_jsonl(path, [_dataset_row(), _dataset_row()])
    monkeypatch.setattr(benchmark, "DATASET_BYTES", len(content))
    monkeypatch.setattr(benchmark, "DATASET_SHA256", hashlib.sha256(content).hexdigest())
    monkeypatch.setattr(benchmark, "DATASET_RECORDS", 2)

    with pytest.raises(benchmark.BenchmarkError, match="duplicate track_id"):
        benchmark.verify_dataset(path)


@dataclass
class _ExtractionResult:
    text: str
    strategy: str = "native"


def test_extraction_boundary_never_passes_reference_or_metadata() -> None:
    calls: list[tuple[str, str, str]] = []

    def extractor(html: str, url: str, *, extraction_profile: str) -> _ExtractionResult:
        calls.append((html, url, extraction_profile))
        return _ExtractionResult("prediction")

    item = benchmark.ExtractionInput(
        dataset_index=0,
        track_id="secret-id",
        url="https://example.test/page",
        html="<p>input only</p>",
    )
    observation = benchmark.extract_one(
        item,
        mode="official",
        extractor=extractor,
        official_cleaner=lambda html: html.replace("input", "clean"),
        scrubber=lambda html: (html, {}),
    )

    assert calls == [
        ("<p>clean only</p>", "https://example.test/page", benchmark.EXTRACTION_PROFILE)
    ]
    assert observation.prediction == "prediction"
    assert observation.error_type is None


def test_scrubbed_mode_uses_only_scrubbed_html() -> None:
    seen: list[str] = []

    def extractor(html: str, url: str, *, extraction_profile: str) -> _ExtractionResult:
        seen.append(html)
        return _ExtractionResult("ok")

    item = benchmark.ExtractionInput(0, "id", "https://example.test", "<p marker>x</p>")
    observation = benchmark.extract_one(
        item,
        mode="scrubbed",
        extractor=extractor,
        official_cleaner=lambda html: html,
        scrubber=lambda html: ("<p>x</p>", {"marker": 1}),
    )

    assert seen == ["<p>x</p>"]
    assert observation.transform_counts == {"marker": 1}


class _Metric:
    def __init__(self, score: float, *, success: bool = True) -> None:
        self.score = score
        self.success = success
        self.details: dict[str, Any] = {}
        self.error_message = None

    def to_dict(self) -> dict[str, Any]:
        return {}


class _Calculator:
    def aggregate_results(
        self,
        batch_results: list[dict[str, benchmark.OfficialMetricResult]],
    ) -> dict[str, _Metric]:
        assert len(batch_results) == 1
        return {
            name: _Metric(
                score,
            )
            for name, score in zip(
                benchmark.CORE_METRICS,
                (0.9, 0.8, 0.7, 0.6, 0.5),
                strict=True,
            )
        }


def test_aggregate_uses_official_per_metric_results_and_official_composition() -> None:
    aggregate = benchmark._official_aggregate(  # noqa: SLF001
        _Calculator(),  # type: ignore[arg-type]
        [{"text_edit": _Metric(1.0)}],  # type: ignore[dict-item]
    )

    assert aggregate["metrics"]["text_edit"]["score"] == 0.9
    assert aggregate["overall"] == pytest.approx(0.7)


def test_claimability_fails_closed_for_dirty_limited_or_partial_run() -> None:
    args = argparse.Namespace(offset=0, limit=8)
    summaries = {
        "official": {
            "pages": 8,
            "extraction_errors": 0,
        }
    }
    result = benchmark._claimability(  # noqa: SLF001
        args=args,
        modes=["official"],
        mode_summaries=summaries,
        source_before={"git_dirty": True, "source_digest": "same"},
        source_after={"git_dirty": True, "source_digest": "same"},
        dataset_stable=True,
        evaluator_stable=True,
        label_guard={"passed": True},
    )

    assert not result["claimable_on_fixed_public_offline_protocol"]
    assert not result["leaderboard_comparable"]
    assert any("limited" in reason for reason in result["reasons"])
    assert any("dirty" in reason for reason in result["reasons"])
    assert any("both" in reason for reason in result["reasons"])


def test_dependency_verifier_rejects_missing_or_drifted_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(benchmark, "EVALUATOR_DEPENDENCIES", {"apted": "1.0.3"})
    monkeypatch.setattr(importlib.metadata, "version", lambda package: "9.9.9")

    with pytest.raises(benchmark.BenchmarkError, match="dependency mismatch"):
        benchmark.verify_dependencies()


def test_run_failure_creates_nonclaimable_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "artifact"
    args = argparse.Namespace(
        dataset=tmp_path / "missing.jsonl",
        evaluator_root=tmp_path / "missing-evaluator",
        output_dir=output,
        mode="both",
        offset=0,
        limit=None,
        concurrency=1,
    )
    monkeypatch.setattr(benchmark, "DATASET_RECORDS", 545)

    with pytest.raises(benchmark.BenchmarkError):
        benchmark.run_benchmark(args)

    marker = (output / "NOT_CLAIMABLE.txt").read_text()
    assert "NOT CLAIMABLE" in marker
    assert "No score" in marker

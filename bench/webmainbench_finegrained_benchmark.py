#!/usr/bin/env python3
"""WebMainBench 545-page fine-grained production-extractor benchmark.

The dataset and the complete official evaluator Git tree are accepted only at
the immutable revisions pinned below.  Metric implementations are imported
from that verified checkout; this runner does not copy or reimplement them.

The official toolkit supports optional LLM-assisted formula splitting.  This
runner deliberately fixes ``use_llm=False`` so it is deterministic, makes no
paid calls, and never downloads model weights.  Its results are therefore
scoped to the official offline/regex protocol, not silently equated with
published rows whose LLM splitter provenance is not fully disclosed.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import importlib
import importlib.metadata
import json
import math
import os
import platform
import subprocess
import sys
import time
import types
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DATASET_REPOSITORY = "https://huggingface.co/datasets/opendatalab/WebMainBench"
DATASET_REVISION = "5da0972e9b58d0c7891ae75053ced97c268f52e3"
DATASET_FILENAME = "WebMainBench_545.jsonl"
DATASET_BYTES = 109_097_918
DATASET_SHA256 = "0efaa4b49a45e320a27fe6e5a0b6aad5b57259fc3321ac3448519cacc74c537e"
DATASET_RECORDS = 545

EVALUATOR_REPOSITORY = "https://github.com/opendatalab/WebMainBench.git"
EVALUATOR_COMMIT = "9d991bdc00c57b57521499494d96be85c31317ba"
EVALUATOR_TREE = "c7e3cb66a5318e8cc4bec52dcc511f06e000717c"
EVALUATOR_LICENSE_SHA256 = "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"
EVALUATOR_FILES = {
    "webmainbench/metrics/calculator.py": (
        "97d1c3b185d3a861d3d516ba6e513a043f6772e4ea25a7ae1e93dda2a42922c3"
    ),
    "webmainbench/metrics/base.py": (
        "3ffe6da1a3020e37a9fdb93c9a3897c2943020ae7db181916aa73eac1276caeb"
    ),
    "webmainbench/metrics/base_content_splitter.py": (
        "bd0526d7bd65dcf69acb7a6c2c1faa6400d212d4d3c301b4a69188af3289e105"
    ),
    "webmainbench/metrics/text_metrics.py": (
        "d3df96d8a065d9f99ba8cf25d745826bf3dbf9a5c48676d2f3f698a77707692b"
    ),
    "webmainbench/metrics/formula_metrics.py": (
        "0f3337a862d054ba57a7c9e932972857e174250294bbd7e3ed4466b557d506a8"
    ),
    "webmainbench/metrics/table_metrics.py": (
        "88138f8571f925e8fee4462721a637dd0687e84e64f8230d31a2a4cb0f928e6d"
    ),
    "webmainbench/metrics/teds_metrics.py": (
        "3c16cf1db20fc450e28f3b3e03363ecd6d6bc976aef22579f6ac196ba1f656b0"
    ),
    "webmainbench/metrics/code_extractor.py": (
        "73f79e037a3c5538268d9136c66a2721fa6518d6586ab276c465712c7d96d2a6"
    ),
    "webmainbench/metrics/formula_extractor.py": (
        "47180830ef1ad2f2c5000848ecd6cfc7179ea4bcc5575d517e3f7d859d13bd8b"
    ),
    "webmainbench/metrics/table_extractor.py": (
        "17b0ea25928eba045c80da2136194db2c8bf074bded21047e5d7ef7cebf65fc2"
    ),
    "webmainbench/config.py": (
        "618a9c6d0ad1e6430ba216b6806f67396bc1b5fdd3e630de67aad3d22f144521"
    ),
    "webmainbench/utils/html_cleaner.py": (
        "8ee565538c7fe9b286140df5fa776868e17c4be6a6779a94eeed18a2d594bd2c"
    ),
}

# Exact versions define the deterministic Clusy protocol because the official
# repository specifies dependency ranges rather than a lock file.
EVALUATOR_DEPENDENCIES = {
    "apted": "1.0.3",
    "beautifulsoup4": "4.14.3",
    "jieba": "0.42.1",
    "openai": "2.49.0",
    "python-dotenv": "1.2.2",
    "rapidfuzz": "3.14.3",
}

EXTRACTION_PROFILE = "balanced"
MODES = ("official", "scrubbed")
CORE_METRICS = (
    "text_edit",
    "code_edit",
    "formula_edit",
    "table_edit",
    "table_TEDS",
)
BREAKDOWN_FIELDS = ("language", "style", "level", "table", "code", "equation")
SOURCE_FIXED_FILES = (
    "bench/webmainbench_finegrained_benchmark.py",
    "bench/WEBMAINBENCH_FINEGRAINED_BENCHMARK.md",
    "app/config.py",
    "app/services/extractor.py",
    "pyproject.toml",
    "uv.lock",
    "native/Cargo.toml",
    "native/Cargo.lock",
)
SOURCE_GLOBS = (
    ("app", "*.py"),
    ("native/src", "*.rs"),
    ("native/python", "*.py"),
)


class BenchmarkError(RuntimeError):
    """An integrity, provenance, or protocol condition failed closed."""


class OfficialMetricResult(Protocol):
    score: float
    success: bool
    details: dict[str, Any]
    error_message: str | None

    def to_dict(self) -> dict[str, Any]: ...


class OfficialMetricCalculator(Protocol):
    def __init__(self, config: dict[str, Any] | None = None) -> None: ...

    def calculate_all(
        self,
        predicted_content: str,
        groundtruth_content: str,
        predicted_content_list: list[dict[str, Any]] | None = None,
        groundtruth_content_list: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> dict[str, OfficialMetricResult]: ...

    def aggregate_results(
        self,
        batch_results: list[dict[str, OfficialMetricResult]],
    ) -> dict[str, OfficialMetricResult]: ...


@dataclass(frozen=True)
class DatasetRecord:
    dataset_index: int
    track_id: str
    url: str
    html: str
    reference: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ExtractionInput:
    """The only values allowed to cross into production extraction."""

    dataset_index: int
    track_id: str
    url: str
    html: str


@dataclass(frozen=True)
class ExtractionObservation:
    prediction: str
    latency_seconds: float
    strategy: str
    error_type: str | None
    error_message: str | None
    transform_counts: dict[str, int] | None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
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


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_write(path, _json_bytes(value))


def _run(command: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )


def verify_dataset(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise BenchmarkError(f"dataset does not exist: {path}")
    size = path.stat().st_size
    digest = _sha256(path)
    if size != DATASET_BYTES:
        raise BenchmarkError(f"dataset byte size mismatch: expected {DATASET_BYTES}, got {size}")
    if digest != DATASET_SHA256:
        raise BenchmarkError(
            f"dataset SHA-256 mismatch: expected {DATASET_SHA256}, got {digest}"
        )

    count = 0
    identifiers: set[str] = set()
    with path.open("rb") as handle:
        for count, line in enumerate(handle, start=1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise BenchmarkError(f"dataset line {count} is invalid JSON") from error
            if not isinstance(row, dict):
                raise BenchmarkError(f"dataset line {count} is not an object")
            for field in ("track_id", "url", "html", "groundtruth_content", "meta"):
                if field not in row:
                    raise BenchmarkError(f"dataset line {count} is missing {field}")
            track_id = row["track_id"]
            if not isinstance(track_id, str) or not track_id:
                raise BenchmarkError(f"dataset line {count} has invalid track_id")
            if track_id in identifiers:
                raise BenchmarkError(f"dataset contains duplicate track_id: {track_id}")
            identifiers.add(track_id)
            if not isinstance(row["url"], str) or not isinstance(row["html"], str):
                raise BenchmarkError(f"dataset line {count} has invalid extractor input")
            if not isinstance(row["groundtruth_content"], str):
                raise BenchmarkError(f"dataset line {count} has invalid reference")
            if not isinstance(row["meta"], dict):
                raise BenchmarkError(f"dataset line {count} has invalid metadata")
    if count != DATASET_RECORDS:
        raise BenchmarkError(
            f"dataset record count mismatch: expected {DATASET_RECORDS}, got {count}"
        )
    return {
        "repository": DATASET_REPOSITORY,
        "revision": DATASET_REVISION,
        "filename": DATASET_FILENAME,
        "path": str(path),
        "bytes": size,
        "sha256": digest,
        "lfs_oid_sha256": digest,
        "records": count,
    }


def verify_evaluator(root: Path) -> dict[str, Any]:
    if not (root / ".git").exists():
        raise BenchmarkError(f"official evaluator is not a Git checkout: {root}")
    commit_result = _run(["git", "rev-parse", "HEAD"], cwd=root)
    tree_result = _run(["git", "rev-parse", "HEAD^{tree}"], cwd=root)
    diff_result = _run(["git", "diff-index", "--quiet", "HEAD", "--"], cwd=root)
    origin_result = _run(["git", "remote", "get-url", "origin"], cwd=root)
    if commit_result.returncode != 0 or commit_result.stdout.strip() != EVALUATOR_COMMIT:
        raise BenchmarkError("official evaluator commit mismatch")
    if tree_result.returncode != 0 or tree_result.stdout.strip() != EVALUATOR_TREE:
        raise BenchmarkError("official evaluator Git tree mismatch")
    if diff_result.returncode != 0:
        raise BenchmarkError("official evaluator has modified tracked files")
    origin = origin_result.stdout.strip()
    if origin_result.returncode != 0 or origin.rstrip("/") != EVALUATOR_REPOSITORY.rstrip("/"):
        raise BenchmarkError(f"official evaluator origin mismatch: {origin!r}")
    license_path = root / "LICENSE"
    if not license_path.is_file() or _sha256(license_path) != EVALUATOR_LICENSE_SHA256:
        raise BenchmarkError("official evaluator license mismatch")

    file_hashes: dict[str, str] = {}
    for relative, expected in EVALUATOR_FILES.items():
        path = root / relative
        if not path.is_file():
            raise BenchmarkError(f"official evaluator file missing: {relative}")
        actual = _sha256(path)
        if actual != expected:
            raise BenchmarkError(f"official evaluator file hash mismatch: {relative}")
        file_hashes[relative] = actual
    return {
        "repository": EVALUATOR_REPOSITORY,
        "path": str(root),
        "commit": EVALUATOR_COMMIT,
        "tree": EVALUATOR_TREE,
        "origin": origin,
        "tracked_files_clean": True,
        "license_sha256": _sha256(license_path),
        "file_sha256": file_hashes,
    }


def verify_dependencies() -> dict[str, str]:
    versions: dict[str, str] = {}
    for package, expected in EVALUATOR_DEPENDENCIES.items():
        try:
            actual = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError as error:
            raise BenchmarkError(
                f"official evaluator dependency is missing: {package}=={expected}"
            ) from error
        if actual != expected:
            raise BenchmarkError(
                f"official evaluator dependency mismatch: {package}=={actual}, "
                f"expected {expected}"
            )
        versions[package] = actual
    return versions


def _namespace_package(name: str, path: Path) -> None:
    package = types.ModuleType(name)
    package.__path__ = [str(path)]
    package.__package__ = name
    sys.modules[name] = package


def load_official_toolkit(
    evaluator_root: Path,
) -> tuple[type[OfficialMetricCalculator], Callable[[str], str], dict[str, str]]:
    """Import exact scorer modules without executing the toolkit's extractor registry."""

    verify_evaluator(evaluator_root)
    dependencies = verify_dependencies()
    namespace = f"_clusy_official_webmainbench_{EVALUATOR_COMMIT[:12]}"
    _namespace_package(namespace, evaluator_root / "webmainbench")
    _namespace_package(f"{namespace}.metrics", evaluator_root / "webmainbench" / "metrics")
    _namespace_package(f"{namespace}.utils", evaluator_root / "webmainbench" / "utils")
    calculator_module = importlib.import_module(f"{namespace}.metrics.calculator")
    cleaner_module = importlib.import_module(f"{namespace}.utils.html_cleaner")
    calculator = getattr(calculator_module, "MetricCalculator", None)
    cleaner = getattr(cleaner_module, "clean_browser_annotation_artifacts", None)
    if not isinstance(calculator, type) or not callable(cleaner):
        raise BenchmarkError("verified official toolkit does not expose expected scorer/cleaner")
    calculator_file = calculator_module.__file__
    cleaner_file = cleaner_module.__file__
    if calculator_file is None or cleaner_file is None:
        raise BenchmarkError("official toolkit modules have no filesystem origin")
    origins = {
        "calculator": str(Path(calculator_file).resolve()),
        "cleaner": str(Path(cleaner_file).resolve()),
    }
    evaluator_resolved = evaluator_root.resolve()
    if any(
        evaluator_resolved not in Path(origin).parents
        for origin in origins.values()
    ):
        raise BenchmarkError("official toolkit import escaped the verified checkout")
    return calculator, cleaner, dependencies


def _source_paths() -> list[Path]:
    paths = {ROOT / relative for relative in SOURCE_FIXED_FILES if (ROOT / relative).is_file()}
    for base_relative, pattern in SOURCE_GLOBS:
        base = ROOT / base_relative
        if base.is_dir():
            paths.update(path for path in base.rglob(pattern) if path.is_file())
    return sorted(paths)


def source_provenance() -> dict[str, Any]:
    commit = _run(["git", "rev-parse", "HEAD"], cwd=ROOT)
    status = _run(["git", "status", "--porcelain"], cwd=ROOT)
    hashes = {path.relative_to(ROOT).as_posix(): _sha256(path) for path in _source_paths()}
    status_lines = [line for line in status.stdout.splitlines() if line]
    return {
        "git_commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "git_dirty": bool(status_lines) or status.returncode != 0,
        "git_status": status_lines,
        "file_sha256": hashes,
        "source_digest": _hash_json(hashes),
    }


def _iter_records(
    dataset: Path,
    *,
    offset: int,
    limit: int | None,
) -> Iterator[DatasetRecord]:
    selected = 0
    with dataset.open("rb") as handle:
        for index, line in enumerate(handle):
            if index < offset:
                continue
            if limit is not None and selected >= limit:
                break
            row = json.loads(line)
            metadata = {
                field: row["meta"].get(field)
                for field in BREAKDOWN_FIELDS
            }
            yield DatasetRecord(
                dataset_index=index,
                track_id=row["track_id"],
                url=row["url"],
                html=row["html"],
                reference=row["groundtruth_content"],
                metadata=metadata,
            )
            selected += 1


def _batch(iterator: Iterator[DatasetRecord], size: int) -> Iterator[list[DatasetRecord]]:
    current: list[DatasetRecord] = []
    for record in iterator:
        current.append(record)
        if len(current) >= size:
            yield current
            current = []
    if current:
        yield current


def _input(record: DatasetRecord) -> ExtractionInput:
    return ExtractionInput(
        dataset_index=record.dataset_index,
        track_id=record.track_id,
        url=record.url,
        html=record.html,
    )


def _strategy_name(value: Any) -> str:
    strategy = getattr(value, "strategy", "")
    return str(getattr(strategy, "value", strategy) or "<empty>")


def _safe_error_message(error: BaseException | str, url: str) -> str:
    rendered = str(error).replace(url, "<url>")
    return rendered[:500]


def extract_one(
    item: ExtractionInput,
    *,
    mode: str,
    extractor: Callable[..., Any],
    official_cleaner: Callable[[str], str],
    scrubber: Callable[[str], tuple[str, dict[str, int]]],
) -> ExtractionObservation:
    started = time.perf_counter()
    transform_counts = None
    try:
        if mode == "official":
            evaluated_html = official_cleaner(item.html)
        elif mode == "scrubbed":
            evaluated_html, transform_counts = scrubber(item.html)
        else:
            raise BenchmarkError(f"unknown benchmark mode: {mode}")
        result = extractor(
            evaluated_html,
            item.url,
            extraction_profile=EXTRACTION_PROFILE,
        )
        prediction = getattr(result, "text", None)
        if not isinstance(prediction, str):
            raise TypeError("production extractor returned non-string text")
        return ExtractionObservation(
            prediction=prediction,
            latency_seconds=time.perf_counter() - started,
            strategy=_strategy_name(result),
            error_type=None,
            error_message=None,
            transform_counts=transform_counts,
        )
    except Exception as error:
        return ExtractionObservation(
            prediction="",
            latency_seconds=time.perf_counter() - started,
            strategy="<error>",
            error_type=type(error).__name__,
            error_message=_safe_error_message(error, item.url),
            transform_counts=transform_counts,
        )


def _metric_dict(result: OfficialMetricResult) -> dict[str, Any]:
    score = float(result.score)
    if not math.isfinite(score):
        raise BenchmarkError("official metric returned a non-finite score")
    return {
        "score": score,
        "success": bool(result.success),
        "details": result.details,
        "error": result.error_message,
    }


def _category(value: Any) -> str:
    if value is None or value == "" or value == []:
        return "<none>"
    if isinstance(value, list):
        return ",".join(sorted(str(item) for item in value)) or "<none>"
    return str(value)


def _latency_summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {"mean_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}
    ordered = sorted(values)

    def percentile(fraction: float) -> float:
        index = min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)
        return ordered[max(index, 0)] * 1000

    return {
        "mean_ms": sum(ordered) * 1000 / len(ordered),
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "max_ms": ordered[-1] * 1000,
    }


def _official_aggregate(
    calculator: OfficialMetricCalculator,
    page_results: list[dict[str, OfficialMetricResult]],
) -> dict[str, Any]:
    aggregated = calculator.aggregate_results(page_results)
    metrics: dict[str, Any] = {}
    for name in CORE_METRICS:
        result = aggregated.get(name)
        if result is None or not result.success:
            metrics[name] = {
                "score": 0.0,
                "successful_pages": 0,
                "failed_pages": len(page_results),
            }
            continue
        details = result.details
        metrics[name] = {
            "score": float(result.score),
            "successful_pages": int(details.get("num_successful", 0)),
            "failed_pages": int(details.get("num_failed", 0)),
            "min": float(details.get("min_score", result.score)),
            "max": float(details.get("max_score", result.score)),
            "sample_std": float(details.get("std_score", 0.0)),
        }
    # This is the composition rule in the official Evaluator._aggregate_metrics:
    # arithmetic mean of the five independently aggregated official metrics.
    overall = sum(float(metrics[name]["score"]) for name in CORE_METRICS) / len(CORE_METRICS)
    return {"overall": overall, "metrics": metrics}


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
    scrubber: Callable[[str], tuple[str, dict[str, int]]],
) -> dict[str, Any]:
    mode_output = output / mode
    mode_output.mkdir(parents=True, exist_ok=False)
    pages_temporary = mode_output / "pages.jsonl.partial"
    page_results: list[dict[str, OfficialMetricResult]] = []
    latencies: list[float] = []
    errors: Counter[str] = Counter()
    strategies: Counter[str] = Counter()
    category_scores: dict[str, dict[str, list[float]]] = {
        field: defaultdict(list) for field in BREAKDOWN_FIELDS
    }
    metric_failures: Counter[str] = Counter()
    pages = 0
    started = time.perf_counter()
    calculator = calculator_type(
        {
            "use_llm": False,
            "cache_dir": str(mode_output / ".official_metric_cache"),
        }
    )

    records = _iter_records(dataset, offset=offset, limit=limit)
    batch_size = max(concurrency * 2, 1)
    with (
        pages_temporary.open("wb") as page_handle,
        concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool,
    ):
        for record_batch in _batch(records, batch_size):
            inputs = [_input(record) for record in record_batch]
            observations = list(
                pool.map(
                    lambda item: extract_one(
                        item,
                        mode=mode,
                        extractor=extractor,
                        official_cleaner=official_cleaner,
                        scrubber=scrubber,
                    ),
                    inputs,
                )
            )
            for record, observation in zip(record_batch, observations, strict=True):
                metrics: dict[str, OfficialMetricResult] = {}
                if observation.error_type is None:
                    metrics = calculator.calculate_all(
                        predicted_content=observation.prediction,
                        groundtruth_content=record.reference,
                        predicted_content_list=None,
                        groundtruth_content_list=None,
                    )
                    page_results.append(metrics)
                else:
                    errors[observation.error_type] += 1
                metric_payload = {
                    name: _metric_dict(result)
                    for name, result in metrics.items()
                }
                for name in CORE_METRICS:
                    result = metrics.get(name)
                    if result is None or not result.success:
                        metric_failures[name] += 1
                text_result = metrics.get("text_edit")
                if text_result is not None and text_result.success:
                    for field, value in record.metadata.items():
                        category_scores[field][_category(value)].append(float(text_result.score))
                row = {
                    "dataset_index": record.dataset_index,
                    "track_id": record.track_id,
                    "reference_sha256": hashlib.sha256(record.reference.encode()).hexdigest(),
                    "prediction": observation.prediction,
                    "metadata": record.metadata,
                    "extraction": {
                        "latency_ms": observation.latency_seconds * 1000,
                        "strategy": observation.strategy,
                        "error": (
                            None
                            if observation.error_type is None
                            else {
                                "type": observation.error_type,
                                "message": observation.error_message,
                            }
                        ),
                    },
                    "input_transform": {
                        "mode": mode,
                        "counts": observation.transform_counts,
                    },
                    "official_metrics": metric_payload,
                }
                page_handle.write(_json_bytes(row))
                pages += 1
                latencies.append(observation.latency_seconds)
                strategies[observation.strategy] += 1
        page_handle.flush()
        os.fsync(page_handle.fileno())
    pages_final = mode_output / "pages.jsonl"
    os.replace(pages_temporary, pages_final)
    elapsed = time.perf_counter() - started
    aggregate = _official_aggregate(calculator, page_results)
    breakdowns = {
        field: {
            category: {
                "pages_with_successful_text_metric": len(scores),
                "text_edit": sum(scores) / len(scores),
            }
            for category, scores in sorted(groups.items())
            if scores
        }
        for field, groups in category_scores.items()
    }
    summary = {
        "mode": mode,
        "pages": pages,
        "extraction_errors": sum(errors.values()),
        "error_types": dict(sorted(errors.items())),
        "metric_failures": dict(sorted(metric_failures.items())),
        "strategies": dict(sorted(strategies.items())),
        "overall": aggregate["overall"],
        "metrics": aggregate["metrics"],
        "breakdowns": breakdowns,
        "timing": {
            "pipeline_wall_seconds": elapsed,
            "pages_per_pipeline_wall_second": pages / elapsed if elapsed else None,
            "extraction_latency": _latency_summary(latencies),
            "scope": "local input transform + production extraction + official metric scoring",
        },
        "pages_sha256": _sha256(pages_final),
    }
    _atomic_json(mode_output / "summary.json", summary)
    return summary


def _selected_pages(offset: int, limit: int | None) -> int:
    remaining = max(DATASET_RECORDS - offset, 0)
    return remaining if limit is None else min(remaining, limit)


def _claimability(
    *,
    args: argparse.Namespace,
    modes: list[str],
    mode_summaries: Mapping[str, Mapping[str, Any]],
    source_before: Mapping[str, Any],
    source_after: Mapping[str, Any],
    dataset_stable: bool,
    evaluator_stable: bool,
    label_guard: Mapping[str, Any],
) -> dict[str, Any]:
    reasons: list[str] = []
    if args.offset != 0:
        reasons.append(f"nonzero dataset offset ({args.offset})")
    if args.limit is not None:
        reasons.append(f"limited dataset run ({args.limit} selected pages)")
    if set(modes) != set(MODES):
        reasons.append("official and annotation-scrubbed tracks were not both run")
    if source_before.get("git_dirty") or source_after.get("git_dirty"):
        reasons.append("Clusy source worktree was dirty")
    if source_before.get("source_digest") != source_after.get("source_digest"):
        reasons.append("relevant Clusy source changed during the run")
    if not dataset_stable:
        reasons.append("dataset changed during the run")
    if not evaluator_stable:
        reasons.append("official evaluator changed during the run")
    if not label_guard.get("passed"):
        reasons.append("production source label-leak guard did not pass")
    for mode, summary in mode_summaries.items():
        if summary.get("pages") != DATASET_RECORDS:
            reasons.append(f"{mode} did not score all {DATASET_RECORDS} pages")
        if summary.get("extraction_errors"):
            reasons.append(f"{mode} had {summary['extraction_errors']} extraction errors")
    return {
        "claimable_on_fixed_public_offline_protocol": not reasons,
        "reasons": reasons,
        "leaderboard_comparable": False,
        "leaderboard_comparability_reason": (
            "the published fine-grained rows do not fully disclose the optional "
            "LLM splitter model, endpoint behavior, cache, and dependency lock; "
            "this runner fixes the official supported use_llm=False path"
        ),
        "universal_or_blind_sota_claimable": False,
        "scope": (
            "public WebMainBench 545-page fine-grained offline extraction; "
            "not fetching, rendering, discovery, or production reliability"
        ),
    }


def _artifact_manifest(output: Path) -> dict[str, Any]:
    files: dict[str, Any] = {}
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            relative = path.relative_to(output).as_posix()
            files[relative] = {"bytes": path.stat().st_size, "sha256": _sha256(path)}
    return {"schema_version": 1, "files": files}


def _prepare_output(path: Path | None) -> Path:
    if path is None:
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        path = ROOT / "bench" / "results" / "webmainbench-finegrained" / stamp
    path = path.resolve()
    if path.exists() and any(path.iterdir()):
        raise BenchmarkError(f"output directory is not empty: {path}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _modes(value: str) -> list[str]:
    return list(MODES) if value == "both" else [value]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--evaluator-root", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--mode", choices=(*MODES, "both"), default="both")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--concurrency", type=int, default=4)
    return parser.parse_args(argv)


def _validate_args(args: argparse.Namespace) -> None:
    if args.offset < 0 or args.offset >= DATASET_RECORDS:
        raise BenchmarkError(f"offset must be in [0, {DATASET_RECORDS - 1}]")
    if args.limit is not None and args.limit <= 0:
        raise BenchmarkError("limit must be positive")
    if args.concurrency <= 0 or args.concurrency > 64:
        raise BenchmarkError("concurrency must be in [1, 64]")


def run_benchmark(args: argparse.Namespace) -> int:
    _validate_args(args)
    output = _prepare_output(args.output_dir)
    dataset_path = args.dataset.resolve()
    evaluator_root = args.evaluator_root.resolve()
    try:
        dataset_before = verify_dataset(dataset_path)
        evaluator_before = verify_evaluator(evaluator_root)
        source_before = source_provenance()
        from bench.webmainbench_benchmark import (
            scan_label_leak_guard,
            scrub_annotation_artifacts,
        )

        label_guard = scan_label_leak_guard()
        calculator_type, official_cleaner, dependencies = load_official_toolkit(evaluator_root)
        from app.services.extractor import extract_content

        modes = _modes(args.mode)
        _atomic_json(
            output / "run_config.json",
            {
                "schema_version": 1,
                "created_at_utc": dt.datetime.now(dt.UTC).isoformat(),
                "dataset": dataset_before,
                "evaluator": evaluator_before,
                "source_before": source_before,
                "protocol": {
                    "name": "WebMainBench 545 official deterministic offline",
                    "official_metric_implementation": (
                        "verified WebMainBench MetricCalculator.calculate_all"
                    ),
                    "aggregation": (
                        "verified BaseMetric.aggregate_results per metric; "
                        "official Evaluator arithmetic mean across five metrics"
                    ),
                    "use_llm": False,
                    "paid_calls": False,
                    "model_weight_downloads": False,
                    "entry_point": "app.services.extractor.extract_content",
                    "extraction_profile": EXTRACTION_PROFILE,
                    "prediction_transform": "identity ExtractionResult.text",
                    "modes": modes,
                    "offset": args.offset,
                    "limit": args.limit,
                    "selected_pages": _selected_pages(args.offset, args.limit),
                    "concurrency": args.concurrency,
                },
                "dependencies": dependencies,
                "python": sys.version,
                "platform": platform.platform(),
            },
        )
        _atomic_json(output / "label_leak_guard.json", label_guard)
        mode_summaries: dict[str, dict[str, Any]] = {}
        for mode in modes:
            mode_summaries[mode] = _run_mode(
                mode=mode,
                dataset=dataset_path,
                output=output,
                offset=args.offset,
                limit=args.limit,
                concurrency=args.concurrency,
                calculator_type=calculator_type,
                official_cleaner=official_cleaner,
                extractor=extract_content,
                scrubber=scrub_annotation_artifacts,
            )

        dataset_after = verify_dataset(dataset_path)
        evaluator_after = verify_evaluator(evaluator_root)
        source_after = source_provenance()
        dataset_stable = dataset_before == dataset_after
        evaluator_stable = evaluator_before == evaluator_after
        claimability = _claimability(
            args=args,
            modes=modes,
            mode_summaries=mode_summaries,
            source_before=source_before,
            source_after=source_after,
            dataset_stable=dataset_stable,
            evaluator_stable=evaluator_stable,
            label_guard=label_guard,
        )
        summary = {
            "schema_version": 1,
            "completed_at_utc": dt.datetime.now(dt.UTC).isoformat(),
            "benchmark": {
                "name": "WebMainBench",
                "track": "545-page fine-grained",
                "public_ground_truth": True,
                "metrics": list(CORE_METRICS),
                "official_metric_code_reused": True,
                "use_llm": False,
            },
            "dataset": {
                "before": dataset_before,
                "after": dataset_after,
                "stable": dataset_stable,
            },
            "evaluator": {
                "before": evaluator_before,
                "after": evaluator_after,
                "stable": evaluator_stable,
                "dependencies": dependencies,
            },
            "source": {
                "before": source_before,
                "after": source_after,
                "stable": source_before["source_digest"] == source_after["source_digest"],
            },
            "label_leak_guard": label_guard,
            "modes": mode_summaries,
            "claimability": claimability,
        }
        _atomic_json(output / "summary.json", summary)
        if not claimability["claimable_on_fixed_public_offline_protocol"]:
            reasons = "\n".join(f"- {reason}" for reason in claimability["reasons"])
            _atomic_write(
                output / "NOT_CLAIMABLE.txt",
                (
                    "NOT CLAIMABLE\n\n"
                    f"{reasons}\n\n"
                    "Development diagnostic only. This is never evidence of universal SOTA.\n"
                ).encode(),
            )
        _atomic_json(output / "manifest.json", _artifact_manifest(output))
        for mode, mode_summary in mode_summaries.items():
            print(
                f"{mode}: pages={mode_summary['pages']} "
                f"overall={mode_summary['overall']:.6f} "
                f"errors={mode_summary['extraction_errors']}"
            )
        if claimability["claimable_on_fixed_public_offline_protocol"]:
            print("PROTOCOL-VALID: scoped public offline result; not leaderboard-equivalent")
        else:
            print("NOT CLAIMABLE: " + "; ".join(claimability["reasons"]))
        print(f"artifacts: {output}")
        return 0
    except Exception:
        if output.exists():
            _atomic_write(
                output / "NOT_CLAIMABLE.txt",
                b"NOT CLAIMABLE\n\n"
                b"- dataset, evaluator, dependencies, source, or run completion "
                b"was not fully verified\n\n"
                b"No score from this incomplete artifact may be reported.\n",
            )
        raise


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run_benchmark(args)
    except BenchmarkError as error:
        print(f"benchmark error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("benchmark interrupted; partial artifact is NOT CLAIMABLE", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

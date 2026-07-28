#!/usr/bin/env python3
"""Fail-closed, resumable WebMainBench v1.1 production-extractor benchmark.

The corpus and official ROUGE-5 implementation are accepted only at the
immutable revisions and byte hashes pinned below.  The extractor receives
exactly ``(html, url, extraction_profile="balanced")``; labels and benchmark
metadata stay in the scoring process and are never passed to production code.

Two deliberately distinct tracks are supported:

* ``raw`` scores the untouched benchmark HTML and is comparable to the
  published WebMainBench protocol.
* ``scrubbed`` removes annotation-only markers before extraction and is the
  leakage-safe robustness track.  Ground truth is never modified.

The 1.35 GB JSONL is streamed in bounded batches.  Checkpoint commits make an
interrupted run resumable without accepting a partially written JSONL record.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
import platform
import re
import resource
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DATASET_REPOSITORY = "https://huggingface.co/datasets/opendatalab/WebMainBench"
DATASET_REVISION = "5da0972e9b58d0c7891ae75053ced97c268f52e3"
DATASET_FILENAME = "webmainbench.jsonl"
DATASET_BYTES = 1_354_734_941
DATASET_SHA256 = "85765fe798f07c14eb1c92945046eaa56e0da59663f70b9c498647d7dfd78884"
DATASET_RECORDS = 7_809

EVALUATOR_REPOSITORY = "https://github.com/opendatalab/MinerU-HTML.git"
EVALUATOR_COMMIT = "73cf266690befd209cae7e6fdff9716d5b31a976"
EVALUATOR_TREE = "e2d533d7926861a7ff12412d86a7799e4a746c1e"
EVALUATOR_RELATIVE_PATH = Path("eval_baselines/utils.py")
EVALUATOR_SHA256 = "0c65796479a159f8ecbd00eb89c185e0a9ef1853b1ac5962ca577ebd98a6923c"
EVALUATOR_LICENSE_SHA256 = "d418c5eb1fe17d2a19b6e8fc76a0346408dab88760d9b33a4b58d9e6988e69c4"
EVALUATOR_DEPENDENCIES = {
    "jieba": "0.42.1",
    "rouge-score": "0.1.2",
}

EXTRACTION_PROFILE = "balanced"
DEFAULT_CHECKPOINT_EVERY = 16
MODES = ("raw", "scrubbed")
BREAKDOWN_FIELDS = ("level", "language", "style", "table", "code", "equation")

# Production sources are scanned before extraction.  The benchmark harness
# itself necessarily contains these strings and is intentionally not scanned.
LABEL_LEAK_PATTERNS = (
    "cc-select",
    "data-anno-uid",
    "convert_main_content",
    "groundtruth_content",
    "ground_truth_content",
    "webmainbench",
    "mark-selected",
    "cc-extrastyle",
    "marked-text",
    "marked-tail",
)
PRODUCTION_SOURCE_GLOBS = (
    ("app", "*.py"),
    ("native/src", "*.rs"),
    ("native/python", "*.py"),
)

SOURCE_FIXED_FILES = (
    "bench/webmainbench_benchmark.py",
    "app/config.py",
    "app/services/extractor.py",
    "pyproject.toml",
    "uv.lock",
    "native/Cargo.toml",
    "native/Cargo.lock",
    "native/pyproject.toml",
    "native/python/clusy_native/__init__.py",
    "native/python/clusy_native/_native.pyi",
    "native/python/clusy_native/py.typed",
)

_MARKER_ATTRIBUTES = frozenset({"cc-select", "data-anno-uid"})
_MARKER_CLASS_TOKENS = frozenset({"mark-selected", "cc-unloaded", "selecto-selection"})
_WRAPPER_TAGS = frozenset({"marked-text", "marked-tail"})
_TAG_NAME_RE = re.compile(r"<\s*(?P<closing>/)?\s*(?P<name>[A-Za-z][\w:.-]*)")
_FORBIDDEN_SCRUBBED_MARKUP = (
    re.compile(r"\bcc-select\b", re.IGNORECASE),
    re.compile(r"\bdata-anno-uid\b", re.IGNORECASE),
    re.compile(r"\bcc-extraStyle\b", re.IGNORECASE),
    re.compile(r"\bmark-selected\b", re.IGNORECASE),
    re.compile(r"<\s*/?\s*marked-(?:text|tail)\b", re.IGNORECASE),
)


class BenchmarkError(RuntimeError):
    """A provenance, integrity, or runner condition invalidates the run."""


@dataclass(frozen=True)
class DatasetRecord:
    """One selected row, with scoring data kept outside the extractor input."""

    dataset_index: int
    track_id: str
    url: str
    html: str
    reference: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ExtractionInput:
    """The complete and deliberately label-free production extractor input."""

    dataset_index: int
    track_id: str
    url: str
    html: str


@dataclass(frozen=True)
class ExtractionObservation:
    prediction: str
    latency_seconds: float
    strategy: str
    word_count: int
    confidence: float | None
    page_type: str
    raw_html_characters: int
    evaluated_html_characters: int
    scrub: dict[str, int] | None
    error: dict[str, str] | None


@dataclass(frozen=True)
class AttributeSpan:
    start: int
    end: int
    name: str
    value_start: int | None
    value_end: int | None


@dataclass
class GroupScore:
    pages: int = 0
    precision_sum: float = 0.0
    recall_sum: float = 0.0
    f1_sum: float = 0.0
    errors: int = 0

    def add(
        self,
        *,
        precision: float,
        recall: float,
        f1: float,
        error: bool,
    ) -> None:
        self.pages += 1
        self.precision_sum += precision
        self.recall_sum += recall
        self.f1_sum += f1
        self.errors += int(error)

    def export(self) -> dict[str, int | float]:
        denominator = self.pages or 1
        return {
            "pages": self.pages,
            "precision": self.precision_sum / denominator,
            "recall": self.recall_sum / denominator,
            "f1": self.f1_sum / denominator,
            "errors": self.errors,
        }


@dataclass
class RunAggregate:
    overall: GroupScore = field(default_factory=GroupScore)
    breakdowns: dict[str, dict[str, GroupScore]] = field(
        default_factory=lambda: {
            field_name: defaultdict(GroupScore) for field_name in BREAKDOWN_FIELDS
        }
    )
    latencies_ms: list[float] = field(default_factory=list)
    strategies: Counter[str] = field(default_factory=Counter)
    error_types: Counter[str] = field(default_factory=Counter)
    scrub_counts: Counter[str] = field(default_factory=Counter)
    prediction_characters: int = 0
    completed_ids: set[str] = field(default_factory=set)
    first_dataset_index: int | None = None
    last_dataset_index: int | None = None

    def add_row(self, row: Mapping[str, Any]) -> None:
        scores = _require_mapping(row, "scores")
        extraction = _require_mapping(row, "extraction")
        metadata = _require_mapping(row, "metadata")
        precision = _score_value(scores, "precision")
        recall = _score_value(scores, "recall")
        f1 = _score_value(scores, "f1")
        error_value = extraction.get("error")
        has_error = error_value is not None
        self.overall.add(
            precision=precision,
            recall=recall,
            f1=f1,
            error=has_error,
        )
        for field_name in BREAKDOWN_FIELDS:
            category = _category(metadata.get(field_name))
            self.breakdowns[field_name][category].add(
                precision=precision,
                recall=recall,
                f1=f1,
                error=has_error,
            )
        latency_ms = _finite_number(extraction.get("latency_ms"), "latency_ms")
        if latency_ms < 0:
            raise BenchmarkError("negative latency in persisted page row")
        self.latencies_ms.append(latency_ms)
        self.strategies[str(extraction.get("strategy", "") or "<empty>")] += 1
        if isinstance(error_value, Mapping):
            self.error_types[str(error_value.get("type", "unknown"))] += 1
        prediction = row.get("prediction")
        if not isinstance(prediction, str):
            raise BenchmarkError("persisted page row has non-string prediction")
        self.prediction_characters += len(prediction)
        input_data = _require_mapping(row, "input")
        scrub = input_data.get("scrub")
        if isinstance(scrub, Mapping):
            for key, value in scrub.items():
                if not isinstance(value, int) or value < 0:
                    raise BenchmarkError(f"invalid persisted scrub counter: {key}")
                self.scrub_counts[str(key)] += value
        dataset_index = row.get("dataset_index")
        if not isinstance(dataset_index, int):
            raise BenchmarkError("persisted page row has invalid dataset_index")
        track_id = row.get("track_id")
        if not isinstance(track_id, str) or not track_id:
            raise BenchmarkError("persisted page row has invalid track_id")
        if track_id in self.completed_ids:
            raise BenchmarkError(f"duplicate persisted track_id: {track_id}")
        self.completed_ids.add(track_id)
        if self.first_dataset_index is None:
            self.first_dataset_index = dataset_index
        self.last_dataset_index = dataset_index

    def export(self, progress: Mapping[str, Any]) -> dict[str, Any]:
        latency = _latency_summary(self.latencies_ms)
        extraction_wall = _finite_number(
            progress.get("pipeline_wall_seconds", 0.0),
            "pipeline_wall_seconds",
        )
        extraction_process = _finite_number(
            progress.get("pipeline_process_seconds", 0.0),
            "pipeline_process_seconds",
        )
        return {
            **self.overall.export(),
            "breakdowns": {
                field_name: {category: score.export() for category, score in sorted(groups.items())}
                for field_name, groups in self.breakdowns.items()
            },
            "strategy_counts": dict(sorted(self.strategies.items())),
            "error_types": dict(sorted(self.error_types.items())),
            "prediction_characters": self.prediction_characters,
            "scrub_totals": dict(sorted(self.scrub_counts.items())),
            "timing": {
                "pipeline_wall_seconds": extraction_wall,
                "pipeline_process_seconds": extraction_process,
                "pages_per_pipeline_wall_second": (
                    self.overall.pages / extraction_wall if extraction_wall > 0 else None
                ),
                "latency_ms": latency,
                "segments": len(progress.get("segments", [])),
                "measurement_scope": (
                    "bounded local worker batches; pipeline wall time includes "
                    "scrubbing where selected and production extraction, but "
                    "excludes dataset JSON parsing, official scoring, and output"
                ),
                "per_page_latency_scope": ("app.services.extractor.extract_content call only"),
            },
        }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run Clusy on the immutable WebMainBench v1.1 corpus with the "
            "official ROUGE-5 evaluator."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("/tmp/clusy-webmainbench") / DATASET_FILENAME,
        help="pinned WebMainBench JSONL downloaded from Hugging Face",
    )
    parser.add_argument(
        "--evaluator-root",
        type=Path,
        default=Path("/tmp/clusy-mineru-html"),
        help="clean checkout of the pinned MinerU-HTML evaluator commit",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="artifact directory; defaults under ignored bench/results/webmainbench",
    )
    parser.add_argument(
        "--mode",
        choices=("raw", "scrubbed", "both"),
        default="both",
        help="official raw track, leakage-safe scrubbed track, or both",
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="zero-based corpus row offset; nonzero runs are NOT CLAIMABLE",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="selected-row limit for smoke tests; limited runs are NOT CLAIMABLE",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        help="synchronous production extractor worker threads",
    )
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=DEFAULT_CHECKPOINT_EVERY,
        help="rows atomically committed per resumable checkpoint",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume an output directory with an identical run fingerprint",
    )
    return parser.parse_args(argv)


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        if check:
            raise BenchmarkError(f"{' '.join(command)} failed: {error}") from error
        return subprocess.CompletedProcess(command, 127, "", str(error))
    if check and result.returncode:
        message = result.stderr.strip() or result.stdout.strip()
        raise BenchmarkError(f"{' '.join(command)} failed: {message}")
    return result


def _git(root: Path, *arguments: str, check: bool = True) -> str:
    return _run(["git", *arguments], cwd=root, check=check).stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _dataset_digest_and_lines(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    line_breaks = 0
    final_byte = b""
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
            line_breaks += chunk.count(b"\n")
            final_byte = chunk[-1:]
    records = line_breaks + int(path.stat().st_size > 0 and final_byte != b"\n")
    return digest.hexdigest(), records


def _huggingface_metadata_path(dataset: Path) -> Path:
    return dataset.parent / ".cache" / "huggingface" / "download" / f"{dataset.name}.metadata"


def verify_dataset(dataset: Path) -> dict[str, Any]:
    if not dataset.is_file():
        raise BenchmarkError(
            f"dataset not found: {dataset}. Download {DATASET_FILENAME} at "
            f"revision {DATASET_REVISION}."
        )
    size = dataset.stat().st_size
    if size != DATASET_BYTES:
        raise BenchmarkError(f"dataset byte-size mismatch: expected {DATASET_BYTES}, found {size}")
    digest, records = _dataset_digest_and_lines(dataset)
    if digest != DATASET_SHA256:
        raise BenchmarkError(f"dataset SHA-256 mismatch: expected {DATASET_SHA256}, found {digest}")
    if records != DATASET_RECORDS:
        raise BenchmarkError(
            f"dataset row-count mismatch: expected {DATASET_RECORDS}, found {records}"
        )

    metadata_path = _huggingface_metadata_path(dataset)
    metadata_revision: str | None = None
    if metadata_path.is_file():
        metadata_lines = metadata_path.read_text(encoding="utf-8").splitlines()
        metadata_revision = metadata_lines[0].strip() if metadata_lines else None
        if metadata_revision != DATASET_REVISION:
            raise BenchmarkError(
                "Hugging Face download metadata revision mismatch: "
                f"expected {DATASET_REVISION}, found {metadata_revision!r}"
            )
    return {
        "repository": DATASET_REPOSITORY,
        "path": str(dataset),
        "filename": DATASET_FILENAME,
        "expected_revision": DATASET_REVISION,
        "revision_verification": (
            "Hugging Face local-dir metadata plus exact immutable blob identity"
            if metadata_revision is not None
            else "exact immutable blob byte-size and SHA-256 identity"
        ),
        "huggingface_metadata_path": (str(metadata_path) if metadata_path.is_file() else None),
        "huggingface_metadata_revision": metadata_revision,
        "bytes": size,
        "sha256": digest,
        "records": records,
        "verified": True,
    }


def verify_evaluator(root: Path) -> dict[str, Any]:
    if not root.is_dir() or not (root / ".git").exists():
        raise BenchmarkError(
            f"{root} is not a Git checkout. Clone {EVALUATOR_REPOSITORY} and "
            f"check out {EVALUATOR_COMMIT}."
        )
    commit = _git(root, "rev-parse", "HEAD")
    if commit != EVALUATOR_COMMIT:
        raise BenchmarkError(
            f"evaluator commit mismatch: expected {EVALUATOR_COMMIT}, found {commit}"
        )
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    if tree != EVALUATOR_TREE:
        raise BenchmarkError(
            f"evaluator Git tree mismatch: expected {EVALUATOR_TREE}, found {tree}"
        )
    tracked_status = _git(
        root,
        "status",
        "--porcelain",
        "--untracked-files=no",
    )
    if tracked_status:
        raise BenchmarkError("evaluator checkout has tracked modifications:\n" + tracked_status)
    critical_hashes = {
        EVALUATOR_RELATIVE_PATH: EVALUATOR_SHA256,
        Path("LICENSE"): EVALUATOR_LICENSE_SHA256,
    }
    for relative, expected in critical_hashes.items():
        path = root / relative
        if not path.is_file():
            raise BenchmarkError(f"pinned evaluator file is missing: {relative}")
        actual = _sha256(path)
        if actual != expected:
            raise BenchmarkError(
                f"evaluator hash mismatch for {relative}: expected {expected}, found {actual}"
            )
    origin_result = _run(
        ["git", "remote", "get-url", "origin"],
        cwd=root,
        check=False,
    )
    return {
        "repository": EVALUATOR_REPOSITORY,
        "root": str(root),
        "expected_commit": EVALUATOR_COMMIT,
        "actual_commit": commit,
        "git_tree": tree,
        "relative_path": EVALUATOR_RELATIVE_PATH.as_posix(),
        "evaluator_sha256": EVALUATOR_SHA256,
        "license_sha256": EVALUATOR_LICENSE_SHA256,
        "origin": origin_result.stdout.strip() or None,
        "tracked_worktree_clean": True,
        "verified": True,
    }


def _require_evaluator_dependency_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for distribution, expected in EVALUATOR_DEPENDENCIES.items():
        try:
            actual = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError as error:
            raise BenchmarkError(
                f"official evaluator dependency missing: {distribution}=={expected}. "
                "Use the documented `uv run --with ...` command."
            ) from error
        if actual != expected:
            raise BenchmarkError(
                f"official evaluator dependency mismatch for {distribution}: "
                f"expected {expected}, found {actual}"
            )
        versions[distribution] = actual
    return versions


def load_official_scorer(
    evaluator_root: Path,
) -> tuple[Callable[[str, str, int], dict[str, float]], dict[str, str]]:
    versions = _require_evaluator_dependency_versions()
    evaluator_path = evaluator_root / EVALUATOR_RELATIVE_PATH
    module_name = f"clusy_webmainbench_evaluator_{EVALUATOR_COMMIT[:12]}"
    spec = importlib.util.spec_from_file_location(module_name, evaluator_path)
    if spec is None or spec.loader is None:
        raise BenchmarkError(f"could not load official evaluator: {evaluator_path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as error:
        raise BenchmarkError(
            f"official evaluator import failed: {type(error).__name__}: {error}"
        ) from error
    scorer = getattr(module, "calc_rouge_n_score", None)
    if not callable(scorer):
        raise BenchmarkError("pinned evaluator does not expose callable calc_rouge_n_score")
    return scorer, versions


def _source_paths() -> list[Path]:
    paths = {ROOT / relative for relative in SOURCE_FIXED_FILES if (ROOT / relative).is_file()}
    for base_relative, pattern in PRODUCTION_SOURCE_GLOBS:
        base = ROOT / base_relative
        if base.is_dir():
            paths.update(path for path in base.rglob(pattern) if path.is_file())
    return sorted(paths)


def _source_hashes() -> dict[str, str]:
    return {path.relative_to(ROOT).as_posix(): _sha256(path) for path in _source_paths()}


def source_provenance() -> dict[str, Any]:
    commit_result = _run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=False)
    status_result = _run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        check=False,
    )
    hashes = _source_hashes()
    status = [line for line in status_result.stdout.splitlines() if line]
    return {
        "root": str(ROOT),
        "git_commit": commit_result.stdout.strip() or None,
        "git_dirty": bool(status),
        "git_status": status,
        "file_sha256": hashes,
        "source_digest": _hash_json(hashes),
        "lock_sha256": {
            relative: hashes.get(relative) for relative in ("uv.lock", "native/Cargo.lock")
        },
        "native_source_sha256": {
            relative: digest
            for relative, digest in hashes.items()
            if relative.startswith("native/")
        },
    }


def scan_label_leak_guard() -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    scanned: list[str] = []
    encoded_patterns = tuple(pattern.encode("utf-8") for pattern in LABEL_LEAK_PATTERNS)
    for base_relative, glob_pattern in PRODUCTION_SOURCE_GLOBS:
        base = ROOT / base_relative
        if not base.is_dir():
            continue
        for path in sorted(base.rglob(glob_pattern)):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT).as_posix()
            scanned.append(relative)
            content = path.read_bytes().lower()
            for pattern, encoded in zip(
                LABEL_LEAK_PATTERNS,
                encoded_patterns,
                strict=True,
            ):
                if encoded in content:
                    line_numbers = [
                        index
                        for index, line in enumerate(content.splitlines(), start=1)
                        if encoded in line
                    ]
                    matches.append(
                        {
                            "file": relative,
                            "pattern": pattern,
                            "lines": line_numbers[:20],
                        }
                    )
    report = {
        "passed": not matches,
        "patterns": list(LABEL_LEAK_PATTERNS),
        "scanned_files": scanned,
        "scanned_files_sha256": _hash_json(scanned),
        "matches": matches,
        "scope": (
            "text scan of production app Python and native Rust source; "
            "benchmark code and artifacts are excluded"
        ),
        "runtime_contract": (
            "only HTML, URL, and the fixed balanced profile enter "
            "app.services.extractor.extract_content"
        ),
    }
    if matches:
        rendered = ", ".join(f"{match['file']}:{match['pattern']}" for match in matches[:20])
        raise BenchmarkError(
            f"label-leak guard found benchmark/annotation tokens in production source: {rendered}"
        )
    return report


def _package_versions() -> dict[str, str | None]:
    packages = (
        "clusy-native",
        "trafilatura",
        "readability-lxml",
        "markdownify",
        "lxml",
        "pydantic",
        "orjson",
        "jieba",
        "rouge-score",
    )
    versions: dict[str, str | None] = {}
    for package in packages:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return versions


def _native_module_metadata() -> dict[str, Any]:
    try:
        import clusy_native
        from clusy_native import _native
    except (ImportError, RuntimeError) as error:
        return {"loaded": False, "error": f"{type(error).__name__}: {error}"}
    extension_value = getattr(_native, "__file__", None)
    package_value = getattr(clusy_native, "__file__", None)
    extension = Path(extension_value).resolve() if extension_value else None
    package = Path(package_value).resolve() if package_value else None
    return {
        "loaded": True,
        "extension_path": str(extension) if extension else None,
        "extension_sha256": (
            _sha256(extension) if extension is not None and extension.is_file() else None
        ),
        "package_path": str(package) if package else None,
        "package_sha256": (_sha256(package) if package is not None and package.is_file() else None),
    }


def _environment_metadata(
    settings: Any,
    native_backend_version: Callable[[], str],
) -> dict[str, Any]:
    rust_result = _run(["rustc", "--version"], check=False)
    return {
        "captured_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "python": {
            "version": sys.version,
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
        },
        "platform": {
            "platform": platform.platform(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "processor": platform.processor() or None,
            "logical_cpu_count": os.cpu_count(),
        },
        "dependencies": _package_versions(),
        "native_backend": {
            "backend_version": native_backend_version(),
            "module": _native_module_metadata(),
            "rustc": rust_result.stdout.strip() or None,
        },
        "production_extraction": {
            "entry_point": "app.services.extractor.extract_content",
            "profile": EXTRACTION_PROFILE,
            "prediction_field": "ExtractionResult.text",
            "native_extraction_enabled": getattr(
                settings,
                "native_extraction_enabled",
                None,
            ),
            "native_extraction_min_confidence": getattr(
                settings,
                "native_extraction_min_confidence",
                None,
            ),
            "parallel_extraction_enabled": getattr(
                settings,
                "parallel_extraction_enabled",
                None,
            ),
            "extraction_merge_mode": getattr(
                settings,
                "extraction_merge_mode",
                None,
            ),
            "max_concurrent_extractions": getattr(
                settings,
                "max_concurrent_extractions",
                None,
            ),
            "extract_max_text_length": getattr(
                settings,
                "extract_max_text_length",
                None,
            ),
        },
    }


def _find_tag_end(html: str, start: int) -> int | None:
    quote: str | None = None
    index = start + 1
    while index < len(html):
        character = html[index]
        if quote is not None:
            if character == quote:
                quote = None
        elif character in {'"', "'"}:
            quote = character
        elif character == ">":
            return index
        index += 1
    return None


def _attribute_spans(tag: str, name_end: int) -> list[AttributeSpan]:
    spans: list[AttributeSpan] = []
    index = name_end
    terminal = len(tag) - 1
    while index < terminal:
        whitespace_start = index
        while index < terminal and tag[index].isspace():
            index += 1
        if index >= terminal or tag[index] in "/>":
            break
        attribute_start = whitespace_start
        name_start = index
        while index < terminal and not tag[index].isspace() and tag[index] not in "=/>":
            index += 1
        if index == name_start:
            index += 1
            continue
        attribute_name = tag[name_start:index].lower()
        while index < terminal and tag[index].isspace():
            index += 1
        value_start: int | None = None
        value_end: int | None = None
        if index < terminal and tag[index] == "=":
            index += 1
            while index < terminal and tag[index].isspace():
                index += 1
            if index < terminal and tag[index] in {'"', "'"}:
                quote = tag[index]
                index += 1
                value_start = index
                while index < terminal and tag[index] != quote:
                    index += 1
                value_end = index
                if index < terminal:
                    index += 1
            else:
                value_start = index
                while index < terminal and not tag[index].isspace() and tag[index] not in ">":
                    index += 1
                value_end = index
        spans.append(
            AttributeSpan(
                start=attribute_start,
                end=index,
                name=attribute_name,
                value_start=value_start,
                value_end=value_end,
            )
        )
    return spans


def _attribute_value(tag: str, attribute: AttributeSpan) -> str | None:
    if attribute.value_start is None or attribute.value_end is None:
        return None
    return tag[attribute.value_start : attribute.value_end]


def _scrub_inline_style(value: str) -> tuple[str, int]:
    removed = 0
    kept: list[str] = []
    trailing_semicolon = value.rstrip().endswith(";")
    for declaration in value.split(";"):
        key, separator, raw_value = declaration.partition(":")
        normalized_key = key.strip().lower()
        normalized_value = re.sub(r"\s+", " ", raw_value.strip().lower())
        is_user_select = normalized_key in {
            "user-select",
            "-webkit-user-select",
            "-moz-user-select",
            "-ms-user-select",
        } and normalized_value in {"none", "none !important"}
        is_annotation_outline = (
            normalized_key == "outline"
            and re.fullmatch(
                r"(?:blue|#0d6efd|rgb\(\s*13\s*,\s*110\s*,\s*253\s*\)) "
                r"dashed (?:1px|2px)(?: !important)?",
                normalized_value,
            )
            is not None
        )
        if separator and (is_user_select or is_annotation_outline):
            removed += 1
        elif declaration:
            kept.append(declaration)
    result = ";".join(kept)
    if result and trailing_semicolon:
        result += ";"
    return result, removed


def _scrub_start_tag(tag: str, name_end: int) -> tuple[str, Counter[str]]:
    replacements: list[tuple[int, int, str]] = []
    counters: Counter[str] = Counter()
    attributes = _attribute_spans(tag, name_end)
    marker_selected = any(
        attribute.name == "cc-select"
        or (
            attribute.name == "class"
            and "mark-selected"
            in {token.lower() for token in (_attribute_value(tag, attribute) or "").split()}
        )
        for attribute in attributes
    )
    for attribute in attributes:
        value = _attribute_value(tag, attribute)
        if attribute.name in _MARKER_ATTRIBUTES:
            replacements.append((attribute.start, attribute.end, ""))
            counters[f"attribute_{attribute.name}"] += 1
            continue
        if attribute.name == "class" and value is not None:
            tokens = value.split()
            kept = [token for token in tokens if token.lower() not in _MARKER_CLASS_TOKENS]
            removed = len(tokens) - len(kept)
            if removed:
                counters["class_tokens"] += removed
                if kept:
                    replacements.append(
                        (attribute.value_start or 0, attribute.value_end or 0, " ".join(kept))
                    )
                else:
                    replacements.append((attribute.start, attribute.end, ""))
            continue
        if attribute.name == "style" and value is not None:
            scrubbed, removed = _scrub_inline_style(value)
            if marker_selected and not value.strip():
                counters["empty_selected_style_attributes"] += 1
                replacements.append((attribute.start, attribute.end, ""))
                continue
            if removed:
                counters["inline_style_declarations"] += removed
                if scrubbed:
                    replacements.append(
                        (
                            attribute.value_start or 0,
                            attribute.value_end or 0,
                            scrubbed,
                        )
                    )
                else:
                    replacements.append((attribute.start, attribute.end, ""))
    for start, end, replacement in sorted(replacements, reverse=True):
        tag = tag[:start] + replacement + tag[end:]
    return tag, counters


def _tag_has_annotation_style_id(tag: str, name_end: int) -> bool:
    for attribute in _attribute_spans(tag, name_end):
        if attribute.name != "id":
            continue
        value = _attribute_value(tag, attribute)
        return value is not None and value.lower() == "cc-extrastyle"
    return False


def scrub_annotation_artifacts(html: str) -> tuple[str, dict[str, int]]:
    """Remove only known annotation UI signals while preserving page text."""
    output: list[str] = []
    counters: Counter[str] = Counter()
    position = 0
    while position < len(html):
        start = html.find("<", position)
        if start < 0:
            output.append(html[position:])
            break
        output.append(html[position:start])
        if html.startswith("<!--", start):
            end = html.find("-->", start + 4)
            if end < 0:
                comment = html[start:]
                if any(
                    pattern.search(comment)
                    for pattern in _FORBIDDEN_SCRUBBED_MARKUP
                ):
                    counters["annotation_comments"] += 1
                else:
                    output.append(comment)
                break
            comment = html[start : end + 3]
            if any(
                pattern.search(comment)
                for pattern in _FORBIDDEN_SCRUBBED_MARKUP
            ):
                # Comments are non-rendered input. Some malformed benchmark
                # pages contain complete annotation-tool HTML snapshots inside
                # comments; dropping only those comments removes the leak
                # signal without changing visible page text.
                counters["annotation_comments"] += 1
            else:
                output.append(comment)
            position = end + 3
            continue
        if html.startswith("<![CDATA[", start):
            end = html.find("]]>", start + 9)
            if end < 0:
                output.append(html[start:])
                break
            output.append(html[start : end + 3])
            position = end + 3
            continue
        end = _find_tag_end(html, start)
        if end is None:
            output.append(html[start:])
            break
        tag = html[start : end + 1]
        match = _TAG_NAME_RE.match(tag)
        if match is None:
            output.append(tag)
            position = end + 1
            continue
        name = match.group("name").lower()
        closing = match.group("closing") is not None
        if name in _WRAPPER_TAGS:
            counters["wrapper_tags"] += 1
            position = end + 1
            continue
        if name == "style" and not closing and _tag_has_annotation_style_id(tag, match.end("name")):
            closing_match = re.search(
                r"</\s*style\b",
                html[end + 1 :],
                flags=re.IGNORECASE,
            )
            if closing_match is None:
                raise BenchmarkError("annotation style block has no closing tag")
            closing_start = end + 1 + closing_match.start()
            closing_end = _find_tag_end(html, closing_start)
            if closing_end is None:
                raise BenchmarkError("annotation style closing tag is malformed")
            counters["annotation_style_blocks"] += 1
            position = closing_end + 1
            continue
        scrubbed_tag, tag_counters = _scrub_start_tag(
            tag,
            match.end("name"),
        )
        counters.update(tag_counters)
        output.append(scrubbed_tag)
        position = end + 1

        # Script and non-annotation style content is raw text.  Copy it without
        # interpreting JavaScript/CSS strings as markup.
        if name in {"script", "style"} and not closing and not tag.rstrip().endswith("/>"):
            closing_match = re.search(
                rf"</\s*{re.escape(name)}\b",
                html[position:],
                flags=re.IGNORECASE,
            )
            if closing_match is None:
                raw_content = html[position:]
                if any(
                    pattern.search(raw_content)
                    for pattern in _FORBIDDEN_SCRUBBED_MARKUP
                ):
                    output.pop()
                    counters[f"annotation_{name}_blocks"] += 1
                else:
                    output.append(raw_content)
                position = len(html)
                break
            closing_start = position + closing_match.start()
            closing_end = _find_tag_end(html, closing_start)
            if closing_end is None:
                raw_content = html[position:]
                if any(
                    pattern.search(raw_content)
                    for pattern in _FORBIDDEN_SCRUBBED_MARKUP
                ):
                    output.pop()
                    counters[f"annotation_{name}_blocks"] += 1
                else:
                    output.append(raw_content)
                position = len(html)
                break
            raw_content = html[position:closing_start]
            if any(
                pattern.search(raw_content)
                for pattern in _FORBIDDEN_SCRUBBED_MARKUP
            ):
                output.pop()
                counters[f"annotation_{name}_blocks"] += 1
            else:
                output.append(raw_content)
                output.append(html[closing_start : closing_end + 1])
            position = closing_end + 1

    scrubbed = "".join(output)
    remaining = [
        pattern.pattern for pattern in _FORBIDDEN_SCRUBBED_MARKUP if pattern.search(scrubbed)
    ]
    if remaining:
        raise BenchmarkError(
            "scrubber postcondition failed; annotation signals remain: " + ", ".join(remaining)
        )
    counters["characters_removed"] = len(html) - len(scrubbed)
    return scrubbed, dict(sorted(counters.items()))


def _selected_total(offset: int, limit: int | None) -> int:
    available = DATASET_RECORDS - offset
    return min(available, limit) if limit is not None else available


def _parse_dataset_record(line: str, dataset_index: int) -> DatasetRecord:
    try:
        document = json.loads(line)
    except json.JSONDecodeError as error:
        raise BenchmarkError(
            f"invalid dataset JSON at zero-based row {dataset_index}: {error}"
        ) from error
    if not isinstance(document, dict):
        raise BenchmarkError(f"dataset row {dataset_index} is not an object")
    track_id = document.get("track_id")
    html = document.get("html")
    reference = document.get("convert_main_content")
    metadata = document.get("meta")
    if not isinstance(track_id, str) or not track_id:
        raise BenchmarkError(f"dataset row {dataset_index} has invalid track_id")
    if not isinstance(html, str):
        raise BenchmarkError(f"dataset row {dataset_index} has non-string HTML")
    if not isinstance(reference, str):
        raise BenchmarkError(f"dataset row {dataset_index} has non-string convert_main_content")
    if not isinstance(metadata, dict):
        raise BenchmarkError(f"dataset row {dataset_index} has invalid metadata")
    url_value = document.get("url", "")
    url = str(url_value or "")
    return DatasetRecord(
        dataset_index=dataset_index,
        track_id=track_id,
        url=url,
        html=html,
        reference=reference,
        metadata=metadata,
    )


def _record_batches(
    dataset: Path,
    *,
    offset: int,
    limit: int | None,
    skip_selected: int,
    batch_size: int,
    completed_ids: set[str],
) -> Iterator[list[DatasetRecord]]:
    selected_total = _selected_total(offset, limit)
    selected_seen = 0
    parsed_ids = set(completed_ids)
    batch: list[DatasetRecord] = []
    with dataset.open("r", encoding="utf-8") as stream:
        for dataset_index, line in enumerate(stream):
            if dataset_index < offset:
                continue
            if selected_seen >= selected_total:
                break
            if selected_seen < skip_selected:
                selected_seen += 1
                continue
            record = _parse_dataset_record(line, dataset_index)
            if record.track_id in parsed_ids:
                raise BenchmarkError(f"duplicate selected track_id in corpus: {record.track_id}")
            parsed_ids.add(record.track_id)
            batch.append(record)
            selected_seen += 1
            if len(batch) >= batch_size:
                yield batch
                batch = []
    if batch:
        yield batch
    if selected_seen != selected_total:
        raise BenchmarkError(
            f"dataset selection expected {selected_total} rows, found {selected_seen}"
        )


def _extract_one(
    item: ExtractionInput,
    *,
    mode: str,
    extractor: Callable[..., Any],
) -> ExtractionObservation:
    evaluated_html = item.html
    scrub: dict[str, int] | None = None
    if mode == "scrubbed":
        evaluated_html, scrub = scrub_annotation_artifacts(item.html)
    started = time.perf_counter()
    try:
        result = extractor(
            evaluated_html,
            item.url,
            extraction_profile=EXTRACTION_PROFILE,
        )
        prediction = getattr(result, "text", None)
        if not isinstance(prediction, str):
            raise TypeError("ExtractionResult.text is not a string")
        strategy = str(getattr(result, "strategy", "") or "")
        word_count = int(getattr(result, "word_count", 0) or 0)
        confidence_value = getattr(result, "confidence", None)
        confidence = (
            float(confidence_value)
            if isinstance(confidence_value, (int, float)) and math.isfinite(float(confidence_value))
            else None
        )
        page_type = str(getattr(result, "page_type", "") or "")
        error = None
    except Exception as exception:
        prediction = ""
        strategy = "error"
        word_count = 0
        confidence = None
        page_type = ""
        error = {
            "type": type(exception).__name__,
            "message": str(exception)[:1000],
        }
    latency_seconds = time.perf_counter() - started
    return ExtractionObservation(
        prediction=prediction,
        latency_seconds=latency_seconds,
        strategy=strategy,
        word_count=word_count,
        confidence=confidence,
        page_type=page_type,
        raw_html_characters=len(item.html),
        evaluated_html_characters=len(evaluated_html),
        scrub=scrub,
        error=error,
    )


def _official_score(
    scorer: Callable[[str, str, int], dict[str, float]],
    reference: str,
    prediction: str,
) -> dict[str, float]:
    try:
        score = scorer(reference, prediction, 5)
    except Exception as error:
        raise BenchmarkError(
            f"official calc_rouge_n_score failed: {type(error).__name__}: {error}"
        ) from error
    if not isinstance(score, dict):
        raise BenchmarkError("official scorer returned a non-dict result")
    normalized = {
        "precision": _score_value(score, "prec"),
        "recall": _score_value(score, "rec"),
        "f1": _score_value(score, "f1"),
    }
    for name, value in normalized.items():
        if not 0.0 <= value <= 1.0:
            raise BenchmarkError(f"official {name} score is outside [0, 1]: {value}")
    return normalized


def _page_row(
    record: DatasetRecord,
    observation: ExtractionObservation,
    *,
    mode: str,
    scorer: Callable[[str, str, int], dict[str, float]],
) -> dict[str, Any]:
    scores = _official_score(scorer, record.reference, observation.prediction)
    return {
        "schema_version": 1,
        "dataset_index": record.dataset_index,
        "track_id": record.track_id,
        "url": record.url,
        "mode": mode,
        "prediction": observation.prediction,
        "scores": scores,
        "metadata": record.metadata,
        "reference": {
            "characters": len(record.reference),
            "sha256": hashlib.sha256(record.reference.encode("utf-8")).hexdigest(),
        },
        "input": {
            "raw_html_characters": observation.raw_html_characters,
            "evaluated_html_characters": observation.evaluated_html_characters,
            "scrub": observation.scrub,
        },
        "extraction": {
            "entry_point": "app.services.extractor.extract_content",
            "profile": EXTRACTION_PROFILE,
            "strategy": observation.strategy,
            "word_count": observation.word_count,
            "confidence": observation.confidence,
            "page_type": observation.page_type,
            "latency_ms": observation.latency_seconds * 1000.0,
            "error": observation.error,
        },
    }


def _initial_progress(mode: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "mode": mode,
        "completed_pages": 0,
        "committed_bytes": 0,
        "pipeline_wall_seconds": 0.0,
        "pipeline_process_seconds": 0.0,
        "segments": [],
        "finalized": False,
    }


def _validate_progress(
    progress: Mapping[str, Any],
    *,
    mode: str,
    selected_total: int,
) -> tuple[int, int]:
    if progress.get("schema_version") != 1 or progress.get("mode") != mode:
        raise BenchmarkError(f"invalid {mode} resume progress schema")
    completed = progress.get("completed_pages")
    committed_bytes = progress.get("committed_bytes")
    if not isinstance(completed, int) or not 0 <= completed <= selected_total:
        raise BenchmarkError(f"invalid {mode} completed_pages in resume state")
    if not isinstance(committed_bytes, int) or committed_bytes < 0:
        raise BenchmarkError(f"invalid {mode} committed_bytes in resume state")
    return completed, committed_bytes


def _scan_page_file(
    path: Path,
    *,
    mode: str,
    expected_pages: int,
    offset: int,
    expected_bytes: int | None = None,
) -> RunAggregate:
    aggregate = RunAggregate()
    if not path.is_file():
        if expected_pages:
            raise BenchmarkError(f"missing {mode} page artifact: {path}")
        return aggregate
    if expected_bytes is not None and path.stat().st_size != expected_bytes:
        raise BenchmarkError(
            f"{mode} committed page bytes mismatch: "
            f"expected {expected_bytes}, found {path.stat().st_size}"
        )
    with path.open("r", encoding="utf-8") as stream:
        for row_number, line in enumerate(stream):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise BenchmarkError(
                    f"invalid persisted {mode} JSONL row {row_number}: {error}"
                ) from error
            if not isinstance(row, dict):
                raise BenchmarkError(f"persisted {mode} row is not an object")
            if row.get("mode") != mode:
                raise BenchmarkError(f"persisted row has wrong mode for {mode}")
            expected_index = offset + row_number
            if row.get("dataset_index") != expected_index:
                raise BenchmarkError(
                    f"{mode} resume rows are not a contiguous corpus prefix: "
                    f"expected index {expected_index}, found {row.get('dataset_index')}"
                )
            aggregate.add_row(row)
    if aggregate.overall.pages != expected_pages:
        raise BenchmarkError(
            f"{mode} resume rows mismatch: expected {expected_pages}, "
            f"found {aggregate.overall.pages}"
        )
    return aggregate


def _resume_mode_state(
    mode_dir: Path,
    *,
    mode: str,
    selected_total: int,
    offset: int,
) -> tuple[dict[str, Any], RunAggregate, Path, Path]:
    progress_path = mode_dir / "progress.json"
    partial_path = mode_dir / "pages.jsonl.partial"
    final_path = mode_dir / "pages.jsonl"
    if progress_path.is_file():
        progress = _read_json(progress_path)
    else:
        progress = _initial_progress(mode)
        _atomic_write_json(progress_path, progress)
    completed, committed_bytes = _validate_progress(
        progress,
        mode=mode,
        selected_total=selected_total,
    )

    if final_path.is_file():
        if partial_path.exists():
            raise BenchmarkError(f"{mode} has both final and partial page artifacts")
        if completed != selected_total:
            raise BenchmarkError(f"{mode} final page artifact exists before all rows completed")
        if final_path.stat().st_size != committed_bytes:
            raise BenchmarkError(f"{mode} final artifact size differs from progress")
        aggregate = _scan_page_file(
            final_path,
            mode=mode,
            expected_pages=completed,
            offset=offset,
            expected_bytes=committed_bytes,
        )
        if not progress.get("finalized"):
            progress = {**progress, "finalized": True}
            _atomic_write_json(progress_path, progress)
        return progress, aggregate, partial_path, final_path

    partial_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path.touch(exist_ok=True)
    size = partial_path.stat().st_size
    if size < committed_bytes:
        raise BenchmarkError(f"{mode} partial artifact is shorter than its committed checkpoint")
    if size > committed_bytes:
        # Rows beyond the atomically published checkpoint may be torn.  They
        # were never committed and are deliberately discarded before resume.
        with partial_path.open("r+b") as stream:
            stream.truncate(committed_bytes)
            stream.flush()
            os.fsync(stream.fileno())
    aggregate = _scan_page_file(
        partial_path,
        mode=mode,
        expected_pages=completed,
        offset=offset,
        expected_bytes=committed_bytes,
    )
    return progress, aggregate, partial_path, final_path


def _run_mode(
    *,
    mode: str,
    dataset: Path,
    output: Path,
    offset: int,
    limit: int | None,
    concurrency: int,
    checkpoint_every: int,
    scorer: Callable[[str, str, int], dict[str, float]],
    extractor: Callable[..., Any],
) -> dict[str, Any]:
    selected_total = _selected_total(offset, limit)
    mode_dir = output / mode
    mode_dir.mkdir(parents=True, exist_ok=True)
    progress, aggregate, partial_path, final_path = _resume_mode_state(
        mode_dir,
        mode=mode,
        selected_total=selected_total,
        offset=offset,
    )
    completed, _ = _validate_progress(
        progress,
        mode=mode,
        selected_total=selected_total,
    )
    if final_path.is_file():
        mode_summary = {
            "mode": mode,
            "selected_pages": selected_total,
            "resumed_from_pages": completed,
            "page_artifact": str(final_path.relative_to(output)),
            "page_artifact_sha256": _sha256(final_path),
            **aggregate.export(progress),
        }
        _atomic_write_json(mode_dir / "summary.json", mode_summary)
        return mode_summary

    resumed_from = completed
    with (
        concurrent.futures.ThreadPoolExecutor(
            max_workers=concurrency,
            thread_name_prefix=f"webmainbench-{mode}",
        ) as executor,
        partial_path.open("ab") as output_stream,
    ):
        for records in _record_batches(
            dataset,
            offset=offset,
            limit=limit,
            skip_selected=completed,
            batch_size=checkpoint_every,
            completed_ids=aggregate.completed_ids,
        ):
            inputs = [
                ExtractionInput(
                    dataset_index=record.dataset_index,
                    track_id=record.track_id,
                    url=record.url,
                    html=record.html,
                )
                for record in records
            ]
            wall_started = time.perf_counter()
            process_started = time.process_time()
            futures = [
                executor.submit(
                    _extract_one,
                    item,
                    mode=mode,
                    extractor=extractor,
                )
                for item in inputs
            ]
            observations = [future.result() for future in futures]
            segment_wall = time.perf_counter() - wall_started
            segment_process = time.process_time() - process_started

            rows = [
                _page_row(
                    record,
                    observation,
                    mode=mode,
                    scorer=scorer,
                )
                for record, observation in zip(
                    records,
                    observations,
                    strict=True,
                )
            ]
            for row in rows:
                payload = (
                    json.dumps(
                        row,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8")
                output_stream.write(payload)
                aggregate.add_row(row)
            output_stream.flush()
            os.fsync(output_stream.fileno())
            completed += len(rows)
            progress = {
                **progress,
                "completed_pages": completed,
                "committed_bytes": output_stream.tell(),
                "pipeline_wall_seconds": (float(progress["pipeline_wall_seconds"]) + segment_wall),
                "pipeline_process_seconds": (
                    float(progress["pipeline_process_seconds"]) + segment_process
                ),
                "segments": [
                    *progress["segments"],
                    {
                        "pages": len(rows),
                        "first_dataset_index": records[0].dataset_index,
                        "last_dataset_index": records[-1].dataset_index,
                        "pipeline_wall_seconds": segment_wall,
                        "pipeline_process_seconds": segment_process,
                        "committed_at_utc": dt.datetime.now(dt.UTC).isoformat(),
                    },
                ],
            }
            _atomic_write_json(mode_dir / "progress.json", progress)
            print(
                f"{mode}: {completed}/{selected_total}",
                file=sys.stderr,
                flush=True,
            )

    if completed != selected_total:
        raise BenchmarkError(f"{mode} completed {completed} pages, expected {selected_total}")
    os.replace(partial_path, final_path)
    _fsync_directory(final_path.parent)
    progress = {**progress, "finalized": True}
    _atomic_write_json(mode_dir / "progress.json", progress)
    mode_summary = {
        "mode": mode,
        "selected_pages": selected_total,
        "resumed_from_pages": resumed_from,
        "page_artifact": str(final_path.relative_to(output)),
        "page_artifact_sha256": _sha256(final_path),
        **aggregate.export(progress),
    }
    _atomic_write_json(mode_dir / "summary.json", mode_summary)
    return mode_summary


def _compare_modes(raw_path: Path, scrubbed_path: Path) -> dict[str, Any]:
    deltas: list[float] = []
    prediction_equal = 0
    scrubbed_better = 0
    raw_better = 0
    score_equal = 0
    pages = 0
    with (
        raw_path.open("r", encoding="utf-8") as raw_stream,
        scrubbed_path.open("r", encoding="utf-8") as scrubbed_stream,
    ):
        while True:
            raw_line = raw_stream.readline()
            scrubbed_line = scrubbed_stream.readline()
            if not raw_line and not scrubbed_line:
                break
            if not raw_line or not scrubbed_line:
                raise BenchmarkError("raw and scrubbed page artifacts differ in length")
            raw = json.loads(raw_line)
            scrubbed = json.loads(scrubbed_line)
            identity = (raw.get("dataset_index"), raw.get("track_id"))
            if identity != (
                scrubbed.get("dataset_index"),
                scrubbed.get("track_id"),
            ):
                raise BenchmarkError("raw and scrubbed page ordering differs")
            raw_score = _score_value(_require_mapping(raw, "scores"), "f1")
            scrubbed_score = _score_value(
                _require_mapping(scrubbed, "scores"),
                "f1",
            )
            delta = scrubbed_score - raw_score
            deltas.append(delta)
            pages += 1
            prediction_equal += int(raw.get("prediction") == scrubbed.get("prediction"))
            if delta > 0:
                scrubbed_better += 1
            elif delta < 0:
                raw_better += 1
            else:
                score_equal += 1
    return {
        "pages": pages,
        "delta_definition": "scrubbed F1 minus raw F1, paired by track_id",
        "mean_f1_delta": statistics.mean(deltas) if deltas else 0.0,
        "f1_delta": _distribution_summary(deltas),
        "scrubbed_better_pages": scrubbed_better,
        "raw_better_pages": raw_better,
        "equal_f1_pages": score_equal,
        "identical_prediction_pages": prediction_equal,
        "identical_prediction_rate": prediction_equal / pages if pages else 0.0,
    }


def _category(value: Any) -> str:
    if value is None or value == "":
        return "<missing>"
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _require_mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    nested = value.get(key)
    if not isinstance(nested, Mapping):
        raise BenchmarkError(f"page row field {key!r} is not an object")
    return nested


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BenchmarkError(f"{label} is not numeric")
    result = float(value)
    if not math.isfinite(result):
        raise BenchmarkError(f"{label} is not finite")
    return result


def _score_value(scores: Mapping[str, Any], key: str) -> float:
    return _finite_number(scores.get(key), f"score {key}")


def _percentile(values: list[float], probability: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _distribution_summary(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "mean": statistics.mean(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "min": min(values),
        "max": max(values),
    }


def _latency_summary(values: list[float]) -> dict[str, float] | None:
    return _distribution_summary(values)


def _peak_rss_bytes() -> int | None:
    try:
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (AttributeError, OSError):
        return None
    return int(value if sys.platform == "darwin" else value * 1024)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BenchmarkError(f"cannot read JSON artifact {path}: {error}") from error
    if not isinstance(value, dict):
        raise BenchmarkError(f"JSON artifact is not an object: {path}")
    return value


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or path.is_relative_to(parent)


def _prepare_output(
    requested: Path | None,
    *,
    resume: bool,
    dataset: Path,
    evaluator_root: Path,
) -> Path:
    if requested is None:
        if resume:
            raise BenchmarkError("--resume requires an explicit --output-dir")
        timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        requested = ROOT / "bench" / "results" / "webmainbench" / timestamp
    output = requested.resolve()
    if _is_within(output, evaluator_root):
        raise BenchmarkError("output directory must not be inside evaluator checkout")
    if _is_within(output, dataset.parent):
        raise BenchmarkError("output directory must not be inside dataset directory")
    if resume:
        if not output.is_dir() or not (output / "run_config.json").is_file():
            raise BenchmarkError("--resume requires an existing output with run_config.json")
    elif output.exists() and any(output.iterdir()):
        raise BenchmarkError(f"refusing to overwrite non-empty output: {output}")
    output.mkdir(parents=True, exist_ok=True)
    return output


def _artifact_manifest(output: Path) -> dict[str, Any]:
    artifacts: dict[str, dict[str, int | str]] = {}
    for path in sorted(output.rglob("*")):
        if not path.is_file() or path.name in {"manifest.json", "manifest.json.tmp"}:
            continue
        artifacts[path.relative_to(output).as_posix()] = {
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
    return {
        "schema_version": 1,
        "root": str(output),
        "artifacts": artifacts,
    }


def _modes(selected: str) -> list[str]:
    return list(MODES) if selected == "both" else [selected]


def _validate_args(args: argparse.Namespace) -> None:
    if args.offset < 0 or args.offset >= DATASET_RECORDS:
        raise BenchmarkError(f"--offset must be between 0 and {DATASET_RECORDS - 1}")
    if args.limit is not None and args.limit < 1:
        raise BenchmarkError("--limit must be positive")
    if args.concurrency is not None and args.concurrency < 1:
        raise BenchmarkError("--concurrency must be positive")
    if args.checkpoint_every < 1:
        raise BenchmarkError("--checkpoint-every must be positive")


def _resume_fingerprint_payload(
    *,
    args: argparse.Namespace,
    modes: list[str],
    concurrency: int,
    dataset: Mapping[str, Any],
    evaluator: Mapping[str, Any],
    source: Mapping[str, Any],
    environment: Mapping[str, Any],
) -> dict[str, Any]:
    native = _require_mapping(environment, "native_backend")
    production = _require_mapping(environment, "production_extraction")
    return {
        "schema_version": 1,
        "dataset": {
            "path": dataset["path"],
            "revision": DATASET_REVISION,
            "bytes": DATASET_BYTES,
            "sha256": DATASET_SHA256,
            "records": DATASET_RECORDS,
        },
        "evaluator": {
            "root": evaluator["root"],
            "commit": EVALUATOR_COMMIT,
            "tree": EVALUATOR_TREE,
            "sha256": EVALUATOR_SHA256,
            "dependencies": EVALUATOR_DEPENDENCIES,
        },
        "selection": {
            "modes": modes,
            "offset": args.offset,
            "limit": args.limit,
        },
        "execution": {
            "concurrency": concurrency,
            "checkpoint_every": args.checkpoint_every,
            "entry_point": "app.services.extractor.extract_content",
            "profile": EXTRACTION_PROFILE,
            "prediction_transform": "identity ExtractionResult.text",
        },
        "source_digest": source["source_digest"],
        "source_git_commit": source.get("git_commit"),
        "python": environment.get("python"),
        "platform": environment.get("platform"),
        "dependencies": environment.get("dependencies"),
        "native_backend": native,
        "production_settings": production,
    }


def _write_or_validate_run_config(
    output: Path,
    *,
    resume: bool,
    payload: dict[str, Any],
    dataset: Mapping[str, Any],
    evaluator: Mapping[str, Any],
    source: Mapping[str, Any],
    environment: Mapping[str, Any],
) -> dict[str, Any]:
    fingerprint = _hash_json(payload)
    path = output / "run_config.json"
    if resume:
        existing = _read_json(path)
        if existing.get("resume_fingerprint") != fingerprint:
            raise BenchmarkError(
                "resume fingerprint mismatch; source, inputs, evaluator, "
                "settings, or run arguments changed"
            )
        return existing
    config = {
        "schema_version": 1,
        "created_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "resume_fingerprint": fingerprint,
        "resume_fingerprint_payload": payload,
        "dataset": dataset,
        "evaluator": evaluator,
        "source_before": source,
        "environment": environment,
    }
    _atomic_write_json(path, config)
    return config


def _claimability(
    *,
    args: argparse.Namespace,
    modes: list[str],
    source_before: Mapping[str, Any],
    source_after: Mapping[str, Any],
    native_before: Mapping[str, Any],
    native_after: Mapping[str, Any],
    mode_summaries: Mapping[str, Mapping[str, Any]],
    label_guard: Mapping[str, Any],
) -> dict[str, Any]:
    reasons: list[str] = []
    if args.offset != 0:
        reasons.append(f"nonzero corpus offset ({args.offset})")
    if args.limit is not None:
        reasons.append(f"limited corpus run ({args.limit} selected pages)")
    if set(modes) != set(MODES):
        reasons.append("both raw and scrubbed tracks were not run")
    if source_before.get("git_dirty") or source_after.get("git_dirty"):
        reasons.append("Clusy source worktree was dirty")
    if source_before.get("source_digest") != source_after.get("source_digest"):
        reasons.append("relevant source changed during the run")
    if native_before != native_after:
        reasons.append("loaded native module changed during the run")
    if not label_guard.get("passed"):
        reasons.append("label-leak source guard did not pass")
    for mode, summary in mode_summaries.items():
        errors = summary.get("errors")
        if errors:
            reasons.append(f"{mode} had {errors} extraction errors")
        if summary.get("selected_pages") != DATASET_RECORDS:
            reasons.append(f"{mode} did not contain all {DATASET_RECORDS} pages")
    return {
        "claimable_on_fixed_public_protocol": not reasons,
        "reasons": reasons,
        "scope": (
            "WebMainBench v1.1 offline main-content extraction only; "
            "not fetching, rendering, discovery, or production reliability"
        ),
        "universal_or_blind_sota_claimable": False,
        "warning": (
            "All labels are public, there is no hidden test server, and the "
            "benchmark HTML contains annotation artifacts.  A clean full run "
            "is eligible only for a precisely scoped, dated comparison."
        ),
    }


def _print_summary(summary: Mapping[str, Any], output: Path) -> None:
    print(f"WebMainBench dataset verified: {DATASET_REVISION}")
    print(f"Official evaluator verified: {EVALUATOR_COMMIT}")
    modes = _require_mapping(summary, "modes")
    for mode, metrics_value in modes.items():
        if not isinstance(metrics_value, Mapping):
            raise BenchmarkError(f"mode summary is not an object: {mode}")
        metrics = metrics_value
        timing = _require_mapping(metrics, "timing")
        throughput = timing.get("pages_per_pipeline_wall_second")
        throughput_text = (
            f"{float(throughput):.2f}" if isinstance(throughput, (int, float)) else "n/a"
        )
        print(
            f"{mode}: pages={metrics['pages']} "
            f"P={float(metrics['precision']):.6f} "
            f"R={float(metrics['recall']):.6f} "
            f"F1={float(metrics['f1']):.6f} "
            f"pages/s={throughput_text} errors={metrics['errors']}"
        )
    claimability = _require_mapping(summary, "claimability")
    if claimability.get("claimable_on_fixed_public_protocol"):
        print("PROTOCOL-VALID; claim scope remains limited to this public benchmark")
    else:
        print("NOT CLAIMABLE: " + "; ".join(claimability.get("reasons", [])))
    print(f"artifacts: {output}")


def run_benchmark(args: argparse.Namespace) -> int:
    _validate_args(args)
    dataset_path = args.dataset.resolve()
    evaluator_root = args.evaluator_root.resolve()
    dataset_before = verify_dataset(dataset_path)
    evaluator_before = verify_evaluator(evaluator_root)
    source_before = source_provenance()
    label_guard = scan_label_leak_guard()

    scorer, evaluator_dependencies = load_official_scorer(evaluator_root)
    from app.config import settings
    from app.services.extractor import extract_content, native_backend_version

    concurrency = args.concurrency
    if concurrency is None:
        concurrency = max(
            1,
            int(getattr(settings, "max_concurrent_extractions", 1)),
        )
    modes = _modes(args.mode)
    environment = _environment_metadata(settings, native_backend_version)
    environment["official_evaluator_dependencies"] = evaluator_dependencies
    output = _prepare_output(
        args.output_dir,
        resume=args.resume,
        dataset=dataset_path,
        evaluator_root=evaluator_root,
    )
    fingerprint_payload = _resume_fingerprint_payload(
        args=args,
        modes=modes,
        concurrency=concurrency,
        dataset=dataset_before,
        evaluator=evaluator_before,
        source=source_before,
        environment=environment,
    )
    run_config = _write_or_validate_run_config(
        output,
        resume=args.resume,
        payload=fingerprint_payload,
        dataset=dataset_before,
        evaluator=evaluator_before,
        source=source_before,
        environment=environment,
    )
    original_source_before = _require_mapping(run_config, "source_before")
    _atomic_write_json(output / "label_leak_guard.json", label_guard)

    mode_summaries: dict[str, dict[str, Any]] = {}
    for mode in modes:
        mode_summaries[mode] = _run_mode(
            mode=mode,
            dataset=dataset_path,
            output=output,
            offset=args.offset,
            limit=args.limit,
            concurrency=concurrency,
            checkpoint_every=args.checkpoint_every,
            scorer=scorer,
            extractor=extract_content,
        )

    # Rehash immutable external inputs and all relevant source after the final
    # prediction.  Dataset/evaluator changes fail closed; source changes are
    # preserved as a NOT CLAIMABLE reason.
    dataset_after = verify_dataset(dataset_path)
    evaluator_after = verify_evaluator(evaluator_root)
    source_after = source_provenance()
    native_before = _require_mapping(
        _require_mapping(environment, "native_backend"),
        "module",
    )
    native_after = _native_module_metadata()
    comparison = None
    if set(modes) == set(MODES):
        comparison = _compare_modes(
            output / "raw" / "pages.jsonl",
            output / "scrubbed" / "pages.jsonl",
        )
    claimability = _claimability(
        args=args,
        modes=modes,
        source_before=original_source_before,
        source_after=source_after,
        native_before=native_before,
        native_after=native_after,
        mode_summaries=mode_summaries,
        label_guard=label_guard,
    )
    summary = {
        "schema_version": 1,
        "completed_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "benchmark": {
            "name": "WebMainBench v1.1",
            "public_ground_truth": True,
            "pages": DATASET_RECORDS,
            "official_metric": (
                "arithmetic mean of per-page jieba-tokenized ROUGE-5 precision, recall, and F1"
            ),
            "raw_track": "untouched official benchmark HTML",
            "scrubbed_track": ("annotation-marker-scrubbed HTML with untouched ground truth"),
        },
        "selection": {
            "offset": args.offset,
            "limit": args.limit,
            "selected_pages": _selected_total(args.offset, args.limit),
            "modes": modes,
        },
        "dataset": {
            "before": dataset_before,
            "after": dataset_after,
            "stable": dataset_before["sha256"] == dataset_after["sha256"],
        },
        "evaluator": {
            "before": evaluator_before,
            "after": evaluator_after,
            "stable": (evaluator_before["evaluator_sha256"] == evaluator_after["evaluator_sha256"]),
            "function": "eval_baselines/utils.py::calc_rouge_n_score",
            "n": 5,
            "dependencies": evaluator_dependencies,
        },
        "source": {
            "before": original_source_before,
            "after": source_after,
            "stable": (original_source_before["source_digest"] == source_after["source_digest"]),
        },
        "label_leak_guard": label_guard,
        "run_configuration": {
            "entry_point": "app.services.extractor.extract_content",
            "extraction_profile": EXTRACTION_PROFILE,
            "prediction_transform": "identity ExtractionResult.text",
            "concurrency": concurrency,
            "checkpoint_every": args.checkpoint_every,
            "resumed": args.resume,
        },
        "modes": mode_summaries,
        "raw_vs_scrubbed": comparison,
        "environment": {
            **environment,
            "native_module_after": native_after,
            "native_module_stable": native_before == native_after,
            "peak_rss_bytes": _peak_rss_bytes(),
        },
        "claimability": claimability,
    }
    _atomic_write_json(output / "summary.json", summary)
    if not claimability["claimable_on_fixed_public_protocol"]:
        _atomic_write_text(
            output / "NOT_CLAIMABLE.txt",
            "NOT CLAIMABLE\n\n"
            + "\n".join(f"- {reason}" for reason in claimability["reasons"])
            + "\n\nThis artifact may be used for development diagnostics only.\n",
        )
    _atomic_write_json(output / "manifest.json", _artifact_manifest(output))
    _print_summary(summary, output)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run_benchmark(args)
    except BenchmarkError as error:
        print(f"benchmark error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("benchmark interrupted; rerun with --resume", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

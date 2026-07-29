#!/usr/bin/env python3
"""Reproducible production-extractor run on the pinned WCXB corpus.

The runner fails closed unless the corpus is the exact clean commit pinned
below. Predictions are the unchanged output of ``extract_content_async`` with
an explicitly recorded production extraction profile. Scoring is delegated to
WCXB's own ``evaluate_results`` implementation, including its snippet metrics.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import gc
import gzip
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import math
import os
import platform
import random
import resource
import statistics
import subprocess
import sys
import time
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench.source_provenance import (  # noqa: E402
    SourceInventoryError,
    git_visible_vendor_files,
)

WCXB_REPOSITORY = (
    "https://github.com/Murrough-Foley/web-content-extraction-benchmark.git"
)
WCXB_COMMIT = "c039d5ee9f5a3a984a0e167e63aacd04e76e78a9"
WCXB_GIT_TREE = "1d3d493fed8c3e01f3c62f817b2123548c5cfd1a"
WCXB_INPUT_TREE_SHA256 = (
    "4d5c9be2094ba5a2b5a8046fdc846b0518a799b60710b4581b457f9731bc3aae"
)
WCXB_EVALUATOR_SHA256 = (
    "ae4ad6299e190177fbb04a7c1190077fc6086b3fa0f31fb37a6538a5e979c559"
)
WCXB_METADATA_SHA256 = (
    "f01ae8e0cf7d7a97f8a05e2a6c6416c56fcd758e5686bf932f1e59f3b3acdba5"
)
WCXB_LICENSE_SHA256 = (
    "6410290cc35ef75d893240d3736e96dc383ed123780702e56cf1a090ae003c72"
)
EXPECTED_SPLIT_PAGES = {"dev": 1497, "test": 511}
DEFAULT_EXTRACTION_PROFILE = "balanced"
EXTRACTION_PROFILES = ("balanced", "article_body", "adaptive", "quality")
DEFAULT_SEED = 20260727
OPAQUE_CLASSIFIER_NAME = "web-page-classifier"
OPAQUE_CLASSIFIER_VERSION = "0.1.0"
OPAQUE_CLASSIFIER_CHECKSUM = "557ae9fe8bf3f86d972a8604cc5fe8c897359de9657fe7a3eda4fddfac7f3856"
OPAQUE_CLASSIFIER_SOURCE = "https://github.com/Murrough-Foley/web-page-classifier"

SOURCE_FIXED_FILES = (
    "bench/wcxb_benchmark.py",
    "bench/source_provenance.py",
    "pyproject.toml",
    "uv.lock",
    "native/Cargo.toml",
    "native/Cargo.lock",
    "native/pyproject.toml",
)


class BenchmarkError(RuntimeError):
    """The requested run cannot produce a provenance-valid result."""


@dataclass(frozen=True)
class PageJob:
    file_id: str
    url: str
    html_path: Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Clusy on the commit-pinned WCXB corpus.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=Path("/tmp/clusy-wcxb"),
        help="clean checkout of the pinned WCXB commit",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="artifact directory; defaults under ignored bench/results/wcxb",
    )
    parser.add_argument(
        "--splits",
        nargs="+",
        choices=("dev", "test"),
        default=("dev", "test"),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        help="page workers; defaults to production max_concurrent_extractions",
    )
    parser.add_argument(
        "--extraction-profile",
        choices=EXTRACTION_PROFILES,
        default=DEFAULT_EXTRACTION_PROFILE,
        help="production extraction profile passed unchanged to the extractor",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--warmup-pages",
        type=int,
        default=3,
        help="warmups per selected split, excluded from timing and scoring",
    )
    parser.add_argument(
        "--limit-per-split",
        type=int,
        help="deterministic smoke subset; artifacts are visibly non-comparable",
    )
    return parser.parse_args()


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        if check:
            raise BenchmarkError(f"{' '.join(args)} failed: {error}") from error
        return subprocess.CompletedProcess(args, 127, "", str(error))
    if check and result.returncode:
        message = result.stderr.strip() or result.stdout.strip()
        raise BenchmarkError(f"{' '.join(args)} failed: {message}")
    return result


def _embedded_classifier_provenance() -> dict[str, Any]:
    document = tomllib.loads((ROOT / "native" / "Cargo.lock").read_text(encoding="utf-8"))
    packages = [
        package
        for package in document.get("package", [])
        if package.get("name") == OPAQUE_CLASSIFIER_NAME
    ]
    if not packages:
        return {
            "embedded": False,
            "training_manifest_verified": False,
        }
    if len(packages) != 1:
        raise BenchmarkError("native Cargo.lock contains multiple web-page-classifier packages")
    package = packages[0]
    version = str(package.get("version", ""))
    checksum = str(package.get("checksum", ""))
    pinned_known_release = (
        version == OPAQUE_CLASSIFIER_VERSION and checksum == OPAQUE_CLASSIFIER_CHECKSUM
    )
    return {
        "embedded": True,
        "name": OPAQUE_CLASSIFIER_NAME,
        "version": version,
        "checksum": checksum,
        "source": OPAQUE_CLASSIFIER_SOURCE,
        "pinned_known_release": pinned_known_release,
        "publisher_reported_training_pages": 1497 if pinned_known_release else None,
        "publisher_reported_page_types": 7 if pinned_known_release else None,
        "training_item_or_split_manifest": None,
        "training_manifest_verified": False,
        "interpretation": (
            "The publisher reports 1,497 training pages across the same seven "
            "page types as WCXB development, but publishes no item/split "
            "manifest. Exact overlap is therefore unresolved rather than "
            "asserted."
        ),
    }


def _git(root: Path, *args: str) -> str:
    return _run(["git", *args], cwd=root).stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _input_tree_digest(corpus: Path) -> str:
    """Hash the evaluator and every scored input by relative name and bytes."""
    digest = hashlib.sha256()
    paths = [corpus / "evaluate.py", corpus / "metadata.json", corpus / "LICENSE"]
    for split in ("dev", "test"):
        paths.extend(sorted((corpus / split / "ground-truth").glob("*.json")))
        paths.extend(sorted((corpus / split / "html").glob("*.html.gz")))
    for path in paths:
        relative = path.relative_to(corpus).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def verify_corpus(corpus: Path) -> dict[str, Any]:
    if not corpus.is_dir() or not (corpus / ".git").exists():
        raise BenchmarkError(
            f"{corpus} is not a Git checkout. Clone {WCXB_REPOSITORY} and "
            f"check out {WCXB_COMMIT}."
        )
    commit = _git(corpus, "rev-parse", "HEAD")
    if commit != WCXB_COMMIT:
        raise BenchmarkError(
            "refusing mutable or mismatched WCXB checkout: "
            f"expected {WCXB_COMMIT}, found {commit}. "
            f"Run `git -C {corpus} checkout --detach {WCXB_COMMIT}`."
        )
    git_tree = _git(corpus, "rev-parse", "HEAD^{tree}")
    if git_tree != WCXB_GIT_TREE:
        raise BenchmarkError(
            f"WCXB Git tree mismatch: expected {WCXB_GIT_TREE}, found {git_tree}"
        )
    tracked_status = _git(
        corpus,
        "status",
        "--porcelain",
        "--untracked-files=no",
    )
    if tracked_status:
        raise BenchmarkError(
            "WCXB has tracked modifications; restore the pinned corpus:\n"
            + tracked_status
        )

    critical_hashes = {
        "evaluate.py": WCXB_EVALUATOR_SHA256,
        "metadata.json": WCXB_METADATA_SHA256,
        "LICENSE": WCXB_LICENSE_SHA256,
    }
    for relative, expected in critical_hashes.items():
        path = corpus / relative
        if not path.is_file():
            raise BenchmarkError(f"pinned WCXB file is missing: {relative}")
        actual = _sha256(path)
        if actual != expected:
            raise BenchmarkError(
                f"WCXB hash mismatch for {relative}: expected {expected}, found {actual}"
            )

    input_digest = _input_tree_digest(corpus)
    if input_digest != WCXB_INPUT_TREE_SHA256:
        raise BenchmarkError(
            "WCXB evaluator/input tree mismatch: "
            f"expected {WCXB_INPUT_TREE_SHA256}, found {input_digest}"
        )

    split_counts: dict[str, int] = {}
    for split, expected_count in EXPECTED_SPLIT_PAGES.items():
        gt_dir = corpus / split / "ground-truth"
        html_dir = corpus / split / "html"
        gt_ids = {path.stem for path in gt_dir.glob("*.json")}
        html_ids = {
            path.name.removesuffix(".html.gz")
            for path in html_dir.glob("*.html.gz")
        }
        if gt_ids != html_ids:
            raise BenchmarkError(
                f"WCXB {split} ground-truth/HTML IDs differ: "
                f"{len(gt_ids - html_ids)} missing HTML, "
                f"{len(html_ids - gt_ids)} missing ground truth"
            )
        if len(gt_ids) != expected_count:
            raise BenchmarkError(
                f"WCXB {split} size mismatch: expected {expected_count}, "
                f"found {len(gt_ids)}"
            )
        split_counts[split] = len(gt_ids)

    branch_result = _run(
        ["git", "symbolic-ref", "--short", "-q", "HEAD"],
        cwd=corpus,
        check=False,
    )
    origin_result = _run(
        ["git", "remote", "get-url", "origin"],
        cwd=corpus,
        check=False,
    )
    return {
        "repository": WCXB_REPOSITORY,
        "root": str(corpus),
        "expected_commit": WCXB_COMMIT,
        "actual_commit": commit,
        "git_tree": git_tree,
        "input_tree_sha256": input_digest,
        "evaluator_sha256": WCXB_EVALUATOR_SHA256,
        "metadata_sha256": WCXB_METADATA_SHA256,
        "license_sha256": WCXB_LICENSE_SHA256,
        "branch": branch_result.stdout.strip() or None,
        "origin": origin_result.stdout.strip() or None,
        "tracked_worktree_clean": True,
        "split_pages": split_counts,
        "pages": sum(split_counts.values()),
        "verified": True,
    }


def _load_official_evaluator(corpus: Path) -> ModuleType:
    evaluator_path = corpus / "evaluate.py"
    spec = importlib.util.spec_from_file_location(
        f"clusy_pinned_wcxb_evaluator_{WCXB_COMMIT[:12]}",
        evaluator_path,
    )
    if spec is None or spec.loader is None:
        raise BenchmarkError(f"could not import official evaluator: {evaluator_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    required = (
        "load_ground_truth",
        "evaluate_results",
        "tokenize",
        "word_f1",
        "snippet_check",
        "get_page_type",
    )
    missing = [name for name in required if not callable(getattr(module, name, None))]
    if missing:
        raise BenchmarkError(
            "pinned WCXB evaluator API is missing: " + ", ".join(missing)
        )
    return module


def percentile(values: list[float], probability: float) -> float:
    if not values:
        raise ValueError("percentile requires values")
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


def _derived_seed(seed: int, label: str) -> int:
    digest = hashlib.sha256(f"{seed}:{label}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _source_hashes() -> dict[str, str]:
    paths = {
        ROOT / relative
        for relative in SOURCE_FIXED_FILES
        if (ROOT / relative).is_file()
    }
    paths.update((ROOT / "app").rglob("*.py"))
    paths.update((ROOT / "native" / "src").rglob("*.rs"))
    paths.update((ROOT / "native" / "python").rglob("*.py"))
    paths.update((ROOT / "native" / "python").rglob("*.pyi"))
    try:
        paths.update(git_visible_vendor_files(ROOT))
    except SourceInventoryError as error:
        raise BenchmarkError(f"native vendor source inventory failed: {error}") from error
    py_typed = ROOT / "native" / "python" / "clusy_native" / "py.typed"
    if py_typed.is_file():
        paths.add(py_typed)
    return {
        path.relative_to(ROOT).as_posix(): _sha256(path)
        for path in sorted(paths)
        if path.is_file()
    }


def _source_provenance() -> dict[str, Any]:
    commit_result = _run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=False)
    status_result = _run(["git", "status", "--porcelain"], cwd=ROOT, check=False)
    status = [line for line in status_result.stdout.splitlines() if line]
    return {
        "root": str(ROOT),
        "git_commit": commit_result.stdout.strip() or None,
        "git_dirty": bool(status),
        "git_status": status,
        "source_sha256": _source_hashes(),
    }


def _package_versions() -> dict[str, str | None]:
    packages = (
        "clusy-native",
        "trafilatura",
        "readability-lxml",
        "markdownify",
        "lxml",
        "pydantic",
        "orjson",
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
            _sha256(extension)
            if extension is not None and extension.is_file()
            else None
        ),
        "package_path": str(package) if package else None,
        "package_sha256": (
            _sha256(package) if package is not None and package.is_file() else None
        ),
    }


def _peak_rss_bytes() -> int | None:
    try:
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (AttributeError, OSError):
        return None
    return int(value if sys.platform == "darwin" else value * 1024)


def _environment_metadata(
    settings: Any,
    native_backend_version: Callable[[], str],
    *,
    extraction_profile: str,
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
            "loaded_module": _native_module_metadata(),
            "rustc": rust_result.stdout.strip() or None,
            "cargo_toml_sha256": (
                _sha256(ROOT / "native" / "Cargo.toml")
                if (ROOT / "native" / "Cargo.toml").is_file()
                else None
            ),
            "cargo_lock_sha256": (
                _sha256(ROOT / "native" / "Cargo.lock")
                if (ROOT / "native" / "Cargo.lock").is_file()
                else None
            ),
        },
        "production_extraction": {
            "entry_point": "app.services.extractor.extract_content_async",
            "profile": extraction_profile,
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


def _prepare_jobs(
    corpus: Path,
    split: str,
    *,
    seed: int,
    limit: int | None,
) -> tuple[list[PageJob], list[str]]:
    jobs: list[PageJob] = []
    for gt_path in sorted((corpus / split / "ground-truth").glob("*.json")):
        document = json.loads(gt_path.read_text(encoding="utf-8"))
        jobs.append(
            PageJob(
                file_id=gt_path.stem,
                url=str(document.get("url", "") or ""),
                html_path=corpus / split / "html" / f"{gt_path.stem}.html.gz",
            )
        )
    random.Random(_derived_seed(seed, split)).shuffle(jobs)
    if limit is not None:
        jobs = jobs[:limit]
    return jobs, sorted(job.file_id for job in jobs)


def _read_html(path: Path) -> str:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return handle.read()


async def _extract_split(
    jobs: list[PageJob],
    *,
    split: str,
    concurrency: int,
    warmup_pages: int,
    extraction_profile: str,
    extractor: Callable[..., Awaitable[Any]],
) -> tuple[dict[str, str], dict[str, dict[str, Any]], dict[str, Any]]:
    warmup_count = min(warmup_pages, len(jobs))
    for job in jobs[:warmup_count]:
        html = _read_html(job.html_path)
        await extractor(
            html,
            job.url,
            extraction_profile=extraction_profile,
        )

    gc.collect()
    queue: asyncio.Queue[PageJob] = asyncio.Queue()
    for job in jobs:
        queue.put_nowait(job)
    predictions: dict[str, str] = {}
    extraction_rows: dict[str, dict[str, Any]] = {}
    completed = 0

    async def worker() -> None:
        nonlocal completed
        while True:
            try:
                job = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                html = _read_html(job.html_path)
                started = time.perf_counter()
                result = await extractor(
                    html,
                    job.url,
                    extraction_profile=extraction_profile,
                )
                extraction_seconds = time.perf_counter() - started
                predictions[job.file_id] = str(result.text or "")
                extraction_rows[job.file_id] = {
                    "strategy": str(getattr(result, "strategy", "") or ""),
                    "word_count": int(getattr(result, "word_count", 0) or 0),
                    "confidence": getattr(result, "confidence", None),
                    "page_type_predicted": str(
                        getattr(result, "page_type", "") or ""
                    ),
                    "extraction_seconds": extraction_seconds,
                    "error": None,
                }
            except Exception as error:
                predictions[job.file_id] = ""
                extraction_rows[job.file_id] = {
                    "strategy": "error",
                    "word_count": 0,
                    "confidence": None,
                    "page_type_predicted": "",
                    "extraction_seconds": 0.0,
                    "error": {
                        "type": type(error).__name__,
                        "message": str(error)[:1000],
                    },
                }
            finally:
                completed += 1
                if completed % 100 == 0 or completed == len(jobs):
                    print(
                        f"{split}: {completed}/{len(jobs)}",
                        file=sys.stderr,
                        flush=True,
                    )
                queue.task_done()

    wall_started = time.perf_counter()
    process_started = time.process_time()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    wall_seconds = time.perf_counter() - wall_started
    process_seconds = time.process_time() - process_started
    expected_ids = {job.file_id for job in jobs}
    if set(predictions) != expected_ids or set(extraction_rows) != expected_ids:
        raise BenchmarkError(f"internal {split} extraction key mismatch")
    latencies = [
        float(row["extraction_seconds"]) * 1000.0
        for row in extraction_rows.values()
    ]
    timing = {
        "measurement_scope": (
            "closed-loop local corpus read/decompression plus exact production "
            "extraction; per-page latency covers extraction only"
        ),
        "load_model": "closed-loop bounded worker pool",
        "concurrency": concurrency,
        "warmup_pages": warmup_count,
        "wall_seconds": wall_seconds,
        "process_seconds": process_seconds,
        "pages_per_wall_second": len(jobs) / wall_seconds if wall_seconds else None,
        "summed_extraction_seconds": sum(latencies) / 1000.0,
        "latency_ms": {
            "mean": statistics.mean(latencies),
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "min": min(latencies),
            "max": max(latencies),
        },
    }
    return predictions, extraction_rows, timing


def _score_with_official_evaluator(
    evaluator: ModuleType,
    *,
    split: str,
    predictions: dict[str, str],
    selected_ids: list[str],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], str]:
    all_ground_truth = evaluator.load_ground_truth(split)
    missing = [file_id for file_id in selected_ids if file_id not in all_ground_truth]
    if missing:
        raise BenchmarkError(
            f"official evaluator omitted {len(missing)} selected {split} pages"
        )
    ground_truth = {file_id: all_ground_truth[file_id] for file_id in selected_ids}
    if set(predictions) != set(ground_truth):
        raise BenchmarkError(f"{split} prediction keys do not match scored keys")
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        rows = evaluator.evaluate_results(
            ground_truth,
            predictions,
            per_type=True,
        )
    if {row["file_id"] for row in rows} != set(selected_ids):
        raise BenchmarkError(f"official evaluator returned incomplete {split} rows")
    return ground_truth, rows, stdout.getvalue()


def _aggregate_rows(
    *,
    official_rows: list[dict[str, Any]],
    ground_truth: dict[str, dict[str, Any]],
    predictions: dict[str, str],
    extraction_rows: dict[str, dict[str, Any]],
    evaluator: ModuleType,
    timing: dict[str, Any],
    official_stdout: str,
) -> dict[str, Any]:
    merged_rows: list[dict[str, Any]] = []
    for official_row in official_rows:
        file_id = official_row["file_id"]
        merged_rows.append(
            {
                **official_row,
                **extraction_rows[file_id],
                "predicted_words": len(evaluator.tokenize(predictions[file_id])),
                "reference_words": len(
                    evaluator.tokenize(ground_truth[file_id]["main_content"])
                ),
            }
        )

    def mean(key: str, rows: list[dict[str, Any]]) -> float:
        return (
            statistics.mean(float(row[key]) for row in rows)
            if rows
            else 0.0
        )

    by_type: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in merged_rows:
        by_type[str(row["page_type"])].append(row)
    errors = [
        {"file_id": row["file_id"], **row["error"]}
        for row in merged_rows
        if row["error"] is not None
    ]
    return {
        "pages": len(merged_rows),
        "precision": mean("precision", merged_rows),
        "recall": mean("recall", merged_rows),
        "f1": mean("f1", merged_rows),
        "with_snippet_rate": mean("with_rate", merged_rows),
        "without_snippet_rate": mean("without_rate", merged_rows),
        "without_snippet_rate_direction": "lower is better",
        "per_page_type": {
            page_type: {
                "pages": len(rows),
                "precision": mean("precision", rows),
                "recall": mean("recall", rows),
                "f1": mean("f1", rows),
                "with_snippet_rate": mean("with_rate", rows),
                "without_snippet_rate": mean("without_rate", rows),
            }
            for page_type, rows in sorted(by_type.items())
        },
        "strategy_counts": dict(
            sorted(Counter(str(row["strategy"]) for row in merged_rows).items())
        ),
        "errors": errors,
        "timing": timing,
        "official_evaluator": {
            "function": "evaluate.py::evaluate_results",
            "stdout": official_stdout,
            "semantics": (
                "macro mean of per-page bag-of-words precision, recall, and F1; "
                "official with/without snippet checks"
            ),
        },
        "pages_detail": sorted(merged_rows, key=lambda row: row["file_id"]),
    }


def _aggregate_full(splits: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rows = [
        row
        for split_metrics in splits.values()
        for row in split_metrics["pages_detail"]
    ]

    def mean(key: str, values: list[dict[str, Any]]) -> float:
        return (
            statistics.mean(float(row[key]) for row in values)
            if values
            else 0.0
        )

    by_type: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_type[str(row["page_type"])].append(row)
    wall_seconds = sum(
        float(metrics["timing"]["wall_seconds"]) for metrics in splits.values()
    )
    latencies = [float(row["extraction_seconds"]) * 1000.0 for row in rows]
    return {
        "pages": len(rows),
        "precision": mean("precision", rows),
        "recall": mean("recall", rows),
        "f1": mean("f1", rows),
        "with_snippet_rate": mean("with_rate", rows),
        "without_snippet_rate": mean("without_rate", rows),
        "without_snippet_rate_direction": "lower is better",
        "per_page_type": {
            page_type: {
                "pages": len(values),
                "precision": mean("precision", values),
                "recall": mean("recall", values),
                "f1": mean("f1", values),
                "with_snippet_rate": mean("with_rate", values),
                "without_snippet_rate": mean("without_rate", values),
            }
            for page_type, values in sorted(by_type.items())
        },
        "strategy_counts": dict(
            sorted(Counter(str(row["strategy"]) for row in rows).items())
        ),
        "errors": [
            error
            for metrics in splits.values()
            for error in metrics["errors"]
        ],
        "timing": {
            "measurement_scope": (
                "sum of sequential split runs; see split timing for details"
            ),
            "wall_seconds": wall_seconds,
            "process_seconds": sum(
                float(metrics["timing"]["process_seconds"])
                for metrics in splits.values()
            ),
            "summed_extraction_seconds": sum(
                float(metrics["timing"]["summed_extraction_seconds"])
                for metrics in splits.values()
            ),
            "pages_per_wall_second": len(rows) / wall_seconds,
            "latency_ms": {
                "mean": statistics.mean(latencies),
                "p50": percentile(latencies, 0.50),
                "p95": percentile(latencies, 0.95),
                "min": min(latencies),
                "max": max(latencies),
            },
        },
        "official_evaluator": {
            "semantics": (
                "macro mean across every official per-page result from both splits"
            )
        },
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    temporary.replace(path)


def _write_text(path: Path, value: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _make_output_dir(
    requested: Path | None,
    *,
    corpus: Path,
    smoke: bool,
) -> Path:
    if requested is None:
        timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        prefix = "smoke-" if smoke else ""
        requested = ROOT / "bench" / "results" / "wcxb" / f"{prefix}{timestamp}"
    output = requested.resolve()
    if output == corpus or output.is_relative_to(corpus):
        raise BenchmarkError("output directory must not be inside the pinned corpus")
    if output.exists() and any(output.iterdir()):
        raise BenchmarkError(f"refusing to overwrite non-empty output: {output}")
    output.mkdir(parents=True, exist_ok=True)
    return output


def _artifact_manifest(output: Path) -> dict[str, Any]:
    artifacts = {}
    for path in sorted(output.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        artifacts[str(path.relative_to(output))] = {
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
    return {"schema_version": 1, "root": str(output), "artifacts": artifacts}


def _print_summary(report: dict[str, Any], output: Path) -> None:
    print(f"WCXB provenance verified: {WCXB_COMMIT}")
    for split, metrics in report["splits"].items():
        print(
            f"{split}: pages={metrics['pages']} F1={metrics['f1']:.6f} "
            f"P={metrics['precision']:.6f} R={metrics['recall']:.6f} "
            f"pages/s={metrics['timing']['pages_per_wall_second']:.2f} "
            f"p50={metrics['timing']['latency_ms']['p50']:.2f}ms "
            f"p95={metrics['timing']['latency_ms']['p95']:.2f}ms"
        )
    if "full" in report:
        print(
            f"full: pages={report['full']['pages']} "
            f"F1={report['full']['f1']:.6f}"
        )
    if report["claimability"]["claimable"]:
        print("CLAIMABLE only within the explicitly limited WCXB extraction scope")
    else:
        print("NOT COMPARABLE / NOT CLAIMABLE: " + "; ".join(
            report["claimability"]["reasons"]
        ))
    print(f"artifacts: {output}")


async def async_main(args: argparse.Namespace) -> int:
    splits = list(args.splits)
    if len(splits) != len(set(splits)):
        raise BenchmarkError("--splits contains duplicates")
    if args.concurrency is not None and args.concurrency < 1:
        raise BenchmarkError("--concurrency must be positive")
    if args.warmup_pages < 0:
        raise BenchmarkError("--warmup-pages cannot be negative")
    if args.limit_per_split is not None and args.limit_per_split < 1:
        raise BenchmarkError("--limit-per-split must be positive")

    corpus = args.corpus.resolve()
    corpus_before = verify_corpus(corpus)
    evaluator = _load_official_evaluator(corpus)
    source_before = _source_provenance()
    classifier_provenance = _embedded_classifier_provenance()

    from app.config import settings
    from app.services.extractor import (
        extract_content_async,
        native_backend_version,
    )

    concurrency = args.concurrency
    if concurrency is None:
        concurrency = max(
            1,
            int(getattr(settings, "max_concurrent_extractions", 1)),
        )
    environment = _environment_metadata(
        settings,
        native_backend_version,
        extraction_profile=args.extraction_profile,
    )
    smoke = args.limit_per_split is not None or set(splits) != {"dev", "test"}
    output = _make_output_dir(args.output, corpus=corpus, smoke=smoke)
    if smoke:
        _write_text(
            output / "NOT_COMPARABLE_SMOKE_OR_PARTIAL_RUN.txt",
            "This run used a page limit and/or omitted an official WCXB split.\n"
            "Do not compare its scores with full-corpus results or publish it.\n",
        )

    split_reports: dict[str, dict[str, Any]] = {}
    artifacts: dict[str, dict[str, str]] = {}
    for split in splits:
        jobs, selected_ids = _prepare_jobs(
            corpus,
            split,
            seed=args.seed,
            limit=args.limit_per_split,
        )
        predictions, extraction_rows, timing = await _extract_split(
            jobs,
            split=split,
            concurrency=concurrency,
            warmup_pages=args.warmup_pages,
            extraction_profile=args.extraction_profile,
            extractor=extract_content_async,
        )
        ground_truth, official_rows, official_stdout = (
            _score_with_official_evaluator(
                evaluator,
                split=split,
                predictions=predictions,
                selected_ids=selected_ids,
            )
        )
        split_report = _aggregate_rows(
            official_rows=official_rows,
            ground_truth=ground_truth,
            predictions=predictions,
            extraction_rows=extraction_rows,
            evaluator=evaluator,
            timing=timing,
            official_stdout=official_stdout,
        )
        prediction_name = f"{split}_predictions.json"
        pages_name = f"{split}_pages.jsonl"
        _write_json(output / prediction_name, dict(sorted(predictions.items())))
        _write_jsonl(output / pages_name, split_report["pages_detail"])
        split_report["prediction_sha256"] = _sha256(output / prediction_name)
        split_report["selected_ids_sha256"] = hashlib.sha256(
            "\n".join(selected_ids).encode("utf-8")
        ).hexdigest()
        split_reports[split] = split_report
        artifacts[split] = {
            "predictions": prediction_name,
            "pages": pages_name,
        }

    corpus_after = verify_corpus(corpus)
    source_after = _source_provenance()
    source_stable = (
        source_before["source_sha256"] == source_after["source_sha256"]
    )
    corpus_stable = (
        corpus_before["input_tree_sha256"]
        == corpus_after["input_tree_sha256"]
    )
    claim_reasons: list[str] = []
    if set(splits) != {"dev", "test"}:
        claim_reasons.append("both official dev and test splits were not run")
    if args.limit_per_split is not None:
        claim_reasons.append(
            f"limited smoke subset ({args.limit_per_split} pages per split)"
        )
    dirty_phases = [
        phase
        for phase, provenance in (("before", source_before), ("after", source_after))
        if provenance["git_dirty"]
    ]
    if dirty_phases:
        claim_reasons.append(
            f"Clusy source worktree was dirty {', '.join(dirty_phases)} the run"
        )
    if not source_stable:
        claim_reasons.append("relevant Clusy source changed during the run")
    if not corpus_stable:
        claim_reasons.append("WCXB corpus changed during the run")
    if (
        classifier_provenance["embedded"]
        and not classifier_provenance["training_manifest_verified"]
    ):
        claim_reasons.append(
            "embedded page classifier has no verified item/split training "
            "manifest, so WCXB training overlap cannot be excluded"
        )

    report: dict[str, Any] = {
        "schema_version": 2,
        "benchmark": {
            "name": "WCXB: Web Content eXtraction Benchmark",
            "scope": (
                "frozen HTML main-content extraction across seven page types; "
                "not crawling, fetching, rendering, or protocol compliance"
            ),
            "official_metric": (
                "macro mean of per-page bag-of-words precision/recall/F1"
            ),
            "public_ground_truth": True,
            "model_provenance": classifier_provenance,
        },
        "claimability": {
            "claimable": not claim_reasons,
            "scope": "WCXB extraction only",
            "reasons": claim_reasons,
            "warning": (
                "Public labels, author overlap, and unresolved embedded-model "
                "training provenance make this reproducible diagnostic evidence, "
                "not an independent blind SOTA adjudication."
            ),
        },
        "corpus": {
            "before": corpus_before,
            "after": corpus_after,
            "stable_during_run": corpus_stable,
        },
        "source": {
            "before": source_before,
            "after": source_after,
            "relevant_source_stable_during_run": source_stable,
        },
        "run_configuration": {
            "splits": splits,
            "limit_per_split": args.limit_per_split,
            "seed": args.seed,
            "page_order": "deterministic per-split shuffle",
            "concurrency": concurrency,
            "warmup_pages_per_split": args.warmup_pages,
            "entry_point": "app.services.extractor.extract_content_async",
            "extraction_profile": args.extraction_profile,
            "prediction_transform": "identity; exact production text",
        },
        "environment": {
            **environment,
            "peak_rss_bytes": _peak_rss_bytes(),
        },
        "artifacts": artifacts,
        "splits": split_reports,
    }
    if len(split_reports) > 1:
        report["full"] = _aggregate_full(split_reports)
    _write_json(output / "summary.json", report)
    _write_json(output / "manifest.json", _artifact_manifest(output))
    _print_summary(report, output)
    return 0


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(async_main(args))
    except BenchmarkError as error:
        print(f"benchmark error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("benchmark interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

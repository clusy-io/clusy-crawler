#!/usr/bin/env python3
"""Reproducible Clusy run on Zyte's Article Extraction Benchmark (AEB).

The benchmark corpus, baseline predictions, and metric are loaded from an
unchanged checkout of the exact AEB commit pinned below.  The production
``extract_content_async`` entry point is benchmarked by default; the synchronous
entry point is available as an explicitly labelled comparison.

This script intentionally fails closed when the AEB checkout is not the pinned
commit or has tracked modifications.  It never fetches a mutable branch and
never copies or reimplements the official four-shingle metric.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import gc
import gzip
import hashlib
import importlib.metadata
import importlib.util
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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from types import ModuleType


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from bench.source_provenance import (  # noqa: E402
    SourceInventoryError,
    git_visible_vendor_files,
)

AEB_REPOSITORY = "https://github.com/scrapinghub/article-extraction-benchmark.git"
AEB_COMMIT = "4a3bc979f76c0df73cb95fe272e2fc1b96f9f010"
AEB_TREE = "258fee1bb38bcb642afec48cb80e51bd1594c259"
AEB_CORPUS_SIZE = 181
AEB_EVALUATOR_SHA256 = "c01bf1cc7989700273ab1ba6d30fcdedc22fdb4301e7b4c1ac20635bb7632ea8"
AEB_GROUND_TRUTH_SHA256 = "512e9a9498912047a966e22f47302e849dfa45dca1f555d97588317dac7e5a3d"
BASELINES = ("trafilatura", "rs_trafilatura")
DEFAULT_SEED = 20260727
DEFAULT_BOOTSTRAP_SAMPLES = 10_000
SPLIT_ALGORITHM = "sha1(item_key) parity: even=dev, odd=test"
PREDICTION_TRANSFORM = "identity-production-output-v1"
SOURCE_FIXED_FILES = (
    "bench/neutral_benchmark.py",
    "bench/source_provenance.py",
    "pyproject.toml",
    "uv.lock",
    "native/Cargo.toml",
    "native/Cargo.lock",
    "native/pyproject.toml",
)


class BenchmarkError(RuntimeError):
    """An input or provenance condition makes the run invalid."""


@dataclass(frozen=True)
class PageInput:
    key: str
    url: str
    html: str
    compressed_bytes: int
    html_bytes: int


@dataclass
class PageOutput:
    key: str
    url: str
    markdown: str
    latency_ms: float
    strategy: str
    word_count: int
    confidence: float | None
    page_type: str
    error: str | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Clusy against the commit-pinned Article Extraction Benchmark.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "aeb_root",
        nargs="?",
        type=Path,
        default=Path("/tmp/clusy-aeb"),
        help="checkout of scrapinghub/article-extraction-benchmark",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="artifact directory; defaults to an ignored timestamped directory under bench/results",
    )
    parser.add_argument(
        "--mode",
        choices=("async", "sync", "both"),
        default="async",
        help="production async path, optional sync comparison, or both",
    )
    parser.add_argument(
        "--extraction-profile",
        choices=("article_body", "balanced"),
        default="article_body",
        help=(
            "production extraction contract; AEB's labels are article bodies, "
            "so article_body is the benchmark default"
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        help="page workers; defaults to production max_concurrent_extractions",
    )
    parser.add_argument(
        "--bootstrap-samples",
        type=int,
        default=DEFAULT_BOOTSTRAP_SAMPLES,
        help="official quality bootstraps and paired delta bootstraps",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--warmup-pages",
        type=int,
        default=5,
        help="deterministically selected warmups excluded from timing",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="deterministic smoke subset; any limited run is marked NOT CLAIMABLE",
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


def _git(root: Path, *args: str, check: bool = True) -> str:
    return _run(["git", *args], cwd=root, check=check).stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_aeb_checkout(root: Path) -> dict[str, Any]:
    if not root.is_dir():
        raise BenchmarkError(
            f"AEB checkout not found at {root}. Clone {AEB_REPOSITORY} and check out {AEB_COMMIT}."
        )
    if not (root / ".git").exists():
        raise BenchmarkError(f"{root} is not a Git checkout; provenance cannot be verified")

    actual_commit = _git(root, "rev-parse", "HEAD")
    if actual_commit != AEB_COMMIT:
        raise BenchmarkError(
            "refusing mutable or mismatched AEB checkout: "
            f"expected {AEB_COMMIT}, found {actual_commit}. "
            f"Run `git -C {root} checkout --detach {AEB_COMMIT}`."
        )

    actual_tree = _git(root, "rev-parse", "HEAD^{tree}")
    if actual_tree != AEB_TREE:
        raise BenchmarkError(f"AEB tree mismatch: expected {AEB_TREE}, found {actual_tree}")

    tracked_status = _git(root, "status", "--porcelain", "--untracked-files=no")
    if tracked_status:
        raise BenchmarkError(
            "AEB has tracked modifications; restore the pinned checkout before benchmarking:\n"
            + tracked_status
        )

    required_hashes = {
        "evaluate.py": AEB_EVALUATOR_SHA256,
        "ground-truth.json": AEB_GROUND_TRUTH_SHA256,
    }
    for relative, expected in required_hashes.items():
        path = root / relative
        if not path.is_file():
            raise BenchmarkError(f"pinned AEB file is missing: {relative}")
        actual = _sha256(path)
        if actual != expected:
            raise BenchmarkError(
                f"pinned AEB file hash mismatch for {relative}: expected {expected}, found {actual}"
            )

    with (root / "ground-truth.json").open("r", encoding="utf-8") as stream:
        ground_truth = json.load(stream)
    if len(ground_truth) != AEB_CORPUS_SIZE:
        raise BenchmarkError(
            f"AEB corpus size mismatch: expected {AEB_CORPUS_SIZE}, found {len(ground_truth)}"
        )
    missing_html = [key for key in ground_truth if not (root / "html" / f"{key}.html.gz").is_file()]
    if missing_html:
        raise BenchmarkError(f"AEB is missing {len(missing_html)} HTML fixtures")
    for baseline in BASELINES:
        if not (root / "output" / f"{baseline}.json").is_file():
            raise BenchmarkError(f"AEB baseline prediction missing: output/{baseline}.json")

    branch_result = _run(
        ["git", "symbolic-ref", "--short", "-q", "HEAD"],
        cwd=root,
        check=False,
    )
    origin_result = _run(
        ["git", "remote", "get-url", "origin"],
        cwd=root,
        check=False,
    )
    return {
        "repository": AEB_REPOSITORY,
        "root": str(root.resolve()),
        "expected_commit": AEB_COMMIT,
        "actual_commit": actual_commit,
        "tree": actual_tree,
        "branch": branch_result.stdout.strip() or None,
        "origin": origin_result.stdout.strip() or None,
        "tracked_worktree_clean": True,
        "evaluator_sha256": AEB_EVALUATOR_SHA256,
        "ground_truth_sha256": AEB_GROUND_TRUTH_SHA256,
        "corpus_pages": len(ground_truth),
        "verified": True,
    }


def load_official_evaluator(root: Path) -> ModuleType:
    evaluator_path = root / "evaluate.py"
    spec = importlib.util.spec_from_file_location(
        f"clusy_pinned_aeb_evaluator_{AEB_COMMIT[:12]}",
        evaluator_path,
    )
    if spec is None or spec.loader is None:
        raise BenchmarkError(f"could not load official evaluator at {evaluator_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    required = (
        "evaluate",
        "load_json",
        "load_prediction",
        "metrics_from_tp_fp_fns",
        "string_shingle_matching",
    )
    missing = [name for name in required if not callable(getattr(module, name, None))]
    if missing:
        raise BenchmarkError(f"pinned evaluator API missing: {', '.join(missing)}")
    return module


def split_name(key: str) -> str:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return "dev" if int(digest, 16) % 2 == 0 else "test"


def percentile(values: list[float], probability: float) -> float:
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


def _derived_seed(seed: int, *labels: str) -> int:
    payload = f"{seed}:" + ":".join(labels)
    return int.from_bytes(hashlib.sha256(payload.encode("utf-8")).digest()[:8], "big")


def _clean_official_metrics(metrics: dict[str, Any], n: int) -> dict[str, Any]:
    return {
        "pages": n,
        "f1": metrics["f1"],
        "precision": metrics["precision"],
        "recall": metrics["recall"],
        "accuracy": metrics["accuracy"],
        "bootstrap_std": {
            "f1": metrics.get("f1_std", 0.0),
            "precision": metrics.get("precision_std", 0.0),
            "recall": metrics.get("recall_std", 0.0),
            "accuracy": metrics.get("accuracy_std", 0.0),
        },
        "metric_implementation": (f"{AEB_COMMIT}:evaluate.py::evaluate/string_shingle_matching"),
    }


def official_quality(
    evaluator: ModuleType,
    ground_truth: dict[str, dict[str, Any]],
    prediction: dict[str, dict[str, Any]],
    *,
    samples: int,
    seed: int,
) -> dict[str, Any]:
    state = random.getstate()
    try:
        random.seed(seed)
        metrics = evaluator.evaluate(ground_truth, prediction, samples)
    finally:
        random.setstate(state)
    return _clean_official_metrics(metrics, len(ground_truth))


def paired_bootstrap(
    evaluator: ModuleType,
    ground_truth: dict[str, dict[str, Any]],
    candidate: dict[str, dict[str, Any]],
    baseline: dict[str, dict[str, Any]],
    *,
    samples: int,
    seed: int,
) -> dict[str, Any]:
    keys = list(ground_truth)
    candidate_counts = []
    baseline_counts = []
    for key in keys:
        truth = ground_truth[key].get("articleBody", "")
        candidate_counts.append(
            evaluator.string_shingle_matching(
                true=truth,
                pred=candidate[key].get("articleBody", ""),
            )
        )
        baseline_counts.append(
            evaluator.string_shingle_matching(
                true=truth,
                pred=baseline[key].get("articleBody", ""),
            )
        )

    candidate_point = evaluator.metrics_from_tp_fp_fns(candidate_counts)["f1"]
    baseline_point = evaluator.metrics_from_tp_fp_fns(baseline_counts)["f1"]

    rng = random.Random(seed)
    size = len(keys)
    deltas: list[float] = []
    for _ in range(samples):
        indices = [rng.randrange(size) for _ in range(size)]
        candidate_f1 = evaluator.metrics_from_tp_fp_fns(
            [candidate_counts[index] for index in indices]
        )["f1"]
        baseline_f1 = evaluator.metrics_from_tp_fp_fns(
            [baseline_counts[index] for index in indices]
        )["f1"]
        deltas.append(candidate_f1 - baseline_f1)

    positive = sum(delta > 0.0 for delta in deltas)
    negative = sum(delta < 0.0 for delta in deltas)
    equal = samples - positive - negative
    return {
        "pages": size,
        "candidate_f1": candidate_point,
        "baseline_f1": baseline_point,
        "delta_f1": candidate_point - baseline_point,
        "bootstrap_delta_mean": statistics.mean(deltas),
        "bootstrap_delta_std": statistics.stdev(deltas) if samples > 1 else 0.0,
        "delta_ci95": [percentile(deltas, 0.025), percentile(deltas, 0.975)],
        "p_candidate_gt_baseline": positive / samples,
        "p_candidate_lt_baseline": negative / samples,
        "p_equal": equal / samples,
        "bootstrap_samples": samples,
        "seed": seed,
        "method": (
            "paired page bootstrap with replacement; each replicate calls the "
            "pinned official metrics_from_tp_fp_fns"
        ),
    }


def _package_versions() -> dict[str, str | None]:
    packages = (
        "clusy-native",
        "trafilatura",
        "lxml",
        "markdownify",
        "readability-lxml",
        "beautifulsoup4",
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


def _cpu_model() -> str | None:
    if sys.platform == "darwin":
        result = _run(["sysctl", "-n", "machdep.cpu.brand_string"], check=False)
        value = result.stdout.strip()
        if value:
            return value
    if sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if line.lower().startswith(("model name", "hardware")):
                    return line.split(":", 1)[-1].strip()
        except OSError:
            pass
    return platform.processor() or None


def _rust_version() -> str | None:
    result = _run(["rustc", "--version"], check=False)
    return result.stdout.strip() or None


def _native_module_metadata() -> dict[str, Any]:
    try:
        import clusy_native
        from clusy_native import _native
    except (ImportError, RuntimeError) as error:
        return {
            "loaded": False,
            "error": f"{type(error).__name__}: {error}",
        }
    module_path_value = getattr(_native, "__file__", None)
    package_path_value = getattr(clusy_native, "__file__", None)
    module_path = Path(module_path_value).resolve() if module_path_value else None
    package_path = Path(package_path_value).resolve() if package_path_value else None
    return {
        "loaded": True,
        "extension_path": str(module_path) if module_path else None,
        "extension_sha256": (
            _sha256(module_path) if module_path is not None and module_path.is_file() else None
        ),
        "package_path": str(package_path) if package_path else None,
        "package_sha256": (
            _sha256(package_path) if package_path is not None and package_path.is_file() else None
        ),
    }


def _peak_rss_bytes() -> int | None:
    try:
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (AttributeError, OSError):
        return None
    # macOS reports bytes; Linux and the BSDs report KiB.
    return int(value if sys.platform == "darwin" else value * 1024)


def _current_rss_bytes() -> int | None:
    if sys.platform.startswith("linux"):
        try:
            resident_pages = int(Path("/proc/self/statm").read_text().split()[1])
            return resident_pages * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            return None
    result = _run(["ps", "-o", "rss=", "-p", str(os.getpid())], check=False)
    try:
        return int(result.stdout.strip()) * 1024
    except ValueError:
        return None


def _source_hashes() -> dict[str, str]:
    paths = {
        PROJECT_ROOT / relative
        for relative in SOURCE_FIXED_FILES
        if (PROJECT_ROOT / relative).is_file()
    }
    paths.update((PROJECT_ROOT / "app").rglob("*.py"))
    paths.update((PROJECT_ROOT / "native" / "src").rglob("*.rs"))
    paths.update((PROJECT_ROOT / "native" / "python").rglob("*.py"))
    paths.update((PROJECT_ROOT / "native" / "python").rglob("*.pyi"))
    try:
        paths.update(git_visible_vendor_files(PROJECT_ROOT))
    except SourceInventoryError as error:
        raise BenchmarkError(f"native vendor source inventory failed: {error}") from error
    py_typed = PROJECT_ROOT / "native" / "python" / "clusy_native" / "py.typed"
    if py_typed.is_file():
        paths.add(py_typed)
    return {
        path.relative_to(PROJECT_ROOT).as_posix(): _sha256(path)
        for path in sorted(paths)
        if path.is_file()
    }


def _source_provenance() -> dict[str, Any]:
    commit_result = _run(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, check=False)
    commit = commit_result.stdout.strip() or None
    status_result = _run(
        ["git", "status", "--porcelain"],
        cwd=PROJECT_ROOT,
        check=False,
    )
    status = [line for line in status_result.stdout.splitlines() if line]
    return {
        "repository_root": str(PROJECT_ROOT),
        "git_commit": commit,
        "git_dirty": bool(status),
        "git_status": status,
        "source_sha256": _source_hashes(),
    }


def _environment_metadata(
    *,
    settings: Any,
    native_backend_version: Callable[[], str],
) -> dict[str, Any]:
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
            "cpu_model": _cpu_model(),
            "logical_cpu_count": os.cpu_count(),
        },
        "dependencies": _package_versions(),
        "native_backend": {
            "backend_version": native_backend_version(),
            "loaded_module": _native_module_metadata(),
            "rustc": _rust_version(),
            "enabled": getattr(settings, "native_extraction_enabled", None),
            "minimum_confidence": getattr(
                settings,
                "native_extraction_min_confidence",
                None,
            ),
            "cargo_toml_sha256": (
                _sha256(PROJECT_ROOT / "native" / "Cargo.toml")
                if (PROJECT_ROOT / "native" / "Cargo.toml").is_file()
                else None
            ),
            "cargo_lock_sha256": (
                _sha256(PROJECT_ROOT / "native" / "Cargo.lock")
                if (PROJECT_ROOT / "native" / "Cargo.lock").is_file()
                else None
            ),
        },
        "production_extraction_settings": {
            "parallel_extraction_enabled": getattr(
                settings,
                "parallel_extraction_enabled",
                None,
            ),
            "extraction_merge_mode": getattr(settings, "extraction_merge_mode", None),
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


def load_pages(
    root: Path,
    ground_truth: dict[str, dict[str, Any]],
    keys: list[str],
) -> list[PageInput]:
    pages: list[PageInput] = []
    for key in keys:
        compressed = (root / "html" / f"{key}.html.gz").read_bytes()
        try:
            html_bytes = gzip.decompress(compressed)
        except (gzip.BadGzipFile, EOFError, OSError) as error:
            raise BenchmarkError(f"invalid pinned HTML fixture {key}: {error}") from error
        pages.append(
            PageInput(
                key=key,
                url=str(ground_truth[key].get("url", "")),
                html=html_bytes.decode("utf-8", "replace"),
                compressed_bytes=len(compressed),
                html_bytes=len(html_bytes),
            )
        )
    return pages


async def run_extraction_mode(
    pages: list[PageInput],
    *,
    mode: str,
    concurrency: int,
    warmup_pages: int,
    async_extractor: Callable[[str, str], Awaitable[Any]],
    sync_extractor: Callable[[str, str], Any],
) -> tuple[dict[str, PageOutput], dict[str, Any]]:
    if mode not in {"async", "sync"}:
        raise ValueError(f"unsupported extraction mode: {mode}")

    async def invoke(page: PageInput) -> Any:
        if mode == "async":
            return await async_extractor(page.html, page.url)
        return await asyncio.to_thread(sync_extractor, page.html, page.url)

    warmup_count = min(warmup_pages, len(pages))
    for page in pages[:warmup_count]:
        await invoke(page)

    gc.collect()
    rss_before = _current_rss_bytes()
    queue: asyncio.Queue[PageInput] = asyncio.Queue()
    for page in pages:
        queue.put_nowait(page)
    outputs: dict[str, PageOutput] = {}

    async def worker() -> None:
        while True:
            try:
                page = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            started = time.perf_counter()
            try:
                result = await invoke(page)
                latency_ms = (time.perf_counter() - started) * 1000.0
                confidence = getattr(result, "confidence", None)
                outputs[page.key] = PageOutput(
                    key=page.key,
                    url=page.url,
                    markdown=str(getattr(result, "text", "") or ""),
                    latency_ms=latency_ms,
                    strategy=str(getattr(result, "strategy", "") or ""),
                    word_count=int(getattr(result, "word_count", 0) or 0),
                    confidence=(float(confidence) if confidence is not None else None),
                    page_type=str(getattr(result, "page_type", "") or ""),
                    error=None,
                )
            except Exception as error:  # benchmark failures score as empty predictions
                latency_ms = (time.perf_counter() - started) * 1000.0
                outputs[page.key] = PageOutput(
                    key=page.key,
                    url=page.url,
                    markdown="",
                    latency_ms=latency_ms,
                    strategy="",
                    word_count=0,
                    confidence=None,
                    page_type="",
                    error=f"{type(error).__name__}: {error}"[:1000],
                )
            finally:
                queue.task_done()

    wall_started = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    wall_seconds = time.perf_counter() - wall_started
    rss_after = _current_rss_bytes()
    peak_rss = _peak_rss_bytes()

    if set(outputs) != {page.key for page in pages}:
        raise BenchmarkError("internal error: extraction output keys do not match inputs")

    latencies = [output.latency_ms for output in outputs.values()]
    errors = [output for output in outputs.values() if output.error]
    strategies: dict[str, int] = {}
    for output in outputs.values():
        strategy = output.strategy or "unknown"
        strategies[strategy] = strategies.get(strategy, 0) + 1
    performance = {
        "measurement_scope": (
            "in-memory decoded HTML -> production ExtractionResult; excludes fixture "
            "read/decompression and official evaluation"
        ),
        "load_model": "closed-loop bounded worker pool",
        "mode": mode,
        "concurrency": concurrency,
        "warmup_pages": warmup_count,
        "measured_pages": len(pages),
        "wall_seconds": wall_seconds,
        "pages_per_second": len(pages) / wall_seconds if wall_seconds else None,
        "latency_ms": {
            "mean": statistics.mean(latencies),
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "min": min(latencies),
            "max": max(latencies),
        },
        "errors": len(errors),
        "strategy_counts": dict(sorted(strategies.items())),
        "process_memory": {
            "rss_before_bytes": rss_before,
            "rss_after_bytes": rss_after,
            "peak_rss_bytes": peak_rss,
            "peak_minus_rss_before_bytes": (
                max(0, peak_rss - rss_before)
                if peak_rss is not None and rss_before is not None
                else None
            ),
            "peak_scope": (
                "process lifetime high-water mark; includes Python/native imports, "
                "loaded AEB HTML, warmups, and retained predictions"
            ),
        },
    }
    return outputs, performance


def _prediction_dict(
    outputs: dict[str, PageOutput],
    ordered_keys: list[str],
) -> dict[str, dict[str, Any]]:
    return {
        key: {
            # Score the exact production body. AEB tokenization already ignores
            # Markdown punctuation; removing headings, links, or HTML here
            # would hide content the service actually returned and inflate the
            # result. The pinned native article path emits plain text anyway.
            "articleBody": outputs[key].markdown,
            "url": outputs[key].url,
        }
        for key in ordered_keys
    }


def _subset(
    mapping: dict[str, dict[str, Any]],
    keys: list[str],
) -> dict[str, dict[str, Any]]:
    return {key: mapping[key] for key in keys}


def evaluate_mode(
    evaluator: ModuleType,
    *,
    ground_truth: dict[str, dict[str, Any]],
    prediction: dict[str, dict[str, Any]],
    baselines: dict[str, dict[str, dict[str, Any]]],
    split_keys: dict[str, list[str]],
    bootstrap_samples: int,
    seed: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    quality: dict[str, Any] = {}
    comparisons: dict[str, Any] = {}
    for split, keys in split_keys.items():
        if not keys:
            quality[split] = {"pages": 0, "skipped": "empty split"}
            comparisons[split] = {"pages": 0, "skipped": "empty split"}
            continue
        gt_part = _subset(ground_truth, keys)
        prediction_part = _subset(prediction, keys)
        quality[split] = {
            "candidate": official_quality(
                evaluator,
                gt_part,
                prediction_part,
                samples=bootstrap_samples,
                seed=_derived_seed(seed, split, "candidate", "quality"),
            ),
            "baselines": {},
        }
        comparisons[split] = {}
        for baseline_name, baseline in baselines.items():
            baseline_part = _subset(baseline, keys)
            quality[split]["baselines"][baseline_name] = official_quality(
                evaluator,
                gt_part,
                baseline_part,
                samples=bootstrap_samples,
                seed=_derived_seed(seed, split, baseline_name, "quality"),
            )
            comparisons[split][baseline_name] = paired_bootstrap(
                evaluator,
                gt_part,
                prediction_part,
                baseline_part,
                samples=bootstrap_samples,
                seed=_derived_seed(seed, split, baseline_name, "paired"),
            )
    return quality, comparisons


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    temporary.replace(path)


def _make_output_dir(requested: Path | None) -> Path:
    if requested is None:
        timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        requested = PROJECT_ROOT / "bench" / "results" / "aeb" / timestamp
    output_dir = requested.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise BenchmarkError(f"refusing to overwrite non-empty output directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _artifact_manifest(output_dir: Path) -> dict[str, Any]:
    artifacts = {}
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        artifacts[str(path.relative_to(output_dir))] = {
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
    return {
        "schema_version": 1,
        "root": str(output_dir),
        "artifacts": artifacts,
    }


def _print_summary(report: dict[str, Any], output_dir: Path) -> None:
    print(f"AEB provenance verified: {AEB_COMMIT}")
    for mode, run in report["runs"].items():
        full = run["quality"]["full"]["candidate"]
        performance = run["performance"]
        print(
            f"{mode}: F1={full['f1']:.6f} "
            f"P={full['precision']:.6f} R={full['recall']:.6f}; "
            f"{performance['pages_per_second']:.2f} pages/s, "
            f"p50={performance['latency_ms']['p50']:.2f}ms, "
            f"p95={performance['latency_ms']['p95']:.2f}ms"
        )
        for baseline, comparison in run["paired_comparisons"]["full"].items():
            low, high = comparison["delta_ci95"]
            print(
                f"  vs {baseline}: ΔF1={comparison['delta_f1']:+.6f}, "
                f"95% CI [{low:+.6f}, {high:+.6f}], "
                f"P(Clusy>{baseline})={comparison['p_candidate_gt_baseline']:.4f}"
            )
    claimability = report["claimability"]
    if claimability["claimable"]:
        print("CLAIMABLE within the explicitly limited AEB article-extraction scope")
    else:
        print("NOT CLAIMABLE: " + "; ".join(claimability["reasons"]))
    print(f"artifacts: {output_dir}")


async def async_main(args: argparse.Namespace) -> int:
    if args.bootstrap_samples < 100:
        raise BenchmarkError("--bootstrap-samples must be at least 100")
    if args.warmup_pages < 0:
        raise BenchmarkError("--warmup-pages cannot be negative")
    if args.limit is not None and args.limit < 2:
        raise BenchmarkError("--limit must be at least 2")

    aeb_root = args.aeb_root.resolve()
    dataset = verify_aeb_checkout(aeb_root)
    evaluator = load_official_evaluator(aeb_root)
    ground_truth: dict[str, dict[str, Any]] = evaluator.load_json(aeb_root / "ground-truth.json")

    from app.config import settings
    from app.services.extractor import (
        extract_content,
        extract_content_async,
        native_backend_version,
    )

    async def benchmark_async_extractor(html: str, url: str) -> Any:
        return await extract_content_async(
            html,
            url,
            extraction_profile=args.extraction_profile,
        )

    def benchmark_sync_extractor(html: str, url: str) -> Any:
        return extract_content(
            html,
            url,
            extraction_profile=args.extraction_profile,
        )

    concurrency = args.concurrency
    if concurrency is None:
        concurrency = int(getattr(settings, "max_concurrent_extractions", 1))
    if concurrency < 1:
        raise BenchmarkError("--concurrency must be positive")

    source_before = _source_provenance()
    environment = _environment_metadata(
        settings=settings,
        native_backend_version=native_backend_version,
    )

    official_keys = list(ground_truth)
    shuffled_keys = official_keys.copy()
    random.Random(args.seed).shuffle(shuffled_keys)
    selected_set = set(shuffled_keys[: args.limit] if args.limit is not None else shuffled_keys)
    selected_keys = [key for key in official_keys if key in selected_set]
    performance_keys = [key for key in shuffled_keys if key in selected_set]
    pages_by_key = {page.key: page for page in load_pages(aeb_root, ground_truth, selected_keys)}
    performance_pages = [pages_by_key[key] for key in performance_keys]

    baseline_predictions: dict[str, dict[str, dict[str, Any]]] = {}
    baseline_versions: dict[str, str] = {}
    for baseline in BASELINES:
        prediction, version = evaluator.load_prediction(aeb_root / "output" / f"{baseline}.json")
        if set(prediction) != set(ground_truth):
            raise BenchmarkError(f"official {baseline} prediction keys do not match AEB")
        baseline_predictions[baseline] = prediction
        baseline_versions[baseline] = version

    split_keys = {
        "full": selected_keys,
        "dev": [key for key in selected_keys if split_name(key) == "dev"],
        "test": [key for key in selected_keys if split_name(key) == "test"],
    }
    split_manifest_payload = {
        "algorithm": SPLIT_ALGORITHM,
        "seed_note": "the split is key-derived and independent of bootstrap/page-order seed",
        "selected_pages": len(selected_keys),
        "expected_full_pages": AEB_CORPUS_SIZE,
        "corpus_complete": len(selected_keys) == AEB_CORPUS_SIZE,
        "splits": {name: keys for name, keys in split_keys.items()},
    }
    split_manifest_hash = hashlib.sha256(
        json.dumps(split_manifest_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()

    output_dir = _make_output_dir(args.output_dir)
    _write_json(output_dir / "split_manifest.json", split_manifest_payload)

    modes = ("async", "sync") if args.mode == "both" else (args.mode,)
    run_reports: dict[str, Any] = {}
    source_commit = source_before.get("git_commit") or "unknown"
    dirty_suffix = "+dirty" if source_before["git_dirty"] else ""
    native_version = environment["native_backend"]["backend_version"]

    for mode in modes:
        outputs, performance = await run_extraction_mode(
            performance_pages,
            mode=mode,
            concurrency=concurrency,
            warmup_pages=args.warmup_pages,
            async_extractor=benchmark_async_extractor,
            sync_extractor=benchmark_sync_extractor,
        )
        prediction = _prediction_dict(outputs, selected_keys)
        quality, comparisons = evaluate_mode(
            evaluator,
            ground_truth=_subset(ground_truth, selected_keys),
            prediction=prediction,
            baselines={
                name: _subset(value, selected_keys) for name, value in baseline_predictions.items()
            },
            split_keys=split_keys,
            bootstrap_samples=args.bootstrap_samples,
            seed=args.seed,
        )

        version = (
            f"clusy-crawler@{source_commit}{dirty_suffix};mode={mode};"
            f"profile={args.extraction_profile};native={native_version};aeb={AEB_COMMIT}"
        )
        prediction_artifact = f"predictions/clusy_{mode}.json"
        markdown_artifact = f"raw/production_markdown_{mode}.json"
        pages_artifact = f"raw/page_metrics_{mode}.jsonl"
        _write_json(
            output_dir / prediction_artifact,
            {"version": version, "output": prediction},
        )
        _write_json(
            output_dir / markdown_artifact,
            {
                "version": version,
                "output": {
                    key: {
                        "markdown": outputs[key].markdown,
                        "url": outputs[key].url,
                        "strategy": outputs[key].strategy,
                        "word_count": outputs[key].word_count,
                        "confidence": outputs[key].confidence,
                        "page_type": outputs[key].page_type,
                        "error": outputs[key].error,
                    }
                    for key in selected_keys
                },
            },
        )
        _write_jsonl(
            output_dir / pages_artifact,
            [
                {
                    "key": key,
                    "split": split_name(key),
                    "url": outputs[key].url,
                    "latency_ms": outputs[key].latency_ms,
                    "strategy": outputs[key].strategy,
                    "word_count": outputs[key].word_count,
                    "confidence": outputs[key].confidence,
                    "page_type": outputs[key].page_type,
                    "error": outputs[key].error,
                    "compressed_input_bytes": pages_by_key[key].compressed_bytes,
                    "decoded_input_bytes": pages_by_key[key].html_bytes,
                }
                for key in selected_keys
            ],
        )
        run_reports[mode] = {
            "entry_point": (
                "app.services.extractor.extract_content_async"
                if mode == "async"
                else "app.services.extractor.extract_content"
            ),
            "extraction_profile": args.extraction_profile,
            "prediction_transform": {
                "name": PREDICTION_TRANSFORM,
                "purpose": (
                    "score the exact production body without a quality-altering normalization layer"
                ),
            },
            "artifacts": {
                "official_prediction_json": prediction_artifact,
                "raw_production_markdown_json": markdown_artifact,
                "per_page_metrics_jsonl": pages_artifact,
            },
            "performance": performance,
            "quality": quality,
            "paired_comparisons": comparisons,
        }

    source_after = _source_provenance()
    source_stable = source_before["source_sha256"] == source_after["source_sha256"]
    claim_reasons: list[str] = []
    if len(selected_keys) != AEB_CORPUS_SIZE:
        claim_reasons.append(
            f"incomplete smoke corpus ({len(selected_keys)}/{AEB_CORPUS_SIZE} pages)"
        )
    if "async" not in modes:
        claim_reasons.append("production async entry point was not measured")
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
        claim_reasons.append("relevant Clusy source files changed during the run")

    report = {
        "schema_version": 1,
        "benchmark": {
            "name": "Zyte/ScrapingHub Article Extraction Benchmark",
            "short_name": "AEB",
            "scope": (
                "frozen article-body extraction only; not a recursive crawl, "
                "JavaScript-rendering, protocol-compliance, or general-web benchmark"
            ),
            "official_metric": "normalized four-token-shingle precision/recall/F1",
            "public_ground_truth": True,
        },
        "claimability": {
            "claimable": not claim_reasons,
            "scope": "AEB article-body extraction only",
            "reasons": claim_reasons,
        },
        "dataset": dataset,
        "run_configuration": {
            "seed": args.seed,
            "bootstrap_samples": args.bootstrap_samples,
            "mode": args.mode,
            "extraction_profile": args.extraction_profile,
            "concurrency": concurrency,
            "warmup_pages": args.warmup_pages,
            "limit": args.limit,
            "selected_pages": len(selected_keys),
            "page_order": "deterministic random shuffle from seed",
            "split_algorithm": SPLIT_ALGORITHM,
            "split_manifest_sha256": split_manifest_hash,
            "baseline_versions": baseline_versions,
        },
        "environment": environment,
        "source": {
            "before": source_before,
            "after": source_after,
            "relevant_source_stable_during_run": source_stable,
        },
        "input": {
            "decoded_html_bytes": sum(page.html_bytes for page in pages_by_key.values()),
            "compressed_html_bytes": sum(page.compressed_bytes for page in pages_by_key.values()),
        },
        "runs": run_reports,
    }
    _write_json(output_dir / "report.json", report)
    _write_json(output_dir / "manifest.json", _artifact_manifest(output_dir))
    _print_summary(report, output_dir)
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

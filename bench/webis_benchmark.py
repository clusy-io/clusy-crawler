#!/usr/bin/env python3
"""Reproducible Webis Web Content Extraction Benchmark runner.

This runner evaluates the deterministic production extraction path against the
complete 3,985-page corpus released with the SIGIR 2023 Webis study.  It fails
closed on repository, archive, extracted-corpus, evaluator, dependency, and
NLTK resource drift.  Scoring calls the pinned upstream ``rouge_eval`` and
``levenshtein_eval`` functions in an isolated Python 3.11 environment.

Ground truth is never passed to production code.  Each production call receives
only ``(html, url, extraction_profile="balanced")``.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime as dt
import hashlib
import importlib.metadata
import io
import json
import math
import os
import platform
import queue
import resource
import shutil
import statistics
import subprocess
import sys
import tarfile
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OFFICIAL_REPOSITORY = "https://github.com/chatnoir-eu/web-content-extraction-benchmark"
OFFICIAL_COMMIT = "36be6d9c4f96d3c613c21144c4d39e5d0cce93af"
OFFICIAL_TREE = "b22ec66c35eb201acfa73d1bc8bfddb4f2e46cfb"
PAPER_DOI = "10.1145/3539618.3591920"
PAPER_URL = "https://dl.acm.org/doi/10.1145/3539618.3591920"
PAPER_PDF = "https://downloads.webis.de/publications/papers/bevendorff_2023c.pdf"

DATA_ARCHIVE_RELATIVE_PATH = Path("datasets/combined.tar.xz")
DATA_ARCHIVE_BYTES = 50_103_424
DATA_ARCHIVE_SHA256 = "ed4e57ecad343cdce51d06fa560c1f50965367ea6714cd08e85a439102bc4b1a"
METRICS_ARCHIVE_RELATIVE_PATH = Path("outputs/metrics-computed.tar.xz")
METRICS_ARCHIVE_BYTES = 3_081_852
METRICS_ARCHIVE_SHA256 = "c1c6c14994fb0e676c2371d46be672d5ba29f092f9678f5d63744c3ca7e075f5"

OFFICIAL_FILE_HASHES = {
    "LICENSE": "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
    "pyproject.toml": "147efda19fd5ad03d90f06fbefe27bd4333b1c09f4edfc71d92d914b8ad58243",
    "poetry.lock": "28debe18006247e6618d927ebdbf0bb92779cc3b6a620efc78695d17ac755268",
    "src/extraction_benchmark/eval.py": (
        "3b5cba4c59fae42492f1d67650622b04e44620208584e4d434f48cc08150462a"
    ),
    "src/extraction_benchmark/util.py": (
        "9156064fde1c3ee2707f7f253cf266b762fa9a788ef655c2b19430e6454907a2"
    ),
    "src/extraction_benchmark/globals.py": (
        "c228d2140feb64a831c9f45e09d634a6e19e9797cfdd8945dab27d0f14b95b5c"
    ),
    "src/extraction_benchmark/paths.py": (
        "a2258732bcffe2a974b13e46628a2a675afeac5965e7dcb191bfe064920c0eed"
    ),
}

DATASETS = (
    "cetd",
    "cleaneval",
    "cleanportaleval",
    "dragnet",
    "google-trends-2017",
    "l3s-gn1",
    "readability",
    "scrapinghub",
)
DATASET_FRIENDLY_NAMES = {
    "cetd": "CETD",
    "cleaneval": "CleanEval",
    "cleanportaleval": "CleanPortalEval",
    "dragnet": "Dragnet",
    "google-trends-2017": "Google-Trends-2017",
    "l3s-gn1": "L3S-GN1",
    "readability": "Readability",
    "scrapinghub": "ScrapingHub",
}
DATASET_PAGE_COUNTS = {
    "cetd": 700,
    "cleaneval": 738,
    "cleanportaleval": 71,
    "dragnet": 1_379,
    "google-trends-2017": 180,
    "l3s-gn1": 621,
    "readability": 115,
    "scrapinghub": 181,
}
TOTAL_PAGES = 3_985
EXTRACTED_FILES = 3_993
EXTRACTED_BYTES = 375_825_000
# SHA-256 over sorted files: uint32-be(relative-name byte length), relative
# POSIX name bytes, then file bytes.
EXTRACTED_TREE_SHA256 = "2818f8118ce2d98ea659d64f006eff217e5ed5ac27bb67b29209015d14290cb9"

SCORER_DEPENDENCIES = {
    "click": "8.1.7",
    "Levenshtein": "0.20.9",
    "matplotlib": "3.9.0",
    "nltk": "3.8.1",
    "numpy": "1.26.4",
    "pandas": "1.5.3",
    "rapidfuzz": "2.15.2",
    "rouge-score": "0.1.2",
    "tqdm": "4.66.4",
}
NLTK_DATA_REPOSITORY = "https://github.com/nltk/nltk_data"
NLTK_DATA_COMMIT = "550b6625bcef1f2abff2ff770a5a0d272c9c6b2a"
PUNKT_ZIP_RELATIVE_PATH = Path("tokenizers/punkt.zip")
PUNKT_ZIP_BYTES = 13_905_355
PUNKT_ZIP_SHA256 = "51c3078994aeaf650bfc8e028be4fb42b4a0d177d41c012b6a983979653660ec"
PUNKT_PY3_ENGLISH_RELATIVE_PATH = Path("tokenizers/punkt/PY3/english.pickle")
PUNKT_PY3_ENGLISH_BYTES = 406_697
PUNKT_PY3_ENGLISH_SHA256 = "5cad3758596392364e3be9803dbd7ebeda384b68937b488a01365f5551bb942c"

BASELINE_MODELS = (
    "ensemble_weighted",
    "ensemble_best",
    "ensemble_majority",
    "trafilatura",
    "readability",
    "resiliparse",
    "go_domdistiller",
)
BASELINE_NAMES = {
    "ensemble_weighted": "(Best weighted)",
    "ensemble_best": "(Best only)",
    "ensemble_majority": "(Majority all)",
    "trafilatura": "Trafilatura",
    "readability": "Readability",
    "resiliparse": "Resiliparse",
    "go_domdistiller": "DOM Distiller",
}
# Exact values independently derived from the pinned official per-page CSVs.
# Checking these catches accidental aggregation drift.
BASELINE_CANARIES = {
    "ensemble_weighted": {
        "macro_rouge_f1_mean": 0.8988436066004737,
        "micro_rouge_f1_mean": 0.8886528838681629,
        "macro_levenshtein_mean": 0.8955325681540933,
        "micro_levenshtein_mean": 0.8847548921517864,
    },
    "trafilatura": {
        "macro_rouge_f1_mean": 0.8834611799388654,
        "micro_rouge_f1_mean": 0.8673556148900864,
        "macro_levenshtein_mean": 0.8795616054724649,
        "micro_levenshtein_mean": 0.8626918939597533,
    },
}

EXTRACTION_PROFILE: Literal["balanced"] = "balanced"
MODEL_NAME = "clusy"

SCORER_HELPER = r"""
import importlib.metadata
import hashlib
import inspect
import json
import sys
from pathlib import Path

import nltk.data
from extraction_benchmark.eval import levenshtein_eval, rouge_eval

punkt_resource = Path(str(nltk.data.find("tokenizers/punkt/english.pickle")))
packages = [
    "click", "Levenshtein", "matplotlib", "nltk", "numpy", "pandas",
    "rapidfuzz", "rouge-score", "tqdm",
]
print(json.dumps({
    "kind": "ready",
    "python": sys.version,
    "python_executable": sys.executable,
    "eval_source": inspect.getsourcefile(rouge_eval),
    "punkt_resource": str(punkt_resource),
    "punkt_resource_sha256": hashlib.sha256(
        punkt_resource.read_bytes()
    ).hexdigest(),
    "dependencies": {
        package: importlib.metadata.version(package) for package in packages
    },
}, ensure_ascii=False), flush=True)

for raw_line in sys.stdin:
    request = json.loads(raw_line)
    if request.get("kind") == "stop":
        break
    try:
        page_id = request["page_id"]
        dataset = request["dataset"]
        target = request["target"]
        prediction = request["prediction"]
        rouge = rouge_eval(page_id, "clusy", dataset, target, prediction)[0]
        levenshtein = levenshtein_eval(
            page_id, "clusy", dataset, target, prediction
        )[0]
        response = {
            "kind": "score",
            "page_id": page_id,
            "dataset": dataset,
            "rouge": rouge,
            "levenshtein": levenshtein,
        }
    except BaseException as error:
        response = {
            "kind": "error",
            "error_type": type(error).__name__,
            "error": str(error),
        }
    print(json.dumps(response, ensure_ascii=False), flush=True)
"""


class BenchmarkError(RuntimeError):
    """A provenance, integrity, scorer, or output condition invalidates a run."""


@dataclass(frozen=True)
class DatasetRecord:
    index: int
    dataset: str
    page_id: str
    url: str
    source: Any
    reference: str
    html_path: Path


@dataclass(frozen=True)
class ExtractionInput:
    """The complete label-free payload passed to production extraction."""

    dataset: str
    page_id: str
    url: str
    html: str


@dataclass(frozen=True)
class ExtractionObservation:
    prediction: str
    latency_seconds: float
    strategy: str
    word_count: int
    confidence: float
    page_type: str
    error: dict[str, str] | None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate Clusy on the pinned 3,985-page Webis SIGIR 2023 content "
            "extraction benchmark with the exact upstream scorer."
        )
    )
    parser.add_argument(
        "--official-repo",
        type=Path,
        required=True,
        help="Pinned chatnoir-eu/web-content-extraction-benchmark checkout.",
    )
    parser.add_argument(
        "--official-python",
        type=Path,
        required=True,
        help="Python 3.11 executable containing the pinned official scorer dependencies.",
    )
    parser.add_argument(
        "--nltk-data",
        type=Path,
        required=True,
        help="NLTK data root containing the pinned tokenizers/punkt.zip.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "bench" / "results" / "webis",
        help="New artifact directory, or an existing partial directory with --resume.",
    )
    parser.add_argument(
        "--datasets",
        default="all",
        help="Comma-separated official dataset IDs, or 'all' (default).",
    )
    parser.add_argument(
        "--limit-per-dataset",
        type=int,
        default=None,
        help="Smoke-test only: evaluate the first N page IDs in each selected dataset.",
    )
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--scorer-concurrency", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=1,
        help="Fsync progress every N batches.",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Verify all pins, corpus, scorer canaries, and official baselines without extraction.",
    )
    return parser.parse_args(argv)


def _run(
    arguments: list[str],
    *,
    cwd: Path,
    check: bool = True,
) -> str:
    result = subprocess.run(
        arguments,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise BenchmarkError(
            f"command failed ({result.returncode}): {' '.join(arguments)}: {detail}"
        )
    return result.stdout.strip()


def _git(root: Path, *arguments: str, check: bool = True) -> str:
    return _run(["git", *arguments], cwd=root, check=check)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _hash_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _tree_digest(root: Path) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    files = 0
    total_bytes = 0
    paths = sorted(path for path in root.rglob("*") if path.is_file())
    for path in paths:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        if len(relative) >= 2**32:
            raise BenchmarkError("relative corpus path is unexpectedly long")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                total_bytes += len(block)
                digest.update(block)
        files += 1
    return digest.hexdigest(), files, total_bytes


def _verify_file(path: Path, expected_bytes: int, expected_sha256: str) -> dict[str, Any]:
    if not path.is_file():
        raise BenchmarkError(f"required pinned file does not exist: {path}")
    actual_bytes = path.stat().st_size
    if actual_bytes != expected_bytes:
        raise BenchmarkError(
            f"size mismatch for {path}: expected {expected_bytes}, got {actual_bytes}"
        )
    actual_sha256 = _sha256(path)
    if actual_sha256 != expected_sha256:
        raise BenchmarkError(
            f"SHA-256 mismatch for {path}: expected {expected_sha256}, got {actual_sha256}"
        )
    return {
        "path": str(path.resolve()),
        "bytes": actual_bytes,
        "sha256": actual_sha256,
    }


def verify_official_repository(root: Path) -> dict[str, Any]:
    root = root.resolve()
    if not (root / ".git").exists():
        raise BenchmarkError(f"not a Git checkout: {root}")
    commit = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    if commit != OFFICIAL_COMMIT or tree != OFFICIAL_TREE:
        raise BenchmarkError(
            f"official repository drift: expected {OFFICIAL_COMMIT}/{OFFICIAL_TREE}, "
            f"got {commit}/{tree}"
        )
    tracked_status = _git(root, "status", "--porcelain=v1", "--untracked-files=no")
    if tracked_status:
        raise BenchmarkError("official repository has modified tracked files")

    verified_files: dict[str, Any] = {}
    for relative, expected_hash in OFFICIAL_FILE_HASHES.items():
        path = root / relative
        if not path.is_file():
            raise BenchmarkError(f"missing pinned official file: {relative}")
        actual_hash = _sha256(path)
        if actual_hash != expected_hash:
            raise BenchmarkError(
                f"official file drift for {relative}: expected {expected_hash}, got {actual_hash}"
            )
        verified_files[relative] = {
            "bytes": path.stat().st_size,
            "sha256": actual_hash,
        }

    data_archive = _verify_file(
        root / DATA_ARCHIVE_RELATIVE_PATH,
        DATA_ARCHIVE_BYTES,
        DATA_ARCHIVE_SHA256,
    )
    metrics_archive = _verify_file(
        root / METRICS_ARCHIVE_RELATIVE_PATH,
        METRICS_ARCHIVE_BYTES,
        METRICS_ARCHIVE_SHA256,
    )
    return {
        "repository": OFFICIAL_REPOSITORY,
        "path": str(root),
        "commit": commit,
        "tree": tree,
        "tracked_worktree_clean": True,
        "files": verified_files,
        "data_archive": data_archive,
        "metrics_archive": metrics_archive,
    }


def verify_corpus(root: Path) -> tuple[dict[str, Any], list[DatasetRecord]]:
    combined = root.resolve() / "datasets" / "combined"
    if not combined.is_dir():
        raise BenchmarkError(
            f"extracted corpus missing at {combined}; extract datasets/combined.tar.xz"
        )
    tree_sha256, files, total_bytes = _tree_digest(combined)
    if (
        tree_sha256 != EXTRACTED_TREE_SHA256
        or files != EXTRACTED_FILES
        or total_bytes != EXTRACTED_BYTES
    ):
        raise BenchmarkError(
            "extracted corpus drift: expected "
            f"{EXTRACTED_TREE_SHA256}/{EXTRACTED_FILES}/{EXTRACTED_BYTES}, got "
            f"{tree_sha256}/{files}/{total_bytes}"
        )

    records: list[DatasetRecord] = []
    dataset_details: dict[str, Any] = {}
    index = 0
    for dataset in DATASETS:
        ground_truth_path = combined / "ground-truth" / f"{dataset}.jsonl"
        html_dir = combined / "html" / dataset
        rows: list[dict[str, Any]] = []
        with ground_truth_path.open("r", encoding="utf-8", newline="") as handle:
            for line_number, raw_line in enumerate(handle, 1):
                try:
                    row = json.loads(raw_line)
                except json.JSONDecodeError as error:
                    raise BenchmarkError(
                        f"invalid ground truth JSON at {ground_truth_path}:{line_number}"
                    ) from error
                if not isinstance(row, dict):
                    raise BenchmarkError(
                        f"non-object ground truth at {ground_truth_path}:{line_number}"
                    )
                rows.append(row)
        rows.sort(key=lambda row: str(row.get("page_id", "")))

        expected_count = DATASET_PAGE_COUNTS[dataset]
        if len(rows) != expected_count:
            raise BenchmarkError(
                f"{dataset} ground-truth count drift: expected {expected_count}, got {len(rows)}"
            )
        ids: set[str] = set()
        empty_references = 0
        urls = 0
        for row in rows:
            page_id = row.get("page_id")
            reference = row.get("plaintext")
            if not isinstance(page_id, str) or len(page_id) != 64:
                raise BenchmarkError(f"invalid {dataset} page_id: {page_id!r}")
            if page_id in ids:
                raise BenchmarkError(f"duplicate {dataset} page_id: {page_id}")
            ids.add(page_id)
            if reference is None:
                reference = ""
            if not isinstance(reference, str):
                raise BenchmarkError(f"non-string {dataset} reference: {page_id}")
            url = row.get("url") or ""
            if not isinstance(url, str):
                raise BenchmarkError(f"non-string {dataset} URL: {page_id}")
            html_path = html_dir / f"{page_id}.html"
            if not html_path.is_file():
                raise BenchmarkError(f"missing HTML for {dataset}/{page_id}")
            try:
                html_path.read_text(encoding="utf-8")
            except UnicodeDecodeError as error:
                raise BenchmarkError(
                    f"official HTML is not valid UTF-8: {dataset}/{page_id}"
                ) from error
            empty_references += int(not reference.strip())
            urls += int(bool(url))
            records.append(
                DatasetRecord(
                    index=index,
                    dataset=dataset,
                    page_id=page_id,
                    url=url,
                    source=row.get("source"),
                    reference=reference,
                    html_path=html_path,
                )
            )
            index += 1
        html_ids = {path.stem for path in html_dir.glob("*.html")}
        if html_ids != ids:
            raise BenchmarkError(
                f"{dataset} ground-truth/HTML ID sets differ "
                f"(ground truth {len(ids)}, HTML {len(html_ids)})"
            )
        dataset_details[dataset] = {
            "pages": len(rows),
            "empty_references": empty_references,
            "urls_available": urls,
            "ground_truth_sha256": _sha256(ground_truth_path),
        }
    if len(records) != TOTAL_PAGES:
        raise BenchmarkError(
            f"combined page count drift: expected {TOTAL_PAGES}, got {len(records)}"
        )
    return (
        {
            "root": str(combined),
            "tree_hash_algorithm": (
                "SHA-256(sorted files: uint32-be(relative-name byte length) || "
                "relative POSIX name bytes || file bytes)"
            ),
            "tree_sha256": tree_sha256,
            "files": files,
            "bytes": total_bytes,
            "pages": len(records),
            "datasets": dataset_details,
        },
        records,
    )


def _metric_summary(values_by_dataset: Mapping[str, list[float]]) -> dict[str, Any]:
    if not values_by_dataset:
        raise BenchmarkError("cannot aggregate an empty score collection")
    per_dataset: dict[str, Any] = {}
    all_values: list[float] = []
    dataset_means: list[float] = []
    dataset_medians: list[float] = []
    for dataset in values_by_dataset:
        values = values_by_dataset[dataset]
        if not values:
            raise BenchmarkError(f"cannot aggregate empty dataset scores: {dataset}")
        mean = statistics.fmean(values)
        median = statistics.median(values)
        per_dataset[dataset] = {
            "pages": len(values),
            "mean": mean,
            "median": median,
        }
        all_values.extend(values)
        dataset_means.append(mean)
        dataset_medians.append(median)
    return {
        "micro": {
            "mean": statistics.fmean(all_values),
            "median": statistics.median(all_values),
            "definition": "all pages pooled (page-weighted)",
        },
        "macro": {
            "mean": statistics.fmean(dataset_means),
            "median": statistics.median(dataset_medians),
            "definition": (
                "mean of dataset means; median of dataset medians "
                "(equal weight per selected dataset)"
            ),
        },
        "per_dataset": per_dataset,
    }


def _read_metric_csv_from_tar(
    archive: tarfile.TarFile,
    member_name: str,
    score_columns: tuple[str, ...],
) -> dict[str, list[float]]:
    member = archive.getmember(member_name)
    extracted = archive.extractfile(member)
    if extracted is None:
        raise BenchmarkError(f"cannot read official metrics member: {member_name}")
    with extracted:
        reader = csv.DictReader(io.TextIOWrapper(extracted, encoding="utf-8"))
        values: dict[str, list[float]] = {column: [] for column in score_columns}
        for row in reader:
            for column in score_columns:
                value = float(row[column])
                if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                    raise BenchmarkError(
                        f"invalid official score in {member_name}: {column}={value}"
                    )
                values[column].append(value)
        return values


def verify_and_load_official_baselines(metrics_archive: Path) -> dict[str, Any]:
    models: dict[str, Any] = {}
    with tarfile.open(metrics_archive, mode="r:xz") as archive:
        for model in BASELINE_MODELS:
            rouge: dict[str, dict[str, list[float]]] = {}
            levenshtein: dict[str, list[float]] = {}
            for dataset in DATASETS:
                rouge_member = f"metrics-computed/rouge/{dataset}/rouge_{model}.csv"
                levenshtein_member = (
                    f"metrics-computed/levenshtein/{dataset}/levenshtein_{model}.csv"
                )
                rouge_values = _read_metric_csv_from_tar(
                    archive, rouge_member, ("prec", "rec", "f1")
                )
                levenshtein_values = _read_metric_csv_from_tar(
                    archive, levenshtein_member, ("dist",)
                )["dist"]
                expected_pages = DATASET_PAGE_COUNTS[dataset]
                if (
                    any(len(values) != expected_pages for values in rouge_values.values())
                    or len(levenshtein_values) != expected_pages
                ):
                    raise BenchmarkError(
                        f"official per-page baseline count drift for {model}/{dataset}"
                    )
                rouge[dataset] = rouge_values
                levenshtein[dataset] = levenshtein_values
            model_summary = {
                "display_name": BASELINE_NAMES[model],
                "rouge_lsum": {
                    field: _metric_summary(
                        {dataset: rouge[dataset][column] for dataset in DATASETS}
                    )
                    for field, column in (
                        ("precision", "prec"),
                        ("recall", "rec"),
                        ("f1", "f1"),
                    )
                },
                "normalized_levenshtein_similarity_ratio": _metric_summary(
                    {dataset: levenshtein[dataset] for dataset in DATASETS}
                ),
            }
            models[model] = model_summary

    for model, expected in BASELINE_CANARIES.items():
        actual = models[model]
        checks = {
            "macro_rouge_f1_mean": actual["rouge_lsum"]["f1"]["macro"]["mean"],
            "micro_rouge_f1_mean": actual["rouge_lsum"]["f1"]["micro"]["mean"],
            "macro_levenshtein_mean": actual["normalized_levenshtein_similarity_ratio"]["macro"][
                "mean"
            ],
            "micro_levenshtein_mean": actual["normalized_levenshtein_similarity_ratio"]["micro"][
                "mean"
            ],
        }
        for key, expected_value in expected.items():
            if not math.isclose(checks[key], expected_value, rel_tol=0.0, abs_tol=1e-14):
                raise BenchmarkError(
                    f"official baseline aggregation drift for {model}/{key}: "
                    f"expected {expected_value}, got {checks[key]}"
                )
    return {
        "source": "pinned official metrics-computed.tar.xz per-page CSVs",
        "archive_sha256": METRICS_ARCHIVE_SHA256,
        "models": models,
    }


def verify_nltk_data(root: Path) -> dict[str, Any]:
    resolved_root = root.resolve()
    punkt_zip = resolved_root / PUNKT_ZIP_RELATIVE_PATH
    verified = _verify_file(punkt_zip, PUNKT_ZIP_BYTES, PUNKT_ZIP_SHA256)
    # NLTK 3.8.1 transparently redirects tokenizers/punkt/english.pickle to
    # tokenizers/punkt/PY3/english.pickle on Python 3.  Verify the bytes the
    # scorer actually loads, not the legacy sibling stored in the same archive.
    py3_english = resolved_root / PUNKT_PY3_ENGLISH_RELATIVE_PATH
    verified["loaded_english_pickle"] = _verify_file(
        py3_english,
        PUNKT_PY3_ENGLISH_BYTES,
        PUNKT_PY3_ENGLISH_SHA256,
    )
    verified["repository"] = NLTK_DATA_REPOSITORY
    verified["commit"] = NLTK_DATA_COMMIT
    verified["immutable_download"] = (
        "https://raw.githubusercontent.com/nltk/nltk_data/"
        f"{NLTK_DATA_COMMIT}/packages/tokenizers/punkt.zip"
    )
    return verified


class OfficialScorer:
    def __init__(
        self,
        *,
        worker_id: int,
        official_repo: Path,
        official_python: Path,
        nltk_data: Path,
        cache_root: Path,
    ) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(official_repo / "src")
        env["NLTK_DATA"] = str(nltk_data)
        env["MPLBACKEND"] = "Agg"
        cache = cache_root / f"worker-{worker_id}"
        cache.mkdir(parents=True, exist_ok=True)
        env["MPLCONFIGDIR"] = str(cache)
        self._worker_id = worker_id
        self._process = subprocess.Popen(
            [str(official_python), "-u", "-c", SCORER_HELPER],
            cwd=official_repo,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        ready = self._read_response("scorer startup")
        if ready.get("kind") != "ready":
            raise BenchmarkError(f"official scorer did not send ready handshake: {ready}")
        expected_source = (official_repo / "src/extraction_benchmark/eval.py").resolve()
        actual_source = Path(str(ready.get("eval_source", ""))).resolve()
        if actual_source != expected_source:
            raise BenchmarkError(f"official scorer imported the wrong evaluator: {actual_source}")
        expected_punkt = (nltk_data / PUNKT_PY3_ENGLISH_RELATIVE_PATH).resolve()
        actual_punkt = Path(str(ready.get("punkt_resource", ""))).resolve()
        if actual_punkt != expected_punkt:
            raise BenchmarkError(
                "official scorer loaded the wrong Punkt resource: "
                f"expected {expected_punkt}, got {actual_punkt}"
            )
        if ready.get("punkt_resource_sha256") != PUNKT_PY3_ENGLISH_SHA256:
            raise BenchmarkError(
                "official scorer loaded an altered Punkt resource: "
                f"expected {PUNKT_PY3_ENGLISH_SHA256}, "
                f"got {ready.get('punkt_resource_sha256')}"
            )
        dependencies = ready.get("dependencies")
        if dependencies != SCORER_DEPENDENCIES:
            raise BenchmarkError(
                f"official scorer dependency drift: expected {SCORER_DEPENDENCIES}, "
                f"got {dependencies}"
            )
        python_version = str(ready.get("python", ""))
        if not python_version.startswith("3.11."):
            raise BenchmarkError(f"official scorer must use Python 3.11, got {python_version!r}")
        self.handshake = ready

    def _read_response(self, context: str) -> dict[str, Any]:
        assert self._process.stdout is not None
        line = self._process.stdout.readline()
        if not line:
            stderr = ""
            if self._process.stderr is not None:
                stderr = self._process.stderr.read().strip()
            raise BenchmarkError(
                f"official scorer worker {self._worker_id} exited during {context}; "
                f"returncode={self._process.poll()}, stderr={stderr}"
            )
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise BenchmarkError(f"invalid scorer JSON during {context}: {line[:500]!r}") from error
        if not isinstance(value, dict):
            raise BenchmarkError(f"non-object scorer response during {context}")
        return value

    def score(self, payload: Mapping[str, str]) -> dict[str, Any]:
        if self._process.poll() is not None:
            raise BenchmarkError(f"official scorer worker {self._worker_id} is not running")
        assert self._process.stdin is not None
        self._process.stdin.write(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        self._process.stdin.flush()
        response = self._read_response(f"{payload.get('dataset')}/{payload.get('page_id')}")
        if response.get("kind") == "error":
            raise BenchmarkError(
                f"official scorer failed: {response.get('error_type')}: {response.get('error')}"
            )
        if response.get("kind") != "score":
            raise BenchmarkError(f"unexpected official scorer response: {response}")
        if response.get("page_id") != payload.get("page_id") or response.get(
            "dataset"
        ) != payload.get("dataset"):
            raise BenchmarkError("official scorer response/request identity mismatch")
        return response

    def close(self) -> None:
        if self._process.poll() is not None:
            return
        try:
            if self._process.stdin is not None:
                self._process.stdin.write('{"kind":"stop"}\n')
                self._process.stdin.flush()
                self._process.stdin.close()
            self._process.wait(timeout=10)
        except (BrokenPipeError, subprocess.TimeoutExpired):
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
        finally:
            if self._process.stdout is not None:
                self._process.stdout.close()
            if self._process.stderr is not None:
                self._process.stderr.close()


class OfficialScorerPool:
    def __init__(
        self,
        *,
        workers: int,
        official_repo: Path,
        official_python: Path,
        nltk_data: Path,
        cache_root: Path,
    ) -> None:
        self._available: queue.Queue[OfficialScorer] = queue.Queue()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)

        def create(worker_id: int) -> OfficialScorer:
            return OfficialScorer(
                worker_id=worker_id,
                official_repo=official_repo,
                official_python=official_python,
                nltk_data=nltk_data,
                cache_root=cache_root,
            )

        futures = [self._executor.submit(create, worker_id) for worker_id in range(workers)]
        self.scorers = [future.result() for future in futures]
        for scorer in self.scorers:
            self._available.put(scorer)

    def score(self, payload: Mapping[str, str]) -> dict[str, Any]:
        scorer = self._available.get()
        try:
            return scorer.score(payload)
        finally:
            self._available.put(scorer)

    def score_many(self, payloads: Sequence[Mapping[str, str]]) -> list[dict[str, Any]]:
        return list(self._executor.map(self.score, payloads))

    def self_test(self) -> dict[str, Any]:
        cases = [
            {
                "page_id": "canary-identical",
                "dataset": "cetd",
                "target": "hello world",
                "prediction": "hello world",
            },
            {
                "page_id": "canary-substitution",
                "dataset": "cetd",
                "target": "hello world",
                "prediction": "hello there",
            },
            {
                "page_id": "canary-empty",
                "dataset": "cetd",
                "target": "",
                "prediction": "",
            },
        ]
        responses = self.score_many(cases)
        expected = (
            (1.0, 1.0, 1.0, 1.0),
            (0.5, 0.5, 0.5, 0.5),
            (1.0, 1.0, 1.0, 1.0),
        )
        observed: list[tuple[float, float, float, float]] = []
        for response in responses:
            rouge = response["rouge"]
            levenshtein = response["levenshtein"]
            observed.append(
                (
                    float(rouge["prec"]),
                    float(rouge["rec"]),
                    float(rouge["f1"]),
                    float(levenshtein["dist"]),
                )
            )
        for actual, wanted in zip(observed, expected, strict=True):
            if any(
                not math.isclose(a, b, rel_tol=0.0, abs_tol=1e-15)
                for a, b in zip(actual, wanted, strict=True)
            ):
                raise BenchmarkError(
                    f"official scorer canary drift: expected {wanted}, got {actual}"
                )
        return {
            "passed": True,
            "cases": len(cases),
            "observed": observed,
            "worker_handshakes": [scorer.handshake for scorer in self.scorers],
        }

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)
        for scorer in self.scorers:
            scorer.close()

    def __enter__(self) -> OfficialScorerPool:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def _source_paths() -> list[Path]:
    paths: set[Path] = {
        ROOT / "bench/webis_benchmark.py",
        ROOT / "pyproject.toml",
        ROOT / "uv.lock",
    }
    for base, patterns in (
        (ROOT / "app", ("*.py",)),
        (ROOT / "native", ("*.py", "*.pyi", "*.rs", "*.toml", "*.lock")),
    ):
        if not base.exists():
            continue
        for pattern in patterns:
            paths.update(path for path in base.rglob(pattern) if path.is_file())
    return sorted(paths)


def _source_hashes() -> dict[str, str]:
    return {path.relative_to(ROOT).as_posix(): _sha256(path) for path in _source_paths()}


def _source_provenance() -> dict[str, Any]:
    head = _git(ROOT, "rev-parse", "HEAD")
    status = _git(ROOT, "status", "--porcelain=v1")
    return {
        "repository_root": str(ROOT),
        "head_commit": head,
        "head_tree": _git(ROOT, "rev-parse", "HEAD^{tree}"),
        "git_status_porcelain": status.splitlines(),
        "worktree_clean": not bool(status),
        "files": _source_hashes(),
    }


def _write_source_snapshot(
    path: Path,
    source: Mapping[str, Any],
    native_module: Mapping[str, Any],
) -> None:
    files = source.get("files")
    if not isinstance(files, Mapping):
        raise BenchmarkError("invalid source provenance for snapshot")

    def add_bytes(archive: tarfile.TarFile, name: str, data: bytes) -> None:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mode = 0o644
        info.mtime = 0
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        archive.addfile(info, io.BytesIO(data))

    with tarfile.open(path, mode="w") as archive:
        for relative in sorted(str(value) for value in files):
            full_path = ROOT / relative
            add_bytes(archive, relative, full_path.read_bytes())

        if native_module.get("loaded"):
            for label in ("extension", "package"):
                metadata = native_module.get(label)
                if not isinstance(metadata, Mapping):
                    raise BenchmarkError(
                        f"loaded native module is missing {label} snapshot metadata"
                    )
                runtime_path = Path(str(metadata.get("path", ""))).resolve()
                if not runtime_path.is_file():
                    raise BenchmarkError(
                        f"native {label} disappeared before snapshot: {runtime_path}"
                    )
                data = runtime_path.read_bytes()
                expected_bytes = metadata.get("bytes")
                expected_sha256 = metadata.get("sha256")
                actual_sha256 = hashlib.sha256(data).hexdigest()
                if len(data) != expected_bytes or actual_sha256 != expected_sha256:
                    raise BenchmarkError(f"native {label} changed before source snapshot")
                add_bytes(
                    archive,
                    f"__runtime__/clusy_native/{label}/{runtime_path.name}",
                    data,
                )


def _package_versions() -> dict[str, str | None]:
    packages = (
        "clusy-crawler",
        "clusy-native",
        "trafilatura",
        "readability-lxml",
        "markdownify",
        "lxml",
        "beautifulsoup4",
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
        return {
            "loaded": False,
            "error": f"{type(error).__name__}: {error}",
        }

    extension_value = getattr(_native, "__file__", None)
    package_value = getattr(clusy_native, "__file__", None)
    extension = Path(extension_value).resolve() if extension_value else None
    package = Path(package_value).resolve() if package_value else None

    def file_metadata(path: Path | None) -> dict[str, Any] | None:
        if path is None or not path.is_file():
            return None
        return {
            "path": str(path),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }

    return {
        "loaded": True,
        "extension": file_metadata(extension),
        "package": file_metadata(package),
    }


def _environment_metadata() -> dict[str, Any]:
    from app.services.extractor import native_backend_version

    return {
        "created_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "python": sys.version,
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "packages": _package_versions(),
        "native_backend": {
            "backend_version": native_backend_version(),
            "module": _native_module_metadata(),
        },
        "extraction_call": (
            'app.services.extractor.extract_content(html, url, extraction_profile="balanced")'
        ),
    }


def _select_records(
    records: list[DatasetRecord],
    selected_datasets: tuple[str, ...],
    limit_per_dataset: int | None,
) -> list[DatasetRecord]:
    selected: list[DatasetRecord] = []
    counts: Counter[str] = Counter()
    for record in records:
        if record.dataset not in selected_datasets:
            continue
        if limit_per_dataset is not None and counts[record.dataset] >= limit_per_dataset:
            continue
        selected.append(record)
        counts[record.dataset] += 1
    return [
        DatasetRecord(
            index=index,
            dataset=record.dataset,
            page_id=record.page_id,
            url=record.url,
            source=record.source,
            reference=record.reference,
            html_path=record.html_path,
        )
        for index, record in enumerate(selected)
    ]


def _extract_one(payload: ExtractionInput) -> ExtractionObservation:
    from app.services.extractor import extract_content

    started = time.perf_counter()
    try:
        result = extract_content(
            payload.html,
            payload.url,
            extraction_profile=EXTRACTION_PROFILE,
        )
        prediction = result.text
        if not isinstance(prediction, str):
            raise TypeError(
                f"production extractor returned non-string text: {type(prediction).__name__}"
            )
        return ExtractionObservation(
            prediction=prediction,
            latency_seconds=time.perf_counter() - started,
            strategy=str(result.strategy or ""),
            word_count=int(result.word_count),
            confidence=float(result.confidence),
            page_type=str(result.page_type or ""),
            error=None,
        )
    except BaseException as error:
        return ExtractionObservation(
            prediction="",
            latency_seconds=time.perf_counter() - started,
            strategy="error",
            word_count=0,
            confidence=0.0,
            page_type="",
            error={
                "type": type(error).__name__,
                "message": str(error)[:500],
            },
        )


def _finite_score(value: Any, label: str) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError) as error:
        raise BenchmarkError(f"non-numeric official score {label}: {value!r}") from error
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise BenchmarkError(f"out-of-range official score {label}: {score}")
    return score


def _page_row(
    record: DatasetRecord,
    html: str,
    observation: ExtractionObservation,
    official_score: Mapping[str, Any],
) -> dict[str, Any]:
    rouge = official_score.get("rouge")
    levenshtein = official_score.get("levenshtein")
    if not isinstance(rouge, Mapping) or not isinstance(levenshtein, Mapping):
        raise BenchmarkError("official scorer returned malformed metrics")
    return {
        "index": record.index,
        "dataset": record.dataset,
        "page_id": record.page_id,
        "url": record.url,
        "source": record.source,
        "input": {
            "html_sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
            "html_characters": len(html),
            "url_available": bool(record.url),
        },
        "reference": {
            "sha256": hashlib.sha256(record.reference.encode("utf-8")).hexdigest(),
            "characters": len(record.reference),
            "whitespace_tokens": len(record.reference.split()),
            "empty": not bool(record.reference.strip()),
        },
        "prediction": observation.prediction,
        "extraction": {
            "latency_ms": observation.latency_seconds * 1000.0,
            "strategy": observation.strategy,
            "word_count": observation.word_count,
            "confidence": observation.confidence,
            "page_type": observation.page_type,
            "error": observation.error,
        },
        "scores": {
            "rouge_lsum": {
                "precision": _finite_score(rouge.get("prec"), "rouge.precision"),
                "recall": _finite_score(rouge.get("rec"), "rouge.recall"),
                "f1": _finite_score(rouge.get("f1"), "rouge.f1"),
            },
            "normalized_levenshtein_similarity_ratio": _finite_score(
                levenshtein.get("dist"), "levenshtein.ratio"
            ),
        },
    }


class Aggregate:
    def __init__(self, datasets: tuple[str, ...]) -> None:
        self.datasets = datasets
        self.values: dict[str, dict[str, list[float]]] = {
            metric: {dataset: [] for dataset in datasets}
            for metric in (
                "rouge_precision",
                "rouge_recall",
                "rouge_f1",
                "levenshtein",
            )
        }
        self.latencies_ms: list[float] = []
        self.strategies: Counter[str] = Counter()
        self.error_types: Counter[str] = Counter()
        self.empty_predictions = 0
        self.prediction_characters = 0
        self.rows = 0

    def add(self, row: Mapping[str, Any]) -> None:
        dataset = str(row["dataset"])
        if dataset not in self.datasets:
            raise BenchmarkError(f"persisted row has unselected dataset: {dataset}")
        scores = row["scores"]
        rouge = scores["rouge_lsum"]
        extraction = row["extraction"]
        prediction = row["prediction"]
        if not isinstance(prediction, str):
            raise BenchmarkError("persisted prediction is not a string")
        self.values["rouge_precision"][dataset].append(
            _finite_score(rouge["precision"], "persisted rouge.precision")
        )
        self.values["rouge_recall"][dataset].append(
            _finite_score(rouge["recall"], "persisted rouge.recall")
        )
        self.values["rouge_f1"][dataset].append(_finite_score(rouge["f1"], "persisted rouge.f1"))
        self.values["levenshtein"][dataset].append(
            _finite_score(
                scores["normalized_levenshtein_similarity_ratio"],
                "persisted levenshtein.ratio",
            )
        )
        latency = float(extraction["latency_ms"])
        if not math.isfinite(latency) or latency < 0:
            raise BenchmarkError(f"invalid persisted latency: {latency}")
        self.latencies_ms.append(latency)
        self.strategies[str(extraction.get("strategy") or "<empty>")] += 1
        error = extraction.get("error")
        if isinstance(error, Mapping):
            self.error_types[str(error.get("type") or "unknown")] += 1
        self.empty_predictions += int(not bool(prediction.strip()))
        self.prediction_characters += len(prediction)
        self.rows += 1

    def export(self) -> dict[str, Any]:
        return {
            "pages": self.rows,
            "rouge_lsum": {
                "precision": _metric_summary(self.values["rouge_precision"]),
                "recall": _metric_summary(self.values["rouge_recall"]),
                "f1": _metric_summary(self.values["rouge_f1"]),
            },
            "normalized_levenshtein_similarity_ratio": _metric_summary(self.values["levenshtein"]),
            "latency_ms": _distribution(self.latencies_ms),
            "strategies": dict(self.strategies.most_common()),
            "errors": {
                "total": sum(self.error_types.values()),
                "by_type": dict(self.error_types.most_common()),
            },
            "empty_predictions": self.empty_predictions,
            "prediction_characters": self.prediction_characters,
        }


def _percentile(values: list[float], probability: float) -> float:
    if not values:
        raise BenchmarkError("cannot calculate percentile of empty values")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _distribution(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "min": min(values),
        "max": max(values),
    }


def _peak_rss_bytes(who: int) -> int | None:
    value = resource.getrusage(who).ru_maxrss
    if value <= 0:
        return None
    # macOS reports bytes; Linux reports KiB.
    return int(value if sys.platform == "darwin" else value * 1024)


def _atomic_write_json(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _atomic_write_text(path: Path, value: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BenchmarkError(f"cannot read valid JSON from {path}") from error
    if not isinstance(value, dict):
        raise BenchmarkError(f"expected JSON object in {path}")
    return value


def _prepare_output(output: Path, resume: bool) -> None:
    if resume:
        if not output.is_dir():
            raise BenchmarkError(f"--resume output directory does not exist: {output}")
        return
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise BenchmarkError(
            f"output already exists: {output}; choose a new path or use --resume"
        ) from error


def _load_partial(
    path: Path,
    progress: Mapping[str, Any],
    records: list[DatasetRecord],
    aggregate: Aggregate,
) -> tuple[int, Any]:
    committed_bytes = int(progress.get("committed_bytes", -1))
    committed_pages = int(progress.get("pages", -1))
    committed_sha256 = progress.get("committed_sha256")
    if committed_bytes < 0 or committed_pages < 0:
        raise BenchmarkError("invalid checkpoint offsets")
    if not isinstance(committed_sha256, str) or len(committed_sha256) != 64:
        raise BenchmarkError("checkpoint is missing a valid committed prefix SHA-256")
    if not path.is_file():
        if committed_bytes == 0 and committed_pages == 0:
            empty_digest = hashlib.sha256()
            if empty_digest.hexdigest() != committed_sha256:
                raise BenchmarkError("empty checkpoint prefix SHA-256 mismatch")
            return 0, empty_digest
        raise BenchmarkError("checkpoint references a missing partial page file")
    actual_size = path.stat().st_size
    if actual_size < committed_bytes:
        raise BenchmarkError(
            f"partial page file is shorter than checkpoint: {actual_size} < {committed_bytes}"
        )
    if actual_size > committed_bytes:
        with path.open("r+b") as handle:
            handle.truncate(committed_bytes)
            handle.flush()
            os.fsync(handle.fileno())
    raw = path.read_bytes()
    if len(raw) != committed_bytes:
        raise BenchmarkError("failed to restore committed partial page prefix")
    prefix_digest = hashlib.sha256(raw)
    if prefix_digest.hexdigest() != committed_sha256:
        raise BenchmarkError("committed partial page prefix SHA-256 mismatch")
    if raw and not raw.endswith(b"\n"):
        raise BenchmarkError("committed partial page prefix does not end at a JSONL record")
    rows = 0
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise BenchmarkError("invalid JSON in committed partial page file") from error
        if rows >= len(records):
            raise BenchmarkError("checkpoint contains more rows than the selected corpus")
        expected = records[rows]
        if not isinstance(row, dict):
            raise BenchmarkError(f"checkpoint row is not an object at index {rows}")
        if (
            row.get("index") != rows
            or row.get("dataset") != expected.dataset
            or row.get("page_id") != expected.page_id
            or row.get("url") != expected.url
            or row.get("source") != expected.source
        ):
            raise BenchmarkError(f"checkpoint row identity mismatch at index {rows}")
        html = expected.html_path.read_text(encoding="utf-8")
        input_metadata = row.get("input")
        reference_metadata = row.get("reference")
        if not isinstance(input_metadata, Mapping) or not isinstance(
            reference_metadata,
            Mapping,
        ):
            raise BenchmarkError(f"checkpoint row provenance is malformed at index {rows}")
        expected_html_sha256 = hashlib.sha256(html.encode("utf-8")).hexdigest()
        expected_reference_sha256 = hashlib.sha256(expected.reference.encode("utf-8")).hexdigest()
        if (
            input_metadata.get("html_sha256") != expected_html_sha256
            or input_metadata.get("html_characters") != len(html)
            or reference_metadata.get("sha256") != expected_reference_sha256
            or reference_metadata.get("characters") != len(expected.reference)
        ):
            raise BenchmarkError(f"checkpoint row corpus provenance mismatch at index {rows}")
        aggregate.add(row)
        rows += 1
    if rows != committed_pages:
        raise BenchmarkError(
            f"checkpoint page mismatch: progress says {committed_pages}, JSONL has {rows}"
        )
    return rows, prefix_digest


def _selected_dataset_counts(records: Iterable[DatasetRecord]) -> dict[str, int]:
    counts: Counter[str] = Counter(record.dataset for record in records)
    return {dataset: counts[dataset] for dataset in DATASETS if counts[dataset]}


def _write_official_model_outputs(output: Path, pages_path: Path) -> dict[str, Any]:
    model_root = output / "official-model-outputs"
    model_root.mkdir(exist_ok=True)
    handles: dict[str, Any] = {}
    paths: dict[str, Path] = {}
    counts: Counter[str] = Counter()
    try:
        for dataset in DATASETS:
            dataset_dir = model_root / dataset
            dataset_dir.mkdir(exist_ok=True)
            path = dataset_dir / f"{MODEL_NAME}.jsonl"
            paths[dataset] = path
            handles[dataset] = path.open("w", encoding="utf-8", newline="\n")
        with pages_path.open("r", encoding="utf-8") as source:
            for raw_line in source:
                row = json.loads(raw_line)
                dataset = row["dataset"]
                payload = {
                    "page_id": row["page_id"],
                    "plaintext": row["prediction"],
                    "model": MODEL_NAME,
                }
                handles[dataset].write(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
                )
                counts[dataset] += 1
        for handle in handles.values():
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        for handle in handles.values():
            handle.close()
    return {
        dataset: {
            "path": str(paths[dataset].relative_to(output)),
            "pages": counts[dataset],
            "bytes": paths[dataset].stat().st_size,
            "sha256": _sha256(paths[dataset]),
        }
        for dataset in DATASETS
        if counts[dataset]
    }


def _artifact_manifest(output: Path) -> dict[str, Any]:
    files: dict[str, Any] = {}
    for path in sorted(candidate for candidate in output.rglob("*") if candidate.is_file()):
        relative = path.relative_to(output).as_posix()
        if relative == "manifest.json":
            continue
        files[relative] = {
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
    return {
        "algorithm": "sha256",
        "root": str(output.resolve()),
        "files": files,
    }


def _claimability(
    *,
    full_corpus: bool,
    source_stable: bool,
    native_stable: bool,
    source: Mapping[str, Any],
    errors: int,
) -> dict[str, Any]:
    reasons: list[str] = []
    if not full_corpus:
        reasons.append("only a selected subset was evaluated")
    if not source_stable:
        reasons.append("production source changed while the benchmark was running")
    if not native_stable:
        reasons.append("loaded native module changed while the benchmark was running")
    if not bool(source.get("worktree_clean")):
        reasons.append(
            "production worktree was dirty; source-snapshot.tar makes this run auditable "
            "but it is not a commit-pinned archival result"
        )
    if errors:
        reasons.append(f"{errors} production extraction errors were scored as empty predictions")
    if not reasons:
        status = "ARCHIVAL_REPRODUCIBLE"
    elif full_corpus and source_stable:
        status = "FULL_CORPUS_PRELIMINARY"
    else:
        status = "NOT_CLAIMABLE"
    return {
        "status": status,
        "reasons": reasons,
        "scope": (
            "End-to-end deterministic Clusy extraction on the fixed Webis corpus; "
            "not evidence of universal or live-web state of the art."
        ),
    }


def _comparison(
    clusy: Mapping[str, Any],
    baselines: Mapping[str, Any],
) -> dict[str, Any]:
    clusy_f1 = clusy["rouge_lsum"]["f1"]["macro"]["mean"]
    clusy_lev = clusy["normalized_levenshtein_similarity_ratio"]["macro"]["mean"]
    published = baselines["models"]
    weighted_f1 = published["ensemble_weighted"]["rouge_lsum"]["f1"]["macro"]["mean"]
    weighted_lev = published["ensemble_weighted"]["normalized_levenshtein_similarity_ratio"][
        "macro"
    ]["mean"]
    single_f1 = published["trafilatura"]["rouge_lsum"]["f1"]["macro"]["mean"]
    single_lev = published["trafilatura"]["normalized_levenshtein_similarity_ratio"]["macro"][
        "mean"
    ]
    return {
        "primary_metric": "official equal-dataset macro mean",
        "clusy": {
            "rouge_lsum_f1": clusy_f1,
            "normalized_levenshtein_similarity_ratio": clusy_lev,
        },
        "published_best_weighted_ensemble": {
            "rouge_lsum_f1": weighted_f1,
            "normalized_levenshtein_similarity_ratio": weighted_lev,
            "clusy_delta_rouge_lsum_f1": clusy_f1 - weighted_f1,
            "clusy_delta_levenshtein_ratio": clusy_lev - weighted_lev,
        },
        "published_best_single_system_trafilatura": {
            "rouge_lsum_f1": single_f1,
            "normalized_levenshtein_similarity_ratio": single_lev,
            "clusy_delta_rouge_lsum_f1": clusy_f1 - single_f1,
            "clusy_delta_levenshtein_ratio": clusy_lev - single_lev,
        },
        "fairness_warning": (
            "Published baselines are the official 2023 outputs and dependency versions; "
            "Clusy is a 2026 end-to-end production pipeline. This is a fixed-output "
            "historical comparison, not a contemporaneous reimplementation contest."
        ),
    }


def _format_score(value: Any) -> str:
    return f"{float(value):.4f}"


def _report_markdown(summary: Mapping[str, Any]) -> str:
    results = summary["results"]
    comparison = summary["comparison"]
    performance = summary["performance"]
    claimability = summary["claimability"]
    evaluator_hash = OFFICIAL_FILE_HASHES["src/extraction_benchmark/eval.py"]
    weighted = comparison["published_best_weighted_ensemble"]
    trafilatura = comparison["published_best_single_system_trafilatura"]
    lines = [
        "# Webis Web Content Extraction Benchmark result",
        "",
        f"- Status: **{claimability['status']}**",
        f"- Pages: {results['pages']:,}",
        f"- Production extraction errors: {results['errors']['total']}",
        f"- Empty predictions: {results['empty_predictions']}",
        f"- Official repository commit: `{OFFICIAL_COMMIT}`",
        f"- Official corpus SHA-256: `{DATA_ARCHIVE_SHA256}`",
        f"- Official evaluator SHA-256: `{evaluator_hash}`",
        "",
        "## Primary fixed-corpus result",
        "",
        "| System | Macro ROUGE-LSum F1 | Macro normalized Levenshtein similarity |",
        "|---|---:|---:|",
        (
            "| Clusy deterministic production path | "
            f"{_format_score(comparison['clusy']['rouge_lsum_f1'])} | "
            f"{_format_score(comparison['clusy']['normalized_levenshtein_similarity_ratio'])} |"
        ),
        (
            "| Published best weighted ensemble | "
            f"{_format_score(weighted['rouge_lsum_f1'])} | "
            f"{_format_score(weighted['normalized_levenshtein_similarity_ratio'])} |"
        ),
        (
            "| Published best single system (Trafilatura) | "
            f"{_format_score(trafilatura['rouge_lsum_f1'])} | "
            f"{_format_score(trafilatura['normalized_levenshtein_similarity_ratio'])} |"
        ),
        "",
        "The official macro average gives each of the eight datasets equal weight. "
        "The artifact also includes page-weighted micro means and medians.",
        "",
        "## Per-dataset ROUGE-LSum F1",
        "",
        "| Dataset | Pages | Mean | Median |",
        "|---|---:|---:|---:|",
    ]
    for dataset, values in results["rouge_lsum"]["f1"]["per_dataset"].items():
        lines.append(
            f"| {DATASET_FRIENDLY_NAMES[dataset]} | {values['pages']} | "
            f"{_format_score(values['mean'])} | {_format_score(values['median'])} |"
        )
    lines.extend(
        [
            "",
            "## Measured performance",
            "",
            f"- Extraction-only wall time: {performance['extraction_wall_seconds']:.3f} s",
            f"- Extraction throughput: {performance['extraction_pages_per_second']:.3f} pages/s",
            f"- Official scoring wall time: {performance['scoring_wall_seconds']:.3f} s",
            f"- End-to-end measured wall time: {performance['measured_wall_seconds']:.3f} s",
            (
                "- Per-page extraction latency p50/p95: "
                f"{results['latency_ms']['median']:.3f}/"
                f"{results['latency_ms']['p95']:.3f} ms"
            ),
            "",
            "## Interpretation limits",
            "",
            "- The corpus is public and has no hidden test split; this run is an auditable "
            "regression/comparison benchmark, not a contamination-proof blind evaluation.",
            "- The eight source datasets use different annotation guidelines and contain "
            "historical pages. The result does not cover fetching, JavaScript rendering, "
            "robots policy, anti-bot behavior, or live-site reliability.",
            "- Clusy returns production Markdown while the target is plaintext. No cleanup "
            "or benchmark-specific post-processing was applied before scoring.",
            "- Published systems are pinned historical 2023 outputs; Clusy is the current "
            "production pipeline and can itself combine specialized and fallback paths.",
            "- `Levenshtein.ratio` is called “distance” by the upstream files, but it is a "
            "similarity ratio where higher is better.",
            "",
            "## Claimability",
            "",
        ]
    )
    if claimability["reasons"]:
        lines.extend(f"- {reason}" for reason in claimability["reasons"])
    else:
        lines.append(
            "- Full pinned corpus, stable source/native binary, clean worktree, and exact scorer."
        )
    lines.extend(
        [
            "",
            f"Paper: {PAPER_URL}",
            "",
        ]
    )
    return "\n".join(lines)


def _parse_datasets(raw: str) -> tuple[str, ...]:
    if raw.strip().lower() == "all":
        return DATASETS
    values = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    unknown = sorted(set(values) - set(DATASETS))
    if unknown:
        raise BenchmarkError(f"unknown dataset IDs: {', '.join(unknown)}")
    if not values:
        raise BenchmarkError("no datasets selected")
    return tuple(dataset for dataset in DATASETS if dataset in values)


def _validate_args(args: argparse.Namespace) -> None:
    if args.limit_per_dataset is not None and args.limit_per_dataset <= 0:
        raise BenchmarkError("--limit-per-dataset must be positive")
    for name in ("concurrency", "scorer_concurrency", "batch_size", "checkpoint_every"):
        if int(getattr(args, name)) <= 0:
            raise BenchmarkError(f"--{name.replace('_', '-')} must be positive")
    if not args.official_python.is_file() or not os.access(args.official_python, os.X_OK):
        raise BenchmarkError(f"--official-python is not an executable file: {args.official_python}")


def run_benchmark(args: argparse.Namespace) -> int:
    _validate_args(args)
    selected_datasets = _parse_datasets(args.datasets)
    official_repo = args.official_repo.resolve()
    # Do not resolve the executable symlink: venv Python binaries commonly
    # point at the base interpreter, and resolving would silently bypass the
    # scorer environment's site-packages.
    official_python = Path(os.path.abspath(args.official_python))
    nltk_data = args.nltk_data.resolve()
    output = args.output.resolve()

    official = verify_official_repository(official_repo)
    corpus, all_records = verify_corpus(official_repo)
    nltk = verify_nltk_data(nltk_data)
    baselines = verify_and_load_official_baselines(official_repo / METRICS_ARCHIVE_RELATIVE_PATH)
    records = _select_records(all_records, selected_datasets, args.limit_per_dataset)
    if not records:
        raise BenchmarkError("selected benchmark contains no pages")

    source_before = _source_provenance()
    environment = _environment_metadata()
    native_backend = environment.get("native_backend")
    if not isinstance(native_backend, Mapping):
        raise BenchmarkError("production environment omitted native backend metadata")
    native_before = native_backend.get("module")
    if not isinstance(native_before, Mapping):
        raise BenchmarkError("production environment omitted native module metadata")
    full_corpus = (
        selected_datasets == DATASETS
        and args.limit_per_dataset is None
        and len(records) == TOTAL_PAGES
    )
    selection = {
        "datasets": list(selected_datasets),
        "limit_per_dataset": args.limit_per_dataset,
        "pages": len(records),
        "dataset_pages": _selected_dataset_counts(records),
        "full_official_corpus": full_corpus,
        "record_order": "canonical dataset order, then lexicographic page_id",
        "selected_ids_sha256": _hash_json([[record.dataset, record.page_id] for record in records]),
    }

    if args.preflight_only:
        cache_root = (
            Path(os.environ.get("TMPDIR", "/tmp")) / f"clusy-webis-scorer-cache-{os.getpid()}"
        )
        cache_root.mkdir(parents=True, exist_ok=False)
        try:
            with OfficialScorerPool(
                workers=args.scorer_concurrency,
                official_repo=official_repo,
                official_python=official_python,
                nltk_data=nltk_data,
                cache_root=cache_root,
            ) as scorers:
                scorer_self_test = scorers.self_test()
        finally:
            shutil.rmtree(cache_root, ignore_errors=True)
        print(
            json.dumps(
                {
                    "status": "preflight_passed",
                    "official": official,
                    "corpus": corpus,
                    "nltk": nltk,
                    "selection": selection,
                    "production_environment": environment,
                    "scorer": scorer_self_test,
                    "baseline_canaries": BASELINE_CANARIES,
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        return 0

    _prepare_output(output, args.resume)
    partial_path = output / "pages.jsonl.partial"
    pages_path = output / "pages.jsonl"
    progress_path = output / "progress.json"
    config_path = output / "run-config.json"
    cache_root = output / ".scorer-cache"

    fingerprint_payload = {
        "schema": 2,
        "official_commit": OFFICIAL_COMMIT,
        "official_tree": OFFICIAL_TREE,
        "data_archive_sha256": DATA_ARCHIVE_SHA256,
        "extracted_tree_sha256": EXTRACTED_TREE_SHA256,
        "metrics_archive_sha256": METRICS_ARCHIVE_SHA256,
        "evaluator_sha256": OFFICIAL_FILE_HASHES["src/extraction_benchmark/eval.py"],
        "punkt_zip_sha256": PUNKT_ZIP_SHA256,
        "punkt_py3_english_sha256": PUNKT_PY3_ENGLISH_SHA256,
        "nltk_data_commit": NLTK_DATA_COMMIT,
        "scorer_dependencies": SCORER_DEPENDENCIES,
        "selection": selection,
        "extraction_profile": EXTRACTION_PROFILE,
        "production_source_files": source_before["files"],
        "production_head": source_before["head_commit"],
        "production_packages": environment["packages"],
        "production_native_backend": native_backend,
        "concurrency": args.concurrency,
        "scorer_concurrency": args.scorer_concurrency,
        "batch_size": args.batch_size,
    }
    fingerprint = _hash_json(fingerprint_payload)
    run_config = {
        "schema": 2,
        "fingerprint": fingerprint,
        "fingerprint_payload": fingerprint_payload,
        "official": official,
        "corpus": corpus,
        "nltk": nltk,
        "selection": selection,
        "environment": environment,
        "production_source": source_before,
        "metric_protocol": {
            "implementation": (
                "direct IPC calls to pinned upstream "
                "extraction_benchmark.eval.rouge_eval and levenshtein_eval"
            ),
            "rouge": (
                "ROUGE-LSum, whitespace tokenization, no stemming, "
                "split_summaries=True, upstream empty-target override"
            ),
            "levenshtein": (
                "Levenshtein.ratio over upstream whitespace-token lists; "
                "higher is better despite upstream 'distance' label"
            ),
            "prediction_transform": "identity",
            "macro": ("mean of eight dataset means; median of eight dataset medians"),
            "micro": "mean/median over all selected pages pooled",
        },
    }

    aggregate = Aggregate(selected_datasets)
    finalized_pages_restored = False
    if args.resume:
        existing_config = _read_json(config_path)
        if existing_config.get("fingerprint") != fingerprint:
            raise BenchmarkError(
                "resume fingerprint mismatch; source, corpus, scorer, selection, "
                "or concurrency configuration changed"
            )
        archival_config = existing_config
        progress = _read_json(progress_path)
        if progress.get("fingerprint") != fingerprint:
            raise BenchmarkError("resume progress fingerprint mismatch")
        if pages_path.is_file():
            if partial_path.exists():
                raise BenchmarkError("resume output contains both final and partial page artifacts")
            committed_bytes = progress.get("committed_bytes")
            if not isinstance(committed_bytes, int) or pages_path.stat().st_size != committed_bytes:
                raise BenchmarkError(
                    "final page artifact size differs from its committed checkpoint"
                )
            completed, committed_digest = _load_partial(
                pages_path,
                progress,
                records,
                aggregate,
            )
            if completed != len(records):
                raise BenchmarkError(
                    "final page artifact exists before the selected corpus is complete"
                )
            finalized_pages_restored = True
        else:
            completed, committed_digest = _load_partial(
                partial_path,
                progress,
                records,
                aggregate,
            )
    else:
        if pages_path.exists() or partial_path.exists():
            raise BenchmarkError("fresh output unexpectedly contains page artifacts")
        _atomic_write_json(config_path, run_config)
        archival_config = run_config
        _write_source_snapshot(
            output / "source-snapshot.tar",
            source_before,
            native_before,
        )
        committed_digest = hashlib.sha256()
        progress = {
            "schema": 2,
            "fingerprint": fingerprint,
            "state": "running",
            "pages": 0,
            "committed_bytes": 0,
            "committed_sha256": committed_digest.hexdigest(),
            "extraction_wall_seconds": 0.0,
            "scoring_wall_seconds": 0.0,
            "measured_wall_seconds": 0.0,
            "batches": 0,
        }
        partial_path.touch()
        _atomic_write_json(progress_path, progress)
        completed = 0

    initial_source = archival_config.get("production_source")
    initial_environment = archival_config.get("environment")
    if not isinstance(initial_source, Mapping) or not isinstance(
        initial_environment,
        Mapping,
    ):
        raise BenchmarkError("run config is missing initial production provenance")
    initial_native_backend = initial_environment.get("native_backend")
    if not isinstance(initial_native_backend, Mapping):
        raise BenchmarkError("run config is missing initial native backend provenance")
    initial_native = initial_native_backend.get("module")
    if not isinstance(initial_native, Mapping):
        raise BenchmarkError("run config is missing initial native module provenance")

    extraction_wall = float(progress.get("extraction_wall_seconds", 0.0))
    scoring_wall = float(progress.get("scoring_wall_seconds", 0.0))
    measured_wall = float(progress.get("measured_wall_seconds", 0.0))
    batches = int(progress.get("batches", 0))

    with OfficialScorerPool(
        workers=args.scorer_concurrency,
        official_repo=official_repo,
        official_python=official_python,
        nltk_data=nltk_data,
        cache_root=cache_root,
    ) as scorers:
        scorer_self_test = scorers.self_test()
        if not finalized_pages_restored:
            with (
                concurrent.futures.ThreadPoolExecutor(
                    max_workers=args.concurrency
                ) as extraction_executor,
                partial_path.open("ab") as page_output,
            ):
                for batch_start in range(completed, len(records), args.batch_size):
                    measured_started = time.perf_counter()
                    batch = records[batch_start : batch_start + args.batch_size]
                    html_values = [record.html_path.read_text(encoding="utf-8") for record in batch]
                    inputs = [
                        ExtractionInput(
                            dataset=record.dataset,
                            page_id=record.page_id,
                            url=record.url,
                            html=html,
                        )
                        for record, html in zip(batch, html_values, strict=True)
                    ]
                    extraction_started = time.perf_counter()
                    observations = list(extraction_executor.map(_extract_one, inputs))
                    extraction_wall += time.perf_counter() - extraction_started

                    score_payloads = [
                        {
                            "page_id": record.page_id,
                            "dataset": record.dataset,
                            "target": record.reference,
                            "prediction": observation.prediction,
                        }
                        for record, observation in zip(
                            batch,
                            observations,
                            strict=True,
                        )
                    ]
                    scoring_started = time.perf_counter()
                    official_scores = scorers.score_many(score_payloads)
                    scoring_wall += time.perf_counter() - scoring_started

                    for record, html, observation, score in zip(
                        batch,
                        html_values,
                        observations,
                        official_scores,
                        strict=True,
                    ):
                        row = _page_row(record, html, observation, score)
                        encoded = (
                            json.dumps(
                                row,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            + "\n"
                        ).encode("utf-8")
                        page_output.write(encoded)
                        committed_digest.update(encoded)
                        aggregate.add(row)
                    measured_wall += time.perf_counter() - measured_started
                    batches += 1

                    should_checkpoint = (
                        batches % args.checkpoint_every == 0 or aggregate.rows == len(records)
                    )
                    if should_checkpoint:
                        page_output.flush()
                        os.fsync(page_output.fileno())
                        progress = {
                            "schema": 2,
                            "fingerprint": fingerprint,
                            "state": "running",
                            "pages": aggregate.rows,
                            "committed_bytes": page_output.tell(),
                            "committed_sha256": committed_digest.hexdigest(),
                            "extraction_wall_seconds": extraction_wall,
                            "scoring_wall_seconds": scoring_wall,
                            "measured_wall_seconds": measured_wall,
                            "batches": batches,
                            "updated_at_utc": dt.datetime.now(dt.UTC).isoformat(),
                        }
                        _atomic_write_json(progress_path, progress)
                        print(
                            f"[webis] {aggregate.rows}/{len(records)} pages; "
                            f"errors={sum(aggregate.error_types.values())}; "
                            f"extract={extraction_wall:.1f}s; score={scoring_wall:.1f}s",
                            flush=True,
                        )

    if aggregate.rows != len(records):
        raise BenchmarkError(
            f"completed page count mismatch: expected {len(records)}, got {aggregate.rows}"
        )
    if not pages_path.is_file():
        os.replace(partial_path, pages_path)
        _fsync_directory(output)
    elif partial_path.exists():
        raise BenchmarkError("both final and partial page artifacts exist after extraction")
    final_pages_sha256 = _sha256(pages_path)
    if final_pages_sha256 != committed_digest.hexdigest():
        raise BenchmarkError("final page artifact differs from its committed prefix SHA-256")
    source_after = _source_provenance()
    source_stable = initial_source.get("files") == source_before["files"] == source_after["files"]
    native_after = _native_module_metadata()
    native_stable = initial_native == native_before == native_after
    official_after = verify_official_repository(official_repo)
    corpus_after, _ = verify_corpus(official_repo)
    nltk_after = verify_nltk_data(nltk_data)
    claim_source = {
        **initial_source,
        "worktree_clean": bool(initial_source.get("worktree_clean"))
        and bool(source_before.get("worktree_clean"))
        and bool(source_after.get("worktree_clean")),
    }
    results = aggregate.export()
    model_outputs = _write_official_model_outputs(output, pages_path)
    errors = int(results["errors"]["total"])
    performance = {
        "concurrency": args.concurrency,
        "scorer_concurrency": args.scorer_concurrency,
        "batch_size": args.batch_size,
        "extraction_wall_seconds": extraction_wall,
        "scoring_wall_seconds": scoring_wall,
        "measured_wall_seconds": measured_wall,
        "extraction_pages_per_second": len(records) / extraction_wall,
        "official_scoring_pages_per_second": len(records) / scoring_wall,
        "end_to_end_pages_per_second": len(records) / measured_wall,
        "peak_rss_self_bytes": _peak_rss_bytes(resource.RUSAGE_SELF),
        "peak_rss_children_bytes": _peak_rss_bytes(resource.RUSAGE_CHILDREN),
        "timing_definition": (
            "Extraction throughput uses only batch production-call wall time; "
            "official scoring throughput uses only scorer wall time; measured "
            "end-to-end includes HTML reads, extraction, scoring, and page-row writes "
            "but excludes provenance preflight and scorer startup."
        ),
    }
    comparison = _comparison(results, baselines)
    claimability = _claimability(
        full_corpus=full_corpus,
        source_stable=source_stable,
        native_stable=native_stable,
        source=claim_source,
        errors=errors,
    )
    summary = {
        "schema": 1,
        "benchmark": {
            "name": "Webis Web Content Extraction Benchmark",
            "paper": PAPER_URL,
            "paper_pdf": PAPER_PDF,
            "paper_doi": PAPER_DOI,
            "official_repository": OFFICIAL_REPOSITORY,
        },
        "completed_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "selection": selection,
        "results": results,
        "performance": performance,
        "comparison": comparison,
        "claimability": claimability,
        "official_baselines": baselines,
        "official_scorer_self_test": scorer_self_test,
        "official_model_outputs": model_outputs,
        "external_inputs_before": {
            "official": official,
            "corpus": corpus,
            "nltk": nltk,
        },
        "external_inputs_after": {
            "official": official_after,
            "corpus": corpus_after,
            "nltk": nltk_after,
        },
        "production_source_before": initial_source,
        "production_source_resume_invocation": source_before,
        "production_source_after": source_after,
        "production_source_stable": source_stable,
        "production_native_before": initial_native,
        "production_native_resume_invocation": native_before,
        "production_native_after": native_after,
        "production_native_stable": native_stable,
        "run_fingerprint": fingerprint,
    }
    _atomic_write_json(output / "summary.json", summary)
    _atomic_write_text(output / "report.md", _report_markdown(summary))
    progress = {
        "schema": 2,
        "fingerprint": fingerprint,
        "state": "complete",
        "pages": len(records),
        "committed_bytes": pages_path.stat().st_size,
        "committed_sha256": final_pages_sha256,
        "extraction_wall_seconds": extraction_wall,
        "scoring_wall_seconds": scoring_wall,
        "measured_wall_seconds": measured_wall,
        "batches": batches,
        "completed_at_utc": summary["completed_at_utc"],
    }
    _atomic_write_json(progress_path, progress)
    shutil.rmtree(cache_root, ignore_errors=True)
    _atomic_write_json(output / "manifest.json", _artifact_manifest(output))

    print(_report_markdown(summary))
    print(f"Artifacts: {output}")
    return 0 if source_stable and native_stable else 2


def main(argv: list[str] | None = None) -> int:
    try:
        return run_benchmark(parse_args(argv))
    except KeyboardInterrupt:
        print("benchmark interrupted; resume with --resume", file=sys.stderr)
        return 130
    except BenchmarkError as error:
        print(f"benchmark invalid: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

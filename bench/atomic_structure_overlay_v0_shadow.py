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
import importlib
import json
import math
import os
import platform
import re
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from types import ModuleType

    from app.services.atomic_structure_overlay_v0 import (
        AtomicStructureOverlayDecisionV0,
        AtomicStructureOverlayReplayV0,
        AtomicStructureOverlayV0Config,
    )

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench import webmainbench_finegrained_benchmark as fine  # noqa: E402
from bench.source_provenance import (  # noqa: E402
    SourceInventoryError,
    verify_loaded_native_source_binding,
)
from bench.webmainbench_benchmark import (  # noqa: E402
    scrub_annotation_artifacts,
)

SCHEMA_VERSION = "webmainbench.atomic-structure-overlay-v0-shadow.4"
DECISION_INPUT_SCHEMA = "webmainbench.atomic-structure-overlay-v0-decision-inputs.1"
BASELINE_PAGE_SCHEMA = "clusy.fixed-baseline-page.2"
BASELINE_MANIFEST_SCHEMA = "clusy.fixed-baseline-provenance.2"
EXPECTED_PAGES = 545
LEGACY_BASELINE_SHA256 = (
    "3d4fefffb7d809b703934ce212602d7f52e7c6d1986f884b5b638f36a9b312af"
)
MODES = ("official", "scrubbed")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_GIT_OBJECT_RE = re.compile(r"[0-9a-f]{40}")
QUALITY_THRESHOLDS = {
    "overall": 0.01,
    "code_edit": 0.03,
    "table_TEDS": 0.02,
    "text_edit": 0.0,
    "formula_edit": 0.0,
}

REQUIRED_SOURCE_FILES = (
    "app/services/atomic_structure_overlay_v0.py",
    "bench/atomic_structure_overlay_v0_shadow.py",
    "bench/export_webmainbench_decision_inputs.py",
    "bench/generate_atomic_structure_baseline.py",
    "bench/source_provenance.py",
    "bench/webmainbench_benchmark.py",
    "bench/webmainbench_finegrained_benchmark.py",
    "docs/ATOMIC_STRUCTURE_OVERLAY_V0.md",
    "native/python/clusy_native/__init__.py",
    "native/python/clusy_native/_native.pyi",
    "native/python/clusy_native/selection_certificate_v0.py",
    "native/src/document_ir_v2/selection_certificate_v0.rs",
    "tests/unit/test_atomic_structure_overlay_v0.py",
    "tests/unit/test_atomic_structure_overlay_v0_shadow.py",
    "tests/unit/test_selection_certificate_v0.py",
    "pyproject.toml",
    "uv.lock",
    "native/Cargo.toml",
    "native/Cargo.lock",
    "native/source-inventory-v1.txt",
)
BASELINE_GENERATOR_FIXED_FILES = frozenset(
    {
        "bench/atomic_structure_overlay_v0_shadow.py",
        "bench/export_webmainbench_decision_inputs.py",
        "bench/generate_atomic_structure_baseline.py",
        "bench/source_provenance.py",
        "bench/webmainbench_benchmark.py",
        "bench/webmainbench_finegrained_benchmark.py",
        "app/config.py",
        "app/services/extractor.py",
        "pyproject.toml",
        "uv.lock",
        "native/Cargo.toml",
        "native/Cargo.lock",
        "native/source-inventory-v1.txt",
        "native/python/clusy_native/__init__.py",
        "native/python/clusy_native/_native.pyi",
        "native/python/clusy_native/selection_certificate_v0.py",
        "native/src/document_ir_v2/selection_certificate_v0.rs",
    }
)
BASELINE_ENVIRONMENT_FIELDS = frozenset(
    {
        "python",
        "python_implementation",
        "python_executable",
        "platform",
        "machine",
        "fixed_environment",
        "credential_guard",
        "uv_lock_sha256",
        "cargo_lock_sha256",
    }
)
BASELINE_CREDENTIAL_ENVIRONMENT_NAMES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ELSEVIER_API_KEY",
        "EXA_API_KEY",
        "FIRECRAWL_API_KEY",
        "IEEE_API_KEY",
        "OPENAI_API_KEY",
        "QUALITY_EXTRACTION_API_KEY",
        "QUALITY_EXTRACTION_BASE_URL",
        "QUALITY_EXTRACTION_MODEL",
    }
)
BASELINE_FIXED_ENVIRONMENT = {
    "ANTHROPIC_API_KEY": "",
    "ELSEVIER_API_KEY": "",
    "ENVIRONMENT": "local",
    "EXTRACT_MAX_TEXT_LENGTH": "500000",
    "EXTRACTION_MERGE_MODE": "union",
    "IEEE_API_KEY": "",
    "MAX_CONCURRENT_EXTRACTIONS": "1",
    "NATIVE_EXTRACTION_ENABLED": "true",
    "NATIVE_EXTRACTION_MIN_CONFIDENCE": "0.60",
    "PARALLEL_EXTRACTION_ENABLED": "false",
    "QUALITY_EXTRACTION_API_KEY": "",
    "QUALITY_EXTRACTION_BASE_URL": "",
    "QUALITY_EXTRACTION_MODEL": "",
}


@dataclass(frozen=True, slots=True)
class DecisionInput:
    dataset_index: int
    track_id: str
    raw_html_digest: str
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


def _run_readonly(command: Sequence[str], *, cwd: Path) -> str:
    result = subprocess.run(
        list(command),
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise fine.BenchmarkError(
            f"provenance command failed ({result.returncode}): "
            f"{' '.join(command)}: {detail or 'no detail'}"
        )
    return result.stdout.strip()


def _module_file(module: ModuleType, expected: Path | None = None) -> dict[str, Any]:
    raw_path = getattr(module, "__file__", None)
    if not isinstance(raw_path, str):
        raise fine.BenchmarkError(f"loaded module has no file origin: {module.__name__}")
    path = Path(raw_path).resolve()
    if not path.is_file():
        raise fine.BenchmarkError(f"loaded module origin is not a file: {path}")
    if expected is not None and path != expected.resolve():
        raise fine.BenchmarkError(
            f"loaded module escaped exact source tree: {module.__name__}: {path}"
        )
    return {
        "module": module.__name__,
        "path": str(path),
        "sha256": _sha256(path),
    }


def _load_overlay_module() -> ModuleType:
    module = importlib.import_module("app.services.atomic_structure_overlay_v0")
    expected = ROOT / "app/services/atomic_structure_overlay_v0.py"
    _module_file(module, expected)
    return module


def _source_provenance_snapshot(
    *,
    overlay_module: ModuleType,
    allow_dirty: bool,
) -> dict[str, Any]:
    try:
        shared = fine.source_provenance()
        native_binding = verify_loaded_native_source_binding(ROOT)
    except (fine.BenchmarkError, SourceInventoryError) as error:
        raise fine.BenchmarkError(f"source provenance failed: {error}") from error

    commit = _run_readonly(["git", "rev-parse", "HEAD"], cwd=ROOT)
    tree = _run_readonly(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT)
    status_output = _run_readonly(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=ROOT,
    )
    if not _GIT_OBJECT_RE.fullmatch(commit) or not _GIT_OBJECT_RE.fullmatch(tree):
        raise fine.BenchmarkError("repository commit/tree identity is not canonical")
    status = status_output.splitlines() if status_output else []
    if status and not allow_dirty:
        raise fine.BenchmarkError(
            "claimable audit requires an exact clean Git tree; "
            "use --allow-dirty-exploratory only for non-claimable diagnostics"
        )

    required_hashes: dict[str, str] = {}
    for relative in REQUIRED_SOURCE_FILES:
        path = ROOT / relative
        if not path.is_file():
            raise fine.BenchmarkError(f"required source file is missing: {relative}")
        required_hashes[relative] = _sha256(path)

    clusy_native = importlib.import_module("clusy_native")
    native_extension = importlib.import_module("clusy_native._native")
    loaded_modules = {
        "overlay": _module_file(
            overlay_module,
            ROOT / "app/services/atomic_structure_overlay_v0.py",
        ),
        "clusy_native_package": _module_file(
            clusy_native,
        ),
        "clusy_native_extension": _module_file(native_extension),
        "selection_certificate_facade": _module_file(
            importlib.import_module("clusy_native.selection_certificate_v0"),
        ),
    }
    installed_source_bindings = {
        "clusy_native_package": "native/python/clusy_native/__init__.py",
        "selection_certificate_facade": (
            "native/python/clusy_native/selection_certificate_v0.py"
        ),
    }
    for module_name, relative in installed_source_bindings.items():
        identity = loaded_modules[module_name]
        loaded_path = Path(identity["path"])
        if (
            ROOT not in loaded_path.parents
            or identity["sha256"] != required_hashes[relative]
        ):
            raise fine.BenchmarkError(
                f"loaded {module_name} is not byte-bound to {relative}"
            )
    extension_path = Path(loaded_modules["clusy_native_extension"]["path"])
    if ROOT not in extension_path.parents:
        raise fine.BenchmarkError(
            "loaded native extension escaped the exact source checkout"
        )
    if not bool(native_binding.get("matched")):
        raise fine.BenchmarkError("loaded native extension is not source-bound")
    if shared.get("git_commit") != commit or bool(shared.get("git_dirty")) != bool(status):
        raise fine.BenchmarkError("shared and strict Git provenance disagree")

    snapshot = {
        "schema_version": "clusy.atomic-overlay-source-provenance.1",
        "git": {
            "commit": commit,
            "tree": tree,
            "clean": not status,
            "status": status,
        },
        "claimable_clean_tree": not status,
        "required_file_sha256": required_hashes,
        "required_file_count": len(required_hashes),
        "lock_sha256": {
            "uv.lock": required_hashes["uv.lock"],
            "native/Cargo.lock": required_hashes["native/Cargo.lock"],
        },
        "loaded_modules": loaded_modules,
        "native_source_binding": native_binding,
        "shared_source_digest": shared.get("source_digest"),
        "shared_file_sha256": shared.get("file_sha256"),
    }
    snapshot["snapshot_digest"] = _hash_json(snapshot)
    return snapshot


def _assert_snapshot_stable(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    name: str,
) -> None:
    if before != after:
        raise fine.BenchmarkError(f"{name} provenance changed during the audit")


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


def _load_baseline(
    path: Path,
    *,
    allow_legacy: bool,
) -> tuple[dict[int, tuple[str, str]], dict[str, Any]]:
    if not path.is_file():
        raise fine.BenchmarkError("fixed baseline does not exist")
    digest = _sha256(path)
    if allow_legacy and digest != LEGACY_BASELINE_SHA256:
        raise fine.BenchmarkError("opaque fixed baseline SHA-256 mismatch")
    rows: dict[int, tuple[str, str]] = {}
    status_records: list[dict[str, Any]] = []
    failures: Counter[str] = Counter()
    successful_records = 0
    with path.open("rb") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise fine.BenchmarkError(
                    f"invalid baseline row {line_number}"
                ) from error
            if not isinstance(row, dict):
                raise fine.BenchmarkError(f"invalid baseline row {line_number}")
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
            if allow_legacy:
                success = True
                strategy = "<legacy-opaque>"
                error_type = None
            else:
                if set(row) != {
                    "schema_version",
                    "dataset_index",
                    "track_id",
                    "prediction",
                    "generation",
                } or row.get("schema_version") != BASELINE_PAGE_SCHEMA:
                    raise fine.BenchmarkError(
                        f"manifest-bound baseline row {line_number} has "
                        "an invalid closed schema"
                    )
                generation = row.get("generation")
                if not isinstance(generation, dict) or set(generation) != {
                    "success",
                    "strategy",
                    "error_type",
                }:
                    raise fine.BenchmarkError(
                        f"manifest-bound baseline row {line_number} has "
                        "an invalid generation record"
                    )
                success_value = generation.get("success")
                strategy_value = generation.get("strategy")
                error_value = generation.get("error_type")
                if (
                    type(success_value) is not bool
                    or not isinstance(strategy_value, str)
                    or not strategy_value
                    or (
                        success_value
                        and error_value is not None
                    )
                    or (
                        not success_value
                        and (
                            not isinstance(error_value, str)
                            or not error_value
                            or prediction
                        )
                    )
                ):
                    raise fine.BenchmarkError(
                        f"manifest-bound baseline row {line_number} has "
                        "a noncanonical generation record"
                    )
                success = bool(success_value)
                strategy = strategy_value
                error_type = error_value if isinstance(error_value, str) else None
            if success:
                successful_records += 1
            elif isinstance(error_type, str):
                failures[error_type] += 1
            rows[index] = (track_id, prediction)
            status_records.append(
                {
                    "dataset_index": index,
                    "track_id": track_id,
                    "prediction_sha256": _sha256_text(prediction),
                    "success": success,
                    "strategy": strategy,
                    "error_type": error_type,
                }
            )
    if set(rows) != set(range(EXPECTED_PAGES)):
        raise fine.BenchmarkError("fixed baseline is not exactly 545 aligned rows")
    metadata = {
        "schema_version": (
            "legacy-opaque"
            if allow_legacy
            else BASELINE_PAGE_SCHEMA
        ),
        "sha256": digest,
        "bytes": path.stat().st_size,
        "records": len(rows),
        "successful_records": successful_records,
        "failed_records": len(rows) - successful_records,
        "failed_dataset_indices": [
            record["dataset_index"]
            for record in status_records
            if not record["success"]
        ],
        "failure_types": dict(sorted(failures.items())),
        "record_status_sha256": _hash_json(status_records),
    }
    return rows, metadata


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _valid_hash_mapping(value: object) -> bool:
    return (
        isinstance(value, dict)
        and bool(value)
        and all(
            isinstance(key, str) and key and _valid_sha256(digest)
            for key, digest in value.items()
        )
    )


def _load_baseline_provenance(
    manifest_path: Path | None,
    *,
    baseline_metadata: Mapping[str, Any],
    decision_inputs_metadata: Mapping[str, Any],
    baseline_path: Path | None = None,
    decision_inputs_path: Path | None = None,
    current_source: Mapping[str, Any] | None = None,
    require_claimable: bool,
) -> dict[str, Any]:
    if manifest_path is None:
        if require_claimable:
            raise fine.BenchmarkError(
                "claimable mode requires --baseline-manifest; the fixed baseline "
                "alone is opaque and cannot establish label-free generator provenance"
            )
        return {
            "schema_version": BASELINE_MANIFEST_SCHEMA,
            "claimable": False,
            "mode": "opaque_baseline_exploratory",
            "reason": (
                "the pinned legacy file has no generator manifest binding exact "
                "label-free inputs, extractor source, native binary, locks, config, "
                "environment, record status, or pre/post stability"
            ),
            "baseline": dict(baseline_metadata),
            "manifest_path": None,
            "manifest_sha256": None,
        }

    path = manifest_path.resolve()
    if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise fine.BenchmarkError("baseline manifest is missing or exceeds 4 MiB")
    try:
        document = json.loads(path.read_bytes())
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise fine.BenchmarkError("baseline manifest is invalid UTF-8 JSON") from error
    if (
        not isinstance(document, dict)
        or set(document) != {
            "schema_version",
            "baseline",
            "decision_inputs",
            "generator",
            "stability",
        }
        or document.get("schema_version") != BASELINE_MANIFEST_SCHEMA
    ):
        raise fine.BenchmarkError("baseline manifest schema mismatch")
    manifest_baseline = document.get("baseline")
    manifest_inputs = document.get("decision_inputs")
    generator = document.get("generator")
    source = generator.get("source_provenance") if isinstance(generator, dict) else None
    native = source.get("native_source_binding") if isinstance(source, dict) else None
    locks = source.get("lock_sha256") if isinstance(source, dict) else None
    fixed_files = source.get("fixed_file_sha256") if isinstance(source, dict) else None
    loaded_modules = source.get("loaded_modules") if isinstance(source, dict) else None
    config = generator.get("config") if isinstance(generator, dict) else None
    environment = generator.get("environment") if isinstance(generator, dict) else None
    cli_args = generator.get("cli_args") if isinstance(generator, dict) else None
    stability = document.get("stability")

    source_without_digest = dict(source) if isinstance(source, dict) else {}
    source_snapshot_digest = source_without_digest.pop("snapshot_digest", None)
    loaded_module_checks = (
        isinstance(loaded_modules, dict)
        and set(loaded_modules) == {
            "extractor",
            "clusy_native_package",
            "clusy_native_extension",
        }
        and all(
            isinstance(identity, dict)
            and set(identity) == {"module", "path", "sha256"}
            and isinstance(identity.get("module"), str)
            and bool(identity.get("module"))
            and isinstance(identity.get("path"), str)
            and bool(identity.get("path"))
            and _valid_sha256(identity.get("sha256"))
            for identity in loaded_modules.values()
        )
    )
    expected_input_identity = {
        key: decision_inputs_metadata[key]
        for key in (
            "schema_version",
            "sha256",
            "bytes",
            "records",
            "html_corpus_sha256",
            "page_id_sha256",
            "canonical_order_verified",
            "closed_fields",
            "contains_reference_or_metadata",
        )
    }
    config_digest = _hash_json(config) if isinstance(config, dict) else None
    environment_digest = (
        _hash_json(environment) if isinstance(environment, dict) else None
    )
    checks = {
        "baseline_identity": (
            isinstance(manifest_baseline, dict)
            and manifest_baseline == dict(baseline_metadata)
        ),
        "decision_inputs_identity": (
            isinstance(manifest_inputs, dict)
            and manifest_inputs == expected_input_identity
        ),
        "records": (
            baseline_metadata.get("records") == EXPECTED_PAGES
            and decision_inputs_metadata.get("records") == EXPECTED_PAGES
        ),
        "baseline_page_schema": (
            baseline_metadata.get("schema_version") == BASELINE_PAGE_SCHEMA
        ),
        "generator_closed_schema": (
            isinstance(generator, dict)
            and set(generator)
            == {
                "entrypoint",
                "prediction_field",
                "reference_labels_used",
                "benchmark_metadata_used",
                "official_metrics_used",
                "vendor_outputs_used",
                "config",
                "environment",
                "source_provenance",
                "cli_args",
            }
        ),
        "entrypoint": (
            isinstance(generator, dict)
            and generator.get("entrypoint")
            == "app.services.extractor.extract_content"
        ),
        "prediction_field": isinstance(generator, dict)
        and generator.get("prediction_field") == "prediction",
        "label_free_generation": (
            isinstance(generator, dict)
            and generator.get("reference_labels_used") is False
            and generator.get("benchmark_metadata_used") is False
            and generator.get("official_metrics_used") is False
            and generator.get("vendor_outputs_used") is False
        ),
        "fixed_config": (
            isinstance(config, dict)
            and config.get("entrypoint")
            == "app.services.extractor.extract_content"
            and config.get("extraction_profile") == "balanced"
            and config.get("url") == ""
            and config.get("prediction_transform")
            == "identity ExtractionResult.text"
            and config.get("input_transform")
            == "bench.webmainbench_benchmark.scrub_annotation_artifacts"
            and config.get("annotation_scrubber_postcondition") is True
            and config.get("concurrency") == 1
            and config.get("network_calls") is False
            and config.get("model_calls") is False
            and config.get("vendor_outputs_used") is False
        ),
        "environment": isinstance(environment, dict) and bool(environment),
        "environment_closed_schema": (
            isinstance(environment, dict)
            and set(environment) == BASELINE_ENVIRONMENT_FIELDS
            and isinstance(environment.get("fixed_environment"), dict)
            and environment.get("fixed_environment")
            == BASELINE_FIXED_ENVIRONMENT
            and isinstance(environment.get("credential_guard"), dict)
            and set(environment["credential_guard"])
            == {"checked_names", "active_names"}
            and isinstance(
                environment["credential_guard"].get("checked_names"),
                list,
            )
            and all(
                isinstance(name, str)
                for name in environment["credential_guard"]["checked_names"]
            )
            and set(environment["credential_guard"]["checked_names"])
            == BASELINE_CREDENTIAL_ENVIRONMENT_NAMES
            and environment["credential_guard"]["active_names"] == []
            and all(
                isinstance(name, str)
                and isinstance(value, str)
                and (
                    not name.endswith("_KEY")
                    or value == ""
                )
                for name, value in environment["fixed_environment"].items()
            )
            and all(
                environment["fixed_environment"].get(name, "") == ""
                for name in BASELINE_CREDENTIAL_ENVIRONMENT_NAMES
            )
        ),
        "cli_args": (
            isinstance(cli_args, dict)
            and set(cli_args)
            == {
                "decision_inputs",
                "output",
                "manifest",
                "expected_records",
            }
            and cli_args.get("expected_records") == EXPECTED_PAGES
            and isinstance(cli_args.get("manifest"), str)
            and cli_args.get("manifest") == str(path)
            and (
                decision_inputs_path is None
                or cli_args.get("decision_inputs")
                == str(decision_inputs_path.resolve())
            )
            and (
                baseline_path is None
                or cli_args.get("output") == str(baseline_path.resolve())
            )
        ),
        "source_commit": isinstance(source, dict)
        and isinstance(source.get("git_commit"), str)
        and _GIT_OBJECT_RE.fullmatch(source["git_commit"]) is not None,
        "source_tree": isinstance(source, dict)
        and isinstance(source.get("git_tree"), str)
        and _GIT_OBJECT_RE.fullmatch(source["git_tree"]) is not None,
        "source_clean": isinstance(source, dict) and source.get("git_clean") is True,
        "source_digest": isinstance(source, dict)
        and _valid_sha256(source.get("source_digest")),
        "source_files": isinstance(source, dict)
        and _valid_hash_mapping(source.get("file_sha256")),
        "fixed_source_files": (
            _valid_hash_mapping(fixed_files)
            and isinstance(fixed_files, dict)
            and set(fixed_files) == BASELINE_GENERATOR_FIXED_FILES
        ),
        "lockfiles": isinstance(locks, dict)
        and set(locks) == {"uv.lock", "native/Cargo.lock"}
        and _valid_sha256(locks.get("uv.lock"))
        and _valid_sha256(locks.get("native/Cargo.lock")),
        "native_binding": isinstance(native, dict)
        and native.get("matched") is True
        and _valid_sha256(native.get("packaged_sha256"))
        and native.get("packaged_sha256") == native.get("current_sha256"),
        "loaded_modules": loaded_module_checks,
        "loaded_module_paths": (
            isinstance(source, dict)
            and isinstance(source.get("source_root"), str)
            and Path(source["source_root"]).is_absolute()
            and isinstance(loaded_modules, dict)
            and loaded_module_checks
            and loaded_modules["extractor"]["path"]
            == str(
                Path(source["source_root"])
                / "app/services/extractor.py"
            )
            and Path(source["source_root"])
            in Path(loaded_modules["clusy_native_package"]["path"]).parents
            and Path(loaded_modules["clusy_native_package"]["path"]).name
            == "__init__.py"
            and Path(
                loaded_modules["clusy_native_package"]["path"]
            ).parent.name
            == "clusy_native"
            and Path(
                loaded_modules["clusy_native_extension"]["path"]
            ).is_absolute()
            and Path(source["source_root"])
            in Path(loaded_modules["clusy_native_extension"]["path"]).parents
            and isinstance(fixed_files, dict)
            and loaded_modules["extractor"]["sha256"]
            == fixed_files.get("app/services/extractor.py")
            and loaded_modules["clusy_native_package"]["sha256"]
            == fixed_files.get("native/python/clusy_native/__init__.py")
        ),
        "source_snapshot_digest": (
            _valid_sha256(source_snapshot_digest)
            and source_snapshot_digest == _hash_json(source_without_digest)
        ),
        "generator_protocol_files_match_current": (
            current_source is None
            or (
                isinstance(fixed_files, dict)
                and set(fixed_files) == BASELINE_GENERATOR_FIXED_FILES
                and all(
                    (ROOT / relative).is_file()
                    and _sha256(ROOT / relative) == digest
                    for relative, digest in fixed_files.items()
                )
            )
        ),
        "generator_source_matches_candidate": (
            current_source is None
            or (
                isinstance(source, dict)
                and source.get("git_commit")
                == current_source.get("git", {}).get("commit")
                and source.get("git_tree")
                == current_source.get("git", {}).get("tree")
                and source.get("source_digest")
                == current_source.get("shared_source_digest")
                and source.get("lock_sha256")
                == current_source.get("lock_sha256")
                and source.get("native_source_binding")
                == current_source.get("native_source_binding")
            )
        ),
        "environment_locks": (
            isinstance(environment, dict)
            and isinstance(locks, dict)
            and environment.get("uv_lock_sha256") == locks.get("uv.lock")
            and environment.get("cargo_lock_sha256")
            == locks.get("native/Cargo.lock")
        ),
        "stability_closed_schema": (
            isinstance(stability, dict)
            and set(stability)
            == {
                "source_snapshot_digest_before",
                "source_snapshot_digest_after",
                "source_stable",
                "decision_inputs_sha256_before",
                "decision_inputs_sha256_after",
                "decision_inputs_stable",
                "config_digest_before",
                "config_digest_after",
                "config_stable",
                "environment_digest_before",
                "environment_digest_after",
                "environment_stable",
            }
        ),
        "source_stable": (
            isinstance(stability, dict)
            and stability.get("source_stable") is True
            and stability.get("source_snapshot_digest_before")
            == source_snapshot_digest
            and stability.get("source_snapshot_digest_after")
            == source_snapshot_digest
        ),
        "decision_inputs_stable": (
            isinstance(stability, dict)
            and stability.get("decision_inputs_stable") is True
            and stability.get("decision_inputs_sha256_before")
            == decision_inputs_metadata.get("sha256")
            and stability.get("decision_inputs_sha256_after")
            == decision_inputs_metadata.get("sha256")
        ),
        "config_stable": (
            isinstance(stability, dict)
            and stability.get("config_stable") is True
            and stability.get("config_digest_before") == config_digest
            and stability.get("config_digest_after") == config_digest
        ),
        "environment_stable": (
            isinstance(stability, dict)
            and stability.get("environment_stable") is True
            and stability.get("environment_digest_before") == environment_digest
            and stability.get("environment_digest_after") == environment_digest
        ),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise fine.BenchmarkError(
            "baseline generator manifest is not claimable: " + ", ".join(failed)
        )
    return {
        "schema_version": BASELINE_MANIFEST_SCHEMA,
        "claimable": True,
        "mode": "manifest_bound_label_free_generator",
        "reason": None,
        "baseline": dict(baseline_metadata),
        "manifest_path": str(path),
        "manifest_sha256": _sha256(path),
        "checks": checks,
        "generator": generator,
    }


def _load_decision_inputs(
    decision_inputs_path: Path,
    predictions: Mapping[int, tuple[str, str]],
    official_cleaner: Callable[[str], str],
) -> tuple[tuple[DecisionInput, ...], dict[str, Any]]:
    decisions: list[DecisionInput] = []
    corpus_identity: list[dict[str, Any]] = []
    page_identity: list[dict[str, Any]] = []
    path = decision_inputs_path.resolve()
    if not path.is_file():
        raise fine.BenchmarkError(f"decision-input projection does not exist: {path}")
    with path.open("rb") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise fine.BenchmarkError(
                    f"invalid decision-input row {line_number}"
                ) from error
            if not isinstance(row, dict) or set(row) != {
                "schema_version",
                "dataset_index",
                "track_id",
                "html",
            }:
                raise fine.BenchmarkError(
                    f"decision-input row {line_number} has an invalid closed schema"
                )
            index = row.get("dataset_index")
            track_id = row.get("track_id")
            html = row.get("html")
            if (
                row.get("schema_version") != DECISION_INPUT_SCHEMA
                or type(index) is not int
                or index != len(decisions)
                or type(track_id) is not str
                or not track_id
                or type(html) is not str
            ):
                raise fine.BenchmarkError(
                    f"decision-input row {line_number} is not canonical"
                )
            baseline_row = predictions.get(index)
            if baseline_row is None or baseline_row[0] != track_id:
                raise fine.BenchmarkError(
                    f"decision-input/baseline mismatch at row {index}"
                )
            official_html = official_cleaner(html)
            scrubbed_html, scrub_counts = scrub_annotation_artifacts(official_html)
            decisions.append(
                DecisionInput(
                    dataset_index=index,
                    track_id=track_id,
                    raw_html_digest=_sha256_text(html),
                    official_html=official_html,
                    scrubbed_html=scrubbed_html,
                    scrub_counts=scrub_counts,
                    baseline_prediction=baseline_row[1],
                )
            )
            corpus_identity.append(
                {
                    "dataset_index": index,
                    "track_id": track_id,
                    "html_sha256": _sha256_text(html),
                }
            )
            page_identity.append(
                {
                    "dataset_index": index,
                    "track_id": track_id,
                }
            )
    if len(decisions) != EXPECTED_PAGES:
        raise fine.BenchmarkError("decision projection requires exactly 545 rows")
    metadata = {
        "schema_version": DECISION_INPUT_SCHEMA,
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "records": len(decisions),
        "html_corpus_sha256": _hash_json(corpus_identity),
        "page_id_sha256": _hash_json(page_identity),
        "canonical_order_verified": True,
        "closed_fields": [
            "schema_version",
            "dataset_index",
            "track_id",
            "html",
        ],
        "contains_reference_or_metadata": False,
    }
    return tuple(decisions), metadata


def _load_scoring_records(
    dataset: Path,
    decisions: tuple[DecisionInput, ...],
) -> tuple[tuple[ScoringRecord, ...], dict[str, Any]]:
    dataset_metadata = fine.verify_dataset(dataset)
    scoring: list[ScoringRecord] = []
    records = fine._iter_records(dataset, offset=0, limit=None)  # noqa: SLF001
    for decision, record in zip(decisions, records, strict=True):
        if (
            decision.dataset_index != record.dataset_index
            or decision.track_id != record.track_id
            or decision.raw_html_digest != _sha256_text(record.html)
        ):
            raise fine.BenchmarkError(
                f"decision projection/dataset mismatch at row {record.dataset_index}"
            )
        scoring.append(
            ScoringRecord(
                dataset_index=record.dataset_index,
                track_id=record.track_id,
                reference=record.reference,
                metadata=record.metadata,
            )
        )
    if len(scoring) != EXPECTED_PAGES:
        raise fine.BenchmarkError("scoring pass requires exactly 545 aligned rows")
    return tuple(scoring), dataset_metadata


def _observe_track(
    html: str,
    prediction: str,
    config: AtomicStructureOverlayV0Config,
    *,
    proposer: Callable[..., AtomicStructureOverlayDecisionV0],
    verifier: Callable[..., AtomicStructureOverlayReplayV0],
) -> TrackObservation:
    started = time.perf_counter()
    decision = proposer(
        html,
        prediction,
        config=config,
    )
    replay = verifier(
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
    *,
    proposer: Callable[..., AtomicStructureOverlayDecisionV0],
    verifier: Callable[..., AtomicStructureOverlayReplayV0],
) -> PageObservation:
    # Only transformed source and the fixed baseline prediction cross this
    # boundary. Reference and metadata remain in the caller until scoring.
    official = _observe_track(
        page.official_html,
        page.baseline_prediction,
        config,
        proposer=proposer,
        verifier=verifier,
    )
    scrubbed = _observe_track(
        page.scrubbed_html,
        page.baseline_prediction,
        config,
        proposer=proposer,
        verifier=verifier,
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


def _accepted_patch_topology(
    decision: AtomicStructureOverlayDecisionV0,
) -> tuple[tuple[Any, ...], ...]:
    accepted = [proposal for proposal in decision.proposals if proposal.accepted]
    accepted.sort(
        key=lambda proposal: (
            proposal.candidate_span_start
            if proposal.candidate_span_start is not None
            else -1,
            proposal.candidate_span_end
            if proposal.candidate_span_end is not None
            else -1,
            proposal.atom_kind,
        )
    )
    return tuple(
        (
            proposal.atom_kind,
            proposal.candidate_span_start,
            proposal.candidate_span_end,
            proposal.replacement_digest,
            proposal.patch_digest,
            proposal.visible_token_digest,
            proposal.visible_token_count,
            proposal.replacement_bytes,
        )
        for proposal in accepted
    )


def _parity_key(
    observation: TrackObservation,
) -> tuple[bool, str, tuple[tuple[Any, ...], ...]]:
    return (
        observation.decision.accepted,
        observation.decision.output_markdown,
        _accepted_patch_topology(observation.decision),
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


def _conservative_paired_aggregates(
    baseline_results: Sequence[Mapping[str, fine.OfficialMetricResult]],
    candidate_results: Sequence[Mapping[str, fine.OfficialMetricResult]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    if len(baseline_results) != EXPECTED_PAGES or len(candidate_results) != EXPECTED_PAGES:
        raise fine.BenchmarkError("conservative aggregate requires exactly 545 paired pages")
    baseline_metrics: dict[str, Any] = {}
    candidate_metrics: dict[str, Any] = {}
    mask_comparison: dict[str, Any] = {}
    for name in fine.CORE_METRICS:
        baseline_scores: list[float] = []
        candidate_scores: list[float] = []
        baseline_mask: list[bool] = []
        candidate_mask: list[bool] = []
        for page_index, (baseline_page, candidate_page) in enumerate(
            zip(baseline_results, candidate_results, strict=True)
        ):
            if name not in baseline_page or name not in candidate_page:
                raise fine.BenchmarkError(
                    f"official metric {name} is missing at page {page_index}"
                )
            baseline_result = baseline_page[name]
            candidate_result = candidate_page[name]
            baseline_score = float(baseline_result.score)
            candidate_score = float(candidate_result.score)
            if not math.isfinite(baseline_score) or not math.isfinite(candidate_score):
                raise fine.BenchmarkError(
                    f"official metric {name} is non-finite at page {page_index}"
                )
            baseline_success = bool(baseline_result.success)
            candidate_success = bool(candidate_result.success)
            baseline_mask.append(baseline_success)
            candidate_mask.append(candidate_success)
            baseline_scores.append(baseline_score if baseline_success else 0.0)
            candidate_scores.append(candidate_score if candidate_success else 0.0)

        def payload(scores: list[float], mask: list[bool]) -> dict[str, Any]:
            failed = [index for index, success in enumerate(mask) if not success]
            return {
                "score": sum(scores) / EXPECTED_PAGES,
                "successful_pages": sum(mask),
                "failed_pages": len(failed),
                "failed_dataset_indices": failed,
                "success_mask_sha256": _hash_json(mask),
                "failure_scoring": "zero",
            }

        baseline_metrics[name] = payload(baseline_scores, baseline_mask)
        candidate_metrics[name] = payload(candidate_scores, candidate_mask)
        mismatches = [
            index
            for index, (baseline_success, candidate_success) in enumerate(
                zip(baseline_mask, candidate_mask, strict=True)
            )
            if baseline_success != candidate_success
        ]
        paired_deltas = [
            candidate - baseline
            for baseline, candidate in zip(
                baseline_scores,
                candidate_scores,
                strict=True,
            )
        ]
        mask_comparison[name] = {
            "exact_match": not mismatches,
            "mismatch_dataset_indices": mismatches,
            "mismatch_count": len(mismatches),
            "paired_delta_mean": sum(paired_deltas) / EXPECTED_PAGES,
            "paired_delta_min": min(paired_deltas),
            "paired_delta_max": max(paired_deltas),
        }

    baseline = {
        "overall": sum(
            float(baseline_metrics[name]["score"]) for name in fine.CORE_METRICS
        )
        / len(fine.CORE_METRICS),
        "metrics": baseline_metrics,
        "protocol": "all 545 pages; every failed metric scores zero",
    }
    candidate = {
        "overall": sum(
            float(candidate_metrics[name]["score"]) for name in fine.CORE_METRICS
        )
        / len(fine.CORE_METRICS),
        "metrics": candidate_metrics,
        "protocol": "all 545 pages; every failed metric scores zero",
    }
    return baseline, candidate, _aggregate_delta(baseline, candidate), {
        "all_core_success_masks_exact": all(
            comparison["exact_match"] for comparison in mask_comparison.values()
        ),
        "metrics": mask_comparison,
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
    dict[str, Any],
    dict[str, Any],
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
    official_baseline = fine._official_aggregate(  # noqa: SLF001
        baseline_calculator,
        baseline_results,
    )
    official_candidate = fine._official_aggregate(  # noqa: SLF001
        candidate_calculator,
        candidate_results,
    )
    (
        conservative_baseline,
        conservative_candidate,
        conservative_delta,
        success_masks,
    ) = _conservative_paired_aggregates(baseline_results, candidate_results)
    return (
        baseline_results,
        candidate_results,
        official_baseline,
        official_candidate,
        conservative_baseline,
        conservative_candidate,
        conservative_delta,
        success_masks,
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
    official_baseline: Mapping[str, Any],
    official_candidate: Mapping[str, Any],
    official_delta: Mapping[str, Any],
    conservative_baseline: Mapping[str, Any],
    conservative_candidate: Mapping[str, Any],
    conservative_delta: Mapping[str, Any],
    success_masks: Mapping[str, Any],
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
        "official_aggregate_diagnostic": {
            "baseline": official_baseline,
            "candidate": official_candidate,
            "delta": official_delta,
            "claim_gate": False,
        },
        "conservative_paired_aggregate": {
            "baseline": conservative_baseline,
            "candidate": conservative_candidate,
            "delta": conservative_delta,
            "success_masks": success_masks,
            "claim_gate": True,
        },
        "quality_shared_across_tracks": True,
        "timing": fine._latency_summary(latencies),  # noqa: SLF001
        "decision_set_digest": _hash_json(
            [decision.decision_digest for decision in decisions]
        ),
        "pages_sha256": _sha256(pages_path),
    }


def _quality_gates(
    mode_summaries: Mapping[str, Mapping[str, Any]],
    conservative_delta: Mapping[str, Any],
    success_masks: Mapping[str, Any],
    parity_failures: int,
    decision_wall_seconds: float,
    maximum_decision_wall_seconds: float,
    *,
    baseline_claimable: bool,
    source_clean: bool,
    source_stable: bool,
    dataset_stable: bool,
    evaluator_stable: bool,
    baseline_stable: bool,
    decision_inputs_stable: bool,
) -> dict[str, Any]:
    checks = {
        "exactly_545_pages_each_track": all(
            int(summary["pages"]) == EXPECTED_PAGES
            for summary in mode_summaries.values()
        ),
        "cross_track_acceptance_output_patch_identity_parity": parity_failures == 0,
        "all_core_metric_success_masks_exact": bool(
            success_masks["all_core_success_masks_exact"]
        ),
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
        "baseline_generator_provenance_claimable": baseline_claimable,
        "exact_clean_source_tree": source_clean,
        "source_pre_post_stable": source_stable,
        "dataset_pre_post_stable": dataset_stable,
        "evaluator_pre_post_stable": evaluator_stable,
        "baseline_pre_post_stable": baseline_stable,
        "decision_inputs_pre_post_stable": decision_inputs_stable,
        "overall_delta_at_least_0_01": (
            float(conservative_delta["overall"]) >= QUALITY_THRESHOLDS["overall"]
        ),
        "code_edit_delta_at_least_0_03": (
            float(conservative_delta["metrics"]["code_edit"]["score"])
            >= QUALITY_THRESHOLDS["code_edit"]
        ),
        "table_TEDS_delta_at_least_0_02": (
            float(conservative_delta["metrics"]["table_TEDS"]["score"])
            >= QUALITY_THRESHOLDS["table_TEDS"]
        ),
        "text_edit_non_regression": (
            float(conservative_delta["metrics"]["text_edit"]["score"])
            >= QUALITY_THRESHOLDS["text_edit"]
        ),
        "formula_edit_non_regression": (
            float(conservative_delta["metrics"]["formula_edit"]["score"])
            >= QUALITY_THRESHOLDS["formula_edit"]
        ),
    }
    return {
        "thresholds": QUALITY_THRESHOLDS,
        "quality_protocol": (
            "paired 545-page conservative aggregates; every metric failure scores "
            "zero and baseline/candidate success masks must match exactly"
        ),
        "performance_thresholds": {
            "maximum_dual_track_decision_and_replay_wall_seconds": (
                maximum_decision_wall_seconds
            ),
            "observed_dual_track_decision_and_replay_wall_seconds": (
                decision_wall_seconds
            ),
            "scope": (
                "545 pages, two source tracks, one decision plus one deterministic "
                "same-implementation recomputation per track"
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

    requested_output = args.output_dir.resolve()
    if requested_output == ROOT or ROOT in requested_output.parents:
        raise fine.BenchmarkError(
            "artifact directory must be outside the source repository so provenance "
            "remains clean and stable"
        )
    overlay_module = _load_overlay_module()
    source_before = _source_provenance_snapshot(
        overlay_module=overlay_module,
        allow_dirty=args.allow_dirty_exploratory,
    )
    output = _prepare_output(requested_output)
    dataset = args.dataset.resolve()
    decision_inputs_path = args.decision_inputs.resolve()
    baseline = args.baseline.resolve()
    evaluator = args.evaluator_root.resolve()
    baseline_rows, baseline_metadata = _load_baseline(
        baseline,
        allow_legacy=args.baseline_manifest is None,
    )
    baseline_digest_before = str(baseline_metadata["sha256"])
    evaluator_before = fine.verify_evaluator(evaluator)
    calculator_type, official_cleaner, dependencies = fine.load_official_toolkit(
        evaluator
    )
    decision_inputs, decision_input_metadata = _load_decision_inputs(
        decision_inputs_path,
        baseline_rows,
        official_cleaner,
    )
    baseline_provenance = _load_baseline_provenance(
        args.baseline_manifest,
        baseline_metadata=baseline_metadata,
        decision_inputs_metadata=decision_input_metadata,
        baseline_path=baseline,
        decision_inputs_path=decision_inputs_path,
        current_source=source_before,
        require_claimable=args.require_claimable_baseline,
    )
    config = overlay_module.AtomicStructureOverlayV0Config(enabled=True)

    _atomic_json(
        output / "run_config.json",
        {
            "schema_version": SCHEMA_VERSION,
            "created_at_utc": dt.datetime.now(dt.UTC).isoformat(),
            "dataset": {
                "path": str(dataset),
                "expected_sha256": fine.DATASET_SHA256,
                "expected_bytes": fine.DATASET_BYTES,
                "expected_records": fine.DATASET_RECORDS,
                "opened_before_frozen_decisions": False,
            },
            "decision_inputs": decision_input_metadata,
            "fixed_baseline": {
                "path": str(baseline),
                "sha256": baseline_digest_before,
                "identity": baseline_metadata,
                "provenance": baseline_provenance,
            },
            "evaluator": evaluator_before,
            "dependencies": dependencies,
            "python": sys.version,
            "platform": platform.platform(),
            "overlay_config": asdict(config),
            "protocol": {
                "default_off": True,
                "production_wiring_changed": False,
                "modes": list(MODES),
                "concurrency": args.concurrency,
                "maximum_decision_and_recomputation_wall_seconds": (
                    args.max_decision_wall_seconds
                ),
                "use_llm": False,
                "model_calls": False,
                "paid_calls": False,
                "vendor_outputs_used": False,
                "decision_inputs": (
                    "separate label-free HTML projection, fixed baseline prediction, "
                    "fixed overlay config"
                ),
                "decision_excludes": (
                    "ground truth, metadata, official metrics, vendor outputs, models"
                ),
                "label_dataset_opened": (
                    "only after dual-track decisions, deterministic recomputation "
                    "receipts, integrity checks, and patch-identity parity freeze"
                ),
                "replay_semantics": "deterministic same-implementation recomputation",
                "official_track_annotation_status": (
                    "official cleaner is label-bearing because cc-select may remain"
                ),
                "scrubbed_track_annotation_status": (
                    "full scrubber postcondition removes known annotation signals"
                ),
            },
            "source_provenance": source_before,
        },
    )

    proposer = overlay_module.propose_atomic_structure_overlay_v0
    verifier = overlay_module.verify_atomic_structure_overlay_v0
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        observations = tuple(
            pool.map(
                lambda decision_input: _observe_page(
                    decision_input,
                    config,
                    proposer=proposer,
                    verifier=verifier,
                ),
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
            "official/scrubbed patch-identity parity failed at pages "
            + ",".join(str(index) for index in parity_failures[:20])
        )
    decision_integrity_failures = [
        observation.dataset_index
        for observation in observations
        if (
            not observation.official.replay.verified
            or not observation.scrubbed.replay.verified
            or (
                observation.official.replay.output_markdown
                != observation.official.decision.output_markdown
            )
            or (
                observation.scrubbed.replay.output_markdown
                != observation.scrubbed.decision.output_markdown
            )
            or not observation.official.decision.visible_tokens_identical
            or not observation.scrubbed.decision.visible_tokens_identical
            or (
                not observation.official.decision.accepted
                and observation.official.decision.output_markdown
                != observation.official.decision.candidate_markdown
            )
            or (
                not observation.scrubbed.decision.accepted
                and observation.scrubbed.decision.output_markdown
                != observation.scrubbed.decision.candidate_markdown
            )
        )
    ]
    if decision_integrity_failures:
        raise fine.BenchmarkError(
            "decision/recomputation integrity failed before label access at pages "
            + ",".join(str(index) for index in decision_integrity_failures[:20])
        )

    # First label-bearing dataset access: all decisions and deterministic
    # recomputations are frozen and exact cross-track patch parity has passed.
    scoring_records, dataset_before = _load_scoring_records(dataset, decision_inputs)
    score_started = time.perf_counter()
    (
        baseline_results,
        candidate_results,
        official_baseline,
        official_candidate,
        conservative_baseline,
        conservative_candidate,
        conservative_delta,
        success_masks,
    ) = _score(
        decision_inputs,
        scoring_records,
        observations,
        calculator_type,
        output,
    )
    scoring_seconds = time.perf_counter() - score_started
    official_delta = _aggregate_delta(official_baseline, official_candidate)

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
                        "sha256": _sha256_text(decision_input.baseline_prediction),
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
                        "patch_topology": _accepted_patch_topology(
                            track.decision
                        ),
                        "proposals": _proposal_payload(track.decision),
                    },
                    "deterministic_recomputation": {
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
                    "label_access": (
                        "after_dual_decision_recomputation_integrity_and_patch_parity"
                    ),
                    "baseline_official_metrics": _metric_payload(baseline_metrics),
                    "candidate_official_metrics": _metric_payload(candidate_metrics),
                }
                handle.write(_json_bytes(row))
            handle.flush()
            os.fsync(handle.fileno())

    dataset_after = fine.verify_dataset(dataset)
    evaluator_after = fine.verify_evaluator(evaluator)
    source_after = _source_provenance_snapshot(
        overlay_module=overlay_module,
        allow_dirty=args.allow_dirty_exploratory,
    )
    baseline_digest_after = _sha256(baseline)
    decision_inputs_digest_after = _sha256(decision_inputs_path)
    source_stable = source_before == source_after
    dataset_stable = dataset_before == dataset_after
    evaluator_stable = evaluator_before == evaluator_after
    baseline_stable = baseline_digest_before == baseline_digest_after
    decision_inputs_stable = (
        decision_input_metadata["sha256"] == decision_inputs_digest_after
    )
    if args.baseline_manifest is not None:
        manifest_path = args.baseline_manifest.resolve()
        if _sha256(manifest_path) != baseline_provenance["manifest_sha256"]:
            raise fine.BenchmarkError("baseline provenance manifest changed during audit")
    if not source_stable:
        _assert_snapshot_stable(source_before, source_after, name="source")
    if not dataset_stable:
        raise fine.BenchmarkError("label dataset changed during audit")
    if not evaluator_stable:
        raise fine.BenchmarkError("official evaluator changed during audit")
    if not baseline_stable:
        raise fine.BenchmarkError("fixed baseline changed during audit")
    if not decision_inputs_stable:
        raise fine.BenchmarkError("decision-input projection changed during audit")

    mode_summaries = {
        mode: _track_summary(
            mode,
            observations,
            official_baseline,
            official_candidate,
            official_delta,
            conservative_baseline,
            conservative_candidate,
            conservative_delta,
            success_masks,
            output / mode / "pages.jsonl",
        )
        for mode in MODES
    }
    gates = _quality_gates(
        mode_summaries,
        conservative_delta,
        success_masks,
        len(parity_failures),
        decision_seconds,
        args.max_decision_wall_seconds,
        baseline_claimable=bool(baseline_provenance["claimable"]),
        source_clean=bool(source_before["claimable_clean_tree"]),
        source_stable=source_stable,
        dataset_stable=dataset_stable,
        evaluator_stable=evaluator_stable,
        baseline_stable=baseline_stable,
        decision_inputs_stable=decision_inputs_stable,
    )
    for mode, mode_summary in mode_summaries.items():
        _atomic_json(output / mode / "summary.json", mode_summary)

    nonclaimable_checks = {
        name: passed
        for name, passed in gates["checks"].items()
        if name
        not in {
            "baseline_generator_provenance_claimable",
            "exact_clean_source_tree",
        }
    }
    summary = {
        "schema_version": SCHEMA_VERSION,
        "completed_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "claimable_sota_or_vendor_evidence": False,
        "production_wiring_changed": False,
        "go_for_545_shadow": gates["passed"],
        "exploratory_shadow_checks_passed": all(nonclaimable_checks.values()),
        "go_for_production": False,
        "decision_used_labels_or_metrics": False,
        "official_source_track_is_label_free": False,
        "dataset": {
            "before": dataset_before,
            "after": dataset_after,
            "stable": dataset_stable,
            "first_opened_after_decisions_frozen": True,
        },
        "decision_inputs": {
            **decision_input_metadata,
            "sha256_after": decision_inputs_digest_after,
            "stable": decision_inputs_stable,
        },
        "evaluator": {
            "before": evaluator_before,
            "after": evaluator_after,
            "stable": evaluator_stable,
        },
        "fixed_baseline": {
            "path": str(baseline),
            "sha256_before": baseline_digest_before,
            "sha256_after": baseline_digest_after,
            "stable": baseline_stable,
            "identity": baseline_metadata,
            "provenance": baseline_provenance,
        },
        "source": {
            "before": source_before,
            "after": source_after,
            "stable": source_stable,
        },
        "cross_track": {
            "parity_definition": (
                "accepted flag, exact output bytes, candidate spans, atom kinds, "
                "replacement/patch/visible-token digests, token counts, and "
                "replacement byte topology"
            ),
            "parity_failures": parity_failures,
            "quality_scored_once_after_exact_patch_parity": True,
        },
        "modes": mode_summaries,
        "claim_quality": {
            "official_aggregate_is_diagnostic_only": True,
            "conservative_paired_baseline": conservative_baseline,
            "conservative_paired_candidate": conservative_candidate,
            "conservative_paired_delta": conservative_delta,
            "success_masks": success_masks,
        },
        "quality_gates": gates,
        "timing": {
            "decision_and_recomputation_wall_seconds": decision_seconds,
            "official_scoring_wall_seconds": scoring_seconds,
            "total_wall_seconds": decision_seconds + scoring_seconds,
        },
        "limitations": [
            "development-only unwired shadow component",
            (
                "opaque fixed baseline; exploratory only"
                if not baseline_provenance["claimable"]
                else "manifest-bound fixed baseline generator provenance"
            ),
            "public benchmark labels score frozen decisions",
            "not an independent blind holdout",
            "replay is deterministic same-implementation recomputation",
            "no SOTA or vendor-comparison claim is supported",
        ],
    }
    if not all(
        math.isfinite(float(conservative_delta["metrics"][name]["score"]))
        for name in fine.CORE_METRICS
    ):
        raise fine.BenchmarkError("quality delta is non-finite")
    _atomic_json(output / "summary.json", summary)
    _atomic_json(output / "manifest.json", _artifact_manifest(output))
    return summary


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument(
        "--decision-inputs",
        required=True,
        type=Path,
        help=(
            "Closed-schema label-free HTML projection created in a separate, "
            "completed process"
        ),
    )
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument(
        "--baseline-manifest",
        type=Path,
        help=(
            "Generator provenance manifest; omitting it makes the run exploratory "
            "and forces the claim gate closed"
        ),
    )
    parser.add_argument(
        "--require-claimable-baseline",
        action="store_true",
        help="Fail before decisions unless a valid baseline generator manifest exists",
    )
    parser.add_argument(
        "--allow-dirty-exploratory",
        action="store_true",
        help=(
            "Allow a dirty source tree for diagnostics; the clean-source claim "
            "gate still fails"
        ),
    )
    parser.add_argument("--evaluator-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--max-decision-wall-seconds",
        type=float,
        default=180.0,
        help=(
            "Dedicated-host wall budget for 545 pages, two tracks, and "
            "decision plus deterministic same-implementation recomputation"
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
    delta = summary["claim_quality"]["conservative_paired_delta"]
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

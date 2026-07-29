#!/usr/bin/env python3
"""Generate a deterministic label-free baseline from an HTML-only projection.

The generator never accepts a benchmark dataset or evaluator path. Its only
corpus input is the closed-schema decision projection. A clean source tree,
loaded native extension, fixed extractor configuration, output rows, and
pre/post stability are bound into an adjacent provenance manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import platform
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench import webmainbench_finegrained_benchmark as fine  # noqa: E402
from bench.source_provenance import (  # noqa: E402
    SourceInventoryError,
    verify_loaded_native_source_binding,
)
from bench.webmainbench_benchmark import scrub_annotation_artifacts  # noqa: E402

DECISION_INPUT_SCHEMA = "webmainbench.atomic-structure-overlay-v0-decision-inputs.1"
BASELINE_PAGE_SCHEMA = "clusy.fixed-baseline-page.2"
BASELINE_MANIFEST_SCHEMA = "clusy.fixed-baseline-provenance.2"
DEFAULT_EXPECTED_RECORDS = 545
EXTRACTION_PROFILE = "balanced"
_GIT_OBJECT_RE = re.compile(r"[0-9a-f]{40}")
_FIXED_ENVIRONMENT = {
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
_CREDENTIAL_ENVIRONMENT_NAMES = (
    "ANTHROPIC_API_KEY",
    "ELSEVIER_API_KEY",
    "EXA_API_KEY",
    "FIRECRAWL_API_KEY",
    "IEEE_API_KEY",
    "OPENAI_API_KEY",
    "QUALITY_EXTRACTION_API_KEY",
    "QUALITY_EXTRACTION_BASE_URL",
    "QUALITY_EXTRACTION_MODEL",
)
_SOURCE_FIXED_FILES = (
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
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hash_json(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _run_readonly(command: Sequence[str]) -> str:
    result = subprocess.run(
        list(command),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise fine.BenchmarkError(
            f"source provenance command failed: {' '.join(command)}: "
            f"{detail or 'no detail'}"
        )
    return result.stdout.strip()


def _module_identity(module: ModuleType, expected: Path | None = None) -> dict[str, Any]:
    raw_path = getattr(module, "__file__", None)
    if not isinstance(raw_path, str):
        raise fine.BenchmarkError(f"loaded module has no origin: {module.__name__}")
    path = Path(raw_path).resolve()
    if not path.is_file():
        raise fine.BenchmarkError(f"loaded module origin is not a file: {path}")
    if expected is not None and path != expected.resolve():
        raise fine.BenchmarkError(
            f"loaded module escaped exact generator checkout: {module.__name__}"
        )
    return {
        "module": module.__name__,
        "path": str(path),
        "sha256": _sha256(path),
    }


def _source_snapshot() -> dict[str, Any]:
    try:
        shared = fine.source_provenance()
        native_binding = verify_loaded_native_source_binding(ROOT)
    except (fine.BenchmarkError, SourceInventoryError) as error:
        raise fine.BenchmarkError(f"generator source provenance failed: {error}") from error
    commit = _run_readonly(["git", "rev-parse", "HEAD"])
    tree = _run_readonly(["git", "rev-parse", "HEAD^{tree}"])
    status_output = _run_readonly(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"]
    )
    status = status_output.splitlines() if status_output else []
    if (
        not _GIT_OBJECT_RE.fullmatch(commit)
        or not _GIT_OBJECT_RE.fullmatch(tree)
        or status
    ):
        raise fine.BenchmarkError(
            "baseline generation requires a canonical, exact clean Git checkout"
        )
    if (
        shared.get("git_commit") != commit
        or shared.get("git_dirty") is not False
        or native_binding.get("matched") is not True
    ):
        raise fine.BenchmarkError("shared and strict generator provenance disagree")
    extractor_module = importlib.import_module("app.services.extractor")
    clusy_native = importlib.import_module("clusy_native")
    native_extension = importlib.import_module("clusy_native._native")
    fixed_hashes = {
        relative: _sha256(ROOT / relative) for relative in _SOURCE_FIXED_FILES
    }
    package_identity = _module_identity(clusy_native)
    extension_identity = _module_identity(native_extension)
    if (
        ROOT not in Path(package_identity["path"]).parents
        or package_identity["sha256"]
        != fixed_hashes["native/python/clusy_native/__init__.py"]
        or ROOT not in Path(extension_identity["path"]).parents
    ):
        raise fine.BenchmarkError(
            "loaded native package or extension escaped exact source binding"
        )
    snapshot = {
        "schema_version": "clusy.fixed-baseline-generator-source.1",
        "source_root": str(ROOT),
        "git_commit": commit,
        "git_tree": tree,
        "git_clean": True,
        "file_sha256": shared.get("file_sha256"),
        "source_digest": shared.get("source_digest"),
        "fixed_file_sha256": fixed_hashes,
        "lock_sha256": {
            "uv.lock": fixed_hashes["uv.lock"],
            "native/Cargo.lock": fixed_hashes["native/Cargo.lock"],
        },
        "native_source_binding": native_binding,
        "loaded_modules": {
            "extractor": _module_identity(
                extractor_module,
                ROOT / "app/services/extractor.py",
            ),
            "clusy_native_package": package_identity,
            "clusy_native_extension": extension_identity,
        },
    }
    snapshot["snapshot_digest"] = _hash_json(snapshot)
    return snapshot


def _configure_fixed_environment() -> None:
    active = [
        name
        for name in _CREDENTIAL_ENVIRONMENT_NAMES
        if os.environ.get(name, "").strip()
    ]
    if active:
        raise fine.BenchmarkError(
            "model or vendor credential paths must be inactive during baseline "
            "generation: " + ", ".join(active)
        )
    for name, value in _FIXED_ENVIRONMENT.items():
        os.environ[name] = value


def _effective_config() -> dict[str, Any]:
    from app.config import settings

    if (
        settings.quality_backend_configured()
        or settings.anthropic_api_key
        or settings.elsevier_api_key
        or settings.ieee_api_key
    ):
        raise fine.BenchmarkError(
            "model or vendor credential path is active in effective settings"
        )
    return {
        "entrypoint": "app.services.extractor.extract_content",
        "extraction_profile": EXTRACTION_PROFILE,
        "url": "",
        "prediction_transform": "identity ExtractionResult.text",
        "input_transform": (
            "bench.webmainbench_benchmark.scrub_annotation_artifacts"
        ),
        "annotation_scrubber_postcondition": True,
        "concurrency": 1,
        "native_extraction_enabled": settings.native_extraction_enabled,
        "native_extraction_min_confidence": settings.native_extraction_min_confidence,
        "extract_max_text_length": settings.extract_max_text_length,
        "parallel_extraction_enabled": settings.parallel_extraction_enabled,
        "extraction_merge_mode": settings.extraction_merge_mode,
        "max_concurrent_extractions": settings.max_concurrent_extractions,
        "network_calls": False,
        "model_calls": False,
        "vendor_outputs_used": False,
    }


def _environment_identity() -> dict[str, Any]:
    return {
        "python": sys.version,
        "python_implementation": platform.python_implementation(),
        "python_executable": str(Path(sys.executable).resolve()),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "fixed_environment": dict(sorted(_FIXED_ENVIRONMENT.items())),
        "credential_guard": {
            "checked_names": list(_CREDENTIAL_ENVIRONMENT_NAMES),
            "active_names": [],
        },
        "uv_lock_sha256": _sha256(ROOT / "uv.lock"),
        "cargo_lock_sha256": _sha256(ROOT / "native/Cargo.lock"),
    }


def _load_projection(
    path: Path,
    *,
    expected_records: int,
) -> tuple[tuple[tuple[int, str, str], ...], dict[str, Any]]:
    resolved = path.resolve()
    if not resolved.is_file():
        raise fine.BenchmarkError(f"decision projection does not exist: {resolved}")
    rows: list[tuple[int, str, str]] = []
    corpus_identity: list[dict[str, Any]] = []
    page_identity: list[dict[str, Any]] = []
    with resolved.open("rb") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise fine.BenchmarkError(
                    f"invalid decision projection row {line_number}"
                ) from error
            if not isinstance(row, dict) or set(row) != {
                "schema_version",
                "dataset_index",
                "track_id",
                "html",
            }:
                raise fine.BenchmarkError(
                    f"decision projection row {line_number} has an invalid schema"
                )
            index = row.get("dataset_index")
            track_id = row.get("track_id")
            html = row.get("html")
            if (
                row.get("schema_version") != DECISION_INPUT_SCHEMA
                or type(index) is not int
                or index != len(rows)
                or type(track_id) is not str
                or not track_id
                or type(html) is not str
            ):
                raise fine.BenchmarkError(
                    f"decision projection row {line_number} is not canonical"
                )
            try:
                html_digest = _sha256_text(html)
            except UnicodeEncodeError as error:
                raise fine.BenchmarkError(
                    f"decision projection row {line_number} contains invalid Unicode"
                ) from error
            rows.append((index, track_id, html))
            corpus_identity.append(
                {
                    "dataset_index": index,
                    "track_id": track_id,
                    "html_sha256": html_digest,
                }
            )
            page_identity.append(
                {
                    "dataset_index": index,
                    "track_id": track_id,
                }
            )
    if len(rows) != expected_records:
        raise fine.BenchmarkError(
            f"decision projection requires exactly {expected_records} records"
        )
    metadata = {
        "schema_version": DECISION_INPUT_SCHEMA,
        "sha256": _sha256(resolved),
        "bytes": resolved.stat().st_size,
        "records": len(rows),
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
    return tuple(rows), metadata


def _strategy_name(result: object) -> str:
    strategy = getattr(result, "strategy", "")
    return str(getattr(strategy, "value", strategy) or "<empty>")


def generate_baseline(
    decision_inputs: Path,
    output: Path,
    manifest: Path,
    *,
    expected_records: int,
    extractor: Callable[..., object],
    source_snapshotter: Callable[[], Mapping[str, Any]],
    config: Mapping[str, Any],
    environment: Mapping[str, Any],
) -> dict[str, Any]:
    if expected_records <= 0:
        raise fine.BenchmarkError("expected records must be positive")
    output = output.resolve()
    manifest = manifest.resolve()
    if output == manifest:
        raise fine.BenchmarkError("baseline output and manifest must differ")
    if output.exists() or manifest.exists():
        raise fine.BenchmarkError("baseline output and manifest must not already exist")
    if output == ROOT or ROOT in output.parents or manifest == ROOT or ROOT in manifest.parents:
        raise fine.BenchmarkError("baseline artifacts must be outside the source checkout")
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)

    rows, projection_before = _load_projection(
        decision_inputs,
        expected_records=expected_records,
    )
    source_before = dict(source_snapshotter())
    config_payload = dict(config)
    environment_payload = dict(environment)
    config_digest_before = _hash_json(config_payload)
    environment_digest_before = _hash_json(environment_payload)
    temporary = output.with_name(f".{output.name}.tmp-{os.getpid()}")
    status_records: list[dict[str, Any]] = []
    failures: Counter[str] = Counter()
    successful_records = 0
    try:
        with temporary.open("xb") as handle:
            for index, track_id, html in rows:
                prediction = ""
                strategy = "<error>"
                error_type: str | None = None
                try:
                    extraction_html, _ = scrub_annotation_artifacts(html)
                    result = extractor(
                        extraction_html,
                        "",
                        extraction_profile=EXTRACTION_PROFILE,
                    )
                    prediction_value = getattr(result, "text", None)
                    if not isinstance(prediction_value, str):
                        raise TypeError("extractor returned non-string text")
                    prediction_value.encode("utf-8")
                    prediction = prediction_value
                    strategy = _strategy_name(result)
                    successful_records += 1
                except Exception as error:
                    error_type = type(error).__name__
                    failures[error_type] += 1
                status = {
                    "success": error_type is None,
                    "strategy": strategy,
                    "error_type": error_type,
                }
                page = {
                    "schema_version": BASELINE_PAGE_SCHEMA,
                    "dataset_index": index,
                    "track_id": track_id,
                    "prediction": prediction,
                    "generation": status,
                }
                handle.write(_json_bytes(page))
                status_records.append(
                    {
                        "dataset_index": index,
                        "track_id": track_id,
                        "prediction_sha256": _sha256_text(prediction),
                        **status,
                    }
                )
            handle.flush()
            os.fsync(handle.fileno())

        projection_after_rows, projection_after = _load_projection(
            decision_inputs,
            expected_records=expected_records,
        )
        source_after = dict(source_snapshotter())
        config_digest_after = _hash_json(config)
        environment_digest_after = _hash_json(environment)
        stability = {
            "source_snapshot_digest_before": source_before.get("snapshot_digest"),
            "source_snapshot_digest_after": source_after.get("snapshot_digest"),
            "source_stable": source_before == source_after,
            "decision_inputs_sha256_before": projection_before["sha256"],
            "decision_inputs_sha256_after": projection_after["sha256"],
            "decision_inputs_stable": (
                rows == projection_after_rows
                and projection_before == projection_after
            ),
            "config_digest_before": config_digest_before,
            "config_digest_after": config_digest_after,
            "config_stable": config_digest_before == config_digest_after,
            "environment_digest_before": environment_digest_before,
            "environment_digest_after": environment_digest_after,
            "environment_stable": (
                environment_digest_before == environment_digest_after
            ),
        }
        if not all(
            stability[name]
            for name in (
                "source_stable",
                "decision_inputs_stable",
                "config_stable",
                "environment_stable",
            )
        ):
            raise fine.BenchmarkError(
                "source, projection, config, or environment changed during generation"
            )

        baseline_metadata = {
            "schema_version": BASELINE_PAGE_SCHEMA,
            "sha256": _sha256(temporary),
            "bytes": temporary.stat().st_size,
            "records": len(status_records),
            "successful_records": successful_records,
            "failed_records": len(status_records) - successful_records,
            "failed_dataset_indices": [
                record["dataset_index"]
                for record in status_records
                if not record["success"]
            ],
            "failure_types": dict(sorted(failures.items())),
            "record_status_sha256": _hash_json(status_records),
        }
        document = {
            "schema_version": BASELINE_MANIFEST_SCHEMA,
            "baseline": baseline_metadata,
            "decision_inputs": projection_before,
            "generator": {
                "entrypoint": "app.services.extractor.extract_content",
                "prediction_field": "prediction",
                "reference_labels_used": False,
                "benchmark_metadata_used": False,
                "official_metrics_used": False,
                "vendor_outputs_used": False,
                "config": config_payload,
                "environment": environment_payload,
                "source_provenance": source_before,
                "cli_args": {
                    "decision_inputs": str(decision_inputs.resolve()),
                    "output": str(output),
                    "manifest": str(manifest),
                    "expected_records": expected_records,
                },
            },
            "stability": stability,
        }
        os.replace(temporary, output)
        manifest_temporary = manifest.with_name(f".{manifest.name}.tmp-{os.getpid()}")
        try:
            with manifest_temporary.open("xb") as handle:
                handle.write(_json_bytes(document))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(manifest_temporary, manifest)
        finally:
            if manifest_temporary.exists():
                manifest_temporary.unlink()
        return document
    finally:
        if temporary.exists():
            temporary.unlink()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decision-inputs", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument(
        "--expected-records",
        type=int,
        default=DEFAULT_EXPECTED_RECORDS,
        help="Expected closed-schema projection rows; claimable audit requires 545",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        _configure_fixed_environment()
        from app.services.extractor import extract_content

        config = _effective_config()
        environment = _environment_identity()
        document = generate_baseline(
            args.decision_inputs,
            args.output,
            args.manifest,
            expected_records=args.expected_records,
            extractor=extract_content,
            source_snapshotter=_source_snapshot,
            config=config,
            environment=environment,
        )
    except fine.BenchmarkError as error:
        print(f"baseline generation error: {error}", file=sys.stderr)
        return 2
    baseline = document["baseline"]
    print(
        f"generated {baseline['records']} label-free baseline rows; "
        f"success={baseline['successful_records']} "
        f"failed={baseline['failed_records']}"
    )
    print(f"baseline: {args.output.resolve()}")
    print(f"manifest: {args.manifest.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

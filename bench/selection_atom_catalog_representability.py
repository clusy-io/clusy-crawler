#!/usr/bin/env python3
"""Measure the opt-in selection-atom catalog on a frozen HTML-only JSONL."""

from __future__ import annotations

import argparse
import datetime as dt
import gc
import hashlib
import json
import math
import os
import platform
import resource
import sys
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import clusy_native  # noqa: E402
import clusy_native.selection_atom_catalog_v1 as catalog_module  # noqa: E402
from clusy_native import (  # noqa: E402
    SelectionAtomCatalogV1Config,
    build_selection_atom_catalog_v1,
)

import bench.source_provenance as provenance_module  # noqa: E402
from bench.source_provenance import verify_loaded_native_source_binding  # noqa: E402

EXPECTED_SHA256 = "e5958b541d844cf011e66e214bf64abb742aec6922e3c32321e2abaf7cf2c735"
EXPECTED_ROWS = 545
EXPECTED_KEYS = {"html", "html_sha256", "row", "track_id", "url"}
SOURCE_PATHS = (
    "bench/selection_atom_catalog_representability.py",
    "bench/source_provenance.py",
    "native/src/document_ir_v2/source_text_mapper_v2.rs",
    "native/src/document_ir_v2/selection_certificate_v0.rs",
    "native/Cargo.lock",
)
REQUIRED_EXECUTED_PYTHON_MODULES = frozenset(
    {
        "clusy_native",
        "clusy_native.document_ir_v2",
        "clusy_native.selection_atom_catalog_v1",
        "clusy_native.source_text_mapper_v2",
    }
)

CONFIG = SelectionAtomCatalogV1Config(
    enabled=True,
    max_source_bytes=4 * 1024 * 1024,
    max_atoms=65_536,
    max_total_atom_source_bytes=4 * 1024 * 1024,
    max_ancestry_steps=2_000_000,
    max_identifier_chars=1_024,
)


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def peak_rss_bytes() -> int:
    maximum = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return maximum if sys.platform == "darwin" else maximum * 1024


def nearest_rank(values: list[int], quantile: float) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def canonical_digest(rows: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(
            json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _module_repository_relative(module_name: str) -> Path:
    if module_name == "clusy_native":
        return Path("__init__.py")
    parts = module_name.split(".")
    if len(parts) < 2 or parts[0] != "clusy_native" or any(not part for part in parts[1:]):
        raise RuntimeError(f"unmapped clusy_native module name: {module_name}")
    return Path(*parts[1:]).with_suffix(".py")


def verify_executed_python_module_inventory(
    modules: Mapping[str, Any],
    *,
    installed_package_root: Path,
    repository_package_root: Path,
    required_modules: frozenset[str],
) -> dict[str, dict[str, int | str | bool]]:
    """Bind every loaded repository-owned ``clusy_native`` Python module."""

    loaded_names = {
        name for name in modules if name == "clusy_native" or name.startswith("clusy_native.")
    }
    missing = sorted(required_modules - loaded_names)
    if missing:
        raise RuntimeError(f"required clusy_native modules are not loaded: {missing}")
    if "clusy_native._native" not in loaded_names:
        raise RuntimeError("clusy_native._native is not loaded")

    installed_root = installed_package_root.resolve(strict=True)
    repository_root = repository_package_root.resolve(strict=True)
    inventory: dict[str, dict[str, int | str | bool]] = {}
    for module_name in sorted(loaded_names):
        if module_name == "clusy_native._native":
            continue
        module = modules[module_name]
        module_path_value = getattr(module, "__file__", None)
        if type(module_path_value) is not str:
            raise RuntimeError(f"{module_name} has no filesystem identity")
        module_path = Path(module_path_value).resolve(strict=True)
        try:
            installed_relative = module_path.relative_to(installed_root)
        except ValueError as error:
            raise RuntimeError(
                f"loaded module is outside the installed clusy_native package: {module_name}"
            ) from error
        expected_relative = _module_repository_relative(module_name)
        if installed_relative != expected_relative:
            raise RuntimeError(
                f"loaded module path does not match its name: {module_name}: "
                f"{installed_relative.as_posix()}"
            )
        repository_candidate = repository_root / expected_relative
        if not repository_candidate.is_file():
            raise RuntimeError(f"loaded module has no repository source: {module_name}")
        repository_path = repository_candidate.resolve(strict=True)
        try:
            repository_path.relative_to(repository_root)
        except ValueError as error:
            raise RuntimeError(
                f"repository module escaped the clusy_native package: {module_name}"
            ) from error
        installed_digest, installed_bytes = sha256_file(module_path)
        repository_digest, repository_bytes = sha256_file(repository_path)
        if (installed_digest, installed_bytes) != (repository_digest, repository_bytes):
            raise RuntimeError(
                f"executed Python module differs from repository source: {module_name}"
            )
        inventory[module_name] = {
            "path": str(module_path),
            "installed_relative": installed_relative.as_posix(),
            "repository_relative": (
                Path("native/python/clusy_native") / expected_relative
            ).as_posix(),
            "bytes": installed_bytes,
            "sha256": installed_digest,
            "repository_bytes_match": True,
        }
    if set(inventory) != loaded_names - {"clusy_native._native"}:
        raise RuntimeError("executed clusy_native Python module inventory is incomplete")
    return inventory


def _verified_repository_execution_modules() -> dict[str, dict[str, int | str | bool]]:
    modules = {
        "benchmark_runner": (
            Path(__file__),
            Path("bench/selection_atom_catalog_representability.py"),
        ),
        "provenance_helper": (
            Path(provenance_module.__file__),
            Path("bench/source_provenance.py"),
        ),
    }
    inventory: dict[str, dict[str, int | str | bool]] = {}
    for name, (executed_path, relative) in modules.items():
        resolved_executed = executed_path.resolve(strict=True)
        resolved_repository = (ROOT / relative).resolve(strict=True)
        if resolved_executed != resolved_repository:
            raise RuntimeError(
                f"executed repository module path mismatch: {name}: {resolved_executed}"
            )
        digest, size = sha256_file(resolved_executed)
        inventory[name] = {
            "path": str(resolved_executed),
            "repository_relative": relative.as_posix(),
            "bytes": size,
            "sha256": digest,
            "repository_bytes_match": True,
        }
    return inventory


def source_identity() -> dict[str, Any]:
    if build_selection_atom_catalog_v1 is not catalog_module.build_selection_atom_catalog_v1:
        raise RuntimeError("benchmark callable is not the catalog module entrypoint")
    native_module = clusy_native._native
    extension_path_value = getattr(native_module, "__file__", None)
    if type(extension_path_value) is not str:
        raise RuntimeError("clusy_native._native has no filesystem identity")
    extension_path = Path(extension_path_value).resolve()
    files: dict[str, dict[str, int | str]] = {}
    for relative in SOURCE_PATHS:
        digest, size = sha256_file(ROOT / relative)
        files[relative] = {"bytes": size, "sha256": digest}
    python_package_path = Path(clusy_native.__file__).resolve(strict=True)
    executed_python_modules = verify_executed_python_module_inventory(
        sys.modules,
        installed_package_root=python_package_path.parent,
        repository_package_root=ROOT / "native" / "python" / "clusy_native",
        required_modules=REQUIRED_EXECUTED_PYTHON_MODULES,
    )
    for module in executed_python_modules.values():
        relative = str(module["repository_relative"])
        files[relative] = {
            "bytes": int(module["bytes"]),
            "sha256": str(module["sha256"]),
        }
    native_source_binding = verify_loaded_native_source_binding(ROOT)
    extension_digest, extension_bytes = sha256_file(extension_path)
    return {
        "python_package_path": str(python_package_path),
        "executed_python_modules": executed_python_modules,
        "executed_repository_modules": _verified_repository_execution_modules(),
        "benchmark_callable": {
            "module": catalog_module.__name__,
            "qualname": catalog_module.build_selection_atom_catalog_v1.__qualname__,
            "package_reexport_is_module_entrypoint": True,
        },
        "native_extension_path": str(extension_path),
        "native_extension_bytes": extension_bytes,
        "native_extension_sha256": extension_digest,
        "native_source_binding": native_source_binding,
        "relevant_files": files,
    }


def physical_memory_bytes() -> int | None:
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        page_count = os.sysconf("SC_PHYS_PAGES")
    except (OSError, ValueError):
        return None
    if type(page_size) is not int or type(page_count) is not int:
        return None
    return page_size * page_count


def environment_identity() -> dict[str, Any]:
    uname = platform.uname()
    return {
        "python": sys.version,
        "python_executable": sys.executable,
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "system": uname.system,
        "release": uname.release,
        "version": uname.version,
        "machine": uname.machine,
        "processor": uname.processor or None,
        "logical_cpu_count": os.cpu_count(),
        "physical_memory_bytes": physical_memory_bytes(),
    }


def parse_row(line: str, line_number: int) -> tuple[int, str, int]:
    value = json.loads(line)
    if type(value) is not dict or set(value) != EXPECTED_KEYS:
        raise RuntimeError(f"line {line_number}: unexpected input schema")
    row = value["row"]
    html = value["html"]
    declared_digest = value["html_sha256"]
    if type(row) is not int or type(html) is not str or type(declared_digest) is not str:
        raise RuntimeError(f"line {line_number}: invalid row types")
    html_bytes = html.encode("utf-8")
    if hashlib.sha256(html_bytes).hexdigest() != declared_digest:
        raise RuntimeError(f"line {line_number}: HTML digest mismatch")
    return row, html, len(html_bytes)


def first_row(path: Path) -> tuple[int, str, int]:
    with path.open(encoding="utf-8") as stream:
        line = stream.readline()
    if not line:
        raise RuntimeError("dataset is empty")
    return parse_row(line, 1)


def cold_probe(path: Path) -> dict[str, Any]:
    row, html, html_bytes = first_row(path)
    started = time.perf_counter_ns()
    result = build_selection_atom_catalog_v1(html, config=CONFIG)
    elapsed_ns = time.perf_counter_ns() - started
    return {
        "row": row,
        "source_html_bytes": html_bytes,
        "latency_ms": elapsed_ns / 1_000_000,
        "accepted": result.accepted,
        "reason": result.reason,
        "atom_count": result.atom_count,
        "kind_counts": dict(result.kind_counts),
        "transformed_span_count": result.source_text_map_transformed_span_count,
        "catalog_digest": result.catalog_digest,
    }


def warm_up(path: Path, pages: int) -> None:
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            _, html, _ = parse_row(line, line_number)
            build_selection_atom_catalog_v1(html, config=CONFIG)
            if line_number >= pages:
                return
    raise RuntimeError("warm-up input ended early")


def measure_run(path: Path, run_index: int) -> dict[str, Any]:
    latencies_ns: list[int] = []
    reason_counts: Counter[str] = Counter()
    kind_counts: Counter[str] = Counter()
    result_rows: list[dict[str, Any]] = []
    total_html_bytes = 0
    total_atoms = 0
    transformed_spans = 0
    started_wall = time.perf_counter_ns()
    usage_before = resource.getrusage(resource.RUSAGE_SELF)

    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            row, html, html_bytes = parse_row(line, line_number)
            started = time.perf_counter_ns()
            result = build_selection_atom_catalog_v1(html, config=CONFIG)
            elapsed = time.perf_counter_ns() - started

            latencies_ns.append(elapsed)
            total_html_bytes += html_bytes
            reason_counts[result.reason] += 1
            total_atoms += result.atom_count
            transformed_spans += result.source_text_map_transformed_span_count
            per_page_kinds = dict(result.kind_counts)
            kind_counts.update(per_page_kinds)
            result_rows.append(
                {
                    "row": row,
                    "accepted": result.accepted,
                    "reason": result.reason,
                    "catalog_digest": result.catalog_digest,
                    "atom_count": result.atom_count,
                    "kind_counts": per_page_kinds,
                    "transformed_span_count": result.source_text_map_transformed_span_count,
                    "mapping_contract": result.text_mapping_contract,
                }
            )

    usage_after = resource.getrusage(resource.RUSAGE_SELF)
    loop_wall_ns = time.perf_counter_ns() - started_wall
    if len(latencies_ns) != EXPECTED_ROWS:
        raise RuntimeError(f"run {run_index}: expected {EXPECTED_ROWS} rows")
    timed_ns = sum(latencies_ns)
    return {
        "run": run_index,
        "pages": len(latencies_ns),
        "source_html_bytes": total_html_bytes,
        "reason_counts": dict(sorted(reason_counts.items())),
        "accepted": reason_counts["accepted"],
        "atom_count": total_atoms,
        "kind_counts": dict(sorted(kind_counts.items())),
        "transformed_span_count": transformed_spans,
        "output_commitment_sha256": canonical_digest(result_rows),
        "timing": {
            "catalog_call_seconds": timed_ns / 1_000_000_000,
            "loop_wall_seconds_including_jsonl_read_and_validation": (loop_wall_ns / 1_000_000_000),
            "pages_per_catalog_call_second": (len(latencies_ns) * 1_000_000_000 / timed_ns),
            "source_html_bytes_per_catalog_call_second": (
                total_html_bytes * 1_000_000_000 / timed_ns
            ),
            "per_page_ms": {
                "p50_nearest_rank": nearest_rank(latencies_ns, 0.50) / 1_000_000,
                "p95_nearest_rank": nearest_rank(latencies_ns, 0.95) / 1_000_000,
                "max": max(latencies_ns) / 1_000_000,
            },
        },
        "resources": {
            "peak_rss_bytes_after_run": peak_rss_bytes(),
            "user_cpu_seconds_delta": usage_after.ru_utime - usage_before.ru_utime,
            "system_cpu_seconds_delta": usage_after.ru_stime - usage_before.ru_stime,
        },
        "_latencies_ns": latencies_ns,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--warmup-pages", type=int, default=16)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.runs <= 0 or args.warmup_pages <= 0:
        parser.error("runs and warmup-pages must be positive")

    dataset_sha256, dataset_bytes = sha256_file(args.dataset)
    if dataset_sha256 != EXPECTED_SHA256:
        raise RuntimeError(f"unexpected dataset SHA-256: {dataset_sha256}")

    recorded_start = utc_now()
    environment = environment_identity()
    measured_source_before = source_identity()
    peak_after_import_and_input_hash = peak_rss_bytes()
    first_cold_probe = cold_probe(args.dataset)
    peak_after_cold_probe = peak_rss_bytes()
    warm_up(args.dataset, args.warmup_pages)
    gc.collect()
    peak_after_warmup = peak_rss_bytes()

    runs = []
    all_latencies: list[int] = []
    for run_index in range(1, args.runs + 1):
        run = measure_run(args.dataset, run_index)
        all_latencies.extend(run.pop("_latencies_ns"))
        runs.append(run)
        gc.collect()

    first = runs[0]
    stable_fields = (
        "pages",
        "source_html_bytes",
        "reason_counts",
        "accepted",
        "atom_count",
        "kind_counts",
        "transformed_span_count",
        "output_commitment_sha256",
    )
    if any(any(run[field] != first[field] for field in stable_fields) for run in runs[1:]):
        raise RuntimeError("measured runs did not produce identical aggregate outputs")
    dataset_sha256_after, dataset_bytes_after = sha256_file(args.dataset)
    if (dataset_sha256_after, dataset_bytes_after) != (dataset_sha256, dataset_bytes):
        raise RuntimeError("dataset identity changed during measurement")
    measured_source_after = source_identity()
    if measured_source_after != measured_source_before:
        raise RuntimeError("executed source identity changed during measurement")

    pooled_ns = sum(all_latencies)
    pooled_source_bytes = first["source_html_bytes"] * len(runs)
    pooled_pages = EXPECTED_ROWS * len(runs)
    legacy_accepted = 76
    current_accepted = first["accepted"]
    report = {
        "schema": "clusy.selection-atom-catalog-mechanism-measurement.v1",
        "recorded_start_utc": recorded_start,
        "recorded_end_utc": utc_now(),
        "claim_boundary": {
            "representation_coverage_only": True,
            "wmb_quality_score": False,
            "end_to_end_crawler_latency": False,
            "vendor_latency_comparison": False,
            "sota_claim": False,
            "production_default_changed": False,
        },
        "input": {
            "jsonl_bytes": dataset_bytes,
            "jsonl_sha256": dataset_sha256,
            "records": EXPECTED_ROWS,
            "required_keys": sorted(EXPECTED_KEYS),
            "labels_present_or_read": False,
        },
        "coverage_comparison": {
            "metric": "all-or-nothing selection-atom representation coverage",
            "denominator_pages": EXPECTED_ROWS,
            "legacy_pre_mapper_development_observation": {
                "accepted_pages": legacy_accepted,
                "coverage_ratio": legacy_accepted / EXPECTED_ROWS,
                "reason_counts": {
                    "accepted": legacy_accepted,
                    "incomplete_source_mapping": 7,
                    "truncated_ir": 1,
                    "unreliable_text_mapping": 461,
                },
                "exact_executable_or_source_artifact_retained": False,
                "performance_comparison_permitted": False,
            },
            "ordered_mapper_catalog": {
                "accepted_pages": current_accepted,
                "coverage_ratio": current_accepted / EXPECTED_ROWS,
                "reason_counts": first["reason_counts"],
            },
            "accepted_page_delta": current_accepted - legacy_accepted,
            "coverage_percentage_point_delta": (
                (current_accepted - legacy_accepted) * 100 / EXPECTED_ROWS
            ),
            "quality_or_sota_inference_permitted": False,
        },
        "protocol": {
            "runs": args.runs,
            "warmup_pages": args.warmup_pages,
            "direction": "forward",
            "concurrency": 1,
            "cold_method": (
                "One fixed first-row call after imports, source fingerprinting, and input "
                "SHA-256 validation, but before any prior catalog invocation in the process. "
                "Process startup/import latency is excluded."
            ),
            "warm_method": (
                f"After the cold probe, call the first {args.warmup_pages} rows as "
                f"unmeasured warm-up, then stream {args.runs} complete forward sweeps. "
                "Each page call is timed separately."
            ),
            "timed_region": (
                "build_selection_atom_catalog_v1(html, enabled config), including "
                "ordered-dom-ir.v2 extraction, ordered-source-text-map.v2, validation, "
                "atom construction, and digests"
            ),
            "excluded_from_timed_region": [
                "process startup and imports",
                "JSONL disk read and JSON parsing",
                "per-row HTML SHA-256 validation",
                "network, rendering, scoring, serialization, and vendor APIs",
            ],
            "percentile": "nearest rank over individual page calls",
            "throughput_denominator": "sum of catalog-call wall nanoseconds only",
            "measured_command": (
                ".venv/bin/python bench/selection_atom_catalog_representability.py "
                f"{args.dataset.resolve()} --runs {args.runs} "
                f"--warmup-pages {args.warmup_pages}"
                + (f" --output {args.output.resolve()}" if args.output is not None else "")
            ),
            "relative_reproduction_command": (
                ".venv/bin/python bench/selection_atom_catalog_representability.py "
                "<frozen-input.jsonl> --runs 3 --warmup-pages 16 "
                "--output <report.json>"
            ),
            "legacy_comparator": (
                "unavailable: the pre-mapper catalog was an uncommitted development "
                "snapshot and no exact executable/source artifact was retained"
            ),
        },
        "environment": environment,
        "source_identity": measured_source_before,
        "source_identity_before_and_after_match": True,
        "input_identity_before_and_after_match": True,
        "cold_probe": first_cold_probe,
        "stable_results": {field: first[field] for field in stable_fields},
        "runs": runs,
        "pooled": {
            "observations": len(all_latencies),
            "catalog_call_seconds": pooled_ns / 1_000_000_000,
            "pages_per_catalog_call_second": pooled_pages * 1_000_000_000 / pooled_ns,
            "source_html_bytes_per_catalog_call_second": (
                pooled_source_bytes * 1_000_000_000 / pooled_ns
            ),
            "per_page_ms": {
                "p50_nearest_rank": nearest_rank(all_latencies, 0.50) / 1_000_000,
                "p95_nearest_rank": nearest_rank(all_latencies, 0.95) / 1_000_000,
                "max": max(all_latencies) / 1_000_000,
            },
        },
        "resources": {
            "measurement": "process lifetime ru_maxrss; peak-ish, not allocator attribution",
            "peak_rss_bytes_after_import_source_and_input_hash": (peak_after_import_and_input_hash),
            "peak_rss_bytes_after_cold_probe": peak_after_cold_probe,
            "peak_rss_bytes_after_import_and_warmup": peak_after_warmup,
            "peak_rss_bytes_after_all_runs": peak_rss_bytes(),
            "additional_peak_above_import_and_warmup_bytes": max(
                0, peak_rss_bytes() - peak_after_warmup
            ),
        },
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        output = args.output.resolve()
        evidence_root = (ROOT / "bench" / "evidence").resolve()
        if evidence_root not in output.parents:
            raise RuntimeError("output must stay under bench/evidence")
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.tmp")
        temporary.write_text(rendered, encoding="utf-8")
        os.replace(temporary, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

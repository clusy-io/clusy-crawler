#!/usr/bin/env python3
"""Score frozen atomic-overlay decisions in a later, label-bearing process.

This process never imports the overlay, extractor, or baseline worker. It
hash-binds and uses the same native graph/certificate primitive solely for
independent raw-source replay. It opens labels/evaluator only after both frozen
artifacts and all replayed outputs have passed their closed-schema and
cryptographic bindings.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
import math
import os
import re
import stat
import sys
import tempfile
from importlib.machinery import EXTENSION_SUFFIXES, ExtensionFileLoader, ModuleSpec
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench.atomic_frozen_replay_v0 import (  # noqa: E402
    FrozenReplayError,
    bind_native_replay_primitives,
    replay_frozen_decision,
)
from bench.claimable_io import (  # noqa: E402
    ClaimableIOError,
    read_verified_bytes,
    write_new_file,
)

BASELINE_SCHEMA = "clusy.atomic-overlay-claim-baseline-artifact.3"
DECISION_SCHEMA = "clusy.atomic-overlay-claim-decision-artifact.3"
SCORE_SCHEMA = "clusy.atomic-overlay-frozen-score.3"
DECISION_INPUT_SCHEMA = "webmainbench.atomic-structure-overlay-v0-decision-inputs.3"
EXPECTED_RECORDS = 545
CORE_METRICS = (
    "text_edit",
    "code_edit",
    "formula_edit",
    "table_edit",
    "table_TEDS",
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_GIT_OBJECT_RE = re.compile(r"[0-9a-f]{40}")
QUALITY_THRESHOLDS = {
    "overall": 0.01,
    "code_edit": 0.03,
    "table_TEDS": 0.02,
    "text_edit": 0.0,
    "formula_edit": 0.0,
}
CLAIM_OVERLAY_CONFIG = {
    "enable_code": True,
    "enable_tables": True,
    "enabled": True,
    "max_atom_tokens": 20_000,
    "max_atoms": 256,
    "max_candidate_bytes": 2 * 1024 * 1024,
    "max_certificate_bytes": 64 * 1024,
    "max_code_bytes": 256 * 1024,
    "max_growth_bytes": 256 * 1024,
    "max_growth_ratio_milli": 4_000,
    "max_output_bytes": 4 * 1024 * 1024,
    "max_replacement_bytes": 512 * 1024,
    "max_source_bytes": 4 * 1024 * 1024,
    "max_table_cells": 2_048,
    "max_table_columns": 64,
    "max_table_rows": 128,
    "max_tokens": 200_000,
    "max_total_certificate_bytes": 256 * 1024,
}


class FrozenScoreError(RuntimeError):
    """Frozen artifacts or official score inputs are not canonical."""


def _assert_fresh_score_process() -> None:
    if "bench.webmainbench_finegrained_benchmark" in sys.modules:
        raise FrozenScoreError(
            "scoring requires a fresh process with no evaluator harness preloaded"
        )
    if any(
        name == "clusy_native" or name.startswith("clusy_native.")
        for name in sys.modules
    ):
        raise FrozenScoreError(
            "scoring requires a fresh process with no native module preloaded"
        )


def _nofollow_temporary_root() -> Path:
    """Return the system temp directory through a link-free lexical path."""

    candidate = Path(os.path.realpath(tempfile.gettempdir()))
    if (
        not candidate.is_absolute()
        or not candidate.name
        or any(component in {"", ".", ".."} for component in candidate.parts[1:])
    ):
        raise FrozenScoreError(
            "native replay temporary root is not lexically canonical"
        )
    cursor = Path(candidate.anchor)
    try:
        for component in candidate.parts[1:]:
            cursor /= component
            metadata = os.lstat(cursor)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
            ):
                raise FrozenScoreError(
                    "native replay temporary root contains a link or non-directory"
                )
    except OSError as error:
        raise FrozenScoreError(
            "native replay temporary root could not be inspected"
        ) from error
    return candidate


def _valid_sha256(value: object) -> bool:
    return type(value) is str and _SHA256_RE.fullmatch(value) is not None


def _valid_wall_seconds(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    observed = float(value)
    return math.isfinite(observed) and 0.0 <= observed <= 180.0


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _read_json_artifact(
    path: Path,
    *,
    maximum_bytes: int,
    schema: str,
    expected_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        content, metadata = read_verified_bytes(
            path,
            maximum_bytes=maximum_bytes,
            expected_sha256=expected_sha256,
        )
    except ClaimableIOError as error:
        raise FrozenScoreError(f"artifact is not snapshot-safe: {path}") from error
    try:
        value = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise FrozenScoreError(f"artifact is invalid UTF-8 JSON: {path}") from error
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != schema
        or value.get("claimable") is not True
        or _json_bytes(value) != content
    ):
        raise FrozenScoreError(f"artifact schema/canonical encoding mismatch: {path}")
    return value, {
        "bytes": metadata.bytes,
        "path": str(metadata.path),
        "sha256": metadata.sha256,
    }


def _load_decision_inputs(
    path: Path,
    *,
    expected_sha256: str,
) -> tuple[tuple[dict[str, Any], ...], dict[str, Any]]:
    """Open the externally pinned raw-only projection before evaluator import."""

    try:
        content, metadata = read_verified_bytes(
            path,
            maximum_bytes=768 * 1024 * 1024,
            expected_sha256=expected_sha256,
        )
    except ClaimableIOError as error:
        raise FrozenScoreError(
            f"decision-input projection is not snapshot-safe: {path}"
        ) from error
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise FrozenScoreError(
                f"decision-input row {line_number} is invalid"
            ) from error
        if (
            not isinstance(record, dict)
            or set(record) != {"dataset_index", "raw_html", "schema_version"}
            or record.get("schema_version") != DECISION_INPUT_SCHEMA
            or type(record.get("dataset_index")) is not int
            or record["dataset_index"] != len(records)
            or type(record.get("raw_html")) is not str
        ):
            raise FrozenScoreError(
                f"decision-input row {line_number} schema is not closed"
            )
        try:
            record["raw_html"].encode("utf-8")
        except UnicodeEncodeError as error:
            raise FrozenScoreError(
                f"decision-input row {line_number} is not strict UTF-8"
            ) from error
        records.append(record)
    canonical = b"".join(_json_bytes(record) + b"\n" for record in records)
    if len(records) != EXPECTED_RECORDS or canonical != content:
        raise FrozenScoreError("decision inputs are not exact canonical 545-row JSONL")
    return tuple(records), {
        "bytes": metadata.bytes,
        "path": str(metadata.path),
        "sha256": metadata.sha256,
    }


def _load_dataset_snapshot(
    path: Path,
    *,
    fine: Any,
) -> tuple[tuple[dict[str, Any], ...], dict[str, Any]]:
    try:
        content, metadata = read_verified_bytes(
            path,
            maximum_bytes=fine.DATASET_BYTES,
            expected_sha256=fine.DATASET_SHA256,
        )
    except ClaimableIOError as error:
        raise FrozenScoreError(f"dataset snapshot failed: {error}") from error
    if metadata.bytes != fine.DATASET_BYTES:
        raise FrozenScoreError("dataset byte count mismatch")
    records: list[dict[str, Any]] = []
    projection_digest = hashlib.sha256()
    for index, line in enumerate(content.splitlines()):
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise FrozenScoreError(f"dataset row {index} is invalid") from error
        if (
            not isinstance(row, dict)
            or type(row.get("html")) is not str
            or type(row.get("groundtruth_content")) is not str
        ):
            raise FrozenScoreError(f"dataset row {index} schema mismatch")
        records.append(
            {
                "dataset_index": index,
                "raw_html_sha256": hashlib.sha256(
                    row["html"].encode("utf-8")
                ).hexdigest(),
                "reference": row["groundtruth_content"],
            }
        )
        projection_digest.update(
            _json_bytes(
                {
                    "dataset_index": index,
                    "raw_html": row["html"],
                    "schema_version": DECISION_INPUT_SCHEMA,
                }
            )
            + b"\n"
        )
    if len(records) != EXPECTED_RECORDS:
        raise FrozenScoreError("dataset does not contain exactly 545 rows")
    return tuple(records), {
        "bytes": metadata.bytes,
        "decision_inputs_sha256": projection_digest.hexdigest(),
        "sha256": metadata.sha256,
    }


def _validate_claim_envelopes(
    baseline: dict[str, Any],
    decisions: dict[str, Any],
) -> None:
    if set(baseline) != {
        "claimable",
        "decision_inputs_sha256",
        "launcher",
        "protocol",
        "schema_version",
        "worker",
        "worker_capsule_sha256",
        "worker_wall_seconds",
    } or set(decisions) != {
        "baseline_artifact_sha256",
        "claimable",
        "decision_inputs_sha256",
        "launcher",
        "protocol",
        "schema_version",
        "worker",
        "worker_capsule_sha256",
        "worker_wall_seconds",
    }:
        raise FrozenScoreError("frozen artifact envelope is not closed")
    baseline_protocol = baseline.get("protocol")
    decision_protocol = decisions.get("protocol")
    if baseline_protocol != {
        "concurrency": 1,
        "dataset_evaluator_scorer_mounted": False,
        "labels_available": False,
        "network": "observed isolated namespace with no route/egress",
    } or decision_protocol != {
        "concurrency": 4,
        "dataset_evaluator_scorer_mounted": False,
        "labels_available": False,
        "wall_seconds": 180.0,
    }:
        raise FrozenScoreError("frozen artifact protocol is not exact")
    for artifact in (baseline, decisions):
        launcher = artifact.get("launcher")
        capsule_hashes = artifact.get("worker_capsule_sha256")
        wall = artifact.get("worker_wall_seconds")
        if (
            not isinstance(launcher, dict)
            or launcher.get("available") is not True
            or not all(
                _valid_sha256(launcher.get(name))
                for name in (
                    "bubblewrap_sha256",
                    "env_sha256",
                    "python_sha256",
                )
            )
            or not isinstance(launcher.get("network_probe"), dict)
            or launcher["network_probe"].get("net_namespace_distinct") is not True
            or launcher["network_probe"].get("non_loopback_route_rows") != 0
            or launcher["network_probe"].get("non_loopback_ipv6_route_rows") != 0
            or launcher["network_probe"].get("egress_connect_ex") == 0
            or launcher["network_probe"].get("ipv6_egress_connect_ex") == 0
            or not isinstance(capsule_hashes, dict)
            or not {"claim_guard.py", "worker.py"}.issubset(capsule_hashes)
            or not all(
                type(name) is str and _valid_sha256(digest)
                for name, digest in capsule_hashes.items()
            )
            or not _valid_wall_seconds(wall)
        ):
            raise FrozenScoreError("frozen launcher/capsule evidence is invalid")
    baseline_worker = baseline.get("worker")
    decision_worker = decisions.get("worker")
    if (
        not isinstance(baseline_worker, dict)
        or set(baseline_worker)
        != {
            "baseline",
            "capsule",
            "decision_inputs_sha256",
            "executed",
            "generator",
            "records",
            "runtime",
            "schema_version",
        }
        or baseline_worker.get("schema_version")
        != "clusy.atomic-overlay-frozen-baseline.3"
        or baseline_worker.get("decision_inputs_sha256")
        != baseline.get("decision_inputs_sha256")
        or baseline_worker.get("generator", {}).get("input_field") != "raw_html"
        or baseline_worker.get("generator", {}).get("labels_available") is not False
        or not isinstance(decision_worker, dict)
        or set(decision_worker)
        != {
            "baseline_sha256",
            "capsule",
            "decision_inputs_sha256",
            "decisions",
            "executed",
            "protocol",
            "runtime",
            "schema_version",
            "worker_wall_ns",
        }
        or decision_worker.get("schema_version")
        != "clusy.atomic-overlay-frozen-decisions.3"
        or decision_worker.get("decision_inputs_sha256")
        != decisions.get("decision_inputs_sha256")
        or decision_worker.get("baseline_sha256")
        != decisions.get("baseline_artifact_sha256")
        or decision_worker.get("protocol")
        != {
            "concurrency": 4,
            "config": CLAIM_OVERLAY_CONFIG,
            "dataset_evaluator_scorer_mounted": False,
            "labels_available": False,
            "wall_seconds": 180.0,
        }
    ):
        raise FrozenScoreError("frozen worker evidence is invalid")
    baseline_capsule = baseline_worker.get("capsule")
    decision_capsule = decision_worker.get("capsule")
    if (
        not isinstance(baseline_capsule, dict)
        or not isinstance(decision_capsule, dict)
        or _GIT_OBJECT_RE.fullmatch(
            str(baseline_capsule.get("source_commit", ""))
        )
        is None
        or _GIT_OBJECT_RE.fullmatch(
            str(baseline_capsule.get("source_tree", ""))
        )
        is None
        or baseline_capsule.get("source_commit")
        != decision_capsule.get("source_commit")
        or baseline_capsule.get("source_tree")
        != decision_capsule.get("source_tree")
        or not _valid_sha256(baseline_capsule.get("native_source_digest"))
        or not _valid_sha256(decision_capsule.get("native_source_digest"))
        or not _valid_sha256(baseline_capsule.get("extension_sha256"))
        or not _valid_sha256(decision_capsule.get("extension_sha256"))
        or baseline_capsule.get("native_source_digest")
        != decision_capsule.get("native_source_digest")
        or baseline_capsule.get("extension_sha256")
        != decision_capsule.get("extension_sha256")
    ):
        raise FrozenScoreError("baseline and decision executable identities differ")
    for artifact, capsule in (
        (baseline, baseline_capsule),
        (decisions, decision_capsule),
    ):
        extension_relative = capsule.get("extension_relative_path")
        capsule_hashes = artifact["worker_capsule_sha256"]
        if (
            type(extension_relative) is not str
            or not extension_relative.startswith("clusy_native/_native.")
            or "/" in extension_relative[len("clusy_native/") :]
            or capsule_hashes.get(extension_relative)
            != capsule.get("extension_sha256")
        ):
            raise FrozenScoreError(
                "native extension identity is not bound to the sealed capsule"
            )


def _verify_local_native_replay_identity(
    decisions: dict[str, Any],
) -> dict[str, Any]:
    """Hash a stable extension snapshot before loading and binding its builtins."""

    capsule = decisions["worker"]["capsule"]
    expected_extension = capsule["extension_sha256"]
    expected_source = capsule["native_source_digest"]
    extension_relative = capsule["extension_relative_path"]
    relative = PurePosixPath(extension_relative)
    candidates: dict[str, Path] = {}
    for entry in sys.path:
        if type(entry) is not str:
            raise FrozenScoreError("scorer sys.path contains a non-string entry")
        root = Path.cwd() if entry == "" else Path(entry)
        candidate = root.joinpath(*relative.parts).absolute()
        candidate_key = os.path.normcase(os.path.normpath(str(candidate)))
        if candidate_key in candidates:
            continue
        if (
            candidate.name != relative.name
            or candidate.parent.name != "clusy_native"
        ):
            raise FrozenScoreError(
                "native replay candidate path is not capsule-relative"
            )
        try:
            os.lstat(candidate)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as error:
            raise FrozenScoreError(
                "native replay candidate path could not be inspected"
            ) from error
        candidates[candidate_key] = candidate
    if len(candidates) != 1:
        raise FrozenScoreError(
            "native replay requires exactly one unambiguous sys.path candidate"
        )
    extension_path = next(iter(candidates.values()))
    try:
        extension_bytes, metadata = read_verified_bytes(
            extension_path,
            maximum_bytes=256 * 1024 * 1024,
            expected_sha256=expected_extension,
        )
    except ClaimableIOError as error:
        raise FrozenScoreError(
            "native replay extension failed pre-execution snapshot verification"
        ) from error

    native: Any = None
    packaged_source: Any = None
    with tempfile.TemporaryDirectory(
        prefix="clusy-native-replay-",
        dir=_nofollow_temporary_root(),
    ) as temporary:
        snapshot = Path(temporary) / relative.name
        try:
            snapshot_metadata = write_new_file(
                snapshot,
                extension_bytes,
                mode=0o400,
            )
            if (
                snapshot_metadata.sha256 != expected_extension
                or snapshot_metadata.bytes != len(extension_bytes)
            ):
                raise FrozenScoreError(
                    "native replay private snapshot identity differs"
            )
            package = ModuleType("clusy_native")
            package.__package__ = "clusy_native"
            package.__path__ = [str(snapshot.parent)]
            package_spec = ModuleSpec(
                "clusy_native",
                loader=None,
                is_package=True,
            )
            package_spec.submodule_search_locations = [str(snapshot.parent)]
            package.__spec__ = package_spec
            loader = ExtensionFileLoader(
                "clusy_native._native",
                str(snapshot),
            )
            spec = spec_from_file_location(
                "clusy_native._native",
                snapshot,
                loader=loader,
            )
            if spec is None:
                raise FrozenScoreError(
                    "verified native replay snapshot has no import spec"
                )
            sys.modules["clusy_native"] = package
            native = module_from_spec(spec)
            sys.modules["clusy_native._native"] = native
            loader.exec_module(native)
            if (
                spec.name != "clusy_native._native"
                or not isinstance(spec.loader, ExtensionFileLoader)
                or type(spec.origin) is not str
                or native.__file__ != spec.origin
                or spec.origin != str(snapshot)
                or not any(
                    spec.origin.endswith(suffix)
                    for suffix in EXTENSION_SUFFIXES
                )
            ):
                raise FrozenScoreError(
                    "local native replay module is not the verified snapshot"
                )
            primitives = (
                native.extract_document_ir_v2_native,
                native.create_local_atomic_selection_certificate_v0_native,
                native.verify_and_replay_local_atomic_selection_certificate_v0_native,
                native.packaged_source_digest,
            )
            if not all(
                inspect.isbuiltin(primitive)
                and primitive.__module__ == "clusy_native._native"
                and primitive.__self__ is native
                for primitive in primitives
            ):
                raise FrozenScoreError(
                    "local native replay primitives are not exact extension builtins"
                )
            packaged_source = native.packaged_source_digest()
            if (
                metadata.sha256 != expected_extension
                or packaged_source != expected_source
                or not _valid_sha256(packaged_source)
            ):
                raise FrozenScoreError(
                    "local native replay primitive differs from frozen worker identity"
                )
            bind_native_replay_primitives(native)
        except (
            AttributeError,
            ClaimableIOError,
            FrozenReplayError,
            ImportError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as error:
            if isinstance(error, FrozenScoreError):
                raise
            raise FrozenScoreError(
                "verified native replay snapshot could not be loaded"
            ) from error
        finally:
            sys.modules.pop("clusy_native._native", None)
            sys.modules.pop("clusy_native", None)
    if native is None or not _valid_sha256(packaged_source):
        raise FrozenScoreError(
            "native replay primitive did not remain bound after snapshot load"
        )
    return {
        "extension_sha256": metadata.sha256,
        "native_source_digest": packaged_source,
        "path": str(metadata.path),
        "pre_execution_snapshot_verified": True,
    }


def _page_success_mask(
    rows: list[dict[str, Any]],
    metric: str,
) -> tuple[bool, ...]:
    return tuple(
        (result := row.get(metric)) is not None and result.success is True
        for row in rows
    )


def _conservative_aggregates(
    baseline_results: list[dict[str, Any]],
    candidate_results: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, bool]]:
    baseline_metrics: dict[str, Any] = {}
    candidate_metrics: dict[str, Any] = {}
    parity: dict[str, bool] = {}
    for metric in CORE_METRICS:
        baseline_scores: list[float] = []
        candidate_scores: list[float] = []
        baseline_mask = _page_success_mask(baseline_results, metric)
        candidate_mask = _page_success_mask(candidate_results, metric)
        for index, (baseline_page, candidate_page) in enumerate(
            zip(baseline_results, candidate_results, strict=True)
        ):
            if metric not in baseline_page or metric not in candidate_page:
                raise FrozenScoreError(f"official metric is missing: {metric}:{index}")
            baseline_score = float(baseline_page[metric].score)
            candidate_score = float(candidate_page[metric].score)
            if not math.isfinite(baseline_score) or not math.isfinite(candidate_score):
                raise FrozenScoreError(
                    f"official metric is non-finite: {metric}:{index}"
                )
            baseline_scores.append(
                baseline_score if baseline_mask[index] else 0.0
            )
            candidate_scores.append(
                candidate_score if candidate_mask[index] else 0.0
            )

        def payload(scores: list[float], mask: tuple[bool, ...]) -> dict[str, Any]:
            failed = [index for index, success in enumerate(mask) if not success]
            return {
                "failed_dataset_indices": failed,
                "failed_pages": len(failed),
                "failure_scoring": "zero",
                "score": sum(scores) / EXPECTED_RECORDS,
                "success_mask_sha256": hashlib.sha256(
                    _json_bytes(list(mask))
                ).hexdigest(),
                "successful_pages": sum(mask),
            }

        baseline_metrics[metric] = payload(baseline_scores, baseline_mask)
        candidate_metrics[metric] = payload(candidate_scores, candidate_mask)
        parity[metric] = baseline_mask == candidate_mask
    baseline = {
        "metrics": baseline_metrics,
        "overall": sum(
            baseline_metrics[metric]["score"] for metric in CORE_METRICS
        )
        / len(CORE_METRICS),
        "protocol": "all 545 pages; every failed metric scores zero",
    }
    candidate = {
        "metrics": candidate_metrics,
        "overall": sum(
            candidate_metrics[metric]["score"] for metric in CORE_METRICS
        )
        / len(CORE_METRICS),
        "protocol": "all 545 pages; every failed metric scores zero",
    }
    delta = {
        "overall": candidate["overall"] - baseline["overall"],
        "metrics": {
            metric: {
                "score": (
                    candidate_metrics[metric]["score"]
                    - baseline_metrics[metric]["score"]
                )
            }
            for metric in CORE_METRICS
        },
    }
    return baseline, candidate, delta, parity


def score(
    baseline_path: Path,
    decisions_path: Path,
    expected_baseline_sha256: str,
    expected_decision_sha256: str,
    decision_inputs_path: Path,
    expected_decision_inputs_sha256: str,
    dataset_path: Path,
    evaluator_root: Path,
    output: Path,
) -> dict[str, Any]:
    if (
        _SHA256_RE.fullmatch(expected_baseline_sha256) is None
        or _SHA256_RE.fullmatch(expected_decision_sha256) is None
        or _SHA256_RE.fullmatch(expected_decision_inputs_sha256) is None
    ):
        raise FrozenScoreError(
            "exact baseline, decision, and raw-projection SHA-256 pins are required"
        )
    _assert_fresh_score_process()
    projection_rows, projection_identity = _load_decision_inputs(
        decision_inputs_path,
        expected_sha256=expected_decision_inputs_sha256,
    )
    baseline, baseline_identity = _read_json_artifact(
        baseline_path,
        maximum_bytes=512 * 1024 * 1024,
        schema=BASELINE_SCHEMA,
        expected_sha256=expected_baseline_sha256,
    )
    decisions, decisions_identity = _read_json_artifact(
        decisions_path,
        maximum_bytes=1024 * 1024 * 1024,
        schema=DECISION_SCHEMA,
        expected_sha256=expected_decision_sha256,
    )
    _validate_claim_envelopes(baseline, decisions)
    native_replay_identity = _verify_local_native_replay_identity(decisions)
    if (
        decisions.get("baseline_artifact_sha256") != baseline_identity["sha256"]
        or decisions.get("decision_inputs_sha256")
        != baseline.get("decision_inputs_sha256")
        or decisions.get("decision_inputs_sha256") != projection_identity["sha256"]
        or decisions.get("protocol", {}).get("concurrency") != 4
        or decisions.get("protocol", {}).get("wall_seconds") != 180.0
        or decisions.get("protocol", {}).get("labels_available") is not False
        or decisions.get("protocol", {}).get("dataset_evaluator_scorer_mounted")
        is not False
    ):
        raise FrozenScoreError("baseline/decision cryptographic protocol binding failed")
    baseline_rows = baseline.get("worker", {}).get("records")
    decision_rows = decisions.get("worker", {}).get("decisions")
    if (
        not isinstance(baseline_rows, list)
        or not isinstance(decision_rows, list)
        or len(baseline_rows) != EXPECTED_RECORDS
        or len(decision_rows) != EXPECTED_RECORDS
    ):
        raise FrozenScoreError("frozen artifacts do not contain exactly 545 rows")

    # Derive every candidate from the externally pinned raw-only projection and
    # frozen baseline before the evaluator or labels can enter this process.
    replayed_rows: list[tuple[str, str]] = []
    replay_receipts: list[dict[str, Any]] = []
    baseline_generation_all_success = True
    for index, (baseline_row, decision_row, projection_row) in enumerate(
        zip(baseline_rows, decision_rows, projection_rows, strict=True)
    ):
        if (
            not isinstance(baseline_row, dict)
            or set(baseline_row) != {"dataset_index", "generation", "prediction"}
            or not isinstance(decision_row, dict)
            or set(decision_row)
            != {
                "baseline_prediction_sha256",
                "dataset_index",
                "decision",
                "raw_html_sha256",
            }
            or baseline_row.get("dataset_index") != index
            or decision_row.get("dataset_index") != index
            or projection_row["dataset_index"] != index
            or type(baseline_row.get("prediction")) is not str
        ):
            raise FrozenScoreError(f"frozen/projection row alignment failed: {index}")
        generation = baseline_row["generation"]
        if (
            not isinstance(generation, dict)
            or set(generation) != {"error_type", "strategy", "success"}
            or type(generation.get("success")) is not bool
            or type(generation.get("strategy")) is not str
            or (
                generation.get("error_type") is not None
                and type(generation.get("error_type")) is not str
            )
            or (generation["success"] is True) != (generation.get("error_type") is None)
        ):
            raise FrozenScoreError(f"frozen baseline generation is invalid: {index}")
        try:
            prediction_bytes = baseline_row["prediction"].encode("utf-8")
            raw_html_bytes = projection_row["raw_html"].encode("utf-8")
        except UnicodeEncodeError as error:
            raise FrozenScoreError(
                f"frozen/projection row is not strict UTF-8: {index}"
            ) from error
        if (
            decision_row.get("baseline_prediction_sha256")
            != hashlib.sha256(prediction_bytes).hexdigest()
            or decision_row.get("raw_html_sha256")
            != hashlib.sha256(raw_html_bytes).hexdigest()
        ):
            raise FrozenScoreError(f"frozen row byte identity failed: {index}")
        try:
            replay = replay_frozen_decision(
                projection_row["raw_html"],
                baseline_row["prediction"],
                decision_row["decision"],
                config=CLAIM_OVERLAY_CONFIG,
            )
        except FrozenReplayError as error:
            raise FrozenScoreError(
                f"independent frozen decision replay failed: {index}: {error}"
            ) from error
        replayed_rows.append((baseline_row["prediction"], replay.output_markdown))
        replay_receipts.append(
            {
                "applied_proposal_ids": replay.applied_proposal_ids,
                "dataset_index": index,
                "decision_digest": replay.decision_digest,
                "input_digest": replay.input_digest,
                "output_digest": replay.output_digest,
                "proposal_ids": replay.proposal_ids,
                "source_digest": replay.source_digest,
                "visible_token_digest": replay.visible_token_digest,
            }
        )
        baseline_generation_all_success = (
            baseline_generation_all_success and generation["success"]
        )

    # This is the first evaluator-harness import and label access. The only
    # candidate strings retained for scoring are independently replayed above.
    fine = importlib.import_module("bench.webmainbench_finegrained_benchmark")
    if tuple(fine.CORE_METRICS) != CORE_METRICS:
        raise FrozenScoreError(
            "official metric inventory disagrees with claim protocol"
        )
    try:
        dataset_rows, dataset_identity = _load_dataset_snapshot(
            dataset_path,
            fine=fine,
        )
        evaluator_before = fine.verify_evaluator(evaluator_root)
        calculator_type, _, dependencies = fine.load_official_toolkit(
            evaluator_root
        )
    except fine.BenchmarkError as error:
        raise FrozenScoreError(f"official score input failed: {error}") from error
    if dataset_identity["decision_inputs_sha256"] != projection_identity["sha256"]:
        raise FrozenScoreError(
            "frozen decisions do not bind the raw-HTML projection of the pinned dataset"
        )
    baseline_calculator = calculator_type({"use_llm": False})
    candidate_calculator = calculator_type({"use_llm": False})
    baseline_results: list[dict[str, Any]] = []
    candidate_results: list[dict[str, Any]] = []
    for index, ((baseline_prediction, candidate_prediction), dataset_row) in enumerate(
        zip(replayed_rows, dataset_rows, strict=True)
    ):
        if (
            dataset_row["dataset_index"] != index
            or dataset_row["raw_html_sha256"]
            != hashlib.sha256(
                projection_rows[index]["raw_html"].encode("utf-8")
            ).hexdigest()
        ):
            raise FrozenScoreError(f"pinned dataset/projection mismatch: {index}")
        baseline_results.append(
            baseline_calculator.calculate_all(
                predicted_content=baseline_prediction,
                groundtruth_content=dataset_row["reference"],
                predicted_content_list=None,
                groundtruth_content_list=None,
            )
        )
        candidate_results.append(
            candidate_calculator.calculate_all(
                predicted_content=candidate_prediction,
                groundtruth_content=dataset_row["reference"],
                predicted_content_list=None,
                groundtruth_content_list=None,
            )
        )
    try:
        evaluator_after = fine.verify_evaluator(evaluator_root)
    except fine.BenchmarkError as error:
        raise FrozenScoreError(f"official evaluator recheck failed: {error}") from error
    if evaluator_before != evaluator_after:
        raise FrozenScoreError("official evaluator changed during scoring")
    official_baseline = fine._official_aggregate(  # noqa: SLF001
        baseline_calculator,
        baseline_results,
    )
    official_candidate = fine._official_aggregate(  # noqa: SLF001
        candidate_calculator,
        candidate_results,
    )
    (
        baseline_aggregate,
        candidate_aggregate,
        delta,
        success_masks,
    ) = _conservative_aggregates(baseline_results, candidate_results)
    checks = {
        "all_metric_success_masks_exact": all(success_masks.values()),
        "baseline_generation_all_success": baseline_generation_all_success,
        "code_edit_delta_at_least_0_03": (
            delta["metrics"]["code_edit"]["score"] >= QUALITY_THRESHOLDS["code_edit"]
        ),
        "formula_edit_non_regression": (
            delta["metrics"]["formula_edit"]["score"]
            >= QUALITY_THRESHOLDS["formula_edit"]
        ),
        "overall_delta_at_least_0_01": (
            delta["overall"] >= QUALITY_THRESHOLDS["overall"]
        ),
        "table_TEDS_delta_at_least_0_02": (
            delta["metrics"]["table_TEDS"]["score"]
            >= QUALITY_THRESHOLDS["table_TEDS"]
        ),
        "text_edit_non_regression": (
            delta["metrics"]["text_edit"]["score"]
            >= QUALITY_THRESHOLDS["text_edit"]
        ),
    }
    if not all(
        math.isfinite(float(item["score"]))
        for item in delta["metrics"].values()
    ) or not math.isfinite(float(delta["overall"])):
        raise FrozenScoreError("score delta is non-finite")
    document = {
        "artifacts": {
            "baseline": baseline_identity,
            "decision_inputs": projection_identity,
            "decisions": decisions_identity,
        },
        "baseline": baseline_aggregate,
        "candidate": candidate_aggregate,
        "dataset": dataset_identity,
        "delta": delta,
        "evaluator": evaluator_before,
        "evaluator_dependencies": dependencies,
        "gates": {
            "checks": checks,
            "passed": all(checks.values()),
            "thresholds": QUALITY_THRESHOLDS,
        },
        "independent_frozen_replay": {
            "completed_before_evaluator_import": True,
            "native_replay_identity": native_replay_identity,
            "records": len(replay_receipts),
            "sha256": hashlib.sha256(_json_bytes(replay_receipts)).hexdigest(),
        },
        "label_access_phase": "later separate scorer process over frozen artifacts",
        "official_aggregate_diagnostic": {
            "baseline": official_baseline,
            "candidate": official_candidate,
            "claim_gate": False,
        },
        "schema_version": SCORE_SCHEMA,
        "success_mask_parity": success_masks,
    }
    try:
        write_new_file(output, _json_bytes(document), mode=0o400)
    except ClaimableIOError as error:
        raise FrozenScoreError(f"could not publish frozen score: {error}") from error
    return document


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-artifact", required=True, type=Path)
    parser.add_argument("--decision-artifact", required=True, type=Path)
    parser.add_argument("--expected-baseline-sha256", required=True)
    parser.add_argument("--expected-decision-sha256", required=True)
    parser.add_argument("--decision-inputs", required=True, type=Path)
    parser.add_argument("--expected-decision-inputs-sha256", required=True)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--evaluator-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        document = score(
            args.baseline_artifact,
            args.decision_artifact,
            args.expected_baseline_sha256,
            args.expected_decision_sha256,
            args.decision_inputs,
            args.expected_decision_inputs_sha256,
            args.dataset,
            args.evaluator_root,
            args.output,
        )
    except FrozenScoreError as error:
        print(f"frozen score error: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "delta": document["delta"],
                "gates": document["gates"],
                "output": str(args.output),
            },
            sort_keys=True,
        )
    )
    return 0 if document["gates"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

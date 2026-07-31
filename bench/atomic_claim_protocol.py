#!/usr/bin/env python3
"""Build frozen baseline/decision artifacts in separate enforceable sandboxes.

This coordinator never accepts a dataset, evaluator, scorer, label path,
callable, extractor configuration, concurrency, or wall-time override.
Unavailable bubblewrap/user namespaces/no-egress proof makes the run
nonclaimable by refusal.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench.claimable_io import (  # noqa: E402
    ClaimableIOError,
    read_verified_bytes,
    write_new_file,
)
from bench.claimable_sandbox import (  # noqa: E402
    CLAIMABLE_CONCURRENCY,
    CLAIMABLE_WALL_SECONDS,
    SandboxExecutionError,
    SandboxUnavailableError,
    probe_sandbox,
    run_claimable_worker,
)
from bench.source_provenance import (  # noqa: E402
    SourceInventoryError,
    native_source_digest,
    verify_loaded_native_source_binding,
)

DECISION_INPUT_SCHEMA = "webmainbench.atomic-structure-overlay-v0-decision-inputs.3"
BASELINE_ARTIFACT_SCHEMA = "clusy.atomic-overlay-claim-baseline-artifact.3"
DECISION_ARTIFACT_SCHEMA = "clusy.atomic-overlay-claim-decision-artifact.3"
EXPECTED_RECORDS = 545
_GIT_OBJECT_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MAX_PROJECTION_BYTES = 768 * 1024 * 1024
_MAX_BASELINE_ARTIFACT_BYTES = 512 * 1024 * 1024


class ClaimProtocolError(RuntimeError):
    """The fixed claim protocol could not produce a valid frozen artifact."""


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _git_output(*arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise ClaimProtocolError(
            "git provenance command failed: "
            + completed.stderr.decode("utf-8", errors="replace")
        )
    return completed.stdout


def _clean_source_identity() -> dict[str, str]:
    commit = _git_output("rev-parse", "HEAD").decode("ascii").strip()
    tree = _git_output("rev-parse", "HEAD^{tree}").decode("ascii").strip()
    status = _git_output(
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    if (
        _GIT_OBJECT_RE.fullmatch(commit) is None
        or _GIT_OBJECT_RE.fullmatch(tree) is None
        or status
    ):
        raise ClaimProtocolError("claim protocol requires an exact clean Git tree")
    return {"source_commit": commit, "source_tree": tree}


def _tracked_python_files(prefix: str) -> tuple[str, ...]:
    raw = _git_output("ls-files", "-z", "--", prefix)
    paths = []
    for encoded in raw.split(b"\0"):
        if not encoded:
            continue
        relative = os.fsdecode(encoded)
        if relative.endswith((".py", ".pyi")):
            paths.append(relative)
    if not paths:
        raise ClaimProtocolError(f"tracked capsule inventory is empty: {prefix}")
    return tuple(sorted(paths))


def _read_source_file(relative: str, *, maximum_bytes: int = 16 * 1024 * 1024) -> bytes:
    try:
        content, _ = read_verified_bytes(
            ROOT / relative,
            maximum_bytes=maximum_bytes,
        )
    except ClaimableIOError as error:
        raise ClaimProtocolError(f"source file integrity failed: {relative}") from error
    committed = _git_output("show", f"HEAD:{relative}")
    if content != committed:
        raise ClaimProtocolError(
            f"source file bytes disagree with the committed tree: {relative}"
        )
    return content


def _native_capsule() -> tuple[dict[str, bytes], dict[str, str]]:
    try:
        binding = verify_loaded_native_source_binding(ROOT)
    except SourceInventoryError as error:
        raise ClaimProtocolError(f"native source binding failed: {error}") from error
    native_package = importlib.import_module("clusy_native")
    native_extension = importlib.import_module("clusy_native._native")
    package_path = Path(str(native_package.__file__))
    extension_path = Path(str(native_extension.__file__))
    try:
        package_bytes, _ = read_verified_bytes(
            package_path,
            maximum_bytes=1024 * 1024,
        )
        extension_bytes, extension_metadata = read_verified_bytes(
            extension_path,
            maximum_bytes=256 * 1024 * 1024,
        )
    except ClaimableIOError as error:
        raise ClaimProtocolError("loaded native package is not snapshot-safe") from error
    if (
        package_bytes
        != _read_source_file("native/python/clusy_native/__init__.py")
        or package_path.parent != extension_path.parent
    ):
        raise ClaimProtocolError(
            "installed native package bytes disagree with the exact checkout"
        )
    capsule: dict[str, bytes] = {}
    for relative in _tracked_python_files("native/python/clusy_native"):
        capsule_relative = Path(relative).relative_to(
            "native/python"
        ).as_posix()
        capsule[capsule_relative] = _read_source_file(relative)
    extension_relative = f"clusy_native/{extension_path.name}"
    capsule[extension_relative] = extension_bytes
    digest = native_source_digest(ROOT)
    if (
        binding.get("matched") is not True
        or binding.get("packaged_sha256") != digest
    ):
        raise ClaimProtocolError("loaded native binary/source digest mismatch")
    return capsule, {
        "extension_relative_path": extension_relative,
        "extension_sha256": extension_metadata.sha256,
        "native_source_digest": digest,
    }


def _decision_capsule(source: Mapping[str, str]) -> tuple[dict[str, bytes], dict[str, Any]]:
    capsule, native = _native_capsule()
    worker = _read_source_file("bench/atomic_decision_worker.py")
    guard = _read_source_file("bench/claim_worker_guard.py")
    overlay_relative = "app/services/atomic_structure_overlay_v0.py"
    overlay = _read_source_file(overlay_relative)
    capsule.update(
        {
            "worker.py": worker,
            "claim_guard.py": guard,
            "app/__init__.py": _read_source_file("app/__init__.py"),
            "app/services/__init__.py": _read_source_file("app/services/__init__.py"),
            overlay_relative: overlay,
        }
    )
    manifest = {
        **native,
        **source,
        "overlay_sha256": _sha256_bytes(overlay),
    }
    return capsule, manifest


def _baseline_capsule(source: Mapping[str, str]) -> tuple[dict[str, bytes], dict[str, Any]]:
    capsule, native = _native_capsule()
    app_hashes: dict[str, str] = {}
    for relative in _tracked_python_files("app"):
        content = _read_source_file(relative)
        capsule[relative] = content
        app_hashes[relative] = _sha256_bytes(content)
    capsule["worker.py"] = _read_source_file("bench/atomic_baseline_worker.py")
    capsule["claim_guard.py"] = _read_source_file("bench/claim_worker_guard.py")
    extractor_relative = "app/services/extractor.py"
    manifest = {
        **native,
        **source,
        "app_file_sha256": app_hashes,
        "extractor_sha256": app_hashes[extractor_relative],
        "lock_sha256": {
            "native/Cargo.lock": _sha256_bytes(
                _read_source_file("native/Cargo.lock")
            ),
            "pyproject.toml": _sha256_bytes(
                _read_source_file("pyproject.toml")
            ),
            "uv.lock": _sha256_bytes(_read_source_file("uv.lock", maximum_bytes=64 * 1024 * 1024)),
        },
    }
    return capsule, manifest


def _load_projection(path: Path) -> tuple[tuple[dict[str, Any], ...], bytes, str]:
    try:
        content, metadata = read_verified_bytes(
            path,
            maximum_bytes=_MAX_PROJECTION_BYTES,
        )
    except ClaimableIOError as error:
        raise ClaimProtocolError(f"decision projection is not snapshot-safe: {error}") from error
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ClaimProtocolError(
                f"invalid projection row {line_number}"
            ) from error
        if not isinstance(record, dict) or set(record) != {
            "dataset_index",
            "raw_html",
            "schema_version",
        }:
            raise ClaimProtocolError(f"projection row {line_number} schema mismatch")
        index = record.get("dataset_index")
        if (
            record.get("schema_version") != DECISION_INPUT_SCHEMA
            or type(index) is not int
            or index != len(records)
            or type(record.get("raw_html")) is not str
        ):
            raise ClaimProtocolError(
                f"projection row {line_number} is not canonical"
            )
        records.append(record)
    if len(records) != EXPECTED_RECORDS:
        raise ClaimProtocolError("projection must contain exactly 545 rows")
    canonical = b"".join(_json_bytes(record) + b"\n" for record in records)
    if content != canonical:
        raise ClaimProtocolError("projection is not canonical JSONL")
    return tuple(records), content, metadata.sha256


def _decode_canonical_worker_output(content: bytes) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ClaimProtocolError("worker emitted invalid UTF-8 JSON") from error
    if not isinstance(value, dict) or _json_bytes(value) != content:
        raise ClaimProtocolError("worker output is not canonical JSON")
    return value


def _validate_execution_evidence(
    worker_output: Mapping[str, Any],
    execution: Any,
) -> None:
    runtime = worker_output.get("runtime")
    launcher = execution.launcher
    probe = launcher.network_probe
    if (
        not isinstance(runtime, dict)
        or launcher.available is not True
        or not isinstance(probe, Mapping)
        or runtime.get("fresh_interpreter") is not True
        or runtime.get("environment") != probe.get("environment")
        or not isinstance(runtime.get("mountinfo_sha256"), str)
        or _SHA256_RE.fullmatch(runtime["mountinfo_sha256"]) is None
    ):
        raise ClaimProtocolError("worker bootstrap evidence disagrees with launcher")
    python = runtime.get("python")
    network = runtime.get("network")
    expected_parent = int(runtime["environment"]["CLUSY_PARENT_NETNS_INODE"])
    if (
        not isinstance(python, dict)
        or python.get("executable") != launcher.python_path
        or python.get("executable_sha256") != launcher.python_sha256
        or python.get("flags")
        != {
            "dont_write_bytecode": True,
            "hash_randomization": 1,
            "isolated": 1,
            "no_site": 1,
            "no_user_site": 1,
        }
        or python.get("hash_determinism")
        != "algorithmic ordering; interpreter hash randomization is allowed"
        or not isinstance(network, dict)
        or network.get("non_loopback_route_rows") != 0
        or network.get("non_loopback_ipv6_route_rows") != 0
        or network.get("egress_connect_ex") == 0
        or network.get("ipv6_egress_connect_ex") == 0
        or network.get("parent_namespace_inode")
        == network.get("worker_namespace_inode")
        or network.get("parent_namespace_inode") != expected_parent
        or _SHA256_RE.fullmatch(str(network.get("route_table_sha256", "")))
        is None
        or _SHA256_RE.fullmatch(
            str(network.get("ipv6_route_table_sha256", ""))
        )
        is None
    ):
        raise ClaimProtocolError("worker interpreter or network evidence failed")


def _assert_source_unchanged(source: Mapping[str, str]) -> None:
    if _clean_source_identity() != dict(source):
        raise ClaimProtocolError("source identity changed during claim execution")


def _write_artifact(path: Path, value: object) -> dict[str, Any]:
    content = _json_bytes(value)
    try:
        metadata = write_new_file(path, content, mode=0o400)
    except ClaimableIOError as error:
        raise ClaimProtocolError(f"could not publish frozen artifact: {error}") from error
    return {
        "bytes": metadata.bytes,
        "path": str(metadata.path),
        "sha256": metadata.sha256,
    }


def run_baseline(decision_inputs: Path, output: Path) -> dict[str, Any]:
    source = _clean_source_identity()
    projection, _, projection_sha256 = _load_projection(decision_inputs)
    capsule, capsule_manifest = _baseline_capsule(source)
    worker_input = {
        "capsule": capsule_manifest,
        "concurrency": 1,
        "decision_inputs_sha256": projection_sha256,
        "records": [
            {
                "dataset_index": record["dataset_index"],
                "raw_html": record["raw_html"],
            }
            for record in projection
        ],
        "schema_version": "clusy.atomic-overlay-claim-baseline-input.3",
    }
    execution = run_claimable_worker(
        capsule,
        stdin=_json_bytes(worker_input),
    )
    worker_output = _decode_canonical_worker_output(execution.stdout)
    if (
        worker_output.get("schema_version")
        != "clusy.atomic-overlay-frozen-baseline.3"
        or worker_output.get("capsule") != capsule_manifest
        or worker_output.get("decision_inputs_sha256") != projection_sha256
    ):
        raise ClaimProtocolError("baseline worker output identity mismatch")
    _validate_execution_evidence(worker_output, execution)
    _assert_source_unchanged(source)
    artifact = {
        "claimable": True,
        "decision_inputs_sha256": projection_sha256,
        "launcher": asdict(execution.launcher),
        "protocol": {
            "concurrency": 1,
            "dataset_evaluator_scorer_mounted": False,
            "labels_available": False,
            "network": "observed isolated namespace with no route/egress",
        },
        "schema_version": BASELINE_ARTIFACT_SCHEMA,
        "worker": worker_output,
        "worker_capsule_sha256": dict(execution.capsule_sha256),
        "worker_wall_seconds": execution.wall_seconds,
    }
    identity = _write_artifact(output, artifact)
    return {"artifact": identity, "document": artifact}


def _load_baseline_artifact(path: Path) -> tuple[dict[str, Any], str]:
    try:
        content, metadata = read_verified_bytes(
            path,
            maximum_bytes=_MAX_BASELINE_ARTIFACT_BYTES,
        )
    except ClaimableIOError as error:
        raise ClaimProtocolError(f"baseline artifact is not snapshot-safe: {error}") from error
    value = _decode_canonical_worker_output(content)
    if (
        value.get("schema_version") != BASELINE_ARTIFACT_SCHEMA
        or value.get("claimable") is not True
        or not isinstance(value.get("worker"), dict)
        or value["worker"].get("schema_version")
        != "clusy.atomic-overlay-frozen-baseline.3"
        or value.get("launcher", {}).get("available") is not True
    ):
        raise ClaimProtocolError("baseline artifact is not claimable")
    return value, metadata.sha256


def run_decisions(
    decision_inputs: Path,
    baseline_artifact: Path,
    output: Path,
) -> dict[str, Any]:
    source = _clean_source_identity()
    projection, _, projection_sha256 = _load_projection(decision_inputs)
    baseline, baseline_sha256 = _load_baseline_artifact(baseline_artifact)
    if baseline.get("decision_inputs_sha256") != projection_sha256:
        raise ClaimProtocolError("baseline/projection identity mismatch")
    baseline_capsule = baseline["worker"].get("capsule")
    if (
        not isinstance(baseline_capsule, dict)
        or baseline_capsule.get("source_commit") != source["source_commit"]
        or baseline_capsule.get("source_tree") != source["source_tree"]
    ):
        raise ClaimProtocolError(
            "baseline and decision workers must execute the same committed tree"
        )
    baseline_rows = baseline["worker"].get("records")
    if not isinstance(baseline_rows, list) or len(baseline_rows) != EXPECTED_RECORDS:
        raise ClaimProtocolError("baseline artifact has invalid rows")
    capsule, capsule_manifest = _decision_capsule(source)
    records: list[dict[str, Any]] = []
    for projection_row, baseline_row in zip(
        projection,
        baseline_rows,
        strict=True,
    ):
        if (
            not isinstance(baseline_row, dict)
            or baseline_row.get("dataset_index")
            != projection_row["dataset_index"]
            or type(baseline_row.get("prediction")) is not str
        ):
            raise ClaimProtocolError("baseline/projection row alignment failed")
        records.append(
            {
                "baseline_prediction": baseline_row["prediction"],
                "dataset_index": projection_row["dataset_index"],
                "raw_html": projection_row["raw_html"],
            }
        )
    worker_input = {
        "baseline_sha256": baseline_sha256,
        "capsule": capsule_manifest,
        "concurrency": CLAIMABLE_CONCURRENCY,
        "decision_inputs_sha256": projection_sha256,
        "records": records,
        "schema_version": "clusy.atomic-overlay-claim-decision-input.3",
        "wall_seconds": CLAIMABLE_WALL_SECONDS,
    }
    execution = run_claimable_worker(
        capsule,
        stdin=_json_bytes(worker_input),
    )
    worker_output = _decode_canonical_worker_output(execution.stdout)
    if (
        worker_output.get("schema_version")
        != "clusy.atomic-overlay-frozen-decisions.3"
        or worker_output.get("capsule") != capsule_manifest
        or worker_output.get("baseline_sha256") != baseline_sha256
        or worker_output.get("decision_inputs_sha256") != projection_sha256
    ):
        raise ClaimProtocolError("decision worker output identity mismatch")
    _validate_execution_evidence(worker_output, execution)
    _assert_source_unchanged(source)
    artifact = {
        "baseline_artifact_sha256": baseline_sha256,
        "claimable": True,
        "decision_inputs_sha256": projection_sha256,
        "launcher": asdict(execution.launcher),
        "protocol": {
            "concurrency": CLAIMABLE_CONCURRENCY,
            "dataset_evaluator_scorer_mounted": False,
            "labels_available": False,
            "wall_seconds": CLAIMABLE_WALL_SECONDS,
        },
        "schema_version": DECISION_ARTIFACT_SCHEMA,
        "worker": worker_output,
        "worker_capsule_sha256": dict(execution.capsule_sha256),
        "worker_wall_seconds": execution.wall_seconds,
    }
    if execution.wall_seconds > CLAIMABLE_WALL_SECONDS:
        raise ClaimProtocolError("decision worker exceeded fixed claim wall")
    identity = _write_artifact(output, artifact)
    return {"artifact": identity, "document": artifact}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    probe = subparsers.add_parser("probe")
    probe.set_defaults(command="probe")
    baseline = subparsers.add_parser("baseline")
    baseline.add_argument("--decision-inputs", required=True, type=Path)
    baseline.add_argument("--output", required=True, type=Path)
    decisions = subparsers.add_parser("decisions")
    decisions.add_argument("--decision-inputs", required=True, type=Path)
    decisions.add_argument("--baseline-artifact", required=True, type=Path)
    decisions.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "probe":
            observation = probe_sandbox()
            print(json.dumps(asdict(observation), sort_keys=True))
            return 0 if observation.available else 1
        if args.command == "baseline":
            result = run_baseline(args.decision_inputs, args.output)
        else:
            result = run_decisions(
                args.decision_inputs,
                args.baseline_artifact,
                args.output,
            )
    except (
        ClaimProtocolError,
        ClaimableIOError,
        SandboxExecutionError,
        SandboxUnavailableError,
        SourceInventoryError,
    ) as error:
        print(f"claim protocol error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result["artifact"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

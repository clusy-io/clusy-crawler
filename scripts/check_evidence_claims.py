#!/usr/bin/env python3
"""Fail-closed validation for benchmark evidence and documentation claims."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = Path("bench/evidence/registry.json")
SCHEMA_PATH = Path("bench/evidence/registry.schema.json")

REGISTRY_ID = "clusy.benchmark-evidence-registry.v1"
SCHEMA_ID = "https://clusy.io/schemas/benchmark-evidence-registry-v1.schema.json"
SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
MARKER_FORMAT = "<!-- clusy-evidence: <claim-id> -->"

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_ID_RE = re.compile(r"^[0-9a-f]{40}$")
CLAIM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")
METRIC_KEY_RE = re.compile(r"^[a-z][a-z0-9._-]{1,63}$")
DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
FORMAT_RE = re.compile(r"^\+?\.[0-9]{1,2}f$")
UTC_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
POINTER_RE = re.compile(r"^/(?:[^/~]|~[01])+(?:/(?:[^/~]|~[01])+)*$")
FENCE_RE = re.compile(r"^\s{0,3}(?P<fence>`{3,}|~{3,})")
MARKER_RE = re.compile(r"<!-- clusy-evidence: (?P<id>[a-z0-9][a-z0-9._-]{2,127}) -->")
ANY_MARKER_RE = re.compile(r"<!--\s*clusy-evidence\b.*?-->")
DATE_RE = re.compile(r"(?<![0-9])20[0-9]{2}-[0-9]{2}-[0-9]{2}(?![0-9])")
NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])[-+]?(?:[0-9]+\.[0-9]+|[0-9]+)(?![A-Za-z0-9])")
METRIC_CONTEXT_RE = re.compile(
    r"\b(?:F1|precision|recall|ROUGE(?:-[A-Za-z0-9]+)?|TEDS|pages/s|requests/s|"
    r"p50|p95|p99|latency|throughput|confidence interval|CI|rate change|speedup|"
    r"gain|faster|slower|increased|decreased)\b",
    re.IGNORECASE,
)
SUPERIORITY_RE = re.compile(
    r"\b(?:beats?|outperforms?|wins?|superior|faster\s+than|better\s+than)\b",
    re.IGNORECASE,
)
VENDOR_RE = re.compile(r"\b(?:Exa|Firecrawl)\b", re.IGNORECASE)
POSITIVE_SOTA_RE = re.compile(
    r"(?:\b(?:is|are|was|were|achieves?|achieved|sets?|delivers?)\b.{0,48}"
    r"\b(?:SOTA|state-of-the-art)\b)|"
    r"(?:\b(?:SOTA|state-of-the-art)\b.{0,36}\b(?:performance|result|crawler|extractor)\b)",
    re.IGNORECASE,
)
UNIVERSAL_SOTA_RE = re.compile(
    r"(?:\b(?:universal|all[- ]around|overall)\s+(?:SOTA|state-of-the-art)\b)|"
    r"(?:全方位\s*(?:SOTA|state-of-the-art))",
    re.IGNORECASE,
)
CURRENT_PRODUCTION_RE = re.compile(
    r"\b(?:current production|currently deployed|current revision|100%\s+traffic)\b",
    re.IGNORECASE,
)
NEGATION_RE = re.compile(
    r"\b(?:not|no|never|cannot|can't|does not|is not|without|prohibit(?:s|ed)?|"
    r"forbid(?:s|den)?|future|target|goal|gate)\b",
    re.IGNORECASE,
)
CLAUSE_BREAK_RE = re.compile(
    r"(?:[.!?;]|\b(?:although|but|however|nevertheless|though|yet)\b)",
    re.IGNORECASE,
)

TOP_KEYS = {
    "claims",
    "documentation",
    "registry_id",
    "registry_note",
    "schema",
    "schema_version",
}
DOCUMENTATION_KEYS = {"enforced_files", "marker_format", "migration_status"}
CLAIM_KEYS = {
    "artifact",
    "id",
    "kind",
    "metrics",
    "permissions",
    "protocol",
    "recorded_at_utc",
    "scope",
    "source",
    "status",
    "verification",
}
SOURCE_KEYS = {"clean", "commit", "repository", "runtime_source_sha256", "tree"}
PROTOCOL_KEYS = {"path", "sha256"}
ARTIFACT_KEYS = {
    "path",
    "raw_archive_sha256",
    "raw_manifest_sha256",
    "raw_retention",
    "sha256",
}
SCOPE_KEYS = {
    "comparators",
    "dataset",
    "dataset_revision",
    "dataset_sha256",
    "execution_boundary",
    "limitations",
    "output_contract",
    "pages",
    "profile",
    "split",
    "task",
    "vendors",
}
COMPARATOR_KEYS = {"name", "version"}
METRIC_KEYS = {
    "aggregation",
    "artifact_pointer",
    "display",
    "format",
    "key",
    "unit",
    "value",
}
PERMISSION_KEYS = {
    "current_production",
    "metric",
    "scoped_sota",
    "scoped_superiority",
    "vendor_superiority",
}
GATE_KEYS = {"allowed", "artifact_pointer"}
VERIFICATION_KEYS = {
    "artifact_complete_pointer",
    "binding_pointer",
    "claimable_pointer",
    "source_stable_pointer",
}
BOOLEAN_VERIFICATION_KEYS = VERIFICATION_KEYS - {"binding_pointer"}

STATUSES = {"Verified", "Diagnostic", "Historical", "Rejected"}
KINDS = {
    "benchmark_result",
    "implementation_benchmark",
    "vendor_benchmark",
    "deployment",
    "rejection",
}
UNITS = {
    "ratio",
    "score",
    "percent",
    "pages_per_second",
    "milliseconds",
    "bytes",
    "count",
}
RETENTION = {"repository", "external_retained", "not_retained"}
VENDORS = {"Clusy", "Exa", "Firecrawl"}
GATE_NAMES = ("scoped_superiority", "vendor_superiority", "scoped_sota")


class DuplicateKeyError(ValueError):
    """JSON object contained a duplicate key."""


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_float(value: str) -> Any:
    raise ValueError(f"registry JSON must not contain floating-point literals: {value}")


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON value is forbidden: {value}")


def _read_json(path: Path, *, registry: bool = False) -> Any:
    text = path.read_text(encoding="utf-8")
    return json.loads(
        text,
        object_pairs_hook=_object_without_duplicates,
        parse_float=_reject_float if registry else Decimal,
        parse_constant=_reject_constant,
    )


def _expect_object(
    value: Any,
    expected_keys: set[str],
    where: str,
    errors: list[str],
) -> dict[str, Any] | None:
    if type(value) is not dict:
        errors.append(f"{where}: expected object")
        return None
    actual = set(value)
    missing = sorted(expected_keys - actual)
    unknown = sorted(actual - expected_keys)
    if missing:
        errors.append(f"{where}: missing keys: {', '.join(missing)}")
    if unknown:
        errors.append(f"{where}: unknown keys: {', '.join(unknown)}")
    return value


def _nonempty_string(value: Any, where: str, errors: list[str]) -> str | None:
    if type(value) is not str or not value:
        errors.append(f"{where}: expected non-empty string")
        return None
    return value


def _matches(value: Any, pattern: re.Pattern[str], where: str, errors: list[str]) -> bool:
    if type(value) is not str or pattern.fullmatch(value) is None:
        errors.append(f"{where}: invalid value")
        return False
    return True


def _safe_relative(value: Any, where: str, errors: list[str]) -> str | None:
    if type(value) is not str or not value:
        errors.append(f"{where}: expected non-empty repository-relative path")
        return None
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or "\\" in value
        or "\0" in value
        or value != pure.as_posix()
    ):
        errors.append(f"{where}: unsafe repository-relative path: {value!r}")
        return None
    return value


def _git_visible_paths(root: Path, errors: list[str]) -> set[str]:
    completed = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        errors.append(f"git-visible inventory failed: {detail or completed.returncode}")
        return set()
    try:
        decoded = completed.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        errors.append(f"git-visible inventory is not UTF-8: {exc}")
        return set()
    return {item for item in decoded.split("\0") if item}


def _visible_file(
    root: Path,
    relative: Any,
    visible: set[str],
    where: str,
    errors: list[str],
) -> Path | None:
    safe = _safe_relative(relative, where, errors)
    if safe is None:
        return None
    if safe not in visible:
        errors.append(f"{where}: path is ignored or not git-visible: {safe}")
        return None
    path = root / safe
    cursor = path
    while cursor != root:
        if cursor.is_symlink():
            errors.append(f"{where}: symbolic links are forbidden: {safe}")
            return None
        if cursor.parent == cursor:
            errors.append(f"{where}: path escaped repository: {safe}")
            return None
        cursor = cursor.parent
    if not path.is_file():
        errors.append(f"{where}: path is not a regular file: {safe}")
        return None
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_hash(value: Any, path: Path | None, where: str, errors: list[str]) -> None:
    if not _matches(value, SHA256_RE, where, errors) or path is None:
        return
    actual = _sha256(path)
    if value != actual:
        errors.append(f"{where}: SHA-256 mismatch: registered={value}, actual={actual}")


def _resolve_pointer(document: Any, pointer: str) -> Any:
    current = document
    for encoded in pointer[1:].split("/"):
        token = encoded.replace("~1", "/").replace("~0", "~")
        if type(current) is dict:
            if token not in current:
                raise KeyError(token)
            current = current[token]
        elif type(current) is list:
            if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                raise KeyError(token)
            index = int(token)
            if index >= len(current):
                raise KeyError(token)
            current = current[index]
        else:
            raise KeyError(token)
    return current


def _as_decimal(value: Any) -> Decimal:
    if type(value) is Decimal:
        return value
    if type(value) is int:
        return Decimal(value)
    if type(value) is str and DECIMAL_RE.fullmatch(value):
        return Decimal(value)
    raise InvalidOperation


def _validate_schema(
    root: Path,
    registry: dict[str, Any],
    visible: set[str],
    errors: list[str],
) -> None:
    binding = _expect_object(registry.get("schema"), {"path", "sha256"}, "schema", errors)
    if binding is None:
        return
    if binding.get("path") != SCHEMA_PATH.as_posix():
        errors.append(f"schema.path: must be {SCHEMA_PATH.as_posix()}")
    schema_path = _visible_file(
        root,
        binding.get("path"),
        visible,
        "schema.path",
        errors,
    )
    _verify_hash(binding.get("sha256"), schema_path, "schema.sha256", errors)
    if schema_path is None:
        return
    try:
        schema = _read_json(schema_path)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"schema: could not parse strict JSON: {exc}")
        return
    if type(schema) is not dict:
        errors.append("schema: expected object")
        return
    expected = {
        "$schema": SCHEMA_DIALECT,
        "$id": SCHEMA_ID,
        "type": "object",
        "additionalProperties": False,
    }
    for key, value in expected.items():
        if schema.get(key) != value:
            errors.append(f"schema.{key}: unexpected schema identity or root contract")
    if type(schema.get("$defs")) is not dict or "claim" not in schema["$defs"]:
        errors.append("schema.$defs: claim definition is required")


def _validate_scope(value: Any, where: str, errors: list[str]) -> dict[str, Any] | None:
    scope = _expect_object(value, SCOPE_KEYS, where, errors)
    if scope is None:
        return None
    for key in (
        "task",
        "dataset",
        "dataset_revision",
        "split",
        "profile",
        "output_contract",
        "execution_boundary",
    ):
        _nonempty_string(scope.get(key), f"{where}.{key}", errors)
    _matches(scope.get("dataset_sha256"), SHA256_RE, f"{where}.dataset_sha256", errors)
    if type(scope.get("pages")) is not int or scope["pages"] < 1:
        errors.append(f"{where}.pages: expected positive integer")
    comparators = scope.get("comparators")
    if type(comparators) is not list:
        errors.append(f"{where}.comparators: expected array")
    else:
        seen_comparators: set[tuple[str, str]] = set()
        for index, item in enumerate(comparators):
            location = f"{where}.comparators[{index}]"
            comparator = _expect_object(item, COMPARATOR_KEYS, location, errors)
            if comparator is None:
                continue
            name = _nonempty_string(comparator.get("name"), f"{location}.name", errors)
            version = _nonempty_string(comparator.get("version"), f"{location}.version", errors)
            if name is not None and version is not None:
                identity = (name, version)
                if identity in seen_comparators:
                    errors.append(f"{location}: duplicate comparator identity")
                seen_comparators.add(identity)
    vendors = scope.get("vendors")
    if type(vendors) is not list:
        errors.append(f"{where}.vendors: expected array")
    else:
        if len(vendors) != len(set(item for item in vendors if type(item) is str)):
            errors.append(f"{where}.vendors: duplicate vendor")
        for index, vendor in enumerate(vendors):
            if vendor not in VENDORS:
                errors.append(f"{where}.vendors[{index}]: unsupported vendor")
    limitations = scope.get("limitations")
    if type(limitations) is not list or not limitations:
        errors.append(f"{where}.limitations: expected non-empty array")
    else:
        for index, limitation in enumerate(limitations):
            _nonempty_string(limitation, f"{where}.limitations[{index}]", errors)
    return scope


def _validate_metrics(
    value: Any,
    artifact: Any,
    where: str,
    errors: list[str],
) -> list[dict[str, Any]]:
    if type(value) is not list or not value:
        errors.append(f"{where}: expected non-empty array")
        return []
    metrics: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_pointers: set[str] = set()
    for index, item in enumerate(value):
        location = f"{where}[{index}]"
        metric = _expect_object(item, METRIC_KEYS, location, errors)
        if metric is None:
            continue
        key = metric.get("key")
        if _matches(key, METRIC_KEY_RE, f"{location}.key", errors):
            if key in seen_keys:
                errors.append(f"{location}.key: duplicate metric key")
            seen_keys.add(key)
        raw_value = metric.get("value")
        valid_value = _matches(raw_value, DECIMAL_RE, f"{location}.value", errors)
        display = _nonempty_string(metric.get("display"), f"{location}.display", errors)
        format_spec = metric.get("format")
        valid_format = _matches(format_spec, FORMAT_RE, f"{location}.format", errors)
        _nonempty_string(metric.get("aggregation"), f"{location}.aggregation", errors)
        if metric.get("unit") not in UNITS:
            errors.append(f"{location}.unit: unsupported unit")
        pointer = metric.get("artifact_pointer")
        if _matches(pointer, POINTER_RE, f"{location}.artifact_pointer", errors):
            if pointer in seen_pointers:
                errors.append(f"{location}.artifact_pointer: duplicate pointer")
            seen_pointers.add(pointer)
            if artifact is not None:
                try:
                    artifact_value = _resolve_pointer(artifact, pointer)
                except KeyError:
                    errors.append(f"{location}.artifact_pointer: target does not exist")
                else:
                    if valid_value:
                        try:
                            if _as_decimal(artifact_value) != Decimal(raw_value):
                                errors.append(
                                    f"{location}: registered value differs from artifact target"
                                )
                        except InvalidOperation:
                            errors.append(f"{location}.artifact_pointer: target is not decimal")
        if valid_value and valid_format and display is not None:
            try:
                rendered = format(Decimal(raw_value), format_spec)
            except (InvalidOperation, ValueError) as exc:
                errors.append(f"{location}: could not format decimal: {exc}")
            else:
                if rendered != display:
                    errors.append(
                        f"{location}.display: expected {rendered!r} from value and format"
                    )
        metrics.append(metric)
    return metrics


def _validate_permissions(
    value: Any,
    artifact: Any,
    claim: dict[str, Any],
    scope: dict[str, Any] | None,
    where: str,
    errors: list[str],
) -> dict[str, Any] | None:
    permissions = _expect_object(value, PERMISSION_KEYS, where, errors)
    if permissions is None:
        return None
    if type(permissions.get("metric")) is not bool:
        errors.append(f"{where}.metric: expected boolean")
    if permissions.get("current_production") is not False:
        errors.append(f"{where}.current_production: versioned documentation forbids this claim")
    gates: dict[str, dict[str, Any]] = {}
    for name in GATE_NAMES:
        location = f"{where}.{name}"
        gate = _expect_object(permissions.get(name), GATE_KEYS, location, errors)
        if gate is None:
            continue
        allowed = gate.get("allowed")
        pointer = gate.get("artifact_pointer")
        if type(allowed) is not bool:
            errors.append(f"{location}.allowed: expected boolean")
            continue
        if allowed:
            if not _matches(pointer, POINTER_RE, f"{location}.artifact_pointer", errors):
                continue
            if artifact is not None:
                try:
                    target = _resolve_pointer(artifact, pointer)
                except KeyError:
                    errors.append(f"{location}.artifact_pointer: target does not exist")
                else:
                    if target is not True:
                        errors.append(f"{location}: artifact gate is not exactly true")
        elif pointer is not None:
            errors.append(f"{location}.artifact_pointer: must be null when gate is closed")
        gates[name] = gate
    status = claim.get("status")
    if status in {"Diagnostic", "Rejected"}:
        for name, gate in gates.items():
            if gate.get("allowed") is True:
                errors.append(f"{where}.{name}: {status} claims cannot open superiority gates")
    vendor_gate = gates.get("vendor_superiority", {})
    if vendor_gate.get("allowed") is True:
        if status != "Verified" or claim.get("kind") != "vendor_benchmark":
            errors.append(f"{where}.vendor_superiority: requires Verified vendor_benchmark")
        scoped_vendors = set(scope.get("vendors", [])) if scope is not None else set()
        if not scoped_vendors.intersection({"Exa", "Firecrawl"}):
            errors.append(f"{where}.vendor_superiority: scope names no external vendor")
    sota_gate = gates.get("scoped_sota", {})
    if sota_gate.get("allowed") is True and status != "Verified":
        errors.append(f"{where}.scoped_sota: requires Verified status")
    return permissions


def _validate_verification(
    value: Any,
    artifact: Any,
    claim: dict[str, Any],
    where: str,
    errors: list[str],
) -> None:
    verification = _expect_object(value, VERIFICATION_KEYS, where, errors)
    if verification is None:
        return
    binding_pointer = verification.get("binding_pointer")
    binding_valid = _matches(
        binding_pointer,
        POINTER_RE,
        f"{where}.binding_pointer",
        errors,
    )
    if binding_valid and artifact is not None:
        try:
            binding = _resolve_pointer(artifact, binding_pointer)
        except KeyError:
            errors.append(f"{where}.binding_pointer: target does not exist")
        else:
            artifact_record = claim.get("artifact")
            protocol_record = claim.get("protocol")
            expected_binding = {
                "id": claim.get("id"),
                "kind": claim.get("kind"),
                "raw_archive_sha256": (
                    artifact_record.get("raw_archive_sha256")
                    if type(artifact_record) is dict
                    else None
                ),
                "raw_manifest_sha256": (
                    artifact_record.get("raw_manifest_sha256")
                    if type(artifact_record) is dict
                    else None
                ),
                "recorded_at_utc": claim.get("recorded_at_utc"),
                "scope": claim.get("scope"),
                "source": claim.get("source"),
                "status": claim.get("status"),
                "protocol_sha256": (
                    protocol_record.get("sha256")
                    if type(protocol_record) is dict
                    else None
                ),
            }
            if binding != expected_binding:
                errors.append(f"{where}.binding_pointer: artifact claim binding is not exact")
    resolved: dict[str, Any] = {}
    for key in sorted(BOOLEAN_VERIFICATION_KEYS):
        pointer = verification.get(key)
        if not _matches(pointer, POINTER_RE, f"{where}.{key}", errors):
            continue
        if artifact is None:
            continue
        try:
            target = _resolve_pointer(artifact, pointer)
        except KeyError:
            errors.append(f"{where}.{key}: target does not exist")
            continue
        if type(target) is not bool:
            errors.append(f"{where}.{key}: target is not a boolean")
            continue
        resolved[key] = target
    status = claim.get("status")
    if status == "Verified":
        for key in BOOLEAN_VERIFICATION_KEYS:
            if resolved.get(key) is not True:
                errors.append(f"{where}.{key}: Verified claim requires an exact true gate")
    if status in {"Diagnostic", "Rejected"} and resolved.get("claimable_pointer") is not False:
        errors.append(f"{where}.claimable_pointer: {status} claim requires an exact false gate")


def _validate_claim(
    claim_value: Any,
    index: int,
    root: Path,
    visible: set[str],
    errors: list[str],
) -> dict[str, Any] | None:
    where = f"claims[{index}]"
    claim = _expect_object(claim_value, CLAIM_KEYS, where, errors)
    if claim is None:
        return None
    _matches(claim.get("id"), CLAIM_ID_RE, f"{where}.id", errors)
    status = claim.get("status")
    if status not in STATUSES:
        errors.append(f"{where}.status: unsupported status")
    kind = claim.get("kind")
    if kind not in KINDS:
        errors.append(f"{where}.kind: unsupported kind")
    if (status == "Rejected") != (kind == "rejection"):
        errors.append(f"{where}: Rejected status and rejection kind must appear together")
    timestamp = claim.get("recorded_at_utc")
    if _matches(timestamp, UTC_RE, f"{where}.recorded_at_utc", errors):
        try:
            dt.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            errors.append(f"{where}.recorded_at_utc: invalid calendar timestamp")

    source = _expect_object(claim.get("source"), SOURCE_KEYS, f"{where}.source", errors)
    if source is not None:
        _nonempty_string(source.get("repository"), f"{where}.source.repository", errors)
        _matches(source.get("commit"), GIT_ID_RE, f"{where}.source.commit", errors)
        _matches(source.get("tree"), GIT_ID_RE, f"{where}.source.tree", errors)
        _matches(
            source.get("runtime_source_sha256"),
            SHA256_RE,
            f"{where}.source.runtime_source_sha256",
            errors,
        )
        if type(source.get("clean")) is not bool:
            errors.append(f"{where}.source.clean: expected boolean")
        if status == "Verified" and source.get("clean") is not True:
            errors.append(f"{where}.source.clean: Verified claims require clean source")

    protocol = _expect_object(
        claim.get("protocol"),
        PROTOCOL_KEYS,
        f"{where}.protocol",
        errors,
    )
    if protocol is not None:
        protocol_path = _visible_file(
            root,
            protocol.get("path"),
            visible,
            f"{where}.protocol.path",
            errors,
        )
        if type(protocol.get("path")) is str and not protocol["path"].startswith(
            "bench/evidence/"
        ):
            errors.append(f"{where}.protocol.path: frozen protocol must be under bench/evidence")
        if protocol.get("path") in {REGISTRY_PATH.as_posix(), SCHEMA_PATH.as_posix()}:
            errors.append(f"{where}.protocol.path: registry control files are not evidence")
        _verify_hash(
            protocol.get("sha256"),
            protocol_path,
            f"{where}.protocol.sha256",
            errors,
        )

    artifact_document: Any = None
    artifact = _expect_object(
        claim.get("artifact"),
        ARTIFACT_KEYS,
        f"{where}.artifact",
        errors,
    )
    if artifact is not None:
        artifact_path = _visible_file(
            root,
            artifact.get("path"),
            visible,
            f"{where}.artifact.path",
            errors,
        )
        if type(artifact.get("path")) is str and not artifact["path"].startswith(
            "bench/evidence/"
        ):
            errors.append(f"{where}.artifact.path: compact artifact must be under bench/evidence")
        if artifact.get("path") in {REGISTRY_PATH.as_posix(), SCHEMA_PATH.as_posix()}:
            errors.append(f"{where}.artifact.path: registry control files are not evidence")
        if type(artifact.get("path")) is str and not artifact["path"].endswith(".json"):
            errors.append(f"{where}.artifact.path: compact artifact must be JSON")
        _verify_hash(
            artifact.get("sha256"),
            artifact_path,
            f"{where}.artifact.sha256",
            errors,
        )
        for key in ("raw_manifest_sha256", "raw_archive_sha256"):
            value = artifact.get(key)
            if value is not None:
                _matches(value, SHA256_RE, f"{where}.artifact.{key}", errors)
        retention = artifact.get("raw_retention")
        if retention not in RETENTION:
            errors.append(f"{where}.artifact.raw_retention: unsupported value")
        if retention == "not_retained" and (
            artifact.get("raw_manifest_sha256") is not None
            or artifact.get("raw_archive_sha256") is not None
        ):
            errors.append(f"{where}.artifact: not_retained requires null raw hashes")
        if retention in {"repository", "external_retained"} and artifact.get(
            "raw_manifest_sha256"
        ) is None:
            errors.append(f"{where}.artifact: retained evidence requires raw manifest hash")
        if retention == "external_retained" and artifact.get("raw_archive_sha256") is None:
            errors.append(f"{where}.artifact: external evidence requires raw archive hash")
        if status == "Verified" and retention == "not_retained":
            errors.append(f"{where}.artifact: Verified claims require retained raw evidence")
        if artifact_path is not None:
            try:
                artifact_document = _read_json(artifact_path)
            except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"{where}.artifact.path: invalid strict JSON: {exc}")

    scope = _validate_scope(claim.get("scope"), f"{where}.scope", errors)
    _validate_metrics(claim.get("metrics"), artifact_document, f"{where}.metrics", errors)
    _validate_permissions(
        claim.get("permissions"),
        artifact_document,
        claim,
        scope,
        f"{where}.permissions",
        errors,
    )
    _validate_verification(
        claim.get("verification"),
        artifact_document,
        claim,
        f"{where}.verification",
        errors,
    )
    return claim


def _outside_fences(text: str) -> list[tuple[int, str]]:
    visible: list[tuple[int, str]] = []
    active_char = ""
    active_length = 0
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = FENCE_RE.match(line)
        if match:
            fence = match.group("fence")
            if not active_char:
                active_char = fence[0]
                active_length = len(fence)
            elif fence[0] == active_char and len(fence) >= active_length:
                active_char = ""
                active_length = 0
            continue
        if not active_char:
            visible.append((line_number, line))
    return visible


def _claim_allowed_tokens(claim: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()

    def collect(value: Any) -> None:
        if type(value) is str:
            tokens.update(DATE_RE.findall(value))
            without_dates = DATE_RE.sub("", value)
            tokens.update(NUMBER_RE.findall(without_dates))
        elif type(value) is int:
            tokens.add(str(value))
        elif type(value) is list:
            for item in value:
                collect(item)
        elif type(value) is dict:
            for item in value.values():
                collect(item)

    collect(claim)
    for metric in claim.get("metrics", []):
        if type(metric) is dict and type(metric.get("display")) is str:
            tokens.add(metric["display"])
    return tokens


def _line_tokens(line: str) -> set[str]:
    without_markers = ANY_MARKER_RE.sub("", line)
    dates = set(DATE_RE.findall(without_markers))
    without_dates = DATE_RE.sub("", without_markers)
    return dates | set(NUMBER_RE.findall(without_dates))


def _negated(line: str, match: re.Match[str]) -> bool:
    prefix = line[max(0, match.start() - 160) : match.start()]
    boundaries = list(CLAUSE_BREAK_RE.finditer(prefix))
    clause_prefix = prefix[boundaries[-1].end() :] if boundaries else prefix
    return NEGATION_RE.search(clause_prefix) is not None


def _gate_allowed(permissions: dict[str, Any], name: str) -> bool:
    gate = permissions.get(name)
    return type(gate) is dict and gate.get("allowed") is True


def _validate_claim_line(
    relative: str,
    line_number: int,
    line: str,
    claim: dict[str, Any],
    errors: list[str],
) -> None:
    where = f"{relative}:{line_number}"
    status = claim.get("status")
    recorded_at = claim.get("recorded_at_utc")
    permissions = claim.get("permissions")
    scope = claim.get("scope")
    if (
        type(status) is not str
        or type(recorded_at) is not str
        or type(permissions) is not dict
        or type(scope) is not dict
    ):
        errors.append(f"{where}: referenced claim is structurally invalid")
        return
    needs_visible_status = status in {"Diagnostic", "Historical", "Rejected"}
    if needs_visible_status and status.casefold() not in line.casefold():
        errors.append(f"{where}: {status} claim must show its status on the claim line")
    recorded_date = recorded_at[:10]
    if status == "Historical" and recorded_date not in line:
        errors.append(f"{where}: Historical claim must show recorded date {recorded_date}")

    allowed_tokens = _claim_allowed_tokens(claim)
    unknown_tokens = sorted(_line_tokens(line) - allowed_tokens)
    if unknown_tokens:
        errors.append(
            f"{where}: numeric/date tokens are not registered for {claim['id']}: "
            f"{', '.join(unknown_tokens)}"
        )

    if (
        METRIC_CONTEXT_RE.search(line)
        and NUMBER_RE.search(line)
        and permissions.get("metric") is not True
    ):
        errors.append(f"{where}: metric publication is not permitted")
    for match in SUPERIORITY_RE.finditer(line):
        if _negated(line, match):
            continue
        if not _gate_allowed(permissions, "scoped_superiority"):
            errors.append(f"{where}: positive superiority language is not permitted")
        if VENDOR_RE.search(line) and not _gate_allowed(permissions, "vendor_superiority"):
            errors.append(f"{where}: positive vendor superiority language is not permitted")
    for match in POSITIVE_SOTA_RE.finditer(line):
        if _negated(line, match):
            continue
        if not _gate_allowed(permissions, "scoped_sota"):
            errors.append(f"{where}: positive SOTA language is not permitted")
            continue
        dataset = scope.get("dataset")
        task = scope.get("task")
        if (
            type(dataset) is not str
            or type(task) is not str
            or recorded_date not in line
            or (
                dataset.casefold() not in line.casefold()
                and task.casefold() not in line.casefold()
            )
        ):
            errors.append(f"{where}: scoped SOTA must name task/dataset and recorded date")


def _validate_documentation(
    root: Path,
    registry: dict[str, Any],
    claims_by_id: dict[str, dict[str, Any]],
    visible: set[str],
    errors: list[str],
) -> None:
    documentation = _expect_object(
        registry.get("documentation"),
        DOCUMENTATION_KEYS,
        "documentation",
        errors,
    )
    if documentation is None:
        return
    status = documentation.get("migration_status")
    if status not in {"pending", "enforced"}:
        errors.append("documentation.migration_status: unsupported value")
    if documentation.get("marker_format") != MARKER_FORMAT:
        errors.append("documentation.marker_format: unsupported marker contract")
    enforced = documentation.get("enforced_files")
    if type(enforced) is not list:
        errors.append("documentation.enforced_files: expected array")
        enforced = []
    elif len(enforced) != len(set(item for item in enforced if type(item) is str)):
        errors.append("documentation.enforced_files: duplicate path")
    if status == "pending" and enforced:
        errors.append("documentation.enforced_files: pending migration requires an empty list")
    if status == "enforced":
        required = {"README.md", "docs/BENCHMARKS.md"}
        if not required.issubset(set(item for item in enforced if type(item) is str)):
            errors.append(
                "documentation.enforced_files: enforced mode requires README.md and "
                "docs/BENCHMARKS.md"
            )

    markdown = sorted(
        relative
        for relative in visible
        if relative.endswith(".md") and not relative.startswith("native/vendor/")
    )
    for relative in markdown:
        path = root / relative
        if not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            errors.append(f"{relative}: invalid UTF-8 while scanning evidence markers: {exc}")
            continue
        for line_number, line in _outside_fences(text):
            marker_tokens = ANY_MARKER_RE.findall(line)
            valid_markers = list(MARKER_RE.finditer(line))
            if len(marker_tokens) != len(valid_markers):
                errors.append(f"{relative}:{line_number}: malformed evidence marker")
            if len(valid_markers) > 1:
                errors.append(f"{relative}:{line_number}: at most one evidence marker is allowed")
            for marker in valid_markers:
                if marker.group("id") not in claims_by_id:
                    errors.append(
                        f"{relative}:{line_number}: unknown evidence claim {marker.group('id')}"
                    )

    for item in enforced:
        path = _visible_file(root, item, visible, "documentation.enforced_files", errors)
        if path is None:
            continue
        if path.suffix.lower() != ".md":
            errors.append(f"documentation.enforced_files: not Markdown: {item}")
            continue
        text = path.read_text(encoding="utf-8")
        for line_number, line in _outside_fences(text):
            markers = list(MARKER_RE.finditer(line))
            marker = markers[0] if len(markers) == 1 else None
            superiority = next(
                (match for match in SUPERIORITY_RE.finditer(line) if not _negated(line, match)),
                None,
            )
            sota = next(
                (match for match in POSITIVE_SOTA_RE.finditer(line) if not _negated(line, match)),
                None,
            )
            metric_claim = bool(METRIC_CONTEXT_RE.search(line) and NUMBER_RE.search(line))
            needs_marker = metric_claim or superiority is not None or sota is not None
            where = f"{item}:{line_number}"
            if UNIVERSAL_SOTA_RE.search(line):
                errors.append(f"{where}: universal/all-around SOTA claims are forbidden")
            if CURRENT_PRODUCTION_RE.search(line):
                errors.append(f"{where}: current-production claims are forbidden in versioned docs")
            if needs_marker and marker is None:
                errors.append(f"{where}: evidence-bearing line requires exactly one marker")
                continue
            if marker is not None and marker.group("id") in claims_by_id:
                _validate_claim_line(
                    item,
                    line_number,
                    line,
                    claims_by_id[marker.group("id")],
                    errors,
                )


def validate_repository(root: Path) -> list[str]:
    """Return every validation failure for one repository."""

    root = root.resolve()
    errors: list[str] = []
    visible = _git_visible_paths(root, errors)
    registry_file = _visible_file(
        root,
        REGISTRY_PATH.as_posix(),
        visible,
        "registry",
        errors,
    )
    if registry_file is None:
        return errors
    try:
        registry = _read_json(registry_file, registry=True)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"registry: could not parse strict JSON: {exc}")
        return errors
    registry_object = _expect_object(registry, TOP_KEYS, "registry", errors)
    if registry_object is None:
        return errors
    canonical = json.dumps(registry_object, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if registry_file.read_text(encoding="utf-8") != canonical:
        errors.append("registry: JSON must use canonical sorted two-space formatting")
    if type(registry_object.get("schema_version")) is not int or registry_object.get(
        "schema_version"
    ) != 1:
        errors.append("registry.schema_version: expected integer 1")
    if registry_object.get("registry_id") != REGISTRY_ID:
        errors.append(f"registry.registry_id: expected {REGISTRY_ID}")
    _nonempty_string(registry_object.get("registry_note"), "registry.registry_note", errors)
    _validate_schema(root, registry_object, visible, errors)

    claims_value = registry_object.get("claims")
    claims: list[dict[str, Any]] = []
    if type(claims_value) is not list:
        errors.append("registry.claims: expected array")
    else:
        seen_ids: set[str] = set()
        for index, claim_value in enumerate(claims_value):
            claim = _validate_claim(claim_value, index, root, visible, errors)
            if claim is None or type(claim.get("id")) is not str:
                continue
            claim_id = claim["id"]
            if claim_id in seen_ids:
                errors.append(f"claims[{index}].id: duplicate claim id: {claim_id}")
            seen_ids.add(claim_id)
            claims.append(claim)
    claims_by_id = {
        claim["id"]: claim
        for claim in claims
        if set(claim) == CLAIM_KEYS
        and type(claim.get("id")) is str
        and CLAIM_ID_RE.fullmatch(claim["id"])
    }
    _validate_documentation(root, registry_object, claims_by_id, visible, errors)
    return errors


def main() -> int:
    errors = validate_repository(ROOT)
    if errors:
        print("Evidence claim validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    registry = _read_json(ROOT / REGISTRY_PATH, registry=True)
    print(
        "Evidence claim validation passed: "
        f"{len(registry['claims'])} registered claims; "
        f"documentation migration={registry['documentation']['migration_status']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

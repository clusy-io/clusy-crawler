#!/usr/bin/env python3
"""Label-free Trafilatura 2.1.0 replay worker for the pinned AEB HTML capsule.

This process deliberately has no evaluator, label, or Clusy import path.  Its
only input is a controller-built capsule containing the exact tracked AEB
``html/*.html.gz`` files and their cryptographic inventory.  It emits
predictions plus a receipt; scoring happens later in the controller.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import gzip
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import site
import stat
import sys
import time
from pathlib import Path
from typing import Any

AEB_COMMIT = "4a3bc979f76c0df73cb95fe272e2fc1b96f9f010"
AEB_TREE = "258fee1bb38bcb642afec48cb80e51bd1594c259"
AEB_PAGES = 181
EXPECTED_PACKAGE = "trafilatura"
EXPECTED_VERSION = "2.1.0"
INPUT_SCHEMA = "clusy.aeb.current-oss.input-capsule.v1"
RESULT_SCHEMA = "clusy.aeb.current-oss.worker-result.v2"
CONFIG = {
    "callable": "trafilatura.extract",
    "keyword_arguments": {"include_comments": False},
    "positional_arguments": ["decoded_html"],
}
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_RESULT_BYTES = 128 * 1024 * 1024
MAX_COMPRESSED_PAGE_BYTES = 32 * 1024 * 1024
MAX_DECODED_PAGE_BYTES = 128 * 1024 * 1024
MAX_DISTRIBUTION_FILE_BYTES = 256 * 1024 * 1024
MAX_RECORD_BYTES = 16 * 1024 * 1024
MAX_SITE_FILE_BYTES = 256 * 1024 * 1024
MAX_PYVENV_CFG_BYTES = 16 * 1024
_HEX64 = frozenset("0123456789abcdef")
_FORBIDDEN_COMPONENTS = frozenset(
    {
        "evaluate.py",
        "ground-truth.json",
        "ground_truth.json",
        "output",
        "outputs",
        "prediction",
        "predictions",
        "reference",
        "references",
    }
)
_NORMALIZE_DISTRIBUTION = re.compile(r"[-_.]+")


class WorkerError(RuntimeError):
    """The replay cannot produce a provenance-valid result."""


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _hash_json(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


CONFIG_SHA256 = _hash_json(CONFIG)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path, *, maximum_bytes: int | None = None) -> str:
    digest = hashlib.sha256()
    consumed = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            consumed += len(chunk)
            if maximum_bytes is not None and consumed > maximum_bytes:
                raise WorkerError(f"file exceeds byte cap: {path.name}")
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX64 for character in value)
    )


def _require_exact_keys(value: Any, expected: set[str], *, context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise WorkerError(f"{context} schema mismatch")
    return value


def _safe_key(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 200:
        raise WorkerError("input key is invalid")
    if value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise WorkerError("input key is not a safe filename stem")
    return value


def _regular_file(path: Path, *, context: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise WorkerError(f"{context} is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise WorkerError(f"{context} must be a regular non-symlink file")
    if metadata.st_nlink != 1:
        raise WorkerError(f"{context} must have exactly one hard link")
    return metadata


def _capsule_files(capsule: Path) -> set[str]:
    try:
        capsule_metadata = capsule.lstat()
    except OSError as error:
        raise WorkerError("input capsule is unavailable") from error
    if stat.S_ISLNK(capsule_metadata.st_mode) or not stat.S_ISDIR(capsule_metadata.st_mode):
        raise WorkerError("input capsule must be a real directory")

    files: set[str] = set()
    directories: set[str] = set()
    for root, child_directories, child_files in os.walk(capsule, followlinks=False):
        root_path = Path(root)
        for name in child_directories:
            path = root_path / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise WorkerError("input capsule contains a linked or special directory")
            directories.add(path.relative_to(capsule).as_posix())
        for name in child_files:
            path = root_path / name
            _regular_file(path, context="input capsule member")
            relative = path.relative_to(capsule).as_posix()
            lowered = {component.lower() for component in Path(relative).parts}
            if lowered & _FORBIDDEN_COMPONENTS:
                raise WorkerError("labels, evaluator, or predictions are visible in input capsule")
            files.add(relative)
    if directories != {"html"}:
        raise WorkerError("input capsule directory layout mismatch")
    return files


def _load_capsule(
    capsule: Path,
    *,
    expected_manifest_sha256: str,
    expected_inventory_sha256: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not _is_sha256(expected_manifest_sha256) or not _is_sha256(expected_inventory_sha256):
        raise WorkerError("controller did not provide valid input commitments")
    actual_files = _capsule_files(capsule)
    manifest_path = capsule / "input-manifest.json"
    manifest_metadata = _regular_file(manifest_path, context="input manifest")
    if manifest_metadata.st_size > MAX_MANIFEST_BYTES:
        raise WorkerError("input manifest exceeds byte cap")
    if _sha256_file(manifest_path) != expected_manifest_sha256:
        raise WorkerError("input manifest SHA-256 mismatch")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkerError("input manifest is not valid UTF-8 JSON") from error
    manifest = _require_exact_keys(
        manifest,
        {
            "dataset",
            "inventory",
            "schema",
            "schema_version",
            "upstream_runner",
        },
        context="input manifest",
    )
    if manifest["schema"] != INPUT_SCHEMA or manifest["schema_version"] != 1:
        raise WorkerError("input manifest identity mismatch")
    dataset = _require_exact_keys(
        manifest["dataset"],
        {"commit", "repository", "tree"},
        context="input dataset",
    )
    if (
        dataset["commit"] != AEB_COMMIT
        or dataset["tree"] != AEB_TREE
        or dataset["repository"]
        != "https://github.com/scrapinghub/article-extraction-benchmark.git"
    ):
        raise WorkerError("input dataset is not the pinned AEB revision")
    upstream_runner = _require_exact_keys(
        manifest["upstream_runner"],
        {"git_blob_oid", "path", "sha256"},
        context="upstream runner",
    )
    if (
        upstream_runner["path"] != "extractors/run_trafilatura.py"
        or not isinstance(upstream_runner["git_blob_oid"], str)
        or not _is_sha256(upstream_runner["sha256"])
    ):
        raise WorkerError("upstream runner identity is invalid")

    inventory = _require_exact_keys(
        manifest["inventory"],
        {
            "commitment_sha256",
            "items",
            "ordering",
            "pages",
        },
        context="input inventory",
    )
    items = inventory["items"]
    if (
        inventory["pages"] != AEB_PAGES
        or inventory["ordering"] != "UTF-8 bytewise key order"
        or not isinstance(items, list)
        or len(items) != AEB_PAGES
        or inventory["commitment_sha256"] != expected_inventory_sha256
        or _hash_json(items) != expected_inventory_sha256
    ):
        raise WorkerError("input inventory commitment mismatch")

    expected_files = {"input-manifest.json"}
    keys: list[str] = []
    normalized_items: list[dict[str, Any]] = []
    for index, raw_item in enumerate(items):
        item = _require_exact_keys(
            raw_item,
            {
                "compressed_bytes",
                "compressed_sha256",
                "decoded_bytes",
                "decoded_sha256",
                "git_blob_oid",
                "key",
                "path",
            },
            context=f"input inventory item {index}",
        )
        key = _safe_key(item["key"])
        expected_path = f"html/{key}.html.gz"
        if item["path"] != expected_path:
            raise WorkerError("input inventory path/key mismatch")
        if (
            not isinstance(item["git_blob_oid"], str)
            or len(item["git_blob_oid"]) not in {40, 64}
            or not _is_sha256(item["compressed_sha256"])
            or not _is_sha256(item["decoded_sha256"])
            or not isinstance(item["compressed_bytes"], int)
            or isinstance(item["compressed_bytes"], bool)
            or not 0 < item["compressed_bytes"] <= MAX_COMPRESSED_PAGE_BYTES
            or not isinstance(item["decoded_bytes"], int)
            or isinstance(item["decoded_bytes"], bool)
            or not 0 < item["decoded_bytes"] <= MAX_DECODED_PAGE_BYTES
        ):
            raise WorkerError("input inventory item value is invalid")
        keys.append(key)
        expected_files.add(expected_path)
        normalized_items.append(item)
    if keys != sorted(keys, key=lambda value: value.encode("utf-8")):
        raise WorkerError("input inventory ordering mismatch")
    if len(keys) != len(set(keys)):
        raise WorkerError("input inventory contains duplicate keys")
    if actual_files != expected_files:
        raise WorkerError("input capsule has missing or extra files")
    for item in normalized_items:
        _load_html(capsule / item["path"], item)
    return manifest, normalized_items


def _normalized_distribution_name(value: str) -> str:
    normalized = _NORMALIZE_DISTRIBUTION.sub("-", value).lower()
    if not normalized or normalized.startswith("-") or normalized.endswith("-"):
        raise WorkerError("runtime distribution name is invalid")
    return normalized


def _record_rows(
    distribution: importlib.metadata.Distribution,
) -> tuple[Path, list[list[str]]]:
    record_entries = [
        entry
        for entry in distribution.files or ()
        if Path(str(entry)).name == "RECORD" and ".dist-info/" in str(entry).replace("\\", "/")
    ]
    if len(record_entries) != 1:
        raise WorkerError("runtime distribution lacks a unique RECORD")
    record_path = Path(str(distribution.locate_file(record_entries[0])))
    metadata = _regular_file(record_path, context="runtime distribution RECORD")
    if metadata.st_size > MAX_RECORD_BYTES:
        raise WorkerError("runtime distribution RECORD exceeds byte cap")
    try:
        text = record_path.read_text(encoding="utf-8")
        rows = list(csv.reader(text.splitlines()))
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        raise WorkerError("runtime distribution RECORD is invalid") from error
    if not rows or any(len(row) != 3 for row in rows):
        raise WorkerError("runtime distribution RECORD row is malformed")
    return record_path, rows


def _record_digest(encoded: str) -> bytes:
    algorithm, separator, digest = encoded.partition("=")
    if algorithm != "sha256" or separator != "=" or not digest:
        raise WorkerError("runtime distribution RECORD hash is not SHA-256")
    try:
        decoded = base64.b64decode(
            digest + "=" * (-len(digest) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError, binascii.Error) as error:
        raise WorkerError("runtime distribution RECORD hash is invalid") from error
    if len(decoded) != 32:
        raise WorkerError("runtime distribution RECORD hash has invalid length")
    return decoded


def _distribution_identity(
    distribution: importlib.metadata.Distribution,
) -> dict[str, Any]:
    version = distribution.version
    raw_name = distribution.metadata.get("Name")
    metadata_version = distribution.metadata.get("Version")
    if not isinstance(raw_name, str) or not raw_name:
        raise WorkerError("runtime distribution has no metadata name")
    name = _normalized_distribution_name(raw_name)
    if not isinstance(version, str) or not version or metadata_version != version:
        raise WorkerError(f"runtime distribution metadata mismatch for {name}")

    record_path, record_rows = _record_rows(distribution)
    prefix = Path(sys.prefix).resolve()
    inventory: list[dict[str, Any]] = []
    metadata_entries: dict[str, str] = {}
    seen_record_paths: set[str] = set()
    for raw_record_path, encoded_hash, encoded_size in record_rows:
        record_entry = raw_record_path.replace("\\", "/")
        if (
            not record_entry
            or "\x00" in record_entry
            or record_entry.startswith("/")
            or record_entry in seen_record_paths
        ):
            raise WorkerError(f"runtime distribution RECORD path is invalid for {name}")
        seen_record_paths.add(record_entry)
        basename = Path(record_entry).name
        if basename == "RECORD":
            if (
                Path(str(distribution.locate_file(raw_record_path))).resolve()
                != record_path.resolve()
            ):
                raise WorkerError(f"runtime distribution RECORD self-entry mismatch for {name}")
            if encoded_hash or encoded_size:
                raise WorkerError(f"runtime distribution RECORD self-hash is not empty for {name}")
            continue
        digest = _record_digest(encoded_hash)
        if not encoded_size.isdecimal():
            raise WorkerError(f"runtime distribution RECORD size is invalid for {name}")
        expected_size = int(encoded_size)
        located = Path(str(distribution.locate_file(raw_record_path)))
        metadata = _regular_file(located, context=f"runtime distribution member for {name}")
        try:
            relative = located.resolve().relative_to(prefix).as_posix()
        except ValueError as error:
            raise WorkerError(
                f"runtime distribution member escapes interpreter prefix for {name}"
            ) from error
        actual_digest = bytes.fromhex(
            _sha256_file(located, maximum_bytes=MAX_DISTRIBUTION_FILE_BYTES)
        )
        if metadata.st_size != expected_size or actual_digest != digest:
            raise WorkerError(f"runtime distribution RECORD verification failed for {name}")
        digest_hex = actual_digest.hex()
        if basename in {"METADATA", "WHEEL", "INSTALLER", "direct_url.json"}:
            if basename in metadata_entries:
                raise WorkerError(f"runtime distribution has duplicate {basename}")
            metadata_entries[basename] = digest_hex
        if record_entry.startswith("../../../bin/") or basename in {
            "INSTALLER",
            "REQUESTED",
            "direct_url.json",
        }:
            continue
        inventory.append(
            {
                "bytes": metadata.st_size,
                "path_from_prefix": relative,
                "record_path": record_entry,
                "sha256": digest_hex,
            }
        )
    inventory.sort(key=lambda item: item["record_path"].encode("utf-8"))
    if not {"METADATA", "WHEEL"} <= metadata_entries.keys():
        raise WorkerError(f"runtime distribution lacks METADATA or WHEEL for {name}")
    return {
        "distribution_inventory": inventory,
        "distribution_inventory_sha256": _hash_json(inventory),
        "files": len(inventory),
        "metadata_file_sha256": dict(sorted(metadata_entries.items())),
        "name": name,
        # uv rewrites raw RECORD order and launcher rows per venv path.  Bind
        # the canonical, verified wheel payload rows so this SHA is portable.
        "record_sha256": _hash_json(
            [
                {
                    "bytes": item["bytes"],
                    "record_path": item["record_path"],
                    "sha256": item["sha256"],
                }
                for item in inventory
            ]
        ),
        "version": version,
    }


def _site_packages_inventory() -> dict[str, Any]:
    prefix = Path(sys.prefix).resolve()
    roots = {
        Path(value).resolve()
        for value in site.getsitepackages()
        if Path(value).resolve().is_relative_to(prefix)
    }
    if len(roots) != 1:
        raise WorkerError("interpreter must expose exactly one venv site-packages root")
    root = roots.pop()
    inventory: list[dict[str, Any]] = []
    total_bytes = 0
    for path in sorted(root.rglob("*"), key=lambda value: value.as_posix().encode("utf-8")):
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise WorkerError("site-packages contains a linked or special member")
        if metadata.st_nlink != 1:
            raise WorkerError("site-packages member must have exactly one hard link")
        relative = path.relative_to(root).as_posix()
        if Path(relative).name in {"INSTALLER", "RECORD", "REQUESTED", "direct_url.json"}:
            continue
        digest = _sha256_file(path, maximum_bytes=MAX_SITE_FILE_BYTES)
        inventory.append(
            {
                "bytes": metadata.st_size,
                "path": relative,
                "sha256": digest,
            }
        )
        total_bytes += metadata.st_size
    if not inventory:
        raise WorkerError("site-packages inventory is empty")
    return {
        "bytes": total_bytes,
        "files": len(inventory),
        "inventory": inventory,
        "inventory_sha256": _hash_json(inventory),
    }


def _venv_identity() -> dict[str, Any]:
    if Path(sys.prefix).resolve() == Path(sys.base_prefix).resolve():
        raise WorkerError("runtime interpreter is not a virtual environment")
    config_path = Path(sys.prefix) / "pyvenv.cfg"
    metadata = _regular_file(config_path, context="runtime pyvenv.cfg")
    if metadata.st_size > MAX_PYVENV_CFG_BYTES:
        raise WorkerError("runtime pyvenv.cfg exceeds byte cap")
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise WorkerError("runtime pyvenv.cfg is invalid") from error
    values: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        normalized_key = key.strip().lower()
        if separator != "=" or not normalized_key or normalized_key in values:
            raise WorkerError("runtime pyvenv.cfg is malformed")
        values[normalized_key] = value.strip()
    if set(values) != {
        "home",
        "implementation",
        "include-system-site-packages",
        "uv",
        "version_info",
    }:
        raise WorkerError("runtime pyvenv.cfg schema mismatch")
    if (
        values["implementation"] != "CPython"
        or values["include-system-site-packages"] != "false"
        or values["version_info"] != platform.python_version()
        or not values["home"]
        or not values["uv"]
    ):
        raise WorkerError("runtime pyvenv.cfg identity mismatch")
    return {
        "builder": "uv",
        "builder_version": values["uv"],
        "include_system_site_packages": False,
    }


def _environment_identity() -> dict[str, Any]:
    distributions: dict[str, importlib.metadata.Distribution] = {}
    for distribution in importlib.metadata.distributions():
        raw_name = distribution.metadata.get("Name")
        if not isinstance(raw_name, str) or not raw_name:
            raise WorkerError("runtime distribution has no metadata name")
        name = _normalized_distribution_name(raw_name)
        if name in distributions:
            raise WorkerError(f"runtime environment has duplicate distribution {name}")
        distributions[name] = distribution
    if EXPECTED_PACKAGE not in distributions:
        raise WorkerError(f"{EXPECTED_PACKAGE} is not installed")
    trafilatura_distribution = distributions[EXPECTED_PACKAGE]
    if trafilatura_distribution.version != EXPECTED_VERSION:
        raise WorkerError(
            "runtime package version mismatch: "
            f"expected {EXPECTED_VERSION}, found {trafilatura_distribution.version}"
        )
    packages = [
        _distribution_identity(distributions[name])
        for name in sorted(distributions, key=lambda value: value.encode("utf-8"))
    ]
    return {
        "interpreter": {
            "cache_tag": sys.implementation.cache_tag,
            "implementation": sys.implementation.name,
            "machine": platform.machine(),
            "platform": sys.platform,
            "python_version": platform.python_version(),
        },
        "packages": packages,
        "packages_sha256": _hash_json(packages),
        "site_packages": _site_packages_inventory(),
        "venv": _venv_identity(),
    }


def _load_html(path: Path, item: dict[str, Any]) -> str:
    metadata = _regular_file(path, context="AEB compressed HTML")
    compressed = path.read_bytes()
    if metadata.st_size != item["compressed_bytes"] or len(compressed) != item["compressed_bytes"]:
        raise WorkerError("compressed HTML size drift")
    if _sha256_bytes(compressed) != item["compressed_sha256"]:
        raise WorkerError("compressed HTML SHA-256 drift")
    try:
        decoded = gzip.decompress(compressed)
    except (gzip.BadGzipFile, EOFError, OSError) as error:
        raise WorkerError("compressed HTML is not a valid gzip stream") from error
    if len(decoded) != item["decoded_bytes"] or _sha256_bytes(decoded) != item["decoded_sha256"]:
        raise WorkerError("decoded HTML identity drift")
    try:
        return decoded.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise WorkerError("decoded HTML is not strict UTF-8") from error


def run(
    *,
    capsule: Path,
    output: Path,
    expected_manifest_sha256: str,
    expected_inventory_sha256: str,
    expected_worker_sha256: str,
) -> dict[str, Any]:
    worker_path = Path(__file__).resolve()
    if (
        not _is_sha256(expected_worker_sha256)
        or _sha256_file(worker_path) != expected_worker_sha256
    ):
        raise WorkerError("worker source SHA-256 mismatch")
    manifest, items = _load_capsule(
        capsule,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_inventory_sha256=expected_inventory_sha256,
    )
    environment = _environment_identity()
    try:
        from trafilatura import extract
    except (ImportError, RuntimeError) as error:
        raise WorkerError("could not import trafilatura.extract") from error
    if not callable(extract):
        raise WorkerError("trafilatura.extract is not callable")

    rows: list[dict[str, Any]] = []
    wall_started = time.perf_counter_ns()
    for item in items:
        html = _load_html(capsule / item["path"], item)
        started = time.perf_counter_ns()
        try:
            prediction_value = extract(html, include_comments=False)
        except Exception as error:
            raise WorkerError(
                f"trafilatura extraction failed for key {item['key']}: "
                f"{type(error).__name__}: {str(error)[:500]}"
            ) from error
        latency_ns = time.perf_counter_ns() - started
        if not isinstance(prediction_value, str):
            raise WorkerError("trafilatura returned a non-string prediction")
        prediction = prediction_value
        prediction_bytes = prediction.encode("utf-8")
        rows.append(
            {
                "articleBody": prediction,
                "decoded_input_sha256": item["decoded_sha256"],
                "key": item["key"],
                "latency_ns": latency_ns,
                "prediction_bytes": len(prediction_bytes),
                "prediction_sha256": _sha256_bytes(prediction_bytes),
            }
        )
    wall_ns = time.perf_counter_ns() - wall_started
    if _environment_identity() != environment:
        raise WorkerError("runtime environment changed during extraction")
    predictions_commitment = _hash_json(
        [
            {
                "articleBody": row["articleBody"],
                "key": row["key"],
                "prediction_bytes": row["prediction_bytes"],
                "prediction_sha256": row["prediction_sha256"],
            }
            for row in rows
        ]
    )
    result = {
        "predictions": rows,
        "receipt": {
            "config": CONFIG,
            "config_sha256": CONFIG_SHA256,
            "environment": environment,
            "input_inventory_sha256": expected_inventory_sha256,
            "input_manifest_sha256": expected_manifest_sha256,
            "pages": len(rows),
            "predictions_commitment_sha256": predictions_commitment,
            "python": {
                "executable": sys.executable,
                "implementation": sys.implementation.name,
                "isolated": sys.flags.isolated == 1,
                "version": sys.version,
            },
            "upstream_runner": manifest["upstream_runner"],
            "wall_ns": wall_ns,
            "worker_sha256": expected_worker_sha256,
        },
        "schema": RESULT_SCHEMA,
        "schema_version": 2,
    }
    encoded = _canonical_bytes(result)
    if len(encoded) > MAX_RESULT_BYTES:
        raise WorkerError("worker result exceeds byte cap")
    output_parent = output.parent
    output_parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + f".tmp-{os.getpid()}")
    if temporary.exists() or output.exists():
        raise WorkerError("refusing to overwrite worker output")
    with temporary.open("xb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, output)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capsule", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--expected-inventory-sha256", required=True)
    parser.add_argument("--expected-worker-sha256", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run(
            capsule=args.capsule.resolve(),
            output=args.output.resolve(),
            expected_manifest_sha256=args.expected_manifest_sha256,
            expected_inventory_sha256=args.expected_inventory_sha256,
            expected_worker_sha256=args.expected_worker_sha256,
        )
    except WorkerError as error:
        print(f"current OSS replay error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

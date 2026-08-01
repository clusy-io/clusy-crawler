#!/usr/bin/env python3
"""Export label-free, annotation-scrubbed WebMainBench selector-study stages.

This exporter is deliberately fail closed.  It verifies the complete pinned
WebMainBench file (size, SHA-256, JSONL record count, and stable file identity)
before it creates a staging directory.  Only then does it make a second pass,
scrub annotation UI artifacts, write deterministic gzip files, independently
audit every emitted row, and atomically publish a previously absent directory.

The split unit is the exact lowercase URL hostname with trailing dots removed.
It is *not* a registrable domain or public-suffix grouping.  Development rows
are the canonical rows 0..544.  Later rows on a development hostname retain the
legacy validation assignment; other hostnames use the historical salted
four-bucket rule: bucket 0 is legacy validation, bucket 1 is repair validation,
and buckets 2/3 remain the sealed final stage.

The payload schema intentionally excludes every benchmark label or category:
``convert_main_content``, ``main_html``, and ``meta`` never cross the export
boundary.  This creates selector-study inputs, not a blind or universal-SOTA
claim: WebMainBench itself is public, and the legacy stage is already exposed.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import errno
import gzip
import hashlib
import html as html_module
import inspect
import json
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench.webmainbench_benchmark import (  # noqa: E402
    DATASET_BYTES as PINNED_DATASET_BYTES,
)
from bench.webmainbench_benchmark import (  # noqa: E402
    DATASET_FILENAME,
    DATASET_REPOSITORY,
    DATASET_REVISION,
    BenchmarkError,
    scrub_annotation_artifacts,
)
from bench.webmainbench_benchmark import (  # noqa: E402
    DATASET_RECORDS as PINNED_DATASET_RECORDS,
)
from bench.webmainbench_benchmark import (  # noqa: E402
    DATASET_SHA256 as PINNED_DATASET_SHA256,
)

SCHEMA_VERSION = "clusy.webmain-v2.selector-study-split.1"
INPUT_SCHEMA_VERSION = "clusy.webmain-v2.label-isolated-input.1"
DEVELOPMENT_RECORDS = 545
SPLIT_SALT = b"clusy-webmain-v2-domain-split-v1\x00"
GZIP_COMPRESSLEVEL = 9

DEVELOPMENT = "development"
LEGACY_VALIDATION = "legacy_validation"
REPAIR_VALIDATION = "repair_validation"
SEALED_FINAL = "sealed_final"
STAGES = (DEVELOPMENT, LEGACY_VALIDATION, REPAIR_VALIDATION, SEALED_FINAL)
STAGE_FILES = {
    DEVELOPMENT: "development-input.jsonl.gz",
    LEGACY_VALIDATION: "legacy-validation-input.jsonl.gz",
    REPAIR_VALIDATION: "repair-validation-input.jsonl.gz",
    SEALED_FINAL: "sealed-final-input.jsonl.gz",
}

EXPORTED_FIELDS = (
    "schema_version",
    "dataset_index",
    "track_id",
    "url",
    "html",
    "raw_html_sha256",
    "scrubbed_html_sha256",
)
LABEL_FIELDS = ("convert_main_content", "main_html", "meta")

_RAW_ANNOTATION_PATTERNS = (
    re.compile(r"\bcc-select\b", re.IGNORECASE),
    re.compile(r"\bdata-anno-uid\b", re.IGNORECASE),
    re.compile(r"\bcc-extrastyle\b", re.IGNORECASE),
    re.compile(r"\bmark-selected\b", re.IGNORECASE),
    re.compile(r"<\s*/?\s*marked-(?:text|tail)\b", re.IGNORECASE),
)
_MAX_ENTITY_DECODE_PASSES = 8


class SelectorStudyError(RuntimeError):
    """An integrity, isolation, or publication condition invalidates export."""


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    bytes: int
    mtime_ns: int
    ctime_ns: int
    links: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> _FileIdentity:
        return cls(
            device=value.st_dev,
            inode=value.st_ino,
            bytes=value.st_size,
            mtime_ns=value.st_mtime_ns,
            ctime_ns=value.st_ctime_ns,
            links=value.st_nlink,
        )

    def export(self) -> dict[str, int]:
        return {
            "device": self.device,
            "inode": self.inode,
            "bytes": self.bytes,
            "mtime_ns": self.mtime_ns,
            "ctime_ns": self.ctime_ns,
            "links": self.links,
        }


@dataclass(slots=True)
class _SplitPlan:
    development_hostnames: set[str]
    stage_hostnames: dict[str, set[str]]
    stage_records: Counter[str]
    route_records: Counter[str]
    track_ids: set[str]
    dataset_sha256: str
    dataset_bytes: int
    dataset_records: int


@dataclass(slots=True)
class _Writer:
    path: Path
    raw: BinaryIO
    compressed: gzip.GzipFile


def _canonical_json_line(value: object) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise SelectorStudyError("could not encode canonical JSON") from error
    return (rendered + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            byte_count += len(chunk)
    return digest.hexdigest(), byte_count


def _hash_hostname_set(hostnames: set[str]) -> str:
    """Hash a canonical JSON array without exposing the hostname inventory."""

    encoded = json.dumps(
        sorted(hostnames),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _canonical_hostname(url: str, *, dataset_index: int) -> str:
    if type(url) is not str or not url:
        raise SelectorStudyError(f"dataset row {dataset_index} has an invalid URL")
    try:
        hostname = urlsplit(url).hostname
    except (TypeError, ValueError) as error:
        raise SelectorStudyError(
            f"dataset row {dataset_index} URL has no parseable exact hostname"
        ) from error
    if hostname is None:
        raise SelectorStudyError(f"dataset row {dataset_index} URL has no exact hostname")
    canonical = hostname.lower().rstrip(".")
    if not canonical or any(character.isspace() or ord(character) < 32 for character in canonical):
        raise SelectorStudyError(f"dataset row {dataset_index} URL has an invalid exact hostname")
    return canonical


def _bucket(hostname: str) -> int:
    digest = hashlib.sha256(SPLIT_SALT + hostname.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 4


def _route(
    *,
    dataset_index: int,
    hostname: str,
    development_hostnames: set[str],
) -> tuple[str, str]:
    if dataset_index < DEVELOPMENT_RECORDS:
        return DEVELOPMENT, "canonical_development_index"
    if hostname in development_hostnames:
        return LEGACY_VALIDATION, "development_hostname_overlap"
    bucket = _bucket(hostname)
    if bucket == 0:
        return LEGACY_VALIDATION, "bucket_0"
    if bucket == 1:
        return REPAIR_VALIDATION, "bucket_1"
    return SEALED_FINAL, f"bucket_{bucket}"


def _parse_row(raw_line: bytes, dataset_index: int) -> dict[str, Any]:
    try:
        value = json.loads(raw_line)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise SelectorStudyError(f"dataset row {dataset_index} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise SelectorStudyError(f"dataset row {dataset_index} is not an object")
    for field in ("track_id", "url", "html"):
        if type(value.get(field)) is not str or not value[field]:
            raise SelectorStudyError(f"dataset row {dataset_index} has invalid {field}")
    return value


def _assert_annotation_free(value: str, *, context: str) -> None:
    """Reject raw or repeatedly entity-escaped annotation-tool signals."""

    representation = value
    for decode_pass in range(_MAX_ENTITY_DECODE_PASSES + 1):
        hits = [
            pattern.pattern
            for pattern in _RAW_ANNOTATION_PATTERNS
            if pattern.search(representation)
        ]
        if hits:
            layer = "raw" if decode_pass == 0 else f"entity-decoded-{decode_pass}"
            raise SelectorStudyError(
                f"annotation artifact remained in {context} ({layer}): {', '.join(hits)}"
            )
        decoded = html_module.unescape(representation)
        if decoded == representation:
            return
        representation = decoded
    if html_module.unescape(representation) != representation:
        raise SelectorStudyError(f"annotation scan exceeded entity decoding bound in {context}")


def _open_verified_dataset(path: Path) -> tuple[BinaryIO, _FileIdentity]:
    absolute = Path(os.path.abspath(os.fspath(path)))
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if type(nofollow) is not int:
        raise SelectorStudyError("O_NOFOLLOW is required for the pinned dataset")
    try:
        descriptor = os.open(absolute, os.O_RDONLY | nofollow)
    except OSError as error:
        raise SelectorStudyError(f"could not open pinned dataset: {absolute}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SelectorStudyError("pinned dataset is not a regular file")
        if metadata.st_nlink != 1:
            raise SelectorStudyError("pinned dataset must have exactly one hard link")
        identity = _FileIdentity.from_stat(metadata)
        if identity.bytes != PINNED_DATASET_BYTES:
            raise SelectorStudyError("pinned dataset byte count mismatch before verification")
        return os.fdopen(descriptor, "rb", closefd=True), identity
    except BaseException:
        os.close(descriptor)
        raise


def _assert_stable_dataset(stream: BinaryIO, expected: _FileIdentity, *, phase: str) -> None:
    observed = _FileIdentity.from_stat(os.fstat(stream.fileno()))
    if observed != expected:
        raise SelectorStudyError(f"pinned dataset changed during {phase}")


def _verify_and_plan(stream: BinaryIO, identity: _FileIdentity) -> _SplitPlan:
    """Verify all pinned bytes and records before any output path is created."""

    if DEVELOPMENT_RECORDS <= 0 or DEVELOPMENT_RECORDS >= PINNED_DATASET_RECORDS:
        raise SelectorStudyError("development record boundary is invalid")
    digest = hashlib.sha256()
    byte_count = 0
    development_hostnames: set[str] = set()
    stage_hostnames = {stage: set() for stage in STAGES}
    stage_records: Counter[str] = Counter()
    route_records: Counter[str] = Counter()
    track_ids: set[str] = set()
    record_count = 0

    for dataset_index, raw_line in enumerate(stream):
        digest.update(raw_line)
        byte_count += len(raw_line)
        row = _parse_row(raw_line, dataset_index)
        track_id = row["track_id"]
        if track_id in track_ids:
            raise SelectorStudyError(
                f"duplicate track_id at dataset row {dataset_index}: {track_id}"
            )
        track_ids.add(track_id)
        hostname = _canonical_hostname(row["url"], dataset_index=dataset_index)
        if dataset_index < DEVELOPMENT_RECORDS:
            development_hostnames.add(hostname)
        stage, reason = _route(
            dataset_index=dataset_index,
            hostname=hostname,
            development_hostnames=development_hostnames,
        )
        stage_hostnames[stage].add(hostname)
        stage_records[stage] += 1
        route_records[reason] += 1
        record_count += 1

    _assert_stable_dataset(stream, identity, phase="pre-write verification")
    observed_sha256 = digest.hexdigest()
    if byte_count != PINNED_DATASET_BYTES:
        raise SelectorStudyError("pinned dataset byte count mismatch")
    if observed_sha256 != PINNED_DATASET_SHA256:
        raise SelectorStudyError("pinned dataset SHA-256 mismatch")
    if record_count != PINNED_DATASET_RECORDS:
        raise SelectorStudyError("pinned dataset JSONL record count mismatch")
    if len(track_ids) != record_count:
        raise SelectorStudyError("pinned dataset track_id cardinality mismatch")
    _assert_isolation(stage_hostnames, development_hostnames)
    return _SplitPlan(
        development_hostnames=development_hostnames,
        stage_hostnames=stage_hostnames,
        stage_records=stage_records,
        route_records=route_records,
        track_ids=track_ids,
        dataset_sha256=observed_sha256,
        dataset_bytes=byte_count,
        dataset_records=record_count,
    )


def _assert_isolation(
    stage_hostnames: Mapping[str, set[str]],
    development_hostnames: set[str],
) -> None:
    post_development = (LEGACY_VALIDATION, REPAIR_VALIDATION, SEALED_FINAL)
    for left_index, left in enumerate(post_development):
        for right in post_development[left_index + 1 :]:
            if stage_hostnames[left] & stage_hostnames[right]:
                raise SelectorStudyError(f"exact-hostname overlap between {left} and {right}")
    if development_hostnames & stage_hostnames[REPAIR_VALIDATION]:
        raise SelectorStudyError("development exact hostname escaped into repair validation")
    if development_hostnames & stage_hostnames[SEALED_FINAL]:
        raise SelectorStudyError("development exact hostname escaped into sealed final")


def _exclusive_staging_directory(output_dir: Path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(output_dir)))
    if not absolute.name:
        raise SelectorStudyError("output directory must have a final path component")
    if os.path.lexists(absolute):
        raise SelectorStudyError(f"refusing to overwrite output directory: {absolute}")
    try:
        parent_metadata = os.lstat(absolute.parent)
    except OSError as error:
        raise SelectorStudyError(f"output parent must already exist: {absolute.parent}") from error
    if not stat.S_ISDIR(parent_metadata.st_mode) or stat.S_ISLNK(parent_metadata.st_mode):
        raise SelectorStudyError("output parent must be a real directory, not a symlink")

    for _ in range(32):
        staging = absolute.parent / (
            f".{absolute.name}.staging-{os.getpid()}-{secrets.token_hex(12)}"
        )
        try:
            os.mkdir(staging, 0o700)
        except FileExistsError:
            continue
        return staging
    raise SelectorStudyError("could not allocate an exclusive staging directory")


def _open_stage_writers(staging: Path) -> dict[str, _Writer]:
    writers: dict[str, _Writer] = {}
    try:
        for stage in STAGES:
            path = staging / STAGE_FILES[stage]
            raw = path.open("xb")
            try:
                compressed = gzip.GzipFile(
                    filename="",
                    mode="wb",
                    compresslevel=GZIP_COMPRESSLEVEL,
                    fileobj=raw,
                    mtime=0,
                )
            except BaseException:
                raw.close()
                raise
            writers[stage] = _Writer(path=path, raw=raw, compressed=compressed)
        return writers
    except BaseException:
        _close_stage_writers(writers, sync=False)
        raise


def _close_stage_writers(writers: Mapping[str, _Writer], *, sync: bool) -> None:
    first_error: BaseException | None = None
    for writer in writers.values():
        try:
            writer.compressed.close()
        except BaseException as error:  # pragma: no cover - rare device failure
            first_error = first_error or error
    for writer in writers.values():
        try:
            if not writer.raw.closed:
                writer.raw.flush()
                if sync:
                    os.fsync(writer.raw.fileno())
                writer.raw.close()
        except BaseException as error:  # pragma: no cover - rare device failure
            first_error = first_error or error
    if first_error is not None:
        raise SelectorStudyError("could not finalize staged gzip output") from first_error


def _source_row_payload(
    row: Mapping[str, Any],
    *,
    dataset_index: int,
    scrubbed_html: str,
) -> dict[str, Any]:
    raw_html = row["html"]
    return {
        "schema_version": INPUT_SCHEMA_VERSION,
        "dataset_index": dataset_index,
        "track_id": row["track_id"],
        "url": row["url"],
        "html": scrubbed_html,
        "raw_html_sha256": _sha256_bytes(raw_html.encode("utf-8")),
        "scrubbed_html_sha256": _sha256_bytes(scrubbed_html.encode("utf-8")),
    }


def _emit_second_pass(
    stream: BinaryIO,
    identity: _FileIdentity,
    plan: _SplitPlan,
    writers: Mapping[str, _Writer],
) -> Counter[str]:
    stream.seek(0)
    digest = hashlib.sha256()
    byte_count = 0
    record_count = 0
    emitted_records: Counter[str] = Counter()
    emitted_hostnames = {stage: set() for stage in STAGES}
    emitted_track_ids: set[str] = set()
    scrub_totals: Counter[str] = Counter()

    for dataset_index, raw_line in enumerate(stream):
        digest.update(raw_line)
        byte_count += len(raw_line)
        row = _parse_row(raw_line, dataset_index)
        track_id = row["track_id"]
        if track_id in emitted_track_ids:
            raise SelectorStudyError(f"duplicate track_id during emission at row {dataset_index}")
        emitted_track_ids.add(track_id)
        hostname = _canonical_hostname(row["url"], dataset_index=dataset_index)
        stage, _ = _route(
            dataset_index=dataset_index,
            hostname=hostname,
            development_hostnames=plan.development_hostnames,
        )
        try:
            scrubbed_html, scrub_counts = scrub_annotation_artifacts(row["html"])
        except BenchmarkError as error:
            raise SelectorStudyError(
                f"annotation scrub failed at dataset row {dataset_index}: {error}"
            ) from error
        if type(scrubbed_html) is not str:
            raise SelectorStudyError(
                f"annotation scrub returned non-string HTML at row {dataset_index}"
            )
        if not isinstance(scrub_counts, dict) or any(
            type(key) is not str or type(value) is not int or value < 0
            for key, value in scrub_counts.items()
        ):
            raise SelectorStudyError(
                f"annotation scrub returned invalid counters at row {dataset_index}"
            )
        _assert_annotation_free(scrubbed_html, context=f"dataset row {dataset_index}")
        scrub_totals.update(scrub_counts)
        payload = _source_row_payload(
            row,
            dataset_index=dataset_index,
            scrubbed_html=scrubbed_html,
        )
        if tuple(sorted(payload)) != tuple(sorted(EXPORTED_FIELDS)):
            raise SelectorStudyError("internal payload field contract violation")
        writers[stage].compressed.write(_canonical_json_line(payload))
        emitted_records[stage] += 1
        emitted_hostnames[stage].add(hostname)
        record_count += 1

    _assert_stable_dataset(stream, identity, phase="output emission")
    if digest.hexdigest() != plan.dataset_sha256 or byte_count != plan.dataset_bytes:
        raise SelectorStudyError("pinned dataset bytes changed between verification passes")
    if record_count != plan.dataset_records or emitted_track_ids != plan.track_ids:
        raise SelectorStudyError("pinned dataset records changed between verification passes")
    if emitted_records != plan.stage_records:
        raise SelectorStudyError("stage record plan changed between verification passes")
    if any(emitted_hostnames[stage] != plan.stage_hostnames[stage] for stage in STAGES):
        raise SelectorStudyError("stage exact-hostname plan changed between passes")
    return scrub_totals


def _validate_payload_row(
    value: object,
    *,
    stage: str,
    line_number: int,
    plan: _SplitPlan,
) -> tuple[int, str, str]:
    if not isinstance(value, dict) or set(value) != set(EXPORTED_FIELDS):
        raise SelectorStudyError(
            f"{stage} output row {line_number} violates the closed payload schema"
        )
    if value.get("schema_version") != INPUT_SCHEMA_VERSION:
        raise SelectorStudyError(f"{stage} output row {line_number} has wrong schema")
    dataset_index = value.get("dataset_index")
    if type(dataset_index) is not int or not 0 <= dataset_index < PINNED_DATASET_RECORDS:
        raise SelectorStudyError(f"{stage} output row {line_number} has invalid dataset_index")
    for field in ("track_id", "url", "html", "raw_html_sha256", "scrubbed_html_sha256"):
        if type(value.get(field)) is not str:
            raise SelectorStudyError(f"{stage} output row {line_number} has invalid {field}")
    if not value["track_id"] or not value["url"]:
        raise SelectorStudyError(f"{stage} output row {line_number} has an empty ID or URL")
    for field in ("raw_html_sha256", "scrubbed_html_sha256"):
        if re.fullmatch(r"[0-9a-f]{64}", value[field]) is None:
            raise SelectorStudyError(f"{stage} output row {line_number} has invalid {field}")
    if _sha256_bytes(value["html"].encode("utf-8")) != value["scrubbed_html_sha256"]:
        raise SelectorStudyError(f"{stage} output row {line_number} scrubbed HTML hash mismatch")
    _assert_annotation_free(
        value["html"],
        context=f"{stage} output row {line_number}",
    )
    hostname = _canonical_hostname(value["url"], dataset_index=dataset_index)
    expected_stage, _ = _route(
        dataset_index=dataset_index,
        hostname=hostname,
        development_hostnames=plan.development_hostnames,
    )
    if expected_stage != stage:
        raise SelectorStudyError(f"{stage} output row {line_number} belongs to {expected_stage}")
    return dataset_index, value["track_id"], hostname


def _audit_stage_outputs(staging: Path, plan: _SplitPlan) -> dict[str, dict[str, Any]]:
    stages: dict[str, dict[str, Any]] = {}
    all_indices: set[int] = set()
    all_track_ids: set[str] = set()
    for stage in STAGES:
        path = staging / STAGE_FILES[stage]
        with path.open("rb") as compressed_stream:
            header = compressed_stream.read(10)
        if len(header) != 10 or header[:3] != b"\x1f\x8b\x08":
            raise SelectorStudyError(f"{stage} output is not a gzip stream")
        if header[4:8] != b"\x00\x00\x00\x00":
            raise SelectorStudyError(f"{stage} gzip mtime is not zero")
        if header[3] & 0x08:
            raise SelectorStudyError(f"{stage} gzip unexpectedly embeds a filename")

        uncompressed_digest = hashlib.sha256()
        uncompressed_bytes = 0
        record_count = 0
        stage_indices: list[int] = []
        hostnames: set[str] = set()
        try:
            with gzip.open(path, "rb") as stream:
                for line_number, raw_line in enumerate(stream, start=1):
                    uncompressed_digest.update(raw_line)
                    uncompressed_bytes += len(raw_line)
                    try:
                        value = json.loads(raw_line)
                    except (json.JSONDecodeError, UnicodeDecodeError) as error:
                        raise SelectorStudyError(
                            f"{stage} output row {line_number} is invalid JSON"
                        ) from error
                    dataset_index, track_id, hostname = _validate_payload_row(
                        value,
                        stage=stage,
                        line_number=line_number,
                        plan=plan,
                    )
                    if dataset_index in all_indices:
                        raise SelectorStudyError(
                            f"duplicate dataset_index across stage outputs: {dataset_index}"
                        )
                    if track_id in all_track_ids:
                        raise SelectorStudyError(
                            f"duplicate track_id across stage outputs: {track_id}"
                        )
                    all_indices.add(dataset_index)
                    all_track_ids.add(track_id)
                    stage_indices.append(dataset_index)
                    hostnames.add(hostname)
                    record_count += 1
        except (EOFError, gzip.BadGzipFile, OSError) as error:
            raise SelectorStudyError(f"could not verify {stage} gzip output") from error

        if record_count != plan.stage_records[stage]:
            raise SelectorStudyError(f"{stage} output record count mismatch")
        if hostnames != plan.stage_hostnames[stage]:
            raise SelectorStudyError(f"{stage} output exact-hostname set mismatch")
        compressed_sha256, compressed_bytes = _sha256_file(path)
        stages[stage] = {
            "path": STAGE_FILES[stage],
            "records": record_count,
            "exact_hostnames": len(hostnames),
            "exact_hostname_set_sha256": _hash_hostname_set(hostnames),
            "first_dataset_index": min(stage_indices) if stage_indices else None,
            "last_dataset_index": max(stage_indices) if stage_indices else None,
            "uncompressed_bytes": uncompressed_bytes,
            "uncompressed_sha256": uncompressed_digest.hexdigest(),
            "compressed_bytes": compressed_bytes,
            "compressed_sha256": compressed_sha256,
            "annotation_artifact_scan": {
                "records_scanned": record_count,
                "raw_and_entity_decoded_passes": _MAX_ENTITY_DECODE_PASSES,
                "passed": True,
            },
        }

    if all_indices != set(range(PINNED_DATASET_RECORDS)):
        raise SelectorStudyError("stage outputs do not partition every dataset index")
    if all_track_ids != plan.track_ids:
        raise SelectorStudyError("stage outputs do not partition every track_id")
    return stages


def _snapshot_source_file(path: Path) -> dict[str, Any]:
    sha256, byte_count = _sha256_file(path)
    try:
        relative = path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        relative = str(path.resolve())
    return {"path": relative, "bytes": byte_count, "sha256": sha256}


def _git_value(*arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ("git", "-C", str(ROOT), *arguments),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _source_provenance() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    scrubber_path_value = inspect.getsourcefile(scrub_annotation_artifacts)
    if scrubber_path_value is None:
        raise SelectorStudyError("could not locate annotation scrubber source")
    paths = {
        "exporter": Path(__file__).resolve(),
        "annotation_scrubber_module": Path(scrubber_path_value).resolve(),
    }
    snapshots = {name: _snapshot_source_file(path) for name, path in paths.items()}
    status = _git_value(
        "status",
        "--porcelain=v1",
        "--",
        *(snapshot["path"] for snapshot in snapshots.values()),
    )
    provenance = {
        "git_commit": _git_value("rev-parse", "HEAD"),
        "git_tree": _git_value("rev-parse", "HEAD^{tree}"),
        "relevant_git_status": status or "",
        "relevant_worktree_dirty": bool(status),
        "files": snapshots,
        "scrubber_callable": ("bench.webmainbench_benchmark.scrub_annotation_artifacts"),
        "python": sys.version,
        "platform": platform.platform(),
    }
    return provenance, snapshots


def _assert_source_stable(before: Mapping[str, Mapping[str, Any]]) -> None:
    for snapshot in before.values():
        path_value = snapshot["path"]
        path = ROOT / path_value if not Path(path_value).is_absolute() else Path(path_value)
        after = _snapshot_source_file(path)
        if after["bytes"] != snapshot["bytes"] or after["sha256"] != snapshot["sha256"]:
            raise SelectorStudyError(f"relevant source changed during export: {path_value}")


def _isolation_manifest(plan: _SplitPlan) -> dict[str, Any]:
    development = plan.development_hostnames
    legacy = plan.stage_hostnames[LEGACY_VALIDATION]
    repair = plan.stage_hostnames[REPAIR_VALIDATION]
    sealed = plan.stage_hostnames[SEALED_FINAL]
    return {
        "unit": "exact lowercase hostname without trailing dot",
        "unit_kind": "exact_hostname",
        "registrable_domain_or_public_suffix_grouping_used": False,
        "post_development_stage_pairwise_overlap": {
            "legacy_validation__repair_validation": len(legacy & repair),
            "legacy_validation__sealed_final": len(legacy & sealed),
            "repair_validation__sealed_final": len(repair & sealed),
        },
        "development_overlap": {
            "legacy_validation": len(development & legacy),
            "repair_validation": len(development & repair),
            "sealed_final": len(development & sealed),
        },
        "passed": not (
            legacy & repair
            or legacy & sealed
            or repair & sealed
            or development & repair
            or development & sealed
        ),
        "intentional_exception": (
            "later rows sharing an exact development hostname remain in legacy "
            "validation for continuity with the existing split; development and "
            "legacy validation are therefore not hostname-disjoint"
        ),
    }


def _manifest(
    *,
    dataset_path: Path,
    identity: _FileIdentity,
    plan: _SplitPlan,
    source: Mapping[str, Any],
    scrub_totals: Counter[str],
    stages: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    overlap_pages = plan.route_records["development_hostname_overlap"]
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "warning": (
            "PUBLIC DATASET SELECTOR STUDY; LEGACY VALIDATION IS EXPOSED; "
            "NOT A BLIND TEST OR UNIVERSAL SOTA CLAIM"
        ),
        "dataset": {
            "repository": DATASET_REPOSITORY,
            "revision": DATASET_REVISION,
            "filename": DATASET_FILENAME,
            "input_path": str(Path(os.path.abspath(os.fspath(dataset_path)))),
            "bytes": plan.dataset_bytes,
            "sha256": plan.dataset_sha256,
            "records": plan.dataset_records,
            "descriptor_identity": identity.export(),
            "verified_before_staging_created": True,
            "verified_again_after_emission": True,
        },
        "source": dict(source),
        "payload_contract": {
            "schema_version": INPUT_SCHEMA_VERSION,
            "exported_fields": list(EXPORTED_FIELDS),
            "label_fields_exported": [],
            "source_fields_explicitly_excluded": list(LABEL_FIELDS),
            "contains_reference_or_category_metadata": False,
            "closed_schema_revalidated_from_gzip": True,
        },
        "split_policy": {
            "hostname_unit": (
                "exact lowercase URL hostname without trailing dot; not a "
                "registrable domain and not a public-suffix-derived domain"
            ),
            "development": f"canonical dataset indices 0..{DEVELOPMENT_RECORDS - 1}",
            "legacy_validation": (
                "all later rows on a development exact hostname, plus non-development "
                "exact hostnames in bucket 0"
            ),
            "repair_validation": (
                "previously reserved non-development exact hostnames in bucket 1"
            ),
            "sealed_final": "non-development exact hostnames in buckets 2 and 3",
            "salt_utf8_hex": SPLIT_SALT.hex(),
            "bucket_function": (
                "uint64_be(first_8_bytes(sha256(salt || exact_hostname_utf8))) mod 4"
            ),
            "route_precedence": ("development index; development-hostname overlap; salted bucket"),
        },
        "routing_records": dict(sorted(plan.route_records.items())),
        "legacy_validation_development_hostname_overlap_pages": overlap_pages,
        "isolation_audit": _isolation_manifest(plan),
        "annotation_scrub": {
            "track": "scrubbed",
            "callable": "bench.webmainbench_benchmark.scrub_annotation_artifacts",
            "aggregate_counters": dict(sorted(scrub_totals.items())),
            "raw_and_entity_annotation_output_scan_passed": True,
        },
        "compression": {
            "format": "gzip",
            "compresslevel": GZIP_COMPRESSLEVEL,
            "mtime": 0,
            "embedded_filename": "",
        },
        "stages": {stage: dict(stages[stage]) for stage in STAGES},
    }


def _write_exclusive_file(path: Path, content: bytes) -> None:
    try:
        with path.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o400)
    except OSError as error:
        raise SelectorStudyError(f"could not write staged file: {path.name}") from error


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    if type(directory_flag) is int:
        flags |= directory_flag
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _promote_directory_no_replace(staging: Path, output_dir: Path) -> None:
    """Atomically rename a directory while refusing an existing destination."""

    output = Path(os.path.abspath(os.fspath(output_dir)))
    if os.path.lexists(output):
        raise SelectorStudyError(f"refusing to overwrite output directory: {output}")
    source_bytes = os.fsencode(staging)
    output_bytes = os.fsencode(output)
    libc = ctypes.CDLL(None, use_errno=True)

    if sys.platform.startswith("linux"):
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise SelectorStudyError("atomic no-replace renameat2 is unavailable")
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        result = renameat2(-100, source_bytes, -100, output_bytes, 1)
    elif sys.platform == "darwin":
        renamex_np = getattr(libc, "renamex_np", None)
        if renamex_np is None:
            raise SelectorStudyError("atomic no-replace renamex_np is unavailable")
        renamex_np.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        renamex_np.restype = ctypes.c_int
        result = renamex_np(source_bytes, output_bytes, 0x00000004)
    else:  # pragma: no cover - the supported benchmark hosts are Linux/macOS
        raise SelectorStudyError(
            "atomic no-replace directory promotion is unsupported on this platform"
        )

    if result != 0:
        error_number = ctypes.get_errno()
        if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
            raise SelectorStudyError(f"refusing to overwrite output directory: {output}")
        raise SelectorStudyError(f"atomic output promotion failed: {os.strerror(error_number)}")
    _fsync_directory(output.parent)


def export_selector_study(dataset: Path, output_dir: Path) -> dict[str, Any]:
    """Verify, export, audit, and atomically publish selector-study inputs."""

    output = Path(os.path.abspath(os.fspath(output_dir)))
    if os.path.lexists(output):
        raise SelectorStudyError(f"refusing to overwrite output directory: {output}")
    source, source_snapshots = _source_provenance()
    stream, identity = _open_verified_dataset(dataset)
    staging: Path | None = None
    promoted = False
    try:
        plan = _verify_and_plan(stream, identity)
        # No output path, staging path, lock, or temporary artifact is created
        # until the complete pinned source has passed the first verification.
        staging = _exclusive_staging_directory(output)
        writers = _open_stage_writers(staging)
        emit_error: BaseException | None = None
        try:
            scrub_totals = _emit_second_pass(stream, identity, plan, writers)
        except BaseException as error:
            emit_error = error
            raise
        finally:
            try:
                _close_stage_writers(writers, sync=emit_error is None)
            except BaseException:
                if emit_error is None:
                    raise
        for writer in writers.values():
            os.chmod(writer.path, 0o400)
        stages = _audit_stage_outputs(staging, plan)
        _assert_source_stable(source_snapshots)
        manifest = _manifest(
            dataset_path=dataset,
            identity=identity,
            plan=plan,
            source=source,
            scrub_totals=scrub_totals,
            stages=stages,
        )
        _write_exclusive_file(
            staging / "split-manifest.json",
            _canonical_json_line(manifest),
        )
        _assert_stable_dataset(stream, identity, phase="final publication")
        _assert_source_stable(source_snapshots)
        _fsync_directory(staging)
        _promote_directory_no_replace(staging, output)
        promoted = True
        return manifest
    finally:
        stream.close()
        if staging is not None and not promoted and staging.exists():
            shutil.rmtree(staging)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manifest = export_selector_study(args.dataset, args.output_dir)
    except SelectorStudyError as error:
        print(f"selector-study export error: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "manifest": str(
                    Path(os.path.abspath(os.fspath(args.output_dir))) / "split-manifest.json"
                ),
                "records": manifest["dataset"]["records"],
                "sha256": manifest["dataset"]["sha256"],
                "stages": {stage: manifest["stages"][stage]["records"] for stage in STAGES},
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

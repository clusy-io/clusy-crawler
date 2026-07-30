#!/usr/bin/env python3
"""Export WebMainBench raw-HTML-only decision inputs for a later audit process.

This process verifies the pinned dataset, emits a closed-schema projection, and
then exits. The audit runner must be invoked separately so label-bearing Python
objects cannot survive into the decision phase. Benchmark task/category IDs,
references, metadata, URLs, and benchmark-specific HTML transforms are omitted.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench import webmainbench_finegrained_benchmark as fine  # noqa: E402
from bench.claimable_io import (  # noqa: E402
    ClaimableIOError,
    read_verified_bytes,
    write_new_file,
)

DECISION_INPUT_SCHEMA = "webmainbench.atomic-structure-overlay-v0-decision-inputs.3"
EXPECTED_PAGES = 545


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


def _hash_json(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _atomic_write(path: Path, content: bytes) -> dict[str, Any]:
    try:
        metadata = write_new_file(path, content, mode=0o400)
    except ClaimableIOError as error:
        raise fine.BenchmarkError(f"could not publish projection artifact: {error}") from error
    return {
        "bytes": metadata.bytes,
        "path": str(metadata.path),
        "sha256": metadata.sha256,
    }


def export_projection(dataset: Path, output: Path) -> dict[str, Any]:
    dataset = dataset if dataset.is_absolute() else Path.cwd() / dataset
    output = output if output.is_absolute() else Path.cwd() / output
    manifest = output.with_suffix(f"{output.suffix}.manifest.json")
    if output.exists() or manifest.exists():
        raise fine.BenchmarkError(
            "projection output and adjacent manifest must not already exist"
        )
    try:
        dataset_bytes, dataset_file = read_verified_bytes(
            dataset,
            maximum_bytes=fine.DATASET_BYTES,
            expected_sha256=fine.DATASET_SHA256,
        )
    except ClaimableIOError as error:
        raise fine.BenchmarkError(f"dataset snapshot failed: {error}") from error
    if dataset_file.bytes != fine.DATASET_BYTES:
        raise fine.BenchmarkError("dataset byte count mismatch")
    dataset_metadata = {
        "bytes": dataset_file.bytes,
        "filename": fine.DATASET_FILENAME,
        "repository": fine.DATASET_REPOSITORY,
        "revision": fine.DATASET_REVISION,
        "sha256": dataset_file.sha256,
    }
    records = 0
    corpus_identity: list[dict[str, Any]] = []
    page_identity: list[dict[str, Any]] = []
    output_rows = bytearray()
    for line_number, line in enumerate(dataset_bytes.splitlines(), start=1):
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise fine.BenchmarkError(
                f"dataset line {line_number} is invalid"
            ) from error
        if not isinstance(record, dict) or type(record.get("html")) is not str:
            raise fine.BenchmarkError(
                f"dataset line {line_number} has invalid HTML"
            )
        raw_html = record["html"]
        output_rows.extend(
            _json_bytes(
                {
                    "schema_version": DECISION_INPUT_SCHEMA,
                    "dataset_index": records,
                    "raw_html": raw_html,
                }
            )
        )
        corpus_identity.append(
            {
                "dataset_index": records,
                "raw_html_sha256": hashlib.sha256(
                    raw_html.encode("utf-8")
                ).hexdigest(),
            }
        )
        page_identity.append(
            {
                "dataset_index": records,
            }
        )
        records += 1
    if records != EXPECTED_PAGES:
        raise fine.BenchmarkError("projection requires exactly 545 records")
    output_identity = _atomic_write(output, bytes(output_rows))

    projection_metadata = {
        "schema_version": DECISION_INPUT_SCHEMA,
        "created_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "source_dataset": dataset_metadata,
        "output": {
            "path": output_identity["path"],
            "bytes": output_identity["bytes"],
            "sha256": output_identity["sha256"],
            "records": records,
            "raw_html_corpus_sha256": _hash_json(corpus_identity),
            "page_id_sha256": _hash_json(page_identity),
            "canonical_order_verified": True,
        },
        "closed_fields": [
            "schema_version",
            "dataset_index",
            "raw_html",
        ],
        "contains_reference_or_metadata": False,
        "contains_track_or_category_id": False,
        "evaluator_derived_cleaner_used": False,
        "input_transform": "identity dataset HTML",
        "benchmark_specific_transform_used": False,
        "process_boundary_required": True,
    }
    _atomic_write(manifest, _json_bytes(projection_metadata))
    return projection_metadata


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        metadata = export_projection(args.dataset, args.output)
    except fine.BenchmarkError as error:
        print(f"decision-input export error: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "bytes": metadata["output"]["bytes"],
                "path": metadata["output"]["path"],
                "process_boundary_required": True,
                "records": metadata["output"]["records"],
                "sha256": metadata["output"]["sha256"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

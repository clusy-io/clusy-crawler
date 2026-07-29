#!/usr/bin/env python3
"""Export WebMainBench HTML-only decision inputs for a later audit process.

This process verifies the pinned dataset, emits a closed-schema projection, and
then exits. The audit runner must be invoked separately so label-bearing Python
objects cannot survive into the decision phase.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench import webmainbench_finegrained_benchmark as fine  # noqa: E402

DECISION_INPUT_SCHEMA = "webmainbench.atomic-structure-overlay-v0-decision-inputs.1"
EXPECTED_PAGES = 545


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def export_projection(dataset: Path, output: Path) -> dict[str, Any]:
    dataset = dataset.resolve()
    output = output.resolve()
    manifest = output.with_suffix(f"{output.suffix}.manifest.json")
    if output.exists() or manifest.exists():
        raise fine.BenchmarkError(
            "projection output and adjacent manifest must not already exist"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    dataset_metadata = fine.verify_dataset(dataset)
    temporary = output.with_name(f".{output.name}.tmp-{os.getpid()}")
    records = 0
    corpus_identity: list[dict[str, Any]] = []
    page_identity: list[dict[str, Any]] = []
    try:
        with temporary.open("xb") as handle:
            for record in fine._iter_records(  # noqa: SLF001
                dataset,
                offset=0,
                limit=None,
            ):
                if record.dataset_index != records:
                    raise fine.BenchmarkError("dataset order is not canonical")
                handle.write(
                    _json_bytes(
                        {
                            "schema_version": DECISION_INPUT_SCHEMA,
                            "dataset_index": record.dataset_index,
                            "track_id": record.track_id,
                            "html": record.html,
                        }
                    )
                )
                corpus_identity.append(
                    {
                        "dataset_index": record.dataset_index,
                        "track_id": record.track_id,
                        "html_sha256": hashlib.sha256(
                            record.html.encode("utf-8")
                        ).hexdigest(),
                    }
                )
                page_identity.append(
                    {
                        "dataset_index": record.dataset_index,
                        "track_id": record.track_id,
                    }
                )
                records += 1
            handle.flush()
            os.fsync(handle.fileno())
        if records != EXPECTED_PAGES:
            raise fine.BenchmarkError("projection requires exactly 545 records")
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()

    projection_metadata = {
        "schema_version": DECISION_INPUT_SCHEMA,
        "created_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "source_dataset": dataset_metadata,
        "output": {
            "path": str(output),
            "bytes": output.stat().st_size,
            "sha256": _sha256(output),
            "records": records,
            "html_corpus_sha256": _hash_json(corpus_identity),
            "page_id_sha256": _hash_json(page_identity),
            "canonical_order_verified": True,
        },
        "closed_fields": [
            "schema_version",
            "dataset_index",
            "track_id",
            "html",
        ],
        "contains_reference_or_metadata": False,
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
        f"exported {metadata['output']['records']} closed-schema rows to "
        f"{metadata['output']['path']}"
    )
    print("export process complete; invoke the audit as a new process")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

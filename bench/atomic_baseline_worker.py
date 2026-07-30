"""Fresh-interpreter production-baseline worker with no injectable callable."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import marshal
import os
import stat
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, "/capsule")

from claim_guard import (  # type: ignore[import-not-found] # noqa: E402
    WorkerGuardError,
    assert_fresh_interpreter,
    assert_import_closure,
    module_identity,
    read_stdin_envelope,
    write_canonical_stdout,
)

INPUT_SCHEMA = "clusy.atomic-overlay-claim-baseline-input.3"
OUTPUT_SCHEMA = "clusy.atomic-overlay-frozen-baseline.3"
EXPECTED_RECORDS = 545
EXTRACTION_PROFILE = "balanced"
FIXED_CONCURRENCY = 1
RUNTIME_SITE = Path("/opt/clusy-claim-runtime/lib/python3.12/site-packages")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _valid_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _activate_locked_runtime() -> tuple[str, ...]:
    """Expose one fixed copied-venv package root despite ``-I -S``.

    CPython 3.12 intentionally does not activate ``pyvenv.cfg`` when ``-S`` is
    present.  The claim protocol therefore names and validates the only allowed
    venv package root instead of consulting ``sysconfig`` from the unactivated
    interpreter.
    """

    if sys.version_info[:2] != (3, 12):
        raise WorkerGuardError("claim runtime requires exact CPython 3.12")
    components = (
        Path("/opt/clusy-claim-runtime"),
        Path("/opt/clusy-claim-runtime/lib"),
        Path("/opt/clusy-claim-runtime/lib/python3.12"),
        RUNTIME_SITE,
    )
    try:
        metadata = tuple(os.lstat(path) for path in components)
    except OSError as error:
        raise WorkerGuardError(
            "fixed production dependency root is unavailable"
        ) from error
    if any(
        not stat.S_ISDIR(item.st_mode) or stat.S_ISLNK(item.st_mode)
        for item in metadata
    ):
        raise WorkerGuardError("fixed production dependency root is not canonical")
    runtime_site = str(RUNTIME_SITE)
    if runtime_site in sys.path:
        raise WorkerGuardError("fixed production dependency root was preloaded")
    sys.path.insert(1, runtime_site)
    for forbidden in ("bench", "datasets", "evaluate", "webmainbench"):
        if importlib.util.find_spec(forbidden) is not None:
            raise WorkerGuardError(
                f"benchmark/evaluator module is importable in baseline worker: {forbidden}"
            )
    return (runtime_site,)


def _validate_capsule_manifest(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "app_file_sha256",
        "extension_relative_path",
        "extension_sha256",
        "extractor_sha256",
        "lock_sha256",
        "native_source_digest",
        "source_commit",
        "source_tree",
    }:
        raise WorkerGuardError("baseline capsule manifest has an invalid schema")
    app_hashes = value.get("app_file_sha256")
    lock_hashes = value.get("lock_sha256")
    if (
        not isinstance(app_hashes, dict)
        or not app_hashes
        or not all(
            type(name) is str
            and name.startswith("app/")
            and _valid_sha256(digest)
            for name, digest in app_hashes.items()
        )
        or not isinstance(lock_hashes, dict)
        or set(lock_hashes) != {"native/Cargo.lock", "pyproject.toml", "uv.lock"}
        or not all(_valid_sha256(digest) for digest in lock_hashes.values())
        or type(value.get("extension_relative_path")) is not str
        or not value["extension_relative_path"].startswith("clusy_native/_native.")
        or not _valid_sha256(value.get("extension_sha256"))
        or not _valid_sha256(value.get("extractor_sha256"))
        or not _valid_sha256(value.get("native_source_digest"))
        or type(value.get("source_commit")) is not str
        or len(value["source_commit"]) != 40
        or type(value.get("source_tree")) is not str
        or len(value["source_tree"]) != 40
    ):
        raise WorkerGuardError("baseline capsule manifest is not canonical")
    return value


def _validate_records(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list) or len(value) != EXPECTED_RECORDS:
        raise WorkerGuardError("baseline worker requires exactly 545 records")
    records: list[dict[str, Any]] = []
    for expected_index, record in enumerate(value):
        if not isinstance(record, dict) or set(record) != {
            "dataset_index",
            "raw_html",
        }:
            raise WorkerGuardError("baseline record schema mismatch")
        if (
            type(record["dataset_index"]) is not int
            or record["dataset_index"] != expected_index
            or type(record["raw_html"]) is not str
        ):
            raise WorkerGuardError("baseline record is not canonical")
        record["raw_html"].encode("utf-8")
        records.append(record)
    return tuple(records)


def _settings_identity(settings: object) -> dict[str, Any]:
    model_dump = getattr(settings, "model_dump", None)
    if not callable(model_dump):
        raise WorkerGuardError("production settings do not expose model_dump")
    payload = model_dump(mode="json")
    model_fields = getattr(type(settings), "model_fields", None)
    if not isinstance(payload, dict) or not isinstance(model_fields, dict):
        raise WorkerGuardError("production settings schema is unavailable")
    if set(payload) != set(model_fields):
        raise WorkerGuardError("settings dump does not cover every frozen field")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    active_credentials = [
        name
        for name, value in payload.items()
        if (
            name.endswith("_api_key")
            or name in {"anthropic_api_key", "openai_api_key"}
        )
        and value not in {"", None}
    ]
    if active_credentials:
        raise WorkerGuardError("model or vendor credential is active")
    return {
        "all_fields": payload,
        "field_names": sorted(payload),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def main() -> int:
    bootstrap = assert_fresh_interpreter()
    envelope = read_stdin_envelope(maximum_bytes=768 * 1024 * 1024)
    if set(envelope) != {
        "capsule",
        "concurrency",
        "decision_inputs_sha256",
        "records",
        "schema_version",
    } or envelope.get("schema_version") != INPUT_SCHEMA:
        raise WorkerGuardError("baseline worker envelope schema mismatch")
    if (
        envelope.get("concurrency") != FIXED_CONCURRENCY
        or not _valid_sha256(envelope.get("decision_inputs_sha256"))
    ):
        raise WorkerGuardError("baseline worker fixed protocol mismatch")
    capsule = _validate_capsule_manifest(envelope["capsule"])
    records = _validate_records(envelope["records"])
    runtime_roots = _activate_locked_runtime()

    extractor_module = importlib.import_module("app.services.extractor")
    config_module = importlib.import_module("app.config")
    native_package = importlib.import_module("clusy_native")
    native_extension = importlib.import_module("clusy_native._native")
    extract_content = getattr(extractor_module, "extract_content", None)
    if (
        not callable(extract_content)
        or extract_content is not extractor_module.extract_content
        or extract_content.__module__ != "app.services.extractor"
        or extract_content.__qualname__ != "extract_content"
    ):
        raise WorkerGuardError("production extractor callable identity mismatch")
    extractor_identity = module_identity(
        extractor_module,
        expected_relative="app/services/extractor.py",
    )
    extension_identity = module_identity(
        native_extension,
        expected_relative=capsule["extension_relative_path"],
    )
    if (
        extractor_identity["sha256"] != capsule["extractor_sha256"]
        or extension_identity["sha256"] != capsule["extension_sha256"]
        or native_package.packaged_source_digest()
        != capsule["native_source_digest"]
    ):
        raise WorkerGuardError("executed extractor/native bytes disagree with pins")
    for relative, expected in capsule["app_file_sha256"].items():
        path = Path("/capsule") / relative
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise WorkerGuardError(f"capsule application byte mismatch: {relative}")
    settings_identity = _settings_identity(config_module.settings)
    callable_identity = {
        "code_sha256": hashlib.sha256(
            marshal.dumps(extract_content.__code__)
        ).hexdigest(),
        "module": extract_content.__module__,
        "module_file": extractor_identity,
        "qualname": extract_content.__qualname__,
    }

    output_rows: list[dict[str, Any]] = []
    status_digest_rows: list[dict[str, Any]] = []
    failure_types: Counter[str] = Counter()
    successful = 0
    for record in records:
        prediction = ""
        strategy = "<error>"
        error_type: str | None = None
        try:
            result = extract_content(
                record["raw_html"],
                "",
                extraction_profile=EXTRACTION_PROFILE,
            )
            prediction_value = getattr(result, "text", None)
            if type(prediction_value) is not str:
                raise TypeError("extractor returned non-string text")
            prediction_value.encode("utf-8")
            prediction = prediction_value
            strategy_value = getattr(result, "strategy", "")
            strategy = str(
                getattr(strategy_value, "value", strategy_value) or "<empty>"
            )
            successful += 1
        except Exception as error:
            error_type = type(error).__name__
            failure_types[error_type] += 1
        generation = {
            "error_type": error_type,
            "strategy": strategy,
            "success": error_type is None,
        }
        output_rows.append(
            {
                "dataset_index": record["dataset_index"],
                "generation": generation,
                "prediction": prediction,
            }
        )
        status_digest_rows.append(
            {
                "dataset_index": record["dataset_index"],
                **generation,
                "prediction_sha256": _sha256_text(prediction),
            }
        )

    imports = assert_import_closure()
    baseline_identity = {
        "failed_records": len(output_rows) - successful,
        "failure_types": dict(sorted(failure_types.items())),
        "record_status_sha256": hashlib.sha256(
            json.dumps(
                status_digest_rows,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "records": len(output_rows),
        "successful_records": successful,
    }
    write_canonical_stdout(
        {
            "baseline": baseline_identity,
            "capsule": capsule,
            "decision_inputs_sha256": envelope["decision_inputs_sha256"],
            "executed": {
                "extractor_callable": callable_identity,
                "extension": extension_identity,
                "imported_module_origins": imports,
                "packaged_native_source_digest": native_package.packaged_source_digest(),
            },
            "generator": {
                "concurrency": FIXED_CONCURRENCY,
                "entrypoint": "app.services.extractor.extract_content",
                "extraction_profile": EXTRACTION_PROFILE,
                "input_field": "raw_html",
                "labels_available": False,
                "prediction_transform": "exact ExtractionResult.text",
                "runtime_import_roots": runtime_roots,
                "settings": settings_identity,
            },
            "records": output_rows,
            "runtime": bootstrap,
            "schema_version": OUTPUT_SCHEMA,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

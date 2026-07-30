"""Fresh-interpreter, label-free atomic-overlay decision worker.

This file is never imported by the benchmark scorer. The claim launcher copies
it to ``/capsule/worker.py`` and sends a canonical projection/baseline envelope
on stdin. It emits one frozen decision artifact on stdout.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import sys
import time
from dataclasses import fields
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

INPUT_SCHEMA = "clusy.atomic-overlay-claim-decision-input.3"
OUTPUT_SCHEMA = "clusy.atomic-overlay-frozen-decisions.3"
EXPECTED_RECORDS = 545
FIXED_CONCURRENCY = 4
FIXED_WALL_SECONDS = 180.0
CLAIM_MAX_CERTIFICATE_BYTES = 64 * 1024
CLAIM_MAX_TOTAL_CERTIFICATE_BYTES = 256 * 1024


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _valid_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_capsule_manifest(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {
        "extension_relative_path",
        "extension_sha256",
        "native_source_digest",
        "overlay_sha256",
        "source_commit",
        "source_tree",
    }:
        raise WorkerGuardError("decision capsule manifest has an invalid schema")
    if (
        type(value["extension_relative_path"]) is not str
        or not value["extension_relative_path"].startswith("clusy_native/_native.")
        or "/" in value["extension_relative_path"][len("clusy_native/") :]
        or not _valid_sha256(value["extension_sha256"])
        or not _valid_sha256(value["native_source_digest"])
        or not _valid_sha256(value["overlay_sha256"])
        or type(value["source_commit"]) is not str
        or len(value["source_commit"]) != 40
        or type(value["source_tree"]) is not str
        or len(value["source_tree"]) != 40
    ):
        raise WorkerGuardError("decision capsule manifest is not canonical")
    return {key: str(item) for key, item in value.items()}


def _validate_records(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list) or len(value) != EXPECTED_RECORDS:
        raise WorkerGuardError("decision worker requires exactly 545 records")
    records: list[dict[str, Any]] = []
    for expected_index, record in enumerate(value):
        if not isinstance(record, dict) or set(record) != {
            "baseline_prediction",
            "dataset_index",
            "raw_html",
        }:
            raise WorkerGuardError("decision input record schema mismatch")
        if (
            type(record["dataset_index"]) is not int
            or record["dataset_index"] != expected_index
            or type(record["raw_html"]) is not str
            or type(record["baseline_prediction"]) is not str
        ):
            raise WorkerGuardError("decision input record is not canonical")
        for field in ("raw_html", "baseline_prediction"):
            record[field].encode("utf-8")
        records.append(record)
    return tuple(records)


def _accepted_replacements(
    decision: Any,
    candidate_markdown: str,
) -> dict[str, str]:
    """Derive every accepted replacement from the final output and exact spans."""

    candidate = candidate_markdown.encode("utf-8")
    output = decision.output_markdown.encode("utf-8")
    accepted = sorted(
        (proposal for proposal in decision.proposals if proposal.accepted),
        key=lambda proposal: (
            proposal.candidate_span_start,
            proposal.candidate_span_end,
        ),
    )
    replacements: dict[str, str] = {}
    candidate_cursor = 0
    output_cursor = 0
    for proposal in accepted:
        start = proposal.candidate_span_start
        end = proposal.candidate_span_end
        if (
            type(start) is not int
            or type(end) is not int
            or start < candidate_cursor
            or end <= start
            or end > len(candidate)
            or type(proposal.replacement_bytes) is not int
            or proposal.replacement_bytes <= 0
        ):
            raise WorkerGuardError("accepted proposal span is not replayable")
        unchanged = candidate[candidate_cursor:start]
        unchanged_end = output_cursor + len(unchanged)
        if output[output_cursor:unchanged_end] != unchanged:
            raise WorkerGuardError("accepted output prefix is not byte-identical")
        replacement_end = unchanged_end + proposal.replacement_bytes
        replacement = output[unchanged_end:replacement_end]
        if len(replacement) != proposal.replacement_bytes:
            raise WorkerGuardError("accepted replacement length is inconsistent")
        try:
            replacements[proposal.proposal_id] = replacement.decode("utf-8")
        except UnicodeDecodeError as error:
            raise WorkerGuardError("accepted replacement is not UTF-8") from error
        candidate_cursor = end
        output_cursor = replacement_end
    if output[output_cursor:] != candidate[candidate_cursor:]:
        raise WorkerGuardError("accepted output suffix is not byte-identical")
    if len(replacements) != len(accepted):
        raise WorkerGuardError("accepted proposal IDs are not unique")
    return replacements


def _certificate_markdown(
    html: str,
    proposals: tuple[Any, ...],
    *,
    overlay_module: Any,
    config: Any,
) -> dict[str, str]:
    accepted = tuple(proposal for proposal in proposals if proposal.accepted)
    if not accepted:
        return {}
    limits = overlay_module.DocumentIRV2Limits(
        max_input_bytes=config.max_source_bytes,
        max_nodes=200_000,
        max_elements=100_000,
        max_text_runs=200_000,
        max_depth=256,
        max_text_run_bytes=min(config.max_source_bytes, 256 * 1024),
        max_total_text_bytes=min(8 * 1024 * 1024, config.max_source_bytes * 2),
        max_math_bytes=min(config.max_source_bytes, 256 * 1024),
        max_table_columns=config.max_table_columns,
    )
    document = overlay_module.extract_document_ir_v2(html, limits=limits)
    output: dict[str, str] = {}
    for proposal in accepted:
        replay = overlay_module.verify_and_replay_local_atomic_selection_certificate_v0(
            document,
            proposal.certificate,
            max_output_bytes=config.max_replacement_bytes,
        )
        markdown = replay.markdown
        if type(markdown) is not str:
            raise WorkerGuardError("certificate replay did not return exact Markdown")
        output[proposal.proposal_id] = markdown
    return output


def _proposal_record(
    proposal: Any,
    *,
    replacement_markdown: str | None,
    certificate_markdown: str | None,
) -> dict[str, Any]:
    return {
        "accepted": proposal.accepted,
        "atom_kind": proposal.atom_kind,
        "candidate_span_end": proposal.candidate_span_end,
        "candidate_span_start": proposal.candidate_span_start,
        "certificate_digest": proposal.certificate_digest,
        "certificate_base64": base64.b64encode(proposal.certificate).decode("ascii"),
        "certificate_markdown": certificate_markdown,
        "config_digest": proposal.config_digest,
        "digest_is_authentication": proposal.digest_is_authentication,
        "graph_digest": proposal.graph_digest,
        "growth_bytes": proposal.growth_bytes,
        "input_bytes": proposal.input_bytes,
        "input_digest": proposal.input_digest,
        "patch_digest": proposal.patch_digest,
        "proposal_id": proposal.proposal_id,
        "proposed_output_bytes": proposal.proposed_output_bytes,
        "reason": proposal.reason,
        "replacement_bytes": proposal.replacement_bytes,
        "replacement_digest": proposal.replacement_digest,
        "replacement_markdown": replacement_markdown,
        "schema_version": proposal.schema_version,
        "selected_id": proposal.selected_id,
        "source_digest": proposal.source_digest,
        "source_order": proposal.source_order,
        "source_span_digest": proposal.source_span_digest,
        "source_span_end": proposal.source_span_end,
        "source_span_start": proposal.source_span_start,
        "structural_score_after": proposal.structural_score_after,
        "structural_score_before": proposal.structural_score_before,
        "visible_token_count": proposal.visible_token_count,
        "visible_token_digest": proposal.visible_token_digest,
    }


def _observe(
    html: str,
    baseline_prediction: str,
    *,
    config: Any,
    proposer: Any,
    verifier: Any,
    monotonic_ns: Any,
    overlay_module: Any,
) -> dict[str, Any]:
    started = monotonic_ns()
    decision = proposer(html, baseline_prediction, config=config)
    decision_elapsed_ns = monotonic_ns() - started
    replay_started = monotonic_ns()
    replay = verifier(
        html,
        baseline_prediction,
        decision,
        config=config,
    )
    replay_elapsed_ns = monotonic_ns() - replay_started
    if (
        replay.verified is not True
        or replay.output_markdown != decision.output_markdown
        or replay.decision_digest != decision.decision_digest
        or (
            not decision.accepted
            and decision.output_markdown != baseline_prediction
        )
        or decision.visible_tokens_identical is not True
    ):
        raise WorkerGuardError("decision/replay integrity failed")
    replacements = _accepted_replacements(decision, baseline_prediction)
    certificate_markdown = _certificate_markdown(
        html,
        decision.proposals,
        overlay_module=overlay_module,
        config=config,
    )
    accepted_ids = {
        proposal.proposal_id for proposal in decision.proposals if proposal.accepted
    }
    if set(replacements) != accepted_ids or set(certificate_markdown) != accepted_ids:
        raise WorkerGuardError("accepted proposal payload inventory mismatch")
    return {
        "accepted": decision.accepted,
        "applied_proposal_ids": list(decision.applied_proposal_ids),
        "candidate_markdown_sha256": _sha256_text(decision.candidate_markdown),
        "config_digest": decision.config_digest,
        "decision_digest": decision.decision_digest,
        "digest_is_authentication": decision.digest_is_authentication,
        "enabled": decision.enabled,
        "growth_bytes": decision.growth_bytes,
        "input_bytes": decision.input_bytes,
        "input_digest": decision.input_digest,
        "output_bytes": decision.output_bytes,
        "output_digest": decision.output_digest,
        "output_markdown": decision.output_markdown,
        "proposals": [
            _proposal_record(
                item,
                replacement_markdown=replacements.get(item.proposal_id),
                certificate_markdown=certificate_markdown.get(item.proposal_id),
            )
            for item in decision.proposals
        ],
        "reason": decision.reason,
        "replay": {
            "decision_digest": replay.decision_digest,
            "output_digest": replay.output_digest,
            "reason": replay.reason,
            "verified": replay.verified,
        },
        "schema_version": decision.schema_version,
        "source_digest": decision.source_digest,
        "timing": {
            "decision_elapsed_ns": decision_elapsed_ns,
            "replay_elapsed_ns": replay_elapsed_ns,
        },
        "visible_token_digest": decision.visible_token_digest,
        "visible_tokens_identical": decision.visible_tokens_identical,
    }


def main() -> int:
    bootstrap = assert_fresh_interpreter()
    envelope = read_stdin_envelope(maximum_bytes=768 * 1024 * 1024)
    if set(envelope) != {
        "baseline_sha256",
        "capsule",
        "concurrency",
        "decision_inputs_sha256",
        "records",
        "schema_version",
        "wall_seconds",
    } or envelope.get("schema_version") != INPUT_SCHEMA:
        raise WorkerGuardError("decision worker envelope schema mismatch")
    if (
        envelope.get("concurrency") != FIXED_CONCURRENCY
        or envelope.get("wall_seconds") != FIXED_WALL_SECONDS
        or not _valid_sha256(envelope.get("baseline_sha256"))
        or not _valid_sha256(envelope.get("decision_inputs_sha256"))
    ):
        raise WorkerGuardError("decision worker protocol constants are not fixed")
    capsule = _validate_capsule_manifest(envelope["capsule"])
    records = _validate_records(envelope["records"])

    monotonic_ns = time.monotonic_ns
    overlay_module = importlib.import_module(
        "app.services.atomic_structure_overlay_v0"
    )
    native_package = importlib.import_module("clusy_native")
    native_extension = importlib.import_module("clusy_native._native")
    overlay_identity = module_identity(
        overlay_module,
        expected_relative="app/services/atomic_structure_overlay_v0.py",
    )
    extension_identity = module_identity(
        native_extension,
        expected_relative=capsule["extension_relative_path"],
    )
    if (
        overlay_identity["sha256"] != capsule["overlay_sha256"]
        or extension_identity["sha256"] != capsule["extension_sha256"]
        or native_package.packaged_source_digest()
        != capsule["native_source_digest"]
    ):
        raise WorkerGuardError("executed Python/native bytes disagree with capsule pins")
    config = overlay_module.AtomicStructureOverlayV0Config(
        enabled=True,
        max_certificate_bytes=CLAIM_MAX_CERTIFICATE_BYTES,
        max_total_certificate_bytes=CLAIM_MAX_TOTAL_CERTIFICATE_BYTES,
    )
    config_values = {
        field.name: object.__getattribute__(config, field.name)
        for field in fields(config)
    }
    if any(type(value) not in {bool, int} for value in config_values.values()):
        raise WorkerGuardError("overlay config contains a non-primitive field")
    proposer = overlay_module.propose_atomic_structure_overlay_v0
    verifier = overlay_module.verify_atomic_structure_overlay_v0

    import concurrent.futures

    worker_started = monotonic_ns()

    def observe_record(record: dict[str, Any]) -> dict[str, Any]:
        observation = _observe(
            record["raw_html"],
            record["baseline_prediction"],
            config=config,
            proposer=proposer,
            verifier=verifier,
            monotonic_ns=monotonic_ns,
            overlay_module=overlay_module,
        )
        return {
            "baseline_prediction_sha256": _sha256_text(
                record["baseline_prediction"]
            ),
            "dataset_index": record["dataset_index"],
            "decision": observation,
            "raw_html_sha256": _sha256_text(record["raw_html"]),
        }

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=FIXED_CONCURRENCY
    ) as pool:
        decisions = list(pool.map(observe_record, records))
    worker_wall_ns = monotonic_ns() - worker_started
    imports = assert_import_closure()
    result = {
        "baseline_sha256": envelope["baseline_sha256"],
        "capsule": capsule,
        "decision_inputs_sha256": envelope["decision_inputs_sha256"],
        "decisions": decisions,
        "executed": {
            "extension": extension_identity,
            "imported_module_origins": imports,
            "overlay": overlay_identity,
            "packaged_native_source_digest": native_package.packaged_source_digest(),
        },
        "protocol": {
            "concurrency": FIXED_CONCURRENCY,
            "config": config_values,
            "dataset_evaluator_scorer_mounted": False,
            "labels_available": False,
            "wall_seconds": FIXED_WALL_SECONDS,
        },
        "runtime": bootstrap,
        "schema_version": OUTPUT_SCHEMA,
        "worker_wall_ns": worker_wall_ns,
    }
    write_canonical_stdout(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

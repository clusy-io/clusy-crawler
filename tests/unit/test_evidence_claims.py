from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts import check_evidence_claims as evidence

ROOT = Path(__file__).resolve().parents[2]
CLAIM_ID = "aeb.article-body.2026-07-29.fixture"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_registry(root: Path, registry: dict[str, Any]) -> None:
    _write_json(root / evidence.REGISTRY_PATH, registry)


def _fixture_repository(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    schema_source = ROOT / evidence.SCHEMA_PATH
    schema_target = tmp_path / evidence.SCHEMA_PATH
    schema_target.parent.mkdir(parents=True)
    schema_target.write_bytes(schema_source.read_bytes())

    protocol_path = tmp_path / "bench/evidence/aeb-fixture/PROTOCOL.md"
    protocol_path.parent.mkdir(parents=True)
    protocol_path.write_text("# Frozen fixture protocol\n", encoding="utf-8")
    report = {
        "metrics": {
            "f1": "0.9721267",
        },
        "permissions": {
            "scoped_sota": False,
            "scoped_superiority": False,
            "vendor_superiority": False,
        },
        "verification": {
            "artifact_complete": True,
            "claimable": True,
            "source_stable": True,
        },
    }
    report_path = tmp_path / "bench/evidence/aeb-fixture/report.json"
    _write_json(report_path, report)

    claim = {
        "artifact": {
            "path": "bench/evidence/aeb-fixture/report.json",
            "raw_archive_sha256": None,
            "raw_manifest_sha256": "a" * 64,
            "raw_retention": "repository",
            "sha256": _sha256(report_path),
        },
        "id": CLAIM_ID,
        "kind": "benchmark_result",
        "metrics": [
            {
                "aggregation": "macro F1",
                "artifact_pointer": "/metrics/f1",
                "display": "0.972127",
                "format": ".6f",
                "key": "f1",
                "unit": "ratio",
                "value": "0.9721267",
            }
        ],
        "permissions": {
            "current_production": False,
            "metric": True,
            "scoped_sota": {
                "allowed": False,
                "artifact_pointer": None,
            },
            "scoped_superiority": {
                "allowed": False,
                "artifact_pointer": None,
            },
            "vendor_superiority": {
                "allowed": False,
                "artifact_pointer": None,
            },
        },
        "protocol": {
            "path": "bench/evidence/aeb-fixture/PROTOCOL.md",
            "sha256": _sha256(protocol_path),
        },
        "recorded_at_utc": "2026-07-29T10:15:52Z",
        "scope": {
            "comparators": [],
            "dataset": "AEB",
            "dataset_revision": "fixture-v1",
            "dataset_sha256": "b" * 64,
            "execution_boundary": "local_closed_loop_extraction",
            "limitations": [
                "not crawling",
            ],
            "output_contract": "production async body, identity transform",
            "pages": 181,
            "profile": "article_body",
            "split": "full",
            "task": "article-body extraction",
            "vendors": [
                "Clusy",
            ],
        },
        "source": {
            "clean": True,
            "commit": "c" * 40,
            "repository": "fixture",
            "runtime_source_sha256": "d" * 64,
            "tree": "e" * 40,
        },
        "status": "Verified",
        "verification": {
            "artifact_complete_pointer": "/verification/artifact_complete",
            "binding_pointer": "/claim_binding",
            "claimable_pointer": "/verification/claimable",
            "source_stable_pointer": "/verification/source_stable",
        },
    }
    report["claim_binding"] = {
        "id": claim["id"],
        "kind": claim["kind"],
        "protocol_sha256": claim["protocol"]["sha256"],
        "raw_archive_sha256": claim["artifact"]["raw_archive_sha256"],
        "raw_manifest_sha256": claim["artifact"]["raw_manifest_sha256"],
        "recorded_at_utc": claim["recorded_at_utc"],
        "scope": claim["scope"],
        "source": claim["source"],
        "status": claim["status"],
    }
    _write_json(report_path, report)
    claim["artifact"]["sha256"] = _sha256(report_path)
    registry = {
        "claims": [
            claim,
        ],
        "documentation": {
            "enforced_files": [
                "README.md",
                "docs/BENCHMARKS.md",
            ],
            "marker_format": evidence.MARKER_FORMAT,
            "migration_status": "enforced",
        },
        "registry_id": evidence.REGISTRY_ID,
        "registry_note": "Strict unit-test fixture.",
        "schema": {
            "path": evidence.SCHEMA_PATH.as_posix(),
            "sha256": _sha256(schema_target),
        },
        "schema_version": 1,
    }
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text(
        "Measured F1 `0.972127`. "
        f"<!-- clusy-evidence: {CLAIM_ID} -->\n",
        encoding="utf-8",
    )
    (tmp_path / "docs/BENCHMARKS.md").write_text(
        "# Evidence\n",
        encoding="utf-8",
    )
    _write_registry(tmp_path, registry)
    return registry, report


def test_repository_registry_is_valid_and_enforced() -> None:
    assert evidence.validate_repository(ROOT) == []
    registry = json.loads((ROOT / evidence.REGISTRY_PATH).read_text(encoding="utf-8"))
    assert registry["claims"]
    assert registry["documentation"]["migration_status"] == "enforced"
    claim_ids = [claim["id"] for claim in registry["claims"]]
    assert len(claim_ids) == len(set(claim_ids))


def test_complete_registered_claim_and_document_marker_pass(tmp_path: Path) -> None:
    _fixture_repository(tmp_path)

    assert evidence.validate_repository(tmp_path) == []


def test_artifact_tampering_breaks_hash_and_metric_binding(tmp_path: Path) -> None:
    _fixture_repository(tmp_path)
    report_path = tmp_path / "bench/evidence/aeb-fixture/report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["metrics"]["f1"] = "0.5000000"
    _write_json(report_path, report)

    errors = evidence.validate_repository(tmp_path)

    assert any("artifact.sha256: SHA-256 mismatch" in error for error in errors)
    assert any("registered value differs from artifact target" in error for error in errors)


def test_schema_hash_tampering_fails(tmp_path: Path) -> None:
    _fixture_repository(tmp_path)
    schema_path = tmp_path / evidence.SCHEMA_PATH
    schema_path.write_bytes(schema_path.read_bytes() + b"\n")

    errors = evidence.validate_repository(tmp_path)

    assert any("schema.sha256: SHA-256 mismatch" in error for error in errors)


def test_unknown_registry_or_claim_keys_fail_closed(tmp_path: Path) -> None:
    registry, _report = _fixture_repository(tmp_path)
    registry["claims"][0]["unreviewed_escape_hatch"] = True
    _write_registry(tmp_path, registry)

    errors = evidence.validate_repository(tmp_path)

    assert any("unknown keys: unreviewed_escape_hatch" in error for error in errors)


def test_duplicate_claim_ids_fail_closed(tmp_path: Path) -> None:
    registry, _report = _fixture_repository(tmp_path)
    registry["claims"].append(json.loads(json.dumps(registry["claims"][0])))
    _write_registry(tmp_path, registry)

    errors = evidence.validate_repository(tmp_path)

    assert any("duplicate claim id" in error for error in errors)


def test_claim_paths_cannot_escape_repository(tmp_path: Path) -> None:
    registry, _report = _fixture_repository(tmp_path)
    registry["claims"][0]["protocol"]["path"] = "../outside.md"
    _write_registry(tmp_path, registry)

    errors = evidence.validate_repository(tmp_path)

    assert any("unsafe repository-relative path" in error for error in errors)


def test_verified_claim_requires_clean_source_and_true_verification_gates(
    tmp_path: Path,
) -> None:
    registry, report = _fixture_repository(tmp_path)
    report = json.loads(json.dumps(report))
    registry["claims"][0]["source"]["clean"] = False
    report["verification"]["source_stable"] = False
    report_path = tmp_path / "bench/evidence/aeb-fixture/report.json"
    _write_json(report_path, report)
    registry["claims"][0]["artifact"]["sha256"] = _sha256(report_path)
    _write_registry(tmp_path, registry)

    errors = evidence.validate_repository(tmp_path)

    assert any("Verified claims require clean source" in error for error in errors)
    assert any("artifact claim binding is not exact" in error for error in errors)
    assert any(
        "source_stable_pointer: Verified claim requires an exact true gate" in error
        for error in errors
    )


def test_unbound_metric_line_fails_when_document_enforcement_is_enabled(
    tmp_path: Path,
) -> None:
    _fixture_repository(tmp_path)
    (tmp_path / "README.md").write_text(
        "Measured F1 `0.999999`.\n",
        encoding="utf-8",
    )

    errors = evidence.validate_repository(tmp_path)

    assert any("evidence-bearing line requires exactly one marker" in error for error in errors)


def test_registered_metric_requires_publication_permission(tmp_path: Path) -> None:
    registry, _report = _fixture_repository(tmp_path)
    registry["claims"][0]["permissions"]["metric"] = False
    _write_registry(tmp_path, registry)

    errors = evidence.validate_repository(tmp_path)

    assert any("metric publication is not permitted" in error for error in errors)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            f"Clusy is better than Exa. <!-- clusy-evidence: {CLAIM_ID} -->\n",
            "positive superiority language is not permitted",
        ),
        (
            f"Clusy is SOTA on AEB. <!-- clusy-evidence: {CLAIM_ID} -->\n",
            "positive SOTA language is not permitted",
        ),
        (
            "The current production revision is healthy.\n",
            "current-production claims are forbidden",
        ),
        (
            "Clusy is all-around SOTA.\n",
            "universal/all-around SOTA claims are forbidden",
        ),
        (
            "This is not a benchmark, but Clusy beats Exa.\n",
            "evidence-bearing line requires exactly one marker",
        ),
        (
            "Clusy是全方位SOTA。\n",
            "universal/all-around SOTA claims are forbidden",
        ),
    ],
)
def test_unsupported_marketing_claims_fail(
    tmp_path: Path,
    line: str,
    expected: str,
) -> None:
    _fixture_repository(tmp_path)
    (tmp_path / "README.md").write_text(line, encoding="utf-8")

    errors = evidence.validate_repository(tmp_path)

    assert any(expected in error for error in errors)


def test_negative_sota_and_vendor_boundary_text_does_not_need_marker(
    tmp_path: Path,
) -> None:
    _fixture_repository(tmp_path)
    (tmp_path / "README.md").write_text(
        "This does not establish SOTA and is not better than Exa or Firecrawl.\n",
        encoding="utf-8",
    )

    assert evidence.validate_repository(tmp_path) == []


def test_diagnostic_claim_requires_false_claimability_and_visible_status(
    tmp_path: Path,
) -> None:
    registry, report = _fixture_repository(tmp_path)
    registry["claims"][0]["status"] = "Diagnostic"
    report["verification"]["claimable"] = False
    report_path = tmp_path / "bench/evidence/aeb-fixture/report.json"
    _write_json(report_path, report)
    registry["claims"][0]["artifact"]["sha256"] = _sha256(report_path)
    _write_registry(tmp_path, registry)

    errors = evidence.validate_repository(tmp_path)

    assert any("Diagnostic claim must show its status" in error for error in errors)


def test_unknown_marker_fails_even_before_document_migration(tmp_path: Path) -> None:
    registry, _report = _fixture_repository(tmp_path)
    registry["claims"] = []
    registry["documentation"] = {
        "enforced_files": [],
        "marker_format": evidence.MARKER_FORMAT,
        "migration_status": "pending",
    }
    (tmp_path / "README.md").write_text(
        "<!-- clusy-evidence: missing.claim -->\n",
        encoding="utf-8",
    )
    _write_registry(tmp_path, registry)

    errors = evidence.validate_repository(tmp_path)

    assert any("unknown evidence claim missing.claim" in error for error in errors)


def test_malformed_marker_fails_before_document_migration(tmp_path: Path) -> None:
    registry, _report = _fixture_repository(tmp_path)
    registry["claims"] = []
    registry["documentation"] = {
        "enforced_files": [],
        "marker_format": evidence.MARKER_FORMAT,
        "migration_status": "pending",
    }
    (tmp_path / "README.md").write_text(
        "<!-- clusy-evidence: malformed claim -->\n",
        encoding="utf-8",
    )
    _write_registry(tmp_path, registry)

    errors = evidence.validate_repository(tmp_path)

    assert any("malformed evidence marker" in error for error in errors)


def test_duplicate_json_key_is_rejected(tmp_path: Path) -> None:
    _fixture_repository(tmp_path)
    registry_path = tmp_path / evidence.REGISTRY_PATH
    registry_path.write_text(
        '{"schema_version":1,"schema_version":1}\n',
        encoding="utf-8",
    )

    errors = evidence.validate_repository(tmp_path)

    assert any("duplicate JSON key: schema_version" in error for error in errors)

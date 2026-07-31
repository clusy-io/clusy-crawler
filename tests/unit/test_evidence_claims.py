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
    report: dict[str, Any] = {
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

    claim: dict[str, Any] = {
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
                "publication_aliases": [
                    "f measure",
                    "f-score",
                    "f1",
                ],
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
                "bench/NEUTRAL_BENCHMARK.md",
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
    canonical_line = evidence._canonical_claim_publication_line(claim)
    assert canonical_line is not None
    (tmp_path / "README.md").write_text(
        f"{canonical_line}\n",
        encoding="utf-8",
    )
    (tmp_path / "docs/BENCHMARKS.md").write_text(
        "# Evidence\n",
        encoding="utf-8",
    )
    (tmp_path / "bench/NEUTRAL_BENCHMARK.md").write_text(
        "# Current evidence protocol\n",
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


def test_enforced_mode_requires_every_current_result_surface(tmp_path: Path) -> None:
    registry, _report = _fixture_repository(tmp_path)
    registry["documentation"]["enforced_files"].remove("bench/NEUTRAL_BENCHMARK.md")
    _write_registry(tmp_path, registry)

    errors = evidence.validate_repository(tmp_path)

    assert any(
        "enforced mode requires" in error
        and "bench/NEUTRAL_BENCHMARK.md" in error
        for error in errors
    )


def test_private_lineage_fails_in_unenforced_first_party_markdown(
    tmp_path: Path,
) -> None:
    _fixture_repository(tmp_path)
    (tmp_path / "docs/ARCHIVE.md").write_text(
        "Retained clean private-source validation.\n",
        encoding="utf-8",
    )

    errors = evidence.validate_repository(tmp_path)

    assert any("restricted evidence lineage is forbidden" in error for error in errors)


@pytest.mark.parametrize(
    "publication",
    [
        "Retained clean pri**vate-source** validation.",
        "Retained clean pri<!-- split -->vate-source validation.",
        "Hosted pri&#118;ate revision evidence.",
    ],
)
def test_reader_visible_private_lineage_disguises_fail(
    tmp_path: Path,
    publication: str,
) -> None:
    _fixture_repository(tmp_path)
    (tmp_path / "docs/ARCHIVE.md").write_text(
        f"{publication}\n",
        encoding="utf-8",
    )

    errors = evidence.validate_repository(tmp_path)

    assert any("restricted evidence lineage is forbidden" in error for error in errors)


def test_current_production_claim_fails_in_unenforced_markdown(tmp_path: Path) -> None:
    _fixture_repository(tmp_path)
    (tmp_path / "docs/ARCHIVE.md").write_text(
        "The current production revision passed the benchmark.\n",
        encoding="utf-8",
    )

    errors = evidence.validate_repository(tmp_path)

    assert any("current-production claims are forbidden" in error for error in errors)


@pytest.mark.parametrize(
    ("relative", "claim_line"),
    [
        ("docs/RESEARCH.md", "Clusy F1 is 0.999.\n"),
        ("docs/RESEARCH.md", "Latest run: F1 `0.999`.\n"),
        ("docs/RESEARCH.md", "We get F1 0.999.\n"),
        ("docs/RESEARCH.md", "AEB result — F1 `0.999`.\n"),
        ("docs/RESEARCH.md", "The latest model gets F1 0.999.\n"),
        ("docs/RESEARCH.md", "Latest run achieved F1 at least 0.999.\n"),
        ("docs/RESEARCH.md", "Latest run: F1 0.999, above zero.\n"),
        ("docs/RESEARCH.md", "Clusy F1 is 0.999; target was 0.900.\n"),
        (
            "docs/RESEARCH.md",
            "Clusy measured F1 0.999 and exceeded the threshold.\n",
        ),
        ("docs/RESEARCH.md", "Clusy F1 0.999, within benchmark.\n"),
        ("docs/RESEARCH.md", "Clusy scored 0.999 F1.\n"),
        ("docs/RESEARCH.md", "Clusy posted a 0.999 F1 result.\n"),
        ("docs/RESEARCH.md", "Our score: 0.999 F1.\n"),
        ("docs/RESEARCH.md", "Clusy achieved an F-score of 0.999.\n"),
        ("docs/RESEARCH.md", "Clusy achieved an F-measure of .999.\n"),
        ("docs/RESEARCH.md", "Clusy posted 9.99e-1 F1.\n"),
        ("docs/RESEARCH.md", "The model returned 0.999 precision.\n"),
        ("docs/RESEARCH.md", "Clusy scored 99.9% on AEB.\n"),
        ("docs/RESEARCH.md", "AEB score: 0.999.\n"),
        ("docs/RESEARCH.md", "Clusy reached .999 on WebMainBench.\n"),
        ("docs/RESEARCH.md", "Our benchmark result was 9.99e-1.\n"),
        ("docs/RESEARCH.md", "AEB result: 0.999.\n"),
        ("docs/RESEARCH.md", "Result on WebMainBench: 0.999.\n"),
        ("docs/RESEARCH.md", "Clusy got 0.999 on AEB.\n"),
        ("docs/RESEARCH.md", "Clusy has 0.999 on WebMainBench.\n"),
        ("docs/RESEARCH.md", "We got .999 on WCXB.\n"),
        ("docs/RESEARCH.md", "Ours: 0.999 on AEB.\n"),
        ("docs/RESEARCH.md", "Clusy — 0.999 on AEB.\n"),
        ("docs/RESEARCH.md", "The crawler clocked 99.9% on Webis.\n"),
        ("docs/RESEARCH.md", "AEB: 0.999.\n"),
        ("bench/WCXB_BENCHMARK.md", "Clusy beats Trafilatura on WCXB.\n"),
        ("docs/RESEARCH.md", "Clusy beats Exa with F1 0.999.\n"),
        ("docs/RESEARCH.md", "Clusy is SOTA on WebMainBench, F1 0.999.\n"),
    ],
)
def test_unregistered_claim_fails_in_any_first_party_markdown(
    tmp_path: Path,
    relative: str,
    claim_line: str,
) -> None:
    _fixture_repository(tmp_path)
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(claim_line, encoding="utf-8")

    errors = evidence.validate_repository(tmp_path)

    assert any(
        f"{relative}:1: evidence-bearing line requires exactly one marker" in error
        for error in errors
    )


def test_protocol_threshold_without_a_result_claim_is_allowed(tmp_path: Path) -> None:
    _fixture_repository(tmp_path)
    (tmp_path / "docs/RESEARCH.md").write_text(
        "- Threshold: table_teds_delta >= 0.02 score. "
        f"{evidence.PROTOCOL_THRESHOLD_MARKER}\n",
        encoding="utf-8",
    )

    assert evidence.validate_repository(tmp_path) == []


def test_protocol_metric_threshold_requires_the_exact_marker(tmp_path: Path) -> None:
    _fixture_repository(tmp_path)
    (tmp_path / "docs/RESEARCH.md").write_text(
        "- Threshold: table_teds_delta >= 0.02 score.\n",
        encoding="utf-8",
    )

    errors = evidence.validate_repository(tmp_path)

    assert any(
        "evidence-bearing line requires exactly one marker" in error
        for error in errors
    )


@pytest.mark.parametrize(
    "claim_line",
    [
        "Latest run: F1 0.999, above zero.",
        "Clusy F1 is 0.999; target was 0.900.",
        "Clusy measured F1 0.999 and exceeded the threshold.",
        "Clusy F1 0.999, within benchmark.",
        "Reported F1 0.999, above zero.",
        "Posted F1 0.999; threshold passed.",
        "Model X has F1 0.999, above zero.",
    ],
)
def test_protocol_threshold_marker_cannot_whitewash_a_result_claim(
    tmp_path: Path,
    claim_line: str,
) -> None:
    _fixture_repository(tmp_path)
    (tmp_path / "docs/RESEARCH.md").write_text(
        f"{claim_line} {evidence.PROTOCOL_THRESHOLD_MARKER}\n",
        encoding="utf-8",
    )

    errors = evidence.validate_repository(tmp_path)

    assert any(
        "protocol-threshold marker requires a non-result metric threshold" in error
        for error in errors
    )
    assert any(
        "evidence-bearing line requires exactly one marker" in error
        for error in errors
    )


def test_one_protocol_threshold_marker_cannot_cover_two_metric_values(
    tmp_path: Path,
) -> None:
    _fixture_repository(tmp_path)
    (tmp_path / "docs/RESEARCH.md").write_text(
        "- Threshold: f1 >= 0.90 score and recall >= 0.80 score. "
        f"{evidence.PROTOCOL_THRESHOLD_MARKER}\n",
        encoding="utf-8",
    )

    errors = evidence.validate_repository(tmp_path)

    assert any(
        "protocol-threshold marker requires a non-result metric threshold" in error
        for error in errors
    )
    assert any(
        "evidence-bearing line requires exactly one marker" in error
        for error in errors
    )


@pytest.mark.parametrize(
    "threshold_line",
    [
        "- Threshold: f1 >= 0.90 score; latest result is 0.99.",
        "- Threshold: f1 >= 0.90 score. extra clause.",
        "- Threshold: f1 >= 0.90 bananas.",
        "Threshold: f1 >= 0.90 score.",
    ],
)
def test_protocol_threshold_requires_exact_full_line_grammar(
    tmp_path: Path,
    threshold_line: str,
) -> None:
    _fixture_repository(tmp_path)
    (tmp_path / "docs/RESEARCH.md").write_text(
        f"{threshold_line} {evidence.PROTOCOL_THRESHOLD_MARKER}\n",
        encoding="utf-8",
    )

    errors = evidence.validate_repository(tmp_path)

    assert any(
        "protocol-threshold marker requires a non-result metric threshold" in error
        for error in errors
    )


@pytest.mark.parametrize("wrong_label", ["Recall", "Precision"])
def test_evidence_marker_cannot_relabel_a_registered_metric(
    tmp_path: Path,
    wrong_label: str,
) -> None:
    _fixture_repository(tmp_path)
    (tmp_path / "README.md").write_text(
        f"{wrong_label} 0.972127. <!-- clusy-evidence: {CLAIM_ID} -->\n",
        encoding="utf-8",
    )

    errors = evidence.validate_repository(tmp_path)

    assert any(
        f"metric label {wrong_label.casefold()!r} is not a registered publication alias"
        in error
        for error in errors
    )


@pytest.mark.parametrize(
    "claim_line",
    [
        "Clusy reported 181.",
        "Clusy reported 0.972127.",
        "Our crawler achieved 0.972127.",
    ],
)
def test_evidence_marker_requires_a_label_for_each_asserted_result_value(
    tmp_path: Path,
    claim_line: str,
) -> None:
    _fixture_repository(tmp_path)
    (tmp_path / "README.md").write_text(
        f"{claim_line} <!-- clusy-evidence: {CLAIM_ID} -->\n",
        encoding="utf-8",
    )

    errors = evidence.validate_repository(tmp_path)

    assert any(
        "asserted result value" in error
        and "has no registered metric label-value binding" in error
        for error in errors
    )


@pytest.mark.parametrize(
    "claim_line",
    [
        "Clusy F1 0.957546.",
        "Trafilatura 2.1.0 F1 0.972127.",
    ],
)
def test_candidate_and_comparator_metric_values_cannot_be_swapped(
    claim_line: str,
) -> None:
    registry = json.loads((ROOT / evidence.REGISTRY_PATH).read_text(encoding="utf-8"))
    claim = registry["claims"][0]
    errors: list[str] = []

    evidence._validate_claim_line(
        "README.md",
        1,
        f"{claim_line} <!-- clusy-evidence: {claim['id']} -->",
        claim,
        errors,
    )

    assert any("is not bound to published value" in error for error in errors)


def test_registered_metric_label_cannot_borrow_another_claim_token(
    tmp_path: Path,
) -> None:
    _fixture_repository(tmp_path)
    (tmp_path / "README.md").write_text(
        f"F1 181. <!-- clusy-evidence: {CLAIM_ID} -->\n",
        encoding="utf-8",
    )

    errors = evidence.validate_repository(tmp_path)

    assert any(
        "metric label 'f1' is not bound to published value '181'" in error
        for error in errors
    )


def test_evidence_marker_cannot_turn_generic_score_into_f1(tmp_path: Path) -> None:
    _fixture_repository(tmp_path)
    (tmp_path / "README.md").write_text(
        f"AEB score: 0.972127. <!-- clusy-evidence: {CLAIM_ID} -->\n",
        encoding="utf-8",
    )

    errors = evidence.validate_repository(tmp_path)

    assert any(
        "metric label 'aeb score' is not a registered publication alias" in error
        for error in errors
    )


def test_registered_alias_cannot_bypass_canonical_publication_line(tmp_path: Path) -> None:
    _fixture_repository(tmp_path)
    (tmp_path / "README.md").write_text(
        f"Measured F-score `0.972127`. <!-- clusy-evidence: {CLAIM_ID} -->\n",
        encoding="utf-8",
    )

    errors = evidence.validate_repository(tmp_path)

    assert any(
        "evidence marker is valid only on the exact canonical registry-derived "
        "publication line" in error
        for error in errors
    )


@pytest.mark.parametrize(
    "publication",
    [
        "| Clusy | 0.972127 |",
        "0.972127; F1.",
        "Clusy F<sub>1</sub> 0.972127.",
        "Clusy F1 was\n0.972127.",
        "Clusy result: 0.972127.",
        "AEB score approximately 0.972127.",
        "Clusy earned 0.972127 in AEB.",
        "Preci**sion** 0.972127.",
    ],
)
def test_marker_rejects_every_noncanonical_reader_visible_publication(
    tmp_path: Path,
    publication: str,
) -> None:
    _fixture_repository(tmp_path)
    (tmp_path / "README.md").write_text(
        f"{publication} <!-- clusy-evidence: {CLAIM_ID} -->\n",
        encoding="utf-8",
    )

    errors = evidence.validate_repository(tmp_path)

    assert any(
        "evidence marker is valid only on the exact canonical registry-derived "
        "publication line" in error
        for error in errors
    )


@pytest.mark.parametrize(
    "publication",
    [
        "| Clusy | 0.972127 |",
        "0.972127; Clusy F1.",
        "Clusy F<sub>1</sub> 0.972127.",
        "Clusy F1 was\n0.972127.",
        "Clusy result: 0.972127.",
        "AEB score approximately 0.972127.",
        "Clusy earned 0.972127 in AEB.",
        "Preci**sion** 0.972127.",
        "Rec<!-- split -->all 0.972127.",
        "[Rec](#metric)all 0.972127.",
        "Clusy F1:\n\n0.972127.",
        "### Clusy F1\n\n**0.972127**",
        "AEB score:\n\n0.972127.",
        "Rec\u200ball 0.972127.",
        "Rec&shy;all 0.972127.",
        "Clusy F1:\n\n\n\n0.972127.",
        "### Clusy F1\n\nMeasured value follows.\n\n0.972127.",
        (
            "### Clusy F1\n\n"
            "Profile: article body.\n\n"
            "Split: full corpus.\n\n"
            "0.999."
        ),
        "```text\nClusy F1 0.999\n```",
        '```json\n{"Clusy F1": 0.999}\n```',
        "### Clusy F1\n\n#### Full corpus\n\n0.999.",
        "Clusy F1:\n\n```text\n0.999\n```",
        "AEB score:\n\n```json\n0.999\n```",
        "```text\nClusy F1\n```\n\n0.999.",
        "```text\nClusy F1\n```\n\n```text\n0.999\n```",
        "F₁ = 0.999.",
        "F１ = 0.999.",
    ],
)
def test_reader_visible_normalization_rejects_unmarked_claim_disguises(
    tmp_path: Path,
    publication: str,
) -> None:
    _fixture_repository(tmp_path)
    (tmp_path / "README.md").write_text(f"{publication}\n", encoding="utf-8")

    errors = evidence.validate_repository(tmp_path)

    assert any(
        "reader-visible measured claim must use the exact canonical "
        "registry-derived publication line" in error
        for error in errors
    )


def test_reader_visible_claim_binding_stops_at_a_new_heading(
    tmp_path: Path,
) -> None:
    _fixture_repository(tmp_path)
    (tmp_path / "README.md").write_text(
        "### Clusy F1\n\n"
        "No measured value is published here.\n\n"
        "### Runtime\n\n"
        "Python 3.11 or newer.\n",
        encoding="utf-8",
    )

    assert evidence.validate_repository(tmp_path) == []


def test_continuous_integration_abbreviation_is_not_a_metric_claim(
    tmp_path: Path,
) -> None:
    _fixture_repository(tmp_path)
    (tmp_path / "README.md").write_text(
        "## Development\n\n"
        "```bash\n"
        "cargo +1.85 test --locked\n"
        "```\n\n"
        "CI builds require the relevant benchmark before publication.\n",
        encoding="utf-8",
    )

    assert evidence.validate_repository(tmp_path) == []


@pytest.mark.parametrize(
    "claim_line",
    [
        "Clusy surpasses Trafilatura 2.1.0.",
        "Clusy exceeds Trafilatura 2.1.0.",
        "Clusy leads Trafilatura 2.1.0.",
        "Clusy tops Trafilatura 2.1.0.",
        "Clusy ranks ahead of Trafilatura 2.1.0.",
        "Clusy is higher-scoring than Trafilatura 2.1.0.",
        "Clusy has the best F1 among these systems.",
        "Clusy dominates Exa.",
        "Clusy is the leading alternative to Firecrawl.",
        "Clusy edges out Trafilatura 2.1.0.",
        "Clusy comes ahead of Trafilatura 2.1.0.",
        "Clusy comes out ahead of Trafilatura 2.1.0.",
        "Clusy remains ahead of Trafilatura 2.1.0.",
        "Clusy is stronger than Trafilatura 2.1.0.",
        "Clusy has higher F1 than Trafilatura 2.1.0.",
        "Clusy has lower error than Trafilatura 2.1.0.",
        "Clusy has less error than Trafilatura 2.1.0.",
        "Clusy takes first place.",
        "Clusy sets a new AEB record.",
        "Clusy is the AEB leader.",
        "Clusy is the fastest crawler.",
        "Clusy is the most accurate extractor.",
        "Clusy has the highest F1.",
        "Clusy has the lowest error rate.",
        "Clusy is more accurate than Trafilatura 2.1.0.",
        "Clusy is twice as fast as Firecrawl.",
        "Clusy achieved a record F1.",
        "Clusy delivers world-leading extraction quality.",
        "Clusy provides best-in-class extraction.",
    ],
)
def test_unregistered_superiority_vocabulary_requires_evidence(
    tmp_path: Path,
    claim_line: str,
) -> None:
    _fixture_repository(tmp_path)
    (tmp_path / "README.md").write_text(f"{claim_line}\n", encoding="utf-8")

    errors = evidence.validate_repository(tmp_path)

    assert any(
        "evidence-bearing line requires exactly one marker" in error
        for error in errors
    )


@pytest.mark.parametrize(
    "claim_line",
    [
        "Clusy matches Trafilatura 2.1.0.",
        "Clusy is on par with Trafilatura 2.1.0.",
        "Clusy trails Trafilatura 2.1.0.",
        "Clusy underperforms Trafilatura 2.1.0.",
        "Clusy falls behind Trafilatura 2.1.0.",
        "Clusy lags behind Trafilatura 2.1.0.",
        "Clusy loses to Trafilatura 2.1.0.",
        "Clusy has lower F1 than Trafilatura 2.1.0.",
        "Clusy has higher error than Trafilatura 2.1.0.",
    ],
)
def test_all_measured_comparison_directions_require_evidence(
    tmp_path: Path,
    claim_line: str,
) -> None:
    _fixture_repository(tmp_path)
    (tmp_path / "README.md").write_text(f"{claim_line}\n", encoding="utf-8")

    errors = evidence.validate_repository(tmp_path)

    assert any(
        "evidence-bearing line requires exactly one marker" in error
        for error in errors
    )


@pytest.mark.parametrize(
    "claim_line",
    [
        "Beta 2 is live in production.",
        "Beta 2 was deployed to production.",
        "Beta 2 serves production traffic.",
        "Beta 2 has been rolled out globally.",
        "Our platform now uses Beta 2.",
        "Production now runs Beta 2.",
        "Beta 2 powers our production platform.",
    ],
)
def test_mutable_deployment_vocabulary_is_forbidden(
    tmp_path: Path,
    claim_line: str,
) -> None:
    _fixture_repository(tmp_path)
    (tmp_path / "README.md").write_text(f"{claim_line}\n", encoding="utf-8")

    errors = evidence.validate_repository(tmp_path)

    assert any("current-production claims are forbidden" in error for error in errors)


def test_canonical_renderer_binds_metric_key_to_artifact_semantics(
    tmp_path: Path,
) -> None:
    registry, _report = _fixture_repository(tmp_path)
    claim = registry["claims"][0]
    claim["metrics"][0]["artifact_pointer"] = "/metrics/not_f1"

    assert evidence._canonical_claim_publication_line(claim) is None


def test_metric_publication_aliases_are_required_and_normalized(tmp_path: Path) -> None:
    registry, _report = _fixture_repository(tmp_path)
    metric = registry["claims"][0]["metrics"][0]
    metric.pop("publication_aliases")
    _write_registry(tmp_path, registry)

    errors = evidence.validate_repository(tmp_path)

    assert any("publication_aliases: expected non-empty array" in error for error in errors)

    metric["publication_aliases"] = ["f1  "]
    _write_registry(tmp_path, registry)
    errors = evidence.validate_repository(tmp_path)

    assert any("publication_aliases[0]: alias must be normalized" in error for error in errors)


def test_archival_protocol_can_retain_non_authorizing_metrics(tmp_path: Path) -> None:
    _fixture_repository(tmp_path)
    protocol_path = tmp_path / "bench/evidence/archive/PROTOCOL.md"
    protocol_path.parent.mkdir(parents=True)
    protocol_path.write_text(
        "# Archived non-authorizing receipt\n\nArchived F1 was `0.123`.\n",
        encoding="utf-8",
    )
    _write_json(
        tmp_path / "bench/evidence/archive/report.json",
        {
            "archive_status": {
                "current_registry_member": False,
                "publication_authorized": False,
            }
        },
    )

    assert evidence.validate_repository(tmp_path) == []


def test_archival_protocol_cannot_self_authorize_a_leadership_claim(
    tmp_path: Path,
) -> None:
    _fixture_repository(tmp_path)
    protocol_path = tmp_path / "bench/evidence/archive/PROTOCOL.md"
    protocol_path.parent.mkdir(parents=True)
    protocol_path.write_text(
        "# Archived non-authorizing receipt\n\n"
        "Clusy is SOTA on WebMainBench, F1 0.999.\n",
        encoding="utf-8",
    )
    _write_json(
        tmp_path / "bench/evidence/archive/report.json",
        {
            "archive_status": {
                "current_registry_member": False,
                "publication_authorized": False,
            }
        },
    )

    errors = evidence.validate_repository(tmp_path)

    assert any(
        "evidence-bearing line requires exactly one marker" in error
        for error in errors
    )


def test_negated_current_production_boundary_is_allowed(tmp_path: Path) -> None:
    _fixture_repository(tmp_path)
    (tmp_path / "docs/ARCHIVE.md").write_text(
        "This does not establish current production state.\n",
        encoding="utf-8",
    )

    assert evidence.validate_repository(tmp_path) == []


def test_personal_path_fails_even_inside_markdown_fence(tmp_path: Path) -> None:
    _fixture_repository(tmp_path)
    (tmp_path / "docs/ARCHIVE.md").write_text(
        "```text\n/Users/example/project/report.json\n```\n",
        encoding="utf-8",
    )

    errors = evidence.validate_repository(tmp_path)

    assert any(
        "personal absolute or restricted workspace path is forbidden" in error
        for error in errors
    )


@pytest.mark.parametrize(
    "publication",
    [
        "/Us**ers**/julin/project/report.json",
        "/Us<!-- split -->ers/julin/project/report.json",
        "/Us&#101;rs/julin/project/report.json",
        "/Users/\njulin/project/report.json",
        ".clusy-<!-- split -->oss-structure-v2/report.json",
        ".clusy-oss-**structure**-v2/report.json",
    ],
)
def test_reader_visible_personal_path_disguises_fail(
    tmp_path: Path,
    publication: str,
) -> None:
    _fixture_repository(tmp_path)
    (tmp_path / "docs/ARCHIVE.md").write_text(
        f"{publication}\n",
        encoding="utf-8",
    )

    errors = evidence.validate_repository(tmp_path)

    assert any(
        "personal absolute or restricted workspace path is forbidden" in error
        for error in errors
    )


def test_personal_path_fails_in_unregistered_json(tmp_path: Path) -> None:
    _fixture_repository(tmp_path)
    _write_json(
        tmp_path / "bench/evidence/archive/metadata.json",
        {"python": "/Users/example/project/.venv/bin/python"},
    )

    errors = evidence.validate_repository(tmp_path)

    assert any(
        "personal absolute or restricted workspace path is forbidden" in error
        for error in errors
    )


def test_unregistered_evidence_bundle_must_be_archival_and_non_authorizing(
    tmp_path: Path,
) -> None:
    _fixture_repository(tmp_path)
    protocol_path = tmp_path / "bench/evidence/archive/PROTOCOL.md"
    protocol_path.parent.mkdir(parents=True)
    protocol_path.write_text("# Benchmark result\n", encoding="utf-8")
    _write_json(
        tmp_path / "bench/evidence/archive/report.json",
        {
            "archive_status": {
                "current_registry_member": False,
                "publication_authorized": True,
            },
            "claim_binding": {"status": "Verified"},
            "permissions": {
                "scoped_sota": False,
                "scoped_superiority": True,
                "vendor_superiority": False,
            },
            "verification": {"claimable": True},
        },
    )

    errors = evidence.validate_repository(tmp_path)

    assert any(
        "unregistered evidence protocol must be visibly archival" in error
        for error in errors
    )
    assert any(
        "unregistered evidence report must declare an archival" in error
        for error in errors
    )
    assert any(
        "unregistered claim binding must have Archived status" in error
        for error in errors
    )
    assert any(
        "unregistered evidence permissions must all be false" in error
        for error in errors
    )
    assert any("unregistered evidence must set claimable=false" in error for error in errors)


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


@pytest.mark.parametrize(
    "claim_line",
    [
        "Clusy does not exceed Trafilatura 2.1.0.",
        "The future target is for Clusy to exceed Trafilatura 2.1.0.",
        "Beta 2 is not live in production.",
        "If Beta 2 is deployed to production, rerun the release gates.",
    ],
)
def test_negated_and_nonassertive_boundaries_remain_allowed(
    tmp_path: Path,
    claim_line: str,
) -> None:
    _fixture_repository(tmp_path)
    (tmp_path / "README.md").write_text(f"{claim_line}\n", encoding="utf-8")

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

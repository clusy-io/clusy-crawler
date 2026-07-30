from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from bench import selection_atom_catalog_representability as runner
from bench.source_provenance import native_source_digest

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "bench" / "evidence" / "selection-atom-catalog-e5958b5" / "report.json"
REPORT_SHA256 = "923ce975b22faae14e583d046c93a20fd9e625472d071cd09caf47ead7ae69b8"
INPUT_SHA256 = "e5958b541d844cf011e66e214bf64abb742aec6922e3c32321e2abaf7cf2c735"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _report() -> dict[str, Any]:
    value = json.loads(REPORT.read_text(encoding="utf-8"))
    assert type(value) is dict
    return value


def test_frozen_selection_atom_catalog_diagnostic_is_self_consistent() -> None:
    report = _report()
    assert _sha256(REPORT) == REPORT_SHA256
    assert report["schema"] == "clusy.selection-atom-catalog-mechanism-measurement.v1"
    assert report["input"] == {
        "jsonl_bytes": 88_727_247,
        "jsonl_sha256": INPUT_SHA256,
        "labels_present_or_read": False,
        "records": 545,
        "required_keys": ["html", "html_sha256", "row", "track_id", "url"],
    }
    assert report["input_identity_before_and_after_match"] is True
    assert report["source_identity_before_and_after_match"] is True

    boundary = report["claim_boundary"]
    assert boundary["representation_coverage_only"] is True
    assert not any(
        boundary[key]
        for key in (
            "end_to_end_crawler_latency",
            "production_default_changed",
            "sota_claim",
            "vendor_latency_comparison",
            "wmb_quality_score",
        )
    )

    coverage = report["coverage_comparison"]
    legacy = coverage["legacy_pre_mapper_development_observation"]
    current = coverage["ordered_mapper_catalog"]
    assert coverage["denominator_pages"] == 545
    assert legacy["accepted_pages"] == 76
    assert current["accepted_pages"] == 537
    assert coverage["accepted_page_delta"] == 461
    assert math.isclose(legacy["coverage_ratio"], 76 / 545)
    assert math.isclose(current["coverage_ratio"], 537 / 545)
    assert math.isclose(
        coverage["coverage_percentage_point_delta"],
        461 * 100 / 545,
    )
    assert sum(legacy["reason_counts"].values()) == 545
    assert sum(current["reason_counts"].values()) == 545
    assert legacy["exact_executable_or_source_artifact_retained"] is False
    assert legacy["performance_comparison_permitted"] is False
    assert coverage["quality_or_sota_inference_permitted"] is False

    stable = report["stable_results"]
    assert stable["accepted"] == 537
    assert stable["reason_counts"] == current["reason_counts"]
    assert stable["atom_count"] == 183_549
    assert sum(stable["kind_counts"].values()) == stable["atom_count"]
    assert stable["kind_counts"] == {
        "code": 12_637,
        "list_item": 68_784,
        "math": 3_047,
        "table_cell": 33_104,
        "text": 65_977,
    }
    assert stable["transformed_span_count"] == 12_399

    runs = report["runs"]
    assert len(runs) == 3
    assert report["pooled"]["observations"] == 3 * 545
    for run in runs:
        assert run["accepted"] == stable["accepted"]
        assert run["reason_counts"] == stable["reason_counts"]
        assert run["atom_count"] == stable["atom_count"]
        assert run["kind_counts"] == stable["kind_counts"]
        assert run["transformed_span_count"] == stable["transformed_span_count"]
        assert run["output_commitment_sha256"] == stable["output_commitment_sha256"]


def test_frozen_diagnostic_binds_current_catalog_and_native_source_inventory() -> None:
    report = _report()
    identity = report["source_identity"]
    binding = identity["native_source_binding"]

    assert binding["matched"] is True
    assert binding["packaged_sha256"] == binding["current_sha256"]
    assert binding["current_sha256"] == native_source_digest(ROOT)
    for relative, recorded in identity["relevant_files"].items():
        path = ROOT / relative
        assert path.stat().st_size == recorded["bytes"]
        assert _sha256(path) == recorded["sha256"]

    executed = identity["executed_python_modules"]
    assert set(executed) >= runner.REQUIRED_EXECUTED_PYTHON_MODULES
    assert all(module["repository_bytes_match"] for module in executed.values())
    assert all(
        identity["relevant_files"][module["repository_relative"]]["sha256"] == module["sha256"]
        for module in executed.values()
    )
    assert identity["benchmark_callable"] == {
        "module": "clusy_native.selection_atom_catalog_v1",
        "package_reexport_is_module_entrypoint": True,
        "qualname": "build_selection_atom_catalog_v1",
    }
    repository_modules = identity["executed_repository_modules"]
    assert set(repository_modules) == {"benchmark_runner", "provenance_helper"}
    assert all(module["repository_bytes_match"] for module in repository_modules.values())
    assert all(
        identity["relevant_files"][module["repository_relative"]]["sha256"] == module["sha256"]
        for module in repository_modules.values()
    )


def _module(name: str, path: Path | None = None) -> ModuleType:
    module = ModuleType(name)
    if path is not None:
        module.__file__ = str(path)
    return module


def test_executed_python_inventory_fails_closed_on_missing_extra_path_and_bytes(
    tmp_path: Path,
) -> None:
    installed = tmp_path / "installed"
    repository = tmp_path / "repository"
    installed.mkdir()
    repository.mkdir()
    (installed / "__init__.py").write_text("value = 1\n", encoding="utf-8")
    (repository / "__init__.py").write_text("value = 1\n", encoding="utf-8")
    native = _module("clusy_native._native")
    baseline = {
        "clusy_native": _module("clusy_native", installed / "__init__.py"),
        "clusy_native._native": native,
    }

    inventory = runner.verify_executed_python_module_inventory(
        baseline,
        installed_package_root=installed,
        repository_package_root=repository,
        required_modules=frozenset({"clusy_native"}),
    )
    assert tuple(inventory) == ("clusy_native",)
    assert inventory["clusy_native"]["repository_bytes_match"] is True

    with pytest.raises(RuntimeError, match="not loaded"):
        runner.verify_executed_python_module_inventory(
            {"clusy_native._native": native},
            installed_package_root=installed,
            repository_package_root=repository,
            required_modules=frozenset({"clusy_native"}),
        )

    (installed / "extra.py").write_text("value = 1\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="no repository source"):
        runner.verify_executed_python_module_inventory(
            {
                **baseline,
                "clusy_native.extra": _module("clusy_native.extra", installed / "extra.py"),
            },
            installed_package_root=installed,
            repository_package_root=repository,
            required_modules=frozenset({"clusy_native"}),
        )

    (installed / "wrong.py").write_text("value = 1\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="does not match its name"):
        runner.verify_executed_python_module_inventory(
            {
                **baseline,
                "clusy_native.extra": _module("clusy_native.extra", installed / "wrong.py"),
            },
            installed_package_root=installed,
            repository_package_root=repository,
            required_modules=frozenset({"clusy_native"}),
        )

    (repository / "extra.py").write_text("value = 2\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="differs from repository"):
        runner.verify_executed_python_module_inventory(
            {
                **baseline,
                "clusy_native.extra": _module("clusy_native.extra", installed / "extra.py"),
            },
            installed_package_root=installed,
            repository_package_root=repository,
            required_modules=frozenset({"clusy_native"}),
        )

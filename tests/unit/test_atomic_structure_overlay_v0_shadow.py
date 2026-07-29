from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from bench import atomic_structure_overlay_v0_shadow as audit
from bench import generate_atomic_structure_baseline as generator
from bench import webmainbench_finegrained_benchmark as fine

if TYPE_CHECKING:
    from pathlib import Path


class _Metric:
    def __init__(self, score: float, *, success: bool = True) -> None:
        self.score = score
        self.success = success
        self.details: dict[str, Any] = {}
        self.error_message = None

    def to_dict(self) -> dict[str, Any]:
        return {}


def _page_metrics(score: float = 0.5) -> dict[str, _Metric]:
    return {name: _Metric(score) for name in fine.CORE_METRICS}


def test_conservative_aggregate_scores_failure_zero_and_rejects_mask_drift() -> None:
    baseline = [_page_metrics() for _ in range(audit.EXPECTED_PAGES)]
    candidate = [_page_metrics() for _ in range(audit.EXPECTED_PAGES)]
    candidate[17]["formula_edit"] = _Metric(1.0, success=False)

    (
        baseline_aggregate,
        candidate_aggregate,
        delta,
        masks,
    ) = audit._conservative_paired_aggregates(  # noqa: SLF001
        baseline,  # type: ignore[arg-type]
        candidate,  # type: ignore[arg-type]
    )

    assert baseline_aggregate["metrics"]["formula_edit"]["failed_pages"] == 0
    assert candidate_aggregate["metrics"]["formula_edit"]["failed_pages"] == 1
    assert candidate_aggregate["metrics"]["formula_edit"]["score"] == pytest.approx(
        0.5 - (0.5 / audit.EXPECTED_PAGES)
    )
    assert delta["metrics"]["formula_edit"]["score"] == pytest.approx(
        -(0.5 / audit.EXPECTED_PAGES)
    )
    assert masks["all_core_success_masks_exact"] is False
    assert masks["metrics"]["formula_edit"]["mismatch_dataset_indices"] == [17]


def _track_with_patch(*, patch_digest: str) -> SimpleNamespace:
    proposal = SimpleNamespace(
        accepted=True,
        atom_kind="code",
        candidate_span_start=7,
        candidate_span_end=11,
        replacement_digest="a" * 64,
        patch_digest=patch_digest,
        visible_token_digest="b" * 64,
        visible_token_count=2,
        replacement_bytes=12,
    )
    decision = SimpleNamespace(
        accepted=True,
        output_markdown="same bytes",
        proposals=(proposal,),
    )
    return SimpleNamespace(decision=decision)


def test_cross_track_parity_binds_exact_patch_topology() -> None:
    official = _track_with_patch(patch_digest="c" * 64)
    scrubbed = _track_with_patch(patch_digest="d" * 64)

    assert audit._parity_key(official) != audit._parity_key(  # type: ignore[arg-type]  # noqa: SLF001
        scrubbed  # type: ignore[arg-type]
    )


def test_opaque_baseline_is_exploratory_and_cannot_enter_claimable_mode(
) -> None:
    baseline_metadata = {
        "schema_version": "legacy-opaque",
        "sha256": "a" * 64,
        "records": audit.EXPECTED_PAGES,
    }
    decision_inputs_metadata = {
        "schema_version": audit.DECISION_INPUT_SCHEMA,
        "sha256": "b" * 64,
        "records": audit.EXPECTED_PAGES,
    }

    exploratory = audit._load_baseline_provenance(  # noqa: SLF001
        None,
        baseline_metadata=baseline_metadata,
        decision_inputs_metadata=decision_inputs_metadata,
        require_claimable=False,
    )

    assert exploratory["claimable"] is False
    assert exploratory["mode"] == "opaque_baseline_exploratory"
    with pytest.raises(fine.BenchmarkError, match="requires --baseline-manifest"):
        audit._load_baseline_provenance(  # noqa: SLF001
            None,
            baseline_metadata=baseline_metadata,
            decision_inputs_metadata=decision_inputs_metadata,
            require_claimable=True,
        )


def _synthetic_source_snapshot() -> dict[str, Any]:
    fixed_files = {
        relative: "5" * 64
        for relative in audit.BASELINE_GENERATOR_FIXED_FILES
    }
    fixed_files["app/services/extractor.py"] = "9" * 64
    fixed_files["native/python/clusy_native/__init__.py"] = "a" * 64
    source = {
        "schema_version": "clusy.fixed-baseline-generator-source.1",
        "source_root": "/source",
        "git_commit": "1" * 40,
        "git_tree": "2" * 40,
        "git_clean": True,
        "file_sha256": {"app/services/extractor.py": "3" * 64},
        "source_digest": "4" * 64,
        "fixed_file_sha256": fixed_files,
        "lock_sha256": {
            "uv.lock": "6" * 64,
            "native/Cargo.lock": "7" * 64,
        },
        "native_source_binding": {
            "matched": True,
            "packaged_sha256": "8" * 64,
            "current_sha256": "8" * 64,
        },
        "loaded_modules": {
            "extractor": {
                "module": "app.services.extractor",
                "path": "/source/app/services/extractor.py",
                "sha256": "9" * 64,
            },
            "clusy_native_package": {
                "module": "clusy_native",
                "path": "/source/native/python/clusy_native/__init__.py",
                "sha256": "a" * 64,
            },
            "clusy_native_extension": {
                "module": "clusy_native._native",
                "path": "/source/.venv/clusy_native._native.so",
                "sha256": "b" * 64,
            },
        },
    }
    source["snapshot_digest"] = generator._hash_json(source)  # noqa: SLF001
    return source


def _synthetic_config() -> dict[str, Any]:
    return {
        "entrypoint": "app.services.extractor.extract_content",
        "extraction_profile": "balanced",
        "url": "",
        "prediction_transform": "identity ExtractionResult.text",
        "input_transform": (
            "bench.webmainbench_benchmark.scrub_annotation_artifacts"
        ),
        "annotation_scrubber_postcondition": True,
        "concurrency": 1,
        "network_calls": False,
        "model_calls": False,
        "vendor_outputs_used": False,
    }


def _synthetic_environment() -> dict[str, Any]:
    return {
        "python": "3.13.5",
        "python_implementation": "CPython",
        "python_executable": "/source/.venv/bin/python",
        "platform": "synthetic",
        "machine": "x86_64",
        "fixed_environment": dict(audit.BASELINE_FIXED_ENVIRONMENT),
        "credential_guard": {
            "checked_names": sorted(audit.BASELINE_CREDENTIAL_ENVIRONMENT_NAMES),
            "active_names": [],
        },
        "uv_lock_sha256": "6" * 64,
        "cargo_lock_sha256": "7" * 64,
    }


def _write_projection(path: Path) -> None:
    rows = [
        {
            "schema_version": audit.DECISION_INPUT_SCHEMA,
            "dataset_index": index,
            "track_id": f"track-{index}",
            "html": f"<p>input {index}</p>",
        }
        for index in range(2)
    ]
    path.write_text(
        "".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def test_label_free_baseline_generator_is_deterministic_and_manifest_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection = tmp_path / "decision-inputs.jsonl"
    _write_projection(projection)

    def extract(html: str, url: str, *, extraction_profile: str) -> SimpleNamespace:
        assert url == ""
        assert extraction_profile == "balanced"
        return SimpleNamespace(text=html.upper(), strategy="synthetic")

    outputs: list[tuple[Path, Path]] = []
    for suffix in ("a", "b"):
        baseline = tmp_path / f"baseline-{suffix}.jsonl"
        manifest = tmp_path / f"baseline-{suffix}.manifest.json"
        generator.generate_baseline(
            projection,
            baseline,
            manifest,
            expected_records=2,
            extractor=extract,
            source_snapshotter=_synthetic_source_snapshot,
            config=_synthetic_config(),
            environment=_synthetic_environment(),
        )
        outputs.append((baseline, manifest))

    assert outputs[0][0].read_bytes() == outputs[1][0].read_bytes()
    first_manifest = json.loads(outputs[0][1].read_bytes())
    second_manifest = json.loads(outputs[1][1].read_bytes())
    for document in (first_manifest, second_manifest):
        document["generator"]["cli_args"]["output"] = "<output>"
        document["generator"]["cli_args"]["manifest"] = "<manifest>"
    assert first_manifest == second_manifest

    monkeypatch.setattr(audit, "EXPECTED_PAGES", 2)
    predictions, baseline_metadata = audit._load_baseline(  # noqa: SLF001
        outputs[0][0],
        allow_legacy=False,
    )
    _, decision_inputs_metadata = audit._load_decision_inputs(  # noqa: SLF001
        projection,
        predictions,
        lambda html: html,
    )
    provenance = audit._load_baseline_provenance(  # noqa: SLF001
        outputs[0][1],
        baseline_metadata=baseline_metadata,
        decision_inputs_metadata=decision_inputs_metadata,
        require_claimable=True,
    )

    assert provenance["claimable"] is True
    assert all(provenance["checks"].values())


def test_label_free_baseline_generator_records_failures_without_messages(
    tmp_path: Path,
) -> None:
    projection = tmp_path / "decision-inputs.jsonl"
    _write_projection(projection)

    def extract(html: str, _url: str, *, extraction_profile: str) -> SimpleNamespace:
        assert extraction_profile == "balanced"
        if "1" in html:
            raise RuntimeError("sensitive unstable detail")
        return SimpleNamespace(text="prediction", strategy="synthetic")

    baseline = tmp_path / "baseline.jsonl"
    manifest = tmp_path / "baseline.manifest.json"
    document = generator.generate_baseline(
        projection,
        baseline,
        manifest,
        expected_records=2,
        extractor=extract,
        source_snapshotter=_synthetic_source_snapshot,
        config=_synthetic_config(),
        environment=_synthetic_environment(),
    )

    assert document["baseline"]["successful_records"] == 1
    assert document["baseline"]["failed_records"] == 1
    assert document["baseline"]["failure_types"] == {"RuntimeError": 1}
    assert b"sensitive unstable detail" not in baseline.read_bytes()
    assert b"sensitive unstable detail" not in manifest.read_bytes()


def test_baseline_generator_rejects_active_model_or_vendor_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used")

    with pytest.raises(fine.BenchmarkError, match="credential paths must be inactive"):
        generator._configure_fixed_environment()  # noqa: SLF001


def test_decision_projection_schema_rejects_label_bearing_fields(
    tmp_path: Path,
) -> None:
    projection = tmp_path / "decision-inputs.jsonl"
    projection.write_text(
        json.dumps(
            {
                "schema_version": audit.DECISION_INPUT_SCHEMA,
                "dataset_index": 0,
                "track_id": "track-0",
                "html": "<p>input</p>",
                "reference": "forbidden",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(fine.BenchmarkError, match="invalid closed schema"):
        audit._load_decision_inputs(  # noqa: SLF001
            projection,
            {
                index: (f"track-{index}", "prediction")
                for index in range(audit.EXPECTED_PAGES)
            },
            lambda html: html,
        )


def test_pre_post_provenance_drift_is_protocol_failure() -> None:
    with pytest.raises(fine.BenchmarkError, match="source provenance changed"):
        audit._assert_snapshot_stable(  # noqa: SLF001
            {"snapshot_digest": "a" * 64},
            {"snapshot_digest": "b" * 64},
            name="source",
        )

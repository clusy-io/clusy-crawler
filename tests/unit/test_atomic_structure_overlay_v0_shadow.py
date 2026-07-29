from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from bench import atomic_claim_protocol as protocol
from bench import atomic_structure_overlay_v0_shadow as legacy
from bench import generate_atomic_structure_baseline as generator
from bench import score_atomic_frozen_decisions as scorer
from bench.claimable_io import (
    ClaimableIOError,
    read_verified_bytes,
    snapshot_file,
    write_new_file,
)
from bench.claimable_sandbox import (
    CLAIMABLE_CONCURRENCY,
    CLAIMABLE_WALL_SECONDS,
    _build_command,
    probe_sandbox,
)


class _Metric:
    def __init__(self, score: float, *, success: bool = True) -> None:
        self.score = score
        self.success = success
        self.details: dict[str, Any] = {}


def _page_metrics(score: float = 0.5) -> dict[str, _Metric]:
    return {
        name: _Metric(score)
        for name in scorer.CORE_METRICS
    }


def test_claimable_baseline_entrypoint_has_no_injection_surface() -> None:
    signature = inspect.signature(generator.generate_baseline)

    assert tuple(signature.parameters) == ("decision_inputs", "output")
    assert all(
        name not in signature.parameters
        for name in (
            "extractor",
            "source_snapshotter",
            "config",
            "environment",
            "concurrency",
            "expected_records",
        )
    )


def test_claim_protocol_cli_has_no_performance_or_callable_overrides() -> None:
    parser_source = inspect.getsource(protocol.parse_args)

    assert "--concurrency" not in parser_source
    assert "--max-decision-wall-seconds" not in parser_source
    assert "--extractor" not in parser_source
    assert CLAIMABLE_CONCURRENCY == 4
    assert CLAIMABLE_WALL_SECONDS == 180.0


def test_sandbox_command_is_env_i_fresh_python_and_actual_no_network() -> None:
    command = _build_command(
        env_path=Path("/usr/bin/env"),
        bwrap_path=Path("/usr/bin/bwrap"),
        python_path=Path("/usr/bin/python3"),
        capsule_descriptors={"worker.py": 17, "app/module.py": 18},
        parent_net_inode=123,
    )

    assert command[:3] == ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin"]
    assert "--unshare-all" in command
    assert "--unshare-net" in command
    assert "--clearenv" in command
    assert command[-4:-1] == ["-I", "-S", "-B"]
    assert "--remount-ro" in command
    assert "/capsule" in command
    assert not any("dataset" in item or "evaluator" in item for item in command)


def test_unavailable_enforceable_sandbox_is_observed_nonclaimable() -> None:
    observation = probe_sandbox()

    if os.uname().sysname != "Linux":
        assert observation.available is False
        assert "Linux" in observation.reason
    if observation.available:
        assert observation.network_probe is not None
        assert observation.network_probe["net_namespace_distinct"] is True
        assert observation.network_probe["non_loopback_route_rows"] == 0
        assert observation.network_probe["non_loopback_ipv6_route_rows"] == 0
        assert observation.network_probe["egress_connect_ex"] != 0
        assert observation.network_probe["ipv6_egress_connect_ex"] != 0


def test_secure_io_rejects_symlink_and_never_replaces_output(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"stable")
    link = tmp_path / "link"
    link.symlink_to(source)

    with pytest.raises((ClaimableIOError, OSError)):
        read_verified_bytes(link, maximum_bytes=64)

    symlink_parent = tmp_path / "symlink-parent"
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    symlink_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(ClaimableIOError):
        write_new_file(symlink_parent / "forbidden", b"value")

    output = tmp_path / "output"
    first = write_new_file(output, b"first")
    assert first.sha256 == __import__("hashlib").sha256(b"first").hexdigest()
    with pytest.raises(ClaimableIOError, match="replace"):
        write_new_file(output, b"second")
    assert output.read_bytes() == b"first"


def test_content_addressed_snapshot_uses_verified_exact_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"snapshot bytes")
    snapshots = tmp_path / "snapshots"

    metadata = snapshot_file(source, snapshots, maximum_bytes=64)

    assert metadata.path.name == metadata.sha256
    assert metadata.path.read_bytes() == b"snapshot bytes"
    assert metadata.path.stat().st_mode & 0o777 == 0o400


def test_projection_closed_schema_rejects_label_bearing_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(protocol, "EXPECTED_RECORDS", 1)
    projection = tmp_path / "projection.jsonl"
    row = {
        "dataset_index": 0,
        "raw_html": "<p>raw</p>",
        "reference": "forbidden label",
        "schema_version": protocol.DECISION_INPUT_SCHEMA,
        "track_id": "track-0",
    }
    write_new_file(
        projection,
        json.dumps(row, sort_keys=True, separators=(",", ":")).encode() + b"\n",
    )

    with pytest.raises(protocol.ClaimProtocolError, match="schema mismatch"):
        protocol._load_projection(projection)  # noqa: SLF001


def test_projection_accepts_only_canonical_index_and_raw_html(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(protocol, "EXPECTED_RECORDS", 1)
    projection = tmp_path / "projection.jsonl"
    row = {
        "dataset_index": 0,
        "raw_html": "<p>raw</p>",
        "schema_version": protocol.DECISION_INPUT_SCHEMA,
    }
    content = (
        json.dumps(
            row,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    write_new_file(projection, content)

    records, observed, _ = protocol._load_projection(projection)  # noqa: SLF001

    assert observed == content
    assert records == (row,)


def test_worker_sources_exclude_dataset_evaluator_scorer_imports() -> None:
    decision_source = Path(protocol.ROOT / "bench/atomic_decision_worker.py").read_text()
    baseline_source = Path(protocol.ROOT / "bench/atomic_baseline_worker.py").read_text()

    for source in (decision_source, baseline_source):
        assert "groundtruth" not in source
        assert "track_id" not in source
        assert "scrubbed_html" not in source
        assert "from bench" not in source
        assert "import bench" not in source
        assert "import webmainbench" not in source
    assert "app.services.extractor" not in decision_source
    assert "app.services.extractor" in baseline_source
    assert "app.services.atomic_structure_overlay_v0" in decision_source


def test_scorer_requires_external_artifact_hashes_before_evaluator_import() -> None:
    signature = inspect.signature(scorer.score)
    score_source = inspect.getsource(scorer.score)

    assert "expected_baseline_sha256" in signature.parameters
    assert "expected_decision_sha256" in signature.parameters
    assert score_source.index("_read_json_artifact(") < score_source.index(
        'import_module("bench.webmainbench_finegrained_benchmark")'
    )


def test_conservative_score_counts_every_failure_as_zero_and_requires_mask_parity() -> None:
    baseline = [_page_metrics() for _ in range(scorer.EXPECTED_RECORDS)]
    candidate = [_page_metrics() for _ in range(scorer.EXPECTED_RECORDS)]
    candidate[17]["formula_edit"] = _Metric(1.0, success=False)

    (
        baseline_aggregate,
        candidate_aggregate,
        delta,
        parity,
    ) = scorer._conservative_aggregates(  # noqa: SLF001
        baseline,  # type: ignore[arg-type]
        candidate,  # type: ignore[arg-type]
    )

    assert baseline_aggregate["metrics"]["formula_edit"]["failed_pages"] == 0
    assert candidate_aggregate["metrics"]["formula_edit"]["failed_pages"] == 1
    assert candidate_aggregate["metrics"]["formula_edit"]["failure_scoring"] == "zero"
    assert delta["metrics"]["formula_edit"]["score"] == pytest.approx(
        -(0.5 / scorer.EXPECTED_RECORDS)
    )
    assert parity["formula_edit"] is False


def test_legacy_combined_runner_can_never_be_a_claimable_path() -> None:
    source = inspect.getsource(legacy.run_audit)
    summary_source = inspect.getsource(legacy._load_baseline_provenance)  # noqa: SLF001

    assert "permanently nonclaimable" in source
    assert '"claimable": False' in summary_source


def test_launcher_observation_mapping_is_immutable_at_result_boundary() -> None:
    evidence = MappingProxyType({"available": True})

    with pytest.raises(TypeError):
        evidence["available"] = False  # type: ignore[index]

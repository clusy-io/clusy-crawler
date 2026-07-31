from __future__ import annotations

import hashlib
import json
from dataclasses import fields
from typing import TYPE_CHECKING, Any

import pytest

from bench import focused_frontier_benchmark as benchmark

if TYPE_CHECKING:
    from pathlib import Path


def _default_fixture() -> benchmark.Fixture:
    return benchmark.load_fixture(
        benchmark.DEFAULT_CORPUS,
        benchmark.DEFAULT_FIXTURE_MANIFEST,
    )


def _write_fixture(
    tmp_path: Path,
    rows: list[dict[str, Any]],
    *,
    manifest_updates: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    corpus = tmp_path / "graph.jsonl"
    corpus_bytes = b"".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")).encode() + b"\n" for row in rows
    )
    corpus.write_bytes(corpus_bytes)
    manifest = json.loads(benchmark.DEFAULT_FIXTURE_MANIFEST.read_text())
    manifest["corpus_records"] = len(rows)
    manifest["corpus_sha256"] = hashlib.sha256(corpus_bytes).hexdigest()
    if manifest_updates:
        manifest.update(manifest_updates)
    manifest_path = tmp_path / "fixture_manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True))
    return corpus, manifest_path


def _default_rows() -> list[dict[str, Any]]:
    return [json.loads(line) for line in benchmark.DEFAULT_CORPUS.read_text().splitlines()]


def _load_modified_fixture(
    corpus: Path,
    manifest: Path,
) -> benchmark.Fixture:
    return benchmark.load_fixture(
        corpus,
        manifest,
        trusted_corpus_sha256=hashlib.sha256(corpus.read_bytes()).hexdigest(),
        trusted_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
    )


def test_pinned_fixture_is_complete_synthetic_and_reachable() -> None:
    fixture = _default_fixture()

    assert hashlib.sha256(fixture.corpus_bytes).hexdigest() == benchmark.PINNED_CORPUS_SHA256
    assert (
        hashlib.sha256(fixture.manifest_bytes).hexdigest()
        == benchmark.PINNED_FIXTURE_MANIFEST_SHA256
    )
    assert len(fixture.nodes) == 28
    assert sum(node.target for node in fixture.nodes.values()) == 6
    assert fixture.max_depth == 2


def test_loader_fails_closed_when_target_label_is_missing(
    tmp_path: Path,
) -> None:
    rows = _default_rows()
    rows[4].pop("target")
    corpus, manifest = _write_fixture(tmp_path, rows)

    with pytest.raises(benchmark.BenchmarkError, match="fields mismatch"):
        _load_modified_fixture(corpus, manifest)


def test_compiled_trust_root_rejects_coordinated_manifest_and_corpus_change(
    tmp_path: Path,
) -> None:
    rows = _default_rows()
    rows[1]["payload_bytes"] += 1
    corpus, manifest = _write_fixture(tmp_path, rows)

    with pytest.raises(benchmark.BenchmarkError, match="compiled trust root"):
        benchmark.load_fixture(corpus, manifest)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"artifact_status": "CLAIMABLE"}, "synthetic-only"),
        ({"claimable": True}, "claimable must be false"),
        ({"fixture_label": "external"}, "synthetic label"),
        ({"corpus_sha256": "0" * 64}, "trust root"),
    ],
)
def test_loader_rejects_status_label_and_hash_drift(
    tmp_path: Path,
    updates: dict[str, Any],
    message: str,
) -> None:
    corpus, manifest = _write_fixture(
        tmp_path,
        _default_rows(),
        manifest_updates=updates,
    )

    with pytest.raises(benchmark.BenchmarkError, match=message):
        _load_modified_fixture(corpus, manifest)


def test_loader_rejects_dangling_unreachable_and_cyclic_graphs(
    tmp_path: Path,
) -> None:
    rows = _default_rows()
    rows[0]["links"][0]["to"] = "unknown"
    corpus, manifest = _write_fixture(tmp_path, rows)
    with pytest.raises(benchmark.BenchmarkError, match="dangling"):
        _load_modified_fixture(corpus, manifest)

    rows = _default_rows()
    rows[0]["links"] = rows[0]["links"][1:]
    corpus, manifest = _write_fixture(tmp_path / "unreachable", rows)
    with pytest.raises(benchmark.BenchmarkError, match="unreachable"):
        _load_modified_fixture(corpus, manifest)

    rows = _default_rows()
    rows[7]["links"] = [{"anchor_terms": [], "to": "n001"}]
    corpus, manifest = _write_fixture(tmp_path / "cycle", rows)
    with pytest.raises(benchmark.BenchmarkError, match="cycle"):
        _load_modified_fixture(corpus, manifest)


def test_policy_context_excludes_evaluator_labels_and_payloads() -> None:
    assert {field.name for field in fields(benchmark.PriorityContext)} == {
        "source_id",
        "destination_id",
        "anchor_terms",
        "depth",
        "query_terms",
        "seed",
    }


@pytest.mark.parametrize("event", ["socket.connect", "subprocess.Popen", "os.system"])
def test_offline_worker_guard_rejects_network_and_child_process_events(event: str) -> None:
    with pytest.raises(benchmark.BenchmarkError, match="offline worker blocked"):
        benchmark._offline_audit_hook(event, ())  # noqa: SLF001


def test_current_constant_matches_bfs_selection_and_exposes_gap() -> None:
    fixture = _default_fixture()
    current = benchmark.run_policy(
        fixture,
        benchmark.CurrentConstantPolicy(fixture.manifest.constant_priority),
    )
    bfs = benchmark.run_policy(fixture, benchmark.BreadthFirstPolicy())
    heuristic = benchmark.run_policy(
        fixture,
        benchmark.QueryAnchorHeuristicPolicy(),
    )

    current_sequence = [row["node_id"] for row in current["trace"]]
    bfs_sequence = [row["node_id"] for row in bfs["trace"]]
    assert current_sequence == bfs_sequence
    assert current["result"]["requests_to_90pct_targets"] == 28
    assert current["result"]["non_target_bytes_before_90pct"] == 2_435_200
    assert current["result"]["yield_auc"] == 0.125
    assert heuristic["result"]["requests_to_90pct_targets"] == 8
    assert heuristic["result"]["non_target_bytes_before_90pct"] == 5_200
    assert heuristic["result"]["yield_auc"] == 0.839285714286


def test_semantic_trace_hash_is_stable_and_excludes_runtime_diagnostics() -> None:
    fixture = _default_fixture()
    first = benchmark.run_policy(fixture, benchmark.DeterministicRandomPolicy())
    second = benchmark.run_policy(fixture, benchmark.DeterministicRandomPolicy())

    assert first["trace"] == second["trace"]
    assert first["result"]["trace_sha256"] == second["result"]["trace_sha256"]
    assert "peak_rss_bytes" not in first["trace"][0]
    assert "decision_latency_ns_p50" not in first["trace"][0]


def test_artifact_manifest_hashes_every_output_and_remains_nonclaimable(
    tmp_path: Path,
) -> None:
    output = tmp_path / "artifact"

    report = benchmark.run_benchmark(
        corpus_path=benchmark.DEFAULT_CORPUS,
        manifest_path=benchmark.DEFAULT_FIXTURE_MANIFEST,
        output_dir=output,
        worker_runner=lambda **kwargs: benchmark._worker(**kwargs),  # noqa: SLF001
    )

    assert report["artifact_status"] == benchmark.ARTIFACT_STATUS
    assert report["claimable"] is False
    assert "NOT CLAIMABLE" in (output / "NOT_CLAIMABLE.txt").read_text()
    run_manifest = json.loads((output / "run_manifest.json").read_text())
    for relative_path, expected_hash in run_manifest["artifacts"].items():
        assert hashlib.sha256((output / relative_path).read_bytes()).hexdigest() == expected_hash
    assert set(run_manifest["policies"]) == set(benchmark.POLICY_NAMES)


def test_runner_refuses_to_mix_with_an_existing_output_directory(
    tmp_path: Path,
) -> None:
    output = tmp_path / "existing"
    output.mkdir()

    with pytest.raises(benchmark.BenchmarkError, match="already exists"):
        benchmark.run_benchmark(
            corpus_path=benchmark.DEFAULT_CORPUS,
            manifest_path=benchmark.DEFAULT_FIXTURE_MANIFEST,
            output_dir=output,
        )

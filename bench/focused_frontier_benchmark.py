#!/usr/bin/env python3
"""Strictly offline synthetic regression for focused crawl-frontier policies.

This harness is intentionally non-claimable. It runs a fixed, hash-pinned
synthetic graph through the real CrawlFrontier and measures when each policy
discovers evaluator-only target labels. It performs no network I/O.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import resource
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.frontier import CrawlFrontier, FrontierConfig  # noqa: E402

PROTOCOL_VERSION: Final = "clusy.focused-frontier.synthetic.v0"
FIXTURE_SCHEMA: Final = "clusy.focused-frontier.fixture-manifest.v0"
REPORT_SCHEMA: Final = "clusy.focused-frontier.report.v0"
RUN_MANIFEST_SCHEMA: Final = "clusy.focused-frontier.run-manifest.v0"
ARTIFACT_STATUS: Final = "SYNTHETIC_ONLY / NOT_CLAIMABLE"
FIXTURE_LABEL: Final = "SYNTHETIC_REGRESSION_ONLY"
DEFAULT_FIXTURE_DIR: Final = REPO_ROOT / "bench" / "focused_frontier_v0"
DEFAULT_CORPUS: Final = DEFAULT_FIXTURE_DIR / "synthetic_graph.jsonl"
DEFAULT_FIXTURE_MANIFEST: Final = DEFAULT_FIXTURE_DIR / "fixture_manifest.json"
PROTOCOL_PATH: Final = DEFAULT_FIXTURE_DIR / "PROTOCOL.md"
PINNED_CORPUS_SHA256: Final = "f3c62e880f137525942f456430468c72b520d9aff4af2750acc72bc6eb603ed8"
PINNED_FIXTURE_MANIFEST_SHA256: Final = (
    "7dbc843cd6c8714785a7e8f91d7a73d130fc4a448e250de0848a1d792a3f856e"
)
POLICY_NAMES: Final = (
    "current_constant",
    "bfs",
    "deterministic_random",
    "pluggable_heuristic",
)
MAX_CORPUS_BYTES: Final = 16 * 1024 * 1024
MAX_RECORDS: Final = 100_000
MAX_LINKS_PER_NODE: Final = 10_000
NODE_ID_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
TERM_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SHA256_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")
MANIFEST_FIELDS: Final = frozenset(
    {
        "artifact_status",
        "claimable",
        "constant_priority",
        "corpus_records",
        "corpus_sha256",
        "fixture_label",
        "query_terms",
        "recall_threshold_denominator",
        "recall_threshold_numerator",
        "schema",
        "seed",
        "seed_node_id",
        "target_count",
    }
)
NODE_FIELDS: Final = frozenset({"id", "links", "payload_bytes", "target"})
LINK_FIELDS: Final = frozenset({"anchor_terms", "to"})


class BenchmarkError(RuntimeError):
    """The synthetic protocol cannot be verified or completed."""


@dataclass(frozen=True, slots=True)
class Link:
    destination_id: str
    anchor_terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Node:
    node_id: str
    payload_bytes: int
    target: bool
    links: tuple[Link, ...]


@dataclass(frozen=True, slots=True)
class FixtureManifest:
    corpus_sha256: str
    corpus_records: int
    seed: int
    seed_node_id: str
    query_terms: tuple[str, ...]
    target_count: int
    constant_priority: int
    recall_threshold_numerator: int
    recall_threshold_denominator: int


@dataclass(frozen=True, slots=True)
class Fixture:
    manifest: FixtureManifest
    nodes: Mapping[str, Node]
    corpus_bytes: bytes
    manifest_bytes: bytes
    max_depth: int


@dataclass(frozen=True, slots=True)
class PriorityContext:
    """Policy-visible data; evaluator labels and payload sizes are absent."""

    source_id: str | None
    destination_id: str
    anchor_terms: tuple[str, ...]
    depth: int
    query_terms: tuple[str, ...]
    seed: int


class PriorityPolicy(Protocol):
    @property
    def name(self) -> str:
        """Stable policy identifier."""

    @property
    def version(self) -> str:
        """Stable implementation version."""

    def priority(self, context: PriorityContext) -> int:
        """Return a deterministic integer priority; higher values run first."""


@dataclass(frozen=True, slots=True)
class CurrentConstantPolicy:
    constant_priority: int
    name: str = "current_constant"
    version: str = "request-constant-v0"

    def priority(self, context: PriorityContext) -> int:
        del context
        return self.constant_priority


@dataclass(frozen=True, slots=True)
class BreadthFirstPolicy:
    name: str = "bfs"
    version: str = "negative-depth-v0"

    def priority(self, context: PriorityContext) -> int:
        return -context.depth


@dataclass(frozen=True, slots=True)
class DeterministicRandomPolicy:
    name: str = "deterministic_random"
    version: str = "sha256-seed-node-v0"

    def priority(self, context: PriorityContext) -> int:
        material = f"{context.seed}\0{context.destination_id}".encode()
        return int.from_bytes(hashlib.sha256(material).digest()[:8], "big") & ((1 << 63) - 1)


@dataclass(frozen=True, slots=True)
class QueryAnchorHeuristicPolicy:
    name: str = "pluggable_heuristic"
    version: str = "query-anchor-overlap-v0"

    def priority(self, context: PriorityContext) -> int:
        # Deliberately uses no destination ID, label, or payload information.
        overlap = len(set(context.anchor_terms) & set(context.query_terms))
        return overlap * 1_000 - context.depth


def _duplicate_rejecting_hook(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise BenchmarkError(f"duplicate JSON key: {key}")
        output[key] = value
    return output


def _load_json_object(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BenchmarkError(f"{label} is not valid UTF-8") from exc
    try:
        value = json.loads(decoded, object_pairs_hook=_duplicate_rejecting_hook)
    except BenchmarkError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise BenchmarkError(f"{label} is not valid strict JSON") from exc
    if type(value) is not dict:
        raise BenchmarkError(f"{label} must be a JSON object")
    return cast("dict[str, Any]", value)


def _require_exact_fields(
    value: Mapping[str, Any],
    expected: frozenset[str],
    *,
    label: str,
) -> None:
    fields = frozenset(value)
    if fields != expected:
        missing = sorted(expected - fields)
        unknown = sorted(fields - expected)
        raise BenchmarkError(f"{label} fields mismatch: missing={missing}, unknown={unknown}")


def _require_int(
    value: Any,
    *,
    label: str,
    minimum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BenchmarkError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise BenchmarkError(f"{label} must be >= {minimum}")
    return value


def _require_string(value: Any, *, label: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise BenchmarkError(f"{label} has an invalid string value")
    return value


def _require_terms(value: Any, *, label: str, allow_empty: bool) -> tuple[str, ...]:
    if type(value) is not list:
        raise BenchmarkError(f"{label} must be a list")
    terms: list[str] = []
    for index, raw_term in enumerate(value):
        terms.append(
            _require_string(
                raw_term,
                label=f"{label}[{index}]",
                pattern=TERM_PATTERN,
            )
        )
    if not allow_empty and not terms:
        raise BenchmarkError(f"{label} must not be empty")
    if len(terms) != len(set(terms)):
        raise BenchmarkError(f"{label} must not contain duplicates")
    return tuple(terms)


def _parse_manifest(raw: bytes) -> FixtureManifest:
    document = _load_json_object(raw, label="fixture manifest")
    _require_exact_fields(document, MANIFEST_FIELDS, label="fixture manifest")
    if document["schema"] != FIXTURE_SCHEMA:
        raise BenchmarkError("fixture manifest schema mismatch")
    if document["artifact_status"] != ARTIFACT_STATUS:
        raise BenchmarkError("fixture manifest artifact_status must remain synthetic-only")
    if document["claimable"] is not False:
        raise BenchmarkError("fixture manifest claimable must be false")
    if document["fixture_label"] != FIXTURE_LABEL:
        raise BenchmarkError("fixture manifest synthetic label mismatch")

    corpus_sha256 = _require_string(
        document["corpus_sha256"],
        label="corpus_sha256",
        pattern=SHA256_PATTERN,
    )
    corpus_records = _require_int(
        document["corpus_records"],
        label="corpus_records",
        minimum=1,
    )
    if corpus_records > MAX_RECORDS:
        raise BenchmarkError("corpus_records exceeds safety cap")
    seed = _require_int(document["seed"], label="seed", minimum=0)
    if seed > (1 << 63) - 1:
        raise BenchmarkError("seed exceeds signed 64-bit range")
    seed_node_id = _require_string(
        document["seed_node_id"],
        label="seed_node_id",
        pattern=NODE_ID_PATTERN,
    )
    query_terms = _require_terms(document["query_terms"], label="query_terms", allow_empty=False)
    if tuple(sorted(query_terms)) != query_terms:
        raise BenchmarkError("query_terms must be sorted")
    target_count = _require_int(document["target_count"], label="target_count", minimum=1)
    constant_priority = _require_int(document["constant_priority"], label="constant_priority")
    threshold_numerator = _require_int(
        document["recall_threshold_numerator"],
        label="recall_threshold_numerator",
        minimum=1,
    )
    threshold_denominator = _require_int(
        document["recall_threshold_denominator"],
        label="recall_threshold_denominator",
        minimum=1,
    )
    if threshold_numerator > threshold_denominator:
        raise BenchmarkError("recall threshold must be in (0, 1]")
    return FixtureManifest(
        corpus_sha256=corpus_sha256,
        corpus_records=corpus_records,
        seed=seed,
        seed_node_id=seed_node_id,
        query_terms=query_terms,
        target_count=target_count,
        constant_priority=constant_priority,
        recall_threshold_numerator=threshold_numerator,
        recall_threshold_denominator=threshold_denominator,
    )


def _parse_link(value: Any, *, node_id: str, index: int) -> Link:
    if type(value) is not dict:
        raise BenchmarkError(f"node {node_id} link {index} must be an object")
    link = cast("dict[str, Any]", value)
    _require_exact_fields(link, LINK_FIELDS, label=f"node {node_id} link {index}")
    destination_id = _require_string(
        link["to"],
        label=f"node {node_id} link {index} destination",
        pattern=NODE_ID_PATTERN,
    )
    anchor_terms = _require_terms(
        link["anchor_terms"],
        label=f"node {node_id} link {index} anchor_terms",
        allow_empty=True,
    )
    return Link(destination_id=destination_id, anchor_terms=anchor_terms)


def _parse_node(value: dict[str, Any], *, line_number: int) -> Node:
    _require_exact_fields(value, NODE_FIELDS, label=f"corpus line {line_number}")
    node_id = _require_string(
        value["id"],
        label=f"corpus line {line_number} id",
        pattern=NODE_ID_PATTERN,
    )
    payload_bytes = _require_int(
        value["payload_bytes"],
        label=f"node {node_id} payload_bytes",
        minimum=1,
    )
    if type(value["target"]) is not bool:
        raise BenchmarkError(f"node {node_id} target label must be boolean")
    raw_links = value["links"]
    if type(raw_links) is not list:
        raise BenchmarkError(f"node {node_id} links must be a list")
    if len(raw_links) > MAX_LINKS_PER_NODE:
        raise BenchmarkError(f"node {node_id} exceeds link safety cap")
    links = tuple(
        _parse_link(raw_link, node_id=node_id, index=index)
        for index, raw_link in enumerate(raw_links)
    )
    destinations = [link.destination_id for link in links]
    if len(destinations) != len(set(destinations)):
        raise BenchmarkError(f"node {node_id} contains duplicate destinations")
    if node_id in destinations:
        raise BenchmarkError(f"node {node_id} contains a self-link")
    return Node(
        node_id=node_id,
        payload_bytes=payload_bytes,
        target=value["target"],
        links=links,
    )


def _graph_depths(nodes: Mapping[str, Node], seed_node_id: str) -> dict[str, int]:
    depths = {seed_node_id: 0}
    queue: deque[str] = deque([seed_node_id])
    while queue:
        source_id = queue.popleft()
        for link in nodes[source_id].links:
            if link.destination_id not in nodes:
                raise BenchmarkError(
                    f"node {source_id} has dangling destination {link.destination_id}"
                )
            if link.destination_id not in depths:
                depths[link.destination_id] = depths[source_id] + 1
                queue.append(link.destination_id)
    if len(depths) != len(nodes):
        unreachable = sorted(set(nodes) - set(depths))
        raise BenchmarkError(f"corpus contains unreachable nodes: {unreachable[:8]}")
    return depths


def _reject_cycles(nodes: Mapping[str, Node], seed_node_id: str) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise BenchmarkError(f"corpus graph contains a cycle at {node_id}")
        if node_id in visited:
            return
        visiting.add(node_id)
        for link in nodes[node_id].links:
            visit(link.destination_id)
        visiting.remove(node_id)
        visited.add(node_id)

    visit(seed_node_id)


def load_fixture(
    corpus_path: Path,
    manifest_path: Path,
    *,
    trusted_corpus_sha256: str = PINNED_CORPUS_SHA256,
    trusted_manifest_sha256: str = PINNED_FIXTURE_MANIFEST_SHA256,
) -> Fixture:
    """Load and completely verify the hash-pinned synthetic graph."""

    try:
        corpus_bytes = corpus_path.read_bytes()
        manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise BenchmarkError(f"fixture input cannot be read: {exc}") from exc
    if not corpus_bytes or len(corpus_bytes) > MAX_CORPUS_BYTES:
        raise BenchmarkError("corpus size is empty or exceeds safety cap")
    if SHA256_PATTERN.fullmatch(trusted_corpus_sha256) is None:
        raise BenchmarkError("trusted corpus SHA-256 is invalid")
    if SHA256_PATTERN.fullmatch(trusted_manifest_sha256) is None:
        raise BenchmarkError("trusted fixture manifest SHA-256 is invalid")
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if manifest_sha256 != trusted_manifest_sha256:
        raise BenchmarkError(
            "fixture manifest SHA-256 does not match the compiled trust root: "
            f"expected {trusted_manifest_sha256}, got {manifest_sha256}"
        )
    corpus_sha256 = hashlib.sha256(corpus_bytes).hexdigest()
    if corpus_sha256 != trusted_corpus_sha256:
        raise BenchmarkError(
            "corpus SHA-256 does not match the compiled trust root: "
            f"expected {trusted_corpus_sha256}, got {corpus_sha256}"
        )
    manifest = _parse_manifest(manifest_bytes)
    if manifest.corpus_sha256 != trusted_corpus_sha256:
        raise BenchmarkError(
            "fixture manifest corpus SHA-256 does not match the compiled trust root"
        )

    nodes: dict[str, Node] = {}
    lines = corpus_bytes.splitlines()
    if len(lines) != manifest.corpus_records:
        raise BenchmarkError(
            f"corpus record count mismatch: expected {manifest.corpus_records}, got {len(lines)}"
        )
    for line_number, raw_line in enumerate(lines, start=1):
        if not raw_line:
            raise BenchmarkError(f"corpus line {line_number} is blank")
        node = _parse_node(
            _load_json_object(raw_line, label=f"corpus line {line_number}"),
            line_number=line_number,
        )
        if node.node_id in nodes:
            raise BenchmarkError(f"duplicate node ID: {node.node_id}")
        nodes[node.node_id] = node

    if manifest.seed_node_id not in nodes:
        raise BenchmarkError("seed_node_id is absent from corpus")
    targets = sum(node.target for node in nodes.values())
    if targets != manifest.target_count:
        raise BenchmarkError(
            f"target count mismatch: expected {manifest.target_count}, got {targets}"
        )
    depths = _graph_depths(nodes, manifest.seed_node_id)
    _reject_cycles(nodes, manifest.seed_node_id)
    return Fixture(
        manifest=manifest,
        nodes=nodes,
        corpus_bytes=corpus_bytes,
        manifest_bytes=manifest_bytes,
        max_depth=max(depths.values()),
    )


def make_policy(name: str, manifest: FixtureManifest) -> PriorityPolicy:
    if name == "current_constant":
        return CurrentConstantPolicy(manifest.constant_priority)
    if name == "bfs":
        return BreadthFirstPolicy()
    if name == "deterministic_random":
        return DeterministicRandomPolicy()
    if name == "pluggable_heuristic":
        return QueryAnchorHeuristicPolicy()
    raise BenchmarkError(f"unknown policy: {name}")


def _node_url(node_id: str) -> str:
    return f"https://fixture.invalid/{node_id}"


def _nearest_rank(values: Sequence[int], quantile: float) -> int:
    if not values:
        raise BenchmarkError("decision latency sample is empty")
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_jsonl_bytes(rows: Sequence[Mapping[str, Any]]) -> bytes:
    return b"".join(_canonical_json_bytes(row) + b"\n" for row in rows)


def _peak_rss_bytes() -> int:
    maximum = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return maximum if sys.platform == "darwin" else maximum * 1024


def _offline_audit_hook(event: str, _arguments: tuple[Any, ...]) -> None:
    """Fail if a worker attempts Python-level network or process escape."""

    if event.startswith("socket.") or event in {
        "os.posix_spawn",
        "os.spawn",
        "os.system",
        "subprocess.Popen",
    }:
        raise BenchmarkError(f"offline worker blocked audit event: {event}")


def _install_offline_guard() -> None:
    # This is defense in depth, not an OS sandbox. The benchmark has no network
    # dependency, and the isolated worker needs neither sockets nor children.
    sys.addaudithook(_offline_audit_hook)


def _timed_priority(
    policy: PriorityPolicy,
    context: PriorityContext,
    latencies_ns: list[int],
) -> int:
    started = time.perf_counter_ns()
    priority = policy.priority(context)
    elapsed = time.perf_counter_ns() - started
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise BenchmarkError(f"policy {policy.name} returned a non-integer priority")
    latencies_ns.append(elapsed)
    return priority


def run_policy(fixture: Fixture, policy: PriorityPolicy) -> dict[str, Any]:
    """Run one deterministic policy through the real production frontier."""

    manifest = fixture.manifest
    latencies_ns: list[int] = []
    seed_context = PriorityContext(
        source_id=None,
        destination_id=manifest.seed_node_id,
        anchor_terms=(),
        depth=0,
        query_terms=manifest.query_terms,
        seed=manifest.seed,
    )
    seed_priority = _timed_priority(policy, seed_context, latencies_ns)
    frontier = CrawlFrontier(
        [_node_url(manifest.seed_node_id)],
        config=FrontierConfig(
            max_depth=fixture.max_depth,
            max_urls=len(fixture.nodes),
            max_urls_per_host=len(fixture.nodes),
            max_fetch_attempts=len(fixture.nodes),
            max_fetch_attempts_per_host=len(fixture.nodes),
            max_attempts_per_url=1,
            host_delay_s=0,
        ),
        seed_priority=seed_priority,
    )
    url_to_node_id = {_node_url(node_id): node_id for node_id in fixture.nodes}
    trace: list[dict[str, Any]] = []
    targets_found = 0
    non_target_bytes = 0
    threshold_target_count = math.ceil(
        manifest.target_count
        * manifest.recall_threshold_numerator
        / manifest.recall_threshold_denominator
    )
    requests_to_threshold: int | None = None
    non_target_bytes_to_threshold: int | None = None
    cumulative_targets_sum = 0

    while True:
        lease = frontier.claim(now=0)
        if lease is None:
            break
        try:
            node_id = url_to_node_id[lease.url]
        except KeyError as exc:
            raise BenchmarkError(f"frontier produced unknown URL: {lease.url}") from exc
        node = fixture.nodes[node_id]
        request_index = len(trace) + 1
        if node.target:
            targets_found += 1
        else:
            non_target_bytes += node.payload_bytes
        if requests_to_threshold is None and targets_found >= threshold_target_count:
            requests_to_threshold = request_index
            non_target_bytes_to_threshold = non_target_bytes
        cumulative_targets_sum += targets_found

        frontier.succeed(lease)
        admissions: list[dict[str, Any]] = []
        for link in node.links:
            context = PriorityContext(
                source_id=node_id,
                destination_id=link.destination_id,
                anchor_terms=link.anchor_terms,
                depth=lease.depth + 1,
                query_terms=manifest.query_terms,
                seed=manifest.seed,
            )
            priority = _timed_priority(policy, context, latencies_ns)
            admission = frontier.admit(
                _node_url(link.destination_id),
                depth=lease.depth + 1,
                priority=priority,
                parent_url=lease.url,
                ready_at=0,
            )
            if not admission.accepted:
                raise BenchmarkError(
                    f"verified graph admission failed for {node_id}->{link.destination_id}: "
                    f"{admission.reason}"
                )
            admissions.append(
                {
                    "destination_id": link.destination_id,
                    "priority": priority,
                }
            )

        trace.append(
            {
                "admissions": admissions,
                "cumulative_target_count": targets_found,
                "depth": lease.depth,
                "node_id": node_id,
                "payload_bytes": node.payload_bytes,
                "priority": lease.priority,
                "request_index": request_index,
                "target": node.target,
                "target_recall_denominator": manifest.target_count,
                "target_recall_numerator": targets_found,
            }
        )

    if len(trace) != len(fixture.nodes):
        raise BenchmarkError(
            f"policy {policy.name} did not traverse full graph: "
            f"{len(trace)} != {len(fixture.nodes)}"
        )
    if targets_found != manifest.target_count:
        raise BenchmarkError(
            f"policy {policy.name} found {targets_found}/{manifest.target_count} targets"
        )
    if requests_to_threshold is None or non_target_bytes_to_threshold is None:
        raise BenchmarkError(f"policy {policy.name} never reached recall threshold")
    frontier_metrics = frontier.metrics()
    if (
        frontier_metrics.pending != 0
        or frontier_metrics.in_flight != 0
        or frontier_metrics.claimed != len(fixture.nodes)
    ):
        raise BenchmarkError(f"policy {policy.name} left inconsistent frontier state")

    auc = Fraction(
        cumulative_targets_sum,
        manifest.target_count * len(trace),
    )
    trace_bytes = _canonical_jsonl_bytes(trace)
    result = {
        "decision_latency_ns_p50": _nearest_rank(latencies_ns, 0.50),
        "decision_latency_ns_p95": _nearest_rank(latencies_ns, 0.95),
        "decision_samples": len(latencies_ns),
        "non_target_bytes_before_90pct": non_target_bytes_to_threshold,
        "peak_rss_bytes": _peak_rss_bytes(),
        "policy": policy.name,
        "policy_version": policy.version,
        "request_budget": len(trace),
        "requests_to_90pct_targets": requests_to_threshold,
        "target_threshold_count": threshold_target_count,
        "targets_total": manifest.target_count,
        "trace_sha256": hashlib.sha256(trace_bytes).hexdigest(),
        "yield_auc": round(float(auc), 12),
        "yield_auc_denominator": auc.denominator,
        "yield_auc_numerator": auc.numerator,
    }
    return {"result": result, "trace": trace}


def _worker(
    *,
    corpus_path: Path,
    manifest_path: Path,
    policy_name: str,
) -> dict[str, Any]:
    fixture = load_fixture(corpus_path, manifest_path)
    return run_policy(fixture, make_policy(policy_name, fixture.manifest))


def _run_isolated_worker(
    *,
    corpus_path: Path,
    manifest_path: Path,
    policy_name: str,
) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["PYTHONHASHSEED"] = "0"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--corpus",
        str(corpus_path),
        "--fixture-manifest",
        str(manifest_path),
        "--policy",
        policy_name,
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        raise BenchmarkError(
            f"isolated worker {policy_name} failed with {completed.returncode}: {stderr[:500]}"
        )
    if completed.stderr:
        raise BenchmarkError(f"isolated worker {policy_name} wrote unexpected stderr")
    payload = _load_json_object(
        completed.stdout.encode("utf-8"),
        label=f"isolated worker {policy_name} output",
    )
    if frozenset(payload) != {"result", "trace"}:
        raise BenchmarkError(f"isolated worker {policy_name} output fields mismatch")
    if type(payload["result"]) is not dict or type(payload["trace"]) is not list:
        raise BenchmarkError(f"isolated worker {policy_name} output types mismatch")
    result = cast("dict[str, Any]", payload["result"])
    trace = cast("list[dict[str, Any]]", payload["trace"])
    trace_bytes = _canonical_jsonl_bytes(trace)
    trace_sha256 = hashlib.sha256(trace_bytes).hexdigest()
    if result.get("policy") != policy_name or result.get("trace_sha256") != trace_sha256:
        raise BenchmarkError(f"isolated worker {policy_name} semantic trace mismatch")
    return {"result": result, "trace": trace}


def _write_new(path: Path, content: bytes) -> None:
    try:
        with path.open("xb") as handle:
            handle.write(content)
    except OSError as exc:
        raise BenchmarkError(f"cannot create artifact {path}: {exc}") from exc


def _prepare_output(output_dir: Path) -> None:
    try:
        output_dir.mkdir(parents=True, exist_ok=False)
        (output_dir / "traces").mkdir()
    except FileExistsError as exc:
        raise BenchmarkError("output directory already exists; refusing to mix artifacts") from exc
    except OSError as exc:
        raise BenchmarkError(f"cannot prepare output directory: {exc}") from exc


def run_benchmark(
    *,
    corpus_path: Path,
    manifest_path: Path,
    output_dir: Path,
    worker_runner: Callable[..., dict[str, Any]] = _run_isolated_worker,
) -> dict[str, Any]:
    """Verify inputs, isolate each policy, and emit a hash-complete artifact."""

    fixture = load_fixture(corpus_path, manifest_path)
    _prepare_output(output_dir)
    marker = (
        b"NOT CLAIMABLE\n\n"
        b"Synthetic regression fixture only. This artifact cannot support a "
        b"product comparison, leaderboard, SOTA, or external benchmark claim.\n"
    )
    _write_new(output_dir / "NOT_CLAIMABLE.txt", marker)

    results: list[dict[str, Any]] = []
    trace_hashes: dict[str, str] = {}
    for policy_name in POLICY_NAMES:
        payload = worker_runner(
            corpus_path=corpus_path,
            manifest_path=manifest_path,
            policy_name=policy_name,
        )
        result = cast("dict[str, Any]", payload["result"])
        trace = cast("list[dict[str, Any]]", payload["trace"])
        trace_bytes = _canonical_jsonl_bytes(trace)
        trace_path = output_dir / "traces" / f"{policy_name}.jsonl"
        _write_new(trace_path, trace_bytes)
        trace_sha256 = hashlib.sha256(trace_bytes).hexdigest()
        if result["trace_sha256"] != trace_sha256:
            raise BenchmarkError(f"trace changed before write for {policy_name}")
        trace_hashes[str(trace_path.relative_to(output_dir))] = trace_sha256
        results.append(result)

    report = {
        "artifact_status": ARTIFACT_STATUS,
        "claimable": False,
        "fixture": {
            "corpus_records": fixture.manifest.corpus_records,
            "corpus_sha256": fixture.manifest.corpus_sha256,
            "fixture_label": FIXTURE_LABEL,
            "query_terms": list(fixture.manifest.query_terms),
            "seed": fixture.manifest.seed,
            "target_count": fixture.manifest.target_count,
        },
        "protocol_version": PROTOCOL_VERSION,
        "results": results,
        "schema": REPORT_SCHEMA,
    }
    report_bytes = _canonical_json_bytes(report) + b"\n"
    _write_new(output_dir / "report.json", report_bytes)

    source_bytes = Path(__file__).resolve().read_bytes()
    protocol_bytes = PROTOCOL_PATH.read_bytes()
    run_manifest = {
        "artifact_status": ARTIFACT_STATUS,
        "artifacts": {
            "NOT_CLAIMABLE.txt": hashlib.sha256(marker).hexdigest(),
            "report.json": hashlib.sha256(report_bytes).hexdigest(),
            **trace_hashes,
        },
        "benchmark_source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "claimable": False,
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "fixture": {
            "corpus_path_basename": corpus_path.name,
            "corpus_sha256": hashlib.sha256(fixture.corpus_bytes).hexdigest(),
            "manifest_path_basename": manifest_path.name,
            "manifest_sha256": hashlib.sha256(fixture.manifest_bytes).hexdigest(),
        },
        "policies": list(POLICY_NAMES),
        "protocol_sha256": hashlib.sha256(protocol_bytes).hexdigest(),
        "protocol_version": PROTOCOL_VERSION,
        "schema": RUN_MANIFEST_SCHEMA,
    }
    _write_new(
        output_dir / "run_manifest.json",
        _canonical_json_bytes(run_manifest) + b"\n",
    )
    return report


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument(
        "--fixture-manifest",
        type=Path,
        default=DEFAULT_FIXTURE_MANIFEST,
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--policy", choices=POLICY_NAMES, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    try:
        if args.worker:
            if args.policy is None:
                raise BenchmarkError("--worker requires --policy")
            _install_offline_guard()
            payload = _worker(
                corpus_path=args.corpus,
                manifest_path=args.fixture_manifest,
                policy_name=args.policy,
            )
            sys.stdout.buffer.write(_canonical_json_bytes(payload) + b"\n")
            return 0
        if args.output_dir is None:
            raise BenchmarkError("--output-dir is required")
        run_benchmark(
            corpus_path=args.corpus,
            manifest_path=args.fixture_manifest,
            output_dir=args.output_dir,
        )
        return 0
    except BenchmarkError as exc:
        print(f"focused frontier benchmark failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

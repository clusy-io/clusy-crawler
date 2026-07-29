# Focused frontier benchmark v0

Status: **`SYNTHETIC_ONLY / NOT_CLAIMABLE`**.

This is a deterministic offline regression harness, not an external benchmark
and not evidence for a product-quality or SOTA headline. Its only purpose is to
measure one known architectural limitation: the recursive owner currently
passes one request-level priority to every discovered link. On a single host,
equal priorities preserve admission order and therefore behave like breadth
first search.

## Fixture boundary

`synthetic_graph.jsonl` is a hand-authored graph. It contains no fetched pages,
vendor output, external benchmark examples, or network-derived content. Every
node has a mandatory boolean target label and payload byte count. Every edge
has only a destination ID and synthetic anchor terms. Node IDs are opaque.

`fixture_manifest.json` pins the raw corpus SHA-256, record count, seed node,
query terms, target count, recall threshold, constant priority, and fixed
random seed. The runner also compiles in independent SHA-256 trust roots for
both the raw manifest and raw corpus, so changing both adjacent files cannot
silently define a different benchmark. The loader rejects unknown/missing
fields, a missing/non-boolean target label, an incorrect synthetic-only label,
hash drift, duplicate IDs, dangling links, cycles, unreachable nodes, and
mismatched target counts.

The target label and payload size are evaluator-only. Priority policies receive
only source/destination IDs, edge anchor terms, depth, query terms, and the
fixed seed. The query-anchor heuristic cannot inspect the target label or
payload bytes.

## Policies

- `current_constant`: exactly models `_RecursiveCrawlJob` by applying the
  manifest's one constant integer priority to the seed and every admitted link.
- `bfs`: assigns `-depth`, providing an explicit breadth-first control.
- `deterministic_random`: assigns a stable SHA-256-derived priority from the
  fixed seed and destination ID.
- `pluggable_heuristic`: a minimal query-conditioned scorer using only query
  overlap in synthetic anchor terms, with a small depth penalty.

All policies feed the real `CrawlFrontier`; the harness changes no production
code. Each policy runs in a fresh subprocess so peak RSS high-water marks do
not leak between policies. Once started, each worker installs a Python audit
guard that rejects socket and child-process events. The code has no network
dependency; the guard makes an accidental future online policy fail closed.

This is a single-in-flight scheduler microbenchmark. It reproduces the current
constant-priority admission decision, but it does not simulate transport,
robots policy, asynchronous completion, ordered retirement, or a production
concurrency window. Its request-yield measurements are architectural
diagnostics, not end-to-end recursive-crawl performance.

## Metrics

For each policy the report records:

- `requests_to_90pct_targets`: inclusive request count at the first prefix
  reaching the pinned 90% target recall threshold;
- `non_target_bytes_before_90pct`: all non-target payload bytes in that
  inclusive prefix;
- `yield_auc`: mean cumulative target recall over the common full reachable
  request budget (higher means targets were found earlier);
- `decision_latency_ns_p50` and `decision_latency_ns_p95`: nearest-rank
  latency of each per-link priority call, including the seed decision;
- `peak_rss_bytes`: worker-process high-water resident set size; and
- `trace_sha256`: SHA-256 over canonical semantic JSONL. Timing and RSS are not
  included in the semantic trace.

The full graph is traversed by every policy, giving all policies the same
request denominator for yield AUC. The runner writes each canonical trace,
`report.json`, `run_manifest.json`, and an unconditional
`NOT_CLAIMABLE.txt`. The run manifest hashes the fixture manifest, corpus,
report, and every trace.

## Run

```bash
uv run python bench/focused_frontier_benchmark.py \
  --output-dir /tmp/clusy-focused-frontier-v0
```

No network access or credential is used by the harness.

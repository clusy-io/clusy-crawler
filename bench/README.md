# Benchmark and evidence index

Clusy separates extraction quality, structure fidelity, local implementation
speed, live service behavior, and vendor comparison. Evidence in one category
does not establish another.

## Public benchmark protocols

| Suite | Scope | Protocol |
| --- | --- | --- |
| Article Extraction Benchmark | Article-body precision, recall, and F1 | [`NEUTRAL_BENCHMARK.md`](NEUTRAL_BENCHMARK.md) |
| WCXB | Main-content extraction across seven page types | [`WCXB_BENCHMARK.md`](WCXB_BENCHMARK.md) |
| Webis | Historical multi-corpus main-content extraction | [`WEBIS_BENCHMARK.md`](WEBIS_BENCHMARK.md) |
| WebMainBench v1.1 | Broad multilingual main-content extraction | [`WEBMAINBENCH_BENCHMARK.md`](WEBMAINBENCH_BENCHMARK.md) |
| WebMainBench 545 | Text, code, formula, and table fidelity | [`WEBMAINBENCH_FINEGRAINED_BENCHMARK.md`](WEBMAINBENCH_FINEGRAINED_BENCHMARK.md) |

## Diagnostic and research protocols

| Protocol | Status | Purpose |
| --- | --- | --- |
| [`evidence/selection-atom-catalog-e5958b5/PROTOCOL.md`](evidence/selection-atom-catalog-e5958b5/PROTOCOL.md) | HTML-only diagnostic | Measure default-off source-map/catalog representation coverage and local mechanism cost without labels or scoring |
| [`WEBMAINBENCH_IR_LABEL_ORACLE.md`](WEBMAINBENCH_IR_LABEL_ORACLE.md) | Label oracle; not claimable | Estimate the ceiling and failure modes of ordered source-backed IR |
| [`lattice_reference/README.md`](lattice_reference/README.md) | Research only | Test an exact typed source-span decoder |
| [`focused_frontier_v0/PROTOCOL.md`](focused_frontier_v0/PROTOCOL.md) | Synthetic only | Exercise pluggable frontier priority without network access |
| [`vendor_eval_v2/PROTOCOL.md`](vendor_eval_v2/PROTOCOL.md) | Synthetic verifier | Validate the sealed scoring kernel used by live-vendor evaluation |

## Live-vendor protocol

[`LIVE_VENDOR_BENCHMARK.md`](LIVE_VENDOR_BENCHMARK.md) is the only approved
Exa/Firecrawl comparison path. It requires a preregistered manifest, sealed
evaluator, exact provider request records, cost and latency accounting, paired
analysis, and explicit comparability limits.

Vendor outputs are evaluation-only. They must not be used for training,
distillation, prompt construction, routing, or runtime extraction.

## Cloud execution standard

Strict performance evidence uses an ephemeral, non-burstable compute host:

1. Transfer an immutable source bundle and verify source, dataset, runner, and
   lockfile hashes before building.
2. Record the image, CPU model/topology, kernel, toolchains, dependency locks,
   storage, and relevant runtime configuration.
3. Finish downloads, builds, indexing, and package maintenance before timing.
4. Run base and candidate on the same pinned cores in counterbalanced order;
   keep every retained sample and invalidate the complete group on noise,
   throttling, source drift, or output mismatch.
5. Report p50/p95, peak memory, order bias, raw samples, and exact output
   commitments. A microbenchmark cannot waive a full-pipeline regression.
6. Repeat a promotion result from a clean boot or independently provisioned
   host before merging.

Cloud hardware makes a run easier to isolate; it does not make absolute rates
portable across machines. Only protocol-matched comparisons receive a
performance interpretation.

## Local result bundles

Benchmark runs may write source-bound artifacts under `bench/results/`. That
directory is intentionally ignored and is not present in a clean checkout.
Only the immutable evidence records linked below are checked in; larger AEB,
WCXB, Webis, and WebMainBench raw bundles must be retained separately with
their hashes and the corresponding reproduction protocol.

## Registered evidence

[`evidence/registry.json`](evidence/registry.json) is the machine-enforced
claim index. It binds every published measurement to a source identity, frozen
protocol, compact artifact, raw-retention status, metric pointer, scope, and
permission gates.

| Record | Status |
| --- | --- |
| [`evidence/aeb-article-body-trafilatura-2-1-73b0297-public/PROTOCOL.md`](evidence/aeb-article-body-trafilatura-2-1-73b0297-public/PROTOCOL.md) | Verified scoped AEB result against exact Trafilatura 2.1.0 |

This is the only current authorized result. Evidence directories absent from
the registry are archival, non-authorizing records and are not part of the
current evidence index.

Specifically, `evidence/aeb-article-body-4dd1755-public/` and
`evidence/native-filter-stack-95b3bbe-public/` are superseded archival
receipts, while `evidence/selection-atom-catalog-e5958b5/` is an archival
research diagnostic. None authorizes publication.

## Interpretation rules

1. Use the metric and aggregation defined by the named protocol.
2. Separate public-label diagnostics from blind or permissioned holdouts.
3. Report extraction-loop throughput separately from HTTP, rendering, and
   end-to-end service latency.
4. Do not compare different output contracts as if they were one task.
5. Retain negative and null results when they determine promotion.
6. A rejected candidate remains rejected even if one retained sample is
   positive.

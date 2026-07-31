# Benchmark evidence

This document is the human-readable index for claims made by the open-source
crawler repository. The machine-enforced source of truth is
[`bench/evidence/registry.json`](../bench/evidence/registry.json).

## Publication rule

Only a current registry entry can authorize a measured statement in an
enforced documentation surface. Every such statement carries its claim ID on
the same line. Unregistered files and local run directories are archival or
diagnostic material only; they do not authorize a public result.

## Registered results

> **Verified evidence — Article Extraction Benchmark · `article_body` · 181 pages.** Clusy F1 `0.972127`; exact Trafilatura 2.1.0 F1 `0.957546`; F1 delta `+0.014581`; F1 delta CI95 low `+0.005547`; F1 delta CI95 high `+0.025336`; paired-bootstrap win fraction `0.9996`; machine-local in-memory throughput `173.97 pages/s`. <!-- clusy-evidence: aeb.article-body.trafilatura-2-1.77b8d00-beta2-public.2026-07-31 -->

### AEB claim boundary

The registered run uses all public AEB pages, the pinned upstream evaluator,
identity transformation of production `article_body` output, deterministic
ordering, and a bounded two-worker loop. It was executed directly from clean
open-source commit `77b8d00c5ebf88ed3afffe64f869ccb8c6922365`; its tree is
identical to the tree tagged `v0.2.0-beta.2`. Before labels are loaded, a
dedicated Python process replays exact Trafilatura 2.1.0 from a 17-package
hash-pinned environment over a label-free HTML capsule.

The raw predictions, comparator receipt, per-page measurements, production
Markdown, original report, and split manifest are retained in a deterministic
hashed external archive. Its members and metadata were normalized, two builds
were byte-identical, and a fresh extraction passed every manifest hash.

This is evidence for article-body extraction on AEB. It does not evaluate
recursive discovery, JavaScript rendering, general-web document structure,
HTTP-service behavior, reliability, cost, or live providers. The local
throughput value is one exact in-memory extraction observation, not a
stability result, crawler rate, HTTP-service rate, or service-level guarantee.

Current evidence:

- [frozen protocol](../bench/evidence/aeb-article-body-trafilatura-2-1-77b8d00-beta2-public/PROTOCOL.md);
- [compact report](../bench/evidence/aeb-article-body-trafilatura-2-1-77b8d00-beta2-public/report.json);
- [registry entry](../bench/evidence/registry.json).

## Protocols without a published result

WCXB, Webis, both WebMainBench tracks, the ordered-IR label oracle, local
implementation microbenchmarks, and live-provider evaluation remain
reproducible protocol surfaces. This release publishes no result from those
tracks. A future result must be run from clean public source, retain its
permitted raw evidence, receive a registry binding, and pass the documentation
validator before it appears here.

## Claim controls

The evidence validator runs in CI and fails closed on:

- missing, ignored, or symlinked protocol and artifact paths;
- source, protocol, artifact, manifest, or archive hash mismatches;
- unclean source for Verified claims;
- metric values that differ from their JSON pointers, and published
  label/value pairs not explicitly bound by the registry;
- unregistered metrics or unsupported comparative and leadership statements
  in first-party Markdown outside explicit protocol-only or archival boundaries;
- evidence markers outside the exact canonical publication line derived from
  registered metric keys, artifact pointers, units, and displays;
- protocol-only numeric thresholds without the dedicated threshold annotation,
  or annotations that cover a result assertion or multiple metric values;
- personal absolute paths or restricted evidence lineage;
- mutable production-state language in first-party documentation; and
- broad leadership or unsupported live-provider claims.

Protocol-only numeric gates use one canonical line:

```text
- Threshold: <metric-id> <operator> <decimal> <unit>. <!-- clusy-protocol-threshold -->
```

Allowed units are `score`, `points`, `ratio`, `percent`, `milliseconds`, and
`count`. The annotation is invalid outside an explicit protocol-only file or
when any extra clause shares its line.

Run it locally:

```bash
uv run python scripts/check_evidence_claims.py
```

## Reproduction entry points

| Suite | Protocol |
| --- | --- |
| AEB | [`bench/NEUTRAL_BENCHMARK.md`](../bench/NEUTRAL_BENCHMARK.md) |
| WCXB | [`bench/WCXB_BENCHMARK.md`](../bench/WCXB_BENCHMARK.md) |
| Webis | [`bench/WEBIS_BENCHMARK.md`](../bench/WEBIS_BENCHMARK.md) |
| WebMain | [`bench/WEBMAINBENCH_BENCHMARK.md`](../bench/WEBMAINBENCH_BENCHMARK.md) |
| Fine-grained WebMain | [`bench/WEBMAINBENCH_FINEGRAINED_BENCHMARK.md`](../bench/WEBMAINBENCH_FINEGRAINED_BENCHMARK.md) |
| Live providers | [`bench/LIVE_VENDOR_BENCHMARK.md`](../bench/LIVE_VENDOR_BENCHMARK.md) |

Ground truth is used only by the scorer after predictions are frozen when the
protocol requires label isolation. Live-provider outputs are evaluation inputs,
not training or distillation data.

## Deployment boundary

Benchmark evidence does not prove a deployment. Operators must independently
verify the exact image/source identity, configuration, readiness,
authentication, SSRF behavior, and live crawl before promotion. See
[Self-hosting](SELF_HOSTING.md) and [Operations](OPERATIONS.md).

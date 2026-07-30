# Benchmark evidence

This document is the human-readable index for claims made by the open-source
crawler repository. The machine-enforced source of truth is
[`bench/evidence/registry.json`](../bench/evidence/registry.json).

## Status vocabulary

| Status | Meaning |
| --- | --- |
| **Verified** | Clean public source, frozen protocol, retained raw evidence, exact compact-artifact binding, and all claimability gates pass |
| **Diagnostic** | Useful engineering signal with a known limitation that prevents a product claim |
| **Historical** | Accurate for a dated source or environment; not a statement about a live deployment |
| **Rejected** | Evaluated candidate that failed a preregistered promotion gate |
| **Pending** | Protocol or execution is incomplete; no result is claimed |

Only a registry entry can authorize a measured statement in this file or the
README. Every such statement carries its claim ID on the same line.

## Registered results

| Status | Suite and boundary | Measured result | Interpretation |
| --- | --- | --- | --- |
| Verified | AEB `article_body`, 181 pages; direct public-repository run | P/R/F1 `0.955147 / 0.989721 / 0.972127`; `152.71` pages/s | F1 `+0.014581` vs exact Trafilatura 2.1.0; paired 95% interval `[+0.005547, +0.025336]`; paired-bootstrap win fraction `0.9996` <!-- clusy-evidence: aeb.article-body.trafilatura-2-1.73b0297-public.2026-07-30 --> |
| Historical 2026-07-29 | Local native extraction on three locked corpora | Rate change: WebMain `+13.9905%`, WCXB `+26.9355%`, stress `+35.3818%` | All registered fields exact; original raw bundle not retained <!-- clusy-evidence: native.filter-stack.95b3bbe-public.2026-07-29 --> |

### AEB claim boundary

The registered run uses all public AEB pages, the pinned upstream evaluator,
identity transformation of production `article_body` output, deterministic
ordering, and a bounded two-worker loop. It was executed directly from clean
open-source commit `73b02974b4cf2aab0764922cf7ac664e0f3bc36f`.
Before labels are loaded, a dedicated Python process replays exact
Trafilatura 2.1.0 from a 17-package hash-pinned environment over a label-free
HTML capsule.

The raw predictions, comparator receipt, per-page measurements, production
Markdown, original report, and split manifest are retained in a hashed
external archive.

This is evidence for article-body extraction on AEB. It does not evaluate
recursive discovery, JavaScript rendering, general-web document structure,
HTTP-service behavior, reliability, cost, or live providers.

Current evidence:

- [frozen protocol](../bench/evidence/aeb-article-body-trafilatura-2-1-73b0297-public/PROTOCOL.md);
- [compact report](../bench/evidence/aeb-article-body-trafilatura-2-1-73b0297-public/report.json);
- [registry entry](../bench/evidence/registry.json).

The older Trafilatura 2.0 comparison remains registered as a dated record; it
is not the current README comparison.

## Evidence under evaluation

The following tracks are intentionally absent from the registered result table:

| Track | Status | Publication boundary |
| --- | --- | --- |
| WCXB extraction | Diagnostic | Dataset-overlap and classifier provenance must be closed |
| Webis extraction | Historical | Existing record came from private source; a direct public rerun is required |
| WebMain direct Markdown | Diagnostic | Existing record is not a direct public-repository run |
| Fine-grained WebMain | Diagnostic | Structure reconstruction is being evaluated with frozen label-free predictions |
| Atomic structure overlay | Diagnostic | Default-off research path; not wired into serving |
| Cloud hot-path A/B | Pending | Formal cross-zone evidence is not yet registered |
| Exa and Firecrawl | Pending | No completed matched live-provider result is registered |

Diagnostic artifacts may guide engineering decisions, but they do not authorize
leadership, deployment, or vendor-superiority language.

## Historical implementation record

The registered native A/B is a newly issued public-only compact record. It
binds the public runtime source, locked binaries and corpora, counterbalanced
local-loop protocol, exact-output commitments, environment, and limitations.
It omits private cloud, image-registry, deployment, traffic, rollback, and
private source metadata.

The original raw bundle is unavailable, so this result is visibly Historical
instead of Verified:

- [sanitized protocol](../bench/evidence/native-filter-stack-95b3bbe-public/PROTOCOL.md);
- [sanitized compact report](../bench/evidence/native-filter-stack-95b3bbe-public/report.json).

## Claim controls

The evidence validator runs in CI and fails closed on:

- missing, ignored, or symlinked protocol and artifact paths;
- source, protocol, artifact, manifest, or archive hash mismatches;
- unclean source for Verified claims;
- metric values that differ from their JSON pointers;
- Diagnostic or Rejected claims with an open superiority gate;
- unregistered measured language in enforced documentation;
- mutable deployment language in versioned documentation; and
- broad leadership or unsupported live-provider claims.

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

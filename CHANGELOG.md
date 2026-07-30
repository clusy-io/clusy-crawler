# Changelog

Notable changes are recorded using
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories. The
project has not published a stable semantic-version tag; source commits and
container digests remain the authoritative release identities.

## Unreleased

No changes yet.

## 0.2.0-beta.1 - 2026-07-30

This branch contains the open-source crawler implementation and its
self-hosting assets. Repository presence never implies that an operator has
deployed a particular revision.

### Added

- Native Rust/PyO3 extraction with ordered DOM IR v2, typed structures, source
  spans, deterministic serialization, and replayable selection certificates.
- Explicit `balanced`, `article_body`, `adaptive`, and `quality` extraction
  profiles with a deterministic fallback.
- Bounded recursive discovery with robots, same-site, host-fairness, trap, and
  resource policies.
- GitHub, PDF, academic, and scholarly-metadata specialists.
- Fixed-protocol harnesses for AEB, WCXB, Webis, WebMainBench v1.1,
  WebMainBench 545, synthetic focused-frontier behavior, and sealed
  live-vendor evaluation.
- Separate static, browser, and optional quality container targets with OCI
  source, license, and revision labels.
- Readiness/version endpoints with source, image, dependency, pipeline, router,
  and non-secret serving-configuration identities.
- Apache-2.0 self-hosting, architecture, operations, benchmark, research,
  security, and contribution documentation.

### Changed

- The native filtered-DOM traversal now carries ancestor filter state in a
  linear preorder stack. It preserved all measured native output fields and
  improved locked-corpus local extraction rate; see
  [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md).
- General extraction is native-first with confidence-gated Python fallbacks.
- Cache keys bind runtime and serving semantics. Policy-aware recursive crawls
  bypass the flat result cache because its envelope cannot replay the full
  redirect, robots, and scope chain.
- Browser rendering uses fresh contexts and validates navigation and
  subresource destinations while preserving Chromium's sandbox.
- The default Compose stack binds to loopback and bundles a non-durable Redis
  cache for local self-hosting.

### Fixed

- Redirect, DNS, IPv4-mapped IPv6, decompressed-body, browser-subresource,
  response-budget, cancellation, shutdown, and cache-failure boundaries.
- Duplicate native source-node emission and discarded fallback DOM work.
- Static-image dependency leakage: the static target contains no Playwright or
  Chromium artifacts.
- Benchmark provenance gaps by binding reports to source, dataset, evaluator,
  dependency, and prediction identities.

### Research

- Source-backed exact lattice decoding, selection certificates, and the
  synthetic focused-frontier policy remain default-off or unwired.
- A filtered-HTML-shape experiment failed its preregistered promotion gates and
  was not included.
- Cross-benchmark leadership and live-provider superiority remain unproven.
  Their gates are defined in [`docs/RESEARCH.md`](docs/RESEARCH.md).

## Evidence

Registered results and claim boundaries are maintained in
[`docs/BENCHMARKS.md`](docs/BENCHMARKS.md). Immutable implementation records
live under [`bench/evidence`](bench/evidence).

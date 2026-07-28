# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial public release as an Apache-2.0 open-source project.
- Native Rust/PyO3 extraction backend with pinned crates and a
  confidence-gated Python fallback.
- Explicit `balanced`, `article_body`, `adaptive`, and `quality` extraction
  profiles with bounded model-assisted fallback behavior.
- Opt-in recursive crawling with a deterministic fair frontier, canonical
  deduplication, crawl-trap budgets, scope checks, and fail-closed robots
  enforcement.
- Dedicated GitHub, academic/PDF, and scholarly-metadata extraction paths.
- Admission, request-body, response-size, timeout, concurrency, and cache-entry
  resource limits.
- Commit-pinned AEB, WCXB, Webis, WebMainBench, fine-grained structure, and
  sealed live-vendor benchmark harnesses with provenance and claimability gates.
- Hardened non-root container build, Chromium sandbox profile, and
  self-contained local Compose stack.
- `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, and GitHub Actions CI
  for Python, Rust, tests, and the deterministic container image.
- `CORS_ALLOW_ORIGINS` setting — CORS is now off by default (opt-in per origin).

### Changed
- **Licensing:** replaced PyMuPDF (AGPL-3.0) with pypdfium2 (BSD) for PDF
  extraction, and html2text (GPLv3) with markdownify (MIT) for HTML→markdown, so
  the whole distribution is permissively licensed.
- Upgraded trafilatura to 2.0.
- Extraction is native-first for the fast deterministic path and retains
  page-type-aware Python strategies as a confidence-gated fallback.
- Cache keys now include source revision and every output-affecting option;
  concurrent identical misses are coalesced.
- Browser renders use fresh contexts and validate navigation/subresource
  destinations while preserving an explicit, sandboxed JS lane.
- Health and result contracts expose build/backend identity, content scope,
  truncation, strategy, cache, and render metadata.

### Fixed
- **SSRF hardening:** redirects are now followed manually and re-validated per
  hop; all resolved IPs are checked (not just the first); IPv4-mapped IPv6 and
  additional reserved ranges are blocked; the Playwright renderer no longer
  disables the same-origin policy.
- **Decompression-bomb / OOM:** response bodies are size-capped while streaming.
- **Recursive crawl safety:** robots redirects, peer addresses, gzip expansion,
  sitemap traversal, off-site navigation, and crawl-trap variants are bounded
  and validated.
- **Lifecycle safety:** native extraction, browser work, structured extraction,
  and shutdown paths use bounded capacity and deterministic cleanup.
- **Extraction correctness:** the multi-strategy union no longer lets a
  full-page HTML dump win the base slot when a clean extractor produced usable
  output (this had collapsed article-body precision on real pages).
- `pytest` now runs on a fresh checkout (`pythonpath` configured).

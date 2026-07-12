# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial public release as an Apache-2.0 open-source project.
- Neutral benchmark harness (`bench/neutral_benchmark.py`,
  `bench/NEUTRAL_BENCHMARK.md`) scoring extraction against Zyte's
  article-extraction-benchmark. Current result: **F1 0.960** (precision 0.955,
  recall 0.965) — ahead of trafilatura 2.0 and Diffbot, behind Zyte AutoExtract.
- `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, GitHub Actions CI
  (ruff + mypy + pytest).
- `CORS_ALLOW_ORIGINS` setting — CORS is now off by default (opt-in per origin).

### Changed
- **Licensing:** replaced PyMuPDF (AGPL-3.0) with pypdfium2 (BSD) for PDF
  extraction, and html2text (GPLv3) with markdownify (MIT) for HTML→markdown, so
  the whole distribution is permissively licensed.
- Upgraded trafilatura to 2.0.
- Extraction pipeline: base extraction now routes news/blog articles (detected
  via Open Graph / schema.org metadata) onto a precision-first path with a
  generic boilerplate-line filter, while keeping the recall-friendly path for
  reference/data/documentation pages.

### Fixed
- **SSRF hardening:** redirects are now followed manually and re-validated per
  hop; all resolved IPs are checked (not just the first); IPv4-mapped IPv6 and
  additional reserved ranges are blocked; the Playwright renderer no longer
  disables the same-origin policy.
- **Decompression-bomb / OOM:** response bodies are size-capped while streaming.
- **Extraction correctness:** the multi-strategy union no longer lets a
  full-page HTML dump win the base slot when a clean extractor produced usable
  output (this had collapsed article-body precision on real pages).
- `pytest` now runs on a fresh checkout (`pythonpath` configured).

# Third-Party Licenses

Clusy Crawler is released under the Apache License 2.0. Its direct application
dependencies use the licenses listed below. No GPL/AGPL direct runtime
dependency is intentionally bundled or required. The exact Python and Rust
dependency graphs are recorded in [`uv.lock`](uv.lock) and
[`native/Cargo.lock`](native/Cargo.lock);
distributors remain responsible for preserving all applicable notices,
including notices for transitive dependencies and Chromium.

## Direct Python runtime dependencies

| Package | Version constraint | License |
|---------|--------------------|---------|
| clusy-native | 0.1.0 (local path package) | Apache-2.0 |
| fastapi | >=0.115,<1.0 | MIT |
| uvicorn[standard] | >=0.45,<1.0 | BSD-3-Clause |
| pydantic | >=2.10,<3.0 | MIT |
| pydantic-settings | >=2.14.2,<3.0 | MIT |
| starlette | >=1.3.1,<2.0 | BSD-3-Clause |
| httpx | >=0.28,<1.0 | BSD-3-Clause |
| h2 | >=4.3 | MIT |
| brotli | >=1.1 | MIT |
| zstandard | >=0.23 | BSD-3-Clause |
| trafilatura | >=2.1,<3.0 | Apache-2.0 |
| markdownify | >=1.2,<2.0 | MIT |
| beautifulsoup4 | >=4.12,<5.0 | MIT |
| readability-lxml | >=0.8.1,<1.0 | Apache-2.0 |
| lxml-html-clean | >=0.4.5,<1.0 | BSD-3-Clause |
| pypdfium2 | >=4.30 | BSD-3-Clause / Apache-2.0 (bundles Google PDFium, BSD-3-Clause) |
| playwright | >=1.48,<2.0 | Apache-2.0 |
| lxml | >=6.0.2,<7.0 | BSD-3-Clause |
| structlog | >=24.4,<26.0 | Apache-2.0 / MIT |
| tenacity | >=9.0,<10.0 | Apache-2.0 |
| aiolimiter | >=1.0,<2.0 | MIT |
| redis | >=5.2,<6.0 | MIT |
| orjson | >=3.10,<4.0 | Apache-2.0 / MIT |

Optional extra `[llm]` (off by default for source installs):
`anthropic>=0.69` (MIT). The checked-in Dockerfile installs this package in the
runtime image, but the feature remains inactive unless configured.

The Dockerfile's `static-runtime` deployment profile uses the frozen lock graph
with `playwright` pruned and therefore also omits its otherwise-unreferenced
`greenlet` and `pyee` dependencies. The `browser-runtime`, `quality-runtime`,
and compatibility `runtime` targets retain the complete direct dependency
graph shown above.

Optional extra `[quality]` (off by default) pins
`mineru-html[openai]` (Apache-2.0) to upstream revision
`73cf266690befd209cae7e6fdff9716d5b31a976`. It provides the MinerU-HTML v1.1
preprocessing, OpenAI-compatible inference adapter, label mapping, and
Markdown conversion pipeline. Its transitive dependency graph is recorded in
`uv.lock`; preserve the packages' notices when distributing this extra.

No model weights are bundled. In particular, the upstream v1.1 compact model
is a Tencent Hunyuan derivative under the Tencent Hunyuan Community License,
not Apache-2.0; that license excludes the EU, UK, and South Korea and imposes
additional use restrictions. Configuring a remote or local checkpoint is an
operator decision and requires a separate model/data/license review.

The architecture record also evaluates model candidates without adding them as
dependencies. Pulpie Orange's published weights are CC BY-NC 4.0 and therefore
must not be used in commercial deployments without separate rights. mmBERT
(MIT) and Qwen3.5-0.8B-Base (Apache-2.0) are permissively licensed candidate
bases, but no weights are copied into this repository and checkpoint licensing
does not replace review of training data, synthetic labels, or output terms.

## Native Rust backend

`clusy-native` is built from source as an ABI3 Python 3.12 extension. Its direct
Rust dependencies are:

| Crate | Pinned version/source | License |
|-------|-----------------------|---------|
| clusy-native | 0.1.0 (workspace) | Apache-2.0 |
| dom_query | =0.24.0 | MIT |
| pyo3 | =0.27.2 | MIT OR Apache-2.0 |
| rs-trafilatura (broad backend) | vendored byte-for-byte from crates.io 0.2.2 archive SHA-256 `e7349ec610fed6e49c175da51034aca74c02ec3951a7acbbf10eb2ac3ee04e3b` (packaged VCS revision `a20bb0d58ee17f0315d2e5f7d2a7119560aecd06`) | MIT OR Apache-2.0 |
| rs-trafilatura (article backend) | vendored from exact revision `9261e087deca9c7a38ddc284a60dd62a47de7b33` | MIT OR Apache-2.0 |
| html-cleaning (article backend) | vendored from exact revision `ba5f8e95e4dc8a6af1f6742c8f31957a47b2327e` | MIT OR Apache-2.0 |
| quick_html2md (article backend) | vendored from exact revision `6260677b3aed7bfc83bc7fae90599120467c4c55` | MIT OR Apache-2.0 |

[`native/Cargo.lock`](native/Cargo.lock) also pins the complete transitive
graph. License expressions reported by Cargo metadata include MIT, Apache-2.0,
BSD-3-Clause, MPL-2.0, Unicode-3.0, Unlicense, and Zlib variants. Consult each
crate's packaged license files when preparing binary-distribution notices; this
summary is not a substitute for those files.

## Build-only tools

| Tool | Version | License / role |
|------|---------|----------------|
| Rust toolchain | >=1.85 | Required only to build `clusy-native`; not copied into the final Docker stage |
| maturin | >=1.9,<2.0 (Docker pins 1.14.1) | MIT OR Apache-2.0; builds the Python wheel |

## Deliberately excluded copyleft dependencies

Two libraries were removed to keep the distribution permissively licensed:

- **PyMuPDF / `fitz`** — AGPL-3.0 (or Artifex commercial). Replaced by
  **pypdfium2** (BSD-3-Clause) for PDF text extraction in
  `app/services/academic.py`. AGPL's network-copyleft clause would otherwise
  impose source-disclosure obligations on anyone running the service.
- **html2text** — GPLv3. Replaced by **markdownify** (MIT) for HTML→markdown
  conversion in `app/services/extractor.py`. GPLv3 is incompatible with an
  Apache-2.0 distribution.

If you re-introduce either library in a fork, note that doing so changes the
effective license of your distribution.

## Bundled browser

Playwright downloads its matching Chromium build during local setup
(`playwright install chromium`) or during the Docker image build. The resulting
browser binaries are included in `browser-runtime`, `quality-runtime`, and the
compatibility `runtime` alias. `static-runtime` contains neither Playwright nor
Chromium. Chromium uses the BSD license and contains components under
additional licenses; retain Chromium's bundled third-party notices when
redistributing a browser-capable image.

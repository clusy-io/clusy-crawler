# Third-Party Licenses

Clusy Crawler is released under the Apache License 2.0. It depends only on
third-party packages under permissive licenses (Apache-2.0, MIT, BSD, MPL-2.0,
LGPL). No copyleft (GPL/AGPL) code is bundled or required.

## Runtime dependencies

| Package | Version constraint | License |
|---------|--------------------|---------|
| fastapi | >=0.115,<1.0 | MIT |
| uvicorn[standard] | >=0.45,<1.0 | BSD-3-Clause |
| pydantic | >=2.10,<3.0 | MIT |
| pydantic-settings | >=2.7,<3.0 | MIT |
| httpx | >=0.28,<1.0 | BSD-3-Clause |
| h2 | >=4.3 | MIT |
| brotli | >=1.1 | MIT |
| zstandard | >=0.23 | BSD-3-Clause |
| trafilatura | >=1.8,<2.0 | Apache-2.0 |
| markdownify | >=1.2,<2.0 | MIT |
| beautifulsoup4 | >=4.12,<5.0 | MIT |
| readability-lxml | >=0.8.1,<1.0 | Apache-2.0 |
| pypdfium2 | >=4.30 | BSD-3-Clause / Apache-2.0 (bundles Google PDFium, BSD-3-Clause) |
| playwright | >=1.48,<2.0 | Apache-2.0 |
| lxml | >=5.3,<6.0 | BSD-3-Clause |
| structlog | >=24.4,<26.0 | Apache-2.0 / MIT |
| tenacity | >=9.0,<10.0 | Apache-2.0 |
| aiolimiter | >=1.0,<2.0 | MIT |
| redis | >=5.2,<6.0 | MIT |
| orjson | >=3.10,<4.0 | Apache-2.0 / MIT |

Optional extra `[llm]` (off by default): `anthropic` (MIT).

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

Playwright downloads a Chromium build at install time (`playwright install
chromium`). Chromium is BSD-3-Clause with additional third-party notices; it is
fetched at runtime, not vendored in this repository.

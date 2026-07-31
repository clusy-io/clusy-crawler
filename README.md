<div align="center">

# Clusy Crawler

**Fast, bounded, source-derived web extraction**

[![CI](https://github.com/clusy-io/clusy-crawler/actions/workflows/ci.yml/badge.svg)](https://github.com/clusy-io/clusy-crawler/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![Rust 1.85](https://img.shields.io/badge/Rust-1.85-000000?logo=rust&logoColor=white)](native/Cargo.toml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache--2.0-4C1)](LICENSE)
[![Release: Beta 2 Preview](https://img.shields.io/badge/release-beta_2_preview-F59E0B)](CHANGELOG.md)

An Apache-2.0, self-hosted FastAPI service for turning HTTP(S) resources into
clean Markdown, HTML, links, or schema-constrained JSON.

[Quick start](#quick-start) · [Architecture](#architecture) ·
[API](#api) · [Evidence](#evidence-status) ·
[Self-hosting](docs/SELF_HOSTING.md) · [Research](docs/RESEARCH.md)

</div>

---

Clusy Crawler combines guarded HTTP/2 fetching, conditional Chromium
rendering, a native Rust/PyO3 extraction core, deterministic specialists,
bounded recursive discovery, optional Redis caching, and explicit completeness
provenance. The default extraction path is local and does not require a model.

> **Release status: Beta 2 Preview**
>
> The deterministic extraction and self-hosting paths are available for
> evaluation. Pin a source commit or image digest: API and operational
> compatibility may still change before the first stable release.

> **Claim boundary**
>
> One direct public-repository AEB article-body result is registered as
> Verified. Broader WebMain, WCXB, and live-provider evaluations remain
> outside the claim set. Every measured statement below is registry-bound.

## Quick start

The default Compose stack runs the browser-capable image and a bundled Redis.
It binds the API to loopback only.

```bash
git clone https://github.com/clusy-io/clusy-crawler.git
cd clusy-crawler
cp .env.example .env

docker compose up --build --detach
curl --fail http://127.0.0.1:11235/health/ready
```

Extract one page:

```bash
curl --fail --request POST http://127.0.0.1:11235/crawl \
  --header 'content-type: application/json' \
  --data '{"urls":["https://example.com"],"js_render":false}'
```

`.env.example` uses local mode with no bearer token. Before sharing the
service, set `ENVIRONMENT=prod`, `CRAWL4AI_API_TOKEN`, an independent
`SERVING_FINGERPRINT_KEY`, and an exact `GIT_SHA`. See
[`docs/SELF_HOSTING.md`](docs/SELF_HOSTING.md).

### Build from source

Requires Python 3.12+, Rust 1.85, and
[`uv`](https://docs.astral.sh/uv/).

```bash
uv sync --locked --extra dev
uv run playwright install chromium
uv run uvicorn app.main:app --host 127.0.0.1 --port 11235
```

For a static-only local process, omit the browser install and disable both
Playwright switches:

```bash
PLAYWRIGHT_ENABLED=false \
PLAYWRIGHT_JAVA_SCRIPT_ENABLED=false \
uv run uvicorn app.main:app --host 127.0.0.1 --port 11235
```

### Runtime images

| Docker target | Contains | Use it for |
| --- | --- | --- |
| `static-runtime` | API, native/Python extraction, PDF support; no Playwright or browser | Lowest-footprint deterministic service |
| `browser-runtime` | Static runtime plus Playwright and Chromium | Conditional or explicit JavaScript rendering; Compose default |
| `quality-runtime` | Browser runtime plus the pinned MinerU-HTML client | Operator-configured OpenAI-compatible quality backend |

No model weights are bundled. Selecting `quality-runtime` does not enable a
backend until its endpoint, API key, and model are configured.

## Architecture

<p align="center">
  <img src="docs/pipeline.svg" alt="Clusy Crawler request pipeline" width="100%">
</p>

```text
request
  │
  ├─ authentication + admission budgets
  ├─ URL / DNS / redirect / robots policy
  ├─ static fetch ───── optional Chromium render
  ├─ specialist or native deterministic extraction
  ├─ optional risk-routed quality backend
  ├─ provenance + output budgets
  └─ Markdown / HTML / links / constrained JSON
```

The main design boundaries are:

- **Fetch once, escalate deliberately.** Ordinary pages stay on the static
  path. Rendering is conditional or explicit.
- **Deterministic result first.** Optional quality backends cannot remove the
  known local fallback.
- **Source-derived structure.** Deterministic paths replay source text. A
  configured quality backend can label source-derived item IDs; Clusy
  independently validates the exact raw response, replays that selection, and
  binds both in a versioned receipt before applying grounding and completeness
  checks. Any failure falls back locally.
- **Separate extraction from discovery.** Main-content selection and the crawl
  frontier are independent state machines; indexing and ranking remain outside
  this service.
- **Return provenance.** Results report route, verified source-selection
  identity when applicable, truncation, completeness, cache, render, and
  per-stage timing state.
- **Bound every request dimension.** URL count, depth, pages, bodies, output,
  retries, deadlines, and concurrency are capped.

Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the complete runtime
contract.

## Capabilities

### Fetch and render

- shared asynchronous HTTP/2 connection pool;
- Brotli and Zstandard response decoding;
- manual redirect handling with SSRF validation on every hop;
- private, loopback, link-local, metadata, and other unsafe address rejection;
- bounded retries, body size, deadlines, and concurrency;
- conditional or explicit sandboxed Chromium rendering; and
- source status, redirect, render, and stage-timing provenance.

### Extract

- native Rust/PyO3 main-content extraction;
- confidence-gated Trafilatura, Readability, Markdownify, and documentation
  fallbacks;
- GitHub repository, file, issue, pull request, commit, and diff specialists;
- PDF and academic-paper extraction;
- optional metadata-only Crossref and configured publisher fallbacks; and
- optional schema/prompt JSON extraction.

### Discover

- explicit recursion with `max_depth > 0`;
- deterministic bounded frontier;
- same-site scope with optional subdomains;
- robots enforcement without a request-side bypass;
- host fairness and crawl-trap budgets; and
- deterministic response ordering.

The built-in frontier is process-local. It is not a durable distributed crawl
queue.

## Extraction profiles

| Profile | Contract | Model required |
| --- | --- | --- |
| `balanced` | Default general main-content extraction | No |
| `article_body` | Precision-oriented article-body extraction | No |
| `adaptive` | Deterministic result first; optional quality escalation on bounded label-free risk | No |
| `quality` | Deterministic result first; attempt the configured quality backend | No |

If the optional quality backend is absent, saturated, unavailable, invalid, or
slower than its deadline, the deterministic candidate remains authoritative.
The quality lane is disabled unless its base URL, API key, and model are all
configured. Successful model output is not persisted in Redis unless an
immutable backend revision is supplied and its source-selection receipt was
independently replayed.

## API

Interactive OpenAPI documentation is available at `/docs`; the schema is at
`/openapi.json`.

| Method | Route | Authentication when configured | Purpose |
| --- | --- | --- | --- |
| `GET` | `/health` | Public | Process liveness |
| `GET` | `/health/ready` | Public | Required component readiness |
| `GET` | `/health/version` | Public | Source, image, pipeline, and non-secret configuration identity |
| `POST` | `/crawl` | Bearer | Batch or bounded recursive crawl |
| `POST` | `/md` | Bearer | One URL to Markdown |
| `POST` | `/html` | Bearer | One URL to source or rendered HTML |
| `POST` | `/map` | Bearer | Bounded URL discovery |

Example authenticated request:

```bash
curl --fail --request POST http://127.0.0.1:11235/crawl \
  --header 'authorization: Bearer replace-me' \
  --header 'content-type: application/json' \
  --data '{
    "urls": ["https://example.com/docs"],
    "extraction_profile": "balanced",
    "formats": ["markdown", "links"],
    "max_age": 0,
    "store_in_cache": false,
    "js_render": null,
    "max_depth": 0
  }'
```

Important request limits:

| Field | Limit |
| --- | ---: |
| `urls` | 1–50 |
| URL length | 4,096 characters |
| `max_pages` | 1–100 |
| `max_depth` | 0–10 |
| `formats` | 1–4 unique values |
| HTML output | At most 5 projected pages |
| JSON schema | 100,000 bytes; depth 20 |
| Extraction prompt | 10,000 characters |
| `/map` result limit | 1–5,000 |

`max_depth=0` processes only the supplied URLs. Recursive requests require
`max_pages >= len(urls)`.

Representative response:

```json
{
  "status": "ok",
  "results": [
    {
      "url": "https://example.com/docs",
      "markdown": "# Example\n\nExtracted content.",
      "links": ["https://example.com/docs/next"],
      "metadata": {
        "content_scope": "main_content",
        "extraction_route": "general_html",
        "rendered": false,
        "cache_status": "live",
        "cache_policy": "no_store",
        "cache_read_permitted": false,
        "cache_write_permitted": false,
        "cache_policy_revision": "crawl-cache-policy.v1",
        "completeness_coverage": "source_full"
      },
      "cached": false,
      "error": null
    }
  ],
  "total_pages": 1,
  "service_identity": {
    "schema_version": "crawl-service-identity.v1",
    "revision": "<git-sha>",
    "config_fingerprint": "<64-lowercase-hex>",
    "image_digest": "sha256:<64-lowercase-hex>"
  }
}
```

`max_age=0` bypasses persistent result-cache reads. `store_in_cache=false`
disables persistent result-cache writes. Use both for a live no-store crawl.
The receipt describes only Clusy's persistent crawl-result cache; it is not a
zero-data-retention promise for process memory, network transit, logs, target
sites, browsers, or infrastructure telemetry.

Exact schemas live in
[`app/models/requests.py`](app/models/requests.py) and
[`app/models/responses.py`](app/models/responses.py).

## Configuration

Copy [`.env.example`](.env.example) and change only the controls required by
your runtime.

Required for production mode:

- `ENVIRONMENT=prod`;
- exact `GIT_SHA`;
- non-empty `CRAWL4AI_API_TOKEN`; and
- an independent `SERVING_FINGERPRINT_KEY` with at least 32 characters.

Set `IMAGE_DIGEST` when the platform exposes an immutable OCI digest.

| Optional capability | Main settings |
| --- | --- |
| Redis | `REDIS_URL`, TTL, operation timeout, entry cap |
| Chromium | `PLAYWRIGHT_ENABLED`, `JS_RENDER_MODE`, timeout and HTML cap |
| Quality backend | `QUALITY_EXTRACTION_*`, immutable backend revision |
| Structured JSON | `ANTHROPIC_API_KEY`, model and resource caps |
| Publisher metadata | `ELSEVIER_API_KEY`, `IEEE_API_KEY` |
| Proxy | `HTTP_PROXY`, `PLAYWRIGHT_PROXY` |

All validated defaults are defined in [`app/config.py`](app/config.py).

## Evidence status

> **Verified evidence — Article Extraction Benchmark · `article_body` · 181 pages.** Clusy F1 `0.972127`; exact Trafilatura 2.1.0 F1 `0.957546`; F1 delta `+0.014581`; F1 delta CI95 low `+0.005547`; F1 delta CI95 high `+0.025336`; paired-bootstrap win fraction `0.9996`; machine-local in-memory throughput `173.97 pages/s`. <!-- clusy-evidence: aeb.article-body.trafilatura-2-1.77b8d00-beta2-public.2026-07-31 -->

The Verified receipt was produced directly from a clean public source tree
identical to the `v0.2.0-beta.2` tag tree. Its exact Trafilatura 2.1.0
comparator ran in a separate hash-pinned, label-free environment. The result is
bound to a frozen protocol, compact report, deterministic retained raw archive,
and exact hashes in
[`bench/evidence/registry.json`](bench/evidence/registry.json). See
[`docs/BENCHMARKS.md`](docs/BENCHMARKS.md) for the execution boundary,
limitations, and evidence-status rules. The throughput value is one local
in-memory extraction observation, not a crawler, HTTP-service, stability, or
service-level result. No other benchmark, implementation, deployment, or
vendor result is authorized for publication by this release.

## Development

```bash
uv sync --locked --extra dev

uv run ruff check .
uv run mypy app
uv run pytest -q

cargo +1.85 fmt --manifest-path native/Cargo.toml --check
cargo +1.85 clippy \
  --manifest-path native/Cargo.toml \
  --locked --all-targets -- -D warnings
cargo +1.85 test --manifest-path native/Cargo.toml --locked
```

CI builds and verifies all three container boundaries. Extraction changes
require the relevant fixed-protocol benchmark; native performance changes
require counterbalanced retain-all evidence and output equivalence unless a
quality delta is preregistered.

See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Documentation

| Document | Purpose |
| --- | --- |
| [`docs/README.md`](docs/README.md) | Documentation index and status vocabulary |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Runtime architecture and failure behavior |
| [`docs/SELF_HOSTING.md`](docs/SELF_HOSTING.md) | Compose, containers, production configuration, upgrades |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | Health, observability, release, rollback, incidents |
| [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md) | Results, artifacts, and claim boundaries |
| [`docs/RESEARCH.md`](docs/RESEARCH.md) | Advanced architecture and SOTA gates |
| [`bench/README.md`](bench/README.md) | Benchmark protocol and evidence index |
| [`SECURITY.md`](SECURITY.md) | Threat model and vulnerability reporting |

## License

Clusy Crawler is licensed under
[`Apache-2.0`](LICENSE). Third-party licenses and provenance are recorded in
[`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md) and
[`native/vendor/NOTICE.md`](native/vendor/NOTICE.md).

# Clusy Crawler — Fast, Permissive Web Extraction Service

A small, self-hosted FastAPI service that turns any URL into clean, LLM-ready
markdown. Async I/O with HTTP/2 pooling, multi-strategy content extraction,
conditional JS rendering via Playwright, and a PDF/academic pipeline — with
**no LLM required** for extraction and **no copyleft dependencies**.

Apache-2.0. Runs anywhere Docker or Python 3.12 runs.

```bash
docker compose up -d --build
curl -X POST localhost:11235/crawl -H 'content-type: application/json' \
  -d '{"urls":["https://example.com"]}'
```

## Architecture

A single stateless FastAPI service. Point any HTTP client at it and get clean
markdown back; add Redis for response caching (optional).

```
   your app ──HTTP──▶  ┌──────────────┐
                       │   crawler    │  FastAPI (:11235)
                       │  ┌────────┐  │  ├─ httpx (HTTP/2) fetch
                       │  │ redis? │  │  ├─ Playwright/Chromium (conditional JS)
                       │  └────────┘  │  └─ trafilatura + readability + pypdfium2
                       └──────────────┘
```

Runs anywhere Docker or Python 3.12 runs. No external services required (Redis
is optional; the LLM structured-extraction format is an optional extra).

## Crawl Pipeline

<p align="center">
  <img src="docs/pipeline.svg" alt="Crawl pipeline: URL in, guard, fetch, conditional JS render, extract, markdown out" width="100%">
</p>

Each request flows through six stages:

1. **URL in** — validate the scheme and normalize the URL.
2. **Guard** — the SSRF check resolves the host and re-checks every redirect hop,
   refusing any that resolve to a private/loopback/link-local/metadata address.
3. **Fetch** — `httpx` over HTTP/2, with a size cap on the decompressed body.
   A Redis cache (optional) short-circuits repeat fetches.
4. **Render?** — *conditional*. The static HTML is fetched first; the browser
   (Playwright/Chromium) is used only when the page is a bot wall, a JS shell,
   or too sparse to have real content. Most fetches never touch the browser.
5. **Extract** — several extractors run in parallel (trafilatura, readability,
   markdownify, plus targeted extractors for docs and GitHub READMEs) and are
   merged into the cleanest result. PDFs go through a structured academic parser.
6. **Markdown** — clean, LLM-ready markdown out (optionally `html`, `links`, or
   schema-constrained JSON).

## Extraction Quality

Measured on a **neutral third-party benchmark** — Zyte's
[article-extraction-benchmark](https://github.com/scrapinghub/article-extraction-benchmark)
(181 news/article pages, token-shingle F1 on the article body, ground truth and
metric authored by someone else). Our real `extract_content` pipeline scored
against 30+ open-source libraries and commercial services:

| Extractor | F1 | Type |
|-----------|:--:|------|
| AutoExtract (Zyte) | 0.970 | commercial |
| **clusy-crawler** | **0.960** | **open-source (this)** |
| trafilatura 2.0 | 0.958 | open-source |
| Diffbot | 0.951 | commercial |
| newspaper4k | 0.949 | open-source |
| readability_js | 0.947 | open-source |
| readability-lxml | 0.922 | open-source |
| goose3 | 0.896 | open-source |
| justext | 0.804 | open-source |

**F1 0.960 ± 0.007** (precision 0.955, recall 0.965). We edge **trafilatura 2.0**
(0.958) and beat **Diffbot** (a commercial service) plus every other open-source
library — behind only Zyte's commercial AutoExtract (0.970). Honest caveat: the
lead over trafilatura is a point-estimate edge (paired bootstrap: +0.003, ~69%
confidence), not a statistically decisive win — trafilatura 2.0 is excellent and
we build on it. The gain holds on a **held-out test half** and does **not**
regress our diverse real-world mix (docs / papers / reference / data pages),
which a blanket precision config would. See
[`bench/NEUTRAL_BENCHMARK.md`](bench/NEUTRAL_BENCHMARK.md) to reproduce.

Only the neutral F1 above is a headline claim. `bench/benchmark_*.py` are
internal regression harnesses that use our own metric — useful for catching
quality regressions across changes, not for cross-tool comparison.

**Not covered:** bot-walled sites (Cloudflare/DataDome — e.g. Reuters, ACM) are
out of scope. Defeating them needs residential proxies and TLS impersonation, a
different and legally fraught class of tool.

### What the engine does well

- **No LLM required** — deterministic, cheap, private extraction. Nothing is
  sent to a third-party model to produce clean markdown.
- **Fast** — async I/O, HTTP/2 connection pooling, conditional (not default)
  JS rendering, and aggressive Redis caching keep live-fetch latency low.
- **Academic / PDF-aware** — arXiv HTML→PDF fallback and structured paper
  parsing (title, authors, abstract, sections, references).
- **Brotli/zstd correct** — bundles the `brotli`/`zstandard` decoders so
  Cloudflare-fronted sites (which default to `content-encoding: br`) decode
  correctly instead of returning binary garbage.

### Known limitations

- **Bot walls** (Cloudflare/DataDome, e.g. ACM, some news sites) are **out of
  scope** — defeating them needs managed residential proxies and TLS/JA3
  impersonation, a different class of (and legally fraught) solution.
- **Wikipedia-style dense tables** and heavy repo chrome are areas where
  dedicated commercial extractors still do better.

## Features

### Extraction Engine

- **Parallel multi-strategy extraction** — Trafilatura, Readability, markdownify,
  and specialized extractors run concurrently via `run_in_executor`, then
  union-merged
- **Page-type awareness** — auto-detects 7 types: article, documentation,
  academic, repository, forum, product, listing — and configures extractors
  per-type (e.g., `favor_precision` for articles, `favor_recall` for forums)
- **Documentation extractor** — targets Sphinx, MkDocs, and Docusaurus
  `.rst-content` / `.md-content` / `.markdown-body`, strips nav sidebars,
  preserves code blocks, API signatures (dt/dd), and heading hierarchy
- **GitHub README extractor** — targets `article.markdown-body`, strips
  GitHub chrome (headers, sidebars, signup prompts, file navigation)
- **Academic pipeline** — pypdfium2 for PDF extraction, structured parsing for
  title, authors, abstract, sections, references. Auto arXiv PDF fallback
  when HTML abstract page is detected
- **Table → Markdown conversion** — post-extraction HTML table detection and
  conversion to markdown table format
- **Code block injection** — detects code blocks stripped by trafilatura and
  re-injects them from the original HTML

### Performance

- **HTTP/2 multiplexing** — multiple concurrent streams over single TCP
  connections (via `httpx[http2]`)
- **Full content-encoding support** — `gzip`/`deflate`/`br`/`zstd` decoders
  bundled; `Accept-Encoding` is negotiated by httpx to exactly what it can
  decode (advertising `br` without the decoder returns undecodable bytes)
- **Keep-alive connection pooling** — 30s `keepalive_expiry` reuses warm
  TCP+TLS across requests to a domain (inline pre-warming was removed — it
  added a serial HEAD round-trip *before* every fetch instead of saving one)
- **Single-pass extraction** — the conditional-JS path extracts once and
  re-extracts only when it actually escalates (was extracting twice per page)
- **Non-blocking pipeline** — PDF (pypdfium2), academic-HTML, and multi-strategy
  extraction run in executors, off the event loop
- **DNS caching** — 300s TTL, async resolution via `loop.getaddrinfo`
- **Configurable concurrency** — `asyncio.Semaphore`-gated in-flight
  operations (default 5), bounded Playwright context-reuse pool (default 2)
- **Resource blocking** — images, fonts, media, stylesheets blocked
  during Playwright renders (30-50% speedup)

### JS Rendering

- **Stealth Playwright** — WebGL vendor spoofing, canvas fingerprint
  randomization, `navigator.webdriver` override, Chrome runtime faking,
  realistic viewport and user agent rotation
- **Smart escalation** — only launches browser when needed: bot-detection
  walls, SPA shells, sparse content (<200 chars visible text)
- **Static site exclusion** — known static sites (Python docs, Wikipedia,
  Rust Book, arXiv, PubMed, StackOverflow, MDN) skip JS entirely
- **Force-JS domains** — ACM, Springer, IEEE, Nature, Medium, Substack
  always rendered with JS (known bot walls / SPAs)
- **Settle detection** — waits up to 2s for dynamic content on sparse
  pages, then extracts whatever is available

### Safety & Operations

- **SSRF protection** — blocks private IPs (10.x, 172.16-31.x, 192.168.x,
  127.x, 169.254.x), IPv6 localhost, link-local at DNS resolution layer
- **Per-domain rate limiting** — token bucket via `aiolimiter`, 2 req/s
  default with configurable burst. Per-domain locks prevent contention
- **Optional Redis cache** — URL + options keyed via SHA-256, configurable
  TTL, degrades gracefully to no-op when Redis is unavailable
- **Bearer token auth** — constant-time comparison via `secrets.compare_digest`;
  set `CRAWL4AI_API_TOKEN` to require it on every endpoint except `/health`
- **Familiar endpoints** — `/crawl`, `/md`, `/html`, `/map`, `/health`, with
  request/response shapes that map cleanly onto common scrape APIs

### Familiar API surface

Endpoints and options modelled on the common scrape-API shape, so it's easy to
adopt if you already use a hosted crawler:

- **Multi-format output** — request any of `markdown` (always), `html` (rendered
  source), and `links` (deduped absolute links) per crawl via `formats`
- **`max_age` cache control** — `null` serves any cached entry within TTL, `0`
  bypasses the cache (always re-crawl), `N` serves cached only if younger than
  `N` seconds. Mirrors Firecrawl `maxAge` / Exa `maxAgeHours`
- **Structured (JSON) extraction** — pass a `json_schema` and/or
  `extraction_prompt` with the `json` format to get schema-constrained JSON
  back (LLM-backed, schema-validated). Optional: requires `ANTHROPIC_API_KEY`,
  configurable model (`EXTRACTION_MODEL`, default `claude-haiku-4-5`); degrades
  to a clear error when unconfigured, markdown crawl unaffected
- **`POST /map`** — site URL discovery via robots.txt → sitemap(s) +
  homepage links, same-domain filtered, optional `search` substring filter

## API Reference

### `GET /health`
Returns `{"status": "ok"}`. Unauthenticated.

### `GET /health/ready`
Readiness probe. Checks HTTP client connectivity, Playwright browser pool,
and Redis (if configured). Returns `{"status": "ready", "checks": {...}}`
or `{"status": "degraded", ...}` with per-check status.

### `GET /health/version`
Returns build info: `{"sha": "...", "environment": "dev", "python_version": "...", "trafilatura_version": "...", "playwright_version": "..."}`

### `POST /crawl`
Full crawl with markdown extraction and metadata.

**Request:**
```json
{
  "urls": ["https://example.com"],
  "js_render": false,
  "word_count_threshold": 10,
  "wait_for_selector": null,
  "formats": ["markdown", "html", "links", "json"],
  "max_age": 3600,
  "json_schema": {
    "type": "object",
    "properties": {"title": {"type": "string"}, "summary": {"type": "string"}}
  },
  "extraction_prompt": "Extract the page title and a one-line summary."
}
```

`formats` defaults to `["markdown"]`. `html`/`links` are populated only when
requested; `json` runs schema-constrained LLM extraction into `extracted`
(needs `ANTHROPIC_API_KEY`). `max_age` is the cache-freshness bar in seconds
(`null` = any cached entry within TTL, `0` = always re-crawl).

**Response:**
```json
{
  "status": "ok",
  "results": [{
    "url": "https://example.com",
    "markdown": "# Title\n\nContent...",
    "html": null,
    "links": null,
    "extracted": {"title": "Example Domain", "summary": "..."},
    "cached": false,
    "metadata": {
      "title": "Example Domain",
      "description": "...",
      "language": "en",
      "source_url": "https://example.com",
      "content_type": "text/html",
      "status_code": 200,
      "word_count": 350,
      "rendered": false,
      "extraction_strategy": "union(trafilatura+readability+markdownify)"
    },
    "error": null
  }],
  "total_time_ms": 453,
  "total_pages": 1
}
```

### `POST /md`
Markdown-only extraction — a lightweight alias for `/crawl` with `formats:
["markdown"]`.

**Request:**
```json
{
  "url": "https://example.com",
  "word_count_threshold": 10,
  "options": {
    "js_render": false,
    "only_main_content": true
  }
}
```

**Response:**
```json
{
  "status": "ok",
  "markdown": "# Title\n\nContent...",
  "metadata": { ... }
}
```

### `POST /html`
Raw HTML extraction.

**Request:**
```json
{ "url": "https://example.com", "js_render": false }
```

**Response:**
```json
{ "status": "ok", "html": "<!doctype html>...", "metadata": { ... } }
```

### `POST /map`
Discover a site's URLs (no rendering, no extraction) via robots.txt → sitemap(s),
supplemented with same-domain homepage links.

**Request:**
```json
{ "url": "https://docs.python.org/3/", "limit": 1000, "search": "asyncio" }
```

**Response:**
```json
{ "status": "ok", "url": "https://docs.python.org/3/", "links": ["https://..."], "count": 42 }
```

## Configuration

All settings via environment variables (`.env` file or process environment):

### Service

| Variable | Default | Description |
|----------|---------|-------------|
| `CRAWL4AI_API_TOKEN` | (empty) | Bearer token. Empty = unauthenticated — only safe on a trusted private network. |
| `CORS_ALLOW_ORIGINS` | (empty) | Comma-separated browser origins. Empty = CORS off. `*` discouraged. |
| `MAX_CONCURRENT_TASKS` | `5` | Global in-flight crawl operations cap. |
| `MAX_CONCURRENT_PAGES` | `2` | Max concurrent Playwright browser pages (500MB-1GB each). |
| `JS_RENDER_MODE` | `conditional` | `conditional` (auto-detect), `force` (always), `never` (no JS). |

### HTTP Client

| Variable | Default | Description |
|----------|---------|-------------|
| `HTTP_TIMEOUT_S` | `30` | Read timeout for HTTP requests. |
| `HTTP_CONNECT_TIMEOUT_S` | `5` | TCP/TLS connection timeout. |
| `HTTP_MAX_KEEPALIVE_CONNECTIONS` | `50` | Idle connections kept alive per host. |
| `HTTP_MAX_CONNECTIONS` | `100` | Total connection pool size. |
| `HTTP_USER_AGENT` | `ClusyCrawler/1.0` | User-Agent header. |
| `ADAPTIVE_TIMEOUT_ENABLED` | `true` | Learn per-domain optimal timeouts. |
| `CONNECTION_WARMING_ENABLED` | `true` | Pre-warm TCP+TLS to new domains. |

### Extraction

| Variable | Default | Description |
|----------|---------|-------------|
| `PARALLEL_EXTRACTION_ENABLED` | `true` | Run strategies concurrently. |
| `EXTRACTION_MERGE_MODE` | `union` | `union` (multi-strategy merge), `best`, `longest`. |
| `EXTRACT_MAX_TEXT_LENGTH` | `500000` | Max characters in extracted text. |
| `EXTRACT_MIN_TEXT_LENGTH` | `50` | Minimum words to consider extraction valid. |

### Playwright / JS Rendering

| Variable | Default | Description |
|----------|---------|-------------|
| `PLAYWRIGHT_ENABLED` | `true` | Enable browser-based JS rendering. |
| `PLAYWRIGHT_TIMEOUT_S` | `25` | Max time for Playwright page load. |

### Rate Limiting

| Variable | Default | Description |
|----------|---------|-------------|
| `RATE_LIMIT_REQUESTS_PER_SECOND` | `2.0` | Per-domain token bucket fill rate. |
| `RATE_LIMIT_BURST` | `5` | Maximum burst before throttling. |

### Cache

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | (empty) | Redis connection string. Empty = no caching. |
| `CACHE_TTL_S` | `3600` | Cache entry TTL in seconds. |

### Structured extraction (optional, `json` format)

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | (empty) | Enables `json` extraction. Empty = feature disabled. |
| `EXTRACTION_MODEL` | `claude-haiku-4-5` | Model for extraction. Set to `claude-opus-4-8` for higher quality. |
| `EXTRACTION_MAX_TOKENS` | `8192` | Max output tokens per extraction. |
| `EXTRACTION_MAX_INPUT_CHARS` | `100000` | Page content truncation before extraction. |

Requires the `llm` extra: `pip install -e ".[llm]"`.

## Project Structure

```
crawler/
├── app/
│   ├── main.py                    # FastAPI app, lifespan, middleware, routers
│   ├── config.py                  # pydantic-settings (all env vars)
│   ├── models/
│   │   ├── requests.py            # Pydantic request models
│   │   └── responses.py           # Pydantic response models
│   ├── routers/
│   │   ├── health.py              # /health, /health/ready, /health/version
│   │   ├── crawl.py               # POST /crawl
│   │   ├── extract.py             # POST /md, POST /html
│   │   └── map.py                 # POST /map (URL discovery)
│   ├── services/
│   │   ├── crawler.py             # Pipeline orchestrator
│   │   ├── fetcher.py             # httpx GET + SSRF guard + JS routing
│   │   ├── extractor.py           # Multi-strategy extraction engine
│   │   ├── renderer.py            # Playwright context-reuse pool + stealth
│   │   ├── rate_limiter.py        # Per-domain token bucket
│   │   ├── academic.py            # PDF + structured academic paper parser
│   │   ├── site_map.py            # Sitemap + homepage link discovery (/map)
│   │   └── structured.py          # Optional LLM JSON extraction (json format)
│   ├── cache/
│   │   └── __init__.py            # Optional Redis cache (timestamped, max_age)
│   ├── middleware/
│   │   └── auth.py                # Bearer token middleware
│   └── lib/
│       ├── http_client.py         # Singleton httpx client + DNS cache
│       └── logging.py             # structlog JSON config
├── tests/
│   ├── conftest.py                # Fixtures, mock cache/rate limiter
│   ├── unit/
│   │   ├── test_extractor.py      # 18 tests: strategies, fallback chain, word count
│   │   ├── test_fetcher.py        # 10 tests: SSRF guard, content types, errors
│   │   ├── test_crawler.py        # 4 tests: orchestration, semaphore, errors
│   │   ├── test_cache.py          # 4 tests: key generation, no-op behavior
│   │   └── test_rate_limiter.py   # 4 tests: isolation, eviction, domain extraction
│   ├── integration/
│   │   ├── test_health.py         # 3 tests: health/ready/version endpoints
│   │   ├── test_crawl.py          # 4 tests: crawl endpoint, auth, validation
│   │   ├── test_md.py             # 6 tests: md/html endpoints
│   │   └── test_auth.py           # 5 tests: auth bypass, required, valid, invalid
│   └── load/
│       └── test_parallelism.py    # 2 tests: semaphore caps, concurrent speed
├── bench/                         # Benchmark harnesses
│   ├── neutral_benchmark.py       # Score vs Zyte's neutral corpus (the credible one)
│   ├── NEUTRAL_BENCHMARK.md       # How to reproduce + results
│   └── benchmark_*.py             # Internal regression harnesses (own metric)
├── Dockerfile                     # FastAPI + Playwright/Chromium runtime
├── docker-compose.yml             # Service definition (crawler + Redis)
├── pyproject.toml                 # Dependencies, ruff, mypy, pytest config
├── .env.example                   # Environment configuration template
└── README.md
```

## Development

```bash
# Install (uv — recommended)
uv sync --extra dev
uv run playwright install chromium

# Or with pip
pip install -e ".[dev]"
python -m playwright install chromium

# Run locally
uv run uvicorn app.main:app --reload --port 11235

# Run tests (works on a fresh checkout — pytest's pythonpath is configured)
uv run --extra dev pytest tests/ -v

# Lint + type-check
uv run --extra dev ruff check .
uv run --extra dev mypy app

# Run tests with coverage
uv run --extra dev pytest tests/ -v --cov=app --cov-report=term-missing

# Lint
ruff check app/ tests/
```

## Deploy

### Docker Compose (recommended)

```bash
cp .env.example .env          # set CRAWL4AI_API_TOKEN for anything public
docker compose up -d --build  # starts crawler (:11235) + Redis cache
curl -sf http://localhost:11235/health   # {"status":"ok"}

curl -sf -X POST http://localhost:11235/crawl \
  -H 'Content-Type: application/json' \
  -d '{"urls":["https://example.com"]}'
```

### Bare Docker

```bash
docker build -t clusy-crawler .
docker run -p 11235:11235 --shm-size=1g \
  -e CRAWL4AI_API_TOKEN=your-token clusy-crawler
```

### Operational notes

- **Memory**: give the container ~2–4 GB. Playwright/Chromium is the heavy part;
  if you hit OOMs, lower `MAX_CONCURRENT_TASKS` / `MAX_CONCURRENT_PAGES`.
- **`/dev/shm`**: Chromium needs ~1 GB (`--shm-size=1g` / `shm_size` in compose).
- **Image size**: ~3 GB (Playwright + Chromium bundled). Set
  `PLAYWRIGHT_ENABLED=false` if you don't need JS rendering — much smaller runtime.
- **Auth**: with no `CRAWL4AI_API_TOKEN`, every endpoint except `/health` is
  unauthenticated. Set a token before exposing the service. See
  [`SECURITY.md`](SECURITY.md).
- **Logs**: `docker compose logs -f crawler` (structured JSON via structlog).

## Key Dependencies

| Library | Version | Purpose |
|---------|---------|---------|
| `fastapi` + `uvicorn` | 0.115+ / 0.45+ | HTTP server |
| `httpx[http2]` | 0.28+ | Async HTTP with HTTP/2 |
| `brotli` + `zstandard` | 1.1+ / 0.23+ | Content-encoding decoders (required for Brotli/zstd sites) |
| `trafilatura` | 1.8+ | Primary content extraction (Apache-2.0) |
| `playwright` | 1.48+ | JS rendering via Chromium |
| `pypdfium2` | 4.30+ | PDF extraction (BSD; replaced AGPL PyMuPDF) |
| `readability-lxml` | 0.8+ | Mozilla Readability fallback |
| `markdownify` | 1.2+ | HTML → Markdown conversion (MIT; replaced GPLv3 html2text) |
| `aiolimiter` | 0.1+ | Per-domain rate limiting |
| `tenacity` | 9.0+ | Retry with exponential backoff |
| `structlog` | 24.4+ | Structured JSON logging |
| `redis` | 5.2+ | Optional response caching |
| `orjson` | 3.10+ | Fast JSON serialization |
| `anthropic` (extra: `llm`) | 0.69+ | Optional structured (JSON) extraction |

## Acknowledgements

Inspired by [crawl4ai](https://github.com/unclecode/crawl4ai), and built on the
excellent [trafilatura](https://github.com/adbar/trafilatura),
[readability-lxml](https://github.com/buriy/python-readability), and
[Playwright](https://github.com/microsoft/playwright). The neutral evaluation
uses Zyte's [article-extraction-benchmark](https://github.com/scrapinghub/article-extraction-benchmark).

## License

Apache License 2.0 — see [LICENSE](LICENSE). Third-party components and their
licenses are listed in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md); all
bundled dependencies are permissively licensed (no GPL/AGPL).

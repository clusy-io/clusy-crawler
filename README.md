# Clusy Crawler — Fast, Source-Grounded Web Extraction Service

A small, self-hosted FastAPI service that turns any URL into clean, LLM-ready
Markdown. V2 combines async HTTP/2 fetching, bounded recursive crawling,
conditional Playwright rendering, native Rust extraction, explicit routing and
completeness provenance, and a PDF/academic pipeline. The default extraction
path is deterministic and requires no LLM. Optional model-assisted profiles are
fail-closed and never replace the deterministic fallback.

Apache-2.0. Use the Docker image, or build from source with Python 3.12 and
Rust 1.85+.

```bash
docker build --build-arg GIT_SHA="$(git rev-parse HEAD)" -t clusy-crawler .
docker run --rm -d --name clusy-crawler -p 11235:11235 \
  --user 10001:10001 --init --read-only --shm-size=1g \
  --tmpfs /tmp:size=512m,mode=1777 \
  --tmpfs /home/crawler:size=64m,mode=0700,uid=10001,gid=10001 \
  --security-opt seccomp="$(pwd)/seccomp_profile.json" \
  --pids-limit=256 --memory=4g --cpus=2 \
  -e ENVIRONMENT=prod \
  -e SERVING_FINGERPRINT_KEY=your-independent-32-plus-char-secret \
  -e CRAWL4AI_API_TOKEN=your-token clusy-crawler
curl -X POST localhost:11235/crawl \
  -H 'Authorization: Bearer your-token' -H 'content-type: application/json' \
  -d '{"urls":["https://example.com"]}'
```

## Architecture

A single FastAPI service with no required durable state. Point any HTTP client
at it and get clean Markdown back; add Redis for versioned response caching
(optional).

```
   your app ──HTTP──▶  ┌──────────────┐
                       │   crawler    │  FastAPI (:11235)
                       │  ┌────────┐  │  ├─ httpx (HTTP/2) fetch
                       │  │ redis? │  │  ├─ Playwright/Chromium (conditional JS)
                       │  └────────┘  │  └─ Rust extractor + Python/PDF fallbacks
                       └──────────────┘
```

Ordinary deterministic HTML/PDF extraction requires no external service.
Redis, schema-constrained JSON extraction, and the model-assisted main-content
lane are independent optional features. Recognized scholarly URLs may use
Crossref or an explicitly configured publisher API as a metadata-only fallback.

## Crawl Pipeline

<p align="center">
  <img src="docs/pipeline.svg" alt="Crawl pipeline: URL in, guard, fetch, conditional JS render, extract, markdown out" width="100%">
</p>

Each request flows through six stages:

1. **URL in** — validate request shape, scheme, and crawl options.
2. **Guard** — the SSRF check resolves the host and re-checks every redirect hop,
   refusing any that resolve to a private/loopback/link-local/metadata address.
3. **Fetch** — `httpx` over HTTP/2, with a size cap on the decompressed body.
   A Redis v10 cache (optional) short-circuits repeat fetches only when the
   request and output-affecting runtime semantics match.
4. **Render?** — *conditional*. Most automatic requests fetch static HTML first
   and escalate on a bot wall, sparse JS shell, or empty extraction; known
   dynamic domains are pre-routed to Chromium.
   When Playwright and JavaScript are enabled, explicit/forced non-PDF JS
   requests go directly to Chromium and avoid a redundant static fetch;
   otherwise the bounded static path remains authoritative.
5. **Extract** — general HTML uses the native Rust/PyO3 backend first, with
   confidence-gated Python fallbacks (Trafilatura, Readability, markdownify,
   and documentation extraction). GitHub/source/PDF/academic specialists may
   run before or bypass that generic cascade. `adaptive` may escalate a risky
   deterministic candidate to an operator-configured quality endpoint.
6. **Markdown** — clean, LLM-ready markdown out (optionally `html`, `links`, or
   schema-constrained JSON). General-HTML results include route, candidate,
   completeness, cache, and per-stage timing provenance; successful specialist
   paths use conservative `output_only` completeness plus
   `source_completeness_unassessed` when source coverage was not scored.

## Benchmark Evidence

The checked-in harnesses call the production extraction entry points and pin
the corpus, evaluator, dependencies, source tree, native module, raw
predictions, and artifact hashes. Article-body and general main-content
Markdown remain separate contracts; no benchmark labels enter runtime routing
and no scoring-only cleanup is applied.

### Published pre-V2 release evidence

The full AEB corpus was run from clean public commit
[`c3ae00d`](https://github.com/clusy-io/clusy-crawler/commit/c3ae00d90b19003b7c635af5dec87ba177abbd85)
through the production asynchronous entry point:

| Suite / profile | Pages | Precision | Recall | F1 | Extraction throughput |
|---|---:|---:|---:|---:|---:|
| AEB `article_body` | 181 | `0.951014` | `0.989665` | **`0.969955`** | **133.3 pages/s** |

This exactly matches the embedded, pinned `rs-trafilatura` article backend on
AEB's aggregate metrics. Against Trafilatura 2.0, ΔF1 was `+0.012452`, with a
paired-bootstrap 95% CI of `[+0.002093, +0.023745]` and
`P(Clusy > Trafilatura) = 0.9892`. It is a narrow,
**SOTA-matching article-body result**, not an independent algorithmic win:
Clusy intentionally embeds that pinned Rust backend.

The clean run used all 181 pages, two workers, five warmup pages, and 10,000
paired-bootstrap replicates. It completed with zero extraction errors, p50
12.97 ms, p95 30.59 ms, and 246,333,440 bytes peak RSS. The harness marked it
claimable only within the AEB article-body extraction scope. This result
predates V2 and is not evidence for V2's broader `balanced` profile. Throughput
is an in-memory extractor measurement on an Apple M4 Pro, not a live-network
crawl result or a portable hardware guarantee.

### Clean V2 fixed-corpus evidence — private execution, OSS source audit

The following results were executed from clean private revision `10ff0c1`,
with WebMainBench's primary clean repeat from documentation-only child
`7f202f4`. They were not executed from OSS commit `837ddda`.

A post-run SHA-256 audit found the benchmark drivers, core extraction
implementation, vendored native algorithm sources, Cargo lockfiles, and
`uv.lock` byte-identical to OSS parent `837ddda`. Of each AEB, WCXB, and
WebMainBench sealed 49-file source set, 44 files were exact. Five files differed:
two comment-only files, two OSS package/lint metadata files, and one config file
with comment wording plus an adaptive page-type validator entry. This branch
ports that only executable delta (`service` pages); it was not exercised by
these fixed `article_body` or `balanced` profiles. Webis additionally
inventories generated native build files absent from public Git. This is
source-audited evidence for the exercised extraction paths, not a benchmark run
of the OSS commit or proof of universal, live-crawler, or vendor-comparative
SOTA.

| Suite / profile | Pages | Quality | Extraction throughput | Honest comparison |
|---|---:|---:|---:|---|
| AEB `article_body` | 181 | P/R/F1 `0.951014 / 0.989665 / 0.969955` | `157.205` pages/s | Matches the embedded pinned `rs-trafilatura` aggregate; not an independent algorithmic win |
| WCXB `balanced`, development | 1,497 | P/R/F1 `0.852732 / 0.898934 / 0.848433` | `90.65` pages/s | Public-label development evidence, not a blind test |
| WCXB `balanced`, public test | 511 | P/R/F1 `0.894822 / 0.928969 / 0.891727` | `139.86` pages/s | Below the published `rs-trafilatura` public-test F1 `0.903` |
| WCXB `balanced`, combined | 2,008 | P/R/F1 `0.863443 / 0.906577 / 0.859450` | `99.5618` pages/s | Sequential split aggregate; not comparable with a development-only headline |
| Webis `balanced` | 3,985 | macro ROUGE-LSum P/R/F1 `0.867148 / 0.908456 / 0.854920`; macro Levenshtein `0.849806` | `274.8155` pages/s | Below pinned Trafilatura `0.883461` and weighted ensemble `0.898844` ROUGE-LSum F1 |
| WebMainBench `balanced`, raw Direct-MD | 7,809 | macro ROUGE-5 P/R/F1 `0.615569 / 0.677841 / 0.606672` | `127.4899` pages/s | Below published Trafilatura `0.6402` and leading model-assisted `0.9098`; output contracts also differ |
| WebMainBench `balanced`, scrubbed Direct-MD | 7,809 | macro ROUGE-5 P/R/F1 `0.615698 / 0.676570 / 0.605703` | `57.8572` pages/s | Annotation markers removed before extraction; paired delta versus raw was `-0.000969` |

| Suite | Clean private source | `manifest.json` SHA-256 |
|---|---|---|
| AEB | `10ff0c1` | `bcd55239efd34908c300aa062ea5272d1cb62f4c78207eb6ab562f5cdaa381da` |
| WCXB | `10ff0c1` | `a051965f480fd3a1cae780d7ccdd289b237163f7e644ba2dde9a9acc53e4b8d0` |
| Webis | `10ff0c1` | `b43a79097a1a04ae3b2254cf25a7ecda9fe2b05be42304e9f79b25638f5ab51e` |
| WebMainBench primary clean repeat | `7f202f4` | `df0deb7b3a2565b164cd0fff8f6687fd5d10173d402156425f9d3e07c6bb35d3` |

All four suites completed with zero extraction errors; Webis also had zero
empty predictions. WCXB's combined score must not be compared with a
development-only leaderboard headline. WebMainBench covers 7,809 pages from
5,434 domains; its repeated page outputs reproduce the original clean run
exactly after excluding per-request latency. Clusy's Direct-MD output differs
from the leaderboard's main `HTML+MD` conversion contract, so this is
same-data evidence rather than an unconditional leaderboard placement.
Throughput is local, closed-loop extraction on artifact-recorded hardware, not
HTTP service or live-web crawl throughput.

The source-backed v2 refiner has also been evaluated in a 545-page,
reference-isolated shadow run:

| Shadow policy | Overall | Text | Code | Formula | Table edit | Table TEDS |
|---|---:|---:|---:|---:|---:|---:|
| Baseline | `0.215484` | `0.758002` | `0.019236` | `0.300180` | `0` | `0` |
| Default refiner | `0.280215` | `0.749641` | `0.084351` | `0.290650` | `0.110996` | `0.165437` |
| Exact visible sequence | `0.227423` | `0.755028` | `0.054644` | `0.293024` | `0.011982` | `0.022439` |

The refiner improved aggregate, code, and table scores but regressed text and
formula scores under both policies. It therefore failed the monotonic
promotion gate and remains **shadow-only and unwired**. This diagnostic is not
a production or leaderboard result. It combines two dirty development
artifacts and remains non-claimable: the default/current manifest SHA-256 is
`c97ef0be89740dbc419534eff12089e25793e0a287eb2faf0d6101225bbf951f`,
and the exact-visible-sequence manifest SHA-256 is
`bc9fc7b71e1a7e3bb6ba02cf6b2c7e216019e12e636157a42b687369af271e80`.

See [`bench/NEUTRAL_BENCHMARK.md`](bench/NEUTRAL_BENCHMARK.md),
[`bench/WCXB_BENCHMARK.md`](bench/WCXB_BENCHMARK.md),
[`bench/WEBIS_BENCHMARK.md`](bench/WEBIS_BENCHMARK.md), and
[`bench/WEBMAINBENCH_BENCHMARK.md`](bench/WEBMAINBENCH_BENCHMARK.md), plus the
separate
[`bench/WEBMAINBENCH_FINEGRAINED_BENCHMARK.md`](bench/WEBMAINBENCH_FINEGRAINED_BENCHMARK.md),
for protocol and reproduction rules. The architecture and promotion gates are
in [`docs/SOTA_ARCHITECTURE.md`](docs/SOTA_ARCHITECTURE.md).

### Exa and Firecrawl comparison policy

Exa and Firecrawl are used only for authorized, paid benchmark execution.
Provider outputs are not used for training, distillation, routing calibration,
or label generation; references are captured independently. The sealed v3
runner processes provider text in memory and persists only hashes, derived
scores, counts, latency/cost/cache evidence, and redacted provenance.

The current public vendor-win gate is intentionally closed. Exa's documented
warm-cache request cannot use the same full-content controls as its cold
request, Firecrawl's documented single-scrape response does not provide the
contractual cache/cost evidence required by the harness, retained hashes cannot
be independently rescored, and no trusted execution-attestation verifier is
implemented. A passing unit test or a favorable pilot therefore cannot support
“Clusy beats Exa/Firecrawl.” Exact scope, budgets, minimum sample sizes,
latency/cost gates, and refusal paths are in
[`bench/LIVE_VENDOR_BENCHMARK.md`](bench/LIVE_VENDOR_BENCHMARK.md).

These suites measure extraction from frozen HTML. They do not measure live
network fetches, discovery completeness, robots compliance, JavaScript
rendering, or distributed crawl recovery.

**Not covered:** bypassing bot walls (Cloudflare/DataDome — e.g. Reuters, ACM)
is out of scope. For recognized ACM/DOI, ScienceDirect PII, and IEEE document
URLs, a failed or obviously sparse publisher page can fall back to official
metadata APIs. That path is explicitly labelled `academic-metadata-*` and never
claims that bibliographic metadata or an abstract is article full text.

### What the engine does well

- **No LLM required by default** — main-content extraction for `balanced` and
  `article_body` is deterministic. Page content reaches a model only when the
  caller explicitly requests configured JSON structured extraction, or when an
  operator-configured `adaptive` / `quality` main-content lane is selected.
- **Fast** — async I/O, HTTP/2 connection pooling, conditional (not default)
  JS rendering, and semantics-bound Redis caching keep live-fetch latency low.
- **Academic / PDF-aware** — arXiv HTML→PDF fallback and structured paper
  parsing (title, authors, abstract, sections, references).
- **Brotli/zstd correct** — bundles the `brotli`/`zstandard` decoders so
  Cloudflare-fronted sites (which default to `content-encoding: br`) decode
  correctly instead of returning binary garbage.

### Known limitations

- **Bot walls** (Cloudflare/DataDome, e.g. ACM, some news sites) are **out of
  scope**. Some deployments may require proxy/fingerprint infrastructure plus
  a separate legal and policy review.
- **Wikipedia-style dense tables** and heavy repo chrome remain weak areas for
  the production `/crawl` cascade. IR v2 represents these structures more
  faithfully, but its refiner has not passed promotion gates and is not wired
  into that cascade.

## Features

### Extraction Engine

- **Native-first extraction** — a Rust/PyO3 `rs-trafilatura` backend handles the
  primary path; non-article candidates below the configured confidence gate
  use Python fallbacks, while article-class native candidates have a dedicated
  acceptance shortcut
- **Parallel fallback extraction** — Trafilatura, Readability, markdownify, and
  specialized extractors can run concurrently via `run_in_executor`, then
  apply the configured merge mode (`union` by default; `best` and `longest`
  are also supported)
- **Page-type awareness** — auto-detects article, documentation, academic,
  repository, forum, product, listing, collection, and generic webpage types,
  then configures extractors per type
- **Documentation extractor** — targets Sphinx, MkDocs, and Docusaurus
  `.rst-content` / `.md-content` / `.markdown-body`, strips nav sidebars,
  preserves code blocks, API signatures (dt/dd), and heading hierarchy
- **GitHub README extractor** — targets `article.markdown-body`, strips
  GitHub chrome (headers, sidebars, signup prompts, file navigation)
- **Academic pipeline** — pypdfium2 for PDF extraction, structured parsing for
  title, authors, abstract, sections, references. Auto arXiv PDF fallback
  when HTML abstract page is detected
- **Publisher metadata fallback** — exact DOI lookup through Crossref; optional
  `ELSEVIER_API_KEY` and `IEEE_API_KEY` enable the official publisher APIs.
  Requests use fixed API hosts, bounded JSON responses, short deadlines,
  concurrency limits, and the normal per-domain rate limiter. Without an IEEE
  key, a rendered title is accepted only after a high-similarity Crossref match
  whose DOI prefix and publisher identify IEEE.
- **Fallback table recovery** — eligible Python-fallback pages can detect HTML
  tables and convert them to Markdown
- **Fallback code recovery** — eligible Python-fallback pages can re-inject
  code blocks that a candidate stripped from the source HTML
- **Auditable V2 metadata** — reports the pipeline revision, selected route and
  reasons, model-attempt/success flags, candidate count/disagreement, bounded
  completeness evidence, cache provenance, and request-local stage timings

### Ordered, source-backed document IR v2

`clusy_native` exposes an additive `ordered-dom-ir.v2` library interface for
selectors and future structure-preserving extraction. It does not silently
replace the benchmarked `/crawl` output:

```python
from clusy_native import extract_document_ir_v2, reconstruct_document_ir_v2

document = extract_document_ir_v2(html)
result = reconstruct_document_ir_v2(
    document,
    selected_ids=[document.elements[0].id],
)
print(result.markdown)
```

The bounded graph provides stable IDs and document order, parent/child paths,
source offsets with explicit reliability flags, retained text runs, and typed
table, list, list-item, and math relations. Preformatted code preserves source
whitespace. Every input/node/depth/text/table/math cap has machine-readable
truncation provenance.

Serialization is deterministic for either the complete graph or an ID-selected
subgraph. Text selections retain only the minimum required ancestor structure;
unknown IDs are returned in `missing_ids` and never broaden the selection.
Serialization reports whether source coverage, code whitespace, and table
grids are complete.

`app/services/document_ir_v2_refiner.py` is a deterministic, fail-closed
candidate refiner built on this IR. It returns the byte-identical input when
validation fails. Its current shadow policies regress text and formula quality,
so the module is not imported by the production extraction cascade.

### Performance

- **HTTP/2 multiplexing** — multiple concurrent streams over single TCP
  connections (via `httpx[http2]`)
- **Full content-encoding support** — `gzip`/`deflate`/`br`/`zstd` decoders
  bundled; `Accept-Encoding` is negotiated by httpx to exactly what it can
  decode (advertising `br` without the decoder returns undecodable bytes)
- **Keep-alive connection pooling** — a shared idle pool with 30s
  `keepalive_expiry` reuses warm TCP+TLS connections (inline pre-warming was removed — it
  added a serial HEAD round-trip *before* every fetch instead of saving one)
- **Single-pass extraction** — the conditional-JS path extracts once and
  re-extracts only when it actually escalates (was extracting twice per page)
- **Non-blocking pipeline** — PDF (pypdfium2), academic-HTML, and multi-strategy
  extraction run in executors, off the event loop
- **Configurable concurrency** — `asyncio.Semaphore`-gated in-flight
  operations (default 5) and browser pages (default 2)
- **Resource blocking** — images, fonts, media, and stylesheets are blocked
  during Playwright renders to reduce unnecessary work

### JS Rendering

- **Fresh browser contexts** — every render gets a new isolated context, which
  is destroyed after the request so cookies, storage, caches, and service
  workers cannot cross crawl boundaries
- **Smart escalation** — only launches browser when needed: bot-detection
  walls, SPA shells, sparse content (<200 chars visible text)
- **Automatic static-site exclusion** — known static sites (Python docs,
  Wikipedia, the Rust Book, arXiv, PubMed, and StackOverflow) skip automatic
  JS escalation; an explicit `js_render=true` still takes precedence
- **Known dynamic domains** — ACM, Springer, IEEE, Nature article URLs, Medium,
  and Substack select the JS path automatically in conditional mode when
  `js_render` is omitted; an explicit request value takes precedence
- **Settle detection** — waits up to 2s for dynamic content on sparse
  pages, then extracts whatever is available
- **No anti-bot guarantee** — browser rendering executes JavaScript, but does
  not promise fingerprint anonymity or bypass Cloudflare/DataDome challenges

### Safety & Operations

- **SSRF protection** — static fetches validate every resolved address and
  redirect hop; browser renders validate document redirects and subrequest
  origins before allowing Chromium to connect
- **Defense in depth required** — DNS can change between validation and
  connection, and an egress proxy may resolve names differently. Deny private,
  loopback, link-local, and cloud-metadata networks at the container/VPC
  boundary as well
- **Browser contexts are defense in depth, not a tenant boundary** — every
  render gets a fresh context, but untrusted browser code still belongs in a
  dedicated container/VPC without mounted secrets or host paths
- **Per-domain rate limiting** — token buckets via `aiolimiter`, 2 req/s
  default with configurable burst. A bounded registry and conservative
  overflow bucket prevent unbounded limiter state
- **Optional Redis cache v10** — SHA-256 keys bind the request, build and image
  identity, pipeline/native/router revisions, relevant fetch/render/extraction
  settings, model availability/configuration, and private endpoint/credential
  identities. Corrupt entries become misses and Redis outages degrade to
  uncached operation
- **Singleflight miss coalescing** — concurrent identical requests share one
  live crawl while each caller receives an independent output projection
- **Bearer token auth** — constant-time comparison via `secrets.compare_digest`;
  set `CRAWL4AI_API_TOKEN` to protect crawl/extraction endpoints; health,
  readiness, version, and OpenAPI discovery remain public
- **Familiar endpoints** — `/crawl`, `/md`, `/html`, `/map`, `/health`, with
  request/response shapes that map cleanly onto common scrape APIs

### Familiar API surface

Endpoints and options modelled on the common scrape-API shape, so it's easy to
adopt if you already use a hosted crawler:

- **Multi-format output** — request any of `markdown` (always), `html` (rendered
  source), and `links` (deduped absolute links) per crawl via `formats`
- **`max_age` cache control** — `null` serves any cached entry within TTL, `0`
  bypasses Redis and requests a live crawl, and `N` serves cached only if
  younger than `N` seconds. Concurrent identical live misses may still be
  process-locally singleflight-coalesced
- **Extraction profiles** — `balanced` (default) targets general main-content
  Markdown; `article_body` explicitly requests the precision-oriented prose
  body path; `adaptive` computes a deterministic adaptive candidate, uses
  bounded label-free risk signals, and optionally escalates to the quality
  endpoint; `quality` computes the exact `balanced` candidate before requesting
  that endpoint. `quality` fails back byte-for-byte to its `balanced`
  candidate. `adaptive` fails back byte-for-byte to its already computed
  deterministic adaptive candidate, which may include an explicitly
  experimental adaptive-only article-rescue candidate
- **Structured (JSON) extraction** — pass a `json_schema` and/or
  `extraction_prompt` with the `json` format to get schema-constrained JSON
  back (LLM-backed, schema-validated). Optional: requires `ANTHROPIC_API_KEY`,
  configurable model (`EXTRACTION_MODEL`, default `claude-haiku-4-5`). When
  unconfigured, `extracted` contains a clear error object while the Markdown
  crawl result remains successful
- **`POST /map`** — bounded, concurrent sitemap-index discovery plus homepage
  links; redirect/peer SSRF checks, gzip expansion limits, same-site filtering,
  and an optional `search` substring filter

## API Reference

### `GET /health`
Returns `{"status": "ok"}`. Unauthenticated.

### `GET /health/ready`
Readiness probe. Checks the native extractor, the connected Playwright browser
process when enabled, Redis when configured, and the local MinerU-HTML adapter
when a quality backend is configured. `http_client` is only an in-process
status marker; it does not inspect a client connection or make an arbitrary
outbound connectivity request. Returns
`{"status": "ready", "checks": {...}}` or `{"status": "degraded", ...}` with
per-check status.

### `GET /health/version`

Returns configured build/image identity (`unknown` when the operator did not
inject it) and the serving-semantics identity used to bind deployments and
benchmark runs:

```json
{
  "sha": "0123456789abcdef",
  "image_digest": "sha256:...",
  "environment": "prod",
  "python_version": "3.12...",
  "native_extractor_version": "...",
  "trafilatura_version": "...",
  "playwright_version": "...",
  "pipeline_revision": "clusy-extraction-v2",
  "adaptive_router_revision": "adaptive-v2",
  "config_fingerprint": "...",
  "config_fingerprint_scheme": "hmac-sha256-v1",
  "quality_backend_configured": false,
  "quality_dependency_available": false,
  "quality_backend_enabled": false,
  "quality_backend_revision": "",
  "playwright_enabled": true
}
```

The fingerprint binds the exact output-affecting serving configuration without
returning plaintext secrets or private endpoint values. Production requires an
independent `SERVING_FINGERPRINT_KEY`; the externally presented bearer token is
part of the fingerprinted serving state but is never reused as HMAC key
material. A non-production process without the independent key uses an
ephemeral key, so its fingerprint is intentionally not portable across
restarts.

### `POST /crawl`
Full crawl with markdown extraction and metadata.

**Request:**
```json
{
  "urls": ["https://example.com"],
  "max_depth": 0,
  "max_pages": 1,
  "allow_subdomains": false,
  "priority": 10,
  "js_render": false,
  "word_count_threshold": 10,
  "extraction_profile": "balanced",
  "wait_for_selector": null,
  "formats": ["markdown", "json"],
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

Recursive discovery is explicitly opt-in with `max_depth > 0`. The default
`max_depth: 0` preserves the original behavior exactly: only explicit `urls`
are crawled, one result is returned for each input, and compatibility
`max_pages` does not truncate a multi-URL request. In recursive mode:

- `max_pages` is one total page budget across every seed and must be at least
  the number of seed URLs.
- `allow_subdomains` defaults to `false`; discovered URLs must remain on an
  exact seed host unless it is enabled.
- `priority` is applied to seed and discovered work. Hosts rotate fairly, while
  each host's higher-priority work runs first.
- every discovered URL passes canonical deduplication, depth/site/budget checks,
  and path, session, facet, query-variant, and calendar trap defenses.
- every seed and discovered URL is checked against its origin's `robots.txt`
  before the page fetch. Exact `ClusyCrawler` groups take precedence over the
  `*` fallback; longest matching `Allow`/`Disallow` wins and `Allow` wins a tie.
  Wildcards and end anchors are supported.
- static redirect targets and Playwright `document` navigations re-run both
  crawl-scope and robots checks before destination bytes. Same-origin path
  redirects are re-evaluated, and off-site redirects cannot escape the
  exact-host/subdomain scope.
- results are returned in deterministic frontier claim order and never exceed
  `max_pages`. Links are retained internally for discovery but are returned
  only when `formats` includes `"links"`.

Robots retrieval is manual and bounded: every redirect receives SSRF and
post-connect peer validation, HTTPS cannot downgrade to HTTP, and there are no
same-hop retries. A 2xx response is parsed; 404/410 and other non-429 4xx
responses allow crawling under RFC 9309's "unavailable" policy. A 408/425/429,
5xx, timeout, network/validation failure, redirect failure, oversized body, or
over-complex policy temporarily denies recursive crawling. A denied seed or
page is still returned with an honest `results[].error`, and the frontier
records `robots_disallowed`. There is intentionally no request-side bypass in
this release. Cached effective URLs are re-checked before use, and recursive
singleflight work is isolated from flat flights that have no policy callback.
None of this code runs for the default flat `max_depth: 0` path.

Request-side `extraction_strategy` and `verbose` remain accepted and ignored
for Crawl4AI wire compatibility.

`extraction_profile` accepts:

- `balanced` (default) — general main-content Markdown for heterogeneous web
  pages, including useful document structure when available. It excludes the
  experimental adaptive-only article rescue.
- `article_body` — explicitly requests the precision-oriented prose body
  candidate. Use it when the desired output is article text rather than a
  page's broader Markdown structure. If no usable article candidate exists,
  the normal confidence-gated fallback pipeline still applies.
- `adaptive` — computes its deterministic candidate first. That cascade begins
  from the balanced path and may evaluate a bounded, explicitly experimental
  adaptive-only article-rescue candidate. Confidence, page type, source
  structure loss, candidate disagreement, and bounded HTML complexity then
  decide whether to call the optional model-assisted pipeline. High-confidence
  simple pages pay no model cost. An unconfigured backend skips the call;
  timeout, capacity, circuit-open, cancellation, invalid, or rejected model
  output returns the precomputed deterministic adaptive candidate
  byte-for-byte.
- `quality` — computes the exact deterministic `balanced` candidate, then asks
  the optional pinned MinerU-HTML-compatible processing pipeline for
  model-assisted main-content Markdown. It requires the `quality` install/image
  target and all three endpoint URL/key/model settings. An unconfigured backend
  skips the call; timeout, cancellation, oversized input, missing dependency,
  capacity/circuit-open state, invalid output, or verifier rejection returns
  the precomputed `balanced` candidate byte-for-byte.

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
      "extraction_strategy": "rs-trafilatura",
      "content_scope": "main_content",
      "truncated": false,
      "truncation_reason": "",
      "pipeline_revision": "clusy-extraction-v2",
      "extraction_route": "native_fast_path",
      "route_reasons": [],
      "model_assisted": false,
      "quality_attempted": false,
      "quality_succeeded": false,
      "candidate_count": 1,
      "candidate_disagreement": 0.0,
      "completeness_score": 0.0,
      "completeness_coverage": "output_only",
      "source_coverage_score": null,
      "output_grounding_score": null,
      "completeness_reasons": [],
      "stage_timings_ms": {
        "queue": 0.2,
        "fetch": 320.1,
        "render": 0.0,
        "extraction": 8.4,
        "total": 328.9
      },
      "cache_status": "live",
      "cache_age_ms": null,
      "cache_lookup_ms": null
    },
    "error": null
  }],
  "total_time_ms": 453,
  "total_pages": 1
}
```

The batch envelope can return HTTP 200 while an individual URL reports
`results[].error`; production monitoring must inspect every result, not only the
outer status code. `content_scope` distinguishes full text, landing/abstract,
metadata-only, source, and general main-content results. When an extractor,
thread, references section, or configured output cap omits content,
`truncated=true` and `truncation_reason` make that partial result explicit.

`completeness_score` remains numeric for wire compatibility, but its coverage
field is inseparable from its meaning. A score of `0.0` with `unassessed` or
`output_only` means source completeness was not scored; it does not assert zero
or perfect source coverage. `completeness_coverage` distinguishes those states
from a fully assessed source and a bounded source prefix. A prefix-limited
assessment is capped at `0.99` and includes
`grounding_budget_limited` in `completeness_reasons`.

When available, nullable `source_coverage_score` measures how much assessed
source content is represented and nullable `output_grounding_score` measures
how much returned content is grounded in that assessed source. These bounded
lexical and structural diagnostics are routing/operations signals, not
semantic truth scores.

On a Redis hit, `cache_status` becomes `hit`, age and lookup latency are
reported, and `stage_timings_ms` contains only the current lookup wall time
with fetch/render/extraction set to zero. Timings from the original live crawl
are intentionally not replayed as if they happened in the cache-hit request.

### `POST /md`
Single-URL Markdown compatibility wrapper around the canonical crawl path.
`options` currently recognizes `js_render` and `wait_for_selector`.

**Request:**
```json
{
  "url": "https://example.com",
  "word_count_threshold": 10,
  "extraction_profile": "balanced",
  "options": {
    "js_render": false,
    "wait_for_selector": null
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
Discover a site's URLs (no rendering, no extraction) via robots.txt →
sitemap(s), supplemented with same-site homepage links (the root host,
its subdomains, and `www` equivalence). Sitemap indexes are
traversed concurrently within fixed file/count/size limits.

**Request:**
```json
{ "url": "https://docs.python.org/3/", "limit": 1000, "search": "3.12" }
```

**Response:**
```json
{ "status": "ok", "url": "https://docs.python.org/3/", "links": ["https://..."], "count": 42 }
```

## Configuration

Common settings are listed below. `app/config.py` is the complete validated
environment schema; values can come from a `.env` file or the process
environment.

### Service

| Variable | Default | Description |
|----------|---------|-------------|
| `ENVIRONMENT` | `local` | `prod` enables fail-closed production auth/sandbox validation. |
| `CRAWL4AI_API_TOKEN` | (empty) | Bearer token. Empty = unauthenticated — only safe on a trusted private network. |
| `SERVING_FINGERPRINT_KEY` | (empty) | Independent high-entropy HMAC key, at least 32 characters and distinct from the bearer token. Required in `prod`; never returned by health endpoints. |
| `GIT_SHA` | `unknown` | Immutable 7–64 character hexadecimal Git revision. Required in `prod` when Redis is configured, preventing cross-version cache reuse. |
| `IMAGE_DIGEST` | `unknown` | Operator-configured deployed OCI manifest digest (`sha256:<64 hex>`); `unknown` when not injected. Included in `/health/version`, the serving fingerprint, and cache v10 identity; a resolved digest is required by claimable deployment benchmarks. |
| `CORS_ALLOW_ORIGINS` | (empty) | Comma-separated browser origins. Empty = CORS off. `*` discouraged. |
| `MAX_CONCURRENT_TASKS` | `5` | Live-crawl semaphore cap; cache hits do not consume it. |
| `MAX_CONCURRENT_PAGES` | `2` | Max concurrent Playwright browser pages. Size this from measurements on your workload. |
| `MAX_PENDING_REQUESTS` | `100` | Admission cap covering queued and in-flight crawl requests; cache hits still pass request admission. |
| `MAX_REQUEST_BODY_BYTES` | `1048576` | Maximum accepted request-body size. |
| `CRAWL_REQUEST_TIMEOUT_S` | `120` | End-to-end crawl request deadline. |
| `MAX_RESPONSE_OUTPUT_BYTES` | `33554432` | Maximum serialized response-output budget. |
| `MAX_DOMAINS_PER_REQUEST` | `50` | Maximum distinct domains accepted in one request. |
| `DEFAULT_MAX_PAGES` | `1` | Default recursive page budget for `/crawl` requests that omit `max_pages`; depth `0` still crawls only the explicit seed URLs. |
| `MAP_TIMEOUT_S` | `30` | Total `/map` discovery deadline. |
| `MAP_MAX_DOWNLOAD_BYTES` | `20971520` | Aggregate `/map` discovery-byte budget, including robots, sitemap bodies/gzip expansion, and the homepage. |
| `MAP_MAX_CONCURRENCY` | `4` | Process-local cap on concurrent `/map` jobs; each job also uses a fixed bounded sitemap batch. |
| `JS_RENDER_MODE` | `conditional` | `conditional` (auto-detect), `force` (always), `never` (no JS). |

### HTTP Client

| Variable | Default | Description |
|----------|---------|-------------|
| `HTTP_TIMEOUT_S` | `30` | Read timeout for HTTP requests. |
| `HTTP_CONNECT_TIMEOUT_S` | `5` | TCP/TLS connection timeout. |
| `HTTP_TOTAL_TIMEOUT_S` | `45` | Shared deadline across validation, retries, and response transfer. |
| `HTTP_MAX_KEEPALIVE_CONNECTIONS` | `50` | Idle connections retained by the shared client pool. |
| `HTTP_MAX_CONNECTIONS` | `100` | Total connection pool size. |
| `HTTP_USER_AGENT` | `ClusyCrawler/1.0` | User-Agent header. |
| `HTTP_MAX_ATTEMPTS` | `3` | Maximum actual outbound attempts, each independently rate-limited. |
| `HTTP_RETRY_MAX_DELAY_S` | `5` | Cap on exponential/`Retry-After` retry delay. |
| `HTTP_PROXY` | (empty) | Optional static-fetch egress proxy; credentials are treated as sensitive config. |
| `PLAYWRIGHT_PROXY` | (empty) | Optional browser egress proxy; configure alongside the HTTP proxy when equivalent routing is required. |

### Recursive robots policy

| Variable | Default | Description |
|----------|---------|-------------|
| `ROBOTS_TIMEOUT_S` | `5` | Total rate-limit, redirect, and network deadline for one policy fetch. |
| `ROBOTS_MAX_REDIRECTS` | `5` | Maximum manually followed, SSRF-validated redirect hops. |
| `ROBOTS_MAX_BODY_BYTES` | `524288` | Maximum decoded robots body size. |
| `ROBOTS_MAX_URL_LENGTH` | `4096` | Maximum URL length admitted by the recursive policy parser. |
| `ROBOTS_MAX_RULES` | `4096` | Maximum parsed rules per policy. |
| `ROBOTS_MAX_RECORDS` | `8192` | Maximum parsed records per policy. |
| `ROBOTS_MAX_LINE_CHARS` | `8192` | Maximum characters accepted in one robots line. |
| `ROBOTS_MAX_CONCURRENCY` | `16` | Process-local concurrent origin-policy fetches. |
| `ROBOTS_CACHE_MAX_ENTRIES` | `2048` | Loop-local LRU entry bound; cached entries contain parsed rules, not bodies. |
| `ROBOTS_CACHE_TTL_S` | `3600` | Parsed 2xx policy TTL. |
| `ROBOTS_UNAVAILABLE_CACHE_TTL_S` | `900` | Allowing 4xx-unavailable policy TTL. |
| `ROBOTS_ERROR_CACHE_TTL_S` | `60` | Fail-closed transient/unsafe policy TTL. |

### Extraction

| Variable | Default | Description |
|----------|---------|-------------|
| `PARALLEL_EXTRACTION_ENABLED` | `true` | Run strategies concurrently. |
| `EXTRACTION_MERGE_MODE` | `union` | `union` (multi-strategy merge), `best`, `longest`. |
| `NATIVE_EXTRACTION_ENABLED` | `true` | Use the compiled Rust/PyO3 backend as the primary extractor. |
| `NATIVE_EXTRACTION_MIN_CONFIDENCE` | `0.60` | Non-article native candidates below this confidence use Python fallback; article-class candidates have a dedicated shortcut. |
| `MAX_CONCURRENT_EXTRACTIONS` | `2` | Max concurrent page-level CPU extraction jobs. |
| `EXTRACT_MAX_TEXT_LENGTH` | `500000` | Max characters in extracted text. |
| `ADAPTIVE_EXTRACTION_MIN_CONFIDENCE` | `0.75` | Adaptive requests escalate candidates below this confidence. |
| `ADAPTIVE_EXTRACTION_STRUCTURAL_SCORE_THRESHOLD` | `3` | Bounded structural-complexity score that triggers adaptive escalation. |
| `ADAPTIVE_EXTRACTION_STRUCTURE_LOSS_THRESHOLD` | `1` | Number of source structure categories entirely absent from the candidate that triggers escalation. |
| `ADAPTIVE_EXTRACTION_CANDIDATE_DISAGREEMENT_THRESHOLD` | `0.45` | Mean pairwise bounded candidate-token distance that triggers escalation. |
| `ADAPTIVE_EXTRACTION_MAX_SCAN_CHARS` | `200000` | Maximum HTML prefix inspected by the adaptive router. |
| `ADAPTIVE_EXTRACTION_RISKY_PAGE_TYPES` | `collection,listing,product` | Comma-separated deterministic page types that trigger adaptive escalation. |

### Model-assisted main content (optional, `adaptive` / `quality` profiles)

| Variable | Default | Description |
|----------|---------|-------------|
| `QUALITY_EXTRACTION_BASE_URL` | (empty) | Operator-controlled OpenAI-compatible endpoint; empty disables this path. |
| `QUALITY_EXTRACTION_API_KEY` | (empty) | Endpoint credential; never included in crawler logs. |
| `QUALITY_EXTRACTION_MODEL` | (empty) | Model served by the endpoint. |
| `QUALITY_EXTRACTION_BACKEND_REVISION` | (empty) | Immutable operator-supplied model/backend build identity. Output remains usable when empty, but successful model output is not persisted in Redis. |
| `QUALITY_EXTRACTION_PROMPT_PROFILE` | `openai_json` | `openai_json` uses the upstream v2/JSON contract for general instruction models; `mineru_compact` uses the short_compact/compact wire contract for compatible compact checkpoints. |
| `QUALITY_EXTRACTION_TIMEOUT_S` | `45` | Total queue-and-inference deadline. |
| `QUALITY_EXTRACTION_CAPACITY_TIMEOUT_S` | `1` | Maximum queue wait for a model worker before deterministic fallback opens the capacity circuit. |
| `QUALITY_EXTRACTION_SHUTDOWN_TIMEOUT_S` | `5` | Bounded worker drain and quality-client cleanup deadline during graceful shutdown. |
| `QUALITY_EXTRACTION_MAX_INPUT_CHARS` | `1000000` | Reject model inference above this input size and use deterministic fallback. |
| `QUALITY_EXTRACTION_MAX_CONCURRENCY` | `2` | Maximum live model workers, including workers whose callers timed out. |
| `QUALITY_EXTRACTION_FAILURE_THRESHOLD` | `3` | Consecutive failures before the circuit opens. |
| `QUALITY_EXTRACTION_COOLDOWN_S` | `30` | Circuit-breaker cooldown before one half-open probe. |

The optional package is source-pinned to MinerU-HTML revision
`73cf266690befd209cae7e6fdff9716d5b31a976`. The crawler does not ship a model
server or silently send pages anywhere: operators must configure their own
endpoint. Upstream benchmark numbers are not Clusy benchmark numbers; only a
run through the checked-in Clusy harness is evidence for this service.

The MinerU-HTML *code* is Apache-2.0, but the official v1.1 compact weights are
derived from Tencent Hunyuan and carry a separate community license that
excludes use in the EU, UK, and South Korea and restricts model-improvement
uses. Clusy does not bundle those weights. A globally deployed service must
serve a checkpoint whose model, training-data, and output licenses have passed
legal review; `mineru_compact` describes a protocol, not approval of a
particular checkpoint.

Redis v10 supports all four extraction profiles. It can cache deterministic
adaptive fast paths and an unconfigured quality lane's exact deterministic
fallback. Verified successful quality output is persisted only when
`QUALITY_EXTRACTION_BACKEND_REVISION` supplies an immutable backend identity.
It deliberately does not persist an unversioned model output or a deterministic
fallback produced after an attempted quality call times out, hits
capacity/circuit state, fails, or is rejected; neither unbound nor transient
backend state may survive for a full cache TTL.

The key binds backend configured state, local adapter availability, and private
identities for the endpoint and API key, so configuring, installing, or
rotating the quality backend cannot reuse an entry from a different serving
state. It also binds the build/image, pipeline, native backend, router, every
adaptive threshold, and relevant
fetch/render/extraction/scholarly settings. Raw HTML is never stored in Redis,
and requests that ask for `html` take the live path. Concurrent identical
misses still share one process-local singleflight.

### Scholarly metadata fallbacks

| Variable | Default | Description |
|----------|---------|-------------|
| `SCHOLARLY_METADATA_ENABLED` | `true` | Allow bounded metadata-only fallback for recognized publisher URLs. |
| `SCHOLARLY_METADATA_TIMEOUT_S` | `8` | Total metadata lookup deadline. |
| `SCHOLARLY_METADATA_MAX_CONCURRENCY` | `2` | Process-wide concurrent metadata lookups. |
| `SCHOLARLY_METADATA_MAX_RESPONSE_BYTES` | `524288` | Maximum decoded JSON response size. |
| `ACADEMIC_PDF_FALLBACK_TIMEOUT_S` | `12` | Shared wall-clock budget for all PDF candidates advertised by one landing page. |
| `ELSEVIER_API_KEY` | (empty) | Enables official ScienceDirect PII lookup. |
| `IEEE_API_KEY` | (empty) | Enables official IEEE article-number lookup. |

### Playwright / JS Rendering

| Variable | Default | Description |
|----------|---------|-------------|
| `PLAYWRIGHT_ENABLED` | `true` | Enable browser-based JS rendering. |
| `PLAYWRIGHT_JAVA_SCRIPT_ENABLED` | `true` | Allow JS requests and automatic escalation. |
| `PLAYWRIGHT_TIMEOUT_S` | `30` | Max time for Playwright page load. |
| `PLAYWRIGHT_DISABLE_SANDBOX` | `false` | Keep Chromium's sandbox enabled. Use the escape hatch only inside a separately isolated runtime. |
| `PLAYWRIGHT_MAX_HTML_BYTES` | `10485760` | Maximum serialized rendered HTML size. |

### Rate Limiting

| Variable | Default | Description |
|----------|---------|-------------|
| `RATE_LIMIT_REQUESTS_PER_SECOND` | `2.0` | Per-domain token bucket fill rate. |
| `RATE_LIMIT_BURST` | `5` | Maximum burst before throttling. |
| `RATE_LIMIT_MAX_DOMAINS` | `1000` | Bounded per-domain limiter registry size before the conservative overflow bucket is used. |

### Cache

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | (empty) | Redis connection string. Empty = no caching. |
| `CACHE_TTL_S` | `3600` | Cache entry TTL in seconds. |
| `CACHE_CONNECT_TIMEOUT_S` | `0.75` | Redis connect/ping timeout before uncached degradation. |
| `CACHE_OPERATION_TIMEOUT_S` | `0.5` | Per-operation Redis deadline. |
| `CACHE_FAILURE_COOLDOWN_S` | `5` | Initial reconnect cooldown after an outage; repeated failures back off to 60s. |
| `CACHE_MAX_ENTRY_BYTES` | `1048576` | Maximum serialized canonical result stored per key. |

### Structured extraction (optional, `json` format)

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | (empty) | Enables `json` extraction. Empty = feature disabled. |
| `EXTRACTION_MODEL` | `claude-haiku-4-5` | Model for extraction. Set to `claude-opus-4-8` for higher quality. |
| `EXTRACTION_MAX_TOKENS` | `8192` | Max output tokens per extraction. |
| `EXTRACTION_MAX_INPUT_CHARS` | `100000` | Page content truncation before extraction. |
| `STRUCTURED_EXTRACTION_MAX_CONCURRENCY` | `2` | Maximum concurrent structured model calls. |
| `STRUCTURED_EXTRACTION_TIMEOUT_S` | `45` | Queue-and-inference deadline for structured extraction. |

With uv, install this feature using `uv sync --extra llm` (combine it with
`--extra dev` for development). The pip workflow is documented below.

## Project Structure

```
crawler/
├── app/
│   ├── main.py                       # FastAPI app, lifespan, middleware, routers
│   ├── config.py                     # Validated environment configuration
│   ├── models/                       # Request/response contracts and V2 metadata
│   ├── routers/
│   │   ├── health.py                 # Health, readiness, build/config identity
│   │   ├── crawl.py                  # POST /crawl
│   │   ├── extract.py                # POST /md and /html
│   │   └── map.py                    # POST /map discovery
│   ├── services/
│   │   ├── crawler.py                # Orchestration, cache projection, singleflight
│   │   ├── extractor.py              # V2 routes, risk, provenance, fallbacks
│   │   ├── quality_extractor.py      # Bounded optional model client + verifier
│   │   ├── document_ir_v2_refiner.py # Shadow-only deterministic IR refiner
│   │   ├── frontier.py               # Fair bounded recursive frontier
│   │   ├── robots.py                 # Fail-closed recursive robots policy
│   │   ├── document_policy.py        # Redirect/discovery scope callbacks
│   │   ├── fetcher.py / renderer.py  # HTTP and isolated Playwright fetch planes
│   │   └── academic.py               # PDF and scholarly extraction/fallbacks
│   ├── cache/__init__.py              # Redis cache v10 client and key semantics
│   └── middleware/                    # Auth and request/resource admission
├── native/
│   ├── src/lib.rs                     # Rust/PyO3 extraction entry points
│   ├── src/document_ir.rs             # Benchmark-pinned ordered IR v1
│   ├── src/document_ir_v2.rs          # Source-backed ordered structural IR v2
│   ├── python/clusy_native/            # Typed Python façades
│   ├── vendor/                         # Audited source-vendored Rust dependencies
│   └── Cargo.lock                     # Exact Rust dependency graph
├── bench/
│   ├── neutral_benchmark.py            # Pinned AEB evaluation
│   ├── wcxb_benchmark.py               # Pinned WCXB evaluation
│   ├── webis_benchmark.py              # Pinned Webis/SIGIR evaluation
│   ├── webmainbench_benchmark.py       # Raw + annotation-scrubbed WebMainBench
│   ├── document_ir_v2_refiner_shadow.py # Reference-isolated shadow diagnostic
│   ├── live_vendor_benchmark.py        # Sealed Exa/Firecrawl protocol
│   └── *_BENCHMARK.md                  # Reproduction and claim boundaries
├── docs/SOTA_ARCHITECTURE.md            # Architecture record and promotion gates
├── tests/                               # Unit, integration, load, and contract tests
├── Dockerfile                           # Multi-stage FastAPI/Chromium runtime
├── docker-compose.yml                   # Self-contained crawler + Redis stack
├── pyproject.toml / uv.lock             # Locked Python project and tooling
├── .env.example                         # Environment configuration template
└── README.md
```

## Development

Source builds require Python 3.12, a Rust 1.85+ toolchain, and a linker
supported by Cargo. Docker users do not need Rust installed on the host.

### uv workflow (recommended)

```bash
# Builds the local clusy-native path dependency through maturin.
rustc --version
uv sync --locked --extra dev
uv run playwright install chromium

# Verify that Python loaded the compiled extension and its pinned backend.
uv run python -c \
  "import clusy_native; print(clusy_native.backend_version())"

# Run locally
uv run uvicorn app.main:app --reload --port 11235

# Tests and static checks
uv run pytest tests/ -v
uv run ruff check .
uv run mypy app
```

`uv.lock` pins the Python graph and [`native/Cargo.lock`](native/Cargo.lock)
pins the Rust graph. After editing Rust sources, force a local extension rebuild:

```bash
uv sync --locked --extra dev --reinstall-package clusy-native
```

To enable optional LLM-backed JSON extraction, add `--extra llm` to the sync
command. To develop the model-assisted main-content path, add `--extra quality`;
the two extras are independent.

### pip/maturin workflow

`[tool.uv.sources]` is uv-specific, so generic pip users must build and install
the local extension before installing the root project:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install "maturin>=1.9,<2.0"
maturin develop --release --locked --manifest-path native/Cargo.toml
python -m pip install -e ".[dev]"
python -m playwright install chromium
```

Use `".[dev,llm]"` in the pip command if structured extraction is required.

## Deploy

### Docker Compose

The checked-in Compose stack is self-contained: it builds the deterministic
runtime image, starts a bounded Redis cache, and publishes the crawler on
`localhost:11235`.

```bash
cp .env.example .env
GIT_SHA="$(git rev-parse HEAD)" docker compose up -d --build

curl -sf http://localhost:11235/health/ready
```

Local Compose defaults to `ENVIRONMENT=local`, which permits empty
authentication and fingerprint keys for development. Before exposing a shared
deployment, set `ENVIRONMENT=prod`, an exact `GIT_SHA`, a non-empty
`CRAWL4AI_API_TOKEN`, and a different high-entropy
`SERVING_FINGERPRINT_KEY` in `.env`.

### Bare Docker

```bash
docker build --build-arg GIT_SHA="$(git rev-parse HEAD)" -t clusy-crawler .
docker run --rm -d --name clusy-crawler -p 11235:11235 --shm-size=1g \
  --user 10001:10001 --init --read-only \
  --tmpfs /tmp:size=512m,mode=1777 \
  --tmpfs /home/crawler:size=64m,mode=0700,uid=10001,gid=10001 \
  --security-opt seccomp="$(pwd)/seccomp_profile.json" \
  --pids-limit=256 --memory=4g --cpus=2 \
  -e ENVIRONMENT=prod \
  -e SERVING_FINGERPRINT_KEY=your-independent-32-plus-char-secret \
  -e CRAWL4AI_API_TOKEN=your-token clusy-crawler

curl -sf -X POST http://localhost:11235/crawl \
  -H 'Authorization: Bearer your-token' \
  -H 'Content-Type: application/json' \
  -d '{"urls":["https://example.com"]}'
```

The Dockerfile is a multi-stage build: digest-pinned `rust:1.85-slim` supplies
the toolchain, a digest-pinned Python 3.12/maturin stage builds a wheel using
`native/Cargo.lock` and exports hash-locked Python requirements from `uv.lock`.
The final Python 3.12 image receives only those verified dependencies, the
wheel, Chromium, and the application. The Rust compiler and Cargo caches are
not copied into the runtime stage. The service runs as the non-root `crawler`
user (UID 10001). `seccomp_profile.json` is the Playwright 1.60 profile derived
from Docker's default policy with `clone`, `setns`, and `unshare` permitted so
Chromium can create its user namespace sandbox. The checked-in Compose service
applies it automatically. It intentionally does not set
`no-new-privileges` or drop every Linux capability: on hosts that disable
unprivileged user namespaces, either setting would prevent Chromium's
version-matched SUID sandbox helper from providing the secure fallback. The
application process still runs non-root, and Chromium's sandbox remains
explicitly enabled.

The default `runtime` image is deterministic and does not contain MinerU-HTML.
Build the opt-in, revision-pinned quality image only when an operator endpoint
is available:

```bash
docker build --target quality-runtime \
  --build-arg GIT_SHA="$(git rev-parse HEAD)" -t clusy-crawler:quality .
# Or with Compose:
GIT_SHA="$(git rev-parse HEAD)" \
  CRAWLER_DOCKER_TARGET=quality-runtime docker compose build crawler
```

### Operational notes

- **Memory**: Chromium is the heaviest component. Observe real workload RSS and
  lower `MAX_CONCURRENT_TASKS` / `MAX_CONCURRENT_PAGES` if the container
  approaches its limit. The Compose example sets a 4 GiB hard limit.
- **`/dev/shm`**: the examples allocate 1 GiB for Chromium. Increase it if
  browser processes crash under parallel rendering.
- **Browser installation**: Chromium is included in the runtime image whether
  or not `PLAYWRIGHT_ENABLED` is false. That setting disables use at runtime; it
  does not create a smaller image. A static-only image requires a separate
  Dockerfile that omits the browser packages and install step.
- **Browser isolation**: URL validation reduces SSRF risk but cannot close every
  DNS/proxy time-of-check-to-time-of-use gap. Chromium's sandbox is explicitly
  enabled by default, the image runs non-root, and Compose applies the pinned
  Playwright seccomp profile. Keep all three controls. Put the service in a
  dedicated network and enforce outbound denies for RFC1918, loopback,
  link-local, and cloud metadata addresses.
- **Untrusted JavaScript**: do not mount secrets, the Docker socket, or writable
  host paths into the crawler container. Prefer a dedicated instance for each
  trust boundary.
- **Auth**: `ENVIRONMENT=prod` refuses to start without
  `CRAWL4AI_API_TOKEN` (or `CRAWLER_API_TOKEN`) and an independent
  `SERVING_FINGERPRINT_KEY`. Non-production modes may run unauthenticated for
  local development. Health diagnostics and OpenAPI discovery remain public.
  See [`SECURITY.md`](SECURITY.md).
- **Build identity**: inject the immutable deployed manifest digest as
  `IMAGE_DIGEST=sha256:...` after registry resolution, then verify
  `/health/version`. A mutable image tag is not an evidence identity.
- **Logs**: `docker compose logs -f crawler` (structured JSON via structlog).

## Key Dependencies

| Library | Version | Purpose |
|---------|---------|---------|
| `fastapi` + `uvicorn` | 0.115+ / 0.45+ | HTTP server |
| `httpx` + `h2` | 0.28+ / 4.3+ | Async HTTP with HTTP/2 |
| `brotli` + `zstandard` | 1.1+ / 0.23+ | Content-encoding decoders (required for Brotli/zstd sites) |
| `clusy-native` | 0.1.0 (local) | Primary Rust/PyO3 extraction extension |
| `rs-trafilatura` | broad 0.2.2 plus a source-vendored article backend | Native extraction graph pinned by `native/Cargo.lock` and the repository tree |
| `pyo3` | 0.27.2 | Rust/Python extension bindings |
| `trafilatura` | >=2.0,<3.0 | Python extraction fallback (Apache-2.0) |
| `playwright` | 1.48+ | JS rendering via Chromium |
| `pypdfium2` | 4.30+ | PDF extraction (BSD; replaced AGPL PyMuPDF) |
| `readability-lxml` | 0.8+ | Mozilla Readability fallback |
| `markdownify` | 1.2+ | HTML → Markdown conversion (MIT; replaced GPLv3 html2text) |
| `aiolimiter` | 1.0+ | Per-domain rate limiting |
| `structlog` | 24.4+ | Structured JSON logging |
| `redis` | 5.2+ | Optional response caching |
| `orjson` | 3.10+ | Fast JSON serialization |
| `anthropic` (extra: `llm`) | 0.69+ | Optional structured (JSON) extraction |
| `mineru-html` (extra/image: `quality`) | exact git revision | Optional model-assisted main-content extraction |

## Acknowledgements

Inspired by [crawl4ai](https://github.com/unclecode/crawl4ai), and built on the
excellent [trafilatura](https://github.com/adbar/trafilatura),
[readability-lxml](https://github.com/buriy/python-readability), and
[Playwright](https://github.com/microsoft/playwright). The neutral evaluation
uses Zyte's [article-extraction-benchmark](https://github.com/scrapinghub/article-extraction-benchmark).

## License

Apache License 2.0 — see [LICENSE](LICENSE). Third-party components and their
licenses are listed in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).

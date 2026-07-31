# Architecture

Clusy Crawler is a bounded FastAPI service that fetches HTTP(S) resources and
returns source-derived Markdown plus provenance. Its default extraction path
is deterministic and local. Chromium, Redis, model-assisted main-content
extraction, and schema-constrained JSON extraction are optional and
independently gated.

## System boundary

```text
caller
  │
  ├─ authentication and request budgets
  ├─ URL, DNS, redirect, scope, and robots policy
  ├─ static HTTP fetch ───── conditional Chromium render
  ├─ source-family specialist ─┐
  │                            ├─ deterministic candidate set
  ├─ native general extractor ─┘
  ├─ optional risk-routed quality backend
  ├─ completeness, projection, and response budgets
  └─ Markdown / HTML / links / constrained JSON + provenance
```

The service extracts content. It is not a search engine: indexing, freshness,
ranking, and query retrieval are separate concerns.

## Request lifecycle

### 1. Admission

`AuthMiddleware` protects data routes with a bearer token when configured.
`ResourceLimitMiddleware` bounds request-body size, admitted requests, and
wall-clock duration before application work begins. Production startup fails
when the bearer token or independent serving-fingerprint key is missing.

The public orchestration surface is:

- `GET /health`
- `GET /health/ready`
- `GET /health/version`
- `GET /docs`
- `GET /openapi.json`

### 2. Fetch

The static fetcher uses a shared bounded HTTP/2 client. It:

- accepts only absolute HTTP(S) URLs;
- resolves and checks every destination address;
- follows redirects manually and revalidates every hop;
- rejects loopback, private, link-local, metadata, multicast, reserved, and
  other unsafe destinations;
- rate-limits every outbound attempt;
- caps URL length, retries, connection time, total time, and decompressed body
  size; and
- handles Brotli and Zstandard content encodings explicitly.

These checks reduce SSRF and resource-exhaustion risk. They do not replace
network egress policy.

### 3. Render escalation

Static HTML is authoritative for ordinary requests. In `conditional` mode the
service escalates when routing signals indicate a JavaScript shell, bot wall,
sparse response, or failed extraction. Callers can explicitly request
rendering; `never` disables escalation.

The render manager owns browser lifecycle, readiness, concurrency, deadlines,
HTML size, and shutdown. Browser images include Playwright's version-matched
Chromium SUID sandbox helper, and production configuration rejects
`PLAYWRIGHT_DISABLE_SANDBOX=true`.

### 4. Extraction

Strong source-family signals can route to specialists for:

- GitHub repositories, files, commits, issues, pull requests, and diffs;
- PDFs and recognized academic papers;
- documentation-like pages; and
- metadata-only scholarly fallbacks for supported publisher identifiers.

General HTML uses the Rust/PyO3 extractor first. Confidence-gated Python
fallbacks include Trafilatura, Readability, Markdownify, and documentation
extraction. Candidate comparison is bounded and deterministic.

| Profile | Contract |
| --- | --- |
| `balanced` | Default general main-content extraction |
| `article_body` | Precision-oriented article-body extraction |
| `adaptive` | Deterministic result first; optional quality escalation on bounded label-free risk |
| `quality` | Deterministic result first; attempt the configured quality backend for eligible HTML |

`adaptive` and `quality` preserve the deterministic result when the optional
backend is absent, saturated, times out, trips its circuit breaker, returns an
invalid response, or loses the verification comparison. The pinned MinerU
adapter classifies source-derived `_item_id` blocks rather than generating page
text. Clusy strictly validates and binds the exact raw JSON or compact response,
requires it to agree with complete parsed labels, and independently replays the
selected DOM into a
[`quality-source-selection.v0`](QUALITY_SOURCE_SELECTION.md) receipt before
accepting its deterministic serialization. Bounded source-token coverage and
ordering remain additional checks. The receipt binds a parser-repaired mapped
DOM; it is not yet an original-byte span certificate.

### 5. Projection and output

Markdown is always present in a successful crawl result. Callers may request
source HTML, discovered links, or schema-constrained JSON. The response records
the extraction route, reasons, model-assistance state, verified source-selection
identity and counts when applicable, completeness coverage, truncation, cache
state, and per-stage timings.

Output is bounded twice:

1. individual text fields are capped by extraction limits; and
2. the complete serialized response is checked against the response-byte
   budget, including JSON escaping and envelopes.

If a response exceeds its budget, rich payloads are removed from the tail
while result cardinality and explicit errors are retained.

## Recursive crawl

Recursive discovery is opt-in with `max_depth > 0`. The frontier is
deterministic and bounded by pages, depth, hosts, attempts, delay, URL length,
and trap budgets. Completion order cannot reorder the final result list.

The recursive path:

- enforces scheme, canonicalization, same-site scope, and optional subdomains;
- fetches and evaluates `robots.txt` without a request-side bypass;
- revalidates robots redirects through the SSRF guard;
- applies host delays and per-host fairness;
- rejects pagination, session, calendar, and high-cardinality traps; and
- reports terminal reasons in telemetry.

The frontier, limiter, and robots cache are process-local. They are not a
restart-safe, cross-replica crawl plane.

## Cache

Redis is optional. Cache keys bind URL, request semantics, profile, source
revision, serving configuration, and credential identities through
non-reversible fingerprints. A hit is returned only when it satisfies the
request's `max_age` policy.

`max_age=0` disables persistent result-cache reads for the request.
`store_in_cache=false` independently disables persistent result-cache writes.
The effective read/write decision is returned in a versioned per-result
receipt. Policy-specific singleflight keys prevent a no-store request from
joining work that may write a result. Every crawl response also carries the
source revision, serving-config fingerprint, and image digest of the process
that produced it.

Policy-aware recursive crawls bypass the flat result cache because the
cache envelope cannot bind and revalidate the complete redirect, robots, and
scope decision chain. They still use policy-partitioned in-process
singleflight.

## Source-backed document IR

The native and Python packages expose additive `ordered-dom-ir.v2` APIs for
ordered elements, text runs, source spans, lists, tables, code, math, and
deterministic selected serialization. Selection certificates bind source,
graph, selection, and output identities for replay.

The opt-in `ordered-source-text-map.v2` adds exact raw-source provenance for
retained text. A bounded source-order scanner decodes named/numeric character
references and tokenizer newline behavior, then pairs each non-ignorable DOM
text run by direct parent, decoded identity, and order. Accepted spans carry
the exact raw UTF-8 fragment, byte offsets, raw/decoded digests, transform
classification, and a deterministic certificate. Repeated same-parent text is
paired in order rather than guessed by substring search.

The map is all-or-nothing. It rejects non-whitespace reorder, foster parenting,
structural repair, malformed crossing, and optional-end ambiguity whenever
they violate retained explicit-element mapping or the direct-parent,
decoded-identity, and order bijection. Standards-defined implicit structure
such as an inserted `tbody` remains eligible when that contract stays exact.
Incomplete/truncated source and every source/event/run/fragment/stack budget
failure reject the map. Parser-reparented whitespace is omitted only when both
sides are exact HTML whitespace outside a whitespace-preserving context;
omission counts and identities are digest-bound.

`selection-atom-catalog.v1` consumes only an accepted map and emits disjoint
lexical atoms plus typed closure metadata. Every atom source span must be
contained by its closure span. `text_run_id` is the narrow lexical replay
pointer; `selection_id` identifies a typed closure owner whose use still
requires the ledger's verified replay policy and a downstream decision about
grouped atoms or enclosing structure.

These APIs are research surfaces. Catalog construction is default-off; the
mapper and catalog are not invoked by serving decisions or wired into serving
API behavior. They do not change the crawler response schema. A digest proves
deterministic identity, not source trust, authorization, or authenticity.

## Failure behavior

| Failure | Behavior |
| --- | --- |
| Authentication failure | HTTP 401 before crawl admission |
| Unsafe URL or redirect | Per-result error; blocked destination is not requested |
| Static fetch failure with eligible render fallback | Bounded Chromium attempt |
| Render unavailable or rejected | Preserve or continue with the static path |
| Optional quality backend failure | Preserve deterministic candidate |
| Structured JSON backend unavailable | Markdown remains available; JSON carries an error |
| Redis unavailable | Live crawl; cache circuit breaker limits repeated failures |
| Recursive robots policy ambiguous or unsafe | Fail closed for that lease |
| Output budget exceeded | Preserve result count; replace rich tail payloads with explicit errors |
| Shutdown | Stop admission, cancel singleflights, drain component cleanup |

## Invariants

- No unchecked redirect hop.
- No production startup without authentication and a distinct fingerprint key.
- No browser sandbox disablement in production.
- No unbounded input, response, retry, concurrency, queue, or recursive
  dimension.
- No optional model output without a deterministic fallback.
- No secret value in health responses, cache keys, or configuration
  fingerprints.
- No benchmark label, reference, or vendor output in runtime extraction input.

See [`../SECURITY.md`](../SECURITY.md) for the operator threat model.

## Deliberate non-goals

The current service does not provide:

- a global search index or relevance ranker;
- a durable distributed frontier;
- cross-replica rate-limit coordination;
- hard process isolation for every parser;
- bundled unrestricted model weights; or
- a cross-benchmark or live-provider superiority guarantee.

Research successors and their promotion gates are documented in
[`RESEARCH.md`](RESEARCH.md).

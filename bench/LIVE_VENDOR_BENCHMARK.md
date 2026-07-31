# Live vendor benchmark v3

This is a fail-closed diagnostic protocol for fixed-URL, main-content Markdown
extraction. It can collect Clusy, Exa, and Firecrawl observations, but the
current provider tracks are not fully scope/cost comparable and cannot support
a public vendor-win claim. It does not compare search quality, crawl discovery,
or answer synthesis.

No live vendor result is bundled with the repository. Passing unit tests means
the runner and its refusal paths work; it is not evidence that Clusy beats a
vendor.

The current hash-only v3 protocol cannot open a public vendor-win claim. The
offline `aggregate` command verifies completed run directories and reports
descriptive gates, but provider text is intentionally not retained, so an
independent reviewer cannot recompute the quality scores. No trusted
execution-attestation verifier is implemented. Therefore every aggregate has
`vendor_win_claimable=false` and `NO_VENDOR_WIN_CLAIM`, even when its observed
quality, latency, and cost gates pass. This is a deliberate fail-closed
limitation, not an assertion that an unsigned local hash chain is a security
attestation.

## What v3 fixes

The v3 runner requires all of the following before creating a paid-provider
request:

- a canonical SHA-256-sealed v3 manifest and an exact runtime digest
  confirmation;
- an explicit `--execute-paid` flag and finite per-selected-provider caps no
  larger than the sealed caps;
- public HTTPS task URLs, no credential-like query names, and a fresh DNS check
  that rejects every private or special address; percent-escaped/invalid DNS
  hosts and ambiguous semicolon query separators are also refused;
- a clean, committed runner and immutable container digest for a claimable run;
- a Clusy `/health/version` preflight matching the sealed source revision,
  `config_fingerprint`, and immutable deployed `image_digest`;
- an operating-system wall deadline around every request, in addition to the
  HTTP timeout;
- an explicit authorization for third-party data transfer.

HTTPX is configured with `trust_env=False`, redirects and retries are disabled,
and provider order is deterministically randomized per task.

Provider outputs are processed in memory. v3 persists only response/content
hashes, derived quality scores, counts, latency, cache evidence, cost/credit
evidence, categorical status and bounded provenance. It does not save raw HTTP
responses, provider titles, extracted text, free-form provider errors,
publication/fetch timestamps, snippets, highlights, links, or redundant
manifest labels in events. Every artifact is scanned for the exact runtime
credentials before it is finalized.

Every retained event string or list is carrier-safe: it must be an exact
schema/protocol constant, an exact sealed manifest/task value, a bounded
enumeration, a SHA-256 digest, a canonical UTC timestamp, or the strict
`url:[redacted]#sha256=<16-hex>` token. Randomized provider order and structure
strata must match deterministic recomputation. Adding raw text to an otherwise
allowed field is rejected even if the event, summary and completion hashes are
all recomputed.

V3 has exactly one timestamp representation:
`YYYY-MM-DDTHH:MM:SS.ffffffZ`. Missing, shortened, or additional fractional
digits; basic/week dates; numeric UTC offsets; and alternate zero-offset
spellings are rejected in references, manifests, cache primes, runs, events,
summaries, and completion records. Reference capture is no later than manifest
sealing, and a run cannot predate its manifest.

The dynamic Clusy service endpoint is never persisted in events. Events contain
`[CLUSY_ENDPOINT_REDACTED]` plus a SHA-256 digest of the exact endpoint. The
fixed public Exa and Firecrawl API endpoints remain visible and are also
hashed. Every Clusy event in one run must carry the same endpoint digest.

Legacy v1 manifests remain loadable for offline validation and historical
scoring only. Paid execution of v1 is refused. The current runner deliberately
rejects v2 artifacts: v3 changes retained artifact schemas, bootstrap
preregistration, Firecrawl cache-write semantics, and cache-prime timing.
Historical v2 evidence requires its pinned v2 runner and must never be
silently interpreted as v3.

## Provider selection and Exa authorization

`providers` must be in canonical order and contain Clusy plus at least one
vendor:

```json
["clusy", "firecrawl"]
```

or:

```json
["clusy", "exa", "firecrawl"]
```

Only selected providers appear in `plans`, `pricing`, and `budgets`. A
Clusy+Firecrawl run neither requires an Exa key nor constructs an Exa request.
Its aggregate claim is scoped only to Clusy versus Firecrawl; the Exa gate
remains closed.

Exa execution has two independent gates. The sealed manifest must contain:

```json
{
  "compliance_acknowledgments": {
    "third_party_data_transfer_authorized": true,
    "exa_live_authorized": true,
    "exa_authorized_purpose": "benchmark_only_no_training_distillation_or_labeling"
  }
}
```

The operator must also pass `--acknowledge-exa-live-use`. This authorization is
limited to benchmarking. It does not authorize training, distillation, or label
generation. When Exa is not selected, use `exa_live_authorized=false` and
`exa_authorized_purpose="not_applicable"`; passing the runtime Exa
acknowledgment is then an error.

## Manifest requirements

Every v3 manifest must pre-register:

- `corpus_sha256` calculated from the exact ordered task/reference records,
  `time_window_id`, `independent_window_index`, and at least two required
  independent windows;
- `bootstrap_samples` in the range 100 through 1,000,000; execution, offline
  scoring, every pairwise row, and every aggregated window must use this exact
  sealed value;
- either `cold_live` with zero cache age and no prime timestamp, or
  `warm_cache` with a positive cache age and a prime timestamp copied exactly
  from the oldest first-attempt task/provider `completed_at` in the paired
  cold run's canonical `events.jsonl`;
- selected plans, disclosed positive USD request estimates, and sealed budget
  caps;
- an output character limit no greater than Exa's 10,000-character API limit,
  Firecrawl `basic` proxy with PDF parsing disabled, and Clusy JavaScript
  policy;
- the expected Clusy revision, configuration SHA-256, and immutable service
  image digest;
- descriptive task labels, host-derived domain units, content/render classes and
  Firecrawl per-task credit caps;
- independently captured text references and structure references.

`benchmark_id` is capped at 98 characters so the timestamp and digest suffix
cannot produce a run ID longer than the 128-character artifact limit.
`run_id` must exactly equal
`<benchmark_id>-<YYYYMMDDTHHMMSSZ>-<manifest-digest-prefix>`; its timestamp
must match `run.created_at`, and the completed directory name must equal it.

Run claim state is also canonical. A claimable integrity run has no reasons
and an empty watermark. A nonclaimable run has `NONCLAIMABLE` and a nonempty,
unique, canonically ordered subset of exactly four supported reasons: explicit
operator selection, dirty worktree, uncommitted runner, and missing/invalid
container digest. Runner revision and container values are lowercase digests
or the literal `unknown`; invalid generator inputs are canonicalized to
`unknown`, never retained as free text. Explicit operator selection is
mandatory for every nonclaimable artifact, and the runner/container reasons
must exactly reflect their corresponding `unknown` values.

Structure references use:

- headings as `"<level>:<heading text>"`;
- list-item text without the marker;
- fenced-code contents without the fence;
- tables as ordered arrays of rows and cells.

Task URLs are canonicalized for evidence identity. Equivalent URLs differing
only by host case/trailing dot, default HTTPS port, or unreserved percent
encoding are rejected as duplicates. `domain_cluster` is not an operator
label: it must equal the runner's conservative host-derived unit. The runner
uses the final two DNS labels (or the exact public IP), intentionally merging
multi-label public suffixes rather than allowing subdomains to inflate domain
counts.

Use public URLs without credentials, fragments, signed query parameters, tokens
or private data. The reference timestamp and method must describe how the
reference was independently obtained; provider output must never be used as
training data or as its own label.

## Provider request tracks and known noncomparability

For every task, the runner makes exactly one first attempt to each selected
provider. Journal lines follow manifest task order and, within each task, the
sealed randomized provider order. Request timestamp bundles are sequential and
non-overlapping; swapping lines or moving an earlier provider request after a
later one is invalid even when every artifact hash is rebuilt.

- Exa Contents uses `urls`, the provider-default main-content text mode, the
  sealed character cap, matched cache age, and a 60-second live-crawl timeout.
  The cold and warm requests intentionally omit `verbosity`,
  `includeSections`, and `excludeSections`. Exa documents that these filters
  require `maxAgeHours: 0`; including them in a warm-cache protocol would make
  the requested scopes incompatible. Exa warm age must be an exact positive
  number of hours so no rounding changes the protocol. A cold result conforms
  diagnostically only when the per-URL status source reports a crawl; a warm
  result conforms diagnostically only when it reports cache. See Exa's
  [Contents Retrieval documentation](https://exa.ai/docs/reference/contents-retrieval).
  Exa documents the default verbosity as `compact`; Clusy and Firecrawl request
  full main content. Because Exa's `full` and section controls require
  `maxAgeHours: 0`, no honest full-content warm Exa request is available in
  this protocol. Exa quality metrics are labeled
  `provider_default_compact_main_content`, are not comparable to
  full-main-content quality, and the Exa quality/vendor gate is always closed.
  Unused provider-specific `credits` and `fetchAge` diagnostics are not
  retained; Exa dollar accounting uses its bounded reported dollar cost when
  present and otherwise the sealed request estimate.
- Firecrawl v2 scrape requests Markdown main content, matched cache age,
  storage policy, `basic` proxy, ad/cleaning settings, no PDF parser, and a
  60-second timeout. `auto`/`enhanced` are refused because Firecrawl documents
  that they may bill five credits; PDF tasks/parsing are refused because
  billing is per page without a request-side page cap.
  `metadata.cacheState` and top-level `creditsUsed` are parsed only as
  diagnostics because they are not guaranteed by the documented
  single-scrape response contract. They are marked
  `contractually_documented=false` and can never open a public claim. Hard-cap
  accounting always charges at least the sealed per-task credit cap, even when
  a lower `creditsUsed` value is observed. Because neither a contractual
  response cost nor a provider-side per-request spend cap is verified, the
  Firecrawl cost/vendor gate is always closed. Origin failures come from
  `metadata.statusCode`/`error`. Both cold and warm requests set
  `storeInCache=true`. Cold sets `maxAge=0` to bypass reads while writing the
  cache entries used by the paired warm phase; warm uses the sealed positive
  age. See Firecrawl's
  [Scrape reference](https://docs.firecrawl.dev/api-reference/endpoint/scrape).
- Clusy requests one Markdown page with the same age, profile and JavaScript
  policy. Cache, render/model escalation, truncation, quality attempt/success,
  completeness, allow-listed stage timings and origin status are captured as
  derived booleans/numbers when the deployed response exposes them. A
  successful Clusy `cache_state=miss` requires `cache_hit=false`, and `hit`
  requires `true`; Exa and Firecrawl retain `cache_hit=null` because their
  selected cache evidence comes from provider-specific status fields. Clusy
  credits and reported local cost remain canonical zero-USD accounting even
  when a 2xx/error body is empty or malformed, so one bad body records a
  canonical failed event instead of aborting the remaining matrix.

After provider normalization, every selected provider is truncated
deterministically to the same sealed character cap before scoring. This is
applied even when a provider also enforces the cap server-side.

Event evidence is internally fail-closed. `cache_hit`, when present, must
exactly agree with `cache_state`; a cold miss cannot claim `cache_hit=true`.
Successful output requires a complete nonempty 2xx response and nonempty
normalized text. Empty-body/text SHA-256 values must agree with their byte and
character counts. A retained response body requires first-byte timestamp and
latency evidence, while an empty body requires both to be null. `http_error`
requires a real non-2xx status; a null HTTP status is transport-error-only.
Every persisted event has `hard_deadline_enforced=true`, including
nonclaimable runs. Failed outputs retain canonical empty text/scoring evidence,
an unknown cache state, and no output-derived diagnostics. Token and structure
scores are tied to component presence and hard count bounds; a perfect score
cannot be paired with zero or contradictory observed counts. Token
precision/recall/F1 must all recover the same integer token overlap within an
absolute float tolerance of `1e-12`. Heading, list, and code F1 use the same
integer-overlap rule. Table-tree similarity retains bounded observed/reference
tree-token counts (`observed_table_tree_tokens` and
`reference_table_tree_tokens`) and must equal a feasible integer
`SequenceMatcher` match ratio. Candidate tokens and observed heading, list,
code, and table counts are capped by the 10,000-character retained-output
protocol; tree-token counts are capped at 20,000. The structure aggregate
remains the exact mean of its present components under the same `1e-12`
absolute tolerance.

Request timestamps and latency use the same measurement boundary: one UTC
start plus monotonic elapsed time determines first-byte and completion
timestamps. Verification allows at most 5 ms for microsecond
rounding/coherence and enforces the 60-second deadline. Clusy stage components
must sum to no more than `total`, and `total` must not exceed client latency,
with a separate 10 ms cross-clock/reporting tolerance; every stage is also
deadline-bounded. The `/health/version` preflight is bounded to 10 seconds.

## Metrics and gates

Each run reports first-attempt success, time to first byte, end-to-end
p50/p90/p95/p99 latency, output token count, normalized cost, cache
conformance, truncation, render/model rates and:

- Unicode multiset token precision, recall and F1;
- heading, list-item and fenced-code multiset F1;
- ordered table-tree token similarity;
- an equal-weight structural aggregate over reference components that exist.

Paired differences are bootstrapped over the conservative host-derived domain
units. Operator `stratum` labels are descriptive only. Structure evidence
strata are derived from actual heading, list, code, and table component
presence in the retained reference Markdown. Every declared heading, list
item, code block, table, row, cell, order, and value must exactly match
deterministic parsing of `reference.text`; matching presence alone is
insufficient. A single run always has
`vendor_win_claimable=false`.

The descriptive aggregate checks cold and warm runs in every sealed time
window, complete integrity/cache/structure/tail-latency/cost evidence, and for
every selected vendor in every run:

- Threshold: token_f1_paired_tasks >= 100 count. <!-- clusy-protocol-threshold -->
- Threshold: token_f1_paired_domain_clusters >= 30 count. <!-- clusy-protocol-threshold -->
- Threshold: structure_strata >= 3 count. <!-- clusy-protocol-threshold -->
- Threshold: structure_paired_domain_clusters_per_stratum >= 10 count. <!-- clusy-protocol-threshold -->
- Threshold: token_f1_superiority_ci_lower > 0 score. <!-- clusy-protocol-threshold -->
- Threshold: structure_superiority_ci_lower > 0 score. <!-- clusy-protocol-threshold -->
- Threshold: first_attempt_success_delta >= -2 points. <!-- clusy-protocol-threshold -->
- Threshold: paired_end_to_end_latency_ci_upper <= 0 milliseconds. <!-- clusy-protocol-threshold -->
- Threshold: clusy_p95_vendor_ratio <= 1.10 ratio. <!-- clusy-protocol-threshold -->
- Threshold: clusy_p99_vendor_ratio <= 1.10 ratio. <!-- clusy-protocol-threshold -->
- Threshold: paired_normalized_cost_ci_upper <= 0 score. <!-- clusy-protocol-threshold -->
- Threshold: clusy_mean_cost_vendor_ratio <= 1.00 ratio. <!-- clusy-protocol-threshold -->
- Threshold: important_structure_stratum_delta >= -1 points. <!-- clusy-protocol-threshold -->

These performance gates prevent a quality-only result with terrible Clusy
latency or normalized cost from passing. The output contains independent Exa
and Firecrawl diagnostic gates. An unselected vendor always remains false.
Firecrawl also remains closed unless future protocol work adds and verifies
contractual cache/credit evidence. All vendors remain publicly nonclaimable
until a real execution-attestation verifier is implemented or a privacy policy
permits independently rescoring retained outputs.

Window IDs and indices alone never count as independent evidence. Aggregate
timing is derived from canonical manifests, run metadata, request events, and
completion artifacts. Within each pair, the oldest cold first-attempt request
completion must exactly equal the warm manifest's sealed prime timestamp. No
warm request may start until the entire cold run's `completion.json` timestamp,
and both the final warm request and warm completion record must occur no later
than the oldest prime plus the sealed cache age. Using the oldest completion
protects the cache entry with the earliest expiry. Window indices must be
chronological, non-overlapping,
and their cold starts must be at least 86,400 seconds apart. This verifies local
artifact timing, not external preregistration. No external timestamp/notary
proof is implemented, which is another reason public claims remain closed.

Observed accounting is recomputed from the event journal. Reported Exa dollar
cost and conservative Firecrawl credits (the larger of reported credits and
the sealed task cap), as well as Clusy normalized cost, must stay within both
runtime execution caps and sealed manifest budgets. Rehashing a larger
observed value cannot bypass this check.

Smaller pilots are supported for adapter validation, cost estimation and
diagnosis. They still produce summaries and confidence intervals, but remain
descriptive and nonclaimable regardless of their observed point estimates.

## Offline preparation

Do not place credentials in a manifest or command line. Load them from the
operator's secret manager into `EXA_API_KEY`, `FIRECRAWL_API_KEY`,
`CLUSY_CRAWLER_URL`, and optionally `CLUSY_CRAWLER_API_KEY`.
`CLUSY_CRAWLER_URL` is the service root; the runner appends `/crawl` and
`/health/version`. A claimable run also requires `CONTAINER_DIGEST` in
`sha256:<64-lowercase-hex>` form for the benchmark runner. This is separate
from `clusy_binding.expected_image_digest`, which binds the deployed crawler
service image returned by `/health/version`.

Create and manually review a draft JSON, then seal it:

```bash
python bench/live_vendor_benchmark.py seal \
  --input bench/manifests/fixed-url-v3.draft.json \
  --output bench/manifests/fixed-url-v3.sealed.json
```

`seal` calculates `corpus_sha256` from the exact ordered `tasks` array before
it calculates the manifest digest.

Validate it and copy the printed digest for the deliberate runtime
confirmation:

```bash
python bench/live_vendor_benchmark.py validate \
  --manifest bench/manifests/fixed-url-v3.sealed.json
```

The output directory must be outside the repository or contained by
`bench/results` or `bench/artifacts`, and must not grant group/other
permissions.

## Exact reviewed run forms

These commands are templates for a human-reviewed run. Replace every
angle-bracket value with the value from the approved sealed manifest. Running
them spends vendor credits.

Clusy + Firecrawl only:

```bash
python bench/live_vendor_benchmark.py run \
  --manifest bench/manifests/fixed-url-v3.sealed.json \
  --output-root bench/artifacts/live-v3 \
  --repo-root . \
  --execute-paid \
  --confirm-manifest-sha256 <64-hex-digest-from-validate> \
  --max-firecrawl-credits <approved-credit-cap> \
  --max-clusy-usd <approved-clusy-usd-cap>
```

Clusy + Exa + Firecrawl, benchmark-only authorization:

```bash
python bench/live_vendor_benchmark.py run \
  --manifest bench/manifests/fixed-url-v3.sealed.json \
  --output-root bench/artifacts/live-v3 \
  --repo-root . \
  --execute-paid \
  --confirm-manifest-sha256 <64-hex-digest-from-validate> \
  --max-exa-usd <approved-exa-usd-cap> \
  --max-firecrawl-credits <approved-firecrawl-credit-cap> \
  --max-clusy-usd <approved-clusy-usd-cap> \
  --acknowledge-exa-live-use
```

`--nonclaimable` permits a dirty checkout or missing container digest but
watermarks the run; it does not relax URL, credential, budget, compliance,
deadline, revision, or privacy controls.

## Offline scoring and aggregation

Rebuild a descriptive summary from a completed hash-only event journal:

```bash
python bench/live_vendor_benchmark.py score \
  --manifest bench/manifests/fixed-url-v3.sealed.json \
  --events bench/artifacts/live-v3/<run-id>/events.jsonl \
  --output bench/artifacts/live-v3/<run-id>-offline-summary.json
```

`score` verifies the sealed manifest and event retention rules, but it does not
turn a standalone summary into aggregate evidence. `--bootstrap-samples` is
optional and, when supplied, is only an exact confirmation of the manifest's
sealed value; it cannot override it.

After completing cold and warm runs for at least two independent windows,
verify each complete artifact chain and evaluate the descriptive pre-registered
gates:

```bash
python bench/live_vendor_benchmark.py aggregate \
  --run-directory <window-1-cold-run-directory> \
  --run-directory <window-1-warm-run-directory> \
  --run-directory <window-2-cold-run-directory> \
  --run-directory <window-2-warm-run-directory> \
  --output <aggregate-result.json>
```

The command refuses standalone summary JSON. For every run directory it
requires exactly five regular top-level files and rejects all extra files,
directories, and symbolic links. It checks
canonical `manifest.json`, `run.json`, `events.jsonl`, `summary.json`, and
`completion.json`; validates their SHA-256 links and protocol fields; verifies
the complete task/provider matrix; recomputes the retained summary and budget
ledger; and requires an exact match to the stored summary. v3 also enforces
exact allowed-key and value-type schemas for the manifest (including nested
objects), run metadata, every event and nested event object, summary, and
completion record. Adding an unknown raw field remains a hard failure even
after an operator recomputes every local hash. Allowed fields are also checked
for deterministic value coherence, canonical floating-point encodings,
bounded counters/timings, status/body/cache/score relations, and observed
budgets; exact keys alone are not treated as sufficient evidence. Stored
summaries are recomputed byte-for-byte, and completion claim/identity values
must exactly match run metadata and summary. The exact selected-provider ×
manifest-task × first-attempt matrix is mandatory for every completed run.
`--nonclaimable` changes publication eligibility, not execution completeness.

Publish the sealed protocol, selected provider scope, confidence intervals,
failures, cache evidence, tail latency and normalized cost with any descriptive
result. Do not publish “better than Exa/Firecrawl” from the current hash-only
artifacts: the public claim gate is intentionally closed.

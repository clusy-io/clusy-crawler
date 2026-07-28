# Claimable Exa and Firecrawl shadow benchmark

This protocol measures Clusy against Exa and Firecrawl without conflating
search, extraction, crawl coverage, and answer synthesis. It is intentionally
separate from `benchmark_sota.py`, which is an eight-URL smoke test and is not
claimable evidence.

There is currently no independent public benchmark that compares all three
systems end to end under matched production conditions. Run this protocol only
with authorized production-equivalent plans, an approved API budget, and a
frozen runner revision.

## Tracks

### 1. Retrieval only

Use at least 300 private queries stratified across fresh news, documentation and
code, people and companies, research, commerce, multilingual content, and the
long tail.

- Exa: `POST /search`, `numResults=10`, no contents. Measure `instant`, `fast`,
  and `auto` as separate tiers.
- Firecrawl: `POST /v2/search`, `limit=10`, `sources=["web"]`, matched country
  and location, no `scrapeOptions`.
- Clusy: query the search/index plane with the same top-k and filters. Do not
  treat crawler extraction as search.

Report Recall@k, nDCG@10, MRR, authoritative/original-source rate, duplicates,
freshness, error rate, p50/p90/p95/p99 latency, and normalized cost.

### 2. Fixed-URL extraction

Use a sealed operator-owned time and domain holdout plus public WCXB and
WebMainBench regression suites.

- Exa Contents: full text, `maxAgeHours=0`, live-crawl timeout 60 seconds.
- Firecrawl scrape: Markdown, `onlyMainContent=true`, `maxAge=0`, timeout 60
  seconds, `storeInCache=false`.
- Clusy: full Markdown, bypass persistent content cache, with the same region,
  language, byte limit, and deadline.

The cold/live track is primary. Run a separately labelled warm-cache track with
the same allowed age for each provider.

Measure content precision/recall/F1, completeness, boilerplate, heading/list
fidelity, code recall, table recall and TEDS, success rate, output tokens,
end-to-end latency, render/model escalation, and normalized cost.

### 3. Crawl/discovery

Use a controlled, versioned site graph with sitemap and sitemap-index variants,
robots rules, canonical/alternate links, redirects, JS-discovered links,
pagination, infinite scroll, soft 404s, facets, session IDs, duplicate clusters,
calendar traps, 429 `Retry-After`, transient 5xx, and mixed media.

Give every provider the same seed, depth, page limit, subdomain policy, sitemap
policy, rendering policy, and deadline. Score eligible-URL precision/recall,
time-to-50/90/99-percent coverage, useful unique pages per second,
requests/bytes/cost per useful page, duplicate-fetch ratio, trap amplification,
JS escalation, host fairness, and robots violations.

### 4. Query-aware context and answer

Freeze the query, candidate URLs, synthesis model, prompt, temperature,
500/2,000/8,000-token budgets, tool-call cap, timeout, and citation format.
Grade retrieved context separately from answer correctness and citation
support. Repack provider-native highlights to the same budget. Provider-native
agent or deep modes belong in a separate, explicitly non-causal product track.

## Dataset construction

1. Sample URLs and queries from the real deployment workload before inspecting
   any provider output.
2. Remove private data and obtain any required evaluation consent.
3. Freeze task IDs, strata, requested scope, reference method, and exclusions in
   a time-stamped manifest.
4. Reserve both time-based and domain-based holdouts. Neither routing thresholds
   nor prompts may be tuned on them.
5. Add canaries and exact/near-duplicate audits. Block benchmark-answer hosts
   during sealed answer evaluations.
6. Use blinded human judgments or independently derived source references. Use
   at least two blinded LLM judges only as a secondary signal, with human
   calibration and disagreement reported.

Public WebMainBench labels are downloadable in one split, so it is a valuable
stress/regression suite rather than a contamination-proof launch gate.

## Execution controls

- Run from an immutable container and clean source revision.
- Match runner region, country/location, language, timeouts, requested scope,
  freshness, and cache state.
- Randomize provider order for each task.
- Run at least five seeds or time windows.
- Preserve first-attempt errors; report retries separately.
- Count timeouts, blocks, empty results, and malformed outputs as failures.
- Hash and retain raw responses before normalization.
- Apply one versioned normalizer and scorer to all providers.
- Pre-register launch thresholds and exclusions.
- Use paired bootstrap 95% confidence intervals, including per-stratum results.
- Publish the quality/latency/cost Pareto frontier rather than one opaque score.

The implementation foundation is
[`live_vendor_benchmark.py`](live_vendor_benchmark.py). It accepts only a
SHA-256-sealed/frozen manifest, makes no request without the explicit
`--execute-paid` flag and required credentials, refuses claimable execution
from a dirty or uncommitted runner, persists hashed raw response artifacts, and
emits deterministic paired bootstrap summaries without declaring a winner.
Use its offline `seal`, `validate`, and `score` commands before authorizing a
live run. The Exa adapter uses `Authorization: Bearer`, `urls`,
`text={"verbosity":"full"}`, `maxAgeHours=0`, and
`livecrawlTimeout=60000`, with no character cap and explicit full verbosity so
compact output is not mistaken for a completeness failure. It deliberately
omits the deprecated `livecrawl` parameter and treats an error in Exa's per-URL
`statuses` array as a failed first attempt. `country` and `location` must be
null in this track because provider-side fetch geography cannot be controlled
consistently across all three APIs; the runner records the caller's
`runner_region` separately and does not claim matched fetch geography.

In the v1 summary, the legacy top-level `claimable` field is explicitly scoped
to `artifact_integrity_only`: it means the sealed first-attempt matrix and
provenance checks are complete. `vendor_win_claimable` remains false. The
runner reports provider p50/p90/p95/p99 distributions and per-stratum paired
estimates, but it intentionally cannot open the vendor-win gate until versioned
structural references (including TEDS), a matched warm track, and a second
independent time window are present.

## Required event record

Each request record must contain:

```text
run_id, task_id, provider, endpoint, mode, plan
api_version, sdk_version, runner_commit, container_digest
utc_timestamp, runner_region, country, location
query_or_seed, top_k, limit, depth, scope, domain_filters
cache_policy, max_age, content_format, token_budget
timeout, retry, randomized_order
started_at, first_byte_at, completed_at, status, error
provider_request_id, cache_hit, fetch_age, credits, normalized_cost
raw_response_sha256, immutable_artifact_path
```

Each result additionally records rank, original and canonical URL, title,
snippet/highlights/text, character and token counts, publication/fetch
timestamps, citation links, and any provider score.

## Launch gates

Before claiming a vendor win:

- extraction quality must be higher on the sealed fixed-URL set with a paired
  95% confidence interval excluding zero;
- success rate must not regress;
- p95 and p99 must be reported for cold and warm tracks;
- cost, render rate, model rate, and output-token scope must be normalized and
  disclosed;
- no important page type, language, or rendering stratum may be hidden by the
  aggregate;
- the same conclusion must hold in at least two independent time windows.

“Better than Exa/Firecrawl” is only valid for the exact layer, mode, scope, date,
and metrics that pass these gates.

## Primary provider references

- [Exa search API](https://exa.ai/docs/reference/search-api-guide)
- [Exa Contents API](https://exa.ai/docs/reference/contents-api-guide)
- [Exa pricing](https://exa.ai/pricing)
- [Firecrawl search API](https://docs.firecrawl.dev/api-reference/endpoint/search)
- [Firecrawl scrape API](https://docs.firecrawl.dev/api-reference/endpoint/scrape)
- [Firecrawl pricing](https://www.firecrawl.dev/pricing)

# Clusy crawler: evidence-driven SOTA architecture

Status: architecture decision record, 2026-07-29.

This document defines the architecture and release gates for making Clusy a
best-in-class crawler for self-hosted use and downstream platforms. It does not
claim that the current implementation is universally state of the art.

## Delivery status

Implemented foundations in the current worktree:

- deterministic `balanced`/`article_body` fast paths plus opt-in
  `adaptive`/`quality` profiles, bounded model runtime, a deterministic
  completeness comparator, verifier, and fallback;
- GitHub and scholarly specialists;
- opt-in recursive crawling with deterministic fair frontier, canonical
  deduplication, trap/page/depth budgets, and fail-closed RFC 9309-style robots
  enforcement; completed requests replenish network capacity without allowing
  response completion order to reorder results;
- the benchmark-pinned Rust `ordered-dom-ir.v1` interface remains available;
- additive, bounded native and Python `ordered-dom-ir.v2` APIs now expose a
  stable ordered element/text graph, source-span reliability and truncation
  provenance, typed table/list/code/math relations, exact preformatted-code
  whitespace, and deterministic full or ID-selected Markdown serialization.
  Selected text is closed under the minimum ancestor structure, and unknown IDs
  never broaden the selection. These APIs do not replace the production
  extractor path;
- a deterministic v2 refiner now aligns an existing Markdown candidate to
  source text in DOM order, promotes grounded structures, reconstructs through
  the native serializer, and rejects fail-closed to the byte-identical input.
  It is explicitly shadow-only and is not imported by the extraction cascade;
- the article backend now serializes only outermost selected source roots in
  temporary JSON-LD/Discourse DOMs, preventing one source subtree from being
  emitted through both an ancestor and a descendant while retaining legitimate
  equal text from disjoint source nodes;
- a backend-neutral render manager now owns lifecycle, readiness, and dispatch
  for the existing isolated local Playwright backend. Defaults are unchanged;
  the contract creates a safe seam for a separately sandboxed render plane
  without adding an unsafe browser launch mode;
- AEB, WCXB, Webis, and WebMainBench regression/evidence harnesses;
- a sealed fixed-URL Exa/Firecrawl comparison runner whose artifact-integrity
  gate is separate from its deliberately closed vendor-win gate. Vendor output
  is benchmark-only and never enters training, distillation, labels, routing,
  or production extraction.

The completed 545-page shadow diagnostic tested both the refiner's default
acceptance rule and a stricter normalized-lexical-token-sequence rule. Both
improved code and table structure metrics and aggregate score, but both
regressed text and formula scores:

| Shadow policy | Overall | Text | Code | Formula | Table edit | Table TEDS |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Baseline | `0.215484` | `0.758002` | `0.019236` | `0.300180` | `0` | `0` |
| Default refiner | `0.280215` | `0.749641` | `0.084351` | `0.290650` | `0.110996` | `0.165437` |
| Normalized lexical token sequence | `0.227423` | `0.755028` | `0.054644` | `0.293024` | `0.011982` | `0.022439` |

This was a development-only, non-claimable shadow run. Its acceptance decisions
used only source HTML and the deterministic candidate; public references were
revealed to the scorer only after both predictions existed. The regressions
fail the monotonic promotion requirement, so neither policy is wired into
runtime.

Still required before a broad SOTA or vendor-win claim:

- finish the v2 representation and serializer promotion gates, then train and
  independently validate a multilingual block selector and boundary refiner;
- wire any candidate only after clean shadow evidence passes the verifier,
  quality, latency, cost, and circuit-breaker gates;
- replace the process-local frontier with a transactional durable queue for
  resumable, multi-worker crawls beyond the bounded request API;
- create a separately consented, operator-owned domain/time corpus, calibrate
  the utility router, and pass multilingual/structure/latency/cost holdouts;
- reproduce the fixed-corpus evidence directly from a recorded OSS commit and
  run an authorized, preregistered live Exa/Firecrawl study.

No selector checkpoint, semantic model, deterministic v2 refiner policy, or
Exa/Firecrawl vendor-win claim has passed its production promotion gates.

## Decision

Clusy will use a cascaded, uncertainty-routed extraction system:

1. Fetch once, with bounded static and browser pools and an explicit freshness
   contract.
2. Route known source families to deterministic specialists.
3. Run the Rust extractor for the general case.
4. Estimate extraction risk from label-free document and output signals.
5. Escalate only risky pages to a compact hierarchical selector: block
   classification first, then source-offset boundary refinement only where a
   block mixes main and auxiliary text.
6. Verify the model output and fall back to the deterministic result on errors
   or implausible output.
7. Return completeness, provenance, cache, render, and routing metadata so the
   caller can make a safe fallback decision.

This preserves the current fast path while adding model capacity where
deterministic heuristics are weakest. A model-first pipeline would impose model
cost and latency on every page. A rules-only pipeline is unlikely to close the
large quality gap on heterogeneous pages.

```text
request
  |
  v
admission control -> canonical URL -> bounded process-local crawl frontier
                                      |
                                      +-> robots/policy + trap budgets
  |
  v
fetch plane: static HTTP -----> render escalation -----> source adapter/API
  |
  v
document graph / ordered block IR
  |
  +--> GitHub, paper, repository, and other deterministic specialists
  |
  +--> Rust general extractor
           |
           v
     risk and disagreement scorer
        |                 |
      low risk          high risk
        |                 |
        |          compact hierarchical selector
        |          (blocks -> boundary spans)
        |                 |
        +------ verifier / safe fallback
                         |
                         v
            Markdown + content contract + telemetry
```

Search and extraction are separate products. The crawler can replace a vendor
content-fetch path, but beating Exa's search product also requires an index,
ranking, freshness, and retrieval plane. Search quality must not be inferred
from extraction benchmarks.

The crawl frontier and the extractor are also separate state machines. The
frontier decides *which URL may run next* under scope, fairness, retry, and trap
budgets. The extractor decides *what content a fetched document contains*.
Keeping those decisions separate makes both layers replayable and prevents
model latency from weakening crawl politeness or host fairness.

## Evidence behind the decision

The current AEB result ran directly from clean public source revision
`4dd1755e9b425c80193982bc6609c06444cf30d5`; both WCXB profiles ran directly
from clean public revision `9c7cc0a84f240910ff764baae75824e269d08350`.
Webis and WebMainBench remain clean private-revision `a19ae17` diagnostics and
are not represented as OSS runs. The broad native path embeds
`web-page-classifier` 0.1.0. Its publisher reports 1,497 training pages across
seven page types but publishes no item/split manifest; WCXB development has
exactly 1,497 pages across the same types. This is strong overlap risk, not
proof of overlap. Together the rows establish reproducible public-benchmark
evidence, not blind, universal, or vendor-comparative SOTA:

| Benchmark | Scope | Current result | Interpretation |
| --- | --- | ---: | --- |
| AEB `article_body` | 181-page article body | P/R/F1 `0.955147 / 0.989721 / 0.972127`; `142.306` pages/s; p50/p95 `12.820 / 25.564 ms` | Direct clean OSS run; ΔF1 `+0.014624` versus Trafilatura 2.0, CI `[+0.005346, +0.025342]` |
| AEB `balanced` | Same 181 pages, general-main-content profile | P/R/F1 `0.928435 / 0.989588 / 0.958037`; `215.265` pages/s; p50/p95 `8.230 / 16.408 ms` | Direct clean OSS run; below pinned `rs-trafilatura` |
| WCXB `balanced`, development | 1,497 pages, seven page types | P/R/F1 `0.852732 / 0.898934 / 0.848433`; `56.58` pages/s; p50/p95 `110.345 / 341.527 ms` | Direct clean OSS public-label diagnostic |
| WCXB `balanced`, public test | 511 pages, seven page types | P/R/F1 `0.894822 / 0.928969 / 0.891727`; `106.96` pages/s; p50/p95 `68.243 / 124.028 ms` | `0.001273` below the pinned upstream point result `0.893` |
| WCXB `balanced`, combined | 2,008 pages | P/R/F1 `0.863443 / 0.906577 / 0.859450`; `64.28` pages/s | Sequential split aggregate; not comparable with a development-only headline |
| WCXB `adaptive`, development | 1,497 pages, seven page types | P/R/F1 `0.844912 / 0.912670 / 0.852667`; `48.54` pages/s; p50/p95 `121.006 / 424.284 ms` | ΔF1 `+0.004235` versus `balanced`; paired 95% CI `[-0.000459, +0.009298]` |
| WCXB `adaptive`, public test | 511 pages, seven page types | P/R/F1 `0.895244 / 0.942960 / 0.901714`; `78.62` pages/s; p50/p95 `82.501 / 227.714 ms` | ΔF1 `+0.009987`, CI `[+0.003424, +0.017720]`; `+0.008714` versus upstream is unpaired |
| WCXB `adaptive`, combined | 2,008 pages | P/R/F1 `0.857721 / 0.920378 / 0.865149`; `53.78` pages/s | ΔF1 `+0.005699`, CI `[+0.001795, +0.009833]`; sequential split aggregate only |
| Webis | 3,985-page main-content extraction | macro ROUGE-LSum P/R/F1 `0.867650 / 0.908477 / 0.855327`; macro Levenshtein `0.850216`; `306.09` pages/s | Private `a19ae17`; `ARCHIVAL_REPRODUCIBLE`; below Trafilatura and the published ensemble |
| WebMainBench v1.1 raw | 7,809-page broad Direct-MD | P/R/F1 `0.615569 / 0.677841 / 0.606672`; `113.02` pages/s | Private `a19ae17` fixed-public diagnostic; below modern semantic-model systems |
| WebMainBench v1.1 scrubbed | 7,809-page broad Direct-MD | P/R/F1 `0.615698 / 0.676570 / 0.605703`; `55.05` pages/s | Private `a19ae17` annotation-contamination diagnostic; zero extraction errors |
| WebMainBench fine-grained | Text/code/formula/table | Overall `0.214089` | Dirty, non-claimable diagnostic; text `0.752301`, code `0.017775`, formula `0.300369`, table/TEDS `0` |

| Suite | Artifact identifier | `manifest.json` SHA-256 |
| --- | --- | --- |
| AEB `article_body`, public `4dd1755` | `bench/results/aeb/20260729T110311Z` | `2f7b61af148387c93ff6381fee5fad663a1a4e731d79d653568da4a656784fc1` |
| AEB `balanced`, public `4dd1755` | `bench/results/aeb/20260729T110342Z` | `4f58b2ff7e55ba017c3fdfae1bb4da3bffab48b67a55f8a458b68de04d81aa70` |
| WCXB `balanced`, public `9c7cc0a` | `bench/results/wcxb/20260729T-oss-balanced-9c7cc0a` | `627995ebc1c9e2005a88b8b007a3e56e2eb04ab9994f6ed3a78834a1958407a8` |
| WCXB `adaptive`, public `9c7cc0a` | `bench/results/wcxb/20260729T-oss-adaptive-9c7cc0a` | `c02cccf91d77540de9e52a795285abcfe9baae244edf236a5e28b70b056908ba` |
| Webis, private `a19ae17` | `bench/results/webis-v3-a19ae17-20260729T1028Z` | `4fae36a0d91b369f1858f029b04e005d8ba4372d67001060a1e3b8106c5e626f` |
| WebMainBench, private `a19ae17` | `bench/results/webmainbench/20260729T101852Z` | `08530d7bc3e15cabdc94a2b405a996e6d277880cee8b9b3913d0c19c5ef04991` |

The WCXB artifacts directly bind public source `9c7cc0a`, and both reproduced
the separately captured private deployment-path predictions byte for byte.
The `adaptive` artifact changed 83 outputs versus `balanced`, with 41 wins, 34
losses, and 1,933 ties under official per-page F1. Its 10,000-replicate paired
bootstrap used fixed seeds 20260729/20260730/20260731. The two profiles ran
sequentially rather than as a randomized performance experiment; in these runs
`adaptive` traded about 16.3% combined throughput for its quality gain.

The WCXB harness correctly closes the unseen claim gate because the embedded
classifier lacks an auditable training-item manifest. AEB `article_body` is
scoped public-benchmark evidence; broad-profile rows are public diagnostics.
Webis is archival-reproducible, and the private Webis/WebMainBench rows must not
be attributed to an OSS commit. All throughput values are local closed-loop
measurements on artifact-recorded hardware with local-corpus inputs, not HTTP
service or live-web throughput. WebMainBench's Direct-MD contract also differs
from the leaderboard's `HTML+MD` conversion path.

The two clean WebMainBench runs reproduced every page's prediction, strategy,
score, error, and metadata exactly after excluding only measured latency.
Their local throughput ranges (`127.4899`–`131.2818` pages/s raw and
`57.3376`–`57.8572` pages/s scrubbed) are reported rather than selecting the
fastest observation as a universal rate.

The separate fine-grained row remains a development/architecture diagnostic,
not part of the clean four-suite evidence. Its dirty artifact matched only 24
of 44 recorded source files against public `837ddda`, including differences in
the harness and production runtime. It had zero extraction errors, but its
structure scores make the current ceiling unambiguous: flattened prose cannot
be repaired into reliable code, equations, and tables after extraction. The
v2 refiner likewise remains shadow-only and unwired because it failed its
monotonic promotion gate.

The full 7,809-page `ordered-dom-ir.v1` label oracle reaches only F1
`0.873146` (precision `0.935255`, recall `0.855861`) even though public
ground-truth marker attributes directly choose blocks. Its ground-truth
recanonicalization canary is exact on all 7,809 pages, so the result is not a
scorer or converter-state artifact. Only 90.91% of labelled markers and 85.06%
of labelled characters are selectable; reconstruction emits 87.21% and 78.88%
respectively. There are 3,164 pages with unrepresented labelled markers, 1,750
with unrepresented table/list markers, and 1,420 with IR truncation. Because
this oracle is below the `0.9098` objective before any classifier error, v1
must not be trained or wired. These label-derived diagnostics are architecture
ceilings only and are explicitly nonclaimable as a system or leaderboard
score.

On the current WebMainBench artifact, low-confidence pages and non-article page
types account for disproportionate error. Yet confidence alone is not
sufficiently calibrated: many high-confidence pages are still poor. This
supports multi-signal uncertainty routing, not a single confidence threshold.

A full-corpus, non-claimable diagnostic that appended recovered tables after
the native output reduced aggregate F1 from `0.606672` to `0.599021` and the
table-page slice from `0.507515` to `0.491307`. That negative result is why the
target design reconstructs selected blocks in their original DOM order instead
of bolting structures onto flattened text.

With the initial adaptive defaults, a label-free replay over WebMainBench marks
`4,165 / 7,809` pages (53.3%) as risky. Their existing deterministic mean F1 is
`0.4881`, versus `0.7422` on the retained fast path, and the risky set contains
78.7% of pages below F1 `0.4`. The separation is useful, but the escalation rate
is too high to call the router production-calibrated. Public labels are used
only for this retrospective diagnostic; thresholds must be selected on a
separate, operator-owned calibration set and governed by a production budget.

Routing alone cannot create an absolute WebMainBench win: even a hypothetical
perfect result on every currently risky page, combined with the unchanged safe
subset, would score only about `0.88` by page-average F1. The ordered IR and
deterministic path therefore must improve as well, while the model-first
`quality` profile remains the absolute-quality lane. `adaptive` is the
quality/latency Pareto lane, not a substitute for measuring `quality`.

WebMainBench's published leaderboard reports `0.9001` for MinerU-HTML-v1.1 and
`0.9098` for a DeepSeek-V3 pipeline. MinerU-HTML and Dripper both simplify HTML,
classify semantic blocks, then reconstruct selected original content. Clusy
should adopt that architectural pattern without putting a large model on the
fast path.

Pulpie provides newer, independent evidence for the same serving shape. Its
authors report `0.862` ROUGE-5 F1 on the 6,647-page English WebMainBench subset
for a 210M encoder at 13.7 pages/s on an L4, using 8,192-token block chunks and
source reconstruction. Those are author-reported subset results, not a Clusy
reproduction or a full-corpus leaderboard comparison. The released Pulpie
weights are also CC BY-NC 4.0, so they are a useful architectural reference but
cannot be the project's commercially usable default.

Recent controlled pretraining work also finds that different deterministic
extractors retain complementary pages and structures: a union increased usable
token yield by up to 71%, while table and code handling materially changed
downstream task quality. Clusy should therefore measure candidate disagreement
and select or merge at the *block* level. It must not concatenate whole
extractor outputs, which creates duplicates and destroys document order.

Primary references:

- [MinerU-HTML](https://github.com/opendatalab/MinerU-HTML)
- [Dripper paper](https://arxiv.org/abs/2511.23119)
- [WebMainBench dataset](https://huggingface.co/datasets/opendatalab/WebMainBench)
- [WCXB paper](https://arxiv.org/abs/2605.21097)
- [Beyond a Single Extractor](https://arxiv.org/abs/2602.19548)
- [Pulpie Orange Small model card](https://huggingface.co/feyninc/pulpie-orange-small)
- [mmBERT model card](https://huggingface.co/jhu-clsp/mmBERT-base)
- [mmBERT paper](https://arxiv.org/abs/2509.06888)
- [Qwen3.5-0.8B-Base model card](https://huggingface.co/Qwen/Qwen3.5-0.8B-Base)

## Target pipeline

### 1. Fetch plane

Use separate bounded pools for static HTTP and browser work. Static HTTP remains
the default. Escalate rendering only on observable signals such as an empty app
shell, script-dominant body, challenge page, or an explicit caller request.

The fetch result must record:

- canonical and final URL;
- HTTP status and content type;
- cache age and freshness policy;
- static, rendered, adapter, or upstream strategy;
- truncation and byte limits;
- robots/policy decision;
- timing for queue, network, render, and extraction.

### 2. Crawl frontier and discovery

Recursive crawling uses a deterministic, persistence-friendly frontier rather
than recursively spawning fetch tasks. Its canonical URL is conservative:
fragments, default ports, dot segments, IDNA, and safe percent escapes are
normalized, while query order and duplicate keys are preserved because they
can change server behavior.

Each crawl has explicit global, per-host, depth, attempt, and response budgets.
The frontier provides:

- exact-host scope by default, with an explicit safe subdomain option;
- priority within fair host round-robin scheduling;
- per-host cooldown and `Retry-After`-aware exponential retry;
- duplicate admission and stale-lease protection;
- bounded query variants, facets, calendar patterns, repeated path segments,
  and session-ID rejection;
- terminal reasons and immutable snapshots suitable for a transactional
  Postgres/Redis queue.

The current opt-in recursive owner performs a robots preflight for every leased
seed and discovered URL before its page fetch. It selects exact configured
product-token groups with `*` fallback, merges duplicate groups, and applies
longest-match `Allow`/`Disallow` rules with wildcard and end-anchor support.
Policy fetches have independent body, line/rule, timeout, concurrency, redirect,
cache-entry, and TTL bounds. Redirects are manual, SSRF-validated on every hop,
post-connect peers are checked when the transport exposes them, and HTTPS
downgrades are refused. Missing and ordinary 4xx files allow; 408/425/429, 5xx,
timeout, network/validation, oversized, redirect, and complexity failures deny
briefly. There is no caller bypass. Blocked pages remain visible as result
errors and terminate as `robots_disallowed`.

The same recursive policy callback runs before every static redirect target and
every Playwright `document` request/navigation, including same-origin path
redirects. It rejects destinations outside the frontier's exact-host/subdomain
scope before checking robots, and denied targets receive no page request.
Secondary academic landing/PDF and GitHub raw document fetches use the same
callback. A denial on one of those optional secondary documents falls back to
the already-fetched root HTML/PDF; a denial on the root document or redirect
remains terminal. Cached effective URLs are re-checked before return, while
policy-aware singleflight work is partitioned from flat work so a recursive
caller cannot join an unguarded fetch.

DNS rebinding/SSRF enforcement, content-policy checks, and redirect validation
remain fetch-plane responsibilities and are applied on every document hop.
Browser subresources retain SSRF enforcement but do not trigger robots lookups;
robots policy controls document retrieval, and per-asset checks would multiply
origin traffic without widening the crawled document set.

### 3. Ordered document graph

`ordered-dom-ir.v1` remains a benchmark-pinned diagnostic interface, not the
production schema. It copies `node.text()` and `try_html()` per block,
suppresses descendants under atomic blocks, repeatedly scans descendant
subtrees, and exposes only a 512-block prefix to its default classifier. It
therefore is neither truly source-offset-backed nor asymptotically suitable for
adversarial nested DOMs.

The current worktree implements a separate, additive `ordered-dom-ir.v2` native
API. It provides stable source-order element and text-run IDs, parent/child
relationships, bounded source-span provenance, explicit completeness and
truncation signals, typed tables/lists/code/math, and deterministic full or
ID-selected serialization under `ordered-dom-ir.v2.markdown.1`. It preserves
exact preformatted whitespace, closes selected runs under necessary ancestors,
and never treats an unknown ID as a request for broader content. V1 remains
unchanged.

The v2 APIs are primitives, not a promoted extraction route. The deterministic
refiner built on top of them remains unwired because its two 545-page shadow
policies gained structure at the cost of text quality. The production target
extends and validates these primitives as one decoded source buffer and
reference-only graph records:

```text
DocumentV2
  SourceBlob(source digest, bytes/UTF-8, charset and decode map)
  Node[](stable source-span ID, DOM/source order, parent/children, raw spans)
  TextRun[](source SpanSet, normalized↔source map, inline path, flags)
  Block[](run references, semantic kind, bottom-up features)
  Structure[](Table, List, Code, Math, Figure)
  Chunk[](model core plus bounded context halo)
```

All direct text and tail content becomes a leaf `TextRun`; whole-block `KEEP`
is only shorthand for a set of runs. A source-spanned HTML5 tokenizer/tree
builder must carry offsets through malformed markup, entities, foster
parenting, and synthetic nodes. The original decoded source is stored once
behind an opaque native handle—blocks never copy complete text or HTML. Nodes
such as scripts, SVG, hidden content, and MathML remain in the auditable graph
with exclusion/visibility flags instead of being physically deleted.

Features are computed in one iterative postorder pass. The hard complexity
contract is `O(source bytes + nodes + runs + structures)` time and memory;
per-block `text()`, `try_html()`, growing-payload retokenization, and descendant
rescans are forbidden.

Each model-visible block needs:

- stable block and parent identifiers;
- DOM order and depth;
- semantic tag and ARIA role;
- text and link density;
- repeated-template and navigation features;
- table, code, equation, list, media, and heading payloads;
- visibility and geometry when rendered;
- deterministic main-content score.

Both deterministic and model extractors must eventually select from this same
graph. The target serializer then reconstructs selected original blocks in DOM
order. This avoids the failure mode where tables or code are appended out of
position and gives a future semantic model a compact, safe input.

A block-only graph is not a sufficient final representation. Real pages can
mix an article title with a comment count in one heading, or target text with
navigation in one layout-table cell. The IR therefore also retains stable
UTF-8 source offsets for normalized text runs and their inline ancestor path.
Most pages still select whole blocks. Only ambiguous boundary blocks enter a
second constrained token/text-run labeller whose output is a set of valid
source ranges. Reconstruction may copy those original ranges and their inline
formatting; it may never synthesize replacement text. This hierarchical
granularity avoids exploding every anchor and span into a global model token
while removing the representational ceiling of whole-block selection.

Structures have typed, deterministic serializers:

- tables retain a sparse row/cell grid, `rowspan`/`colspan`, headers/scope,
  caption, rowgroups, nesting, and a data-vs-layout score; simple rectangles
  become GFM, complex tables use sanitized source-backed HTML, and layout
  tables unwrap selected runs;
- code retains exact whitespace, language, inline/block kind, and a safe fence
  longer than any contained backtick run;
- math retains MathML and source-present TeX from annotations, KaTeX/MathJax,
  `data-latex`, or math scripts; the model never invents equations;
- lists retain ordered start/value, depth, task state, and nesting; figures
  retain source-order alt/caption/ARIA payloads.

Long pages are fully covered, never prefix-truncated. An 8,192-token encoder
chunk uses roughly 7,168 core tokens plus at most 512 tokens of halo on either
side, preferentially cutting at section/block boundaries. Every unit appears
exactly once as core and total halo stays at or below 15%. Ordinary
tables/code/math are atomic; oversized structures split by row, code line, or
text run under one structure ID. Chunk embeddings feed a small whole-page
sequence head/CRF so independent chunks cannot make contradictory boundaries.

`SelectionV2` contains only the source digest and parser/tokenizer/model
revisions, `KEEP`/`DROP`/`REFINE` block RLE, structure actions, sorted
non-overlapping source-backed spans, and a coverage digest. It never contains
generated page text. Any invalid digest, boundary, ancestor closure, duplicate,
structure action, or output budget rejects the whole semantic result and
returns the deterministic fallback; partial-prefix reconstruction is forbidden.

The pre-training gates are intentionally strict:

- full 7,809-page v2 label-oracle marker and character coverage at least
  `99.9%`, zero representation truncation, overall F1 at least `0.985`, and
  Hard/Table at least `0.97`;
- fresh ground-truth canonicalization exact on 7,809/7,809 pages and identical
  forward, reverse, and shuffled-corpus results;
- fine-grained structure oracle overall at least `0.90`, code/formula at least
  `0.95`, and table edit/TEDS at least `0.85`;
- doubling deep or wide DOM size grows runtime and RSS by less than `2.5x`;
  parse+pack p95 is at most `1.25x` v1 and representative single-core
  throughput is at least 100 pages/s.

No selector training or production wiring starts until these representation,
serializer, determinism, and complexity gates pass.

### 4. Specialist adapters

Source-family adapters run before generic extraction when their contracts are
more reliable. GitHub repositories and academic pages are initial specialists.
Adapters must share the same response contract, limits, cache policy, and
telemetry as generic extraction. A failed or incomplete adapter falls through
to the general path.

### 5. Deterministic fast path

The Rust extractor remains the default general extractor. The article-body
profile stays isolated because its AEB behavior is a regression gate. The
balanced profile remains deterministic and preserves the additive API shape;
V2 deliberately changes route, cache, completeness, and provenance semantics.

### 6. Risk router

The adaptive profile first computes the deterministic result, then decides
whether to escalate. Initial label-free signals should include:

- extractor confidence and page type;
- selected-text to visible-text ratio;
- heading, list, table, code, and link preservation;
- repeated-block and navigation density;
- DOM complexity and candidate disagreement;
- empty, extremely short, extremely long, or boilerplate-heavy output;
- source-adapter completeness.

Thresholds are configuration, versioned into cache keys, and calibrated on a
separately consented, operator-owned development corpus. Public benchmark
labels must never enter production routing or post-processing.

The production router should predict *incremental utility*, not merely whether
a page looks unusual. A small calibrated structural model consumes the signals
above plus deterministic-candidate disagreement and estimates both
`P(deterministic failure)` and the expected quality gain from the encoder.
`adaptive` escalates only when expected gain clears a versioned GPU/latency
budget; `quality` ignores that economic gate and takes the semantic path.
Thresholds are calibrated per language/page-type stratum with a global
escalation cap and a protected rare-stratum floor. Shadow logs retain the
counterfactual deterministic result so calibration can be audited without
public-label feedback. If confidence calibration drifts, the router falls back
to the deterministic lane rather than silently expanding model spend.

### 7. Compact semantic quality path

The preferred model path is a locally served hierarchical selector in the
MinerU-HTML/Dripper family. Stage one consumes the compact block sequence and
returns selected block IDs or constrained labels. Stage two runs only on
uncertain boundary blocks and returns BIO labels or start/end offsets over
source-backed text runs. Neither stage returns free-form page text. This keeps
tokens, hallucination surface, latency, and reconstruction error bounded while
allowing precise mixed-block boundaries.

An OpenAI-compatible frontier model endpoint could be evaluated as a separately
licensed fallback or teacher, but it is not the desired per-page production
default and none has passed promotion. Batching, connection reuse, deadlines,
concurrency caps, and a circuit breaker are required. This prospective model
work is separate from vendor benchmarking: output captured from Exa or
Firecrawl by the comparison runner is never used for training, distillation,
labeling, synthetic labeling, or routing calibration.

Architecture and weights are separate decisions. MinerU-HTML's code is
Apache-2.0, while its official v1.1 compact weights are Hunyuan-derived and the
model license excludes use in the EU, UK, and South Korea and restricts using
outputs to improve another model. Those weights are useful as a reference
implementation only where licensed; they are not suitable as a broadly
deployable default. Production promotion requires a broadly deployable
checkpoint and documented base-model, training-data, synthetic-data, and
output rights.

For a future project-trained checkpoint, the leading serving candidate is
`jhu-clsp/mmBERT-base`: its official model card reports an MIT license, 307M
parameters, 8,192-token context, and pretraining across more than 1,800
languages. An encoder is a better default fit for bounded per-block labels than
an autoregressive decoder: every chunk is classified in one forward pass, and
long pages are already handled by deterministic chunking. `mmBERT-small`
(140M) is the distillation/throughput candidate.

`Qwen3.5-0.8B-Base` remains the higher-capacity constrained-decoder and teacher
candidate: its official model card reports an Apache-2.0 license, 201-language
coverage, and a native 262,144-token context. It may win on ambiguous boundary
refinement, but the long advertised context does not justify paying decoder
latency on ordinary pages. Neither candidate is bundled or selected by this
ADR. Training-data, synthetic-label, and output rights still require review
even when the base checkpoint license is permissive.

The promotion experiment is therefore explicit: fine-tune the same
domain/time-separated labels on mmBERT-base block classification, an
mmBERT-small distilled variant, and the constrained Qwen candidate. Select by
the Pareto frontier of held-out quality, GPU-seconds/page, peak memory, p95
latency, and multilingual worst-stratum quality—not aggregate quality alone.
Pulpie's non-commercial weights may be used only as a published reference
point unless the operator obtains separate commercial rights.

The model is fine-tuned as a constrained selector:

- input is the bounded ordered block sequence, never raw unbounded HTML;
- the first output is a grammar-constrained run-length encoding of valid block
  IDs plus an explicit set of boundary-refinement block IDs;
- the optional second output is constrained to valid text-run IDs and source
  offsets inside those blocks;
- the loss weights main/other imbalance, start/end boundary accuracy, structure
  coverage, and false-positive boilerplate separately;
- inference returns block/span scores and source-backed selections, while the
  deterministic serializer alone produces Markdown;
- dynamic batching groups compatible token lengths; quantized and full-precision
  checkpoints are evaluated separately rather than assuming quantization is
  free.

Long pages use hierarchical chunking with overlap and deterministic merge
rather than sending the advertised maximum context on every request. The
multilingual encoder is the serving prior; a decoder is promoted only if its
incremental held-out quality justifies its GPU-seconds, memory, and tail latency
within a separately budgeted quality lane.

Public benchmark pages and labels remain evaluation-only. Training and routing
calibration use a separately consented, operator-owned workload sample with
node-level human labels, hard negatives, and licensed teacher assistance.
Splits are by registrable domain and time, not random page, so templates cannot
leak between train and validation. A sealed domain/time holdout, near-duplicate
audit, canaries, label provenance, and model/data hashes are release artifacts.
Active learning prioritizes pages with deterministic/model disagreement,
uncertain boundaries, rare languages, tables/code/formulas, rendering, and
production fallbacks.

### 8. Verification and fallback

Model output is accepted only if it references valid blocks and passes
deterministic checks for minimum content, duplication, ordering, and preservation
of salient structures. Timeout, overload, invalid labels, or implausible output
returns the already-computed deterministic result. Adaptive extraction therefore
must not reduce availability.

### 9. Integration contract

Downstream applications should expose one stable extraction capability:

- Clusy handles configured, supported full-text extraction.
- Search, discovery, and external fallbacks are separate host-application
  concerns.
- A host may fall back only on retryable transport/service failures,
  blocked/empty/incomplete content, or an explicit provider-only mode.
- Missing endpoints or tokens and non-retryable authentication or contract
  errors must fail closed instead of silently creating external traffic.

The response must expose `content_scope`, `truncated`, `strategy`, `rendered`,
`cached`, `word_count`, and a machine-readable incompleteness reason. Without
this contract the caller cannot distinguish a successful landing-page summary
from a complete extraction.

### 10. Serving and observability

Static fetch, browser rendering, CPU parsing, and semantic inference use
separate concurrency pools and queue deadlines. One wedged model call cannot
consume crawl or renderer capacity. Every optional stage is guarded by
backpressure, a circuit breaker, and a deterministic fallback already in hand.

Cache identity includes source revision, profile, model and prompt revision,
normalizer/serializer revision, and every routing threshold. Provider, cache
age, route decision, rejection/fallback reason, queue/network/render/parse/model
timings, input/output bytes, and truncation provenance are emitted as structured
telemetry. SLOs are reported per page type and route, not only as an aggregate.

## Benchmark and release gates

Every release candidate runs from a clean, recorded source revision. Raw
predictions, configurations, environment information, scorer versions, and
failures are retained.

### Public fixed-corpus gates

- AEB `article_body`: no statistically meaningful regression.
- WCXB and Webis: no regression in aggregate or major page-type slices.
- WebMainBench raw track: improve aggregate F1 and table/no-table, page-type,
  confidence, and difficulty slices.
- WebMainBench scrubbed track: comparable behavior, used as a contamination
  diagnostic rather than a replacement for the raw leaderboard track.
- All runs: zero extraction errors, bounded memory, and reported throughput.

A WebMainBench score alone cannot validate live fetching, rendering, freshness,
anti-bot behavior, or search.

The dated stretch objectives for the semantic pipeline are deliberately above
the strongest results currently recorded by the project:

| Suite | Promotion objective |
| --- | ---: |
| AEB article body | Retain F1 `0.972127` without changing its contract |
| WCXB public test | Reach F1 `>=0.910`, with page-type slices and paired uncertainty versus the frozen Clusy baseline |
| Webis | Exceed macro ROUGE-LSum `0.898844` |
| WebMainBench full | Exceed ROUGE-5 F1 `0.9098` |
| WebMainBench fine-grained subset | Exceed overall `0.8256`, including table/code/formula metrics |

These are engineering objectives, not claims. Public-label results must be
replicated on a sealed, operator-owned time/domain holdout before the adaptive
profile becomes the default.

### Live Exa/Firecrawl comparison

The existing small hand-picked vendor scripts are smoke tests, not credible
SOTA evidence. A claimable comparison requires:

1. A preregistered, time-stamped URL sample drawn from an authorized,
   representative workload and stratified by page type, language, geography,
   rendering need, and difficulty.
2. Frozen URLs and task definitions before any system output is inspected.
3. Identical requested scope and freshness semantics for every provider.
4. Blinded human judgments or source-derived references for completeness,
   correctness, structure, and noise.
5. Cold and warm-cache trials, repeated runs, timeouts counted as failures, and
   p50/p95/p99 end-to-end latency.
6. Provider-reported and normalized cost, bytes, model tokens, render use, and
   success rate.
7. Paired bootstrap confidence intervals and per-stratum results, with all
   exclusions disclosed.
8. A held-out final set that is not used for routing or threshold calibration.

The checked-in v3 live runner reports p50/p90/p95/p99 provider distributions
and per-stratum paired estimates. Its public vendor-win gate remains
unconditionally closed: the retained hash-only artifacts cannot independently
reconstruct or rescore ephemeral provider content, trusted-execution
attestation is not implemented, and the documented Exa/Firecrawl cache and
content contracts do not satisfy all matched-scope evidence requirements.
Structural references, matched cold/warm tracks, two independent time windows,
and every statistical/latency/cost gate are still required, but cannot by
themselves reopen that public claim gate.

The Exa and Firecrawl calls made by this runner are benchmark-only. Their
responses are scored under the sealed comparison protocol and are never used
as training data, distillation targets, human or synthetic labels, or
production routing-calibration inputs.

Exa advertises content retrieval across complex layouts and configurable
freshness, while Firecrawl exposes cached scraping, rendering/proxy options, and
structured Markdown output. Vendor capability and price claims must be taken
from their current official documentation when the comparison is executed:

- [Exa Contents API](https://exa.ai/docs/reference/contents-api-guide)
- [Exa pricing](https://exa.ai/pricing)
- [Firecrawl scrape API](https://docs.firecrawl.dev/api-reference/endpoint/scrape)
- [Firecrawl pricing](https://www.firecrawl.dev/pricing)

### Claim policy

Use the narrowest statement supported by evidence:

- “The clean public AEB run at `4dd1755` scored F1 `0.972127`, `+0.002172`
  above the embedded pinned `rs-trafilatura` prediction” is acceptable within
  the article-body extraction scope when accompanied by the paired 95% CI
  `[0, +0.006589]`. It is a generic bug fix to a patched embedded backend, not
  an independent algorithmic win or evidence for V2 `balanced`.
- “The clean public WCXB `adaptive` run at `9c7cc0a` scored public-test F1
  `0.901714`, ΔF1 `+0.009987` versus the direct OSS `balanced` artifact, paired
  95% CI `[+0.003424, +0.017720]`” is acceptable as public-benchmark diagnostic
  evidence. It is not an unseen or independent SOTA claim because labels are
  public and the embedded classifier's training-item manifest is unavailable.
- The clean private Webis/WebMainBench rows may be described as source-audited
  fixed-corpus diagnostics only when their private `a19ae17` execution
  provenance is stated. They must not be attributed to an OSS commit.
- “Better than Exa/Firecrawl” cannot be supported by the current hash-only v3
  artifacts. It requires a redesigned independently rescorable or attested,
  matched-scope protocol plus favorable paired confidence and operational
  evidence.
- “Universal SOTA crawler” is not an acceptable inference from extraction-only
  corpora.

## Delivery sequence

1. **Adaptive foundation — implemented, opt-in:** deterministic-first routing,
   versioned thresholds, quality fallback, and cache correctness.
2. **Ordered block IR — additive API implemented, runtime promotion pending:**
   bounded Rust graph, typed serializers, ID-selected reconstruction, and the
   shadow-only deterministic refiner.
3. **Compact model — planned:** local block classifier, batching, constrained
   labels, verifier, and circuit breaker.
4. **Serving scale — partial/planned:** backend-neutral render lifecycle is
   implemented; split fetch/render/model worker pools, tenant budgets,
   backpressure, distributed cache, tracing, and per-route SLOs remain.
5. **Evidence — in progress:** AEB is reproduced directly from clean public
   `4dd1755`; both WCXB profiles are reproduced directly from clean public
   `9c7cc0a`; Webis and WebMainBench remain private `a19ae17` diagnostics.
   Operator-owned calibration and a preregistered live vendor study remain
   separate gates before any broad claim.

The adaptive foundation can ship behind an opt-in profile. It becomes the
default only after the quality, latency, cost, and escalation-budget gates pass
in shadow traffic.

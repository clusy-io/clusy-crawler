# Research architecture and SOTA gates

The project aims to improve extraction quality, efficiency, and reliability
over strong open and commercial systems. That target is not treated as a
current fact. Every claim must be earned on a named protocol and remain valid
after deployment.

## Evidence boundary

The only current published measurement is the scoped AEB result in
[`BENCHMARKS.md`](BENCHMARKS.md). Other protocols and research paths in this
document define reproducible experiments and promotion gates; they do not
publish additional benchmark, implementation, vendor, or deployment results.

## Design principles

1. **Select source pointers, not generated page text.** Learned components may
   score source-backed spans or nodes; deterministic serializers produce the
   output.
2. **Keep the fast path.** Easy pages should not pay model or browser latency.
3. **Recover structure without changing content.** Code, table, list, and math
   reconstruction must preserve the required visible-token invariants.
4. **Use disagreement as evidence.** Extractor disagreement and provenance gaps
   are routing signals, not permission to concatenate every candidate.
5. **Make decisions replayable.** Inputs, model identity, decoder,
   configuration, and output bind to a versioned receipt.
6. **Fail closed to a known deterministic result.**
7. **Separate extraction, discovery, and search.** Each has its own state
   machine and benchmark contract.

## Target pipeline

```text
fetch once
   │
   ├─ static response
   ├─ optional render trace
   └─ specialist adapter result
   │
   ▼
lossless Source Ledger / PageArtifact
   │
   ├─ specialist candidates
   ├─ native deterministic candidates
   ├─ typed DOM candidates
   └─ optional pointer-model candidates
   │
   ▼
source-backed candidate lattice
   │
   ├─ exact interval and relation constraints
   ├─ calibrated utility / risk routing
   └─ typed serializer
   │
   ▼
replay verifier
   │
   ├─ accept certified improvement
   └─ fall back byte-for-byte
```

## Lossless Source Ledger

The target `PageArtifact` is a bounded immutable representation containing:

- original response bytes and decoded text with offset maps;
- redirect, content-encoding, MIME, and render provenance;
- ordered DOM elements and text runs;
- reliable and unreliable source spans as distinct types;
- table, list, code, math, link, and landmark relations;
- candidate outputs with source-support intervals; and
- truncation and parser-repair events.

Every transform consumes one representation version and emits another with an
explicit loss record. Reconstructed DOM text must not silently become
equivalent to original source bytes.

### Source-map and atom-catalog protocol

The additive `ordered-source-text-map.v2` maps retained DOM text back to exact
raw UTF-8 source fragments without literal substring guessing. It decodes HTML
character references and tokenizer newline behavior, pairs repeated siblings
by source order, binds raw offsets and digests, and rejects the complete map
when non-whitespace parser reorder/repair violates retained explicit-element
mapping or the direct-parent/order/text bijection, or on any resource limit.
Standards-defined implicit structure remains eligible when that contract stays
exact. The opt-in
`selection-atom-catalog.v1` uses that map to expose lexical text, code, table
cell, list-item, and math atoms.

The frozen HTML-only diagnostic uses no labels or scorer and remains disabled
by default. Its report is an archival, non-authorizing research artifact; no
quality, performance-comparison, SOTA, vendor, production, or deployment claim
follows. The reproducible protocol and source/binary-bound report are
[`selection-atom-catalog-e5958b5`](../bench/evidence/selection-atom-catalog-e5958b5/PROTOCOL.md).

## Source-backed candidate lattice

A single extractor misses different content families. The EACL 2026 study
[Beyond a Single Extractor](https://aclanthology.org/2026.findings-eacl.307/)
reports substantial differences in surviving pages and downstream table/code
performance across extractors. The target architecture uses extractors as
candidate generators rather than concatenating their outputs.

Each candidate becomes source intervals and typed relations. The decoder
selects a non-overlapping, ordered, resource-bounded subgraph subject to:

- DOM and source order;
- containment and atomic-structure closure;
- duplicate-source suppression;
- landmark and boilerplate penalties;
- query-independent or query-conditioned relevance;
- completeness and precision priors; and
- strict output budgets.

For fixed scores, decoding must be exact and deterministic.

## Typed serializers

| Source type | Output contract |
| --- | --- |
| Prose | Paragraph and heading boundaries from verified source order |
| Code | Whitespace-preserving fenced block with collision-safe delimiter |
| Table | Rectangular grid or explicit HTML fallback; no invented cells |
| List | Ordered, unordered, or description semantics with nesting |
| Math | Source-present MathML or TeX with provenance; no formula generation |
| Link | Visible text plus validated absolute destination |

A serializer may improve structure only when it proves the visible-token and
source-support invariants required by its contract.

## Exact Atomic Structure Overlay

The first incremental research implementation targets isolated code blocks and
simple tables. It may replace one complete candidate region only when:

- the selected local source subgraph is complete and replayable;
- the source and candidate match is unique;
- the replacement has a strict structural gain;
- prefix and suffix bytes remain unchanged;
- the complete normalized visible-token sequence is identical; and
- local and global resource gates pass.

The overlay is useful only if it achieves non-zero real coverage and monotonic
fine-grained quality. Until then it remains unwired research.

## Compact pointer model

The learned path is a compact multilingual graph-pointer model with two heads:

1. main-content or query-conditioned block selection; and
2. source-offset boundary refinement for mixed blocks.

The model does not emit page text. It sees bounded structural, lexical, URL,
and render features and returns calibrated node/span scores. A constrained
decoder and source replay remain authoritative.

Training-item provenance and license compatibility are release gates. A
non-commercial checkpoint cannot become a commercial default merely because
its public score is strong.

## Utility router

The router estimates expected gain from additional work using label-free
signals:

- deterministic confidence and completeness;
- candidate disagreement;
- lost source structures;
- parser repair and unreliable-span density;
- page family and language;
- render evidence; and
- backend health, latency, and cost.

It chooses deterministic acceptance, atomic repair, pointer-model selection,
rendering, or safe fallback. Calibration is evaluated separately from
extractor quality.

## Focused crawl frontier

Discovery uses a separate deterministic policy. The research successor groups
awake links by bounded DOM-path features and applies a sleeping-bandit policy
with delayed ordered feedback. Reward comes from newly discovered permissioned
target content after fetch, never from evaluator labels visible before
selection.

Required protections include fixed hashing, bounded action state,
deterministic collision handling, drift windows, host fairness, robots
enforcement, and trap budgets. Synthetic fixtures can validate the state
machine; they cannot establish a live-web superiority claim.

## Durable crawl plane

The request-local frontier is appropriate for bounded API calls. Larger crawls
need a transactional queue with:

- idempotent leases and acknowledgements;
- restart-safe attempt and robots state;
- cross-worker host politeness;
- content-addressed fetch artifacts;
- deterministic result ordering; and
- per-tenant budget and cancellation semantics.

This plane must not change extraction decisions or weaken URL safety.

## Evaluation boundary

| Data class | Permitted use |
| --- | --- |
| Public benchmark development labels | Diagnostics and development, disclosed |
| Public benchmark test labels | Fixed-protocol regression, disclosed |
| Consented training corpus | Training and calibration |
| Consented domain/time holdout | Final pre-production decision |
| Exa/Firecrawl outputs | Benchmark evaluation only |
| Synthetic frontier fixtures | State-machine regression only |

Vendor outputs, references, page-type labels, snippets, and evaluator metrics
must never cross into runtime extraction inputs.

## Promotion gates

These gates decide whether a candidate may advance to the next engineering
state. They do not authorize a SOTA claim. A small monotonic overlay gain can
be worth shipping while remaining far below the strongest comparable system.

### Deterministic runtime change

- exact complete outputs on locked equivalence corpora unless a quality change
  is preregistered;
- statistically and directionally stable benefit on the target metric;
- no important-corpus, p95, memory, cancellation, or security regression;
- full Python/Rust lint, type, test, and container checks; and
- target-platform verification before traffic moves.

### Structure overlay

- byte-identical fallback for every rejection;
- no text or formula regression.
- Threshold: certificate_replay_success == 100 percent. <!-- clusy-protocol-threshold -->
- Threshold: visible_token_identity == 100 percent. <!-- clusy-protocol-threshold -->
- Threshold: webmainbench_overall_delta >= 0.01 score. <!-- clusy-protocol-threshold -->
- Threshold: code_edit_delta >= 0.03 score. <!-- clusy-protocol-threshold -->
- Threshold: table_teds_delta >= 0.02 score. <!-- clusy-protocol-threshold -->

### Learned quality path

- versioned training-item manifest and license review;
- no overlap among training, calibration, and final holdout;
- multilingual and structured-content gains with paired uncertainty;
- calibrated router utility after latency and cost;
- adversarial grounding and prompt-injection tests;
- deterministic fallback under timeout, malformed output, saturation, and
  outage; and
- target-platform canary and rollback evidence.

### Vendor comparison

- preregistered URLs and scoring authority;
- reviewed comparable request tracks;
- sealed evaluator and complete provider request records;
- paired quality, success, latency, and cost analysis;
- no unresolved provider error or truncation bias; and
- a win gate that remains closed when comparability is incomplete.

## Scoped SOTA claim gate

A state-of-the-art statement must name one frozen task and clear a higher bar
than runtime promotion:

1. Pin the dataset, evaluator, output contract, comparable-system versions,
   leaderboard snapshot, source, dependencies, and configuration before the
   final run.
2. Beat the strongest comparable result on the primary metric under the
   preregistered paired confidence procedure.
3. Report every required language, difficulty, page-family, and
   structured-content slice; no material slice may be hidden by the aggregate.
4. Reproduce the result from a clean checkout on independent compute and
   publish the raw predictions, failures, hashes, and scoring artifact allowed
   by the data license.
5. Pass a separate permissioned domain/time holdout so a public-label win is
   not presented as unseen generalization.
6. Measure success rate, p50/p95 latency, peak memory, and unit cost at the
   actual service boundary.

- Threshold: paired_interval_confidence_level == 95 percent. <!-- clusy-protocol-threshold -->
- Threshold: primary_metric_superiority_ci_lower > 0 score. <!-- clusy-protocol-threshold -->

A Direct-MD score must not be compared as if it used an HTML-to-Markdown
canonicalization contract. A broader leadership claim additionally requires a
Pareto result across quality, success rate, tail latency, and unit cost for
every named, protocol-matched track. If a provider track is not comparable,
the claim remains open rather than being scored as a win.

## Delivery order

1. Prove the exact atomic overlay or reject it.
2. Implement the lossless ledger and typed serializer contracts.
3. Promote the source-backed multi-extractor lattice.
4. Train and calibrate the pointer model on auditable data.
5. Validate the utility router on a consented domain/time holdout.
6. Add the durable crawl plane without changing extraction semantics.
7. Run permissioned live-vendor evaluation.
8. Claim SOTA only for tasks whose complete gates pass.

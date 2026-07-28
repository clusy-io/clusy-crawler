# Commit-pinned Article Extraction Benchmark

This harness measures Clusy on Zyte/ScrapingHub's independent
[Article Extraction Benchmark][aeb] (AEB). It calls the production
`app.services.extractor.extract_content_async` entry point by default, loads the
official evaluator directly from the pinned AEB checkout, and preserves raw
predictions and complete provenance.

It is deliberately an **article-body extraction benchmark**, not evidence for
an unqualified "SOTA crawler" claim.

## Reproduce

Prepare the exact frozen dataset:

```bash
git clone https://github.com/scrapinghub/article-extraction-benchmark.git /tmp/clusy-aeb
git -C /tmp/clusy-aeb checkout --detach 4a3bc979f76c0df73cb95fe272e2fc1b96f9f010
uv sync --frozen
```

Run the production asynchronous extractor:

```bash
uv run python bench/neutral_benchmark.py /tmp/clusy-aeb \
  --mode async \
  --extraction-profile article_body \
  --bootstrap-samples 10000
```

The runner refuses any AEB commit other than
`4a3bc979f76c0df73cb95fe272e2fc1b96f9f010`, verifies the Git tree and SHA-256
hashes of `evaluate.py` and `ground-truth.json`, and refuses tracked dataset
modifications. It never downloads or evaluates a mutable `HEAD`.

For a non-publishable smoke run:

```bash
uv run python bench/neutral_benchmark.py /tmp/clusy-aeb \
  --limit 12 \
  --bootstrap-samples 200
```

For an explicitly labelled synchronous comparison:

```bash
uv run python bench/neutral_benchmark.py /tmp/clusy-aeb --mode both
```

`--mode sync` alone is marked non-claimable because it omits the production
asynchronous entry point.

## What is measured

Quality uses AEB's evaluator unchanged:

- case-insensitive four-token shingles;
- page-normalized true-positive, false-positive, and false-negative counts;
- macro precision and recall, followed by their harmonic-mean F1;
- the evaluator's exact-token accuracy field;
- the official bootstrap standard deviations.

Clusy returns Markdown while AEB ground truth is plain article text. The runner
scores the exact production body without stripping headings, links, or HTML:
AEB's tokenizer already ignores markup punctuation, while any extra content
words should count against precision. The native article path currently emits
plain text (which is valid Markdown), and the untouched production output is
retained separately for audit.

The stable development/test split is `sha1(item_key)` parity:

- even hash: development;
- odd hash: test.

It is independent of page order and the bootstrap seed. The report contains
full, development, and test quality for Clusy and both official baselines:

- `trafilatura.json` (`2.0.0` at the pinned AEB commit);
- `rs_trafilatura.json` (`9261e08` at the pinned AEB commit).

For each split and baseline, the runner performs a paired page bootstrap. Each
replicate samples the same page indices for Clusy and the baseline and calls the
official `metrics_from_tp_fp_fns`. The report includes point-estimate ΔF1,
percentile 95% CI, and `P(Clusy > baseline)`. The default 10,000 replicates and
all page ordering are deterministic from seed `20260727`.

Performance reports:

- per-page p50 and p95 latency;
- total pages/second;
- errors and chosen extraction strategies;
- peak process RSS;
- Python, platform, CPU, dependency, Rust, native-backend, Git, lockfile, and
  relevant-source hashes.

Performance is **in-memory decoded HTML to `ExtractionResult`**. Fixture reads,
gzip decompression, prediction-file serialization, and evaluation are excluded.
It is a closed-loop bounded-worker microbenchmark, not an HTTP-service or
Internet throughput benchmark. Static HTTP, browser rendering, and live-network
capacity must be measured separately.

## Current local validation

The final full 2026-07-27 working-tree run used the production asynchronous
entry point, two workers, five warmup pages, and 10,000 paired bootstrap
replicates:

| Mode | Precision | Recall | F1 | pages/s | p50 | p95 | errors |
|---|---:|---:|---:|---:|---:|---:|---:|
| async | 0.951014 | 0.989665 | 0.969955 | 141.70 | 12.64 ms | 25.92 ms | 0 |

The async result is byte-for-byte equivalent under AEB's metric to the pinned
`rs_trafilatura` prediction. Versus Trafilatura 2.0, ΔF1 is +0.012452 with a
paired-bootstrap 95% interval of [+0.002093, +0.023745] and win probability
0.9892. Peak process RSS was 262,897,664 bytes.

The artifact directory is
`bench/results/aeb/final-production-frozen-20260727-v1/` (ignored by Git). The run is
intentionally marked `NOT CLAIMABLE` because the optimized source is still an
uncommitted working tree. Commit the implementation and rerun before publishing
the number externally.

## Artifacts and publication rules

Timestamped runs default to `bench/results/aeb/`, which is gitignored. Each run
contains:

- `report.json` — quality, paired inference, performance, environment, and
  provenance;
- `predictions/clusy_async.json` — exact production bodies in AEB JSON shape;
- `raw/production_markdown_async.json` — untouched production outputs and
  extractor metadata;
- `raw/page_metrics_async.jsonl` — per-page latency, split, strategy, and errors;
- `split_manifest.json` — exact full/dev/test membership;
- `manifest.json` — size and SHA-256 of every other artifact.

The runner prints and records `NOT CLAIMABLE` if:

- fewer than all 181 pages were evaluated;
- the production asynchronous entry point was omitted;
- the Clusy worktree was dirty; or
- relevant source files changed while the run was executing.

A publishable result should preserve the entire artifact directory, run from a
clean committed Clusy revision, show the exact resource envelope, and report
confidence intervals—not just point estimates. Public AEB labels make the
benchmark vulnerable to test-set tuning, so changes should be developed against
the deterministic development half and checked once against the test half.

## Scope and current comparison point

AEB contains 181 news/blog article pages. Its HTML was fetched with Splash with
JavaScript disabled. It evaluates only `articleBody`; it does not measure:

- products, forums, listings, documentation, tables, code, or formulas;
- link discovery, crawl completeness, URL traps, or deduplication;
- robots.txt, sitemap, retry, redirect, or politeness correctness;
- JavaScript rendering and dynamic-link discovery;
- HTTP latency, sustained load, crash recovery, or cache behavior.

At the pinned commit, the official leaderboard reports rs-trafilatura at
`0.970 ± 0.004`, Trafilatura 2.0 at `0.958 ± 0.006`, and the historical 2019
AutoExtract result at `0.970 ± 0.005`. Any older statement that `0.960` is the
best open-source AEB score is therefore obsolete.

For a broader extraction claim, add:

- [WebMainBench][webmainbench]: 7,809 human tag-annotated pages and a 545-page
  manually calibrated Markdown subset with text, code, formula, table, and TEDS
  metrics. Its style/difficulty labels are model-assigned and its authors also
  develop leading baselines.
- [WCXB][wcxb]: 2,008 pages across seven explicit page types. Its annotations
  are LLM-assisted and human-reviewed, its public test set can now be tuned
  against, and its author also maintains the leading rs-trafilatura baseline.
- [Webis/ChatNoir][webis]: an independent reproducibility study combining eight
  human-labelled datasets, useful as a robustness check despite older pages.

A full crawler claim additionally needs deterministic discovery, protocol,
rendering, deduplication, fault, resume, and security suites. No weighted blend
of those tasks should be presented as a universal SOTA number.

[aeb]: https://github.com/scrapinghub/article-extraction-benchmark
[webmainbench]: https://huggingface.co/datasets/opendatalab/WebMainBench
[wcxb]: https://github.com/Murrough-Foley/web-content-extraction-benchmark
[webis]: https://github.com/chatnoir-eu/web-content-extraction-benchmark

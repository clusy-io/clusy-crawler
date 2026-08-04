# Commit-pinned Article Extraction Benchmark

This harness measures Clusy on Zyte/ScrapingHub's independent
[Article Extraction Benchmark][aeb] (AEB). It calls the production
`app.services.extractor.extract_content_async` entry point by default, loads the
official evaluator directly from the pinned AEB checkout, and preserves raw
predictions and complete provenance. It retains AEB's historical baseline
outputs and independently replays exact Trafilatura `2.1.0` before labels or
evaluator code enter the benchmark process.

This **article-body extraction benchmark** does not support an unqualified
crawler-leadership claim.

## Reproduce

The registered receipt belongs to clean public commit
`77b8d00c5ebf88ed3afffe64f869ccb8c6922365`, whose tree is identical to
`v0.2.0-beta.2`. Use a separate clone so the historical candidate is exact and
the current checkout remains untouched:

```bash
git clone https://github.com/clusy-io/clusy-crawler.git \
  /tmp/clusy-crawler-aeb-beta2
git -C /tmp/clusy-crawler-aeb-beta2 checkout --detach \
  77b8d00c5ebf88ed3afffe64f869ccb8c6922365
cd /tmp/clusy-crawler-aeb-beta2
```

Prepare the exact frozen dataset and comparator environment:

```bash
git clone https://github.com/scrapinghub/article-extraction-benchmark.git /tmp/clusy-aeb
git -C /tmp/clusy-aeb checkout --detach 4a3bc979f76c0df73cb95fe272e2fc1b96f9f010
uv sync --frozen

uv venv --python 3.13.5 /tmp/clusy-trafilatura-2.1
uv pip sync --require-hashes \
  --python /tmp/clusy-trafilatura-2.1/bin/python \
  bench/aeb_trafilatura_2_1_0_requirements.lock
```

Running the harness from a newer source tree creates a new, unregistered
evaluation. It does not reproduce or replace the historical registered
receipt below.

Run the production asynchronous extractor:

```bash
uv run python bench/neutral_benchmark.py /tmp/clusy-aeb \
  --mode async \
  --extraction-profile article_body \
  --trafilatura-python /tmp/clusy-trafilatura-2.1/bin/python \
  --bootstrap-samples 10000
```

The runner refuses any AEB commit other than
`4a3bc979f76c0df73cb95fe272e2fc1b96f9f010`, verifies the Git tree and SHA-256
hashes of `evaluate.py` and `ground-truth.json`, and refuses tracked dataset
modifications. It never downloads or evaluates a mutable `HEAD`.

The current-OSS replay is fail-closed:

- the controller inventories the exact 181 tracked `html/*.html.gz` Git blobs,
  compressed bytes, decoded bytes, keys, and pinned upstream
  `extractors/run_trafilatura.py`;
- only those gzip files and a cryptographic manifest enter a temporary
  label-free capsule;
- a fresh Python `-I -B` process with a minimal environment verifies the
  capsule, installed `trafilatura==2.1.0` payload, reviewed worker source, and
  the sole call configuration
  `trafilatura.extract(html, include_comments=False)`;
- the frozen CPython `3.13.5` Darwin/arm64 environment, built with uv `0.11.6`,
  contains exactly 17 distributions. Every installed `RECORD` entry is checked
  against its declared SHA-256 and size, every canonical distribution payload
  is bound to the reviewed environment manifest, and the complete importable
  `site-packages` tree is inventoried so an extra or modified module fails
  closed;
- the separate requirements lock pins all 17 versions and accepted archive
  hashes under `--require-hashes`; the controller verifies that it includes the
  reviewed Trafilatura `2.1.0` wheel hash. The production `uv.lock` remains
  independent candidate provenance rather than being forced to match
  comparator transitive versions;
- the controller does not put `ground-truth.json`, `evaluate.py`, historical
  outputs, or Clusy predictions in the capsule, and the reviewed worker opens
  only capsule inputs. Python `-I` isolates imports and environment influence;
  it is not an OS filesystem sandbox. Labels and evaluator code are loaded only
  after the worker exits;
- another platform needs its own reviewed environment manifest and must not
  reuse the Darwin/arm64 receipt;
- every prediction, input binding, receipt, and provenance file is hashed by
  the final artifact manifest.

For a non-publishable smoke run:

```bash
uv run python bench/neutral_benchmark.py /tmp/clusy-aeb \
  --trafilatura-python /tmp/clusy-trafilatura-2.1/bin/python \
  --limit 12 \
  --bootstrap-samples 200
```

`--limit` reduces only the Clusy candidate run. The provenance-gated
Trafilatura comparator still replays all 181 pages so its receipt remains
complete.

For an explicitly labelled synchronous comparison:

```bash
uv run python bench/neutral_benchmark.py /tmp/clusy-aeb \
  --trafilatura-python /tmp/clusy-trafilatura-2.1/bin/python \
  --mode both
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

It is independent of page order and the bootstrap seed. The current authorized
comparison point is the label-free local replay of exact Trafilatura `2.1.0`
with the pinned upstream call configuration. The runner may retain additional
upstream diagnostics, but those outputs are not authorized publication claims.

For each split, the runner performs a paired page bootstrap against the current
comparison point. Each replicate samples the same page indices for both
systems and calls the official `metrics_from_tp_fp_fns`. The report includes
point-estimate ΔF1, a percentile confidence interval, and a paired direction
statistic. Sampling and page ordering are deterministic from the recorded seed.

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

## Evidence status

The current registered result is the clean public, Beta 2 tree-equivalent
[`77b8d00` evidence record](evidence/aeb-article-body-trafilatura-2-1-77b8d00-beta2-public/PROTOCOL.md).
> **Verified evidence — Article Extraction Benchmark · `article_body` · 181 pages.** Clusy F1 `0.972127`; exact Trafilatura 2.1.0 F1 `0.957546`; F1 delta `+0.014581`; F1 delta CI95 low `+0.005547`; F1 delta CI95 high `+0.025336`; paired-bootstrap win fraction `0.9996`; machine-local in-memory throughput `173.97 pages/s`. <!-- clusy-evidence: aeb.article-body.trafilatura-2-1.77b8d00-beta2-public.2026-07-31 -->

The retained raw artifact manifest SHA-256 is
`fffeba35b9581920f4053eb6e044c5ae16e6d231c97adc110add07cea1987542`;
the archive SHA-256 is
`823ea88f1fdf260d03edfd0f4c0cc8fb1030f5c7583c48daceeabbcb2058f3be`.
## Artifacts and publication rules

Timestamped runs default to `bench/results/aeb/`, which is gitignored. Each run
contains:

- `report.json` — quality, paired inference, performance, environment, and
  provenance;
- `predictions/clusy_async.json` — exact production bodies in AEB JSON shape;
- `raw/production_markdown_async.json` — untouched production outputs and
  extractor metadata;
- `raw/page_metrics_async.jsonl` — per-page latency, split, strategy, and errors;
- `baselines/trafilatura_2_1_0_input_manifest.json` — label-free fixture
  inventory and pinned upstream runner identity;
- `baselines/trafilatura_2_1_0_environment_manifest.json` — reviewed Python,
  platform, exact 17-package closure, canonical `RECORD` commitments, and full
  `site-packages` commitment;
- `baselines/trafilatura_2_1_0_requirements.lock` — exact retained
  hash-pinned installation lock verified by the controller;
- `baselines/trafilatura_2_1_0_worker_result.json` — exact per-page predictions,
  input hashes, full distribution/file inventories, call configuration, and
  worker receipt;
- `baselines/trafilatura_2_1_0_predictions.json` — normalized AEB prediction
  artifact used by the paired scorer;
- `split_manifest.json` — exact full/dev/test membership;
- `manifest.json` — size and SHA-256 of every other artifact.

The runner prints and records `NOT CLAIMABLE` if:

- fewer than all 181 pages were evaluated;
- the production asynchronous entry point was omitted;
- exact Trafilatura `2.1.0`, its 17-distribution installed environment,
  hash-pinned requirements lock, reviewed wheel hash, label-free 181-page
  replay, or receipt verification did not succeed;
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

Upstream leaderboard context can change and is not a current claim in this
repository. Any future comparison must freeze the comparator version and
evidence under the registry contract.

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
rendering, deduplication, fault, resume, and security suites. No universal SOTA
number follows from a weighted blend of those tasks.

[aeb]: https://github.com/scrapinghub/article-extraction-benchmark
[webmainbench]: https://huggingface.co/datasets/opendatalab/WebMainBench
[wcxb]: https://github.com/Murrough-Foley/web-content-extraction-benchmark
[webis]: https://github.com/chatnoir-eu/web-content-extraction-benchmark

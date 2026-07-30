# Commit-pinned Article Extraction Benchmark

This harness measures Clusy on Zyte/ScrapingHub's independent
[Article Extraction Benchmark][aeb] (AEB). It calls the production
`app.services.extractor.extract_content_async` entry point by default, loads the
official evaluator directly from the pinned AEB checkout, and preserves raw
predictions and complete provenance. It retains AEB's historical baseline
outputs and independently replays exact Trafilatura `2.1.0` before labels or
evaluator code enter the benchmark process.

It is deliberately an **article-body extraction benchmark**, not evidence for
an unqualified "SOTA crawler" claim.

## Reproduce

Prepare the exact frozen dataset:

```bash
git clone https://github.com/scrapinghub/article-extraction-benchmark.git /tmp/clusy-aeb
git -C /tmp/clusy-aeb checkout --detach 4a3bc979f76c0df73cb95fe272e2fc1b96f9f010
uv sync --frozen

uv venv --python 3.13.5 /tmp/clusy-trafilatura-2.1
uv pip sync --require-hashes \
  --python /tmp/clusy-trafilatura-2.1/bin/python \
  bench/aeb_trafilatura_2_1_0_requirements.lock
```

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

It is independent of page order and the bootstrap seed. The report contains
full, development, and test quality for Clusy and three distinct comparison
points:

- historical `trafilatura.json` (`2.0.0` at the pinned AEB commit);
- historical `rs_trafilatura.json` (`9261e08` at the pinned AEB commit);
- label-free local replay `trafilatura_2_1_0`, using exact Trafilatura `2.1.0`
  and the pinned upstream call configuration.

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

## Current clean public validation

The full 2026-07-29 run used clean public source revision
`4252a0b71a0a2157194d3466445b70bb373d73b6`, the production asynchronous entry
point, two workers, five warmup pages, and 10,000 paired-bootstrap replicates:

| Mode | Precision | Recall | F1 | pages/s | p50 | p95 | errors |
|---|---:|---:|---:|---:|---:|---:|---:|
| async | 0.955147 | 0.989721 | 0.972127 | 137.844 | 12.933 ms | 26.488 ms | 0 |

The candidate is +0.002172 F1 above the pinned `rs_trafilatura` prediction,
with a paired-bootstrap 95% interval of [0, +0.006589], win probability
0.6349, loss probability 0, and equality probability 0.3651. Versus
Trafilatura 2.0, ΔF1 is +0.014624 with a paired-bootstrap 95% interval of
[+0.005346, +0.025342] and win probability 0.9995. Peak process RSS was
264,536,064 bytes.

The only changed prediction relative to the earlier clean public run is a page
that exercised overlapping wrapper and descendant roots in temporary JSON-LD
DOM serialization; the other 180 prediction records are byte-identical. The
generic fix filters selected roots by source ancestry before serialization. It
does not collapse equal text from disjoint source nodes and contains no page,
hostname, or benchmark-specific condition.

The artifact directory is `bench/results/aeb/20260729T090959Z` (ignored by
Git). Its `manifest.json` SHA-256 is
`7b014b56cd8d99dc8280bb2ac7b5f86e3c55972fb4c380289b9025fe13be29b0`;
the report SHA-256 is
`fc80a5840b81e9f871faa2a0f60c4829f9cf6fea3224bf520c1c0ac207df6b3f`.
The harness verified clean, stable public source before and after execution and
marked the complete result `CLAIMABLE` only within the AEB article-body scope.
Throughput is an in-memory extractor result on an Apple M4 Pro and is not a
live-network crawling claim.

For historical context, clean public commit `c3ae00d` and clean private
revision `10ff0c1` both scored F1 0.969955 before this fix, at 133.33 and
157.205 pages/s respectively. The local throughput difference between runs is
not a quality or service-capacity comparison.

The retained private pre-fix V2 artifact identifier is
`bench/results/aeb/20260729T000138Z`; its `manifest.json` SHA-256 is
`bcd55239efd34908c300aa062ea5272d1cb62f4c78207eb6ab562f5cdaa381da`.

A post-run SHA-256 audit compared the private artifact's 49 recorded source
files with public `837ddda`: 44 matched and none was missing. The suite runner,
core extraction implementation, native algorithm sources, Cargo files, and
`uv.lock` matched byte-for-byte. The five differences were `app/config.py`,
`app/main.py`, `app/services/renderer.py`, `native/pyproject.toml`, and
`pyproject.toml`; they comprise OSS configuration, comments, and package
metadata, and the only executable delta is an adaptive-profile page-type
validator that this `article_body` run did not exercise. The full snapshots are
therefore not byte-identical, and the recorded native binary was not reproduced
from `837ddda`. Treat the private result as source-audited evidence for the
exercised extraction path, not as a run of the OSS commit.
### Trafilatura 2.1.0 comparison track

The exact Trafilatura `2.1.0` replay has no registered public result yet. A
formal number may be added only after this harness is committed cleanly, all
adversarial and repository gates pass, and a complete source-bound OSS
artifact is retained. Private-repository or diagnostic output must not be
copied into public README claims.

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

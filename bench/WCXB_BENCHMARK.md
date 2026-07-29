# Commit-pinned WCXB methodology

This harness measures the production Clusy HTML-to-main-content extractor on
[WCXB][wcxb]. It does not measure fetching, link discovery, robots compliance,
JavaScript rendering, politeness, deduplication, or crawl recovery. A WCXB win
would therefore be an extraction result—not an unqualified “SOTA crawler”
result.

## Frozen inputs

The runner accepts only this exact, clean corpus:

- commit: `c039d5ee9f5a3a984a0e167e63aacd04e76e78a9`
- Git tree: `1d3d493fed8c3e01f3c62f817b2123548c5cfd1a`
- evaluator SHA-256:
  `ae4ad6299e190177fbb04a7c1190077fc6086b3fa0f31fb37a6538a5e979c559`
- evaluator/input-tree SHA-256:
  `4d5c9be2094ba5a2b5a8046fdc846b0518a799b60710b4581b457f9731bc3aae`
- pages: 1,497 development + 511 public test = 2,008

It rejects another commit, any tracked corpus modification, altered critical
files, missing/extra scored inputs, and output directories inside the corpus.
The full input tree is hashed before and after extraction.

## Reproduce

```bash
git clone https://github.com/Murrough-Foley/web-content-extraction-benchmark.git \
  /tmp/clusy-wcxb
git -C /tmp/clusy-wcxb checkout --detach \
  c039d5ee9f5a3a984a0e167e63aacd04e76e78a9
uv sync --frozen
uv run python bench/wcxb_benchmark.py \
  --corpus /tmp/clusy-wcxb \
  --splits dev test \
  --extraction-profile balanced
```

`balanced` is the default for backward compatibility. Other production
profiles must be requested explicitly, for example:

```bash
uv run python bench/wcxb_benchmark.py \
  --corpus /tmp/clusy-wcxb \
  --splits dev test \
  --extraction-profile adaptive
```

The selected profile is passed unchanged and recorded in both the environment
and run configuration. Results from different profiles are separate system
configurations and must not be silently pooled. The production call is:

```python
await extract_content_async(html, url, extraction_profile=recorded_profile)
```

No reference text, page type, or snippets are passed to the extractor. Its
returned text is stored and scored unchanged.

For a fast harness check:

```bash
uv run python bench/wcxb_benchmark.py \
  --corpus /tmp/clusy-wcxb \
  --limit-per-split 3
```

Limited or single-split outputs contain a prominent
`NOT_COMPARABLE_SMOKE_OR_PARTIAL_RUN.txt` watermark and are always marked
non-claimable.

## Official scoring and artifacts

The harness imports the pinned `evaluate.py` only after provenance validation
and invokes its `load_ground_truth` and `evaluate_results` functions. Reported
precision, recall, and F1 are macro means of the official per-page bag-of-words
scores. Official required-snippet and forbidden-snippet rates are retained;
lower forbidden-snippet rate is better.

Timestamped output defaults under ignored `bench/results/wcxb/` and contains:

- exact raw `<split>_predictions.json`;
- official and extraction per-page rows in `<split>_pages.jsonl`;
- `summary.json` with full/dev/test metrics, p50/p95, throughput, peak RSS,
  dependency/native binary hashes, corpus state, and source state;
- `manifest.json` with artifact sizes and SHA-256 hashes.

A run is artifact-valid within the WCXB extraction scope only when both
complete splits run, the corpus is verified and stable, and the Clusy source is
committed, clean, and stable throughout the run. Extraction failures remain
empty predictions and are scored rather than silently dropped. Artifact
validity alone does not open the unseen-performance or SOTA claim gate.

## Interpretation limits

- Every HTML file and label—including the “test” labels—is public. This is
  public validation, not a blind held-out estimate.
- WCXB annotation drafting was LLM-assisted and human-reviewed.
- WCXB and the leading rs-trafilatura baseline share an author, and difficult
  pages received extractor-informed adversarial review. Use an independently
  maintained suite such as WebMainBench or Webis alongside WCXB.
- The broad native extractor embeds [`web-page-classifier`][classifier]
  `0.1.0` (Cargo checksum
  `557ae9fe8bf3f86d972a8604cc5fe8c897359de9657fe7a3eda4fddfac7f3856`).
  Its publisher reports training on 1,497 pages across the same seven page
  types, while WCXB development contains exactly 1,497 pages across those
  seven types. No item-level or split-level training manifest is published.
  This is strong overlap risk, not proof of exact overlap: development and
  combined rows are training-provenance diagnostics, and even the
  upstream-held-out test row is not independently auditable as unseen until a
  manifest or a transparent-model replay is available.
- `metadata.json` omits 139 development files; the pinned evaluator and actual
  ground-truth directories contain and score all 2,008 pages.
- The repository does not contain complete predictions/configuration for its
  published rs-trafilatura and Trafilatura tables, so those headline numbers
  are context, not a locally reproducible paired comparison.
- Bag-of-words F1 does not measure Markdown structure, table structure, code,
  formula fidelity, or ordering.

Do not tune against individual WCXB labels or page types and then present the
same public corpus as independent confirmation.

WCXB pages, labels, and predictions are benchmark-only inputs and outputs; they
are not used for training, distillation, label generation, or production
routing calibration.

## Current direct OSS validation

The full 2026-07-29 runs came directly from clean public source revision
`9c7cc0a84f240910ff764baae75824e269d08350`, with eight requested workers and
three warmups per split:

| Profile / split | Pages | Precision | Recall | F1 | pages/s | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `balanced` development | 1,497 | 0.852732 | 0.898934 | 0.848433 | 56.58 | 110.345 ms | 341.527 ms |
| `balanced` public test | 511 | 0.894822 | 0.928969 | 0.891727 | 106.96 | 68.243 ms | 124.028 ms |
| `balanced` combined | 2,008 | 0.863443 | 0.906577 | 0.859450 | 64.28 | 97.125 ms | 295.621 ms |
| `adaptive` development | 1,497 | 0.844912 | 0.912670 | 0.852667 | 48.54 | 121.006 ms | 424.284 ms |
| `adaptive` public test | 511 | 0.895244 | 0.942960 | 0.901714 | 78.62 | 82.501 ms | 227.714 ms |
| `adaptive` combined | 2,008 | 0.857721 | 0.920378 | 0.865149 | 53.78 | 110.542 ms | 376.669 ms |

Both runs produced all 2,008 predictions with zero extraction errors and
verified clean, stable public source throughout. Their ignored artifacts are:

| Profile | Artifact directory | Manifest SHA-256 | Summary SHA-256 |
|---|---|---|---|
| `balanced` | `bench/results/wcxb/20260729T-oss-balanced-9c7cc0a` | `627995ebc1c9e2005a88b8b007a3e56e2eb04ab9994f6ed3a78834a1958407a8` | `32be7cb53622a3caff574a3fae490238e66df35969442630d3f9b6824bea4d42` |
| `adaptive` | `bench/results/wcxb/20260729T-oss-adaptive-9c7cc0a` | `c02cccf91d77540de9e52a795285abcfe9baae244edf236a5e28b70b056908ba` | `5137537fdd02c9f622aa6bae2531c98a79029199f0ea36c220a7c9ffcee8006a` |

Public revision `f5647e1` later added an output-equivalent native fallback
optimization, mirrored by hosted private revision `0fb00ee`. Its controlled
before/after speed evidence is documented in the root README; it does not
change the quality rows or prediction hashes above.

The `adaptive` prediction SHA-256 values are
`0520fcf3bf2dffe578dfe53e9cbff08ef874be2b521ad86b8dc2eca36fc3de00`
for development and
`e3e670a76d55f2b34172d915be41c8b2f69a2cc0060a52a96f88403c6d4852b8`
for public test. They are byte-identical to the separately captured private
deployment-path predictions.

Against this direct OSS `balanced` artifact, `adaptive` changed 66 development
and 17 test outputs. Under official per-page F1 it had 41 wins, 34 losses, and
1,933 ties overall. A 10,000-replicate paired page bootstrap with replacement
used fixed seeds 20260729, 20260730, and 20260731:

| Split | ΔF1 (`adaptive - balanced`) | 95% CI | P(Δ > 0) |
|---|---:|---:|---:|
| development | +0.004235 | [-0.000459, +0.009298] | 0.9604 |
| public test | +0.009987 | [+0.003424, +0.017720] | 0.9995 |
| combined | +0.005699 | [+0.001795, +0.009833] | 0.9983 |

The two profiles were run sequentially rather than as a randomized performance
experiment. Their throughput and latency rows are honest run observations, not
a portable speed comparison. In these runs `adaptive` traded about 16.3%
combined throughput for the quality gain.

The pinned WCXB commit reports `rs-trafilatura` at 0.859 on development and
0.893 on public test. `adaptive` is 0.008714 above that public-test point
result, but the upstream predictions are unavailable for a paired comparison,
and the systems share backend/model provenance. Combined rows must not be
compared with a development-only headline.

Describe this as reproducible direct-OSS WCXB extraction evidence rather than a
blind or universal SOTA result. The labels are public, annotation drafting was
LLM-assisted and human-reviewed, the benchmark shares author overlap with the
leading baseline, and the embedded classifier's training items are unresolved.

[wcxb]: https://github.com/Murrough-Foley/web-content-extraction-benchmark
[classifier]: https://github.com/Murrough-Foley/web-page-classifier
[webmainbench]: https://huggingface.co/datasets/opendatalab/WebMainBench
[webis]: https://github.com/chatnoir-eu/web-content-extraction-benchmark

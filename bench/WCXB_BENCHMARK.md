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

## Evidence status

This release publishes no WCXB result. Outputs from protocol runs remain local
and non-authorizing unless a clean public-source run, complete retained
artifacts, and an exact evidence-registry binding are added together. The
public-label and classifier-provenance limitations above must remain visible
in any future report.

[wcxb]: https://github.com/Murrough-Foley/web-content-extraction-benchmark
[classifier]: https://github.com/Murrough-Foley/web-page-classifier
[webmainbench]: https://huggingface.co/datasets/opendatalab/WebMainBench
[webis]: https://github.com/chatnoir-eu/web-content-extraction-benchmark

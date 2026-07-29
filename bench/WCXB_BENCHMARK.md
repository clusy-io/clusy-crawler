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
  --splits dev test
```

The production call is explicit:

```python
await extract_content_async(html, url, extraction_profile="balanced")
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

A run is publishable within the WCXB extraction scope only when both complete
splits run, the corpus is verified and stable, and the Clusy source is committed,
clean, and stable throughout the run. Extraction failures remain empty
predictions and are scored rather than silently dropped.

## Interpretation limits

- Every HTML file and label—including the “test” labels—is public. This is
  public validation, not a blind held-out estimate.
- WCXB annotation drafting was LLM-assisted and human-reviewed.
- WCXB and the leading rs-trafilatura baseline share an author, and difficult
  pages received extractor-informed adversarial review. Use an independently
  maintained suite such as WebMainBench or Webis alongside WCXB.
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

## Retained clean private-source validation

The full 2026-07-29 `balanced` run used clean private source revision
`10ff0c1a7c9a2083958b674d64e15bb5a8a1b90e`, two workers, and three warmups per
split. It was not executed from public OSS revision
`837dddababc612bfa1ce438307b1e2fb29b4c2f5` or from the current public commit:

| Split | Pages | Precision | Recall | F1 | pages/s | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| development | 1,497 | 0.852732 | 0.898934 | 0.848433 | 90.65 | 13.61 ms | 59.42 ms |
| public test | 511 | 0.894822 | 0.928969 | 0.891727 | 139.86 | 11.08 ms | 33.04 ms |
| combined | 2,008 | 0.863443 | 0.906577 | 0.859450 | 99.5618 | 12.74 ms | 50.93 ms |

All 2,008 pages produced predictions with zero extraction errors. Peak process
RSS was 486,506,496 bytes; that is a process-lifetime high-water mark including
imports, corpus processing, retained predictions, and evaluation structures.
The retained private artifact identifier is
`bench/results/wcxb/20260729T000227Z`; its `manifest.json` SHA-256 is
`a051965f480fd3a1cae780d7ccdd289b237163f7e644ba2dde9a9acc53e4b8d0`.
The source was clean before and after the run and stable throughout at
`10ff0c1`; the harness marked the complete result claimable within the WCXB
extraction scope.

The published comparison table recorded by this evidence reports
`rs-trafilatura` at 0.859 on development and 0.903 on public test. Clusy is
below both. The combined 0.859450 row above must not be compared with the
development-only 0.859 headline.

A post-run SHA-256 audit compared the artifact's 49 recorded source files with
public `837ddda`: 44 matched and none was missing. The WCXB runner, core
extraction implementation, native algorithm sources, Cargo files, and
`uv.lock` matched byte-for-byte. The five differences were `app/config.py`,
`app/main.py`, `app/services/renderer.py`, `native/pyproject.toml`, and
`pyproject.toml`; they comprise OSS configuration, comments, and package
metadata, and the only executable delta is an adaptive-profile page-type
validator that this `balanced` run did not exercise. The full snapshots are not
byte-identical, and the recorded native binary was not reproduced from
`837ddda`.

Describe this as clean, source-audited WCXB extraction evidence from the
private revision, not as a run of the OSS commit, a blind result, or universal
SOTA. The labels are public, annotation drafting was LLM-assisted and
human-reviewed, and the benchmark shares author overlap with the leading
baseline.

[wcxb]: https://github.com/Murrough-Foley/web-content-extraction-benchmark
[webmainbench]: https://huggingface.co/datasets/opendatalab/WebMainBench
[webis]: https://github.com/chatnoir-eu/web-content-extraction-benchmark

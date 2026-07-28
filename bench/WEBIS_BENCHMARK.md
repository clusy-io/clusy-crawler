# Webis Web Content Extraction Benchmark

This harness evaluates Clusy’s unchanged deterministic production extractor on
the full 3,985-page corpus from *An Empirical Comparison of Web Content
Extraction Algorithms* (SIGIR 2023, DOI
[`10.1145/3539618.3591920`](https://dl.acm.org/doi/10.1145/3539618.3591920)).
It calls the pinned upstream scorer rather than copying or approximating its
metrics.

## What is pinned

- Official repository:
  `chatnoir-eu/web-content-extraction-benchmark@36be6d9c4f96d3c613c21144c4d39e5d0cce93af`
- Git tree: `b22ec66c35eb201acfa73d1bc8bfddb4f2e46cfb`
- Combined corpus archive SHA-256:
  `ed4e57ecad343cdce51d06fa560c1f50965367ea6714cd08e85a439102bc4b1a`
- Extracted corpus tree SHA-256:
  `2818f8118ce2d98ea659d64f006eff217e5ed5ac27bb67b29209015d14290cb9`
- Official evaluator SHA-256:
  `3b5cba4c59fae42492f1d67650622b04e44620208584e4d434f48cc08150462a`
- Official precomputed metrics archive SHA-256:
  `c1c6c14994fb0e676c2371d46be672d5ba29f092f9678f5d63744c3ca7e075f5`
- Python 3.11 and every scorer-relevant direct dependency at its Poetry lock
  version, including NLTK 3.8.1.
- NLTK data repository commit:
  `550b6625bcef1f2abff2ff770a5a0d272c9c6b2a`
- NLTK `punkt.zip` SHA-256:
  `51c3078994aeaf650bfc8e028be4fb42b4a0d177d41c012b6a983979653660ec`
- Python 3's actually loaded
  `tokenizers/punkt/PY3/english.pickle` SHA-256:
  `5cad3758596392364e3be9803dbd7ebeda384b68937b488a01365f5551bb942c`

The runner verifies all of these before it evaluates a page and re-verifies the
repository, corpus, and NLTK resources after extraction. It also validates the
evaluator subprocess's resolved Punkt path and bytes, its aggregation against
exact values derived from the official per-page baseline CSV files, and the
exact loaded Clusy native extension bytes.

## Corpus

| Dataset | Pages |
|---|---:|
| CETD | 700 |
| CleanEval | 738 |
| CleanPortalEval | 71 |
| Dragnet | 1,379 |
| Google-Trends-2017 | 180 |
| L3S-GN1 | 621 |
| Readability | 115 |
| ScrapingHub | 181 |
| **Total** | **3,985** |

The production extractor receives only HTML and the optional URL:

```python
extract_content(html, url, extraction_profile="balanced")
```

Ground truth, dataset identity, source metadata, and scores remain in the
benchmark process. The prediction is scored unchanged; there is no
benchmark-specific cleanup.

## Reproduce

Clone and materialize the exact official repository:

```bash
git clone https://github.com/chatnoir-eu/web-content-extraction-benchmark.git /tmp/webis-wceb
git -C /tmp/webis-wceb checkout 36be6d9c4f96d3c613c21144c4d39e5d0cce93af
git -C /tmp/webis-wceb lfs pull \
  --include='datasets/combined.tar.xz,outputs/metrics-computed.tar.xz' \
  --exclude=''
tar -xJf /tmp/webis-wceb/datasets/combined.tar.xz -C /tmp/webis-wceb/datasets
```

Create the isolated scorer environment. These are the scorer-relevant exact
versions from the official `poetry.lock`; installing the project’s full
historical extraction stack is unnecessary:

```bash
uv venv --python 3.11 /tmp/webis-scorer
uv pip install --python /tmp/webis-scorer/bin/python \
  click==8.1.7 Levenshtein==0.20.9 matplotlib==3.9.0 nltk==3.8.1 \
  numpy==1.26.4 pandas==1.5.3 rapidfuzz==2.15.2 \
  rouge-score==0.1.2 tqdm==4.66.4
mkdir -p /tmp/webis-nltk-data/tokenizers
curl --fail --location \
  https://raw.githubusercontent.com/nltk/nltk_data/550b6625bcef1f2abff2ff770a5a0d272c9c6b2a/packages/tokenizers/punkt.zip \
  --output /tmp/webis-nltk-data/tokenizers/punkt.zip
unzip -q /tmp/webis-nltk-data/tokenizers/punkt.zip \
  -d /tmp/webis-nltk-data/tokenizers
```

Run the fail-closed preflight:

```bash
uv run python bench/webis_benchmark.py \
  --official-repo /tmp/webis-wceb \
  --official-python /tmp/webis-scorer/bin/python \
  --nltk-data /tmp/webis-nltk-data \
  --preflight-only
```

Run the complete benchmark:

```bash
uv run python bench/webis_benchmark.py \
  --official-repo /tmp/webis-wceb \
  --official-python /tmp/webis-scorer/bin/python \
  --nltk-data /tmp/webis-nltk-data \
  --output bench/results/webis-full
```

An interrupted run can resume only when the corpus, scorer, production source,
loaded native extension, installed production package versions, selection, and
concurrency fingerprint is identical:

```bash
uv run python bench/webis_benchmark.py \
  --official-repo /tmp/webis-wceb \
  --official-python /tmp/webis-scorer/bin/python \
  --nltk-data /tmp/webis-nltk-data \
  --output bench/results/webis-full \
  --resume
```

For a non-claimable smoke test, add `--limit-per-dataset 1`.

Every checkpoint stores the byte length and SHA-256 of the committed JSONL
prefix. Resume truncates an uncommitted tail, verifies the committed digest,
and cross-checks each restored row's dataset/page identity plus HTML and
reference hashes against the pinned corpus. A crash after the atomic
`pages.jsonl.partial` to `pages.jsonl` rename is also recoverable: resume
recognizes and verifies the complete final artifact before regenerating the
remaining summaries and manifest.

## Metrics and aggregation

- ROUGE-LSum precision, recall, and F1 use the upstream whitespace tokenizer,
  no stemming, sentence splitting, and the upstream empty-target override.
- The second metric invokes upstream `Levenshtein.ratio` on whitespace-token
  lists. Although upstream files call it “distance,” it is a similarity ratio:
  higher is better.
- “Micro” is the mean or median across all pooled pages.
- “Macro” follows the official evaluator: the mean of dataset means and median
  of dataset medians, with each of the eight datasets weighted equally.

The official published-output reference is 0.898844 macro ROUGE-LSum F1 and
0.895533 macro Levenshtein similarity for the best weighted ensemble. The best
single published system, Trafilatura, is 0.883461 and 0.879562 respectively.

## Current local validation

The full 2026-07-27 working-tree run completed all 3,985 pages with zero
extraction errors and zero empty predictions:

| Result | Macro mean | Micro/page-weighted mean |
|---|---:|---:|
| ROUGE-LSum precision | 0.867148 | 0.841455 |
| ROUGE-LSum recall | 0.908456 | 0.879140 |
| ROUGE-LSum F1 | **0.854920** | 0.816145 |
| normalized Levenshtein similarity | **0.849806** | 0.810808 |

The result is 0.028541 ROUGE-LSum F1 below the pinned best single system and
0.043923 below the best weighted ensemble; it is not a SOTA result. Extraction
took 14.356 seconds (277.588 pages/s; p50/p95 7.740/25.145 ms). The official
scorer took 1,507.122 seconds and dominates end-to-end time.

The artifact directory is
`bench/results/webis-full-frozen-20260727-v1/` (ignored by Git). Its source,
native module, official repository, corpus, and NLTK resources were stable
before and after the run. It is marked `FULL_CORPUS_PRELIMINARY` solely because
the production worktree was dirty.

## Artifact contract

Each completed run contains:

- `summary.json` and human-readable `report.md`;
- per-page `pages.jsonl`, including unchanged predictions, official scores,
  latency, strategy, and error state;
- upstream-compatible
  `official-model-outputs/<dataset>/clusy.jsonl`;
- `run-config.json`, checkpoint `progress.json`, and a SHA-256 `manifest.json`;
- `source-snapshot.tar`, making a dirty-worktree result auditable;
- the exact loaded native extension and package initializer inside
  `source-snapshot.tar`;
- exact before/after production source and native-binary hashes. Source or
  native drift during a run invalidates archival claimability.

## Honest interpretation

This is a strong, public, fixed-corpus benchmark, not proof of universal
state-of-the-art crawling:

- It has public labels and no hidden test split.
- Its eight historical datasets have differing annotation policies.
- It measures content extraction from stored HTML, not fetching, JavaScript,
  anti-bot handling, robots policy, or live production reliability.
- Clusy emits production Markdown while labels are plaintext; the harness does
  not normalize that difference away.
- The comparison is against pinned 2023 outputs. Clusy is a current end-to-end
  pipeline with specialized and fallback paths, so this is not an
  apples-to-apples single-algorithm comparison.
- Some systems in the original study had known dataset-training overlap, which
  the paper discusses. Treat rankings as evidence on this corpus, not a blanket
  SOTA claim.
- The upstream repository’s Apache-2.0 license covers its code. Archived HTML
  and annotations originate from eight third-party datasets and websites; the
  benchmark does not grant new redistribution rights for that source content.

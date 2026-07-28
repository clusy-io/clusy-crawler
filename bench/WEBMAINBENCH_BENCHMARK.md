# Commit-pinned WebMainBench v1.1 benchmark

This harness measures the production Clusy HTML-to-main-content extractor on
[WebMainBench v1.1][dataset]. It evaluates frozen HTML snapshots only. It does
not measure fetching, JavaScript rendering, discovery, robots compliance,
deduplication, retry behavior, or production service reliability.

The benchmark has 7,809 human-annotated pages from 5,434 domains. Its coverage
is substantially larger and more diverse than AEB or WCXB, but all labels are
public and there is no hidden test server. A valid result is therefore a
precisely scoped public-benchmark measurement, not proof of universal SOTA.

## Frozen inputs

The runner accepts only these immutable artifacts:

- Hugging Face dataset revision:
  `5da0972e9b58d0c7891ae75053ced97c268f52e3`
- file: `webmainbench.jsonl`
- exact size: `1,354,734,941` bytes
- SHA-256:
  `85765fe798f07c14eb1c92945046eaa56e0da59663f70b9c498647d7dfd78884`
- records: `7,809`
- official evaluator repository: [MinerU-HTML][evaluator]
- evaluator commit:
  `73cf266690befd209cae7e6fdff9716d5b31a976`
- evaluator Git tree:
  `e2d533d7926861a7ff12412d86a7799e4a746c1e`
- `eval_baselines/utils.py` SHA-256:
  `0c65796479a159f8ecbd00eb89c185e0a9ef1853b1ac5962ca577ebd98a6923c`

The dataset revision is bound to its exact immutable blob size and hash. When
Hugging Face local-directory metadata is present, its revision is checked too.
The evaluator must be an unmodified checkout at the exact commit and tree. The
runner imports `calc_rouge_n_score` directly from that verified file and refuses
a copy, rewrite, or mismatched evaluator.

The official repository does not pin its two scoring dependencies. This runner
does, so reruns use:

- `jieba==0.42.1`
- `rouge-score==0.1.2`

## Prepare and run

Download the exact dataset:

```bash
hf download opendatalab/WebMainBench webmainbench.jsonl \
  --repo-type dataset \
  --revision 5da0972e9b58d0c7891ae75053ced97c268f52e3 \
  --local-dir /tmp/clusy-webmainbench

shasum -a 256 /tmp/clusy-webmainbench/webmainbench.jsonl
```

The checksum must be:

```text
85765fe798f07c14eb1c92945046eaa56e0da59663f70b9c498647d7dfd78884
```

Prepare the official evaluator:

```bash
git clone https://github.com/opendatalab/MinerU-HTML.git \
  /tmp/clusy-mineru-html
git -C /tmp/clusy-mineru-html checkout --detach \
  73cf266690befd209cae7e6fdff9716d5b31a976
```

Install the locked project and invoke both required tracks:

```bash
uv sync --frozen
uv run --frozen \
  --with jieba==0.42.1 \
  --with rouge-score==0.1.2 \
  python bench/webmainbench_benchmark.py \
  --dataset /tmp/clusy-webmainbench/webmainbench.jsonl \
  --evaluator-root /tmp/clusy-mineru-html \
  --mode both
```

The production call is fixed:

```python
extract_content(html, url, extraction_profile="balanced").text
```

The extractor receives no reference text, difficulty, language, style, table,
code, equation, or other benchmark metadata. The returned text is scored and
stored unchanged.

For a non-publishable harness smoke test:

```bash
uv run --frozen \
  --with jieba==0.42.1 \
  --with rouge-score==0.1.2 \
  python bench/webmainbench_benchmark.py \
  --dataset /tmp/clusy-webmainbench/webmainbench.jsonl \
  --evaluator-root /tmp/clusy-mineru-html \
  --mode both \
  --offset 0 \
  --limit 8 \
  --concurrency 2
```

Any `--limit`, nonzero `--offset`, omitted track, extraction error, dirty
worktree, or source change produces `NOT_CLAIMABLE.txt`.

## Raw and leakage-safe tracks

WebMainBench's released input HTML contains annotation-tool artifacts,
including `cc-select="true"` on selected ground-truth elements,
`data-anno-uid`, `mark-selected`, `marked-text`/`marked-tail` wrappers, and an
injected `cc-extraStyle` block. This creates a direct label-leak channel even
though common extractors may ignore it.

The runner therefore requires two separately reported tracks:

1. `raw` passes the HTML byte-for-byte as represented by the decoded JSON
   string. This is the official-comparable track.
2. `scrubbed` removes only known annotation attributes, marker class tokens,
   marker wrappers while preserving their text, the injected annotation style
   block, and annotation-only inline `user-select`/selection-outline
   declarations.

Ground truth is never scrubbed or transformed. The scrubber fails closed if
known marker markup remains. `raw_vs_scrubbed` in `summary.json` reports the
paired score delta and exact prediction agreement.

Before either track starts, a label-leak guard scans production Python and Rust
source. It rejects references to benchmark ground-truth field names or
annotation markers. Benchmark code is excluded because it must name the fields
to validate and score them. This static guard is evidence of the input
contract, not a proof against every possible indirect overfit.

## Official metric and aggregation

For every page, the pinned official function:

1. tokenizes reference and prediction with `jieba.lcut`;
2. creates 5-grams with `rouge_score.rouge_scorer._create_ngrams`;
3. computes overlap precision, recall, and F1 with `_score_ngrams`.

The headline precision, recall, and F1 are arithmetic means of the official
per-page values. The runner does not reimplement or normalize the scorer.

`summary.json` includes:

- overall precision, recall, and F1 per track;
- difficulty (`level`), language, style, table, code, and equation breakdowns;
- strategies and extraction error types;
- pages per measured pipeline second and per-page latency distribution;
- process peak RSS;
- source, native module, Python dependency, `uv.lock`, and Cargo lock hashes;
- dataset and evaluator before/after verification;
- raw-versus-scrubbed paired deltas and prediction equality.

The throughput scope is local, closed-loop HTML extraction. Dataset JSON
parsing, official scoring, and artifact serialization are excluded. Scrubbed
pipeline timing includes the mandatory scrub transform. Do not present this as
HTTP crawler throughput.

## Current local validation

The full 2026-07-27 working-tree run completed all 7,809 pages in both required
tracks with zero extraction errors:

| Track | Precision | Recall | F1 | Pipeline throughput |
|---|---:|---:|---:|---:|
| raw Direct-MD | 0.615569 | 0.677841 | **0.606672** | 117.58 pages/s |
| annotation-scrubbed Direct-MD | 0.615698 | 0.676570 | **0.605703** | 56.78 pages/s |

Raw and scrubbed predictions were byte-identical on 7,437/7,809 pages
(95.24%); the scrubbed-minus-raw mean F1 delta was -0.000969. This supports the
label-leak guard but does not make the score competitive: the published
Trafilatura `HTML+MD` row is 0.6402 and the leading model-assisted pipeline is
0.9098. Output-mode differences mean this is a same-data/scorer Direct-MD
measurement, not an unconditional official leaderboard placement.

The artifact directory is
`bench/results/webmainbench/full-frozen-20260727-v2/` (ignored by Git). Dataset,
evaluator, production source, and loaded native module remained stable during
the run. It is non-publishable because the worktree was dirty.

## Streaming, atomic checkpoints, and resume

The 1.35 GB JSONL is never loaded as a corpus-sized Python object. The runner
streams selected rows and keeps at most `--checkpoint-every` records plus the
bounded worker batch in memory.

Each mode writes `pages.jsonl.partial`. A checkpoint is committed only after
all complete JSONL records are flushed and `fsync`ed, followed by an atomic
`progress.json` replacement. If the process stops between those operations,
the next resume truncates only the uncommitted tail and reruns it. Once all
rows finish, the partial file is atomically renamed to `pages.jsonl`.

Resume an interrupted explicit output directory with the exact original
arguments and environment:

```bash
uv run --frozen \
  --with jieba==0.42.1 \
  --with rouge-score==0.1.2 \
  python bench/webmainbench_benchmark.py \
  --dataset /tmp/clusy-webmainbench/webmainbench.jsonl \
  --evaluator-root /tmp/clusy-mineru-html \
  --mode both \
  --output-dir bench/results/webmainbench/20260727T120000Z \
  --resume
```

Resume refuses any change to source hashes, native module, production settings,
dataset/evaluator identity, modes, selection, concurrency, or checkpoint size.
Per-segment extraction time is checkpointed, so resumed throughput remains the
sum of measured extraction segments rather than an invented estimate.

Final artifacts are:

- `run_config.json` — immutable resume fingerprint and starting provenance;
- `label_leak_guard.json` — scanned files, patterns, and result;
- `<mode>/pages.jsonl` — exact prediction, P/R/F1, metadata, diagnostics, and
  reference hash for every page;
- `<mode>/progress.json` — committed checkpoints and measured segments;
- `<mode>/summary.json` — streaming aggregate and breakdowns;
- `summary.json` — final provenance, both tracks, comparison, RSS, and
  claimability;
- `manifest.json` — byte size and SHA-256 of every other final artifact.

## What may and may not be claimed

The current paper's full-protocol comparison points include
DeepSeek-V3.2-in-Dripper at `0.9098`, GPT-5-in-Dripper at `0.9024`,
Gemini-2.5-Pro-in-Dripper at `0.8979`, and Dripper standalone at `0.8779`.
Those LLM entries are block classifiers inside the Dripper pipeline, not direct
raw-page LLM calls.

Clusy is recorded as a direct `MD` output: the benchmark stores
`ExtractionResult.text` unchanged. Several paper rows first produce main HTML
and then use the official `html2text` canonicalizer (`HTML+MD`). Always report
the output mode beside the score; do not silently describe a direct-Markdown
run as the paper's `HTML+MD` configuration.

A result may be described only as, for example:

> Best measured mean ROUGE-5 F1 among the named systems on the exact public
> WebMainBench v1.1 protocol as of YYYY-MM-DD.

That wording is appropriate only after a clean full run, both raw and scrubbed
results, zero extraction errors, exact protocol comparability, and preservation
of the complete artifact directory. A point-score win does not establish
statistical significance when competing systems' per-page predictions are not
available.

Do not claim:

- universal or blind SOTA;
- “better than GPT-5 webpage extraction” without the Dripper qualification;
- production crawler reliability or live-site success;
- broad multilingual superiority—the dataset is predominantly English and
  Chinese despite containing 46 language labels;
- GitHub, arXiv, PubMed, or journal-specific robustness from this corpus alone.

Use the independent peer-reviewed [Webis/WCEB][webis], modern WCXB, live
site-specific regression fixtures, protocol/security tests, and production
load/chaos testing as separate confirmation.

[dataset]: https://huggingface.co/datasets/opendatalab/WebMainBench
[paper]: https://arxiv.org/html/2511.23119v2
[evaluator]: https://github.com/opendatalab/MinerU-HTML/tree/v1.1
[webis]: https://github.com/chatnoir-eu/web-content-extraction-benchmark

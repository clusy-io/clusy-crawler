# WebMainBench 545-page fine-grained benchmark

This harness measures the production Clusy extractor on the official
WebMainBench 545-page fine-grained track. It imports the metric classes from a
verified, immutable checkout of the official toolkit; no metric is copied or
reimplemented in Clusy.

It is complementary to `webmainbench_benchmark.py`, which evaluates the
7,809-page ROUGE-5 track.

## Frozen provenance

Dataset:

- repository: `opendatalab/WebMainBench`
- revision: `5da0972e9b58d0c7891ae75053ced97c268f52e3`
- file: `WebMainBench_545.jsonl`
- records: `545`
- bytes: `109,097,918`
- SHA-256 / Git LFS OID:
  `0efaa4b49a45e320a27fe6e5a0b6aad5b57259fc3321ac3448519cacc74c537e`

Official evaluator:

- repository: `https://github.com/opendatalab/WebMainBench.git`
- commit: `9d991bdc00c57b57521499494d96be85c31317ba`
- Git tree: `c7e3cb66a5318e8cc4bec52dcc511f06e000717c`
- license: Apache-2.0

The runner verifies the dataset bytes, hash, row count, and schema. It also
verifies the evaluator origin, commit, complete Git tree, tracked-file
cleanliness, license, and scorer file hashes before and after the run.

## Protocol boundary

The production call is fixed:

```python
extract_content(html, url, extraction_profile="balanced").text
```

The extractor receives only HTML, URL, and the fixed profile. It never receives
`groundtruth_content`, metadata, or any official metric object. A source guard
rejects benchmark field names and annotation tokens in production Python and
Rust.

Both modes are required for a protocol-valid artifact:

1. `official` applies the exact
   `clean_browser_annotation_artifacts` transform from the verified toolkit,
   matching its `BaseExtractor` wrapper.
2. `scrubbed` removes all known annotation markers using Clusy’s independently
   tested leakage-safe scrubber, while leaving the reference untouched.

The official `MetricCalculator` computes:

- `text_edit`
- `code_edit`
- `formula_edit`
- `table_edit`
- `table_TEDS`
- `overall`, the arithmetic mean of those five aggregate scores

Metric failures for absent content types are handled by the official
implementation and reported with their valid-page denominators.

## Deterministic scorer configuration

The official toolkit optionally uses an external LLM to refine formula
splitting. Its published fine-grained rows do not fully disclose the model
snapshot, endpoint behavior, cache, and complete dependency lock. This harness
therefore forces the official supported `use_llm=False` path:

- no paid API calls;
- no model-weight downloads;
- deterministic regex content splitting;
- exact dependency versions checked at runtime.

A result is claimable only as a dated result on the **official deterministic
offline protocol**. It is never silently treated as numerically comparable to
published rows produced with an unspecified optional LLM splitter.

Pinned metric dependencies:

```text
apted==1.0.3
beautifulsoup4==4.14.3
jieba==0.42.1
openai==2.49.0
python-dotenv==1.2.2
rapidfuzz==3.14.3
```

`openai` is imported by the official splitter module but no client is created
and no request is made when `use_llm=False`.

## Current production baseline

The complete 2026-07-28 working-tree run scored all 545 pages in both required
modes with zero extraction errors:

| Metric | Official offline | Valid pages |
|---|---:|---:|
| `overall` | **0.214089** | — |
| `text_edit` | 0.752301 | 545 |
| `code_edit` | 0.017775 | 100 |
| `formula_edit` | 0.300369 | 281 |
| `table_edit` | 0.000000 | 85 |
| `table_TEDS` | 0.000000 | 85 |

The annotation-scrubbed scores were identical, and the production predictions
were byte-identical for all 545 pages. The official mode completed at 19.44
pages/s including input cleanup, extraction, and official scoring. Production
extraction latency was 11.07 ms p50 and 47.40 ms p95 on this machine.

This run is **not claimable** because the Clusy worktree was dirty. Its complete
artifact is:

```text
bench/results/webmainbench-finegrained/sota-architecture-regression-20260728/
```

`summary.json` SHA-256:
`15acb7b8da3b4f6a36c7f03d62ae65e8aaac5f7a296459b26079436a642032b9`.
The copied artifact was verified byte-for-byte against the original run.
Its `manifest.json` SHA-256 is
`9770f8d4ffcc65683ff96bdd7eb3876a48f18204414213f8ed07e1582ee4bfc2`.

This artifact predates the clean V2 runs. A post-run comparison against public
OSS revision `837dddababc612bfa1ce438307b1e2fb29b4c2f5` matched only 24 of its 44
recorded source files; the 20 differences include this benchmark's runner and
documentation plus production extractor and native-IR files. It is therefore
neither clean V2 evidence nor source-equivalent evidence for the OSS commit.
Retain it only as the explicitly non-claimable architecture diagnostic
described below.

### Architectural diagnosis

The result isolates a structural serialization failure rather than a general
text-selection failure:

- `text_edit=0.7523` shows that useful prose is often retained.
- Of the 88 successfully scored pages with non-empty reference code, only 15
  had any code recognized in the prediction. Code is commonly flattened
  without fenced-block semantics.
- Of the 81 successfully scored pages with non-empty reference tables, zero had
  any table recognized in the prediction. Four additional pages produced
  false-positive table syntax. Some outputs contain pipe-delimited rows but
  omit a valid Markdown header separator, so the official table parser
  correctly treats them as plain text.
- The offline formula score needs caution: 98/119 reference-formula pages had
  a recognized predicted formula, but 162 pages with no reference formula
  produced regex-level false positives. This is exactly the distinction the
  toolkit's optional, incompletely specified LLM refinement attempts to make.

Accordingly, the next quality gate is not another prose threshold tweak.
Ordered IR selection and deterministic reconstruction must preserve semantic
`pre`/`code`, math, and `table` nodes; reconstruction must emit canonical fenced
code and valid GFM tables (including the header separator row). The 545-page
track should become a mandatory structure-preservation regression gate, while
the 7,809-page ROUGE track remains the broad main-content gate.

## Prepare and run

```bash
hf download opendatalab/WebMainBench WebMainBench_545.jsonl \
  --repo-type dataset \
  --revision 5da0972e9b58d0c7891ae75053ced97c268f52e3 \
  --local-dir /tmp/clusy-webmainbench

git clone https://github.com/opendatalab/WebMainBench.git \
  /tmp/clusy-webmainbench-evaluator
git -C /tmp/clusy-webmainbench-evaluator checkout --detach \
  9d991bdc00c57b57521499494d96be85c31317ba

uv run --frozen \
  --with apted==1.0.3 \
  --with beautifulsoup4==4.14.3 \
  --with jieba==0.42.1 \
  --with openai==2.49.0 \
  --with python-dotenv==1.2.2 \
  --with rapidfuzz==3.14.3 \
  python bench/webmainbench_finegrained_benchmark.py \
  --dataset /tmp/clusy-webmainbench/WebMainBench_545.jsonl \
  --evaluator-root /tmp/clusy-webmainbench-evaluator \
  --mode both
```

A development smoke test can select a small prefix:

```bash
uv run --frozen \
  --with apted==1.0.3 \
  --with beautifulsoup4==4.14.3 \
  --with jieba==0.42.1 \
  --with openai==2.49.0 \
  --with python-dotenv==1.2.2 \
  --with rapidfuzz==3.14.3 \
  python bench/webmainbench_finegrained_benchmark.py \
  --dataset /tmp/clusy-webmainbench/WebMainBench_545.jsonl \
  --evaluator-root /tmp/clusy-webmainbench-evaluator \
  --mode both \
  --limit 8
```

Limited, partial-mode, dirty-worktree, changed-source, unstable-input, failed
extraction, and interrupted runs produce `NOT_CLAIMABLE.txt`. Public labels
make this a regression and comparison benchmark, not a blind test. Even a clean
win cannot establish universal crawler SOTA.

Final artifacts include exact per-page predictions and official metric details,
per-mode summaries, source/input/evaluator provenance, the label-leak report,
claimability reasons, and a SHA-256 manifest.

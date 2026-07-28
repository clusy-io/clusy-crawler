# WebMainBench ordered-IR label oracle

> **LABEL_ORACLE — NOT CLAIMABLE.** This diagnostic reads public
> WebMainBench ground-truth annotations to choose blocks. Its output is not a
> model score, production extractor score, leaderboard submission, or SOTA
> result.

`bench/webmainbench_ir_label_oracle.py` measures one pre-training architecture
question: how faithfully can `ordered-dom-ir.v1` represent and reconstruct the
regions that WebMainBench labels as main content?

It does not call `app.services.extractor.extract_content`, change an extraction
default, or register a production route. The runner statically rejects any
`app/**/*.py` import from `bench` before it reads the corpus. A focused test
enforces the same boundary.

## What the oracle does

For each frozen page, the runner:

1. requires both `main_html` and at least one real `cc-select="true"` marker in
   the raw HTML and `main_html`;
2. parses the raw HTML into the bounded native `ordered-dom-ir.v1`;
3. maps annotation markers to selectable units through DOM ancestry and stable
   `data-anno-uid` values;
4. resolves any selectable ancestor/descendant collision deterministically:
   the smallest descendant cover is used when it preserves all marker IDs;
   otherwise the ancestor is the only selected unit;
5. reconstructs complete stored `outer_html` units in DOM order with the
   minimum ancestor skeleton;
6. converts that HTML with the pinned evaluator's own
   `HTML2TextWrapper(bodywidth=0, ignore_links=True, ignore_images=True)`;
7. scores the resulting Markdown with the pinned official
   `calc_rouge_n_score(..., n=5)` and arithmetic-means per-page precision,
   recall, and F1.

No classifier is invoked. The policy selects every representable labelled
region; it does not search all subsets to maximize ROUGE against the reference.
The resulting value is therefore a deterministic policy-level representability
ceiling diagnostic, not a mathematical supremum over arbitrary predictions.

## Frozen protocol

The runner reuses the fail-closed WebMainBench verifier:

- dataset revision:
  `5da0972e9b58d0c7891ae75053ced97c268f52e3`;
- exact JSONL SHA-256:
  `85765fe798f07c14eb1c92945046eaa56e0da59663f70b9c498647d7dfd78884`;
- exact records: `7,809`;
- evaluator commit:
  `73cf266690befd209cae7e6fdff9716d5b31a976`;
- evaluator tree:
  `e2d533d7926861a7ff12412d86a7799e4a746c1e`;
- official scorer dependencies:
  `jieba==0.42.1`, `rouge-score==0.1.2`;
- official HTML canonicalizer source SHA-256:
  `96d5475f48a78061a9ba98fa1a87a12bc7f3d4e83c4ff8269ecb3980f1ebaa36`;
- benchmark-only canonicalizer dependencies:
  `html2text==2025.4.15`, `html-text==0.7.0`.

`html2text` is GPLv3 and remains a transient benchmark-only dependency. It is
not added to Clusy's project dependencies, package, Docker image, or production
request path.

## Run

Install the four transient evaluator dependencies without changing
`pyproject.toml` or `uv.lock`, then run the full corpus:

```bash
uv run --frozen \
  --with html2text==2025.4.15 \
  --with html-text==0.7.0 \
  --with jieba==0.42.1 \
  --with rouge-score==0.1.2 \
  python bench/webmainbench_ir_label_oracle.py \
  --dataset /tmp/clusy-webmainbench/webmainbench.jsonl \
  --evaluator-root /tmp/clusy-mineru-html-src \
  --output-dir bench/results/webmainbench-ir-label-oracle/full \
  --acknowledge-label-oracle-not-claimable
```

The acknowledgement flag is mandatory. For a deterministic smoke sample:

```bash
uv run --frozen \
  --with html2text==2025.4.15 \
  --with html-text==0.7.0 \
  --with jieba==0.42.1 \
  --with rouge-score==0.1.2 \
  python bench/webmainbench_ir_label_oracle.py \
  --dataset /tmp/clusy-webmainbench/webmainbench.jsonl \
  --evaluator-root /tmp/clusy-mineru-html-src \
  --sample-size 100 \
  --sample-seed ordered-dom-ir.label-oracle.v1 \
  --output-dir bench/results/webmainbench-ir-label-oracle/sample-100 \
  --acknowledge-label-oracle-not-claimable
```

Sampling ranks every corpus index by
`SHA-256(seed || NUL || decimal_index)`, takes the smallest hashes, and restores
corpus order. The selected indices and their digest are stored in
`summary.json`.

## Diagnostics

Alongside official score ceilings, the summary reports:

- official metadata breakdowns for level, language, style, table, code, and
  equation;
- ground-truth table/list markup and selected table/list unit categories;
- marker and labelled-character coverage before and after reconstruction;
- labelled markers specifically under table and list contexts;
- whole-block noise forced by mixed labelled/unlabelled selectable units;
- directly unselectable markers versus markers later dropped by reconstruction;
- coarse selectable containers and overlapping selectable-unit collisions;
- input, node, depth, block-count, stored-text, and stored-HTML truncation;
- incomplete reconstruction and selected stored-HTML truncation;
- a ground-truth `main_html` recanonicalization canary, which separates
  canonicalizer/version drift from IR loss.

Table/list losses are particularly important. A marker in direct table-cell
text can be unrepresentable when the same cell contains a separately emitted
semantic descendant. Conversely, a marker inside the bounded inline anchor can
be selected and rebuilt with its table skeleton without including sibling
cell noise. The per-page artifacts distinguish these cases.

## Artifacts and interpretation

Every page row and summary contains:

```json
{"diagnostic":"label_oracle","label_oracle":true,"claimable":false}
```

The artifact directory also always contains
`NOT_CLAIMABLE_LABEL_ORACLE.txt`. Predictions and source HTML are not
persisted; hashes, counts, exact scores, provenance, and a file manifest are.

A low score identifies an IR granularity, hard-limit, or reconstruction
ceiling before classifier training. A high score only shows that labelled
content is representable under this public-label policy. It says nothing about
whether a model can identify those blocks without labels, and it does not
measure fetching, rendering, discovery, robots compliance, latency under load,
or platform reliability.

The corrected deterministic 100-page seed above measured F1 `0.873965`, with
all 100 ground-truth HTML conversions exactly reproducing their stored
reference strings. This sample is useful only as a harness check and
architectural warning.

## Full-corpus diagnostic, 2026-07-28

> **LABEL_ORACLE — NOT CLAIMABLE.** These numbers use public labels directly
> and are not production quality, model quality, or a leaderboard result.

The complete pinned 7,809-page v6 run produced:

| Diagnostic | Pages | Precision | Recall | F1 |
|---|---:|---:|---:|---:|
| Label-selected ordered IR | 7,809 | 0.935255 | 0.855861 | **0.873146** |
| Ground-truth HTML recanonicalization canary | 7,809 | 0.999872 | 0.999872 | 0.999872 |

The canonicalizer reproduced the stored reference string exactly on all 7,809
pages. The canary metric is slightly below one only because the official
ROUGE-5 function returns zero for an identical non-empty string with fewer
than five tokens.

Architecture coverage:

- selectable-unit marker recall: `0.909095`;
- emitted marker recall after reconstruction: `0.872075`;
- selectable-unit labelled non-whitespace character recall: `0.850629`;
- emitted labelled-character recall: `0.788780`;
- pages with an unselectable label marker: `3,087`;
- pages with an unrepresented table/list marker: `1,750`;
- pages with zero selectable labelled units: `18`;
- IR-truncated pages: `1,420`, including `1,377` with stored block-HTML
  truncation;
- selected-unit HTML truncation that dropped labelled content during
  reconstruction: `96` pages;
- coarse selectable containers: `215,415` units on `3,121` pages;
- mixed labelled/unlabelled selected blocks: `3,980` units on `1,393` pages;
- duplicate annotation UID ambiguity: eight UIDs on one page; no marker lacked
  a UID.

The most important score splits are:

| Category | Pages | F1 |
|---|---:|---:|
| Ground-truth table markup | 2,672 | 0.784944 |
| No ground-truth table markup | 5,137 | 0.919023 |
| Unrepresented table/list marker | 1,750 | 0.718478 |
| No unrepresented table/list marker | 6,059 | 0.917818 |
| Coarse selectable container present | 3,121 | 0.823777 |
| No coarse selectable container | 4,688 | 0.906013 |
| IR truncated | 1,420 | 0.780315 |
| IR not truncated | 6,389 | 0.893778 |
| Reconstruction dropped a selected marker | 96 | 0.069433 |
| No selected-marker reconstruction drop | 7,713 | 0.883149 |

The artifact directory is
`bench/results/webmainbench-ir-label-oracle/full-20260728-v6/`. Its immutable
top-level hashes are:

- `pages.jsonl`:
  `2fe3b8db2fdb746b0ce72b9568c82a8952af3d6a3aafab6512dac9ab2d017b38`;
- `summary.json`:
  `49612019919c7853927b1276fa47ca3d9312a7dc2cf3bd9e19854bb74c292767`;
- `manifest.json`:
  `83f63881162a7127cf0d058465eda98e56f2529cd85c4d4875fc5fb6f53b3663`.

The result identifies a real architecture ceiling below the desired target.
The next IR revision should expose bounded child/text-run ranges inside
compound cells, list items, and mixed blocks; make selectable units
non-overlapping by construction; and store lightweight ancestor start/end
tags separately from complete selectable fragments. That removes the dominant
table granularity loss and avoids duplicating huge container `outer_html`
values that currently exhaust the storage budget.

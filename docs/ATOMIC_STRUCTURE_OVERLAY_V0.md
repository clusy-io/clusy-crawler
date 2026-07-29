# Exact Atomic Structure Overlay v0

## Status

This is an additive, development-only component. It is disabled by default and
is not imported by the production extractor, exposed through configuration, or
wired to an API. It makes no model, vendor, or network calls.

An explicitly enabled shadow call can replace only a locally aligned plain-text
code block or simple rectangular data table. Every rejected call returns the
input Markdown byte-for-byte. Digests are deterministic replay identities, not
signatures or trust claims.

## Acceptance contract

Each accepted patch must satisfy all of these conditions:

1. ordered DOM IR v2 is untruncated;
2. the selected `pre` or `table` has a complete reliable source span;
3. a local native selection certificate proves the selected subtree, exact
   text provenance, graph digest, source digest, and deterministic replay;
4. parse errors or unmapped elements outside the selected subtree do not veto
   the local proof, while repair, ambiguity, entities, CR, NUL, or incomplete
   provenance inside it do;
5. the normalized visible-token sequence occurs exactly once in the source
   graph and candidate;
6. candidate alignment uses one precomputed positional visible-Markdown token
   index, not raw Markdown tokens;
7. existing fences, GFM tables, headings, lists, blockquotes, links, inline
   code, HTML tags, and entities are protected from destructive alignment;
8. the replacement has a strict structural gain;
9. prefix and suffix bytes are unchanged;
10. local and global growth and resource limits hold; and
11. the complete output and input have exactly the same normalized visible
    token sequence.

Code output is the native replay, including collision-safe fences. Table output
is deterministic GFM derived from an exact IR grid. A table must have at least
two rows and columns, a complete rectangular grid, an all-header first row, no
later header cells, no spans, nested table, complex block descendant, or empty
cell.

The only parser-inserted table node allowed by the local certificate is a
standards-mandated direct `tbody`. Its rows and cells must remain explicit,
reliable, ordered, source-contained, and grid-complete; inter-row gaps may
contain only HTML whitespace or comments. Invisible ASCII formatting text
between table tags may be ignored, but non-whitespace unmapped text is rejected.
Implicit rows, cells, arbitrary repairs, and entity-decoded text remain closed.

Plain rectangular pipe rows are treated as delimiter syntax for token
alignment, allowing a missing GFM separator to be added. Existing GFM tables
are protected and never rewritten.

## Determinism and bounds

Proposal and decision records bind source, graph, source span, input,
replacement, patch, configuration, certificate, visible tokens, and output
with domain-separated length-framed SHA-256 digests. Replay recomputes the
decision from source and candidate. Timing hooks are observational, excluded
from every digest, and unable to alter a decision.

Source bytes, candidate bytes, visible-token positions, source-token positions,
element relationships, text runs, and table grids are indexed once per call.
Per-atom lookup uses those bounded indexes. Default limits include 4 MiB source,
2 MiB candidate, 4 MiB output, 256 atoms, 200,000 page tokens, 20,000 atom
tokens, 128 rows, 64 columns, 2,048 cells, and bounded replacement,
certificate, total-certificate, and growth bytes.

## Pinned 545-page audit

`bench/atomic_structure_overlay_v0_shadow.py` verifies the pinned dataset,
fixed baseline, evaluator Git tree, evaluator source files, and exact evaluator
dependency versions. It creates and independently replays decisions on two
source tracks:

- `official`: the verified official cleaner; this track is annotation-bearing
  because `cc-select` may remain;
- `scrubbed`: the official cleaner followed by the repository's full
  annotation scrubber and its postcondition.

The tracks must agree on acceptance, exact output bytes, and accepted atom-kind
sequence. Ground truth, metadata, and official metrics are first accessed after
both decision records, replay receipts, and cross-track parity are frozen.
The fresh-run command also preregisters a 180-second dedicated-host wall budget
for 545 pages, two tracks, and one decision plus one independent replay per
track. This is a regression envelope for that stated host protocol, not a
cross-hardware performance comparison.

Pinned inputs:

- WebMainBench 545 SHA-256:
  `0efaa4b49a45e320a27fe6e5a0b6aad5b57259fc3321ac3448519cacc74c537e`;
- fixed 545-page baseline SHA-256:
  `3d4fefffb7d809b703934ce212602d7f52e7c6d1986f884b5b638f36a9b312af`;
- official evaluator commit:
  `9d991bdc00c57b57521499494d96be85c31317ba`.

### Clean Ubuntu x86_64 protocol

Use a dedicated Ubuntu 24.04 x86_64 host with at least four vCPUs. Install
`build-essential`, `ca-certificates`, `curl`, `git`, and `pkg-config`; then pin
uv `0.11.6`, CPython `3.13.5`, and Rust `1.85.0`. From the exact hardened source
tree:

```bash
uv python install 3.13.5
uv sync --frozen --extra dev --python 3.13.5

rustfmt --edition 2021 --check \
  native/src/document_ir_v2/selection_certificate_v0.rs
cargo test --locked --manifest-path native/Cargo.toml
cargo clippy --locked --manifest-path native/Cargo.toml \
  --all-targets -- -D warnings

uv run --frozen ruff check app native/python tests
uv run --frozen ruff check --no-force-exclude \
  bench/atomic_structure_overlay_v0_shadow.py
uv run --frozen mypy app native/python/clusy_native \
  app/services/atomic_structure_overlay_v0.py \
  bench/atomic_structure_overlay_v0_shadow.py \
  tests/unit/test_atomic_structure_overlay_v0.py \
  tests/unit/test_selection_certificate_v0.py
uv run --frozen pytest -q
```

Copy the pinned dataset and fixed baseline to the host, and clone the evaluator
at the commit above without tracked modifications. The runner independently
checks dataset size/hash, baseline hash, evaluator origin/commit/tree/license,
every evaluator file hash, and exact scorer dependency versions.

Run into a new, empty artifact directory:

```bash
uv run --frozen --python 3.13.5 \
  --with apted==1.0.3 \
  --with beautifulsoup4==4.14.3 \
  --with jieba==0.42.1 \
  --with openai==2.49.0 \
  --with python-dotenv==1.2.2 \
  --with rapidfuzz==3.14.3 \
  python bench/atomic_structure_overlay_v0_shadow.py \
  --dataset /data/WebMainBench_545.jsonl \
  --baseline /data/fixed-baseline-pages.jsonl \
  --evaluator-root /opt/WebMainBench \
  --output-dir /artifacts/atomic-overlay-v0-545-fresh \
  --concurrency 4 \
  --max-decision-wall-seconds 180
```

The output must contain `run_config.json`, `summary.json`, `manifest.json`, and
`official/` plus `scrubbed/` `pages.jsonl` and `summary.json` files. A complete
NO-GO run returns exit status 1 but still writes these artifacts; integrity or
protocol failure returns status 2 and must not be reported as a completed
quality result.

Observed on the complete 545 pages:

- 24 accepted pages;
- 48 accepted proposals: 37 code and 11 table;
- zero cross-track parity, replay, visible-token-identity, or fallback-identity
  failures;
- official and scrubbed prediction files contain the same output bytes per
  page; their artifact hashes differ because their source-bound decision
  records differ.

Official offline metric aggregates (`use_llm=False`):

| Metric | Baseline | Candidate | Delta | Gate |
| --- | ---: | ---: | ---: | ---: |
| Overall | 0.214089 | 0.263576 | +0.049486 | ≥ +0.010 |
| Code edit | 0.017775 | 0.158518 | +0.140743 | ≥ +0.030 |
| Table TEDS | 0.000000 | 0.057938 | +0.057938 | ≥ +0.020 |
| Table edit | 0.000000 | 0.047268 | +0.047268 | reported |
| Text edit | 0.752301 | 0.752712 | +0.000411 | non-regression |
| Formula edit | 0.300369 | 0.301442 | +0.001073 | non-regression |

The fixed prediction artifacts have SHA-256:

- official track:
  `ed7b58e6fc0ee9f27c106270d0921ad4023108f9e6a8281e33c97cbacd0c83b7`;
- scrubbed track:
  `fe2f8c7b1234424eda25bb4914d5825cc890d14dc7661a212dfb8a82927b98e2`.

The hashes differ because each row includes source-bound decision metadata; the
prediction field is required to be byte-identical across tracks.

## Decision and limitations

The pinned run is **GO for continued 545-page shadow evaluation** under the
stated gates. It is **NO-GO for production wiring**: the component remains
default-off and unwired.

This result is not a SOTA or vendor-comparison claim. It is a development
experiment scored on public benchmark labels after frozen decisions, not an
independent blind holdout. Coverage is 24/545 pages, and the exact local proof
intentionally rejects many malformed, ambiguous, entity-decoded, nested,
layout, or complex structures.

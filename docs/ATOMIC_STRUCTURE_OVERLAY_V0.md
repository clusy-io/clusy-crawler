# Exact Atomic Structure Overlay v0

## Status

The overlay is an additive research component. It is disabled by default,
unwired from the production extraction cascade, and makes no network, model, or
vendor calls.

When explicitly enabled, it can replace only a locally aligned plain-text code
block or simple rectangular table. Rejection returns the candidate Markdown
byte-for-byte.

No score from an earlier source revision is evidence for the current tree. A
fresh, clean, manifest-bound run is required before reporting current quality.
This component does not by itself support a SOTA, Exa, or Firecrawl claim.

## Safety contract

An accepted patch requires:

1. an untruncated ordered DOM IR v2;
2. a complete and reliable source span for one selected `pre` or `table`;
3. a native local selection certificate binding source, graph, selected
   subtree, exact text provenance, output, deterministic replay, and an
   explicit `local_atomic` wire scope;
4. no repair, ambiguity, entity decoding, CR, NUL, or incomplete provenance
   inside the selected scope;
5. exactly one normalized visible-token match in both source and candidate;
6. alignment against a precomputed positional visible-Markdown token index;
7. protection for existing fences, GFM tables, headings, lists, blockquotes,
   links, inline code, HTML, and entities;
8. strict structural gain;
9. byte-identical prefix and suffix;
10. local and global resource and growth limits; and
11. identical normalized visible tokens for the full input and output.

Code output is the native certificate replay with a collision-safe fence.
Table output is deterministic GFM from an exact rectangular IR grid. Tables
with spans, nesting, complex block descendants, missing grid cells, mixed
header layout, or unsafe parser repair are rejected.

The only parser-inserted table element eligible for local certification is a
standards-mandated direct `tbody`. Its rows and cells must still be explicit,
reliable, ordered, source-contained, and grid-complete.

Digests are deterministic identities. They are not signatures, authorization
tokens, or proof that the input source is trustworthy.

Full-document and local-atomic certificates carry distinct canonical scope
flags and cannot be replayed through the other verifier. Local start tags are
lexically validated against retained DOM attributes; duplicate attributes,
ambiguous unquoted values, entity-decoded values, noncanonical controls, and
unsupported self-closing repair are rejected.

## Resource bounds

The default configuration limits source to 4 MiB, candidate and certificate
totals to 2 MiB, output to 4 MiB, atoms to 256, page tokens to 200,000, atom
tokens to 20,000, tables to 128 rows × 64 columns and 2,048 cells, and
replacement and growth bytes to fixed caps.

The audit records the complete effective configuration. Timing observations are
excluded from decision digests, buffered internally, and published to a caller
hook only after the decision or replay receipt is immutable. Invalid Unicode
and multibyte byte-budget overflow fail closed without an unbounded UTF-8
materialization.

## Pinned 545-page audit

The audit consists of two separate processes:

1. `bench/export_webmainbench_decision_inputs.py` verifies the pinned dataset,
   writes a closed-schema projection containing only index, track ID, and HTML,
   and exits.
2. `bench/atomic_structure_overlay_v0_shadow.py` loads only that projection and
   the fixed baseline during decisions. It opens the label-bearing dataset only
   after both source tracks, deterministic recomputations, integrity checks, and
   exact patch parity are frozen.

The source tracks are:

- `official`: the verified official cleaner; this track remains
  annotation-bearing because `cc-select` may remain;
- `scrubbed`: the official cleaner followed by the repository scrubber and its
  postcondition.

Cross-track parity binds the accepted flag, exact output bytes, candidate
spans, atom kinds, replacement and patch digests, visible-token digest and
count, and replacement byte topology.

Recomputation uses the same implementation. It is a determinism check, not an
independent verifier.

### Quality accounting

The official aggregate is retained as a diagnostic. Claim gates use a paired
545-page conservative aggregate:

- every failed metric result scores zero;
- baseline and candidate success masks must match exactly for every core
  metric;
- all 545 pages remain in every metric denominator; and
- text and formula must not regress.

The preregistered quality thresholds are:

| Metric | Minimum delta |
| --- | ---: |
| Overall mean of five core metrics | +0.010 |
| Code edit | +0.030 |
| Table TEDS | +0.020 |
| Text edit | 0.000 |
| Formula edit | 0.000 |

These are continuation gates for this shadow experiment. Passing them is not a
vendor comparison or a SOTA result.

### Claim modes

| Mode | Requirements | Permitted interpretation |
| --- | --- | --- |
| Exploratory | Opaque fixed baseline and/or dirty source allowed explicitly | Engineering diagnostic only |
| Manifest-bound | Clean exact source, stable inputs, and valid baseline generator manifest | Current-tree shadow result under the stated protocol |

The historical fixed baseline file is opaque and remains exploratory. A
claimable run must generate a new baseline from the same HTML-only projection
with `bench/generate_atomic_structure_baseline.py`.

A valid `clusy.fixed-baseline-provenance.2` manifest binds:

- baseline bytes and SHA-256, the decision-projection SHA-256, and a canonical
  digest over every `(index, track ID, HTML SHA-256)` tuple;
- exactly 545 rows in canonical index order plus a separate page-ID/order
  digest;
- every row's prediction digest, strategy, and success or failure type;
- generator entrypoint, prediction field, fixed single-thread configuration,
  and environment;
- explicit `false` values for reference-label, official-metric, and vendor
  output use during generation; benchmark metadata use is also explicitly
  false;
- clean generator Git commit and tree, full source-file hashes, source digest,
  `uv.lock`, `native/Cargo.lock`, loaded module origins and hashes, loaded native
  binary hash, and the native source binding; and
- the exact generator, projection exporter, manifest validator, native
  certificate, CLI-argument, and schema source bytes;
- a closed, whitelisted environment record with credential values redacted;
- exact pre/post source, projection, configuration, and environment stability.

The audit additionally requires the generator commit, tree, source digest,
locks, native binding, and protocol files to match the candidate checkout.

Missing or malformed provenance makes the run exploratory. It is never inferred
from the baseline contents.

### Pinned identities

- WebMainBench 545 SHA-256:
  `0efaa4b49a45e320a27fe6e5a0b6aad5b57259fc3321ac3448519cacc74c537e`
- historical opaque baseline SHA-256, accepted only in exploratory mode:
  `3d4fefffb7d809b703934ce212602d7f52e7c6d1986f884b5b638f36a9b312af`
- official evaluator commit:
  `9d991bdc00c57b57521499494d96be85c31317ba`

The runner also binds the current Git commit and tree, every executed local
source file, Python and Rust locks, loaded Python module origins, the loaded
native extension binary, and the native extension's packaged source digest. It
verifies source, dataset, evaluator, baseline, manifest, and decision-projection
stability before and after the run.

## Fresh cloud protocol

Use a new dedicated Ubuntu 24.04 x86_64 VM with at least four physical or
dedicated vCPUs. Disable autoscaling and concurrent workloads for a performance
run. Record the cloud, region, instance type, CPU model, image ID, and attached
storage class with the artifacts.

AWS and Azure are both suitable. Prefer a compute-optimized, non-burstable
instance with local or provisioned SSD storage. Do not compare wall time across
different instance families.

Install `build-essential`, `ca-certificates`, `curl`, `git`, and `pkg-config`.
Pin uv `0.11.6`, CPython `3.13.5`, and Rust `1.85.0`, then validate the exact
clean commit:

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
  bench/atomic_structure_overlay_v0_shadow.py \
  bench/export_webmainbench_decision_inputs.py \
  bench/generate_atomic_structure_baseline.py
uv run --frozen mypy --explicit-package-bases \
  app native/python/clusy_native \
  bench/atomic_structure_overlay_v0_shadow.py \
  bench/export_webmainbench_decision_inputs.py \
  bench/generate_atomic_structure_baseline.py
uv run --frozen pytest -q
git status --short
```

The final command must print nothing for a manifest-bound run.

Place the pinned dataset and clean official evaluator checkout outside the
source repository. Baseline and audit artifacts must also be outside the
repository.

Create the HTML-only projection and let that process terminate:

```bash
uv run --frozen --python 3.13.5 \
  python bench/export_webmainbench_decision_inputs.py \
  --dataset /data/WebMainBench_545.jsonl \
  --output /data/WebMainBench_545.decision-inputs.jsonl
```

Generate the baseline from that projection. This generator has no dataset or
evaluator argument, uses the production extractor with a fixed `balanced`
configuration and empty URL, applies the repository annotation scrubber with
its postcondition before extraction, records failures as empty predictions, and
never opens reference or metadata fields. It fails before import when an OpenAI,
Anthropic, Exa, Firecrawl, quality-backend, Elsevier, or IEEE credential path is
active; run it in a credential-free benchmark process:

```bash
uv run --frozen --python 3.13.5 \
  python bench/generate_atomic_structure_baseline.py \
  --decision-inputs /data/WebMainBench_545.decision-inputs.jsonl \
  --output /data/atomic-overlay-v0-baseline.jsonl \
  --manifest /data/atomic-overlay-v0-baseline.manifest.json \
  --expected-records 545
```

Then start the audit as a new process:

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
  --decision-inputs /data/WebMainBench_545.decision-inputs.jsonl \
  --baseline /data/atomic-overlay-v0-baseline.jsonl \
  --baseline-manifest /data/atomic-overlay-v0-baseline.manifest.json \
  --require-claimable-baseline \
  --evaluator-root /opt/WebMainBench \
  --output-dir /artifacts/atomic-overlay-v0-545-fresh \
  --concurrency 4 \
  --max-decision-wall-seconds 180
```

For an explicitly non-claimable diagnostic, omit the manifest and
`--require-claimable-baseline`. Add `--allow-dirty-exploratory` only when a
dirty-tree diagnostic is intentional.

Exit status `0` means every gate passed. Status `1` means a complete NO-GO run
with artifacts. Status `2` means an integrity or protocol failure and must not
be reported as a completed quality result.

Expected artifacts are `run_config.json`, `summary.json`, `manifest.json`, and
an `official/` and `scrubbed/` directory containing `pages.jsonl` and
`summary.json`.

## Promotion boundary

The audit can authorize continued shadow evaluation only. Production wiring,
API exposure, and default enablement require a separate review, broader
adversarial and holdout evaluation, service-level latency and memory evidence,
rollback controls, and an explicit production change.

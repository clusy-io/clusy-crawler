# Exact Atomic Structure Overlay v0

> Research component · disabled by default · not wired to production

The overlay restores structure only when source, DOM graph, candidate text, and
deterministic replay agree exactly. It supports two atomic shapes:

- one complete `<pre>` region, rendered with a collision-safe Markdown fence;
- one simple rectangular data table, rendered as deterministic GFM.

Any failed gate returns the input Markdown byte-for-byte. The component makes
no network, model, or vendor calls.

## Current evidence

This tree has targeted adversarial, unit, static, and Rust verification. It
does **not** yet have a fresh 545-page result. Earlier scores belong to earlier
source trees and must not be presented as evidence for this revision.

No SOTA, Exa, Firecrawl, quality, or throughput claim is valid until the frozen
cloud protocol below completes and its artifact gates pass.

## Acceptance contract

An accepted patch requires all of the following:

1. Complete UTF-8 input and an untruncated ordered DOM IR v2.
2. Zero HTML parse errors anywhere in the document.
3. One complete, reliable source span for a selected `pre` or `table`.
4. A native `local_atomic` certificate binding exact source bytes, graph,
   selection, output, and replay.
5. Exact selected-scope token-event parity between lexical source and DOM.
6. ASCII case-insensitive end-tag names followed only by SP, TAB, LF, or FF.
   CR, NUL, attributes, `/`, declarations, comments, unmatched tags, and
   parser repair are rejected.
7. Exact atom identity after only two explicit normalizations:
   whitespace runs and valid Markdown backslash escapes. Case, Unicode scalar
   sequence, symbols, and punctuation are preserved.
8. Exactly one source occurrence and one unprotected candidate occurrence.
9. Strict structural gain, bounded replacement/growth, and byte-identical
   prefix and suffix.
10. Exact full-output visible identity under the same normalization.
11. Deterministic recomputation through the public verifier.

The only allowed implicit selected-scope element is the standard direct
`tbody` inserted beneath a `table`. Its rows and cells must remain explicit,
ordered, source-contained, and grid-complete.

Digests are deterministic identities, not signatures or authorization tokens.

## API boundary

`propose_atomic_structure_overlay_v0` and
`verify_atomic_structure_overlay_v0` accept no timing callback. They invoke no
caller-controlled observer while a decision or replay is live. Benchmark
timing is measured separately around the public calls inside a fresh isolated
worker.

Every primitive field of `AtomicStructureOverlayV0Config` is copied into a new
instance and revalidated at each public boundary. Mutation of a frozen object
through `object.__setattr__` therefore fails closed.

Default limits include:

| Resource | Default |
| --- | ---: |
| Source | 4 MiB |
| Candidate | 2 MiB |
| Output | 4 MiB |
| Atoms | 256 |
| Page identity tokens | 200,000 |
| Atom identity tokens | 20,000 |
| Table shape | 128 × 64 |
| Table cells | 2,048 |

## Claim architecture

The claim path has four process boundaries:

```text
pinned dataset
    │
    ├─ projection exporter ── dataset index + raw HTML only
    │
    ├─ no-network baseline worker ── frozen baseline artifact
    │
    ├─ no-network decision worker ── frozen decisions + replay receipts
    │
    └─ later scorer process ── labels + official evaluator + frozen artifacts
```

Baseline and decision workers:

- start as fresh `python -I -S -B` interpreters under `env -i`;
- run inside bubblewrap with a distinct network namespace and no non-loopback
  IPv4 or IPv6 route;
- must fail observed IPv4 and IPv6 egress probes;
- receive input through a pipe and source files through sealed memfds;
- see a read-only minimal capsule, not the repository;
- cannot mount or import the dataset, evaluator, benchmark scorer, or labels;
- cannot see task/category IDs, URLs, references, metadata, or benchmark-only
  HTML transforms;
- record the actual environment, namespace, route, mount, interpreter, and
  imported-module evidence;
- bind exact executed Python files, the loaded native extension SHA-256, and
  the extension’s packaged native-source digest.

If Linux user namespaces, bubblewrap, sealed memfds, read-only remounts, or the
no-egress proof are unavailable, the launcher refuses claimability. macOS is
therefore suitable for development but not for a claim run.

The production baseline worker has no callable/config/environment injection
surface. It resolves exactly
`app.services.extractor.extract_content`, pins its module and code bytes, uses
the fixed `balanced` profile, and records every effective `Settings` field.

The decision protocol fixes:

| Setting | Claim value |
| --- | ---: |
| Concurrency | 4 |
| Decision/replay wall budget | 180 s |
| Input | unmodified raw HTML |
| Pages | 545 |

There are no claim-mode CLI overrides. Alternate concurrency or budgets belong
to the permanently nonclaimable legacy diagnostic runner.

## File integrity

Claim inputs are opened by walking every directory component through
`openat`-style, `O_DIRECTORY | O_NOFOLLOW` descriptors, then reading the final
regular file from one `O_NOFOLLOW` descriptor. The protocol checks link count,
device, inode, size, mtime, ctime, byte count, and SHA-256 before and after the
read. Verified data bytes remain in memory and are sent once through worker
stdin; code and module files are supplied through sealed memfds.

Outputs are created with `O_EXCL`, fsynced, made read-only, and published with a
no-replace hard link. Existing paths are never overwritten.

## Quality accounting

The later scorer retains the official aggregate as a diagnostic. Claim gates
use paired conservative accounting:

- all 545 pages remain in every denominator;
- every metric failure scores zero;
- baseline and candidate success masks must match for every core metric;
- text and formula must not regress.

The scorer requires caller-supplied SHA-256 pins for both frozen artifacts. It
reads and validates both through stable descriptors before importing the
benchmark harness or touching labels. It then recomputes the raw-HTML
projection hash from the pinned dataset, so a different 545-row projection
cannot be substituted.

Annotation-scrubbed input remains available only in the permanently
nonclaimable legacy sensitivity runner. It never participates in claimable
acceptance or raw-output selection.

Preregistered continuation thresholds:

| Metric | Minimum delta |
| --- | ---: |
| Overall mean | +0.010 |
| Code edit | +0.030 |
| Table TEDS | +0.020 |
| Text edit | 0.000 |
| Formula edit | 0.000 |

Passing these thresholds authorizes further shadow evaluation only. It is not
by itself a vendor comparison or universal SOTA result.

## Fresh AWS or Azure run

Use a dedicated, non-burstable Ubuntu 24.04 x86_64 machine with at least four
dedicated vCPUs and SSD storage. Record provider, region, instance type, image
ID, CPU model, and storage class. Do not compare wall time across instance
families.

Install the host prerequisites:

```bash
sudo apt-get update
sudo apt-get install -y \
  bubblewrap build-essential ca-certificates git pkg-config \
  python3 python3-venv
```

Create a copied, production-only runtime outside the repository. It must not
contain benchmark, evaluator, dataset, or scorer packages:

```bash
sudo /usr/bin/python3 -m venv --copies /opt/clusy-claim-runtime
uv export --frozen --no-dev --no-emit-project --no-emit-local \
  --output-file /tmp/clusy-claim-requirements.txt
sudo /opt/clusy-claim-runtime/bin/pip install \
  --require-hashes \
  --requirement /tmp/clusy-claim-requirements.txt
sudo install -d -m 0750 -o "$USER" /artifacts
```

Retain the exact `uv --version`, runtime package inventory, cloud image ID, and
instance metadata with the artifacts.

Validate the candidate:

```bash
uv sync --frozen --all-groups --all-extras
uv run ruff check app bench native/python tests
uv run mypy --explicit-package-bases app bench native/python/clusy_native
cargo fmt --manifest-path native/Cargo.toml -- --check
cargo test --locked --manifest-path native/Cargo.toml
git status --short
```

The last command must print nothing.

Probe enforceable isolation:

```bash
uv run python bench/atomic_claim_protocol.py probe
```

Export the closed, label-free projection in a separate process:

```bash
uv run python bench/export_webmainbench_decision_inputs.py \
  --dataset /data/WebMainBench_545.jsonl \
  --output /artifacts/decision-inputs.v3.jsonl
```

Generate the production baseline in its own sandbox:

```bash
uv run python bench/atomic_claim_protocol.py baseline \
  --decision-inputs /artifacts/decision-inputs.v3.jsonl \
  --output /artifacts/baseline.claim.json
```

Freeze decisions and replay receipts in a new sandbox:

```bash
uv run python bench/atomic_claim_protocol.py decisions \
  --decision-inputs /artifacts/decision-inputs.v3.jsonl \
  --baseline-artifact /artifacts/baseline.claim.json \
  --output /artifacts/decisions.claim.json
```

Only after both workers exit, score in a separate process:

Set `BASELINE_SHA256` and `DECISION_SHA256` to the exact artifact hashes printed
by the two preceding commands; do not derive them by reopening the artifact
paths inside the scorer.

```bash
uv run python bench/score_atomic_frozen_decisions.py \
  --baseline-artifact /artifacts/baseline.claim.json \
  --decision-artifact /artifacts/decisions.claim.json \
  --expected-baseline-sha256 "$BASELINE_SHA256" \
  --expected-decision-sha256 "$DECISION_SHA256" \
  --dataset /data/WebMainBench_545.jsonl \
  --evaluator-root /opt/WebMainBench \
  --output /artifacts/score.claim.json
```

## Promotion boundary

Production wiring, API exposure, or default enablement requires a separate
review, broader adversarial and holdout evaluation, service-level latency and
memory evidence, rollback controls, and an explicit production change.

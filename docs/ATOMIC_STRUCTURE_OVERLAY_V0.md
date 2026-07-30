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
- require a copied CPython 3.12 executable;
- give only the baseline worker the single fixed dependency root
  `/opt/clusy-claim-runtime/lib/python3.12/site-packages`; it explicitly adds
  that read-only path under `-S` instead of inferring it from `sysconfig`;
- keep the decision worker capsule/standard-library/native-only, with no
  `site-packages` path added;
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

The exact worker environment requires `PWD=/capsule`. Bubblewrap sets it when
entering the capsule, and both the namespace probe and worker guard compare the
complete environment rather than a subset. The protocol does not claim a
fixed interpreter hash seed: `-I` ignores `PYTHONHASHSEED`. Output identity is
instead independent of hash-table iteration, and fresh randomized-hash
subprocess tests must remain byte-identical.

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
| Per-certificate bytes | 64 KiB |
| Per-page certificate total | 256 KiB |

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

The scorer requires caller-supplied SHA-256 pins for the raw-only projection
and both frozen artifacts. Before importing the benchmark harness or touching
labels, it reads all three through stable descriptors, closes every row and
proposal schema, binds the local native replay primitive to the exact worker
extension SHA-256 and packaged native-source digest, and refuses preloaded
native modules, non-extension loaders, or replaced primitive functions. It
locates the exact capsule-relative extension without importing its package,
stable-reads and hashes those bytes, writes and rehashes a private read-only
snapshot through link-refusing file primitives, and only then initializes the
extension. The frozen replay layer has no fallback native import and accepts
only the built-ins bound from that checked snapshot. It then reconstructs the
bounded graph from each raw HTML row and independently recomputes:

- raw source, baseline input, fixed config, source-span, certificate,
  replacement, patch, proposal, visible-token, output, and decision digests;
- every accepted byte patch and the complete candidate output;
- the complete `pre`/`table` proposal inventory, local eligibility,
  source/candidate uniqueness, exact candidate span, protected-region
  exclusion, aggregate certificate budget, overlap handling, and global
  decision;
- certificate wire scope, source identity, selected ID/order/span, graph
  digest, output length/digest, and canonical encoding against the
  reconstructed raw-source graph.

Code replacement is the authoritative raw-graph certificate replay exactly.
Table replacement is derived from that replay’s canonical native table
fragment, then rendered through the separately frozen GFM escaping contract.
Serialized `certificate_markdown`, `replacement_markdown`, and
`output_markdown` are equality diagnostics only; none supplies replay content.
The scorer retains and scores only its derived output. Rejected proposals carry
no patch or certificate payload whose preimage cannot be replayed.

Only after all 545 rows pass does the scorer import the official evaluator and
labels. It then reprojects the pinned dataset and requires that whole-file
projection SHA-256 to equal the externally supplied pin.

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
  python3.12 python3.12-venv
```

Create a copied, production-only runtime outside the repository. It must not
contain benchmark, evaluator, dataset, or scorer packages:

```bash
sudo /usr/bin/python3.12 -m venv --copies /opt/clusy-claim-runtime
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
uv run pytest -q
uv run ruff check app bench native/python tests
uv run mypy --explicit-package-bases \
  app/services/atomic_structure_overlay_v0.py \
  bench/atomic_baseline_worker.py \
  bench/atomic_claim_protocol.py \
  bench/atomic_decision_worker.py \
  bench/atomic_frozen_replay_v0.py \
  bench/claim_worker_guard.py \
  bench/claimable_sandbox.py \
  bench/score_atomic_frozen_decisions.py
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

The exporter prints one canonical JSON object. Record its `sha256` value
out-of-band as `DECISION_INPUTS_SHA256`; do not derive it inside the scorer.

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
by the two preceding commands. Set `DECISION_INPUTS_SHA256` to the exporter
value recorded before either worker ran. Do not derive any of these pins by
reopening their paths inside the scorer.

```bash
uv run python bench/score_atomic_frozen_decisions.py \
  --baseline-artifact /artifacts/baseline.claim.json \
  --decision-artifact /artifacts/decisions.claim.json \
  --expected-baseline-sha256 "$BASELINE_SHA256" \
  --expected-decision-sha256 "$DECISION_SHA256" \
  --decision-inputs /artifacts/decision-inputs.v3.jsonl \
  --expected-decision-inputs-sha256 "$DECISION_INPUTS_SHA256" \
  --dataset /data/WebMainBench_545.jsonl \
  --evaluator-root /opt/WebMainBench \
  --output /artifacts/score.claim.json
```

## Promotion boundary

Production wiring, API exposure, or default enablement requires a separate
review, broader adversarial and holdout evaluation, service-level latency and
memory evidence, rollback controls, and an explicit production change.

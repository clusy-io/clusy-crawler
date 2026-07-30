# Local-atomic batch certificate bridge v0

Status: research-only, default-off, and outside every production extraction
route. This document describes the isolated candidate built on `8d2d2f5` plus
the atomic-overlay and typed-batch research commits. It is not a quality,
benchmark-leadership, or deployment claim.

## Purpose

The atomic structure overlay previously crossed the Python/native boundary up
to three times per accepted atom:

1. clone the complete IR graph and create one local certificate;
2. clone it again and verify the certificate during proposal evaluation;
3. clone it a third time and replay the certificate while applying the patch.

The bridge keeps the same local policy and exact wire records while amortizing
the graph work across all `pre` and `table` roots.

## Contract

Each native create or verify call performs:

- one immutable graph/source clone;
- one global topology and structure-reference validation;
- one source digest and one complete graph digest;
- bounded per-atom local subtree, ancestor, tokenizer, and serialization
  validation;
- one independently canonical `selection-certificate.v0` record per accepted
  atom, with the `local_atomic` wire-scope flag;
- exact verification and Markdown replay from the certificate-selected root.

A local provenance failure produces an empty, stable rejection record for only
that request index. It does not discard valid siblings. Duplicate IDs,
unbounded inputs, invalid batch shape, or a globally invalid graph fail the
batch boundary. Native and Python checks both enforce atom-count, per-record,
aggregate certificate, per-atom output, and aggregate output limits.
If an aggregate native limit would reject a deterministic tail, the overlay
continues that tail in a fresh bounded batch. Aggregate implementation limits
therefore bound peak work without changing the legacy per-atom eligibility
surface.

Certificate digests are deterministic replay identities. They are not
signatures, authorization tokens, or proof that a source is trustworthy.

## Overlay bridge

The default-disabled overlay now:

1. creates certificates for all enumerated `pre` and `table` roots in one or
   more bounded native batches;
2. preserves every existing eligibility, candidate-alignment, token-identity,
   growth, overlap, and decision-record gate;
3. verifies all ultimately accepted certificates in one or more bounded native
   batches;
4. applies verified replacements in reverse candidate-byte order.

The per-atom legacy bridge remains available only through the private research
function argument `use_batch_certificate_bridge=False`. Locked fixtures cover
accepted code, accepted tables, mixed structures, local provenance rejection,
and the disabled identity path. The full decision dataclass, decision digest,
certificate bytes, output Markdown, and output bytes must match exactly.

## Diagnostic mechanism benchmark

Command:

```bash
python bench/local_atomic_batch_v0_micro.py \
  --atoms 48 \
  --filler-paragraphs 1000 \
  --iterations 11 \
  --warmups 3
```

On the local arm64 development host (Darwin 25.6.0, Python 3.13.5, Rust
1.85.0), the synthetic 54,412-byte fixture produced 1,267 elements, 1,120 text
runs, and 48 alternating code/table atoms:

| Paired mechanism | Legacy median | Batch median | Median speedup | Graph clones |
| --- | ---: | ---: | ---: | ---: |
| Native create + exact replay | 299.801 ms | 16.881 ms | 17.76x | 96 → 2 |
| Complete overlay proposal | 485.016 ms | 50.871 ms | 9.53x | 144 → 2 |

The script refuses to time unless both arms produce identical certificate
bytes, exact native replays, complete overlay decisions, decision digests, and
output bytes. The retained diagnostic JSON is
`bench/local_atomic_batch_v0_micro.sample.json`.

These values are local synthetic mechanism measurements. They are not a
WebMainBench result, a production latency estimate, or a SOTA claim.

## Verification commands

```bash
cargo fmt --manifest-path native/Cargo.toml -- --check
cargo test --locked --manifest-path native/Cargo.toml
pytest -q tests/unit/test_local_atomic_batch_v0.py \
  tests/unit/test_atomic_structure_overlay_v0.py \
  tests/unit/test_selection_certificate_v0.py
ruff check app native/python tests
mypy app native/python/clusy_native
```

Promotion still requires label-free holdout quality evaluation, end-to-end
memory and latency evidence on production-shaped pages, complete platform and
container gates, and an explicit production wiring change.

# Typed atomic overlay batch: phase-one prototype

Status: research-only, default-off, unwired. This note describes an archival
prototype in implementation terms and is not a production or quality claim.

This file records the original full-document phase-one prototype. A later
bridge ports the hardened `pre`/`table` overlay to a batch-native
`local_atomic` scope; see `LOCAL_ATOMIC_BATCH_BRIDGE_V0.md`. `list` and `math`
remain on the stricter full-document prototype path.

## Cost model

The overlay parses HTML once inside one proposal call. Parsing is not the main
per-atom cost. The native certificate boundary is:

```text
NativeDocumentIRV2
  -> OwnedCertificateDocument::from_native
  -> clone Graph + source String
  -> validate topology/structure/source provenance
  -> hash the complete graph
  -> serialize one selection
```

`create_local_atomic_selection_certificate_v0_native` and
`verify_and_replay_local_atomic_selection_certificate_v0_native` each perform
that full clone. The overlay calls both while evaluating every certificate-
eligible atom, then verifies each accepted certificate again while applying
patches. A proposal therefore performs up to `2E + A` full graph clones for
`E` certificate-eligible atoms and `A` accepted atoms. Its public verifier
recomputes the proposal, including another parse and the same clone/validation
work. Candidate tokenization and per-atom Markdown serialization add cost, but
the graph clone, graph digest, and global validation are the dominant repeated
work on structure-rich pages.

## What phase one implements

The prototype adds an independent batch path with these invariants:

- exactly one bounded IR v2 parse per build or verification call;
- one graph/source clone and one batch-wide validation/digest phase per native
  call, rather than per atom;
- outermost, source-ordered, non-overlapping `code`, `table`, `list`, and
  `math` roots only;
- one independently replayable, unchanged `selection-certificate.v0` wire
  record per atom;
- explicit byte source span and a domain-separated source-span digest;
- coordinates and source identities accepted unchanged by the existing exact
  typed source-span lattice oracle;
- deterministic native IR v2 Markdown only—no model and no free generation;
- aggregate certificate/output, atom-count, source, and per-atom output limits;
- default disabled, with no import or call from the production extraction
  cascade.

The batch currently uses the stricter full-document certificate scope. It
rejects a document when unrelated source mapping or parse provenance is
incomplete. It does not yet port the local code/table rescue scope used by the
later bridge.

## Reproduction protocol

Command:

```bash
python bench/typed_atomic_overlay_batch_v0_micro.py \
  --atoms 48 \
  --filler-paragraphs 1000 \
  --iterations 9 \
  --warmups 2
```

The script must verify that every Markdown output and every certificate byte
is identical between arms before timing. Run output is local diagnostic
material and is not authorized as WebMainBench evidence or a SOTA result.

## Missing evidence before any promotion

Phase one is not promotable. It still needs:

1. An exact, source-pinned WebMainBench-545 run with the baseline and decision
   inputs, source manifest, environment, and retained raw artifacts.
2. Page-level and structure-level acceptance, precision/recall/F1 deltas,
   paired confidence intervals, regressions, and rejection-reason counts.
3. The candidate-alignment and all-or-nothing patch layer. This prototype
   certifies typed replacements but does not splice them into crawler output.
4. Exact visible-token preservation across candidate, patch, and final output.
5. A local-provenance extension for list and math. The later bridge covers
   `pre` and `table`, with adversarial tokenizer, scope, tamper, aggregate-cap,
   and unrelated-sibling tests.
6. Independent frozen certificate replay, not only same-implementation replay.
7. Fine-grained WebMainBench latency, p95/p99, peak RSS, output growth,
   certificate bytes, and cold/warm measurements against the locked per-atom
   and batch implementations.
8. Platform contract, container, and deployment gates after a candidate is
   wired. No production route is changed here.

## Original bounded next patch

The next implementation should keep this batch boundary and:

1. lift the local exact-tokenizer checks into a typed
   `code/table/list/math` validator;
2. render each already-validated root directly instead of rebuilding a global
   selection index and traversing all graph roots once per atom;
3. align certified atom tokens to unique candidate byte spans through the
   source-span lattice, reject overlaps, and apply patches in reverse byte
   order;
4. bind candidate span, patch, final output, config, and visible-token digests
   in one deterministic decision record;
5. verify a stored decision with one parse and one native batch replay, without
   recomputing proposal policy.

The later `pre`/`table` bridge implements items 1, 3, and 5 for the existing
atomic overlay. Direct indexed rendering and the remaining typed roots are
still open.

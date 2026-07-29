# Blind live-vendor benchmark v5: synthetic scorer kernel

Status: **`SYNTHETIC_ONLY / NOT_CLAIMABLE`**.

This directory contains synthetic scoring, canonicalization, and paired
bootstrap primitives for a possible future blind benchmark. It contains no
provider adapter, HTTP client, credential reader, live-data loader, arm
unblinding path, or vendor-comparison publisher. Passing its tests establishes
only the behavior of this synthetic kernel.

Every score and bootstrap document carries literal, non-configurable fields:

```json
{"artifact_status":"SYNTHETIC_ONLY / NOT_CLAIMABLE","claimable":false}
```

Changing an imported constant cannot turn those outputs into a claim. The
in-process drift canary described below is useful for detecting accidental
changes, but it is not a security boundary and grants no claim authority.

## Identity and authority boundary

The protocol family is `clusy.blind-vendor.scorer.synthetic.v5`.
`PROTOCOL_VERSION` binds:

- the protocol and claim manifest digests;
- the Python runtime/build and Unicode database identity;
- packaged `kernel.py` and fixture source digests;
- the fixture canonical digest; and
- a stable semantic-code drift-canary digest.

The drift manifest serializes execution-relevant code-object fields and
callable defaults without `marshal`. It is reproducible across clean imports
of the same runtime and detects ordinary function replacement, code/default
replacement, important global rebinding, source changes after import, and
fixture changes after import.
Because the expected values and the check execute in the same process, an
attacker controlling that process can replace both. The manifest is therefore
named and labeled only as an accidental-drift canary.

`external_verifier.py` is a standalone process and does not import or execute
the scorer. Its fixed `external_trust_root.json` would have to bind an external
repository commit, scorer source, assets, protocol manifest, allowed input
commitments, signing-key identity, signature scheme, and signature. The
checked-in root is deliberately `UNSIGNED_NOT_TRUSTED`, all authority fields
are empty, and no public key or signature verifier is approved. Consequently
the verifier emits no artifact and exits unsuccessfully for every input.
Locally editing the root status is insufficient: the absence of an approved
key still fails closed. The standalone entry point validates this root before
it stats or opens the requested artifact path.

All fixed-root and dormant future-authorized file reads require an exact
built-in integer byte cap, reject symlinks and non-regular files, preflight
type/size with `lstat`, recheck the opened descriptor with `fstat`, and stream
at most the cap plus one detection byte. Inode and size changes across the
preflight/read boundary fail closed. There is no unbounded `Path.read_bytes`
path.

This is an explicit blocker, not an attestation placeholder. V5 remains
synthetic-only until an independent release authority and reviewed signing
implementation exist.

## Verified synthetic inputs

`synthetic_fixtures.json` uses schema
`clusy.blind-vendor.synthetic-fixtures.v5`. At import and before scoring or
artifact emission, the kernel:

1. rereads the fixture bytes and checks their import-bound source digest;
2. parses strict UTF-8 JSON while rejecting duplicate keys;
3. checks the exact v5 schema and fixture structure;
4. checks the pinned compact, sorted UTF-8 canonical digest; and
5. requires a unique registered fixture ID.

`score_verified_fixture(id)` is the only verified-input entry point. It loads
candidate, truth, and lies internally and returns a canonical SHA-256
commitment over the fixture schema plus the complete selected fixture.
`score_example(...)` remains available for synthetic diagnostics, but its
artifact provenance is always `caller-supplied-unverified` and is never
claimable.

The fixture canonical SHA-256 is
`c33b3e9a4dd953c95b329b102efede43a244ae0e4c7b5ce2c6ecfd2289217774`.

## Visible-text normalization and budgets

HTML or Markdown rendering into visible text belongs to a future sealed
adapter. This kernel accepts exact built-in Unicode strings only. It rejects
surrogates and raw code-point or UTF-8 budget overflow, then applies:

1. removal of pinned default-ignorables that can block normalization;
2. NFKC, full casefold, then NFKC;
3. one ASCII space per Unicode-whitespace run; and
4. removal of `C*` characters and pinned default-ignorables.

Truth and every lie must normalize to at least five code points, at least one
lie is required, and an empty candidate is a scored failure rather than a
dropped row.

Per-string limits are supplemented by aggregate raw-code-point, raw-UTF-8,
normalized-code-point, and diagnostic-skeleton caps across one example. Public
q-gram functions also require exact built-in strings, validate exact positive
integer sizes, and enforce q-gram work limits. Total window work, whole-output,
LCS, bootstrap, canonical-tree, and canonical-output work is preflighted
against compiled ceilings. Callers may only reduce ceilings through an exact
`KernelLimits` instance. Inputs are rejected rather than truncated.

## Q-gram metrics

The base unit is a character 5-gram multiset with repeated occurrences
retained. For candidate `C` and reference `R`, overlap is
`sum(min(count_C(g), count_R(g)))`; precision, recall, and F1 derive from
integer counts.

### Best bounded window

Best-window scoring examines every start for normalized lengths equal to the
reference length times:

`1/2, 3/4, 1, 5/4, 3/2, 2`

Lengths use positive integer round-half-up, clamp to
`[5, candidate_length]`, and deduplicate. Exact cross-products break ties by
F1, recall, precision, distance from reference length, shorter length, then
earlier start.

For every reference, `max_window_evaluations` charges the complete reference
q-gram `Counter` preprocessing plus candidate scanning for every registered
window length:

`reference_grams + candidate_grams * registered_window_length_count`

This total is checked before either reference or window counters are built. A
short or empty candidate therefore cannot bypass the cost of a large
reference.

### Whole-output and positional fidelity

Whole-output F1 compares the complete candidate and truth multisets, so page
chrome reduces precision. Positional F1 adds one of 64 relative-position
buckets to every q-gram key, detecting broad reorderings even when unpositioned
multisets collide.

### Full-candidate ordered fidelity

Ordered F1 is the exact longest common subsequence of the **complete normalized
candidate** and truth q-gram sequences. It uses a deterministic bit-parallel
recurrence and reports complete-candidate precision. It does not select a best
window first, switch algorithms on a collision predicate, or enumerate
one-edit alternatives. This removes the former window-selection discontinuity:
a local edit changes only its local q-gram neighborhood rather than selecting
a different input sequence for the order metric.

Mask construction and transition word operations are charged and rejected
before large match masks are allocated.

## Unicode confusable diagnostic

The packaged Unicode 16.0.0 `confusables.txt` bytes are pinned to SHA-256
`95bd0aad6dced5ebc63436f459c06ab21a8d107cd842fb57f5c3a1e91bca8611`;
the version header, syntax, and redistribution license are checked/preserved.

The implemented transform is an `internalSkeleton`-shaped diagnostic
(the bound runtime's NFD, the pinned Unicode 16 mapping, then runtime NFD). It is exposed only as
`confusable_internal_skeleton_diagnostic`.

It is **not** represented as the UTS #39 `skeleton` operation. Unicode 16
defines that operation through `bidiSkeleton(LTR, X)`, whose complete
bidirectional dependency is not implemented here. The runtime Unicode database
can also differ from Unicode 16, so this is not a Unicode-conformance claim.
Internal-skeleton leakage is reported as a diagnostic and cannot select
effective lie leakage, truth quality, joint utility, a primary metric, or a
vendor winner.

Effective lie leakage is ordinary normalized best-window leakage only. If `Q`
is the exact-rational harmonic mean of best-window, whole-output, positional,
and full-candidate ordered truth F1, and `L` is ordinary leakage:

- discriminative margin is `Q - L`;
- lie rejection is `1 - L`; and
- joint utility is `Q * (1 - L)`.

`Q`, the margin, and joint utility are reduced exact fractions before float
conversion. Artifact ratios use decimal integer strings and display metrics
use fixed 12-decimal strings.

## Paired bootstrap

Every `PairedObservation` must explicitly provide `unit_id`, `cluster_id`, and
`language_group`; there are no singleton or `und` defaults. A cluster cannot
span language groups, and every language group needs at least two independent
clusters.

The point estimator:

1. orients paired deltas so positive favors the left arm;
2. equally averages units inside each cluster;
3. equally averages clusters inside each language group; and
4. equally averages language groups.

The registered bootstrap uses 10,000 samples and seed `7291337`. Whole
clusters are resampled within each language group. Draws use 64-bit SHAKE-256
rejection sampling, eliminating modulo bias; every attempted block is included
in the stream digest.

The resampling domain binds the paired design but not ordered arm outcomes.
Swapping arms therefore uses identical draws. V5 records positive, tie, and
negative replicate counts plus the exact tie-adjusted probability numerator
`2*positive + tie` and denominator `2*samples`. Arm swapping exactly exchanges
positive/negative counts, preserves ties, and makes the two integer numerators
sum to the common denominator. Mean and interval endpoints are exactly
antisymmetric under the same binary64 inputs.

Bootstrap numeric values accept exact built-in `int` or `float` only.
Oversized integers are range-checked before conversion, and non-finite,
overflowing, subclassed, or out-of-registry values fail closed.

## Canonical JSON

Canonical JSON accepts exact built-in JSON objects/arrays/scalars, sorts object
keys, and emits compact UTF-8 without ASCII escaping. It rejects non-string
keys, surrogates, non-finite floats, negative zero, cycles, excessive depth,
subclassed scalars/containers, and integers outside the interoperable JSON
range. Exact encoded size is computed before full serialization.

## Machine-readable prohibited uses

`CLAIM_MANIFEST["prohibited_uses"]` forbids all of the following for vendor
content or outputs:

- persistence or retention;
- training or fine-tuning;
- distillation;
- calibration;
- prompt or scorer tuning;
- model or scorer selection; and
- vendor-winner publication.

These prohibitions are machine-readable protocol content, not advisory prose.
A future benchmark would additionally need externally committed pages,
truth/lies, language and cluster metadata, anonymous-arm mapping, adapter
behavior, failure completion rows, timing/cost gates, input commitments, an
independent signed trust root, and post-gate unblinding. None exists in v5.

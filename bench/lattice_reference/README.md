# Typed source-span lattice reference

This directory is an unwired research oracle. Nothing under `app/` imports it,
and it does not change crawler output.

The candidate representation is source-backed:

```text
(source_start, source_end, source_identity, block_id, ancestor_block_ids,
 type, granularity, base_score, type_logit)
```

Interpretations sharing one canonical source identity are marginalized locally
with a stable log-sum-exp. Those frozen local posteriors define expected
type-transition features. The top-1 decoder then solves an exact longest-path
problem over the monotone non-overlap DAG for that stated scoring function. Its
score includes node evidence, selected coverage, source gaps, fragmentation,
posterior-expected type compatibility, and posterior-expected heading-to-body
continuity. Ties are resolved by coverage, path cardinality, and source
identity, in that order.

The numerical contract is part of the objective, rather than an implementation
detail:

1. every public-entry budget, weight field, candidate field, identifier, and
   tuple is read once and snapshotted into an exact built-in
   `int`/`float`/`str`/`tuple` or a fresh base dataclass before computation;
   base `str.__len__` and `int.bit_length` preflights prove that copying a
   string/int subclass fits its caller-appropriate bound before the base copy
   is allocated;
2. candidate scores, posterior `exp`/`log` results, coordinates, budgets,
   ancestry references, strings, and weights are validated before decoding;
3. local log-sum-exp and posterior primitives are finite binary64 operations;
4. the resulting marginal scores, posterior masses, and frozen weights are
   lifted with their exact binary64 values into rational arithmetic;
5. node, transition, total-path, and tie comparisons use that exact arithmetic;
   and
6. the selected total is rounded to binary64 once for `DecodedPath.score`.

Snapshotting deliberately calls the built-in base operations for numeric,
string, and tuple subclasses, so a stateful `__int__`, `__float__`, `__str__`,
`__iter__`, comparison, or hashing override cannot change validation versus
scoring. String subclasses are length-checked against the remaining shared
codepoint budget before `str.__str__` can allocate a base-string copy. Integer
subclasses are bit-length-checked before `int.__int__`: coordinates and budgets
use the bit length of their compiled hard bound, while integer-valued binary64
inputs use the 1,024-bit finite-conversion ceiling and still undergo the exact
conversion/finite check afterward. `DecoderWeights` is copied to the exact base
dataclass and validated by the base-class method; subclass method overrides are
never dispatched. As with any in-process Python API, obtaining an iterable item
or reading an object attribute may itself execute caller code; the contract
begins with that single read and guarantees the caller-owned field is never
consulted again.

A catalog is rejected if a local primitive or the exposed final score is not
finite. This prevents accumulation order, overflow, or a second scoring
implementation from changing top-1. Posterior masses are exactly renormalized
after being lifted, so the transition objective uses a proper frozen local
distribution even when the displayed binary64 probabilities sum to only
approximately one.

This is not exact global marginalization of a CRF whose type variables are
coupled by transition potentials. That stronger model would need a joint
span-and-type state space (or a separately proved elimination scheme). The
reference deliberately makes the cheaper local-posterior contract explicit so
an implementation cannot inherit a stronger, false exactness claim.

The reference is polynomial only because it rejects non-canonical source
catalogues: a source identity must map to one span, a block identity must map
to one span plus one granularity and represented-ancestor set, every referenced
ancestor ID must itself be represented, and represented ancestors must contain
represented descendants. Duplicate and dangling ancestor IDs fail closed.
Multiple source identities may alias one block only when all that block
metadata agrees; the decoder still selects at most one alias. These invariants
make duplicate-source and ancestor/descendant exclusion equivalent to interval
exclusion. Supporting arbitrary repeated “colours” would require a different
formulation and must not be hidden behind a supposedly exact low-cost decoder.

Exact rational arithmetic has an additional promotion boundary. In one decoder
call, every accepted numerator and denominator, plus every pre-reduction
integer product created by result-producing arithmetic, is strictly below
`2**16384`. Exact ordering/equality uses the standard-library `Fraction`
comparison on canonical snapshots. Such a comparison may create numerator-
denominator cross-products, each strictly below `2**32768`; comparisons do not
consume admission fuel. A valid public binary invocation whose widest input
component has `b` bits consumes `4*b²` deterministic admission-fuel units; a
binary64/integer lift or unary invocation consumes `b²`. Charging happens after
input/canonical validation (and division-by-zero rejection) but before any
algebraic fast path or result-producing arithmetic. The shared call limit is
`2**42` units. Addition uses the denominator GCD, and
multiplication/division cross-cancel, before any result-producing product. Each
such product is proved to fit the component ceiling before it is allocated.
Admission fuel is only a deterministic policy brake: it is not a model or
upper bound for validation, comparison, GCD, multiplication, allocation, bit
complexity, or wall-clock work. A catalog whose exact denominator would exceed
the component ceiling therefore fails closed before that denominator is
materialized. Within the accepted component/admission domain, the operations
are algebraically identical to `Fraction` and no approximation, rounding, or
denominator truncation is introduced.

An exact-type check alone is insufficient for Python `Fraction`: its slotted
components can be replaced through low-level `object.__setattr__`. Every
arithmetic entry therefore reads the caller-owned numerator and denominator
once, requires exact base `int` components, validates a positive denominator
and canonical lowest terms (including `0/1` as the sole zero form), and rebuilds
a fresh base `Fraction`. Arithmetic, comparisons, admission charging, and
algebraic fast paths use only that rebuilt snapshot.

The DP stores predecessor indices, cached score/coverage/cardinality metadata,
preindexes each validated ancestor tuple as an exact immutable set, and
reconstructs only the selected path. The ancestry index makes ordinary
compatibility an expected `O(1)` lookup instead of rescanning the tuple for
every DP pair. An exact binary-lifting index over the finalized backpointer tree
finds the longest common prefix of two tied paths and compares their first
different source identity in `O(log n)` time. It uses no hashes or probabilistic
fingerprints.

Let `c` be the raw candidate count, `a` the total number of raw ancestor
references, `s` the total snapshotted string codepoints, `w` the total weight
entries, `n` the marginalized span count, `t` the maximum latent type count,
`ℓ` the maximum identifier length, and `q` the number of comparisons that reach
the final lexicographic tie-break. The polynomial decoder has
`O(s + w + (c log c + a log a) * ℓ + n² * t² + q * (log n + ℓ))`
character/arithmetic/comparison operations and
`O(s + w + c + a + n log n)` retained memory. Under unit-cost identifier
operations this reduces to
`O(c log c + a log a + n² * t² + q log n)`. Since `q = O(n²)`, its unit-cost
worst case is `O(c log c + a log a + n² * (t² + log n))`. Ordinary transitions
never copy path tuples. Exact-rational operand sizes and call counts are
separately bounded by the component ceiling, candidate ceilings, and fixed
decoder call graph. The admission-fuel policy is not substituted for a
bit-complexity proof.

Resource ceilings are compiled into the reference and caller-provided budgets
may only lower them. General marginalization/DP accepts at most 4,096 raw
candidates, 65,536 total raw ancestor references, 10,000,000 document
characters, 24-bit non-negative source coordinates, 1,000,000 total
snapshotted string codepoints, 1,024 total weight entries, 16,384-bit rational
components, and `2**42` rational admission-fuel units. Integer-valued binary64
inputs have a separate 1,024-bit preflight. For decoding, the string ceiling is
shared by weights and candidates; standalone marginalization spends it only on
candidates. The exhaustive oracle has the tighter non-bypassable limits of 16
marginalized spans and 64 raw candidates; it rejects an oversized requested
budget before consuming the candidate iterable. An absent type-compatibility
table and a zero heading-to-body weight skip their mathematically null
posterior transition work. These limits bound hostile iterables, giant
integers/identifiers, ancestry metadata, latent-type and weight-table expansion,
rational denominator growth, and the oracle's exponential search; the oracle
remains intended only for small synthetic catalogs. Within that hard ceiling,
it computes each independent exact node/transition value once in an `O(n²)`
cache before enumerating subsets, rather than repeating `t²` posterior
arithmetic inside every subset.

The exhaustive oracle is deliberately independent of the DP compatibility,
transition, path-state, and tie helpers. It performs a fully pairwise
feasibility check. The small DP probe is:

```bash
python -m bench.lattice_reference.benchmark_decoder --spans 512
```

The ablation baselines are named by their actual ranking rule:
`score_first_greedy_decode` ranks frozen node scores first and
`source_order_greedy_decode` ranks canonical source order first.
`greedy_decode` remains only as a compatibility name for the score-first
variant. Each baseline starts with the empty path and accepts a feasible
addition only when the exact full-path score/coverage/cardinality/lexicographic
contract improves. Consequently no baseline is forced to emit a negative-score
straw path.

## Innovation boundary

Weighted interval scheduling, semi-Markov decoding, dynamic programming, and
ordered block decoding are not new. The research contribution under test is
the web-specific combination of:

1. typed, overlapping, multi-granularity source spans;
2. calibrated local latent-type marginalization rather than independent
   hard type heads;
3. fail-closed source identity and ancestry invariants;
4. exact source-faithful reconstruction constraints; and
5. quality/structure/latency evidence against frozen independent, greedy, and
   union/max ablations.

No novelty or SOTA claim is made by this reference implementation.

Primary prior-art anchors include
[Web2Text](https://arxiv.org/abs/1801.02607) for structured sequence decoding,
[NeuScraper](https://aclanthology.org/2024.acl-short.72/) for neural web
extraction, [HtmlRAG](https://arxiv.org/abs/2411.02959) for structure-preserving
HTML processing, and
[Beyond a Single Extractor](https://aclanthology.org/2026.findings-eacl.307/)
for evidence that extractors have complementary coverage and materially
different table/code behavior.

## Preregistered promotion hypotheses

H1 compares fixed segmentation, union/max candidates, and the marginalized
lattice on frozen WCXB development pages. The lattice must improve overall
macro F1 by at least 1.5 points, improve the weak page-type mean (service,
collection, listing, product) by at least 3 points, keep article precision loss
within 0.5 point, and beat union/max under a paired 95% bootstrap interval.

H2 freezes all candidate scores and compares independent thresholding,
source-order greedy selection, and exact decoding. Exact decoding must reduce
duplicate, inversion, and ancestor-overlap diagnostics by at least 80%, improve
WebMain ROUGE-5 by at least 0.5 point and WCXB macro F1 by at least 0.5 point,
while keeping the future Rust decoder below 5 ms p95 or 10% of end-to-end
latency.

Neither hypothesis may be tuned on the held-out public-test or vendor result
aggregates.

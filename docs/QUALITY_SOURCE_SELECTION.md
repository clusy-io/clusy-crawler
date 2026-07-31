# Verified quality-source selection

The optional quality lane uses the pinned MinerU-HTML adapter as a block
classifier. The model labels bounded `_item_id` values as `main` or `other`;
it does not receive authority to generate the returned page text. A
deterministic converter serializes the selected HTML.

Clusy accepts that path only after constructing a
`quality-source-selection.v0` receipt:

1. the simplified prompt and mapped DOM contain the same unique, contiguous
   item catalogue;
2. the exact raw response is bound into the receipt and strictly parsed as the
   configured JSON or compact response contract;
3. the raw response and the adapter's parsed labels agree exactly, with every
   item labeled once using only `main` or `other`;
4. at least one item is selected;
5. Clusy independently prunes the mapped DOM using the selected IDs;
6. the replayed DOM equals the pinned adapter's selected DOM; and
7. the receipt binds the source, raw response, simplified DOM, mapped DOM,
   complete labels, selected DOM, upstream revision, prompt profile, and
   response format with SHA-256 identities.

Any missing, malformed, partial, duplicate-key, compact-order-violating, or
replay-divergent selection fails closed to the already-computed deterministic
candidate. JSON object member order is semantically irrelevant and accepted.
Bounded token grounding, source ordering, minimum-content, duplication,
structure-regression, output-size, concurrency, timeout, and circuit-breaker
checks still apply after receipt verification.

Successful response metadata exposes:

- `source_selection_schema`;
- `source_selection_receipt_sha256`;
- `source_selection_item_count`;
- `source_selection_selected_count`; and
- `source_selection_replay_verified`.

Model-assisted results are eligible for Redis only when this provenance is
complete and the operator supplies an immutable backend revision. The receipt
digests establish deterministic identity, not authentication, page
authorization, or truth.

## Boundary

The current receipt binds a parser-repaired, source-derived mapped DOM. It is
stronger than post-hoc Markdown grounding, but it is not yet an original-byte
span certificate. The native `ordered-dom-ir.v2` and selection-certificate
work remains the path to byte-offset provenance for every selected structure.
No SOTA or universal-quality claim follows from this safety contract.

# Verified quality-source selection

The optional quality lane treats its model as a bounded block classifier, not
as a text generator. The model labels source-DOM `_item_id` values `main` or
`other`; Clusy owns selection replay and the returned Markdown.

## Production receipt

Accepted quality output uses
`quality-source-selection-serialization.v1`. The mint runs inside the existing
bounded quality worker and closes this chain:

1. Pin MinerU-HTML `1.1.2` to source revision
   `73cf266690befd209cae7e6fdff9716d5b31a976`, preprocessing cutoff `500`,
   and one of two prompt contracts: `openai_json → (v2, json)` or
   `mineru_compact → (short_compact, compact)`.
2. Rerun the pinned preprocessor from the raw page and require its simplified
   and mapped HTML strings to equal the upstream artifacts exactly.
3. Strictly parse the complete raw model response, require it to equal the
   adapter's complete labels, and rebuild the v0 selection receipt from the
   independently derived DOM.
4. Replay selected pointers from that DOM and require the canonical selected
   HTML to equal the adapter's selected HTML.
5. Apply the fixed structural work admission below, then run the pinned
   `mineru-webkit==0.1.6` `mm_md` converter once under a process-wide lock.
   Upstream conversion is disabled with `output_format="none"`; the mint's
   local output is authoritative.
6. Bind the full nested receipt, raw-source identity, URL identity, selected
   DOM, output bytes and identity, stage pins, and verification flags in a
   closed canonical identity.
7. Authenticate that identity with a domain-separated, ephemeral
   process-local HMAC-SHA256 capability.

The acceptance boundary checks the exact receipt type, every closed field,
the current raw page, URL and output identities, the full nested receipt, and
the HMAC with `compare_digest`. It does not re-import or rerun mutable third-
party stages on the event loop. A self-consistently rehashed public dataclass
cannot mint the capability.

For authenticated v1 output, the lossy token-grounding and source-order
heuristics are redundant and may be skipped. Minimum content, unsafe
structure, unbalanced fences, duplication, structure regression, output caps,
concurrency, timeout, and circuit-breaker gates still apply. Any failure keeps
the already-computed deterministic candidate.

### Serializer work admission

Before MinerU-HTML is initialized, raw source is capped at 1,000,000
characters, 5,000 parsed elements, depth 64, and 8,000 text fragments. The
operator input setting may lower the character limit but cannot raise this hard
ceiling. Caller-supplied `cc*` tags, `_item_id`, `data-uid`, and every
attribute whose local name starts with `cc-` are rejected before preprocessing.
This bounds the pinned preprocessor's recursive copies and descendant scans and
prevents source markup from colliding with its internal pointer namespace.
Structural ineligibility does not count as a backend outage or open the circuit breaker.
`QualityExtractor` applies this admission before breaker and capacity state;
the trusted mint repeats it so its safety contract does not depend on caller
discipline.

The converter's final output check cannot bound work already performed. In
particular, MinerU-Webkit 0.1.6 can rasterize inline SVG, pad a sparse table to
`rows × maximum_columns`, repeat a long base URL for every relative image, and
emit indentation proportional to list depth. The mint therefore rejects unsafe
structure before loading or locking the converter:

| Input or work unit | Fixed v1 admission |
| --- | --- |
| Source URL | At most 4,096 characters, matching fetch and request admission |
| Selected visible text | At most `min(configured output cap, 256 KiB)` |
| Canonical selected DOM | At most `min(768 KiB, 2 × output cap + 256 KiB)` |
| Parsed DOM | At most 20,000 elements, depth 64, and 20,000 text fragments |
| Lists | Depth 32; cumulative marker/indentation projection is capped |
| Simple-table work | Aggregate slot cap `max(64, min(100,000, floor(output cap / 3)))`; the combined work projection must still fit the output cap |
| Images | Cumulative base-URL and attribute expansion is capped before URL resolution |
| Code recognition | Global element/fragment scan-work projection at most 4,000,000 |
| Plain-text formula parsing | At most 2,048 pinned MathJax delimiter/environment tokens, 8,192 LaTeX control/group tokens, and group depth 128 outside MathJax skip tags |
| Active/internal markup | Scripts and every `cc*` tag at the selected-DOM boundary are rejected |
| Raster/formula markup | Inline SVG, MathML, and recognized KaTeX/MathJax/TeX signals are rejected |

The pinned converter selects host-specific formula macro expansion with a raw
URL substring check for `mathinsight.org`. The quality serializer therefore
rejects a conservative case-insensitive superset of every matching URL,
including occurrences in lookalike hosts, userinfo, paths, or query strings.
Rejected pages keep the deterministic candidate, whose ordered DOM path handles
code and math without granting the optional model or converter authority over
returned text.

MinerU-HTML may create `cc-alg-uc-text` while mapping mixed inline/block
source. The pinned mapper and Clusy's independent replay both remove that
wrapper before serialization. The quality image exercises this real mixed
content path; the selected-DOM boundary therefore remains closed to all `cc*`
tags rather than relying on an allowlist.

The preflight also computes a combined bounded-work projection from canonical
markup, visible text, entity escaping, element wrappers, bounded formula-token
work, image expansion, table materialization, and list indentation and requires
it to fit the configured output cap. The exact serialized text is checked
against the same cap afterward. The quality image's no-network smoke runs real
accepted table/list/image/code/entity-escaping and mixed-content cases through
the pinned stages and verifies that their output stays within the projection;
it also verifies that representative SVG, table, image, formula, and code work
amplifiers are rejected before conversion.

The 256 KiB visible-text ceiling is a conservative initial-production admission
constant, not a latency guarantee by itself. Queueing, remote inference, local
preprocessing, and serialization all share the quality request deadline. A
timed-out worker retains its concurrency permit until it actually exits; no
permit is forged and the deterministic candidate remains available.

## Legacy receipt

`quality-source-selection.v0` remains readable for compatibility. It binds the
source, raw response, simplified and mapped DOMs, labels, selected pointers,
selected DOM, revision, profile, and response format with deterministic
SHA-256 identities. Those hashes are not authentication, so legacy v0 output
continues through the stricter token-grounding and source-order gates but is
never admitted to the persistent crawl-result cache.

## Exposed provenance

Successful response metadata exposes only non-secret provenance:

- `source_selection_schema`;
- `source_selection_receipt_sha256`;
- `source_selection_item_count`;
- `source_selection_selected_count`; and
- `source_selection_replay_verified`.

The process MAC and selected DOM are not included in API responses, logs, or
the persistent cache projection. Redis receives accepted text and public
metadata only after verification and remains a trusted infrastructure
boundary. A different cache-integrity design is required before treating
Redis as adversarial.

## Threat and lifecycle boundary

The HMAC is a short-lived in-process capability, not a portable signature.
The key is generated lazily, rotates after fork or process restart, and makes
outstanding receipts from another worker invalid. Receipts must not be queued,
pickled, or verified across processes. Exact replay of an unchanged receipt in
the same process is harmless because it remains bound to the same source, URL,
and output.

This design assumes reviewed application code, a commit-bound container, and
root-owned dependencies. It protects the service from admitted untrusted pages,
model responses, and caller-constructed receipt objects; it does not protect a
process whose Python code, imports, or image has already been compromised.

[MinerU-HTML](https://github.com/opendatalab/MinerU-HTML) and
[MinerU-Webkit](https://github.com/ccprocessor/MinerU-Webkit) are pinned
optional dependencies licensed under Apache-2.0. No model weights are bundled
in the image.

The receipt proves source derivation and deterministic serialization, not that
the selected content is relevant, complete, truthful, or state of the art.
Original-byte span provenance remains the separate `ordered-dom-ir.v2` and
selection-certificate research path.

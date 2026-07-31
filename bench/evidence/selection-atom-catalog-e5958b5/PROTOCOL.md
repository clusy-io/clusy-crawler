# Ordered source-text mapper and selection-atom catalog protocol

> Archival diagnostic · non-authorizing · HTML-only · default-off research API

This protocol can measure two narrow properties of the opt-in
`selection-atom-catalog.v1` path:

1. whether a page can be represented as a complete, source-backed atom
   catalog; and
2. local single-process mechanism cost from decoded HTML to the completed
   catalog.

It does not run a WebMainBench scorer, use labels, fetch or render pages, call
a vendor API, or measure the crawler service boundary. The retained
`report.json` is an archival research receipt and is not registered or
authorized for public result claims.

## Frozen input

The input is the exact 545-row HTML projection used by the local inference
campaign, not the upstream benchmark JSONL:

| Property | Value |
| --- | --- |
| Rows | `545` |
| JSONL bytes | `88,727,247` |
| JSONL SHA-256 | `e5958b541d844cf011e66e214bf64abb742aec6922e3c32321e2abaf7cf2c735` |
| Required row keys | `html`, `html_sha256`, `row`, `track_id`, `url` |
| HTML bytes per complete sweep | `84,446,528` |
| Labels present or read | no |

The runner verifies the complete file hash and every per-row HTML hash. It
rechecks the complete file identity after the measured sweeps and rejects the
run on any drift.

## Representation-coverage contract

Coverage means only that the all-or-nothing catalog accepted the page. It is
not extraction precision, recall, content selection quality, Markdown quality,
or a benchmark score.

Each sweep must record accepted and rejected pages, rejection reasons, atom
kind counts, transformed spans, and a complete output commitment. Repeated
sweeps must agree before the report is considered internally consistent. No
comparison with an unretained executable or source snapshot is permitted.

## Mapping contract

`ordered-source-text-map.v2` scans bounded raw UTF-8 source order and pairs
retained DOM text runs by direct parent, decoded lexical identity, and order.
It records exact raw byte offsets, raw fragments, decoded and raw digests, and
per-span deterministic certificates. Named and numeric character references,
newline normalization, and repeated same-parent text are explicit transforms.

The mapper rejects the complete map when non-whitespace reorder, foster
parenting, structural repair, malformed crossing, or optional-end ambiguity
violates retained explicit-element mapping or the direct-parent,
decoded-identity, and order bijection. Standards-defined implicit structure
such as an inserted `tbody` remains eligible when that contract stays exact.
Incomplete/truncated source and every source/event/run/fragment/stack budget
failure reject the map. Parser-reparented whitespace may be omitted only when
it is exact HTML whitespace outside a whitespace-preserving context; every
skipped source-token identity and reason, skipped DOM-run identity, and both
counts are bound into the map digest. Digests identify content; they do not
authenticate it.

Each catalog atom's source span must be contained by its typed closure span.
`text_run_id` is the narrow lexical replay pointer. `selection_id` is typed
closure metadata subject to the document ledger's verified replay policy; it
does not decide which grouped atoms or enclosing typed structure a downstream
selector must retain.

## Mechanism-timing protocol

The runner used one process and one thread of catalog work:

1. import the package;
2. bind repository Python bytes, the loaded extension, and the complete native
   source inventory;
3. hash the complete input;
4. run one fixed first-row cold probe before any earlier catalog invocation;
5. run the first 16 rows as unmeasured warm-up; and
6. stream three complete forward sweeps, timing every catalog call separately.

The timed call is
`build_selection_atom_catalog_v1(html, enabled_config)`. It includes
`ordered-dom-ir.v2` extraction, `ordered-source-text-map.v2`, catalog
validation, atom construction, and digests. It excludes process startup,
imports, JSONL I/O/parsing, per-row input hashing, network, rendering, scoring,
serialization, and vendor APIs.

The report records catalog-call wall time, page latency distribution,
throughput, process-lifetime peak RSS, runtime environment, loaded extension,
repository modules, and before/after source identities. These are
machine-local diagnostics, not portable service-performance evidence.

## Reproduction

Using a repository-local environment and any local input copy matching the
frozen SHA-256 above:

```bash
.venv/bin/python bench/selection_atom_catalog_representability.py \
  frozen-input.jsonl --runs 3 --warmup-pages 16 \
  --output bench/evidence/selection-atom-catalog-e5958b5/report.json
```

The complete canonical result is retained in [report.json](report.json).

## Claim boundary

This archival record does not authorize a public measurement. It does not
support a WebMainBench quality claim, a SOTA claim, a vendor comparison, an
end-to-end crawler latency claim, production throughput, or a deployment
claim. The catalog remains disabled by default and does not change the crawler
response schema.

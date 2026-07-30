# Ordered source-text mapper and selection-atom catalog diagnostic

> Diagnostic · HTML-only · default-off research API · not a quality or SOTA result

This record measures two narrow properties of the opt-in
`selection-atom-catalog.v1` path:

1. whether a page can be represented as a complete, source-backed atom
   catalog; and
2. local single-process mechanism cost from decoded HTML to the completed
   catalog.

It does not run a WebMainBench scorer, use labels, fetch or render pages, call a
vendor API, or measure the crawler service boundary.

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

## Representation coverage

Coverage means only that the all-or-nothing catalog accepted the page. It is
not extraction precision, recall, content selection quality, Markdown quality,
or a benchmark score.

| Catalog observation | Accepted | Coverage | Rejections |
| --- | ---: | ---: | --- |
| Pre-mapper development snapshot | `76 / 545` | `13.944954%` | unreliable text mapping `461`; incomplete source mapping `7`; truncated IR `1` |
| Ordered mapper + catalog | `537 / 545` | `98.532110%` | incomplete source mapping `7`; truncated IR `1` |

The observed change is `+461` accepted pages, or `+84.587156` percentage
points. The pre-mapper snapshot was an uncommitted development state and its
exact executable/source artifact was not retained. Its coverage counts are a
diagnostic observation; no timing A/B or reproducible implementation
comparison is permitted.

Every one of the three final sweeps produced the same aggregate and per-page
catalog commitment:

- output commitment SHA-256:
  `4882a299fbd467fc8612362b784ad4704fe93f944d7255d91fa200d62783cf61`;
- atoms: `183,549`;
- text: `65,977`;
- list item: `68,784`;
- table cell: `33,104`;
- code: `12,637`;
- math: `3,047`; and
- raw spans requiring entity/newline/tokenizer transformation: `12,399`.

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

## Mechanism timing

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

| Warm pooled metric | Result |
| --- | ---: |
| Page observations | `1,635` |
| Catalog-call wall | `35.021393 s` |
| Per-page p50, nearest rank | `11.618834 ms` |
| Per-page p95, nearest rank | `53.050500 ms` |
| Maximum page call | `1,220.335042 ms` |
| Throughput | `46.685751 pages/s` |
| Source HTML throughput | `7,233,852.37 bytes/s` |

The fixed cold probe was row 1, a `763,532`-byte page, and took `9.450667 ms`.
That one page is recorded for warm/cold methodology only; it is not a
population statistic.

## Environment and resources

The measured process used CPython `3.13.5` on macOS `26.6`, Darwin `25.6.0`,
arm64, 12 logical CPUs, and 24 GiB physical memory. Process-lifetime
`ru_maxrss` was:

| Point | Bytes |
| --- | ---: |
| After imports, source binding, and input hash | `40,665,088` |
| After cold probe | `55,836,672` |
| After warm-up | `299,171,840` |
| After all measured sweeps | `560,087,040` |
| Increase above post-warm-up peak | `260,915,200` |

`ru_maxrss` is a peak-ish process diagnostic. It includes Python, the native
extension, allocator retention, and the largest page/result seen; it is not an
allocation attribution or production memory forecast.

The loaded extension SHA-256 was
`73257bdcf8066265680a9f78b935384b7043187bd4a7f322d6c8c9b1acb9c867`.
Its build-time and current 132-file native source inventory digests both
matched
`e874ba9aa5f25daef7259bddd989342161a54d589519b0c7fe175b96c6329166`.
Every loaded repository-owned `clusy_native` Python source module—seven in this
run—matched its exact installed-to-repository module-name path and bytes. The
package re-export was the catalog module's exact benchmark callable.
The runner SHA-256 was
`f04f4d8ae3e2ed09cbe39198667137d9c9ad506293ce4bffcd372e9ec4cbd70f`;
the shared provenance helper SHA-256 was
`6d05da10f2aee3874acd59347636090faa894370ceaf7d2129a319e2df59eb89`.
Their executed filesystem paths matched the repository paths. The complete
runner, helper, Python import-chain, native-source, extension, and input
identities matched again after the run.

## Reproduction

Machine-specific measured command:

```bash
.venv/bin/python bench/selection_atom_catalog_representability.py \
  /Users/julin/ClusyV2/webclient/node_modules/.clusy-modal-l40s/inputs/inference-input.jsonl \
  --runs 3 --warmup-pages 16 \
  --output bench/evidence/selection-atom-catalog-e5958b5/report.json
```

Portable form, using a local copy with the frozen SHA-256 above:

```bash
.venv/bin/python bench/selection_atom_catalog_representability.py \
  /path/to/frozen-input.jsonl --runs 3 --warmup-pages 16 \
  --output bench/evidence/selection-atom-catalog-e5958b5/report.json
```

The complete canonical result is retained in [report.json](report.json).

## Claim boundary

This record supports only representation coverage and machine-local mechanism
diagnostics for the exact source, binary, environment, and HTML projection
recorded above. It does not support a WebMainBench quality claim, a SOTA claim,
a vendor comparison, an end-to-end crawler latency claim, a production
throughput claim, or a deployment claim. The catalog remains disabled by
default and does not change the production crawler response schema.

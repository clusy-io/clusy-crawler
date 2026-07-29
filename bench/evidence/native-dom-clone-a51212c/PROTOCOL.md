# Native DOM-clone A/B

This checked-in record binds an independently audited implementation A/B for
one runtime change in the broad native extractor:

```rust
- Document::from(doc.html().to_string())
+ doc.clone()
```

The verdict is GREEN for this specific promotion: zero blockers, zero major
findings, and one provenance-related minor finding. It is not a SOTA claim,
quality-score claim, service benchmark, or vendor comparison.

## Lineage

- Baseline: private commit
  `0fb00ee12c6bd02852dd08329f641f303f568570`.
- Timed candidate: that baseline plus only the one-line runtime change above;
  diff SHA-256
  `d7ec54bcd8f139988356e60f2d4f181cbc0b052266db557acee74fafc3295c2d`.
- Promoted private commit:
  `a51212c38a41522110bb3a556d88858ee15fbaba`.
- Mirrored public commit:
  `ffd61dbe68c81ad0c85e0c30374d4e3c165e2ae7`.
- Production revision: `clusy-crawler--v2-a51212c-static`.
- Production image:
  `sha256:86b3e6e4003dff15fdd8096d4d04a30df1068fea9cdf0cbb501ff509ee397b79`.

The promoted commit contains the same one-line runtime change plus focused
tests and test comments. The timed candidate wheel predates those test-only
additions, so it is not a bit-for-bit build of the promoted commit. That
distinction is the audit's one minor finding.

## Frozen inputs and output commitment

The A/B used two workers, chunk size 32, and cross order:

```text
baseline-1 → candidate-1 → candidate-2 → baseline-2
```

For each page, the driver hashed the page ID plus ten returned extraction
fields: `text`, `plain_text`, `article_text`, `title`, `description`,
`language`, `page_type`, `word_count`, `confidence`, and `strategy`.
Canonical, sorted-key, compact UTF-8 JSON plus one line feed was streamed into
SHA-256 in deterministic input order.

The timed region includes local corpus reads, gzip or JSONL parsing,
thread-pool scheduling, native extraction, canonical serialization, and
hashing. It excludes process startup and imports. It does not measure HTTP,
rendering, service latency, or Internet behavior.

| Corpus | Frozen identity | Pages | Labels read |
|---|---|---:|---|
| WCXB | commit `c039d5e`; input-tree SHA-256 `4d5c9be2...c3aae` | 2,008 | no |
| WebMainBench | JSONL SHA-256 `85765fe7...78884` | 7,809 | no |
| Malformed synthetic | seed `0xC1057`; generator SHA-256 `d9a1744f...f61b` | 20,000 | n/a |

## Results

Every hashed extraction field was exact across all four runs:

| Corpus | Aggregate output SHA-256 | Baseline mean | Candidate mean | Throughput delta |
|---|---|---:|---:|---:|
| WCXB | `ddb0ff4f...aec2` | 93.2901 pages/s | 104.3447 pages/s | +11.8498% |
| WebMainBench | `cf85a751...9175` | 152.2089 pages/s | 174.9288 pages/s | +14.9268% |
| Malformed synthetic | `40c7678b...5c32` | 7,712.1863 pages/s | 8,161.1569 pages/s | +5.8216% |

The 5,000-page `article_body=true` control was also exact. Its single timing
sample per variant is diagnostic only.

A constructed `<form><plaintext>` fallback intentionally differs. The old
serialize-and-reparse clone polluted plaintext with serializer-inserted closing
tags. Direct cloning preserves the source plaintext without that contamination.
Focused regression tests bind this correction.

## Promotion gates

- broad `cargo test --all-targets`: pass;
- broad `cargo clippy --lib`: pass with existing warnings;
- native Rust tests: 15/15;
- relevant Python tests: 107/107;
- promoted clone-isolation and plaintext fallback tests: pass;
- private CI run `30465266738`: success;
- public CI run `30465279502`: success;
- zero-traffic revision health, version identity, auth, SSRF, and live
  `example.com` crawl: pass;
- production main-domain identity and live crawl after traffic shift: pass.

The production revision is healthy at 100% traffic. Four prior healthy
revisions remain at 0% for rollback.

## Claim boundary

Only two timed samples were collected per main variant. There is no confidence
interval, significance test, per-page latency distribution, retained raw
output, RSS, energy trace, or hardware-counter record. Other interactive and
system processes were present. No vendor API, vendor key, or vendor output was
used.

The full independent machine-local audit had report SHA-256
`a143322f6b941211f0472083c21bb094bfac906cbeeaa3c5114c6b0bf28473ec`
and protocol SHA-256
`1c9249025440c4cc1eb3b1fdae13d693d5d5141490b54cccdd0c63a67eb1664d`.
The checked-in [compact report](report.json) preserves the claim-relevant
lineage, samples, corpus and output hashes, gates, and limitations without
depending on ephemeral local paths.

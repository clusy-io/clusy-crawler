# Native filtered-traversal stack A/B

This checked-in record binds a formal, retain-all implementation A/B for one
runtime change in the broad native extractor: filtered DOM traversal now
propagates exclusion and `article`/`main` ancestry state through a preorder
stack instead of walking every node's ancestor chain.

The promotion verdict is GREEN for this change. It is not an end-to-end service
benchmark, a quality-score improvement, a vendor comparison, or a SOTA claim.

## Lineage and mechanism

- Runtime baseline: private commit
  `a51212c38a41522110bb3a556d88858ee15fbaba`.
- Baseline `extract.rs` SHA-256:
  `8be756f965e66afbb98567728f915b10b34353fd87c16d37765387b15424c5aa`.
- Candidate runtime diff SHA-256:
  `37e7ddafa252ad042c5bf37d3a8b271a806a6e4dfd27a640c70c7d7175dfa1ac`.
- Candidate/promoted `extract.rs` SHA-256:
  `6dc82c36a25a29d5e08d15a422989f1ccd907c3592553202be7bf08b49d07504`.
- Promoted private commit:
  `bdbfd7cb7c70739d85a109fede276d53692e843d`.
- Mirrored public commit:
  `95b3bbecdf447980ca845fc2442e4e4555418671`.
- Production revision: `clusy-crawler--v2-bdbfd7c-static`.
- Production image:
  `sha256:638378e7bdf5b00c75b2aa3f56b057a645dd900d3114d9336d0e507d95a7afb8`.

The old traversal was O(N × depth) in the worst case because every retained
text node inspected its ancestors. The candidate retains one state entry per
open preorder branch, making state propagation O(N) time and O(depth) memory.
The stack is popped by parent identity, so implicit elements, foster parenting,
misnested markup, templates, SVG/MathML, deep branch changes, and pre-detached
nodes are covered without relying on source nesting.

The timed binaries are locked independently below. Their broad-extractor
source differs by the candidate runtime patch, and the promoted private/public
files have the exact candidate source hash. The compact record does not assert
whole-build reproducibility from the promoted commit.

## Frozen inputs and protocol

| Material | Records | SHA-256 |
| --- | ---: | --- |
| Baseline binary | — | `c4429e3af1bf6a3f9b0bca66380cc75b6cdca0b7becb367ebee647c2b4286dc7` |
| Candidate binary | — | `449f3a344cf3a4cbe49564b5ec6f7abdd7a978055f133480891fdc87443550b9` |
| WebMain JSONL | 7,809 | `85765fe798f07c14eb1c92945046eaa56e0da59663f70b9c498647d7dfd78884` |
| WCXB JSONL | 2,008 | `3634f976b2a2234f36bb0e9892e31b9bf769bffe69fbf876eeddedaef1a2a919` |
| Deterministic stress JSONL | 248 | `506a8026f488405bbc0946effa256e819899c64d7a13185bbb2201508a6f8e05` |

Inputs were locked before and after the run, and the lock files were
byte-identical. Each binary/corpus pair received a 128-record warm-up; the
binary also warmed the first three records before every measured loop.

For every corpus, the fixed primary sequence was:

```text
base-forward
candidate-reverse
candidate-forward
base-reverse
base-reverse
candidate-forward
candidate-reverse
base-forward
```

Each variant therefore has four retained samples: two forward and two reverse,
with A/B position counterbalanced. The headline rate is pooled pages divided by
pooled measured seconds. No sample was selected or removed based on speed.

The timed loop consumes all ten native extraction fields. Input loading,
process startup, imports, HTTP, browser rendering, scoring, and vendor calls are
outside the timer.

## Throughput results

| Corpus | Baseline pooled pages/s | Candidate pooled pages/s | Gain |
| --- | ---: | ---: | ---: |
| WebMain | 100.761604 | 114.858642 | +13.9905% |
| WCXB | 60.212135 | 76.430548 | +26.9355% |
| Stress | 165.452689 | 223.992892 | +35.3818% |

All four paired rounds were positive on every corpus. Direction-specific pooled
gains were also positive: WebMain +12.7490% forward and +15.2343% reverse;
WCXB +24.9892% and +28.9172%; stress +35.4428% and +35.3212%.

A separately reported, fixed WebMain A/B/B/A sensitivity run remained positive:
101.263854 versus 113.692162 pages/s, or +12.2732%. It does not replace or mix
with the primary sequence.

## Resource results

Two identical `/usr/bin/time -l` WCXB runs per variant used the fixed
base/candidate/candidate/base sequence:

| Arithmetic mean | Baseline | Candidate | Candidate change |
| --- | ---: | ---: | ---: |
| Outer wall time | 33.590 s | 26.345 s | -21.5689% |
| User time | 32.995 s | 25.855 s | -21.6396% |
| Instructions retired | 626,021,861,950 | 486,242,215,464 | -22.3282% |
| Cycles elapsed | 139,434,503,584 | 109,075,952,151 | -21.7726% |
| Maximum RSS | 584,916,992 B | 583,024,640 B | -0.3235% |
| Peak memory footprint | 571,736,952 B | 568,820,600 B | -0.5101% |

Both variants recorded zero page faults. macOS did not expose a branch counter,
so no branch-miss result is claimed.

## Complete output equality

For every page, exactly ten UTF-8 fields were serialized in fixed order with
unsigned 64-bit big-endian length prefixes: `text`, `plain_text`,
`article_text`, `title`, `description`, `language`, `page_type`, `word_count`,
`confidence`, and `strategy`.

| Corpus | Records | Fields | Dump bytes | Base/candidate SHA-256 | `cmp` |
| --- | ---: | ---: | ---: | --- | ---: |
| WebMain | 7,809 | 78,090 | 150,613,567 | `9b6f5e126f4bebc2a59e688138ae74b4677c7a2503038f7de1dd102a6e652f1b` | 0 |
| WCXB | 2,008 | 20,080 | 42,028,160 | `356544b710fe4a255ed2264722ee719d6e45cee7fd00390482dd3bc8f761e77a` | 0 |
| Stress | 248 | 2,480 | 2,444,862 | `72c75882e1c22a7d12a0b2b56ad4618f3e5d3805ecf092e4005e436486442704` | 0 |

All six dumps passed full framing, record-count, ten-field, and zero-trailing
byte validation before direct byte comparison. This proves output invariance
for the locked inputs and fields, not for every possible HTML document.

## Contention and promotion gates

All 24 primary throughput samples, four resource samples, and four
supplemental samples succeeded. No sample was excluded. Protected Codex/VS Code
and system UI processes remained active; observed contention was annotated and
retained. No concurrent benchmark, `cargo`, `rustc`, or `pytest` process was
observed in the collected pre-sample snapshots.

Promotion gates:

- broad Rust tests: 919/919;
- native Rust tests: 29/29;
- full Python suite: 1,111/1,111;
- Ruff, mypy, and clippy: pass with no new clippy warnings;
- private CI run `30470417642`: success;
- public CI run `30470648038`: success;
- zero-traffic production readiness, exact version/image identity, auth, SSRF,
  and live crawl: pass;
- production main-domain readiness, identity, unauthorized request, and live
  crawl after traffic shift: pass;
- normalized Container Apps template unchanged, SHA-256
  `2a99688c0b32fbd94f6fde9d94309d96622dd4afc934f6d493522355a65fe7b8`;
  five prior healthy zero-traffic revisions remain available for rollback.

## Integrity and claim boundary

The full machine-local bundle is 386 MiB and is intentionally not checked into
Git. Its integrity roots are:

- full `report.json`:
  `ad9b66cc5d4f8f2acced7a12a0f1e0014654b51cf09949cf19893ab80a2a9511`;
- full `PROTOCOL.md`:
  `9367846106ac12d716ace8ebdbd03fb09f0e073b1ec4a780f8495792a9c7ca6a`;
- `SHA256SUMS` and detached manifest:
  `9a4933f4286203b0033d331aa42d781ae26bb0b7321b832ea41372427528b3ce`;
- manifest verification: 258/258 files passed.

The checked-in [compact report](report.json) retains claim-relevant lineage,
raw rate samples, frozen hashes, complete-output commitments, resource means,
deployment gates, and limitations. No network or vendor API was invoked, and
no vendor key or output appears in either record.

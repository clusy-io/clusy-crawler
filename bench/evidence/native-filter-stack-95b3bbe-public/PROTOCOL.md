# Archived native filtered-traversal implementation A/B

Status: **ARCHIVED — NOT AUTHORIZED FOR PUBLICATION**, recorded 2026-07-29.
This receipt is absent from the current evidence registry and is retained only
for audit history.

## Scope

This record evaluates one extraction-loop implementation change: propagating
filtered-ancestor and `article`/`main` state during preorder traversal instead
of walking ancestors for every node. The candidate runtime file is
byte-identical to public commit
`95b3bbecdf447980ca845fc2442e4e4555418671`.

The result covers local native extraction over preloaded records. It excludes
input loading, process startup, HTTP, Chromium, network behavior, service
latency, ground-truth scoring, and provider APIs.

The timed binaries were locked independently. Runtime source equivalence is
established, but the record does not claim whole-build reproducibility from the
later public commit.

## Frozen identities

| Material | Identity |
| --- | --- |
| Public candidate commit | `95b3bbecdf447980ca845fc2442e4e4555418671` |
| Public candidate tree | `47e414aa9fbae67cbc4f7cbd506a3aa9ff13ec84` |
| Candidate runtime source | SHA-256 `6dc82c36a25a29d5e08d15a422989f1ccd907c3592553202be7bf08b49d07504` |
| Baseline binary | SHA-256 `c4429e3af1bf6a3f9b0bca66380cc75b6cdca0b7becb367ebee647c2b4286dc7` |
| Candidate binary | SHA-256 `449f3a344cf3a4cbe49564b5ec6f7abdd7a978055f133480891fdc87443550b9` |
| WebMain input | SHA-256 `85765fe798f07c14eb1c92945046eaa56e0da59663f70b9c498647d7dfd78884` |
| WCXB input | SHA-256 `3634f976b2a2234f36bb0e9892e31b9bf769bffe69fbf876eeddedaef1a2a919` |
| Stress input | SHA-256 `506a8026f488405bbc0946effa256e819899c64d7a13185bbb2201508a6f8e05` |

The recorded environment was an Apple M4 Pro running macOS on arm64 with
Rust 1.85.0. Interactive system processes remained present and are part of the
historical boundary.

## Method

Each binary/corpus pair received a fixed warm-up. The primary sequence retained
four samples per variant, with forward/reverse traversal and A/B position
counterbalanced:

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

The headline rate is pooled pages divided by pooled measured seconds. No sample
was selected or removed based on speed.

## Archived measured receipt

| Corpus | Baseline pages/s | Candidate pages/s | Change |
| --- | ---: | ---: | ---: |
| WebMain | `100.761604` | `114.858642` | `+13.9905%` |
| WCXB | `60.212135` | `76.430548` | `+26.9355%` |
| Deterministic stress | `165.452689` | `223.992892` | `+35.3818%` |

All ten serialized output fields were byte-identical between variants for every
record in the three locked corpora:

| Corpus | Records | Shared output SHA-256 |
| --- | ---: | --- |
| WebMain | 7,809 | `9b6f5e126f4bebc2a59e688138ae74b4677c7a2503038f7de1dd102a6e652f1b` |
| WCXB | 2,008 | `356544b710fe4a255ed2264722ee719d6e45cee7fd00390482dd3bc8f761e77a` |
| Stress | 248 | `72c75882e1c22a7d12a0b2b56ad4618f3e5d3805ecf092e4005e436486442704` |

## Retention and claim boundary

The original large raw bundle is no longer retained. This compact record is
archival and non-authorizing; its dated values may not be published as a
current implementation result.

It does not support a metric, superiority, quality, service, deployment,
provider, or state-of-the-art claim.

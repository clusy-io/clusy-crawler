# Rejected native filtered-HTML-shape A/B

## Decision

**NO-GO.** Candidate `415d36c` is intentionally not merged, pushed, mirrored,
or deployed. Production retains the exact `bdbfd7c` native runtime.

The candidate removed a second DOM parse used only to count `<p>` and
`<table>` elements in serializer-generated filtered HTML. Its counters were
attached to the serializer's sole opening-tag emission point and passed
property, route, malformed-HTML, and full-corpus output-equivalence gates.
However, the measured gain was below the predeclared approximately 2% threshold
on both main corpora, one WCXB paired round was negative, WebMain sensitivity
reversed sign, and selected memory metrics regressed.

This is a local extraction-loop promotion decision, not an HTTP-service,
live-web, vendor, quality-score, or SOTA benchmark.

## Locked lineage

- Baseline runtime commit:
  `bdbfd7cb7c70739d85a109fede276d53692e843d`.
- Baseline binary SHA-256:
  `449f3a344cf3a4cbe49564b5ec6f7abdd7a978055f133480891fdc87443550b9`.
- Isolated candidate commit:
  `415d36c017073cc15c10694323d70fcc6c761c39`.
- Candidate parent: exact `bdbfd7c`.
- Candidate tree:
  `741c1bf26f6f336914a390f585f38bea8cd76ae0`.
- Candidate patch SHA-256:
  `0dfd5cf7295b26a44043d10809d9afc9ce4ebe13e730394fab66aac700d5c951`.
- Candidate `extract.rs` SHA-256:
  `7537d2e65b0540134a12f829068aafae040bc003731a0b33336c5f18ee2d9fb8`.
- Candidate binary SHA-256:
  `b8dde75177c29eb013f6e688152a5df8f297fe1ea2d280454abf299fe317c1eb`.

The candidate passed 934/934 broad tests, 15/15 native tests, 1,152
deterministic structural parity combinations, focused adversarial route tests,
and pre-timing complete ten-field equality on all three corpora.

## Fixed primary protocol

Every binary/corpus pair received a 128-record warm-up. Each corpus then used
six samples per variant: exactly three forward and three reverse, with A/B
position balanced three/three:

```text
baseline-forward
candidate-reverse
candidate-forward
baseline-reverse
baseline-reverse
candidate-forward
candidate-reverse
baseline-forward
baseline-forward
candidate-reverse
candidate-forward
baseline-reverse
```

Pooled throughput is total pages divided by total measured seconds. All 36
primary samples were retained. A material protected-UI snapshot triggered a
separate fixed A/B/B/A sensitivity sequence for WebMain and WCXB; it never
replaced primary data. Before each sensitivity sample, a source-independent UI
gate required VS Code renderer snapshots below 30%.

No timed sample began alongside a detected `cargo`, `rustc`, `pytest`,
`maturin`, or other profile peer. Exactly identified Chrome/media processes
were stopped for the primary/sensitivity windows and identity-checked before
being resumed. Codex, VS Code, and WindowServer remained active.

## Results

| Corpus | Baseline pooled p/s | Candidate pooled p/s | Gain | Median gain | p95 seconds change |
| --- | ---: | ---: | ---: | ---: | ---: |
| WebMain | 113.935229 | 115.202263 | +1.1121% | +0.6558% | -1.3866% |
| WCXB | 76.782052 | 77.450084 | +0.8700% | +0.7869% | -0.6328% |
| Stress | 221.947142 | 224.022774 | +0.9352% | +1.3062% | +0.6447% |

Direction-stratified pooled gains were WebMain `-0.4195% / +2.6615%`,
WCXB `+0.6551% / +1.0847%`, and stress `+0.4338% / +1.4386%` for
forward/reverse. WCXB had one negative paired round; stress had two.

The fixed sensitivity results were:

- WebMain: `-0.6350%`;
- WCXB: `+1.1117%`.

Two identical `/usr/bin/time -l` WCXB runs per variant used A/B/B/A order:

| Mean metric | Baseline | Candidate | Candidate change |
| --- | ---: | ---: | ---: |
| Outer real seconds | 26.710 | 26.390 | -1.1981% |
| Instructions retired | 486,489,396,493 | 482,845,606,074 | -0.7490% |
| Cycles elapsed | 109,794,806,721 | 108,814,895,851 | -0.8925% |
| Maximum RSS bytes | 584,613,888 | 586,539,008 | +0.3293% |
| Peak footprint bytes | 568,509,292 | 572,646,264 | +0.7277% |

The small wall/instruction/cycle reductions did not offset the sub-threshold,
direction-sensitive throughput result and selected memory regressions.

## Complete output equality

Fresh full ten-field dumps from both final binaries passed exact record,
ten-field framing, and zero-trailing-byte checks before complete byte
comparison:

| Corpus | Records | Fields | Bytes | Shared SHA-256 | `cmp` |
| --- | ---: | ---: | ---: | --- | ---: |
| WebMain | 7,809 | 78,090 | 150,613,567 | `9b6f5e126f4bebc2a59e688138ae74b4677c7a2503038f7de1dd102a6e652f1b` | 0 |
| WCXB | 2,008 | 20,080 | 42,028,160 | `356544b710fe4a255ed2264722ee719d6e45cee7fd00390482dd3bc8f761e77a` | 0 |
| Stress | 248 | 2,480 | 2,444,862 | `72c75882e1c22a7d12a0b2b56ad4618f3e5d3805ecf092e4005e436486442704` | 0 |

## Integrity and claim boundary

The full machine-local artifact is not checked into Git. Its integrity roots
are:

- full `report.json`:
  `e61e06fbe9ee3914b07a3906af9f18a03280224bd9ffa67fa5a1f0c744636e18`;
- full `PROTOCOL.md`:
  `46b4bada4396e16a5de261fa7c0eda246084d3a2980ce44ccb280fe934c7dc56`;
- `SHA256SUMS`:
  `42269812659c88578199568abc2225403b80de62019656842c782cde02191774`;
- manifest verification: 423/423 files passed.

No network, vendor API, vendor key, or vendor output was used. The checked-in
[compact report](report.json) preserves the raw rate samples, frozen identities,
resource means, exact-output commitments, gates, and limitations.

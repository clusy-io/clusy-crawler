# AEB article-body evaluation

Status: verified for the scope defined below.

## Scope

This run evaluates article-body extraction directly from the public repository
on all 181 pages in the pinned ScrapingHub/Zyte Article Extraction Benchmark
(AEB) at commit `4a3bc979f76c0df73cb95fe272e2fc1b96f9f010`.
AEB's unchanged four-token-shingle evaluator scores the exact production
`article_body` output. The prediction transform is identity.

The result is not a recursive-crawling, JavaScript-rendering, general-web,
service-latency, reliability, or vendor-API benchmark.

## Frozen identities

| Material | Identity |
| --- | --- |
| Public crawler source | commit `4dd1755e9b425c80193982bc6609c06444cf30d5`; tree `6278cefeb65b9f7c1811b154af9ce9c9581718a7` |
| Runtime source manifest | SHA-256 `c4c19bd36359e61e4098b55e812b099a48c90f66a9c5a202b544f5d0d28829f0` |
| AEB checkout | commit `4a3bc979f76c0df73cb95fe272e2fc1b96f9f010`; tree `258fee1bb38bcb642afec48cb80e51bd1594c259` |
| Ground truth | SHA-256 `512e9a9498912047a966e22f47302e849dfa45dca1f555d97588317dac7e5a3d` |
| Evaluator | SHA-256 `c01bf1cc7989700273ab1ba6d30fcdedc22fdb4301e7b4c1ac20635bb7632ea8` |
| Split manifest | SHA-256 `d74381d0cc5fdf0425674d59ee689819b5ddbb886549d319982845cf6b83efd4` |

The crawler and AEB worktrees were clean. Relevant crawler source hashes were
identical before and after the run. The runtime-source identity above is the
SHA-256 of the sorted compact JSON source-hash map plus its terminating line
feed.

## Method

- full 181-page corpus;
- deterministic page shuffle, seed `20260727`;
- five untimed warm-up pages;
- closed-loop concurrency of two;
- exact production async extraction entry point;
- pinned Trafilatura `2.0.0` and embedded rs-trafilatura `9261e08`
  comparators;
- 10,000 paired page-bootstrap replicates for comparison intervals.

The timer begins after fixtures are decoded and ends when the production
`ExtractionResult` is available. Fixture I/O, decompression, and official
scoring are outside the timer.

## Results

| Metric | Result |
| --- | ---: |
| Precision | `0.955147` |
| Recall | `0.989721` |
| F1 | `0.972127` |
| Local throughput | `142.31` pages/s |
| F1 delta vs Trafilatura 2.0 | `+0.014624` |
| Paired 95% interval for the F1 delta | `[+0.005346, +0.025342]` |

The local throughput result is specific to the recorded Apple M4 Pro
environment and the in-memory extraction boundary. It is not an HTTP-service
throughput claim.

## Retention and claim boundary

The compact report is checked in. The full result directory—including exact
predictions, per-page metrics, production Markdown, split manifest, and
original report—is retained externally:

- raw manifest SHA-256:
  `2f7b61af148387c93ff6381fee5fad663a1a4e731d79d653568da4a656784fc1`;
- retained archive SHA-256:
  `16b79ccd4fd87a689bfa9ee34f7119c4d34511fe926afa38eb1b45e24d393bbb`.

This evidence permits publication of the registered metrics and the scoped
comparison with Trafilatura 2.0. It does not permit a SOTA claim or a
comparison with Exa or Firecrawl.

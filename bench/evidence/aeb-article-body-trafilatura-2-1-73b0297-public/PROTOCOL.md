# AEB article-body evaluation against Trafilatura 2.1.0

Status: verified for the scope defined below.

## Scope

This run evaluates article-body extraction directly from the public repository
on all 181 pages in the pinned ScrapingHub/Zyte Article Extraction Benchmark
(AEB) at commit `4a3bc979f76c0df73cb95fe272e2fc1b96f9f010`.
AEB's unchanged four-token-shingle evaluator scores the exact production
`article_body` output. The prediction transform is identity.

The result is not a recursive-crawling, JavaScript-rendering, general-web,
service-latency, reliability, cost, or vendor-API benchmark.

## Frozen identities

| Material | Identity |
| --- | --- |
| Public crawler source | commit `73b02974b4cf2aab0764922cf7ac664e0f3bc36f`; tree `9fb2f1150a4082e7bd905bfc406ab11520da0153` |
| Runtime source manifest | 231 files; SHA-256 `91890c92612dc1f74c143196b234ced9f546c396155daafd816113b4051d95a5` |
| Loaded native extension | SHA-256 `73257bdcf8066265680a9f78b935384b7043187bd4a7f322d6c8c9b1acb9c867` |
| Native source inventory | 132 files; SHA-256 `e874ba9aa5f25daef7259bddd989342161a54d589519b0c7fe175b96c6329166` |
| AEB checkout | commit `4a3bc979f76c0df73cb95fe272e2fc1b96f9f010`; tree `258fee1bb38bcb642afec48cb80e51bd1594c259` |
| AEB HTML inventory | 181 pages; SHA-256 `1c9833287ef2ee3bf3d9d948dbec300f867316e71815c003640d57e7567a04e9` |
| Ground truth | SHA-256 `512e9a9498912047a966e22f47302e849dfa45dca1f555d97588317dac7e5a3d` |
| Evaluator | SHA-256 `c01bf1cc7989700273ab1ba6d30fcdedc22fdb4301e7b4c1ac20635bb7632ea8` |
| Comparator controller | SHA-256 `c982030b72af4ae3a0ab00b2e0acfb74d2a67faec1c01781187f9099d7a7b64c` |
| Comparator worker | schema v2; SHA-256 `12fa9f0cde9b89b7c4a77cd4546ac5b31ae5155339388b90db0f965fc5de2f42` |
| Comparator environment | CPython 3.13.5 Darwin/arm64; 17 distributions; 2,632 site files; manifest SHA-256 `fa522352d9e0369dbd1e17794adb09c9e47b9f316f30a6ef6971dc1221eb391f` |
| Comparator requirements lock | `--require-hashes`; SHA-256 `68b1fe778be9ec1d65ed930f1b3e57e15d195cce5b9c4b87fae6df3cb22d9d5f` |
| Trafilatura wheel | `trafilatura-2.1.0-py3-none-any.whl`; SHA-256 `0eded5207a806445ddebbe36eae30b9035fe6a2f233c36f6fe82663fca8b9d30` |
| Production lock | SHA-256 `61949fb0daede4db5b6f1a6b95311075dc879e8f66cf8c27c400ab22779fa11f` |
| Raw report | SHA-256 `f0d8e5ab91bf40910d06860a9a8028ceea9947307f21f734eb636d997ccefc9b` |
| Raw artifact manifest | SHA-256 `a4fc6b4c0dfd3937c3ab70664c32a1179aef6e16625d2908ac8d5a36cbd61b02` |

The crawler and AEB worktrees were clean. Relevant crawler source hashes were
identical before and after the complete invocation, including comparator
replay. The runtime-source identity is the SHA-256 of the sorted compact JSON
source-hash map plus its terminating line feed.

## Comparator isolation

Before loading AEB labels or evaluator code, the controller copied only the
181 committed compressed HTML fixtures and their cryptographic inventory into
a temporary capsule. A dedicated Python `-I -B` process executed the pinned
upstream call:

```python
trafilatura.extract(html, include_comments=False)
```

The worker verified every capsule byte, its own source, the exact installed
17-distribution payload, all `RECORD` entries, and the complete importable
`site-packages` inventory. The controller separately verified the hash-pinned
requirements lock and reviewed Trafilatura wheel hash. Python isolation is not
an OS filesystem sandbox; the reviewed worker opens only capsule inputs, and
the controller does not supply labels, evaluator code, historical outputs, or
Clusy predictions to it.

Comparator output is used only for benchmark evaluation, never training,
distillation, label generation, routing, or production calibration.

## Method

- full 181-page corpus;
- deterministic page shuffle, seed `20260727`;
- five untimed warm-up pages;
- closed-loop concurrency of two;
- exact production asynchronous extraction entry point;
- exact Trafilatura `2.1.0`, historical Trafilatura `2.0.0`, and embedded
  rs-trafilatura `9261e08` comparators;
- 10,000 paired page-bootstrap replicates for comparison intervals.

The timer begins after fixtures are decoded and ends when the production
`ExtractionResult` is available. Fixture I/O, decompression, comparator replay,
serialization, and official scoring are outside the candidate timer.

## Results

| Metric | Result |
| --- | ---: |
| Precision | `0.955147` |
| Recall | `0.989721` |
| F1 | `0.972127` |
| Trafilatura 2.1.0 F1 | `0.957546` |
| F1 delta vs Trafilatura 2.1.0 | `+0.014581` |
| Paired 95% interval for the F1 delta | `[+0.005547, +0.025336]` |
| Paired-bootstrap win fraction | `0.9996` |
| Local throughput | `152.71` pages/s |
| Candidate latency p50 / p95 | `12.00 / 24.05` ms |
| Candidate errors | `0` |

The local performance values are specific to the recorded Apple M4 Pro
environment and the in-memory extraction boundary. They are not HTTP-service
or cross-machine throughput claims.

The point score also exceeds the pinned historical rs-trafilatura prediction,
but its paired interval starts at equality and its win fraction is `0.6349`.
This record therefore does not authorize a statistically conclusive leadership
claim against rs-trafilatura, AutoExtract, or an unqualified AEB leaderboard.

## Retention and claim boundary

The compact report is checked in. The full 6.1 MiB result
directory—including exact predictions, per-page metrics, production Markdown,
split manifest, comparator receipt, environment manifest, requirements lock,
and original report—is retained in the ignored benchmark-results store:

- raw manifest SHA-256:
  `a4fc6b4c0dfd3937c3ab70664c32a1179aef6e16625d2908ac8d5a36cbd61b02`;
- retained archive SHA-256:
  `8e05a6fb120aaa75ec85170d7b8be0267288aa5acdb33614dafb9458ce7b345b`.

This evidence permits publication of the registered AEB metrics and the
scoped comparison with exact Trafilatura 2.1.0. It does not permit a universal
SOTA claim or a comparison with Exa or Firecrawl.

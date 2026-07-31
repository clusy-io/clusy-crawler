# AEB article-body evaluation for Beta 2 against Trafilatura 2.1.0

Status: verified for the scope defined below.

## Scope

This run evaluates article-body extraction directly from the public repository
on all 181 pages in the pinned ScrapingHub/Zyte Article Extraction Benchmark
(AEB) at commit `4a3bc979f76c0df73cb95fe272e2fc1b96f9f010`.
AEB's unchanged four-token-shingle evaluator scores the exact production
`article_body` output. The prediction transform is identity.

The measured source tree is also the exact tree tagged `v0.2.0-beta.2`. The
result is not a recursive-crawling, JavaScript-rendering, general-web,
service-latency, reliability, cost, or vendor-API benchmark.

## Frozen identities

| Material | Identity |
| --- | --- |
| Public crawler source | commit `77b8d00c5ebf88ed3afffe64f869ccb8c6922365`; tree `b90c3d6e7b4d816b915da1f0739662bd159752d5` |
| Beta 2 tag | `v0.2.0-beta.2`; commit `8d38b5ca5dfdb6ba786046d8a0958410a41f48d2`; tree `b90c3d6e7b4d816b915da1f0739662bd159752d5` |
| Runtime source manifest | 232 files; SHA-256 `5d49abe41606e3cb1dc74427fa749191c6579d7ea2971c69a7258df5b5829547` |
| Loaded native extension | SHA-256 `99a3e60396884634dec196e4a934e1b0ac40cbf4a1fe92d4cf0467ba19de8e01` |
| Native package entry point | SHA-256 `eac8fb5bfb89a272fedaad7b58d847dd2741676a2b514e163e395ae2e1bd8a9c` |
| Native source inventory | 132 files; SHA-256 `a890cd68664b22017fdd580ece3543a77ba3d74ab8636c3a9c0eed786f38f9af` |
| AEB checkout | commit `4a3bc979f76c0df73cb95fe272e2fc1b96f9f010`; tree `258fee1bb38bcb642afec48cb80e51bd1594c259` |
| AEB HTML inventory | 181 pages; SHA-256 `1c9833287ef2ee3bf3d9d948dbec300f867316e71815c003640d57e7567a04e9` |
| Ground truth | SHA-256 `512e9a9498912047a966e22f47302e849dfa45dca1f555d97588317dac7e5a3d` |
| Evaluator | SHA-256 `c01bf1cc7989700273ab1ba6d30fcdedc22fdb4301e7b4c1ac20635bb7632ea8` |
| Comparator controller | SHA-256 `c982030b72af4ae3a0ab00b2e0acfb74d2a67faec1c01781187f9099d7a7b64c` |
| Comparator worker | schema v2; SHA-256 `12fa9f0cde9b89b7c4a77cd4546ac5b31ae5155339388b90db0f965fc5de2f42` |
| Comparator environment | CPython 3.13.5 Darwin/arm64; 17 distributions; 2,632 site files; manifest SHA-256 `fa522352d9e0369dbd1e17794adb09c9e47b9f316f30a6ef6971dc1221eb391f` |
| Comparator requirements lock | `--require-hashes`; SHA-256 `68b1fe778be9ec1d65ed930f1b3e57e15d195cce5b9c4b87fae6df3cb22d9d5f` |
| Trafilatura wheel | `trafilatura-2.1.0-py3-none-any.whl`; SHA-256 `0eded5207a806445ddebbe36eae30b9035fe6a2f233c36f6fe82663fca8b9d30` |
| Production lock | SHA-256 `20b4c8bcfee02bf69b17ec3a3abc1f4f4dcb71e33fbf111ce2d6a2e7b7bb8559` |
| Raw report | SHA-256 `3326579380cc61dc78c4fefcd7384c4d2d6ecb2e0ba5ea38bd11fbbe8c6cff93` |
| Raw artifact manifest | SHA-256 `fffeba35b9581920f4053eb6e044c5ae16e6d231c97adc110add07cea1987542` |
| Deterministic raw archive | 2,075,819 bytes; SHA-256 `823ea88f1fdf260d03edfd0f4c0cc8fb1030f5c7583c48daceeabbcb2058f3be` |

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
| Observed local extraction throughput | `173.97` pages/s |
| Observed candidate latency p50 / p95 | `10.30 / 20.83` ms |
| Candidate errors | `0` |

The performance values are one exact observation on the recorded Darwin arm64
host. They cover only the in-memory extraction boundary and are not a
stability result, HTTP-service rate, crawler rate, service-level guarantee, or
cross-machine throughput claim.

The point score also exceeds the pinned historical rs-trafilatura prediction,
but its paired interval starts at equality and its win fraction is `0.6349`.
This record therefore does not authorize a statistically conclusive leadership
claim against rs-trafilatura, AutoExtract, or an unqualified AEB leaderboard.

## Retention and claim boundary

The compact report is checked in. The complete raw directory—including exact
predictions, per-page metrics, production Markdown, split manifest, comparator
receipt, environment manifest, requirements lock, and original report—is
retained in a deterministic external archive:

- archive format: sorted USTAR members with fixed modes, zero timestamps,
  zero numeric owners, fixed owner names, and gzip with no stored name or
  timestamp;
- 15 safe, normalized members; no absolute paths, parent traversal, symlinks,
  or unsupported entry types;
- raw manifest SHA-256:
  `fffeba35b9581920f4053eb6e044c5ae16e6d231c97adc110add07cea1987542`;
- retained archive SHA-256:
  `823ea88f1fdf260d03edfd0f4c0cc8fb1030f5c7583c48daceeabbcb2058f3be`;
- uncompressed USTAR SHA-256:
  `2cf338004244085db452eba490b7fffb5424d4d22db78b227a9339aaa183ef2c`.

Two separate archive builds were byte-identical. A fresh extraction matched
the source directory byte-for-byte, and every extracted artifact matched its
manifest size and SHA-256.

This evidence permits publication of the registered AEB metrics, the scoped
comparison with exact Trafilatura 2.1.0, and the explicitly bounded local
performance observation. It does not permit a universal SOTA claim, a crawler
or service performance claim, or a comparison with Exa or Firecrawl.

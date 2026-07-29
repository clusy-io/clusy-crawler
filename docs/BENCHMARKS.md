# Benchmarks and verified evidence

This page summarizes current evidence. Suite protocols and immutable records
under [`../bench`](../bench/README.md) remain authoritative.

## Claim boundary

Current evidence supports:

- a scoped article-body quality win over Trafilatura 2.0;
- a strong public WCXB diagnostic for `adaptive`;
- reproducible broad-corpus baselines that expose current gaps;
- exact-output local native-extraction speedups; and
- a historical deployment verification for the implementation mirrored at
  public commit `95b3bbe`.

It does **not** support:

- universal web-extraction SOTA;
- a blind WebMainBench or WCXB leaderboard claim;
- an overall Exa or Firecrawl win;
- search-quality conclusions; or
- HTTP-service throughput conclusions from extraction loops.

## Quality matrix

All values were recorded on 2026-07-29 with commit-pinned public protocols.
Rates are local closed-loop extraction, not Internet or HTTP-service
throughput.

| Suite | Scope | Quality | Local rate | Interpretation |
| --- | --- | ---: | ---: | --- |
| AEB `article_body` | 181 article bodies | P/R/F1 `0.955147 / 0.989721 / 0.972127` | `142.31` pages/s | `+0.014624` F1 vs Trafilatura 2.0; paired 95% CI `[+0.005346, +0.025342]` |
| AEB `balanced` | Same public pages, general profile | P/R/F1 `0.928435 / 0.989588 / 0.958037` | `215.26` pages/s | Below the pinned embedded Rust article backend |
| WCXB `balanced`, test | 511 pages, seven types | P/R/F1 `0.894822 / 0.928969 / 0.891727` | `106.96` pages/s | Public diagnostic; classifier provenance unresolved |
| WCXB `adaptive`, test | 511 pages, seven types | P/R/F1 `0.895244 / 0.942960 / 0.901714` | `78.62` pages/s | Point delta `+0.009987` F1 vs `balanced`; public diagnostic |
| Webis `balanced` | 3,985 historical pages | ROUGE-LSum F1 `0.855327`; Levenshtein `0.850216` | `306.09` pages/s | Archival result; below published Trafilatura and ensemble rows |
| WebMainBench raw | 7,809 broad pages | ROUGE-5 P/R/F1 `0.615569 / 0.677841 / 0.606672` | `113.02` pages/s | Fixed public Direct-MD diagnostic |
| WebMainBench scrubbed | Same pages without annotation UI signals | ROUGE-5 P/R/F1 `0.615698 / 0.676570 / 0.605703` | `55.05` pages/s | Required leakage-sensitivity track |
| WebMainBench 545 | Text, code, formula, and table | Overall `0.214089` | — | Text `0.752301`; code `0.017775`; formula `0.300369`; table/TEDS `0` |

For scale, the upstream WebMainBench card reported on 2026-07-29 a best full
dataset `HTML+MD` score of `0.9098`, Dripper fallback at `0.8925`, and
MinerU-HTML 4.1.1 at `0.8256` overall on the fine-grained 545 subset. Clusy's
full-dataset row uses a different Direct-MD contract and is therefore not a
leaderboard placement. These reference values define the present gap; they do
not turn a later incremental gain into a SOTA result. See the
[upstream benchmark card](https://huggingface.co/datasets/opendatalab/WebMainBench).

The repository does not check in the generated AEB, WCXB, Webis, or
WebMainBench raw result bundles. Their harnesses and exact reproduction
contracts are present; immutable implementation A/B records are checked in
under `bench/evidence/`. Do not present a summary row as locally auditable raw
evidence when its corresponding bundle is absent.

The WCXB embedded page classifier publishes no item-level training manifest.
Its reported training size and page-type distribution create material overlap
risk with WCXB development. WCXB remains a reproducible public diagnostic, not
independent unseen-test evidence.

## Exact-output implementation A/B

The promoted native filtered traversal replaced repeated ancestor walks with
an O(N) preorder state stack.

| Locked corpus | Pages | Complete output | Pooled extraction-rate change |
| --- | ---: | --- | ---: |
| WebMainBench | 7,809 | All ten fields byte-identical | `+13.9905%` |
| WCXB | 2,008 | All ten fields byte-identical | `+26.9355%` |
| Deterministic stress set | 248 | All ten fields byte-identical | `+35.3818%` |

On two WCXB resource runs per variant, mean retired instructions decreased
`22.3282%`, cycles decreased `21.7726%`, maximum RSS decreased `0.3235%`, and
peak memory footprint decreased `0.5101%`.

The complete compact record is
[`native-filter-stack-bdbfd7c`](../bench/evidence/native-filter-stack-bdbfd7c/PROTOCOL.md).

A later filtered-HTML-shape candidate preserved outputs but failed its speed,
sensitivity, and memory gates. It was rejected and not deployed. See
[`rejected-native-filtered-shape-415d36c`](../bench/evidence/rejected-native-filtered-shape-415d36c/PROTOCOL.md).

## Public and deployed lineage

The filtered-traversal implementation has distinct commit identities because
the public and private repositories have different histories:

| Identity | Value |
| --- | --- |
| Public promoted commit | `95b3bbecdf447980ca845fc2442e4e4555418671` |
| Separately deployed private source | `bdbfd7cb7c70739d85a109fede276d53692e843d` |
| Deployed OCI digest | `sha256:638378e7bdf5b00c75b2aa3f56b057a645dd900d3114d9336d0e507d95a7afb8` |
| Deployed revision name | `clusy-crawler--v2-bdbfd7c-static` |
| Rollback revisions healthy at release | `5` |

The runtime source file and implementation diff are bound in the immutable
record. The separate deployment passed readiness, source/image identity,
unauthorized-request, SSRF, and live-crawl checks before and after traffic
moved.

This is historical implementation evidence. The current public branch contains
later cache, research, evidence, and documentation changes and is **not**
represented as deployed.

## Evidence hierarchy

From strongest to weakest:

1. permissioned hidden domain/time holdout with sealed scoring;
2. commit-pinned public test with isolated runtime inputs;
3. public development diagnostic;
4. exact-output implementation A/B;
5. synthetic state-machine regression;
6. anecdotal live example.

A lower tier cannot establish a higher-tier claim.

## Reproduce

Use the protocol for the named suite:

- [`../bench/NEUTRAL_BENCHMARK.md`](../bench/NEUTRAL_BENCHMARK.md)
- [`../bench/WCXB_BENCHMARK.md`](../bench/WCXB_BENCHMARK.md)
- [`../bench/WEBIS_BENCHMARK.md`](../bench/WEBIS_BENCHMARK.md)
- [`../bench/WEBMAINBENCH_BENCHMARK.md`](../bench/WEBMAINBENCH_BENCHMARK.md)
- [`../bench/WEBMAINBENCH_FINEGRAINED_BENCHMARK.md`](../bench/WEBMAINBENCH_FINEGRAINED_BENCHMARK.md)
- [`../bench/LIVE_VENDOR_BENCHMARK.md`](../bench/LIVE_VENDOR_BENCHMARK.md)

Each harness binds its dataset, evaluator, dependencies, source inventory,
prediction artifacts, and claimability decision. Vendor outputs are permitted
only in the sealed comparison protocol and never as training, distillation,
routing, prompt, or runtime-extraction input.

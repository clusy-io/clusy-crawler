# Neutral extraction benchmark

We grade article-body extraction on a corpus and metric **we did not author** —
Zyte's [article-extraction-benchmark](https://github.com/scrapinghub/article-extraction-benchmark)
(181 news/article pages, token-shingle F1 against a hand-labelled article body).
This is the number to trust; the `benchmark_sota.py` / `benchmark_deep.py`
harnesses use our own metric and are for internal regression tracking only.

## Reproduce

```bash
git clone https://github.com/scrapinghub/article-extraction-benchmark /tmp/aeb
python bench/neutral_benchmark.py /tmp/aeb      # runs our extract_content over the corpus
cd /tmp/aeb && python evaluate.py               # scores every output/*.json, incl. ours
```

`evaluate.py` prints F1 ± bootstrap CI for our extractor alongside 30+ others.

## Result (2026-07, trafilatura 2.0 base)

| Extractor | F1 | Type |
|-----------|:--:|------|
| AutoExtract (Zyte) | 0.970 | commercial |
| **clusy-crawler** | **0.960 ± 0.007** | **this** |
| trafilatura 2.0 | 0.958 | open-source |
| Diffbot | 0.951 | commercial |
| newspaper4k | 0.949 | open-source |
| readability_js | 0.947 | open-source |
| readability-lxml | 0.922 | open-source |
| goose3 | 0.896 | open-source |
| justext | 0.804 | open-source |

Precision 0.955, recall 0.965. We edge trafilatura 2.0 and beat Diffbot plus
every open-source library; AutoExtract (0.970) is ahead.

**Significance (honest):** paired bootstrap of F1(ours) − F1(trafilatura) =
**+0.003, 95% CI [−0.010, +0.014], P(ours > trafilatura) ≈ 0.69**. So this is a
point-estimate lead on the leaderboard, not a statistically decisive win — on
181 pages a 0.2-point gap sits inside the noise. trafilatura 2.0 is genuinely
excellent and we build on it.

## Not overfit, and no real-world regression

Because the corpus is fixed and its ground truth is visible, tuning heuristics
to *these* pages would inflate the score dishonestly. Guards:

1. **Held-out split.** Every change was validated on a `sha1(key)`-parity *test*
   half it was not tuned on; the final config scores F1 **0.961 on the test
   half**, so the gain isn't an artifact of the tuned half.
2. **Principled, generic changes.** (a) Fix the union base-selection so a
   full-page dump can never win the base slot (this alone was the big one — it
   had collapsed article precision to ~0.55); (b) apply the precision-first path
   only to pages that *declare* themselves news via Open Graph / schema.org
   metadata, not "anything non-technical"; (c) a generic short-line boilerplate
   strip (share bars, bylines, credits).
3. **No regression on diversity.** A blanket favor_precision scored higher on
   this news corpus but *dropped real content* on Wikipedia / reference / data /
   tutorial pages (verified on a 30-URL live mix: reference quality fell
   98 → 68). Gating on the news signal recovers those to 98 while keeping the
   news gain — the improvement is real, not benchmark-chasing.

#!/usr/bin/env python3
"""Deep, category-stratified benchmark on the platform's REAL crawl mix.

The crawler is wired into the agent as `clusy_crawler_scrape` for web research during
data-science / notebook work, so this corpus reflects what the agent actually
fetches: library & API docs, ML papers, GitHub repos, Stack Overflow, tutorials,
Wikipedia reference, data/stats sites, news, and deliberately-hard bot-walled
pages (to characterize failure modes honestly).

Reuses the scoring + vendor adapters from benchmark_sota.py. Adds:
  * per-CATEGORY quality & latency aggregation (where are we strong/weak?)
  * p50/p95 latency, success rate, win counts
  * its own vendor cache (bench/vendor_baseline_deep.json)

Usage:
    EXA_API_KEY=... FIRECRAWL_API_KEY=... \
      python bench/benchmark_deep.py --runs 3 [--refresh-vendors]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

import httpx

# Reuse scoring + adapters (same dir on sys.path when run as a script).
from benchmark_sota import (
    Case,
    fetch_crawler,
    fetch_exa,
    fetch_firecrawl,
    quality,
)

HERE = Path(__file__).parent
VENDOR_CACHE = HERE / "vendor_baseline_deep.json"

# ── Realistic platform corpus ───────────────────────────────────────
CASES: list[Case] = [
    # --- Python data-stack docs (the bread and butter) ---
    Case("pandas DataFrame API", "https://pandas.pydata.org/docs/reference/api/pandas.DataFrame.html",
         "lib-docs", ["dataframe", "data", "columns"], expect_code=True),
    Case("NumPy linspace", "https://numpy.org/doc/stable/reference/generated/numpy.linspace.html",
         "lib-docs", ["linspace", "samples", "interval"], expect_code=True),
    Case("scikit-learn RandomForest",
         "https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.RandomForestClassifier.html",
         "lib-docs", ["random forest", "estimators", "fit"], expect_code=True),
    Case("PyTorch nn.Linear", "https://pytorch.org/docs/stable/generated/torch.nn.Linear.html",
         "lib-docs", ["linear", "features", "bias"], expect_code=True),
    Case("Matplotlib pyplot.plot",
         "https://matplotlib.org/stable/api/_as_gen/matplotlib.pyplot.plot.html",
         "lib-docs", ["plot", "axes", "matplotlib"], expect_code=True),
    Case("Python stdlib json", "https://docs.python.org/3/library/json.html",
         "lib-docs", ["json", "serialize", "object"], expect_code=True),
    # --- JS-heavy framework docs (escalation stress) ---
    Case("React useState", "https://react.dev/reference/react/useState",
         "framework-docs", ["usestate", "state", "component"], expect_code=True, needs_js=True),
    Case("FastAPI first steps", "https://fastapi.tiangolo.com/tutorial/first-steps/",
         "framework-docs", ["fastapi", "path", "uvicorn"], expect_code=True),
    Case("MDN fetch()", "https://developer.mozilla.org/en-US/docs/Web/API/Window/fetch",
         "framework-docs", ["fetch", "request", "promise"], expect_code=True),
    # --- ML papers (arXiv abstract → PDF fallback) ---
    Case("arXiv: Attention", "https://arxiv.org/abs/1706.03762",
         "ml-paper", ["attention", "transformer", "sequence"]),
    Case("arXiv: GPT-3", "https://arxiv.org/abs/2005.14165",
         "ml-paper", ["language model", "few-shot", "gpt"]),
    Case("arXiv: ResNet", "https://arxiv.org/abs/1512.03385",
         "ml-paper", ["residual", "deep", "network"]),
    Case("arXiv: BERT", "https://arxiv.org/abs/1810.04805",
         "ml-paper", ["bert", "bidirectional", "pre-training"]),
    # --- Code repositories (GitHub READMEs) ---
    Case("GitHub: pandas", "https://github.com/pandas-dev/pandas",
         "repo", ["pandas", "data", "python"], expect_code=True),
    Case("GitHub: transformers", "https://github.com/huggingface/transformers",
         "repo", ["transformers", "models", "pytorch"], expect_code=True),
    Case("GitHub: scikit-learn", "https://github.com/scikit-learn/scikit-learn",
         "repo", ["scikit-learn", "machine learning", "python"], expect_code=True),
    # --- Q&A / forums ---
    Case("StackOverflow: pandas select",
         "https://stackoverflow.com/questions/17071871/how-do-i-select-rows-from-a-dataframe-based-on-column-values",
         "qa-forum", ["dataframe", "select", "rows"], expect_code=True),
    Case("StackOverflow: dict merge",
         "https://stackoverflow.com/questions/38987/how-do-i-merge-two-dictionaries-in-a-single-expression",
         "qa-forum", ["dictionary", "merge", "python"], expect_code=True),
    # --- Tutorials / blogs ---
    Case("Real Python: JSON", "https://realpython.com/python-json/",
         "tutorial", ["json", "python", "dump"], expect_code=True),
    Case("ML Mastery: random forest",
         "https://machinelearningmastery.com/random-forest-ensemble-in-python/",
         "tutorial", ["random forest", "ensemble", "python"], expect_code=True),
    # --- Reference / encyclopedic (table/math heavy) ---
    # NB: these concept pages have NO data tables (verified: 0 .wikitable each),
    # so expect_table is False — flagging them True unfairly credited tools that
    # render Wikipedia nav sidebars as tables.
    Case("Wikipedia: Gradient boosting", "https://en.wikipedia.org/wiki/Gradient_boosting",
         "reference", ["gradient boosting", "loss", "tree"]),
    Case("Wikipedia: SVM", "https://en.wikipedia.org/wiki/Support_vector_machine",
         "reference", ["support", "vector", "hyperplane"]),
    Case("Wikipedia: Random forest", "https://en.wikipedia.org/wiki/Random_forest",
         "reference", ["random forest", "decision tree", "bagging"]),
    Case("Wikipedia: Web scraping", "https://en.wikipedia.org/wiki/Web_scraping",
         "reference", ["web scraping", "data", "html"]),
    # --- Data / statistics / government (heavy JS, charts) ---
    Case("Our World in Data: CO2", "https://ourworldindata.org/co2-emissions",
         "data-stats", ["co2", "emissions"], needs_js=True),
    Case("World Bank: GDP", "https://data.worldbank.org/indicator/NY.GDP.MKTP.CD",
         "data-stats", ["gdp", "world bank"], needs_js=True),
    # --- News / current events ---
    Case("Cloudflare blog", "https://blog.cloudflare.com/",
         "news", ["cloudflare"]),
    Case("Hacker News front", "https://news.ycombinator.com",
         "news", ["hacker news", "comments"]),
    # --- Deliberately hard / bot-walled (honest failure modes) ---
    Case("Medium (soft paywall, force-JS)", "https://medium.com/tag/data-science",
         "hard-wall", ["data science"], needs_js=True),
    Case("Reuters (bot wall)", "https://www.reuters.com/technology/",
         "hard-wall", ["reuters", "technology"], needs_js=True),
]


async def measure_crawler(runs: int) -> dict[str, list]:
    out: dict[str, list] = {c.name: [] for c in CASES}
    async with httpx.AsyncClient() as client:
        for c in CASES:  # warmup
            await fetch_crawler(client, c)
        for _ in range(runs):
            for c in CASES:
                s = await fetch_crawler(client, c)
                s.quality = quality(s.md, c) if not s.error else {}
                out[c.name].append(s)
    return out


async def measure_vendors(exa_key: str, fc_key: str) -> dict:
    res: dict = {"exa": {}, "firecrawl": {}}
    async with httpx.AsyncClient() as client:
        for c in CASES:
            if exa_key:
                s = await fetch_exa(client, c, exa_key)
                s.quality = quality(s.md, c) if not s.error else {}
                res["exa"][c.name] = asdict(s)
            if fc_key:
                s = await fetch_firecrawl(client, c, fc_key)
                s.quality = quality(s.md, c) if not s.error else {}
                res["firecrawl"][c.name] = asdict(s)
    return res


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p))]


def report(crawler: dict, vendors: dict, runs: int) -> None:
    by_name = {c.name: c for c in CASES}

    # Per-URL rows (crawler median across runs vs vendor single run)
    print("\n" + "=" * 104)
    print(f"DEEP BENCHMARK — {len(CASES)} URLs, crawler runs={runs}")
    print("quality = 0.40·coverage + 0.30·structure + 0.30·(1−noise)")
    print("=" * 104)
    print(f"{'Category':<16}{'Case':<30}{'crawler':>9}{'exa':>7}{'fire':>7}{'c.p50ms':>9}{'words':>7}")
    print("-" * 104)

    cat_q: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    cat_lat: dict[str, list] = defaultdict(list)
    all_lat: list[float] = []
    fails = {"crawler": [], "exa": [], "firecrawl": []}
    wins = {"crawler": 0, "exa": 0, "firecrawl": 0, "tie": 0}

    for c in CASES:
        samples = crawler.get(c.name, [])
        ok = [s for s in samples if not s.error]
        if ok:
            cq = statistics.median([s.quality["score"] for s in ok])
            clat = pct([s.latency_ms for s in ok], 0.5)
            cw = max(s.quality["words"] for s in ok)
            cat_q[c.category]["crawler"].append(cq)
            cat_lat[c.category].append(clat)
            all_lat.append(clat)
        else:
            cq, clat, cw = None, None, 0
            fails["crawler"].append(c.name)

        vq = {}
        for v in ("exa", "firecrawl"):
            s = vendors.get(v, {}).get(c.name)
            if s and not s.get("error"):
                vq[v] = s["quality"]["score"]
                cat_q[c.category][v].append(vq[v])
            else:
                vq[v] = None
                fails[v].append(c.name)

        # win attribution (highest quality; tie if within 2 pts)
        scored = {k: x for k, x in {"crawler": cq, **vq}.items() if x is not None}
        if scored:
            best = max(scored.values())
            leaders = [k for k, x in scored.items() if best - x <= 2]
            if len(leaders) == 1:
                wins[leaders[0]] += 1
            else:
                wins["tie"] += 1

        cqs = f"{cq:>9.0f}" if cq is not None else f"{'ERR':>9}"
        exs = f"{vq['exa']:>7.0f}" if vq["exa"] is not None else f"{'ERR':>7}"
        fcs = f"{vq['firecrawl']:>7.0f}" if vq["firecrawl"] is not None else f"{'ERR':>7}"
        lts = f"{clat:>9.0f}" if clat is not None else f"{'-':>9}"
        print(f"{c.category:<16}{c.name[:29]:<30}{cqs}{exs}{fcs}{lts}{cw:>7}")

    # Per-category aggregate
    print("\n" + "=" * 104)
    print("PER-CATEGORY QUALITY (mean) + crawler latency")
    print("-" * 104)
    print(f"{'Category':<16}{'n':>3}{'crawler':>9}{'exa':>7}{'fire':>7}{'c.p50':>8}{'c.p95':>8}  winner")
    for cat in sorted(cat_q):
        n = len(cat_q[cat]["crawler"]) or len(next(iter(cat_q[cat].values()), []))
        cm = statistics.mean(cat_q[cat]["crawler"]) if cat_q[cat]["crawler"] else 0
        em = statistics.mean(cat_q[cat]["exa"]) if cat_q[cat]["exa"] else 0
        fm = statistics.mean(cat_q[cat]["firecrawl"]) if cat_q[cat]["firecrawl"] else 0
        lat = cat_lat[cat]
        winner = max([("crawler", cm), ("exa", em), ("firecrawl", fm)], key=lambda x: x[1])[0]
        print(f"{cat:<16}{n:>3}{cm:>9.1f}{em:>7.1f}{fm:>7.1f}"
              f"{pct(lat,0.5):>8.0f}{pct(lat,0.95):>8.0f}  {winner}")

    # Overall
    def overall(vendor: str) -> tuple[float, float]:
        qs = [q for cat in cat_q.values() for q in cat[vendor]]
        return (statistics.mean(qs) if qs else 0), len(qs)

    print("\n" + "=" * 104)
    print("OVERALL")
    print("-" * 104)
    cq, cn = overall("crawler")
    eq, en = overall("exa")
    fq, fn = overall("firecrawl")
    print(f"crawler  : quality {cq:5.1f}  | p50 {pct(all_lat,0.5):.0f}ms p95 {pct(all_lat,0.95):.0f}ms"
          f"  | success {cn}/{len(CASES)}")
    print(f"exa      : quality {eq:5.1f}  | success {en}/{len(CASES)}")
    print(f"firecrawl: quality {fq:5.1f}  | success {fn}/{len(CASES)}")
    print(f"\nper-URL wins (≤2pt = tie): {wins}")
    if any(fails.values()):
        print("\nfailures / empty:")
        for v, fl in fails.items():
            if fl:
                print(f"  {v}: {', '.join(fl)}")

    (HERE / "result_deep.json").write_text(json.dumps({
        "runs": runs,
        "overall": {"crawler": cq, "exa": eq, "firecrawl": fq,
                    "crawler_p50": pct(all_lat, 0.5), "crawler_p95": pct(all_lat, 0.95)},
        "wins": wins,
        "fails": fails,
        "crawler": {k: [asdict(s) for s in v] for k, v in crawler.items()},
    }, indent=2))
    print(f"\nSaved → {HERE / 'result_deep.json'}")


async def main() -> None:
    import os
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--refresh-vendors", action="store_true")
    args = ap.parse_args()

    if not args.refresh_vendors and VENDOR_CACHE.exists():
        print("Using cached vendor baseline (--refresh-vendors to re-measure).")
        vendors = json.loads(VENDOR_CACHE.read_text())
        # Re-score cached vendor markdown with the CURRENT case flags so a change
        # to gold/structure labels applies equally to vendors and crawler.
        by_name = {c.name: c for c in CASES}
        for v in vendors.values():
            for name, s in v.items():
                if not s.get("error") and name in by_name:
                    s["quality"] = quality(s.get("md", ""), by_name[name])
    else:
        print("Measuring vendors (Exa + Firecrawl) — costs API credits...")
        vendors = await measure_vendors(os.getenv("EXA_API_KEY", ""), os.getenv("FIRECRAWL_API_KEY", ""))
        VENDOR_CACHE.write_text(json.dumps(vendors, indent=2))

    print(f"Measuring local crawler ({args.runs} runs/URL + warmup)...")
    crawler = await measure_crawler(args.runs)
    report(crawler, vendors, args.runs)


if __name__ == "__main__":
    asyncio.run(main())

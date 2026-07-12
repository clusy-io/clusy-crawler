#!/usr/bin/env python3
"""Rigorous Clusy Crawler vs Exa vs Firecrawl benchmark.

Unlike benchmark_compare.py (single-run, word-count-as-quality), this harness:
  * Runs the local crawler N times per URL and reports p50/p95 latency.
  * Measures competitors ONCE and caches the result (they are the fixed
    reference baseline and cost money per call) — see --refresh-vendors.
  * Scores extraction QUALITY deterministically, not by word count:
      - gold-phrase coverage  (did we capture the main content?)   40%
      - structure fidelity    (headings / code / tables preserved) 30%
      - noise penalty         (CSS/JS/nav boilerplate leakage)     30%
  * Writes a JSON result file so BEFORE/AFTER runs can be diffed.

Usage:
    EXA_API_KEY=... FIRECRAWL_API_KEY=... \
      python bench/benchmark_sota.py --label before --runs 3

    # later, after code changes (re-uses cached vendor numbers):
    python bench/benchmark_sota.py --label after --runs 3

    # force re-measuring the paid vendors:
    python bench/benchmark_sota.py --label after --refresh-vendors
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

CRAWLER_URL = os.getenv("CRAWLER_URL", "http://127.0.0.1:11235")
HERE = Path(__file__).parent
VENDOR_CACHE = HERE / "vendor_baseline.json"


# ── Benchmark corpus ────────────────────────────────────────────────
# gold: lowercased phrases that MUST appear in a faithful extraction.
# struct: expected structural elements (headings/code/table).
@dataclass
class Case:
    name: str
    url: str
    category: str
    gold: list[str]
    expect_code: bool = False
    expect_table: bool = False
    needs_js: bool = False


CASES: list[Case] = [
    Case("Example.com", "https://example.com", "static",
         ["documentation examples", "learn more"]),
    Case("Wikipedia: Web scraping", "https://en.wikipedia.org/wiki/Web_scraping", "article",
         ["web scraping", "data scraping", "html", "crawler"], expect_table=True),
    Case("Python asyncio docs", "https://docs.python.org/3/library/asyncio.html", "documentation",
         ["asyncio", "coroutine", "event loop", "async def"], expect_code=True),
    Case("Rust Book ch1", "https://doc.rust-lang.org/book/ch01-00-getting-started.html",
         "documentation", ["getting started", "installation", "cargo"], expect_code=True),
    Case("GitHub README crawl4ai", "https://github.com/unclecode/crawl4ai", "repository",
         ["crawl4ai", "llm", "web crawler"], expect_code=True),
    Case("arXiv Mamba abstract", "https://arxiv.org/abs/2312.00752", "academic",
         ["mamba", "state space", "sequence", "transformer"]),
    Case("MDN: Array", "https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Array",
         "documentation", ["array", "javascript", "method", "elements"], expect_code=True),
    Case("Hacker News front", "https://news.ycombinator.com", "listing",
         ["hacker news", "comments"]),
]


# ── Quality scoring (deterministic) ─────────────────────────────────

# Heuristic detectors for content that should NOT be in clean markdown.
_CSS_RULE = re.compile(r"\{[^{}]*(?:color|margin|padding|font|background|width|display|border)\s*:[^{}]*\}")
_JS_LEAK = re.compile(r"\b(function\s*\(|var\s+\w+\s*=|document\.|window\.|=>\s*\{|addEventListener)\b")
_NAV_BOILERPLATE = re.compile(
    r"\b(skip to (main )?content|cookie|accept all|sign in|sign up|subscribe|"
    r"privacy policy|terms of service|all rights reserved|toggle navigation)\b",
    re.IGNORECASE,
)


def _strip_code(md: str) -> str:
    """Remove fenced + inline code so legitimate code EXAMPLES (common in docs)
    aren't scored as CSS/JS leakage. Length penalty still uses the full output."""
    md = re.sub(r"```.*?```", " ", md, flags=re.DOTALL)
    md = re.sub(r"`[^`]+`", " ", md)
    return md


def _noise_score(md: str) -> tuple[float, dict]:
    """1.0 = clean, 0.0 = heavy boilerplate/markup leakage."""
    if not md:
        return 0.0, {"css": 0, "js": 0, "nav": 0}
    prose = _strip_code(md)
    css = len(_CSS_RULE.findall(prose))
    js = len(_JS_LEAK.findall(prose))
    nav = len(_NAV_BOILERPLATE.findall(prose))
    # Penalty proportional to leak density per 1k chars.
    per_k = 1000.0 / max(len(md), 1)
    penalty = (css * 4 + js * 2 + nav * 1) * per_k
    score = max(0.0, 1.0 - min(penalty, 1.0))
    return score, {"css": css, "js": js, "nav": nav}


def _structure_score(md: str, case: Case) -> float:
    if not md:
        return 0.0
    checks: list[bool] = []
    has_heading = bool(re.search(r"^#{1,6}\s+\S", md, re.MULTILINE))
    checks.append(has_heading)
    if case.expect_code:
        checks.append("```" in md or bool(re.search(r"`[^`]+`", md)))
    if case.expect_table:
        checks.append(bool(re.search(r"\|.*\|.*\n\|[-: |]+\|", md)))
    return sum(checks) / len(checks) if checks else 1.0


def _gold_coverage(md: str, case: Case) -> float:
    if not case.gold:
        return 1.0
    low = md.lower()
    hit = sum(1 for g in case.gold if g.lower() in low)
    return hit / len(case.gold)


def quality(md: str, case: Case) -> dict:
    cov = _gold_coverage(md, case)
    struct = _structure_score(md, case)
    noise, breakdown = _noise_score(md)
    score = 0.40 * cov + 0.30 * struct + 0.30 * noise
    return {
        "score": round(score * 100, 1),
        "coverage": round(cov, 3),
        "structure": round(struct, 3),
        "noise": round(noise, 3),
        "noise_detail": breakdown,
        "words": len(md.split()),
        "chars": len(md),
    }


# ── Vendor adapters ─────────────────────────────────────────────────


@dataclass
class Sample:
    vendor: str
    case: str
    url: str
    latency_ms: float = 0.0
    md: str = ""
    error: str = ""
    quality: dict = field(default_factory=dict)


async def fetch_crawler(client: httpx.AsyncClient, case: Case) -> Sample:
    t0 = time.monotonic()
    try:
        resp = await client.post(
            f"{CRAWLER_URL}/crawl",
            json={"urls": [case.url], "js_render": None},
            timeout=90,
        )
        dt = (time.monotonic() - t0) * 1000
        r = resp.json()["results"][0]
        if r.get("error"):
            return Sample("crawler", case.name, case.url, dt, error=r["error"])
        return Sample("crawler", case.name, case.url, dt, md=r.get("markdown", ""))
    except Exception as e:
        return Sample("crawler", case.name, case.url, (time.monotonic() - t0) * 1000, error=str(e))


async def fetch_firecrawl(client: httpx.AsyncClient, case: Case, key: str) -> Sample:
    t0 = time.monotonic()
    try:
        resp = await client.post(
            "https://api.firecrawl.dev/v2/scrape",
            headers={"Authorization": f"Bearer {key}"},
            json={"url": case.url, "formats": ["markdown"], "onlyMainContent": True},
            timeout=120,
        )
        dt = (time.monotonic() - t0) * 1000
        d = resp.json()
        if not d.get("success", True):
            return Sample("firecrawl", case.name, case.url, dt, error=str(d.get("error", "fail")))
        md = (d.get("data") or {}).get("markdown", "") or ""
        return Sample("firecrawl", case.name, case.url, dt, md=md)
    except Exception as e:
        return Sample("firecrawl", case.name, case.url, (time.monotonic() - t0) * 1000, error=str(e))


async def fetch_exa(client: httpx.AsyncClient, case: Case, key: str) -> Sample:
    t0 = time.monotonic()
    try:
        resp = await client.post(
            "https://api.exa.ai/contents",
            headers={"x-api-key": key, "Content-Type": "application/json"},
            json={"urls": [case.url], "text": True},
            timeout=60,
        )
        dt = (time.monotonic() - t0) * 1000
        d = resp.json()
        results = d.get("results", [])
        if not results:
            return Sample("exa", case.name, case.url, dt, error="no results")
        md = results[0].get("text", "") or ""
        return Sample("exa", case.name, case.url, dt, md=md)
    except Exception as e:
        return Sample("exa", case.name, case.url, (time.monotonic() - t0) * 1000, error=str(e))


# ── Runner ──────────────────────────────────────────────────────────


async def measure_crawler(runs: int) -> dict[str, list[Sample]]:
    out: dict[str, list[Sample]] = {c.name: [] for c in CASES}
    async with httpx.AsyncClient() as client:
        # warmup (browser cold start, DNS)
        for c in CASES:
            await fetch_crawler(client, c)
        for _ in range(runs):
            for c in CASES:
                s = await fetch_crawler(client, c)
                s.quality = quality(s.md, c) if not s.error else {}
                out[c.name].append(s)
    return out


async def measure_vendors() -> dict[str, dict[str, Sample]]:
    exa_key = os.getenv("EXA_API_KEY", "")
    fc_key = os.getenv("FIRECRAWL_API_KEY", "")
    res: dict[str, dict[str, Sample]] = {"exa": {}, "firecrawl": {}}
    async with httpx.AsyncClient() as client:
        for c in CASES:
            if exa_key:
                s = await fetch_exa(client, c, exa_key)
                s.quality = quality(s.md, c) if not s.error else {}
                res["exa"][c.name] = s
            if fc_key:
                s = await fetch_firecrawl(client, c, fc_key)
                s.quality = quality(s.md, c) if not s.error else {}
                res["firecrawl"][c.name] = s
    return res


def load_vendor_cache() -> dict | None:
    if VENDOR_CACHE.exists():
        return json.loads(VENDOR_CACHE.read_text())
    return None


def save_vendor_cache(vendors: dict[str, dict[str, Sample]]) -> None:
    serial = {v: {k: asdict(s) for k, s in cases.items()} for v, cases in vendors.items()}
    VENDOR_CACHE.write_text(json.dumps(serial, indent=2))


def p(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(len(s) * pct))
    return s[idx]


def report(label: str, crawler: dict[str, list[Sample]], vendors: dict, runs: int) -> None:
    print("\n" + "=" * 100)
    print(f"BENCHMARK [{label}]  —  crawler runs={runs}, quality = 0.4·coverage + 0.3·structure + 0.3·(1-noise)")
    print("=" * 100)
    hdr = f"{'Case':<26}{'Vendor':<11}{'p50 ms':>8}{'p95 ms':>8}{'Qual':>6}{'Cov':>6}{'Str':>6}{'Noise':>6}{'Words':>7}"
    print(hdr)
    print("-" * 100)

    crawler_q, crawler_lat = [], []
    vend_q = {"exa": [], "firecrawl": []}
    vend_lat = {"exa": [], "firecrawl": []}

    for c in CASES:
        samples = crawler.get(c.name, [])
        ok = [s for s in samples if not s.error]
        if ok:
            lats = [s.latency_ms for s in ok]
            quals = [s.quality["score"] for s in ok]
            med_q = statistics.median(quals)
            best = max(ok, key=lambda s: s.quality["score"])
            crawler_q.append(med_q)
            crawler_lat.append(p(lats, 0.5))
            print(f"{c.name:<26}{'crawler':<11}{p(lats,0.5):>8.0f}{p(lats,0.95):>8.0f}"
                  f"{med_q:>6.0f}{best.quality['coverage']:>6.2f}{best.quality['structure']:>6.2f}"
                  f"{best.quality['noise']:>6.2f}{best.quality['words']:>7d}")
        else:
            err = samples[0].error[:40] if samples else "no data"
            print(f"{c.name:<26}{'crawler':<11}  ERROR: {err}")

        for v in ("exa", "firecrawl"):
            s = vendors.get(v, {}).get(c.name)
            if not s:
                continue
            if isinstance(s, dict):
                lat, q, err = s["latency_ms"], s.get("quality") or {}, s.get("error", "")
            else:
                lat, q, err = s.latency_ms, s.quality, s.error
            if err:
                print(f"{'':<26}{v:<11}  ERROR: {err[:40]}")
                continue
            vend_q[v].append(q["score"])
            vend_lat[v].append(lat)
            print(f"{'':<26}{v:<11}{lat:>8.0f}{lat:>8.0f}{q['score']:>6.0f}{q['coverage']:>6.2f}"
                  f"{q['structure']:>6.2f}{q['noise']:>6.2f}{q['words']:>7d}")
        print()

    print("=" * 100)
    print("AGGREGATE (mean across cases)")
    print("-" * 100)
    print(f"{'Vendor':<12}{'avg p50 ms':>12}{'avg quality':>13}{'cases':>7}")

    def line(name, lat, q):
        if q:
            print(f"{name:<12}{statistics.mean(lat):>12.0f}{statistics.mean(q):>13.1f}{len(q):>7d}")

    line("crawler", crawler_lat, crawler_q)
    line("exa", vend_lat["exa"], vend_q["exa"])
    line("firecrawl", vend_lat["firecrawl"], vend_q["firecrawl"])

    # Persist
    out_file = HERE / f"result_{label}.json"
    payload = {
        "label": label,
        "runs": runs,
        "crawler": {k: [asdict(s) for s in v] for k, v in crawler.items()},
        "aggregate": {
            "crawler": {"p50_ms": statistics.mean(crawler_lat) if crawler_lat else 0,
                        "quality": statistics.mean(crawler_q) if crawler_q else 0},
        },
    }
    out_file.write_text(json.dumps(payload, indent=2))
    print(f"\nSaved → {out_file}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="run")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--refresh-vendors", action="store_true")
    args = ap.parse_args()

    cached = None if args.refresh_vendors else load_vendor_cache()
    if cached is None:
        print("Measuring vendors (Exa + Firecrawl) — this costs API credits...")
        vendors = await measure_vendors()
        save_vendor_cache(vendors)
        vendors_serial = {v: {k: asdict(s) for k, s in cs.items()} for v, cs in vendors.items()}
    else:
        print("Using cached vendor baseline (use --refresh-vendors to re-measure).")
        vendors_serial = cached

    print(f"Measuring local crawler ({args.runs} runs/URL + warmup)...")
    crawler = await measure_crawler(args.runs)
    report(args.label, crawler, vendors_serial, args.runs)


if __name__ == "__main__":
    asyncio.run(main())

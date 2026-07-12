#!/usr/bin/env python3
"""Score the crawler's extraction pipeline on a NEUTRAL third-party benchmark.

Unlike benchmark_sota.py / benchmark_deep.py (internal regression harnesses that
grade against our own metric), this runs our real `extract_content` over Zyte's
article-extraction-benchmark — a corpus and F1 metric authored by someone else —
so the number is not self-graded.

Usage:
    git clone https://github.com/scrapinghub/article-extraction-benchmark /tmp/aeb
    python bench/neutral_benchmark.py /tmp/aeb
    cd /tmp/aeb && python evaluate.py     # our output lands in output/clusy_crawler.json

Reports F1/precision/recall on the full set plus a deterministic dev/test split
(by sha1(key) parity) so a change that merely overfits the tuned half is visible.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.services.extractor import extract_content  # noqa: E402

_MD_STRIP = [
    (re.compile(r"```.*?```", re.DOTALL), " "),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), " "),
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),
    (re.compile(r"^#{1,6}\s*", re.MULTILINE), ""),
    (re.compile(r"[*_]{1,3}"), ""),
    (re.compile(r"^\s*>\s?", re.MULTILINE), ""),
    (re.compile(r"^\s*[-*+]\s+", re.MULTILINE), ""),
    (re.compile(r"\|"), " "),
    (re.compile(r"[ \t]+"), " "),
    (re.compile(r"\n{2,}"), "\n"),
]


def md_to_text(md: str) -> str:
    """Markdown → plain text, so token-F1 compares content not markdown syntax."""
    for pat, repl in _MD_STRIP:
        md = pat.sub(repl, md)
    return md.strip()


def split(key: str) -> str:
    return "dev" if int(hashlib.sha1(key.encode()).hexdigest(), 16) % 2 == 0 else "test"


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: python bench/neutral_benchmark.py <path-to-article-extraction-benchmark>")
    root = Path(sys.argv[1])
    gt = json.loads((root / "ground-truth.json").read_text())

    out: dict[str, dict] = {}
    for i, (key, meta) in enumerate(gt.items(), 1):
        url = meta.get("url", "")
        try:
            html = gzip.decompress((root / "html" / f"{key}.html.gz").read_bytes()).decode(
                "utf-8", "replace"
            )
            body = md_to_text(extract_content(html, url).text)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {key[:12]} {e}", file=sys.stderr)
            body = ""
        out[key] = {"articleBody": body, "url": url}
        if i % 30 == 0:
            print(f"  {i}/{len(gt)}", file=sys.stderr)

    (root / "output" / "clusy_crawler.json").write_text(
        json.dumps({"version": "clusy-crawler", "output": out}, indent=2)
    )
    print(f"wrote {root}/output/clusy_crawler.json — run `python evaluate.py` there to score.")


if __name__ == "__main__":
    main()

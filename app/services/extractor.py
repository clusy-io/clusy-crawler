from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
import trafilatura
from lxml import html as lxml_html
from markdownify import markdownify

from app.config import settings

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

logger = structlog.get_logger()


def _html_to_markdown(html: str) -> str:
    """Convert an HTML fragment to markdown.

    Uses markdownify (MIT) — the permissive replacement for html2text (GPLv3).
    Mirrors the old html2text settings: no hard wrapping (``body_width = 0``),
    images stripped, links kept. ATX (``#``) headings match the rest of the
    pipeline's output.
    """
    return markdownify(html, strip=["img"], heading_style="ATX")


@dataclass
class ExtractionResult:
    text: str = ""
    title: str = ""
    description: str = ""
    language: str = ""
    word_count: int = 0
    strategy: str = ""


def _count_words(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))


# Minimum words for a strategy's output to be considered usable. Kept low so a
# short-but-complete page (e.g. example.com) yields its real clean text instead
# of falling through to the CSS-leaking raw_lxml last resort. The escalate-to-JS
# decision is made separately (crawler.SPARSE_WORD_FLOOR), not here.
_MIN_ACCEPT_WORDS = 8

# Strategy trust order for choosing the union base. Specialized + trafilatura
# produce the cleanest markdown; markdownify captures the most text but also the
# most boilerplate, so it loses ties.
_STRATEGY_RANK = {
    "documentation": 5,
    "github-readme": 5,
    "trafilatura": 4,
    "readability": 3,
    "markdownify": 2,
    "raw_lxml": 0,
}

# Above this word count the base extraction is "rich" enough that augmenting it
# with other strategies' paragraphs adds more noise than signal, so we skip it.
_RICH_WORDS = 400

# Strategies whose paragraphs are safe to fold into a clean base for recall.
# These are boilerplate-REMOVING extractors; the full-page dumps (markdownify,
# raw_lxml) are deliberately excluded — see _merge_union.
_CLEAN_AUGMENT_STRATEGIES = frozenset(
    {"trafilatura", "readability", "documentation", "github-readme"}
)

# A genuine news/blog article, per its own metadata (Open Graph og:type, an
# article schema, or an article:published_time). This is deliberately NARROW:
# the precision-first body path (favor_precision + boilerplate strip) lifts
# short news F1 but DROPS real content on long, content-rich pages (Wikipedia,
# data/reference pages, tutorials), so we gate it on a positive news signal
# rather than "anything that isn't technical".
_NEWS_META = re.compile(
    r'og:type"\s+content="(?:article|news)'
    r'|"@type"\s*:\s*"(?:News|Blog|Reportage)?(?:Article|BlogPosting)"'
    r'|property="article:published_time"',
    re.IGNORECASE,
)


def _is_news_article(html_content: str) -> bool:
    return bool(_NEWS_META.search(html_content[:60000]))


# ── Code block preservation ────────────────────────────────────────


def _extract_code_blocks_from_html(html: str) -> list[str]:
    """Extract all <pre><code> blocks from HTML as markdown code blocks.

    Returns pre-formatted markdown code blocks ready for injection.
    """
    blocks: list[str] = []
    pattern = re.compile(
        r"<pre[^>]*>\s*<code[^>]*>(.*?)</code>\s*</pre>",
        re.IGNORECASE | re.DOTALL,
    )
    for m in pattern.finditer(html):
        code = m.group(1)
        code = code.replace("&lt;", "<").replace("&gt;", ">")
        code = code.replace("&amp;", "&").replace("&quot;", '"')
        code = code.replace("&#39;", "'").replace("&#x27;", "'")
        if len(code.strip()) > 10:  # Filter trivial fragments
            blocks.append(f"\n```\n{code}\n```\n")
    return blocks


def _inject_missing_code_blocks(extracted_text: str, original_html: str) -> str:
    """Ensure all code blocks from original HTML appear in extracted text.

    If trafilatura stripped a code block that existed in the original HTML,
    re-inject it at the end of the output.
    """
    html_blocks = _extract_code_blocks_from_html(original_html)
    if not html_blocks:
        return extracted_text

    # Check which blocks are already present (fuzzy match)
    existing_code = set()
    for match in re.finditer(r"```(?:\w+)?\n(.*?)```", extracted_text, re.DOTALL):
        existing_code.add(match.group(1).strip()[:100])

    missing = []
    for block in html_blocks:
        inner = re.search(r"```\n(.*?)```", block, re.DOTALL)
        if inner:
            code_snippet = inner.group(1).strip()[:100]
            if code_snippet not in existing_code:
                missing.append(block)

    if missing:
        extracted_text += "\n\n## Extracted Code\n\n" + "\n".join(missing)

    return extracted_text


# ── Page type detection ────────────────────────────────────────────


def _detect_page_type(html_content: str, url: str) -> str:
    lower = html_content[:20000].lower()
    lower_url = url.lower()

    academic_urls = (
        "arxiv.org/abs/", "arxiv.org/pdf/", "papers.", "proceedings.",
        "doi.org/10.", "/paper/", "pubmed", "scholar",
    )
    latex_pattern = re.search(r"\\begin\{|\\cite\{|\\ref\{|\\usepackage", lower)
    if any(k in lower_url for k in academic_urls) or latex_pattern:
        return "academic"

    if any(k in lower_url for k in ("github.com", "gitlab.com", "bitbucket.org")):
        return "repository"

    if any(k in lower_url for k in ("docs.", "/docs/", "readthedocs", "documentation")):
        return "documentation"

    article_indicators = (
        "<article", 'role="article"', 'class="post',
        'class="entry', 'class="blog', "blog-post", "post-content",
    )
    if any(k in lower for k in article_indicators):
        return "article"

    if any(k in lower for k in ("forum", "thread", "discussion", "topic", "board")):
        return "forum"

    if re.search(r'\$\d+\.\d{2}|price|add.to.cart|buy.now|sku', lower):
        return "product"

    listing_indicators = ("search result", "items found", "sort by", "filter by")
    result_count = lower.count('class="result') + lower.count('class="item')
    if any(k in lower for k in listing_indicators) or result_count >= 3:
        return "listing"

    if re.search(r'gallery|grid|masonry|card', lower) and lower.count('<img') >= 3:
        return "collection"

    return "webpage"


# ── Individual strategies ──────────────────────────────────────────


def _extract_with_trafilatura(
    html_content: str, url: str, page_type: str
) -> ExtractionResult | None:
    # News/blog articles take the precision-first body path: favor_precision drops
    # residual boilerplate, and comments are reader chatter we exclude. Inline
    # tables (a data table in the article flow) are kept — trafilatura only emits
    # main-content tables — while the widget-table DUMP is a separate step we skip
    # for news. Beats standalone trafilatura 2.0 on the Zyte news corpus (F1 0.960
    # vs 0.958, holds on a held-out half) WITHOUT the recall loss that a blanket
    # favor_precision inflicts on Wikipedia/reference/tutorial pages.
    news = _is_news_article(html_content)
    config: dict[str, Any] = {
        "output_format": "markdown",
        "include_images": False,
        "include_links": True,
        "include_comments": False,
        "include_tables": True,
        "favor_precision": news or page_type == "academic",
        "url": url,
    }
    text = trafilatura.extract(html_content, **config)
    if not text or not text.strip():
        return None
    metadata = trafilatura.bare_extraction(html_content, url=url, favor_precision=True)
    return ExtractionResult(
        text=text,
        title=getattr(metadata, "title", "") or "",
        description=getattr(metadata, "description", "") or "",
        language=getattr(metadata, "language", "") or "",
        word_count=_count_words(text),
        strategy="trafilatura",
    )


def _extract_with_readability(
    html_content: str, url: str, page_type: str
) -> ExtractionResult | None:
    try:
        from readability import Document
        doc = Document(html_content)
        title = doc.title() or ""
        summary_html = doc.summary()
        if not summary_html or not summary_html.strip():
            return None
        text = _html_to_markdown(summary_html)
        word_count = _count_words(text)
        if word_count < _MIN_ACCEPT_WORDS:
            return None
        return ExtractionResult(
            text=text, title=title, word_count=word_count, strategy="readability"
        )
    except Exception:
        return None


def _extract_with_markdownify(
    html_content: str, url: str, page_type: str
) -> ExtractionResult | None:
    text = _html_to_markdown(html_content)
    if not text or not text.strip():
        return None
    word_count = _count_words(text)
    if word_count < _MIN_ACCEPT_WORDS:
        return None
    try:
        tree = lxml_html.fromstring(html_content.encode("utf-8"))
        title_el = tree.xpath("//title/text()")
        title = title_el[0].strip() if title_el else ""
    except Exception:
        title = ""
    return ExtractionResult(text=text, title=title, word_count=word_count, strategy="markdownify")


def _extract_raw_text(html_content: str) -> ExtractionResult:
    try:
        tree = lxml_html.fromstring(html_content.encode("utf-8"))
        # Drop non-content subtrees first — otherwise <style>/<script> bodies
        # leak verbatim into the output (e.g. example.com → CSS rules in markdown).
        for sel in ("script", "style", "noscript", "template", "head", "svg"):
            for el in tree.cssselect(sel):
                parent = el.getparent()
                if parent is not None:
                    parent.remove(el)
        text_parts: list[str] = []
        for el in tree.iter():
            if (
                el.tag in ("table", "pre", "div", "p", "br", "h1", "h2", "h3", "h4", "h5", "h6")
                and text_parts
                and not text_parts[-1].endswith("\n")
            ):
                text_parts.append("\n")
            if el.text:
                text_parts.append(el.text.strip())
            if el.tail:
                text_parts.append(el.tail.strip())
        text = " ".join(text_parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html_content)
    text = re.sub(r"\s+", " ", text).strip()
    return ExtractionResult(text=text, word_count=_count_words(text), strategy="raw_lxml")


# ── GitHub-specific extraction ──────────────────────────────────────


def _extract_github(html_content: str, url: str) -> ExtractionResult | None:
    """Extract GitHub's README from `.markdown-body` via markdownify.

    The README body is already chrome-free in GitHub's server-rendered HTML, so
    running markdownify on just that subtree yields clean markdown (headings, code
    fences, links) without the fragile custom DOM walker that previously
    collapsed the whole README to a single title line.
    """
    try:
        tree = lxml_html.fromstring(html_content.encode("utf-8"))
    except Exception:
        return None

    main = None
    for sel in ("article.markdown-body", ".markdown-body", '[itemprop="text"]', "#readme"):
        found = tree.cssselect(sel)
        if found:
            main = found[0]
            break
    if main is None:
        return None

    title = ""
    h1 = main.cssselect("h1")
    if h1:
        title = h1[0].text_content().strip()

    inner = lxml_html.tostring(main, encoding="unicode")
    text = _html_to_markdown(inner).strip()

    word_count = _count_words(text)
    if word_count < _MIN_ACCEPT_WORDS:
        return None

    return ExtractionResult(
        text=text, title=title, word_count=word_count, strategy="github-readme"
    )


# ── Documentation-specific extraction ───────────────────────────────

_DOC_NOISE_SELECTORS = [
    # Navigation elements (safest to remove)
    ".wy-nav-side", ".wy-nav-top", ".wy-side-nav-search",
    ".wy-menu-vertical", ".sphinxsidebar", ".sphinxsidebarwrapper",
    ".md-sidebar--primary", ".md-sidebar--secondary",
    ".md-header", ".md-footer", ".md-tabs",
    ".rst-versions", ".documentation-breadcrumbs",
    '[role="navigation"]', '[role="search"]',
    # Action buttons
    ".headerlink", ".viewcode-link", ".edit-this-page",
    ".theme-switcher", ".md-source", ".md-top", ".md-version",
    ".feedback",
    # Non-content
    "script", "style", "noscript", "iframe", "template",
]


def _extract_documentation(html_content: str, url: str) -> ExtractionResult | None:
    try:
        tree = lxml_html.fromstring(html_content.encode("utf-8"))
    except Exception:
        return _extract_with_trafilatura(html_content, url, "documentation")

    for sel in _DOC_NOISE_SELECTORS:
        try:
            for el in tree.cssselect(sel):
                parent = el.getparent()
                if parent is not None:
                    parent.remove(el)
        except Exception:
            pass

    main = None
    main_selectors = [
        'div[role="main"]', "main", "article",
        ".document", ".rst-content", ".md-content",
        ".markdown-body", ".content", "#content",
        ".documentation", ".doc-content", ".wy-nav-content",
    ]
    for sel in main_selectors:
        found = tree.cssselect(sel)
        if found:
            main = found[0]
            break
    if main is None:
        main = tree

    title = ""
    for title_sel in ("h1", ".documenttitle", ".page-title", "title"):
        found = main.cssselect(title_sel)
        if found:
            title = found[0].text_content().strip()
            if len(title) > 5:
                break

    parts: list[str] = []
    if title:
        parts.append(f"# {title}\n")
    _doc_walk(main, parts)

    text = "\n".join(parts)
    word_count = _count_words(text)
    if word_count < _MIN_ACCEPT_WORDS:
        return _extract_with_trafilatura(html_content, url, "documentation")

    return ExtractionResult(
        text=text, title=title,
        description=title[:300] if title else "",
        word_count=word_count, strategy="documentation",
    )


def _doc_walk(el: Any, parts: list[str], level: int = 0) -> None:
    for child in el:
        tag = child.tag if isinstance(child.tag, str) else ""

        if tag == "pre" or (tag == "div" and "highlight" in (child.get("class", "") or "")):
            code_el = child.cssselect("code") or [child]
            code_text = code_el[0].text_content()
            lang = ""
            classes = code_el[0].get("class", "") or ""
            for c in classes.split():
                if c.startswith("language-") or c.startswith("lang-"):
                    lang = c.split("-", 1)[1]
                    break
            parts.append(f"\n```{lang}\n{code_text}\n```\n")
            continue

        if tag == "code":
            ct = child.text_content()
            if ct.strip():
                parts.append(f"`{ct}`")
            continue

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            h_level = int(tag[1])
            text = child.text_content().strip()
            if text and len(text) > 1:
                parts.append(f"\n{'#' * (h_level + 1)} {text}\n")
            continue

        if tag == "dt":
            sig = child.text_content().strip()
            dd = child.getnext()
            if dd is not None and dd.tag == "dd":
                desc = dd.text_content().strip()
                parts.append(f"\n**`{sig}`**\n\n{desc}\n")
            else:
                parts.append(f"\n**`{sig}`**\n")
            continue

        if tag == "dd":
            continue

        if tag in ("p", "div", "section", "li", "blockquote", "td", "th"):
            text = child.text_content().strip()
            if text and len(text) > 1 and not any(text in p for p in parts[-2:]):
                # Add newlines before major sections
                if tag in ("div", "section"):
                    parts.append("\n")
                parts.append(text + "\n")
            # Recurse only if this element might have useful children
            if tag in ("div", "section", "li", "blockquote"):
                _doc_walk(child, parts, level + 1)
            continue

        if tag in ("a", "span", "em", "strong", "b", "i", "small", "img", "br", "hr"):
            continue

        if tag in ("ul", "ol", "dl"):
            _doc_walk(child, parts, level + 1)
            continue

        if tag in ("nav", "header", "footer", "script", "style"):
            continue

        if tag == "table":
            # Convert simple tables to text
            rows = child.cssselect("tr")
            if rows:
                parts.append("\n")
                for row in rows:
                    cells = row.cssselect("td, th")
                    parts.append(" | ".join(c.text_content().strip() for c in cells) + "\n")
                parts.append("\n")
            continue

        _doc_walk(child, parts, level + 1)


# ── Parallel multi-strategy execution ───────────────────────────────


async def _parallel_extract(html_content: str, url: str, page_type: str) -> list[ExtractionResult]:
    loop = asyncio.get_running_loop()

    tasks = [
        loop.run_in_executor(None, _extract_with_trafilatura, html_content, url, page_type),
        loop.run_in_executor(None, _extract_with_readability, html_content, url, page_type),
        loop.run_in_executor(None, _extract_with_markdownify, html_content, url, page_type),
    ]

    if page_type == "documentation":
        tasks.append(loop.run_in_executor(None, _extract_documentation, html_content, url))
    if page_type == "repository":
        tasks.append(loop.run_in_executor(None, _extract_github, html_content, url))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: list[ExtractionResult] = []
    for r in results:
        if isinstance(r, ExtractionResult) and r.word_count >= _MIN_ACCEPT_WORDS:
            out.append(r)
    return out


# ── Multi-extractor union merging ───────────────────────────────────


def _merge_union(results: list[ExtractionResult], news: bool = False) -> ExtractionResult:
    """Choose the cleanest substantial extraction, augmenting only when sparse.

    The previous implementation always concatenated every strategy and appended
    paragraphs *truncated to 200 chars* — corrupting text and inflating word
    count with broken fragments. Here we pick a quality-ranked base and only
    fold in genuinely-unique full paragraphs when the base is short, so rich
    clean pages stay clean and sparse pages still gain recall.
    """
    if not results:
        return ExtractionResult(strategy="empty")
    if len(results) == 1:
        return results[0]

    ranked = sorted(
        results,
        key=lambda r: (_STRATEGY_RANK.get(r.strategy, 1), r.word_count),
        reverse=True,
    )
    # A specialized extractor (github-readme / documentation) is clean by design
    # and is the base unconditionally.
    specialized = [
        r for r in ranked
        if r.strategy in ("documentation", "github-readme") and r.word_count >= 30
    ]
    # Otherwise the base is the highest-ranked BOILERPLATE-REMOVING extractor.
    # A full-page dump (markdownify / raw_lxml) may only become the base when no
    # clean extractor produced usable output — the old `word_count >= 0.5·max`
    # rule let the full-page dump win the base slot whenever the clean extractors
    # were much shorter (i.e. did their job), collapsing precision to ~0.55 on
    # real article corpora. See _CLEAN_AUGMENT_STRATEGIES.
    clean = [r for r in ranked if r.strategy in _CLEAN_AUGMENT_STRATEGIES]
    base = specialized[0] if specialized else (clean[0] if clean else ranked[0])

    merged_text = base.text
    used = [base.strategy]
    # Augment a thin base for recall — but never augment a specialized base
    # (that would re-inject the very chrome it was built to strip), and only
    # ever pull paragraphs from other BOILERPLATE-REMOVING extractors. The
    # full-page strategies (markdownify / raw_lxml) exist solely to guarantee
    # *some* output when every clean extractor fails; folding their paragraphs
    # into a clean base re-injects nav/footer/related-article chrome and tanks
    # precision (measured: ~1 F1 point on the Zyte article corpus).
    # On news/blog pages a clean base is already the complete body, and folding
    # in another extractor's paragraphs only re-admits boilerplate — so only
    # rescue a base that clearly FAILED (tiny). Other pages keep the higher
    # floor, where augmentation genuinely lifts recall.
    augment_floor = 100 if news else _RICH_WORDS
    if base.word_count < augment_floor and base.strategy not in ("documentation", "github-readme"):
        seen = {_dedup_key(p) for p in _split_paragraphs(base.text)}
        additions: list[str] = []
        for r in results:
            if r is base or r.strategy not in _CLEAN_AUGMENT_STRATEGIES:
                continue
            contributed = False
            for p in _split_paragraphs(r.text):
                key = _dedup_key(p)
                if key and key not in seen:
                    seen.add(key)
                    additions.append(p)
                    contributed = True
            if contributed:
                used.append(r.strategy)
        if additions:
            merged_text = base.text + "\n\n" + "\n\n".join(additions)

    best_title = base.title
    for r in results:
        if r.title and len(r.title) > len(best_title):
            best_title = r.title

    unique_used = list(dict.fromkeys(used))
    strategy = unique_used[0] if len(unique_used) == 1 else "union(" + "+".join(unique_used) + ")"
    return ExtractionResult(
        text=merged_text, title=best_title,
        description=base.description, language=base.language,
        word_count=_count_words(merged_text),
        strategy=strategy,
    )


def _merge_longest(results: list[ExtractionResult]) -> ExtractionResult:
    if not results:
        return ExtractionResult(strategy="empty")
    return max(results, key=lambda r: r.word_count)


# Generic article boilerplate that survives body extraction — share/subscribe
# widgets, bylines, image credits, "N min read", legal footers. Content-agnostic
# (no per-page tuning). Applied only to PROSE pages, and only to short lines, so
# a real sentence that merely opens with a trigger word is never removed.
_BOILERPLATE_LINE = re.compile(
    r"^(?:"
    r"share (?:this|on)|follow us|read (?:more|next)|"
    r"related(?: (?:articles|stories|posts|reading))?|"
    r"advertisement|sign (?:in|up)|log ?in|subscribe|newsletter|most (?:read|popular)|more from|"
    r"image (?:caption|source)|photo(?:graph)?:|credit:|getty|reuters|associated press|"
    r"click here|tags?:|filed under|posted in|categories:|comments?|leave a (?:reply|comment)|"
    r"\d+ min(?:ute)? read|updated:|published:|by [A-Z][a-z]+ [A-Z][a-z]+\s*$|"
    r"copyright|all rights reserved|terms (?:of|&)|privacy policy|cookie"
    r")\b",
    re.IGNORECASE,
)
_MD_LINE_PREFIX = re.compile(r"^[\s>#*+\-]*")
_BOILERPLATE_MAX_WORDS = 10


def _strip_boilerplate_lines(text: str) -> str:
    """Drop short boilerplate lines (share bars, bylines, credits, legal footers).

    Only removes lines that BOTH match a boilerplate pattern AND are short — real
    body sentences that happen to start with a trigger word run long and survive.
    Measured on the Zyte article corpus: F1 0.955 → 0.963 (precision 0.947 →
    0.958), and the gain holds on a held-out test half, so it is not overfit.
    """
    kept: list[str] = []
    for line in text.split("\n"):
        probe = _MD_LINE_PREFIX.sub("", line).strip()
        short = len(probe.split()) <= _BOILERPLATE_MAX_WORDS
        if probe and short and _BOILERPLATE_LINE.match(probe):
            continue
        kept.append(line)
    return "\n".join(kept)


def _split_paragraphs(text: str) -> list[str]:
    """Full paragraphs (NOT truncated) suitable for both dedup and content."""
    paras = re.split(r"\n\s*\n", text)
    return [p.strip() for p in paras if len(p.strip()) > 30]


def _dedup_key(paragraph: str) -> str:
    """Whitespace/case-normalized prefix used only as a dedup signature."""
    return re.sub(r"\s+", " ", paragraph.lower()).strip()[:200]


def _ensure_title_heading(text: str, title: str) -> str:
    """Guarantee the markdown opens with the page title as an H1.

    Trafilatura often drops the page's <h1> from the body. SOTA scrapers
    (Firecrawl/Exa) surface it as a top-level heading — it both aids LLM
    structure and matches what callers expect from `# Title`.
    """
    if not title or not text.strip():
        return text
    body = text.lstrip()
    first = body.split("\n", 1)[0].strip()
    if first.startswith("#"):
        return text  # already has a leading heading
    title = title.strip()
    if first.lower() == title.lower():
        return "# " + body  # promote a bare title line to a heading
    return f"# {title}\n\n{body}"


# ── URL fixup ───────────────────────────────────────────────────────


def _fix_broken_urls(text: str) -> str:
    """Remove stray spaces from URL link-text inserted by html2text whitespace collapse.

    When HTML source line-wraps a long URL text node inside an <a> tag,
    html2text collapses the newline to a space, producing broken display URLs
    like ``[https://www. haringey6.ac.uk](...)``.
    """
    def _fix_link(match: re.Match[str]) -> str:
        link_text = match.group(1)
        url = match.group(2)
        if link_text.startswith(("http://", "https://")):
            link_text = link_text.replace(" ", "")
        return f"[{link_text}]({url})"

    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _fix_link, text)


# ── Table preservation ──────────────────────────────────────────────


def _has_html_tables(text: str) -> bool:
    return bool(re.search(r"<table[^>]*>", text, re.IGNORECASE))


def _convert_tables_in_text(text: str) -> str:
    table_pattern = re.compile(r"<table[^>]*>(.*?)</table>", re.IGNORECASE | re.DOTALL)

    def _replace_table(match: re.Match[str]) -> str:
        table_html = match.group(0)
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.IGNORECASE | re.DOTALL)
        if not rows:
            return match.group(0)
        md_rows: list[str] = []
        max_cols = 0
        for row_html in rows:
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.IGNORECASE | re.DOTALL)
            cell_texts = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
            max_cols = max(max_cols, len(cell_texts))
            md_rows.append("| " + " | ".join(cell_texts) + " |")
        if not md_rows:
            return match.group(0)
        header_sep = "|" + "|".join([" --- " for _ in range(max_cols)]) + "|"
        md_rows.insert(1, header_sep)
        return "\n" + "\n".join(md_rows) + "\n"

    return table_pattern.sub(_replace_table, text)


# ── Content-table recovery (trafilatura drops many tables) ──────────

_HAS_GFM_TABLE = re.compile(r"\|[-: ]+\|[-: |]*\n")
_WIKI_NOISE = re.compile(r"\[\s*edit(?:\s*\|\s*edit source)?\s*\]", re.IGNORECASE)
_BAD_TABLE_CLASS = re.compile(
    r"navbox|infobox|sidebar|metadata|toccolours|vertical|mbox|ambox", re.I
)


def _table_to_gfm(table: Any) -> str | None:
    rows = table.cssselect("tr")
    if len(rows) < 2:
        return None
    grid: list[list[str]] = []
    for row in rows:
        cells = row.cssselect("th, td")
        if not cells:
            continue
        grid.append([re.sub(r"\s+", " ", c.text_content()).strip() for c in cells])
    grid = [r for r in grid if any(r)]
    if len(grid) < 2:
        return None
    width = max(len(r) for r in grid)
    grid = [r + [""] * (width - len(r)) for r in grid]
    if width < 2:
        return None
    out = ["| " + " | ".join(grid[0]) + " |", "|" + "|".join([" --- "] * width) + "|"]
    for r in grid[1:]:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def _append_content_tables(text: str, html: str) -> str:
    """Recover real data tables that trafilatura silently dropped.

    Targets Wikipedia `.wikitable` and generic tables with a header row, skips
    layout/navbox/infobox tables, and appends any not already rendered. No-op
    when the extraction already contains GFM tables.
    """
    if _HAS_GFM_TABLE.search(text):
        return text
    try:
        tree = lxml_html.fromstring(html.encode("utf-8"))
    except Exception:
        return text

    seen: set[str] = set()
    tables: list[str] = []
    for table in tree.cssselect("table"):
        cls = table.get("class", "") or ""
        if _BAD_TABLE_CLASS.search(cls) or table.get("role") == "presentation":
            continue
        is_wikitable = "wikitable" in cls
        if not is_wikitable and not table.cssselect("th"):
            continue  # generic tables need a header row to be worth rendering
        gfm = _table_to_gfm(table)
        if not gfm:
            continue
        sig = gfm[:120]
        if sig in seen:
            continue
        seen.add(sig)
        tables.append(gfm)
        if len(tables) >= 10:
            break

    if not tables:
        return text
    return text.rstrip() + "\n\n## Tables\n\n" + "\n\n".join(tables) + "\n"


def _strip_wiki_noise(text: str) -> str:
    return _WIKI_NOISE.sub("", text)


# ── Public API ─────────────────────────────────────────────────────


def _post_process(text: str, html_content: str, title: str, news: bool) -> str:
    """Shared post-extraction cleanup. On news/blog articles the code-block and
    data-table injectors are skipped — there they only fold in embed/widget
    chrome, not body content — and a generic boilerplate-line strip runs. Other
    pages keep code/table injection and the title H1, where they ARE content.
    """
    if not news:
        text = _inject_missing_code_blocks(text, html_content)
    text = _fix_broken_urls(text)
    if _has_html_tables(text):
        text = _convert_tables_in_text(text)
    text = _strip_wiki_noise(text)
    if news:
        text = _strip_boilerplate_lines(text)
    if not news:
        text = _append_content_tables(text, html_content)
        # Prepend the <title> as an H1 only for non-news pages. On articles the
        # <title> tag carries site branding ("Headline - WSJ") and trafilatura
        # already surfaces the real in-body headline, so synthesising another H1
        # just duplicates it with branding noise (the title stays in metadata).
        text = _ensure_title_heading(text, title)
    return text


def extract_content(html_content: str, url: str = "") -> ExtractionResult:
    page_type = _detect_page_type(html_content, url)

    strategies: list[Callable[[], ExtractionResult | None]] = [
        lambda: _extract_with_trafilatura(html_content, url, page_type),
        lambda: _extract_with_readability(html_content, url, page_type),
        lambda: _extract_with_markdownify(html_content, url, page_type),
    ]
    for strategy in strategies:
        result = strategy()
        if result and result.word_count >= _MIN_ACCEPT_WORDS:
            result.text = _post_process(
                result.text, html_content, result.title, _is_news_article(html_content)
            )
            if len(result.text) > settings.extract_max_text_length:
                result.text = result.text[: settings.extract_max_text_length]
            return result

    result = _extract_raw_text(html_content)
    result.text = _fix_broken_urls(result.text)
    if len(result.text) > settings.extract_max_text_length:
        result.text = result.text[: settings.extract_max_text_length]
    return result


async def extract_content_async(html_content: str, url: str = "") -> ExtractionResult:
    if not settings.parallel_extraction_enabled:
        return extract_content(html_content, url)

    page_type = _detect_page_type(html_content, url)

    try:
        results = await _parallel_extract(html_content, url, page_type)
    except Exception:
        return extract_content(html_content, url)

    if not results:
        result = _extract_raw_text(html_content)
        if len(result.text) > settings.extract_max_text_length:
            result.text = result.text[: settings.extract_max_text_length]
        return result

    news = _is_news_article(html_content)
    if settings.extraction_merge_mode == "union":
        merged = _merge_union(results, news)
    elif settings.extraction_merge_mode == "longest":
        merged = _merge_longest(results)
    else:
        merged = results[0]

    merged.text = _post_process(merged.text, html_content, merged.title, news)

    if len(merged.text) > settings.extract_max_text_length:
        merged.text = merged.text[: settings.extract_max_text_length]

    return merged

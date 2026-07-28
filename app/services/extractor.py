from __future__ import annotations

import asyncio
import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

import structlog
import trafilatura
from lxml import html as lxml_html
from markdownify import markdownify
from trafilatura.core import determine_returnstring
from trafilatura.settings import Document, Extractor

from app.config import settings
from app.services.github import extract_github_page, is_github_url

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

logger = structlog.get_logger()

ExtractionProfile = Literal["balanced", "article_body", "adaptive", "quality"]
ADAPTIVE_ROUTER_REVISION = "adaptive-v1"


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
    confidence: float = 0.0
    page_type: str = ""
    truncated: bool = False
    truncation_reason: str = ""


def _count_words(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))


def _log_host(url: str) -> str:
    """Return only a bounded hostname for telemetry.

    Extraction URLs routinely contain signed query strings. They are useful
    inputs but must never become log fields or exception text.
    """
    try:
        return (urlsplit(url).hostname or "unknown")[:253]
    except ValueError:
        return "invalid"


# Minimum words for a strategy's output to be considered usable. Kept low so a
# short-but-complete page (e.g. example.com) yields its real clean text instead
# of falling through to the CSS-leaking raw_lxml last resort. The escalate-to-JS
# decision is made separately (crawler.SPARSE_WORD_FLOOR), not here.
_MIN_ACCEPT_WORDS = 8

# Strategy trust order for choosing the union base. Specialized + trafilatura
# produce the cleanest markdown; markdownify captures the most text but also the
# most boilerplate, so it loses ties.
_STRATEGY_RANK = {
    "rs-trafilatura": 6,
    "documentation": 5,
    "github-readme": 5,
    "github-thread": 5,
    "github-tree": 5,
    "github-commit": 5,
    "github-commit-partial": 5,
    "github-compare": 5,
    "github-compare-partial": 5,
    "github-release": 5,
    "trafilatura": 4,
    "readability": 3,
    "markdownify": 2,
    "raw_lxml": 0,
}

_native_import_warning_emitted = False
_extraction_semaphore: asyncio.Semaphore | None = None


def _get_extraction_semaphore() -> asyncio.Semaphore:
    """Bound page-level CPU work independently from network concurrency."""
    global _extraction_semaphore
    if _extraction_semaphore is None:
        _extraction_semaphore = asyncio.Semaphore(max(1, settings.max_concurrent_extractions))
    return _extraction_semaphore


def native_backend_version() -> str:
    """Return the native backend version, or ``unavailable`` for diagnostics."""
    try:
        from clusy_native import backend_version

        return backend_version()
    except (ImportError, RuntimeError):
        return "unavailable"


# Above this word count the base extraction is "rich" enough that augmenting it
# with other strategies' paragraphs adds more noise than signal, so we skip it.
_RICH_WORDS = 400

# Strategies whose paragraphs are safe to fold into a clean base for recall.
# These are boilerplate-REMOVING extractors; the full-page dumps (markdownify,
# raw_lxml) are deliberately excluded — see _merge_union.
_CLEAN_AUGMENT_STRATEGIES = frozenset(
    {
        "trafilatura",
        "readability",
        "documentation",
        "github-readme",
        "github-thread",
        "github-tree",
        "github-commit",
        "github-commit-partial",
        "github-compare",
        "github-compare-partial",
        "github-release",
    }
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
        "arxiv.org/abs/",
        "arxiv.org/pdf/",
        "papers.",
        "proceedings.",
        "doi.org/10.",
        "/paper/",
        "pubmed",
        "scholar",
    )
    latex_pattern = re.search(r"\\begin\{|\\cite\{|\\ref\{|\\usepackage", lower)
    if any(k in lower_url for k in academic_urls) or latex_pattern:
        return "academic"

    if is_github_url(url) or any(k in lower_url for k in ("gitlab.com", "bitbucket.org")):
        return "repository"

    if any(k in lower_url for k in ("docs.", "/docs/", "readthedocs", "documentation")):
        return "documentation"

    article_indicators = (
        "<article",
        'role="article"',
        'class="post',
        'class="entry',
        'class="blog',
        "blog-post",
        "post-content",
    )
    if any(k in lower for k in article_indicators):
        return "article"

    if any(k in lower for k in ("forum", "thread", "discussion", "topic", "board")):
        return "forum"

    if re.search(r"\$\d+\.\d{2}|price|add.to.cart|buy.now|sku", lower):
        return "product"

    listing_indicators = ("search result", "items found", "sort by", "filter by")
    result_count = lower.count('class="result') + lower.count('class="item')
    if any(k in lower for k in listing_indicators) or result_count >= 3:
        return "listing"

    if re.search(r"gallery|grid|masonry|card", lower) and lower.count("<img") >= 3:
        return "collection"

    return "webpage"


# ── Individual strategies ──────────────────────────────────────────


def _extract_with_native(
    html_content: str,
    url: str,
    extraction_profile: ExtractionProfile = "balanced",
) -> ExtractionResult | None:
    """Run the pinned native primary extractor.

    Import failure is deliberately non-fatal so source checkouts that have not
    built the extension still retain the complete Python fallback pipeline.
    Production images build the extension and expose its version in health and
    benchmark metadata.
    """
    global _native_import_warning_emitted

    if not settings.native_extraction_enabled:
        return None
    try:
        from clusy_native import extract_html
    except ImportError as error:
        if not _native_import_warning_emitted:
            logger.warning(
                "native_extractor_unavailable",
                failure_type=type(error).__name__,
            )
            _native_import_warning_emitted = True
        return None

    try:
        native = extract_html(
            html_content,
            url,
            extraction_profile == "article_body",
        )
    except (RuntimeError, ValueError) as error:
        logger.warning(
            "native_extraction_failed",
            host=_log_host(url),
            failure_type=type(error).__name__,
        )
        return None

    # General main-content Markdown and article-body extraction are distinct
    # evaluation targets. Keep the heterogeneous broad backend as the default;
    # callers that explicitly request article_body receive the separately
    # pinned precision-oriented candidate when one is available.
    article_selected = extraction_profile == "article_body" and bool(native.article_text)
    text = (native.article_text if article_selected else native.plain_text).strip()
    if not text:
        return None
    return ExtractionResult(
        text=text,
        title=native.title,
        description=native.description,
        language=native.language,
        word_count=_count_words(text),
        strategy="rs-trafilatura",
        confidence=max(0.0, min(float(native.confidence), 1.0)),
        page_type="article" if article_selected else native.page_type,
    )


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
    # Use the Document returned by bare_extraction for both content and
    # metadata. Calling extract() followed by bare_extraction() parsed every
    # page twice and accounted for almost half of extraction CPU in profiling.
    options = Extractor(
        output_format="markdown",
        images=False,
        links=True,
        comments=False,
        tables=True,
        precision=news or page_type == "academic",
        url=url,
    )
    document = trafilatura.bare_extraction(
        html_content,
        options=options,
        as_dict=False,
    )
    if not isinstance(document, Document):
        return None
    text = determine_returnstring(document, options)
    if not text.strip():
        return None
    return ExtractionResult(
        text=text,
        title=document.title or "",
        description=document.description or "",
        language=document.language or "",
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
    """Adapt the bounded, route-aware GitHub extractor to the common result."""
    extracted = extract_github_page(html_content, url)
    if extracted is None:
        return None
    return ExtractionResult(
        text=extracted.text,
        title=extracted.title,
        language=extracted.language,
        word_count=_count_words(extracted.text),
        strategy=extracted.strategy,
        page_type="repository",
        truncated=getattr(extracted, "truncated", False),
        truncation_reason=getattr(extracted, "truncation_reason", ""),
    )


# ── Documentation-specific extraction ───────────────────────────────

_DOC_NOISE_SELECTORS = [
    # Navigation elements (safest to remove)
    ".wy-nav-side",
    ".wy-nav-top",
    ".wy-side-nav-search",
    ".wy-menu-vertical",
    ".sphinxsidebar",
    ".sphinxsidebarwrapper",
    ".md-sidebar--primary",
    ".md-sidebar--secondary",
    ".md-header",
    ".md-footer",
    ".md-tabs",
    ".rst-versions",
    ".documentation-breadcrumbs",
    '[role="navigation"]',
    '[role="search"]',
    # Action buttons
    ".headerlink",
    ".viewcode-link",
    ".edit-this-page",
    ".theme-switcher",
    ".md-source",
    ".md-top",
    ".md-version",
    ".feedback",
    # Non-content
    "script",
    "style",
    "noscript",
    "iframe",
    "template",
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
        'div[role="main"]',
        "main",
        "article",
        ".document",
        ".rst-content",
        ".md-content",
        ".markdown-body",
        ".content",
        "#content",
        ".documentation",
        ".doc-content",
        ".wy-nav-content",
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

    # Convert the already-isolated, noise-pruned content subtree once. The old
    # walker appended each container's full ``text_content()`` and then walked
    # its children, repeating deeply nested documentation paragraphs up to six
    # times and tripling output size.
    inner = lxml_html.tostring(main, encoding="unicode")
    text = _html_to_markdown(inner).strip()
    word_count = _count_words(text)
    if word_count < _MIN_ACCEPT_WORDS:
        return _extract_with_trafilatura(html_content, url, "documentation")

    return ExtractionResult(
        text=text,
        title=title,
        description=title[:300] if title else "",
        word_count=word_count,
        strategy="documentation",
    )


# ── Parallel multi-strategy execution ───────────────────────────────


async def _parallel_extract(html_content: str, url: str, page_type: str) -> list[ExtractionResult]:
    loop = asyncio.get_running_loop()

    workers = [
        loop.run_in_executor(None, _extract_with_trafilatura, html_content, url, page_type),
        loop.run_in_executor(None, _extract_with_readability, html_content, url, page_type),
        loop.run_in_executor(None, _extract_with_markdownify, html_content, url, page_type),
    ]

    if page_type == "documentation":
        workers.append(loop.run_in_executor(None, _extract_documentation, html_content, url))
    if page_type == "repository":
        workers.append(loop.run_in_executor(None, _extract_github, html_content, url))

    try:
        results = await asyncio.gather(
            *(asyncio.shield(worker) for worker in workers),
            return_exceptions=True,
        )
    except asyncio.CancelledError:
        # Executor futures cannot be force-cancelled once their threads start.
        # Hold the caller's extraction permit until every real worker exits so
        # cancellation floods cannot exceed the configured CPU concurrency.
        await asyncio.gather(
            *(asyncio.shield(worker) for worker in workers),
            return_exceptions=True,
        )
        raise
    out: list[ExtractionResult] = []
    for r in results:
        if isinstance(r, ExtractionResult) and r.word_count >= _MIN_ACCEPT_WORDS:
            out.append(r)
    return out


async def _await_worker_holding_permit(
    semaphore: asyncio.Semaphore,
    awaitable_factory: Callable[[], Coroutine[Any, Any, Any]],
) -> Any:
    """Let cancellation return promptly while real work retains its permit."""
    await semaphore.acquire()
    try:
        worker = asyncio.create_task(awaitable_factory())
    except BaseException:
        semaphore.release()
        raise

    def release_when_worker_finishes(done: asyncio.Future[Any]) -> None:
        if not done.cancelled():
            done.exception()
        semaphore.release()

    worker.add_done_callback(release_when_worker_finishes)
    return await asyncio.shield(worker)


async def _to_thread_holding_cancellation(
    function: Any,
    *args: Any,
    semaphore: asyncio.Semaphore | None = None,
) -> Any:
    permit = semaphore or _get_extraction_semaphore()
    return await _await_worker_holding_permit(
        permit,
        lambda: asyncio.to_thread(function, *args),
    )


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
    # A specialized extractor (GitHub content / documentation) is clean by design
    # and is the base unconditionally.
    specialized = [
        r
        for r in ranked
        if (r.strategy == "documentation" or r.strategy.startswith("github-"))
        and r.word_count >= 30
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
    if (
        base.word_count < augment_floor
        and base.strategy != "documentation"
        and not base.strategy.startswith("github-")
    ):
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
        text=merged_text,
        title=best_title,
        description=base.description,
        language=base.language,
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


def _post_process_native(text: str) -> str:
    """Keep the independently benchmarked native body byte-for-byte stable."""
    return text


def _truncate_at_boundary(text: str) -> str:
    """Apply the output cap without cutting through most of a paragraph."""
    limit = settings.extract_max_text_length
    if len(text) <= limit:
        return text
    marker = "\n\n[content truncated at configured limit]"
    if limit <= len(marker):
        return marker[:limit]
    content_limit = max(0, limit - len(marker))
    boundary = text.rfind(
        "\n\n",
        max(0, content_limit - 10_000),
        content_limit,
    )
    if boundary < limit // 2:
        boundary = content_limit
    return text[:boundary].rstrip() + marker


def _finalize_result(
    result: ExtractionResult,
    html_content: str,
    news: bool,
) -> ExtractionResult:
    if result.strategy == "rs-trafilatura":
        result.text = _post_process_native(result.text)
    elif result.strategy.startswith("github-"):
        # GitHub's helper has already isolated and sanitized the content
        # subtree. Re-scanning the full page here would re-inject navigation
        # code snippets, reaction widgets, and timeline tables.
        result.text = _fix_broken_urls(result.text)
    else:
        result.text = _post_process(
            result.text,
            html_content,
            result.title,
            news,
        )
    before_truncation = result.text
    result.text = _truncate_at_boundary(before_truncation)
    if result.text != before_truncation:
        result.truncated = True
        result.truncation_reason = "configured text limit"
    # Post-processing can add a title/tables/code or remove boilerplate. Report
    # the final output count rather than the pre-processing strategy count.
    result.word_count = _count_words(result.text)
    return result


def _native_is_confident(result: ExtractionResult | None) -> bool:
    return bool(
        result
        and result.word_count >= _MIN_ACCEPT_WORDS
        # The explicit article-body backend has no independently calibrated
        # confidence, while the broad backend's article-class output held up
        # strongly on WCXB. Keep both article paths out of the generic
        # confidence fallback gate.
        and (
            result.page_type == "article"
            or result.confidence >= settings.native_extraction_min_confidence
        )
    )


@dataclass(frozen=True, slots=True)
class AdaptiveRiskDecision:
    risky: bool
    structural_score: int
    reasons: tuple[str, ...]


_TABLE_TAG = re.compile(r"<table\b", re.IGNORECASE)
_TABLE_SPAN = re.compile(r"\b(?:rowspan|colspan)\s*=", re.IGNORECASE)
_PRE_TAG = re.compile(r"<pre\b", re.IGNORECASE)
_CODE_TAG = re.compile(r"<code\b", re.IGNORECASE)
_MATH_MARKUP = re.compile(
    r"<math\b|class\s*=\s*[\"'][^\"']*(?:katex|mathjax)|"
    r"data-(?:mathml|latex)\s*=",
    re.IGNORECASE,
)
_LIST_ITEM_TAG = re.compile(r"<li\b", re.IGNORECASE)
_LINK_TAG = re.compile(r"<a\b", re.IGNORECASE)
_QUALITY_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_CJK_CHARACTER = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    r"\u3040-\u30ff\uac00-\ud7af]"
)
_UNSAFE_QUALITY_HTML = re.compile(
    r"<\s*(?:script|style|iframe|object|embed)\b",
    re.IGNORECASE,
)


def _bounded_match_count(pattern: re.Pattern[str], value: str, limit: int) -> int:
    count = 0
    for _match in pattern.finditer(value):
        count += 1
        if count >= limit:
            break
    return count


def _adaptive_risk_decision(
    candidate: ExtractionResult,
    html_content: str,
) -> AdaptiveRiskDecision:
    """Classify whether an adaptive request merits optional model assistance.

    The router uses only bounded production inputs: the deterministic candidate's
    own confidence/type and coarse structural complexity in a prefix of the HTML.
    It never consumes expected output, corpus metadata, or caller annotations.
    """
    scan = html_content[: settings.adaptive_extraction_max_scan_chars]
    structural_score = 0

    table_count = _bounded_match_count(_TABLE_TAG, scan, 3)
    structural_score += min(table_count, 2)
    if table_count and _TABLE_SPAN.search(scan):
        structural_score += 1

    pre_count = _bounded_match_count(_PRE_TAG, scan, 2)
    code_count = _bounded_match_count(_CODE_TAG, scan, 4)
    if pre_count >= 2 or code_count >= 4:
        structural_score += 1
    if _MATH_MARKUP.search(scan):
        structural_score += 1
    if _bounded_match_count(_LIST_ITEM_TAG, scan, 20) >= 20:
        structural_score += 1
    if _bounded_match_count(_LINK_TAG, scan, 80) >= 80:
        structural_score += 1

    reasons: list[str] = []
    if candidate.confidence < settings.adaptive_extraction_min_confidence:
        reasons.append("low_confidence")
    risky_page_types = {
        value.strip()
        for value in settings.adaptive_extraction_risky_page_types.split(",")
        if value.strip()
    }
    if candidate.page_type in risky_page_types:
        reasons.append("risky_page_type")
    if structural_score >= settings.adaptive_extraction_structural_score_threshold:
        reasons.append("structural_complexity")

    return AdaptiveRiskDecision(
        risky=bool(reasons),
        structural_score=structural_score,
        reasons=tuple(reasons),
    )


def _quality_tokens(value: str) -> list[str]:
    tokens = [
        token
        for token in (match.group(0).casefold() for match in _QUALITY_TOKEN.finditer(value))
        if len(token) > 1 or token.isdigit()
    ]
    # CJK prose does not use whitespace word boundaries. Character units keep
    # grounding and minimum-content checks language-neutral without a tokenizer
    # dependency; the full runs above still preserve stronger exact matches.
    tokens.extend(_CJK_CHARACTER.findall(value.casefold()))
    return tokens


def _quality_content_units(value: str) -> int:
    cjk_characters = len(_CJK_CHARACTER.findall(value))
    return max(_count_words(value), cjk_characters // 2)


def _quality_source_text(html_content: str) -> str:
    """Visible source text plus link/image attributes used by Markdown output."""
    try:
        root = lxml_html.fromstring(html_content)
    except Exception:
        return ""
    for node in root.xpath("//script|//style|//noscript|//template"):
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)
    attributes = root.xpath("//@href | //@src | //@alt | //@title")
    return " ".join([root.text_content(), *(str(value) for value in attributes)])


def _quality_source_metadata(html_content: str) -> tuple[str, str, str]:
    """Recover cheap document metadata when quality runs before a fast path."""
    try:
        root = lxml_html.fromstring(html_content)
    except Exception:
        return "", "", ""

    title_values = root.xpath(
        "//meta[translate(@property,'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
        "'abcdefghijklmnopqrstuvwxyz')='og:title']/@content | //title/text()"
    )
    description_values = root.xpath(
        "//meta[translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
        "'abcdefghijklmnopqrstuvwxyz')='description']/@content | "
        "//meta[translate(@property,'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
        "'abcdefghijklmnopqrstuvwxyz')='og:description']/@content"
    )
    language_values = root.xpath("//html/@lang")
    title = str(title_values[0]).strip() if title_values else ""
    description = str(description_values[0]).strip() if description_values else ""
    language = str(language_values[0]).strip() if language_values else ""
    return title, description, language


def _quality_rejection_reason(
    quality_text: str,
    html_content: str,
    deterministic: ExtractionResult | None,
) -> str | None:
    """Fail closed before model-assisted Markdown replaces stable output.

    MinerU should only select nodes from the supplied document. Multiset token
    grounding therefore catches hallucinated text *and* repetition inflation,
    while the remaining checks reject truncated or malformed serialization.
    """
    quality_words = _quality_content_units(quality_text)
    minimum_words = _MIN_ACCEPT_WORDS
    if deterministic is not None:
        if (
            deterministic.page_type in {"article", "documentation"}
            and deterministic.confidence >= 0.75
            and deterministic.word_count >= 80
        ):
            # A trusted prose/document candidate must not be replaced by a
            # grounded but incomplete excerpt. No analogous ratio is imposed
            # on noisy listing/product candidates, where aggressive cleanup is
            # the point of escalation.
            minimum_words = max(minimum_words, deterministic.word_count // 2)
        elif deterministic.word_count >= 100:
            minimum_words = min(
                24,
                max(minimum_words, deterministic.word_count // 20),
            )
    if quality_words < minimum_words:
        return "insufficient_content"
    if "\x00" in quality_text or _UNSAFE_QUALITY_HTML.search(quality_text):
        return "unsafe_structure"
    if quality_text.count("```") % 2:
        return "unbalanced_code_fence"

    blocks = [
        re.sub(r"\s+", " ", block).strip().casefold()
        for block in re.split(r"\n\s*\n", quality_text)
        if _quality_content_units(block) >= 4
    ]
    if len(blocks) >= 3:
        duplicate_blocks = sum(count - 1 for count in Counter(blocks).values())
        if duplicate_blocks / len(blocks) > 0.25:
            return "duplicate_content"

    quality_tokens = _quality_tokens(quality_text)
    source_tokens = _quality_tokens(_quality_source_text(html_content))
    if not quality_tokens or not source_tokens:
        return "ungrounded_content"
    quality_counts = Counter(quality_tokens)
    source_counts = Counter(source_tokens)
    grounded = sum((quality_counts & source_counts).values())
    if grounded / len(quality_tokens) < 0.80:
        return "ungrounded_content"
    return None


def _python_cascade(
    html_content: str,
    url: str,
    page_type: str,
) -> ExtractionResult:
    strategies: list[Callable[[], ExtractionResult | None]] = [
        lambda: _extract_with_trafilatura(html_content, url, page_type),
        lambda: _extract_with_readability(html_content, url, page_type),
        lambda: _extract_with_markdownify(html_content, url, page_type),
    ]
    for strategy in strategies:
        result = strategy()
        if result and result.word_count >= _MIN_ACCEPT_WORDS:
            return result
    return _extract_raw_text(html_content)


def extract_content(
    html_content: str,
    url: str = "",
    extraction_profile: ExtractionProfile = "balanced",
) -> ExtractionResult:
    page_type = _detect_page_type(html_content, url)
    news = _is_news_article(html_content)
    # The optional MinerU path is asynchronous. Synchronous callers retain the
    # deterministic balanced behavior and never fail because the optional
    # dependency or remote inference service is absent.
    deterministic_profile: ExtractionProfile = (
        "balanced"
        if extraction_profile in {"adaptive", "quality"}
        else extraction_profile
    )

    # GitHub's server-rendered README subtree is an exceptionally strong,
    # deterministic signal and avoids repository chrome.
    if page_type == "repository":
        specialized = _extract_github(html_content, url)
        if specialized is not None:
            return _finalize_result(specialized, html_content, news)

    native = _extract_with_native(html_content, url, deterministic_profile)
    if _native_is_confident(native):
        assert native is not None
        return _finalize_result(native, html_content, news)

    fallback = _python_cascade(html_content, url, page_type)
    # Preserve a substantial native result unless its own quality predictor is
    # very low or the clean fallback recovered materially more content.
    if (
        native is not None
        and native.word_count >= _MIN_ACCEPT_WORDS
        and native.confidence >= 0.35
        and fallback.word_count < native.word_count * 1.25
    ):
        return _finalize_result(native, html_content, news)
    return _finalize_result(fallback, html_content, news)


async def _try_quality_result(
    html_content: str,
    url: str,
    page_type: str,
    deterministic: ExtractionResult | None = None,
) -> ExtractionResult | None:
    try:
        from app.services.quality_extractor import extract_quality_content

        quality = await extract_quality_content(html_content, url)
    except Exception as error:
        # This is a final containment boundary around an optional path.
        # Error messages and URLs are deliberately excluded from telemetry.
        logger.warning(
            "quality_extraction_fallback",
            reason="integration_failed",
            failure_type=type(error).__name__,
        )
        return None
    if quality is None:
        return None
    rejection_reason = _quality_rejection_reason(
        quality.text,
        html_content,
        deterministic,
    )
    if rejection_reason is not None:
        logger.warning(
            "quality_extraction_fallback",
            reason="verification_failed",
            verification=rejection_reason,
        )
        return None
    if deterministic is None:
        title, description, language = _quality_source_metadata(html_content)
    else:
        title = deterministic.title
        description = deterministic.description
        language = deterministic.language
    return ExtractionResult(
        text=quality.text,
        title=title,
        description=description,
        language=language,
        word_count=_count_words(quality.text),
        strategy=quality.strategy,
        # The deterministic confidence calibrates a different body and must not
        # be presented as a score for model-assisted output.
        confidence=0.0,
        page_type=(
            deterministic.page_type
            if deterministic is not None and deterministic.page_type
            else page_type
        ),
    )


async def _extract_deterministic_after_specialized(
    html_content: str,
    url: str,
    extraction_profile: ExtractionProfile,
    *,
    page_type: str,
    news: bool,
    semaphore: asyncio.Semaphore,
) -> ExtractionResult:
    native = await _to_thread_holding_cancellation(
        _extract_with_native,
        html_content,
        url,
        extraction_profile,
        semaphore=semaphore,
    )
    if _native_is_confident(native):
        assert native is not None
        return _finalize_result(native, html_content, news)

    if not settings.parallel_extraction_enabled:
        fallback = await _to_thread_holding_cancellation(
            _python_cascade,
            html_content,
            url,
            page_type,
            semaphore=semaphore,
        )
        if (
            native is not None
            and native.word_count >= _MIN_ACCEPT_WORDS
            and native.confidence >= 0.35
            and fallback.word_count < native.word_count * 1.25
        ):
            return _finalize_result(native, html_content, news)
        return _finalize_result(fallback, html_content, news)

    try:
        results = await _await_worker_holding_permit(
            semaphore,
            lambda: _parallel_extract(html_content, url, page_type),
        )
    except Exception:
        fallback = await _to_thread_holding_cancellation(
            _python_cascade,
            html_content,
            url,
            page_type,
            semaphore=semaphore,
        )
        return _finalize_result(fallback, html_content, news)

    if not results:
        result = native or _extract_raw_text(html_content)
        return _finalize_result(result, html_content, news)

    if settings.extraction_merge_mode == "union":
        merged = _merge_union(results, news)
    elif settings.extraction_merge_mode == "longest":
        merged = _merge_longest(results)
    else:
        merged = results[0]

    if (
        native is not None
        and native.word_count >= _MIN_ACCEPT_WORDS
        and native.confidence >= 0.35
        and merged.word_count < native.word_count * 1.25
    ):
        return _finalize_result(native, html_content, news)
    return _finalize_result(merged, html_content, news)


async def extract_content_async(
    html_content: str,
    url: str = "",
    extraction_profile: ExtractionProfile = "balanced",
) -> ExtractionResult:
    page_type = _detect_page_type(html_content, url)
    news = _is_news_article(html_content)
    semaphore = _get_extraction_semaphore()

    # GitHub's server-rendered route-specific subtrees are both cleaner and
    # cheaper than model inference. Fall through to generic extraction only
    # when the deterministic GitHub adapter has no usable content.
    if page_type == "repository":
        specialized = await _to_thread_holding_cancellation(
            _extract_github,
            html_content,
            url,
            semaphore=semaphore,
        )
        if specialized is not None:
            return _finalize_result(specialized, html_content, news)

    if extraction_profile == "quality":
        # Quality inference must compete against a complete deterministic
        # candidate. Running model-first left the verifier with only its
        # eight-unit absolute floor, so a grounded excerpt could replace a
        # long page. The baseline is also the exact no-model fallback and
        # supplies metadata plus a page-type-aware completeness threshold.
        deterministic = await _extract_deterministic_after_specialized(
            html_content,
            url,
            "balanced",
            page_type=page_type,
            news=news,
            semaphore=semaphore,
        )
        quality_result = await _try_quality_result(
            html_content,
            url,
            page_type,
            deterministic,
        )
        return quality_result if quality_result is not None else deterministic

    if extraction_profile == "adaptive":
        deterministic = await _extract_deterministic_after_specialized(
            html_content,
            url,
            "balanced",
            page_type=page_type,
            news=news,
            semaphore=semaphore,
        )
        decision = _adaptive_risk_decision(deterministic, html_content)
        if not decision.risky:
            return deterministic
        logger.info(
            "adaptive_extraction_escalating",
            reasons="+".join(decision.reasons),
            structural_score=decision.structural_score,
            page_type=deterministic.page_type or page_type,
            confidence=round(deterministic.confidence, 3),
        )
        quality_result = await _try_quality_result(
            html_content,
            url,
            page_type,
            deterministic,
        )
        return quality_result if quality_result is not None else deterministic

    return await _extract_deterministic_after_specialized(
        html_content,
        url,
        extraction_profile,
        page_type=page_type,
        news=news,
        semaphore=semaphore,
    )

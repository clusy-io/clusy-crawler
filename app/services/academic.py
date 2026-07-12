from __future__ import annotations

import re
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger()

# Patterns for academic structure detection
ABSTRACT_HEADERS = re.compile(
    r"^(?:abstract|summary|synopsis)\b",
    re.IGNORECASE | re.MULTILINE,
)
SECTION_HEADER = re.compile(
    r"^(?:\d+\.?\s+|[IVX]+\.\s+)?(?:introduction|background|method|"
    r"experiment|result|discussion|conclusion|related.work|"
    r"future.work|acknowledgment|appendix|supplement|"
    r"implementation|evaluation|analysis|approach|overview|"
    r"preliminar|motivation|contribution|problem|solution|"
    r"literature.review|survey|case.study|framework|architecture|"
    r"design|setup|limitation|comparison|ablation)",
    re.IGNORECASE | re.MULTILINE,
)
REFERENCE_HEADER = re.compile(
    r"^(?:references?|bibliography|works.cited|citations?)\s*$",
    re.IGNORECASE,
)

CITATION_PATTERN = re.compile(
    r"\[(\d+(?:[,-]\d+)*)\]|"
    r"\(([A-Z][a-z]+(?:\s+(?:et\s+al\.?|and\s+[A-Z][a-z]+))?,\s*\d{4}[a-z]?)\)|"
    r"\\cite\{([^}]+)\}",
)


@dataclass
class AcademicPaper:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    abstract: str = ""
    sections: list[Section] = field(default_factory=list)
    references_raw: str = ""
    full_text: str = ""
    word_count: int = 0
    language: str = ""
    doi: str = ""

    def to_markdown(self) -> str:
        parts: list[str] = []
        if self.title:
            parts.append(f"# {self.title}\n")
        if self.authors:
            parts.append(f"**Authors**: {', '.join(self.authors)}\n")
        if self.abstract:
            parts.append(f"## Abstract\n\n{self.abstract}\n")
        for sec in self.sections:
            parts.append(sec.to_markdown())
        if self.references_raw:
            parts.append(f"## References\n\n{self.references_raw}\n")
        return "\n".join(parts)


@dataclass
class Section:
    heading: str = ""
    level: int = 1
    content: str = ""

    def to_markdown(self) -> str:
        prefix = "#" * min(self.level + 1, 4)
        return f"{prefix} {self.heading}\n\n{self.content}\n"


def extract_pdf(contents: bytes, url: str = "") -> AcademicPaper:
    """Extract structured content from PDF bytes using pypdfium2.

    pypdfium2 (BSD-3-Clause, on Google's PDFium) replaced PyMuPDF/`fitz`, which
    is AGPL-3.0 and incompatible with a permissive OSS release.
    """
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(contents)
    paper = AcademicPaper()

    try:
        full_parts: list[str] = []
        metadata = doc.get_metadata_dict() or {}
        paper.title = metadata.get("Title", "") or ""
        paper.doi = _find_doi(str(metadata))

        for page_num in range(min(len(doc), 200)):
            page = doc[page_num]
            textpage = page.get_textpage()
            try:
                text = textpage.get_text_range()
            finally:
                textpage.close()
                page.close()
            if text:
                full_parts.append(text)

        paper.full_text = "\n".join(full_parts)

        if not paper.title and paper.full_text:
            paper.title = _extract_title_from_text(paper.full_text)

        paper.authors = _extract_authors(paper.full_text)
        paper.abstract = _extract_abstract(paper.full_text)
        paper.sections = _segment_sections(paper.full_text)
        paper.references_raw = _extract_references(paper.full_text)
        paper.word_count = len(paper.full_text.split())
    finally:
        doc.close()

    return paper


def extract_long_html(html: str, url: str = "") -> AcademicPaper:
    """Extract academic structure from HTML (arXiv, ACM, etc.)."""
    from lxml import html as lxml_html

    paper = AcademicPaper()

    try:
        tree = lxml_html.fromstring(html.encode("utf-8"))
    except Exception:
        # Try removing non-HTML content
        clean = re.sub(r"<[^>]+>", " ", html)
        paper.full_text = re.sub(r"\s+", " ", clean).strip()
        paper.word_count = len(paper.full_text.split())
        return paper

    # Title extraction
    title_tags = tree.xpath(
        "//meta[@name='citation_title']/@content"
        "| //meta[@property='og:title']/@content"
        "| //title/text()"
        "| //h1/text()"
    )
    if title_tags:
        paper.title = str(title_tags[0]).strip()

    # Authors
    author_tags = tree.xpath(
        "//meta[@name='citation_author']/@content"
        "| //meta[@name='dc.creator']/@content"
    )
    paper.authors = [str(a).strip() for a in author_tags if a]

    # DOI
    doi_tags = tree.xpath(
        "//meta[@name='citation_doi']/@content"
        "| //meta[@name='dc.identifier']/@content[contains(.,'10.')]"
    )
    if doi_tags:
        paper.doi = str(doi_tags[0]).strip()

    # Full text from body — include tail text and block element content
    body = tree.xpath("//body") or tree.xpath("//article") or [tree]
    text_parts: list[str] = []
    block_tags = {"div", "p", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6",
                  "li", "td", "th", "dt", "dd", "pre", "blockquote", "br", "hr"}
    for el in body[0].iter():
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag in ("script", "style", "noscript", "template", "nav", "header", "footer"):
            continue
        if tag in block_tags:
            # Use text_content for block elements (gets all nested text)
            txt = el.text_content().strip()
            if txt and len(txt) > 1:
                text_parts.append(txt)
        elif el.text:
            text_parts.append(el.text.strip())
        if el.tail:
            text_parts.append(el.tail.strip())

    paper.full_text = "\n".join(t for t in text_parts if t)
    paper.word_count = len(paper.full_text.split())

    # Try PubMed-specific abstract extraction first
    abstract_meta = tree.xpath(
        "//meta[@name='description']/@content"
        "| //meta[@name='citation_abstract']/@content"
        "| //div[contains(@class,'abstract')]//text()"
        "| //*[@id='abstract']//text()"
        "| //*[contains(@class,'abstract-content')]//text()"
    )
    if abstract_meta:
        paper.abstract = " ".join(str(a).strip() for a in abstract_meta if str(a).strip())[:5000]

    if not paper.abstract:
        paper.abstract = _extract_abstract(paper.full_text)
    paper.sections = _segment_sections(paper.full_text)
    paper.references_raw = _extract_references(paper.full_text)

    return paper


def _find_doi(metadata_str: str) -> str:
    """Extract DOI from metadata string."""
    m = re.search(r"10\.\d{4,}/[^\s\"']+", metadata_str)
    return m.group(0) if m else ""


def _extract_title_from_text(text: str) -> str:
    """Heuristic title extraction from first non-empty lines."""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines:
        return ""
    # Title is typically the first line before a blank line or author line
    for line in lines[:10]:
        if len(line.split()) >= 3 and len(line) < 200:
            return line
    return lines[0][:200] if lines else ""


def _extract_authors(text: str) -> list[str]:
    """Extract author names from text near the beginning."""
    first_5000 = text[:5000]
    lines = first_5000.split("\n")

    author_line = ""
    for i, line in enumerate(lines[:30]):
        lower = line.lower().strip()
        # Author sections typically contain university domains, commas, "and"
        if any(
            kw in lower
            for kw in ("university", "institute", "laboratory", "@", "college")
        ):
            author_line = (
                lines[i - 1]
                if i > 0 and not _looks_like_title(lines[i - 1])
                else line
            )
            break

    if not author_line:
        # Fallback: look for line with many commas (author lists)
        for line in lines[:15]:
            if line.count(",") >= 2 and len(line) < 300:
                author_line = line
                break

    # Split on "," and "and", clean
    parts = re.split(r",|\band\b", author_line)
    authors = []
    for p in parts:
        p = p.strip()
        # Filter out obviously non-name text
        if (
            len(p.split()) >= 2
            and len(p) < 80
            and not any(
                kw in p.lower()
                for kw in ("university", "institute", "department", "abstract", "email")
            )
        ):
            authors.append(p)
    return authors[:20]


def _extract_abstract(text: str) -> str:
    """Extract abstract from paper text."""
    # Find "Abstract" header
    m = ABSTRACT_HEADERS.search(text)
    if not m:
        # Fallback: first substantial paragraph after title
        paragraphs = re.split(r"\n\s*\n", text[:8000])
        for p in paragraphs[1:6]:
            p = p.strip()
            if 100 < len(p) < 3000 and not _looks_like_authors(p):
                return p[:3000]
        return ""

    start = m.end()
    # Abstract ends at next section header or blank line after substantial content
    remaining = text[start : start + 5000]
    # Find the end: next section header or reference header
    end_match = SECTION_HEADER.search(remaining) or REFERENCE_HEADER.search(remaining)
    abstract = (
        remaining[: end_match.start()].strip() if end_match else remaining[:3000].strip()
    )

    return abstract


def _segment_sections(text: str) -> list[Section]:
    """Segment text into academic sections."""
    sections: list[Section] = []
    lines = text.split("\n")
    current = Section(heading="Body", level=1, content="")
    in_references = False

    body_started = False
    abstract_end = 0

    # Skip past abstract
    m = ABSTRACT_HEADERS.search(text)
    if m:
        abstract_end = m.end() + 1000  # rough

    # Track byte position incrementally — recomputing sum(lines[:i]) each
    # iteration is O(n²) and made large pages take tens of seconds.
    line_pos = 0
    for line in lines:
        pos = line_pos
        line_pos += len(line) + 1

        if pos < abstract_end:
            continue

        stripped = line.strip()

        if not stripped:
            continue

        # Detect reference section
        if REFERENCE_HEADER.match(stripped):
            in_references = True
            if current.content.strip():
                sections.append(current)
            current = Section(heading="References", level=1, content="")
            continue

        if in_references:
            current.content += stripped + "\n"
            continue

        # Detect section headers
        sec_match = SECTION_HEADER.match(stripped)
        if sec_match and len(stripped) < 150 and body_started:
            if current.content.strip():
                sections.append(current)
            # Determine level from numbering
            level = 1
            if stripped.startswith(("  ", "\t")) or stripped[0].isdigit():
                m2 = re.match(r"^(\d+)\.(\d+)", stripped)
                if m2:
                    level = 2
            current = Section(heading=stripped[:120], level=level, content="")
            continue

        body_started = True
        if body_started:
            current.content += stripped + "\n"

    if current.content.strip():
        sections.append(current)

    return sections


def _extract_references(text: str) -> str:
    """Extract references section from text."""
    m = REFERENCE_HEADER.search(text)
    if not m:
        return ""

    ref_text = text[m.start() :]
    # Try to truncate at appendices or end
    appendix = re.search(r"\nappendix|\nsupplementary", ref_text, re.IGNORECASE)
    if appendix:
        ref_text = ref_text[: appendix.start()]

    ref_text = ref_text[:15000]  # Max 15K chars for references
    return ref_text.strip()


def _looks_like_title(line: str) -> bool:
    """Heuristic: does this line look like a paper title?"""
    return len(line.split()) >= 3 and len(line) < 200 and line[0].isupper()


def _looks_like_authors(text: str) -> bool:
    """Heuristic: does this text look like an author list?"""
    return ", " in text and len(text) < 300 and text.count(",") >= 1

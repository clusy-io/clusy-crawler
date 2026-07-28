from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import unescape
from typing import Any
from urllib.parse import (
    parse_qsl,
    quote,
    unquote,
    urlencode,
    urljoin,
    urlparse,
    urlunparse,
)

import structlog

logger = structlog.get_logger()

# DOI syntax is deliberately bounded.  The suffix alphabet comes from the
# Crossref recommendation and is broad enough for legacy identifiers without
# swallowing arbitrary prose after a DOI.
DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9<>]+", re.IGNORECASE)
ARXIV_ID_PATTERN = re.compile(
    r"(?P<id>(?:\d{4}\.\d{4,5}|[a-z][a-z0-9.-]*/\d{7})(?:v\d+)?)",
    re.IGNORECASE,
)
PMID_PATTERN = re.compile(r"^\d{1,12}$")
PMCID_PATTERN = re.compile(r"^PMC\d+$", re.IGNORECASE)
_MAX_ACADEMIC_TEXT_CHARS = 2_000_000
_MAX_REFERENCE_CHARS = 15_000
_MAX_ACADEMIC_AUTHORS = 500
_REFERENCE_TRUNCATION_MARKER = (
    "<!-- references truncated at 15000 characters -->"
)
_PREPRINT_HOST_SUFFIXES = ("biorxiv.org", "medrxiv.org", "chemrxiv.org")

_ACADEMIC_HOST_SUFFIXES = (
    "acs.org",
    "acm.org",
    "aip.org",
    "annualreviews.org",
    "aps.org",
    "biorxiv.org",
    "biomedcentral.com",
    "bmj.com",
    "cambridge.org",
    "cell.com",
    "chemrxiv.org",
    "degruyter.com",
    "elifesciences.org",
    "europepmc.org",
    "frontiersin.org",
    "hindawi.com",
    "ieee.org",
    "iop.org",
    "jamanetwork.com",
    "jmlr.org",
    "jstor.org",
    "karger.com",
    "medrxiv.org",
    "mdpi.com",
    "nature.com",
    "nejm.org",
    "openreview.net",
    "oup.com",
    "plos.org",
    "pnas.org",
    "proceedings.mlr.press",
    "proceedings.neurips.cc",
    "royalsocietypublishing.org",
    "rsc.org",
    "sagepub.com",
    "sciencedirect.com",
    "science.org",
    "springer.com",
    "tandfonline.com",
    "thelancet.com",
    "wiley.com",
)

# Patterns for academic structure detection
ABSTRACT_HEADERS = re.compile(
    r"^(?:abstract|summary|synopsis)\s*:?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
SECTION_HEADER = re.compile(
    r"^(?:\d+(?:\.\d+)*\.?\s+|[IVX]+\.\s+)?(?:"
    r"introductions?|background|methods?|experiments?|results?|discussions?|"
    r"conclusions?|related[\s-]+work|future[\s-]+work|"
    r"acknowledg(?:e)?ments?|appendix|appendices|supplement(?:ary)?|"
    r"implementations?|evaluations?|analyses?|analysis|approaches?|overviews?|"
    r"preliminar(?:y|ies)|motivations?|contributions?|problems?|solutions?|"
    r"literature[\s-]+review|surveys?|case[\s-]+stud(?:y|ies)|frameworks?|"
    r"architectures?|designs?|setups?|limitations?|comparisons?|ablations?"
    r")\b",
    re.IGNORECASE | re.MULTILINE,
)
REFERENCE_HEADER = re.compile(
    r"^(?:references?|bibliography|works.cited|citations?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_PRESERVED_POST_REFERENCE_HEADER = re.compile(
    r"^(?:"
    r"(?:appendix|appendices)(?:\s+[A-Z0-9][^\n]{0,120})?"
    r"|supplement(?:ary)?(?:\s+(?:material|information|methods?|data))?"
    r"(?:\s*[:.-]\s*[^\n]{1,100})?"
    r"|associated\s+data"
    r"|acknowledg(?:e)?ments?"
    r"|(?:data|code)\s+availability"
    r")\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_SKIPPED_POST_REFERENCE_HEADER = re.compile(
    r"^(?:author\s+information|authors?\s+and\s+affiliations|"
    r"corresponding\s+author|correspondence|ethics\s+declarations?|"
    r"additional\s+information|publisher(?:'s|’s)?\s+note)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
POST_REFERENCE_HEADER = re.compile(
    rf"(?:{_PRESERVED_POST_REFERENCE_HEADER.pattern}|"
    rf"{_SKIPPED_POST_REFERENCE_HEADER.pattern})",
    re.IGNORECASE | re.MULTILINE,
)

_PDF_TITLE_NOISE = re.compile(
    r"provided proper attribution|permission (?:is|to )|all rights reserved|"
    r"copyright|creative commons|accepted manuscript|preprint submitted|"
    r"terms (?:and|of) (?:use|service)|downloaded from|"
    r"^arxiv\s*:|^https?://|^www\.",
    re.IGNORECASE,
)
_PDF_AUTHOR_NOISE = re.compile(
    r"^(?:google brain|microsoft research|facebook ai research|"
    r"department of .+|school of .+|.+ university|.+ institute|"
    r".+ laboratory|.+ laboratories)$",
    re.IGNORECASE,
)
_REFERENCE_UI_LINE = re.compile(
    r"^(?:article|cas|pubmed|google scholar|view article|crossref|"
    r"web of science|download pdf|download references|ads|math|doi|"
    r"pmc free article|pubmed central)$",
    re.IGNORECASE,
)
_REFERENCE_UI_ONLY = re.compile(
    r"^(?:(?:Article|CAS|PubMed|Google Scholar|View Article|Crossref|"
    r"Web of Science|Download PDF|Download references|ADS|MATH|DOI|"
    r"PMC free article|PubMed Central)\s*){2,}$",
    re.IGNORECASE,
)
_REFERENCE_UI_TAIL = re.compile(
    r"(?:\s+(?:Article|CAS|PubMed|Google Scholar|View Article|Crossref|"
    r"Web of Science|Download PDF|Download references|ADS|MATH|DOI|"
    r"PMC free article|PubMed Central))+\s*$",
    re.IGNORECASE,
)
_REFERENCE_UI_SEQUENCE = re.compile(
    r"(?:(?<!\w)\[?\s*(?:Article|CAS|PubMed(?:\s+Central)?|Google Scholar|"
    r"View Article|Crossref|Web of Science|Download PDF|Download references|"
    r"ADS|MATH|DOI|PMC free article)\s*\]?){2,}",
    re.IGNORECASE,
)
_REFERENCE_UI_BRACKET = re.compile(
    r"\[\s*(?:Article|CAS|PubMed(?:\s+Central)?|Google Scholar|View Article|"
    r"Crossref|Web of Science|Download PDF|Download references|ADS|MATH|DOI|"
    r"PMC free article)\s*\]",
    re.IGNORECASE,
)
_REFERENCE_BIB_YEAR = re.compile(r"\b(?:18|19|20)\d{2}[a-z]?\b", re.IGNORECASE)
_REFERENCE_BIB_IDENTIFIER = re.compile(
    r"\b(?:10\.\d{4,9}/|arxiv\s*:|pmid\s*:|https?://)",
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
    journal: str = ""
    publication_date: str = ""
    license: str = ""
    pmid: str = ""
    pmcid: str = ""
    arxiv_id: str = ""
    canonical_url: str = ""
    pdf_url: str = ""
    truncated: bool = False
    truncation_reason: str = ""

    def to_markdown(self) -> str:
        parts: list[str] = []
        if self.title:
            parts.append(f"# {self.title}\n")
        if self.authors:
            parts.append(f"**Authors**: {', '.join(self.authors)}\n")
        identifiers: list[str] = []
        if self.doi:
            doi_href = f"https://doi.org/{quote(self.doi, safe='/')}"
            identifiers.append(f"DOI: [{self.doi}]({doi_href})")
        if self.pmid:
            identifiers.append(f"PMID: {self.pmid}")
        if self.pmcid:
            identifiers.append(f"PMCID: {self.pmcid}")
        if self.arxiv_id:
            identifiers.append(f"arXiv: {self.arxiv_id}")
        if identifiers:
            parts.append(f"**Identifiers**: {' · '.join(identifiers)}\n")
        if self.journal:
            publication = self.journal
            if self.publication_date:
                publication += f" ({self.publication_date})"
            parts.append(f"**Published in**: {publication}\n")
        if self.abstract:
            parts.append(f"## Abstract\n\n{self.abstract}\n")
        for sec in self.sections:
            parts.append(sec.to_markdown())
        if not self.sections and self.full_text:
            fallback_body = _fallback_body_text(self)
            if fallback_body:
                parts.append(f"## Full Text\n\n{fallback_body}\n")
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


def _mark_paper_truncated(paper: AcademicPaper, reason: str) -> None:
    paper.truncated = True
    reasons = [
        item.strip()
        for item in paper.truncation_reason.split(";")
        if item.strip()
    ]
    if reason not in reasons:
        reasons.append(reason)
    paper.truncation_reason = "; ".join(reasons)


def _host_matches(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith(f".{suffix}")


def _url_parts(url: str) -> tuple[str, str]:
    """Return a normalized hostname and decoded path for a URL-like value."""
    candidate = url.strip()
    if candidate.lower().startswith("doi:"):
        return "doi.org", f"/{candidate[4:].strip()}"
    parsed = urlparse(candidate)
    return (parsed.hostname or "").lower().rstrip("."), unquote(parsed.path)


def _arxiv_id_from_url(url: str) -> str:
    host, path = _url_parts(url)
    if not _host_matches(host, "arxiv.org"):
        return ""
    match = re.match(r"^/(?:abs|pdf|html|format)/", path, re.IGNORECASE)
    if not match:
        return ""
    identifier = path[match.end() :].removesuffix(".pdf").strip("/")
    parsed_id = ARXIV_ID_PATTERN.fullmatch(identifier)
    return parsed_id.group("id") if parsed_id else ""


def _pubmed_id_from_url(url: str) -> str:
    host, path = _url_parts(url)
    parts = [part for part in path.split("/") if part]
    candidate = ""
    if host == "pubmed.ncbi.nlm.nih.gov" and parts:
        candidate = parts[0]
    elif (
        _host_matches(host, "ncbi.nlm.nih.gov")
        and len(parts) >= 2
        and parts[0].lower() == "pubmed"
    ):
        candidate = parts[1]
    return candidate if PMID_PATTERN.fullmatch(candidate) else ""


def _pmcid_from_url(url: str) -> str:
    host, path = _url_parts(url)
    if not (
        host == "pmc.ncbi.nlm.nih.gov" or _host_matches(host, "ncbi.nlm.nih.gov")
    ):
        return ""
    for part in path.split("/"):
        if PMCID_PATTERN.fullmatch(part):
            return part.upper()
    return ""


def normalize_doi(value: str) -> str:
    """Return the DOI token in a URL, ``doi:`` value, or free-form field.

    Common prose punctuation is removed without corrupting balanced parentheses,
    which occur in valid legacy DOI suffixes.
    """
    decoded = unescape(unquote(value)).strip()
    match = DOI_PATTERN.search(decoded)
    if not match:
        return ""
    doi = match.group(0)
    doi = doi.rstrip(".,;:!?")
    pairs = (("(", ")"), ("[", "]"), ("{", "}"), ("<", ">"))
    changed = True
    while doi and changed:
        changed = False
        for opening, closing in pairs:
            if doi.endswith(closing) and doi.count(closing) > doi.count(opening):
                doi = doi[:-1]
                changed = True
    return doi


def classify_academic_url(url: str) -> str | None:
    """Classify known scholarly and repository URLs using hostname boundaries."""
    host, _path = _url_parts(url)
    if not host:
        return None
    if _host_matches(host, "arxiv.org") and _arxiv_id_from_url(url):
        return "arxiv"
    if _pubmed_id_from_url(url):
        return "pubmed"
    if _pmcid_from_url(url):
        return "pmc"
    if host in {"doi.org", "dx.doi.org"} and normalize_doi(url):
        return "doi"
    if _host_matches(host, "github.com"):
        return "github"
    if any(_host_matches(host, suffix) for suffix in _ACADEMIC_HOST_SUFFIXES):
        return "journal"
    return None


def canonicalize_academic_url(url: str) -> str:
    """Canonicalize stable identifiers while leaving publisher URLs intact."""
    source = classify_academic_url(url)
    if source == "arxiv":
        return f"https://arxiv.org/abs/{_arxiv_id_from_url(url)}"
    if source == "pubmed":
        return f"https://pubmed.ncbi.nlm.nih.gov/{_pubmed_id_from_url(url)}/"
    if source == "pmc":
        return f"https://pmc.ncbi.nlm.nih.gov/articles/{_pmcid_from_url(url)}/"
    if source == "doi":
        return f"https://doi.org/{normalize_doi(url)}"

    parsed = urlparse(url)
    if source == "github":
        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) >= 2:
            path_parts[1] = path_parts[1].removesuffix(".git")
            path = "/" + "/".join(path_parts)
            return urlunparse(("https", "github.com", path, "", parsed.query, ""))
    return url


def extract_academic_doi(url: str, html: str = "") -> str:
    """Extract a DOI only from a recognized scholarly URL or structured HTML.

    URL query strings and arbitrary visible prose are intentionally ignored.
    This makes the result suitable as an exact Crossref lookup key after a
    caller has fetched a known publisher page.
    """
    source = classify_academic_url(url)
    if source not in {"arxiv", "pubmed", "pmc", "doi", "journal"}:
        return ""

    _host, path = _url_parts(url)
    path_doi = normalize_doi(path)
    if path_doi:
        return path_doi
    if not html:
        return ""

    try:
        from lxml import html as lxml_html

        # Crawl responses are bounded upstream, but keep this public helper
        # independently bounded as it may also be used by fallback callers.
        tree = lxml_html.fromstring(html[:_MAX_ACADEMIC_TEXT_CHARS])
    except Exception:
        return ""

    meta = _meta_values(tree)
    candidates = _values_for_keys(
        meta,
        "citation_doi",
        "dc.identifier",
        "dc.identifier.doi",
        "dcterms.identifier",
        "doi",
        "identifier",
        "prism.doi",
    )
    for entity in _json_ld_entities(tree):
        for key in ("doi", "identifier", "sameAs", "@id"):
            raw = entity.get(key)
            if isinstance(raw, list):
                candidates.extend(str(value) for value in raw[:20])
            elif isinstance(raw, dict):
                candidates.extend(
                    str(raw.get(key) or "")
                    for key in ("value", "@value", "url", "@id")
                )
            elif raw:
                candidates.append(str(raw))

    candidates.extend(
        str(value)
        for value in tree.xpath(
            "//link[contains("
            "concat(' ',translate(normalize-space(@rel),"
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),' '),"
            "' canonical ')]/@href"
        )
    )
    for candidate in candidates[:200]:
        doi = normalize_doi(candidate)
        if doi:
            return doi
    return ""


def _meta_values(tree: Any) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for meta in tree.xpath("//meta"):
        key = (
            meta.get("name")
            or meta.get("property")
            or meta.get("itemprop")
            or ""
        ).strip().lower()
        content = re.sub(r"\s+", " ", unescape(meta.get("content") or "")).strip()
        if key and content:
            values.setdefault(key, []).append(content)
    return values


def _json_ld_entities(tree: Any) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            entities.append(value)
            graph = value.get("@graph")
            if isinstance(graph, list):
                for item in graph:
                    collect(item)
            main_entity = value.get("mainEntity")
            if isinstance(main_entity, (dict, list)):
                collect(main_entity)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    for script in tree.xpath(
        "//script[contains("
        "translate(@type,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
        "'ld+json')]"
    ):
        raw = script.text or ""
        if not raw.strip():
            continue
        try:
            collect(json.loads(raw))
        except (TypeError, ValueError):
            logger.debug("academic_jsonld_invalid")
    return entities


def _json_ld_scholarly_entity(entities: list[dict[str, Any]]) -> dict[str, Any]:
    scholarly_types = {
        "article",
        "medicalscholarlyarticle",
        "newsarticle",
        "report",
        "scholarlyarticle",
        "techarticle",
    }
    for entity in entities:
        raw_type = entity.get("@type", "")
        types = raw_type if isinstance(raw_type, list) else [raw_type]
        if any(str(item).lower() in scholarly_types for item in types):
            return entity
    return entities[0] if entities else {}


def _ieee_document_metadata(html: str) -> dict[str, Any]:
    marker = re.search(r"xplGlobal\.document\.metadata\s*=\s*", html)
    if not marker:
        return {}
    try:
        value, _end = json.JSONDecoder().raw_decode(html, marker.end())
    except (TypeError, ValueError):
        logger.debug("academic_ieee_metadata_invalid")
        return {}
    return value if isinstance(value, dict) else {}


def _markup_to_text(value: str) -> str:
    if "<" not in value:
        return re.sub(r"\s+", " ", unescape(value)).strip()
    try:
        from lxml import html as lxml_html

        wrapper = lxml_html.fromstring(f"<div>{value}</div>")
        return _element_text(wrapper)
    except Exception:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(value))).strip()


def _first(values: dict[str, list[str]], *keys: str) -> str:
    for key in keys:
        candidates = values.get(key)
        if candidates:
            return candidates[0]
    return ""


def _publication_date(
    meta: dict[str, list[str]],
    entity: dict[str, Any],
    ieee_metadata: dict[str, Any],
    url: str,
) -> str:
    candidates = _values_for_keys(
        meta,
        "citation_publication_date",
        "citation_date",
        "dc.date",
        "dcterms.issued",
        "prism.publicationdate",
    )
    entity_date = entity.get("datePublished")
    if isinstance(entity_date, str):
        candidates.append(entity_date)
    for value in (
        ieee_metadata.get("displayPublicationDate"),
        ieee_metadata.get("publicationDate"),
    ):
        if isinstance(value, str):
            candidates.append(value)

    cleaned: list[str] = []
    for value in candidates:
        candidate = re.sub(r"\s+", " ", unescape(value)).strip()[:64]
        if candidate and candidate not in cleaned:
            cleaned.append(candidate)
    if not cleaned:
        return ""

    host, _path = _url_parts(url)
    is_preprint = any(
        _host_matches(host, suffix) for suffix in _PREPRINT_HOST_SUFFIXES
    )
    if not is_preprint:
        return cleaned[0]

    default_day = re.fullmatch(
        r"(?P<year>\d{4})[-/]01[-/]01(?:[T ]00:00(?::00(?:\.0+)?)?Z?)?",
        cleaned[0],
    )
    if default_day is None:
        return cleaned[0]

    # Several preprint landing pages synthesize January 1 when their citation
    # export knows only a year. A second structured source may carry the actual
    # posting date; otherwise retain honest year-only precision.
    year = default_day.group("year")
    for candidate in cleaned[1:]:
        if candidate.startswith(year) and not re.fullmatch(
            rf"{year}[-/]01[-/]01(?:[T ]00:00(?::00(?:\.0+)?)?Z?)?",
            candidate,
        ):
            return candidate
    return year


def _clean_title(value: str, source: str) -> str:
    title = re.sub(r"\s+", " ", unescape(value)).strip()
    if source == "pubmed":
        title = re.sub(r"\s*[-|]\s*PubMed\s*$", "", title, flags=re.IGNORECASE)
    return title[:1000]


def _author_names(
    meta: dict[str, list[str]],
    entity: dict[str, Any],
) -> tuple[list[str], bool]:
    # Do not concatenate equivalent Highwire and JSON-LD lists: publishers
    # often format one as "Family, Given" and the other as "Given Family", so
    # string de-duplication cannot recognize them and doubles every author.
    raw_names = list(meta.get("citation_author", []))
    if not raw_names:
        for combined in meta.get("citation_authors", []):
            raw_names.extend(combined.split(";"))
    if not raw_names:
        raw_names.extend(meta.get("dc.creator", []))
        raw_names.extend(meta.get("dc.creator.personalname", []))

    if not raw_names:
        structured = entity.get("author", [])
        if not isinstance(structured, list):
            structured = [structured]
        for author in structured[:1000]:
            if isinstance(author, str):
                raw_names.append(author)
            elif isinstance(author, dict):
                name = author.get("name")
                if not name:
                    name = " ".join(
                        str(author.get(key, "")).strip()
                        for key in ("givenName", "familyName")
                    ).strip()
                if name:
                    raw_names.append(str(name))

    authors: list[str] = []
    seen: set[str] = set()
    truncated = len(raw_names) > 1000
    for raw_name in raw_names[:1000]:
        name = re.sub(r"\s+", " ", unescape(str(raw_name))).strip(" ;,")[:300]
        key = name.casefold()
        if name and key not in seen:
            authors.append(name)
            seen.add(key)
        if len(authors) >= _MAX_ACADEMIC_AUTHORS:
            truncated = truncated or len(raw_names) > len(seen)
            break
    return authors, truncated


def academic_pdf_candidates(html: str, url: str) -> list[str]:
    """Return a small, ordered PDF fallback budget for a landing page.

    Preprint servers commonly advertise several aliases for the same generated
    PDF. Trying each alias serially can consume most of a crawl deadline, so
    those hosts intentionally get one high-confidence attempt.
    """
    try:
        from lxml import html as lxml_html

        tree = lxml_html.fromstring(html)
    except Exception:
        tree = None
    return _academic_pdf_candidates_from_tree(
        tree,
        url,
        ieee_metadata=_ieee_document_metadata(html),
    )


def _academic_pdf_candidates_from_tree(
    tree: Any | None,
    url: str,
    *,
    ieee_metadata: dict[str, Any] | None = None,
) -> list[str]:
    candidates: list[str] = []
    if tree is not None:
        meta = _meta_values(tree)
        for key in (
            "citation_pdf_url",
            "eprints.document_url",
            "wkhealth_pdf_url",
        ):
            candidates.extend(meta.get(key, []))
        candidates.extend(
            str(value)
            for value in tree.xpath(
                "//link[contains("
                "translate(@type,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                "'application/pdf')]/@href"
            )
        )
        # Publisher buttons are useful only when either the URL or the visible
        # label explicitly says PDF.  This avoids following generic downloads.
        for anchor in tree.xpath("//a[@href]"):
            href = str(anchor.get("href") or "").strip()
            label = _element_text(anchor).lower()
            supplement = re.search(
                r"\b(?:supplement(?:ary)?|supporting|poster|figure|table)\b",
                f"{href} {label}",
                re.IGNORECASE,
            )
            if not supplement and (
                ".pdf" in href.lower()
                or re.search(r"\b(?:view|download)?\s*pdf\b", label)
            ):
                candidates.append(href)

    if ieee_metadata and (
        ieee_metadata.get("isOpenAccess")
        or ieee_metadata.get("isFreeDocument")
        or ieee_metadata.get("openAccessFlag") == "T"
    ):
        candidates.append(str(ieee_metadata.get("pdfUrl") or ""))

    arxiv_id = _arxiv_id_from_url(url)
    if arxiv_id:
        candidates.append(f"https://arxiv.org/pdf/{arxiv_id}")

    output: list[str] = []
    seen: set[str] = set()
    host, _path = _url_parts(url)
    candidate_budget = (
        1
        if any(_host_matches(host, suffix) for suffix in _PREPRINT_HOST_SUFFIXES)
        else 10
    )
    for candidate in candidates:
        absolute = urljoin(url, unescape(candidate)).split("#", 1)[0]
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        dedupe_key = _pdf_candidate_dedupe_key(absolute)
        if absolute == url or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        output.append(absolute)
        if len(output) >= candidate_budget:
            break
    return output


def _pdf_candidate_dedupe_key(url: str) -> str:
    """Normalize presentation-only PDF URL variants for candidate fan-out."""
    parsed = urlparse(url)
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.casefold()
            not in {
                "download",
                "downloadformat",
                "downloadpdf",
                "utm_campaign",
                "utm_content",
                "utm_medium",
                "utm_source",
                "utm_term",
            }
        ),
        doseq=True,
    )
    hostname = (parsed.hostname or "").lower().rstrip(".")
    try:
        port = parsed.port
    except ValueError:
        return url
    if port is not None and not (
        (parsed.scheme.lower() == "https" and port == 443)
        or (parsed.scheme.lower() == "http" and port == 80)
    ):
        hostname = f"{hostname}:{port}"
    return urlunparse(
        (
            parsed.scheme.lower(),
            hostname,
            parsed.path,
            parsed.params,
            query,
            "",
        )
    )


def extract_pdf(contents: bytes, url: str = "") -> AcademicPaper:
    """Extract structured content from PDF bytes using pypdfium2.

    pypdfium2 (BSD-3-Clause, on Google's PDFium) replaced PyMuPDF/`fitz`, which
    is AGPL-3.0 and incompatible with a permissive OSS release.
    """
    import pypdfium2 as pdfium

    paper = AcademicPaper(canonical_url=canonicalize_academic_url(url) if url else "")
    paper.arxiv_id = _arxiv_id_from_url(url)
    paper.pmid = _pubmed_id_from_url(url)
    paper.pmcid = _pmcid_from_url(url)
    paper.doi = normalize_doi(url)
    doc = pdfium.PdfDocument(contents)

    try:
        full_parts: list[str] = []
        metadata = doc.get_metadata_dict() or {}
        metadata_title = str(metadata.get("Title", "") or "").strip()[:1000]
        metadata_doi = _find_doi(str(metadata))
        if metadata_doi:
            paper.doi = metadata_doi
        metadata_author = str(metadata.get("Author", "") or "").strip()[:30_000]

        total_pages = len(doc)
        if total_pages > 200:
            _mark_paper_truncated(paper, "PDF page limit (200 pages)")

        extracted_characters = 0
        for page_num in range(min(total_pages, 200)):
            # Account for the newline inserted between retained pages. Check the
            # budget before opening the next page so reaching the cap never
            # triggers one more potentially expensive PDFium text decode.
            separator_characters = 1 if full_parts else 0
            remaining = (
                _MAX_ACADEMIC_TEXT_CHARS
                - extracted_characters
                - separator_characters
            )
            if remaining <= 0:
                break

            page = None
            textpage = None
            page_characters = 0
            try:
                page = doc[page_num]
                textpage = page.get_textpage()
                # PDFium can expose a very large decompressed text layer from a
                # small input PDF. Bound the native-to-Python allocation itself,
                # rather than creating the whole page string and slicing later.
                # pypdfium2 requires ``count`` to stay inside its internal
                # character range; passing the (usually much larger) document
                # budget raises ArgumentError on ordinary short pages.
                page_characters = textpage.count_chars()
                text = (
                    textpage.get_text_range(count=min(remaining, page_characters))
                    if page_characters > 0
                    else ""
                )
            except Exception as exc:
                # One malformed page must not discard text successfully decoded
                # from the rest of a large paper.
                logger.warning(
                    "academic_pdf_page_failed",
                    page=page_num + 1,
                    error_type=type(exc).__name__,
                )
                _mark_paper_truncated(
                    paper,
                    "PDF page text decode failure",
                )
                continue
            finally:
                if textpage is not None:
                    textpage.close()
                if page is not None:
                    page.close()
            if text:
                bounded = text[:remaining]
                full_parts.append(bounded)
                extracted_characters += separator_characters + len(bounded)
                if extracted_characters >= _MAX_ACADEMIC_TEXT_CHARS:
                    if page_characters > len(bounded) or page_num + 1 < total_pages:
                        _mark_paper_truncated(
                            paper,
                            f"PDF text limit ({_MAX_ACADEMIC_TEXT_CHARS} characters)",
                        )
                    break

        paper.full_text = "\n".join(full_parts)
        first_page_text = full_parts[0] if full_parts else ""
        paper.title = _pdf_metadata_title(
            metadata_title,
            first_page_text,
            arxiv_id=paper.arxiv_id,
        )
        paper.authors = _pdf_metadata_authors(
            metadata_author,
            first_page_text,
            arxiv_id=paper.arxiv_id,
        )

        if not paper.title and paper.full_text:
            paper.title = _extract_title_from_text(first_page_text or paper.full_text)

        if not paper.authors:
            paper.authors = _extract_authors(first_page_text or paper.full_text)
        paper.abstract = _extract_abstract(paper.full_text)
        paper.sections = _segment_sections(
            paper.full_text,
            title=paper.title,
            abstract=paper.abstract,
            authors=paper.authors,
        )
        paper.references_raw = _extract_references(paper.full_text)
        if _REFERENCE_TRUNCATION_MARKER in paper.references_raw:
            _mark_paper_truncated(
                paper,
                "references limit (15000 characters)",
            )
        paper.word_count = len(paper.full_text.split())
    finally:
        doc.close()

    return paper


def extract_long_html(html: str, url: str = "") -> AcademicPaper:
    """Extract scholarly structure from Highwire, JSON-LD, and semantic HTML."""
    from lxml import html as lxml_html

    source = classify_academic_url(url) or ""
    paper = AcademicPaper(
        canonical_url=canonicalize_academic_url(url) if url else "",
        arxiv_id=_arxiv_id_from_url(url),
        pmid=_pubmed_id_from_url(url),
        pmcid=_pmcid_from_url(url),
        doi=normalize_doi(url),
    )

    try:
        tree = lxml_html.fromstring(html)
    except Exception:
        # Try removing non-HTML content
        clean = re.sub(r"<[^>]+>", " ", html)
        full_text = re.sub(r"\s+", " ", clean).strip()
        if len(full_text) > _MAX_ACADEMIC_TEXT_CHARS:
            _mark_paper_truncated(
                paper,
                f"HTML text limit ({_MAX_ACADEMIC_TEXT_CHARS} characters)",
            )
        paper.full_text = full_text[:_MAX_ACADEMIC_TEXT_CHARS]
        paper.word_count = len(paper.full_text.split())
        return paper

    meta = _meta_values(tree)
    entities = _json_ld_entities(tree)
    entity = _json_ld_scholarly_entity(entities)
    ieee_metadata = _ieee_document_metadata(html)

    entity_title = str(entity.get("headline") or entity.get("name") or "").strip()
    h1 = tree.xpath("//article//h1[1] | //main//h1[1] | //h1[1]")
    h1_title = _element_text(h1[0]) if h1 else ""
    title_element = tree.xpath("//title[1]")
    document_title = title_element[0].text_content().strip() if title_element else ""
    paper.title = _clean_title(
        _first(meta, "citation_title", "dc.title", "dcterms.title")
        or entity_title
        or str(
            ieee_metadata.get("displayDocTitle")
            or ieee_metadata.get("formulaStrippedArticleTitle")
            or ieee_metadata.get("title")
            or ""
        )
        or _first(meta, "og:title", "twitter:title")
        or h1_title
        or document_title,
        source,
    )

    paper.authors, authors_truncated = _author_names(meta, entity)
    if authors_truncated:
        _mark_paper_truncated(
            paper,
            f"author metadata limit ({_MAX_ACADEMIC_AUTHORS} authors)",
        )
    if not paper.authors:
        ieee_authors = ieee_metadata.get("authors", [])
        if isinstance(ieee_authors, list):
            paper.authors = [
                re.sub(r"\s+", " ", str(author.get("name") or "")).strip()
                for author in ieee_authors
                if isinstance(author, dict) and author.get("name")
            ][:100]
            paper.authors = [author[:300] for author in paper.authors]
        if not paper.authors:
            paper.authors = _split_author_field(
                str(ieee_metadata.get("authorNames") or "")
            )

    identifier_values = [
        *_values_for_keys(
            meta,
            "citation_doi",
            "dc.identifier",
            "dc.identifier.doi",
            "dcterms.identifier",
            "doi",
            "identifier",
            "prism.doi",
        ),
        str(entity.get("identifier") or ""),
        str(entity.get("sameAs") or ""),
        str(entity.get("@id") or ""),
        str(ieee_metadata.get("doi") or ""),
        str(ieee_metadata.get("doiLink") or ""),
    ]
    for identifier in identifier_values:
        doi = normalize_doi(identifier)
        if doi:
            paper.doi = doi
            break

    paper.pmid = (
        _clean_identifier(
            _first(meta, "citation_pmid", "pmid"),
            PMID_PATTERN,
        )
        or paper.pmid
    )
    paper.pmcid = (
        _clean_identifier(
            _first(meta, "citation_pmcid", "pmcid"),
            PMCID_PATTERN,
            uppercase=True,
        )
        or paper.pmcid
    )
    paper.arxiv_id = (
        _clean_identifier(
            _first(meta, "citation_arxiv_id", "arxiv_id"),
            ARXIV_ID_PATTERN,
        )
        or paper.arxiv_id
    )

    paper.journal = _first(
        meta,
        "citation_journal_title",
        "prism.publicationname",
        "dc.source",
    )
    entity_journal = entity.get("isPartOf")
    if not paper.journal and isinstance(entity_journal, dict):
        paper.journal = str(entity_journal.get("name") or "").strip()
    if not paper.journal:
        paper.journal = str(
            ieee_metadata.get("displayPublicationTitle")
            or ieee_metadata.get("publicationTitle")
            or ""
        ).strip()
    paper.journal = paper.journal[:500]
    paper.publication_date = _publication_date(meta, entity, ieee_metadata, url)
    entity_license = entity.get("license")
    if isinstance(entity_license, dict):
        entity_license = entity_license.get("url") or entity_license.get("name")
    paper.license = (
        _first(meta, "citation_license", "dcterms.license")
        or str(entity_license or "").strip()
        or _extract_license_url(tree, url)
        or _first(meta, "dc.rights", "prism.copyright")
        or str(ieee_metadata.get("articleCopyRight") or "").strip()
    )
    paper.license = paper.license[:1000]

    html_language = tree.get("lang") or ""
    paper.language = (
        _first(meta, "citation_language", "dc.language", "og:locale")
        or str(entity.get("inLanguage") or "").strip()
        or html_language.strip()
    )
    paper.language = paper.language[:64]

    canonical_links = tree.xpath(
        "//link[contains("
        "concat(' ',translate(normalize-space(@rel),"
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),' '),"
        "' canonical ')]/@href"
    )
    advertised_url = (
        str(canonical_links[0]).strip()
        if canonical_links
        else _first(meta, "og:url", "citation_abstract_html_url")
    )
    if advertised_url:
        candidate = urljoin(url, advertised_url)
        parsed_candidate = urlparse(candidate)
        if parsed_candidate.scheme in {"http", "https"} and parsed_candidate.hostname:
            paper.canonical_url = canonicalize_academic_url(candidate)

    pdf_candidates = _academic_pdf_candidates_from_tree(
        tree,
        url,
        ieee_metadata=ieee_metadata,
    )
    if pdf_candidates:
        paper.pdf_url = pdf_candidates[0]
    elif ieee_metadata.get("pdfUrl"):
        paper.pdf_url = urljoin(url, str(ieee_metadata["pdfUrl"]))

    # Full text from leaf-level semantic blocks. Container ``text_content()``
    # must not be combined with a recursive walk: doing both repeats every
    # descendant once per ancestor and severely corrupts long papers.
    root = _select_content_root(tree, source)
    _remove_academic_noise(root)

    text_parts: list[str] = []
    content_nodes = root.xpath(
        ".//h1|.//h2|.//h3|.//h4|.//h5|.//h6|"
        ".//p|.//li|.//dt|.//dd|.//pre|.//blockquote|.//figcaption|.//td|.//th"
    )
    content_node_set = set(content_nodes)
    for el in content_nodes:
        # A list item, table cell, or blockquote often wraps one or more
        # paragraphs. Emit the deepest semantic blocks only so their text does
        # not appear once through the container and again through each child.
        if any(descendant in content_node_set for descendant in el.iterdescendants()):
            continue
        txt = _element_text(el)
        if txt and len(txt) > 1 and (not text_parts or text_parts[-1] != txt):
            text_parts.append(txt)

    # Some generated paper pages use only nested <div>s. Retain a conservative
    # fallback, but take the root text exactly once.
    if not text_parts:
        fallback_text = _element_text(root)
        if fallback_text:
            text_parts.append(fallback_text)

    full_text = "\n".join(t for t in text_parts if t)
    if len(full_text) > _MAX_ACADEMIC_TEXT_CHARS:
        _mark_paper_truncated(
            paper,
            f"HTML text limit ({_MAX_ACADEMIC_TEXT_CHARS} characters)",
        )
    paper.full_text = full_text[:_MAX_ACADEMIC_TEXT_CHARS]
    paper.word_count = len(paper.full_text.split())

    entity_abstract = str(
        entity.get("abstract") or entity.get("description") or ""
    ).strip()
    ieee_abstract = _markup_to_text(str(ieee_metadata.get("abstract") or ""))
    paper.abstract = (
        _first(meta, "citation_abstract", "dc.description", "dcterms.abstract")
        or entity_abstract
        or ieee_abstract
        or _extract_abstract_from_tree(tree)
        or _first(meta, "description", "og:description")
    )
    # Highwire/JSON-LD abstract fields are often HTML fragments (bioRxiv) or
    # concatenate adjacent structural tags without whitespace (older PLOS).
    # Parse markup before whitespace normalization so tags never leak and
    # heading/paragraph boundaries remain separated.
    paper.abstract = _normalize_structured_abstract(
        _markup_to_text(paper.abstract)
    )[:10000]

    if not paper.abstract:
        paper.abstract = _extract_abstract(paper.full_text)
    parsed_path = urlparse(url).path.lower()
    abstract_landing = source == "pubmed" or (
        source == "arxiv" and parsed_path.startswith("/abs/")
    )
    host, _path = _url_parts(url)
    is_preprint = any(
        _host_matches(host, suffix) for suffix in _PREPRINT_HOST_SUFFIXES
    )
    if is_preprint and paper.abstract:
        abstract_words = len(paper.abstract.split())
        # A bioRxiv-family page with little more than its abstract and author
        # widgets is a landing record. Full-text preprint HTML is normally many
        # times larger and remains eligible for full-text extraction.
        abstract_landing = abstract_landing or paper.word_count < max(
            1000,
            abstract_words * 3,
        )
    if ieee_metadata and paper.abstract:
        abstract_words = len(paper.abstract.split())
        abstract_landing = abstract_landing or paper.word_count < max(
            400,
            abstract_words * 2,
        )
    if abstract_landing and paper.abstract:
        # arXiv /abs and PubMed records are metadata landing pages, not full
        # articles.  Their surrounding controls, MeSH search buttons, and
        # recommendation widgets must not masquerade as paper body text.
        paper.full_text = paper.abstract
        paper.word_count = len(paper.abstract.split())
        paper.sections = []
        paper.references_raw = ""
    else:
        paper.sections = _segment_sections(
            paper.full_text,
            title=paper.title,
            abstract=paper.abstract,
            authors=paper.authors,
        )
        paper.references_raw = _extract_references(paper.full_text)
        if _REFERENCE_TRUNCATION_MARKER in paper.references_raw:
            _mark_paper_truncated(
                paper,
                "references limit (15000 characters)",
            )

    return paper


def _values_for_keys(meta: dict[str, list[str]], *keys: str) -> list[str]:
    output: list[str] = []
    for key in keys:
        output.extend(meta.get(key, []))
    return output


def _clean_identifier(
    value: str,
    pattern: re.Pattern[str],
    *,
    uppercase: bool = False,
) -> str:
    candidate = value.strip()
    match = pattern.fullmatch(candidate)
    if not match:
        return ""
    if "id" in match.groupdict():
        candidate = match.group("id")
    return candidate.upper() if uppercase else candidate


def _element_text(element: Any) -> str:
    chunks = [str(chunk).strip() for chunk in element.itertext() if str(chunk).strip()]
    text = re.sub(r"\s+", " ", " ".join(chunks)).strip()
    return re.sub(r"\s+([,.;:!?])", r"\1", text)


def _select_content_root(tree: Any, source: str = "") -> Any:
    """Choose the smallest substantial semantic container on a paper page."""
    source_selectors: tuple[str, ...] = ()
    if source == "arxiv":
        source_selectors = (
            "//*[@id='abs']//*[contains(concat(' ',normalize-space(@class),' '),' abstract ')]",
            "//*[@id='abs']",
        )
    elif source == "pubmed":
        source_selectors = (
            "//*[@id='abstract']//*[contains(concat(' ',normalize-space(@class),' '),"
            "' abstract-content ')]",
            "//*[@id='abstract']",
        )
    elif source == "pmc":
        source_selectors = ("//*[@id='main-content']//article", "//article")

    for selector in (
        *source_selectors,
        "//article",
        "//main[@role='main']",
        "//main",
        "//*[@role='main']",
        "//*[@id='main-content']",
        "//*[@id='content']",
    ):
        for candidate in tree.xpath(selector):
            visible = _element_text(candidate)
            if len(visible) >= 200:
                return candidate
    body = tree.xpath("//body")
    return body[0] if body else tree


def _remove_academic_noise(root: Any) -> None:
    noisy_nodes = root.xpath(
        ".//script|.//style|.//noscript|.//template|.//nav|.//header|.//footer|.//aside|"
        ".//button|.//form|.//select|.//input|"
        ".//*[@aria-hidden='true']|"
        ".//*[contains(concat(' ',normalize-space(@class),' '),' cookie-banner ')]|"
        ".//*[contains(concat(' ',normalize-space(@class),' '),' cookie-consent ')]|"
        ".//*[contains(concat(' ',normalize-space(@class),' '),' social-share ')]|"
        ".//*[contains(concat(' ',normalize-space(@class),' '),' related-articles ')]|"
        ".//*[contains(concat(' ',normalize-space(@class),' '),' recommended ')]|"
        ".//*[contains(concat(' ',normalize-space(@class),' '),"
        "' c-article-references__links ')]|"
        ".//*[contains(concat(' ',normalize-space(@class),' '),"
        "' c-article-authors-search ')]|"
        ".//*[contains(concat(' ',normalize-space(@class),' '),"
        "' author-tooltip-find-more ')]|"
        ".//*[contains(concat(' ',normalize-space(@class),' '),' reflinks ')]"
    )
    for noisy in noisy_nodes:
        parent = noisy.getparent()
        if parent is not None:
            parent.remove(noisy)

    # PMC and several other JATS renderers place citation controls directly
    # beside the bibliographic <cite>, without a stable wrapper. Remove only
    # links whose complete label is a known lookup action; citation prose and
    # ordinary inline links remain untouched.
    for node in root.xpath(".//a"):
        label = _element_text(node)
        if not _REFERENCE_UI_LINE.fullmatch(label):
            continue
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)


def _extract_abstract_from_tree(tree: Any) -> str:
    candidates = tree.xpath(
        "//*[@id='abstract'] | "
        "//*[contains(concat(' ',normalize-space(@class),' '),' abstract-content ')] | "
        "//*[contains(concat(' ',normalize-space(@class),' '),' abstract ')]"
    )
    for candidate in candidates:
        text = _element_text(candidate)
        text = re.sub(r"^(?:abstract|summary)\s*:?\s*", "", text, flags=re.IGNORECASE)
        if len(text) >= 40:
            return text[:10000]
    return ""


def _normalize_structured_abstract(value: str) -> str:
    """Restore boundaries lost by metadata serializers between abstract fields."""
    value = re.sub(r"(?<=[a-z0-9)])\.(?=[A-Z])", ". ", value)
    labels = (
        "Background",
        "Objective",
        "Objectives",
        "Methods",
        "Methodology",
        "Principal Findings",
        "Findings",
        "Results",
        "Conclusions",
        "Conclusion",
        "Significance",
    )
    labels_pattern = "|".join(re.escape(label) for label in labels)
    value = re.sub(
        rf"\b({labels_pattern})(?=[A-Z])",
        r"\1: ",
        value,
    )
    return re.sub(r"\s+", " ", value).strip()


def _extract_license_url(tree: Any, base_url: str) -> str:
    hrefs = tree.xpath(
        "//link[contains(concat(' ',translate(normalize-space(@rel),"
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),' '),' license ')]/@href"
        " | //a[contains(concat(' ',translate(normalize-space(@rel),"
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),' '),' license ')]/@href"
        " | //a[contains(translate(@href,"
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
        "'creativecommons.org/licenses/')]/@href"
        " | //a[contains(translate(@href,"
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
        "'creativecommons.org/publicdomain/')]/@href"
    )
    for href in hrefs:
        candidate = urljoin(base_url, str(href).strip())
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            return candidate
    return ""


def _split_author_field(value: str) -> list[str]:
    value = value[:30_000]
    separator = ";" if ";" in value else r"\s+(?:and|&)\s+"
    raw_authors = (
        value.split(";")
        if separator == ";"
        else re.split(separator, value, flags=re.IGNORECASE)
    )
    output: list[str] = []
    seen: set[str] = set()
    for raw_author in raw_authors:
        author = re.sub(r"\s+", " ", raw_author).strip(" ,;")[:300]
        key = author.casefold()
        if author and key not in seen:
            output.append(author)
            seen.add(key)
    return output[:100]


def _fallback_body_text(paper: AcademicPaper) -> str:
    """Keep unsegmented full text while avoiding obvious metadata repetition."""
    excluded = {
        re.sub(r"\s+", " ", value).strip().casefold()
        for value in [paper.title, paper.abstract, *paper.authors]
        if value
    }
    lines: list[str] = []
    for line in paper.full_text.splitlines():
        normalized = re.sub(r"\s+", " ", line).strip()
        if not normalized or normalized.casefold() in excluded:
            continue
        if REFERENCE_HEADER.fullmatch(normalized):
            break
        lines.append(normalized)
    return "\n\n".join(lines)


def _find_doi(metadata_str: str) -> str:
    """Extract DOI from metadata string."""
    return normalize_doi(metadata_str)


def _normalized_pdf_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", unescape(value).casefold()))


def _pdf_metadata_title(value: str, first_page: str, *, arxiv_id: str) -> str:
    title = re.sub(r"\s+", " ", unescape(value)).strip()[:1000]
    if not title:
        return ""
    if not arxiv_id:
        return title

    words = title.split()
    normalized_title = _normalized_pdf_text(title)
    normalized_page = _normalized_pdf_text(first_page[:20_000])
    if (
        len(title) < 8
        or len(words) < 2
        or len(words) > 60
        or _PDF_TITLE_NOISE.search(title)
        or not normalized_title
        or normalized_title not in normalized_page
    ):
        return ""
    return title


def _pdf_metadata_authors(value: str, first_page: str, *, arxiv_id: str) -> list[str]:
    if not value:
        return []
    authors = _split_author_field(value)
    if not arxiv_id:
        return authors

    # PDF producer fields on arXiv frequently contain the generating
    # institution rather than the paper's authors. Prefer no author metadata to
    # confidently wrong attribution; the first-page heuristic can still recover
    # an actual comma-separated author line.
    header = first_page.split("\nAbstract", 1)[0][:20_000]
    normalized_header = _normalized_pdf_text(header)
    output: list[str] = []
    for author in authors:
        normalized = _normalized_pdf_text(author)
        if (
            len(author) < 3
            or _PDF_AUTHOR_NOISE.fullmatch(author)
            or not normalized
            or normalized not in normalized_header
        ):
            continue
        output.append(author)
    return output


def _extract_title_from_text(text: str) -> str:
    """Heuristic title extraction from first non-empty lines."""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines:
        return ""
    # Title is typically the first line before a blank line or author line
    for line in lines[:10]:
        if (
            2 <= len(line.split()) <= 40
            and 8 <= len(line) < 300
            and not _PDF_TITLE_NOISE.search(line)
            and not _PDF_AUTHOR_NOISE.fullmatch(line)
            and not normalize_doi(line)
            and not (line.count(",") >= 2 and _looks_like_authors(line))
        ):
            return line
    return ""


def _extract_authors(text: str) -> list[str]:
    """Extract author names from text near the beginning."""
    first_5000 = text[:5000]
    lines = first_5000.split("\n")

    author_line = ""
    for i, line in enumerate(lines[:30]):
        lower = line.lower().strip()
        # Author sections typically contain university domains, commas, "and"
        if any(kw in lower for kw in ("university", "institute", "laboratory", "@", "college")):
            author_line = lines[i - 1] if i > 0 and not _looks_like_title(lines[i - 1]) else line
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
    abstract = remaining[: end_match.start()].strip() if end_match else remaining[:3000].strip()

    return abstract


def _looks_like_section_heading(
    value: str,
    *,
    allow_generic_numbered: bool = True,
) -> bool:
    """Return a conservative academic heading match.

    PDF text has no DOM tag boundary, so a keyword prefix alone is unsafe:
    ordinary sentences such as "Results show ..." and "architectures from..."
    were previously promoted to headings. Require a short, non-sentence line
    and an exact keyword word boundary.
    """
    if re.search(r"[.!?]\s*$", value):
        return False
    numbered_match = re.match(
        r"^(?:\d+(?:\.\d+)*\.?\s+|[IVX]+\.\s+)",
        value,
        flags=re.IGNORECASE,
    )
    strict_numbered_match = re.match(
        r"^(?:\d+\.\s+|\d+\.\d+(?:\.\d+)*\.?\s+|[IVX]+\.\s+)",
        value,
        flags=re.IGNORECASE,
    )
    topic_match = SECTION_HEADER.match(value)
    if numbered_match is not None:
        if len(value.split()) > 18 or "[[" in value or "]]" in value:
            return False
        if topic_match is not None:
            return True
        if not allow_generic_numbered:
            return False
        numeric = re.match(r"^(\d+)", value)
        if strict_numbered_match is not None:
            return numeric is None or int(numeric.group(1)) <= 50
        top_level = re.match(r"^(\d+)\s+(.+)$", value)
        if top_level is None or not 1 <= int(top_level.group(1)) <= 20:
            return False
        tail = top_level.group(2)
        first_alpha = next((char for char in tail if char.isalpha()), "")
        return bool(first_alpha and first_alpha.isupper() and len(tail.split()) <= 12)

    if topic_match is None:
        return False

    words = value.split()
    if len(words) > 6:
        return False
    folded = value.casefold()
    appendix_like = folded.startswith(("appendix", "appendices", "supplement"))
    if appendix_like:
        return len(words) <= 12 and not any(char in value for char in "()[]?!")
    if any(char in value for char in ",:;()[]?!"):
        return False
    return value.count("-") <= 1


def _reference_header_line_index(lines: list[str]) -> int | None:
    """Choose the most bibliography-like exact References header.

    Scientific diagrams frequently contain standalone labels such as
    ``Reference`` or ``Reference Models`` before the actual bibliography.
    Selecting the first regex match silently discarded the remaining paper.
    Score every exact header using bounded following evidence and prefer the
    strongest candidate, with later position as a deterministic tie-break.
    """
    candidates = [
        index
        for index, line in enumerate(lines)
        if REFERENCE_HEADER.fullmatch(re.sub(r"\s+", " ", line).strip())
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    def score(index: int) -> tuple[int, int]:
        sample = "\n".join(lines[index + 1 : index + 161])[:12_000]
        evidence = (
            len(_REFERENCE_BIB_YEAR.findall(sample))
            + 2 * len(_REFERENCE_BIB_IDENTIFIER.findall(sample))
        )
        return evidence, index

    return max(candidates, key=score)


def _segment_sections(
    text: str,
    *,
    title: str = "",
    abstract: str = "",
    authors: list[str] | None = None,
) -> list[Section]:
    """Segment academic text without duplicating abstract or references."""
    sections: list[Section] = []
    lines = text.split("\n")
    current = Section(heading="Body", level=1, content="")
    current_lines: list[str] = []
    skipping_abstract = False
    in_references = False
    skipping_end_matter = False
    past_references = False
    inside_appendix = False
    reference_header_index = _reference_header_line_index(lines)
    normalized_abstract = re.sub(r"\s+", " ", abstract).strip().casefold()
    repeated_heading_counts: dict[str, int] = {}
    for raw_line in lines:
        candidate = re.sub(r"\s+", " ", raw_line).strip()
        if (
            candidate
            and re.match(
                r"^(?:\d+(?:\.\d+)*\.?\s+|[IVX]+\.\s+)",
                candidate,
                flags=re.IGNORECASE,
            )
            is None
            and _looks_like_section_heading(
                candidate,
                allow_generic_numbered=False,
            )
        ):
            key = candidate.casefold()
            repeated_heading_counts[key] = repeated_heading_counts.get(key, 0) + 1
    excluded = {
        re.sub(r"\s+", " ", value).strip().casefold()
        for value in [title, abstract, *(authors or [])]
        if value
    }

    def append_current() -> None:
        nonlocal current_lines
        if not current_lines:
            return
        current.content = "\n".join(current_lines) + "\n"
        sections.append(current)
        current_lines = []

    for line_index, line in enumerate(lines):
        stripped = re.sub(r"\s+", " ", line).strip()

        if not stripped:
            continue
        folded = stripped.casefold()
        if folded in excluded:
            continue

        if skipping_end_matter:
            if _PRESERVED_POST_REFERENCE_HEADER.fullmatch(stripped):
                current = Section(heading=stripped[:120], level=1, content="")
                current_lines = []
                skipping_end_matter = False
                inside_appendix = folded.startswith(("appendix", "appendices"))
            continue

        if in_references:
            if _PRESERVED_POST_REFERENCE_HEADER.fullmatch(stripped):
                current = Section(heading=stripped[:120], level=1, content="")
                current_lines = []
                in_references = False
                inside_appendix = folded.startswith(("appendix", "appendices"))
            elif _SKIPPED_POST_REFERENCE_HEADER.fullmatch(stripped):
                skipping_end_matter = True
                in_references = False
            continue

        # Detect reference section
        if line_index == reference_header_index:
            append_current()
            in_references = True
            past_references = True
            skipping_abstract = False
            continue

        if past_references and _SKIPPED_POST_REFERENCE_HEADER.fullmatch(stripped):
            append_current()
            skipping_end_matter = True
            continue

        if ABSTRACT_HEADERS.fullmatch(stripped):
            skipping_abstract = True
            continue

        # Detect section headers
        sec_match = _looks_like_section_heading(
            stripped,
            allow_generic_numbered=not inside_appendix,
        )
        if sec_match and repeated_heading_counts.get(folded, 0) > 2:
            sec_match = False
        if sec_match and len(stripped) < 150:
            if skipping_abstract:
                normalized_heading = re.sub(
                    r"^(?:\d+(?:\.\d+)*\.?\s+|[IVX]+\.\s+)",
                    "",
                    stripped,
                    flags=re.IGNORECASE,
                ).casefold()
                # Structured abstracts commonly contain Background / Methods /
                # Results / Conclusions subheads.  Do not mistake those for the
                # paper body; a numbered section or Introduction is a reliable
                # boundary.
                if not (
                    re.match(r"^(?:\d+(?:\.\d+)*\.?\s+|[IVX]+\.\s+)", stripped)
                    or normalized_heading.startswith("introduction")
                ):
                    continue
            append_current()
            # Determine level from numbering
            level = 1
            numbering = re.match(r"^(\d+(?:\.\d+)+)", stripped)
            if numbering:
                level = min(numbering.group(1).count(".") + 1, 3)
            current = Section(heading=stripped[:120], level=level, content="")
            current_lines = []
            skipping_abstract = False
            inside_appendix = folded.startswith(("appendix", "appendices"))
            continue

        if skipping_abstract:
            # Structured metadata already emitted the abstract. Skip matching
            # HTML/PDF abstract lines, but resume on the first line that is not
            # part of that known abstract. This preserves unheaded article
            # bodies such as Nature research articles.
            if folded and folded in normalized_abstract:
                continue
            skipping_abstract = False
        current_lines.append(stripped)

    append_current()

    return sections


def _extract_references(text: str) -> str:
    """Extract references section from text."""
    lines = text.splitlines()
    header_index = _reference_header_line_index(lines)
    if header_index is None:
        return ""

    ref_text = "\n".join(lines[header_index + 1 :])
    post_references = POST_REFERENCE_HEADER.search(ref_text)
    if post_references:
        ref_text = ref_text[: post_references.start()]
    ref_text = _clean_references(ref_text)
    if len(ref_text) <= _MAX_REFERENCE_CHARS:
        return ref_text

    marker = _REFERENCE_TRUNCATION_MARKER
    content_budget = _MAX_REFERENCE_CHARS - len(marker) - 2
    prefix = ref_text[:content_budget]
    minimum_boundary = content_budget // 2
    boundary = prefix.rfind("\n\n")
    if boundary < minimum_boundary:
        boundary = prefix.rfind("\n")
    if boundary < minimum_boundary:
        sentence_boundaries = [
            match.end()
            for match in re.finditer(r"[.!?](?:\s+|$)", prefix)
        ]
        boundary = sentence_boundaries[-1] if sentence_boundaries else -1
    if boundary < minimum_boundary:
        boundary = prefix.rfind(" ")
    if boundary <= 0:
        boundary = content_budget
    return f"{prefix[:boundary].rstrip()}\n\n{marker}"


def _clean_references(value: str) -> str:
    """Remove publisher link controls while preserving bibliographic prose."""
    lines: list[str] = []
    for raw_line in value.strip().splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line or _REFERENCE_UI_LINE.fullmatch(line) or _REFERENCE_UI_ONLY.fullmatch(line):
            continue
        line = _REFERENCE_UI_BRACKET.sub("", line)
        line = _REFERENCE_UI_SEQUENCE.sub(" ", line)
        line = _REFERENCE_UI_TAIL.sub("", line).rstrip()
        line = re.sub(r"\[\s*\]", "", line)
        line = re.sub(r"(?:(?<=\s)|^)[\[\]](?=\s|$)", " ", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _looks_like_title(line: str) -> bool:
    """Heuristic: does this line look like a paper title?"""
    return len(line.split()) >= 3 and len(line) < 200 and line[0].isupper()


def _looks_like_authors(text: str) -> bool:
    """Heuristic: does this text look like an author list?"""
    return ", " in text and len(text) < 300 and text.count(",") >= 1

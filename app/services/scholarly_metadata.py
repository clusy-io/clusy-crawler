from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from html import unescape
from typing import Any, Literal
from urllib.parse import quote, unquote, urlparse

import httpx
import structlog

from app.config import settings
from app.lib.http_client import get_http_client
from app.services import fetcher as fetcher_module
from app.services.academic import (
    DOI_PATTERN,
    AcademicPaper,
    classify_academic_url,
    extract_academic_doi,
    normalize_doi,
)
from app.services.rate_limiter import get_rate_limiter

logger = structlog.get_logger()

_CROSSREF_HOST = "api.crossref.org"
_ELSEVIER_HOST = "api.elsevier.com"
_IEEE_HOST = "ieeexploreapi.ieee.org"
_ALLOWED_API_HOSTS = frozenset({_CROSSREF_HOST, _ELSEVIER_HOST, _IEEE_HOST})

_PII_PATTERN = re.compile(r"[A-Z][A-Z0-9]{9,39}", re.ASCII)
_IEEE_DOCUMENT_PATTERN = re.compile(r"[1-9]\d{0,11}", re.ASCII)
_MARKUP_PATTERN = re.compile(r"<[^>]{0,500}>")
_SPACE_PATTERN = re.compile(r"\s+")
_TITLE_TOKEN_PATTERN = re.compile(r"[a-z0-9]+", re.ASCII)
_IEEE_TITLE_BRANDING = re.compile(
    r"\s*(?:[|–—-]\s*)?(?:ieee\s+xplore|ieee\s+journals?.*)$",
    re.IGNORECASE,
)
_UNTRUSTED_TITLE_MARKERS = re.compile(
    r"access denied|attention required|captcha|checking your browser|"
    r"document\s*$|enable javascript|human verification|ieee xplore\s*$|"
    r"page not found|request blocked|security check",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PublisherTarget:
    provider: Literal["crossref", "elsevier", "ieee"]
    identifier: str


@dataclass(frozen=True)
class ScholarlyMetadataResult:
    paper: AcademicPaper
    strategy: str


_metadata_semaphore: asyncio.Semaphore | None = None
_metadata_loop: asyncio.AbstractEventLoop | None = None


def _get_metadata_semaphore() -> asyncio.Semaphore:
    global _metadata_semaphore, _metadata_loop
    loop = asyncio.get_running_loop()
    if _metadata_semaphore is None or _metadata_loop is not loop:
        _metadata_semaphore = asyncio.Semaphore(settings.scholarly_metadata_max_concurrency)
        _metadata_loop = loop
    return _metadata_semaphore


def _safe_original_url(url: str) -> tuple[str, str] | None:
    """Return a normalized host/path only for an ordinary public HTTP URL.

    Identifier parsing never reads query parameters or fragments, and rejects
    credentials and non-default ports. This keeps an attacker-controlled crawl
    URL from becoming an arbitrary metadata-API request.
    """
    if not url or len(url) > 4096 or "\x00" in url:
        return None
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (
            port is not None
            and (
                (parsed.scheme.lower() == "http" and port != 80)
                or (parsed.scheme.lower() == "https" and port != 443)
            )
        )
    ):
        return None
    try:
        path = unquote(parsed.path, errors="strict")
    except (UnicodeDecodeError, ValueError):
        return None
    if len(path) > 2048 or any(ord(character) < 32 for character in path):
        return None
    return parsed.hostname.lower().rstrip("."), path


def _strict_doi(value: str) -> str:
    candidate = value.strip().strip("/")
    if not candidate or len(candidate) > 512:
        return ""
    if any(part in {".", ".."} for part in candidate.split("/")):
        return ""
    match = DOI_PATTERN.fullmatch(candidate)
    if not match:
        return ""
    return normalize_doi(match.group(0))


def classify_publisher_target(
    url: str,
    *,
    trusted_doi: str = "",
) -> PublisherTarget | None:
    """Extract an exact metadata target from a recognized scholarly URL.

    ``trusted_doi`` is reserved for a DOI already extracted from structured
    metadata on the fetched publisher document. It is never inferred from page
    prose or URL query parameters here.
    """
    parts = _safe_original_url(url)
    if parts is None:
        return None
    host, path = parts
    explicit_doi = _strict_doi(normalize_doi(trusted_doi))
    if explicit_doi and classify_academic_url(url) in {
        "arxiv",
        "pubmed",
        "pmc",
        "doi",
        "journal",
    }:
        return PublisherTarget("crossref", explicit_doi)

    if host in {"doi.org", "dx.doi.org"}:
        doi = _strict_doi(path)
        return PublisherTarget("crossref", doi) if doi else None

    if host == "dl.acm.org":
        match = re.fullmatch(
            r"/doi/(?:abs/|full(?:html)?/|pdf/)?(?P<doi>10\..+?)/?",
            path,
            re.IGNORECASE,
        )
        doi = _strict_doi(match.group("doi")) if match else ""
        return PublisherTarget("crossref", doi) if doi else None

    # Wiley, Science, Springer, PLOS, and many other publishers embed a DOI in
    # an otherwise provider-specific path. The academic helper enforces known
    # hostname boundaries and ignores query strings and visible prose.
    path_doi = _strict_doi(extract_academic_doi(url))
    if path_doi:
        return PublisherTarget("crossref", path_doi)

    if host in {"sciencedirect.com", "www.sciencedirect.com"}:
        match = re.fullmatch(
            r"/(?:science/article/)?pii/(?P<pii>[A-Za-z0-9]+)/?",
            path,
        )
        pii = match.group("pii").upper() if match else ""
        return PublisherTarget("elsevier", pii) if pii and _PII_PATTERN.fullmatch(pii) else None

    if host == "ieeexplore.ieee.org":
        match = re.fullmatch(r"/document/(?P<number>\d+)/?", path)
        number = match.group("number") if match else ""
        return (
            PublisherTarget("ieee", number)
            if number and _IEEE_DOCUMENT_PATTERN.fullmatch(number)
            else None
        )
    return None


def _clean_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = _MARKUP_PATTERN.sub(" ", unescape(value))
    return _SPACE_PATTERN.sub(" ", text).strip()[:limit]


def _first_text(value: Any, limit: int) -> str:
    if isinstance(value, list):
        for item in value:
            cleaned = _clean_text(item, limit)
            if cleaned:
                return cleaned
        return ""
    return _clean_text(value, limit)


def _date_from_parts(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    date_parts = value.get("date-parts")
    if (
        not isinstance(date_parts, list)
        or not date_parts
        or not isinstance(date_parts[0], list)
        or not date_parts[0]
    ):
        return ""
    parts: list[int] = []
    for raw_part in date_parts[0][:3]:
        if not isinstance(raw_part, int) or raw_part < 1:
            break
        parts.append(raw_part)
    if not parts:
        return ""
    output = str(parts[0])
    if len(parts) >= 2 and parts[1] <= 12:
        output += f"-{parts[1]:02d}"
    if len(parts) >= 3 and parts[2] <= 31:
        output += f"-{parts[2]:02d}"
    return output


def _crossref_authors(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    seen: set[str] = set()
    for entry in value[:100]:
        if not isinstance(entry, dict):
            continue
        name = _clean_text(entry.get("name"), 300)
        if not name:
            given = _clean_text(entry.get("given"), 150)
            family = _clean_text(entry.get("family"), 150)
            name = " ".join(part for part in (given, family) if part)
        key = name.casefold()
        if name and key not in seen:
            output.append(name)
            seen.add(key)
    return output


def _crossref_paper(message: Any, *, expected_doi: str = "") -> AcademicPaper | None:
    if not isinstance(message, dict):
        return None
    doi = normalize_doi(_clean_text(message.get("DOI"), 512))
    if expected_doi and doi.casefold() != expected_doi.casefold():
        return None
    title = _first_text(message.get("title"), 1000)
    if not doi or not title:
        return None
    publication_date = ""
    for key in ("published-print", "published-online", "published", "issued"):
        publication_date = _date_from_parts(message.get(key))
        if publication_date:
            break
    license_url = ""
    licenses = message.get("license")
    if isinstance(licenses, list):
        for entry in licenses[:10]:
            if not isinstance(entry, dict):
                continue
            candidate = _clean_text(entry.get("URL"), 1000)
            parsed = urlparse(candidate)
            if parsed.scheme in {"http", "https"} and parsed.hostname:
                license_url = candidate
                break
    return AcademicPaper(
        title=title,
        authors=_crossref_authors(message.get("author")),
        abstract=_clean_text(message.get("abstract"), 10_000),
        doi=doi,
        journal=_first_text(message.get("container-title"), 500),
        publication_date=publication_date,
        license=license_url,
        canonical_url=f"https://doi.org/{quote(doi, safe='/')}",
    )


def _elsevier_authors(core: dict[str, Any]) -> list[str]:
    raw_authors: Any = core.get("authors")
    if isinstance(raw_authors, dict):
        raw_authors = raw_authors.get("author")
    if not isinstance(raw_authors, list):
        creator = core.get("dc:creator")
        raw_authors = creator if isinstance(creator, list) else [creator]
    output: list[str] = []
    seen: set[str] = set()
    for entry in raw_authors[:100]:
        if isinstance(entry, dict):
            name = _clean_text(
                entry.get("$") or entry.get("ce:indexed-name") or entry.get("preferred-name"),
                300,
            )
            if not name and isinstance(entry.get("preferred-name"), dict):
                name = _clean_text(entry["preferred-name"].get("ce:indexed-name"), 300)
        else:
            name = _clean_text(entry, 300)
        key = name.casefold()
        if name and key not in seen:
            output.append(name)
            seen.add(key)
    return output


def _elsevier_paper(payload: Any, pii: str) -> AcademicPaper | None:
    if not isinstance(payload, dict):
        return None
    response = payload.get("full-text-retrieval-response")
    if not isinstance(response, dict):
        return None
    core = response.get("coredata")
    if not isinstance(core, dict):
        return None
    returned_pii = _clean_text(core.get("pii") or core.get("prism:pii"), 64).upper()
    if returned_pii != pii:
        return None
    title = _clean_text(core.get("dc:title"), 1000)
    doi = normalize_doi(_clean_text(core.get("prism:doi"), 512))
    if not title:
        return None
    canonical_url = (
        f"https://doi.org/{quote(doi, safe='/')}"
        if doi
        else f"https://www.sciencedirect.com/science/article/pii/{pii}"
    )
    return AcademicPaper(
        title=title,
        authors=_elsevier_authors(core),
        abstract=_clean_text(core.get("dc:description"), 10_000),
        doi=doi,
        journal=_clean_text(core.get("prism:publicationName"), 500),
        publication_date=_clean_text(
            core.get("prism:coverDate") or core.get("prism:coverDisplayDate"),
            64,
        ),
        canonical_url=canonical_url,
    )


def _ieee_authors(value: Any) -> list[str]:
    if isinstance(value, dict):
        value = value.get("authors")
    if not isinstance(value, list):
        return []
    output: list[str] = []
    seen: set[str] = set()
    for entry in value[:100]:
        name = (
            _clean_text(
                entry.get("full_name") or entry.get("author_name"),
                300,
            )
            if isinstance(entry, dict)
            else _clean_text(entry, 300)
        )
        key = name.casefold()
        if name and key not in seen:
            output.append(name)
            seen.add(key)
    return output


def _ieee_paper(payload: Any, article_number: str) -> AcademicPaper | None:
    if not isinstance(payload, dict):
        return None
    articles = payload.get("articles")
    if not isinstance(articles, list):
        return None
    for article in articles[:10]:
        if not isinstance(article, dict):
            continue
        returned_number = str(article.get("article_number") or "").strip()
        if returned_number != article_number:
            continue
        title = _clean_text(article.get("title") or article.get("article_title"), 1000)
        if not title:
            return None
        doi = normalize_doi(_clean_text(article.get("doi"), 512))
        canonical_url = (
            f"https://doi.org/{quote(doi, safe='/')}"
            if doi
            else f"https://ieeexplore.ieee.org/document/{article_number}"
        )
        return AcademicPaper(
            title=title,
            authors=_ieee_authors(article.get("authors")),
            abstract=_clean_text(article.get("abstract"), 10_000),
            doi=doi,
            journal=_clean_text(
                article.get("publication_title") or article.get("publisher"),
                500,
            ),
            publication_date=_clean_text(
                article.get("publication_date") or article.get("publication_year"),
                64,
            ),
            canonical_url=canonical_url,
        )
    return None


def _api_url_is_fixed(
    provider: Literal["crossref", "elsevier", "ieee"],
    url: str,
) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if not (
        parsed.scheme == "https"
        and parsed.hostname in _ALLOWED_API_HOSTS
        and parsed.username is None
        and parsed.password is None
        and parsed.port in {None, 443}
        and not parsed.query
        and not parsed.fragment
    ):
        return False
    provider_host = {
        "crossref": _CROSSREF_HOST,
        "elsevier": _ELSEVIER_HOST,
        "ieee": _IEEE_HOST,
    }[provider]
    if parsed.hostname != provider_host:
        return False
    if provider == "crossref":
        return parsed.path == "/works" or parsed.path.startswith("/works/")
    if provider == "elsevier":
        return parsed.path.startswith("/content/article/pii/")
    return parsed.path == "/api/v1/search/articles"


async def _request_json(
    provider: Literal["crossref", "elsevier", "ieee"],
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> Any | None:
    if not _api_url_is_fixed(provider, url):
        logger.warning("scholarly_metadata_request_rejected", provider=provider)
        return None
    public_error = await fetcher_module.validate_public_url(url)
    if public_error:
        logger.warning("scholarly_metadata_ssrf_blocked", provider=provider)
        return None

    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    try:
        async with asyncio.timeout(settings.scholarly_metadata_timeout_s):
            await get_rate_limiter().acquire(url)
            client = get_http_client()
            chunks: list[bytes] = []
            observed = 0
            async with client.stream(
                "GET",
                url,
                headers=request_headers,
                params=params,
                follow_redirects=False,
            ) as response:
                peer_error = fetcher_module._response_peer_error(response)
                if peer_error:
                    logger.warning(
                        "scholarly_metadata_ssrf_blocked",
                        provider=provider,
                    )
                    return None
                if not 200 <= response.status_code < 300:
                    logger.info(
                        "scholarly_metadata_status",
                        provider=provider,
                        status_code=response.status_code,
                    )
                    return None
                content_type = response.headers.get("content-type", "").lower()
                if "json" not in content_type:
                    logger.info(
                        "scholarly_metadata_invalid_content_type",
                        provider=provider,
                    )
                    return None
                declared = response.headers.get("content-length")
                if declared:
                    try:
                        if int(declared) > settings.scholarly_metadata_max_response_bytes:
                            logger.info(
                                "scholarly_metadata_response_too_large",
                                provider=provider,
                            )
                            return None
                    except ValueError:
                        pass
                async for chunk in response.aiter_bytes():
                    observed += len(chunk)
                    if observed > settings.scholarly_metadata_max_response_bytes:
                        logger.info(
                            "scholarly_metadata_response_too_large",
                            provider=provider,
                        )
                        return None
                    chunks.append(chunk)
    except (TimeoutError, httpx.TimeoutException):
        logger.info("scholarly_metadata_timeout", provider=provider)
        return None
    except httpx.TransportError as exc:
        logger.warning(
            "scholarly_metadata_transport_failed",
            provider=provider,
            error_type=type(exc).__name__,
        )
        return None
    except Exception as exc:
        logger.warning(
            "scholarly_metadata_request_failed",
            provider=provider,
            error_type=type(exc).__name__,
        )
        return None
    try:
        return json.loads(b"".join(chunks))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.info("scholarly_metadata_invalid_json", provider=provider)
        return None


async def _crossref_by_doi(doi: str) -> AcademicPaper | None:
    encoded_doi = quote(doi, safe="")
    payload = await _request_json(
        "crossref",
        f"https://{_CROSSREF_HOST}/works/{encoded_doi}",
    )
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return None
    return _crossref_paper(payload.get("message"), expected_doi=doi)


def _crossref_record_matches_elsevier_pii(message: Any, pii: str) -> bool:
    """Require an exact PII and an independently Elsevier-bound record."""
    if not isinstance(message, dict):
        return False
    alternative_ids = message.get("alternative-id")
    if not isinstance(alternative_ids, list) or not any(
        str(value).strip().upper() == pii for value in alternative_ids[:50]
    ):
        return False

    publisher = _normalized_title(_clean_text(message.get("publisher"), 500))
    if "elsevier" in publisher:
        return True

    urls: list[str] = []
    for key in ("URL", "url"):
        value = message.get(key)
        if isinstance(value, str):
            urls.append(value)
    resource = message.get("resource")
    primary = resource.get("primary") if isinstance(resource, dict) else None
    if isinstance(primary, dict) and isinstance(primary.get("URL"), str):
        urls.append(primary["URL"])
    links = message.get("link")
    if isinstance(links, list):
        for link in links[:20]:
            if isinstance(link, dict) and isinstance(link.get("URL"), str):
                urls.append(link["URL"])

    for value in urls:
        try:
            host = (urlparse(value).hostname or "").lower().rstrip(".")
        except ValueError:
            continue
        if host in {"elsevier.com", "sciencedirect.com"} or host.endswith(
            (".elsevier.com", ".sciencedirect.com")
        ):
            return True
    return False


async def _crossref_by_elsevier_pii(pii: str) -> AcademicPaper | None:
    """Resolve a ScienceDirect PII without guessing from a fuzzy first hit."""
    payload = await _request_json(
        "crossref",
        f"https://{_CROSSREF_HOST}/works",
        params={"filter": f"alternative-id:{pii}", "rows": "5"},
    )
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return None
    message = payload.get("message")
    items = message.get("items") if isinstance(message, dict) else None
    if not isinstance(items, list):
        return None

    matches: dict[str, AcademicPaper] = {}
    for item in items[:5]:
        if not _crossref_record_matches_elsevier_pii(item, pii):
            continue
        paper = _crossref_paper(item)
        if paper is not None:
            matches[paper.doi.casefold()] = paper
    return next(iter(matches.values())) if len(matches) == 1 else None


def _trusted_ieee_title(value: str) -> str:
    title = _SPACE_PATTERN.sub(" ", unescape(value)).strip()[:1000]
    title = _IEEE_TITLE_BRANDING.sub("", title).strip(" |–—-")
    tokens = _TITLE_TOKEN_PATTERN.findall(title.casefold())
    if len(title) < 12 or len(tokens) < 3 or _UNTRUSTED_TITLE_MARKERS.search(title):
        return ""
    return title


def _normalized_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", unescape(value)).casefold()
    return " ".join(_TITLE_TOKEN_PATTERN.findall(normalized))


def _titles_match(left: str, right: str) -> bool:
    normalized_left = _normalized_title(left)
    normalized_right = _normalized_title(right)
    if not normalized_left or not normalized_right:
        return False
    left_tokens = set(normalized_left.split())
    right_tokens = set(normalized_right.split())
    union = left_tokens | right_tokens
    token_jaccard = len(left_tokens & right_tokens) / len(union) if union else 0.0
    sequence_ratio = SequenceMatcher(
        None,
        normalized_left,
        normalized_right,
        autojunk=False,
    ).ratio()
    return sequence_ratio >= 0.92 and token_jaccard >= 0.85


def _crossref_record_is_ieee(message: Any) -> bool:
    if not isinstance(message, dict):
        return False
    doi = normalize_doi(_clean_text(message.get("DOI"), 512)).casefold()
    publisher = _normalized_title(_clean_text(message.get("publisher"), 500))
    publisher_is_ieee = "ieee" in publisher or (
        "institute of electrical and electronics engineers" in publisher
    )
    return doi.startswith("10.1109/") and publisher_is_ieee


def _crossref_record_matches_ieee_number(message: Any, article_number: str) -> bool:
    """Bind a Crossref title-search hit to the requested IEEE document number."""
    if not isinstance(message, dict):
        return False

    alternative_ids = message.get("alternative-id")
    if isinstance(alternative_ids, list):
        for value in alternative_ids[:20]:
            if str(value).strip() == article_number:
                return True

    resource = message.get("resource")
    primary = resource.get("primary") if isinstance(resource, dict) else None
    primary_url = primary.get("URL") if isinstance(primary, dict) else None
    if isinstance(primary_url, str):
        target = classify_publisher_target(primary_url)
        if (
            target is not None
            and target.provider == "ieee"
            and target.identifier == article_number
        ):
            return True
    return False


async def _crossref_by_ieee_title(
    title: str,
    article_number: str,
) -> AcademicPaper | None:
    trusted_title = _trusted_ieee_title(title)
    if not trusted_title:
        return None
    payload = await _request_json(
        "crossref",
        f"https://{_CROSSREF_HOST}/works",
        params={"query.title": trusted_title, "rows": "3"},
    )
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return None
    message = payload.get("message")
    items = message.get("items") if isinstance(message, dict) else None
    if not isinstance(items, list):
        return None
    matches: list[AcademicPaper] = []
    for item in items[:3]:
        if (
            not isinstance(item, dict)
            or not _crossref_record_is_ieee(item)
            or not _crossref_record_matches_ieee_number(item, article_number)
        ):
            continue
        candidate_title = _first_text(item.get("title"), 1000)
        if not _titles_match(trusted_title, candidate_title):
            continue
        paper = _crossref_paper(item)
        if paper is not None:
            matches.append(paper)
    # More than one high-confidence match is still ambiguous; never guess.
    return matches[0] if len(matches) == 1 else None


def _merge_missing(primary: AcademicPaper, enrichment: AcademicPaper) -> AcademicPaper:
    for field_name in (
        "title",
        "authors",
        "abstract",
        "doi",
        "journal",
        "publication_date",
        "license",
        "canonical_url",
    ):
        if not getattr(primary, field_name):
            setattr(primary, field_name, getattr(enrichment, field_name))
    return primary


async def _lookup_target(
    target: PublisherTarget,
    *,
    trusted_title: str,
) -> ScholarlyMetadataResult | None:
    if target.provider == "crossref":
        paper = await _crossref_by_doi(target.identifier)
        return (
            ScholarlyMetadataResult(paper, "academic-metadata-crossref")
            if paper is not None
            else None
        )

    if target.provider == "elsevier":
        api_key = settings.elsevier_api_key.strip()
        if not api_key:
            paper = await _crossref_by_elsevier_pii(target.identifier)
            return (
                ScholarlyMetadataResult(
                    paper,
                    "academic-metadata-elsevier-crossref-pii",
                )
                if paper is not None
                else None
            )
        payload = await _request_json(
            "elsevier",
            f"https://{_ELSEVIER_HOST}/content/article/pii/{target.identifier}",
            headers={"X-ELS-APIKey": api_key},
        )
        paper = _elsevier_paper(payload, target.identifier)
        if paper is None:
            paper = await _crossref_by_elsevier_pii(target.identifier)
            return (
                ScholarlyMetadataResult(
                    paper,
                    "academic-metadata-elsevier-crossref-pii",
                )
                if paper is not None
                else None
            )
        strategy = "academic-metadata-elsevier"
        if paper.doi:
            enrichment = await _crossref_by_doi(paper.doi)
            if enrichment is not None:
                paper = _merge_missing(paper, enrichment)
                strategy += "+crossref"
        return ScholarlyMetadataResult(paper, strategy)

    api_key = settings.ieee_api_key.strip()
    if api_key:
        payload = await _request_json(
            "ieee",
            f"https://{_IEEE_HOST}/api/v1/search/articles",
            params={
                "apikey": api_key,
                "article_number": target.identifier,
                "max_records": "1",
                "start_record": "1",
            },
        )
        paper = _ieee_paper(payload, target.identifier)
        return (
            ScholarlyMetadataResult(paper, "academic-metadata-ieee") if paper is not None else None
        )

    paper = await _crossref_by_ieee_title(trusted_title, target.identifier)
    return (
        ScholarlyMetadataResult(paper, "academic-metadata-ieee-crossref-title")
        if paper is not None
        else None
    )


async def lookup_publisher_metadata(
    url: str,
    *,
    trusted_title: str = "",
    trusted_doi: str = "",
) -> ScholarlyMetadataResult | None:
    """Return a bounded metadata-only fallback for a recognized publisher URL.

    Callers remain responsible for trying the publisher page first and may pass
    ``trusted_doi=extract_academic_doi(url, fetched_html)``. This function never
    returns article full text and never raises provider failures.
    """
    if not settings.scholarly_metadata_enabled:
        return None
    target = classify_publisher_target(url, trusted_doi=trusted_doi)
    if target is None:
        return None
    try:
        async with asyncio.timeout(settings.scholarly_metadata_timeout_s):
            async with _get_metadata_semaphore():
                return await _lookup_target(target, trusted_title=trusted_title)
    except TimeoutError:
        logger.info(
            "scholarly_metadata_timeout",
            provider=target.provider,
        )
        return None
    except Exception as exc:
        logger.warning(
            "scholarly_metadata_lookup_failed",
            provider=target.provider,
            error_type=type(exc).__name__,
        )
        return None

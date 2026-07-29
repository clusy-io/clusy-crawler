from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings, settings
from app.services import crawler as crawler_mod
from app.services import scholarly_metadata as metadata_mod
from app.services.academic import AcademicPaper
from app.services.fetcher import FetchResult
from app.services.scholarly_metadata import (
    ScholarlyMetadataResult,
    classify_publisher_target,
    lookup_publisher_metadata,
)

_TEST_IMAGE_DIGEST = "sha256:" + ("a" * 64)
_TEST_FINGERPRINT_KEY = "fingerprint-test-key-" + ("f" * 32)


@pytest.fixture
def allow_fixed_api_dns(monkeypatch):
    async def allow(_url: str) -> None:
        return None

    monkeypatch.setattr(metadata_mod.fetcher_module, "validate_public_url", allow)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    "url",
    [
        "https://dl.acm.org.attacker.test/doi/10.1145/123.456",
        "https://attacker.test/?doi=https://doi.org/10.1145/123.456",
        "https://user@dl.acm.org/doi/10.1145/123.456",
        "https://dl.acm.org:444/doi/10.1145/123.456",
        "https://dl.acm.org/doi/not-a-doi",
        "https://www.sciencedirect.com/science/article/pii/../../admin",
        "https://www.sciencedirect.com/science/article/pii/S12%2Fetc",
        "https://ieeexplore.ieee.org/document/9478947/other",
        "javascript:https://doi.org/10.1145/123.456",
    ],
)
def test_publisher_target_rejects_malicious_or_ambiguous_urls(url: str) -> None:
    assert classify_publisher_target(url) is None


@pytest.mark.parametrize(
    ("url", "provider", "identifier"),
    [
        (
            "https://doi.org/10.1145/3581783.3611715?utm_source=test",
            "crossref",
            "10.1145/3581783.3611715",
        ),
        (
            "https://dl.acm.org/doi/abs/10.1145/3581783.3611715",
            "crossref",
            "10.1145/3581783.3611715",
        ),
        (
            "https://onlinelibrary.wiley.com/doi/10.1002/cae.22725",
            "crossref",
            "10.1002/cae.22725",
        ),
        (
            "https://www.science.org/doi/10.1126/science.1225829",
            "crossref",
            "10.1126/science.1225829",
        ),
        (
            "https://www.sciencedirect.com/science/article/pii/S0167739X23001234",
            "elsevier",
            "S0167739X23001234",
        ),
        (
            "https://ieeexplore.ieee.org/document/9478947/",
            "ieee",
            "9478947",
        ),
    ],
)
def test_publisher_target_extracts_only_strict_path_identifiers(
    url: str,
    provider: str,
    identifier: str,
) -> None:
    target = classify_publisher_target(url)

    assert target is not None
    assert target.provider == provider
    assert target.identifier == identifier


def test_trusted_structured_doi_overrides_provider_specific_identifier() -> None:
    target = classify_publisher_target(
        "https://www.sciencedirect.com/science/article/pii/S0167739X23001234",
        trusted_doi="doi:10.1016/j.future.2023.01.001",
    )

    assert target is not None
    assert target.provider == "crossref"
    assert target.identifier == "10.1016/j.future.2023.01.001"
    assert (
        classify_publisher_target(
            "https://attacker.test/paper",
            trusted_doi="10.1016/j.future.2023.01.001",
        )
        is None
    )


@pytest.mark.anyio
async def test_crossref_exact_doi_success_maps_bibliographic_metadata(
    monkeypatch,
    allow_fixed_api_dns,
) -> None:
    doi = "10.1145/3581783.3611715"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.crossref.org"
        assert request.url.raw_path == b"/works/10.1145%2F3581783.3611715"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "status": "ok",
                "message": {
                    "DOI": doi,
                    "title": ["A Credible Systems Result"],
                    "author": [
                        {"given": "Ada", "family": "Lovelace"},
                        {"name": "Reliable Consortium"},
                    ],
                    "abstract": "<jats:p>A bounded metadata abstract.</jats:p>",
                    "container-title": ["Proceedings of a Conference"],
                    "published-online": {"date-parts": [[2024, 7, 9]]},
                    "license": [{"URL": "https://creativecommons.org/licenses/by/4.0/"}],
                },
            },
        )

    client = _client(handler)
    monkeypatch.setattr(metadata_mod, "get_http_client", lambda: client)
    try:
        result = await lookup_publisher_metadata(
            f"https://dl.acm.org/doi/{doi}",
        )
    finally:
        await client.aclose()

    assert result is not None
    assert result.strategy == "academic-metadata-crossref"
    assert result.paper.title == "A Credible Systems Result"
    assert result.paper.authors == ["Ada Lovelace", "Reliable Consortium"]
    assert result.paper.abstract == "A bounded metadata abstract."
    assert result.paper.doi == doi
    assert result.paper.journal == "Proceedings of a Conference"
    assert result.paper.publication_date == "2024-07-09"
    assert result.paper.full_text == ""


@pytest.mark.anyio
@pytest.mark.parametrize(
    "url",
    [
        "https://onlinelibrary.wiley.com/doi/10.1002/cae.22725",
        "https://www.science.org/doi/10.1002/cae.22725",
    ],
)
async def test_known_publisher_doi_paths_use_exact_crossref_lookup(
    monkeypatch,
    allow_fixed_api_dns,
    url: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path == b"/works/10.1002%2Fcae.22725"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "status": "ok",
                "message": {
                    "DOI": "10.1002/cae.22725",
                    "title": ["A Publisher-Independent Fallback"],
                },
            },
        )

    client = _client(handler)
    monkeypatch.setattr(metadata_mod, "get_http_client", lambda: client)
    try:
        result = await lookup_publisher_metadata(url)
    finally:
        await client.aclose()

    assert result is not None
    assert result.strategy == "academic-metadata-crossref"
    assert result.paper.title == "A Publisher-Independent Fallback"


@pytest.mark.anyio
async def test_structured_html_doi_can_drive_exact_crossref_fallback(
    monkeypatch,
    allow_fixed_api_dns,
) -> None:
    doi = "10.1016/j.future.2024.01.007"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path == b"/works/10.1016%2Fj.future.2024.01.007"
        assert "filter" not in request.url.params
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "status": "ok",
                "message": {
                    "DOI": doi,
                    "title": ["Structured DOI Wins Over a PII Search"],
                },
            },
        )

    client = _client(handler)
    monkeypatch.setattr(metadata_mod, "get_http_client", lambda: client)
    try:
        result = await lookup_publisher_metadata(
            "https://www.sciencedirect.com/science/article/pii/S0167739X24000001",
            trusted_doi=doi,
        )
    finally:
        await client.aclose()

    assert result is not None
    assert result.strategy == "academic-metadata-crossref"
    assert result.paper.doi == doi


@pytest.mark.anyio
async def test_elsevier_pii_uses_key_header_and_crossref_enrichment(
    monkeypatch,
    allow_fixed_api_dns,
) -> None:
    pii = "S0167739X23001234"
    key = "elsevier-secret-value"
    monkeypatch.setattr(settings, "elsevier_api_key", key)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.elsevier.com":
            assert request.headers["X-ELS-APIKey"] == key
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "full-text-retrieval-response": {
                        "coredata": {
                            "pii": pii,
                            "dc:title": "Safe PII Retrieval",
                            "prism:doi": "10.1016/j.future.2023.01.001",
                            "prism:publicationName": "Future Systems",
                            "prism:coverDate": "2024-01-02",
                        },
                        "originalText": "full text must never be mapped",
                    }
                },
            )
        assert request.url.host == "api.crossref.org"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "status": "ok",
                "message": {
                    "DOI": "10.1016/j.future.2023.01.001",
                    "title": ["Safe PII Retrieval"],
                    "author": [{"given": "Grace", "family": "Hopper"}],
                    "abstract": "<p>Crossref abstract enrichment.</p>",
                },
            },
        )

    client = _client(handler)
    monkeypatch.setattr(metadata_mod, "get_http_client", lambda: client)
    try:
        result = await lookup_publisher_metadata(
            f"https://www.sciencedirect.com/science/article/pii/{pii}",
        )
    finally:
        await client.aclose()

    assert result is not None
    assert result.strategy == "academic-metadata-elsevier+crossref"
    assert result.paper.authors == ["Grace Hopper"]
    assert result.paper.abstract == "Crossref abstract enrichment."
    assert result.paper.full_text == ""
    assert "full text must never be mapped" not in result.paper.to_markdown()


@pytest.mark.anyio
async def test_elsevier_rejects_response_without_exact_requested_pii(
    monkeypatch,
    allow_fixed_api_dns,
) -> None:
    monkeypatch.setattr(settings, "elsevier_api_key", "configured")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "full-text-retrieval-response": {
                    "coredata": {
                        "dc:title": "Unbound Elsevier Result",
                        "prism:doi": "10.1016/j.future.2023.01.001",
                    }
                }
            },
        )

    client = _client(handler)
    monkeypatch.setattr(metadata_mod, "get_http_client", lambda: client)
    try:
        result = await lookup_publisher_metadata(
            "https://www.sciencedirect.com/science/article/pii/S0167739X23001234",
        )
    finally:
        await client.aclose()

    assert result is None


@pytest.mark.anyio
async def test_elsevier_pii_without_key_accepts_unique_exact_crossref_binding(
    monkeypatch,
    allow_fixed_api_dns,
) -> None:
    pii = "S0167739X23001234"
    monkeypatch.setattr(settings, "elsevier_api_key", "")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.crossref.org"
        assert request.url.params["filter"] == f"alternative-id:{pii}"
        assert request.url.params["rows"] == "5"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "status": "ok",
                "message": {
                    "items": [
                        {
                            "DOI": "10.1016/j.future.2023.01.001",
                            "title": ["Strictly Bound ScienceDirect Metadata"],
                            "publisher": "Elsevier BV",
                            "alternative-id": [pii],
                        },
                        {
                            "DOI": "10.9999/unrelated",
                            "title": ["A fuzzy but unbound search hit"],
                            "publisher": "Elsevier BV",
                            "alternative-id": ["DIFFERENT-PII"],
                        },
                    ]
                },
            },
        )

    client = _client(handler)
    monkeypatch.setattr(metadata_mod, "get_http_client", lambda: client)
    try:
        result = await lookup_publisher_metadata(
            f"https://www.sciencedirect.com/science/article/pii/{pii}",
        )
    finally:
        await client.aclose()

    assert result is not None
    assert result.strategy == "academic-metadata-elsevier-crossref-pii"
    assert result.paper.doi == "10.1016/j.future.2023.01.001"


@pytest.mark.anyio
@pytest.mark.parametrize("failure_mode", ["unbound", "ambiguous"])
async def test_elsevier_pii_crossref_fallback_rejects_unbound_or_ambiguous_hits(
    monkeypatch,
    allow_fixed_api_dns,
    failure_mode: str,
) -> None:
    pii = "S0167739X23001234"
    monkeypatch.setattr(settings, "elsevier_api_key", "")
    items = [
        {
            "DOI": "10.1016/j.future.2023.01.001",
            "title": ["First candidate"],
            "publisher": "Unrelated Publisher",
            "alternative-id": [pii],
        }
    ]
    if failure_mode == "ambiguous":
        items = [
            {
                "DOI": "10.1016/j.future.2023.01.001",
                "title": ["First candidate"],
                "publisher": "Elsevier BV",
                "alternative-id": [pii],
            },
            {
                "DOI": "10.1016/j.future.2023.01.002",
                "title": ["Second candidate"],
                "publisher": "Elsevier BV",
                "alternative-id": [pii],
            },
        ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"status": "ok", "message": {"items": items}},
        )

    client = _client(handler)
    monkeypatch.setattr(metadata_mod, "get_http_client", lambda: client)
    try:
        result = await lookup_publisher_metadata(
            f"https://www.sciencedirect.com/science/article/pii/{pii}",
        )
    finally:
        await client.aclose()

    assert result is None


@pytest.mark.anyio
async def test_ieee_api_key_exact_article_mapping(
    monkeypatch,
    allow_fixed_api_dns,
) -> None:
    key = "ieee-secret-value"
    monkeypatch.setattr(settings, "ieee_api_key", key)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "ieeexploreapi.ieee.org"
        assert request.url.params["apikey"] == key
        assert request.url.params["article_number"] == "9478947"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "articles": [
                    {
                        "article_number": "9478947",
                        "title": "Verified IEEE Metadata",
                        "doi": "10.1109/TKDE.2021.3091234",
                        "authors": {
                            "authors": [
                                {"full_name": "Ada Researcher"},
                                {"full_name": "Grace Scientist"},
                            ]
                        },
                        "abstract": "The official API abstract.",
                        "publication_title": "IEEE Transactions on Knowledge",
                        "publication_year": "2022",
                    }
                ]
            },
        )

    client = _client(handler)
    monkeypatch.setattr(metadata_mod, "get_http_client", lambda: client)
    try:
        result = await lookup_publisher_metadata("https://ieeexplore.ieee.org/document/9478947")
    finally:
        await client.aclose()

    assert result is not None
    assert result.strategy == "academic-metadata-ieee"
    assert result.paper.title == "Verified IEEE Metadata"
    assert result.paper.authors == ["Ada Researcher", "Grace Scientist"]
    assert result.paper.full_text == ""


@pytest.mark.anyio
async def test_ieee_without_key_requires_unique_high_similarity_crossref_match(
    monkeypatch,
    allow_fixed_api_dns,
) -> None:
    monkeypatch.setattr(settings, "ieee_api_key", "")
    title = "Learning Reliable Representations for Production Crawlers"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["query.title"] == title
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "status": "ok",
                "message": {
                    "items": [
                        {
                            "DOI": "10.1109/TKDE.2024.1234567",
                            "title": [title],
                            "publisher": (
                                "Institute of Electrical and Electronics Engineers (IEEE)"
                            ),
                            "container-title": ["IEEE Transactions on Knowledge"],
                            "resource": {
                                "primary": {
                                    "URL": "https://ieeexplore.ieee.org/document/9478947/"
                                }
                            },
                        }
                    ]
                },
            },
        )

    client = _client(handler)
    monkeypatch.setattr(metadata_mod, "get_http_client", lambda: client)
    try:
        result = await lookup_publisher_metadata(
            "https://ieeexplore.ieee.org/document/9478947",
            trusted_title=f"{title} | IEEE Xplore",
        )
    finally:
        await client.aclose()

    assert result is not None
    assert result.strategy == "academic-metadata-ieee-crossref-title"
    assert result.paper.title == title


@pytest.mark.anyio
async def test_ieee_title_match_without_article_number_binding_is_rejected(
    monkeypatch,
    allow_fixed_api_dns,
) -> None:
    monkeypatch.setattr(settings, "ieee_api_key", "")
    title = "Learning Reliable Representations for Production Crawlers"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "status": "ok",
                "message": {
                    "items": [
                        {
                            "DOI": "10.1109/TKDE.2024.1234567",
                            "title": [title],
                            "publisher": (
                                "Institute of Electrical and Electronics Engineers (IEEE)"
                            ),
                            "resource": {
                                "primary": {
                                    "URL": "https://ieeexplore.ieee.org/document/1234567/"
                                }
                            },
                        }
                    ]
                },
            },
        )

    client = _client(handler)
    monkeypatch.setattr(metadata_mod, "get_http_client", lambda: client)
    try:
        result = await lookup_publisher_metadata(
            "https://ieeexplore.ieee.org/document/9478947",
            trusted_title=title,
        )
    finally:
        await client.aclose()

    assert result is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("candidate_title", "publisher", "doi"),
    [
        (
            "A Completely Different Paper with Unrelated Results",
            "Institute of Electrical and Electronics Engineers (IEEE)",
            "10.1109/TKDE.2024.1234567",
        ),
        (
            "Learning Reliable Representations for Production Crawlers",
            "Elsevier BV",
            "10.1016/j.future.2024.01.001",
        ),
    ],
)
async def test_ieee_crossref_title_fallback_rejects_wrong_matches(
    monkeypatch,
    allow_fixed_api_dns,
    candidate_title: str,
    publisher: str,
    doi: str,
) -> None:
    monkeypatch.setattr(settings, "ieee_api_key", "")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "status": "ok",
                "message": {
                    "items": [
                        {
                            "DOI": doi,
                            "title": [candidate_title],
                            "publisher": publisher,
                        }
                    ]
                },
            },
        )

    client = _client(handler)
    monkeypatch.setattr(metadata_mod, "get_http_client", lambda: client)
    try:
        result = await lookup_publisher_metadata(
            "https://ieeexplore.ieee.org/document/9478947",
            trusted_title="Learning Reliable Representations for Production Crawlers",
        )
    finally:
        await client.aclose()

    assert result is None


@pytest.mark.anyio
async def test_metadata_response_body_is_strictly_bounded(
    monkeypatch,
    allow_fixed_api_dns,
) -> None:
    monkeypatch.setattr(settings, "scholarly_metadata_max_response_bytes", 32)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"status":"ok","message":"' + (b"x" * 100) + b'"}',
        )

    client = _client(handler)
    monkeypatch.setattr(metadata_mod, "get_http_client", lambda: client)
    try:
        result = await lookup_publisher_metadata("https://doi.org/10.1145/123.456")
    finally:
        await client.aclose()

    assert result is None


@pytest.mark.anyio
async def test_metadata_queue_wait_is_inside_total_deadline(monkeypatch) -> None:
    monkeypatch.setattr(settings, "scholarly_metadata_timeout_s", 0.01)
    monkeypatch.setattr(metadata_mod, "_metadata_semaphore", asyncio.Semaphore(0))
    monkeypatch.setattr(metadata_mod, "_metadata_loop", asyncio.get_running_loop())

    result = await asyncio.wait_for(
        lookup_publisher_metadata("https://doi.org/10.1145/123.456"),
        timeout=0.1,
    )

    assert result is None


class _RecordingLogger:
    def __init__(self) -> None:
        self.records: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def info(self, *args: Any, **kwargs: Any) -> None:
        self.records.append((args, kwargs))

    def warning(self, *args: Any, **kwargs: Any) -> None:
        self.records.append((args, kwargs))


@pytest.mark.anyio
async def test_429_timeout_and_transport_logs_do_not_leak_keys_or_bodies(
    monkeypatch,
    allow_fixed_api_dns,
) -> None:
    secret = "never-log-this-api-key"
    secret_body = "never-log-this-response-body"
    recorder = _RecordingLogger()
    monkeypatch.setattr(metadata_mod, "logger", recorder)
    monkeypatch.setattr(settings, "ieee_api_key", secret)

    def rate_limited(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"content-type": "application/json"},
            content=secret_body.encode(),
        )

    client = _client(rate_limited)
    monkeypatch.setattr(metadata_mod, "get_http_client", lambda: client)
    try:
        assert (
            await lookup_publisher_metadata("https://ieeexplore.ieee.org/document/9478947") is None
        )
    finally:
        await client.aclose()

    def transport_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"provider echoed {secret} and {secret_body}",
            request=request,
        )

    client = _client(transport_failure)
    monkeypatch.setattr(metadata_mod, "get_http_client", lambda: client)
    try:
        assert (
            await lookup_publisher_metadata("https://ieeexplore.ieee.org/document/9478947") is None
        )
    finally:
        await client.aclose()

    async def slow_handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={},
        )

    monkeypatch.setattr(settings, "scholarly_metadata_timeout_s", 0.01)
    client = _client(slow_handler)
    monkeypatch.setattr(metadata_mod, "get_http_client", lambda: client)
    try:
        assert (
            await lookup_publisher_metadata("https://ieeexplore.ieee.org/document/9478947") is None
        )
    finally:
        await client.aclose()

    logged = repr(recorder.records)
    assert secret not in logged
    assert secret_body not in logged
    assert "ieeexploreapi.ieee.org" not in logged


@pytest.mark.anyio
async def test_crawler_preserves_original_fetch_error_when_lookup_fails(
    monkeypatch,
) -> None:
    url = "https://dl.acm.org/doi/10.1145/123.456"

    async def fetch(*_args, **_kwargs):
        return FetchResult(
            error="HTTP 403",
            status_code=403,
            final_url=url,
        )

    async def missing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(crawler_mod.fetcher_module, "fetch_url", fetch)
    monkeypatch.setattr(
        crawler_mod.scholarly_metadata_module,
        "lookup_publisher_metadata",
        missing,
    )
    monkeypatch.setattr(crawler_mod, "_crawl_semaphore", None)

    result = await crawler_mod._crawl_uncached(
        url=url,
        decide_js=False,
        auto_render=False,
        wait_for_selector=None,
        word_count_threshold=10,
        extraction_profile="balanced",
    )

    assert result.error == "HTTP 403"
    assert result.markdown == ""


@pytest.mark.anyio
async def test_crawler_labels_metadata_only_fallback_and_omits_blocked_html(
    monkeypatch,
) -> None:
    url = "https://dl.acm.org/doi/10.1145/123.456"

    async def fetch(*_args, **_kwargs):
        return FetchResult(
            error="HTTP 403",
            status_code=403,
            final_url=url,
            html="<html>secret bot-wall body</html>",
        )

    async def found(*_args, **_kwargs):
        return ScholarlyMetadataResult(
            AcademicPaper(
                title="Metadata Is Not Full Text",
                abstract="A trustworthy bibliographic abstract.",
                doi="10.1145/123.456",
                canonical_url="https://doi.org/10.1145/123.456",
            ),
            "academic-metadata-crossref",
        )

    monkeypatch.setattr(crawler_mod.fetcher_module, "fetch_url", fetch)
    monkeypatch.setattr(
        crawler_mod.scholarly_metadata_module,
        "lookup_publisher_metadata",
        found,
    )
    monkeypatch.setattr(crawler_mod, "_crawl_semaphore", None)

    result = await crawler_mod._crawl_uncached(
        url=url,
        decide_js=False,
        auto_render=False,
        wait_for_selector=None,
        word_count_threshold=10,
        extraction_profile="balanced",
    )

    assert result.error is None
    assert result.html is None
    assert result.metadata is not None
    assert result.metadata.extraction_strategy == "academic-metadata-crossref"
    assert result.metadata.content_scope == "metadata_only"
    assert result.metadata.origin_status_code == 403
    assert result.metadata.origin_error == "HTTP 403"
    assert result.metadata.doi == "10.1145/123.456"
    assert result.metadata.pipeline_revision == "clusy-extraction-v2"
    assert result.metadata.extraction_route == "scholarly_metadata_fallback"
    assert result.metadata.route_reasons == [
        "publisher_full_text_unavailable",
        "metadata_only",
    ]
    assert result.metadata.completeness_score == 0.0
    assert result.metadata.completeness_coverage == "output_only"
    assert result.metadata.source_coverage_score is None
    assert result.metadata.output_grounding_score is None
    assert result.metadata.cache_status == "live"
    assert set(result.metadata.stage_timings_ms) == {
        "queue",
        "fetch",
        "render",
        "extraction",
        "total",
    }
    assert result.markdown.startswith("> **Metadata-only record:**")
    assert "article full text were not retrieved" in result.markdown
    assert "secret bot-wall body" not in result.markdown


def test_prod_rejects_disabled_chromium_sandbox() -> None:
    with pytest.raises(ValidationError, match="PLAYWRIGHT_DISABLE_SANDBOX"):
        Settings(
            environment="prod",
            crawler_api_token="configured",
            serving_fingerprint_key=_TEST_FINGERPRINT_KEY,
            image_digest=_TEST_IMAGE_DIGEST,
            playwright_enabled=True,
            playwright_disable_sandbox=True,
            _env_file=None,
        )


def test_prod_allows_static_only_mode_without_chromium_sandbox() -> None:
    configured = Settings(
        environment="prod",
        crawler_api_token="configured",
        serving_fingerprint_key=_TEST_FINGERPRINT_KEY,
        image_digest=_TEST_IMAGE_DIGEST,
        playwright_enabled=False,
        playwright_disable_sandbox=True,
        _env_file=None,
    )

    assert configured.environment == "prod"


def test_prod_redis_requires_immutable_build_revision() -> None:
    with pytest.raises(ValidationError, match="GIT_SHA"):
        Settings(
            environment="prod",
            crawler_api_token="configured",
            serving_fingerprint_key=_TEST_FINGERPRINT_KEY,
            image_digest=_TEST_IMAGE_DIGEST,
            redis_url="redis://cache.internal/0",
            git_sha="unknown",
            _env_file=None,
        )

    configured = Settings(
        environment="prod",
        crawler_api_token="configured",
        serving_fingerprint_key=_TEST_FINGERPRINT_KEY,
        image_digest=_TEST_IMAGE_DIGEST,
        redis_url="redis://cache.internal/0",
        git_sha="0123456789abcdef",
        _env_file=None,
    )
    assert configured.git_sha == "0123456789abcdef"


def test_image_digest_allows_unknown_but_rejects_malformed_identity() -> None:
    configured = Settings(
        environment="prod",
        crawler_api_token="configured",
        serving_fingerprint_key=_TEST_FINGERPRINT_KEY,
        image_digest="unknown",
        _env_file=None,
    )
    assert configured.image_digest == "unknown"

    with pytest.raises(ValidationError):
        Settings(
            image_digest="sha256:not-a-digest",
            _env_file=None,
        )


def test_prod_requires_independent_strong_fingerprint_key() -> None:
    with pytest.raises(ValidationError, match="SERVING_FINGERPRINT_KEY"):
        Settings(
            environment="prod",
            crawler_api_token="configured",
            serving_fingerprint_key="",
            _env_file=None,
        )

    with pytest.raises(ValidationError, match="at least 32"):
        Settings(
            serving_fingerprint_key="weak",
            _env_file=None,
        )


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (" " * 32, "must not contain whitespace"),
        ("ab" * 32, "insufficient character diversity"),
    ],
)
def test_supplied_fingerprint_key_rejects_obvious_weak_values(
    value: str,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(
            serving_fingerprint_key=value,
            _env_file=None,
        )


def test_invalid_fingerprint_key_is_hidden_in_validation_errors() -> None:
    malformed_secret = "leak-me-" + ("x" * 40) + " "

    with pytest.raises(ValidationError) as captured:
        Settings(
            serving_fingerprint_key=malformed_secret,
            _env_file=None,
        )

    assert malformed_secret not in str(captured.value)
    assert "input_value" not in str(captured.value)


def test_local_empty_fingerprint_key_uses_development_fallback() -> None:
    configured = Settings(
        serving_fingerprint_key="",
        _env_file=None,
    )

    assert configured.serving_fingerprint_key == ""


def test_prod_fingerprint_key_must_not_reuse_bearer_token() -> None:
    reused = "0123456789abcdef" * 4

    with pytest.raises(ValidationError, match="must differ"):
        Settings(
            environment="prod",
            crawler_api_token=reused,
            serving_fingerprint_key=reused,
            _env_file=None,
        )


def test_prod_unicode_fingerprint_reuse_is_rejected_without_type_error() -> None:
    reused = "高熵密钥甲乙丙丁" * 4

    with pytest.raises(ValidationError, match="must differ"):
        Settings(
            environment="prod",
            crawler_api_token=reused,
            serving_fingerprint_key=reused,
            _env_file=None,
        )


@pytest.mark.parametrize(
    "value",
    [
        "0123456789abcdef" * 4,
        "V4u7Rq2mC9xL5pT8nH3sK6wD1zF0jBge",
    ],
)
def test_valid_diverse_fingerprint_keys_are_accepted(value: str) -> None:
    configured = Settings(
        serving_fingerprint_key=value,
        _env_file=None,
    )

    assert configured.serving_fingerprint_key == value

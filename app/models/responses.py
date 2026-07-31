from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

CACHE_POLICY_REVISION = "crawl-cache-policy.v1"
SERVICE_IDENTITY_RECEIPT_REVISION = "crawl-service-identity.v1"


class ExtractionMetadata(BaseModel):
    title: str = ""
    description: str = ""
    language: str = ""
    source_url: str = ""
    content_type: str = ""
    status_code: int = 0
    word_count: int = 0
    rendered: bool = False
    extraction_strategy: str = ""
    authors: list[str] = Field(default_factory=list)
    doi: str = ""
    pmid: str = ""
    pmcid: str = ""
    arxiv_id: str = ""
    journal: str = ""
    published_at: str = ""
    canonical_url: str = ""
    license: str = ""
    content_scope: Literal[
        "main_content",
        "source",
        "full_text",
        "landing",
        "metadata_only",
    ] = "main_content"
    truncated: bool = False
    truncation_reason: str = ""
    origin_status_code: int = 0
    origin_error: str = ""
    pipeline_revision: str = ""
    extraction_route: str = ""
    route_reasons: list[str] = Field(default_factory=list)
    model_assisted: bool = False
    quality_attempted: bool = False
    quality_succeeded: bool = False
    candidate_count: int = Field(default=1, ge=0)
    candidate_disagreement: float = Field(default=0.0, ge=0, le=1)
    # Zero is the conservative compatibility value when source completeness is
    # unassessed. Coverage and nullable grounding fields distinguish unknown
    # from an assessed low score without changing the numeric API contract.
    completeness_score: float = Field(default=0.0, ge=0, le=1)
    completeness_coverage: Literal[
        "unassessed",
        "output_only",
        "source_full",
        "source_prefix",
    ] = "unassessed"
    source_coverage_score: float | None = Field(default=None, ge=0, le=1)
    output_grounding_score: float | None = Field(default=None, ge=0, le=1)
    completeness_reasons: list[str] = Field(default_factory=list)
    source_selection_schema: str = ""
    source_selection_receipt_sha256: str = Field(
        default="",
        pattern=r"^(?:|[0-9a-f]{64})$",
    )
    source_selection_item_count: int = Field(default=0, ge=0)
    source_selection_selected_count: int = Field(default=0, ge=0)
    source_selection_replay_verified: bool = False
    stage_timings_ms: dict[str, float] = Field(default_factory=dict)
    cache_status: Literal["live", "hit"] = "live"
    cache_age_ms: float | None = Field(default=None, ge=0)
    cache_lookup_ms: float | None = Field(default=None, ge=0)
    # Request-local receipt for the persistent crawl-result cache. This does
    # not claim that unrelated operational logs or upstream providers have a
    # zero-data-retention policy.
    cache_policy: Literal["default", "no_store"] = "default"
    cache_read_permitted: bool = True
    cache_write_permitted: bool = True
    cache_policy_revision: str = CACHE_POLICY_REVISION


class CrawlResult(BaseModel):
    url: str
    markdown: str = ""
    html: str | None = None
    links: list[str] | None = None
    extracted: dict[str, Any] | None = None
    metadata: ExtractionMetadata | None = None
    cached: bool = False
    error: str | None = None


class ServiceIdentityReceipt(BaseModel):
    """Immutable serving identity of the process that produced a response."""

    schema_version: str = SERVICE_IDENTITY_RECEIPT_REVISION
    revision: str
    config_fingerprint: str
    image_digest: str


class CrawlResponse(BaseModel):
    status: str = "ok"
    results: list[CrawlResult] = Field(default_factory=list)
    total_time_ms: float = 0
    total_pages: int = 0
    service_identity: ServiceIdentityReceipt


class MDResponse(BaseModel):
    status: str = "ok"
    markdown: str = ""
    metadata: ExtractionMetadata | None = None


class HTMLResponse(BaseModel):
    status: str = "ok"
    html: str = ""
    metadata: ExtractionMetadata | None = None


class MapResponse(BaseModel):
    status: str = "ok"
    url: str = ""
    links: list[str] = Field(default_factory=list)
    count: int = 0


class ErrorResponse(BaseModel):
    detail: str
    status: str = "error"


class HealthResponse(BaseModel):
    status: str


class ReadyResponse(BaseModel):
    status: str
    checks: dict[str, str]


class VersionResponse(BaseModel):
    sha: str
    image_digest: str = "unknown"
    environment: str
    python_version: str
    trafilatura_version: str
    playwright_version: str
    pipeline_revision: str = ""
    adaptive_router_revision: str = ""
    config_fingerprint: str = ""
    config_fingerprint_scheme: str = "hmac-sha256-v1"
    quality_backend_configured: bool = False
    quality_dependency_available: bool = False
    quality_backend_enabled: bool = False
    quality_backend_revision: str = ""
    quality_source_selection_schema: str = ""
    playwright_enabled: bool = False
    crawl_store_in_cache_supported: bool = True
    crawl_cache_policy_revision: str = CACHE_POLICY_REVISION
    crawl_service_identity_schema: str = SERVICE_IDENTITY_RECEIPT_REVISION

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


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


class CrawlResult(BaseModel):
    url: str
    markdown: str = ""
    html: str | None = None
    links: list[str] | None = None
    extracted: dict[str, Any] | None = None
    metadata: ExtractionMetadata | None = None
    cached: bool = False
    error: str | None = None


class CrawlResponse(BaseModel):
    status: str = "ok"
    results: list[CrawlResult] = Field(default_factory=list)
    total_time_ms: float = 0
    total_pages: int = 0


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
    environment: str
    python_version: str
    trafilatura_version: str
    playwright_version: str

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


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
    results: list[CrawlResult] = []
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
    links: list[str] = []
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

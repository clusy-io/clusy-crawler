from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CrawlRequest(BaseModel):
    urls: list[str] = Field(..., min_length=1, max_length=50)
    max_pages: int = Field(default=1, ge=1, le=100)
    priority: int = Field(default=10, ge=1, le=100)
    word_count_threshold: int = Field(default=10, ge=0)
    extraction_strategy: str = Field(default="NoExtractionStrategy")
    verbose: bool = Field(default=False)
    js_render: bool | None = Field(default=None)
    wait_for_selector: str | None = Field(default=None)
    # Output formats to populate per result. "markdown" is always returned;
    # "html" adds the (rendered) source HTML, "links" adds discovered links.
    formats: list[str] = Field(default_factory=lambda: ["markdown"])
    # Freshness control (seconds). None = serve any cached entry within its TTL;
    # 0 = always re-crawl (bypass cache); N = serve cached only if younger than N.
    # Mirrors Firecrawl's maxAge / Exa's maxAgeHours.
    max_age: int | None = Field(default=None, ge=0)
    # Structured extraction (the "json" format). Provide a JSON Schema for
    # schema-constrained output, and/or a natural-language extraction prompt.
    # Requires ANTHROPIC_API_KEY; otherwise the result carries an error.
    json_schema: dict[str, Any] | None = Field(default=None)
    extraction_prompt: str | None = Field(default=None)


class MDRequest(BaseModel):
    url: str = Field(..., min_length=1)
    word_count_threshold: int = Field(default=10, ge=0)
    options: dict[str, Any] = Field(default_factory=dict)


class HTMLRequest(BaseModel):
    url: str = Field(..., min_length=1)
    js_render: bool = Field(default=False)


class MapRequest(BaseModel):
    url: str = Field(..., min_length=1)
    limit: int = Field(default=1000, ge=1, le=5000)
    search: str | None = Field(default=None)

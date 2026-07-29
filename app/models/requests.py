from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

from app.config import settings

BoundedUrl = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4096),
]
ExtractionProfileName = Literal["balanced", "article_body", "adaptive", "quality"]
CrawlFormat = Literal["markdown", "html", "links", "json"]


def _default_formats() -> list[CrawlFormat]:
    return ["markdown"]


def _json_depth(value: Any, depth: int = 0) -> int:
    if not isinstance(value, (dict, list)):
        return depth
    children = value.values() if isinstance(value, dict) else value
    return max((_json_depth(child, depth + 1) for child in children), default=depth + 1)


class CrawlRequest(BaseModel):
    urls: list[BoundedUrl] = Field(..., min_length=1, max_length=50)
    max_pages: int = Field(default=settings.default_max_pages, ge=1, le=100)
    # Recursive discovery is opt-in. Depth zero preserves the historical
    # contract: crawl only the explicit input URLs and ignore max_pages.
    max_depth: int = Field(default=0, ge=0, le=10)
    allow_subdomains: bool = Field(default=False)
    priority: int = Field(default=10, ge=1, le=100)
    word_count_threshold: int = Field(default=10, ge=0)
    extraction_strategy: str = Field(default="NoExtractionStrategy", max_length=128)
    extraction_profile: ExtractionProfileName = "balanced"
    verbose: bool = Field(default=False)
    js_render: bool | None = Field(default=None)
    wait_for_selector: str | None = Field(default=None, max_length=512)
    # Output formats to populate per result. "markdown" is always returned;
    # "html" adds the (rendered) source HTML, "links" adds discovered links.
    formats: list[CrawlFormat] = Field(
        default_factory=_default_formats,
        min_length=1,
        max_length=4,
    )
    # Freshness control (seconds). None = serve any cached entry within its TTL;
    # 0 = always re-crawl (bypass cache); N = serve cached only if younger than N.
    # Mirrors Firecrawl's maxAge / Exa's maxAgeHours.
    max_age: int | None = Field(default=None, ge=0)
    # Structured extraction (the "json" format). Provide a JSON Schema for
    # schema-constrained output, and/or a natural-language extraction prompt.
    # Requires ANTHROPIC_API_KEY; otherwise the result carries an error.
    json_schema: dict[str, Any] | None = Field(default=None)
    extraction_prompt: str | None = Field(default=None, max_length=10_000)

    @model_validator(mode="after")
    def validate_bounded_structure(self) -> CrawlRequest:
        if len(set(self.formats)) != len(self.formats):
            raise ValueError("formats must not contain duplicates")
        if self.max_depth > 0 and self.max_pages < len(self.urls):
            raise ValueError("max_pages must be greater than or equal to the number of seed URLs")
        projected_pages = self.max_pages if self.max_depth > 0 else len(self.urls)
        if "html" in self.formats and projected_pages > 5:
            raise ValueError("html output is limited to 5 URLs per request")
        if self.json_schema is not None:
            encoded = json.dumps(
                self.json_schema,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
            if len(encoded) > 100_000:
                raise ValueError("json_schema exceeds the 100000-byte limit")
            if _json_depth(self.json_schema) > 20:
                raise ValueError("json_schema exceeds the maximum depth of 20")
        return self


class MDRequest(BaseModel):
    url: BoundedUrl
    word_count_threshold: int = Field(default=10, ge=0)
    extraction_profile: ExtractionProfileName = "balanced"
    options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_options(self) -> MDRequest:
        if len(json.dumps(self.options, ensure_ascii=False).encode()) > 16_384:
            raise ValueError("options exceeds the 16384-byte limit")
        selector = self.options.get("wait_for_selector")
        if selector is not None and (not isinstance(selector, str) or len(selector) > 512):
            raise ValueError("wait_for_selector must be a string of at most 512 characters")
        return self


class HTMLRequest(BaseModel):
    url: BoundedUrl
    js_render: bool = Field(default=False)


class MapRequest(BaseModel):
    url: BoundedUrl
    limit: int = Field(default=1000, ge=1, le=5000)
    search: str | None = Field(default=None, max_length=1000)

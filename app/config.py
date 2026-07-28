from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    environment: Literal["dev", "prod", "local", "test"] = "local"
    git_sha: str = Field(
        default="unknown",
        validation_alias="GIT_SHA",
        pattern=r"^(?:unknown|[0-9a-fA-F]{7,64})$",
    )

    # Service
    crawler_host: str = "0.0.0.0"
    crawler_port: int = Field(default=11235, ge=1, le=65535)
    # Comma-separated CORS allow-list. Empty (default) disables cross-origin
    # browser access entirely. Set e.g. "https://app.example.com" to allow a
    # specific web origin; "*" is accepted but strongly discouraged for a
    # public deployment.
    cors_allow_origins: str = ""
    # Bearer token for crawl/extraction endpoints. Health, readiness, version,
    # and OpenAPI discovery stay public for orchestration and diagnostics.
    # Accept both the documented CRAWL4AI_API_TOKEN and the field-name default
    # CRAWLER_API_TOKEN.
    crawler_api_token: str = Field(
        default="",
        validation_alias=AliasChoices("CRAWL4AI_API_TOKEN", "CRAWLER_API_TOKEN"),
    )

    # HTTP fetcher
    http_timeout_s: float = Field(default=30.0, gt=0)
    http_connect_timeout_s: float = Field(default=5.0, gt=0)
    http_total_timeout_s: float = Field(default=45.0, gt=0, le=300)
    http_max_keepalive_connections: int = Field(default=50, ge=0)
    http_max_connections: int = Field(default=100, ge=1)
    http_user_agent: str = "ClusyCrawler/1.0"
    # Every retry is an actual outbound request and is independently
    # rate-limited. Retry-After is honoured, but capped so a hostile origin
    # cannot pin a worker indefinitely.
    http_max_attempts: int = Field(default=3, ge=1, le=5)
    http_retry_max_delay_s: float = Field(default=5.0, ge=0, le=30)
    # robots.txt is consulted only by the opt-in recursive crawl path.  The
    # policy fetch has its own much smaller budgets and deliberately performs
    # no transport/status retries; redirects are followed manually so every
    # hop receives the normal SSRF validation.
    robots_timeout_s: float = Field(default=5.0, gt=0, le=30)
    robots_max_redirects: int = Field(default=5, ge=0, le=10)
    robots_max_body_bytes: int = Field(
        default=512 * 1024,
        # RFC 9309 section 2.5 requires parsing at least 500 KiB.
        ge=500 * 1024,
        le=2 * 1024 * 1024,
    )
    robots_max_url_length: int = Field(default=4096, ge=256, le=8192)
    robots_max_rules: int = Field(default=4096, ge=1, le=20_000)
    robots_max_records: int = Field(default=8192, ge=1, le=50_000)
    robots_max_line_chars: int = Field(default=8192, ge=128, le=65_536)
    robots_max_concurrency: int = Field(default=16, ge=1, le=128)
    robots_cache_max_entries: int = Field(default=2048, ge=1, le=100_000)
    robots_cache_ttl_s: int = Field(default=3600, ge=1, le=86_400)
    robots_unavailable_cache_ttl_s: int = Field(default=900, ge=1, le=86_400)
    robots_error_cache_ttl_s: int = Field(default=60, ge=1, le=3600)
    # Optional egress proxy (e.g. a residential/ISP proxy) for bot-walled sites
    # that block datacenter IPs. Empty = direct. Set both to the same URL to
    # route HTTP fetches and Playwright renders through it.
    http_proxy: str = ""
    playwright_proxy: str = ""

    # Adaptive timeouts
    adaptive_timeout_enabled: bool = True
    connection_warming_enabled: bool = True

    # Parallel extraction
    parallel_extraction_enabled: bool = True
    extraction_merge_mode: Literal["union", "best", "longest"] = "union"
    # rs-trafilatura is the fast primary backend. The Python ensemble remains a
    # fallback when the native quality predictor is below this threshold.
    native_extraction_enabled: bool = True
    native_extraction_min_confidence: float = Field(default=0.60, ge=0, le=1)
    max_concurrent_extractions: int = Field(default=2, ge=1)

    # Parallelism
    max_concurrent_tasks: int = Field(default=5, ge=1)
    max_concurrent_pages: int = Field(default=2, ge=1)
    max_domains_per_request: int = Field(default=50, ge=1)
    default_max_pages: int = Field(default=1, ge=1)
    # Admission and request-shape limits bound memory even when many clients
    # arrive before the crawl semaphore. Values include in-flight requests.
    max_pending_requests: int = Field(default=100, ge=1)
    max_request_body_bytes: int = Field(default=1_048_576, ge=1024)
    crawl_request_timeout_s: float = Field(default=120.0, gt=0, le=1800)
    # The floor is large enough to return one minimal error object for every
    # permitted URL even when all rich payloads must be dropped.
    max_response_output_bytes: int = Field(default=32 * 1024 * 1024, ge=16 * 1024)
    map_timeout_s: float = Field(default=30.0, gt=0, le=300)
    map_max_download_bytes: int = Field(default=20 * 1024 * 1024, ge=1024)
    map_max_concurrency: int = Field(default=4, ge=1, le=32)

    # Rate limiting
    rate_limit_requests_per_second: float = Field(default=2.0, gt=0)
    rate_limit_burst: int = Field(default=5, ge=1)
    rate_limit_max_domains: int = Field(default=1000, ge=16)

    # Content extraction
    extract_max_text_length: int = Field(default=500_000, ge=1)
    extract_min_text_length: int = Field(default=50, ge=0)

    # Playwright / JS rendering
    playwright_enabled: bool = True
    playwright_timeout_s: float = Field(default=30.0, gt=0)
    playwright_java_script_enabled: bool = True
    js_render_mode: Literal["conditional", "force", "never"] = "conditional"
    # Chromium's sandbox remains enabled by default. This escape hatch exists
    # only for explicitly isolated runtimes that cannot provide it.
    playwright_disable_sandbox: bool = False
    playwright_max_html_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)

    # Optional model-assisted quality path. The default remains the local,
    # deterministic extractor; "quality" requests use this OpenAI-compatible
    # endpoint and fall back safely when it is unavailable. No key or URL means
    # the feature is disabled rather than a startup failure.
    quality_extraction_base_url: str = ""
    quality_extraction_api_key: str = ""
    quality_extraction_model: str = ""
    # Prompt contract depends on the served model, not the HTTP protocol.
    # Generic instruction-following models use v2/JSON; the official MinerU
    # v1.1 0.5B compact checkpoint requires short_compact/compact.
    quality_extraction_prompt_profile: Literal[
        "openai_json",
        "mineru_compact",
    ] = "openai_json"
    quality_extraction_timeout_s: float = Field(default=45.0, gt=0, le=300)
    quality_extraction_capacity_timeout_s: float = Field(
        default=1.0,
        gt=0,
        le=30,
    )
    quality_extraction_shutdown_timeout_s: float = Field(
        default=5.0,
        gt=0,
        le=30,
    )
    quality_extraction_max_input_chars: int = Field(default=1_000_000, ge=1024)
    quality_extraction_max_concurrency: int = Field(default=2, ge=1, le=32)
    quality_extraction_failure_threshold: int = Field(default=3, ge=1, le=100)
    quality_extraction_cooldown_s: float = Field(default=30.0, ge=1, le=600)
    # The adaptive profile always produces the normal deterministic candidate
    # first, then consults the optional quality backend only when these bounded,
    # label-free signals classify the page as structurally risky.
    adaptive_extraction_min_confidence: float = Field(default=0.75, ge=0, le=1)
    adaptive_extraction_structural_score_threshold: int = Field(
        default=3,
        ge=1,
        le=7,
    )
    adaptive_extraction_max_scan_chars: int = Field(
        default=200_000,
        ge=4096,
        le=1_000_000,
    )
    adaptive_extraction_risky_page_types: str = "collection,listing,product"

    # Metadata-only fallbacks for recognized scholarly publisher URLs. Crossref
    # DOI lookup is public; Elsevier and IEEE access is enabled only when their
    # official API keys are configured. Responses use much tighter budgets than
    # arbitrary crawl pages and never become article full-text output.
    scholarly_metadata_enabled: bool = True
    scholarly_metadata_timeout_s: float = Field(default=8.0, gt=0, le=30)
    scholarly_metadata_max_concurrency: int = Field(default=2, ge=1, le=16)
    scholarly_metadata_max_response_bytes: int = Field(
        default=512 * 1024,
        ge=1024,
        le=5 * 1024 * 1024,
    )
    # PDF links advertised by one landing page are attempted within one shared
    # wall-clock budget. A sequence of dead publisher links must not consume the
    # entire request deadline before the useful abstract/metadata can return.
    academic_pdf_fallback_timeout_s: float = Field(default=12.0, gt=0, le=60)
    elsevier_api_key: str = Field(default="", max_length=4096)
    ieee_api_key: str = Field(default="", max_length=4096)

    # Cache (Redis)
    redis_url: str = ""
    cache_ttl_s: int = Field(default=3600, ge=1)
    cache_max_size_mb: int = Field(default=500, ge=1)
    cache_connect_timeout_s: float = Field(default=0.75, gt=0, le=10)
    cache_operation_timeout_s: float = Field(default=0.5, gt=0, le=10)
    cache_failure_cooldown_s: float = Field(default=5.0, gt=0, le=300)
    cache_max_entry_bytes: int = Field(default=1_048_576, ge=1024)

    # Optional LLM structured extraction (the "json" output format).
    # Empty api key = feature disabled (markdown crawl still works). Default
    # model is the cheap/fast Haiku tier appropriate for high-volume page
    # extraction; set to "claude-opus-4-8" for maximum extraction quality.
    anthropic_api_key: str = ""
    extraction_model: str = "claude-haiku-4-5"
    extraction_max_tokens: int = Field(default=8192, ge=1)
    extraction_max_input_chars: int = Field(default=100_000, ge=1)
    structured_extraction_max_concurrency: int = Field(default=2, ge=1, le=32)
    structured_extraction_timeout_s: float = Field(default=45.0, gt=0, le=300)

    # Crawl4AI-compatible endpoints
    crawl4ai_compat: bool = True

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "populate_by_name": True,
    }

    @field_validator("adaptive_extraction_risky_page_types")
    @classmethod
    def validate_adaptive_page_types(cls, value: str) -> str:
        allowed = {
            "academic",
            "article",
            "collection",
            "documentation",
            "forum",
            "listing",
            "product",
            "repository",
            "webpage",
        }
        page_types = [item.strip().lower() for item in value.split(",") if item.strip()]
        if not page_types:
            raise ValueError("ADAPTIVE_EXTRACTION_RISKY_PAGE_TYPES must not be empty")
        unknown = sorted(set(page_types) - allowed)
        if unknown:
            raise ValueError(
                "ADAPTIVE_EXTRACTION_RISKY_PAGE_TYPES contains unsupported values: "
                + ", ".join(unknown)
            )
        return ",".join(dict.fromkeys(page_types))

    @model_validator(mode="after")
    def require_production_auth(self) -> Settings:
        if self.environment == "prod" and not self.crawler_api_token:
            raise ValueError("CRAWL4AI_API_TOKEN (or CRAWLER_API_TOKEN) is required in prod")
        if (
            self.environment == "prod"
            and self.playwright_enabled
            and self.playwright_disable_sandbox
        ):
            raise ValueError(
                "PLAYWRIGHT_DISABLE_SANDBOX cannot be true in prod while Playwright is enabled"
            )
        if self.environment == "prod" and self.redis_url and self.git_sha == "unknown":
            raise ValueError("GIT_SHA is required in prod when Redis caching is enabled")
        return self


settings = Settings()

from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    environment: Literal["dev", "prod", "local", "test"] = "local"

    # Service
    crawler_host: str = "0.0.0.0"
    crawler_port: int = 11235
    # Comma-separated CORS allow-list. Empty (default) disables cross-origin
    # browser access entirely — correct for the internal service-to-service
    # deployment. Set e.g. "https://app.example.com" to allow a specific web
    # origin; "*" is accepted but strongly discouraged for a public deployment.
    cors_allow_origins: str = ""
    # Bearer token gating every endpoint except /health. Accept both the
    # documented CRAWL4AI_API_TOKEN (README/.env/deploy/agent all use this)
    # and the field-name default CRAWLER_API_TOKEN — previously only the
    # latter was read, so the documented token was silently ignored and the
    # service ran unauthenticated.
    crawler_api_token: str = Field(
        default="",
        validation_alias=AliasChoices("CRAWL4AI_API_TOKEN", "CRAWLER_API_TOKEN"),
    )

    # HTTP fetcher
    http_timeout_s: float = 30.0
    http_connect_timeout_s: float = 5.0
    http_max_keepalive_connections: int = 50
    http_max_connections: int = 100
    http_user_agent: str = "ClusyCrawler/1.0"
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
    extraction_merge_mode: str = "union"  # "union" | "best" | "longest"

    # Parallelism
    max_concurrent_tasks: int = 5
    max_concurrent_pages: int = 2
    max_domains_per_request: int = 50
    default_max_pages: int = 1

    # Rate limiting
    rate_limit_requests_per_second: float = 2.0
    rate_limit_burst: int = 5

    # Content extraction
    extract_max_text_length: int = 500_000
    extract_min_text_length: int = 50

    # Playwright / JS rendering
    playwright_enabled: bool = True
    playwright_timeout_s: float = 30.0
    playwright_java_script_enabled: bool = True
    js_render_mode: Literal["conditional", "force", "never"] = "conditional"

    # Cache (Redis)
    redis_url: str = ""
    cache_ttl_s: int = 3600
    cache_max_size_mb: int = 500

    # Optional LLM structured extraction (the "json" output format).
    # Empty api key = feature disabled (markdown crawl still works). Default
    # model is the cheap/fast Haiku tier appropriate for high-volume page
    # extraction; set to "claude-opus-4-8" for maximum extraction quality.
    anthropic_api_key: str = ""
    extraction_model: str = "claude-haiku-4-5"
    extraction_max_tokens: int = 8192
    extraction_max_input_chars: int = 100_000

    # Crawl4AI-compatible endpoints
    crawl4ai_compat: bool = True

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "populate_by_name": True,
    }


settings = Settings()

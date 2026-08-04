from __future__ import annotations

from app.config import Settings


def test_crawler_api_token_is_the_preferred_environment_name(monkeypatch) -> None:
    monkeypatch.setenv("CRAWLER_API_TOKEN", "preferred-token")
    monkeypatch.setenv("CRAWL4AI_API_TOKEN", "compatibility-token")

    configured = Settings(_env_file=None)

    assert configured.crawler_api_token == "preferred-token"


def test_crawl4ai_api_token_remains_a_compatibility_alias(monkeypatch) -> None:
    monkeypatch.delenv("CRAWLER_API_TOKEN", raising=False)
    monkeypatch.setenv("CRAWL4AI_API_TOKEN", "compatibility-token")

    configured = Settings(_env_file=None)

    assert configured.crawler_api_token == "compatibility-token"
    assert "crawler_api_token_compat" not in configured.model_dump()


def test_empty_preferred_token_falls_back_to_compatibility_alias(monkeypatch) -> None:
    monkeypatch.setenv("CRAWLER_API_TOKEN", "")
    monkeypatch.setenv("CRAWL4AI_API_TOKEN", "compatibility-token")

    configured = Settings(_env_file=None)

    assert configured.crawler_api_token == "compatibility-token"

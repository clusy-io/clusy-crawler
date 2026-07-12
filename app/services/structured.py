"""Optional LLM-powered structured extraction — the /extract (JSON) capability.

Parity with Firecrawl `/extract` and Exa summaries: turn a crawled page into
structured JSON against a caller-supplied JSON Schema, or a freeform answer
against a prompt. Uses the official Anthropic SDK with schema-constrained
structured outputs (`output_config.format`).

Degrades gracefully: if `anthropic` isn't installed or no API key is configured,
extraction returns a clear error string instead of raising — the markdown crawl
still succeeds.

Model is operator-configurable via EXTRACTION_MODEL. Default is
``claude-haiku-4-5`` — the cheap, fast tier appropriate for high-volume page
extraction; set it to ``claude-opus-4-8`` for maximum extraction quality.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from app.config import settings

logger = structlog.get_logger()

_client: Any = None
_client_init = False


def _get_client() -> Any:
    """Lazily construct a singleton AsyncAnthropic client, or None if unavailable."""
    global _client, _client_init
    if _client_init:
        return _client
    _client_init = True
    if not settings.anthropic_api_key:
        return None
    try:
        from anthropic import AsyncAnthropic

        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    except Exception as e:  # pragma: no cover - import/usage guard
        logger.warning("anthropic_unavailable", error=str(e))
        _client = None
    return _client


def _ensure_strict(schema: dict[str, Any]) -> dict[str, Any]:
    """Recursively set additionalProperties:false on objects (required by the API)."""
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    if out.get("type") == "object":
        out.setdefault("additionalProperties", False)
        props = out.get("properties")
        if isinstance(props, dict):
            out["properties"] = {k: _ensure_strict(v) for k, v in props.items()}
    if out.get("type") == "array" and isinstance(out.get("items"), dict):
        out["items"] = _ensure_strict(out["items"])
    return out


async def extract_structured(
    content: str,
    json_schema: dict[str, Any] | None = None,
    prompt: str | None = None,
) -> dict[str, Any]:
    """Extract structured data from page content.

    With a json_schema → schema-constrained JSON (validated by the API).
    With only a prompt → a freeform answer under {"result": ...}.
    Returns {"error": "..."} when the LLM backend is unavailable or fails.
    """
    client = _get_client()
    if client is None:
        return {"error": "structured extraction not configured (set ANTHROPIC_API_KEY)"}

    if not content.strip():
        return {"error": "no content to extract from"}

    # Cap the input so a giant page can't blow past the context window / budget.
    snippet = content[: settings.extraction_max_input_chars]
    instruction = prompt or "Extract the structured data described by the schema from the page."
    user_text = f"{instruction}\n\n---\nPAGE CONTENT:\n{snippet}"

    kwargs: dict[str, Any] = {
        "model": settings.extraction_model,
        "max_tokens": settings.extraction_max_tokens,
        "messages": [{"role": "user", "content": user_text}],
    }
    if json_schema:
        kwargs["output_config"] = {
            "format": {"type": "json_schema", "schema": _ensure_strict(json_schema)}
        }

    try:
        resp = await client.messages.create(**kwargs)
    except Exception as e:
        logger.warning("structured_extraction_failed", error=str(e))
        return {"error": f"extraction failed: {e}"}

    text = ""
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "text":
            text += block.text

    if json_schema:
        try:
            parsed: dict[str, Any] = json.loads(text)
            return parsed
        except json.JSONDecodeError:
            return {"error": "model did not return valid JSON", "raw": text[:2000]}
    return {"result": text}

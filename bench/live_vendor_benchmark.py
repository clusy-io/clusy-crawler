#!/usr/bin/env python3
"""Sealed, fixed-URL extraction benchmark for Clusy, Exa, and Firecrawl.

This runner deliberately has no implicit execution mode.  ``validate`` and
``score`` are offline.  ``run`` refuses to create an HTTP client unless all of
the following are true:

* the manifest is frozen, sealed, and has a valid content digest;
* ``--execute-paid`` was passed;
* both paid-provider API keys and the Clusy endpoint are present;
* the checkout is clean and the runner is committed, unless the operator
  explicitly selected ``--nonclaimable``.

The module is importable so tests and internal orchestration can inject an
HTTPX mock transport without touching the network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Any, Final, Literal, Protocol, cast
from urllib.parse import urlsplit

import httpx

SCHEMA_VERSION: Final = "clusy.live-vendor.fixed-url.v1"
EVENT_SCHEMA_VERSION: Final = "clusy.live-vendor.event.v1"
SUMMARY_SCHEMA_VERSION: Final = "clusy.live-vendor.summary.v1"
PROVIDERS: Final = ("clusy", "exa", "firecrawl")
RUNNER_PATH: Final = Path(__file__).resolve()
MAX_MANIFEST_BYTES: Final = 10_000_000
MAX_TASKS: Final = 20_000
MAX_RESPONSE_BYTES: Final = 128 * 1024 * 1024
EXA_ENDPOINT: Final = "https://api.exa.ai/contents"
FIRECRAWL_ENDPOINT: Final = "https://api.firecrawl.dev/v2/scrape"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTAINER_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)

Provider = Literal["clusy", "exa", "firecrawl"]
JsonObject = dict[str, Any]
ClientFactory = Callable[[], httpx.Client]


class BenchmarkError(RuntimeError):
    """Expected validation or execution refusal."""


class ProviderResultError(BenchmarkError):
    """A provider reported a per-item failure inside a successful HTTP response."""


def _reject_constant(value: str) -> None:
    raise BenchmarkError(f"non-finite JSON number is forbidden: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise BenchmarkError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def _load_json_bytes(data: bytes) -> JsonObject:
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BenchmarkError("JSON must be UTF-8") from exc
    try:
        value = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise BenchmarkError(f"invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise BenchmarkError("top-level JSON value must be an object")
    return cast("JsonObject", value)


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Return the canonical representation used by manifest and artifact hashes."""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise BenchmarkError(f"value is not canonical JSON: {exc}") from exc
    return encoded.encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest_payload(document: Mapping[str, Any]) -> JsonObject:
    payload = dict(document)
    payload.pop("manifest_sha256", None)
    return payload


def calculate_manifest_sha256(document: Mapping[str, Any]) -> str:
    """Hash the canonical manifest excluding its self-referential digest field."""
    return sha256_bytes(canonical_json_bytes(manifest_payload(document)))


def seal_manifest_document(document: Mapping[str, Any]) -> JsonObject:
    """Return a frozen, sealed copy with a freshly calculated digest."""
    sealed = dict(document)
    sealed["sealed"] = True
    sealed["frozen"] = True
    sealed.pop("manifest_sha256", None)
    sealed["manifest_sha256"] = calculate_manifest_sha256(sealed)
    return sealed


def _require_string(
    obj: Mapping[str, Any],
    key: str,
    *,
    allow_empty: bool = False,
    max_length: int = 4096,
) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise BenchmarkError(f"{key} must be a string")
    if (not allow_empty and not value.strip()) or len(value) > max_length:
        raise BenchmarkError(f"{key} is empty or too long")
    return value


def _require_number(
    obj: Mapping[str, Any],
    key: str,
    *,
    minimum: float = 0,
) -> float:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BenchmarkError(f"{key} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise BenchmarkError(f"{key} must be finite and at least {minimum}")
    return number


def _validate_http_url(url: str, *, field_name: str) -> None:
    if url != url.strip() or any(ord(character) < 0x20 for character in url):
        raise BenchmarkError(f"{field_name} contains whitespace or control characters")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise BenchmarkError(f"{field_name} is not a valid URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise BenchmarkError(f"{field_name} must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise BenchmarkError(f"{field_name} must not contain credentials")
    if port is not None and not (1 <= port <= 65535):
        raise BenchmarkError(f"{field_name} contains an invalid port")


@dataclass(frozen=True)
class Reference:
    text: str
    sha256: str
    method: str


@dataclass(frozen=True)
class Task:
    task_id: str
    url: str
    stratum: str
    language: str
    reference: Reference | None


@dataclass(frozen=True)
class Pricing:
    currency: str
    per_request: float


@dataclass(frozen=True)
class Manifest:
    document: JsonObject = field(repr=False)
    digest: str
    benchmark_id: str
    created_at: str
    seed: int
    runner_region: str
    country: str | None
    location: str | None
    scope: str
    extraction_profile: str
    timeout_seconds: float
    plans: Mapping[Provider, str]
    pricing: Mapping[Provider, Pricing]
    tasks: tuple[Task, ...]


def _optional_short_string(
    obj: Mapping[str, Any],
    key: str,
    *,
    max_length: int = 256,
) -> str | None:
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > max_length:
        raise BenchmarkError(f"{key} must be null or a short string")
    return value


def _parse_reference(value: Any, task_id: str) -> Reference | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise BenchmarkError(f"task {task_id}: reference must be an object")
    text = _require_string(value, "text", allow_empty=True, max_length=10_000_000)
    expected_sha = _require_string(value, "sha256", max_length=64)
    if not _SHA256.fullmatch(expected_sha):
        raise BenchmarkError(f"task {task_id}: reference sha256 is invalid")
    actual_sha = sha256_bytes(text.encode("utf-8"))
    if actual_sha != expected_sha:
        raise BenchmarkError(f"task {task_id}: reference sha256 mismatch")
    method = _require_string(value, "method", max_length=256)
    return Reference(text=text, sha256=expected_sha, method=method)


def _parse_tasks(value: Any) -> tuple[Task, ...]:
    if not isinstance(value, list) or not value:
        raise BenchmarkError("tasks must be a non-empty array")
    if len(value) > MAX_TASKS:
        raise BenchmarkError(f"tasks exceeds the {MAX_TASKS}-task safety limit")
    tasks: list[Task] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise BenchmarkError(f"task {index}: must be an object")
        task_id = _require_string(raw, "task_id", max_length=128)
        if not _SAFE_ID.fullmatch(task_id):
            raise BenchmarkError(f"task {index}: task_id contains unsafe characters")
        if task_id in seen:
            raise BenchmarkError(f"duplicate task_id: {task_id}")
        seen.add(task_id)
        url = _require_string(raw, "url")
        _validate_http_url(url, field_name=f"task {task_id} URL")
        if urlsplit(url).fragment:
            raise BenchmarkError(f"task {task_id}: URL fragments are forbidden")
        stratum = _require_string(raw, "stratum", max_length=128)
        language = _require_string(raw, "language", max_length=64)
        tasks.append(
            Task(
                task_id=task_id,
                url=url,
                stratum=stratum,
                language=language,
                reference=_parse_reference(raw.get("reference"), task_id),
            )
        )
    return tuple(tasks)


def _parse_provider_strings(value: Any, field_name: str) -> Mapping[Provider, str]:
    if not isinstance(value, dict):
        raise BenchmarkError(f"{field_name} must be an object")
    parsed: dict[Provider, str] = {}
    for provider in PROVIDERS:
        parsed[provider] = _require_string(value, provider, max_length=256)
    unknown = set(value) - set(PROVIDERS)
    if unknown:
        raise BenchmarkError(f"{field_name} has unknown providers: {sorted(unknown)}")
    return parsed


def _parse_pricing(value: Any) -> Mapping[Provider, Pricing]:
    if not isinstance(value, dict):
        raise BenchmarkError("pricing must be an object")
    parsed: dict[Provider, Pricing] = {}
    for provider_name in PROVIDERS:
        raw = value.get(provider_name)
        if not isinstance(raw, dict):
            raise BenchmarkError(f"pricing.{provider_name} must be an object")
        currency = _require_string(raw, "currency", max_length=16).upper()
        if currency != "USD":
            raise BenchmarkError("normalized benchmark pricing currently requires USD")
        per_request = _require_number(raw, "per_request", minimum=0)
        parsed[provider_name] = Pricing(
            currency=currency,
            per_request=per_request,
        )
    unknown = set(value) - set(PROVIDERS)
    if unknown:
        raise BenchmarkError(f"pricing has unknown providers: {sorted(unknown)}")
    return parsed


def parse_manifest(document: JsonObject) -> Manifest:
    """Validate and convert a sealed manifest."""
    if document.get("schema_version") != SCHEMA_VERSION:
        raise BenchmarkError(f"schema_version must be {SCHEMA_VERSION}")
    if document.get("sealed") is not True or document.get("frozen") is not True:
        raise BenchmarkError("manifest must set both sealed=true and frozen=true")
    declared_digest = _require_string(document, "manifest_sha256", max_length=64)
    if not _SHA256.fullmatch(declared_digest):
        raise BenchmarkError("manifest_sha256 must be 64 lowercase hexadecimal characters")
    actual_digest = calculate_manifest_sha256(document)
    if declared_digest != actual_digest:
        raise BenchmarkError(
            f"manifest digest mismatch: declared {declared_digest}, actual {actual_digest}"
        )
    benchmark_id = _require_string(document, "benchmark_id", max_length=128)
    if not _SAFE_ID.fullmatch(benchmark_id):
        raise BenchmarkError("benchmark_id contains unsafe characters")
    created_at = _require_string(document, "created_at", max_length=64)
    try:
        parsed_created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BenchmarkError("created_at must be an ISO-8601 timestamp") from exc
    if parsed_created.tzinfo is None:
        raise BenchmarkError("created_at must include a timezone")
    seed_raw = document.get("seed")
    if isinstance(seed_raw, bool) or not isinstance(seed_raw, int):
        raise BenchmarkError("seed must be an integer")
    if not (0 <= seed_raw < 2**63):
        raise BenchmarkError("seed must be in [0, 2^63)")
    runner_region = _require_string(document, "runner_region", max_length=128)
    country = _optional_short_string(document, "country")
    location = _optional_short_string(document, "location")
    if country is not None or location is not None:
        raise BenchmarkError(
            "country and location must be null: fixed-URL fetch geography cannot be "
            "controlled consistently across all three providers"
        )
    scope = _require_string(document, "scope", max_length=128)
    if scope != "main_content":
        raise BenchmarkError("fixed-URL matched track requires scope=main_content")
    content_format = _require_string(document, "content_format", max_length=32)
    if content_format != "markdown":
        raise BenchmarkError("fixed-URL matched track requires content_format=markdown")
    mode = _require_string(document, "mode", max_length=32)
    if mode != "cold_live":
        raise BenchmarkError("this runner implements only the primary cold_live track")
    profile = _require_string(document, "clusy_extraction_profile", max_length=32)
    if profile not in {"balanced", "adaptive", "quality"}:
        raise BenchmarkError("unsupported clusy_extraction_profile")
    timeout_seconds = _require_number(document, "timeout_seconds", minimum=1)
    if timeout_seconds != 60:
        raise BenchmarkError("matched cold/live protocol requires timeout_seconds=60")
    allowed_keys = {
        "schema_version",
        "sealed",
        "frozen",
        "manifest_sha256",
        "benchmark_id",
        "created_at",
        "seed",
        "runner_region",
        "country",
        "location",
        "scope",
        "content_format",
        "mode",
        "clusy_extraction_profile",
        "timeout_seconds",
        "plans",
        "pricing",
        "tasks",
    }
    unknown = set(document) - allowed_keys
    if unknown:
        raise BenchmarkError(f"manifest has unknown fields: {sorted(unknown)}")
    return Manifest(
        document=document,
        digest=actual_digest,
        benchmark_id=benchmark_id,
        created_at=created_at,
        seed=seed_raw,
        runner_region=runner_region,
        country=country,
        location=location,
        scope=scope,
        extraction_profile=profile,
        timeout_seconds=timeout_seconds,
        plans=_parse_provider_strings(document.get("plans"), "plans"),
        pricing=_parse_pricing(document.get("pricing")),
        tasks=_parse_tasks(document.get("tasks")),
    )


def load_manifest(path: Path) -> Manifest:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise BenchmarkError(f"cannot read manifest {path}: {exc}") from exc
    if len(data) > MAX_MANIFEST_BYTES:
        raise BenchmarkError("manifest exceeds the 10 MB safety limit")
    return parse_manifest(_load_json_bytes(data))


@dataclass(frozen=True)
class Credentials:
    exa_api_key: str = field(repr=False)
    firecrawl_api_key: str = field(repr=False)
    clusy_base_url: str
    clusy_api_key: str = field(default="", repr=False)

    @classmethod
    def from_environment(cls) -> Credentials:
        return cls(
            exa_api_key=os.environ.get("EXA_API_KEY", ""),
            firecrawl_api_key=os.environ.get("FIRECRAWL_API_KEY", ""),
            clusy_base_url=os.environ.get("CLUSY_CRAWLER_URL", ""),
            clusy_api_key=os.environ.get("CLUSY_CRAWLER_API_KEY", ""),
        )

    def validate(self, *, claimable: bool) -> None:
        missing: list[str] = []
        if not self.exa_api_key.strip():
            missing.append("EXA_API_KEY")
        if not self.firecrawl_api_key.strip():
            missing.append("FIRECRAWL_API_KEY")
        if not self.clusy_base_url.strip():
            missing.append("CLUSY_CRAWLER_URL")
        if missing:
            raise BenchmarkError(
                f"required execution credentials are missing: {', '.join(missing)}"
            )
        for name, value in (
            ("EXA_API_KEY", self.exa_api_key),
            ("FIRECRAWL_API_KEY", self.firecrawl_api_key),
            ("CLUSY_CRAWLER_API_KEY", self.clusy_api_key),
        ):
            if value and (
                value != value.strip() or any(ord(character) < 0x20 for character in value)
            ):
                raise BenchmarkError(f"{name} contains whitespace or control characters")
        _validate_http_url(self.clusy_base_url, field_name="CLUSY_CRAWLER_URL")
        parsed = urlsplit(self.clusy_base_url)
        if parsed.query or parsed.fragment:
            raise BenchmarkError("CLUSY_CRAWLER_URL must not contain a query or fragment")
        if claimable and parsed.scheme != "https":
            raise BenchmarkError("claimable runs require an HTTPS Clusy endpoint")

    @property
    def secret_values(self) -> tuple[str, ...]:
        return tuple(
            value
            for value in (
                self.exa_api_key,
                self.firecrawl_api_key,
                self.clusy_api_key,
            )
            if value
        )


@dataclass(frozen=True)
class GitState:
    commit: str
    clean: bool
    runner_committed: bool
    detail: str


def inspect_git_state(repo_root: Path, runner_path: Path = RUNNER_PATH) -> GitState:
    def invoke(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    head = invoke("rev-parse", "--verify", "HEAD")
    if head.returncode != 0:
        return GitState("", False, False, "git HEAD is unavailable")
    commit = head.stdout.strip()
    status = invoke("status", "--porcelain=v1", "--untracked-files=all")
    if status.returncode != 0:
        return GitState(commit, False, False, "git status failed")
    try:
        relative_runner = runner_path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return GitState(commit, False, False, "runner is outside repository")
    tracked = invoke("ls-files", "--error-unmatch", "--", str(relative_runner))
    runner_diff = invoke("diff", "--quiet", "HEAD", "--", str(relative_runner))
    runner_committed = tracked.returncode == 0 and runner_diff.returncode == 0
    clean = not status.stdout.strip()
    detail = "clean" if clean else "working tree has tracked or untracked changes"
    if not runner_committed:
        detail += "; runner is uncommitted or differs from HEAD"
    return GitState(commit, clean, runner_committed, detail)


@dataclass(frozen=True)
class ClaimContext:
    claimable: bool
    watermark: str
    reasons: tuple[str, ...]
    runner_commit: str
    runner_sha256: str
    container_digest: str


def prepare_claim_context(
    *,
    repo_root: Path,
    nonclaimable: bool,
    git_state: GitState | None = None,
    container_digest: str | None = None,
) -> ClaimContext:
    state = git_state or inspect_git_state(repo_root)
    runner_sha = sha256_bytes(RUNNER_PATH.read_bytes())
    digest = (
        container_digest if container_digest is not None else os.environ.get("CONTAINER_DIGEST", "")
    )
    reasons: list[str] = []
    if nonclaimable:
        reasons.append("operator explicitly selected nonclaimable mode")
    if not state.clean:
        reasons.append("working tree is dirty")
    if not state.runner_committed:
        reasons.append("runner is uncommitted or differs from HEAD")
    if not _CONTAINER_DIGEST.fullmatch(digest):
        reasons.append("CONTAINER_DIGEST is missing or not a sha256 digest")
    if reasons and not nonclaimable:
        raise BenchmarkError(
            "claimable run refused: "
            + "; ".join(reasons)
            + ". Commit the frozen runner and use an immutable container, or explicitly "
            "select --nonclaimable."
        )
    return ClaimContext(
        claimable=not reasons,
        watermark="" if not reasons else "NONCLAIMABLE",
        reasons=tuple(reasons),
        runner_commit=state.commit or "unknown",
        runner_sha256=runner_sha,
        container_digest=digest or "unknown",
    )


@dataclass(frozen=True)
class PreparedRequest:
    provider: Provider
    endpoint: str
    api_version: str
    method: str
    headers: Mapping[str, str] = field(repr=False)
    json_body: Mapping[str, Any]
    timeout_seconds: float


def build_provider_request(
    provider: Provider,
    *,
    task: Task,
    manifest: Manifest,
    credentials: Credentials,
) -> PreparedRequest:
    common_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "clusy-sealed-fixed-url-benchmark/1",
    }
    timeout_ms = int(manifest.timeout_seconds * 1000)
    if provider == "exa":
        return PreparedRequest(
            provider=provider,
            endpoint=EXA_ENDPOINT,
            api_version="contents-current-2026-07-28",
            method="POST",
            headers={
                **common_headers,
                "Authorization": f"Bearer {credentials.exa_api_key}",
            },
            json_body={
                "urls": [task.url],
                "text": {"verbosity": "full"},
                "maxAgeHours": 0,
                "livecrawlTimeout": timeout_ms,
            },
            timeout_seconds=manifest.timeout_seconds,
        )
    if provider == "firecrawl":
        return PreparedRequest(
            provider=provider,
            endpoint=FIRECRAWL_ENDPOINT,
            api_version="v2",
            method="POST",
            headers={
                **common_headers,
                "Authorization": f"Bearer {credentials.firecrawl_api_key}",
            },
            json_body={
                "url": task.url,
                "formats": ["markdown"],
                "onlyMainContent": True,
                "maxAge": 0,
                "storeInCache": False,
                "timeout": timeout_ms,
            },
            timeout_seconds=manifest.timeout_seconds,
        )
    headers = dict(common_headers)
    if credentials.clusy_api_key:
        headers["Authorization"] = f"Bearer {credentials.clusy_api_key}"
    endpoint = credentials.clusy_base_url.rstrip("/") + "/crawl"
    return PreparedRequest(
        provider=provider,
        endpoint=endpoint,
        api_version="clusy-crawl-v1",
        method="POST",
        headers=headers,
        json_body={
            "urls": [task.url],
            "max_pages": 1,
            "formats": ["markdown"],
            "max_age": 0,
            "extraction_profile": manifest.extraction_profile,
        },
        timeout_seconds=manifest.timeout_seconds,
    )


@dataclass(frozen=True)
class WireResponse:
    status_code: int | None
    headers: Mapping[str, str]
    body: bytes
    started_at: str
    first_byte_at: str | None
    completed_at: str
    latency_ms: float
    transport_error: str | None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class RequestExecutor(Protocol):
    def execute(self, request: PreparedRequest) -> WireResponse:
        """Execute exactly one request without retries."""

    def close(self) -> None:
        """Release resources."""


class HttpxRequestExecutor:
    """One-attempt streaming HTTPX executor with bounded response retention."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def execute(self, request: PreparedRequest) -> WireResponse:
        started_wall = _utc_now()
        started = time.perf_counter()
        first_byte: str | None = None
        chunks: list[bytes] = []
        total = 0
        status_code: int | None = None
        headers: Mapping[str, str] = {}
        try:
            with self._client.stream(
                request.method,
                request.endpoint,
                headers=request.headers,
                json=request.json_body,
                timeout=request.timeout_seconds,
            ) as response:
                status_code = response.status_code
                headers = dict(response.headers)
                for chunk in response.iter_bytes():
                    if first_byte is None:
                        first_byte = _utc_now()
                    total += len(chunk)
                    if total > MAX_RESPONSE_BYTES:
                        raise BenchmarkError(
                            f"response exceeds {MAX_RESPONSE_BYTES}-byte safety limit"
                        )
                    chunks.append(chunk)
                body = b"".join(chunks)
            error: str | None = None
        except Exception as exc:
            body = b"".join(chunks)
            error = f"{type(exc).__name__}: {exc}"
        completed = time.perf_counter()
        return WireResponse(
            status_code=status_code,
            headers=headers,
            body=body,
            started_at=started_wall,
            first_byte_at=first_byte,
            completed_at=_utc_now(),
            latency_ms=(completed - started) * 1000,
            transport_error=error,
        )

    def close(self) -> None:
        self._client.close()


@dataclass(frozen=True)
class NormalizedResult:
    status: str
    error: str | None
    text: str
    title: str
    canonical_url: str
    publication_timestamp: str | None
    fetch_timestamp: str | None
    provider_request_id: str | None
    cache_hit: bool | None
    fetch_age: float | None
    credits: float | None
    provider_score: float | None
    citation_links: tuple[str, ...]


def normalize_text(text: str) -> str:
    """Apply the same conservative, deterministic normalization to all providers."""
    normalized = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\x00", "")
    return "\n".join(line.rstrip() for line in normalized.split("\n")).strip()


def _json_response(body: bytes) -> JsonObject:
    if not body:
        raise BenchmarkError("empty response body")
    return _load_json_bytes(body)


def _as_optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _as_optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _as_optional_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _string_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _header_request_id(headers: Mapping[str, str]) -> str | None:
    lowered = {key.lower(): value for key, value in headers.items()}
    for name in ("x-request-id", "request-id", "cf-ray"):
        if lowered.get(name):
            return lowered[name]
    return None


def _normalization_error(message: str, request_id: str | None = None) -> NormalizedResult:
    return NormalizedResult(
        status="malformed_response",
        error=message,
        text="",
        title="",
        canonical_url="",
        publication_timestamp=None,
        fetch_timestamp=None,
        provider_request_id=request_id,
        cache_hit=None,
        fetch_age=None,
        credits=None,
        provider_score=None,
        citation_links=(),
    )


def normalize_provider_response(provider: Provider, wire: WireResponse) -> NormalizedResult:
    request_id = _header_request_id(wire.headers)
    if wire.transport_error:
        result = _normalization_error(wire.transport_error, request_id)
        return NormalizedResult(**{**result.__dict__, "status": "transport_error"})
    if wire.status_code is None or not (200 <= wire.status_code < 300):
        detail = f"HTTP {wire.status_code}" if wire.status_code is not None else "HTTP unavailable"
        try:
            document = _json_response(wire.body)
            body_error = document.get("error") or document.get("detail") or document.get("message")
            if isinstance(body_error, str):
                detail += f": {body_error}"
        except BenchmarkError:
            pass
        result = _normalization_error(detail, request_id)
        return NormalizedResult(**{**result.__dict__, "status": "http_error"})
    try:
        document = _json_response(wire.body)
    except BenchmarkError as exc:
        return _normalization_error(str(exc), request_id)
    try:
        if provider == "exa":
            statuses = document.get("statuses")
            if not isinstance(statuses, list) or not statuses or not isinstance(statuses[0], dict):
                raise BenchmarkError("Exa response has no per-URL status")
            exa_status = cast("JsonObject", statuses[0])
            status_value = exa_status.get("status")
            if status_value == "error":
                status_error = exa_status.get("error")
                error_obj = (
                    cast("JsonObject", status_error) if isinstance(status_error, dict) else {}
                )
                tag = _as_optional_string(error_obj.get("tag")) or "UNKNOWN"
                error_code = _as_optional_number(error_obj.get("httpStatusCode"))
                code_suffix = f" (HTTP {int(error_code)})" if error_code is not None else ""
                raise ProviderResultError(f"Exa per-URL error: {tag}{code_suffix}")
            if status_value != "success":
                raise BenchmarkError("Exa per-URL status is neither success nor error")
            results = document.get("results")
            if not isinstance(results, list) or not results or not isinstance(results[0], dict):
                raise BenchmarkError("Exa response has no first result")
            item = cast("JsonObject", results[0])
            text = item.get("text")
            if not isinstance(text, str):
                raise BenchmarkError("Exa result text is missing")
            request_id = (
                _as_optional_string(document.get("requestId"))
                or _as_optional_string(document.get("request_id"))
                or request_id
            )
            extras = item.get("extras")
            extras_obj = cast("JsonObject", extras) if isinstance(extras, dict) else {}
            normalized = NormalizedResult(
                status="ok",
                error=None,
                text=normalize_text(text),
                title=_as_optional_string(item.get("title")) or "",
                canonical_url=_as_optional_string(item.get("url")) or "",
                publication_timestamp=_as_optional_string(item.get("publishedDate")),
                fetch_timestamp=_as_optional_string(item.get("crawledDate")),
                provider_request_id=request_id,
                cache_hit=_as_optional_bool(item.get("cacheHit")),
                fetch_age=_as_optional_number(item.get("fetchAge")),
                credits=_as_optional_number(document.get("credits")),
                provider_score=_as_optional_number(item.get("score")),
                citation_links=_string_list(extras_obj.get("links")),
            )
        elif provider == "firecrawl":
            if document.get("success") is False:
                provider_error = (
                    _as_optional_string(document.get("error"))
                    or _as_optional_string(document.get("message"))
                    or "unspecified error"
                )
                raise ProviderResultError(f"Firecrawl response reported failure: {provider_error}")
            data = document.get("data")
            if not isinstance(data, dict):
                raise BenchmarkError("Firecrawl response data is missing")
            item = cast("JsonObject", data)
            text = item.get("markdown")
            if not isinstance(text, str):
                raise BenchmarkError("Firecrawl markdown is missing")
            metadata = item.get("metadata")
            metadata_obj = cast("JsonObject", metadata) if isinstance(metadata, dict) else {}
            normalized = NormalizedResult(
                status="ok",
                error=None,
                text=normalize_text(text),
                title=_as_optional_string(metadata_obj.get("title")) or "",
                canonical_url=(
                    _as_optional_string(metadata_obj.get("canonicalUrl"))
                    or _as_optional_string(metadata_obj.get("sourceURL"))
                    or ""
                ),
                publication_timestamp=_as_optional_string(metadata_obj.get("publishedTime")),
                fetch_timestamp=_as_optional_string(metadata_obj.get("fetchedAt")),
                provider_request_id=(
                    _as_optional_string(document.get("id"))
                    or _as_optional_string(document.get("requestId"))
                    or request_id
                ),
                cache_hit=_as_optional_bool(metadata_obj.get("cacheHit")),
                fetch_age=_as_optional_number(metadata_obj.get("fetchAge")),
                credits=_as_optional_number(document.get("creditsUsed")),
                provider_score=None,
                citation_links=_string_list(item.get("links")),
            )
        else:
            results = document.get("results")
            if not isinstance(results, list) or not results or not isinstance(results[0], dict):
                raise BenchmarkError("Clusy response has no first result")
            item = cast("JsonObject", results[0])
            item_error = item.get("error")
            if isinstance(item_error, str) and item_error:
                raise ProviderResultError(f"Clusy extraction error: {item_error}")
            text = item.get("markdown")
            if not isinstance(text, str):
                raise BenchmarkError("Clusy markdown is missing")
            metadata = item.get("metadata")
            metadata_obj = cast("JsonObject", metadata) if isinstance(metadata, dict) else {}
            normalized = NormalizedResult(
                status="ok",
                error=None,
                text=normalize_text(text),
                title=_as_optional_string(metadata_obj.get("title")) or "",
                canonical_url=(
                    _as_optional_string(metadata_obj.get("canonical_url"))
                    or _as_optional_string(item.get("url"))
                    or ""
                ),
                publication_timestamp=_as_optional_string(metadata_obj.get("published_at")),
                fetch_timestamp=None,
                provider_request_id=request_id,
                cache_hit=_as_optional_bool(item.get("cached")),
                fetch_age=None,
                credits=0,
                provider_score=None,
                citation_links=_string_list(item.get("links")),
            )
    except ProviderResultError as exc:
        result = _normalization_error(str(exc), request_id)
        return NormalizedResult(**{**result.__dict__, "status": "provider_error"})
    except BenchmarkError as exc:
        return _normalization_error(str(exc), request_id)
    if not normalized.text:
        return NormalizedResult(**{**normalized.__dict__, "status": "empty_output"})
    return normalized


def redact_url(url: str) -> str:
    """Return a non-reversible URL locator without domain, path, query, or credentials."""
    if not url:
        return ""
    digest = sha256_bytes(url.encode("utf-8"))[:16]
    return f"url:[redacted]#sha256={digest}"


def redact_error(message: str | None, secrets: Sequence[str]) -> str | None:
    if message is None:
        return None
    redacted = message
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED_SECRET]")
    redacted = _URL_IN_TEXT.sub(lambda match: redact_url(match.group(0)), redacted)
    return redacted[:2000]


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def _tokenize(text: str) -> list[str]:
    """Language-neutral deterministic tokens; CJK characters remain atomic."""
    tokens: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            tokens.append("".join(current).casefold())
            current.clear()

    for character in unicodedata.normalize("NFC", text):
        codepoint = ord(character)
        is_cjk = (
            0x3400 <= codepoint <= 0x4DBF
            or 0x4E00 <= codepoint <= 0x9FFF
            or 0xF900 <= codepoint <= 0xFAFF
            or 0x3040 <= codepoint <= 0x30FF
            or 0xAC00 <= codepoint <= 0xD7AF
        )
        if is_cjk:
            flush()
            tokens.append(character)
        elif character.isalnum() or character == "_":
            current.append(character)
        else:
            flush()
    flush()
    return tokens


def score_text(candidate: str, reference: str) -> JsonObject:
    candidate_tokens = _tokenize(normalize_text(candidate))
    reference_tokens = _tokenize(normalize_text(reference))
    overlap = sum((Counter(candidate_tokens) & Counter(reference_tokens)).values())
    precision = overlap / len(candidate_tokens) if candidate_tokens else 0.0
    recall = overlap / len(reference_tokens) if reference_tokens else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "token_precision": precision,
        "token_recall": recall,
        "token_f1": f1,
        "candidate_tokens": len(candidate_tokens),
        "reference_tokens": len(reference_tokens),
        "tokenizer": "clusy-unicode-tokenizer.v1",
    }


def randomized_orders(manifest: Manifest) -> Mapping[str, tuple[Provider, ...]]:
    orders: dict[str, tuple[Provider, ...]] = {}
    for task in manifest.tasks:
        providers = sorted(
            PROVIDERS,
            key=lambda provider: hashlib.sha256(
                f"{manifest.seed}:{task.task_id}:{provider}".encode()
            ).digest(),
        )
        orders[task.task_id] = cast("tuple[Provider, ...]", tuple(providers))
    return orders


def _provider_body_fields(
    provider: Provider, wire: WireResponse
) -> tuple[str | None, bool | None, float | None, float | None]:
    """Read non-content accounting fields without changing normalization status."""
    try:
        document = _json_response(wire.body)
    except BenchmarkError:
        return None, None, None, None
    if provider == "firecrawl":
        return (
            _as_optional_string(document.get("id"))
            or _as_optional_string(document.get("requestId")),
            None,
            _as_optional_number(document.get("creditsUsed")),
            None,
        )
    if provider == "exa":
        cost_dollars = document.get("costDollars")
        cost_obj = cast("JsonObject", cost_dollars) if isinstance(cost_dollars, dict) else {}
        return (
            _as_optional_string(document.get("requestId"))
            or _as_optional_string(document.get("request_id")),
            None,
            _as_optional_number(document.get("credits")),
            _as_optional_number(cost_obj.get("total")),
        )
    return None, None, 0, 0


def build_event(
    *,
    run_id: str,
    task: Task,
    provider: Provider,
    order: tuple[Provider, ...],
    order_position: int,
    request: PreparedRequest,
    wire: WireResponse,
    result: NormalizedResult,
    raw_sha256: str,
    raw_artifact_path: str,
    normalized_artifact_path: str,
    manifest: Manifest,
    claim: ClaimContext,
    secrets: Sequence[str],
) -> JsonObject:
    error = redact_error(result.error, secrets)
    request_id, body_cache_hit, body_credits, reported_cost = _provider_body_fields(
        provider,
        wire,
    )
    credits = result.credits if result.credits is not None else body_credits
    normalized_cost = manifest.pricing[provider].per_request
    scoring = (
        score_text(result.text if result.status == "ok" else "", task.reference.text)
        if task.reference
        else None
    )
    return {
        "event_schema_version": EVENT_SCHEMA_VERSION,
        "claimable": claim.claimable,
        "watermark": claim.watermark,
        "nonclaimable_reasons": list(claim.reasons),
        "run_id": run_id,
        "task_id": task.task_id,
        "stratum": task.stratum,
        "language": task.language,
        "provider": provider,
        "endpoint": request.endpoint,
        "mode": "cold_live",
        "plan": manifest.plans[provider],
        "api_version": request.api_version,
        "sdk_version": f"httpx/{httpx.__version__}",
        "runner_commit": claim.runner_commit,
        "runner_sha256": claim.runner_sha256,
        "container_digest": claim.container_digest,
        "manifest_sha256": manifest.digest,
        "utc_timestamp": wire.started_at,
        "runner_region": manifest.runner_region,
        "country": manifest.country,
        "location": manifest.location,
        "provider_fetch_geo_control": "unsupported_common_denominator",
        "query_or_seed": redact_url(task.url),
        "query_sha256": sha256_bytes(task.url.encode("utf-8")),
        "top_k": None,
        "limit": 1,
        "depth": 0,
        "scope": manifest.scope,
        "domain_filters": [],
        "cache_policy": "cold_live_no_cache",
        "max_age": 0,
        "content_format": "markdown",
        "token_budget": None,
        "timeout": manifest.timeout_seconds,
        "retry": 0,
        "attempt": 1,
        "randomized_order": list(order),
        "order_position": order_position,
        "randomization_seed": manifest.seed,
        "randomization_algorithm": "sha256-sort.v1",
        "started_at": wire.started_at,
        "first_byte_at": wire.first_byte_at,
        "completed_at": wire.completed_at,
        "latency_ms": wire.latency_ms,
        "http_status": wire.status_code,
        "status": result.status,
        "error": error,
        "provider_request_id": redact_error(
            result.provider_request_id or request_id,
            secrets,
        ),
        "cache_hit": result.cache_hit if result.cache_hit is not None else body_cache_hit,
        "fetch_age": result.fetch_age,
        "credits": credits,
        "normalized_cost": normalized_cost,
        "normalized_cost_currency": manifest.pricing[provider].currency,
        "normalized_cost_source": "frozen_manifest_per_request",
        "provider_reported_cost": reported_cost,
        "provider_reported_cost_currency": "USD" if reported_cost is not None else None,
        "raw_response_sha256": raw_sha256,
        "raw_response_bytes": len(wire.body),
        "raw_response_present": wire.status_code is not None or bool(wire.body),
        "raw_response_complete": wire.transport_error is None,
        "immutable_artifact_path": raw_artifact_path,
        "normalized_artifact_path": normalized_artifact_path,
        "rank": 1,
        "original_url": redact_url(task.url),
        "canonical_url": redact_url(result.canonical_url),
        "title": result.title,
        "snippet": "",
        "highlights": [],
        "text": result.text,
        "normalized_text_sha256": sha256_bytes(result.text.encode("utf-8")),
        "character_count": len(result.text),
        "token_count": len(_tokenize(result.text)),
        "token_count_method": "clusy-unicode-tokenizer.v1",
        "publication_timestamp": result.publication_timestamp,
        "fetch_timestamp": result.fetch_timestamp,
        "citation_links": [redact_url(url) for url in result.citation_links],
        "provider_score": result.provider_score,
        "reference_sha256": task.reference.sha256 if task.reference else None,
        "reference_method": task.reference.method if task.reference else None,
        "scoring": scoring,
    }


def _make_default_client() -> httpx.Client:
    return httpx.Client(follow_redirects=False, http2=True)


def _safe_run_id(manifest: Manifest) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{manifest.benchmark_id}-{stamp}-{manifest.digest[:12]}"


def execute_benchmark(
    *,
    manifest_path: Path,
    output_root: Path,
    repo_root: Path,
    execute_paid: bool,
    nonclaimable: bool,
    credentials: Credentials | None = None,
    executor_factory: Callable[[], RequestExecutor] | None = None,
    git_state: GitState | None = None,
    container_digest: str | None = None,
    bootstrap_samples: int = 10_000,
) -> Path:
    """Execute one attempt/provider/task and return the immutable run directory."""
    manifest = load_manifest(manifest_path)
    if not execute_paid:
        raise BenchmarkError(
            "paid execution is disabled; pass --execute-paid only after budget authorization"
        )
    claim = prepare_claim_context(
        repo_root=repo_root,
        nonclaimable=nonclaimable,
        git_state=git_state,
        container_digest=container_digest,
    )
    execution_credentials = credentials or Credentials.from_environment()
    execution_credentials.validate(claimable=claim.claimable)
    if bootstrap_samples < 100 or bootstrap_samples > 1_000_000:
        raise BenchmarkError("bootstrap_samples must be between 100 and 1000000")

    run_id = _safe_run_id(manifest)
    run_directory = output_root / run_id
    try:
        run_directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    except FileExistsError as exc:
        raise BenchmarkError(f"run directory already exists: {run_directory}") from exc

    metadata = {
        "run_id": run_id,
        "manifest_sha256": manifest.digest,
        "claimable": claim.claimable,
        "watermark": claim.watermark,
        "nonclaimable_reasons": list(claim.reasons),
        "runner_commit": claim.runner_commit,
        "runner_sha256": claim.runner_sha256,
        "container_digest": claim.container_digest,
        "created_at": _utc_now(),
    }
    _atomic_write(run_directory / "run.json", canonical_json_bytes(metadata) + b"\n")
    _atomic_write(
        run_directory / "manifest.json",
        canonical_json_bytes(manifest.document) + b"\n",
    )

    factory = executor_factory or (lambda: HttpxRequestExecutor(_make_default_client()))
    executor = factory()
    events: list[JsonObject] = []
    orders = randomized_orders(manifest)
    events_partial_path = run_directory / "events.jsonl.partial"
    events_hasher = hashlib.sha256()
    try:
        with events_partial_path.open("xb", buffering=0) as journal:
            os.chmod(events_partial_path, 0o600)
            for task_index, task in enumerate(manifest.tasks):
                order = orders[task.task_id]
                for position, provider in enumerate(order):
                    request = build_provider_request(
                        provider,
                        task=task,
                        manifest=manifest,
                        credentials=execution_credentials,
                    )
                    wire = executor.execute(request)
                    raw_sha = sha256_bytes(wire.body)
                    stem = f"{task_index:06d}-{task.task_id}-{provider}-attempt1"
                    raw_relative = Path("raw") / f"{stem}.body"
                    _atomic_write(run_directory / raw_relative, wire.body)
                    result = normalize_provider_response(provider, wire)
                    normalized_document = {
                        "provider": provider,
                        "task_id": task.task_id,
                        "status": result.status,
                        "error": redact_error(
                            result.error,
                            execution_credentials.secret_values,
                        ),
                        "text": result.text,
                        "title": result.title,
                        "canonical_url": redact_url(result.canonical_url),
                    }
                    normalized_relative = Path("normalized") / f"{stem}.json"
                    _atomic_write(
                        run_directory / normalized_relative,
                        canonical_json_bytes(normalized_document) + b"\n",
                    )
                    event = build_event(
                        run_id=run_id,
                        task=task,
                        provider=provider,
                        order=order,
                        order_position=position,
                        request=request,
                        wire=wire,
                        result=result,
                        raw_sha256=raw_sha,
                        raw_artifact_path=str(raw_relative),
                        normalized_artifact_path=str(normalized_relative),
                        manifest=manifest,
                        claim=claim,
                        secrets=execution_credentials.secret_values,
                    )
                    events.append(event)
                    event_line = canonical_json_bytes(event) + b"\n"
                    journal.write(event_line)
                    events_hasher.update(event_line)
                    journal.flush()
                    os.fsync(journal.fileno())
    finally:
        executor.close()

    events_path = run_directory / "events.jsonl"
    os.replace(events_partial_path, events_path)
    directory_descriptor = os.open(run_directory, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    summary = summarize_events(
        manifest,
        events,
        bootstrap_samples=bootstrap_samples,
        events_sha256=events_hasher.hexdigest(),
    )
    if claim.claimable and summary.get("claimable") is not True:
        raise BenchmarkError("internal evidence validation refused a claimable completion")
    summary_bytes = canonical_json_bytes(summary) + b"\n"
    _atomic_write(
        run_directory / "summary.json",
        summary_bytes,
    )
    completion = {
        "run_id": run_id,
        "claimable": summary["claimable"],
        "watermark": summary["watermark"],
        "manifest_sha256": manifest.digest,
        "events_sha256": events_hasher.hexdigest(),
        "summary_sha256": sha256_bytes(summary_bytes),
        "completed_at": _utc_now(),
    }
    _atomic_write(
        run_directory / "completion.json",
        canonical_json_bytes(completion) + b"\n",
    )
    for artifact in run_directory.rglob("*"):
        if artifact.is_file():
            artifact.chmod(0o400)
    return run_directory


def _percentile(sorted_values: Sequence[float], quantile: float) -> float:
    if not sorted_values:
        raise BenchmarkError("cannot calculate a percentile of no values")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = quantile * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def deterministic_bootstrap_ci(
    values: Sequence[float],
    *,
    seed: int,
    samples: int,
) -> tuple[float, float]:
    if not values:
        raise BenchmarkError("cannot bootstrap no paired values")
    if samples < 100:
        raise BenchmarkError("at least 100 bootstrap samples are required")
    generator = random.Random(seed)
    count = len(values)
    means = [
        sum(values[generator.randrange(count)] for _ in range(count)) / count
        for _ in range(samples)
    ]
    means.sort()
    return _percentile(means, 0.025), _percentile(means, 0.975)


def _pair_seed(manifest_seed: int, metric: str, left: str, right: str) -> int:
    material = f"{manifest_seed}:{metric}:{left}:{right}".encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def _distribution(values: Sequence[float]) -> JsonObject | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "mean": sum(ordered) / len(ordered),
        "p50": _percentile(ordered, 0.50),
        "p90": _percentile(ordered, 0.90),
        "p95": _percentile(ordered, 0.95),
        "p99": _percentile(ordered, 0.99),
    }


def _metric_value(
    event: Mapping[str, Any],
    metric: str,
    *,
    reference: Reference | None,
) -> float | None:
    if metric == "success":
        return 1.0 if event.get("status") == "ok" else 0.0
    if metric == "latency_ms":
        return _as_optional_number(event.get("latency_ms"))
    if metric == "normalized_cost":
        return _as_optional_number(event.get("normalized_cost"))
    if metric == "token_f1":
        if reference is None:
            return None
        if event.get("status") != "ok":
            return 0.0
        text = event.get("text")
        if not isinstance(text, str):
            return 0.0
        return cast("float", score_text(text, reference.text)["token_f1"])
    return None


def _provider_group_summary(
    manifest: Manifest,
    events: Sequence[Mapping[str, Any]],
) -> JsonObject:
    references = {task.task_id: task.reference for task in manifest.tasks}
    successes = [1.0 if event.get("status") == "ok" else 0.0 for event in events]
    latencies = [
        value
        for event in events
        if (value := _as_optional_number(event.get("latency_ms"))) is not None
    ]
    costs = [
        value
        for event in events
        if (value := _as_optional_number(event.get("normalized_cost"))) is not None
    ]
    token_f1: list[float] = []
    for event in events:
        task_id = event.get("task_id")
        reference = references.get(task_id) if isinstance(task_id, str) else None
        value = _metric_value(event, "token_f1", reference=reference)
        if value is not None:
            token_f1.append(value)
    return {
        "task_count": len(events),
        "success_rate": sum(successes) / len(successes) if successes else None,
        "latency_ms": _distribution(latencies),
        "normalized_cost": {
            "observed_count": len(costs),
            "total": sum(costs),
            "mean": sum(costs) / len(costs) if costs else None,
        },
        "reference_task_count": len(token_f1),
        "mean_token_f1": sum(token_f1) / len(token_f1) if token_f1 else None,
    }


def summarize_events(
    manifest: Manifest,
    events: Sequence[Mapping[str, Any]],
    *,
    bootstrap_samples: int = 10_000,
    events_sha256: str | None = None,
) -> JsonObject:
    """Produce paired per-task deltas and deterministic bootstrap intervals."""
    by_task_provider: dict[tuple[str, str], Mapping[str, Any]] = {}
    duplicate_first_attempt = False
    for event in events:
        task_id = event.get("task_id")
        provider = event.get("provider")
        attempt = event.get("attempt")
        if isinstance(task_id, str) and provider in PROVIDERS and attempt == 1:
            key = (task_id, cast("str", provider))
            if key in by_task_provider:
                duplicate_first_attempt = True
            else:
                by_task_provider[key] = event
    metrics = (
        ("success", True),
        ("latency_ms", False),
        ("normalized_cost", False),
        ("token_f1", True),
    )
    pairwise: list[JsonObject] = []
    for left, right in combinations(PROVIDERS, 2):
        for metric, higher_is_better in metrics:
            per_task: list[JsonObject] = []
            deltas: list[float] = []
            for task in manifest.tasks:
                left_event = by_task_provider.get((task.task_id, left))
                right_event = by_task_provider.get((task.task_id, right))
                if left_event is None or right_event is None:
                    continue
                left_value = _metric_value(
                    left_event,
                    metric,
                    reference=task.reference,
                )
                right_value = _metric_value(
                    right_event,
                    metric,
                    reference=task.reference,
                )
                if left_value is None or right_value is None:
                    continue
                delta = left_value - right_value
                deltas.append(delta)
                per_task.append(
                    {
                        "task_id": task.task_id,
                        "stratum": task.stratum,
                        "left": left_value,
                        "right": right_value,
                        "delta": delta,
                    }
                )
            if not deltas:
                continue
            lower, upper = deterministic_bootstrap_ci(
                deltas,
                seed=_pair_seed(manifest.seed, metric, left, right),
                samples=bootstrap_samples,
            )
            stratum_deltas: dict[str, list[float]] = {}
            for row in per_task:
                stratum_deltas.setdefault(cast("str", row["stratum"]), []).append(
                    cast("float", row["delta"])
                )
            by_stratum: list[JsonObject] = []
            for stratum, values in sorted(stratum_deltas.items()):
                stratum_lower, stratum_upper = deterministic_bootstrap_ci(
                    values,
                    seed=_pair_seed(
                        manifest.seed,
                        f"{metric}:{stratum}",
                        left,
                        right,
                    ),
                    samples=bootstrap_samples,
                )
                by_stratum.append(
                    {
                        "stratum": stratum,
                        "paired_task_count": len(values),
                        "mean_delta_left_minus_right": sum(values) / len(values),
                        "bootstrap_ci_95": [stratum_lower, stratum_upper],
                    }
                )
            pairwise.append(
                {
                    "left_provider": left,
                    "right_provider": right,
                    "metric": metric,
                    "higher_is_better": higher_is_better,
                    "paired_task_count": len(deltas),
                    "mean_delta_left_minus_right": sum(deltas) / len(deltas),
                    "bootstrap_samples": bootstrap_samples,
                    "bootstrap_rng": "python-mt19937-with-sha256-derived-seed.v1",
                    "bootstrap_ci_95": [lower, upper],
                    "by_stratum": by_stratum,
                    "per_task_deltas": per_task,
                }
            )
    claims = {event.get("claimable") for event in events}
    expected_keys = {(task.task_id, provider) for task in manifest.tasks for provider in PROVIDERS}
    manifest_matches = all(event.get("manifest_sha256") == manifest.digest for event in events)
    evidence_fields_valid = all(
        event.get("event_schema_version") == EVENT_SCHEMA_VERSION
        and isinstance(event.get("run_id"), str)
        and bool(event.get("run_id"))
        and isinstance(event.get("runner_commit"), str)
        and bool(re.fullmatch(r"[0-9a-f]{40,64}", cast("str", event.get("runner_commit"))))
        and isinstance(event.get("runner_sha256"), str)
        and bool(_SHA256.fullmatch(cast("str", event.get("runner_sha256"))))
        and isinstance(event.get("container_digest"), str)
        and bool(_CONTAINER_DIGEST.fullmatch(cast("str", event.get("container_digest"))))
        and isinstance(event.get("raw_response_sha256"), str)
        and bool(_SHA256.fullmatch(cast("str", event.get("raw_response_sha256"))))
        and isinstance(event.get("normalized_text_sha256"), str)
        and bool(_SHA256.fullmatch(cast("str", event.get("normalized_text_sha256"))))
        for event in events
    )
    events_digest_valid = isinstance(events_sha256, str) and bool(_SHA256.fullmatch(events_sha256))
    artifact_integrity_claimable = (
        bool(events)
        and claims == {True}
        and set(by_task_provider) == expected_keys
        and len(events) == len(expected_keys)
        and not duplicate_first_attempt
        and manifest_matches
        and evidence_fields_valid
        and events_digest_valid
    )
    provider_summaries: list[JsonObject] = []
    for provider in PROVIDERS:
        provider_events = [
            event
            for event in events
            if event.get("provider") == provider and event.get("attempt") == 1
        ]
        strata = sorted(
            {
                cast("str", event.get("stratum"))
                for event in provider_events
                if isinstance(event.get("stratum"), str)
            }
        )
        provider_summaries.append(
            {
                "provider": provider,
                **_provider_group_summary(manifest, provider_events),
                "by_stratum": [
                    {
                        "stratum": stratum,
                        **_provider_group_summary(
                            manifest,
                            [
                                event
                                for event in provider_events
                                if event.get("stratum") == stratum
                            ],
                        ),
                    }
                    for stratum in strata
                ],
            }
        )
    return {
        "summary_schema_version": SUMMARY_SCHEMA_VERSION,
        "manifest_sha256": manifest.digest,
        "events_sha256": events_sha256,
        # Backward-compatible field: this validates the artifact matrix and
        # provenance only. It is intentionally not a vendor-win assertion.
        "claimable": artifact_integrity_claimable,
        "claimable_scope": "artifact_integrity_only",
        "artifact_integrity_claimable": artifact_integrity_claimable,
        "vendor_win_claimable": False,
        "vendor_win_watermark": "NO_VENDOR_WIN_CLAIM",
        "vendor_win_gate": {
            "passed": False,
            "observed_independent_time_windows": 1 if events else 0,
            "required_independent_time_windows": 2,
            "structural_fidelity_metrics_complete": False,
            "teds_complete": False,
            "cold_and_warm_tail_latency_complete": False,
            "reason": (
                "This v1 runner validates one cold fixed-URL artifact window. "
                "Structural references, TEDS, a matched warm track, and a "
                "second independent time window are required for a vendor win."
            ),
        },
        "watermark": (
            "" if artifact_integrity_claimable else "NONCLAIMABLE"
        ),
        "event_count": len(events),
        "first_attempt_only": True,
        "complete_provider_task_matrix": set(by_task_provider) == expected_keys,
        "duplicate_first_attempt": duplicate_first_attempt,
        "event_manifest_matches": manifest_matches,
        "evidence_fields_valid": evidence_fields_valid,
        "events_digest_valid": events_digest_valid,
        "provider_summaries": provider_summaries,
        "pairwise": pairwise,
        "interpretation": (
            "Descriptive paired estimates only. Apply the pre-registered launch gates; "
            "this file does not declare a vendor winner."
        ),
    }


def read_jsonl(path: Path) -> list[JsonObject]:
    records: list[JsonObject] = []
    try:
        with path.open("rb") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    records.append(_load_json_bytes(line))
                except BenchmarkError as exc:
                    raise BenchmarkError(f"{path}:{line_number}: {exc}") from exc
    except OSError as exc:
        raise BenchmarkError(f"cannot read {path}: {exc}") from exc
    return records


def _resolve_artifact(run_directory: Path, relative_value: Any) -> Path:
    if not isinstance(relative_value, str) or not relative_value:
        raise BenchmarkError("event artifact path must be a non-empty string")
    relative = Path(relative_value)
    if relative.is_absolute():
        raise BenchmarkError("event artifact path must be relative")
    root = run_directory.resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise BenchmarkError("event artifact path escapes the run directory")
    return candidate


def verify_event_artifacts(
    *,
    manifest: Manifest,
    events: Sequence[Mapping[str, Any]],
    run_directory: Path,
) -> None:
    """Fail closed if offline inputs no longer match their sealed artifacts."""
    valid_tasks = {task.task_id for task in manifest.tasks}
    for index, event in enumerate(events):
        if event.get("manifest_sha256") != manifest.digest:
            raise BenchmarkError(f"event {index}: manifest_sha256 mismatch")
        if event.get("task_id") not in valid_tasks or event.get("provider") not in PROVIDERS:
            raise BenchmarkError(f"event {index}: task/provider is outside the manifest")
        raw_path = _resolve_artifact(run_directory, event.get("immutable_artifact_path"))
        try:
            raw_data = raw_path.read_bytes()
        except OSError as exc:
            raise BenchmarkError(f"event {index}: cannot read raw artifact: {exc}") from exc
        expected_raw_sha = event.get("raw_response_sha256")
        if not isinstance(expected_raw_sha, str) or sha256_bytes(raw_data) != expected_raw_sha:
            raise BenchmarkError(f"event {index}: raw response artifact hash mismatch")

        text = event.get("text")
        expected_text_sha = event.get("normalized_text_sha256")
        if (
            not isinstance(text, str)
            or not isinstance(expected_text_sha, str)
            or sha256_bytes(text.encode("utf-8")) != expected_text_sha
        ):
            raise BenchmarkError(f"event {index}: normalized text hash mismatch")
        normalized_path = _resolve_artifact(
            run_directory,
            event.get("normalized_artifact_path"),
        )
        try:
            normalized_document = _load_json_bytes(normalized_path.read_bytes())
        except OSError as exc:
            raise BenchmarkError(f"event {index}: cannot read normalized artifact: {exc}") from exc
        if (
            normalized_document.get("task_id") != event.get("task_id")
            or normalized_document.get("provider") != event.get("provider")
            or normalized_document.get("status") != event.get("status")
            or normalized_document.get("text") != text
        ):
            raise BenchmarkError(f"event {index}: normalized artifact mismatch")


def score_existing_run(
    *,
    manifest_path: Path,
    events_path: Path,
    output_path: Path,
    bootstrap_samples: int,
) -> None:
    manifest = load_manifest(manifest_path)
    events = read_jsonl(events_path)
    verify_event_artifacts(
        manifest=manifest,
        events=events,
        run_directory=events_path.parent,
    )
    if output_path.exists():
        raise BenchmarkError(f"refusing to overwrite existing summary: {output_path}")
    summary = summarize_events(
        manifest,
        events,
        bootstrap_samples=bootstrap_samples,
        events_sha256=sha256_bytes(events_path.read_bytes()),
    )
    _atomic_write(output_path, canonical_json_bytes(summary) + b"\n")
    output_path.chmod(0o400)


def _write_new_file(path: Path, data: bytes) -> None:
    if path.exists():
        raise BenchmarkError(f"refusing to overwrite existing file: {path}")
    _atomic_write(path, data, mode=0o400)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate a sealed manifest offline")
    validate.add_argument("--manifest", required=True, type=Path)

    seal = subparsers.add_parser("seal", help="seal a draft manifest offline")
    seal.add_argument("--input", required=True, type=Path)
    seal.add_argument("--output", required=True, type=Path)

    run = subparsers.add_parser("run", help="execute the sealed fixed-URL benchmark")
    run.add_argument("--manifest", required=True, type=Path)
    run.add_argument("--output-root", required=True, type=Path)
    run.add_argument("--repo-root", type=Path, default=RUNNER_PATH.parent.parent)
    run.add_argument(
        "--execute-paid",
        action="store_true",
        help="authorize API execution after an external budget approval",
    )
    run.add_argument(
        "--nonclaimable",
        action="store_true",
        help="explicitly watermark output nonclaimable and permit a dirty checkout",
    )
    run.add_argument("--bootstrap-samples", type=int, default=10_000)

    score = subparsers.add_parser("score", help="score existing event JSONL offline")
    score.add_argument("--manifest", required=True, type=Path)
    score.add_argument("--events", required=True, type=Path)
    score.add_argument("--output", required=True, type=Path)
    score.add_argument("--bootstrap-samples", type=int, default=10_000)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    try:
        if arguments.command == "validate":
            manifest = load_manifest(arguments.manifest)
            print(
                canonical_json_bytes(
                    {
                        "valid": True,
                        "manifest_sha256": manifest.digest,
                        "tasks": len(manifest.tasks),
                    }
                ).decode()
            )
        elif arguments.command == "seal":
            raw = arguments.input.read_bytes()
            if len(raw) > MAX_MANIFEST_BYTES:
                raise BenchmarkError("manifest exceeds the 10 MB safety limit")
            document = seal_manifest_document(_load_json_bytes(raw))
            # Parse before writing, so seal never emits an unusable artifact.
            parse_manifest(document)
            _write_new_file(
                arguments.output,
                canonical_json_bytes(document) + b"\n",
            )
        elif arguments.command == "run":
            run_directory = execute_benchmark(
                manifest_path=arguments.manifest,
                output_root=arguments.output_root,
                repo_root=arguments.repo_root,
                execute_paid=arguments.execute_paid,
                nonclaimable=arguments.nonclaimable,
                bootstrap_samples=arguments.bootstrap_samples,
            )
            print(str(run_directory))
        elif arguments.command == "score":
            score_existing_run(
                manifest_path=arguments.manifest,
                events_path=arguments.events,
                output_path=arguments.output,
                bootstrap_samples=arguments.bootstrap_samples,
            )
            print(str(arguments.output))
        else:
            raise BenchmarkError("unknown command")
    except (BenchmarkError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

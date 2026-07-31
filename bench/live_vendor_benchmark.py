#!/usr/bin/env python3
"""Sealed, fixed-URL extraction benchmark for Clusy, Exa, and Firecrawl.

This runner deliberately has no implicit execution mode.  ``validate`` and
``score`` are offline.  ``run`` refuses to create an HTTP client unless all of
the following are true:

* the manifest is frozen, sealed, and has a valid content digest;
* ``--execute-paid`` was passed;
* credentials for every selected provider and the Clusy endpoint are present;
* selected-provider budget caps and compliance acknowledgments are explicit;
* the checkout is clean and the runner is committed, unless the operator
  explicitly selected ``--nonclaimable``.

Provider bodies are scored in memory; v3 persists only hashes and derived
metrics.  The module is importable so tests and internal orchestration can
inject an HTTPX mock transport without touching the network.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import random
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path
from typing import Any, Final, Literal, Protocol, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

V1_SCHEMA_VERSION: Final = "clusy.live-vendor.fixed-url.v1"
V1_EVENT_SCHEMA_VERSION: Final = "clusy.live-vendor.event.v1"
V1_SUMMARY_SCHEMA_VERSION: Final = "clusy.live-vendor.summary.v1"
LEGACY_V2_SCHEMA_VERSION: Final = "clusy.live-vendor.fixed-url.v2"
SCHEMA_VERSION: Final = "clusy.live-vendor.fixed-url.v3"
EVENT_SCHEMA_VERSION: Final = "clusy.live-vendor.event.v3"
SUMMARY_SCHEMA_VERSION: Final = "clusy.live-vendor.summary.v3"
AGGREGATE_SCHEMA_VERSION: Final = "clusy.live-vendor.aggregate.v3"
PROVIDERS: Final = ("clusy", "exa", "firecrawl")
MIN_CLAIM_PAIRED_TASKS: Final = 100
MIN_CLAIM_DOMAIN_CLUSTERS: Final = 30
MIN_CLAIM_STRATA: Final = 3
MIN_CLAIM_STRATUM_DOMAIN_CLUSTERS: Final = 10
MIN_INDEPENDENT_WINDOW_SPACING_SECONDS: Final = 24 * 60 * 60
MAX_CLAIM_P95_LATENCY_RATIO: Final = 1.10
MAX_CLAIM_P99_LATENCY_RATIO: Final = 1.10
MAX_CLAIM_NORMALIZED_COST_RATIO: Final = 1.00
MAX_BENCHMARK_ID_LENGTH: Final = 98
RUNNER_PATH: Final = Path(__file__).resolve()
MAX_MANIFEST_BYTES: Final = 10_000_000
MAX_TASKS: Final = 20_000
MAX_RESPONSE_BYTES: Final = 128 * 1024 * 1024
MAX_RUN_ARTIFACT_BYTES: Final = 512 * 1024 * 1024
EXA_ENDPOINT: Final = "https://api.exa.ai/contents"
FIRECRAWL_ENDPOINT: Final = "https://api.firecrawl.dev/v2/scrape"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTAINER_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_REVISION = re.compile(r"^[0-9a-f]{7,64}$")
_SENSITIVE_QUERY_NAMES: Final = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "code",
        "credential",
        "jwt",
        "key",
        "password",
        "passwd",
        "secret",
        "session",
        "session_id",
        "sessionid",
        "sig",
        "signature",
        "signed",
        "token",
        "x-amz-credential",
        "x-amz-security-token",
        "x-amz-signature",
        "x-goog-credential",
        "x-goog-signature",
    }
)
_SPECIAL_HOST_SUFFIXES: Final = (
    ".internal",
    ".invalid",
    ".local",
    ".localhost",
    ".test",
)
_STAGE_TIMING_KEYS: Final = ("queue", "fetch", "render", "extraction", "total")
_URL_UNRESERVED: Final = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_STRUCTURE_STRATA: Final = ("headings", "lists", "code", "tables")
_CLAIM_REASON_ORDER: Final = (
    "operator explicitly selected nonclaimable mode",
    "working tree is dirty",
    "runner is uncommitted or differs from HEAD",
    "CONTAINER_DIGEST is missing or not a sha256 digest",
)
_STATUS_VALUES: Final = frozenset(
    {
        "ok",
        "malformed_response",
        "transport_error",
        "http_error",
        "provider_error",
        "empty_output",
    }
)
_CACHE_STATE_VALUES: Final = frozenset({"hit", "miss", "unknown"})
_REDACTED_URL = re.compile(r"^url:\[redacted\]#sha256=[0-9a-f]{16}$")
_V3_UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
LATENCY_TIMESTAMP_TOLERANCE_MS: Final = 5.0
STAGE_TIMING_TOLERANCE_MS: Final = 10.0
SCORE_FLOAT_TOLERANCE: Final = 1e-12

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


def calculate_corpus_sha256(tasks: Any) -> str:
    """Bind the declared corpus to the exact ordered task/reference records."""
    if not isinstance(tasks, list):
        raise BenchmarkError("tasks must be an array before corpus hashing")
    return sha256_bytes(canonical_json_bytes({"tasks": tasks}))


def seal_manifest_document(document: Mapping[str, Any]) -> JsonObject:
    """Return a frozen, sealed copy with a freshly calculated digest."""
    sealed = dict(document)
    if sealed.get("schema_version") == SCHEMA_VERSION:
        sealed["corpus_sha256"] = calculate_corpus_sha256(sealed.get("tasks"))
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
    if (
        not math.isfinite(number)
        or number < minimum
        or (number == 0 and math.copysign(1.0, number) < 0)
    ):
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


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.is_global


def _validate_public_https_url(url: str, *, field_name: str) -> str:
    """Validate the non-network portion of a sealed public benchmark URL."""
    _validate_http_url(url, field_name=field_name)
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise BenchmarkError(f"{field_name} must use HTTPS")
    hostname = (parsed.hostname or "").rstrip(".").casefold()
    if not hostname or hostname == "localhost" or hostname.endswith(_SPECIAL_HOST_SUFFIXES):
        raise BenchmarkError(f"{field_name} must name a public DNS host")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is None and "." not in hostname:
        raise BenchmarkError(f"{field_name} must name a public DNS host")
    if literal is not None and not literal.is_global:
        raise BenchmarkError(f"{field_name} must not target a private or special IP")
    if literal is None:
        try:
            ascii_hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise BenchmarkError(f"{field_name} contains an invalid DNS host") from exc
        labels = ascii_hostname.split(".")
        if len(ascii_hostname) > 253 or any(
            not label or not _DNS_LABEL.fullmatch(label) for label in labels
        ):
            raise BenchmarkError(f"{field_name} contains an invalid DNS host")
    if ";" in parsed.query or re.search(r"%3[bB]", parsed.query):
        raise BenchmarkError(f"{field_name} query must not use ambiguous semicolon separators")
    for name, _value in parse_qsl(parsed.query, keep_blank_values=True):
        normalized_name = name.strip().casefold().replace("-", "_")
        if ";" in normalized_name:
            raise BenchmarkError(f"{field_name} query contains an ambiguous parameter name")
        if (
            normalized_name in _SENSITIVE_QUERY_NAMES
            or normalized_name.startswith("x_amz_")
            or normalized_name.startswith("x_goog_")
        ):
            raise BenchmarkError(f"{field_name} contains a sensitive query parameter name: {name}")
    return hostname


def _normalize_percent_encoding(value: str, *, field_name: str) -> str:
    if any(ord(character) > 0x7F for character in value):
        raise BenchmarkError(f"{field_name} must use ASCII with UTF-8 percent encoding")
    result: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character != "%":
            result.append(character)
            index += 1
            continue
        if index + 2 >= len(value):
            raise BenchmarkError(f"{field_name} contains an incomplete percent escape")
        encoded = value[index + 1 : index + 3]
        if not re.fullmatch(r"[0-9A-Fa-f]{2}", encoded):
            raise BenchmarkError(f"{field_name} contains an invalid percent escape")
        decoded = chr(int(encoded, 16))
        result.append(decoded if decoded in _URL_UNRESERVED else f"%{encoded.upper()}")
        index += 3
    return "".join(result)


def canonical_task_url_identity(url: str) -> str:
    """Return a conservative RFC-3986-style identity for duplicate detection."""
    _validate_public_https_url(url, field_name="task URL")
    parsed = urlsplit(url)
    raw_hostname = (parsed.hostname or "").rstrip(".").casefold()
    try:
        hostname = raw_hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise BenchmarkError("task URL hostname cannot be canonicalized") from exc
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        hostname = literal.compressed
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if parsed.port not in {None, 443}:
        netloc = f"{netloc}:{parsed.port}"
    path = _normalize_percent_encoding(
        parsed.path or "/",
        field_name="task URL path",
    )
    if any(segment in {".", ".."} for segment in path.split("/")):
        raise BenchmarkError("task URL path must not contain dot segments")
    normalized_query = _normalize_percent_encoding(
        parsed.query,
        field_name="task URL query",
    )
    query = urlencode(
        sorted(parse_qsl(normalized_query, keep_blank_values=True)),
        doseq=True,
    )
    return urlunsplit(("https", netloc, path, query, ""))


def derive_domain_cluster(url: str) -> str:
    """Derive a conservative anti-inflation domain unit from the URL host.

    The last two DNS labels intentionally form a lower-bound grouping rather
    than pretending to implement a public-suffix list.  It can merge unrelated
    sites beneath multi-label suffixes, which is conservative for evidence
    counts, while never letting subdomains inflate the count.
    """
    canonical = urlsplit(canonical_task_url_identity(url))
    hostname = canonical.hostname or ""
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        return literal.compressed
    labels = hostname.split(".")
    return ".".join(labels[-2:])


DnsResolver = Callable[[str, int], Sequence[tuple[Any, ...]]]


def _default_dns_resolver(hostname: str, port: int) -> Sequence[tuple[Any, ...]]:
    return socket.getaddrinfo(
        hostname,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
    )


def validate_public_dns_targets(
    manifest: Manifest,
    *,
    resolver: DnsResolver = _default_dns_resolver,
) -> None:
    """Resolve every sealed target before paid calls and reject any non-public answer."""
    resolved_hosts: dict[str, tuple[str, ...]] = {}
    for task in manifest.tasks:
        hostname = _validate_public_https_url(
            task.url,
            field_name=f"task {task.task_id} URL",
        )
        if hostname in resolved_hosts:
            continue
        try:
            answers = resolver(hostname, 443)
        except OSError as exc:
            raise BenchmarkError(f"cannot resolve task host {hostname}: {exc}") from exc
        addresses = sorted(
            {
                str(sockaddr[0])
                for answer in answers
                if len(answer) >= 5 and isinstance((sockaddr := answer[4]), tuple) and sockaddr
            }
        )
        if not addresses:
            raise BenchmarkError(f"task host {hostname} resolved to no addresses")
        if any(not _is_public_ip(address) for address in addresses):
            raise BenchmarkError(f"task host {hostname} resolved to a private or special address")
        resolved_hosts[hostname] = tuple(addresses)


def _parse_iso_timestamp(value: str, *, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BenchmarkError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise BenchmarkError(f"{field_name} must include a timezone")
    return parsed


def _format_v3_utc_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_v3_utc_timestamp(value: str, *, field_name: str) -> datetime:
    """Parse the one canonical v3 UTC timestamp representation."""
    if not _V3_UTC_TIMESTAMP.fullmatch(value):
        raise BenchmarkError(f"{field_name} must use canonical UTC YYYY-MM-DDTHH:MM:SS.ffffffZ")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise BenchmarkError(
            f"{field_name} must use canonical UTC YYYY-MM-DDTHH:MM:SS.ffffffZ"
        ) from exc
    if _format_v3_utc_timestamp(parsed) != value:
        raise BenchmarkError(f"{field_name} is not a round-trip canonical UTC timestamp")
    return parsed


@dataclass(frozen=True)
class StructureReference:
    headings: tuple[str, ...]
    list_items: tuple[str, ...]
    code_blocks: tuple[str, ...]
    tables: tuple[tuple[tuple[str, ...], ...], ...]


@dataclass(frozen=True)
class Reference:
    text: str
    sha256: str
    method: str
    captured_at: str | None
    structure: StructureReference | None


def reference_structure_strata(reference: Reference | None) -> tuple[str, ...]:
    if reference is None or reference.structure is None:
        return ()
    structure = reference.structure
    present = {
        "headings": bool(structure.headings),
        "lists": bool(structure.list_items),
        "code": bool(structure.code_blocks),
        "tables": bool(structure.tables),
    }
    return tuple(name for name in _STRUCTURE_STRATA if present[name])


@dataclass(frozen=True)
class Task:
    task_id: str
    url: str
    stratum: str
    language: str
    reference: Reference | None
    domain_cluster: str
    content_type: str
    render_class: str
    firecrawl_credit_cap: float


@dataclass(frozen=True)
class Pricing:
    currency: str
    per_request: float


@dataclass(frozen=True)
class BudgetCaps:
    exa_usd: float
    firecrawl_credits: float
    clusy_usd: float


@dataclass(frozen=True)
class RequestPolicy:
    max_output_characters: int
    firecrawl_proxy: str
    firecrawl_block_ads: bool
    firecrawl_only_clean_content: bool
    firecrawl_parse_pdf: bool
    clusy_js_render: str


@dataclass(frozen=True)
class ClusyBinding:
    expected_revision: str
    expected_config_sha256: str
    expected_image_digest: str


@dataclass(frozen=True)
class ComplianceAcknowledgments:
    third_party_data_transfer_authorized: bool
    exa_live_authorized: bool
    exa_authorized_purpose: str


@dataclass(frozen=True)
class Manifest:
    document: JsonObject = field(repr=False)
    schema_version: str
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
    providers: tuple[Provider, ...]
    plans: Mapping[Provider, str]
    pricing: Mapping[Provider, Pricing]
    tasks: tuple[Task, ...]
    mode: str
    corpus_sha256: str
    time_window_id: str
    independent_window_index: int
    required_independent_windows: int
    cache_max_age_seconds: int
    warm_cache_primed_at: str | None
    bootstrap_samples: int
    budgets: BudgetCaps | None
    request_policy: RequestPolicy | None
    clusy_binding: ClusyBinding | None
    compliance_acknowledgments: ComplianceAcknowledgments | None


@dataclass(frozen=True)
class WindowTimingEvidence:
    run_id: str
    mode: str
    time_window_id: str
    independent_window_index: int
    required_independent_windows: int
    cache_max_age_seconds: int
    manifest_created_at: datetime
    warm_cache_primed_at: datetime | None
    run_created_at: datetime
    first_request_started_at: datetime
    oldest_request_completed_at: datetime
    last_request_completed_at: datetime
    completion_at: datetime


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


def _parse_string_array(
    value: Any,
    *,
    field_name: str,
    max_items: int = 10_000,
    max_item_length: int = 1_000_000,
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > max_items:
        raise BenchmarkError(f"{field_name} must be a bounded array")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or len(item) > max_item_length:
            raise BenchmarkError(f"{field_name}[{index}] must be a bounded string")
        result.append(item)
    return tuple(result)


def _parse_structure_reference(value: Any, task_id: str) -> StructureReference:
    if not isinstance(value, dict):
        raise BenchmarkError(f"task {task_id}: reference.structure must be an object")
    allowed = {"headings", "list_items", "code_blocks", "tables"}
    unknown = set(value) - allowed
    if unknown:
        raise BenchmarkError(
            f"task {task_id}: reference.structure has unknown fields: {sorted(unknown)}"
        )
    tables_raw = value.get("tables")
    if not isinstance(tables_raw, list) or len(tables_raw) > 1_000:
        raise BenchmarkError(f"task {task_id}: reference.structure.tables is invalid")
    tables: list[tuple[tuple[str, ...], ...]] = []
    for table_index, table_raw in enumerate(tables_raw):
        if not isinstance(table_raw, list) or len(table_raw) > 10_000:
            raise BenchmarkError(
                f"task {task_id}: reference table {table_index} must be a bounded row array"
            )
        rows: list[tuple[str, ...]] = []
        for row_index, row_raw in enumerate(table_raw):
            rows.append(
                _parse_string_array(
                    row_raw,
                    field_name=(f"task {task_id} reference table {table_index} row {row_index}"),
                    max_items=1_000,
                    max_item_length=100_000,
                )
            )
        tables.append(tuple(rows))
    return StructureReference(
        headings=_parse_string_array(
            value.get("headings"),
            field_name=f"task {task_id} reference headings",
        ),
        list_items=_parse_string_array(
            value.get("list_items"),
            field_name=f"task {task_id} reference list_items",
        ),
        code_blocks=_parse_string_array(
            value.get("code_blocks"),
            field_name=f"task {task_id} reference code_blocks",
        ),
        tables=tuple(tables),
    )


def _parse_reference(
    value: Any,
    task_id: str,
    *,
    require_current: bool,
) -> Reference | None:
    if value is None:
        if require_current:
            raise BenchmarkError(f"task {task_id}: v3 requires a reference")
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
    captured_at: str | None = None
    structure: StructureReference | None = None
    allowed = {"text", "sha256", "method"}
    if require_current:
        captured_at = _require_string(value, "captured_at", max_length=64)
        _parse_v3_utc_timestamp(
            captured_at,
            field_name=f"task {task_id} reference.captured_at",
        )
        structure = _parse_structure_reference(value.get("structure"), task_id)
        derived_structure = _extract_markdown_structure(text)
        if structure != derived_structure:
            raise BenchmarkError(
                f"task {task_id}: reference.structure must exactly match "
                "deterministic parsing of reference.text"
            )
        allowed |= {"captured_at", "structure"}
    unknown = set(value) - allowed
    if unknown:
        raise BenchmarkError(f"task {task_id}: reference has unknown fields: {sorted(unknown)}")
    return Reference(
        text=text,
        sha256=expected_sha,
        method=method,
        captured_at=captured_at,
        structure=structure,
    )


def _parse_tasks(value: Any, *, require_current: bool) -> tuple[Task, ...]:
    if not isinstance(value, list) or not value:
        raise BenchmarkError("tasks must be a non-empty array")
    if len(value) > MAX_TASKS:
        raise BenchmarkError(f"tasks exceeds the {MAX_TASKS}-task safety limit")
    tasks: list[Task] = []
    seen: set[str] = set()
    seen_canonical_urls: dict[str, str] = {}
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
        if require_current:
            _validate_public_https_url(url, field_name=f"task {task_id} URL")
            canonical_url = canonical_task_url_identity(url)
            previous_task = seen_canonical_urls.get(canonical_url)
            if previous_task is not None:
                raise BenchmarkError(
                    f"task {task_id}: URL is canonically equivalent to task {previous_task}"
                )
            seen_canonical_urls[canonical_url] = task_id
        else:
            _validate_http_url(url, field_name=f"task {task_id} URL")
        if urlsplit(url).fragment:
            raise BenchmarkError(f"task {task_id}: URL fragments are forbidden")
        stratum = _require_string(raw, "stratum", max_length=128)
        if require_current and not _SAFE_ID.fullmatch(stratum):
            raise BenchmarkError(f"task {task_id}: stratum must be a stable identifier")
        language = _require_string(raw, "language", max_length=64)
        if require_current:
            declared_domain_cluster = _require_string(
                raw,
                "domain_cluster",
                max_length=128,
            )
            domain_cluster = derive_domain_cluster(url)
            if declared_domain_cluster != domain_cluster:
                raise BenchmarkError(
                    f"task {task_id}: domain_cluster must equal derived value {domain_cluster}"
                )
        else:
            domain_cluster = urlsplit(url).hostname or "unknown"
        content_type = (
            _require_string(raw, "content_type", max_length=64) if require_current else "unknown"
        )
        render_class = (
            _require_string(raw, "render_class", max_length=64) if require_current else "unknown"
        )
        if require_current and render_class not in {"static", "dynamic", "pdf", "mixed"}:
            raise BenchmarkError(f"task {task_id}: unsupported render_class")
        firecrawl_credit_cap = (
            _require_number(raw, "firecrawl_credit_cap", minimum=1) if require_current else 0.0
        )
        allowed = {"task_id", "url", "stratum", "language", "reference"}
        if require_current:
            allowed |= {
                "domain_cluster",
                "content_type",
                "render_class",
                "firecrawl_credit_cap",
            }
        unknown = set(raw) - allowed
        if unknown:
            raise BenchmarkError(f"task {task_id} has unknown fields: {sorted(unknown)}")
        tasks.append(
            Task(
                task_id=task_id,
                url=url,
                stratum=stratum,
                language=language,
                reference=_parse_reference(
                    raw.get("reference"),
                    task_id,
                    require_current=require_current,
                ),
                domain_cluster=domain_cluster,
                content_type=content_type,
                render_class=render_class,
                firecrawl_credit_cap=firecrawl_credit_cap,
            )
        )
    return tuple(tasks)


def _parse_providers(value: Any) -> tuple[Provider, ...]:
    if not isinstance(value, list):
        raise BenchmarkError("providers must be an array")
    if any(not isinstance(item, str) for item in value):
        raise BenchmarkError("providers must contain only provider names")
    if len(value) != len(set(value)):
        raise BenchmarkError("providers must not contain duplicates")
    unknown = set(value) - set(PROVIDERS)
    if unknown:
        raise BenchmarkError(f"providers contains unsupported values: {sorted(unknown)}")
    providers = cast(
        "tuple[Provider, ...]",
        tuple(provider for provider in PROVIDERS if provider in value),
    )
    if "clusy" not in providers or len(providers) < 2:
        raise BenchmarkError("providers must select Clusy and at least one vendor")
    if list(providers) != value:
        raise BenchmarkError("providers must use canonical order: clusy, exa, firecrawl")
    return providers


def _parse_provider_strings(
    value: Any,
    field_name: str,
    *,
    providers: Sequence[Provider] = PROVIDERS,
) -> Mapping[Provider, str]:
    if not isinstance(value, dict):
        raise BenchmarkError(f"{field_name} must be an object")
    parsed: dict[Provider, str] = {}
    for provider in providers:
        parsed[provider] = _require_string(value, provider, max_length=256)
    unknown = set(value) - set(providers)
    if unknown:
        raise BenchmarkError(f"{field_name} has unknown providers: {sorted(unknown)}")
    return parsed


def _parse_pricing(
    value: Any,
    *,
    providers: Sequence[Provider] = PROVIDERS,
) -> Mapping[Provider, Pricing]:
    if not isinstance(value, dict):
        raise BenchmarkError("pricing must be an object")
    parsed: dict[Provider, Pricing] = {}
    for provider_name in providers:
        raw = value.get(provider_name)
        if not isinstance(raw, dict):
            raise BenchmarkError(f"pricing.{provider_name} must be an object")
        unknown_fields = set(raw) - {"currency", "per_request"}
        if unknown_fields:
            raise BenchmarkError(
                f"pricing.{provider_name} has unknown fields: {sorted(unknown_fields)}"
            )
        currency = _require_string(raw, "currency", max_length=16)
        if currency != "USD":
            raise BenchmarkError(
                "normalized benchmark pricing requires the canonical currency spelling USD"
            )
        per_request = _require_number(raw, "per_request", minimum=0)
        parsed[provider_name] = Pricing(
            currency=currency,
            per_request=per_request,
        )
    unknown = set(value) - set(providers)
    if unknown:
        raise BenchmarkError(f"pricing has unknown providers: {sorted(unknown)}")
    return parsed


def _parse_v1_manifest(document: JsonObject) -> Manifest:
    """Validate the legacy v1 schema for offline reproducibility only."""
    if document.get("schema_version") != V1_SCHEMA_VERSION:
        raise BenchmarkError(f"schema_version must be {V1_SCHEMA_VERSION}")
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
    benchmark_id = _require_string(
        document,
        "benchmark_id",
        max_length=128,
    )
    if not _SAFE_ID.fullmatch(benchmark_id):
        raise BenchmarkError("benchmark_id contains unsafe characters")
    created_at = _require_string(document, "created_at", max_length=64)
    _parse_iso_timestamp(created_at, field_name="created_at")
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
        schema_version=V1_SCHEMA_VERSION,
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
        providers=PROVIDERS,
        plans=_parse_provider_strings(document.get("plans"), "plans"),
        pricing=_parse_pricing(document.get("pricing")),
        tasks=_parse_tasks(document.get("tasks"), require_current=False),
        mode="cold_live",
        corpus_sha256="",
        time_window_id="v1-unbound",
        independent_window_index=1,
        required_independent_windows=2,
        cache_max_age_seconds=0,
        warm_cache_primed_at=None,
        bootstrap_samples=10_000,
        budgets=None,
        request_policy=None,
        clusy_binding=None,
        compliance_acknowledgments=None,
    )


def _parse_budget_caps(
    value: Any,
    *,
    providers: Sequence[Provider],
) -> BudgetCaps:
    if not isinstance(value, dict):
        raise BenchmarkError("budgets must be an object")
    fields: dict[Provider, str] = {
        "clusy": "clusy_usd",
        "exa": "exa_usd",
        "firecrawl": "firecrawl_credits",
    }
    allowed = {fields[provider] for provider in providers}
    unknown = set(value) - allowed
    if unknown:
        raise BenchmarkError(f"budgets has unknown fields: {sorted(unknown)}")
    parsed = {
        provider: _require_number(value, fields[provider], minimum=0) for provider in providers
    }
    return BudgetCaps(
        exa_usd=parsed.get("exa", 0.0),
        firecrawl_credits=parsed.get("firecrawl", 0.0),
        clusy_usd=parsed.get("clusy", 0.0),
    )


def _parse_request_policy(value: Any) -> RequestPolicy:
    if not isinstance(value, dict):
        raise BenchmarkError("request_policy must be an object")
    allowed = {
        "max_output_characters",
        "firecrawl_proxy",
        "firecrawl_block_ads",
        "firecrawl_only_clean_content",
        "firecrawl_parse_pdf",
        "clusy_js_render",
    }
    unknown = set(value) - allowed
    if unknown:
        raise BenchmarkError(f"request_policy has unknown fields: {sorted(unknown)}")
    max_characters = value.get("max_output_characters")
    if (
        isinstance(max_characters, bool)
        or not isinstance(max_characters, int)
        or not (1 <= max_characters <= 10_000)
    ):
        raise BenchmarkError("request_policy.max_output_characters must be in [1, 10000]")
    firecrawl_proxy = _require_string(value, "firecrawl_proxy", max_length=16)
    if firecrawl_proxy != "basic":
        raise BenchmarkError(
            "paid fixed-cap track requires firecrawl_proxy=basic; auto/enhanced "
            "can bill up to five credits per request"
        )
    only_clean = value.get("firecrawl_only_clean_content")
    if only_clean is not False:
        raise BenchmarkError(
            "matched deterministic track requires firecrawl_only_clean_content=false"
        )
    block_ads = value.get("firecrawl_block_ads")
    parse_pdf = value.get("firecrawl_parse_pdf")
    if not isinstance(block_ads, bool) or not isinstance(parse_pdf, bool):
        raise BenchmarkError("Firecrawl policy flags must be booleans")
    if parse_pdf:
        raise BenchmarkError(
            "paid fixed-cap track requires firecrawl_parse_pdf=false because "
            "PDF parsing is billed per page without a request-side page cap"
        )
    clusy_js_render = _require_string(value, "clusy_js_render", max_length=16)
    if clusy_js_render not in {"conditional", "force", "never"}:
        raise BenchmarkError("unsupported request_policy.clusy_js_render")
    return RequestPolicy(
        max_output_characters=max_characters,
        firecrawl_proxy=firecrawl_proxy,
        firecrawl_block_ads=block_ads,
        firecrawl_only_clean_content=only_clean,
        firecrawl_parse_pdf=parse_pdf,
        clusy_js_render=clusy_js_render,
    )


def _parse_clusy_binding(value: Any) -> ClusyBinding:
    if not isinstance(value, dict):
        raise BenchmarkError("clusy_binding must be an object")
    allowed = {
        "expected_revision",
        "expected_config_sha256",
        "expected_image_digest",
    }
    unknown = set(value) - allowed
    if unknown:
        raise BenchmarkError(f"clusy_binding has unknown fields: {sorted(unknown)}")
    revision = _require_string(value, "expected_revision", max_length=64)
    config_sha = _require_string(value, "expected_config_sha256", max_length=64)
    image_digest = _require_string(value, "expected_image_digest", max_length=71)
    if not _REVISION.fullmatch(revision):
        raise BenchmarkError("clusy_binding.expected_revision is invalid")
    if not _SHA256.fullmatch(config_sha):
        raise BenchmarkError("clusy_binding.expected_config_sha256 is invalid")
    if not _CONTAINER_DIGEST.fullmatch(image_digest):
        raise BenchmarkError("clusy_binding.expected_image_digest is invalid")
    return ClusyBinding(
        expected_revision=revision,
        expected_config_sha256=config_sha,
        expected_image_digest=image_digest,
    )


def _parse_compliance_acknowledgments(
    value: Any,
    *,
    providers: Sequence[Provider],
) -> ComplianceAcknowledgments:
    if not isinstance(value, dict):
        raise BenchmarkError("compliance_acknowledgments must be an object")
    allowed = {
        "third_party_data_transfer_authorized",
        "exa_live_authorized",
        "exa_authorized_purpose",
    }
    unknown = set(value) - allowed
    if unknown:
        raise BenchmarkError(f"compliance_acknowledgments has unknown fields: {sorted(unknown)}")
    transfer_authorized = value.get("third_party_data_transfer_authorized")
    exa_authorized = value.get("exa_live_authorized")
    exa_purpose = value.get("exa_authorized_purpose")
    if not isinstance(transfer_authorized, bool) or not isinstance(
        exa_authorized,
        bool,
    ):
        raise BenchmarkError("compliance acknowledgments must be booleans")
    expected_exa_purpose = (
        "benchmark_only_no_training_distillation_or_labeling"
        if "exa" in providers
        else "not_applicable"
    )
    if exa_purpose != expected_exa_purpose:
        raise BenchmarkError(f"exa_authorized_purpose must be {expected_exa_purpose}")
    if not transfer_authorized:
        raise BenchmarkError("third-party benchmark data transfer must be explicitly authorized")
    if "exa" in providers and not exa_authorized:
        raise BenchmarkError("Exa may be selected only when exa_live_authorized=true is sealed")
    if "exa" not in providers and exa_authorized:
        raise BenchmarkError("exa_live_authorized must be false when Exa is not selected")
    return ComplianceAcknowledgments(
        third_party_data_transfer_authorized=transfer_authorized,
        exa_live_authorized=exa_authorized,
        exa_authorized_purpose=exa_purpose,
    )


def _parse_v3_manifest(document: JsonObject) -> Manifest:
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
    benchmark_id = _require_string(
        document,
        "benchmark_id",
        max_length=MAX_BENCHMARK_ID_LENGTH,
    )
    if not _SAFE_ID.fullmatch(benchmark_id):
        raise BenchmarkError("benchmark_id contains unsafe characters")
    created_at = _require_string(document, "created_at", max_length=64)
    created_datetime = _parse_v3_utc_timestamp(created_at, field_name="created_at")
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
            "country and location must be null: provider fetch geography is unmatched"
        )
    if _require_string(document, "scope", max_length=128) != "main_content":
        raise BenchmarkError("fixed-URL matched track requires scope=main_content")
    if _require_string(document, "content_format", max_length=32) != "markdown":
        raise BenchmarkError("fixed-URL matched track requires content_format=markdown")
    mode = _require_string(document, "mode", max_length=32)
    if mode not in {"cold_live", "warm_cache"}:
        raise BenchmarkError("v3 mode must be cold_live or warm_cache")
    profile = _require_string(document, "clusy_extraction_profile", max_length=32)
    if profile not in {"balanced", "adaptive", "quality"}:
        raise BenchmarkError("unsupported clusy_extraction_profile")
    providers = _parse_providers(document.get("providers"))
    timeout_seconds = _require_number(document, "timeout_seconds", minimum=1)
    if timeout_seconds != 60:
        raise BenchmarkError("matched protocol requires timeout_seconds=60")
    corpus_sha = _require_string(document, "corpus_sha256", max_length=64)
    if not _SHA256.fullmatch(corpus_sha):
        raise BenchmarkError("corpus_sha256 is invalid")
    time_window_id = _require_string(document, "time_window_id", max_length=128)
    if not _SAFE_ID.fullmatch(time_window_id):
        raise BenchmarkError("time_window_id contains unsafe characters")
    window_index = document.get("independent_window_index")
    required_windows = document.get("required_independent_windows")
    if isinstance(window_index, bool) or not isinstance(window_index, int) or window_index < 1:
        raise BenchmarkError("independent_window_index must be a positive integer")
    if (
        isinstance(required_windows, bool)
        or not isinstance(required_windows, int)
        or not (2 <= required_windows <= 20)
        or window_index > required_windows
    ):
        raise BenchmarkError("required_independent_windows must cover the window index")
    cache_age = document.get("cache_max_age_seconds")
    if (
        isinstance(cache_age, bool)
        or not isinstance(cache_age, int)
        or cache_age < 0
        or cache_age > 604_800
    ):
        raise BenchmarkError("cache_max_age_seconds must be in [0, 604800]")
    primed_at = _optional_short_string(document, "warm_cache_primed_at", max_length=64)
    if mode == "cold_live" and (cache_age != 0 or primed_at is not None):
        raise BenchmarkError("cold_live requires max age 0 and no warm prime")
    if mode == "warm_cache":
        if cache_age < 1 or primed_at is None:
            raise BenchmarkError("warm_cache requires a positive age and warm prime timestamp")
        if "exa" in providers and cache_age % 3600 != 0:
            raise BenchmarkError(
                "warm_cache cache_max_age_seconds must be an exact multiple of "
                "3600 when Exa is selected"
            )
        primed_datetime = _parse_v3_utc_timestamp(
            primed_at,
            field_name="warm_cache_primed_at",
        )
        if primed_datetime > created_datetime:
            raise BenchmarkError("warm_cache_primed_at must be at or before manifest.created_at")
        if (created_datetime - primed_datetime).total_seconds() > cache_age:
            raise BenchmarkError(
                "warm cache prime was already outside max age when the manifest was sealed"
            )
    bootstrap_samples = document.get("bootstrap_samples")
    if (
        isinstance(bootstrap_samples, bool)
        or not isinstance(bootstrap_samples, int)
        or not (100 <= bootstrap_samples <= 1_000_000)
    ):
        raise BenchmarkError("bootstrap_samples must be an integer in [100, 1000000]")
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
        "corpus_sha256",
        "time_window_id",
        "independent_window_index",
        "required_independent_windows",
        "cache_max_age_seconds",
        "warm_cache_primed_at",
        "bootstrap_samples",
        "budgets",
        "request_policy",
        "clusy_binding",
        "providers",
        "compliance_acknowledgments",
    }
    unknown = set(document) - allowed_keys
    if unknown:
        raise BenchmarkError(f"manifest has unknown fields: {sorted(unknown)}")
    compliance = _parse_compliance_acknowledgments(
        document.get("compliance_acknowledgments"),
        providers=providers,
    )
    tasks = _parse_tasks(document.get("tasks"), require_current=True)
    for task in tasks:
        reference = task.reference
        if reference is None or reference.captured_at is None:
            raise BenchmarkError(f"task {task.task_id}: v3 reference timestamp is missing")
        captured_at = _parse_v3_utc_timestamp(
            reference.captured_at,
            field_name=f"task {task.task_id} reference.captured_at",
        )
        if captured_at > created_datetime:
            raise BenchmarkError(
                f"task {task.task_id}: reference.captured_at must be at or before "
                "manifest.created_at"
            )
    if "firecrawl" in providers and any(
        task.render_class == "pdf" or "pdf" in task.content_type.casefold() for task in tasks
    ):
        raise BenchmarkError(
            "Firecrawl paid fixed-cap track excludes PDF tasks because safe "
            "per-page billing cannot be bounded by this request protocol"
        )
    actual_corpus_sha = calculate_corpus_sha256(document.get("tasks"))
    if corpus_sha != actual_corpus_sha:
        raise BenchmarkError("corpus_sha256 does not bind the exact ordered task/reference records")
    budgets = _parse_budget_caps(document.get("budgets"), providers=providers)
    pricing = _parse_pricing(document.get("pricing"), providers=providers)
    if any(item.per_request <= 0 for item in pricing.values()):
        raise BenchmarkError(
            "v3 requires a positive disclosed USD cost estimate for every selected provider"
        )
    expected_exa = len(tasks) * pricing["exa"].per_request if "exa" in providers else 0.0
    expected_clusy = len(tasks) * pricing["clusy"].per_request
    expected_firecrawl = (
        sum(task.firecrawl_credit_cap for task in tasks) if "firecrawl" in providers else 0.0
    )
    if expected_exa > budgets.exa_usd + 1e-12:
        raise BenchmarkError("manifest Exa budget is below its frozen request estimate")
    if expected_clusy > budgets.clusy_usd + 1e-12:
        raise BenchmarkError("manifest Clusy budget is below its frozen request estimate")
    if expected_firecrawl > budgets.firecrawl_credits + 1e-12:
        raise BenchmarkError("manifest Firecrawl budget is below task credit caps")
    return Manifest(
        document=document,
        schema_version=SCHEMA_VERSION,
        digest=actual_digest,
        benchmark_id=benchmark_id,
        created_at=created_at,
        seed=seed_raw,
        runner_region=runner_region,
        country=country,
        location=location,
        scope="main_content",
        extraction_profile=profile,
        timeout_seconds=timeout_seconds,
        providers=providers,
        plans=_parse_provider_strings(
            document.get("plans"),
            "plans",
            providers=providers,
        ),
        pricing=pricing,
        tasks=tasks,
        mode=mode,
        corpus_sha256=corpus_sha,
        time_window_id=time_window_id,
        independent_window_index=window_index,
        required_independent_windows=required_windows,
        cache_max_age_seconds=cache_age,
        warm_cache_primed_at=primed_at,
        bootstrap_samples=bootstrap_samples,
        budgets=budgets,
        request_policy=_parse_request_policy(document.get("request_policy")),
        clusy_binding=_parse_clusy_binding(document.get("clusy_binding")),
        compliance_acknowledgments=compliance,
    )


def parse_manifest(document: JsonObject) -> Manifest:
    """Validate v3, or load v1 for offline validation/scoring compatibility."""
    schema_version = document.get("schema_version")
    if schema_version == SCHEMA_VERSION:
        return _parse_v3_manifest(document)
    if schema_version == LEGACY_V2_SCHEMA_VERSION:
        raise BenchmarkError(
            "legacy v2 artifacts require the pinned v2 runner; the current "
            "runner deliberately accepts only v3 (plus offline-only v1)"
        )
    if schema_version == V1_SCHEMA_VERSION:
        return _parse_v1_manifest(document)
    raise BenchmarkError(
        f"schema_version must be {SCHEMA_VERSION} (or legacy offline-only {V1_SCHEMA_VERSION})"
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

    def validate(
        self,
        *,
        claimable: bool,
        providers: Sequence[Provider] = PROVIDERS,
    ) -> None:
        missing: list[str] = []
        if "exa" in providers and not self.exa_api_key.strip():
            missing.append("EXA_API_KEY")
        if "firecrawl" in providers and not self.firecrawl_api_key.strip():
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
    commit_valid = bool(re.fullmatch(r"[0-9a-f]{40,64}", state.commit))
    runner_provenance_valid = state.runner_committed and commit_valid
    if not runner_provenance_valid:
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
        runner_commit=state.commit if runner_provenance_valid else "unknown",
        runner_sha256=runner_sha,
        container_digest=digest if _CONTAINER_DIGEST.fullmatch(digest) else "unknown",
    )


@dataclass(frozen=True)
class PreparedRequest:
    provider: Provider
    endpoint: str
    api_version: str
    method: str
    headers: Mapping[str, str] = field(repr=False)
    json_body: Mapping[str, Any] | None
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
    policy = manifest.request_policy
    cache_age_seconds = manifest.cache_max_age_seconds
    exa_max_age_hours = 0 if manifest.mode == "cold_live" else cache_age_seconds // 3600
    if provider == "exa":
        # Exa documents that verbosity and section filters require a live crawl
        # (maxAgeHours=0).  Using includeSections for cold runs but requesting a
        # warm cache entry would therefore compare different scopes.  Keep the
        # exact same provider-default main-content request in both modes.
        text_options: JsonObject = {}
        if policy is not None:
            text_options["maxCharacters"] = policy.max_output_characters
        return PreparedRequest(
            provider=provider,
            endpoint=EXA_ENDPOINT,
            api_version="contents-current-2026-07-28",
            method="POST",
            headers={
                **common_headers,
                "x-api-key": credentials.exa_api_key,
            },
            json_body={
                "urls": [task.url],
                "text": text_options,
                "maxAgeHours": exa_max_age_hours,
                "livecrawlTimeout": timeout_ms,
            },
            timeout_seconds=manifest.timeout_seconds,
        )
    if provider == "firecrawl":
        firecrawl_body: JsonObject = {
            "url": task.url,
            "formats": ["markdown"],
            "onlyMainContent": True,
            "maxAge": cache_age_seconds * 1000,
            # Both phases write their first-attempt result.  Cold bypasses reads
            # with maxAge=0, then establishes the cache entry whose oldest
            # request-completion timestamp is sealed into the paired warm manifest.
            "storeInCache": True,
            "timeout": timeout_ms,
        }
        if policy is not None:
            firecrawl_body.update(
                {
                    "onlyCleanContent": policy.firecrawl_only_clean_content,
                    "blockAds": policy.firecrawl_block_ads,
                    "removeBase64Images": True,
                    "skipTlsVerification": False,
                    "proxy": policy.firecrawl_proxy,
                    "parsers": ["pdf"] if policy.firecrawl_parse_pdf else [],
                }
            )
        return PreparedRequest(
            provider=provider,
            endpoint=FIRECRAWL_ENDPOINT,
            api_version="v2",
            method="POST",
            headers={
                **common_headers,
                "Authorization": f"Bearer {credentials.firecrawl_api_key}",
            },
            json_body=firecrawl_body,
            timeout_seconds=manifest.timeout_seconds,
        )
    headers = dict(common_headers)
    if credentials.clusy_api_key:
        headers["Authorization"] = f"Bearer {credentials.clusy_api_key}"
    endpoint = credentials.clusy_base_url.rstrip("/") + "/crawl"
    clusy_body: JsonObject = {
        "urls": [task.url],
        "max_pages": 1,
        "formats": ["markdown"],
        "max_age": cache_age_seconds,
        "extraction_profile": manifest.extraction_profile,
    }
    if policy is not None:
        clusy_body["js_render"] = {
            "conditional": None,
            "force": True,
            "never": False,
        }[policy.clusy_js_render]
    return PreparedRequest(
        provider=provider,
        endpoint=endpoint,
        api_version="clusy-crawl-v1",
        method="POST",
        headers=headers,
        json_body=clusy_body,
        timeout_seconds=manifest.timeout_seconds,
    )


def build_clusy_version_request(
    manifest: Manifest,
    credentials: Credentials,
) -> PreparedRequest:
    headers = {
        "Accept": "application/json",
        "User-Agent": "clusy-sealed-fixed-url-benchmark/2",
    }
    if credentials.clusy_api_key:
        headers["Authorization"] = f"Bearer {credentials.clusy_api_key}"
    return PreparedRequest(
        provider="clusy",
        endpoint=credentials.clusy_base_url.rstrip("/") + "/health/version",
        api_version="clusy-health-version-v1",
        method="GET",
        headers=headers,
        json_body=None,
        timeout_seconds=min(10.0, manifest.timeout_seconds),
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
    first_byte_latency_ms: float | None = None
    hard_deadline_enforced: bool = False


def _utc_now() -> str:
    return _format_v3_utc_timestamp(datetime.now(UTC))


class RequestExecutor(Protocol):
    def execute(self, request: PreparedRequest) -> WireResponse:
        """Execute exactly one request without retries."""

    def close(self) -> None:
        """Release resources."""


class _WallDeadlineError(TimeoutError):
    pass


def _hard_wall_deadline_supported() -> bool:
    return (
        threading.current_thread() is threading.main_thread()
        and hasattr(signal, "SIGALRM")
        and hasattr(signal, "ITIMER_REAL")
    )


@contextmanager
def _hard_wall_deadline(seconds: float) -> Any:
    """Enforce one wall-clock deadline on POSIX main-thread benchmark runners."""
    if not _hard_wall_deadline_supported():
        yield False
        return

    def expire(_signum: int, _frame: Any) -> None:
        raise _WallDeadlineError(f"hard wall deadline exceeded after {seconds:g}s")

    started = time.monotonic()
    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)
    signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield True
    finally:
        elapsed = time.monotonic() - started
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer[0] > 0:
            signal.setitimer(
                signal.ITIMER_REAL,
                max(0.000001, old_timer[0] - elapsed),
                old_timer[1],
            )


class HttpxRequestExecutor:
    """One-attempt streaming HTTPX executor with bounded response retention."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def execute(self, request: PreparedRequest) -> WireResponse:
        started_wall = datetime.now(UTC)
        started = time.perf_counter()
        first_byte: str | None = None
        first_byte_latency_ms: float | None = None
        chunks: list[bytes] = []
        total = 0
        status_code: int | None = None
        headers: Mapping[str, str] = {}
        hard_deadline_enforced = False
        try:
            with _hard_wall_deadline(request.timeout_seconds) as deadline_enforced:
                hard_deadline_enforced = deadline_enforced
                request_kwargs: JsonObject = {
                    "headers": request.headers,
                    "timeout": httpx.Timeout(request.timeout_seconds),
                }
                if request.json_body is not None:
                    request_kwargs["json"] = request.json_body
                with self._client.stream(
                    request.method,
                    request.endpoint,
                    **request_kwargs,
                ) as response:
                    status_code = response.status_code
                    headers = dict(response.headers)
                    for chunk in response.iter_bytes():
                        if first_byte is None:
                            first_byte_latency_ms = (time.perf_counter() - started) * 1000
                            first_byte = _format_v3_utc_timestamp(
                                started_wall + timedelta(milliseconds=first_byte_latency_ms)
                            )
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
        latency_ms = (completed - started) * 1000
        return WireResponse(
            status_code=status_code,
            headers=headers,
            body=body,
            started_at=_format_v3_utc_timestamp(started_wall),
            first_byte_at=first_byte,
            completed_at=_format_v3_utc_timestamp(
                started_wall + timedelta(milliseconds=latency_ms)
            ),
            latency_ms=latency_ms,
            transport_error=error,
            first_byte_latency_ms=first_byte_latency_ms,
            hard_deadline_enforced=hard_deadline_enforced,
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
    cache_state: str = "unknown"
    origin_status_code: int | None = None
    warning: str | None = None
    rendered: bool | None = None
    model_used: bool | None = None
    truncated: bool | None = None
    truncation_reason: str | None = None
    extraction_strategy: str | None = None
    content_scope: str | None = None
    quality_attempted: bool | None = None
    quality_succeeded: bool | None = None
    completeness_score: float | None = None
    stage_timings_ms: Mapping[str, float] = field(default_factory=dict)
    benchmark_cap_applied: bool = False


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


def _numeric_stage_timings(value: Any) -> Mapping[str, float]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, float] = {}
    for key in _STAGE_TIMING_KEYS:
        number = _as_optional_number(value.get(key))
        if number is not None and number >= 0:
            result[key] = number
    return result


def _apply_output_character_cap(
    result: NormalizedResult,
    *,
    max_characters: int,
) -> NormalizedResult:
    """Apply the same deterministic post-normalization cap to every provider."""
    if len(result.text) <= max_characters:
        return result
    return replace(
        result,
        text=result.text[:max_characters],
        truncated=True,
        truncation_reason="benchmark_output_character_cap",
        benchmark_cap_applied=True,
    )


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


def _canonical_failed_result(
    result: NormalizedResult,
    *,
    status: str,
) -> NormalizedResult:
    """Remove output-derived diagnostics that cannot describe a successful extraction."""
    return replace(
        result,
        status=status,
        text="",
        title="",
        canonical_url="",
        publication_timestamp=None,
        fetch_timestamp=None,
        cache_hit=None,
        fetch_age=None,
        provider_score=None,
        citation_links=(),
        cache_state="unknown",
        origin_status_code=None,
        warning=None,
        rendered=None,
        model_used=None,
        truncated=None,
        truncation_reason=None,
        extraction_strategy=None,
        content_scope=None,
        quality_attempted=None,
        quality_succeeded=None,
        completeness_score=None,
        stage_timings_ms={},
        benchmark_cap_applied=False,
    )


def normalize_provider_response(provider: Provider, wire: WireResponse) -> NormalizedResult:
    request_id = _header_request_id(wire.headers)
    if wire.transport_error:
        result = _normalization_error(wire.transport_error, request_id)
        return NormalizedResult(**{**result.__dict__, "status": "transport_error"})
    if wire.status_code is None:
        raise BenchmarkError("response status is unavailable without a transport error")
    if not (200 <= wire.status_code < 300):
        detail = f"HTTP {wire.status_code}"
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
            exa_source = (_as_optional_string(exa_status.get("source")) or "").casefold()
            cache_state = (
                "miss"
                if exa_source in {"crawled", "live", "livecrawl"}
                else "hit"
                if exa_source in {"cached", "cache"}
                else "unknown"
            )
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
                cache_state=cache_state,
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
            origin_status = _as_optional_number(metadata_obj.get("statusCode"))
            metadata_error = _as_optional_string(metadata_obj.get("error"))
            if metadata_error:
                raise ProviderResultError(
                    f"Firecrawl origin metadata reported failure: {metadata_error}"
                )
            if origin_status is not None and int(origin_status) >= 400:
                raise ProviderResultError(f"Firecrawl origin returned HTTP {int(origin_status)}")
            fire_cache_state = (
                _as_optional_string(metadata_obj.get("cacheState")) or "unknown"
            ).casefold()
            if fire_cache_state not in {"hit", "miss"}:
                fire_cache_state = "unknown"
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
                cache_state=fire_cache_state,
                origin_status_code=(int(origin_status) if origin_status is not None else None),
                warning=_as_optional_string(item.get("warning")),
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
            origin_status = _as_optional_number(
                metadata_obj.get("origin_status_code")
            ) or _as_optional_number(metadata_obj.get("status_code"))
            origin_error = _as_optional_string(metadata_obj.get("origin_error"))
            if origin_error:
                raise ProviderResultError(f"Clusy origin error: {origin_error}")
            if origin_status is not None and int(origin_status) >= 400:
                raise ProviderResultError(f"Clusy origin returned HTTP {int(origin_status)}")
            cached = _as_optional_bool(item.get("cached"))
            extraction_strategy = _as_optional_string(
                metadata_obj.get("extraction_route")
            ) or _as_optional_string(metadata_obj.get("extraction_strategy"))
            model_assisted = _as_optional_bool(metadata_obj.get("model_assisted"))
            if model_assisted is None and extraction_strategy is not None:
                model_assisted = "model" in extraction_strategy.casefold()
            completeness_score = _as_optional_number(metadata_obj.get("completeness_score"))
            if completeness_score is not None and not (0 <= completeness_score <= 1):
                completeness_score = None
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
                cache_hit=cached,
                fetch_age=None,
                credits=0,
                provider_score=None,
                citation_links=_string_list(item.get("links")),
                cache_state=("hit" if cached is True else "miss" if cached is False else "unknown"),
                origin_status_code=(int(origin_status) if origin_status is not None else None),
                rendered=_as_optional_bool(metadata_obj.get("rendered")),
                model_used=model_assisted,
                truncated=_as_optional_bool(metadata_obj.get("truncated")),
                truncation_reason=_as_optional_string(metadata_obj.get("truncation_reason")),
                extraction_strategy=extraction_strategy,
                content_scope=_as_optional_string(metadata_obj.get("content_scope")),
                quality_attempted=_as_optional_bool(metadata_obj.get("quality_attempted")),
                quality_succeeded=_as_optional_bool(metadata_obj.get("quality_succeeded")),
                completeness_score=completeness_score,
                stage_timings_ms=_numeric_stage_timings(metadata_obj.get("stage_timings_ms")),
            )
    except ProviderResultError as exc:
        result = _normalization_error(str(exc), request_id)
        return NormalizedResult(**{**result.__dict__, "status": "provider_error"})
    except BenchmarkError as exc:
        return _normalization_error(str(exc), request_id)
    if not normalized.text:
        return _canonical_failed_result(normalized, status="empty_output")
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


def _structure_key(value: str) -> str:
    return " ".join(normalize_text(value).casefold().split())


def _multiset_f1(candidate: Sequence[str], reference: Sequence[str]) -> float | None:
    if not reference:
        return None
    candidate_keys = [_structure_key(value) for value in candidate if _structure_key(value)]
    reference_keys = [_structure_key(value) for value in reference if _structure_key(value)]
    if not reference_keys:
        return None
    overlap = sum((Counter(candidate_keys) & Counter(reference_keys)).values())
    precision = overlap / len(candidate_keys) if candidate_keys else 0.0
    recall = overlap / len(reference_keys)
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _extract_markdown_structure(text: str) -> StructureReference:
    headings: list[str] = []
    list_items: list[str] = []
    code_blocks: list[str] = []
    tables: list[tuple[tuple[str, ...], ...]] = []
    lines = normalize_text(text).splitlines()
    in_code = False
    code: list[str] = []
    table_rows: list[tuple[str, ...]] = []

    def flush_table() -> None:
        nonlocal table_rows
        if table_rows:
            tables.append(tuple(table_rows))
            table_rows = []

    for line in lines:
        if line.lstrip().startswith("```"):
            flush_table()
            if in_code:
                code_blocks.append("\n".join(code))
                code = []
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code.append(line)
            continue
        heading = re.match(r"^\s*(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            flush_table()
            headings.append(f"{len(heading.group(1))}:{heading.group(2)}")
            continue
        list_item = re.match(r"^\s*(?:[-+*]|\d+[.)])\s+(.+)$", line)
        if list_item:
            flush_table()
            list_items.append(list_item.group(1))
            continue
        if "|" in line:
            cells = tuple(cell.strip() for cell in line.strip().strip("|").split("|"))
            is_separator = cells and all(
                bool(re.fullmatch(r":?-{3,}:?", cell.replace(" ", ""))) for cell in cells
            )
            if is_separator:
                continue
            if not is_separator and len(cells) >= 2:
                table_rows.append(cells)
                continue
        flush_table()
    if in_code:
        code_blocks.append("\n".join(code))
    flush_table()
    return StructureReference(
        headings=tuple(headings),
        list_items=tuple(list_items),
        code_blocks=tuple(code_blocks),
        tables=tuple(tables),
    )


def _table_tree_tokens(
    tables: Sequence[Sequence[Sequence[str]]],
) -> list[str]:
    tokens: list[str] = []
    for table in tables:
        tokens.append("TABLE")
        for row in table:
            tokens.append("ROW")
            tokens.extend(f"CELL:{_structure_key(cell)}" for cell in row)
    return tokens[:20_000]


def score_structure(candidate: str, reference: StructureReference) -> JsonObject:
    observed = _extract_markdown_structure(candidate)
    heading_f1 = _multiset_f1(observed.headings, reference.headings)
    list_f1 = _multiset_f1(observed.list_items, reference.list_items)
    code_f1 = _multiset_f1(observed.code_blocks, reference.code_blocks)
    reference_table_tokens = _table_tree_tokens(reference.tables)
    observed_table_tokens = _table_tree_tokens(observed.tables)
    table_tree_similarity = (
        SequenceMatcher(
            None,
            observed_table_tokens,
            reference_table_tokens,
            autojunk=False,
        ).ratio()
        if reference_table_tokens
        else None
    )
    component_values = [
        value
        for value in (heading_f1, list_f1, code_f1, table_tree_similarity)
        if value is not None
    ]
    return {
        "heading_f1": heading_f1,
        "list_f1": list_f1,
        "code_f1": code_f1,
        "table_tree_similarity": table_tree_similarity,
        "structure_score": (
            sum(component_values) / len(component_values) if component_values else None
        ),
        "structure_metric": "clusy-markdown-structure.v1",
        "reference_component_count": len(component_values),
        "observed_headings": len(observed.headings),
        "observed_list_items": len(observed.list_items),
        "observed_code_blocks": len(observed.code_blocks),
        "observed_tables": len(observed.tables),
        "observed_table_tree_tokens": len(observed_table_tokens),
        "reference_table_tree_tokens": len(reference_table_tokens),
    }


def randomized_orders(manifest: Manifest) -> Mapping[str, tuple[Provider, ...]]:
    orders: dict[str, tuple[Provider, ...]] = {}
    for task in manifest.tasks:
        providers = sorted(
            manifest.providers,
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
    if provider == "clusy":
        # Clusy uses sealed fixed local accounting; it does not depend on a
        # successful or JSON-decodable provider body.
        return None, None, 0.0, 0.0
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
    raise BenchmarkError(f"unsupported provider accounting adapter: {provider}")


def _cache_state_matches_mode(manifest: Manifest, result: NormalizedResult) -> bool:
    expected = "miss" if manifest.mode == "cold_live" else "hit"
    return result.cache_state == expected


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
    manifest: Manifest,
    claim: ClaimContext,
    clusy_preflight: Mapping[str, Any] | None,
    execution_caps: BudgetCaps | None,
) -> JsonObject:
    request_id, _body_cache_hit, body_credits, reported_cost = _provider_body_fields(
        provider,
        wire,
    )
    observed_request_id = result.provider_request_id or request_id
    if provider == "firecrawl":
        credits = result.credits if result.credits is not None else body_credits
    elif provider == "clusy":
        credits = 0.0
    else:
        credits = None
    normalized_cost = manifest.pricing[provider].per_request
    scoring: JsonObject | None = None
    if task.reference:
        candidate = result.text if result.status == "ok" else ""
        scoring = score_text(candidate, task.reference.text)
        if task.reference.structure is not None:
            scoring.update(score_structure(candidate, task.reference.structure))
    return {
        "event_schema_version": (
            EVENT_SCHEMA_VERSION
            if manifest.schema_version == SCHEMA_VERSION
            else V1_EVENT_SCHEMA_VERSION
        ),
        "claimable": claim.claimable,
        "run_id": run_id,
        "task_id": task.task_id,
        "stratum": task.stratum,
        "structure_strata": list(reference_structure_strata(task.reference)),
        "structure_strata_basis": "reference_component_presence.v1",
        "language": task.language,
        "domain_cluster": task.domain_cluster,
        "content_type": task.content_type,
        "render_class": task.render_class,
        "provider": provider,
        "endpoint": (request.endpoint if provider != "clusy" else "[CLUSY_ENDPOINT_REDACTED]"),
        "endpoint_sha256": sha256_bytes(request.endpoint.encode("utf-8")),
        "mode": manifest.mode,
        "time_window_id": manifest.time_window_id,
        "independent_window_index": manifest.independent_window_index,
        "required_independent_windows": manifest.required_independent_windows,
        "plan": manifest.plans[provider],
        "api_version": request.api_version,
        "sdk_version": f"httpx/{httpx.__version__}",
        "runner_commit": claim.runner_commit,
        "runner_sha256": claim.runner_sha256,
        "container_digest": claim.container_digest,
        "clusy_preflight": dict(clusy_preflight) if clusy_preflight else None,
        "manifest_sha256": manifest.digest,
        "runner_region": manifest.runner_region,
        "query_sha256": sha256_bytes(task.url.encode("utf-8")),
        "quality_scope": (
            "provider_default_compact_main_content" if provider == "exa" else "full_main_content"
        ),
        "quality_scope_comparable_to_full_main_content": provider != "exa",
        "max_age": manifest.cache_max_age_seconds,
        "timeout": manifest.timeout_seconds,
        "attempt": 1,
        "randomized_order": list(order),
        "order_position": order_position,
        "randomization_seed": manifest.seed,
        "randomization_algorithm": "sha256-sort.v1",
        "started_at": wire.started_at,
        "first_byte_at": wire.first_byte_at,
        "first_byte_latency_ms": wire.first_byte_latency_ms,
        "completed_at": wire.completed_at,
        "latency_ms": wire.latency_ms,
        "hard_deadline_enforced": wire.hard_deadline_enforced,
        "http_status": wire.status_code,
        "status": result.status,
        "provider_request_id_sha256": (
            sha256_bytes(observed_request_id.encode("utf-8"))
            if observed_request_id is not None
            else None
        ),
        "cache_hit": (result.cache_hit if provider == "clusy" and result.status == "ok" else None),
        "cache_state": result.cache_state,
        "cache_evidence_matches_mode": _cache_state_matches_mode(manifest, result),
        "cache_evidence_source": (
            "undocumented_response_field"
            if provider == "firecrawl"
            else "provider_status_source"
            if provider == "exa"
            else "clusy_response_contract"
        ),
        "cache_evidence_contractually_documented": provider != "firecrawl",
        # Provider-specific age fields are neither required by nor comparable
        # across this fixed protocol, so v3 does not retain them as numeric carriers.
        "fetch_age": None,
        "credits": credits,
        "credit_evidence_source": (
            "undocumented_response_field"
            if provider == "firecrawl" and body_credits is not None
            else "sealed_task_cap_fallback"
            if provider == "firecrawl"
            else "provider_response"
            if provider == "exa"
            else "sealed_manifest_per_request"
        ),
        "credit_evidence_contractually_documented": provider != "firecrawl",
        "scheduled_firecrawl_credit_cap": (
            task.firecrawl_credit_cap if provider == "firecrawl" else None
        ),
        "normalized_cost": normalized_cost,
        "normalized_cost_currency": manifest.pricing[provider].currency,
        "normalized_cost_source": "frozen_manifest_per_request",
        "provider_reported_cost": reported_cost,
        "provider_reported_cost_currency": "USD" if reported_cost is not None else None,
        "execution_budget_caps": (
            {
                "exa_usd": execution_caps.exa_usd,
                "firecrawl_credits": execution_caps.firecrawl_credits,
                "clusy_usd": execution_caps.clusy_usd,
            }
            if execution_caps is not None
            else None
        ),
        "raw_response_sha256": raw_sha256,
        "raw_response_bytes": len(wire.body),
        "raw_response_observed": wire.status_code is not None or bool(wire.body),
        "raw_response_complete": wire.transport_error is None,
        "canonical_url": redact_url(result.canonical_url),
        "normalized_text_sha256": sha256_bytes(result.text.encode("utf-8")),
        "character_count": len(result.text),
        "benchmark_output_cap_characters": (
            manifest.request_policy.max_output_characters
            if manifest.request_policy is not None
            else None
        ),
        "benchmark_output_cap_applied": result.benchmark_cap_applied,
        "token_count": len(_tokenize(result.text)),
        "token_count_method": "clusy-unicode-tokenizer.v1",
        "origin_status_code": result.origin_status_code,
        "warning_observed": result.warning is not None,
        "rendered": result.rendered,
        "model_used": result.model_used,
        "quality_attempted": result.quality_attempted,
        "quality_succeeded": result.quality_succeeded,
        "completeness_score": result.completeness_score,
        "stage_timings_ms": dict(result.stage_timings_ms),
        "truncated": result.truncated,
        "provider_score": result.provider_score,
        "reference_sha256": task.reference.sha256 if task.reference else None,
        "reference_method": task.reference.method if task.reference else None,
        "scoring": scoring,
    }


def _make_default_client() -> httpx.Client:
    return httpx.Client(
        follow_redirects=False,
        http2=True,
        trust_env=False,
    )


def _safe_run_id(manifest: Manifest, *, created_at: str | None = None) -> str:
    timestamp = (
        _parse_v3_utc_timestamp(created_at, field_name="run.created_at")
        if created_at is not None
        else datetime.now(UTC)
    )
    stamp = timestamp.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{manifest.benchmark_id}-{stamp}-{manifest.digest[:12]}"


def _validated_execution_caps(
    manifest: Manifest,
    *,
    confirmed_manifest_sha256: str | None,
    max_exa_usd: float | None,
    max_firecrawl_credits: float | None,
    max_clusy_usd: float | None,
    acknowledge_exa_live_use: bool,
) -> BudgetCaps:
    if manifest.schema_version != SCHEMA_VERSION or manifest.budgets is None:
        raise BenchmarkError("paid execution requires a v3 manifest; v1 is offline-compatible only")
    if confirmed_manifest_sha256 != manifest.digest:
        raise BenchmarkError("--confirm-manifest-sha256 must exactly match the sealed manifest")
    cap_values: dict[Provider, tuple[str, float | None]] = {
        "clusy": ("max_clusy_usd", max_clusy_usd),
        "exa": ("max_exa_usd", max_exa_usd),
        "firecrawl": ("max_firecrawl_credits", max_firecrawl_credits),
    }
    normalized: dict[Provider, float] = {}
    for provider, (name, value) in cap_values.items():
        if provider in manifest.providers:
            if value is None or not math.isfinite(value) or value < 0:
                raise BenchmarkError(f"{name} must be an explicit finite non-negative cap")
            normalized[provider] = float(value)
        else:
            if value is not None and (not math.isfinite(value) or value != 0):
                raise BenchmarkError(
                    f"{name} must be omitted or zero when {provider} is not selected"
                )
            normalized[provider] = 0.0
    compliance = manifest.compliance_acknowledgments
    if "exa" in manifest.providers and (
        compliance is None or not compliance.exa_live_authorized or not acknowledge_exa_live_use
    ):
        raise BenchmarkError(
            "Exa execution requires both sealed authorization and --acknowledge-exa-live-use"
        )
    if "exa" not in manifest.providers and acknowledge_exa_live_use:
        raise BenchmarkError("--acknowledge-exa-live-use is invalid when Exa is not selected")
    caps = BudgetCaps(
        exa_usd=normalized["exa"],
        firecrawl_credits=normalized["firecrawl"],
        clusy_usd=normalized["clusy"],
    )
    if caps.exa_usd > manifest.budgets.exa_usd + 1e-12:
        raise BenchmarkError("execution Exa cap exceeds the sealed manifest cap")
    if caps.firecrawl_credits > manifest.budgets.firecrawl_credits + 1e-12:
        raise BenchmarkError("execution Firecrawl cap exceeds the sealed manifest cap")
    if caps.clusy_usd > manifest.budgets.clusy_usd + 1e-12:
        raise BenchmarkError("execution Clusy cap exceeds the sealed manifest cap")
    expected_exa = (
        len(manifest.tasks) * manifest.pricing["exa"].per_request
        if "exa" in manifest.providers
        else 0.0
    )
    expected_firecrawl = (
        sum(task.firecrawl_credit_cap for task in manifest.tasks)
        if "firecrawl" in manifest.providers
        else 0.0
    )
    expected_clusy = len(manifest.tasks) * manifest.pricing["clusy"].per_request
    if expected_exa > caps.exa_usd + 1e-12:
        raise BenchmarkError("execution Exa cap is below the frozen request estimate")
    if expected_firecrawl > caps.firecrawl_credits + 1e-12:
        raise BenchmarkError("execution Firecrawl cap is below task credit caps")
    if expected_clusy > caps.clusy_usd + 1e-12:
        raise BenchmarkError("execution Clusy cap is below the frozen request estimate")
    return caps


def _prepare_output_root(output_root: Path, repo_root: Path) -> Path:
    if output_root.is_symlink():
        raise BenchmarkError("output root must not be a symlink")
    resolved_output = output_root.resolve()
    resolved_repo = repo_root.resolve()
    if resolved_output == resolved_repo:
        raise BenchmarkError("output root must not be the repository root")
    if resolved_repo in resolved_output.parents:
        allowed_roots = (
            (resolved_repo / "bench" / "results").resolve(),
            (resolved_repo / "bench" / "artifacts").resolve(),
        )
        if not any(
            resolved_output == allowed or allowed in resolved_output.parents
            for allowed in allowed_roots
        ):
            raise BenchmarkError(
                "in-repository output must be contained by bench/results or bench/artifacts"
            )
    try:
        resolved_output.mkdir(parents=True, exist_ok=True, mode=0o700)
        mode = resolved_output.stat().st_mode & 0o777
    except OSError as exc:
        raise BenchmarkError(f"cannot prepare output root: {exc}") from exc
    if mode & 0o077:
        raise BenchmarkError("output root must not grant group or other permissions")
    return resolved_output


def _assert_secret_free(
    data: bytes,
    secrets: Sequence[str],
    *,
    artifact_name: str,
) -> None:
    for secret in secrets:
        if secret and secret.encode("utf-8") in data:
            raise BenchmarkError(f"secret scan refused artifact completion: {artifact_name}")


def _run_clusy_preflight(
    *,
    executor: RequestExecutor,
    manifest: Manifest,
    credentials: Credentials,
    secrets: Sequence[str],
) -> JsonObject:
    binding = manifest.clusy_binding
    if binding is None:
        raise BenchmarkError("v3 requires a Clusy revision/config/image binding")
    request = build_clusy_version_request(manifest, credentials)
    wire = executor.execute(request)
    _assert_secret_free(wire.body, secrets, artifact_name="Clusy version preflight")
    if not wire.hard_deadline_enforced:
        raise BenchmarkError("Clusy preflight lacks a hard wall deadline")
    if wire.transport_error:
        raise BenchmarkError(f"Clusy version preflight failed: {wire.transport_error}")
    if wire.status_code is None or not (200 <= wire.status_code < 300):
        raise BenchmarkError(f"Clusy version preflight returned HTTP {wire.status_code}")
    document = _json_response(wire.body)
    revision = _as_optional_string(document.get("sha"))
    lowered_headers = {name.casefold(): value for name, value in wire.headers.items()}
    config_sha = (
        _as_optional_string(document.get("config_fingerprint"))
        or _as_optional_string(document.get("config_sha256"))
        or lowered_headers.get("x-clusy-config-sha256")
    )
    image_digest = _as_optional_string(document.get("image_digest")) or lowered_headers.get(
        "x-clusy-image-digest"
    )
    if revision != binding.expected_revision:
        raise BenchmarkError("Clusy deployed revision does not match sealed binding")
    if config_sha != binding.expected_config_sha256:
        raise BenchmarkError("Clusy config fingerprint does not match sealed binding")
    if image_digest != binding.expected_image_digest:
        raise BenchmarkError("Clusy image digest does not match sealed binding")
    return {
        "revision": revision,
        "config_fingerprint": config_sha,
        "image_digest": image_digest,
        "version_response_sha256": sha256_bytes(canonical_json_bytes(document)),
        "latency_ms": wire.latency_ms,
        "hard_deadline_enforced": wire.hard_deadline_enforced,
    }


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
    bootstrap_samples: int | None = None,
    confirmed_manifest_sha256: str | None = None,
    max_exa_usd: float | None = None,
    max_firecrawl_credits: float | None = None,
    max_clusy_usd: float | None = None,
    acknowledge_exa_live_use: bool = False,
    dns_resolver: DnsResolver = _default_dns_resolver,
) -> Path:
    """Execute one attempt/provider/task and return the immutable run directory."""
    manifest = load_manifest(manifest_path)
    if not execute_paid:
        raise BenchmarkError(
            "paid execution is disabled; pass --execute-paid only after budget authorization"
        )
    execution_caps = _validated_execution_caps(
        manifest,
        confirmed_manifest_sha256=confirmed_manifest_sha256,
        max_exa_usd=max_exa_usd,
        max_firecrawl_credits=max_firecrawl_credits,
        max_clusy_usd=max_clusy_usd,
        acknowledge_exa_live_use=acknowledge_exa_live_use,
    )
    claim = prepare_claim_context(
        repo_root=repo_root,
        nonclaimable=nonclaimable,
        git_state=git_state,
        container_digest=container_digest,
    )
    execution_credentials = credentials or Credentials.from_environment()
    execution_credentials.validate(
        claimable=claim.claimable,
        providers=manifest.providers,
    )
    if claim.claimable and not _hard_wall_deadline_supported():
        raise BenchmarkError("claimable v3 execution requires POSIX hard wall deadlines")
    validate_public_dns_targets(manifest, resolver=dns_resolver)
    if bootstrap_samples is not None and bootstrap_samples != manifest.bootstrap_samples:
        raise BenchmarkError(
            "--bootstrap-samples must exactly match the sealed manifest value "
            f"{manifest.bootstrap_samples}"
        )

    manifest_bytes = canonical_json_bytes(manifest.document) + b"\n"
    _assert_secret_free(
        manifest_bytes,
        execution_credentials.secret_values,
        artifact_name="sealed manifest",
    )
    prepared_output_root = _prepare_output_root(output_root, repo_root)
    factory = executor_factory or (lambda: HttpxRequestExecutor(_make_default_client()))
    preflight_executor = factory()
    try:
        clusy_preflight = _run_clusy_preflight(
            executor=preflight_executor,
            manifest=manifest,
            credentials=execution_credentials,
            secrets=execution_credentials.secret_values,
        )
    finally:
        preflight_executor.close()

    run_created_at = _utc_now()
    run_id = _safe_run_id(manifest, created_at=run_created_at)
    run_directory = prepared_output_root / run_id
    try:
        run_directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    except FileExistsError as exc:
        raise BenchmarkError(f"run directory already exists: {run_directory}") from exc

    metadata = {
        "run_id": run_id,
        "manifest_sha256": manifest.digest,
        "evaluated_providers": list(manifest.providers),
        "quality_scope_by_provider": {
            provider: (
                "provider_default_compact_main_content"
                if provider == "exa"
                else "full_main_content"
            )
            for provider in manifest.providers
        },
        "quality_scope_comparable_to_full_main_content": {
            provider: provider != "exa" for provider in manifest.providers
        },
        "claimable": claim.claimable,
        "watermark": claim.watermark,
        "nonclaimable_reasons": list(claim.reasons),
        "runner_commit": claim.runner_commit,
        "runner_sha256": claim.runner_sha256,
        "container_digest": claim.container_digest,
        "clusy_preflight": clusy_preflight,
        "execution_budget_caps": {
            "exa_usd": execution_caps.exa_usd,
            "firecrawl_credits": execution_caps.firecrawl_credits,
            "clusy_usd": execution_caps.clusy_usd,
        },
        "created_at": run_created_at,
    }
    _validate_run_artifact(metadata, manifest=manifest)
    metadata_bytes = canonical_json_bytes(metadata) + b"\n"
    _assert_secret_free(
        metadata_bytes,
        execution_credentials.secret_values,
        artifact_name="run metadata",
    )
    _atomic_write(run_directory / "run.json", metadata_bytes)
    _atomic_write(
        run_directory / "manifest.json",
        manifest_bytes,
    )

    events: list[JsonObject] = []
    budget_ledger = {
        "exa_usd": 0.0,
        "firecrawl_credits": 0.0,
        "clusy_usd": 0.0,
    }
    orders = randomized_orders(manifest)
    events_partial_path = run_directory / "events.jsonl.partial"
    events_hasher = hashlib.sha256()
    executor = factory()
    try:
        with events_partial_path.open("xb", buffering=0) as journal:
            os.chmod(events_partial_path, 0o600)
            for task_index, task in enumerate(manifest.tasks):
                order = orders[task.task_id]
                for position, provider in enumerate(order):
                    if provider == "exa" and (
                        not acknowledge_exa_live_use
                        or manifest.compliance_acknowledgments is None
                        or not manifest.compliance_acknowledgments.exa_live_authorized
                    ):
                        raise BenchmarkError(
                            "internal refusal: Exa live-use authorization is absent"
                        )
                    request = build_provider_request(
                        provider,
                        task=task,
                        manifest=manifest,
                        credentials=execution_credentials,
                    )
                    wire = executor.execute(request)
                    if not wire.hard_deadline_enforced:
                        raise BenchmarkError("request execution lacked a hard wall deadline")
                    raw_sha = sha256_bytes(wire.body)
                    _assert_secret_free(
                        wire.body,
                        execution_credentials.secret_values,
                        artifact_name=(
                            f"ephemeral response {task_index}:{task.task_id}:{provider}"
                        ),
                    )
                    result = normalize_provider_response(provider, wire)
                    policy = manifest.request_policy
                    if policy is None:
                        raise BenchmarkError("v3 request policy is unavailable")
                    result = _apply_output_character_cap(
                        result,
                        max_characters=policy.max_output_characters,
                    )
                    _request_id, _cache_hit, reported_credits, reported_cost = (
                        _provider_body_fields(provider, wire)
                    )
                    if provider == "exa":
                        budget_ledger["exa_usd"] += (
                            reported_cost
                            if reported_cost is not None
                            else manifest.pricing["exa"].per_request
                        )
                        if budget_ledger["exa_usd"] > execution_caps.exa_usd + 1e-12:
                            raise BenchmarkError("observed Exa cost exceeded the hard cap")
                    elif provider == "firecrawl":
                        # creditsUsed is not part of Firecrawl's documented
                        # single-scrape response contract.  It may be useful
                        # diagnostically, but it must never reduce the sealed
                        # conservative charge used for the hard cap.
                        budget_ledger["firecrawl_credits"] += max(
                            task.firecrawl_credit_cap,
                            reported_credits or 0.0,
                        )
                        if (
                            budget_ledger["firecrawl_credits"]
                            > execution_caps.firecrawl_credits + 1e-12
                        ):
                            raise BenchmarkError("observed Firecrawl credits exceeded the hard cap")
                    else:
                        budget_ledger["clusy_usd"] += manifest.pricing["clusy"].per_request
                        if budget_ledger["clusy_usd"] > execution_caps.clusy_usd + 1e-12:
                            raise BenchmarkError("observed Clusy cost exceeded the hard cap")
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
                        manifest=manifest,
                        claim=claim,
                        clusy_preflight=clusy_preflight,
                        execution_caps=execution_caps,
                    )
                    _validate_event_artifact(event, event_index=len(events))
                    events.append(event)
                    event_line = canonical_json_bytes(event) + b"\n"
                    _assert_secret_free(
                        event_line,
                        execution_credentials.secret_values,
                        artifact_name="events journal",
                    )
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
    verify_event_artifacts(
        manifest=manifest,
        events=events,
        run_directory=run_directory,
    )
    summary = summarize_events(
        manifest,
        events,
        bootstrap_samples=manifest.bootstrap_samples,
        events_sha256=events_hasher.hexdigest(),
    )
    summary["observed_budget_ledger"] = dict(budget_ledger)
    _validate_summary_artifact(summary, manifest=manifest)
    if claim.claimable and summary.get("claimable") is not True:
        raise BenchmarkError("internal evidence validation refused a claimable completion")
    summary_bytes = canonical_json_bytes(summary) + b"\n"
    _assert_secret_free(
        summary_bytes,
        execution_credentials.secret_values,
        artifact_name="summary",
    )
    _atomic_write(
        run_directory / "summary.json",
        summary_bytes,
    )
    completion = {
        "run_id": run_id,
        "claimable": summary["claimable"],
        "watermark": summary["watermark"],
        "manifest_sha256": manifest.digest,
        "manifest_artifact_sha256": sha256_bytes(manifest_bytes),
        "run_sha256": sha256_bytes(metadata_bytes),
        "events_sha256": events_hasher.hexdigest(),
        "summary_sha256": sha256_bytes(summary_bytes),
        "completed_at": _utc_now(),
    }
    _validate_completion_artifact(
        completion,
        manifest=manifest,
        run_metadata=metadata,
        summary=summary,
    )
    completion_bytes = canonical_json_bytes(completion) + b"\n"
    _assert_secret_free(
        completion_bytes,
        execution_credentials.secret_values,
        artifact_name="completion",
    )
    _atomic_write(
        run_directory / "completion.json",
        completion_bytes,
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
    if metric == "first_byte_latency_ms":
        return _as_optional_number(event.get("first_byte_latency_ms"))
    if metric == "normalized_cost":
        return _as_optional_number(event.get("normalized_cost"))
    if metric == "cache_conformance":
        return 1.0 if event.get("cache_evidence_matches_mode") is True else 0.0
    if metric == "truncation_free":
        return 0.0 if event.get("truncated") is True else 1.0
    if metric in {
        "token_f1",
        "heading_f1",
        "list_f1",
        "code_f1",
        "table_tree_similarity",
        "structure_score",
    }:
        if reference is None:
            return None
        if event.get("status") != "ok":
            return 0.0
        scoring = event.get("scoring")
        if isinstance(scoring, dict):
            value = _as_optional_number(scoring.get(metric))
            if value is not None:
                return value
        if metric == "token_f1":
            text = event.get("text")
            if not isinstance(text, str):
                return 0.0
            return cast("float", score_text(text, reference.text)["token_f1"])
        return None
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
    first_byte_latencies = [
        value
        for event in events
        if (value := _as_optional_number(event.get("first_byte_latency_ms"))) is not None
    ]
    costs = [
        value
        for event in events
        if (value := _as_optional_number(event.get("normalized_cost"))) is not None
    ]
    token_f1: list[float] = []
    structure_scores: list[float] = []
    for event in events:
        task_id = event.get("task_id")
        reference = references.get(task_id) if isinstance(task_id, str) else None
        value = _metric_value(event, "token_f1", reference=reference)
        if value is not None:
            token_f1.append(value)
        structure_value = _metric_value(event, "structure_score", reference=reference)
        if structure_value is not None:
            structure_scores.append(structure_value)
    cache_matches = [
        1.0 if event.get("cache_evidence_matches_mode") is True else 0.0 for event in events
    ]
    truncation_free = [0.0 if event.get("truncated") is True else 1.0 for event in events]
    output_tokens = [
        value
        for event in events
        if (value := _as_optional_number(event.get("token_count"))) is not None
    ]
    render_values = [
        value for event in events if isinstance((value := event.get("rendered")), bool)
    ]
    model_values = [
        value for event in events if isinstance((value := event.get("model_used")), bool)
    ]
    quality_attempted_values = [
        value for event in events if isinstance((value := event.get("quality_attempted")), bool)
    ]
    quality_succeeded_values = [
        value for event in events if isinstance((value := event.get("quality_succeeded")), bool)
    ]
    completeness_scores = [
        value
        for event in events
        if (value := _as_optional_number(event.get("completeness_score"))) is not None
    ]
    stage_timings: dict[str, list[float]] = {key: [] for key in _STAGE_TIMING_KEYS}
    for event in events:
        timings = event.get("stage_timings_ms")
        if not isinstance(timings, dict):
            continue
        for key in _STAGE_TIMING_KEYS:
            value = _as_optional_number(timings.get(key))
            if value is not None and value >= 0:
                stage_timings[key].append(value)
    return {
        "task_count": len(events),
        "success_rate": sum(successes) / len(successes) if successes else None,
        "latency_ms": _distribution(latencies),
        "first_byte_latency_ms": _distribution(first_byte_latencies),
        "normalized_cost": {
            "observed_count": len(costs),
            "total": sum(costs),
            "mean": sum(costs) / len(costs) if costs else None,
        },
        "reference_task_count": len(token_f1),
        "mean_token_f1": sum(token_f1) / len(token_f1) if token_f1 else None,
        "structure_reference_task_count": len(structure_scores),
        "mean_structure_score": (
            sum(structure_scores) / len(structure_scores) if structure_scores else None
        ),
        "cache_evidence_match_rate": (
            sum(cache_matches) / len(cache_matches) if cache_matches else None
        ),
        "truncation_free_rate": (
            sum(truncation_free) / len(truncation_free) if truncation_free else None
        ),
        "output_tokens": _distribution(output_tokens),
        "render_rate": (
            sum(1.0 if value else 0.0 for value in render_values) / len(render_values)
            if render_values
            else None
        ),
        "model_rate": (
            sum(1.0 if value else 0.0 for value in model_values) / len(model_values)
            if model_values
            else None
        ),
        "quality_attempt_rate": (
            sum(1.0 if value else 0.0 for value in quality_attempted_values)
            / len(quality_attempted_values)
            if quality_attempted_values
            else None
        ),
        "quality_success_rate_when_reported": (
            sum(1.0 if value else 0.0 for value in quality_succeeded_values)
            / len(quality_succeeded_values)
            if quality_succeeded_values
            else None
        ),
        "completeness_score": _distribution(completeness_scores),
        "stage_timings_ms": {
            key: _distribution(values) for key, values in stage_timings.items() if values
        },
    }


def protocol_sha256(manifest: Manifest) -> str:
    """Fingerprint matched settings while allowing mode/window manifests to differ."""
    document = manifest.document
    payload = {
        key: document.get(key)
        for key in (
            "schema_version",
            "seed",
            "runner_region",
            "country",
            "location",
            "scope",
            "content_format",
            "clusy_extraction_profile",
            "timeout_seconds",
            "plans",
            "pricing",
            "budgets",
            "providers",
            "corpus_sha256",
            "required_independent_windows",
            "bootstrap_samples",
            "request_policy",
            "clusy_binding",
            "compliance_acknowledgments",
        )
    }
    return sha256_bytes(canonical_json_bytes(payload))


def summarize_events(
    manifest: Manifest,
    events: Sequence[Mapping[str, Any]],
    *,
    bootstrap_samples: int | None = None,
    events_sha256: str | None = None,
) -> JsonObject:
    """Produce paired per-task deltas and deterministic bootstrap intervals."""
    selected_bootstrap_samples = (
        manifest.bootstrap_samples if bootstrap_samples is None else bootstrap_samples
    )
    if selected_bootstrap_samples != manifest.bootstrap_samples:
        raise BenchmarkError(
            "bootstrap_samples must exactly match the sealed manifest value "
            f"{manifest.bootstrap_samples}"
        )
    by_task_provider: dict[tuple[str, str], Mapping[str, Any]] = {}
    duplicate_first_attempt = False
    for event in events:
        task_id = event.get("task_id")
        provider = event.get("provider")
        attempt = event.get("attempt")
        if isinstance(task_id, str) and provider in manifest.providers and attempt == 1:
            key = (task_id, cast("str", provider))
            if key in by_task_provider:
                duplicate_first_attempt = True
            else:
                by_task_provider[key] = event
    metrics = (
        ("success", True),
        ("latency_ms", False),
        ("first_byte_latency_ms", False),
        ("normalized_cost", False),
        ("token_f1", True),
        ("structure_score", True),
        ("heading_f1", True),
        ("list_f1", True),
        ("code_f1", True),
        ("table_tree_similarity", True),
        ("cache_conformance", True),
        ("truncation_free", True),
    )
    pairwise: list[JsonObject] = []
    for left, right in combinations(manifest.providers, 2):
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
                        "structure_strata": list(reference_structure_strata(task.reference)),
                        "domain_cluster": task.domain_cluster,
                        "left": left_value,
                        "right": right_value,
                        "delta": delta,
                    }
                )
            if not deltas:
                continue
            cluster_deltas: dict[str, list[float]] = {}
            for row in per_task:
                cluster_deltas.setdefault(
                    cast("str", row["domain_cluster"]),
                    [],
                ).append(cast("float", row["delta"]))
            clustered_values = [
                sum(values) / len(values) for _cluster, values in sorted(cluster_deltas.items())
            ]
            lower, upper = deterministic_bootstrap_ci(
                clustered_values,
                seed=_pair_seed(manifest.seed, metric, left, right),
                samples=selected_bootstrap_samples,
            )
            stratum_deltas: dict[str, dict[str, list[float]]] = {}
            for row in per_task:
                strata = (
                    cast("list[str]", row["structure_strata"])
                    if metric == "structure_score"
                    else [cast("str", row["stratum"])]
                )
                for stratum in strata:
                    stratum_deltas.setdefault(
                        stratum,
                        {},
                    ).setdefault(
                        cast("str", row["domain_cluster"]),
                        [],
                    ).append(cast("float", row["delta"]))
            by_stratum: list[JsonObject] = []
            for stratum, clusters in sorted(stratum_deltas.items()):
                values = [
                    sum(cluster_values) / len(cluster_values)
                    for _cluster, cluster_values in sorted(clusters.items())
                ]
                stratum_lower, stratum_upper = deterministic_bootstrap_ci(
                    values,
                    seed=_pair_seed(
                        manifest.seed,
                        f"{metric}:{stratum}",
                        left,
                        right,
                    ),
                    samples=selected_bootstrap_samples,
                )
                by_stratum.append(
                    {
                        "stratum": stratum,
                        "stratum_basis": (
                            "reference_component_presence.v1"
                            if metric == "structure_score"
                            else "manifest_descriptive_label"
                        ),
                        "paired_task_count": sum(
                            len(cluster_values) for cluster_values in clusters.values()
                        ),
                        "paired_domain_cluster_count": len(values),
                        "mean_delta_left_minus_right": sum(values) / len(values),
                        "bootstrap_ci_95": [stratum_lower, stratum_upper],
                        "bootstrap_unit": "domain_cluster_mean",
                    }
                )
            pairwise.append(
                {
                    "left_provider": left,
                    "right_provider": right,
                    "metric": metric,
                    "higher_is_better": higher_is_better,
                    "paired_task_count": len(deltas),
                    "paired_domain_cluster_count": len(clustered_values),
                    "mean_delta_left_minus_right": sum(deltas) / len(deltas),
                    "bootstrap_samples": selected_bootstrap_samples,
                    "bootstrap_rng": "python-mt19937-with-sha256-derived-seed.v1",
                    "bootstrap_unit": "domain_cluster_mean",
                    "bootstrap_ci_95": [lower, upper],
                    "by_stratum": by_stratum,
                    "per_task_deltas": per_task,
                }
            )
    claims = {event.get("claimable") for event in events}
    expected_keys = {
        (task.task_id, provider) for task in manifest.tasks for provider in manifest.providers
    }
    manifest_matches = all(event.get("manifest_sha256") == manifest.digest for event in events)
    expected_event_schema = (
        EVENT_SCHEMA_VERSION
        if manifest.schema_version == SCHEMA_VERSION
        else V1_EVENT_SCHEMA_VERSION
    )
    evidence_fields_valid = all(
        event.get("event_schema_version") == expected_event_schema
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
        and (
            manifest.schema_version != SCHEMA_VERSION
            or (
                isinstance(event.get("endpoint_sha256"), str)
                and bool(_SHA256.fullmatch(cast("str", event.get("endpoint_sha256"))))
                and (
                    event.get("provider") != "clusy"
                    or event.get("endpoint") == "[CLUSY_ENDPOINT_REDACTED]"
                )
            )
        )
        and (
            manifest.schema_version != SCHEMA_VERSION
            or (
                event.get("hard_deadline_enforced") is True
                and isinstance(event.get("cache_evidence_matches_mode"), bool)
                and isinstance(
                    event.get("cache_evidence_contractually_documented"),
                    bool,
                )
                and isinstance(
                    event.get("credit_evidence_contractually_documented"),
                    bool,
                )
                and event.get("time_window_id") == manifest.time_window_id
                and isinstance(event.get("clusy_preflight"), dict)
                and manifest.request_policy is not None
                and event.get("benchmark_output_cap_characters")
                == manifest.request_policy.max_output_characters
                and isinstance(event.get("benchmark_output_cap_applied"), bool)
                and isinstance(event.get("stage_timings_ms"), dict)
            )
        )
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
    for provider in manifest.providers:
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
                            [event for event in provider_events if event.get("stratum") == stratum],
                        ),
                    }
                    for stratum in strata
                ],
            }
        )
    structural_references_complete = all(
        task.reference is not None and task.reference.structure is not None
        for task in manifest.tasks
    )
    cache_evidence_complete = bool(events) and all(
        event.get("cache_evidence_matches_mode") is True for event in events
    )
    contractual_cache_and_credit_evidence_complete = bool(events) and all(
        event.get("cache_evidence_contractually_documented") is True
        and event.get("credit_evidence_contractually_documented") is True
        for event in events
    )
    current_protocol = manifest.schema_version == SCHEMA_VERSION
    return {
        "summary_schema_version": (
            SUMMARY_SCHEMA_VERSION if current_protocol else V1_SUMMARY_SCHEMA_VERSION
        ),
        "manifest_sha256": manifest.digest,
        "manifest_schema_version": manifest.schema_version,
        "corpus_sha256": manifest.corpus_sha256 or None,
        "protocol_sha256": protocol_sha256(manifest) if current_protocol else None,
        "mode": manifest.mode,
        "cache_max_age_seconds": manifest.cache_max_age_seconds,
        "warm_cache_primed_at": manifest.warm_cache_primed_at,
        "time_window_id": manifest.time_window_id,
        "independent_window_index": manifest.independent_window_index,
        "required_independent_time_windows": manifest.required_independent_windows,
        "bootstrap_samples": manifest.bootstrap_samples,
        "evaluated_providers": list(manifest.providers),
        "quality_scope_by_provider": {
            provider: (
                "provider_default_compact_main_content"
                if provider == "exa"
                else "full_main_content"
            )
            for provider in manifest.providers
        },
        "quality_scope_comparable_to_full_main_content": {
            provider: provider != "exa" for provider in manifest.providers
        },
        "compliance_acknowledgments": (
            {
                "third_party_data_transfer_authorized": (
                    manifest.compliance_acknowledgments.third_party_data_transfer_authorized
                ),
                "exa_live_authorized": (manifest.compliance_acknowledgments.exa_live_authorized),
                "exa_authorized_purpose": (
                    manifest.compliance_acknowledgments.exa_authorized_purpose
                ),
            }
            if manifest.compliance_acknowledgments is not None
            else None
        ),
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
            "required_independent_time_windows": manifest.required_independent_windows,
            "structural_fidelity_metrics_complete": structural_references_complete,
            "table_tree_metric_complete": structural_references_complete,
            "cache_evidence_complete": cache_evidence_complete,
            "contractual_cache_and_credit_evidence_complete": (
                contractual_cache_and_credit_evidence_complete
            ),
            "quality_metrics_independently_verifiable": not current_protocol,
            "execution_attestation_verified": False,
            "cold_and_warm_tail_latency_complete": False,
            "reason": (
                "One run cannot establish a vendor win. Aggregate matched cold and "
                "warm v3 runs across every pre-registered independent time window."
                if current_protocol
                else "The legacy v1 runner validates one cold fixed-URL artifact window "
                "and is offline-compatible only."
            ),
        },
        "watermark": ("" if artifact_integrity_claimable else "NONCLAIMABLE"),
        "event_count": len(events),
        "provider_output_retention_policy": "hashes_and_derived_metrics_only",
        "raw_provider_outputs_retained": False if current_protocol else None,
        "quality_metrics_independently_verifiable": not current_protocol,
        "execution_attestation_verified": False,
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


def _pairwise_row(
    summary: Mapping[str, Any],
    *,
    competitor: str,
    metric: str,
) -> Mapping[str, Any] | None:
    rows = summary.get("pairwise")
    if not isinstance(rows, list):
        return None
    for row in rows:
        if (
            isinstance(row, dict)
            and row.get("left_provider") == "clusy"
            and row.get("right_provider") == competitor
            and row.get("metric") == metric
        ):
            return row
    return None


def _valid_ci(row: Mapping[str, Any] | None) -> tuple[float, float] | None:
    if row is None:
        return None
    interval = row.get("bootstrap_ci_95")
    if not isinstance(interval, list) or len(interval) != 2:
        return None
    lower = _as_optional_number(interval[0])
    upper = _as_optional_number(interval[1])
    if lower is None or upper is None:
        return None
    return lower, upper


def _claim_count_at_least(value: Any, minimum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _provider_summary_row(
    summary: Mapping[str, Any],
    provider: str,
) -> Mapping[str, Any] | None:
    rows = summary.get("provider_summaries")
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("provider") == provider:
            return row
    return None


def _nested_number(
    value: Mapping[str, Any] | None,
    group: str,
    metric: str,
) -> float | None:
    if value is None:
        return None
    nested = value.get(group)
    if not isinstance(nested, dict):
        return None
    return _as_optional_number(nested.get(metric))


def validate_independent_window_timing(
    evidence: Sequence[WindowTimingEvidence],
) -> tuple[str, ...]:
    """Validate artifact-derived cold/warm pairing and independent spacing."""
    reasons: list[str] = []
    if not evidence:
        return ("no artifact-derived window timing evidence was supplied",)
    required_values = {item.required_independent_windows for item in evidence}
    if len(required_values) != 1:
        reasons.append("artifact-derived required window counts are inconsistent")
        required_windows = 0
    else:
        required_windows = next(iter(required_values))
    by_key: dict[tuple[int, str], WindowTimingEvidence] = {}
    for item in evidence:
        key = (item.independent_window_index, item.mode)
        if key in by_key:
            reasons.append("duplicate artifact-derived mode/window-index timing evidence")
        by_key[key] = item
        if not (
            item.manifest_created_at
            <= item.run_created_at
            <= item.first_request_started_at
            <= item.oldest_request_completed_at
            <= item.last_request_completed_at
            <= item.completion_at
        ):
            reasons.append(
                f"window {item.independent_window_index} {item.mode} timestamps are not monotonic"
            )
        if item.mode == "cold_live":
            if item.cache_max_age_seconds != 0 or item.warm_cache_primed_at is not None:
                reasons.append(
                    f"window {item.independent_window_index} cold timing evidence "
                    "contains a warm-cache prime"
                )
        elif item.mode == "warm_cache":
            prime = item.warm_cache_primed_at
            if (
                prime is None
                or item.cache_max_age_seconds <= 0
                or prime > item.manifest_created_at
                or item.first_request_started_at < prime
                or item.last_request_completed_at
                > prime + timedelta(seconds=item.cache_max_age_seconds)
                or item.completion_at > prime + timedelta(seconds=item.cache_max_age_seconds)
            ):
                reasons.append(
                    f"window {item.independent_window_index} warm run is not "
                    "contained in its sealed cache-prime interval"
                )
        else:
            reasons.append(f"unsupported artifact-derived mode {item.mode}")

    paired_cold_starts: list[tuple[int, datetime, datetime]] = []
    for index in range(1, required_windows + 1):
        cold = by_key.get((index, "cold_live"))
        warm = by_key.get((index, "warm_cache"))
        if cold is None or warm is None:
            reasons.append(f"window index {index} lacks one artifact-derived cold/warm pair")
            continue
        if cold.time_window_id != warm.time_window_id:
            reasons.append(f"window index {index} cold/warm artifact IDs do not match")
        if warm.warm_cache_primed_at != cold.oldest_request_completed_at:
            reasons.append(
                f"window index {index} warm prime is not bound to the paired "
                "oldest cold request completion"
            )
        if cold.completion_at > warm.first_request_started_at:
            reasons.append(f"window index {index} warm requests overlap the paired cold run")
        paired_cold_starts.append((index, cold.first_request_started_at, warm.completion_at))

    paired_cold_starts.sort()
    for previous, current in zip(
        paired_cold_starts,
        paired_cold_starts[1:],
        strict=False,
    ):
        previous_index, previous_start, previous_warm_completion = previous
        current_index, current_start, _current_warm_completion = current
        if current_start <= previous_start:
            reasons.append("independent window indices are not in chronological order")
        if (
            current_start - previous_start
        ).total_seconds() < MIN_INDEPENDENT_WINDOW_SPACING_SECONDS:
            reasons.append(
                f"windows {previous_index} and {current_index} are separated by "
                f"less than {MIN_INDEPENDENT_WINDOW_SPACING_SECONDS} seconds"
            )
        if current_start < previous_warm_completion:
            reasons.append(f"window {current_index} overlaps the preceding warm run")
    return tuple(sorted(set(reasons)))


def _evaluate_v3_summaries(
    summaries: Sequence[Mapping[str, Any]],
    *,
    artifact_chains_verified: bool,
    timing_validation_reasons: Sequence[str],
) -> JsonObject:
    """Evaluate a scoped Clusy-vendor gate from matched multi-window evidence."""
    common_reasons: list[str] = []
    if not summaries:
        raise BenchmarkError("aggregate requires at least one summary")
    if not artifact_chains_verified:
        common_reasons.append("inputs were not loaded from verified completed-run artifact chains")
    common_reasons.extend(timing_validation_reasons)
    # v3 deliberately retains only hashes and derived metrics.  That protects
    # provider output, but it means a third party cannot recompute extraction
    # quality from the artifacts.  No trusted execution-attestation verifier is
    # implemented in this runner, so a public vendor-win claim must remain
    # closed even after the local hash chain has been checked.
    common_reasons.extend(
        (
            "hash-only retention prevents independent recomputation of quality metrics",
            "no verifiable execution attestation is present",
            "no externally verifiable preregistration timestamp is present",
        )
    )
    if any(
        summary.get("summary_schema_version") != SUMMARY_SCHEMA_VERSION for summary in summaries
    ):
        raise BenchmarkError("aggregate accepts only v3 summaries")
    corpora = {summary.get("corpus_sha256") for summary in summaries}
    protocols = {summary.get("protocol_sha256") for summary in summaries}
    bootstrap_values = {summary.get("bootstrap_samples") for summary in summaries}
    required_values = {summary.get("required_independent_time_windows") for summary in summaries}
    provider_sets: list[tuple[str, ...]] = []
    for summary in summaries:
        raw_providers = summary.get("evaluated_providers")
        if (
            not isinstance(raw_providers, list)
            or any(provider not in PROVIDERS for provider in raw_providers)
            or len(raw_providers) != len(set(raw_providers))
            or "clusy" not in raw_providers
            or len(raw_providers) < 2
            or raw_providers != [provider for provider in PROVIDERS if provider in raw_providers]
        ):
            common_reasons.append("evaluated provider scope is malformed")
            continue
        provider_sets.append(tuple(cast("list[str]", raw_providers)))
    selected_providers: tuple[Provider, ...]
    if len(set(provider_sets)) != 1 or len(provider_sets) != len(summaries):
        common_reasons.append("summaries do not share one evaluated provider scope")
        selected_providers = PROVIDERS
    else:
        selected_providers = cast(
            "tuple[Provider, ...]",
            provider_sets[0],
        )
    competitors = tuple(provider for provider in selected_providers if provider != "clusy")
    if len(corpora) != 1 or None in corpora:
        common_reasons.append("summaries do not share one sealed corpus")
    if len(protocols) != 1 or None in protocols:
        common_reasons.append("summaries do not share one matched protocol")
    if len(bootstrap_values) != 1 or any(
        isinstance(value, bool) or not isinstance(value, int) or not (100 <= value <= 1_000_000)
        for value in bootstrap_values
    ):
        common_reasons.append(
            "summaries do not share one valid pre-registered bootstrap sample count"
        )
    if len(required_values) != 1 or not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 2
        for value in required_values
    ):
        common_reasons.append("required window counts are inconsistent")
        required_windows = 2
    else:
        required_windows = cast("int", next(iter(required_values)))
    observed_modes = {summary.get("mode") for summary in summaries}
    if observed_modes - {"cold_live", "warm_cache"}:
        common_reasons.append("summaries contain an unsupported mode")
    if len(summaries) != 2 * required_windows:
        common_reasons.append("aggregate requires exactly one cold and one warm summary per window")
    run_keys = [(summary.get("mode"), summary.get("time_window_id")) for summary in summaries]
    if len(set(run_keys)) != len(run_keys):
        common_reasons.append("duplicate mode/time-window summaries are forbidden")
    for mode in ("cold_live", "warm_cache"):
        windows = {
            summary.get("time_window_id") for summary in summaries if summary.get("mode") == mode
        }
        indices = {
            summary.get("independent_window_index")
            for summary in summaries
            if summary.get("mode") == mode
        }
        if len(windows) < required_windows:
            common_reasons.append(f"{mode} has fewer than {required_windows} independent windows")
        if indices != set(range(1, required_windows + 1)):
            common_reasons.append(f"{mode} does not cover every registered window index")
    cold_ages = {
        summary.get("cache_max_age_seconds")
        for summary in summaries
        if summary.get("mode") == "cold_live"
    }
    warm_ages = {
        summary.get("cache_max_age_seconds")
        for summary in summaries
        if summary.get("mode") == "warm_cache"
    }
    if cold_ages != {0}:
        common_reasons.append("cold_live cache ages are not all zero")
    if len(warm_ages) != 1 or any(
        isinstance(age, bool) or not isinstance(age, int) or age <= 0 for age in warm_ages
    ):
        common_reasons.append("warm_cache age is missing or inconsistent")
    for index in range(1, required_windows + 1):
        cold_ids = {
            summary.get("time_window_id")
            for summary in summaries
            if summary.get("mode") == "cold_live"
            and summary.get("independent_window_index") == index
        }
        warm_ids = {
            summary.get("time_window_id")
            for summary in summaries
            if summary.get("mode") == "warm_cache"
            and summary.get("independent_window_index") == index
        }
        if len(cold_ids) != 1 or cold_ids != warm_ids:
            common_reasons.append(f"cold/warm summaries are not paired for window index {index}")
    if any(summary.get("artifact_integrity_claimable") is not True for summary in summaries):
        common_reasons.append("one or more runs failed artifact-integrity validation")
    if any(
        not isinstance(summary.get("vendor_win_gate"), dict)
        or cast("Mapping[str, Any]", summary["vendor_win_gate"]).get(
            "structural_fidelity_metrics_complete"
        )
        is not True
        or cast("Mapping[str, Any]", summary["vendor_win_gate"]).get("cache_evidence_complete")
        is not True
        for summary in summaries
    ):
        common_reasons.append("structure or cache evidence is incomplete")
    for summary in summaries:
        provider_summaries = summary.get("provider_summaries")
        if (
            not isinstance(provider_summaries, list)
            or len(provider_summaries) != len(selected_providers)
            or {item.get("provider") for item in provider_summaries if isinstance(item, dict)}
            != set(selected_providers)
        ):
            common_reasons.append("provider summaries are incomplete")
            continue
        for provider_summary in provider_summaries:
            if not isinstance(provider_summary, dict):
                common_reasons.append("provider summary is malformed")
                continue
            latency = provider_summary.get("latency_ms")
            cost = provider_summary.get("normalized_cost")
            if (
                not isinstance(latency, dict)
                or _as_optional_number(latency.get("p95")) is None
                or _as_optional_number(latency.get("p99")) is None
            ):
                common_reasons.append("p95/p99 latency is incomplete")
            if not isinstance(cost, dict) or _as_optional_number(cost.get("mean")) is None:
                common_reasons.append("normalized provider cost is incomplete")

    provider_gates: JsonObject = {}
    for competitor in ("exa", "firecrawl"):
        provider_reasons = list(common_reasons)
        if competitor not in competitors:
            provider_reasons = [f"{competitor} was not selected in this benchmark scope"]
        elif competitor == "exa" and any(
            not isinstance(summary.get("compliance_acknowledgments"), dict)
            or cast(
                "Mapping[str, Any]",
                summary["compliance_acknowledgments"],
            ).get("exa_live_authorized")
            is not True
            or cast(
                "Mapping[str, Any]",
                summary["compliance_acknowledgments"],
            ).get("exa_authorized_purpose")
            != "benchmark_only_no_training_distillation_or_labeling"
            for summary in summaries
        ):
            provider_reasons.append(
                "Exa live-use authorization is absent from one or more sealed runs"
            )
        if competitor == "exa" and competitor in competitors:
            provider_reasons.append(
                "Exa warm-cache text uses provider-default compact main content; "
                "official section/verbosity filters require maxAgeHours=0, so its "
                "quality scope is not comparable to full-main-content extraction"
            )
        if competitor == "firecrawl" and competitor in competitors:
            provider_reasons.append(
                "Firecrawl cacheState/creditsUsed evidence is not contractually "
                "documented and no provider-side per-request spend cap is "
                "verified; diagnostic observations cannot support a public claim"
            )
        for summary in summaries:
            if competitor not in competitors:
                break
            run_label = (
                f"{summary.get('mode', 'unknown-mode')}/"
                f"{summary.get('time_window_id', 'unknown-window')}"
            )
            token_row = _pairwise_row(
                summary,
                competitor=competitor,
                metric="token_f1",
            )
            if token_row is None or not _claim_count_at_least(
                token_row.get("paired_task_count"),
                MIN_CLAIM_PAIRED_TASKS,
            ):
                provider_reasons.append(
                    f"{run_label} token_f1 has fewer than {MIN_CLAIM_PAIRED_TASKS} paired tasks"
                )
            if token_row is None or not _claim_count_at_least(
                token_row.get("paired_domain_cluster_count"),
                MIN_CLAIM_DOMAIN_CLUSTERS,
            ):
                provider_reasons.append(
                    f"{run_label} token_f1 has fewer than "
                    f"{MIN_CLAIM_DOMAIN_CLUSTERS} paired domain clusters"
                )
            for metric in ("token_f1", "structure_score"):
                interval = _valid_ci(_pairwise_row(summary, competitor=competitor, metric=metric))
                if interval is None or interval[0] <= 0:
                    provider_reasons.append(
                        f"Clusy {metric} superiority over {competitor} is not replicated"
                    )
            success_interval = _valid_ci(
                _pairwise_row(summary, competitor=competitor, metric="success")
            )
            if success_interval is None or success_interval[0] < -0.02:
                provider_reasons.append(f"Clusy success is not non-inferior to {competitor}")

            latency_interval = _valid_ci(
                _pairwise_row(summary, competitor=competitor, metric="latency_ms")
            )
            if latency_interval is None or latency_interval[1] > 0:
                provider_reasons.append(f"Clusy paired latency is not non-inferior to {competitor}")
            cost_interval = _valid_ci(
                _pairwise_row(
                    summary,
                    competitor=competitor,
                    metric="normalized_cost",
                )
            )
            if cost_interval is None or cost_interval[1] > 0:
                provider_reasons.append(
                    f"Clusy normalized cost is not non-inferior to {competitor}"
                )

            clusy_summary = _provider_summary_row(summary, "clusy")
            competitor_summary = _provider_summary_row(summary, competitor)
            for percentile, maximum_ratio in (
                ("p95", MAX_CLAIM_P95_LATENCY_RATIO),
                ("p99", MAX_CLAIM_P99_LATENCY_RATIO),
            ):
                clusy_latency = _nested_number(
                    clusy_summary,
                    "latency_ms",
                    percentile,
                )
                competitor_latency = _nested_number(
                    competitor_summary,
                    "latency_ms",
                    percentile,
                )
                if (
                    clusy_latency is None
                    or competitor_latency is None
                    or competitor_latency <= 0
                    or clusy_latency > competitor_latency * maximum_ratio
                ):
                    provider_reasons.append(
                        f"Clusy {percentile} latency exceeds the "
                        f"{maximum_ratio:.2f}x non-inferiority limit versus {competitor}"
                    )
            clusy_cost = _nested_number(
                clusy_summary,
                "normalized_cost",
                "mean",
            )
            competitor_cost = _nested_number(
                competitor_summary,
                "normalized_cost",
                "mean",
            )
            if (
                clusy_cost is None
                or competitor_cost is None
                or competitor_cost <= 0
                or clusy_cost > competitor_cost * MAX_CLAIM_NORMALIZED_COST_RATIO
            ):
                provider_reasons.append(
                    "Clusy mean normalized cost exceeds the "
                    f"{MAX_CLAIM_NORMALIZED_COST_RATIO:.2f}x non-inferiority "
                    f"limit versus {competitor}"
                )
            structure_row = _pairwise_row(
                summary,
                competitor=competitor,
                metric="structure_score",
            )
            strata = structure_row.get("by_stratum") if structure_row else None
            if not isinstance(strata, list) or not strata:
                provider_reasons.append(f"{competitor} structure strata are missing")
            else:
                stratum_names = {
                    stratum.get("stratum")
                    for stratum in strata
                    if isinstance(stratum, dict) and isinstance(stratum.get("stratum"), str)
                }
                if any(
                    not isinstance(stratum, dict)
                    or stratum.get("stratum") not in _STRUCTURE_STRATA
                    or stratum.get("stratum_basis") != "reference_component_presence.v1"
                    for stratum in strata
                ):
                    provider_reasons.append(
                        f"{run_label} structure strata are not derived from "
                        "reference component presence"
                    )
                if len(strata) < MIN_CLAIM_STRATA or len(stratum_names) < MIN_CLAIM_STRATA:
                    provider_reasons.append(
                        f"{run_label} structure evidence has fewer than "
                        f"{MIN_CLAIM_STRATA} distinct strata"
                    )
                for stratum in strata:
                    stratum_name = (
                        stratum.get("stratum", "<malformed>")
                        if isinstance(stratum, dict)
                        else "<malformed>"
                    )
                    if not isinstance(stratum, dict) or not _claim_count_at_least(
                        stratum.get("paired_domain_cluster_count"),
                        MIN_CLAIM_STRATUM_DOMAIN_CLUSTERS,
                    ):
                        provider_reasons.append(
                            f"{run_label} structure stratum {stratum_name} has fewer "
                            f"than {MIN_CLAIM_STRATUM_DOMAIN_CLUSTERS} paired "
                            "domain clusters"
                        )
                    interval = _valid_ci(stratum) if isinstance(stratum, dict) else None
                    if interval is None or interval[0] < -0.01:
                        provider_reasons.append(
                            f"important structure stratum regressed versus {competitor}"
                        )
                        break
        provider_reasons = sorted(set(provider_reasons))
        provider_gates[competitor] = {
            "selected": competitor in competitors,
            "passed": competitor in competitors and not provider_reasons,
            "minimum_evidence": {
                "paired_tasks_per_run": MIN_CLAIM_PAIRED_TASKS,
                "paired_domain_clusters_per_run": MIN_CLAIM_DOMAIN_CLUSTERS,
                "distinct_strata_per_run": MIN_CLAIM_STRATA,
                "paired_domain_clusters_per_structure_stratum": (MIN_CLAIM_STRATUM_DOMAIN_CLUSTERS),
            },
            "reasons": provider_reasons,
        }

    common_reasons = sorted(set(common_reasons))
    scoped_reasons = [
        f"{competitor}: {reason}"
        for competitor in competitors
        for reason in cast(
            "Sequence[str]",
            cast("Mapping[str, Any]", provider_gates[competitor])["reasons"],
        )
    ]
    reasons = sorted(set(common_reasons + scoped_reasons))
    passed = bool(competitors) and all(
        cast("Mapping[str, Any]", provider_gates[competitor]).get("passed") is True
        for competitor in competitors
    )
    return {
        "aggregate_schema_version": AGGREGATE_SCHEMA_VERSION,
        "corpus_sha256": next(iter(corpora)) if len(corpora) == 1 else None,
        "protocol_sha256": next(iter(protocols)) if len(protocols) == 1 else None,
        "bootstrap_samples": (next(iter(bootstrap_values)) if len(bootstrap_values) == 1 else None),
        "run_count": len(summaries),
        "required_independent_time_windows": required_windows,
        "evaluated_providers": list(selected_providers),
        "claim_scope": {
            "layer": "fixed_url_main_content_markdown_extraction",
            "clusy_compared_to": list(competitors),
            "does_not_cover_unselected_providers": True,
        },
        "vendor_win_claimable": passed,
        "vendor_win_claimable_by_provider": {
            provider: cast("Mapping[str, Any]", provider_gates[provider]).get("passed") is True
            for provider in ("exa", "firecrawl")
        },
        "exa_vendor_win_claimable": cast(
            "Mapping[str, Any]",
            provider_gates["exa"],
        ).get("passed")
        is True,
        "vendor_win_watermark": "" if passed else "NO_VENDOR_WIN_CLAIM",
        "artifact_chains_verified": artifact_chains_verified,
        "independent_window_timing_verified": not timing_validation_reasons,
        "quality_metrics_independently_verifiable": False,
        "execution_attestation_verified": False,
        "gate": {
            "passed": passed,
            "success_noninferiority_margin": 0.02,
            "important_stratum_nonregression_margin": 0.01,
            "paired_latency_noninferiority_max_delta_ms": 0,
            "p95_latency_noninferiority_ratio": MAX_CLAIM_P95_LATENCY_RATIO,
            "p99_latency_noninferiority_ratio": MAX_CLAIM_P99_LATENCY_RATIO,
            "paired_normalized_cost_noninferiority_max_delta": 0,
            "mean_normalized_cost_noninferiority_ratio": (MAX_CLAIM_NORMALIZED_COST_RATIO),
            "minimum_evidence": {
                "paired_tasks_per_selected_competitor_per_run": (MIN_CLAIM_PAIRED_TASKS),
                "paired_domain_clusters_per_selected_competitor_per_run": (
                    MIN_CLAIM_DOMAIN_CLUSTERS
                ),
                "distinct_structure_strata_per_run": MIN_CLAIM_STRATA,
                "paired_domain_clusters_per_structure_stratum": (MIN_CLAIM_STRATUM_DOMAIN_CLUSTERS),
            },
            "reasons": reasons,
            "by_provider": provider_gates,
        },
        "interpretation": (
            "Claim scope is fixed-URL main-content Markdown extraction under the "
            "sealed corpus, protocol, plans, selected providers, modes, and time "
            "windows only; it does not cover any unselected provider."
            if passed
            else "The fixed-URL vendor-win gate remains closed."
        ),
    }


def aggregate_v3_summaries(summaries: Sequence[Mapping[str, Any]]) -> JsonObject:
    """Build a descriptive aggregate from unverified in-memory summaries.

    Arbitrary dictionaries are never accepted as public claim evidence.
    """
    return _evaluate_v3_summaries(
        summaries,
        artifact_chains_verified=False,
        timing_validation_reasons=(
            "independent-window timing was not derived from completed-run artifacts",
        ),
    )


def aggregate_existing_summaries(
    *,
    summary_paths: Sequence[Path],
    output_path: Path,
) -> None:
    del summary_paths, output_path
    raise BenchmarkError(
        "standalone summary files are not aggregate evidence; provide completed "
        "run directories so manifest, events, summary, and completion hashes can "
        "be verified"
    )


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
    task_by_id = {task.task_id: task for task in manifest.tasks}
    expected_orders = randomized_orders(manifest)
    if manifest.schema_version == SCHEMA_VERSION:
        expected_sequence = [
            (task.task_id, provider)
            for task in manifest.tasks
            for provider in expected_orders[task.task_id]
        ]
        actual_sequence = [(event.get("task_id"), event.get("provider")) for event in events]
        if actual_sequence != expected_sequence:
            raise BenchmarkError(
                "v3 event journal sequence must exactly match manifest task order "
                "and each task's sealed randomized provider order"
            )
    shared_run_id: str | None = None
    shared_claimable: bool | None = None
    shared_provenance: tuple[Any, ...] | None = None
    shared_preflight: Mapping[str, Any] | None = None
    shared_caps: Mapping[str, Any] | None = None
    shared_clusy_endpoint_sha256: str | None = None
    previous_event_completed_at: datetime | None = None
    for index, event in enumerate(events):
        if manifest.schema_version == SCHEMA_VERSION:
            _validate_event_artifact(event, event_index=index)
            event_started_at = _parse_v3_utc_timestamp(
                cast("str", event["started_at"]),
                field_name=f"event {index}.started_at",
            )
            event_completed_at = _parse_v3_utc_timestamp(
                cast("str", event["completed_at"]),
                field_name=f"event {index}.completed_at",
            )
            if (
                previous_event_completed_at is not None
                and event_started_at < previous_event_completed_at
            ):
                raise BenchmarkError(
                    f"event {index}: request bundle overlaps or predates its predecessor"
                )
            previous_event_completed_at = event_completed_at
        if event.get("manifest_sha256") != manifest.digest:
            raise BenchmarkError(f"event {index}: manifest_sha256 mismatch")
        if (
            event.get("task_id") not in task_by_id
            or event.get("provider") not in manifest.providers
        ):
            raise BenchmarkError(f"event {index}: task/provider is outside the manifest")
        provider = event.get("provider")
        task_id = cast("str", event.get("task_id"))
        task = task_by_id[task_id]
        if manifest.schema_version == SCHEMA_VERSION:
            expected_order = expected_orders[task_id]
            expected_position = expected_order.index(cast("Provider", provider))
            event_run_id = cast("str", event.get("run_id"))
            if not re.fullmatch(
                rf"{re.escape(manifest.benchmark_id)}-\d{{8}}T\d{{6}}Z-"
                rf"{manifest.digest[:12]}",
                event_run_id,
            ):
                raise BenchmarkError(f"event {index}: run_id is not bound to the manifest")
            if shared_run_id is None:
                shared_run_id = event_run_id
            elif event_run_id != shared_run_id:
                raise BenchmarkError(f"event {index}: run_id differs across events")
            event_claimable = cast("bool", event.get("claimable"))
            if shared_claimable is None:
                shared_claimable = event_claimable
            elif event_claimable is not shared_claimable:
                raise BenchmarkError(f"event {index}: claimable differs across events")
            provenance = (
                event.get("runner_commit"),
                event.get("runner_sha256"),
                event.get("container_digest"),
            )
            if shared_provenance is None:
                shared_provenance = provenance
            elif provenance != shared_provenance:
                raise BenchmarkError(f"event {index}: provenance differs across events")
            if event_claimable and (
                event.get("runner_commit") == "unknown"
                or event.get("container_digest") == "unknown"
            ):
                raise BenchmarkError(f"event {index}: claimable provenance is unknown")
            preflight = cast("Mapping[str, Any]", event.get("clusy_preflight"))
            if shared_preflight is None:
                shared_preflight = preflight
            elif preflight != shared_preflight:
                raise BenchmarkError(f"event {index}: Clusy preflight differs across events")
            binding = manifest.clusy_binding
            if (
                binding is None
                or preflight.get("revision") != binding.expected_revision
                or preflight.get("config_fingerprint") != binding.expected_config_sha256
                or preflight.get("image_digest") != binding.expected_image_digest
            ):
                raise BenchmarkError(f"event {index}: Clusy preflight does not match manifest")
            caps = cast("Mapping[str, Any]", event.get("execution_budget_caps"))
            _validate_execution_caps_against_manifest(
                caps,
                manifest=manifest,
                field_name=f"event {index} execution caps",
            )
            if shared_caps is None:
                shared_caps = caps
            elif caps != shared_caps:
                raise BenchmarkError(f"event {index}: execution caps differ across events")
            if (
                event.get("stratum") != task.stratum
                or event.get("structure_strata") != list(reference_structure_strata(task.reference))
                or event.get("structure_strata_basis") != "reference_component_presence.v1"
                or event.get("language") != task.language
                or event.get("domain_cluster") != task.domain_cluster
                or event.get("content_type") != task.content_type
                or event.get("render_class") != task.render_class
                or event.get("mode") != manifest.mode
                or event.get("time_window_id") != manifest.time_window_id
                or event.get("independent_window_index") != manifest.independent_window_index
                or event.get("required_independent_windows")
                != manifest.required_independent_windows
                or event.get("plan") != manifest.plans[cast("Provider", provider)]
                or event.get("api_version")
                != {
                    "clusy": "clusy-crawl-v1",
                    "exa": "contents-current-2026-07-28",
                    "firecrawl": "v2",
                }[cast("Provider", provider)]
                or event.get("runner_region") != manifest.runner_region
                or event.get("query_sha256") != sha256_bytes(task.url.encode("utf-8"))
                or event.get("quality_scope")
                != (
                    "provider_default_compact_main_content"
                    if provider == "exa"
                    else "full_main_content"
                )
                or event.get("quality_scope_comparable_to_full_main_content")
                is not (provider != "exa")
                or event.get("max_age") != manifest.cache_max_age_seconds
                or event.get("timeout") != manifest.timeout_seconds
                or event.get("randomized_order") != list(expected_order)
                or event.get("order_position") != expected_position
                or event.get("randomization_seed") != manifest.seed
                or event.get("attempt") != 1
                or event.get("reference_sha256")
                != (task.reference.sha256 if task.reference else None)
                or event.get("reference_method")
                != (task.reference.method if task.reference else None)
            ):
                raise BenchmarkError(f"event {index}: sealed protocol fields do not match")
            scoring = cast("Mapping[str, Any]", event.get("scoring"))
            reference = task.reference
            if (
                reference is None
                or reference.structure is None
                or scoring.get("reference_tokens") != len(_tokenize(reference.text))
                or scoring.get("reference_component_count")
                != sum(
                    bool(component)
                    for component in (
                        reference.structure.headings,
                        reference.structure.list_items,
                        reference.structure.code_blocks,
                        reference.structure.tables,
                    )
                )
                or scoring.get("reference_table_tree_tokens")
                != len(_table_tree_tokens(reference.structure.tables))
            ):
                raise BenchmarkError(f"event {index}: scoring reference fields do not match")
            for score_key, count_key, reference_count in (
                (
                    "heading_f1",
                    "observed_headings",
                    len(reference.structure.headings),
                ),
                (
                    "list_f1",
                    "observed_list_items",
                    len(reference.structure.list_items),
                ),
                (
                    "code_f1",
                    "observed_code_blocks",
                    len(reference.structure.code_blocks),
                ),
            ):
                score = scoring.get(score_key)
                if score is None:
                    continue
                observed_count = cast("int", scoring[count_key])
                _validate_discrete_overlap_feasibility(
                    (
                        (
                            score_key,
                            float(score),
                            observed_count + reference_count,
                            2.0,
                        ),
                    ),
                    max_overlap=min(observed_count, reference_count),
                    field_name=f"event {index}.scoring",
                )
            perfect_structure_counts = (
                ("heading_f1", "observed_headings", len(reference.structure.headings)),
                ("list_f1", "observed_list_items", len(reference.structure.list_items)),
                ("code_f1", "observed_code_blocks", len(reference.structure.code_blocks)),
                ("table_tree_similarity", "observed_tables", len(reference.structure.tables)),
            )
            if any(
                scoring.get(score_key) == 1 and scoring.get(count_key) != expected_count
                for score_key, count_key, expected_count in perfect_structure_counts
            ):
                raise BenchmarkError(
                    f"event {index}: perfect structure scores contradict observed counts"
                )
            expected_pricing = manifest.pricing[cast("Provider", provider)]
            if (
                _as_optional_number(event.get("normalized_cost")) != expected_pricing.per_request
                or event.get("normalized_cost_currency") != expected_pricing.currency
                or event.get("normalized_cost_source") != "frozen_manifest_per_request"
            ):
                raise BenchmarkError(f"event {index}: normalized cost does not match manifest")
            endpoint = event.get("endpoint")
            endpoint_sha = event.get("endpoint_sha256")
            if not isinstance(endpoint_sha, str) or not _SHA256.fullmatch(endpoint_sha):
                raise BenchmarkError(f"event {index}: endpoint hash is invalid")
            if provider == "clusy":
                if endpoint != "[CLUSY_ENDPOINT_REDACTED]":
                    raise BenchmarkError(f"event {index}: dynamic Clusy endpoint was retained")
                if shared_clusy_endpoint_sha256 is None:
                    shared_clusy_endpoint_sha256 = endpoint_sha
                elif endpoint_sha != shared_clusy_endpoint_sha256:
                    raise BenchmarkError(
                        f"event {index}: Clusy endpoint hash differs within the run"
                    )
            elif provider == "exa":
                if endpoint != EXA_ENDPOINT or endpoint_sha != sha256_bytes(EXA_ENDPOINT.encode()):
                    raise BenchmarkError(f"event {index}: Exa endpoint evidence is invalid")
            elif provider == "firecrawl" and (
                endpoint != FIRECRAWL_ENDPOINT
                or endpoint_sha != sha256_bytes(FIRECRAWL_ENDPOINT.encode())
            ):
                raise BenchmarkError(f"event {index}: Firecrawl endpoint evidence is invalid")
            expected_cache_source = {
                "clusy": "clusy_response_contract",
                "exa": "provider_status_source",
                "firecrawl": "undocumented_response_field",
            }[cast("Provider", provider)]
            expected_cache_state = "miss" if manifest.mode == "cold_live" else "hit"
            if (
                event.get("cache_evidence_source") != expected_cache_source
                or event.get("cache_evidence_matches_mode")
                is not (event.get("cache_state") == expected_cache_state)
                or (
                    event.get("status") != "ok"
                    and (
                        event.get("cache_state") != "unknown"
                        or event.get("cache_evidence_matches_mode") is not False
                    )
                )
            ):
                raise BenchmarkError(f"event {index}: cache evidence is incoherent")
            if provider == "clusy":
                if event.get("status") == "ok" and (
                    event.get("cache_state") not in {"hit", "miss"}
                    or event.get("cache_hit") is not (event.get("cache_state") == "hit")
                ):
                    raise BenchmarkError(
                        f"event {index}: Clusy cache_hit must mechanically encode cache_state"
                    )
            elif event.get("cache_hit") is not None:
                raise BenchmarkError(f"event {index}: {provider} cache_hit must be null in v3")
            if provider == "firecrawl":
                if (
                    event.get("cache_evidence_source") != "undocumented_response_field"
                    or event.get("cache_evidence_contractually_documented") is not False
                    or event.get("credit_evidence_contractually_documented") is not False
                    or event.get("credit_evidence_source")
                    not in {
                        "undocumented_response_field",
                        "sealed_task_cap_fallback",
                    }
                ):
                    raise BenchmarkError(
                        f"event {index}: Firecrawl diagnostic fields were "
                        "misrepresented as contractual evidence"
                    )
                firecrawl_credits = event.get("credits")
                expected_credit_source = (
                    "undocumented_response_field"
                    if firecrawl_credits is not None
                    else "sealed_task_cap_fallback"
                )
                if (
                    event.get("credit_evidence_source") != expected_credit_source
                    or event.get("scheduled_firecrawl_credit_cap") != task.firecrawl_credit_cap
                    or event.get("provider_reported_cost") is not None
                    or event.get("provider_reported_cost_currency") is not None
                ):
                    raise BenchmarkError(f"event {index}: Firecrawl cost evidence is incoherent")
            elif (
                event.get("cache_evidence_contractually_documented") is not True
                or event.get("credit_evidence_contractually_documented") is not True
            ):
                raise BenchmarkError(f"event {index}: contractual evidence flags are invalid")
            if provider == "exa":
                if (
                    event.get("credit_evidence_source") != "provider_response"
                    or event.get("credits") is not None
                    or event.get("fetch_age") is not None
                    or event.get("scheduled_firecrawl_credit_cap") is not None
                    or (
                        event.get("provider_reported_cost") is None
                        and event.get("provider_reported_cost_currency") is not None
                    )
                    or (
                        event.get("provider_reported_cost") is not None
                        and event.get("provider_reported_cost_currency") != "USD"
                    )
                    or event.get("origin_status_code") is not None
                ):
                    raise BenchmarkError(f"event {index}: Exa cost/status evidence is incoherent")
            elif provider == "clusy" and (
                event.get("credit_evidence_source") != "sealed_manifest_per_request"
                or _as_optional_number(event.get("credits")) != 0
                or event.get("scheduled_firecrawl_credit_cap") is not None
                or _as_optional_number(event.get("provider_reported_cost")) != 0
                or event.get("provider_reported_cost_currency") != "USD"
                or event.get("fetch_age") is not None
            ):
                raise BenchmarkError(f"event {index}: Clusy cost evidence is incoherent")
            elif provider == "firecrawl" and event.get("fetch_age") is not None:
                raise BenchmarkError(f"event {index}: Firecrawl fetch age is not retained")
            if provider != "clusy" and (
                event.get("rendered") is not None
                or event.get("model_used") is not None
                or event.get("quality_attempted") is not None
                or event.get("quality_succeeded") is not None
                or event.get("completeness_score") is not None
                or event.get("stage_timings_ms") != {}
            ):
                raise BenchmarkError(f"event {index}: Clusy-only diagnostics leaked providers")
            if provider != "exa" and event.get("provider_score") is not None:
                raise BenchmarkError(f"event {index}: provider score is unsupported")
            if provider != "firecrawl" and event.get("warning_observed") is not False:
                raise BenchmarkError(f"event {index}: warning evidence is unsupported")
            if (
                event.get("quality_succeeded") is True
                and event.get("quality_attempted") is not True
            ):
                raise BenchmarkError(f"event {index}: quality evidence is incoherent")
            timings = cast("Mapping[str, Any]", event.get("stage_timings_ms"))
            total_timing = _as_optional_number(timings.get("total"))
            component_timings = [
                value
                for key in ("queue", "fetch", "render", "extraction")
                if (value := _as_optional_number(timings.get(key))) is not None
            ]
            if total_timing is not None and (
                sum(component_timings) > total_timing + STAGE_TIMING_TOLERANCE_MS
                or total_timing
                > float(cast("float", event["latency_ms"])) + STAGE_TIMING_TOLERANCE_MS
            ):
                raise BenchmarkError(f"event {index}: stage timings are incoherent")
            if total_timing is None and component_timings:
                raise BenchmarkError(f"event {index}: stage timings lack a total")
            raw_sha = event.get("raw_response_sha256")
            text_sha = event.get("normalized_text_sha256")
            if (
                not isinstance(raw_sha, str)
                or not _SHA256.fullmatch(raw_sha)
                or not isinstance(text_sha, str)
                or not _SHA256.fullmatch(text_sha)
            ):
                raise BenchmarkError(f"event {index}: v3 output hashes are invalid")
            continue
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
    if manifest.schema_version == SCHEMA_VERSION and events:
        if shared_caps is None:
            raise BenchmarkError("v3 events are missing shared execution caps")
        _validate_observed_budget_ledger(
            _recompute_budget_ledger(manifest, events),
            manifest=manifest,
            execution_caps=shared_caps,
            field_name="event journal observed budget ledger",
        )


def _load_canonical_json_artifact(path: Path, *, artifact_name: str) -> JsonObject:
    if path.is_symlink():
        raise BenchmarkError(f"{artifact_name} must not be a symbolic link")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise BenchmarkError(f"cannot read {artifact_name}: {exc}") from exc
    if not data or len(data) > MAX_RUN_ARTIFACT_BYTES:
        raise BenchmarkError(f"{artifact_name} is empty or exceeds the artifact size limit")
    document = _load_json_bytes(data)
    if data != canonical_json_bytes(document) + b"\n":
        raise BenchmarkError(f"{artifact_name} is not canonical JSON with one final newline")
    return document


def _load_canonical_event_journal(path: Path) -> tuple[list[JsonObject], bytes]:
    if path.is_symlink():
        raise BenchmarkError("events journal must not be a symbolic link")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise BenchmarkError(f"cannot read events journal: {exc}") from exc
    if not data or len(data) > MAX_RUN_ARTIFACT_BYTES:
        raise BenchmarkError("events journal is empty or exceeds the artifact size limit")
    events: list[JsonObject] = []
    for line_number, line in enumerate(data.splitlines(keepends=True), start=1):
        if not line.endswith(b"\n") or line == b"\n":
            raise BenchmarkError(
                f"events journal line {line_number} is blank or lacks a final newline"
            )
        event = _load_json_bytes(line)
        if line != canonical_json_bytes(event) + b"\n":
            raise BenchmarkError(f"events journal line {line_number} is not canonical JSON")
        events.append(event)
    return events, data


_PREFLIGHT_KEYS: Final = frozenset(
    {
        "revision",
        "config_fingerprint",
        "image_digest",
        "version_response_sha256",
        "latency_ms",
        "hard_deadline_enforced",
    }
)
_EXECUTION_BUDGET_KEYS: Final = frozenset({"exa_usd", "firecrawl_credits", "clusy_usd"})
_RUN_ARTIFACT_KEYS: Final = frozenset(
    {
        "run_id",
        "manifest_sha256",
        "evaluated_providers",
        "quality_scope_by_provider",
        "quality_scope_comparable_to_full_main_content",
        "claimable",
        "watermark",
        "nonclaimable_reasons",
        "runner_commit",
        "runner_sha256",
        "container_digest",
        "clusy_preflight",
        "execution_budget_caps",
        "created_at",
    }
)
_COMPLETION_ARTIFACT_KEYS: Final = frozenset(
    {
        "run_id",
        "claimable",
        "watermark",
        "manifest_sha256",
        "manifest_artifact_sha256",
        "run_sha256",
        "events_sha256",
        "summary_sha256",
        "completed_at",
    }
)
_SUMMARY_ARTIFACT_KEYS: Final = frozenset(
    {
        "summary_schema_version",
        "manifest_sha256",
        "manifest_schema_version",
        "corpus_sha256",
        "protocol_sha256",
        "mode",
        "cache_max_age_seconds",
        "warm_cache_primed_at",
        "time_window_id",
        "independent_window_index",
        "required_independent_time_windows",
        "bootstrap_samples",
        "evaluated_providers",
        "quality_scope_by_provider",
        "quality_scope_comparable_to_full_main_content",
        "compliance_acknowledgments",
        "events_sha256",
        "claimable",
        "claimable_scope",
        "artifact_integrity_claimable",
        "vendor_win_claimable",
        "vendor_win_watermark",
        "vendor_win_gate",
        "watermark",
        "event_count",
        "provider_output_retention_policy",
        "raw_provider_outputs_retained",
        "quality_metrics_independently_verifiable",
        "execution_attestation_verified",
        "first_attempt_only",
        "complete_provider_task_matrix",
        "duplicate_first_attempt",
        "event_manifest_matches",
        "evidence_fields_valid",
        "events_digest_valid",
        "provider_summaries",
        "pairwise",
        "interpretation",
        "observed_budget_ledger",
    }
)
_SUMMARY_GATE_KEYS: Final = frozenset(
    {
        "passed",
        "observed_independent_time_windows",
        "required_independent_time_windows",
        "structural_fidelity_metrics_complete",
        "table_tree_metric_complete",
        "cache_evidence_complete",
        "contractual_cache_and_credit_evidence_complete",
        "quality_metrics_independently_verifiable",
        "execution_attestation_verified",
        "cold_and_warm_tail_latency_complete",
        "reason",
    }
)
_EVENT_REQUIRED_STRING_FIELDS: Final = frozenset(
    {
        "event_schema_version",
        "run_id",
        "task_id",
        "stratum",
        "structure_strata_basis",
        "language",
        "domain_cluster",
        "content_type",
        "render_class",
        "provider",
        "endpoint",
        "endpoint_sha256",
        "mode",
        "time_window_id",
        "plan",
        "api_version",
        "sdk_version",
        "runner_commit",
        "runner_sha256",
        "container_digest",
        "manifest_sha256",
        "runner_region",
        "query_sha256",
        "quality_scope",
        "randomization_algorithm",
        "started_at",
        "completed_at",
        "status",
        "cache_state",
        "cache_evidence_source",
        "credit_evidence_source",
        "normalized_cost_currency",
        "normalized_cost_source",
        "raw_response_sha256",
        "canonical_url",
        "normalized_text_sha256",
        "token_count_method",
        "reference_sha256",
        "reference_method",
    }
)
_EVENT_OPTIONAL_STRING_FIELDS: Final = frozenset(
    {
        "first_byte_at",
        "provider_request_id_sha256",
        "provider_reported_cost_currency",
    }
)
_EVENT_REQUIRED_BOOLEAN_FIELDS: Final = frozenset(
    {
        "claimable",
        "quality_scope_comparable_to_full_main_content",
        "hard_deadline_enforced",
        "cache_evidence_matches_mode",
        "cache_evidence_contractually_documented",
        "credit_evidence_contractually_documented",
        "raw_response_observed",
        "raw_response_complete",
        "benchmark_output_cap_applied",
        "warning_observed",
    }
)
_EVENT_OPTIONAL_BOOLEAN_FIELDS: Final = frozenset(
    {
        "cache_hit",
        "rendered",
        "model_used",
        "quality_attempted",
        "quality_succeeded",
        "truncated",
    }
)
_EVENT_REQUIRED_INTEGER_FIELDS: Final = frozenset(
    {
        "independent_window_index",
        "required_independent_windows",
        "max_age",
        "attempt",
        "order_position",
        "randomization_seed",
        "raw_response_bytes",
        "character_count",
        "benchmark_output_cap_characters",
        "token_count",
    }
)
_EVENT_OPTIONAL_INTEGER_FIELDS: Final = frozenset({"http_status", "origin_status_code"})
_EVENT_REQUIRED_NUMBER_FIELDS: Final = frozenset({"timeout", "latency_ms", "normalized_cost"})
_EVENT_OPTIONAL_NUMBER_FIELDS: Final = frozenset(
    {
        "first_byte_latency_ms",
        "fetch_age",
        "credits",
        "scheduled_firecrawl_credit_cap",
        "provider_reported_cost",
        "completeness_score",
        "provider_score",
    }
)
_EVENT_STRING_LIST_FIELDS: Final = frozenset(
    {
        "structure_strata",
        "randomized_order",
    }
)
_EVENT_NESTED_FIELDS: Final = frozenset(
    {"clusy_preflight", "execution_budget_caps", "stage_timings_ms", "scoring"}
)
_EVENT_ARTIFACT_KEYS: Final = frozenset(
    _EVENT_REQUIRED_STRING_FIELDS
    | _EVENT_OPTIONAL_STRING_FIELDS
    | _EVENT_REQUIRED_BOOLEAN_FIELDS
    | _EVENT_OPTIONAL_BOOLEAN_FIELDS
    | _EVENT_REQUIRED_INTEGER_FIELDS
    | _EVENT_OPTIONAL_INTEGER_FIELDS
    | _EVENT_REQUIRED_NUMBER_FIELDS
    | _EVENT_OPTIONAL_NUMBER_FIELDS
    | _EVENT_STRING_LIST_FIELDS
    | _EVENT_NESTED_FIELDS
)
_SCORING_KEYS: Final = frozenset(
    {
        "token_precision",
        "token_recall",
        "token_f1",
        "candidate_tokens",
        "reference_tokens",
        "tokenizer",
        "heading_f1",
        "list_f1",
        "code_f1",
        "table_tree_similarity",
        "structure_score",
        "structure_metric",
        "reference_component_count",
        "observed_headings",
        "observed_list_items",
        "observed_code_blocks",
        "observed_tables",
        "observed_table_tree_tokens",
        "reference_table_tree_tokens",
    }
)


def _require_exact_keys(
    value: Any,
    expected: frozenset[str],
    *,
    field_name: str,
) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise BenchmarkError(f"{field_name} must be an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise BenchmarkError(
            f"{field_name} fields do not match the v3 schema (missing={missing}, unknown={unknown})"
        )
    return value


def _is_finite_number(value: Any, *, minimum: float | None = None) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    number = float(value)
    return (
        math.isfinite(number)
        and (minimum is None or number >= minimum)
        and not (number == 0 and math.copysign(1.0, number) < 0)
    )


def _is_canonical_float(value: Any, *, minimum: float | None = None) -> bool:
    return type(value) is float and _is_finite_number(value, minimum=minimum)


def _is_integer(value: Any, *, minimum: int | None = None) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and (minimum is None or value >= minimum)
    )


def _is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _validate_preflight_artifact(value: Any, *, field_name: str) -> None:
    preflight = _require_exact_keys(value, _PREFLIGHT_KEYS, field_name=field_name)
    if (
        not isinstance(preflight.get("revision"), str)
        or not _REVISION.fullmatch(cast("str", preflight["revision"]))
        or not isinstance(preflight.get("config_fingerprint"), str)
        or not _SHA256.fullmatch(cast("str", preflight["config_fingerprint"]))
        or not isinstance(preflight.get("image_digest"), str)
        or not _CONTAINER_DIGEST.fullmatch(cast("str", preflight["image_digest"]))
        or not isinstance(preflight.get("version_response_sha256"), str)
        or not _SHA256.fullmatch(cast("str", preflight["version_response_sha256"]))
        or not _is_canonical_float(preflight.get("latency_ms"), minimum=0)
        or float(preflight["latency_ms"]) > 10_000 + LATENCY_TIMESTAMP_TOLERANCE_MS
        or preflight.get("hard_deadline_enforced") is not True
    ):
        raise BenchmarkError(f"{field_name} has invalid v3 field types or values")


def _validate_execution_budget_artifact(value: Any, *, field_name: str) -> None:
    budgets = _require_exact_keys(
        value,
        _EXECUTION_BUDGET_KEYS,
        field_name=field_name,
    )
    if any(not _is_canonical_float(budgets.get(key), minimum=0) for key in budgets):
        raise BenchmarkError(f"{field_name} values must be finite non-negative numbers")


def _validate_execution_caps_against_manifest(
    value: Mapping[str, Any],
    *,
    manifest: Manifest,
    field_name: str,
) -> None:
    budgets = manifest.budgets
    if budgets is None or any(
        float(value[key]) > getattr(budgets, key) + 1e-12 for key in _EXECUTION_BUDGET_KEYS
    ):
        raise BenchmarkError(f"{field_name} exceeds the sealed manifest")
    minimum_caps = {
        "exa_usd": (
            len(manifest.tasks) * manifest.pricing["exa"].per_request
            if "exa" in manifest.providers
            else 0.0
        ),
        "firecrawl_credits": (
            sum(task.firecrawl_credit_cap for task in manifest.tasks)
            if "firecrawl" in manifest.providers
            else 0.0
        ),
        "clusy_usd": len(manifest.tasks) * manifest.pricing["clusy"].per_request,
    }
    if any(float(value[key]) + 1e-12 < minimum for key, minimum in minimum_caps.items()):
        raise BenchmarkError(f"{field_name} is below frozen request estimates")
    provider_by_budget = {
        "exa_usd": "exa",
        "firecrawl_credits": "firecrawl",
        "clusy_usd": "clusy",
    }
    if any(
        provider_by_budget[key] not in manifest.providers and float(value[key]) != 0
        for key in _EXECUTION_BUDGET_KEYS
    ):
        raise BenchmarkError(f"{field_name} contains a cap for an unselected provider")


def _validate_claim_coherence(document: Mapping[str, Any], *, field_name: str) -> None:
    claimable = document.get("claimable")
    watermark = document.get("watermark")
    reasons = document.get("nonclaimable_reasons")
    if (
        not isinstance(claimable, bool)
        or not isinstance(watermark, str)
        or not _is_string_list(reasons)
    ):
        raise BenchmarkError(f"{field_name} claim fields have invalid types")
    reason_list = cast("list[str]", reasons)
    canonical_reasons = [reason for reason in _CLAIM_REASON_ORDER if reason in reason_list]
    if reason_list != canonical_reasons:
        raise BenchmarkError(
            f"{field_name} nonclaimable reasons are unknown, duplicate, or out of order"
        )
    if claimable is not (not reason_list):
        raise BenchmarkError(f"{field_name} claimable flag does not match its reasons")
    if (claimable and watermark != "") or (
        not claimable and (watermark != "NONCLAIMABLE" or not reason_list)
    ):
        raise BenchmarkError(f"{field_name} watermark does not match claimability")
    runner_commit = document.get("runner_commit")
    container_digest = document.get("container_digest")
    if runner_commit != "unknown" and (
        not isinstance(runner_commit, str) or not re.fullmatch(r"[0-9a-f]{40,64}", runner_commit)
    ):
        raise BenchmarkError(f"{field_name} runner_commit is not canonical")
    if container_digest != "unknown" and (
        not isinstance(container_digest, str) or not _CONTAINER_DIGEST.fullmatch(container_digest)
    ):
        raise BenchmarkError(f"{field_name} container_digest is not canonical")
    if claimable and (runner_commit == "unknown" or container_digest == "unknown"):
        raise BenchmarkError(f"{field_name} claimable provenance cannot be unknown")
    operator_reason = _CLAIM_REASON_ORDER[0]
    runner_reason = _CLAIM_REASON_ORDER[2]
    container_reason = _CLAIM_REASON_ORDER[3]
    if not claimable and operator_reason not in reason_list:
        raise BenchmarkError(f"{field_name} nonclaimable mode lacks its canonical operator reason")
    if (runner_commit == "unknown") is not (runner_reason in reason_list):
        raise BenchmarkError(
            f"{field_name} runner reason does not match the observed provenance condition"
        )
    if (container_digest == "unknown") is not (container_reason in reason_list):
        raise BenchmarkError(
            f"{field_name} container reason does not match the observed digest condition"
        )


def _validate_run_id(
    run_id: Any,
    *,
    manifest: Manifest,
    created_at: str,
) -> None:
    if not isinstance(run_id, str) or not _SAFE_ID.fullmatch(run_id):
        raise BenchmarkError("run metadata run_id is invalid")
    pattern = re.compile(
        rf"^{re.escape(manifest.benchmark_id)}-"
        rf"(?P<stamp>\d{{8}}T\d{{6}}Z)-{manifest.digest[:12]}$"
    )
    match = pattern.fullmatch(run_id)
    if match is None:
        raise BenchmarkError("run metadata run_id is not bound to benchmark/timestamp/manifest")
    created = _parse_v3_utc_timestamp(created_at, field_name="run.created_at")
    if created.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ") != match.group("stamp"):
        raise BenchmarkError("run metadata run_id timestamp does not match run.created_at")


def _validate_run_artifact(
    document: JsonObject,
    *,
    manifest: Manifest,
) -> None:
    run = _require_exact_keys(document, _RUN_ARTIFACT_KEYS, field_name="run metadata")
    if (
        not isinstance(run.get("manifest_sha256"), str)
        or not _SHA256.fullmatch(cast("str", run["manifest_sha256"]))
        or not _is_string_list(run.get("evaluated_providers"))
        or not isinstance(run.get("claimable"), bool)
        or not isinstance(run.get("watermark"), str)
        or not _is_string_list(run.get("nonclaimable_reasons"))
        or not isinstance(run.get("runner_commit"), str)
        or not isinstance(run.get("runner_sha256"), str)
        or not _SHA256.fullmatch(cast("str", run["runner_sha256"]))
        or not isinstance(run.get("container_digest"), str)
        or not isinstance(run.get("created_at"), str)
    ):
        raise BenchmarkError("run metadata has invalid v3 field types")
    created_at = cast("str", run["created_at"])
    _validate_run_id(run.get("run_id"), manifest=manifest, created_at=created_at)
    if _parse_v3_utc_timestamp(
        created_at,
        field_name="run.created_at",
    ) < _parse_v3_utc_timestamp(
        manifest.created_at,
        field_name="manifest.created_at",
    ):
        raise BenchmarkError("run metadata predates the sealed manifest")
    _validate_claim_coherence(run, field_name="run metadata")
    if run.get("manifest_sha256") != manifest.digest:
        raise BenchmarkError("run metadata manifest digest mismatch")
    if run.get("evaluated_providers") != list(manifest.providers):
        raise BenchmarkError("run metadata provider scope mismatch")
    quality_scopes = _require_exact_keys(
        run.get("quality_scope_by_provider"),
        frozenset(manifest.providers),
        field_name="run metadata quality_scope_by_provider",
    )
    quality_comparability = _require_exact_keys(
        run.get("quality_scope_comparable_to_full_main_content"),
        frozenset(manifest.providers),
        field_name="run metadata quality_scope_comparable_to_full_main_content",
    )
    if any(not isinstance(value, str) for value in quality_scopes.values()) or any(
        not isinstance(value, bool) for value in quality_comparability.values()
    ):
        raise BenchmarkError("run metadata quality scope maps have invalid value types")
    expected_quality_scopes = {
        provider: (
            "provider_default_compact_main_content" if provider == "exa" else "full_main_content"
        )
        for provider in manifest.providers
    }
    if quality_scopes != expected_quality_scopes or quality_comparability != {
        provider: provider != "exa" for provider in manifest.providers
    }:
        raise BenchmarkError("run metadata quality scope maps do not match the protocol")
    _validate_preflight_artifact(
        run.get("clusy_preflight"),
        field_name="run metadata clusy_preflight",
    )
    _validate_execution_budget_artifact(
        run.get("execution_budget_caps"),
        field_name="run metadata execution_budget_caps",
    )
    binding = manifest.clusy_binding
    preflight = cast("Mapping[str, Any]", run["clusy_preflight"])
    if (
        binding is None
        or preflight.get("revision") != binding.expected_revision
        or preflight.get("config_fingerprint") != binding.expected_config_sha256
        or preflight.get("image_digest") != binding.expected_image_digest
    ):
        raise BenchmarkError("run metadata Clusy preflight does not match the manifest")
    _validate_execution_caps_against_manifest(
        cast("Mapping[str, Any]", run["execution_budget_caps"]),
        manifest=manifest,
        field_name="run metadata execution caps",
    )


def _validate_completion_artifact(
    document: JsonObject,
    *,
    manifest: Manifest,
    run_metadata: Mapping[str, Any],
    summary: Mapping[str, Any],
) -> None:
    completion = _require_exact_keys(
        document,
        _COMPLETION_ARTIFACT_KEYS,
        field_name="completion record",
    )
    if (
        not isinstance(completion.get("run_id"), str)
        or not _SAFE_ID.fullmatch(cast("str", completion["run_id"]))
        or not isinstance(completion.get("claimable"), bool)
        or not isinstance(completion.get("watermark"), str)
        or not isinstance(completion.get("completed_at"), str)
    ):
        raise BenchmarkError("completion record has invalid v3 field types")
    for key in (
        "manifest_sha256",
        "manifest_artifact_sha256",
        "run_sha256",
        "events_sha256",
        "summary_sha256",
    ):
        value = completion.get(key)
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise BenchmarkError(f"completion record {key} is not a SHA-256 digest")
    _parse_v3_utc_timestamp(
        cast("str", completion["completed_at"]),
        field_name="completion.completed_at",
    )
    if (
        completion.get("run_id") != run_metadata.get("run_id")
        or completion.get("claimable") != run_metadata.get("claimable")
        or completion.get("claimable") != summary.get("claimable")
        or completion.get("watermark") != run_metadata.get("watermark")
        or completion.get("watermark") != summary.get("watermark")
        or completion.get("manifest_sha256") != manifest.digest
    ):
        raise BenchmarkError("completion record claim/protocol values do not cohere")
    completed = _parse_v3_utc_timestamp(
        cast("str", completion["completed_at"]),
        field_name="completion.completed_at",
    )
    run_created = _parse_v3_utc_timestamp(
        cast("str", run_metadata["created_at"]),
        field_name="run.created_at",
    )
    if completed < run_created:
        raise BenchmarkError("completion record predates run creation")


def _validate_discrete_overlap_feasibility(
    measurements: Sequence[tuple[str, float, int, float]],
    *,
    max_overlap: int,
    field_name: str,
) -> int:
    """Require every ratio to arise from one bounded integer overlap."""
    recovered: set[int] = set()
    for measurement_name, score, denominator, numerator_factor in measurements:
        if denominator == 0:
            overlap = 0
            expected = 0.0
        else:
            overlap = round(score * denominator / numerator_factor)
            expected = numerator_factor * overlap / denominator
        if (
            overlap < 0
            or overlap > max_overlap
            or not math.isclose(
                score,
                expected,
                rel_tol=0.0,
                abs_tol=SCORE_FLOAT_TOLERANCE,
            )
        ):
            raise BenchmarkError(
                f"{field_name}.{measurement_name} is not feasible from integer counts"
            )
        recovered.add(overlap)
    if len(recovered) != 1:
        raise BenchmarkError(f"{field_name} ratios do not share one integer overlap")
    return next(iter(recovered))


def _validate_scoring_artifact(value: Any, *, event_index: int) -> None:
    scoring = _require_exact_keys(
        value,
        _SCORING_KEYS,
        field_name=f"event {event_index}.scoring",
    )
    for key in ("token_precision", "token_recall", "token_f1"):
        if not _is_canonical_float(scoring.get(key), minimum=0) or float(scoring[key]) > 1:
            raise BenchmarkError(f"event {event_index}.scoring.{key} is invalid")
    for key in (
        "heading_f1",
        "list_f1",
        "code_f1",
        "table_tree_similarity",
        "structure_score",
    ):
        item = scoring.get(key)
        if item is not None and (not _is_canonical_float(item, minimum=0) or float(item) > 1):
            raise BenchmarkError(f"event {event_index}.scoring.{key} is invalid")
    for key in (
        "candidate_tokens",
        "reference_tokens",
        "reference_component_count",
        "observed_headings",
        "observed_list_items",
        "observed_code_blocks",
        "observed_tables",
        "observed_table_tree_tokens",
        "reference_table_tree_tokens",
    ):
        if not _is_integer(scoring.get(key), minimum=0):
            raise BenchmarkError(f"event {event_index}.scoring.{key} is invalid")
    if (
        cast("int", scoring["reference_component_count"]) > len(_STRUCTURE_STRATA)
        or cast("int", scoring["candidate_tokens"]) > 10_000
        or cast("int", scoring["reference_tokens"]) > 10_000_000
        or any(
            cast("int", scoring[key]) > 10_000
            for key in (
                "observed_headings",
                "observed_list_items",
                "observed_code_blocks",
                "observed_tables",
            )
        )
        or cast("int", scoring["observed_table_tree_tokens"]) > 20_000
        or cast("int", scoring["reference_table_tree_tokens"]) > 20_000
    ):
        raise BenchmarkError(f"event {event_index}.scoring counters exceed protocol bounds")
    if (
        scoring.get("tokenizer") != "clusy-unicode-tokenizer.v1"
        or scoring.get("structure_metric") != "clusy-markdown-structure.v1"
    ):
        raise BenchmarkError(f"event {event_index}.scoring metric identifiers are invalid")
    precision = float(scoring["token_precision"])
    recall = float(scoring["token_recall"])
    candidate_tokens = cast("int", scoring["candidate_tokens"])
    reference_tokens = cast("int", scoring["reference_tokens"])
    _validate_discrete_overlap_feasibility(
        (
            ("token_precision", precision, candidate_tokens, 1.0),
            ("token_recall", recall, reference_tokens, 1.0),
            (
                "token_f1",
                float(scoring["token_f1"]),
                candidate_tokens + reference_tokens,
                2.0,
            ),
        ),
        max_overlap=min(candidate_tokens, reference_tokens),
        field_name=f"event {event_index}.scoring",
    )
    observed_tables = cast("int", scoring["observed_tables"])
    observed_table_tokens = cast("int", scoring["observed_table_tree_tokens"])
    reference_table_tokens = cast("int", scoring["reference_table_tree_tokens"])
    if (observed_tables == 0) is not (observed_table_tokens == 0) or (
        observed_tables > 0 and observed_table_tokens < min(4 * observed_tables, 20_000)
    ):
        raise BenchmarkError(
            f"event {event_index}.scoring table counts contradict tree-token counts"
        )
    table_score = scoring.get("table_tree_similarity")
    if (table_score is None) is not (reference_table_tokens == 0):
        raise BenchmarkError(
            f"event {event_index}.scoring table score contradicts reference tree tokens"
        )
    if table_score is not None:
        _validate_discrete_overlap_feasibility(
            (
                (
                    "table_tree_similarity",
                    float(table_score),
                    observed_table_tokens + reference_table_tokens,
                    2.0,
                ),
            ),
            max_overlap=min(observed_table_tokens, reference_table_tokens),
            field_name=f"event {event_index}.scoring",
        )
    for score_key, count_key in (
        ("heading_f1", "observed_headings"),
        ("list_f1", "observed_list_items"),
        ("code_f1", "observed_code_blocks"),
        ("table_tree_similarity", "observed_tables"),
    ):
        score = scoring.get(score_key)
        if score is not None and float(score) > 0 and scoring.get(count_key) == 0:
            raise BenchmarkError(
                f"event {event_index}.scoring.{score_key} requires observed structure"
            )
    component_values = [
        float(value)
        for key in (
            "heading_f1",
            "list_f1",
            "code_f1",
            "table_tree_similarity",
        )
        if (value := scoring.get(key)) is not None
    ]
    expected_structure = sum(component_values) / len(component_values) if component_values else None
    if scoring.get("reference_component_count") != len(component_values) or (
        expected_structure is None
        and scoring.get("structure_score") is not None
        or expected_structure is not None
        and (
            scoring.get("structure_score") is None
            or not math.isclose(
                float(scoring["structure_score"]),
                expected_structure,
                rel_tol=0.0,
                abs_tol=SCORE_FLOAT_TOLERANCE,
            )
        )
    ):
        raise BenchmarkError(f"event {event_index}.scoring structure metrics are incoherent")


def _validate_event_artifact(event: Mapping[str, Any], *, event_index: int) -> None:
    event_object = _require_exact_keys(
        event,
        _EVENT_ARTIFACT_KEYS,
        field_name=f"event {event_index}",
    )
    for key in _EVENT_REQUIRED_STRING_FIELDS:
        value = event_object.get(key)
        if not isinstance(value, str) or len(value) > 512:
            raise BenchmarkError(f"event {event_index}.{key} must be a bounded string")
    for key in _EVENT_OPTIONAL_STRING_FIELDS:
        if event_object.get(key) is not None and not isinstance(event_object.get(key), str):
            raise BenchmarkError(f"event {event_index}.{key} must be null or a string")
    for key in _EVENT_REQUIRED_BOOLEAN_FIELDS:
        if not isinstance(event_object.get(key), bool):
            raise BenchmarkError(f"event {event_index}.{key} must be a boolean")
    for key in _EVENT_OPTIONAL_BOOLEAN_FIELDS:
        if event_object.get(key) is not None and not isinstance(event_object.get(key), bool):
            raise BenchmarkError(f"event {event_index}.{key} must be null or a boolean")
    for key in _EVENT_REQUIRED_INTEGER_FIELDS:
        if not _is_integer(event_object.get(key), minimum=0):
            raise BenchmarkError(f"event {event_index}.{key} must be a non-negative integer")
    for key in _EVENT_OPTIONAL_INTEGER_FIELDS:
        if event_object.get(key) is not None and not _is_integer(
            event_object.get(key),
            minimum=0,
        ):
            raise BenchmarkError(
                f"event {event_index}.{key} must be null or a non-negative integer"
            )
    for key in _EVENT_REQUIRED_NUMBER_FIELDS:
        if not _is_canonical_float(event_object.get(key), minimum=0):
            raise BenchmarkError(
                f"event {event_index}.{key} must be a canonical non-negative float"
            )
    for key in _EVENT_OPTIONAL_NUMBER_FIELDS:
        if event_object.get(key) is not None and not _is_canonical_float(
            event_object.get(key),
            minimum=0,
        ):
            raise BenchmarkError(
                f"event {event_index}.{key} must be null or a canonical non-negative float"
            )
    for key in _EVENT_STRING_LIST_FIELDS:
        value = event_object.get(key)
        if (
            not _is_string_list(value)
            or len(cast("list[str]", value)) > len(PROVIDERS) + len(_STRUCTURE_STRATA)
            or any(len(item) > 128 for item in cast("list[str]", value))
        ):
            raise BenchmarkError(f"event {event_index}.{key} must be a bounded string array")
    if (
        event_object.get("event_schema_version") != EVENT_SCHEMA_VERSION
        or event_object.get("structure_strata_basis") != "reference_component_presence.v1"
        or event_object.get("provider") not in PROVIDERS
        or event_object.get("mode") not in {"cold_live", "warm_cache"}
        or event_object.get("sdk_version") != f"httpx/{httpx.__version__}"
        or event_object.get("randomization_algorithm") != "sha256-sort.v1"
        or event_object.get("status") not in _STATUS_VALUES
        or event_object.get("cache_state") not in _CACHE_STATE_VALUES
        or event_object.get("hard_deadline_enforced") is not True
        or event_object.get("normalized_cost_source") != "frozen_manifest_per_request"
        or event_object.get("normalized_cost_currency") != "USD"
        or event_object.get("token_count_method") != "clusy-unicode-tokenizer.v1"
    ):
        raise BenchmarkError(f"event {event_index} contains a noncanonical categorical value")
    if (
        not _SAFE_ID.fullmatch(cast("str", event_object["run_id"]))
        or event_object.get("runner_commit") != "unknown"
        and not re.fullmatch(
            r"[0-9a-f]{40,64}",
            cast("str", event_object["runner_commit"]),
        )
        or event_object.get("container_digest") != "unknown"
        and not _CONTAINER_DIGEST.fullmatch(cast("str", event_object["container_digest"]))
    ):
        raise BenchmarkError(f"event {event_index} provenance identifiers are not canonical")
    for key in (
        "endpoint_sha256",
        "runner_sha256",
        "manifest_sha256",
        "query_sha256",
        "raw_response_sha256",
        "normalized_text_sha256",
        "reference_sha256",
    ):
        if not _SHA256.fullmatch(cast("str", event_object[key])):
            raise BenchmarkError(f"event {event_index}.{key} is not a SHA-256 digest")
    request_id_sha = event_object.get("provider_request_id_sha256")
    if request_id_sha is not None and (
        not isinstance(request_id_sha, str) or not _SHA256.fullmatch(request_id_sha)
    ):
        raise BenchmarkError(
            f"event {event_index}.provider_request_id_sha256 is not null or a digest"
        )
    canonical_url = cast("str", event_object["canonical_url"])
    if canonical_url and not _REDACTED_URL.fullmatch(canonical_url):
        raise BenchmarkError(f"event {event_index}.canonical_url is not a redacted URL token")
    if (
        event_object.get("provider_reported_cost") is None
        and event_object.get("provider_reported_cost_currency") is not None
    ) or (
        event_object.get("provider_reported_cost") is not None
        and event_object.get("provider_reported_cost_currency") != "USD"
    ):
        raise BenchmarkError(f"event {event_index} provider-reported cost currency is invalid")
    if (
        event_object.get("attempt") != 1
        or not (1 <= cast("int", event_object["independent_window_index"]) <= 20)
        or not (2 <= cast("int", event_object["required_independent_windows"]) <= 20)
        or cast("int", event_object["independent_window_index"])
        > cast("int", event_object["required_independent_windows"])
        or not (0 <= cast("int", event_object["max_age"]) <= 604_800)
        or not (0 <= cast("int", event_object["order_position"]) < len(PROVIDERS))
        or not (0 <= cast("int", event_object["randomization_seed"]) < 2**63)
        or cast("int", event_object["raw_response_bytes"]) > MAX_RESPONSE_BYTES
        or not (1 <= cast("int", event_object["benchmark_output_cap_characters"]) <= 10_000)
        or cast("int", event_object["character_count"])
        > cast("int", event_object["benchmark_output_cap_characters"])
        or cast("int", event_object["token_count"]) > cast("int", event_object["character_count"])
    ):
        raise BenchmarkError(f"event {event_index} integer evidence is outside protocol bounds")
    timeout = float(event_object["timeout"])
    latency = float(event_object["latency_ms"])
    if timeout != 60 or latency > timeout * 1000 + LATENCY_TIMESTAMP_TOLERANCE_MS:
        raise BenchmarkError(f"event {event_index} timeout/latency evidence is invalid")
    first_byte_latency = event_object.get("first_byte_latency_ms")
    first_byte_at = event_object.get("first_byte_at")
    if (first_byte_latency is None) is not (first_byte_at is None) or (
        first_byte_latency is not None
        and float(first_byte_latency) > latency + LATENCY_TIMESTAMP_TOLERANCE_MS
    ):
        raise BenchmarkError(f"event {event_index} first-byte evidence is incoherent")
    for key in ("completeness_score", "provider_score"):
        value = event_object.get(key)
        if value is not None and float(value) > 1:
            raise BenchmarkError(f"event {event_index}.{key} must be in [0, 1]")
    if event_object.get("benchmark_output_cap_applied") is True and (
        event_object.get("character_count") != event_object.get("benchmark_output_cap_characters")
        or event_object.get("truncated") is not True
    ):
        raise BenchmarkError(f"event {event_index} output-cap evidence is incoherent")
    if event_object.get("raw_response_observed") is not (
        event_object.get("http_status") is not None
        or cast("int", event_object["raw_response_bytes"]) > 0
    ):
        raise BenchmarkError(f"event {event_index} raw-response observation flag is incoherent")
    if event_object.get("raw_response_complete") is not (
        event_object.get("status") != "transport_error"
    ):
        raise BenchmarkError(f"event {event_index} raw-response completion flag is incoherent")
    raw_bytes = cast("int", event_object["raw_response_bytes"])
    raw_sha = cast("str", event_object["raw_response_sha256"])
    empty_sha = sha256_bytes(b"")
    if (raw_bytes > 0) is not (first_byte_at is not None):
        raise BenchmarkError(f"event {event_index} raw body and first-byte evidence are incoherent")
    if (raw_bytes == 0) is not (raw_sha == empty_sha):
        raise BenchmarkError(f"event {event_index} raw body length/hash evidence is incoherent")
    character_count = cast("int", event_object["character_count"])
    text_sha = cast("str", event_object["normalized_text_sha256"])
    if (character_count == 0) is not (text_sha == empty_sha):
        raise BenchmarkError(
            f"event {event_index} normalized text length/hash evidence is incoherent"
        )
    for key in ("http_status", "origin_status_code"):
        value = event_object.get(key)
        if value is not None and not (100 <= cast("int", value) <= 599):
            raise BenchmarkError(f"event {event_index}.{key} is outside the HTTP status range")
    status = cast("str", event_object["status"])
    http_status = event_object.get("http_status")
    if status == "ok" and (
        http_status is None
        or not (200 <= cast("int", http_status) < 300)
        or raw_bytes == 0
        or character_count == 0
        or (
            event_object.get("origin_status_code") is not None
            and not (200 <= cast("int", event_object["origin_status_code"]) < 400)
        )
    ):
        raise BenchmarkError(f"event {event_index} successful output lacks a 2xx body/text")
    if status in {"malformed_response", "provider_error", "empty_output"} and (
        http_status is None or not (200 <= cast("int", http_status) < 300)
    ):
        raise BenchmarkError(
            f"event {event_index} normalized failure status contradicts HTTP evidence"
        )
    if status == "http_error" and (http_status is None or 200 <= cast("int", http_status) < 300):
        raise BenchmarkError(f"event {event_index} http_error requires a non-2xx HTTP status")
    if http_status is None and status != "transport_error":
        raise BenchmarkError(f"event {event_index} null HTTP status is transport-error-only")
    cache_hit = event_object.get("cache_hit")
    cache_state = event_object.get("cache_state")
    if cache_hit is not None and (
        cache_state == "unknown" or cache_hit is not (cache_state == "hit")
    ):
        raise BenchmarkError(f"event {event_index} cache_hit contradicts cache_state")
    _validate_preflight_artifact(
        event_object.get("clusy_preflight"),
        field_name=f"event {event_index}.clusy_preflight",
    )
    _validate_execution_budget_artifact(
        event_object.get("execution_budget_caps"),
        field_name=f"event {event_index}.execution_budget_caps",
    )
    timings = event_object.get("stage_timings_ms")
    if not isinstance(timings, dict) or set(timings) - set(_STAGE_TIMING_KEYS):
        raise BenchmarkError(f"event {event_index}.stage_timings_ms has unknown fields")
    if any(
        not _is_canonical_float(value, minimum=0)
        or float(value) > timeout * 1000 + LATENCY_TIMESTAMP_TOLERANCE_MS
        for value in timings.values()
    ):
        raise BenchmarkError(f"event {event_index}.stage_timings_ms has invalid values")
    _validate_scoring_artifact(event_object.get("scoring"), event_index=event_index)
    scoring = cast("Mapping[str, Any]", event_object["scoring"])
    if scoring.get("candidate_tokens") != event_object.get("token_count"):
        raise BenchmarkError(f"event {event_index} token counts do not cohere")
    if any(
        cast("int", scoring[key]) > character_count
        for key in (
            "candidate_tokens",
            "observed_headings",
            "observed_list_items",
            "observed_code_blocks",
            "observed_tables",
            "observed_table_tree_tokens",
        )
    ):
        raise BenchmarkError(f"event {event_index} scoring counters exceed output bounds")
    component_fields = {
        "headings": ("heading_f1", "observed_headings"),
        "lists": ("list_f1", "observed_list_items"),
        "code": ("code_f1", "observed_code_blocks"),
        "tables": ("table_tree_similarity", "observed_tables"),
    }
    structure_strata = cast("list[str]", event_object["structure_strata"])
    for stratum, (score_key, _count_key) in component_fields.items():
        if (stratum in structure_strata) is not (scoring.get(score_key) is not None):
            raise BenchmarkError(
                f"event {event_index} scoring components contradict structure_strata"
            )
    if status != "ok" and (
        character_count != 0
        or event_object.get("token_count") != 0
        or text_sha != empty_sha
        or event_object.get("canonical_url") != ""
        or event_object.get("cache_hit") is not None
        or event_object.get("cache_state") != "unknown"
        or event_object.get("fetch_age") is not None
        or event_object.get("origin_status_code") is not None
        or event_object.get("warning_observed") is not False
        or event_object.get("rendered") is not None
        or event_object.get("model_used") is not None
        or event_object.get("quality_attempted") is not None
        or event_object.get("quality_succeeded") is not None
        or event_object.get("completeness_score") is not None
        or event_object.get("stage_timings_ms") != {}
        or event_object.get("truncated") is not None
        or event_object.get("provider_score") is not None
        or event_object.get("benchmark_output_cap_applied") is not False
        or any(
            scoring.get(key) != 0
            for key in (
                "token_precision",
                "token_recall",
                "token_f1",
                "candidate_tokens",
                "observed_headings",
                "observed_list_items",
                "observed_code_blocks",
                "observed_tables",
                "observed_table_tree_tokens",
            )
        )
        or any(
            scoring.get(key) not in {0, None}
            for key in (
                "heading_f1",
                "list_f1",
                "code_f1",
                "table_tree_similarity",
                "structure_score",
            )
        )
    ):
        raise BenchmarkError(f"event {event_index} failed-output evidence is not canonical")
    started = _parse_v3_utc_timestamp(
        cast("str", event_object["started_at"]),
        field_name=f"event {event_index}.started_at",
    )
    completed = _parse_v3_utc_timestamp(
        cast("str", event_object["completed_at"]),
        field_name=f"event {event_index}.completed_at",
    )
    if completed < started:
        raise BenchmarkError(f"event {event_index} completion predates its start")
    timestamp_latency_ms = (completed - started).total_seconds() * 1000
    if abs(timestamp_latency_ms - latency) > LATENCY_TIMESTAMP_TOLERANCE_MS:
        raise BenchmarkError(
            f"event {event_index} latency differs from timestamps by more than "
            f"{LATENCY_TIMESTAMP_TOLERANCE_MS:g} ms"
        )
    if isinstance(first_byte_at, str):
        first_byte = _parse_v3_utc_timestamp(
            first_byte_at,
            field_name=f"event {event_index}.first_byte_at",
        )
        if not (started <= first_byte <= completed):
            raise BenchmarkError(f"event {event_index} first byte is outside request bounds")
        first_byte_timestamp_ms = (first_byte - started).total_seconds() * 1000
        if (
            first_byte_latency is None
            or abs(first_byte_timestamp_ms - float(first_byte_latency))
            > LATENCY_TIMESTAMP_TOLERANCE_MS
        ):
            raise BenchmarkError(f"event {event_index} first-byte latency differs from timestamps")


def _validate_summary_artifact(
    document: JsonObject,
    *,
    manifest: Manifest,
) -> None:
    summary = _require_exact_keys(
        document,
        _SUMMARY_ARTIFACT_KEYS,
        field_name="stored summary",
    )
    if (
        summary.get("summary_schema_version") != SUMMARY_SCHEMA_VERSION
        or summary.get("manifest_schema_version") != SCHEMA_VERSION
        or summary.get("manifest_sha256") != manifest.digest
        or summary.get("corpus_sha256") != manifest.corpus_sha256
        or summary.get("protocol_sha256") != protocol_sha256(manifest)
        or summary.get("mode") != manifest.mode
        or summary.get("cache_max_age_seconds") != manifest.cache_max_age_seconds
        or summary.get("warm_cache_primed_at") != manifest.warm_cache_primed_at
        or summary.get("time_window_id") != manifest.time_window_id
        or summary.get("independent_window_index") != manifest.independent_window_index
        or summary.get("required_independent_time_windows") != manifest.required_independent_windows
        or summary.get("bootstrap_samples") != manifest.bootstrap_samples
        or summary.get("evaluated_providers") != list(manifest.providers)
        or not isinstance(summary.get("provider_summaries"), list)
        or not isinstance(summary.get("pairwise"), list)
        or not isinstance(summary.get("claimable"), bool)
        or summary.get("artifact_integrity_claimable") is not summary.get("claimable")
        or summary.get("watermark") != ("" if summary.get("claimable") is True else "NONCLAIMABLE")
        or summary.get("claimable_scope") != "artifact_integrity_only"
        or summary.get("vendor_win_claimable") is not False
        or summary.get("vendor_win_watermark") != "NO_VENDOR_WIN_CLAIM"
        or summary.get("provider_output_retention_policy") != "hashes_and_derived_metrics_only"
        or summary.get("raw_provider_outputs_retained") is not False
        or summary.get("quality_metrics_independently_verifiable") is not False
        or summary.get("execution_attestation_verified") is not False
        or summary.get("interpretation")
        != (
            "Descriptive paired estimates only. Apply the pre-registered launch gates; "
            "this file does not declare a vendor winner."
        )
        or not isinstance(summary.get("events_sha256"), str)
        or not _SHA256.fullmatch(cast("str", summary["events_sha256"]))
    ):
        raise BenchmarkError("stored summary has invalid v3 field types or protocol values")
    expected_quality_scopes = {
        provider: (
            "provider_default_compact_main_content" if provider == "exa" else "full_main_content"
        )
        for provider in manifest.providers
    }
    expected_comparability = {provider: provider != "exa" for provider in manifest.providers}
    compliance = manifest.compliance_acknowledgments
    expected_compliance = (
        {
            "third_party_data_transfer_authorized": (
                compliance.third_party_data_transfer_authorized
            ),
            "exa_live_authorized": compliance.exa_live_authorized,
            "exa_authorized_purpose": compliance.exa_authorized_purpose,
        }
        if compliance is not None
        else None
    )
    if (
        summary.get("quality_scope_by_provider") != expected_quality_scopes
        or summary.get("quality_scope_comparable_to_full_main_content") != expected_comparability
        or summary.get("compliance_acknowledgments") != expected_compliance
    ):
        raise BenchmarkError("stored summary scope/compliance fields do not match manifest")
    prime = summary.get("warm_cache_primed_at")
    if isinstance(prime, str):
        _parse_v3_utc_timestamp(
            prime,
            field_name="stored summary warm_cache_primed_at",
        )
    gate = _require_exact_keys(
        summary.get("vendor_win_gate"),
        _SUMMARY_GATE_KEYS,
        field_name="stored summary vendor_win_gate",
    )
    if (
        gate.get("passed") is not False
        or gate.get("observed_independent_time_windows") != 1
        or gate.get("required_independent_time_windows") != manifest.required_independent_windows
        or gate.get("quality_metrics_independently_verifiable") is not False
        or gate.get("execution_attestation_verified") is not False
        or gate.get("cold_and_warm_tail_latency_complete") is not False
        or gate.get("reason")
        != (
            "One run cannot establish a vendor win. Aggregate matched cold and "
            "warm v3 runs across every pre-registered independent time window."
        )
    ):
        raise BenchmarkError("stored summary vendor_win_gate is not canonical")
    _validate_execution_budget_artifact(
        summary.get("observed_budget_ledger"),
        field_name="stored summary observed_budget_ledger",
    )


def _summary_bootstrap_samples(
    summary: Mapping[str, Any],
    *,
    manifest: Manifest,
) -> int:
    top_level = summary.get("bootstrap_samples")
    if top_level != manifest.bootstrap_samples:
        raise BenchmarkError(
            "stored summary bootstrap sample count does not match the sealed manifest"
        )
    rows = summary.get("pairwise")
    if not isinstance(rows, list) or not rows:
        raise BenchmarkError("stored summary has no pairwise bootstrap evidence")
    samples = {row.get("bootstrap_samples") for row in rows if isinstance(row, dict)}
    if len(samples) != 1 or any(
        isinstance(value, bool) or not isinstance(value, int) or not (100 <= value <= 1_000_000)
        for value in samples
    ):
        raise BenchmarkError("stored summary bootstrap sample count is invalid or inconsistent")
    selected = cast("int", next(iter(samples)))
    if selected != manifest.bootstrap_samples:
        raise BenchmarkError("pairwise bootstrap sample count does not match the sealed manifest")
    return selected


def _recompute_budget_ledger(
    manifest: Manifest,
    events: Sequence[Mapping[str, Any]],
) -> JsonObject:
    task_by_id = {task.task_id: task for task in manifest.tasks}
    ledger = {
        "exa_usd": 0.0,
        "firecrawl_credits": 0.0,
        "clusy_usd": 0.0,
    }
    for index, event in enumerate(events):
        provider = event.get("provider")
        normalized_cost = _as_optional_number(event.get("normalized_cost"))
        if normalized_cost is None:
            raise BenchmarkError(f"event {index}: normalized cost is unavailable")
        if provider == "exa":
            reported_cost = _as_optional_number(event.get("provider_reported_cost"))
            ledger["exa_usd"] += reported_cost if reported_cost is not None else normalized_cost
        elif provider == "firecrawl":
            task_id = event.get("task_id")
            task = task_by_id.get(task_id) if isinstance(task_id, str) else None
            if task is None:
                raise BenchmarkError(f"event {index}: Firecrawl task is unavailable")
            ledger["firecrawl_credits"] += max(
                task.firecrawl_credit_cap,
                _as_optional_number(event.get("credits")) or 0.0,
            )
        elif provider == "clusy":
            ledger["clusy_usd"] += normalized_cost
        else:
            raise BenchmarkError(f"event {index}: provider is unsupported")
    return ledger


def _validate_observed_budget_ledger(
    ledger: Mapping[str, Any],
    *,
    manifest: Manifest,
    execution_caps: Mapping[str, Any],
    field_name: str,
) -> None:
    _validate_execution_budget_artifact(ledger, field_name=field_name)
    _validate_execution_budget_artifact(
        execution_caps,
        field_name=f"{field_name} execution caps",
    )
    budgets = manifest.budgets
    if budgets is None:
        raise BenchmarkError(f"{field_name} has no sealed manifest budgets")
    for key in _EXECUTION_BUDGET_KEYS:
        observed = float(ledger[key])
        if (
            observed > float(execution_caps[key]) + 1e-12
            or observed > getattr(budgets, key) + 1e-12
        ):
            raise BenchmarkError(f"{field_name} {key} exceeds execution or sealed manifest caps")


def verify_completed_run_directory(run_directory: Path) -> JsonObject:
    """Verify a completed local run and recompute every retained summary field.

    This establishes local artifact-chain consistency only.  Because v3 does
    not retain provider text, it cannot independently establish that the
    derived quality scores faithfully represent the ephemeral responses.
    """
    if run_directory.is_symlink() or not run_directory.is_dir():
        raise BenchmarkError("completed run path must be a real directory")
    expected_artifact_names = {
        "manifest.json",
        "run.json",
        "events.jsonl",
        "summary.json",
        "completion.json",
    }
    try:
        entries = list(run_directory.iterdir())
    except OSError as exc:
        raise BenchmarkError(f"cannot enumerate completed run directory: {exc}") from exc
    actual_names = {entry.name for entry in entries}
    if actual_names != expected_artifact_names or any(
        entry.is_symlink() or not entry.is_file() for entry in entries
    ):
        raise BenchmarkError(
            "completed run must contain exactly the five regular hash-only "
            "artifacts and no extra files, directories, or symbolic links"
        )

    manifest_path = run_directory / "manifest.json"
    run_path = run_directory / "run.json"
    events_path = run_directory / "events.jsonl"
    summary_path = run_directory / "summary.json"
    completion_path = run_directory / "completion.json"
    for path, label in (
        (manifest_path, "manifest artifact"),
        (run_path, "run metadata"),
        (events_path, "events journal"),
        (summary_path, "stored summary"),
        (completion_path, "completion record"),
    ):
        if path.is_symlink() or not path.is_file():
            raise BenchmarkError(f"completed run is missing a regular {label}")

    manifest_document = _load_canonical_json_artifact(
        manifest_path,
        artifact_name="manifest artifact",
    )
    manifest = parse_manifest(manifest_document)
    if manifest.schema_version != SCHEMA_VERSION:
        raise BenchmarkError("aggregate accepts only completed v3 runs")
    run_metadata = _load_canonical_json_artifact(
        run_path,
        artifact_name="run metadata",
    )
    events, events_bytes = _load_canonical_event_journal(events_path)
    stored_summary = _load_canonical_json_artifact(
        summary_path,
        artifact_name="stored summary",
    )
    completion = _load_canonical_json_artifact(
        completion_path,
        artifact_name="completion record",
    )
    _validate_run_artifact(run_metadata, manifest=manifest)
    _validate_summary_artifact(stored_summary, manifest=manifest)
    _validate_completion_artifact(
        completion,
        manifest=manifest,
        run_metadata=run_metadata,
        summary=stored_summary,
    )

    events_sha = sha256_bytes(events_bytes)
    summary_bytes = canonical_json_bytes(stored_summary) + b"\n"
    manifest_bytes = canonical_json_bytes(manifest_document) + b"\n"
    run_bytes = canonical_json_bytes(run_metadata) + b"\n"
    for key, expected in (
        ("manifest_sha256", manifest.digest),
        ("manifest_artifact_sha256", sha256_bytes(manifest_bytes)),
        ("run_sha256", sha256_bytes(run_bytes)),
        ("events_sha256", events_sha),
        ("summary_sha256", sha256_bytes(summary_bytes)),
    ):
        if completion.get(key) != expected:
            raise BenchmarkError(f"completion record {key} does not match its artifact")

    run_id = run_metadata.get("run_id")
    if not isinstance(run_id, str) or not _SAFE_ID.fullmatch(run_id):
        raise BenchmarkError("run metadata run_id is invalid")
    if run_directory.name != run_id:
        raise BenchmarkError("completed run directory name does not match run_id")
    if completion.get("run_id") != run_id:
        raise BenchmarkError("completion record run_id mismatch")
    if run_metadata.get("manifest_sha256") != manifest.digest:
        raise BenchmarkError("run metadata manifest digest mismatch")
    if run_metadata.get("evaluated_providers") != list(manifest.providers):
        raise BenchmarkError("run metadata provider scope mismatch")
    expected_quality_scopes = {
        provider: (
            "provider_default_compact_main_content" if provider == "exa" else "full_main_content"
        )
        for provider in manifest.providers
    }
    if run_metadata.get("quality_scope_by_provider") != expected_quality_scopes or run_metadata.get(
        "quality_scope_comparable_to_full_main_content"
    ) != {provider: provider != "exa" for provider in manifest.providers}:
        raise BenchmarkError("run metadata quality scope disclosure is invalid")
    preflight = run_metadata.get("clusy_preflight")
    binding = manifest.clusy_binding
    if (
        binding is None
        or not isinstance(preflight, dict)
        or preflight.get("revision") != binding.expected_revision
        or preflight.get("config_fingerprint") != binding.expected_config_sha256
        or preflight.get("image_digest") != binding.expected_image_digest
        or preflight.get("hard_deadline_enforced") is not True
    ):
        raise BenchmarkError("run metadata Clusy preflight does not match the manifest")
    source_provenance_valid = (
        isinstance(run_metadata.get("runner_commit"), str)
        and bool(cast("str", run_metadata.get("runner_commit")))
        and isinstance(run_metadata.get("runner_sha256"), str)
        and bool(_SHA256.fullmatch(cast("str", run_metadata.get("runner_sha256"))))
        and isinstance(run_metadata.get("container_digest"), str)
        and bool(cast("str", run_metadata.get("container_digest")))
    )
    if run_metadata.get("claimable") is True:
        source_provenance_valid = (
            source_provenance_valid
            and bool(
                re.fullmatch(
                    r"[0-9a-f]{40,64}",
                    cast("str", run_metadata.get("runner_commit")),
                )
            )
            and bool(_CONTAINER_DIGEST.fullmatch(cast("str", run_metadata.get("container_digest"))))
        )
    if not source_provenance_valid:
        raise BenchmarkError("run metadata source/container provenance is invalid")
    if completion.get("claimable") != stored_summary.get("claimable"):
        raise BenchmarkError("completion and summary claimable flags differ")
    if completion.get("watermark") != stored_summary.get("watermark"):
        raise BenchmarkError("completion and summary watermarks differ")
    completed_at = completion.get("completed_at")
    if not isinstance(completed_at, str):
        raise BenchmarkError("completion timestamp is missing")
    _parse_v3_utc_timestamp(completed_at, field_name="completion.completed_at")

    verify_event_artifacts(
        manifest=manifest,
        events=events,
        run_directory=run_directory,
    )
    run_created_at = _parse_v3_utc_timestamp(
        cast("str", run_metadata["created_at"]),
        field_name="run.created_at",
    )
    completion_at = _parse_v3_utc_timestamp(
        cast("str", completion["completed_at"]),
        field_name="completion.completed_at",
    )
    for index, event in enumerate(events):
        event_started_at = _parse_v3_utc_timestamp(
            cast("str", event["started_at"]),
            field_name=f"event {index}.started_at",
        )
        event_completed_at = _parse_v3_utc_timestamp(
            cast("str", event["completed_at"]),
            field_name=f"event {index}.completed_at",
        )
        if event_started_at < run_created_at or event_completed_at > completion_at:
            raise BenchmarkError(f"event {index}: timestamps fall outside the completed run")
    provenance_fields = (
        "claimable",
        "runner_commit",
        "runner_sha256",
        "container_digest",
        "clusy_preflight",
        "execution_budget_caps",
    )
    for index, event in enumerate(events):
        if event.get("run_id") != run_id:
            raise BenchmarkError(f"event {index}: run_id mismatch")
        for field_name in provenance_fields:
            if event.get(field_name) != run_metadata.get(field_name):
                raise BenchmarkError(f"event {index}: {field_name} differs from run metadata")

    bootstrap_samples = _summary_bootstrap_samples(
        stored_summary,
        manifest=manifest,
    )
    recomputed_summary = summarize_events(
        manifest,
        events,
        bootstrap_samples=bootstrap_samples,
        events_sha256=events_sha,
    )
    recomputed_ledger = _recompute_budget_ledger(
        manifest,
        events,
    )
    _validate_observed_budget_ledger(
        recomputed_ledger,
        manifest=manifest,
        execution_caps=cast("Mapping[str, Any]", run_metadata["execution_budget_caps"]),
        field_name="completed run observed budget ledger",
    )
    recomputed_summary["observed_budget_ledger"] = recomputed_ledger
    if canonical_json_bytes(recomputed_summary) != canonical_json_bytes(stored_summary):
        raise BenchmarkError(
            "stored summary does not exactly match recomputation from the sealed "
            "manifest and event journal"
        )
    return stored_summary


def _window_timing_evidence_from_run(
    run_directory: Path,
) -> WindowTimingEvidence:
    manifest = parse_manifest(
        _load_canonical_json_artifact(
            run_directory / "manifest.json",
            artifact_name="manifest artifact",
        )
    )
    run_metadata = _load_canonical_json_artifact(
        run_directory / "run.json",
        artifact_name="run metadata",
    )
    events, _events_bytes = _load_canonical_event_journal(run_directory / "events.jsonl")
    completion = _load_canonical_json_artifact(
        run_directory / "completion.json",
        artifact_name="completion record",
    )
    if not events:
        raise BenchmarkError("completed run has no timing-bearing events")
    started_at: list[datetime] = []
    completed_at: list[datetime] = []
    for index, event in enumerate(events):
        event_started = event.get("started_at")
        event_completed = event.get("completed_at")
        if not isinstance(event_started, str) or not isinstance(event_completed, str):
            raise BenchmarkError(f"event {index}: request timestamps are missing")
        parsed_started = _parse_v3_utc_timestamp(
            event_started,
            field_name=f"event {index}.started_at",
        )
        parsed_completed = _parse_v3_utc_timestamp(
            event_completed,
            field_name=f"event {index}.completed_at",
        )
        if parsed_completed < parsed_started:
            raise BenchmarkError(f"event {index}: completion predates start")
        started_at.append(parsed_started)
        completed_at.append(parsed_completed)
    run_created = _require_string(run_metadata, "created_at", max_length=64)
    completion_timestamp = _require_string(
        completion,
        "completed_at",
        max_length=64,
    )
    prime = (
        _parse_v3_utc_timestamp(
            manifest.warm_cache_primed_at,
            field_name="warm_cache_primed_at",
        )
        if manifest.warm_cache_primed_at is not None
        else None
    )
    run_id = _require_string(run_metadata, "run_id", max_length=128)
    return WindowTimingEvidence(
        run_id=run_id,
        mode=manifest.mode,
        time_window_id=manifest.time_window_id,
        independent_window_index=manifest.independent_window_index,
        required_independent_windows=manifest.required_independent_windows,
        cache_max_age_seconds=manifest.cache_max_age_seconds,
        manifest_created_at=_parse_v3_utc_timestamp(
            manifest.created_at,
            field_name="manifest.created_at",
        ),
        warm_cache_primed_at=prime,
        run_created_at=_parse_v3_utc_timestamp(
            run_created,
            field_name="run.created_at",
        ),
        first_request_started_at=min(started_at),
        oldest_request_completed_at=min(completed_at),
        last_request_completed_at=max(completed_at),
        completion_at=_parse_v3_utc_timestamp(
            completion_timestamp,
            field_name="completion.completed_at",
        ),
    )


def aggregate_completed_run_directories(
    *,
    run_directories: Sequence[Path],
    output_path: Path,
) -> None:
    if output_path.exists():
        raise BenchmarkError(f"refusing to overwrite existing aggregate: {output_path}")
    if not run_directories:
        raise BenchmarkError("aggregate requires at least one completed run directory")
    resolved = [path.resolve() for path in run_directories]
    if len(resolved) != len(set(resolved)):
        raise BenchmarkError("duplicate completed run directories are forbidden")
    summaries = [verify_completed_run_directory(path) for path in run_directories]
    timing_evidence = [_window_timing_evidence_from_run(path) for path in run_directories]
    timing_reasons = validate_independent_window_timing(timing_evidence)
    aggregate = _evaluate_v3_summaries(
        summaries,
        artifact_chains_verified=True,
        timing_validation_reasons=timing_reasons,
    )
    _atomic_write(output_path, canonical_json_bytes(aggregate) + b"\n")
    output_path.chmod(0o400)


def score_existing_run(
    *,
    manifest_path: Path,
    events_path: Path,
    output_path: Path,
    bootstrap_samples: int | None = None,
) -> None:
    manifest = load_manifest(manifest_path)
    if bootstrap_samples is not None and bootstrap_samples != manifest.bootstrap_samples:
        raise BenchmarkError(
            "--bootstrap-samples must exactly match the sealed manifest value "
            f"{manifest.bootstrap_samples}"
        )
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
        bootstrap_samples=manifest.bootstrap_samples,
        events_sha256=sha256_bytes(events_path.read_bytes()),
    )
    if manifest.schema_version == SCHEMA_VERSION:
        summary["observed_budget_ledger"] = _recompute_budget_ledger(
            manifest,
            events,
        )
        _validate_summary_artifact(summary, manifest=manifest)
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
    run.add_argument(
        "--confirm-manifest-sha256",
        required=True,
        help="exact digest confirmation for the sealed paid manifest",
    )
    run.add_argument(
        "--max-exa-usd",
        type=float,
        help="required only when Exa is selected; otherwise omit or set zero",
    )
    run.add_argument(
        "--max-firecrawl-credits",
        type=float,
        help="required only when Firecrawl is selected; otherwise omit or set zero",
    )
    run.add_argument("--max-clusy-usd", required=True, type=float)
    run.add_argument(
        "--acknowledge-exa-live-use",
        action="store_true",
        help=(
            "second, runtime Exa authorization gate; has no effect unless the sealed "
            "manifest also selects and authorizes Exa"
        ),
    )
    run.add_argument(
        "--bootstrap-samples",
        type=int,
        default=None,
        help="optional exact confirmation of the sealed manifest bootstrap_samples",
    )

    score = subparsers.add_parser("score", help="score existing event JSONL offline")
    score.add_argument("--manifest", required=True, type=Path)
    score.add_argument("--events", required=True, type=Path)
    score.add_argument("--output", required=True, type=Path)
    score.add_argument(
        "--bootstrap-samples",
        type=int,
        default=None,
        help="optional exact confirmation of the sealed manifest bootstrap_samples",
    )

    aggregate = subparsers.add_parser(
        "aggregate",
        help="evaluate the v3 multi-mode, multi-window diagnostic gates offline",
    )
    aggregate.add_argument(
        "--run-directory",
        required=True,
        action="append",
        type=Path,
        help=(
            "completed run directory containing manifest.json, run.json, "
            "events.jsonl, summary.json, and completion.json"
        ),
    )
    aggregate.add_argument("--output", required=True, type=Path)
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
                        "schema_version": manifest.schema_version,
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
                confirmed_manifest_sha256=arguments.confirm_manifest_sha256,
                max_exa_usd=arguments.max_exa_usd,
                max_firecrawl_credits=arguments.max_firecrawl_credits,
                max_clusy_usd=arguments.max_clusy_usd,
                acknowledge_exa_live_use=arguments.acknowledge_exa_live_use,
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
        elif arguments.command == "aggregate":
            aggregate_completed_run_directories(
                run_directories=arguments.run_directory,
                output_path=arguments.output,
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

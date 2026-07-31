"""Deterministic, persistence-friendly crawl-frontier state machine.

This module deliberately performs no I/O.  A caller discovers URLs, submits
them with :meth:`CrawlFrontier.admit`, leases work with ``claim()``, and reports
the result with ``succeed()`` or ``fail()``.  The records and metrics exposed by
the frontier are immutable snapshots so the same transitions can later be
backed by a transactional durable queue.

Canonicalization is intentionally conservative.  It removes URL syntax that
cannot affect an HTTP response (fragments, default ports, dot segments), but it
does not sort or discard query parameters: both parameter order and duplicate
keys can be semantically significant.
"""

from __future__ import annotations

import hashlib
import heapq
import ipaddress
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlsplit, urlunsplit

if TYPE_CHECKING:
    from collections.abc import Mapping

_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_PATH_SAFE = _UNRESERVED | frozenset("!$&'()*+,;=:@/")
_QUERY_SAFE = _UNRESERVED | frozenset("!$&'()*+,;=:@/?")
_HEX = frozenset("0123456789abcdefABCDEF")
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_PATH_SESSION = re.compile(r"(?:^|[;/])(?:jsessionid|phpsessid|sessionid)=", re.IGNORECASE)
_QUERY_SESSION = re.compile(
    r"(?:^|[&;])(?:aspsessionid|jsessionid|phpsessid|session_id|sessionid)=",
    re.IGNORECASE,
)
_CALENDAR_PATH = re.compile(r"(?:^|/)(?:calendar|calendars|events|agenda|archive)(?:/|$)", re.I)
_YEAR = re.compile(r"(?<!\d)(\d{4})(?!\d)")
_CALENDAR_NUMBER = re.compile(r"(?<![A-Za-z])(?:\d{1,4})(?![A-Za-z])")
_STRONG_SESSION_KEYS = frozenset(
    {
        "aspsessionid",
        "jsessionid",
        "phpsessid",
        "session_id",
        "sessionid",
    }
)
_AMBIGUOUS_SESSION_KEYS = frozenset({"session", "sid"})
_SESSION_VALUE = re.compile(r"^[A-Za-z0-9._~-]{16,}$")
_CALENDAR_KEYS = frozenset(
    {
        "calendar",
        "date",
        "day",
        "end_date",
        "month",
        "start_date",
        "view_date",
        "year",
    }
)
_FACET_KEY_PARTS = frozenset(
    {
        "attribute",
        "brand",
        "category",
        "color",
        "facet",
        "filter",
        "order",
        "price",
        "size",
        "sort",
        "tag",
    }
)


class RejectionReason(StrEnum):
    """Why a URL admission did not create a new frontier record."""

    DUPLICATE = "duplicate"
    INVALID_URL = "invalid_url"
    UNSUPPORTED_SCHEME = "unsupported_scheme"
    USERINFO_NOT_ALLOWED = "userinfo_not_allowed"
    OFF_SITE = "off_site"
    DEPTH_BUDGET = "depth_budget"
    GLOBAL_URL_BUDGET = "global_url_budget"
    HOST_URL_BUDGET = "host_url_budget"
    PATH_TOO_DEEP = "path_too_deep"
    TRAP_REPEATED_PATH = "trap_repeated_path"
    TRAP_QUERY_PARAMETERS = "trap_query_parameters"
    TRAP_QUERY_VARIANTS = "trap_query_variants"
    TRAP_FACETS = "trap_facets"
    TRAP_CALENDAR = "trap_calendar"
    TRAP_SESSION = "trap_session"


class FrontierState(StrEnum):
    """Lifecycle state of an admitted canonical URL."""

    QUEUED = "queued"
    RETRY_WAIT = "retry_wait"
    IN_FLIGHT = "in_flight"
    TERMINAL = "terminal"


class TerminalReason(StrEnum):
    """Final disposition of an admitted URL."""

    SUCCEEDED = "succeeded"
    PERMANENT_FAILURE = "permanent_failure"
    RETRY_EXHAUSTED = "retry_exhausted"
    GLOBAL_FETCH_BUDGET = "global_fetch_budget"
    HOST_FETCH_BUDGET = "host_fetch_budget"
    ROBOTS_DISALLOWED = "robots_disallowed"
    CONTENT_REJECTED = "content_rejected"
    CANCELLED = "cancelled"


class UrlCanonicalizationError(ValueError):
    """A URL cannot safely participate in this HTTP frontier."""

    def __init__(self, reason: RejectionReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class StaleLeaseError(RuntimeError):
    """A completion does not match the currently in-flight attempt."""


@dataclass(frozen=True, slots=True)
class FrontierConfig:
    """Resource, scope, politeness, and trap policy for one crawl."""

    max_depth: int = 4
    max_urls: int = 10_000
    max_urls_per_host: int = 2_000
    max_fetch_attempts: int | None = None
    max_fetch_attempts_per_host: int | None = None
    max_attempts_per_url: int = 3
    allow_subdomains: bool = False
    host_delay_s: float = 1.0
    retry_backoff_base_s: float = 1.0
    max_retry_delay_s: float = 120.0
    max_url_length: int = 8_192
    max_path_segments: int = 96
    max_repeated_path_segment: int = 3
    max_query_parameters: int = 24
    max_repeated_query_key: int = 6
    max_query_variants_per_path: int = 32
    max_facet_parameters: int = 8
    max_calendar_variants_per_pattern: int = 24
    min_calendar_year: int = 1990
    max_calendar_year: int = 2100

    def __post_init__(self) -> None:
        positive: dict[str, int] = {
            "max_urls": self.max_urls,
            "max_urls_per_host": self.max_urls_per_host,
            "max_attempts_per_url": self.max_attempts_per_url,
            "max_url_length": self.max_url_length,
            "max_path_segments": self.max_path_segments,
            "max_repeated_path_segment": self.max_repeated_path_segment,
            "max_query_parameters": self.max_query_parameters,
            "max_repeated_query_key": self.max_repeated_query_key,
            "max_query_variants_per_path": self.max_query_variants_per_path,
            "max_facet_parameters": self.max_facet_parameters,
            "max_calendar_variants_per_pattern": self.max_calendar_variants_per_pattern,
        }
        for name, positive_value in positive.items():
            if positive_value <= 0:
                raise ValueError(f"{name} must be positive")
        optional_positive: dict[str, int | None] = {
            "max_fetch_attempts": self.max_fetch_attempts,
            "max_fetch_attempts_per_host": self.max_fetch_attempts_per_host,
        }
        for name, optional_value in optional_positive.items():
            if optional_value is not None and optional_value <= 0:
                raise ValueError(f"{name} must be positive when set")
        if self.max_depth < 0:
            raise ValueError("max_depth must be non-negative")
        non_negative_floats: dict[str, float] = {
            "host_delay_s": self.host_delay_s,
            "retry_backoff_base_s": self.retry_backoff_base_s,
            "max_retry_delay_s": self.max_retry_delay_s,
        }
        for name, float_value in non_negative_floats.items():
            if not math.isfinite(float_value) or float_value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.min_calendar_year > self.max_calendar_year:
            raise ValueError("min_calendar_year cannot exceed max_calendar_year")

    @property
    def global_fetch_limit(self) -> int:
        if self.max_fetch_attempts is not None:
            return self.max_fetch_attempts
        return self.max_urls * self.max_attempts_per_url

    @property
    def host_fetch_limit(self) -> int:
        if self.max_fetch_attempts_per_host is not None:
            return self.max_fetch_attempts_per_host
        return self.max_urls_per_host * self.max_attempts_per_url


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    accepted: bool
    url: str | None
    reason: RejectionReason | None = None
    reprioritized: bool = False


@dataclass(frozen=True, slots=True)
class FrontierLease:
    """Token identifying exactly one in-flight fetch attempt."""

    url: str
    host: str
    depth: int
    priority: int
    attempt: int
    parent_url: str | None


@dataclass(frozen=True, slots=True)
class FrontierRecord:
    """Immutable record suitable for diagnostics or persistence adapters."""

    url: str
    host: str
    depth: int
    priority: int
    state: FrontierState
    attempts: int
    ready_at: float
    parent_url: str | None
    terminal_reason: TerminalReason | None
    order: int


@dataclass(frozen=True, slots=True)
class FailureResult:
    retry_scheduled: bool
    ready_at: float | None
    terminal_reason: TerminalReason | None


@dataclass(frozen=True, slots=True)
class FrontierMetrics:
    admission_attempts: int
    admitted: int
    rejected: int
    duplicates: int
    claimed: int
    retries_scheduled: int
    pending: int
    in_flight: int
    terminal: int
    rejection_reasons: Mapping[RejectionReason, int]
    terminal_reasons: Mapping[TerminalReason, int]


@dataclass(slots=True)
class _Entry:
    url: str
    host: str
    path: str
    query: str
    depth: int
    priority: int
    parent_url: str | None
    order: int
    state: FrontierState = FrontierState.QUEUED
    attempts: int = 0
    ready_at: float = 0.0
    generation: int = 0
    terminal_reason: TerminalReason | None = None

    def snapshot(self) -> FrontierRecord:
        return FrontierRecord(
            url=self.url,
            host=self.host,
            depth=self.depth,
            priority=self.priority,
            state=self.state,
            attempts=self.attempts,
            ready_at=self.ready_at,
            parent_url=self.parent_url,
            terminal_reason=self.terminal_reason,
            order=self.order,
        )


_WaitingHeapItem = tuple[float, int, int, str]
_ReadyHeapItem = tuple[int, int, int, str]


@dataclass(slots=True)
class _HostState:
    waiting: list[_WaitingHeapItem] = field(default_factory=list)
    ready: list[_ReadyHeapItem] = field(default_factory=list)
    next_allowed_at: float = 0.0
    last_served_turn: int = -1
    admitted: int = 0
    fetches: int = 0


def canonicalize_url(url: str, *, max_length: int = 8_192) -> str:
    """Return a conservative ASCII canonical HTTP URL.

    Query order and duplicate parameters are preserved.  Userinfo is rejected
    instead of silently discarded, which avoids turning an attacker-controlled
    authority into a misleading deduplication key.
    """

    if not isinstance(url, str) or not url:
        raise UrlCanonicalizationError(RejectionReason.INVALID_URL, "URL must be non-empty")
    if len(url) > max_length:
        raise UrlCanonicalizationError(RejectionReason.INVALID_URL, "URL exceeds length limit")
    if url != url.strip():
        raise UrlCanonicalizationError(
            RejectionReason.INVALID_URL,
            "URL has leading or trailing whitespace",
        )
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in url):
        raise UrlCanonicalizationError(
            RejectionReason.INVALID_URL,
            "URL contains control characters",
        )
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise UrlCanonicalizationError(RejectionReason.INVALID_URL, "malformed URL") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UrlCanonicalizationError(
            RejectionReason.UNSUPPORTED_SCHEME,
            "only HTTP and HTTPS URLs are supported",
        )
    if parsed.username is not None or parsed.password is not None:
        raise UrlCanonicalizationError(
            RejectionReason.USERINFO_NOT_ALLOWED,
            "URL userinfo is not allowed",
        )
    try:
        raw_host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise UrlCanonicalizationError(RejectionReason.INVALID_URL, "invalid URL port") from exc
    if not raw_host:
        raise UrlCanonicalizationError(RejectionReason.INVALID_URL, "URL host is required")
    if parsed.netloc.endswith(":"):
        raise UrlCanonicalizationError(RejectionReason.INVALID_URL, "URL port is empty")

    host, is_ipv6 = _normalize_host(raw_host)
    if parsed.netloc.startswith("[") and not is_ipv6:
        raise UrlCanonicalizationError(
            RejectionReason.INVALID_URL,
            "bracketed hosts must be IPv6 literals",
        )
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    authority = f"[{host}]" if is_ipv6 else host
    if port is not None:
        authority = f"{authority}:{port}"

    path = _normalize_component(parsed.path or "/", _PATH_SAFE)
    path = _remove_dot_segments(path)
    query = _normalize_component(parsed.query, _QUERY_SAFE)
    canonical = urlunsplit((scheme, authority, path, query, ""))
    if len(canonical) > max_length:
        raise UrlCanonicalizationError(
            RejectionReason.INVALID_URL,
            "canonical URL exceeds length limit",
        )
    return canonical


def _normalize_host(raw_host: str) -> tuple[str, bool]:
    if raw_host.endswith(".."):
        raise UrlCanonicalizationError(
            RejectionReason.INVALID_URL,
            "host has multiple trailing dots",
        )
    host = raw_host.removesuffix(".")
    if not host:
        raise UrlCanonicalizationError(RejectionReason.INVALID_URL, "URL host is empty")
    if "%" in host:
        raise UrlCanonicalizationError(
            RejectionReason.INVALID_URL,
            "scoped IP literals are not allowed",
        )
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            ascii_host = host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise UrlCanonicalizationError(
                RejectionReason.INVALID_URL,
                "host cannot be encoded with IDNA",
            ) from exc
        if len(ascii_host) > 253 or any(
            not _HOST_LABEL.fullmatch(label) for label in ascii_host.split(".")
        ):
            raise UrlCanonicalizationError(
                RejectionReason.INVALID_URL,
                "invalid DNS host",
            ) from None
        return ascii_host, False
    return address.compressed.lower(), address.version == 6


def _normalize_component(value: str, safe: frozenset[str]) -> str:
    output: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "%":
            if (
                index + 2 >= len(value)
                or value[index + 1] not in _HEX
                or value[index + 2] not in _HEX
            ):
                raise UrlCanonicalizationError(
                    RejectionReason.INVALID_URL,
                    "URL contains an invalid percent escape",
                )
            encoded = value[index + 1 : index + 3].upper()
            decoded = chr(int(encoded, 16))
            output.append(decoded if decoded in _UNRESERVED else f"%{encoded}")
            index += 3
            continue
        if character in safe:
            output.append(character)
        else:
            try:
                encoded_character = character.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise UrlCanonicalizationError(
                    RejectionReason.INVALID_URL,
                    "URL contains invalid Unicode",
                ) from exc
            output.extend(f"%{byte:02X}" for byte in encoded_character)
        index += 1
    return "".join(output)


def _remove_dot_segments(path: str) -> str:
    """Remove RFC 3986 dot segments without collapsing meaningful ``//``."""

    trailing_slash = path.endswith(("/", "/.", "/.."))
    output: list[str] = []
    for segment in path.split("/"):
        if segment == ".":
            continue
        if segment == "..":
            if len(output) > 1:
                output.pop()
            continue
        output.append(segment)
    normalized = "/".join(output)
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    if trailing_slash and not normalized.endswith("/"):
        normalized += "/"
    return normalized or "/"


def _host_from_canonical(url: str) -> str:
    host = urlsplit(url).hostname
    if host is None:
        raise AssertionError("canonical URL unexpectedly has no host")
    return host


def _same_site(host: str, roots: frozenset[str], allow_subdomains: bool) -> bool:
    if host in roots:
        return True
    if not allow_subdomains:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return any(host.endswith(f".{root}") for root in roots)
    return False


def _query_pairs(query: str) -> list[tuple[str, str]]:
    if not query:
        return []
    try:
        return parse_qsl(query, keep_blank_values=True, strict_parsing=False)
    except ValueError as exc:
        raise UrlCanonicalizationError(
            RejectionReason.INVALID_URL,
            "query parameters cannot be parsed",
        ) from exc


def _key_token(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")


def _contains_key_part(key: str, parts: frozenset[str]) -> bool:
    tokens = frozenset(_key_token(key).split("_"))
    return bool(tokens & parts)


def _stable_seed(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:8], "big")


class CrawlFrontier:
    """Single-owner deterministic frontier.

    Eligible hosts receive stable least-recently-served turns.  Within each host
    higher integer priorities run first.  This host-first ordering prevents a
    busy origin from starving another origin while retaining useful priority
    scheduling inside each politeness queue.
    """

    def __init__(
        self,
        seed_urls: list[str] | tuple[str, ...],
        *,
        config: FrontierConfig | None = None,
        seed_priority: int = 0,
    ) -> None:
        if not seed_urls:
            raise ValueError("at least one seed URL is required")
        if isinstance(seed_priority, bool) or not isinstance(seed_priority, int):
            raise TypeError("seed_priority must be an integer")
        self.config = config or FrontierConfig()
        canonical_seeds = [
            canonicalize_url(seed, max_length=self.config.max_url_length) for seed in seed_urls
        ]
        self._roots = frozenset(_host_from_canonical(seed) for seed in canonical_seeds)
        self._entries: dict[str, _Entry] = {}
        self._hosts: dict[str, _HostState] = {}
        self._path_query_variants: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
        self._calendar_variants: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
        self._rejection_reasons: Counter[RejectionReason] = Counter()
        self._terminal_reasons: Counter[TerminalReason] = Counter()
        self._admission_attempts = 0
        self._admitted = 0
        self._duplicates = 0
        self._claimed = 0
        self._retries_scheduled = 0
        self._order = 0
        self._turn = 0
        for seed in canonical_seeds:
            self._admission_attempts += 1
            result = self._admit_canonical(
                seed,
                depth=0,
                priority=seed_priority,
                parent_url=None,
                ready_at=0.0,
            )
            if not result.accepted and result.reason is not RejectionReason.DUPLICATE:
                raise ValueError(f"seed URL rejected by frontier policy: {result.reason}")

    @property
    def roots(self) -> frozenset[str]:
        return self._roots

    def is_in_scope(self, url: str) -> bool:
        """Whether a URL remains inside this frontier's configured site scope."""

        try:
            canonical = canonicalize_url(url, max_length=self.config.max_url_length)
        except UrlCanonicalizationError:
            return False
        return _same_site(
            _host_from_canonical(canonical),
            self._roots,
            self.config.allow_subdomains,
        )

    def admit(
        self,
        url: str,
        *,
        depth: int,
        priority: int = 0,
        parent_url: str | None = None,
        ready_at: float = 0.0,
    ) -> AdmissionResult:
        """Validate and enqueue a discovered URL."""

        self._admission_attempts += 1
        if depth < 0:
            return self._reject(RejectionReason.DEPTH_BUDGET)
        if not math.isfinite(ready_at) or ready_at < 0:
            raise ValueError("ready_at must be finite and non-negative")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise TypeError("priority must be an integer")
        try:
            canonical = canonicalize_url(url, max_length=self.config.max_url_length)
        except UrlCanonicalizationError as exc:
            return self._reject(exc.reason)
        return self._admit_canonical(
            canonical,
            depth=depth,
            priority=priority,
            parent_url=parent_url,
            ready_at=ready_at,
        )

    def _admit_canonical(
        self,
        url: str,
        *,
        depth: int,
        priority: int,
        parent_url: str | None,
        ready_at: float,
    ) -> AdmissionResult:
        host = _host_from_canonical(url)
        if not _same_site(host, self._roots, self.config.allow_subdomains):
            return self._reject(RejectionReason.OFF_SITE, url)

        existing = self._entries.get(url)
        if existing is not None:
            self._duplicates += 1
            self._rejection_reasons[RejectionReason.DUPLICATE] += 1
            reprioritized = False
            if existing.state in {FrontierState.QUEUED, FrontierState.RETRY_WAIT}:
                better_depth = depth < existing.depth
                better_priority = priority > existing.priority
                if better_depth:
                    existing.depth = depth
                    existing.parent_url = parent_url
                if better_priority:
                    existing.priority = priority
                if better_depth or better_priority:
                    self._enqueue(existing, min(existing.ready_at, ready_at))
                    reprioritized = True
            return AdmissionResult(
                accepted=False,
                url=url,
                reason=RejectionReason.DUPLICATE,
                reprioritized=reprioritized,
            )

        if depth > self.config.max_depth:
            return self._reject(RejectionReason.DEPTH_BUDGET, url)
        split = urlsplit(url)
        path = split.path
        query = split.query
        trap = self._trap_reason(host, path, query, url)
        if trap is not None:
            return self._reject(trap, url)
        if self._admitted >= self.config.max_urls:
            return self._reject(RejectionReason.GLOBAL_URL_BUDGET, url)
        host_state = self._hosts.setdefault(host, _HostState())
        if host_state.admitted >= self.config.max_urls_per_host:
            return self._reject(RejectionReason.HOST_URL_BUDGET, url)

        self._order += 1
        entry = _Entry(
            url=url,
            host=host,
            path=path,
            query=query,
            depth=depth,
            priority=priority,
            parent_url=parent_url,
            order=self._order,
        )
        self._entries[url] = entry
        self._admitted += 1
        host_state.admitted += 1
        if query:
            self._path_query_variants[(host, path)].add(query)
        calendar_signature = self._calendar_signature(path, query)
        if calendar_signature is not None:
            self._calendar_variants[(host, calendar_signature)].add(url)
        self._enqueue(entry, ready_at)
        return AdmissionResult(accepted=True, url=url)

    def _reject(
        self,
        reason: RejectionReason,
        url: str | None = None,
    ) -> AdmissionResult:
        self._rejection_reasons[reason] += 1
        return AdmissionResult(accepted=False, url=url, reason=reason)

    def _trap_reason(
        self,
        host: str,
        path: str,
        query: str,
        url: str,
    ) -> RejectionReason | None:
        segments = [segment for segment in path.split("/") if segment]
        if len(segments) > self.config.max_path_segments:
            return RejectionReason.PATH_TOO_DEEP
        counts = Counter(segment.casefold() for segment in segments)
        if counts and max(counts.values()) > self.config.max_repeated_path_segment:
            return RejectionReason.TRAP_REPEATED_PATH
        if _PATH_SESSION.search(path):
            return RejectionReason.TRAP_SESSION

        pairs = _query_pairs(query)
        if _QUERY_SESSION.search(query):
            return RejectionReason.TRAP_SESSION
        if len(pairs) > self.config.max_query_parameters:
            return RejectionReason.TRAP_QUERY_PARAMETERS
        normalized_pairs = [(_key_token(key), value) for key, value in pairs]
        normalized_keys = [key for key, _value in normalized_pairs]
        if any(
            key in _STRONG_SESSION_KEYS
            or (key in _AMBIGUOUS_SESSION_KEYS and _SESSION_VALUE.fullmatch(value))
            for key, value in normalized_pairs
        ):
            return RejectionReason.TRAP_SESSION
        key_counts = Counter(normalized_keys)
        if key_counts and max(key_counts.values()) > self.config.max_repeated_query_key:
            return RejectionReason.TRAP_QUERY_PARAMETERS
        facet_count = sum(_contains_key_part(key, _FACET_KEY_PARTS) for key in normalized_keys)
        if facet_count > self.config.max_facet_parameters:
            return RejectionReason.TRAP_FACETS

        if query:
            variants = self._path_query_variants[(host, path)]
            if query not in variants and len(variants) >= self.config.max_query_variants_per_path:
                return RejectionReason.TRAP_QUERY_VARIANTS

        calendar_signature = self._calendar_signature(path, query)
        if calendar_signature is not None:
            calendar_values = [value for key, value in pairs if _key_token(key) in _CALENDAR_KEYS]
            years = [
                int(match) for value in (path, *calendar_values) for match in _YEAR.findall(value)
            ]
            if any(
                year < self.config.min_calendar_year or year > self.config.max_calendar_year
                for year in years
            ):
                return RejectionReason.TRAP_CALENDAR
            variants = self._calendar_variants[(host, calendar_signature)]
            if (
                url not in variants
                and len(variants) >= self.config.max_calendar_variants_per_pattern
            ):
                return RejectionReason.TRAP_CALENDAR
        return None

    @staticmethod
    def _calendar_signature(path: str, query: str) -> str | None:
        pairs = _query_pairs(query)
        has_calendar_key = any(_key_token(key) in _CALENDAR_KEYS for key, _value in pairs)
        if not has_calendar_key and not _CALENDAR_PATH.search(path):
            return None
        path_signature = _CALENDAR_NUMBER.sub("{n}", path.casefold())
        query_parts = [
            f"{key}={{calendar}}" if _key_token(key) in _CALENDAR_KEYS else f"{key}={value}"
            for key, value in pairs
        ]
        return f"{path_signature}?{'&'.join(query_parts)}"

    def _enqueue(self, entry: _Entry, ready_at: float) -> None:
        entry.generation += 1
        entry.ready_at = ready_at
        if entry.attempts:
            entry.state = FrontierState.RETRY_WAIT
        else:
            entry.state = FrontierState.QUEUED
        state = self._hosts.setdefault(entry.host, _HostState())
        heapq.heappush(
            state.waiting,
            (ready_at, entry.order, entry.generation, entry.url),
        )

    def claim(self, *, now: float) -> FrontierLease | None:
        """Lease the next eligible URL, or ``None`` when no work is ready."""

        if not math.isfinite(now) or now < 0:
            raise ValueError("now must be finite and non-negative")
        while True:
            if self._claimed >= self.config.global_fetch_limit:
                self._terminate_pending(None, TerminalReason.GLOBAL_FETCH_BUDGET)
                return None

            candidates: list[tuple[int, int, str, int, _Entry]] = []
            exhausted_hosts: list[str] = []
            for host, state in self._hosts.items():
                if state.fetches >= self.config.host_fetch_limit:
                    exhausted_hosts.append(host)
                    continue
                self._promote_ready(state, now)
                entry = self._peek_ready(state)
                if entry is None or state.next_allowed_at > now:
                    continue
                candidates.append(
                    (
                        state.last_served_turn,
                        -entry.priority,
                        host,
                        entry.order,
                        entry,
                    )
                )
            for host in exhausted_hosts:
                self._terminate_pending(host, TerminalReason.HOST_FETCH_BUDGET)
            if not candidates:
                return None

            _last_turn, _priority, host, _order, selected = min(candidates)
            state = self._hosts[host]
            self._pop_ready(state, selected)
            selected.state = FrontierState.IN_FLIGHT
            selected.attempts += 1
            self._claimed += 1
            state.fetches += 1
            state.last_served_turn = self._turn
            self._turn += 1
            state.next_allowed_at = max(
                state.next_allowed_at,
                now + self.config.host_delay_s,
            )
            return FrontierLease(
                url=selected.url,
                host=selected.host,
                depth=selected.depth,
                priority=selected.priority,
                attempt=selected.attempts,
                parent_url=selected.parent_url,
            )

    def _promote_ready(self, state: _HostState, now: float) -> None:
        while state.waiting and state.waiting[0][0] <= now:
            _ready_at, _order, generation, url = heapq.heappop(state.waiting)
            entry = self._entries[url]
            if entry.generation != generation or entry.state not in {
                FrontierState.QUEUED,
                FrontierState.RETRY_WAIT,
            }:
                continue
            heapq.heappush(
                state.ready,
                (-entry.priority, entry.order, generation, entry.url),
            )

    def _peek_ready(self, state: _HostState) -> _Entry | None:
        while state.ready:
            _priority, _order, generation, url = state.ready[0]
            entry = self._entries[url]
            if entry.generation == generation and entry.state in {
                FrontierState.QUEUED,
                FrontierState.RETRY_WAIT,
            }:
                return entry
            heapq.heappop(state.ready)
        return None

    @staticmethod
    def _pop_ready(state: _HostState, expected: _Entry) -> None:
        _priority, _order, _generation, url = heapq.heappop(state.ready)
        if url != expected.url:
            raise AssertionError("frontier ready heap changed during claim")

    def succeed(self, lease: FrontierLease) -> FrontierRecord:
        entry = self._resolve_lease(lease)
        self._terminate(entry, TerminalReason.SUCCEEDED)
        return entry.snapshot()

    def fail(
        self,
        lease: FrontierLease,
        *,
        now: float,
        retryable: bool,
        retry_after: str | int | float | None = None,
        wall_now: datetime | None = None,
        terminal_reason: TerminalReason = TerminalReason.PERMANENT_FAILURE,
    ) -> FailureResult:
        """Report failure, scheduling a bounded retry when policy permits."""

        if not math.isfinite(now) or now < 0:
            raise ValueError("now must be finite and non-negative")
        entry = self._resolve_lease(lease)
        if not retryable:
            if terminal_reason is TerminalReason.SUCCEEDED:
                raise ValueError("a failed lease cannot terminate as succeeded")
            self._terminate(entry, terminal_reason)
            return FailureResult(False, None, terminal_reason)
        if entry.attempts >= self.config.max_attempts_per_url:
            self._terminate(entry, TerminalReason.RETRY_EXHAUSTED)
            return FailureResult(False, None, TerminalReason.RETRY_EXHAUSTED)

        retry_after_s = self._retry_after_seconds(retry_after, wall_now)
        exponent = min(entry.attempts - 1, 62)
        backoff = min(
            self.config.max_retry_delay_s,
            self.config.retry_backoff_base_s * (2**exponent),
        )
        delay = min(
            self.config.max_retry_delay_s,
            max(backoff, retry_after_s or 0.0),
        )
        ready_at = now + delay
        if retry_after_s is not None:
            host_state = self._hosts[entry.host]
            host_state.next_allowed_at = max(
                host_state.next_allowed_at,
                now + min(retry_after_s, self.config.max_retry_delay_s),
            )
        self._retries_scheduled += 1
        self._enqueue(entry, ready_at)
        return FailureResult(True, ready_at, None)

    def _retry_after_seconds(
        self,
        value: str | int | float | None,
        wall_now: datetime | None,
    ) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            if not math.isfinite(float(value)):
                return None
            return min(self.config.max_retry_delay_s, max(0.0, float(value)))
        stripped = value.strip()
        if stripped.isdecimal():
            return min(self.config.max_retry_delay_s, float(stripped))
        if wall_now is None:
            return None
        try:
            parsed = parsedate_to_datetime(stripped)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        if wall_now.tzinfo is None:
            wall_now = wall_now.replace(tzinfo=UTC)
        seconds = max(0.0, (parsed - wall_now).total_seconds())
        return min(self.config.max_retry_delay_s, seconds)

    def _resolve_lease(self, lease: FrontierLease) -> _Entry:
        entry = self._entries.get(lease.url)
        if (
            entry is None
            or entry.state is not FrontierState.IN_FLIGHT
            or entry.attempts != lease.attempt
            or entry.host != lease.host
        ):
            raise StaleLeaseError("lease is stale or does not belong to this frontier")
        return entry

    def _terminate(self, entry: _Entry, reason: TerminalReason) -> None:
        entry.state = FrontierState.TERMINAL
        entry.terminal_reason = reason
        entry.generation += 1
        self._terminal_reasons[reason] += 1

    def _terminate_pending(
        self,
        host: str | None,
        reason: TerminalReason,
    ) -> None:
        for entry in self._entries.values():
            if (host is None or entry.host == host) and entry.state in {
                FrontierState.QUEUED,
                FrontierState.RETRY_WAIT,
            }:
                self._terminate(entry, reason)

    def cancel_pending(self) -> int:
        before = sum(self._terminal_reasons.values())
        self._terminate_pending(None, TerminalReason.CANCELLED)
        return sum(self._terminal_reasons.values()) - before

    def next_wake_at(self, *, now: float) -> float | None:
        """Earliest scheduler time that could make pending work claimable."""

        if not math.isfinite(now) or now < 0:
            raise ValueError("now must be finite and non-negative")
        wake_times: list[float] = []
        for state in self._hosts.values():
            self._promote_ready(state, now)
            if self._peek_ready(state) is not None:
                wake_times.append(max(now, state.next_allowed_at))
                continue
            while state.waiting:
                ready_at, _order, generation, url = state.waiting[0]
                entry = self._entries[url]
                if entry.generation == generation and entry.state in {
                    FrontierState.QUEUED,
                    FrontierState.RETRY_WAIT,
                }:
                    wake_times.append(max(ready_at, state.next_allowed_at))
                    break
                heapq.heappop(state.waiting)
        return min(wake_times) if wake_times else None

    def record(self, url: str) -> FrontierRecord | None:
        try:
            canonical = canonicalize_url(url, max_length=self.config.max_url_length)
        except UrlCanonicalizationError:
            return None
        entry = self._entries.get(canonical)
        return entry.snapshot() if entry is not None else None

    def records(self) -> tuple[FrontierRecord, ...]:
        return tuple(
            entry.snapshot()
            for entry in sorted(self._entries.values(), key=lambda item: item.order)
        )

    def metrics(self) -> FrontierMetrics:
        states = Counter(entry.state for entry in self._entries.values())
        rejection_reasons = MappingProxyType(dict(self._rejection_reasons))
        terminal_reasons = MappingProxyType(dict(self._terminal_reasons))
        return FrontierMetrics(
            admission_attempts=self._admission_attempts,
            admitted=self._admitted,
            rejected=sum(
                count
                for reason, count in self._rejection_reasons.items()
                if reason is not RejectionReason.DUPLICATE
            ),
            duplicates=self._duplicates,
            claimed=self._claimed,
            retries_scheduled=self._retries_scheduled,
            pending=states[FrontierState.QUEUED] + states[FrontierState.RETRY_WAIT],
            in_flight=states[FrontierState.IN_FLIGHT],
            terminal=states[FrontierState.TERMINAL],
            rejection_reasons=rejection_reasons,
            terminal_reasons=terminal_reasons,
        )

    def deterministic_partition(self, partitions: int) -> int:
        """Stable shard hint for a future durable frontier backend."""

        if partitions <= 0:
            raise ValueError("partitions must be positive")
        roots_key = "\0".join(sorted(self._roots))
        return _stable_seed(roots_key) % partitions

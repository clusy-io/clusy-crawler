"""Bounded RFC 9309-style robots policy for opt-in recursive crawls.

The flat ``max_depth=0`` crawl path never constructs or calls this service.  A
recursive crawl checks every leased seed/discovered URL before the page fetch.

Retrieval policy is intentionally explicit:

* a successful 2xx response is parsed and its longest matching rule wins
  (``Allow`` wins an equal-specificity tie);
* 404/410 and other non-429 4xx responses mean the robots resource is
  unavailable, so crawling is allowed as RFC 9309 permits;
* 429, 5xx, timeout, transport failure, unsafe/invalid redirects, oversized or
  over-complex files, and other ambiguous protocol failures are temporarily
  fail-closed;
* no status or transport retries are performed.  Redirects are manual,
  validated at every hop, downgrade-protected, and bounded.

Only parsed policy is cached; response bodies and requested URLs are not.  The
cache is a bounded loop-local LRU, and concurrent checks for one origin share a
cancellation-safe singleflight task.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.config import settings
from app.lib.http_client import get_http_client
from app.services.fetcher import validate_public_url, validate_response_peer
from app.services.frontier import UrlCanonicalizationError, canonicalize_url
from app.services.rate_limiter import get_rate_limiter

if TYPE_CHECKING:
    from app.config import Settings

_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_HEX = frozenset("0123456789abcdefABCDEF")
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_TRANSIENT_CLIENT_STATUSES = frozenset({408, 425, 429})

UrlValidator = Callable[[str], Awaitable[str | None]]
PeerValidator = Callable[[httpx.Response], str | None]
ClientProvider = Callable[[], httpx.AsyncClient]
RateAcquire = Callable[[str], Awaitable[None]]
Clock = Callable[[], float]


class RobotsDecisionReason(StrEnum):
    """Stable, URL-free reason for one robots decision."""

    ALLOWED = "allowed"
    EXPLICIT_DISALLOW = "explicit_disallow"
    MISSING = "missing"
    CLIENT_ERROR = "client_error"
    TRANSIENT_CLIENT_ERROR = "transient_client_error"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    UNSAFE = "unsafe"
    RESPONSE_TOO_LARGE = "response_too_large"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    PROTOCOL_ERROR = "protocol_error"
    POLICY_TOO_COMPLEX = "policy_too_complex"


@dataclass(frozen=True, slots=True)
class RobotsDecision:
    """The policy answer for one page URL."""

    allowed: bool
    reason: RobotsDecisionReason
    cache_hit: bool = False
    status_code: int = 0
    matched_specificity: int = 0

    @property
    def explicitly_disallowed(self) -> bool:
        return self.reason is RobotsDecisionReason.EXPLICIT_DISALLOW


def robots_blocked_error(decision: RobotsDecision) -> str:
    """Return an honest bounded user-facing error for a denied recursive URL."""

    if decision.explicitly_disallowed:
        return "robots.txt explicitly disallows this URL for the configured crawler user-agent"
    if decision.reason in {
        RobotsDecisionReason.SERVER_ERROR,
        RobotsDecisionReason.TRANSIENT_CLIENT_ERROR,
        RobotsDecisionReason.TIMEOUT,
        RobotsDecisionReason.NETWORK_ERROR,
    }:
        return "robots.txt is temporarily unavailable; recursive crawling is denied by policy"
    return "robots.txt could not be evaluated safely; recursive crawling is denied by policy"


@dataclass(frozen=True, slots=True)
class RobotsPolicyConfig:
    timeout_s: float = 5.0
    max_redirects: int = 5
    max_body_bytes: int = 512 * 1024
    max_url_length: int = 4096
    max_rules: int = 4096
    max_records: int = 8192
    max_line_chars: int = 8192
    max_concurrency: int = 16
    cache_max_entries: int = 2048
    cache_ttl_s: int = 3600
    unavailable_cache_ttl_s: int = 900
    error_cache_ttl_s: int = 60
    user_agent: str = "ClusyCrawler/1.0"

    @classmethod
    def from_settings(cls, configured: Settings) -> RobotsPolicyConfig:
        return cls(
            timeout_s=configured.robots_timeout_s,
            max_redirects=configured.robots_max_redirects,
            max_body_bytes=configured.robots_max_body_bytes,
            max_url_length=configured.robots_max_url_length,
            max_rules=configured.robots_max_rules,
            max_records=configured.robots_max_records,
            max_line_chars=configured.robots_max_line_chars,
            max_concurrency=configured.robots_max_concurrency,
            cache_max_entries=configured.robots_cache_max_entries,
            cache_ttl_s=configured.robots_cache_ttl_s,
            unavailable_cache_ttl_s=configured.robots_unavailable_cache_ttl_s,
            error_cache_ttl_s=configured.robots_error_cache_ttl_s,
            user_agent=configured.http_user_agent,
        )

    def __post_init__(self) -> None:
        positive_ints = {
            "max_body_bytes": self.max_body_bytes,
            "max_url_length": self.max_url_length,
            "max_rules": self.max_rules,
            "max_records": self.max_records,
            "max_line_chars": self.max_line_chars,
            "max_concurrency": self.max_concurrency,
            "cache_max_entries": self.cache_max_entries,
            "cache_ttl_s": self.cache_ttl_s,
            "unavailable_cache_ttl_s": self.unavailable_cache_ttl_s,
            "error_cache_ttl_s": self.error_cache_ttl_s,
        }
        for name, value in positive_ints.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_body_bytes < 500 * 1024:
            raise ValueError("max_body_bytes must be at least the RFC 9309 500 KiB floor")
        if self.timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if self.max_redirects < 0:
            raise ValueError("max_redirects must be non-negative")
        if not self.user_agent.strip():
            raise ValueError("user_agent must not be empty")


class RobotsParseLimitError(ValueError):
    """A robots file cannot be represented inside configured parser bounds."""


@dataclass(frozen=True, slots=True)
class _Rule:
    allow: bool
    pattern: str
    end_anchored: bool
    specificity: int

    def matches(self, path_query: str) -> bool:
        pattern = self.pattern if self.end_anchored else f"{self.pattern}*"
        return _glob_matches(pattern, path_query)


@dataclass(frozen=True, slots=True)
class _Group:
    agents: tuple[str, ...]
    rules: tuple[_Rule, ...]


@dataclass(frozen=True, slots=True)
class RobotsRules:
    """Selected rules for the configured crawler product token."""

    rules: tuple[_Rule, ...]

    def evaluate(self, url: str) -> tuple[bool, int]:
        split = urlsplit(url)
        # RFC 9309 section 2.2.2 makes the origin's robots URI implicitly
        # allowed so a broad rule can never prevent policy refresh.
        if split.path == "/robots.txt" and not split.query:
            return True, 0
        path_query = _normalize_octets(split.path or "/")
        if split.query:
            path_query = f"{path_query}?{_normalize_octets(split.query)}"

        matched: _Rule | None = None
        for rule in self.rules:
            if not rule.matches(path_query):
                continue
            if matched is None or rule.specificity > matched.specificity:
                matched = rule
                continue
            if rule.specificity == matched.specificity and rule.allow and not matched.allow:
                matched = rule
        if matched is None:
            return True, 0
        return matched.allow, matched.specificity


def parse_robots_txt(
    body: bytes,
    *,
    user_agent: str,
    max_rules: int = 4096,
    max_records: int = 8192,
    max_line_chars: int = 8192,
) -> RobotsRules:
    """Parse and select RFC 9309-style groups for ``user_agent``.

    Exact product-token groups are merged.  The ``*`` groups are used only when
    there is no exact group, matching RFC 9309's fallback semantics.
    """

    text = body.decode("utf-8-sig", errors="replace")
    groups: list[_Group] = []
    agents: list[str] = []
    rules: list[_Rule] = []
    record_count = 0
    rule_count = 0

    def finish_group() -> None:
        nonlocal agents, rules
        if agents:
            groups.append(_Group(tuple(agents), tuple(rules)))
        agents = []
        rules = []

    for raw_line in text.splitlines():
        if len(raw_line) > max_line_chars:
            raise RobotsParseLimitError("robots line exceeds parser bound")
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            # RFC 9309's ABNF permits empty lines among group records. Treating
            # a blank as a terminator can silently drop a later Disallow.
            continue
        if "\x00" in line or ":" not in line:
            continue
        field, raw_value = line.split(":", 1)
        field = field.strip().casefold()
        value = raw_value.strip()
        record_count += 1
        if record_count > max_records:
            raise RobotsParseLimitError("robots record count exceeds parser bound")

        if field == "user-agent":
            if rules:
                finish_group()
            token = _agent_token(value)
            if token:
                agents.append(token)
            continue
        if field not in {"allow", "disallow"} or not agents or not value:
            continue
        rule = _parse_rule(value, allow=field == "allow")
        if rule is None:
            continue
        rule_count += 1
        if rule_count > max_rules:
            raise RobotsParseLimitError("robots rule count exceeds parser bound")
        rules.append(rule)
    finish_group()

    product = _product_token(user_agent)
    exact = [group for group in groups if product and product in group.agents]
    selected = exact or [group for group in groups if "*" in group.agents]
    return RobotsRules(tuple(rule for group in selected for rule in group.rules))


def _agent_token(value: str) -> str:
    token = value.strip().split(None, 1)[0] if value.strip() else ""
    if token == "*":
        return token
    return token.split("/", 1)[0].casefold()


def _product_token(user_agent: str) -> str:
    token = user_agent.strip().split(None, 1)[0]
    return token.split("/", 1)[0].casefold()


def _parse_rule(value: str, *, allow: bool) -> _Rule | None:
    if not value.startswith("/"):
        return None
    end_anchored = value.endswith("$")
    raw_pattern = value[:-1] if end_anchored else value
    pattern = _normalize_octets(raw_pattern)
    # Consecutive wildcards are semantically identical and needlessly increase
    # matcher work.
    while "**" in pattern:
        pattern = pattern.replace("**", "*")
    specificity = _pattern_specificity(pattern)
    if specificity == 0:
        return None
    return _Rule(
        allow=allow,
        pattern=pattern,
        end_anchored=end_anchored,
        specificity=specificity,
    )


def _normalize_octets(value: str) -> str:
    """Normalize percent escapes as RFC 9309 compares encoded path octets."""

    output: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if (
            character == "%"
            and index + 2 < len(value)
            and value[index + 1] in _HEX
            and value[index + 2] in _HEX
        ):
            encoded = value[index + 1 : index + 3].upper()
            decoded = chr(int(encoded, 16))
            output.append(decoded if decoded in _UNRESERVED else f"%{encoded}")
            index += 3
            continue
        if 0x21 <= ord(character) <= 0x7E:
            output.append(character)
        else:
            output.extend(f"%{byte:02X}" for byte in character.encode("utf-8"))
        index += 1
    return "".join(output)


def _pattern_specificity(pattern: str) -> int:
    specificity = 0
    index = 0
    while index < len(pattern):
        if pattern[index] == "*":
            index += 1
            continue
        if (
            pattern[index] == "%"
            and index + 2 < len(pattern)
            and pattern[index + 1] in _HEX
            and pattern[index + 2] in _HEX
        ):
            specificity += 1
            index += 3
            continue
        specificity += 1
        index += 1
    return specificity


def _glob_matches(pattern: str, value: str) -> bool:
    """Linear wildcard matcher where only ``*`` has special meaning."""

    pattern_index = 0
    value_index = 0
    star_index = -1
    retry_value_index = -1
    while value_index < len(value):
        if pattern_index < len(pattern) and pattern[pattern_index] == value[value_index]:
            pattern_index += 1
            value_index += 1
            continue
        if pattern_index < len(pattern) and pattern[pattern_index] == "*":
            star_index = pattern_index
            retry_value_index = value_index
            pattern_index += 1
            continue
        if star_index >= 0:
            retry_value_index += 1
            value_index = retry_value_index
            pattern_index = star_index + 1
            continue
        return False
    while pattern_index < len(pattern) and pattern[pattern_index] == "*":
        pattern_index += 1
    return pattern_index == len(pattern)


@dataclass(frozen=True, slots=True)
class _Policy:
    rules: RobotsRules | None
    default_allowed: bool
    reason: RobotsDecisionReason
    status_code: int = 0

    def decide(self, url: str, *, cache_hit: bool) -> RobotsDecision:
        if self.rules is None:
            return RobotsDecision(
                allowed=self.default_allowed,
                reason=self.reason,
                cache_hit=cache_hit,
                status_code=self.status_code,
            )
        allowed, specificity = self.rules.evaluate(url)
        return RobotsDecision(
            allowed=allowed,
            reason=(
                RobotsDecisionReason.ALLOWED
                if allowed
                else RobotsDecisionReason.EXPLICIT_DISALLOW
            ),
            cache_hit=cache_hit,
            status_code=self.status_code,
            matched_specificity=specificity,
        )


@dataclass(frozen=True, slots=True)
class _FetchedPolicy:
    policy: _Policy
    ttl_s: int


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    policy: _Policy
    expires_at: float


@dataclass(slots=True)
class _Flight:
    task: asyncio.Task[_Policy]
    waiters: int = 0


class _RobotsBodyTooLargeError(RuntimeError):
    pass


class RobotsPolicyService:
    """Fetch, parse, cache, and evaluate robots policy for page URLs."""

    def __init__(
        self,
        config: RobotsPolicyConfig | None = None,
        *,
        client_provider: ClientProvider = get_http_client,
        validate_url: UrlValidator = validate_public_url,
        validate_peer: PeerValidator = validate_response_peer,
        acquire_rate_limit: RateAcquire | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        self.config = config or RobotsPolicyConfig.from_settings(settings)
        self._client_provider = client_provider
        self._validate_url = validate_url
        self._validate_peer = validate_peer
        self._acquire_rate_limit = acquire_rate_limit or _acquire_rate_limit
        self._clock = clock
        self._cache: OrderedDict[tuple[str, str], _CacheEntry] = OrderedDict()
        self._flights: dict[tuple[str, str], _Flight] = {}
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(self.config.max_concurrency)
        self._closed = False

    async def check(self, url: str) -> RobotsDecision:
        """Return policy for ``url`` without ever fetching the page itself."""

        try:
            canonical = canonicalize_url(url, max_length=self.config.max_url_length)
            origin, robots_url = _robots_coordinates(canonical)
        except (UrlCanonicalizationError, ValueError):
            return RobotsDecision(
                allowed=False,
                reason=RobotsDecisionReason.UNSAFE,
            )
        target = urlsplit(canonical)
        if target.path == "/robots.txt" and not target.query:
            # The policy resource itself is implicitly allowed by RFC 9309 and
            # therefore must not depend on successfully fetching itself first.
            return RobotsDecision(
                allowed=True,
                reason=RobotsDecisionReason.ALLOWED,
            )

        key = (origin, _product_token(self.config.user_agent))
        now = self._clock()
        async with self._lock:
            if self._closed:
                return RobotsDecision(
                    allowed=False,
                    reason=RobotsDecisionReason.NETWORK_ERROR,
                )
            cached = self._cache.get(key)
            if cached is not None:
                if cached.expires_at > now:
                    self._cache.move_to_end(key)
                    return cached.policy.decide(canonical, cache_hit=True)
                self._cache.pop(key, None)

            flight = self._flights.get(key)
            if flight is None:
                task = asyncio.create_task(
                    self._fetch_and_cache(key, robots_url),
                    name="robots-policy-fetch",
                )
                flight = _Flight(task=task)
                self._flights[key] = flight
            flight.waiters += 1

        try:
            policy = await asyncio.shield(flight.task)
            return policy.decide(canonical, cache_hit=False)
        finally:
            await self._release_waiter(key, flight)

    async def _fetch_and_cache(
        self,
        key: tuple[str, str],
        robots_url: str,
    ) -> _Policy:
        try:
            fetched = await self._fetch_policy(robots_url)
            async with self._lock:
                if not self._closed:
                    self._cache[key] = _CacheEntry(
                        policy=fetched.policy,
                        expires_at=self._clock() + fetched.ttl_s,
                    )
                    self._cache.move_to_end(key)
                    while len(self._cache) > self.config.cache_max_entries:
                        self._cache.popitem(last=False)
            return fetched.policy
        finally:
            current = asyncio.current_task()
            async with self._lock:
                flight = self._flights.get(key)
                if flight is not None and flight.task is current:
                    self._flights.pop(key, None)

    async def _release_waiter(
        self,
        key: tuple[str, str],
        flight: _Flight,
    ) -> None:
        drain: asyncio.Task[_Policy] | None = None
        async with self._lock:
            if self._flights.get(key) is not flight:
                return
            flight.waiters = max(0, flight.waiters - 1)
            if flight.waiters == 0 and not flight.task.done():
                flight.task.cancel()
                drain = flight.task
        if drain is not None:
            await asyncio.gather(drain, return_exceptions=True)

    async def _fetch_policy(self, robots_url: str) -> _FetchedPolicy:
        try:
            async with self._semaphore, asyncio.timeout(self.config.timeout_s):
                return await self._fetch_policy_within_deadline(robots_url)
        except TimeoutError:
            return self._deny_error(RobotsDecisionReason.TIMEOUT)
        except _RobotsBodyTooLargeError:
            return self._deny_error(RobotsDecisionReason.RESPONSE_TOO_LARGE)
        except httpx.HTTPError:
            return self._deny_error(RobotsDecisionReason.NETWORK_ERROR)
        except Exception:
            # Dependency/client failures are policy failures, never bypasses.
            # CancelledError inherits BaseException and still propagates.
            return self._deny_error(RobotsDecisionReason.NETWORK_ERROR)

    async def _fetch_policy_within_deadline(self, robots_url: str) -> _FetchedPolicy:
        client = self._client_provider()
        current = robots_url
        for redirect_count in range(self.config.max_redirects + 1):
            unsafe = await self._validate_url(current)
            if unsafe:
                return self._deny_error(RobotsDecisionReason.UNSAFE)

            await self._acquire_rate_limit(current)
            async with client.stream(
                "GET",
                current,
                follow_redirects=False,
                headers={
                    "User-Agent": self.config.user_agent,
                    "Accept": "text/plain,*/*;q=0.1",
                    "Accept-Encoding": "identity",
                },
            ) as response:
                if self._validate_peer(response):
                    return self._deny_error(RobotsDecisionReason.UNSAFE)
                status = response.status_code
                if status in _REDIRECT_STATUSES:
                    location = response.headers.get("location", "")
                    if not location:
                        return self._deny_error(
                            RobotsDecisionReason.PROTOCOL_ERROR,
                            status_code=status,
                        )
                    if redirect_count >= self.config.max_redirects:
                        return self._deny_error(
                            RobotsDecisionReason.TOO_MANY_REDIRECTS,
                            status_code=status,
                        )
                    redirected = self._validated_redirect_target(current, location)
                    if redirected is None:
                        return self._deny_error(
                            RobotsDecisionReason.UNSAFE,
                            status_code=status,
                        )
                    current = redirected
                    continue

                if 200 <= status < 300:
                    body = await self._read_success_body(response)
                    try:
                        rules = parse_robots_txt(
                            body,
                            user_agent=self.config.user_agent,
                            max_rules=self.config.max_rules,
                            max_records=self.config.max_records,
                            max_line_chars=self.config.max_line_chars,
                        )
                    except RobotsParseLimitError:
                        return self._deny_error(
                            RobotsDecisionReason.POLICY_TOO_COMPLEX,
                            status_code=status,
                        )
                    return _FetchedPolicy(
                        policy=_Policy(
                            rules=rules,
                            default_allowed=True,
                            reason=RobotsDecisionReason.ALLOWED,
                            status_code=status,
                        ),
                        ttl_s=self.config.cache_ttl_s,
                    )
                if status in {404, 410}:
                    return self._allow_unavailable(
                        RobotsDecisionReason.MISSING,
                        status_code=status,
                    )
                if status in _TRANSIENT_CLIENT_STATUSES:
                    return self._deny_error(
                        RobotsDecisionReason.TRANSIENT_CLIENT_ERROR,
                        status_code=status,
                    )
                if 400 <= status < 500:
                    return self._allow_unavailable(
                        RobotsDecisionReason.CLIENT_ERROR,
                        status_code=status,
                    )
                if 500 <= status < 600:
                    return self._deny_error(
                        RobotsDecisionReason.SERVER_ERROR,
                        status_code=status,
                    )
                return self._deny_error(
                    RobotsDecisionReason.PROTOCOL_ERROR,
                    status_code=status,
                )

        return self._deny_error(RobotsDecisionReason.TOO_MANY_REDIRECTS)

    async def _read_success_body(self, response: httpx.Response) -> bytes:
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > self.config.max_body_bytes:
                    raise _RobotsBodyTooLargeError
            except ValueError:
                pass

        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > self.config.max_body_bytes:
                raise _RobotsBodyTooLargeError
            chunks.append(chunk)
        return b"".join(chunks)

    def _validated_redirect_target(self, current: str, location: str) -> str | None:
        try:
            target = canonicalize_url(
                urljoin(current, location),
                max_length=self.config.max_url_length,
            )
        except (UrlCanonicalizationError, ValueError):
            return None
        # Never allow an HTTPS robots policy to silently downgrade to plaintext.
        if urlsplit(current).scheme == "https" and urlsplit(target).scheme != "https":
            return None
        return target

    def _allow_unavailable(
        self,
        reason: RobotsDecisionReason,
        *,
        status_code: int,
    ) -> _FetchedPolicy:
        return _FetchedPolicy(
            policy=_Policy(
                rules=None,
                default_allowed=True,
                reason=reason,
                status_code=status_code,
            ),
            ttl_s=self.config.unavailable_cache_ttl_s,
        )

    def _deny_error(
        self,
        reason: RobotsDecisionReason,
        *,
        status_code: int = 0,
    ) -> _FetchedPolicy:
        return _FetchedPolicy(
            policy=_Policy(
                rules=None,
                default_allowed=False,
                reason=reason,
                status_code=status_code,
            ),
            ttl_s=self.config.error_cache_ttl_s,
        )

    async def close(self) -> None:
        """Cancel and drain outstanding policy work, then clear cached state."""

        async with self._lock:
            self._closed = True
            tasks = [flight.task for flight in self._flights.values()]
            self._cache.clear()
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._lock:
            self._flights.clear()


def _robots_coordinates(url: str) -> tuple[str, str]:
    split = urlsplit(url)
    if not split.scheme or not split.netloc:
        raise ValueError("canonical URL has no origin")
    origin = urlunsplit((split.scheme, split.netloc, "", "", ""))
    return origin, f"{origin}/robots.txt"


async def _acquire_rate_limit(url: str) -> None:
    await get_rate_limiter().acquire(url)


_robots_policy: RobotsPolicyService | None = None
_robots_policy_loop: asyncio.AbstractEventLoop | None = None


def get_robots_policy() -> RobotsPolicyService:
    """Return the policy service bound to the current application event loop."""

    global _robots_policy, _robots_policy_loop
    loop = asyncio.get_running_loop()
    if _robots_policy is None or _robots_policy_loop is not loop:
        _robots_policy = RobotsPolicyService()
        _robots_policy_loop = loop
    return _robots_policy


async def close_robots_policy() -> None:
    """Close the loop-local robots service during application shutdown."""

    global _robots_policy, _robots_policy_loop
    policy = _robots_policy
    _robots_policy = None
    _robots_policy_loop = None
    if policy is not None:
        await policy.close()

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services.robots import (
    RobotsDecisionReason,
    RobotsParseLimitError,
    RobotsPolicyConfig,
    RobotsPolicyService,
    parse_robots_txt,
    robots_blocked_error,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def _allows(body: bytes, url: str, *, user_agent: str = "ClusyCrawler/1.0") -> bool:
    rules = parse_robots_txt(body, user_agent=user_agent)
    allowed, _specificity = rules.evaluate(url)
    return allowed


def test_specific_user_agent_groups_replace_star_fallback_and_are_merged():
    body = b"""
        User-agent: *
        Disallow: /

        User-agent: ClusyCrawler
        Disallow: /private

        User-agent: clusycrawler/2.0
        Allow: /private/public
    """

    assert _allows(body, "https://example.com/ordinary")
    assert not _allows(body, "https://example.com/private/record")
    assert _allows(body, "https://example.com/private/public/index")


def test_star_group_is_used_when_configured_product_has_no_exact_group():
    body = b"""
        User-agent: OtherCrawler
        Disallow: /
        User-agent: *
        Disallow: /private
    """

    assert _allows(body, "https://example.com/public")
    assert not _allows(body, "https://example.com/private")


def test_longest_match_and_equal_specificity_allow_tie_are_deterministic():
    body = b"""
        User-agent: *
        Disallow: /catalog/
        Allow: /catalog/public/
        Disallow: /same
        Allow: /same
    """

    assert not _allows(body, "https://example.com/catalog/private/item")
    assert _allows(body, "https://example.com/catalog/public/item")
    assert _allows(body, "https://example.com/same/path")


def test_wildcard_end_anchor_query_and_percent_octets_are_supported():
    body = b"""
        User-agent: *
        Disallow: /*.pdf$
        Disallow: /search?*secret=
        Disallow: /caf%C3%A9
        Allow: /caf%C3%A9/menu
        Disallow: /encoded%7Eprivate
    """

    assert not _allows(body, "https://example.com/report.pdf")
    assert _allows(body, "https://example.com/report.pdf?download=1")
    assert not _allows(body, "https://example.com/search?q=x&secret=yes")
    assert not _allows(body, "https://example.com/caf%C3%A9")
    assert _allows(body, "https://example.com/caf%C3%A9/menu/today")
    assert not _allows(body, "https://example.com/encoded~private")


def test_empty_disallow_comments_bom_and_path_case_follow_robots_semantics():
    body = (
        b"\xef\xbb\xbfUser-agent: * # selected\n"
        b"Disallow:\n"
        b"Disallow: /CaseSensitive # comment\n"
    )

    assert not _allows(body, "https://example.com/CaseSensitive")
    assert _allows(body, "https://example.com/casesensitive")
    assert _allows(body, "https://example.com/anything")


def test_blank_lines_inside_a_group_do_not_drop_later_disallow():
    body = b"""
        User-agent: *
        Allow: /public

        Disallow: /private
    """

    assert _allows(body, "https://example.com/public")
    assert not _allows(body, "https://example.com/private")


def test_origin_robots_uri_is_implicitly_allowed_even_under_root_disallow():
    body = b"User-agent: *\nDisallow: /\n"

    assert _allows(body, "https://example.com/robots.txt")
    assert not _allows(body, "https://example.com/robots.txt?alternate=1")


@pytest.mark.parametrize(
    ("kwargs", "body"),
    [
        (
            {"max_rules": 1},
            b"User-agent: *\nDisallow: /one\nDisallow: /two\n",
        ),
        (
            {"max_records": 1},
            b"User-agent: *\nDisallow: /one\n",
        ),
        (
            {"max_line_chars": 10},
            b"User-agent: *\n",
        ),
    ],
)
def test_parser_limits_fail_closed_instead_of_silently_dropping_rules(kwargs, body):
    with pytest.raises(RobotsParseLimitError):
        parse_robots_txt(body, user_agent="ClusyCrawler/1.0", **kwargs)


def _config(**overrides: Any) -> RobotsPolicyConfig:
    values: dict[str, Any] = {
        "timeout_s": 1,
        "max_redirects": 2,
        "max_body_bytes": 512 * 1024,
        "max_url_length": 4096,
        "max_rules": 100,
        "max_records": 200,
        "max_line_chars": 256,
        "max_concurrency": 4,
        "cache_max_entries": 8,
        "cache_ttl_s": 100,
        "unavailable_cache_ttl_s": 20,
        "error_cache_ttl_s": 5,
        "user_agent": "ClusyCrawler/9.0",
    }
    values.update(overrides)
    return RobotsPolicyConfig(**values)


def test_body_budget_cannot_be_configured_below_rfc_500_kib_floor():
    with pytest.raises(ValueError, match="500 KiB"):
        _config(max_body_bytes=(500 * 1024) - 1)
    with pytest.raises(ValidationError):
        Settings(robots_max_body_bytes=(500 * 1024) - 1)


async def _public(_url: str) -> str | None:
    return None


async def _no_rate_limit(_url: str) -> None:
    return None


def _service(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    config: RobotsPolicyConfig | None = None,
    validator: Callable[[str], Any] = _public,
    peer_validator: Callable[[httpx.Response], str | None] = lambda _response: None,
    clock: Callable[[], float] | None = None,
) -> tuple[RobotsPolicyService, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = RobotsPolicyService(
        config or _config(),
        client_provider=lambda: client,
        validate_url=validator,
        validate_peer=peer_validator,
        acquire_rate_limit=_no_rate_limit,
        clock=clock or (lambda: 0.0),
    )
    return service, client


async def _close(service: RobotsPolicyService, client: httpx.AsyncClient) -> None:
    await service.close()
    await client.aclose()


async def test_success_fetch_uses_origin_robots_path_configured_ua_and_cached_rules():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=b"User-agent: ClusyCrawler\nDisallow: /private\n",
        )

    service, client = _service(handler)
    try:
        first = await service.check("https://example.com/private/a?token=secret")
        second = await service.check("https://example.com/public")
    finally:
        await _close(service, client)

    assert not first.allowed
    assert first.reason is RobotsDecisionReason.EXPLICIT_DISALLOW
    assert "explicitly disallows" in robots_blocked_error(first)
    assert second.allowed
    assert second.cache_hit
    assert len(requests) == 1
    assert str(requests[0].url) == "https://example.com/robots.txt"
    assert requests[0].headers["user-agent"] == "ClusyCrawler/9.0"
    assert requests[0].headers["accept-encoding"] == "identity"


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (404, RobotsDecisionReason.MISSING),
        (410, RobotsDecisionReason.MISSING),
        (401, RobotsDecisionReason.CLIENT_ERROR),
        (403, RobotsDecisionReason.CLIENT_ERROR),
    ],
)
async def test_missing_and_non_transient_4xx_allow_per_documented_rfc_policy(status, reason):
    service, client = _service(lambda _request: httpx.Response(status))
    try:
        decision = await service.check("https://example.com/page")
    finally:
        await _close(service, client)

    assert decision.allowed
    assert decision.reason is reason
    assert decision.status_code == status


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (408, RobotsDecisionReason.TRANSIENT_CLIENT_ERROR),
        (429, RobotsDecisionReason.TRANSIENT_CLIENT_ERROR),
        (500, RobotsDecisionReason.SERVER_ERROR),
        (503, RobotsDecisionReason.SERVER_ERROR),
    ],
)
async def test_transient_statuses_fail_closed_without_retry(status, reason):
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status)

    service, client = _service(handler)
    try:
        decision = await service.check("https://example.com/page")
    finally:
        await _close(service, client)

    assert not decision.allowed
    assert decision.reason is reason
    assert decision.status_code == status
    assert calls == 1


async def test_timeout_fails_closed_and_is_not_retried():
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(1)
        return httpx.Response(200, content=b"User-agent: *\nAllow: /\n")

    service, client = _service(handler, config=_config(timeout_s=0.01))
    try:
        decision = await service.check("https://example.com/page")
    finally:
        await _close(service, client)

    assert not decision.allowed
    assert decision.reason is RobotsDecisionReason.TIMEOUT
    assert calls == 1


async def test_redirects_are_manual_bounded_and_each_hop_is_ssrf_validated():
    requests: list[str] = []
    validated: list[str] = []

    async def validator(url: str) -> str | None:
        validated.append(url)
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "https://policy.example/robots"})
        return httpx.Response(200, content=b"User-agent: *\nDisallow: /private\n")

    service, client = _service(handler, validator=validator)
    try:
        decision = await service.check("https://example.com/private")
    finally:
        await _close(service, client)

    assert not decision.allowed
    assert requests == [
        "https://example.com/robots.txt",
        "https://policy.example/robots",
    ]
    assert validated == requests


async def test_unsafe_redirect_is_rejected_before_second_request():
    requests: list[str] = []

    async def validator(url: str) -> str | None:
        if "metadata.internal" in url:
            return "unsafe"
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "http://metadata.internal/latest"},
        )

    service, client = _service(handler, validator=validator)
    try:
        decision = await service.check("http://example.com/page")
    finally:
        await _close(service, client)

    assert not decision.allowed
    assert decision.reason is RobotsDecisionReason.UNSAFE
    assert requests == ["http://example.com/robots.txt"]


async def test_https_to_http_redirect_and_redirect_userinfo_are_rejected():
    locations = iter(
        [
            "http://example.com/robots.txt",
            "https://user:secret@example.com/robots.txt",
        ]
    )

    for location in locations:
        calls = 0

        def handler(_request: httpx.Request, *, target: str = location) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(302, headers={"location": target})

        service, client = _service(handler)
        try:
            decision = await service.check("https://example.com/page")
        finally:
            await _close(service, client)
        assert not decision.allowed
        assert decision.reason is RobotsDecisionReason.UNSAFE
        assert calls == 1


async def test_redirect_limit_is_exact_and_does_not_issue_an_extra_request():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": f"/robots-{calls}"})

    service, client = _service(handler, config=_config(max_redirects=1))
    try:
        decision = await service.check("https://example.com/page")
    finally:
        await _close(service, client)

    assert not decision.allowed
    assert decision.reason is RobotsDecisionReason.TOO_MANY_REDIRECTS
    assert calls == 2


@pytest.mark.parametrize("declared", [True, False])
async def test_declared_and_streamed_body_limits_fail_closed(declared):
    body_limit = 500 * 1024
    content = b"User-agent: *\n" + (b"Allow: /x\n" * ((body_limit // 10) + 1))
    headers = {"content-length": str(len(content))} if declared else {}
    service, client = _service(
        lambda _request: httpx.Response(200, headers=headers, content=content),
        config=_config(max_body_bytes=body_limit),
    )
    try:
        decision = await service.check("https://example.com/page")
    finally:
        await _close(service, client)

    assert not decision.allowed
    assert decision.reason is RobotsDecisionReason.RESPONSE_TOO_LARGE


async def test_peer_rebinding_failure_is_denied_before_body_use():
    service, client = _service(
        lambda _request: httpx.Response(200, content=b"User-agent: *\nAllow: /\n"),
        peer_validator=lambda _response: "unsafe peer",
    )
    try:
        decision = await service.check("https://example.com/page")
    finally:
        await _close(service, client)

    assert not decision.allowed
    assert decision.reason is RobotsDecisionReason.UNSAFE


async def test_cache_ttl_and_lru_entry_bound_are_enforced():
    now = [0.0]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host or "")
        return httpx.Response(404)

    service, client = _service(
        handler,
        config=_config(cache_max_entries=2, unavailable_cache_ttl_s=10),
        clock=lambda: now[0],
    )
    try:
        await service.check("https://one.example/a")
        await service.check("https://two.example/a")
        await service.check("https://one.example/b")  # refresh LRU position
        await service.check("https://three.example/a")  # evicts two
        await service.check("https://two.example/b")
        now[0] = 11
        await service.check("https://one.example/c")
    finally:
        await _close(service, client)

    assert calls == [
        "one.example",
        "two.example",
        "three.example",
        "two.example",
        "one.example",
    ]


async def test_concurrent_same_origin_checks_singleflight_and_one_waiter_can_cancel():
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return httpx.Response(200, content=b"User-agent: *\nDisallow: /private\n")

    service, client = _service(handler)
    first = asyncio.create_task(service.check("https://example.com/private/a"))
    second = asyncio.create_task(service.check("https://example.com/private/b"))
    await asyncio.wait_for(started.wait(), timeout=1)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    try:
        second_decision = await asyncio.wait_for(second, timeout=1)
    finally:
        await _close(service, client)

    assert not second_decision.allowed
    assert calls == 1


async def test_last_waiter_cancellation_cancels_and_drains_policy_fetch():
    started = asyncio.Event()
    finished = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()
        raise AssertionError("unreachable")

    service, client = _service(handler)
    check = asyncio.create_task(service.check("https://example.com/page"))
    await asyncio.wait_for(started.wait(), timeout=1)
    check.cancel()
    with pytest.raises(asyncio.CancelledError):
        await check
    try:
        await asyncio.wait_for(finished.wait(), timeout=1)
        assert not service._flights
    finally:
        await _close(service, client)


async def test_invalid_url_is_denied_without_any_network_request():
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    service, client = _service(handler)
    try:
        decision = await service.check("http://user:secret@example.com/page")
    finally:
        await _close(service, client)

    assert not decision.allowed
    assert decision.reason is RobotsDecisionReason.UNSAFE
    assert calls == 0


async def test_exact_origin_robots_uri_is_allowed_without_self_preflight():
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    service, client = _service(handler)
    try:
        decision = await service.check("https://example.com/robots.txt")
    finally:
        await _close(service, client)

    assert decision.allowed
    assert decision.reason is RobotsDecisionReason.ALLOWED
    assert calls == 0

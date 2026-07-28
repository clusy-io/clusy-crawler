from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.frontier import (
    CrawlFrontier,
    FrontierConfig,
    FrontierState,
    RejectionReason,
    StaleLeaseError,
    TerminalReason,
    UrlCanonicalizationError,
    canonicalize_url,
)


def test_canonicalization_is_conservative_and_idna_aware():
    assert (
        canonicalize_url(
            "HTTP://BÜCHER.Example.:80/a/./b/../%7e?q=b&a=1#ignored",
        )
        == "http://xn--bcher-kva.example/a/~?q=b&a=1"
    )
    assert (
        canonicalize_url(
            "http://[2001:0db8:0:0::1]:80/a",
        )
        == "http://[2001:db8::1]/a"
    )
    assert canonicalize_url("https://example.com:80/") == "https://example.com:80/"
    assert canonicalize_url("https://example.com/a%2fb") == "https://example.com/a%2Fb"
    assert canonicalize_url("https://example.com/a//b/../c") == "https://example.com/a//c"


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("ftp://example.com/file", RejectionReason.UNSUPPORTED_SCHEME),
        ("https://user:secret@example.com/", RejectionReason.USERINFO_NOT_ALLOWED),
        ("https://example.com:/", RejectionReason.INVALID_URL),
        ("https://example.com../", RejectionReason.INVALID_URL),
        ("https://exa%mple.com/", RejectionReason.INVALID_URL),
        ("http://[fe80::1%25eth0]/", RejectionReason.INVALID_URL),
        ("http://[v1.example]/", RejectionReason.INVALID_URL),
        (" https://example.com/", RejectionReason.INVALID_URL),
        ("https://example.com/%zz", RejectionReason.INVALID_URL),
        ("https://example.com/\ud800", RejectionReason.INVALID_URL),
    ],
)
def test_canonicalization_rejects_ambiguous_or_unsafe_authorities(url, reason):
    with pytest.raises(UrlCanonicalizationError) as exc_info:
        canonicalize_url(url)
    assert exc_info.value.reason is reason


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/",
        "http://xn--bcher-kva.example/a/~?q=b&a=1",
        "http://[2001:db8::1]/a",
        "https://example.com/a//c",
        "https://example.com/a%2Fb?x=1&x=2",
    ],
)
def test_canonicalization_is_idempotent(url):
    assert canonicalize_url(canonicalize_url(url)) == canonicalize_url(url)


def test_dedup_strips_only_response_irrelevant_syntax_and_preserves_query_order():
    frontier = CrawlFrontier(["https://Example.com:443/root#seed"])

    duplicate = frontier.admit(
        "https://example.com:443/root#different",
        depth=1,
        priority=10,
    )
    first_order = frontier.admit("https://example.com/items?a=1&b=2", depth=1)
    second_order = frontier.admit("https://example.com/items?b=2&a=1", depth=1)

    assert duplicate.reason is RejectionReason.DUPLICATE
    assert duplicate.reprioritized
    assert first_order.accepted
    assert second_order.accepted
    assert first_order.url != second_order.url
    assert frontier.metrics().duplicates == 1
    assert frontier.metrics().admission_attempts == 4


def test_duplicate_can_improve_priority_and_depth_without_delaying_existing_work():
    frontier = CrawlFrontier(
        ["https://example.com/"],
        config=FrontierConfig(host_delay_s=0),
    )
    accepted = frontier.admit(
        "https://example.com/article",
        depth=3,
        priority=1,
        ready_at=0,
    )
    duplicate = frontier.admit(
        "https://EXAMPLE.com:443/article#section",
        depth=1,
        priority=50,
        parent_url="https://example.com/better-parent",
        ready_at=20,
    )

    assert accepted.accepted
    assert duplicate.reason is RejectionReason.DUPLICATE
    assert duplicate.reprioritized
    lease = frontier.claim(now=0)
    assert lease is not None
    assert lease.url == "https://example.com/article"
    assert lease.priority == 50
    assert lease.depth == 1


def test_site_scope_uses_label_boundaries_and_configurable_subdomains():
    exact = CrawlFrontier(["https://example.com/"])
    assert exact.admit("https://docs.example.com/", depth=1).reason is RejectionReason.OFF_SITE

    subtree = CrawlFrontier(
        ["https://example.com/"],
        config=FrontierConfig(allow_subdomains=True),
    )
    assert subtree.admit("https://docs.example.com/", depth=1).accepted
    assert (
        subtree.admit("https://example.com.attacker.test/", depth=1).reason
        is RejectionReason.OFF_SITE
    )
    assert subtree.admit("https://notexample.com/", depth=1).reason is RejectionReason.OFF_SITE


def test_depth_host_and_global_url_budgets_are_explicit():
    host_limited = CrawlFrontier(
        ["https://a.example/"],
        config=FrontierConfig(max_depth=1, max_urls=10, max_urls_per_host=2),
    )
    assert host_limited.admit("https://a.example/one", depth=1).accepted
    assert (
        host_limited.admit("https://a.example/two", depth=1).reason
        is RejectionReason.HOST_URL_BUDGET
    )
    assert (
        host_limited.admit("https://a.example/deep", depth=2).reason is RejectionReason.DEPTH_BUDGET
    )

    globally_limited = CrawlFrontier(
        ["https://a.example/", "https://b.example/"],
        config=FrontierConfig(max_urls=2, max_urls_per_host=2),
    )
    assert (
        globally_limited.admit("https://a.example/extra", depth=1).reason
        is RejectionReason.GLOBAL_URL_BUDGET
    )


def test_repeated_path_and_session_traps_catch_encoded_variants():
    frontier = CrawlFrontier(
        ["https://example.com/"],
        config=FrontierConfig(max_repeated_path_segment=2),
    )

    assert (
        frontier.admit("https://example.com/a/%61/a/end", depth=1).reason
        is RejectionReason.TRAP_REPEATED_PATH
    )
    assert (
        frontier.admit(
            "https://example.com/page?PHP%53ESSID=abc",
            depth=1,
        ).reason
        is RejectionReason.TRAP_SESSION
    )
    assert (
        frontier.admit(
            "https://example.com/page;jsessionid=abc",
            depth=1,
        ).reason
        is RejectionReason.TRAP_SESSION
    )
    assert (
        frontier.admit(
            "https://example.com/page?x=1;sessionid=abc",
            depth=1,
        ).reason
        is RejectionReason.TRAP_SESSION
    )
    assert frontier.admit("https://example.com/story?sid=42", depth=1).accepted


def test_query_parameter_facet_and_variant_traps_do_not_rewrite_queries():
    parameter_frontier = CrawlFrontier(
        ["https://example.com/"],
        config=FrontierConfig(max_query_parameters=2, max_repeated_query_key=2),
    )
    assert (
        parameter_frontier.admit(
            "https://example.com/search?a=1&a=2&a=3",
            depth=1,
        ).reason
        is RejectionReason.TRAP_QUERY_PARAMETERS
    )

    facet_frontier = CrawlFrontier(
        ["https://example.com/"],
        config=FrontierConfig(max_facet_parameters=1),
    )
    assert (
        facet_frontier.admit(
            "https://example.com/products?filter[color]=red&sort=price",
            depth=1,
        ).reason
        is RejectionReason.TRAP_FACETS
    )

    variant_frontier = CrawlFrontier(
        ["https://example.com/"],
        config=FrontierConfig(max_query_variants_per_path=2),
    )
    assert variant_frontier.admit("https://example.com/items?a=1&b=2", depth=1).accepted
    assert variant_frontier.admit("https://example.com/items?b=2&a=1", depth=1).accepted
    assert (
        variant_frontier.admit("https://example.com/items?a=2&b=1", depth=1).reason
        is RejectionReason.TRAP_QUERY_VARIANTS
    )


def test_calendar_traps_bound_years_and_structural_variants():
    frontier = CrawlFrontier(
        ["https://example.com/"],
        config=FrontierConfig(
            max_calendar_variants_per_pattern=2,
            min_calendar_year=2020,
            max_calendar_year=2030,
        ),
    )
    assert frontier.admit("https://example.com/calendar/2025/01", depth=1).accepted
    assert frontier.admit("https://example.com/calendar/2025/02", depth=1).accepted
    assert (
        frontier.admit("https://example.com/calendar/2025/03", depth=1).reason
        is RejectionReason.TRAP_CALENDAR
    )
    assert (
        frontier.admit("https://example.com/events?year=9999", depth=1).reason
        is RejectionReason.TRAP_CALENDAR
    )
    assert frontier.admit("https://example.com/articles/1980/history", depth=1).accepted


def test_priority_scheduling_and_equal_priority_host_fairness_are_deterministic():
    frontier = CrawlFrontier(
        ["https://a.example/root", "https://b.example/root"],
        config=FrontierConfig(host_delay_s=0),
    )
    assert frontier.admit("https://a.example/high", depth=1, priority=10).accepted
    assert frontier.admit("https://b.example/high", depth=1, priority=10).accepted

    claimed: list[str] = []
    for _index in range(4):
        lease = frontier.claim(now=0)
        assert lease is not None
        claimed.append(lease.url)
        frontier.succeed(lease)

    assert claimed == [
        "https://a.example/high",
        "https://b.example/high",
        "https://a.example/root",
        "https://b.example/root",
    ]


def test_host_round_robin_prevents_one_origin_from_starving_another():
    frontier = CrawlFrontier(
        ["https://a.example/root", "https://b.example/root"],
        config=FrontierConfig(host_delay_s=0),
    )
    assert frontier.admit("https://a.example/high-1", depth=1, priority=100).accepted
    assert frontier.admit("https://a.example/high-2", depth=1, priority=100).accepted

    first = frontier.claim(now=0)
    assert first is not None
    assert first.url == "https://a.example/high-1"
    frontier.succeed(first)

    second = frontier.claim(now=0)
    assert second is not None
    assert second.url == "https://b.example/root"


def test_host_politeness_exposes_next_wake_time():
    frontier = CrawlFrontier(
        ["https://example.com/one"],
        config=FrontierConfig(host_delay_s=1.5),
    )
    assert frontier.admit("https://example.com/two", depth=1).accepted

    first = frontier.claim(now=10)
    assert first is not None
    frontier.succeed(first)
    assert frontier.claim(now=11.49) is None
    assert frontier.next_wake_at(now=11.49) == pytest.approx(11.5)

    second = frontier.claim(now=11.5)
    assert second is not None
    assert second.url == "https://example.com/two"


def test_retry_after_and_backoff_are_bounded_and_exhaustion_is_terminal():
    frontier = CrawlFrontier(
        ["https://example.com/"],
        config=FrontierConfig(
            host_delay_s=0,
            max_attempts_per_url=2,
            retry_backoff_base_s=2,
            max_retry_delay_s=10,
        ),
    )
    first = frontier.claim(now=0)
    assert first is not None
    retry = frontier.fail(first, now=0, retryable=True, retry_after="9999")

    assert retry.retry_scheduled
    assert retry.ready_at == 10
    assert frontier.claim(now=9.99) is None
    assert frontier.next_wake_at(now=9.99) == 10

    second = frontier.claim(now=10)
    assert second is not None
    assert second.attempt == 2
    exhausted = frontier.fail(second, now=10, retryable=True)
    assert not exhausted.retry_scheduled
    assert exhausted.terminal_reason is TerminalReason.RETRY_EXHAUSTED
    assert frontier.metrics().terminal_reasons[TerminalReason.RETRY_EXHAUSTED] == 1


def test_http_date_retry_after_uses_explicit_wall_clock_and_is_capped():
    frontier = CrawlFrontier(
        ["https://example.com/"],
        config=FrontierConfig(
            host_delay_s=0,
            retry_backoff_base_s=1,
            max_retry_delay_s=30,
        ),
    )
    lease = frontier.claim(now=100)
    assert lease is not None
    result = frontier.fail(
        lease,
        now=100,
        retryable=True,
        retry_after="Wed, 21 Oct 2015 07:29:30 GMT",
        wall_now=datetime(2015, 10, 21, 7, 28, tzinfo=UTC),
    )
    assert result.ready_at == 130


def test_global_fetch_budget_terminalizes_unclaimed_work_with_reason():
    frontier = CrawlFrontier(
        ["https://a.example/", "https://b.example/"],
        config=FrontierConfig(
            host_delay_s=0,
            max_fetch_attempts=1,
        ),
    )
    lease = frontier.claim(now=0)
    assert lease is not None
    frontier.succeed(lease)

    assert frontier.claim(now=0) is None
    remaining = [record for record in frontier.records() if record.url != lease.url]
    assert len(remaining) == 1
    assert remaining[0].state is FrontierState.TERMINAL
    assert remaining[0].terminal_reason is TerminalReason.GLOBAL_FETCH_BUDGET

    metrics = frontier.metrics()
    assert metrics.claimed == 1
    assert metrics.terminal == 2
    assert metrics.terminal_reasons[TerminalReason.SUCCEEDED] == 1
    assert metrics.terminal_reasons[TerminalReason.GLOBAL_FETCH_BUDGET] == 1


def test_host_fetch_budget_does_not_block_other_hosts():
    frontier = CrawlFrontier(
        [
            "https://a.example/one",
            "https://a.example/two",
            "https://b.example/one",
        ],
        config=FrontierConfig(
            host_delay_s=0,
            max_fetch_attempts_per_host=1,
        ),
    )
    first = frontier.claim(now=0)
    assert first is not None
    assert first.host == "a.example"
    frontier.succeed(first)

    second = frontier.claim(now=0)
    assert second is not None
    assert second.host == "b.example"
    frontier.succeed(second)

    assert frontier.claim(now=0) is None
    a_records = [record for record in frontier.records() if record.host == "a.example"]
    assert {record.terminal_reason for record in a_records} == {
        TerminalReason.SUCCEEDED,
        TerminalReason.HOST_FETCH_BUDGET,
    }


def test_stale_leases_cannot_ack_a_later_state():
    frontier = CrawlFrontier(
        ["https://example.com/"],
        config=FrontierConfig(host_delay_s=0),
    )
    lease = frontier.claim(now=0)
    assert lease is not None
    frontier.succeed(lease)

    with pytest.raises(StaleLeaseError):
        frontier.succeed(lease)


def test_non_retryable_failures_and_cancellation_are_accounted():
    frontier = CrawlFrontier(
        ["https://example.com/one"],
        config=FrontierConfig(host_delay_s=0),
    )
    assert frontier.admit("https://example.com/two", depth=1).accepted
    lease = frontier.claim(now=0)
    assert lease is not None
    failed = frontier.fail(
        lease,
        now=0,
        retryable=False,
        terminal_reason=TerminalReason.ROBOTS_DISALLOWED,
    )
    assert failed.terminal_reason is TerminalReason.ROBOTS_DISALLOWED
    assert frontier.cancel_pending() == 1

    metrics = frontier.metrics()
    assert metrics.pending == 0
    assert metrics.terminal_reasons[TerminalReason.ROBOTS_DISALLOWED] == 1
    assert metrics.terminal_reasons[TerminalReason.CANCELLED] == 1

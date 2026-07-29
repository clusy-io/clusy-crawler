from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import orjson
import pytest
from pydantic import ValidationError

from app.config import settings
from app.models.requests import CrawlRequest
from app.models.responses import CrawlResult, ExtractionMetadata
from app.services import crawler as crawler_module
from app.services.frontier import CrawlFrontier, TerminalReason
from app.services.robots import RobotsDecision, RobotsDecisionReason

FakeCrawl = Callable[..., Awaitable[CrawlResult]]


@pytest.fixture(autouse=True)
def _allow_recursive_robots(monkeypatch):
    class AllowAllRobots:
        async def check(self, _url: str) -> RobotsDecision:
            return RobotsDecision(
                allowed=True,
                reason=RobotsDecisionReason.ALLOWED,
            )

    policy = AllowAllRobots()
    monkeypatch.setattr(
        "app.services.robots.get_robots_policy",
        lambda: policy,
    )


def _graph_crawler(
    graph: dict[str, list[str] | Exception],
    calls: list[str],
    *,
    delays: dict[str, float] | None = None,
) -> FakeCrawl:
    async def crawl(
        url: str,
        js_render: bool | None = None,
        wait_for_selector: str | None = None,
        word_count_threshold: int = 10,
        formats: list[str] | None = None,
        max_age: int | None = None,
        extraction_profile: str = "balanced",
        document_policy: Any = None,
    ) -> CrawlResult:
        del (
            js_render,
            wait_for_selector,
            word_count_threshold,
            max_age,
            extraction_profile,
            document_policy,
        )
        assert formats is not None and "links" in formats
        calls.append(url)
        if delays and (delay := delays.get(url)):
            await asyncio.sleep(delay)
        outcome = graph.get(url, [])
        if isinstance(outcome, Exception):
            raise outcome
        return CrawlResult(
            url=url,
            markdown=f"content for {url}",
            html="<html>internal</html>",
            links=list(outcome),
        )

    return crawl


async def test_recursive_success_is_claim_ordered_and_projects_internal_links(
    monkeypatch,
):
    root = "https://example.com/"
    a = "https://example.com/a"
    b = "https://example.com/b"
    c = "https://example.com/c"
    calls: list[str] = []
    monkeypatch.setattr(
        crawler_module,
        "_crawl_single_url",
        _graph_crawler(
            {
                root: [a, b],
                a: [c],
                b: [c],
                c: [],
            },
            calls,
            # B completes first, but retirement and output remain claim ordered.
            delays={a: 0.02},
        ),
    )
    monkeypatch.setattr(crawler_module.settings, "max_concurrent_tasks", 2)

    results = await crawler_module.crawl_urls(
        [root],
        max_depth=2,
        max_pages=10,
        priority=77,
    )

    assert [result.url for result in results] == [root, a, b, c]
    assert calls == [root, a, b, c]
    assert all(result.links is None for result in results)
    assert all(result.html is None for result in results)


async def test_recursive_error_is_returned_once_and_never_retried_or_expanded(
    monkeypatch,
):
    root = "https://example.com/"
    child = "https://example.com/child"
    calls: list[str] = []

    async def error_crawl(**kwargs: Any) -> CrawlResult:
        url = str(kwargs["url"])
        calls.append(url)
        return CrawlResult(
            url=url,
            links=[child],
            error="origin rejected request",
        )

    monkeypatch.setattr(crawler_module, "_crawl_single_url", error_crawl)

    results = await crawler_module.crawl_urls(
        [root],
        max_depth=3,
        max_pages=10,
    )

    assert calls == [root]
    assert len(results) == 1
    assert results[0].error == "origin rejected request"


async def test_recursive_unexpected_child_exception_becomes_one_error_without_retry(
    monkeypatch,
):
    root = "https://example.com/"
    child = "https://example.com/child"
    calls: list[str] = []
    monkeypatch.setattr(
        crawler_module,
        "_crawl_single_url",
        _graph_crawler(
            {
                root: [child],
                child: RuntimeError("transport already exhausted its retries"),
            },
            calls,
        ),
    )

    results = await crawler_module.crawl_urls(
        [root],
        max_depth=1,
        max_pages=5,
    )

    assert calls == [root, child]
    assert [result.url for result in results] == [root, child]
    assert results[1].error == "crawl failed (RuntimeError)"


async def test_recursive_scope_dedupe_and_traps_reject_adversarial_links(
    monkeypatch,
):
    root = "https://example.com/"
    good = "https://example.com/good"
    calls: list[str] = []
    monkeypatch.setattr(
        crawler_module,
        "_crawl_single_url",
        _graph_crawler(
            {
                root: [
                    good,
                    "https://EXAMPLE.com:443/good#duplicate",
                    "https://outside.test/page",
                    "https://example.com.attacker.test/page",
                    "https://user:secret@example.com/private",
                    "https://example.com/page?PHPSESSID=token",
                    "https://example.com/a/a/a/a",
                    "javascript:alert(1)",
                    "https://docs.example.com/subdomain",
                ],
                good: [],
            },
            calls,
        ),
    )

    results = await crawler_module.crawl_urls(
        [root],
        max_depth=3,
        max_pages=20,
        allow_subdomains=False,
    )

    assert calls == [root, good]
    assert [result.url for result in results] == [root, good]


async def test_recursive_completion_logs_bounded_url_free_frontier_metrics(
    monkeypatch,
):
    root = "https://example.com/"
    child = "https://example.com/child"
    events: list[tuple[str, dict[str, Any]]] = []

    class CapturingLogger:
        def info(self, event: str, **fields: Any) -> None:
            events.append((event, fields))

    monkeypatch.setattr(crawler_module, "logger", CapturingLogger())
    monkeypatch.setattr(
        crawler_module,
        "_crawl_single_url",
        _graph_crawler(
            {
                root: [child, "https://outside.test/rejected"],
                child: [],
            },
            [],
        ),
    )

    await crawler_module.crawl_urls(
        [root],
        max_depth=1,
        max_pages=3,
    )

    assert len(events) == 1
    event, fields = events[0]
    assert event == "recursive_crawl_frontier_finished"
    assert fields["outcome"] == "success"
    assert fields["admitted"] == 2
    assert fields["rejected"] == 1
    assert fields["claimed"] == 2
    assert fields["terminal"] == 2
    assert fields["rejection_reasons"] == {"off_site": 1}
    assert fields["terminal_reasons"] == {"succeeded": 2}
    assert fields["robots_disallowed"] == 0
    assert "https://" not in repr(fields)


async def test_recursive_subdomain_policy_is_explicit(monkeypatch):
    root = "https://example.com/"
    subdomain = "https://docs.example.com/page"
    suffix_attack = "https://example.com.attacker.test/page"
    calls: list[str] = []
    monkeypatch.setattr(
        crawler_module,
        "_crawl_single_url",
        _graph_crawler(
            {
                root: [subdomain, suffix_attack],
                subdomain: [],
            },
            calls,
        ),
    )

    results = await crawler_module.crawl_urls(
        [root],
        max_depth=1,
        max_pages=5,
        allow_subdomains=True,
    )

    assert calls == [root, subdomain]
    assert [result.url for result in results] == [root, subdomain]


async def test_recursive_depth_is_enforced_by_frontier_admission(monkeypatch):
    root = "https://example.com/"
    child = "https://example.com/child"
    grandchild = "https://example.com/grandchild"
    calls: list[str] = []
    monkeypatch.setattr(
        crawler_module,
        "_crawl_single_url",
        _graph_crawler(
            {
                root: [child],
                child: [grandchild],
                grandchild: [],
            },
            calls,
        ),
    )

    results = await crawler_module.crawl_urls(
        [root],
        max_depth=1,
        max_pages=10,
    )

    assert calls == [root, child]
    assert [result.url for result in results] == [root, child]


async def test_recursive_max_pages_is_one_budget_across_all_seeds(monkeypatch):
    first_seed = "https://a.example/"
    second_seed = "https://b.example/"
    first_child = "https://a.example/one"
    second_child = "https://b.example/one"
    calls: list[str] = []
    monkeypatch.setattr(
        crawler_module,
        "_crawl_single_url",
        _graph_crawler(
            {
                first_seed: [first_child],
                second_seed: [second_child],
                first_child: [],
                second_child: [],
            },
            calls,
        ),
    )

    results = await crawler_module.crawl_urls(
        [first_seed, second_seed],
        max_depth=2,
        max_pages=3,
    )

    assert len(results) == 3
    assert len(calls) == 3
    assert {first_seed, second_seed}.issubset(calls)


async def test_recursive_owner_never_exceeds_existing_crawl_concurrency(monkeypatch):
    root = "https://example.com/"
    children = [f"https://example.com/{index}" for index in range(8)]
    active = 0
    max_active = 0
    lock = asyncio.Lock()

    async def crawl(**kwargs: Any) -> CrawlResult:
        nonlocal active, max_active
        url = str(kwargs["url"])
        if url == root:
            return CrawlResult(url=url, markdown="root", links=children)
        async with lock:
            active += 1
            max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        async with lock:
            active -= 1
        return CrawlResult(url=url, markdown=url, links=[])

    monkeypatch.setattr(crawler_module, "_crawl_single_url", crawl)
    monkeypatch.setattr(crawler_module.settings, "max_concurrent_tasks", 3)

    results = await crawler_module.crawl_urls(
        [root],
        max_depth=1,
        max_pages=20,
    )

    assert len(results) == 9
    assert max_active == 3


async def test_recursive_fast_completion_refills_behind_slow_oldest_claim(
    monkeypatch,
):
    slow = "https://a.example/"
    fast = "https://b.example/"
    queued = "https://c.example/"
    release_slow = asyncio.Event()
    queued_started = asyncio.Event()

    async def crawl(**kwargs: Any) -> CrawlResult:
        url = str(kwargs["url"])
        if url == slow:
            await release_slow.wait()
        elif url == queued:
            queued_started.set()
        return CrawlResult(url=url, markdown=url, links=[])

    monkeypatch.setattr(crawler_module, "_crawl_single_url", crawl)
    monkeypatch.setattr(crawler_module.settings, "max_concurrent_tasks", 2)

    job = asyncio.create_task(
        crawler_module.crawl_urls(
            [slow, fast, queued],
            max_depth=1,
            max_pages=3,
        )
    )
    try:
        # The third request starts when the fast second claim completes; it
        # does not wait behind the deliberately blocked first claim.
        await asyncio.wait_for(queued_started.wait(), timeout=0.5)
        assert not job.done()
    finally:
        release_slow.set()

    results = await job
    assert [result.url for result in results] == [slow, fast, queued]


async def test_default_depth_zero_bypasses_frontier_and_preserves_flat_order(
    monkeypatch,
):
    urls = ["https://b.example/input", "https://a.example/input"]
    calls: list[str] = []

    class MustNotConstruct:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("frontier must remain opt-in")

    async def flat_crawl(url: str, *_args: Any, **_kwargs: Any) -> CrawlResult:
        calls.append(url)
        return CrawlResult(url=url, markdown=url)

    monkeypatch.setattr(crawler_module, "CrawlFrontier", MustNotConstruct)
    monkeypatch.setattr(crawler_module, "_crawl_single_url", flat_crawl)

    results = await crawler_module.crawl_urls(
        urls,
        max_pages=1,
        priority=99,
        allow_subdomains=True,
    )

    assert calls == urls
    assert [result.url for result in results] == urls


async def test_recursive_priority_is_applied_to_seeds_and_discovered_items(
    monkeypatch,
):
    real_frontier = CrawlFrontier
    captured_seed_priorities: list[int] = []
    captured_child_priorities: list[int] = []

    class TrackingFrontier(real_frontier):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured_seed_priorities.append(int(kwargs["seed_priority"]))
            super().__init__(*args, **kwargs)

        def admit(self, url: str, **kwargs: Any):  # type: ignore[no-untyped-def]
            captured_child_priorities.append(int(kwargs["priority"]))
            return super().admit(url, **kwargs)

    root = "https://example.com/"
    child = "https://example.com/child"
    monkeypatch.setattr(crawler_module, "CrawlFrontier", TrackingFrontier)
    monkeypatch.setattr(
        crawler_module,
        "_crawl_single_url",
        _graph_crawler({root: [child], child: []}, []),
    )

    await crawler_module.crawl_urls(
        [root],
        max_depth=1,
        max_pages=2,
        priority=63,
    )

    assert captured_seed_priorities == [63]
    assert captured_child_priorities == [63]


async def test_recursive_cancellation_drains_tasks_and_terminalizes_frontier(
    monkeypatch,
):
    real_frontier = CrawlFrontier
    frontiers: list[CrawlFrontier] = []
    started = 0
    finished = 0
    both_started = asyncio.Event()
    logged_outcomes: list[tuple[str, str]] = []

    class TrackingFrontier(real_frontier):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            frontiers.append(self)

    async def blocking_crawl(**_kwargs: Any) -> CrawlResult:
        nonlocal started, finished
        started += 1
        if started == 2:
            both_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished += 1
        raise AssertionError("unreachable")

    monkeypatch.setattr(crawler_module, "CrawlFrontier", TrackingFrontier)
    monkeypatch.setattr(crawler_module, "_crawl_single_url", blocking_crawl)
    monkeypatch.setattr(crawler_module.settings, "max_concurrent_tasks", 2)
    monkeypatch.setattr(
        crawler_module,
        "_log_recursive_frontier_metrics",
        lambda _frontier, *, outcome, error_type="": logged_outcomes.append((outcome, error_type)),
    )

    task = asyncio.create_task(
        crawler_module.crawl_urls(
            ["https://a.example/", "https://b.example/"],
            max_depth=1,
            max_pages=2,
        )
    )
    await asyncio.wait_for(both_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert finished == 2
    assert len(frontiers) == 1
    metrics = frontiers[0].metrics()
    assert metrics.pending == 0
    assert metrics.in_flight == 0
    assert metrics.terminal_reasons[TerminalReason.CANCELLED] == 2
    assert logged_outcomes == [("cancelled", "CancelledError")]
    assert not any(
        candidate.get_name().startswith("recursive-crawl-page-") and not candidate.done()
        for candidate in asyncio.all_tasks()
    )


async def test_recursive_timeout_uses_the_same_cancellation_cleanup(monkeypatch):
    real_frontier = CrawlFrontier
    frontiers: list[CrawlFrontier] = []
    started = asyncio.Event()
    finished = asyncio.Event()

    class TrackingFrontier(real_frontier):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            frontiers.append(self)

    async def blocking_crawl(**_kwargs: Any) -> CrawlResult:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()
        raise AssertionError("unreachable")

    monkeypatch.setattr(crawler_module, "CrawlFrontier", TrackingFrontier)
    monkeypatch.setattr(crawler_module, "_crawl_single_url", blocking_crawl)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await crawler_module.crawl_urls(
                ["https://example.com/"],
                max_depth=1,
                max_pages=2,
            )

    assert started.is_set()
    assert finished.is_set()
    metrics = frontiers[0].metrics()
    assert metrics.pending == 0
    assert metrics.in_flight == 0
    assert metrics.terminal_reasons[TerminalReason.CANCELLED] == 1


async def test_crawl_endpoint_wires_recursive_options_and_reports_actual_pages(
    client,
    monkeypatch,
):
    root = "https://example.com/"
    child = "https://example.com/child"
    calls: list[str] = []
    monkeypatch.setattr(
        crawler_module,
        "_crawl_single_url",
        _graph_crawler({root: [child], child: []}, calls),
    )

    response = await client.post(
        "/crawl",
        json={
            "urls": [root],
            "max_depth": 1,
            "max_pages": 2,
            "allow_subdomains": False,
            "priority": 42,
            "formats": ["markdown", "links"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_pages"] == 2
    assert [result["url"] for result in payload["results"]] == [root, child]
    assert payload["results"][0]["links"] == [child]
    assert payload["results"][1]["links"] == []
    assert calls == [root, child]


async def test_crawl_endpoint_default_multi_url_ignores_compat_max_pages(
    client,
    monkeypatch,
):
    urls = ["https://b.example/input", "https://a.example/input"]
    calls: list[str] = []

    async def flat_crawl(url: str, *_args: Any, **_kwargs: Any) -> CrawlResult:
        calls.append(url)
        return CrawlResult(url=url, markdown=url)

    monkeypatch.setattr(crawler_module, "_crawl_single_url", flat_crawl)
    response = await client.post(
        "/crawl",
        json={
            "urls": urls,
            "max_pages": 1,
            "priority": 91,
            "allow_subdomains": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_pages"] == 2
    assert [result["url"] for result in payload["results"]] == urls
    assert calls == urls


def test_recursive_request_validates_shared_page_budget_only_when_enabled():
    flat = CrawlRequest(
        urls=["https://a.example/", "https://b.example/"],
        max_pages=1,
    )
    assert flat.max_depth == 0
    assert flat.allow_subdomains is False

    with pytest.raises(ValidationError, match="number of seed URLs"):
        CrawlRequest(
            urls=["https://a.example/", "https://b.example/"],
            max_depth=1,
            max_pages=1,
        )


def test_recursive_request_default_uses_validated_service_setting() -> None:
    request = CrawlRequest(urls=["https://example.com/"], max_depth=1)

    assert request.max_pages == settings.default_max_pages
    assert (
        CrawlRequest.model_fields["max_pages"].default
        == settings.default_max_pages
    )


def test_recursive_html_limit_uses_total_page_budget():
    with pytest.raises(ValidationError, match="html output is limited"):
        CrawlRequest(
            urls=["https://example.com/"],
            max_depth=2,
            max_pages=6,
            formats=["html"],
        )


async def test_recursive_seed_is_checked_and_explicit_disallow_skips_page_fetch(
    monkeypatch,
):
    root = "https://example.com/private"
    crawl_calls: list[str] = []
    policy_calls: list[str] = []
    events: list[tuple[str, dict[str, Any]]] = []

    class DenyRobots:
        async def check(self, url: str) -> RobotsDecision:
            policy_calls.append(url)
            return RobotsDecision(
                allowed=False,
                reason=RobotsDecisionReason.EXPLICIT_DISALLOW,
                status_code=200,
                matched_specificity=8,
            )

    class CapturingLogger:
        def info(self, event: str, **fields: Any) -> None:
            events.append((event, fields))

        def warning(self, _event: str, **_fields: Any) -> None:
            pass

    monkeypatch.setattr(
        "app.services.robots.get_robots_policy",
        lambda: DenyRobots(),
    )
    monkeypatch.setattr(crawler_module, "logger", CapturingLogger())
    monkeypatch.setattr(
        crawler_module,
        "_crawl_single_url",
        _graph_crawler({root: []}, crawl_calls),
    )

    results = await crawler_module.crawl_urls(
        [root],
        max_depth=1,
        max_pages=1,
    )

    assert policy_calls == [root]
    assert crawl_calls == []
    assert len(results) == 1
    assert results[0].url == root
    assert "explicitly disallows" in (results[0].error or "")
    assert events[-1][0] == "recursive_crawl_frontier_finished"
    assert events[-1][1]["terminal_reasons"] == {"robots_disallowed": 1}
    assert events[-1][1]["robots_disallowed"] == 1
    assert "https://" not in repr(events[-1][1])


async def test_discovered_url_is_checked_before_fetch_and_blocked_result_is_returned(
    monkeypatch,
):
    root = "https://example.com/"
    child = "https://example.com/private"
    crawl_calls: list[str] = []
    policy_calls: list[str] = []

    class SelectiveRobots:
        async def check(self, url: str) -> RobotsDecision:
            policy_calls.append(url)
            return RobotsDecision(
                allowed=url != child,
                reason=(
                    RobotsDecisionReason.ALLOWED
                    if url != child
                    else RobotsDecisionReason.EXPLICIT_DISALLOW
                ),
            )

    policy = SelectiveRobots()
    monkeypatch.setattr(
        "app.services.robots.get_robots_policy",
        lambda: policy,
    )
    monkeypatch.setattr(
        crawler_module,
        "_crawl_single_url",
        _graph_crawler({root: [child], child: []}, crawl_calls),
    )

    results = await crawler_module.crawl_urls(
        [root],
        max_depth=1,
        max_pages=2,
    )

    assert policy_calls == [root, child]
    assert crawl_calls == [root]
    assert [result.url for result in results] == [root, child]
    assert results[0].error is None
    assert "explicitly disallows" in (results[1].error or "")


async def test_recursive_policy_failure_is_fail_closed_without_logging_a_url(
    monkeypatch,
):
    root = "https://example.com/signed?secret=value"
    crawl_calls: list[str] = []
    warnings: list[tuple[str, dict[str, Any]]] = []

    class BrokenRobots:
        async def check(self, _url: str) -> RobotsDecision:
            raise RuntimeError("policy implementation failed")

    class CapturingLogger:
        def info(self, _event: str, **_fields: Any) -> None:
            pass

        def warning(self, event: str, **fields: Any) -> None:
            warnings.append((event, fields))

    monkeypatch.setattr(
        "app.services.robots.get_robots_policy",
        lambda: BrokenRobots(),
    )
    monkeypatch.setattr(crawler_module, "logger", CapturingLogger())
    monkeypatch.setattr(
        crawler_module,
        "_crawl_single_url",
        _graph_crawler({root: []}, crawl_calls),
    )

    results = await crawler_module.crawl_urls(
        [root],
        max_depth=1,
        max_pages=1,
    )

    assert crawl_calls == []
    assert "policy check failed" in (results[0].error or "")
    assert warnings == [
        (
            "recursive_robots_policy_failed",
            {"error_type": "RuntimeError"},
        )
    ]
    assert "https://" not in repr(warnings)


async def test_flat_depth_zero_never_constructs_or_checks_robots(monkeypatch):
    url = "https://example.com/"
    calls: list[str] = []

    def robots_must_not_run():
        raise AssertionError("robots policy is recursive-only")

    async def flat_crawl(url: str, *_args: Any, **_kwargs: Any) -> CrawlResult:
        calls.append(url)
        return CrawlResult(url=url, markdown="ok")

    monkeypatch.setattr(
        "app.services.robots.get_robots_policy",
        robots_must_not_run,
    )
    monkeypatch.setattr(crawler_module, "_crawl_single_url", flat_crawl)

    results = await crawler_module.crawl_urls([url], max_depth=0)

    assert calls == [url]
    assert [result.markdown for result in results] == ["ok"]


@pytest.mark.parametrize(
    ("destination", "expected_terminal", "expected_error"),
    [
        (
            "https://example.com/private",
            TerminalReason.ROBOTS_DISALLOWED,
            "explicitly disallows",
        ),
        (
            "https://outside.example/final",
            TerminalReason.CONTENT_REJECTED,
            "leaves the configured crawl scope",
        ),
    ],
)
async def test_recursive_flat_cached_redirect_is_refetched_and_denied_before_destination_request(
    monkeypatch,
    destination,
    expected_terminal,
    expected_error,
):
    from app.services import fetcher as fetcher_module

    source = "https://example.com/start"
    page_requests: list[str] = []
    robots_calls: list[str] = []
    cache_calls: list[str] = []
    frontiers: list[CrawlFrontier] = []
    real_frontier = CrawlFrontier

    class TrackingFrontier(real_frontier):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            frontiers.append(self)

    class RedirectRobots:
        async def check(self, url: str) -> RobotsDecision:
            robots_calls.append(url)
            denied = url == "https://example.com/private"
            return RobotsDecision(
                allowed=not denied,
                reason=(
                    RobotsDecisionReason.EXPLICIT_DISALLOW
                    if denied
                    else RobotsDecisionReason.ALLOWED
                ),
            )

    cached = CrawlResult(
        url=source,
        markdown="flat cached content that lacks redirect provenance",
        metadata=ExtractionMetadata(source_url=source),
    )

    class FlatCache:
        async def get(self, _key):
            cache_calls.append("get")
            return orjson.dumps(
                {
                    "t": 9_999_999_999,
                    "r": cached.model_dump(),
                }
            )

        async def set(self, _key, _value, ttl=None):
            cache_calls.append("set")

    async def fake_validate(_url: str) -> str | None:
        return None

    async def fake_stream(url: str, _client: Any):
        page_requests.append(url)
        if url == source:
            return 302, {"location": destination}, b""
        raise AssertionError("denied redirect destination received a page request")

    policy = RedirectRobots()
    monkeypatch.setattr(crawler_module, "CrawlFrontier", TrackingFrontier)
    monkeypatch.setattr(
        "app.services.robots.get_robots_policy",
        lambda: policy,
    )
    monkeypatch.setattr(crawler_module, "get_cache", lambda: FlatCache())
    monkeypatch.setattr(fetcher_module, "validate_public_url", fake_validate)
    monkeypatch.setattr(fetcher_module, "_stream_one", fake_stream)

    results = await crawler_module.crawl_urls(
        [source],
        max_depth=1,
        max_pages=1,
        max_age=None,
        js_render=False,
    )

    assert page_requests == [source]
    assert cache_calls == []
    assert expected_error in (results[0].error or "")
    assert len(frontiers) == 1
    record = frontiers[0].record(source)
    assert record is not None
    assert record.terminal_reason is expected_terminal
    if expected_terminal is TerminalReason.CONTENT_REJECTED:
        assert destination not in robots_calls
    else:
        assert destination in robots_calls

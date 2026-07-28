from __future__ import annotations

import json
from collections import Counter
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from bench.live_vendor_benchmark import (
    EVENT_SCHEMA_VERSION,
    EXA_ENDPOINT,
    FIRECRAWL_ENDPOINT,
    BenchmarkError,
    Credentials,
    GitState,
    HttpxRequestExecutor,
    WireResponse,
    calculate_manifest_sha256,
    deterministic_bootstrap_ci,
    execute_benchmark,
    load_manifest,
    normalize_provider_response,
    normalize_text,
    parse_manifest,
    randomized_orders,
    redact_error,
    score_existing_run,
    score_text,
    seal_manifest_document,
    sha256_bytes,
    summarize_events,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

JsonObject = dict[str, Any]
_CONTAINER_DIGEST = "sha256:" + ("a" * 64)


def _draft_manifest(*, task_count: int = 2, with_references: bool = True) -> JsonObject:
    tasks: list[JsonObject] = []
    for index in range(task_count):
        reference_text = f"Reference article {index} with stable facts."
        reference = (
            {
                "text": reference_text,
                "sha256": sha256_bytes(reference_text.encode()),
                "method": "blinded-human-v1",
            }
            if with_references
            else None
        )
        tasks.append(
            {
                "task_id": f"task-{index}",
                "url": f"https://private.example/page/{index}?secret=hidden",
                "stratum": "docs" if index % 2 == 0 else "article",
                "language": "en",
                "reference": reference,
            }
        )
    return {
        "schema_version": "clusy.live-vendor.fixed-url.v1",
        "benchmark_id": "sealed-holdout",
        "created_at": "2026-07-28T12:00:00Z",
        "seed": 4172,
        "runner_region": "us-west",
        "country": None,
        "location": None,
        "scope": "main_content",
        "content_format": "markdown",
        "mode": "cold_live",
        "clusy_extraction_profile": "adaptive",
        "timeout_seconds": 60,
        "plans": {
            "clusy": "production-shadow",
            "exa": "team",
            "firecrawl": "growth",
        },
        "pricing": {
            "clusy": {"currency": "USD", "per_request": 0},
            "exa": {"currency": "USD", "per_request": 0.001},
            "firecrawl": {"currency": "USD", "per_request": 0.001},
        },
        "tasks": tasks,
    }


def _write_manifest(path: Path, *, task_count: int = 2) -> JsonObject:
    document = seal_manifest_document(_draft_manifest(task_count=task_count))
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return document


def _credentials(**overrides: str) -> Credentials:
    values = {
        "exa_api_key": "exa-secret",
        "firecrawl_api_key": "fire-secret",
        "clusy_base_url": "https://clusy.test",
        "clusy_api_key": "clusy-secret",
    }
    values.update(overrides)
    return Credentials(**values)


def _clean_git_state() -> GitState:
    return GitState(
        commit="f" * 40,
        clean=True,
        runner_committed=True,
        detail="clean",
    )


def _mock_executor_factory(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[[], HttpxRequestExecutor]:
    def factory() -> HttpxRequestExecutor:
        client = httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )
        return HttpxRequestExecutor(client)

    return factory


def _load_events(run_directory: Path) -> list[JsonObject]:
    return [
        json.loads(line)
        for line in (run_directory / "events.jsonl").read_text().splitlines()
        if line
    ]


def test_manifest_seal_is_canonical_and_tamper_evident(tmp_path: Path) -> None:
    sealed = seal_manifest_document(_draft_manifest())
    assert sealed["sealed"] is True
    assert sealed["frozen"] is True
    assert sealed["manifest_sha256"] == calculate_manifest_sha256(sealed)
    parsed = parse_manifest(sealed)
    assert parsed.digest == sealed["manifest_sha256"]
    assert len(parsed.tasks) == 2

    sealed["tasks"][0]["url"] = "https://attacker.example/changed"
    with pytest.raises(BenchmarkError, match="digest mismatch"):
        parse_manifest(sealed)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda doc: doc.update(sealed=False), "sealed=true"),
        (lambda doc: doc.update(frozen=False), "frozen=true"),
        (lambda doc: doc.update(timeout_seconds=59), "digest mismatch"),
        (lambda doc: doc["tasks"].append(doc["tasks"][0]), "digest mismatch"),
    ],
)
def test_manifest_fails_closed(
    mutation: Callable[[JsonObject], None],
    message: str,
) -> None:
    sealed = seal_manifest_document(_draft_manifest())
    mutation(sealed)
    with pytest.raises(BenchmarkError, match=message):
        parse_manifest(sealed)


def test_manifest_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema_version":"a","schema_version":"b"}', encoding="utf-8")
    with pytest.raises(BenchmarkError, match="duplicate JSON key"):
        load_manifest(path)


def test_reference_hash_is_verified_when_manifest_is_sealed() -> None:
    draft = _draft_manifest()
    draft["tasks"][0]["reference"]["sha256"] = "0" * 64
    with pytest.raises(BenchmarkError, match="reference sha256 mismatch"):
        parse_manifest(seal_manifest_document(draft))


def test_manifest_rejects_unmatched_provider_fetch_geography() -> None:
    draft = _draft_manifest()
    draft["country"] = "US"
    with pytest.raises(BenchmarkError, match="country and location must be null"):
        parse_manifest(seal_manifest_document(draft))


def test_randomized_provider_order_is_seeded_and_complete() -> None:
    manifest = parse_manifest(seal_manifest_document(_draft_manifest(task_count=8)))
    first = randomized_orders(manifest)
    second = randomized_orders(manifest)
    assert first == second
    assert all(set(order) == {"clusy", "exa", "firecrawl"} for order in first.values())
    assert len(set(first.values())) > 1


def test_execution_refuses_without_explicit_paid_flag_before_executor_creation(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    created = False

    def forbidden_factory() -> HttpxRequestExecutor:
        nonlocal created
        created = True
        raise AssertionError("network executor must not be created")

    with pytest.raises(BenchmarkError, match="--execute-paid"):
        execute_benchmark(
            manifest_path=manifest_path,
            output_root=tmp_path / "runs",
            repo_root=tmp_path,
            execute_paid=False,
            nonclaimable=False,
            credentials=_credentials(),
            executor_factory=forbidden_factory,
            git_state=_clean_git_state(),
            container_digest=_CONTAINER_DIGEST,
        )
    assert created is False
    assert not (tmp_path / "runs").exists()


def test_execution_refuses_dirty_runner_unless_explicitly_nonclaimable(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    dirty = GitState(
        commit="f" * 40,
        clean=False,
        runner_committed=False,
        detail="dirty",
    )
    with pytest.raises(BenchmarkError, match="claimable run refused"):
        execute_benchmark(
            manifest_path=manifest_path,
            output_root=tmp_path / "runs",
            repo_root=tmp_path,
            execute_paid=True,
            nonclaimable=False,
            credentials=_credentials(),
            git_state=dirty,
            container_digest=_CONTAINER_DIGEST,
        )
    assert not (tmp_path / "runs").exists()


def test_execution_refuses_missing_vendor_key_before_executor_creation(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path)
    created = False

    def forbidden_factory() -> HttpxRequestExecutor:
        nonlocal created
        created = True
        raise AssertionError("network executor must not be created")

    with pytest.raises(BenchmarkError, match="FIRECRAWL_API_KEY"):
        execute_benchmark(
            manifest_path=manifest_path,
            output_root=tmp_path / "runs",
            repo_root=tmp_path,
            execute_paid=True,
            nonclaimable=False,
            credentials=_credentials(firecrawl_api_key=""),
            executor_factory=forbidden_factory,
            git_state=_clean_git_state(),
            container_digest=_CONTAINER_DIGEST,
        )
    assert created is False


def test_matched_requests_raw_artifacts_and_event_schema(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(manifest_path, task_count=1)
    observed: list[tuple[str, JsonObject, dict[str, str]]] = []
    raw_bodies: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        observed.append((str(request.url), body, dict(request.headers)))
        if request.url.host == "api.exa.ai":
            response_body = json.dumps(
                {
                    "requestId": "exa-request",
                    "credits": 1,
                    "costDollars": {"total": 0.001},
                    "statuses": [
                        {
                            "id": "https://private.example/page/0?secret=hidden",
                            "status": "success",
                        }
                    ],
                    "results": [
                        {
                            "url": "https://private.example/page/0?canonical=private",
                            "title": "Exa title",
                            "text": "Reference article 0 with stable facts.",
                        }
                    ],
                }
            ).encode()
        elif request.url.host == "api.firecrawl.dev":
            response_body = json.dumps(
                {
                    "success": True,
                    "id": "fire-request",
                    "creditsUsed": 1,
                    "data": {
                        "markdown": "Reference article 0 with stable facts.",
                        "metadata": {
                            "title": "Fire title",
                            "sourceURL": "https://private.example/page/0?private=yes",
                        },
                    },
                }
            ).encode()
        else:
            response_body = json.dumps(
                {
                    "status": "ok",
                    "results": [
                        {
                            "url": "https://private.example/page/0?secret=hidden",
                            "markdown": "Reference article 0 with stable facts.",
                            "cached": False,
                            "metadata": {
                                "title": "Clusy title",
                                "canonical_url": (
                                    "https://private.example/page/0?canonical=private"
                                ),
                            },
                        }
                    ],
                }
            ).encode()
        raw_bodies[request.url.host or ""] = response_body
        return httpx.Response(
            200,
            headers={"x-request-id": f"{request.url.host}-header-id"},
            content=response_body,
        )

    run_directory = execute_benchmark(
        manifest_path=manifest_path,
        output_root=tmp_path / "runs",
        repo_root=tmp_path,
        execute_paid=True,
        nonclaimable=False,
        credentials=_credentials(),
        executor_factory=_mock_executor_factory(handler),
        git_state=_clean_git_state(),
        container_digest=_CONTAINER_DIGEST,
        bootstrap_samples=200,
    )

    assert len(observed) == 3
    by_host = {
        urlsplit_host: (url, body, headers)
        for url, body, headers in observed
        if (urlsplit_host := httpx.URL(url).host)
    }
    exa_url, exa_body, exa_headers = by_host["api.exa.ai"]
    assert exa_url == EXA_ENDPOINT
    assert exa_body == {
        "urls": ["https://private.example/page/0?secret=hidden"],
        "text": {"verbosity": "full"},
        "maxAgeHours": 0,
        "livecrawlTimeout": 60_000,
    }
    assert exa_headers["authorization"] == "Bearer exa-secret"

    fire_url, fire_body, fire_headers = by_host["api.firecrawl.dev"]
    assert fire_url == FIRECRAWL_ENDPOINT
    assert fire_body == {
        "url": "https://private.example/page/0?secret=hidden",
        "formats": ["markdown"],
        "onlyMainContent": True,
        "maxAge": 0,
        "storeInCache": False,
        "timeout": 60_000,
    }
    assert fire_headers["authorization"] == "Bearer fire-secret"

    clusy_url, clusy_body, clusy_headers = by_host["clusy.test"]
    assert clusy_url == "https://clusy.test/crawl"
    assert clusy_body == {
        "urls": ["https://private.example/page/0?secret=hidden"],
        "max_pages": 1,
        "formats": ["markdown"],
        "max_age": 0,
        "extraction_profile": "adaptive",
    }
    assert clusy_headers["authorization"] == "Bearer clusy-secret"

    events = _load_events(run_directory)
    assert len(events) == 3
    event_bytes = (run_directory / "events.jsonl").read_bytes()
    for secret in (b"exa-secret", b"fire-secret", b"clusy-secret"):
        assert secret not in event_bytes
    assert [event["provider"] for event in events] == list(
        randomized_orders(parse_manifest(document))["task-0"]
    )
    required_fields = {
        "run_id",
        "task_id",
        "provider",
        "endpoint",
        "mode",
        "plan",
        "api_version",
        "sdk_version",
        "runner_commit",
        "container_digest",
        "utc_timestamp",
        "runner_region",
        "country",
        "location",
        "query_or_seed",
        "top_k",
        "limit",
        "depth",
        "scope",
        "domain_filters",
        "cache_policy",
        "max_age",
        "content_format",
        "token_budget",
        "timeout",
        "retry",
        "randomized_order",
        "started_at",
        "first_byte_at",
        "completed_at",
        "status",
        "error",
        "provider_request_id",
        "cache_hit",
        "fetch_age",
        "credits",
        "normalized_cost",
        "raw_response_sha256",
        "immutable_artifact_path",
        "rank",
        "original_url",
        "canonical_url",
        "title",
        "snippet",
        "highlights",
        "text",
        "character_count",
        "token_count",
        "publication_timestamp",
        "fetch_timestamp",
        "citation_links",
        "provider_score",
    }
    for event in events:
        assert event["event_schema_version"] == EVENT_SCHEMA_VERSION
        assert event["claimable"] is True
        assert event["watermark"] == ""
        assert event["retry"] == 0
        assert event["attempt"] == 1
        assert required_fields <= event.keys()
        assert event["country"] is None
        assert event["location"] is None
        assert event["provider_fetch_geo_control"] == "unsupported_common_denominator"
        assert "private.example" not in event["original_url"]
        assert "?secret=" not in event["original_url"]
        assert "?canonical=" not in event["canonical_url"]
        artifact = run_directory / event["immutable_artifact_path"]
        expected_body = raw_bodies[httpx.URL(event["endpoint"]).host]
        assert artifact.read_bytes() == expected_body
        assert event["raw_response_sha256"] == sha256_bytes(expected_body)
        assert oct(artifact.stat().st_mode & 0o777) == "0o400"
        assert event["scoring"]["token_f1"] == 1

    summary = json.loads((run_directory / "summary.json").read_text())
    assert summary["claimable"] is True
    assert summary["claimable_scope"] == "artifact_integrity_only"
    assert summary["artifact_integrity_claimable"] is True
    assert summary["vendor_win_claimable"] is False
    assert summary["vendor_win_watermark"] == "NO_VENDOR_WIN_CLAIM"
    assert summary["vendor_win_gate"]["passed"] is False
    assert summary["watermark"] == ""
    assert "does not declare a vendor winner" in summary["interpretation"]
    assert len(summary["pairwise"]) == 12
    assert all(item["by_stratum"] for item in summary["pairwise"])
    assert {
        provider_summary["provider"]
        for provider_summary in summary["provider_summaries"]
    } == {"clusy", "exa", "firecrawl"}
    assert all(
        provider_summary["latency_ms"]["p99"]
        >= provider_summary["latency_ms"]["p50"]
        for provider_summary in summary["provider_summaries"]
    )
    assert summary["events_sha256"] == sha256_bytes((run_directory / "events.jsonl").read_bytes())
    completion = json.loads((run_directory / "completion.json").read_text())
    assert completion["events_sha256"] == summary["events_sha256"]
    exa_event = next(event for event in events if event["provider"] == "exa")
    assert exa_event["provider_reported_cost"] == 0.001


def test_first_attempt_http_error_is_retained_without_retry(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, task_count=1)
    calls: Counter[str] = Counter()
    fire_error = b'{"error":"blocked at https://private.example/a?token=secret"}'

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        calls[host] += 1
        if host == "api.firecrawl.dev":
            return httpx.Response(503, content=fire_error)
        if host == "api.exa.ai":
            return httpx.Response(
                200,
                json={
                    "statuses": [
                        {
                            "id": "https://private.example/page/0?secret=hidden",
                            "status": "success",
                        }
                    ],
                    "results": [{"text": "Reference article 0 with stable facts."}],
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [{"markdown": "Reference article 0 with stable facts.", "cached": False}]
            },
        )

    run_directory = execute_benchmark(
        manifest_path=manifest_path,
        output_root=tmp_path / "runs",
        repo_root=tmp_path,
        execute_paid=True,
        nonclaimable=True,
        credentials=_credentials(),
        executor_factory=_mock_executor_factory(handler),
        git_state=GitState("f" * 40, False, False, "dirty"),
        container_digest="",
        bootstrap_samples=200,
    )
    assert calls == {
        "api.exa.ai": 1,
        "api.firecrawl.dev": 1,
        "clusy.test": 1,
    }
    events = _load_events(run_directory)
    fire_event = next(event for event in events if event["provider"] == "firecrawl")
    assert fire_event["status"] == "http_error"
    assert fire_event["attempt"] == 1
    assert fire_event["retry"] == 0
    assert "url:[redacted]" in fire_event["error"]
    assert "private.example" not in fire_event["error"]
    assert "?token=" not in fire_event["error"]
    assert (run_directory / fire_event["immutable_artifact_path"]).read_bytes() == fire_error
    assert all(event["claimable"] is False for event in events)
    assert all(event["watermark"] == "NONCLAIMABLE" for event in events)
    summary = json.loads((run_directory / "summary.json").read_text())
    assert summary["claimable"] is False
    assert summary["watermark"] == "NONCLAIMABLE"


def test_exa_http_200_per_url_error_is_a_failed_attempt() -> None:
    body = json.dumps(
        {
            "requestId": "request-1",
            "statuses": [
                {
                    "id": "https://private.example/a",
                    "status": "error",
                    "error": {
                        "tag": "CRAWL_LIVECRAWL_TIMEOUT",
                        "httpStatusCode": 504,
                    },
                }
            ],
            "results": [],
        }
    ).encode()
    wire = WireResponse(
        status_code=200,
        headers={},
        body=body,
        started_at="2026-07-28T00:00:00Z",
        first_byte_at="2026-07-28T00:00:01Z",
        completed_at="2026-07-28T00:01:00Z",
        latency_ms=60_000,
        transport_error=None,
    )
    result = normalize_provider_response("exa", wire)
    assert result.status == "provider_error"
    assert result.text == ""
    assert result.error == "Exa per-URL error: CRAWL_LIVECRAWL_TIMEOUT (HTTP 504)"


def test_error_redaction_removes_all_credentials_and_url_details() -> None:
    redacted = redact_error(
        "request https://user:pass@example.com/private?q=1 failed with api-secret",
        ("api-secret",),
    )
    assert redacted is not None
    assert "api-secret" not in redacted
    assert "user" not in redacted
    assert "pass" not in redacted
    assert "/private" not in redacted
    assert "?q=1" not in redacted
    assert "[REDACTED_SECRET]" in redacted


def test_normalization_and_multilingual_scoring_are_deterministic() -> None:
    assert normalize_text(" Alpha  \r\nBeta\x00\r\n") == "Alpha\nBeta"
    score = score_text("Alpha 中文 beta beta", "alpha 中文 beta")
    assert score["token_recall"] == 1
    assert score["token_precision"] == pytest.approx(4 / 5)
    assert score["token_f1"] == pytest.approx(8 / 9)
    assert score["tokenizer"] == "clusy-unicode-tokenizer.v1"


def test_bootstrap_and_pairwise_summary_are_deterministic() -> None:
    first = deterministic_bootstrap_ci([0.1, 0.2, -0.1, 0.4], seed=7, samples=500)
    second = deterministic_bootstrap_ci([0.1, 0.2, -0.1, 0.4], seed=7, samples=500)
    assert first == second

    manifest = parse_manifest(seal_manifest_document(_draft_manifest(task_count=2)))
    events: list[JsonObject] = []
    for task_index, task in enumerate(manifest.tasks):
        candidates = {
            "clusy": f"Reference article {task_index} with stable facts.",
            "exa": f"Reference article {task_index}",
            "firecrawl": f"Reference article {task_index} with stable",
        }
        for provider, text in candidates.items():
            events.append(
                {
                    "task_id": task.task_id,
                    "provider": provider,
                    "attempt": 1,
                    "status": "ok",
                    "latency_ms": 100 + task_index,
                    "normalized_cost": 0.001,
                    "text": text,
                    "claimable": True,
                    "manifest_sha256": manifest.digest,
                }
            )
    summary_a = summarize_events(manifest, events, bootstrap_samples=300)
    summary_b = summarize_events(manifest, events, bootstrap_samples=300)
    assert summary_a == summary_b
    clusy_exa_f1 = next(
        item
        for item in summary_a["pairwise"]
        if item["left_provider"] == "clusy"
        and item["right_provider"] == "exa"
        and item["metric"] == "token_f1"
    )
    expected_delta = (
        1
        - score_text(
            "Reference article 0",
            "Reference article 0 with stable facts.",
        )["token_f1"]
    )
    assert clusy_exa_f1["mean_delta_left_minus_right"] == pytest.approx(expected_delta)
    assert len(clusy_exa_f1["per_task_deltas"]) == 2
    assert {row["stratum"] for row in clusy_exa_f1["by_stratum"]} == {
        "article",
        "docs",
    }
    assert summary_a["claimable"] is False
    assert summary_a["vendor_win_claimable"] is False
    assert summary_a["watermark"] == "NONCLAIMABLE"


def test_offline_score_verifies_artifacts_and_rejects_tampering(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, task_count=1)

    def handler(request: httpx.Request) -> httpx.Response:
        text = "Reference article 0 with stable facts."
        if request.url.host == "api.exa.ai":
            return httpx.Response(
                200,
                json={
                    "statuses": [
                        {
                            "id": "https://private.example/page/0?secret=hidden",
                            "status": "success",
                        }
                    ],
                    "results": [{"text": text}],
                },
            )
        if request.url.host == "api.firecrawl.dev":
            return httpx.Response(
                200,
                json={"success": True, "data": {"markdown": text}},
            )
        return httpx.Response(
            200,
            json={"status": "ok", "results": [{"markdown": text, "cached": False}]},
        )

    run_directory = execute_benchmark(
        manifest_path=manifest_path,
        output_root=tmp_path / "runs",
        repo_root=tmp_path,
        execute_paid=True,
        nonclaimable=False,
        credentials=_credentials(),
        executor_factory=_mock_executor_factory(handler),
        git_state=_clean_git_state(),
        container_digest=_CONTAINER_DIGEST,
        bootstrap_samples=100,
    )
    events_path = run_directory / "events.jsonl"
    offline_summary = run_directory / "offline-summary.json"
    score_existing_run(
        manifest_path=manifest_path,
        events_path=events_path,
        output_path=offline_summary,
        bootstrap_samples=100,
    )
    assert json.loads(offline_summary.read_text())["claimable"] is True

    first_event = _load_events(run_directory)[0]
    raw_path = run_directory / first_event["immutable_artifact_path"]
    raw_path.chmod(0o600)
    raw_path.write_bytes(b"tampered")
    with pytest.raises(BenchmarkError, match="artifact hash mismatch"):
        score_existing_run(
            manifest_path=manifest_path,
            events_path=events_path,
            output_path=run_directory / "tampered-summary.json",
            bootstrap_samples=100,
        )

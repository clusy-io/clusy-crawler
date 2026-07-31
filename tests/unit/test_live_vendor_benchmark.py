from __future__ import annotations

import json
import socket
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from bench.live_vendor_benchmark import (
    _EVENT_OPTIONAL_STRING_FIELDS,
    _EVENT_REQUIRED_STRING_FIELDS,
    _EVENT_STRING_LIST_FIELDS,
    EVENT_SCHEMA_VERSION,
    EXA_ENDPOINT,
    FIRECRAWL_ENDPOINT,
    SCHEMA_VERSION,
    SUMMARY_SCHEMA_VERSION,
    V1_SCHEMA_VERSION,
    BenchmarkError,
    Credentials,
    GitState,
    HttpxRequestExecutor,
    WindowTimingEvidence,
    WireResponse,
    _recompute_budget_ledger,
    _safe_run_id,
    aggregate_completed_run_directories,
    aggregate_v3_summaries,
    calculate_corpus_sha256,
    calculate_manifest_sha256,
    canonical_json_bytes,
    canonical_task_url_identity,
    derive_domain_cluster,
    deterministic_bootstrap_ci,
    execute_benchmark,
    load_manifest,
    normalize_provider_response,
    normalize_text,
    parse_manifest,
    prepare_claim_context,
    randomized_orders,
    redact_error,
    score_existing_run,
    score_structure,
    score_text,
    seal_manifest_document,
    sha256_bytes,
    summarize_events,
    validate_independent_window_timing,
    verify_completed_run_directory,
    verify_event_artifacts,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

JsonObject = dict[str, Any]
_CONTAINER_DIGEST = "sha256:" + ("a" * 64)
_REVISION = "f" * 40
_CONFIG_SHA = "b" * 64
_SERVICE_IMAGE_DIGEST = "sha256:" + ("c" * 64)
_PUBLIC_DNS_ANSWER = (
    socket.AF_INET,
    socket.SOCK_STREAM,
    socket.IPPROTO_TCP,
    "",
    ("93.184.216.34", 443),
)
_EVENT_CARRIER_FIELDS = tuple(
    sorted(
        _EVENT_REQUIRED_STRING_FIELDS | _EVENT_OPTIONAL_STRING_FIELDS | _EVENT_STRING_LIST_FIELDS
    )
)


def _reference_text(index: int) -> str:
    return (
        f"# Reference article {index}\n\n"
        "- stable facts\n\n"
        "```python\n"
        f"print({index})\n"
        "```\n\n"
        "| Key | Value |\n"
        "| --- | --- |\n"
        f"| index | {index} |"
    )


def _draft_manifest(
    *,
    task_count: int = 2,
    providers: Sequence[str] = ("clusy", "exa", "firecrawl"),
    mode: str = "cold_live",
    window_index: int = 1,
) -> JsonObject:
    tasks: list[JsonObject] = []
    for index in range(task_count):
        reference_text = _reference_text(index)
        tasks.append(
            {
                "task_id": f"task-{index}",
                "url": f"https://example.com/benchmark/page/{index}",
                "stratum": "docs" if index % 2 == 0 else "article",
                "language": "en",
                "domain_cluster": "example.com",
                "content_type": "markdown-doc",
                "render_class": "static",
                "firecrawl_credit_cap": 1,
                "reference": {
                    "text": reference_text,
                    "sha256": sha256_bytes(reference_text.encode()),
                    "method": "blinded-human-v2",
                    "captured_at": "2026-07-28T12:00:00.000000Z",
                    "structure": {
                        "headings": [f"1:Reference article {index}"],
                        "list_items": ["stable facts"],
                        "code_blocks": [f"print({index})"],
                        "tables": [
                            [
                                ["Key", "Value"],
                                ["index", str(index)],
                            ]
                        ],
                    },
                },
            }
        )
    selected = tuple(providers)
    plans = {
        provider: {
            "clusy": "production-shadow",
            "exa": "team",
            "firecrawl": "growth",
        }[provider]
        for provider in selected
    }
    pricing = {
        provider: {
            "currency": "USD",
            "per_request": {
                "clusy": 0.002,
                "exa": 0.001,
                "firecrawl": 0.001,
            }[provider],
        }
        for provider in selected
    }
    budgets: JsonObject = {"clusy_usd": task_count * 0.002}
    if "exa" in selected:
        budgets["exa_usd"] = task_count * 0.001
    if "firecrawl" in selected:
        budgets["firecrawl_credits"] = task_count
    is_warm = mode == "warm_cache"
    exa_selected = "exa" in selected
    document: JsonObject = {
        "schema_version": SCHEMA_VERSION,
        "benchmark_id": "sealed-holdout",
        "created_at": "2026-07-28T12:00:00.000000Z",
        "seed": 4172,
        "runner_region": "us-west",
        "country": None,
        "location": None,
        "scope": "main_content",
        "content_format": "markdown",
        "mode": mode,
        "clusy_extraction_profile": "adaptive",
        "timeout_seconds": 60,
        "providers": list(selected),
        "plans": plans,
        "pricing": pricing,
        "tasks": tasks,
        "corpus_sha256": "",
        "time_window_id": f"window-{window_index}",
        "independent_window_index": window_index,
        "required_independent_windows": 2,
        "bootstrap_samples": 100,
        "cache_max_age_seconds": 3600 if is_warm else 0,
        "warm_cache_primed_at": ("2026-07-28T11:55:00.000000Z" if is_warm else None),
        "budgets": budgets,
        "request_policy": {
            "max_output_characters": 10_000,
            "firecrawl_proxy": "basic",
            "firecrawl_block_ads": True,
            "firecrawl_only_clean_content": False,
            "firecrawl_parse_pdf": False,
            "clusy_js_render": "conditional",
        },
        "clusy_binding": {
            "expected_revision": _REVISION,
            "expected_config_sha256": _CONFIG_SHA,
            "expected_image_digest": _SERVICE_IMAGE_DIGEST,
        },
        "compliance_acknowledgments": {
            "third_party_data_transfer_authorized": True,
            "exa_live_authorized": exa_selected,
            "exa_authorized_purpose": (
                "benchmark_only_no_training_distillation_or_labeling"
                if exa_selected
                else "not_applicable"
            ),
        },
    }
    document["corpus_sha256"] = calculate_corpus_sha256(tasks)
    return document


def _v1_draft_manifest() -> JsonObject:
    text = "Legacy reference"
    return {
        "schema_version": V1_SCHEMA_VERSION,
        "benchmark_id": "legacy-holdout",
        "created_at": "2026-07-28T12:00:00Z",
        "seed": 1,
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
        "tasks": [
            {
                "task_id": "legacy-0",
                "url": "http://legacy.invalid/page",
                "stratum": "legacy",
                "language": "en",
                "reference": {
                    "text": text,
                    "sha256": sha256_bytes(text.encode()),
                    "method": "legacy",
                },
            }
        ],
    }


def _write_manifest(
    path: Path,
    *,
    task_count: int = 2,
    providers: Sequence[str] = ("clusy", "exa", "firecrawl"),
    mode: str = "cold_live",
    window_index: int = 1,
) -> JsonObject:
    document = seal_manifest_document(
        _draft_manifest(
            task_count=task_count,
            providers=providers,
            mode=mode,
            window_index=window_index,
        )
    )
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return document


def _credentials(**overrides: str) -> Credentials:
    values = {
        "exa_api_key": "dummy-exa-secret",
        "firecrawl_api_key": "dummy-fire-secret",
        "clusy_base_url": "https://clusy.test",
        "clusy_api_key": "dummy-clusy-secret",
    }
    values.update(overrides)
    return Credentials(**values)


def _clean_git_state() -> GitState:
    return GitState(
        commit=_REVISION,
        clean=True,
        runner_committed=True,
        detail="clean",
    )


def _public_dns_resolver(_hostname: str, _port: int) -> list[tuple[Any, ...]]:
    return [_PUBLIC_DNS_ANSWER]


def _execution_kwargs(document: JsonObject) -> JsonObject:
    providers = set(document["providers"])
    task_count = len(document["tasks"])
    return {
        "confirmed_manifest_sha256": document["manifest_sha256"],
        "max_exa_usd": task_count * 0.001 if "exa" in providers else None,
        "max_firecrawl_credits": task_count if "firecrawl" in providers else None,
        "max_clusy_usd": task_count * 0.002,
        "acknowledge_exa_live_use": "exa" in providers,
        "dns_resolver": _public_dns_resolver,
    }


def _mock_executor_factory(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[[], HttpxRequestExecutor]:
    def factory() -> HttpxRequestExecutor:
        client = httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            trust_env=False,
        )
        return HttpxRequestExecutor(client)

    return factory


def _success_handler(
    *,
    mode: str = "cold_live",
    observed: list[httpx.Request] | None = None,
    text_override: str | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    task_index = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal task_index
        if observed is not None:
            observed.append(request)
        if request.method == "GET" and request.url.path == "/health/version":
            return httpx.Response(
                200,
                json={
                    "sha": _REVISION,
                    "config_fingerprint": _CONFIG_SHA,
                    "image_digest": _SERVICE_IMAGE_DIGEST,
                },
            )
        body = json.loads(request.content)
        url = body.get("url") or body.get("urls", [""])[0]
        match = str(url).rsplit("/", maxsplit=1)[-1]
        task_index = int(match) if match.isdigit() else task_index
        text = text_override if text_override is not None else _reference_text(task_index)
        cached = mode == "warm_cache"
        if request.url.host == "api.exa.ai":
            return httpx.Response(
                200,
                json={
                    "requestId": "exa-request",
                    "credits": 1,
                    "costDollars": {"total": 0.001},
                    "rawOnlyMarker": "DO_NOT_PERSIST_PROVIDER_RAW_OUTPUT",
                    "statuses": [
                        {
                            "id": url,
                            "status": "success",
                            "source": "cached" if cached else "crawled",
                        }
                    ],
                    "results": [{"url": url, "title": "Provider title", "text": text}],
                },
            )
        if request.url.host == "api.firecrawl.dev":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "id": "fire-request",
                    "creditsUsed": 1,
                    "rawOnlyMarker": "DO_NOT_PERSIST_PROVIDER_RAW_OUTPUT",
                    "data": {
                        "markdown": text,
                        "metadata": {
                            "title": "Provider title",
                            "sourceURL": url,
                            "cacheState": "hit" if cached else "miss",
                            "statusCode": 200,
                        },
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "rawOnlyMarker": "DO_NOT_PERSIST_PROVIDER_RAW_OUTPUT",
                "results": [
                    {
                        "url": url,
                        "markdown": text,
                        "cached": cached,
                        "metadata": {
                            "title": "Provider title",
                            "canonical_url": url,
                            "origin_status_code": 200,
                            "rendered": False,
                            "truncated": False,
                            "extraction_strategy": "native",
                            "extraction_route": "native",
                            "model_assisted": False,
                            "quality_attempted": True,
                            "quality_succeeded": True,
                            "completeness_score": 0.99,
                            "stage_timings_ms": {
                                "queue": 1,
                                "fetch": 2,
                                "render": 0,
                                "extraction": 3,
                                "total": 6,
                            },
                            "content_scope": "main_content",
                        },
                    }
                ],
            },
        )

    return handler


def _run(
    *,
    tmp_path: Path,
    manifest_path: Path,
    document: JsonObject,
    handler: Callable[[httpx.Request], httpx.Response],
    credentials: Credentials | None = None,
    nonclaimable: bool = False,
) -> Path:
    return execute_benchmark(
        manifest_path=manifest_path,
        output_root=tmp_path / "bench" / "results",
        repo_root=tmp_path,
        execute_paid=True,
        nonclaimable=nonclaimable,
        credentials=credentials or _credentials(),
        executor_factory=_mock_executor_factory(handler),
        git_state=(
            GitState(_REVISION, False, False, "dirty") if nonclaimable else _clean_git_state()
        ),
        container_digest="" if nonclaimable else _CONTAINER_DIGEST,
        bootstrap_samples=100,
        **_execution_kwargs(document),
    )


def _load_events(run_directory: Path) -> list[JsonObject]:
    return [
        json.loads(line)
        for line in (run_directory / "events.jsonl").read_text().splitlines()
        if line
    ]


def _rewrite_rehashed_event_chain(
    run_directory: Path,
    events: Sequence[JsonObject],
    *,
    manifest_document: JsonObject | None = None,
    recompute_summary: bool = False,
) -> None:
    events_path = run_directory / "events.jsonl"
    summary_path = run_directory / "summary.json"
    completion_path = run_directory / "completion.json"
    for path in (events_path, summary_path, completion_path):
        path.chmod(0o600)
    events_bytes = b"".join(canonical_json_bytes(event) + b"\n" for event in events)
    events_path.write_bytes(events_bytes)
    if recompute_summary:
        if manifest_document is None:
            raise AssertionError("manifest_document is required to recompute summary")
        manifest = parse_manifest(manifest_document)
        summary = summarize_events(
            manifest,
            events,
            bootstrap_samples=manifest.bootstrap_samples,
            events_sha256=sha256_bytes(events_bytes),
        )
        summary["observed_budget_ledger"] = _recompute_budget_ledger(
            manifest,
            events,
        )
    else:
        summary = json.loads(summary_path.read_text())
        summary["events_sha256"] = sha256_bytes(events_bytes)
    summary_bytes = canonical_json_bytes(summary) + b"\n"
    summary_path.write_bytes(summary_bytes)
    completion = json.loads(completion_path.read_text())
    completion["events_sha256"] = sha256_bytes(events_bytes)
    completion["summary_sha256"] = sha256_bytes(summary_bytes)
    completion_path.write_bytes(canonical_json_bytes(completion) + b"\n")


def _assert_direct_and_rehashed_event_rejected(
    *,
    manifest_document: JsonObject,
    run_directory: Path,
    events: Sequence[JsonObject],
    recompute_summary: bool = False,
) -> None:
    with pytest.raises(BenchmarkError):
        verify_event_artifacts(
            manifest=parse_manifest(manifest_document),
            events=events,
            run_directory=run_directory,
        )
    _rewrite_rehashed_event_chain(
        run_directory,
        events,
        manifest_document=manifest_document,
        recompute_summary=recompute_summary,
    )
    with pytest.raises(BenchmarkError):
        verify_completed_run_directory(run_directory)


def test_v3_manifest_is_canonical_tamper_evident_and_subset_aware() -> None:
    draft = _draft_manifest(providers=("clusy", "firecrawl"))
    draft.pop("corpus_sha256")
    sealed = seal_manifest_document(draft)
    assert sealed["sealed"] is True
    assert sealed["corpus_sha256"] == calculate_corpus_sha256(sealed["tasks"])
    assert sealed["manifest_sha256"] == calculate_manifest_sha256(sealed)
    parsed = parse_manifest(sealed)
    assert parsed.providers == ("clusy", "firecrawl")
    assert set(parsed.pricing) == {"clusy", "firecrawl"}

    sealed["tasks"][0]["url"] = "https://attacker.example/changed"
    with pytest.raises(BenchmarkError, match="digest mismatch"):
        parse_manifest(sealed)


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-07-28T12:00:00Z",
        "2026-07-28T12:00:00.0Z",
        "2026-07-28T12:00:00.000Z",
        "2026-07-28T12:00:00.0000000Z",
        "20260728T120000.000000Z",
        "2026-W31-2T12:00:00.000000Z",
        "2026-07-28T12:00:00.000000+00:00",
        "2026-07-28T12:00:00.000000-00:00",
    ],
)
def test_v3_rejects_every_alternate_timestamp_spelling(timestamp: str) -> None:
    draft = _draft_manifest(providers=("clusy", "firecrawl"))
    draft["created_at"] = timestamp
    with pytest.raises(BenchmarkError, match="canonical UTC"):
        parse_manifest(seal_manifest_document(draft))


def test_v3_uses_the_same_strict_timestamp_parser_for_reference_prime_and_run_id() -> None:
    bad_reference = _draft_manifest(providers=("clusy", "firecrawl"))
    bad_reference["tasks"][0]["reference"]["captured_at"] = "2026-07-28T12:00:00+00:00"
    with pytest.raises(BenchmarkError, match="canonical UTC"):
        parse_manifest(seal_manifest_document(bad_reference))

    bad_prime = _draft_manifest(
        providers=("clusy", "firecrawl"),
        mode="warm_cache",
    )
    bad_prime["warm_cache_primed_at"] = "2026-07-28T11:55:00Z"
    with pytest.raises(BenchmarkError, match="canonical UTC"):
        parse_manifest(seal_manifest_document(bad_prime))

    manifest = parse_manifest(
        seal_manifest_document(_draft_manifest(providers=("clusy", "firecrawl")))
    )
    with pytest.raises(BenchmarkError, match="canonical UTC"):
        _safe_run_id(manifest, created_at="2026-07-28T12:00:00Z")


def test_v3_rejects_temporal_inversion_noncanonical_currency_and_negative_zero() -> None:
    future_reference = _draft_manifest(providers=("clusy", "firecrawl"))
    future_reference["tasks"][0]["reference"]["captured_at"] = "2026-07-28T12:00:01.000000Z"
    with pytest.raises(BenchmarkError, match=r"reference\.captured_at must be at or before"):
        parse_manifest(seal_manifest_document(future_reference))

    lowercase_currency = _draft_manifest(providers=("clusy", "firecrawl"))
    lowercase_currency["pricing"]["clusy"]["currency"] = "usd"
    with pytest.raises(BenchmarkError, match="canonical currency spelling USD"):
        parse_manifest(seal_manifest_document(lowercase_currency))

    negative_zero = _draft_manifest(providers=("clusy", "firecrawl"))
    negative_zero["budgets"]["clusy_usd"] = -0.0
    with pytest.raises(BenchmarkError, match="finite and at least"):
        parse_manifest(seal_manifest_document(negative_zero))


@pytest.mark.parametrize(
    "url",
    [
        "https://%65xample.com/page",
        "https://bad_.example.com/page",
        "https://example.com/page?safe=1;api_key=secret",
        "https://example.com/page?safe=1%3Bapi_key=secret",
    ],
)
def test_v3_rejects_escaped_or_invalid_hosts_and_ambiguous_query_names(
    url: str,
) -> None:
    draft = _draft_manifest(task_count=1, providers=("clusy", "firecrawl"))
    draft["tasks"][0]["url"] = url
    with pytest.raises(BenchmarkError):
        parse_manifest(seal_manifest_document(draft))


def test_v3_rejects_legacy_v2_and_unknown_nested_manifest_fields() -> None:
    legacy = _draft_manifest(providers=("clusy", "firecrawl"))
    legacy["schema_version"] = "clusy.live-vendor.fixed-url.v2"
    with pytest.raises(BenchmarkError, match="pinned v2 runner"):
        parse_manifest(seal_manifest_document(legacy))

    unknown_pricing = _draft_manifest(providers=("clusy", "firecrawl"))
    unknown_pricing["pricing"]["firecrawl"]["raw_vendor_payload"] = {}
    with pytest.raises(BenchmarkError, match="pricing.firecrawl has unknown fields"):
        parse_manifest(seal_manifest_document(unknown_pricing))


def test_v3_seals_bootstrap_samples_and_bounds_benchmark_id() -> None:
    missing_bootstrap = _draft_manifest(providers=("clusy", "firecrawl"))
    missing_bootstrap.pop("bootstrap_samples")
    with pytest.raises(BenchmarkError, match="bootstrap_samples"):
        parse_manifest(seal_manifest_document(missing_bootstrap))

    maximum_id = _draft_manifest(providers=("clusy", "firecrawl"))
    maximum_id["benchmark_id"] = "a" * 98
    maximum_manifest = parse_manifest(seal_manifest_document(maximum_id))
    assert len(_safe_run_id(maximum_manifest)) == 128

    oversized_id = _draft_manifest(providers=("clusy", "firecrawl"))
    oversized_id["benchmark_id"] = "a" * 99
    with pytest.raises(BenchmarkError, match="benchmark_id is empty or too long"):
        parse_manifest(seal_manifest_document(oversized_id))


def test_execution_bootstrap_override_must_match_manifest_before_executor(
    tmp_path: Path,
) -> None:
    draft = _draft_manifest(task_count=1, providers=("clusy", "firecrawl"))
    draft["bootstrap_samples"] = 101
    document = seal_manifest_document(draft)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    factory_called = False

    def executor_factory() -> HttpxRequestExecutor:
        nonlocal factory_called
        factory_called = True
        return _mock_executor_factory(_success_handler())()

    with pytest.raises(BenchmarkError, match="must exactly match the sealed manifest value 101"):
        execute_benchmark(
            manifest_path=manifest_path,
            output_root=tmp_path / "bench" / "results",
            repo_root=tmp_path,
            execute_paid=True,
            nonclaimable=False,
            credentials=_credentials(exa_api_key=""),
            executor_factory=executor_factory,
            git_state=_clean_git_state(),
            container_digest=_CONTAINER_DIGEST,
            bootstrap_samples=100,
            **_execution_kwargs(document),
        )
    assert factory_called is False


def test_v1_is_loadable_offline_but_cannot_execute_paid(tmp_path: Path) -> None:
    document = seal_manifest_document(_v1_draft_manifest())
    assert parse_manifest(document).schema_version == V1_SCHEMA_VERSION
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(BenchmarkError, match="offline-compatible only"):
        execute_benchmark(
            manifest_path=path,
            output_root=tmp_path / "bench" / "results",
            repo_root=tmp_path,
            execute_paid=True,
            nonclaimable=False,
            credentials=_credentials(),
            confirmed_manifest_sha256=document["manifest_sha256"],
            max_exa_usd=1,
            max_firecrawl_credits=1,
            max_clusy_usd=1,
        )


def test_nonclaimable_context_canonicalizes_provenance_and_reason_order(
    tmp_path: Path,
) -> None:
    context = prepare_claim_context(
        repo_root=tmp_path,
        nonclaimable=True,
        git_state=GitState(
            commit="RAW_COMMIT_CARRIER",
            clean=False,
            runner_committed=True,
            detail="not retained",
        ),
        container_digest="RAW_CONTAINER_CARRIER",
    )
    assert context.claimable is False
    assert context.watermark == "NONCLAIMABLE"
    assert context.reasons == (
        "operator explicitly selected nonclaimable mode",
        "working tree is dirty",
        "runner is uncommitted or differs from HEAD",
        "CONTAINER_DIGEST is missing or not a sha256 digest",
    )
    assert context.runner_commit == "unknown"
    assert context.container_digest == "unknown"


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://example.com/page", "must use HTTPS"),
        ("https://127.0.0.1/page", "private or special IP"),
        ("https://localhost/page", "public DNS host"),
        ("https://example.com/page?token=value", "sensitive query"),
        ("https://example.com/page?X-Amz-Signature=value", "sensitive query"),
    ],
)
def test_v3_rejects_unsafe_target_urls(url: str, message: str) -> None:
    draft = _draft_manifest(providers=("clusy", "firecrawl"))
    draft["tasks"][0]["url"] = url
    with pytest.raises(BenchmarkError, match=message):
        parse_manifest(seal_manifest_document(draft))


def test_canonical_urls_and_domain_units_cannot_inflate_evidence() -> None:
    assert (
        canonical_task_url_identity("https://EXAMPLE.com.:443/benchmark/%70age?x=%7e")
        == "https://example.com/benchmark/page?x=~"
    )
    assert canonical_task_url_identity(
        "https://example.com/page?b=2&a=1"
    ) == canonical_task_url_identity("https://example.com/page?a=1&b=2")
    assert derive_domain_cluster("https://a.b.example.com/page") == "example.com"

    duplicate = _draft_manifest(
        task_count=2,
        providers=("clusy", "firecrawl"),
    )
    duplicate["tasks"][1]["url"] = "https://EXAMPLE.com.:443/benchmark/%70age/0"
    with pytest.raises(BenchmarkError, match="canonically equivalent"):
        parse_manifest(seal_manifest_document(duplicate))

    arbitrary_cluster = _draft_manifest(
        task_count=1,
        providers=("clusy", "firecrawl"),
    )
    arbitrary_cluster["tasks"][0]["url"] = "https://sub.example.com/page"
    arbitrary_cluster["tasks"][0]["domain_cluster"] = "made-up-cluster"
    with pytest.raises(BenchmarkError, match="must equal derived value example.com"):
        parse_manifest(seal_manifest_document(arbitrary_cluster))


def test_structure_strata_are_derived_from_reference_text_not_labels() -> None:
    inflated = _draft_manifest(
        task_count=1,
        providers=("clusy", "firecrawl"),
    )
    reference = inflated["tasks"][0]["reference"]
    reference["text"] = "# One real heading"
    reference["sha256"] = sha256_bytes(reference["text"].encode())
    reference["structure"] = {
        "headings": ["1:Invented heading with the same component presence"],
        "list_items": [],
        "code_blocks": [],
        "tables": [],
    }
    with pytest.raises(BenchmarkError, match="must exactly match deterministic parsing"):
        parse_manifest(seal_manifest_document(inflated))

    draft = _draft_manifest(
        task_count=3,
        providers=("clusy", "firecrawl"),
    )
    for index, task in enumerate(draft["tasks"]):
        task["url"] = f"https://docs{index}.example{index}.com/page"
        task["domain_cluster"] = f"example{index}.com"
        task["stratum"] = f"operator-label-{index}"
        text = f"# Heading {index}"
        task["reference"] = {
            "text": text,
            "sha256": sha256_bytes(text.encode()),
            "method": "blinded-human-v2",
            "captured_at": "2026-07-28T12:00:00.000000Z",
            "structure": {
                "headings": [f"1:Heading {index}"],
                "list_items": [],
                "code_blocks": [],
                "tables": [],
            },
        }
    manifest = parse_manifest(seal_manifest_document(draft))
    events: list[JsonObject] = []
    for task in manifest.tasks:
        for provider, score in (("clusy", 1.0), ("firecrawl", 0.8)):
            events.append(
                {
                    "task_id": task.task_id,
                    "provider": provider,
                    "attempt": 1,
                    "status": "ok",
                    "latency_ms": 100,
                    "normalized_cost": 0.001,
                    "scoring": {
                        "token_f1": score,
                        "structure_score": score,
                    },
                }
            )
    summary = summarize_events(manifest, events, bootstrap_samples=100)
    structure_row = next(row for row in summary["pairwise"] if row["metric"] == "structure_score")
    assert {row["stratum"] for row in structure_row["by_stratum"]} == {"headings"}
    assert all(
        row["stratum_basis"] == "reference_component_presence.v1"
        for row in structure_row["by_stratum"]
    )


def test_manifest_rejects_bad_reference_hash_and_unmatched_geography() -> None:
    draft = _draft_manifest()
    draft["tasks"][0]["reference"]["sha256"] = "0" * 64
    with pytest.raises(BenchmarkError, match="reference sha256 mismatch"):
        parse_manifest(seal_manifest_document(draft))

    draft = _draft_manifest()
    draft["country"] = "US"
    with pytest.raises(BenchmarkError, match="country and location must be null"):
        parse_manifest(seal_manifest_document(draft))

    document = seal_manifest_document(_draft_manifest())
    document["corpus_sha256"] = "0" * 64
    document["manifest_sha256"] = calculate_manifest_sha256(document)
    with pytest.raises(BenchmarkError, match="exact ordered task"):
        parse_manifest(document)


def test_manifest_requires_explicit_benchmark_only_exa_authorization() -> None:
    draft = _draft_manifest()
    draft["compliance_acknowledgments"]["exa_live_authorized"] = False
    with pytest.raises(BenchmarkError, match="exa_live_authorized=true"):
        parse_manifest(seal_manifest_document(draft))

    draft = _draft_manifest()
    draft["compliance_acknowledgments"]["exa_authorized_purpose"] = "training"
    with pytest.raises(BenchmarkError, match="benchmark_only"):
        parse_manifest(seal_manifest_document(draft))


def test_exa_manifest_enforces_official_character_and_exact_hour_caps() -> None:
    draft = _draft_manifest()
    draft["request_policy"]["max_output_characters"] = 10_001
    with pytest.raises(BenchmarkError, match=r"\[1, 10000\]"):
        parse_manifest(seal_manifest_document(draft))

    draft = _draft_manifest(mode="warm_cache")
    draft["cache_max_age_seconds"] = 3601
    with pytest.raises(BenchmarkError, match="exact multiple of 3600"):
        parse_manifest(seal_manifest_document(draft))

    draft = _draft_manifest(mode="warm_cache")
    draft["warm_cache_primed_at"] = "2026-07-28T12:00:01.000000Z"
    with pytest.raises(BenchmarkError, match="at or before manifest.created_at"):
        parse_manifest(seal_manifest_document(draft))


def test_firecrawl_paid_track_refuses_unbounded_proxy_and_pdf_costs() -> None:
    for proxy in ("auto", "enhanced"):
        draft = _draft_manifest(providers=("clusy", "firecrawl"))
        draft["request_policy"]["firecrawl_proxy"] = proxy
        with pytest.raises(BenchmarkError, match="requires firecrawl_proxy=basic"):
            parse_manifest(seal_manifest_document(draft))

    draft = _draft_manifest(providers=("clusy", "firecrawl"))
    draft["request_policy"]["firecrawl_parse_pdf"] = True
    with pytest.raises(BenchmarkError, match="PDF parsing is billed per page"):
        parse_manifest(seal_manifest_document(draft))

    draft = _draft_manifest(providers=("clusy", "firecrawl"))
    draft["tasks"][0]["render_class"] = "pdf"
    draft["tasks"][0]["content_type"] = "application/pdf"
    with pytest.raises(BenchmarkError, match="excludes PDF tasks"):
        parse_manifest(seal_manifest_document(draft))


def test_randomized_order_uses_only_selected_providers() -> None:
    manifest = parse_manifest(
        seal_manifest_document(
            _draft_manifest(
                task_count=8,
                providers=("clusy", "firecrawl"),
            )
        )
    )
    first = randomized_orders(manifest)
    assert first == randomized_orders(manifest)
    assert all(set(order) == {"clusy", "firecrawl"} for order in first.values())
    assert all("exa" not in order for order in first.values())
    assert len(set(first.values())) > 1


def test_execution_refuses_without_paid_flag_before_executor_creation(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        providers=("clusy", "firecrawl"),
    )
    created = False

    def forbidden_factory() -> HttpxRequestExecutor:
        nonlocal created
        created = True
        raise AssertionError("network executor must not be created")

    with pytest.raises(BenchmarkError, match="--execute-paid"):
        execute_benchmark(
            manifest_path=manifest_path,
            output_root=tmp_path / "bench" / "results",
            repo_root=tmp_path,
            execute_paid=False,
            nonclaimable=False,
            credentials=_credentials(),
            executor_factory=forbidden_factory,
            git_state=_clean_git_state(),
            container_digest=_CONTAINER_DIGEST,
            **_execution_kwargs(document),
        )
    assert created is False


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"confirmed_manifest_sha256": "0" * 64}, "confirm-manifest"),
        ({"max_firecrawl_credits": 0}, "below task credit caps"),
        ({"max_clusy_usd": 0}, "below the frozen request estimate"),
    ],
)
def test_digest_and_budget_refusals_happen_before_executor_creation(
    tmp_path: Path,
    override: JsonObject,
    message: str,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "firecrawl"),
    )
    execution = _execution_kwargs(document)
    execution.update(override)
    created = False

    def forbidden_factory() -> HttpxRequestExecutor:
        nonlocal created
        created = True
        raise AssertionError("network executor must not be created")

    with pytest.raises(BenchmarkError, match=message):
        execute_benchmark(
            manifest_path=manifest_path,
            output_root=tmp_path / "bench" / "results",
            repo_root=tmp_path,
            execute_paid=True,
            nonclaimable=False,
            credentials=_credentials(),
            executor_factory=forbidden_factory,
            git_state=_clean_git_state(),
            container_digest=_CONTAINER_DIGEST,
            **execution,
        )
    assert created is False


def test_exa_requires_runtime_acknowledgment_before_executor_creation(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "exa"),
    )
    execution = _execution_kwargs(document)
    execution["acknowledge_exa_live_use"] = False
    created = False

    def forbidden_factory() -> HttpxRequestExecutor:
        nonlocal created
        created = True
        raise AssertionError("network executor must not be created")

    with pytest.raises(BenchmarkError, match="acknowledge-exa-live-use"):
        execute_benchmark(
            manifest_path=manifest_path,
            output_root=tmp_path / "bench" / "results",
            repo_root=tmp_path,
            execute_paid=True,
            nonclaimable=False,
            credentials=_credentials(),
            executor_factory=forbidden_factory,
            git_state=_clean_git_state(),
            container_digest=_CONTAINER_DIGEST,
            **execution,
        )
    assert created is False


def test_private_dns_answer_is_rejected_before_executor_creation(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "firecrawl"),
    )
    execution = _execution_kwargs(document)
    execution["dns_resolver"] = lambda _host, _port: [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("127.0.0.1", 443),
        )
    ]
    created = False

    def forbidden_factory() -> HttpxRequestExecutor:
        nonlocal created
        created = True
        raise AssertionError("network executor must not be created")

    with pytest.raises(BenchmarkError, match="private or special address"):
        execute_benchmark(
            manifest_path=manifest_path,
            output_root=tmp_path / "bench" / "results",
            repo_root=tmp_path,
            execute_paid=True,
            nonclaimable=False,
            credentials=_credentials(),
            executor_factory=forbidden_factory,
            git_state=_clean_git_state(),
            container_digest=_CONTAINER_DIGEST,
            **execution,
        )
    assert created is False


def test_clusy_preflight_requires_preregistered_service_image_digest(
    tmp_path: Path,
) -> None:
    missing = _draft_manifest(providers=("clusy", "firecrawl"))
    missing["clusy_binding"].pop("expected_image_digest")
    with pytest.raises(BenchmarkError, match="expected_image_digest"):
        parse_manifest(seal_manifest_document(missing))

    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "firecrawl"),
    )
    paid_posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal paid_posts
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "sha": _REVISION,
                    "config_fingerprint": _CONFIG_SHA,
                    "image_digest": "sha256:" + ("d" * 64),
                },
            )
        paid_posts += 1
        return _success_handler()(request)

    with pytest.raises(BenchmarkError, match="image digest does not match"):
        _run(
            tmp_path=tmp_path,
            manifest_path=manifest_path,
            document=document,
            handler=handler,
        )
    assert paid_posts == 0


def test_provider_subset_never_requires_or_calls_exa(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "firecrawl"),
    )
    observed: list[httpx.Request] = []
    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=_success_handler(observed=observed),
        credentials=_credentials(exa_api_key=""),
    )
    assert all(request.url.host != "api.exa.ai" for request in observed)
    assert {event["provider"] for event in _load_events(run_directory)} == {
        "clusy",
        "firecrawl",
    }
    summary = json.loads((run_directory / "summary.json").read_text())
    assert summary["evaluated_providers"] == ["clusy", "firecrawl"]


def test_matched_requests_and_hash_only_evidence(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(manifest_path, task_count=1)
    observed: list[httpx.Request] = []
    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=_success_handler(observed=observed),
    )

    post_by_host = {request.url.host: request for request in observed if request.method == "POST"}
    assert set(post_by_host) == {"clusy.test", "api.exa.ai", "api.firecrawl.dev"}
    version_request = next(request for request in observed if request.method == "GET")
    assert str(version_request.url) == "https://clusy.test/health/version"

    exa = post_by_host["api.exa.ai"]
    assert str(exa.url) == EXA_ENDPOINT
    assert json.loads(exa.content) == {
        "urls": ["https://example.com/benchmark/page/0"],
        "text": {
            "maxCharacters": 10_000,
        },
        "maxAgeHours": 0,
        "livecrawlTimeout": 60_000,
    }
    assert exa.headers["x-api-key"] == "dummy-exa-secret"

    firecrawl = post_by_host["api.firecrawl.dev"]
    assert str(firecrawl.url) == FIRECRAWL_ENDPOINT
    assert json.loads(firecrawl.content) == {
        "url": "https://example.com/benchmark/page/0",
        "formats": ["markdown"],
        "onlyMainContent": True,
        "maxAge": 0,
        "storeInCache": True,
        "timeout": 60_000,
        "onlyCleanContent": False,
        "blockAds": True,
        "removeBase64Images": True,
        "skipTlsVerification": False,
        "proxy": "basic",
        "parsers": [],
    }
    assert firecrawl.headers["authorization"] == "Bearer dummy-fire-secret"

    clusy = post_by_host["clusy.test"]
    assert str(clusy.url) == "https://clusy.test/crawl"
    assert json.loads(clusy.content) == {
        "urls": ["https://example.com/benchmark/page/0"],
        "max_pages": 1,
        "formats": ["markdown"],
        "max_age": 0,
        "extraction_profile": "adaptive",
        "js_render": None,
    }

    events = _load_events(run_directory)
    assert len(events) == 3
    assert [event["provider"] for event in events] == list(
        randomized_orders(parse_manifest(document))["task-0"]
    )
    for event in events:
        assert event["event_schema_version"] == EVENT_SCHEMA_VERSION
        assert event["claimable"] is True
        assert event["hard_deadline_enforced"] is True
        assert event["cache_evidence_matches_mode"] is True
        assert not {
            "publication_timestamp",
            "fetch_timestamp",
            "snippet",
            "highlights",
            "citation_links",
            "domain_filters",
            "country",
            "location",
            "error",
            "watermark",
            "nonclaimable_reasons",
            "cache_policy",
            "content_format",
            "corpus_sha256",
            "provider_fetch_geo_control",
            "original_url",
            "query_or_seed",
            "text",
            "title",
            "warning",
            "truncation_reason",
            "extraction_strategy",
            "content_scope",
        } & set(event)
        assert event["benchmark_output_cap_characters"] == 10_000
        assert event["benchmark_output_cap_applied"] is False
        assert event["scoring"]["token_f1"] == 1
        assert event["scoring"]["structure_score"] == 1
        assert len(event["raw_response_sha256"]) == 64
        assert len(event["normalized_text_sha256"]) == 64
        assert len(event["endpoint_sha256"]) == 64
    clusy_event = next(event for event in events if event["provider"] == "clusy")
    assert clusy_event["endpoint"] == "[CLUSY_ENDPOINT_REDACTED]"

    persisted = b"\n".join(path.read_bytes() for path in run_directory.rglob("*") if path.is_file())
    assert b"DO_NOT_PERSIST_PROVIDER_RAW_OUTPUT" not in persisted
    for secret in (
        b"dummy-exa-secret",
        b"dummy-fire-secret",
        b"dummy-clusy-secret",
    ):
        assert secret not in persisted
    assert b"https://clusy.test" not in persisted
    assert not (run_directory / "raw").exists()
    assert not (run_directory / "normalized").exists()

    summary = json.loads((run_directory / "summary.json").read_text())
    assert summary["claimable"] is True
    assert summary["vendor_win_claimable"] is False
    assert summary["provider_output_retention_policy"] == ("hashes_and_derived_metrics_only")
    assert summary["raw_provider_outputs_retained"] is False
    assert summary["vendor_win_gate"]["passed"] is False
    assert {item["provider"] for item in summary["provider_summaries"]} == {
        "clusy",
        "exa",
        "firecrawl",
    }
    clusy_summary = next(
        item for item in summary["provider_summaries"] if item["provider"] == "clusy"
    )
    assert clusy_summary["model_rate"] == 0
    assert clusy_summary["quality_attempt_rate"] == 1
    assert clusy_summary["quality_success_rate_when_reported"] == 1
    assert clusy_summary["completeness_score"]["mean"] == 0.99
    assert clusy_summary["stage_timings_ms"]["total"]["mean"] == 6


def test_warm_cache_requests_and_evidence_are_matched(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "firecrawl"),
        mode="warm_cache",
    )
    observed: list[httpx.Request] = []
    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=_success_handler(mode="warm_cache", observed=observed),
    )
    fire_request = next(request for request in observed if request.url.host == "api.firecrawl.dev")
    assert json.loads(fire_request.content)["maxAge"] == 3_600_000
    assert json.loads(fire_request.content)["storeInCache"] is True
    clusy_request = next(
        request
        for request in observed
        if request.method == "POST" and request.url.host == "clusy.test"
    )
    assert json.loads(clusy_request.content)["max_age"] == 3600
    assert all(
        event["cache_state"] == "hit" and event["cache_evidence_matches_mode"] is True
        for event in _load_events(run_directory)
    )
    fire_event = next(
        event for event in _load_events(run_directory) if event["provider"] == "firecrawl"
    )
    assert fire_event["cache_evidence_source"] == "undocumented_response_field"
    assert fire_event["cache_evidence_contractually_documented"] is False
    assert fire_event["credit_evidence_contractually_documented"] is False
    summary = json.loads((run_directory / "summary.json").read_text())
    assert summary["vendor_win_gate"]["contractual_cache_and_credit_evidence_complete"] is False


def test_firecrawl_missing_credits_is_conservative_and_never_claim_evidence(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "firecrawl"),
    )
    base_handler = _success_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.firecrawl.dev":
            return base_handler(request)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "markdown": _reference_text(0),
                    "metadata": {
                        "sourceURL": body["url"],
                        "cacheState": "miss",
                        "statusCode": 200,
                    },
                },
            },
        )

    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=handler,
    )
    fire_event = next(
        event for event in _load_events(run_directory) if event["provider"] == "firecrawl"
    )
    assert fire_event["credits"] is None
    assert fire_event["credit_evidence_source"] == "sealed_task_cap_fallback"
    summary = json.loads((run_directory / "summary.json").read_text())
    assert summary["observed_budget_ledger"]["firecrawl_credits"] == 1

    aggregate = aggregate_v3_summaries(
        [_aggregate_run(mode, index) for mode in ("cold_live", "warm_cache") for index in (1, 2)]
    )
    assert any(
        "provider-side per-request spend cap" in reason
        for reason in aggregate["gate"]["by_provider"]["firecrawl"]["reasons"]
    )


def test_exa_cold_and_warm_use_consistent_compact_diagnostic_scope(
    tmp_path: Path,
) -> None:
    text_requests: list[JsonObject] = []
    for mode in ("cold_live", "warm_cache"):
        manifest_path = tmp_path / f"{mode}-manifest.json"
        document = _write_manifest(
            manifest_path,
            task_count=1,
            providers=("clusy", "exa"),
            mode=mode,
        )
        observed: list[httpx.Request] = []
        run_directory = _run(
            tmp_path=tmp_path / mode,
            manifest_path=manifest_path,
            document=document,
            handler=_success_handler(mode=mode, observed=observed),
            credentials=_credentials(firecrawl_api_key=""),
        )
        exa_request = next(request for request in observed if request.url.host == "api.exa.ai")
        body = json.loads(exa_request.content)
        text_requests.append(body["text"])
        assert "includeSections" not in body["text"]
        assert "verbosity" not in body["text"]
        assert body["maxAgeHours"] == (0 if mode == "cold_live" else 1)
        exa_event = next(
            event for event in _load_events(run_directory) if event["provider"] == "exa"
        )
        assert exa_event["quality_scope"] == ("provider_default_compact_main_content")
        assert exa_event["quality_scope_comparable_to_full_main_content"] is False
    assert text_requests == [{"maxCharacters": 10_000}] * 2


def test_same_post_normalization_character_cap_applies_to_every_provider(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(manifest_path, task_count=1)
    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=_success_handler(text_override="x" * 12_000),
    )
    events = _load_events(run_directory)
    assert {event["character_count"] for event in events} == {10_000}
    assert {event["benchmark_output_cap_characters"] for event in events} == {10_000}
    assert all(event["benchmark_output_cap_applied"] is True for event in events)
    assert all(event["truncated"] is True for event in events)
    assert len({event["normalized_text_sha256"] for event in events}) == 1


def test_first_attempt_error_is_redacted_without_raw_retention(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "firecrawl"),
    )
    calls: Counter[str] = Counter()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "sha": _REVISION,
                    "config_fingerprint": _CONFIG_SHA,
                    "image_digest": _SERVICE_IMAGE_DIGEST,
                },
            )
        host = request.url.host or ""
        calls[host] += 1
        if host == "api.firecrawl.dev":
            return httpx.Response(
                503,
                json={
                    "error": (
                        "blocked at https://example.com/private?token=value "
                        "DO_NOT_PERSIST_PROVIDER_RAW_OUTPUT"
                    )
                },
            )
        return _success_handler()(request)

    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=handler,
        nonclaimable=True,
    )
    assert calls == {"clusy.test": 1, "api.firecrawl.dev": 1}
    fire_event = next(
        event for event in _load_events(run_directory) if event["provider"] == "firecrawl"
    )
    assert fire_event["status"] == "http_error"
    assert fire_event["attempt"] == 1
    assert "error" not in fire_event
    assert "error_detail_retained" not in fire_event
    assert "raw_response_retained" not in fire_event
    assert not (run_directory / "raw").exists()
    persisted = b"\n".join(path.read_bytes() for path in run_directory.rglob("*") if path.is_file())
    assert b"DO_NOT_PERSIST_PROVIDER_RAW_OUTPUT" not in persisted


@pytest.mark.parametrize(
    ("status_code", "body", "expected_status"),
    [
        (200, b"", "malformed_response"),
        (200, b"not-json", "malformed_response"),
        (503, b"not-json-error", "http_error"),
    ],
)
def test_clusy_fixed_accounting_survives_empty_malformed_and_error_bodies(
    tmp_path: Path,
    status_code: int,
    body: bytes,
    expected_status: str,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "firecrawl"),
    )
    success = _success_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" or request.url.host == "api.firecrawl.dev":
            return success(request)
        return httpx.Response(status_code, content=body)

    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=handler,
        credentials=_credentials(exa_api_key=""),
    )
    verify_completed_run_directory(run_directory)
    clusy_event = next(
        event for event in _load_events(run_directory) if event["provider"] == "clusy"
    )
    assert clusy_event["status"] == expected_status
    assert clusy_event["credits"] == 0.0
    assert clusy_event["provider_reported_cost"] == 0.0
    assert clusy_event["provider_reported_cost_currency"] == "USD"
    assert clusy_event["character_count"] == 0
    assert clusy_event["cache_state"] == "unknown"
    assert clusy_event["cache_hit"] is None


def test_provider_cache_and_origin_evidence_parsing() -> None:
    exa = normalize_provider_response(
        "exa",
        WireResponse(
            200,
            {},
            json.dumps(
                {
                    "statuses": [{"status": "success", "source": "crawled"}],
                    "results": [{"text": "x"}],
                }
            ).encode(),
            "2026-07-28T00:00:00Z",
            "2026-07-28T00:00:00Z",
            "2026-07-28T00:00:01Z",
            1,
            None,
        ),
    )
    assert exa.status == "ok"
    assert exa.cache_state == "miss"

    fire = normalize_provider_response(
        "firecrawl",
        WireResponse(
            200,
            {},
            json.dumps(
                {
                    "success": True,
                    "data": {
                        "markdown": "x",
                        "metadata": {"cacheState": "hit", "statusCode": 200},
                    },
                }
            ).encode(),
            "2026-07-28T00:00:00Z",
            "2026-07-28T00:00:00Z",
            "2026-07-28T00:00:01Z",
            1,
            None,
        ),
    )
    assert fire.status == "ok"
    assert fire.cache_state == "hit"
    assert fire.origin_status_code == 200

    clusy = normalize_provider_response(
        "clusy",
        WireResponse(
            200,
            {},
            json.dumps(
                {
                    "results": [
                        {
                            "markdown": "x",
                            "cached": False,
                            "metadata": {
                                "extraction_strategy": "model-looking-name",
                                "model_assisted": False,
                                "quality_attempted": True,
                                "quality_succeeded": False,
                                "completeness_score": 0.8,
                                "stage_timings_ms": {
                                    "queue": 1,
                                    "fetch": 2,
                                    "render": 0,
                                    "extraction": 3,
                                    "total": 6,
                                    "unregistered": 999,
                                },
                            },
                        }
                    ]
                }
            ).encode(),
            "2026-07-28T00:00:00Z",
            "2026-07-28T00:00:00Z",
            "2026-07-28T00:00:01Z",
            1,
            None,
        ),
    )
    assert clusy.model_used is False
    assert clusy.quality_attempted is True
    assert clusy.quality_succeeded is False
    assert clusy.completeness_score == 0.8
    assert set(clusy.stage_timings_ms) == {
        "queue",
        "fetch",
        "render",
        "extraction",
        "total",
    }


def test_exa_per_url_error_is_a_failed_attempt() -> None:
    body = json.dumps(
        {
            "statuses": [
                {
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
    result = normalize_provider_response(
        "exa",
        WireResponse(
            200,
            {},
            body,
            "2026-07-28T00:00:00Z",
            None,
            "2026-07-28T00:01:00Z",
            60_000,
            None,
        ),
    )
    assert result.status == "provider_error"
    assert result.error == "Exa per-URL error: CRAWL_LIVECRAWL_TIMEOUT (HTTP 504)"


def test_normalization_text_and_structure_scoring_are_deterministic() -> None:
    assert normalize_text(" Alpha  \r\nBeta\x00\r\n") == "Alpha\nBeta"
    text_score = score_text("Alpha 中文 beta beta", "alpha 中文 beta")
    assert text_score["token_recall"] == 1
    assert text_score["token_precision"] == pytest.approx(4 / 5)
    assert text_score["token_f1"] == pytest.approx(8 / 9)

    manifest = parse_manifest(
        seal_manifest_document(
            _draft_manifest(
                task_count=1,
                providers=("clusy", "firecrawl"),
            )
        )
    )
    reference = manifest.tasks[0].reference
    assert reference is not None
    assert reference.structure is not None
    structure_score = score_structure(_reference_text(0), reference.structure)
    assert structure_score["heading_f1"] == 1
    assert structure_score["list_f1"] == 1
    assert structure_score["code_f1"] == 1
    assert structure_score["table_tree_similarity"] == 1
    assert structure_score["structure_score"] == 1


def test_bootstrap_and_descriptive_summary_are_deterministic() -> None:
    assert deterministic_bootstrap_ci(
        [0.1, 0.2, -0.1, 0.4],
        seed=7,
        samples=500,
    ) == deterministic_bootstrap_ci(
        [0.1, 0.2, -0.1, 0.4],
        seed=7,
        samples=500,
    )
    manifest = parse_manifest(
        seal_manifest_document(
            _draft_manifest(
                task_count=2,
                providers=("clusy", "firecrawl"),
            )
        )
    )
    events: list[JsonObject] = []
    for task in manifest.tasks:
        for provider, score in (("clusy", 1.0), ("firecrawl", 0.8)):
            events.append(
                {
                    "task_id": task.task_id,
                    "provider": provider,
                    "attempt": 1,
                    "status": "ok",
                    "latency_ms": 100,
                    "normalized_cost": 0.001,
                    "claimable": True,
                    "manifest_sha256": manifest.digest,
                    "scoring": {
                        "token_f1": score,
                        "structure_score": score,
                    },
                }
            )
    first = summarize_events(manifest, events, bootstrap_samples=100)
    second = summarize_events(manifest, events, bootstrap_samples=100)
    assert first == second
    row = next(item for item in first["pairwise"] if item["metric"] == "token_f1")
    assert row["mean_delta_left_minus_right"] == pytest.approx(0.2)
    assert row["paired_task_count"] == 2
    assert row["paired_domain_cluster_count"] == 1
    assert first["claimable"] is False
    assert first["vendor_win_claimable"] is False


def test_offline_score_supports_hash_only_v3_and_rejects_retention_tamper(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "firecrawl"),
    )
    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=_success_handler(),
    )
    events_path = run_directory / "events.jsonl"
    offline_summary = tmp_path / "offline-summary.json"
    score_existing_run(
        manifest_path=manifest_path,
        events_path=events_path,
        output_path=offline_summary,
        bootstrap_samples=100,
    )
    assert json.loads(offline_summary.read_text())["claimable"] is True

    tampered_events = _load_events(run_directory)
    tampered_events[0]["text"] = "provider output must not be retained"
    tampered_path = tmp_path / "tampered-events.jsonl"
    tampered_path.write_text(
        "".join(json.dumps(event) + "\n" for event in tampered_events),
        encoding="utf-8",
    )
    with pytest.raises(BenchmarkError, match="fields do not match"):
        score_existing_run(
            manifest_path=manifest_path,
            events_path=tampered_path,
            output_path=tmp_path / "tampered-summary.json",
            bootstrap_samples=100,
        )


def _timing_pair(
    index: int,
    cold_start: datetime,
) -> tuple[WindowTimingEvidence, WindowTimingEvidence]:
    oldest_cold_completion = cold_start + timedelta(seconds=30)
    cold_completion = cold_start + timedelta(minutes=2)
    cold = WindowTimingEvidence(
        run_id=f"cold-{index}",
        mode="cold_live",
        time_window_id=f"window-{index}",
        independent_window_index=index,
        required_independent_windows=2,
        cache_max_age_seconds=0,
        manifest_created_at=cold_start - timedelta(hours=1),
        warm_cache_primed_at=None,
        run_created_at=cold_start - timedelta(minutes=1),
        first_request_started_at=cold_start,
        oldest_request_completed_at=oldest_cold_completion,
        last_request_completed_at=cold_start + timedelta(minutes=1),
        completion_at=cold_completion,
    )
    warm = WindowTimingEvidence(
        run_id=f"warm-{index}",
        mode="warm_cache",
        time_window_id=f"window-{index}",
        independent_window_index=index,
        required_independent_windows=2,
        cache_max_age_seconds=3600,
        manifest_created_at=cold_completion + timedelta(seconds=10),
        warm_cache_primed_at=oldest_cold_completion,
        run_created_at=cold_completion + timedelta(seconds=20),
        first_request_started_at=cold_completion + timedelta(seconds=30),
        oldest_request_completed_at=cold_completion + timedelta(seconds=35),
        last_request_completed_at=cold_completion + timedelta(seconds=40),
        completion_at=cold_completion + timedelta(seconds=50),
    )
    return cold, warm


def test_window_timing_requires_artifact_bound_prime_and_24h_spacing() -> None:
    first = _timing_pair(1, datetime(2026, 7, 28, tzinfo=UTC))
    second = _timing_pair(2, datetime(2026, 7, 29, tzinfo=UTC))
    assert validate_independent_window_timing([*first, *second]) == ()

    bad_prime = replace(
        first[1],
        warm_cache_primed_at=first[0].completion_at,
    )
    prime_reasons = validate_independent_window_timing([first[0], bad_prime, *second])
    assert any("not bound to the paired oldest cold" in reason for reason in prime_reasons)

    too_close = _timing_pair(
        2,
        first[0].first_request_started_at + timedelta(hours=1),
    )
    spacing_reasons = validate_independent_window_timing([*first, *too_close])
    assert any("less than 86400 seconds" in reason for reason in spacing_reasons)

    expired_warm = replace(
        first[1],
        last_request_completed_at=(first[0].oldest_request_completed_at + timedelta(seconds=3601)),
        completion_at=first[0].oldest_request_completed_at + timedelta(seconds=3602),
    )
    expiry_reasons = validate_independent_window_timing([first[0], expired_warm, *second])
    assert any(
        "not contained in its sealed cache-prime interval" in reason for reason in expiry_reasons
    )


def _aggregate_run(
    mode: str,
    index: int,
    *,
    paired_tasks: int = 100,
    domain_clusters: int = 30,
    stratum_names: tuple[str, ...] = ("headings", "lists", "code"),
    stratum_clusters: int = 10,
    clusy_latency_ms: float = 90,
    competitor_latency_ms: float = 100,
    clusy_cost: float = 0.0009,
    competitor_cost: float = 0.001,
) -> JsonObject:
    pairwise: list[JsonObject] = []
    for metric, interval in (
        ("token_f1", [0.01, 0.04]),
        ("structure_score", [0.01, 0.03]),
        ("success", [-0.01, 0.01]),
        (
            "latency_ms",
            (
                [-20.0, 0.0]
                if clusy_latency_ms <= competitor_latency_ms
                else [1.0, clusy_latency_ms - competitor_latency_ms]
            ),
        ),
        (
            "normalized_cost",
            (
                [-0.001, 0.0]
                if clusy_cost <= competitor_cost
                else [0.0001, clusy_cost - competitor_cost]
            ),
        ),
    ):
        pairwise.append(
            {
                "left_provider": "clusy",
                "right_provider": "firecrawl",
                "metric": metric,
                "paired_task_count": paired_tasks,
                "paired_domain_cluster_count": domain_clusters,
                "bootstrap_ci_95": interval,
                "by_stratum": (
                    [
                        {
                            "stratum": stratum,
                            "stratum_basis": "reference_component_presence.v1",
                            "paired_domain_cluster_count": stratum_clusters,
                            "bootstrap_ci_95": [0.0, 0.03],
                        }
                        for stratum in stratum_names
                    ]
                    if metric == "structure_score"
                    else []
                ),
            }
        )
    return {
        "summary_schema_version": SUMMARY_SCHEMA_VERSION,
        "bootstrap_samples": 100,
        "corpus_sha256": "c" * 64,
        "protocol_sha256": "d" * 64,
        "required_independent_time_windows": 2,
        "mode": mode,
        "cache_max_age_seconds": 0 if mode == "cold_live" else 3600,
        "time_window_id": f"window-{index}",
        "independent_window_index": index,
        "evaluated_providers": ["clusy", "firecrawl"],
        "artifact_integrity_claimable": True,
        "vendor_win_gate": {
            "structural_fidelity_metrics_complete": True,
            "cache_evidence_complete": True,
            "contractual_cache_and_credit_evidence_complete": True,
        },
        "provider_summaries": [
            {
                "provider": "clusy",
                "latency_ms": {
                    "p95": clusy_latency_ms,
                    "p99": clusy_latency_ms,
                },
                "normalized_cost": {"mean": clusy_cost},
            },
            {
                "provider": "firecrawl",
                "latency_ms": {
                    "p95": competitor_latency_ms,
                    "p99": competitor_latency_ms,
                },
                "normalized_cost": {"mean": competitor_cost},
            },
        ],
        "compliance_acknowledgments": {
            "third_party_data_transfer_authorized": True,
            "exa_live_authorized": False,
            "exa_authorized_purpose": "not_applicable",
        },
        "pairwise": pairwise,
    }


def test_arbitrary_summary_aggregate_is_descriptive_and_all_claims_stay_closed() -> None:
    incomplete = aggregate_v3_summaries([_aggregate_run("cold_live", 1)])
    assert incomplete["vendor_win_claimable"] is False

    complete = aggregate_v3_summaries(
        [
            _aggregate_run("cold_live", 1),
            _aggregate_run("cold_live", 2),
            _aggregate_run("warm_cache", 1),
            _aggregate_run("warm_cache", 2),
        ]
    )
    assert complete["vendor_win_claimable"] is False
    assert complete["vendor_win_claimable_by_provider"] == {
        "exa": False,
        "firecrawl": False,
    }
    assert complete["exa_vendor_win_claimable"] is False
    assert complete["claim_scope"]["clusy_compared_to"] == ["firecrawl"]
    assert complete["claim_scope"]["does_not_cover_unselected_providers"] is True
    assert complete["gate"]["by_provider"]["exa"]["passed"] is False
    assert "not selected" in complete["gate"]["by_provider"]["exa"]["reasons"][0]
    assert complete["artifact_chains_verified"] is False
    assert complete["quality_metrics_independently_verifiable"] is False
    assert complete["execution_attestation_verified"] is False
    firecrawl_reasons = complete["gate"]["by_provider"]["firecrawl"]["reasons"]
    assert any("not loaded from verified" in reason for reason in firecrawl_reasons)
    assert any("no verifiable execution attestation" in reason for reason in firecrawl_reasons)
    assert any("provider-side per-request spend cap" in reason for reason in firecrawl_reasons)
    assert complete["gate"]["minimum_evidence"] == {
        "paired_tasks_per_selected_competitor_per_run": 100,
        "paired_domain_clusters_per_selected_competitor_per_run": 30,
        "distinct_structure_strata_per_run": 3,
        "paired_domain_clusters_per_structure_stratum": 10,
    }


def test_aggregate_requires_one_preregistered_bootstrap_sample_count() -> None:
    summaries = [
        _aggregate_run(mode, index) for mode in ("cold_live", "warm_cache") for index in (1, 2)
    ]
    summaries[-1]["bootstrap_samples"] = 101
    aggregate = aggregate_v3_summaries(summaries)
    assert any(
        "pre-registered bootstrap sample count" in reason for reason in aggregate["gate"]["reasons"]
    )


def test_aggregate_keeps_one_task_one_cluster_pilot_descriptive() -> None:
    summaries = [
        _aggregate_run(mode, index, paired_tasks=1, domain_clusters=1)
        for mode in ("cold_live", "warm_cache")
        for index in (1, 2)
    ]
    aggregate = aggregate_v3_summaries(summaries)
    assert aggregate["run_count"] == 4
    assert aggregate["vendor_win_claimable"] is False
    assert aggregate["gate"]["by_provider"]["firecrawl"]["passed"] is False
    reasons = aggregate["gate"]["by_provider"]["firecrawl"]["reasons"]
    assert any("fewer than 100 paired tasks" in reason for reason in reasons)
    assert any("fewer than 30 paired domain clusters" in reason for reason in reasons)


@pytest.mark.parametrize(
    ("run_kwargs", "expected_reason"),
    [
        (
            {"stratum_names": ("headings", "lists")},
            "fewer than 3 distinct strata",
        ),
        (
            {"stratum_clusters": 9},
            "fewer than 10 paired domain clusters",
        ),
    ],
)
def test_aggregate_requires_broad_structure_strata(
    run_kwargs: JsonObject,
    expected_reason: str,
) -> None:
    summaries = [
        _aggregate_run(mode, index, **run_kwargs)
        for mode in ("cold_live", "warm_cache")
        for index in (1, 2)
    ]
    aggregate = aggregate_v3_summaries(summaries)
    assert aggregate["vendor_win_claimable"] is False
    assert any(
        expected_reason in reason
        for reason in aggregate["gate"]["by_provider"]["firecrawl"]["reasons"]
    )


@pytest.mark.parametrize(
    ("run_overrides", "reason_fragment"),
    [
        (
            {
                "clusy_latency_ms": 10_000,
                "competitor_latency_ms": 100,
            },
            "p95 latency exceeds",
        ),
        (
            {
                "clusy_cost": 100,
                "competitor_cost": 0.001,
            },
            "mean normalized cost exceeds",
        ),
    ],
)
def test_aggregate_rejects_terrible_clusy_latency_or_cost(
    run_overrides: JsonObject,
    reason_fragment: str,
) -> None:
    summaries = [
        _aggregate_run(mode, index, **run_overrides)
        for mode in ("cold_live", "warm_cache")
        for index in (1, 2)
    ]
    aggregate = aggregate_v3_summaries(summaries)
    assert aggregate["vendor_win_claimable"] is False
    reasons = aggregate["gate"]["by_provider"]["firecrawl"]["reasons"]
    assert any(reason_fragment in reason for reason in reasons)


@pytest.mark.parametrize(
    ("gate_name", "reason_fragment"),
    [
        ("paired_latency", "paired latency is not non-inferior"),
        ("p95_latency", "p95 latency exceeds"),
        ("p99_latency", "p99 latency exceeds"),
        ("paired_cost", "normalized cost is not non-inferior"),
        ("mean_cost", "mean normalized cost exceeds"),
    ],
)
def test_each_latency_and_cost_gate_fails_independently(
    gate_name: str,
    reason_fragment: str,
) -> None:
    summaries = [
        _aggregate_run(mode, index) for mode in ("cold_live", "warm_cache") for index in (1, 2)
    ]
    for summary in summaries:
        if gate_name in {"paired_latency", "paired_cost"}:
            metric = "latency_ms" if gate_name == "paired_latency" else "normalized_cost"
            row = next(item for item in summary["pairwise"] if item["metric"] == metric)
            row["bootstrap_ci_95"] = [0.1, 1.0]
        else:
            clusy_summary = next(
                item for item in summary["provider_summaries"] if item["provider"] == "clusy"
            )
            if gate_name == "p95_latency":
                clusy_summary["latency_ms"]["p95"] = 10_000
            elif gate_name == "p99_latency":
                clusy_summary["latency_ms"]["p99"] = 10_000
            else:
                clusy_summary["normalized_cost"]["mean"] = 100
    aggregate = aggregate_v3_summaries(summaries)
    reasons = aggregate["gate"]["by_provider"]["firecrawl"]["reasons"]
    assert any(reason_fragment in reason for reason in reasons)


def test_completed_run_chain_is_verified_but_hash_only_claim_stays_closed(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "exa"),
    )
    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=_success_handler(),
        credentials=_credentials(firecrawl_api_key=""),
    )
    verified_summary = verify_completed_run_directory(run_directory)
    assert verified_summary["artifact_integrity_claimable"] is True

    output_path = tmp_path / "aggregate.json"
    aggregate_completed_run_directories(
        run_directories=[run_directory],
        output_path=output_path,
    )
    aggregate = json.loads(output_path.read_text())
    assert aggregate["artifact_chains_verified"] is True
    assert aggregate["independent_window_timing_verified"] is False
    assert aggregate["vendor_win_claimable"] is False
    assert any(
        "lacks one artifact-derived cold/warm pair" in reason
        for reason in aggregate["gate"]["reasons"]
    )
    assert any(
        "no verifiable execution attestation" in reason
        for reason in aggregate["gate"]["by_provider"]["exa"]["reasons"]
    )
    assert any(
        "quality scope is not comparable" in reason
        for reason in aggregate["gate"]["by_provider"]["exa"]["reasons"]
    )


@pytest.mark.parametrize("mutation", ["line_swap", "timestamp_bundle_swap"])
def test_journal_sequence_and_nonoverlapping_bundles_survive_no_full_rehash_bypass(
    tmp_path: Path,
    mutation: str,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "firecrawl"),
    )
    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=_success_handler(),
        credentials=_credentials(exa_api_key=""),
    )
    events = _load_events(run_directory)
    if mutation == "line_swap":
        events[0], events[1] = events[1], events[0]
    else:
        bundle_keys = (
            "started_at",
            "first_byte_at",
            "completed_at",
            "latency_ms",
            "first_byte_latency_ms",
        )
        first_bundle = {key: events[0][key] for key in bundle_keys}
        second_bundle = {key: events[1][key] for key in bundle_keys}
        events[0].update(second_bundle)
        events[1].update(first_bundle)
    _assert_direct_and_rehashed_event_rejected(
        manifest_document=document,
        run_directory=run_directory,
        events=events,
        recompute_summary=True,
    )


def test_http_error_requires_a_real_non_2xx_status_after_full_rehash(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "firecrawl"),
    )
    success = _success_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.firecrawl.dev":
            return httpx.Response(503, content=b"provider unavailable")
        return success(request)

    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=handler,
        credentials=_credentials(exa_api_key=""),
    )
    events = _load_events(run_directory)
    fire_event = next(event for event in events if event["provider"] == "firecrawl")
    assert fire_event["status"] == "http_error"
    fire_event["http_status"] = None
    _assert_direct_and_rehashed_event_rejected(
        manifest_document=document,
        run_directory=run_directory,
        events=events,
        recompute_summary=True,
    )


@pytest.mark.parametrize(
    ("providers", "provider", "forged_cache_hit"),
    [
        (("clusy", "firecrawl"), "clusy", None),
        (("clusy", "exa"), "exa", False),
        (("clusy", "firecrawl"), "firecrawl", False),
    ],
)
def test_provider_specific_cache_hit_nullability_rejects_full_rehash(
    tmp_path: Path,
    providers: Sequence[str],
    provider: str,
    forged_cache_hit: bool | None,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=providers,
    )
    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=_success_handler(),
        credentials=_credentials(
            exa_api_key="" if "exa" not in providers else "dummy-exa-secret",
            firecrawl_api_key=("" if "firecrawl" not in providers else "dummy-fire-secret"),
        ),
    )
    events = _load_events(run_directory)
    target = next(event for event in events if event["provider"] == provider)
    target["cache_hit"] = forged_cache_hit
    _assert_direct_and_rehashed_event_rejected(
        manifest_document=document,
        run_directory=run_directory,
        events=events,
        recompute_summary=True,
    )


def test_nonclaimable_completed_run_still_requires_the_exact_full_matrix(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "firecrawl"),
    )
    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=_success_handler(),
        credentials=_credentials(exa_api_key=""),
        nonclaimable=True,
    )
    events = _load_events(run_directory)
    events.pop()
    with pytest.raises(BenchmarkError, match="event journal sequence"):
        verify_event_artifacts(
            manifest=parse_manifest(document),
            events=events,
            run_directory=run_directory,
        )
    _rewrite_rehashed_event_chain(
        run_directory,
        events,
        manifest_document=document,
        recompute_summary=True,
    )
    with pytest.raises(BenchmarkError, match="event journal sequence"):
        verify_completed_run_directory(run_directory)


@pytest.mark.parametrize("mutation", ["first_byte", "deadline"])
def test_body_first_byte_and_deadline_evidence_cannot_be_dropped_after_rehash(
    tmp_path: Path,
    mutation: str,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "firecrawl"),
    )
    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=_success_handler(),
        credentials=_credentials(exa_api_key=""),
        nonclaimable=True,
    )
    events = _load_events(run_directory)
    target = next(event for event in events if event["provider"] == "clusy")
    if mutation == "first_byte":
        assert target["raw_response_bytes"] > 0
        target["first_byte_at"] = None
        target["first_byte_latency_ms"] = None
    else:
        target["hard_deadline_enforced"] = False
    _assert_direct_and_rehashed_event_rejected(
        manifest_document=document,
        run_directory=run_directory,
        events=events,
        recompute_summary=True,
    )


@pytest.mark.parametrize("artifact_name", ["run", "summary", "completion"])
def test_completed_run_rejects_noncanonical_artifact_timestamps_after_rehash(
    tmp_path: Path,
    artifact_name: str,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "firecrawl"),
        mode="warm_cache",
    )
    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=_success_handler(mode="warm_cache"),
        credentials=_credentials(exa_api_key=""),
    )
    run_path = run_directory / "run.json"
    summary_path = run_directory / "summary.json"
    completion_path = run_directory / "completion.json"
    for path in (run_path, summary_path, completion_path):
        path.chmod(0o600)
    run = json.loads(run_path.read_text())
    summary = json.loads(summary_path.read_text())
    completion = json.loads(completion_path.read_text())
    if artifact_name == "run":
        run["created_at"] = run["created_at"].replace(".000000Z", "Z")
        # Real timestamps usually have non-zero microseconds.
        run["created_at"] = run["created_at"].split(".", maxsplit=1)[0] + "Z"
        run_bytes = canonical_json_bytes(run) + b"\n"
        run_path.write_bytes(run_bytes)
        completion["run_sha256"] = sha256_bytes(run_bytes)
    elif artifact_name == "summary":
        summary["warm_cache_primed_at"] = "2026-07-28T11:55:00Z"
        summary_bytes = canonical_json_bytes(summary) + b"\n"
        summary_path.write_bytes(summary_bytes)
        completion["summary_sha256"] = sha256_bytes(summary_bytes)
    else:
        completion["completed_at"] = completion["completed_at"].split(".", maxsplit=1)[0] + "Z"
    completion_path.write_bytes(canonical_json_bytes(completion) + b"\n")

    with pytest.raises(BenchmarkError):
        verify_completed_run_directory(run_directory)


def test_event_timestamp_parser_rejects_full_rehashed_alternate_spelling(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "firecrawl"),
    )
    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=_success_handler(),
        credentials=_credentials(exa_api_key=""),
    )
    events = _load_events(run_directory)
    events[0]["started_at"] = events[0]["started_at"].split(".", maxsplit=1)[0] + "Z"
    _assert_direct_and_rehashed_event_rejected(
        manifest_document=document,
        run_directory=run_directory,
        events=events,
    )


@pytest.mark.parametrize(
    ("providers", "provider", "field_name", "value"),
    [
        (("clusy", "firecrawl"), "firecrawl", "credits", 99.0),
        (("clusy", "exa"), "exa", "provider_reported_cost", 99.0),
        (("clusy", "firecrawl"), "clusy", "normalized_cost", 99.0),
    ],
)
def test_observed_provider_costs_cannot_exceed_caps_after_full_rehash(
    tmp_path: Path,
    providers: Sequence[str],
    provider: str,
    field_name: str,
    value: float,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=providers,
    )
    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=_success_handler(),
        credentials=_credentials(
            exa_api_key="" if "exa" not in providers else "dummy-exa-secret",
            firecrawl_api_key=("" if "firecrawl" not in providers else "dummy-fire-secret"),
        ),
    )
    events = _load_events(run_directory)
    target = next(event for event in events if event["provider"] == provider)
    target[field_name] = value
    _assert_direct_and_rehashed_event_rejected(
        manifest_document=document,
        run_directory=run_directory,
        events=events,
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "latency_timestamp_mismatch",
        "cache_hit_state_mismatch",
        "negative_zero",
        "integer_float_alias",
        "preflight_deadline",
        "stage_total",
        "candidate_counter_bound",
        "structure_counter_bound",
    ],
)
def test_numeric_timing_and_cache_bypasses_fail_direct_and_after_full_rehash(
    tmp_path: Path,
    mutation: str,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "firecrawl"),
    )
    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=_success_handler(),
        credentials=_credentials(exa_api_key=""),
    )
    events = _load_events(run_directory)
    target = next(event for event in events if event["provider"] == "clusy")
    if mutation == "latency_timestamp_mismatch":
        target["latency_ms"] += 1_000.0
    elif mutation == "cache_hit_state_mismatch":
        assert target["cache_state"] == "miss"
        target["cache_hit"] = True
    elif mutation == "negative_zero":
        target["latency_ms"] = -0.0
    elif mutation == "integer_float_alias":
        target["normalized_cost"] = 0
    elif mutation == "preflight_deadline":
        target["clusy_preflight"]["latency_ms"] = 10_001.0
    elif mutation == "candidate_counter_bound":
        target["scoring"]["candidate_tokens"] = 10**1_000
    elif mutation == "structure_counter_bound":
        target["scoring"]["observed_headings"] = 10**1_000
    else:
        target["stage_timings_ms"]["total"] = target["latency_ms"] + 100.0
    _assert_direct_and_rehashed_event_rejected(
        manifest_document=document,
        run_directory=run_directory,
        events=events,
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "empty_raw_body",
        "missing_http_status",
        "empty_raw_hash_with_body",
        "empty_text_hash_with_text",
        "zero_tokens_perfect_score",
        "zero_structure_perfect_score",
    ],
)
def test_success_body_hash_and_scoring_bypasses_fail_after_full_rehash(
    tmp_path: Path,
    mutation: str,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "firecrawl"),
    )
    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=_success_handler(),
        credentials=_credentials(exa_api_key=""),
    )
    events = _load_events(run_directory)
    target = next(event for event in events if event["provider"] == "clusy")
    empty_sha = sha256_bytes(b"")
    if mutation == "empty_raw_body":
        target["raw_response_bytes"] = 0
        target["raw_response_sha256"] = empty_sha
    elif mutation == "missing_http_status":
        target["http_status"] = None
    elif mutation == "empty_raw_hash_with_body":
        target["raw_response_sha256"] = empty_sha
    elif mutation == "empty_text_hash_with_text":
        target["normalized_text_sha256"] = empty_sha
    elif mutation == "zero_tokens_perfect_score":
        target["token_count"] = 0
        target["scoring"]["candidate_tokens"] = 0
        target["scoring"]["token_precision"] = 1.0
        target["scoring"]["token_recall"] = 1.0
        target["scoring"]["token_f1"] = 1.0
    else:
        target["scoring"]["observed_headings"] = 0
        target["scoring"]["observed_list_items"] = 0
        target["scoring"]["observed_code_blocks"] = 0
        target["scoring"]["observed_tables"] = 0
    _assert_direct_and_rehashed_event_rejected(
        manifest_document=document,
        run_directory=run_directory,
        events=events,
    )


@pytest.mark.parametrize(
    "metric_family",
    [
        "token_precision_recall_f1",
        "heading_f1",
        "list_f1",
        "code_f1",
        "table_tree_similarity",
    ],
)
def test_fractional_overlap_scores_fail_direct_and_after_full_rehash(
    tmp_path: Path,
    metric_family: str,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "firecrawl"),
    )
    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=_success_handler(),
        credentials=_credentials(exa_api_key=""),
    )
    events = _load_events(run_directory)
    scoring = next(event["scoring"] for event in events if event["provider"] == "clusy")
    if metric_family == "token_precision_recall_f1":
        candidate_count = scoring["candidate_tokens"]
        reference_count = scoring["reference_tokens"]
        scoring["token_precision"] = 0.5 / candidate_count
        scoring["token_recall"] = 0.5 / reference_count
        scoring["token_f1"] = 1.0 / (candidate_count + reference_count)
    elif metric_family in {"heading_f1", "list_f1", "code_f1"}:
        count_fields = {
            "heading_f1": ("observed_headings", "headings"),
            "list_f1": ("observed_list_items", "list_items"),
            "code_f1": ("observed_code_blocks", "code_blocks"),
        }
        observed_key, reference_key = count_fields[metric_family]
        denominator = scoring[observed_key] + len(
            document["tasks"][0]["reference"]["structure"][reference_key]
        )
        scoring[metric_family] = 1.0 / denominator
        component_scores = [
            scoring[key]
            for key in (
                "heading_f1",
                "list_f1",
                "code_f1",
                "table_tree_similarity",
            )
            if scoring[key] is not None
        ]
        scoring["structure_score"] = sum(component_scores) / len(component_scores)
    else:
        denominator = scoring["observed_table_tree_tokens"] + scoring["reference_table_tree_tokens"]
        scoring["table_tree_similarity"] = 1.0 / denominator
        component_scores = [
            scoring[key]
            for key in (
                "heading_f1",
                "list_f1",
                "code_f1",
                "table_tree_similarity",
            )
            if scoring[key] is not None
        ]
        scoring["structure_score"] = sum(component_scores) / len(component_scores)
    _assert_direct_and_rehashed_event_rejected(
        manifest_document=document,
        run_directory=run_directory,
        events=events,
    )


def test_empty_outputs_are_generated_with_canonical_failed_evidence(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "firecrawl"),
    )
    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=_success_handler(text_override=""),
        credentials=_credentials(exa_api_key=""),
    )
    verify_completed_run_directory(run_directory)
    for event in _load_events(run_directory):
        assert event["status"] == "empty_output"
        assert event["cache_state"] == "unknown"
        assert event["cache_hit"] is None
        assert event["canonical_url"] == ""
        assert event["character_count"] == 0
        assert event["token_count"] == 0
        assert event["provider_score"] is None
        assert event["stage_timings_ms"] == {}
        assert event["scoring"]["token_f1"] == 0.0
        assert event["scoring"]["observed_tables"] == 0


def test_max_output_generator_timestamps_verify_at_the_same_measured_boundary(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "firecrawl"),
    )
    oversized = "# Heading\n\n" + ("payload " * 2_000)
    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=_success_handler(text_override=oversized),
        credentials=_credentials(exa_api_key=""),
    )
    verify_completed_run_directory(run_directory)
    for event in _load_events(run_directory):
        assert event["character_count"] == 10_000
        assert event["benchmark_output_cap_applied"] is True
        started = datetime.strptime(
            event["started_at"],
            "%Y-%m-%dT%H:%M:%S.%fZ",
        ).replace(tzinfo=UTC)
        completed = datetime.strptime(
            event["completed_at"],
            "%Y-%m-%dT%H:%M:%S.%fZ",
        ).replace(tzinfo=UTC)
        wall_ms = (completed - started).total_seconds() * 1_000
        assert abs(wall_ms - event["latency_ms"]) <= 5


def test_clusy_endpoint_hash_must_be_shared_across_the_run_after_rehash(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=2,
        providers=("clusy", "firecrawl"),
    )
    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=_success_handler(),
        credentials=_credentials(exa_api_key=""),
    )
    events = _load_events(run_directory)
    clusy_events = [event for event in events if event["provider"] == "clusy"]
    clusy_events[1]["endpoint_sha256"] = "9" * 64
    _assert_direct_and_rehashed_event_rejected(
        manifest_document=document,
        run_directory=run_directory,
        events=events,
    )


def test_unknown_provenance_requires_all_observed_nonclaim_reasons_after_rehash(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "firecrawl"),
    )
    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=_success_handler(),
        credentials=_credentials(exa_api_key=""),
        nonclaimable=True,
    )
    run_path = run_directory / "run.json"
    completion_path = run_directory / "completion.json"
    run_path.chmod(0o600)
    completion_path.chmod(0o600)
    run = json.loads(run_path.read_text())
    assert run["runner_commit"] == "unknown"
    assert run["container_digest"] == "unknown"
    run["nonclaimable_reasons"] = ["operator explicitly selected nonclaimable mode"]
    run_bytes = canonical_json_bytes(run) + b"\n"
    run_path.write_bytes(run_bytes)
    completion = json.loads(completion_path.read_text())
    completion["run_sha256"] = sha256_bytes(run_bytes)
    completion_path.write_bytes(canonical_json_bytes(completion) + b"\n")
    with pytest.raises(BenchmarkError, match="runner reason"):
        verify_completed_run_directory(run_directory)


def test_completed_run_rejects_extra_raw_files_directories_and_symlinks(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "exa"),
    )
    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=_success_handler(),
        credentials=_credentials(firecrawl_api_key=""),
    )

    extra_file = run_directory / "raw-provider-output.txt"
    extra_file.write_text("must never coexist with verified evidence")
    with pytest.raises(BenchmarkError, match="exactly the five"):
        verify_completed_run_directory(run_directory)
    extra_file.unlink()

    extra_directory = run_directory / "raw"
    extra_directory.mkdir()
    with pytest.raises(BenchmarkError, match="exactly the five"):
        verify_completed_run_directory(run_directory)
    extra_directory.rmdir()

    extra_link = run_directory / "summary-link"
    extra_link.symlink_to(run_directory / "summary.json")
    with pytest.raises(BenchmarkError, match="exactly the five"):
        verify_completed_run_directory(run_directory)
    extra_link.unlink()


@pytest.mark.parametrize("field_name", _EVENT_CARRIER_FIELDS)
def test_every_retained_event_string_or_list_rejects_raw_carrier_after_full_rehash(
    tmp_path: Path,
    field_name: str,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "firecrawl"),
    )
    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=_success_handler(),
        credentials=_credentials(exa_api_key=""),
    )
    manifest = parse_manifest(document)
    events = _load_events(run_directory)
    carrier: Any = (
        ["RAW_PROVIDER_TEXT_CARRIER"]
        if field_name in _EVENT_STRING_LIST_FIELDS
        else "RAW_PROVIDER_TEXT_CARRIER"
    )
    events[0][field_name] = carrier

    with pytest.raises(BenchmarkError):
        verify_event_artifacts(
            manifest=manifest,
            events=events,
            run_directory=run_directory,
        )

    events_path = run_directory / "events.jsonl"
    summary_path = run_directory / "summary.json"
    completion_path = run_directory / "completion.json"
    for path in (events_path, summary_path, completion_path):
        path.chmod(0o600)
    events_bytes = b"".join(canonical_json_bytes(event) + b"\n" for event in events)
    events_path.write_bytes(events_bytes)
    summary = json.loads(summary_path.read_text())
    summary["events_sha256"] = sha256_bytes(events_bytes)
    summary_bytes = canonical_json_bytes(summary) + b"\n"
    summary_path.write_bytes(summary_bytes)
    completion = json.loads(completion_path.read_text())
    completion["events_sha256"] = sha256_bytes(events_bytes)
    completion["summary_sha256"] = sha256_bytes(summary_bytes)
    completion_path.write_bytes(canonical_json_bytes(completion) + b"\n")

    with pytest.raises(BenchmarkError):
        verify_completed_run_directory(run_directory)


@pytest.mark.parametrize(
    ("field_name", "carrier"),
    [
        ("claimable", False),
        ("watermark", "RAW_CLAIM_CARRIER"),
        ("nonclaimable_reasons", ["RAW_REASON_CARRIER"]),
        (
            "nonclaimable_reasons",
            [
                "working tree is dirty",
                "operator explicitly selected nonclaimable mode",
            ],
        ),
        ("runner_commit", "RAW_COMMIT_CARRIER"),
        ("container_digest", "RAW_CONTAINER_CARRIER"),
        ("run_id", "RAW_RUN_ID_CARRIER"),
    ],
)
def test_rehashed_run_claim_or_identity_carriers_are_rejected(
    tmp_path: Path,
    field_name: str,
    carrier: Any,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "firecrawl"),
    )
    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=_success_handler(),
        credentials=_credentials(exa_api_key=""),
    )
    run_path = run_directory / "run.json"
    completion_path = run_directory / "completion.json"
    run_path.chmod(0o600)
    completion_path.chmod(0o600)
    run_artifact = json.loads(run_path.read_text())
    run_artifact[field_name] = carrier
    run_bytes = canonical_json_bytes(run_artifact) + b"\n"
    run_path.write_bytes(run_bytes)
    completion = json.loads(completion_path.read_text())
    completion["run_sha256"] = sha256_bytes(run_bytes)
    completion_path.write_bytes(canonical_json_bytes(completion) + b"\n")

    with pytest.raises(BenchmarkError):
        verify_completed_run_directory(run_directory)


def test_completed_run_directory_name_is_bound_to_run_id(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "firecrawl"),
    )
    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=_success_handler(),
        credentials=_credentials(exa_api_key=""),
    )
    renamed = run_directory.parent / "carrier-renamed-run"
    run_directory.rename(renamed)
    with pytest.raises(BenchmarkError, match="directory name does not match run_id"):
        verify_completed_run_directory(renamed)


@pytest.mark.parametrize(
    ("artifact_name", "error_match"),
    [
        ("manifest", "manifest has unknown fields"),
        ("run", "run metadata fields do not match"),
        ("run_nested", "run metadata clusy_preflight fields do not match"),
        ("event", "event 0 fields do not match"),
        ("event_nested", r"event 0\.clusy_preflight fields do not match"),
        ("summary", "stored summary fields do not match"),
        ("summary_nested", "stored summary vendor_win_gate fields do not match"),
        ("completion", "completion record fields do not match"),
    ],
)
def test_completed_run_rejects_rehashed_unknown_artifact_fields(
    tmp_path: Path,
    artifact_name: str,
    error_match: str,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "firecrawl"),
    )
    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=_success_handler(),
        credentials=_credentials(exa_api_key=""),
    )
    paths = {
        name: run_directory / filename
        for name, filename in {
            "manifest": "manifest.json",
            "run": "run.json",
            "events": "events.jsonl",
            "summary": "summary.json",
            "completion": "completion.json",
        }.items()
    }
    for path in paths.values():
        path.chmod(0o600)
    manifest_artifact = json.loads(paths["manifest"].read_text())
    run_artifact = json.loads(paths["run"].read_text())
    events_artifact = _load_events(run_directory)
    summary_artifact = json.loads(paths["summary"].read_text())
    completion_artifact = json.loads(paths["completion"].read_text())

    if artifact_name == "manifest":
        manifest_artifact["raw_provider_output"] = {"forbidden": True}
        manifest_artifact["manifest_sha256"] = calculate_manifest_sha256(manifest_artifact)
        new_manifest_sha = manifest_artifact["manifest_sha256"]
        run_artifact["manifest_sha256"] = new_manifest_sha
        for event in events_artifact:
            event["manifest_sha256"] = new_manifest_sha
        summary_artifact["manifest_sha256"] = new_manifest_sha
        completion_artifact["manifest_sha256"] = new_manifest_sha
    elif artifact_name == "run":
        run_artifact["raw_provider_output"] = {"forbidden": True}
    elif artifact_name == "run_nested":
        run_artifact["clusy_preflight"]["raw_provider_output"] = {"forbidden": True}
    elif artifact_name == "event":
        events_artifact[0]["raw_provider_output"] = {"forbidden": True}
    elif artifact_name == "event_nested":
        events_artifact[0]["clusy_preflight"]["raw_provider_output"] = {"forbidden": True}
    elif artifact_name == "summary":
        summary_artifact["raw_provider_output"] = {"forbidden": True}
    elif artifact_name == "summary_nested":
        summary_artifact["vendor_win_gate"]["raw_provider_output"] = {"forbidden": True}
    else:
        completion_artifact["raw_provider_output"] = {"forbidden": True}

    manifest_bytes = canonical_json_bytes(manifest_artifact) + b"\n"
    run_bytes = canonical_json_bytes(run_artifact) + b"\n"
    events_bytes = b"".join(canonical_json_bytes(event) + b"\n" for event in events_artifact)
    summary_artifact["events_sha256"] = sha256_bytes(events_bytes)
    summary_bytes = canonical_json_bytes(summary_artifact) + b"\n"
    completion_artifact.update(
        {
            "manifest_artifact_sha256": sha256_bytes(manifest_bytes),
            "run_sha256": sha256_bytes(run_bytes),
            "events_sha256": sha256_bytes(events_bytes),
            "summary_sha256": sha256_bytes(summary_bytes),
        }
    )
    paths["manifest"].write_bytes(manifest_bytes)
    paths["run"].write_bytes(run_bytes)
    paths["events"].write_bytes(events_bytes)
    paths["summary"].write_bytes(summary_bytes)
    paths["completion"].write_bytes(canonical_json_bytes(completion_artifact) + b"\n")

    with pytest.raises(BenchmarkError, match=error_match):
        verify_completed_run_directory(run_directory)


def test_completed_run_recompute_rejects_rehashed_summary_tamper(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "exa"),
    )
    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=_success_handler(),
        credentials=_credentials(firecrawl_api_key=""),
    )
    summary_path = run_directory / "summary.json"
    completion_path = run_directory / "completion.json"
    summary_path.chmod(0o600)
    completion_path.chmod(0o600)
    summary = json.loads(summary_path.read_text())
    token_row = next(row for row in summary["pairwise"] if row["metric"] == "token_f1")
    token_row["mean_delta_left_minus_right"] = 999
    summary_bytes = canonical_json_bytes(summary) + b"\n"
    summary_path.write_bytes(summary_bytes)
    completion = json.loads(completion_path.read_text())
    completion["summary_sha256"] = sha256_bytes(summary_bytes)
    completion_path.write_bytes(canonical_json_bytes(completion) + b"\n")

    with pytest.raises(BenchmarkError, match="does not exactly match recomputation"):
        verify_completed_run_directory(run_directory)


@pytest.mark.parametrize(
    ("artifact_name", "field_path"),
    [
        ("summary", ("interpretation",)),
        ("summary", ("vendor_win_gate", "reason")),
        ("completion", ("run_id",)),
        ("completion", ("watermark",)),
        ("completion", ("completed_at",)),
    ],
)
def test_summary_and_completion_cannot_become_rehashed_text_carriers(
    tmp_path: Path,
    artifact_name: str,
    field_path: tuple[str, ...],
) -> None:
    manifest_path = tmp_path / "manifest.json"
    document = _write_manifest(
        manifest_path,
        task_count=1,
        providers=("clusy", "firecrawl"),
    )
    run_directory = _run(
        tmp_path=tmp_path,
        manifest_path=manifest_path,
        document=document,
        handler=_success_handler(),
        credentials=_credentials(exa_api_key=""),
    )
    summary_path = run_directory / "summary.json"
    completion_path = run_directory / "completion.json"
    summary_path.chmod(0o600)
    completion_path.chmod(0o600)
    summary = json.loads(summary_path.read_text())
    completion = json.loads(completion_path.read_text())
    target = summary if artifact_name == "summary" else completion
    if len(field_path) == 1:
        target[field_path[0]] = "RAW_TEXT_CARRIER"
    else:
        target[field_path[0]][field_path[1]] = "RAW_TEXT_CARRIER"
    if artifact_name == "summary":
        summary_bytes = canonical_json_bytes(summary) + b"\n"
        summary_path.write_bytes(summary_bytes)
        completion["summary_sha256"] = sha256_bytes(summary_bytes)
    completion_path.write_bytes(canonical_json_bytes(completion) + b"\n")

    with pytest.raises(BenchmarkError):
        verify_completed_run_directory(run_directory)


def test_error_redaction_removes_credentials_and_url_details() -> None:
    redacted = redact_error(
        "request https://user:pass@example.com/private?q=1 failed with api-secret",
        ("api-secret",),
    )
    assert redacted is not None
    assert "api-secret" not in redacted
    assert "/private" not in redacted
    assert "?q=1" not in redacted
    assert "[REDACTED_SECRET]" in redacted


def test_manifest_loader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text(
        '{"schema_version":"a","schema_version":"b"}',
        encoding="utf-8",
    )
    with pytest.raises(BenchmarkError, match="duplicate JSON key"):
        load_manifest(path)

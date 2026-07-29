from __future__ import annotations

import asyncio
import threading

import pytest

from app.config import settings
from app.services import extractor as extractor_module
from app.services import quality_extractor as quality_module
from app.services.extractor import (
    ExtractionResult,
    _count_words,
    _extract_raw_text,
    _extract_with_markdownify,
    _extract_with_readability,
    _extract_with_trafilatura,
    _log_host,
    extract_content,
    extract_content_async,
)


@pytest.mark.anyio
async def test_cancelled_thread_waiter_returns_but_worker_retains_permit():
    from app.services.extractor import _to_thread_holding_cancellation

    started = threading.Event()
    release = threading.Event()

    def worker():
        started.set()
        release.wait(timeout=1)
        return "done"

    semaphore = asyncio.Semaphore(1)
    task = asyncio.create_task(
        _to_thread_holding_cancellation(worker, semaphore=semaphore)
    )
    assert await asyncio.to_thread(started.wait, 0.5)
    assert semaphore.locked()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.1)
    assert semaphore.locked()
    release.set()
    await asyncio.sleep(0.02)
    assert not semaphore.locked()


@pytest.mark.anyio
async def test_cancelled_queued_thread_waiter_does_not_start_work():
    from app.services.extractor import _to_thread_holding_cancellation

    calls = 0

    def worker():
        nonlocal calls
        calls += 1

    semaphore = asyncio.Semaphore(0)
    task = asyncio.create_task(
        _to_thread_holding_cancellation(worker, semaphore=semaphore)
    )
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == 0


@pytest.mark.asyncio
async def test_cancelled_disabled_adaptive_retains_permit_until_native_worker_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    queued_started = threading.Event()
    call_lock = threading.Lock()
    profiles: list[str] = []
    semaphore = asyncio.Semaphore(1)

    def native(_: str, __: str, profile: str) -> ExtractionResult:
        with call_lock:
            call_index = len(profiles)
            profiles.append(profile)
        if call_index == 0:
            started.set()
            release.wait(timeout=1)
        else:
            queued_started.set()
        return _native_candidate("native", 500)

    monkeypatch.setattr(settings, "quality_extraction_base_url", "")
    monkeypatch.setattr(settings, "quality_extraction_api_key", "")
    monkeypatch.setattr(settings, "quality_extraction_model", "")
    monkeypatch.setattr(extractor_module, "_extract_with_native", native)
    monkeypatch.setattr(
        extractor_module,
        "_get_extraction_semaphore",
        lambda: semaphore,
    )

    first = asyncio.create_task(
        extract_content_async(
            "<html><body><article>first</article></body></html>",
            "https://example.test/first",
            extraction_profile="adaptive",
        )
    )
    second: asyncio.Task[ExtractionResult] | None = None
    try:
        assert await asyncio.to_thread(started.wait, 0.5)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, timeout=0.1)
        assert semaphore.locked()

        second = asyncio.create_task(
            extract_content_async(
                "<html><body><article>second</article></body></html>",
                "https://example.test/second",
                extraction_profile="adaptive",
            )
        )
        await asyncio.sleep(0.02)
        assert not queued_started.is_set()
        assert profiles == ["balanced"]

        release.set()
        result = await asyncio.wait_for(second, timeout=0.5)
        assert queued_started.is_set()
        assert profiles == ["balanced", "balanced"]
        assert result.route_reasons == ("adaptive_quality_backend_disabled_fast_path",)
        assert not semaphore.locked()
    finally:
        release.set()
        if second is not None and not second.done():
            await asyncio.gather(second, return_exceptions=True)


SAMPLE_HTML = """<!DOCTYPE html>
<html>
<head><title>Test Article</title>
<meta name="description" content="A test description">
</head>
<body>
<article>
<h1>Hello World</h1>
<p>This is a test article with some meaningful content. It has multiple
sentences to ensure we have enough words for the extraction threshold
to pass correctly in all the test scenarios.</p>
<p>Second paragraph with additional content that makes the text longer
and more realistic for a real-world article extraction scenario.</p>
<p>Third paragraph with even more content to push the word count well
above the minimum threshold required by the extractor for reliable
extraction.</p>
</article>
</body>
</html>"""

EMPTY_HTML = "<html><body><div></div></body></html>"


def _native_candidate(
    prefix: str,
    words: int,
    *,
    confidence: float = 0.99,
) -> ExtractionResult:
    text = " ".join(f"{prefix}{index}" for index in range(words))
    return ExtractionResult(
        text=text,
        word_count=words,
        strategy="rs-trafilatura",
        confidence=confidence,
        page_type="article",
    )


class TestWordCount:
    def test_empty(self):
        assert _count_words("") == 0

    def test_whitespace_only(self):
        assert _count_words("   \n  \t  ") == 0

    def test_simple(self):
        assert _count_words("hello world") == 2

    def test_punctuation(self):
        assert _count_words("hello, world! how are you?") == 5


def test_log_host_never_exposes_credentials_or_query() -> None:
    assert (
        _log_host("https://user:secret@example.com/paper?token=private#fragment")
        == "example.com"
    )
    assert _log_host("https://[broken") == "invalid"


class TestTrafilatura:
    def test_extracts_content(self):
        result = _extract_with_trafilatura(SAMPLE_HTML, "https://example.com", "article")
        assert result is not None
        assert "Hello World" in result.text
        assert result.strategy == "trafilatura"
        assert result.word_count > 10

    def test_returns_none_for_empty(self):
        result = _extract_with_trafilatura(EMPTY_HTML, "https://example.com", "article")
        assert result is None


class TestReadability:
    def test_extracts_main_content(self):
        result = _extract_with_readability(SAMPLE_HTML, "https://example.com", "article")
        assert result is not None
        assert result.strategy == "readability"
        assert result.word_count > 10

    def test_returns_none_for_empty(self):
        result = _extract_with_readability(EMPTY_HTML, "https://example.com", "article")
        assert result is None


class TestMarkdownify:
    def test_converts_html(self):
        content = "<html><body><p>" + "hello world test content here. " * 50 + "</p></body></html>"
        result = _extract_with_markdownify(content, "", "article")
        assert result is not None
        assert "hello world" in result.text
        assert result.strategy == "markdownify"


class TestRawText:
    def test_strips_tags(self):
        result = _extract_raw_text("<html><body><p>Hello world</p></body></html>")
        assert "Hello world" in result.text
        assert result.strategy == "raw_lxml"

    def test_handles_broken_html(self):
        result = _extract_raw_text("not html <b>at all")
        assert len(result.text) > 0


class TestExtractContent:
    def test_falls_back_through_chain(self):
        result = extract_content(SAMPLE_HTML, "https://example.com")
        assert result.strategy in (
            "rs-trafilatura",
            "trafilatura",
            "readability",
            "markdownify",
            "raw_lxml",
        )
        assert result.word_count > 0

    def test_native_article_body_is_scored_output_without_synthetic_title(self):
        result = extract_content(
            SAMPLE_HTML,
            "https://example.com/news/test-article",
            extraction_profile="article_body",
        )
        assert result.strategy == "rs-trafilatura"
        assert result.page_type == "article"
        assert result.text.startswith("This is a test article")
        assert not result.text.startswith("#")

    def test_balanced_profile_preserves_main_content_heading(self):
        result = extract_content(SAMPLE_HTML, "https://example.com/news/test-article")
        assert result.strategy == "rs-trafilatura"
        assert result.text.startswith("Hello World")
        assert "This is a test article" in result.text

    def test_handles_garbage(self):
        result = extract_content("   ", "")
        assert result.strategy == "raw_lxml"

    def test_sparse_balanced_candidate_skips_experimental_article_rescue(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        broad = _native_candidate("broad", 20)
        article = _native_candidate("article", 40)
        calls: list[str] = []

        def native(
            _html: str,
            _url: str,
            profile: str,
        ) -> ExtractionResult:
            calls.append(profile)
            return article if profile == "article_body" else broad

        monkeypatch.setattr(extractor_module, "_extract_with_native", native)
        html = f"<html><body><!--{'x' * 5000}--><article>source</article></body></html>"

        result = extract_content(
            html,
            "https://example.test/rescue",
            extraction_profile="balanced",
        )

        assert calls == ["balanced"]
        assert result.text == broad.text
        assert result.route == "native_fast_path"
        assert result.route_reasons == ()
        assert result.candidate_count == 1

    def test_dense_balanced_candidate_skips_article_second_pass(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        broad = _native_candidate("broad", 500)
        calls: list[str] = []

        def native(
            _html: str,
            _url: str,
            profile: str,
        ) -> ExtractionResult:
            calls.append(profile)
            return broad

        monkeypatch.setattr(extractor_module, "_extract_with_native", native)

        result = extract_content(
            f"<article>{'source ' * 20}</article>",
            "https://example.test/dense",
            extraction_profile="balanced",
        )

        assert calls == ["balanced"]
        assert result.text == broad.text
        assert result.route == "native_fast_path"

    def test_article_body_profile_never_enters_balanced_rescue(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        article = _native_candidate("scored", 30)
        calls: list[str] = []

        def native(
            _html: str,
            _url: str,
            profile: str,
        ) -> ExtractionResult:
            calls.append(profile)
            return article

        monkeypatch.setattr(extractor_module, "_extract_with_native", native)
        html = f"<html><body><!--{'x' * 5000}--><article>source</article></body></html>"

        result = extract_content(
            html,
            "https://example.test/article-body",
            extraction_profile="article_body",
        )

        assert calls == ["article_body"]
        assert result.text == article.text
        assert result.route == "native_fast_path"


@pytest.mark.anyio
async def test_async_balanced_profile_skips_experimental_article_rescue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broad = _native_candidate("broad", 20)
    article = _native_candidate("article", 40)
    calls: list[str] = []

    def native(
        _html: str,
        _url: str,
        profile: str,
    ) -> ExtractionResult:
        calls.append(profile)
        return article if profile == "article_body" else broad

    monkeypatch.setattr(extractor_module, "_extract_with_native", native)

    result = await extract_content_async(
        "<html><body><article>source</article></body></html>",
        "https://example.test/balanced",
        extraction_profile="balanced",
    )

    assert calls == ["balanced"]
    assert result.text == broad.text
    assert result.route == "native_fast_path"


@pytest.mark.anyio
async def test_adaptive_profile_preserves_selected_native_article_rescue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broad = _native_candidate("broad", 20)
    article = _native_candidate("article", 40)
    calls: list[str] = []

    def native(
        _html: str,
        _url: str,
        profile: str,
    ) -> ExtractionResult:
        calls.append(profile)
        return article if profile == "article_body" else broad

    async def unavailable_quality(*_args: object) -> None:
        raise AssertionError("disabled adaptive path must not call quality")

    def forbidden_upgrade_work(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("disabled adaptive path must skip upgrade-only work")

    monkeypatch.setattr(extractor_module, "_extract_with_native", native)
    monkeypatch.setattr(
        settings,
        "quality_extraction_base_url",
        "",
    )
    monkeypatch.setattr(
        settings,
        "quality_extraction_api_key",
        "",
    )
    monkeypatch.setattr(
        settings,
        "quality_extraction_model",
        "",
    )
    monkeypatch.setattr(
        extractor_module,
        "_adaptive_risk_decision",
        forbidden_upgrade_work,
    )
    monkeypatch.setattr(
        extractor_module,
        "_candidate_disagreement",
        forbidden_upgrade_work,
    )
    monkeypatch.setattr(
        extractor_module,
        "_structural_loss_score",
        forbidden_upgrade_work,
    )
    monkeypatch.setattr(
        extractor_module,
        "_bounded_grounding_coverage",
        forbidden_upgrade_work,
    )
    monkeypatch.setattr(
        extractor_module,
        "_python_cascade",
        forbidden_upgrade_work,
    )
    monkeypatch.setattr(
        extractor_module,
        "_parallel_extract",
        forbidden_upgrade_work,
    )
    monkeypatch.setattr(
        quality_module,
        "extract_quality_content",
        unavailable_quality,
    )
    html = f"<html><body><!--{'x' * 5000}--><article>source</article></body></html>"

    result = await extract_content_async(
        html,
        "https://example.test/adaptive-rescue",
        extraction_profile="adaptive",
    )

    assert calls == ["balanced", "article_body"]
    assert result.text == article.text
    assert result.route == "native_article_rescue"
    assert "experimental_adaptive_article_rescue" in result.route_reasons
    assert result.quality_attempted is False
    assert result.route_reasons[-1] == "adaptive_quality_backend_disabled_fast_path"
    assert result.candidate_count == 1
    assert result.candidate_disagreement == 0.0
    assert result.completeness_score == 0.0
    assert result.completeness_coverage == "output_only"


def test_native_fast_path_reports_unassessed_source_completeness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _native_candidate("native", 30)
    monkeypatch.setattr(
        extractor_module,
        "_extract_with_native",
        lambda *_args: candidate,
    )

    result = extract_content(
        "<html><body><h1>Source heading</h1><p>Source prose.</p></body></html>",
        "https://example.test/native",
        extraction_profile="balanced",
    )

    assert result.completeness_score == 0.0
    assert isinstance(result.completeness_score, float)
    assert result.completeness_coverage == "output_only"


def test_github_specialist_reports_explicit_source_route() -> None:
    text = "Repository README content with enough words for a stable result."
    result = extractor_module._finalize_result(
        ExtractionResult(
            text=text,
            word_count=10,
            strategy="github-repository",
        ),
        f"<html><body><main><p>{text}</p></main></body></html>",
        False,
    )

    assert result.route == "github_source"
    assert result.route_reasons == ("github_source_specialist",)


def test_structure_assessment_reports_bounded_source_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = ExtractionResult(
        text="Plain output with enough words to avoid the sparse output deduction.",
        word_count=11,
        strategy="trafilatura",
    )
    monkeypatch.setattr(
        extractor_module.settings,
        "adaptive_extraction_max_scan_chars",
        4096,
    )

    extractor_module._annotate_completeness(
        candidate,
        "<html><body><h1>Missing heading</h1></body></html>" + (" " * 5000),
    )

    assert candidate.completeness_score is not None
    assert candidate.completeness_score < 1
    assert candidate.completeness_coverage == "source_prefix"
    assert "headings_missing" in candidate.completeness_reasons


def test_unrelated_plain_output_cannot_report_perfect_completeness() -> None:
    source = " ".join(f"source{index}" for index in range(80))
    unrelated = " ".join(f"unrelated{index}" for index in range(40))
    candidate = ExtractionResult(
        text=unrelated,
        word_count=40,
        strategy="trafilatura",
    )

    extractor_module._annotate_completeness(
        candidate,
        f"<html><body><p>{source}</p></body></html>",
    )

    assert candidate.completeness_coverage == "source_full"
    assert candidate.completeness_score == 0
    assert candidate.source_coverage_score == 0
    assert candidate.output_grounding_score == 0
    assert "low_source_coverage" in candidate.completeness_reasons
    assert "low_output_grounding" in candidate.completeness_reasons


def test_exact_plain_source_output_can_report_full_grounded_coverage() -> None:
    source = " ".join(f"grounded{index}" for index in range(40))
    candidate = ExtractionResult(
        text=source,
        word_count=40,
        strategy="trafilatura",
    )

    extractor_module._annotate_completeness(
        candidate,
        f"<html><body><p>{source}</p></body></html>",
    )

    assert candidate.completeness_coverage == "source_full"
    assert candidate.completeness_score == 1
    assert candidate.source_coverage_score == 1
    assert candidate.output_grounding_score == 1


def test_grounding_assessment_enforces_character_and_token_budgets() -> None:
    source = "文" * 90_000
    html = f"<html><body>{source}</body></html>"

    coverage = extractor_module._bounded_grounding_coverage(
        html,
        source,
    )

    assert coverage is not None
    assert coverage.source_tokens_assessed <= (
        extractor_module._COMPLETENESS_MAX_TOKENS
    )
    assert coverage.output_tokens_assessed <= (
        extractor_module._COMPLETENESS_MAX_TOKENS
    )
    assert coverage.fully_assessed is False

    candidate = ExtractionResult(
        text=source,
        word_count=45_000,
        strategy="trafilatura",
    )
    extractor_module._annotate_completeness(candidate, html)
    assert candidate.completeness_coverage == "source_prefix"
    assert candidate.completeness_score == 0.99
    assert "grounding_budget_limited" in candidate.completeness_reasons

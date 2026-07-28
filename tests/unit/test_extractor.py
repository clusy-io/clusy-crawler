from __future__ import annotations

import asyncio
import threading

import pytest

from app.services.extractor import (
    _count_words,
    _extract_raw_text,
    _extract_with_markdownify,
    _extract_with_readability,
    _extract_with_trafilatura,
    _log_host,
    extract_content,
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

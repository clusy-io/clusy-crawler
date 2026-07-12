from __future__ import annotations

from app.services.extractor import (
    _count_words,
    _extract_raw_text,
    _extract_with_markdownify,
    _extract_with_readability,
    _extract_with_trafilatura,
    extract_content,
)

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
        assert result.strategy in ("trafilatura", "readability", "markdownify", "raw_lxml")
        assert result.word_count > 0

    def test_handles_garbage(self):
        result = extract_content("   ", "")
        assert result.strategy == "raw_lxml"

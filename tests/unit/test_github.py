from __future__ import annotations

import pytest

from app.services.extractor import extract_content, extract_content_async
from app.services.github import (
    MAX_EXTRACTED_CHARS,
    MAX_THREAD_COMMENTS,
    GitHubPageKind,
    classify_github_url,
    extract_github_page,
    find_blob_raw_url,
    infer_source_language,
    wrap_github_source,
)


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("https://github.com/", GitHubPageKind.ROOT),
        ("https://github.com/openai", GitHubPageKind.ROOT),
        ("https://github.com/openai/codex", GitHubPageKind.REPOSITORY),
        ("https://github.com/openai/codex/tree/main/docs", GitHubPageKind.TREE),
        ("https://github.com/openai/codex/blob/main/README.md", GitHubPageKind.BLOB),
        ("https://github.com/openai/codex/issues/123", GitHubPageKind.ISSUES),
        ("https://github.com/openai/codex/pull/456", GitHubPageKind.PULL),
        ("https://github.com/openai/codex/discussions/789", GitHubPageKind.DISCUSSIONS),
        ("https://github.com/openai/codex/releases/tag/v1.0", GitHubPageKind.RELEASES),
        ("https://github.com/openai/codex/commit/abc123", GitHubPageKind.COMMIT),
        (
            "https://github.com/openai/codex/compare/v1.0...v2.0",
            GitHubPageKind.COMPARE,
        ),
        (
            "https://raw.githubusercontent.com/openai/codex/main/README.md",
            GitHubPageKind.RAW,
        ),
        ("https://github.com/openai/codex/raw/main/README.md", GitHubPageKind.RAW),
    ],
)
def test_classifies_exact_github_routes(url: str, kind: GitHubPageKind) -> None:
    parsed = classify_github_url(url)
    assert parsed is not None
    assert parsed.kind == kind


@pytest.mark.parametrize(
    "url",
    [
        "https://evilgithub.com/openai/codex",
        "https://github.com.attacker.test/openai/codex",
        "https://raw.githubusercontent.com.attacker.test/openai/codex/main/a.py",
        "https://github.example/openai/codex",
    ],
)
def test_rejects_github_lookalike_hosts(url: str) -> None:
    assert classify_github_url(url) is None


README_HTML = """
<html><body>
  <nav><div class="markdown-body">Navigation must not win.</div></nav>
  <main>
    <div id="readme">
      <article class="markdown-body" itemprop="text">
        <h1>Clusy Demo</h1>
        <p>A precise repository README with a <a href="/docs">documentation link</a>.</p>
        <pre><code>uv run pytest
print("kept")</code></pre>
      </article>
    </div>
  </main>
</body></html>
"""


def test_repository_readme_preserves_markdown_code_and_links() -> None:
    result = extract_content(README_HTML, "https://github.com/acme/clusy")

    assert result.strategy == "github-readme"
    assert result.title == "Clusy Demo"
    assert "# Clusy Demo" in result.text
    assert "[documentation link](/docs)" in result.text
    assert 'print("kept")' in result.text
    assert "Navigation must not win" not in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["adaptive", "quality"])
async def test_model_assisted_profiles_use_deterministic_github_route_first(
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
) -> None:
    async def unexpected_quality_call(*_args: object) -> None:
        raise AssertionError("GitHub specialized extraction must run first")

    monkeypatch.setattr(
        "app.services.quality_extractor.extract_quality_content",
        unexpected_quality_call,
    )

    result = await extract_content_async(
        README_HTML,
        "https://github.com/acme/clusy",
        extraction_profile=profile,
    )

    assert result.strategy == "github-readme"


THREAD_HTML = """
<html><body>
  <nav>
    Navigation chrome
    <pre><code>raise RuntimeError("chrome must not be injected")</code></pre>
  </nav>
  <main>
    <h1 data-testid="issue-title">Cache races under load</h1>
    <section data-testid="issue-body">
      <div class="markdown-body">
        <p>The opening report links to the <a href="https://example.test/tracker">tracker</a>.</p>
        <pre><code>def reproduce():
    return "content code"</code></pre>
        <div data-testid="reactions-container">👍 99</div>
        <button>Quote reply</button>
      </div>
    </section>
    <article data-testid="comment-body">
      <div class="markdown-body"><p>First useful comment with diagnosis.</p></div>
    </article>
    <article data-testid="comment-body">
      <div class="markdown-body"><p>First useful comment with diagnosis.</p></div>
    </article>
    <article data-testid="comment-body">
      <div class="markdown-body"><p>Second useful comment with the final fix.</p></div>
    </article>
    <div class="TimelineItem">force-pushed timeline chrome</div>
  </main>
</body></html>
"""


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/clusy/issues/17",
        "https://github.com/acme/clusy/pull/17",
        "https://github.com/acme/clusy/discussions/17",
    ],
)
def test_thread_extracts_first_post_and_unique_comments_without_chrome(url: str) -> None:
    result = extract_content(THREAD_HTML, url)

    assert result.strategy == "github-thread"
    assert result.title == "Cache races under load"
    assert result.text.startswith("# Cache races under load")
    assert "[tracker](https://example.test/tracker)" in result.text
    assert 'return "content code"' in result.text
    assert result.text.count("The opening report links to") == 1
    assert result.text.count("First useful comment with diagnosis.") == 1
    assert result.text.count("Second useful comment with the final fix.") == 1
    assert result.text.count("## Comments") == 1
    assert "99" not in result.text
    assert "Quote reply" not in result.text
    assert "timeline chrome" not in result.text
    assert "chrome must not be injected" not in result.text


def test_thread_prefers_nested_inner_body_without_creating_a_fake_comment() -> None:
    html = """
    <main>
      <h1 data-testid="issue-title">Nested body</h1>
      <section data-testid="issue-body">
        <h2>Description</h2>
        <p>author and issue actions outside the body</p>
        <div class="markdown-body">
          <div class="markdown-body NewMarkdownViewer-safe-html">
            <h2>Actual report</h2>
            <p>The issue body must appear exactly once.</p>
          </div>
        </div>
      </section>
    </main>
    """

    result = extract_github_page(html, "https://github.com/acme/clusy/issues/7284")

    assert result is not None
    assert result.text.count("The issue body must appear exactly once.") == 1
    assert "## Comments" not in result.text
    assert "author and issue actions outside the body" not in result.text
    assert result.truncated is False


def _thread_with_comments(count: int, extra: str = "") -> str:
    comments = "".join(
        (
            '<article data-testid="comment-body">'
            f'<div class="markdown-body"><p>Unique comment {index:03d}</p></div>'
            "</article>"
        )
        for index in range(count)
    )
    return (
        "<main>"
        '<h1 data-testid="issue-title">Bounded thread</h1>'
        '<section data-testid="issue-body">'
        '<div class="markdown-body"><p>Opening report.</p></div>'
        "</section>"
        f"{comments}{extra}</main>"
    )


def test_thread_exact_comment_cap_is_complete() -> None:
    result = extract_github_page(
        _thread_with_comments(MAX_THREAD_COMMENTS),
        "https://github.com/acme/clusy/issues/1",
    )

    assert result is not None
    assert f"Unique comment {MAX_THREAD_COMMENTS - 1:03d}" in result.text
    assert result.truncated is False
    assert "<!-- truncated:" not in result.text


def test_thread_observes_cap_plus_one_and_marks_partial() -> None:
    result = extract_github_page(
        _thread_with_comments(MAX_THREAD_COMMENTS + 1),
        "https://github.com/acme/clusy/issues/1",
    )

    assert result is not None
    assert f"Unique comment {MAX_THREAD_COMMENTS - 1:03d}" in result.text
    assert f"Unique comment {MAX_THREAD_COMMENTS:03d}" not in result.text
    assert result.truncated is True
    assert result.truncation_reason == "github_thread_comment_limit"
    assert "github_thread_comment_limit" in result.text
    assert "<!-- truncated:" in result.text


def test_thread_visible_load_control_marks_partial_before_internal_cap() -> None:
    result = extract_github_page(
        _thread_with_comments(1, "<button>Show 6 previous replies</button>"),
        "https://github.com/acme/clusy/discussions/1",
    )

    assert result is not None
    assert result.truncated is True
    assert result.truncation_reason == "github_thread_not_fully_loaded"
    assert "github_thread_not_fully_loaded" in result.text
    assert "Show 6 previous replies" not in result.text


def test_thread_total_output_cap_sets_explicit_status_and_marker() -> None:
    chunk = "bounded payload " * 6_000
    comments = "".join(
        (
            '<article data-testid="comment-body"><div class="markdown-body">'
            f"<p>Comment {index}: {chunk}</p></div></article>"
        )
        for index in range(6)
    )
    html = (
        "<main>"
        '<h1 data-testid="issue-title">Large bounded thread</h1>'
        '<section data-testid="issue-body"><div class="markdown-body">'
        f"<p>Opening: {chunk}</p></div></section>{comments}</main>"
    )

    result = extract_github_page(html, "https://github.com/acme/clusy/issues/2")

    assert result is not None
    assert len(result.text) <= MAX_EXTRACTED_CHARS
    assert result.truncated is True
    assert "github_thread_output_char_limit" in result.truncation_reason
    assert "github_thread_output_char_limit" in result.text


RELEASE_HTML = """
<html><body><main>
  <div class="release-header"><h1>Clusy v2.0.0</h1></div>
  <section class="release-body">
    <div class="markdown-body">
      <h2>Highlights</h2>
      <p>Faster crawling with bounded resource use.</p>
      <pre><code>clusy crawl https://example.test</code></pre>
      <div class="comment-reactions">rocket reaction</div>
    </div>
  </section>
</main></body></html>
"""


def test_release_extracts_release_body() -> None:
    result = extract_content(
        RELEASE_HTML,
        "https://github.com/acme/clusy/releases/tag/v2.0.0",
    )

    assert result.strategy == "github-release"
    assert result.title == "Clusy v2.0.0"
    assert "Faster crawling with bounded resource use." in result.text
    assert "clusy crawl https://example.test" in result.text
    assert "rocket reaction" not in result.text


def test_listing_empty_and_untrusted_pages_do_not_specialize() -> None:
    assert extract_github_page("<html><body></body></html>", "https://github.com/a/b") is None
    assert (
        extract_github_page(THREAD_HTML, "https://github.com/acme/clusy/issues")
        is None
    )
    assert (
        extract_github_page(README_HTML, "https://evilgithub.com/acme/clusy")
        is None
    )


TREE_HTML = """
<html>
  <head><meta charset="utf-8"><title>clusy/src at main · acme/clusy · GitHub</title></head>
  <body>
    <nav>
      Uh oh! Sign in
      <a href="/acme/clusy/blob/main/navigation.py">navigation noise</a>
    </nav>
    <main>
      <table aria-label="Files">
        <tr>
          <td><a href="/acme/clusy/tree/main/src/pkg">pkg</a></td>
          <td><a href="/acme/clusy/tree/main/src/pkg">pkg</a></td>
        </tr>
        <tr><td><a href="/acme/clusy/blob/main/src/app.py">app.py</a></td></tr>
        <tr><td><a href="/other/repo/blob/main/evil.py">evil.py</a></td></tr>
      </table>
      <button>Directory actions</button>
    </main>
  </body>
</html>
"""


def test_tree_extracts_only_same_repository_directory_entries() -> None:
    result = extract_github_page(
        TREE_HTML,
        "https://github.com/acme/clusy/tree/main/src",
    )

    assert result is not None
    assert result.strategy == "github-tree"
    assert result.title == "clusy/src at main · acme/clusy"
    assert result.text.count("[pkg]") == 1
    assert "[app.py]" in result.text
    assert "directory" in result.text
    assert "file" in result.text
    assert "navigation noise" not in result.text
    assert "evil.py" not in result.text
    assert "Uh oh" not in result.text
    assert "Directory actions" not in result.text


COMMIT_HTML = """
<html>
  <head><meta charset="utf-8"><title>Fix cache races · acme/clusy@abc123 · GitHub</title></head>
  <body><main>
    <p>2 files changed</p>
    <p>Lines changed: 2 additions &amp; 1 deletion</p>
    <table aria-label="Diff for: src/cache.py">
      <tbody>
        <tr><td class="diff-hunk-cell">@@ -1,2 +1,3 @@</td></tr>
        <tr><td>1</td><td>1</td><td class="diff-text-cell">-old = True</td></tr>
        <tr><td></td><td>2</td><td class="diff-text-cell">+new = True</td></tr>
      </tbody>
    </table>
  </main></body>
</html>
"""


def test_commit_extracts_semantic_summary_files_and_diff() -> None:
    result = extract_github_page(
        COMMIT_HTML,
        "https://github.com/acme/clusy/commit/abc123",
    )

    assert result is not None
    assert result.strategy == "github-commit"
    assert result.title == "Fix cache races · acme/clusy@abc123"
    assert "2 files changed" in result.text
    assert "Lines changed: 2 additions & 1 deletion" in result.text
    assert "### `src/cache.py`" in result.text
    assert "```diff" in result.text
    assert "@@ -1,2 +1,3 @@" in result.text
    assert "-old = True" in result.text
    assert "+new = True" in result.text
    assert result.truncated is False


def test_commit_ignores_hidden_zero_commit_control_noise() -> None:
    result = extract_github_page(
        COMMIT_HTML.replace(
            "<p>2 files changed</p>",
            "<p>0 commit</p><p>2 files changed</p>",
        ),
        "https://github.com/acme/clusy/commit/abc123",
    )

    assert result is not None
    assert "0 commit" not in result.text
    assert "2 files changed" in result.text


def test_compare_with_semantic_diff_is_complete() -> None:
    result = extract_github_page(
        COMMIT_HTML,
        "https://github.com/acme/clusy/compare/base...head",
    )

    assert result is not None
    assert result.strategy == "github-compare"
    assert "### `src/cache.py`" in result.text
    assert result.truncated is False


def test_compare_retains_real_commit_count() -> None:
    result = extract_github_page(
        COMMIT_HTML.replace(
            "<p>2 files changed</p>",
            "<p>2 commits</p><p>2 files changed</p>",
        ),
        "https://github.com/acme/clusy/compare/base...head",
    )

    assert result is not None
    assert "2 commits" in result.text


def test_compare_render_failure_is_explicitly_partial_not_normal_content() -> None:
    html = """
    <html>
      <head><meta charset="utf-8"><title>Comparing v1...v2 · acme/clusy · GitHub</title></head>
      <body><main>
        <p>2 commits</p><p>4 files changed</p>
        <p>Unfortunately it looks like we can’t render this comparison for you.</p>
      </main></body>
    </html>
    """

    result = extract_github_page(
        html,
        "https://github.com/acme/clusy/compare/v1...v2",
    )

    assert result is not None
    assert result.strategy == "github-compare-partial"
    assert result.truncated is True
    assert result.truncation_reason == "github_comparison_diff_unavailable"
    assert "[!WARNING]" in result.text
    assert "did not render the comparison diff" in result.text
    assert "can’t render this comparison for you" not in result.text
    assert "github_comparison_diff_unavailable" in result.text


def test_change_page_without_semantic_summary_or_diff_does_not_specialize() -> None:
    assert (
        extract_github_page(
            "<html><head><title>Commit · GitHub</title></head><main>chrome</main></html>",
            "https://github.com/acme/clusy/commit/abc123",
        )
        is None
    )


@pytest.mark.parametrize(
    ("html", "url", "strategy"),
    [
        (
            TREE_HTML,
            "https://github.com/acme/clusy/tree/main/src",
            "github-tree",
        ),
        (
            COMMIT_HTML,
            "https://github.com/acme/clusy/commit/abc123",
            "github-commit",
        ),
        (
            COMMIT_HTML,
            "https://github.com/acme/clusy/compare/base...head",
            "github-compare",
        ),
    ],
)
def test_new_github_routes_win_the_production_extractor_preflight(
    html: str,
    url: str,
    strategy: str,
) -> None:
    result = extract_content(html, url)

    assert result.strategy == strategy


def test_blob_raw_url_uses_rendered_canonical_link_without_guessing_ref() -> None:
    blob_url = "https://github.com/acme/clusy/blob/feature/docs/v2/src/main.py"
    raw_url = (
        "https://raw.githubusercontent.com/acme/clusy/"
        "feature/docs/v2/src/main.py?download=1"
    )
    html = f"""
    <html><body>
      <a data-testid="raw-button"
         href="https://raw.githubusercontent.com/attacker/clusy/main/src/main.py">wrong repo</a>
      <a data-testid="raw-button"
         href="https://raw.githubusercontent.com.evil.test/acme/clusy/main/src/main.py">evil</a>
      <a data-testid="raw-button" href="{raw_url}">Raw</a>
    </body></html>
    """

    assert find_blob_raw_url(html, blob_url) == raw_url


def test_blob_raw_url_accepts_current_live_github_raw_route() -> None:
    blob_url = "https://github.com/psf/requests/blob/main/README.md"
    raw_url = "https://github.com/psf/requests/raw/refs/heads/main/README.md"
    html = f'<a data-testid="raw-button" href="{raw_url}">Raw</a>'

    assert find_blob_raw_url(html, blob_url) == raw_url


@pytest.mark.parametrize(
    "href",
    [
        "https://raw.githubusercontent.com/other/clusy/main/src/main.py",
        "https://raw.githubusercontent.com/acme/other/main/src/main.py",
        "https://raw.githubusercontent.com.evil.test/acme/clusy/main/src/main.py",
        "https://github.com/acme/clusy/blob/main/src/main.py",
        "http://raw.githubusercontent.com/acme/clusy/main/src/main.py",
        "https://user@raw.githubusercontent.com/acme/clusy/main/src/main.py",
        "https://raw.githubusercontent.com/acme/clusy/main/src/other.py",
    ],
)
def test_blob_raw_url_rejects_untrusted_or_mismatched_link(href: str) -> None:
    html = f'<a data-testid="raw-button" href="{href}">Raw</a>'
    assert (
        find_blob_raw_url(
            html,
            "https://github.com/acme/clusy/blob/main/src/main.py",
        )
        is None
    )


@pytest.mark.parametrize(
    ("url", "content_type", "language"),
    [
        ("https://raw.githubusercontent.com/a/b/main/main.py", "", "python"),
        ("https://github.com/a/b/blob/main/app.tsx", "", "tsx"),
        ("https://github.com/a/b/blob/main/Dockerfile", "", "dockerfile"),
        ("https://github.com/a/b/blob/main/data", "application/json; charset=utf-8", "json"),
        ("https://github.com/a/b/blob/main/unknown", "", "text"),
    ],
)
def test_source_language_detection(url: str, content_type: str, language: str) -> None:
    assert infer_source_language(url, content_type) == language


def test_source_wrapper_preserves_markdown_and_uses_collision_safe_fence() -> None:
    markdown = "# Existing README\n\nUseful prose."
    readme = wrap_github_source(
        markdown,
        "https://raw.githubusercontent.com/a/b/main/README.md",
        "text/markdown",
    )
    assert readme is not None
    assert readme.text == markdown
    assert readme.language == "markdown"

    source = wrap_github_source(
        'print("hello")\n```\nnot a closing wrapper',
        "https://raw.githubusercontent.com/a/b/main/example.py",
    )
    assert source is not None
    assert source.strategy == "github-source"
    assert source.language == "python"
    assert source.text.startswith("# example.py\n\n````python\n")
    assert source.text.endswith("\n````")


def test_github_outputs_have_hard_caps_and_empty_source_is_rejected() -> None:
    huge = "useful content " * (MAX_EXTRACTED_CHARS // 5)
    html = (
        '<main><h1 data-testid="issue-title">Large issue</h1>'
        f'<div data-testid="issue-body"><div class="markdown-body">{huge}</div></div></main>'
    )
    result = extract_github_page(html, "https://github.com/acme/clusy/issues/1")

    assert result is not None
    assert len(result.text) <= MAX_EXTRACTED_CHARS
    assert result.truncated is True
    assert "github_thread_item_char_limit" in result.truncation_reason
    assert "<!-- truncated:" in result.text
    assert wrap_github_source("", "https://raw.githubusercontent.com/a/b/main/a.py") is None

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit, urlunsplit

from lxml import html as lxml_html
from markdownify import markdownify

if TYPE_CHECKING:
    from typing import Any

# GitHub pages can contain thousands of timeline items (or a generated source
# file several megabytes long). These limits are deliberately local to the
# specialized extractor so an unusual repository cannot monopolize a worker.
MAX_THREAD_COMMENTS = 100
MAX_RELEASE_BODIES = 50
MAX_TREE_ENTRIES = 1_000
MAX_DIFF_FILES = 100
MAX_DIFF_LINES_PER_FILE = 5_000
MAX_ITEM_CHARS = 100_000
MAX_EXTRACTED_CHARS = 500_000
MAX_SOURCE_CHARS = 1_000_000

_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})
_RAW_HOST = "raw.githubusercontent.com"
_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
_RESERVED_ROOTS = frozenset(
    {
        "about",
        "account",
        "collections",
        "customer-stories",
        "enterprise",
        "events",
        "explore",
        "features",
        "issues",
        "login",
        "marketplace",
        "new",
        "notifications",
        "organizations",
        "orgs",
        "pricing",
        "readme",
        "search",
        "security",
        "settings",
        "signup",
        "site",
        "sponsors",
        "topics",
        "trending",
        "users",
    }
)


class GitHubPageKind(StrEnum):
    ROOT = "root"
    REPOSITORY = "repo"
    TREE = "tree"
    BLOB = "blob"
    ISSUES = "issues"
    PULL = "pull"
    DISCUSSIONS = "discussions"
    RELEASES = "releases"
    COMMIT = "commit"
    COMPARE = "compare"
    RAW = "raw"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class GitHubURL:
    kind: GitHubPageKind
    owner: str = ""
    repository: str = ""
    number: int | None = None


@dataclass(frozen=True, slots=True)
class GitHubExtraction:
    text: str
    title: str
    strategy: str
    language: str = ""
    truncated: bool = False
    truncation_reason: str = ""


def _safe_hostname(url: str) -> tuple[str, str] | None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
    ):
        return None
    return host, parsed.path


def _path_segments(path: str) -> list[str]:
    parts = [unquote(part) for part in path.split("/") if part]
    if any(part in {".", ".."} or "/" in part or "\\" in part for part in parts):
        return []
    return parts


def _valid_owner_repo(owner: str, repository: str) -> bool:
    return bool(
        len(owner) <= 39
        and len(repository) <= 100
        and _OWNER_RE.fullmatch(owner)
        and _NAME_RE.fullmatch(repository)
    )


def classify_github_url(url: str) -> GitHubURL | None:
    """Classify only canonical public GitHub hosts and well-formed paths.

    Hostname parsing is exact: lookalikes such as ``evilgithub.com`` and
    ``github.com.attacker.test`` never enter GitHub-specific extraction.
    """
    host_path = _safe_hostname(url)
    if host_path is None:
        return None
    host, path = host_path
    if host not in _GITHUB_HOSTS and host != _RAW_HOST:
        return None

    parts = _path_segments(path)
    if host == _RAW_HOST:
        if len(parts) < 4 or not _valid_owner_repo(parts[0], parts[1]):
            return GitHubURL(GitHubPageKind.OTHER)
        return GitHubURL(GitHubPageKind.RAW, parts[0], parts[1])

    if not parts or len(parts) == 1 or parts[0].casefold() in _RESERVED_ROOTS:
        return GitHubURL(GitHubPageKind.ROOT)
    owner, repository = parts[0], parts[1].removesuffix(".git")
    if not _valid_owner_repo(owner, repository):
        return GitHubURL(GitHubPageKind.OTHER)
    if len(parts) == 2:
        return GitHubURL(GitHubPageKind.REPOSITORY, owner, repository)

    route = parts[2].casefold()
    route_kinds = {
        "tree": GitHubPageKind.TREE,
        "blob": GitHubPageKind.BLOB,
        "raw": GitHubPageKind.RAW,
        "issues": GitHubPageKind.ISSUES,
        "pull": GitHubPageKind.PULL,
        "discussions": GitHubPageKind.DISCUSSIONS,
        "releases": GitHubPageKind.RELEASES,
        "commit": GitHubPageKind.COMMIT,
        "compare": GitHubPageKind.COMPARE,
    }
    kind = route_kinds.get(route, GitHubPageKind.OTHER)
    number: int | None = None
    if (
        kind in {GitHubPageKind.ISSUES, GitHubPageKind.PULL, GitHubPageKind.DISCUSSIONS}
        and len(parts) >= 4
        and parts[3].isdigit()
    ):
        number = int(parts[3])
    return GitHubURL(kind, owner, repository, number)


def is_github_url(url: str) -> bool:
    return classify_github_url(url) is not None


def _parse_html(html_content: str) -> Any | None:
    if not html_content.strip():
        return None
    try:
        return lxml_html.fromstring(html_content.encode("utf-8"))
    except (TypeError, ValueError, lxml_html.ParserError):
        return None


_NOISE_SELECTORS = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "form",
    "button",
    ".comment-reactions",
    ".reaction-summary-item",
    ".js-reaction-group",
    ".js-comment-edit-history",
    ".timeline-comment-actions",
    ".timeline-comment-header",
    '[data-testid="reactions-container"]',
    '[data-testid="comment-header"]',
    '[aria-label="Reactions"]',
)


def _detach_noise(element: Any) -> None:
    for selector in _NOISE_SELECTORS:
        try:
            matches = element.cssselect(selector)
        except Exception:
            continue
        for match in matches:
            parent = match.getparent()
            if parent is not None:
                parent.remove(match)


def _clean_markdown(text: str) -> str:
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def _cap_markdown(text: str, limit: int) -> str:
    return _cap_markdown_with_status(text, limit)[0]


def _cap_markdown_with_status(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    marker = "\n\n<!-- truncated -->"
    cut = max(0, limit - len(marker))
    boundary = text.rfind("\n\n", max(0, cut - 4000), cut)
    if boundary < cut // 2:
        boundary = cut
    return text[:boundary].rstrip() + marker, True


def _element_markdown(element: Any) -> str:
    return _element_markdown_with_status(element)[0]


def _element_markdown_with_status(element: Any) -> tuple[str, bool]:
    isolated = copy.deepcopy(element)
    _detach_noise(isolated)
    fragment = lxml_html.tostring(isolated, encoding="unicode")
    return _cap_markdown_with_status(
        _clean_markdown(markdownify(fragment, strip=["img"], heading_style="ATX")),
        MAX_ITEM_CHARS,
    )


def _dedup_key(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _ordered_elements(root: Any, selectors: tuple[str, ...]) -> list[Any]:
    tree = root.getroottree()
    document_order = {
        tree.getpath(element): index for index, element in enumerate(root.iter())
    }
    found: dict[str, Any] = {}
    for selector in selectors:
        try:
            for element in root.cssselect(selector):
                found[tree.getpath(element)] = element
        except Exception:
            continue
    return sorted(
        found.values(),
        key=lambda element: document_order.get(tree.getpath(element), 0),
    )


def _limited_unique_markdown(
    elements: list[Any],
    limit: int,
    *,
    seen_keys: set[str] | None = None,
) -> tuple[list[str], bool, bool]:
    results: list[str] = []
    seen = set() if seen_keys is None else set(seen_keys)
    item_truncated = False
    for element in elements:
        text, was_truncated = _element_markdown_with_status(element)
        key = _dedup_key(text)
        if not key or key in seen:
            continue
        seen.add(key)
        # Inspect one unique item beyond the cap. Merely filling the cap does
        # not prove that content was omitted.
        if len(results) >= limit:
            return results, True, item_truncated
        results.append(text)
        item_truncated = item_truncated or was_truncated
    return results, False, item_truncated


def _unique_markdown(elements: list[Any], limit: int) -> list[str]:
    return _limited_unique_markdown(elements, limit)[0]


def _plain_text(element: Any) -> str:
    return re.sub(r"\s+", " ", element.text_content()).strip()


def _first_title(root: Any, selectors: tuple[str, ...]) -> str:
    for selector in selectors:
        try:
            candidates = root.cssselect(selector)
        except Exception:
            continue
        for candidate in candidates:
            title = _plain_text(candidate)
            if title:
                return title[:1000]
    return ""


def _drop_repeated_title(body: str, title: str) -> str:
    first_line, separator, remainder = body.partition("\n")
    heading = re.sub(r"^#{1,6}\s+", "", first_line).strip()
    if separator and heading.casefold() == title.casefold():
        return remainder.lstrip()
    return body


def _compose_thread(
    title: str,
    initial_body: str,
    comments: list[str],
    truncation_reasons: list[str],
) -> tuple[str, list[str]]:
    parts: list[str] = []
    if title:
        parts.append(f"# {title}")
    if initial_body:
        initial = _drop_repeated_title(initial_body, title) if title else initial_body
        if initial:
            parts.append(initial)
    if comments:
        parts.append("## Comments")
        parts.append("\n\n---\n\n".join(comments))

    text = "\n\n".join(parts).strip()
    reasons = list(dict.fromkeys(truncation_reasons))
    if len(text) > MAX_EXTRACTED_CHARS:
        reasons.append("github_thread_output_char_limit")
    reasons = list(dict.fromkeys(reasons))
    if not reasons:
        return text, reasons

    marker = (
        "<!-- truncated: GitHub thread is partial; reasons="
        + ",".join(reasons)
        + " -->"
    )
    budget = max(0, MAX_EXTRACTED_CHARS - len(marker) - 2)
    bounded, _ = _cap_markdown_with_status(text, budget)
    return bounded.rstrip() + "\n\n" + marker, reasons


def _extract_readme(root: Any) -> GitHubExtraction | None:
    selectors = (
        "#readme article.markdown-body",
        "#readme .markdown-body",
        '[data-testid="readme"] .markdown-body',
        "article.markdown-body[itemprop='text']",
        "article.markdown-body",
        ".markdown-body[itemprop='text']",
    )
    elements = _ordered_elements(root, selectors)
    if not elements:
        return None
    text = _element_markdown(elements[0])
    if not _dedup_key(text):
        return None
    title = _first_title(elements[0], ("h1",))
    return GitHubExtraction(text=text, title=title, strategy="github-readme")


_THREAD_INITIAL_BODY_GROUPS = (
    ('[data-testid="issue-body"] .markdown-body', '[data-testid="issue-body"]'),
    (
        '[data-testid="discussion-body"] .markdown-body',
        '[data-testid="discussion-body"]',
    ),
    (
        '[data-testid="pull-request-body"] .markdown-body',
        '[data-testid="pull-request-body"]',
    ),
    (".js-issue-body .markdown-body", ".js-issue-body"),
    (
        ".discussion .js-comment-container .js-comment-body.markdown-body",
        ".discussion .js-comment-container",
    ),
)

_THREAD_COMMENT_FAMILIES = (
    (
        '[data-testid="comment-body"]',
        (".markdown-body",),
    ),
    (
        ".js-comment-container",
        (
            ".js-comment-body.markdown-body",
            ".js-comment-body",
            ".comment-body.markdown-body",
            ".comment-body",
        ),
    ),
    (
        ".timeline-comment",
        (".comment-body.markdown-body", ".comment-body"),
    ),
    ("td.comment-body", ()),
    ("div.comment-body", ()),
)

_THREAD_MORE_ITEMS_RE = re.compile(
    r"\b(?:show|load|view)\s+(?:\d[\d,]*\s+)?"
    r"(?:more|previous)\s+(?:comments?|replies?)\b|"
    r"\bload\s+more\b",
    re.IGNORECASE,
)
_THREAD_REPORTED_COUNT_RE = re.compile(
    r"\b(\d[\d,]*)\s+(?:comments?|replies?)\b",
    re.IGNORECASE,
)

_THREAD_TITLE_SELECTORS = (
    'h1[data-testid="issue-title"]',
    '[data-testid="issue-title"]',
    'h1[data-testid="discussion-title"]',
    '[data-testid="discussion-title"]',
    "h1 .js-issue-title",
    "bdi.js-issue-title",
    "h1 .markdown-title",
    "h1",
)


def _first_preferred_element(
    root: Any,
    selector_groups: tuple[tuple[str, str], ...],
) -> Any | None:
    """Choose one logical body, preferring its inner Markdown over its wrapper."""
    for inner_selector, outer_selector in selector_groups:
        try:
            inner = root.cssselect(inner_selector)
        except Exception:
            inner = []
        if inner:
            return inner[0]
        try:
            outer = root.cssselect(outer_selector)
        except Exception:
            outer = []
        if outer:
            return outer[0]
    return None


def _comment_elements(root: Any) -> list[Any]:
    """Return one content element per logical comment in document order."""
    for outer_selector, inner_selectors in _THREAD_COMMENT_FAMILIES:
        try:
            containers = root.cssselect(outer_selector)
        except Exception:
            containers = []
        if not containers:
            continue

        selected: list[Any] = []
        seen_paths: set[str] = set()
        tree = root.getroottree()
        for container in containers:
            content = container
            for inner_selector in inner_selectors:
                try:
                    inner = container.cssselect(inner_selector)
                except Exception:
                    inner = []
                if inner:
                    content = inner[0]
                    break
            path = tree.getpath(content)
            if path not in seen_paths:
                seen_paths.add(path)
                selected.append(content)
        return selected
    return []


def _thread_has_unloaded_items(scope: Any, extracted_comments: int) -> bool:
    for selector in ("button", "a", "summary", "[role='button']", "form"):
        try:
            controls = scope.cssselect(selector)
        except Exception:
            continue
        for control in controls:
            label = " ".join(
                filter(
                    None,
                    (
                        control.get("aria-label", ""),
                        control.get("title", ""),
                        control.get("value", ""),
                        _plain_text(control),
                    ),
                )
            )
            if _THREAD_MORE_ITEMS_RE.search(label):
                return True

    reported = 0
    for selector in ("h1", "h2", "h3", "[aria-label]"):
        try:
            candidates = scope.cssselect(selector)
        except Exception:
            continue
        for candidate in candidates:
            label = " ".join(
                filter(
                    None,
                    (candidate.get("aria-label", ""), _plain_text(candidate)),
                )
            )
            for match in _THREAD_REPORTED_COUNT_RE.finditer(label):
                reported = max(reported, int(match.group(1).replace(",", "")))
    return reported > extracted_comments


def _extract_thread(root: Any) -> GitHubExtraction | None:
    scope_candidates = root.cssselect("main")
    scope = scope_candidates[0] if scope_candidates else root
    title = _first_title(scope, _THREAD_TITLE_SELECTORS)
    initial_element = _first_preferred_element(scope, _THREAD_INITIAL_BODY_GROUPS)
    candidates = _comment_elements(scope)
    if initial_element is None and candidates:
        initial_element, candidates = candidates[0], candidates[1:]

    initial_body = ""
    initial_truncated = False
    initial_path = ""
    if initial_element is not None:
        initial_body, initial_truncated = _element_markdown_with_status(initial_element)
        initial_path = scope.getroottree().getpath(initial_element)

    filtered_candidates = [
        candidate
        for candidate in candidates
        if scope.getroottree().getpath(candidate) != initial_path
    ]
    seen = {_dedup_key(initial_body)} if _dedup_key(initial_body) else set()
    comments, hit_comment_cap, comment_item_truncated = _limited_unique_markdown(
        filtered_candidates,
        MAX_THREAD_COMMENTS,
        seen_keys=seen,
    )
    reasons: list[str] = []
    if hit_comment_cap:
        reasons.append("github_thread_comment_limit")
    if initial_truncated or comment_item_truncated:
        reasons.append("github_thread_item_char_limit")
    if _thread_has_unloaded_items(scope, len(comments)):
        reasons.append("github_thread_not_fully_loaded")

    text, reasons = _compose_thread(title, initial_body, comments, reasons)
    if not _dedup_key(text):
        return None
    return GitHubExtraction(
        text=text,
        title=title,
        strategy="github-thread",
        truncated=bool(reasons),
        truncation_reason=",".join(reasons),
    )


def _page_title(root: Any) -> str:
    title = _first_title(root, ("title",))
    title = re.sub(r"\s+·\s+GitHub\s*$", "", title, flags=re.IGNORECASE)
    return title[:1000]


def _safe_generated_text(text: str, limit: int = 1000) -> str:
    text = text.replace("\u200e", "").replace("\u200f", "").replace("\x00", "")
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _markdown_link(label: str, href: str) -> str:
    safe_label = re.sub(r"([\\\[\]])", r"\\\1", _safe_generated_text(label))
    safe_href = href.replace(">", "%3E").replace(" ", "%20")
    return f"[{safe_label}](<{safe_href}>)"


def _inline_code(text: str) -> str:
    text = _safe_generated_text(text)
    longest_run = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(1, longest_run + 1)
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def _fenced_block(text: str, language: str) -> str:
    longest_run = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest_run + 1)
    return f"{fence}{language}\n{text}\n{fence}"


def _trusted_repository_link(
    href: str,
    github_url: GitHubURL,
) -> tuple[GitHubPageKind, str] | None:
    href = href.strip()
    if not href or href.startswith(("#", "//")):
        return None
    absolute = "https://github.com" + href if href.startswith("/") else href
    parsed = classify_github_url(absolute)
    if (
        parsed is None
        or parsed.owner.casefold() != github_url.owner.casefold()
        or parsed.repository.casefold() != github_url.repository.casefold()
        or parsed.kind not in {GitHubPageKind.TREE, GitHubPageKind.BLOB}
    ):
        return None
    return parsed.kind, href


def _extract_tree(root: Any, github_url: GitHubURL) -> GitHubExtraction | None:
    scope_candidates = root.cssselect("main")
    scope = scope_candidates[0] if scope_candidates else root
    listing_tables: list[Any] = []
    for table in scope.cssselect("table"):
        if table.cssselect('a[href*="/blob/"], a[href*="/tree/"]'):
            listing_tables.append(table)
    if not listing_tables:
        return None

    entries: list[tuple[GitHubPageKind, str, str]] = []
    seen: set[tuple[GitHubPageKind, str]] = set()
    hit_cap = False
    for table in listing_tables:
        for link in table.cssselect('a[href*="/blob/"], a[href*="/tree/"]'):
            href = (link.get("href") or "").strip()
            trusted = _trusted_repository_link(href, github_url)
            if trusted is None:
                continue
            kind, safe_href = trusted
            name = _safe_generated_text(link.text_content())
            if not name or name in {".", "..", "parent directory"}:
                continue
            key = (kind, safe_href)
            if key in seen:
                continue
            seen.add(key)
            if len(entries) >= MAX_TREE_ENTRIES:
                hit_cap = True
                break
            entries.append((kind, name, safe_href))
        if hit_cap:
            break
    if not entries:
        return None

    title = _page_title(root) or f"{github_url.owner}/{github_url.repository}"
    lines = [f"# {title}", "", "## Files", ""]
    for kind, name, href in entries:
        entry_type = "directory" if kind == GitHubPageKind.TREE else "file"
        lines.append(f"- {_markdown_link(name, href)} — {entry_type}")
    if hit_cap:
        lines.extend(
            (
                "",
                "<!-- truncated: GitHub tree entry limit "
                f"({MAX_TREE_ENTRIES}) reached; additional entries omitted -->",
            )
        )
    text, output_truncated = _cap_markdown_with_status(
        "\n".join(lines),
        MAX_EXTRACTED_CHARS,
    )
    reasons: list[str] = []
    if hit_cap:
        reasons.append("github_tree_entry_limit")
    if output_truncated:
        reasons.append("github_tree_output_char_limit")
    return GitHubExtraction(
        text=text,
        title=title,
        strategy="github-tree",
        truncated=bool(reasons),
        truncation_reason=",".join(reasons),
    )


_DIFF_TABLE_LABEL_RE = re.compile(r"^Diff for:\s*(.+)$", re.IGNORECASE)
_COMPARE_UNAVAILABLE_RE = re.compile(
    r"(?:can(?:not|'t|’t)|could(?:\s+not|n't|n’t)|unable\s+to)\s+"
    r"render\s+(?:this\s+)?comparison|"
    r"comparison\s+is\s+taking\s+too\s+long\s+to\s+generate|"
    r"error\s+while\s+loading\s+(?:this\s+|the\s+)?comparison",
    re.IGNORECASE,
)
_CHANGE_SUMMARY_PATTERNS = (
    re.compile(r"\b\d[\d,]*\s+commits?\b", re.IGNORECASE),
    re.compile(r"\b\d[\d,]*\s+files?\s+changed\b", re.IGNORECASE),
    re.compile(
        r"\bLines changed:\s*\d[\d,]*\s+additions?\s*&\s*"
        r"\d[\d,]*\s+deletions?\b",
        re.IGNORECASE,
    ),
)
_FILES_CHANGED_RE = re.compile(r"\b(\d[\d,]*)\s+files?\s+changed\b", re.IGNORECASE)


def _change_summary(
    scope: Any,
    *,
    include_commit_count: bool,
) -> tuple[list[str], int | None]:
    # Join DOM text nodes explicitly: adjacent elements do not necessarily
    # carry whitespace in minified GitHub HTML, and ``text_content()`` would
    # otherwise turn ``</p><p>`` into a single unmatchable token.
    visible_text = _safe_generated_text(
        " ".join(scope.itertext()),
        limit=200_000,
    )
    summary: list[str] = []
    seen: set[str] = set()
    patterns = (
        _CHANGE_SUMMARY_PATTERNS
        if include_commit_count
        else _CHANGE_SUMMARY_PATTERNS[1:]
    )
    for pattern in patterns:
        for match in pattern.finditer(visible_text):
            value = _safe_generated_text(match.group(0), limit=200)
            key = value.casefold()
            if key not in seen:
                seen.add(key)
                summary.append(value)
                break
    files_changed_match = _FILES_CHANGED_RE.search(visible_text)
    files_changed = (
        int(files_changed_match.group(1).replace(",", ""))
        if files_changed_match
        else None
    )
    return summary, files_changed


def _diff_row_text(row: Any) -> str | None:
    cells = row.cssselect("td")
    if not cells:
        return None
    cell = cells[-1]
    text = "".join(cell.itertext()).replace("\r", "").replace("\n", "")
    text = text.replace("\u200e", "").replace("\u200f", "")
    classes = set((cell.get("class") or "").split())
    if "diff-hunk-cell" in classes:
        return text.strip()
    if not text:
        return " "
    if text[0] not in {"+", "-", " ", "@"}:
        return " " + text
    return text


def _extract_diff_sections(scope: Any) -> tuple[list[str], list[str]]:
    sections: list[str] = []
    reasons: list[str] = []
    seen_paths: set[str] = set()
    for table in scope.cssselect("table[aria-label]"):
        label = _safe_generated_text(table.get("aria-label", ""))
        match = _DIFF_TABLE_LABEL_RE.match(label)
        if match is None:
            continue
        raw_path = match.group(1)
        path_key = _safe_generated_text(raw_path)
        if not path_key or path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        if len(sections) >= MAX_DIFF_FILES:
            reasons.append("github_diff_file_limit")
            break

        lines: list[str] = []
        hit_line_cap = False
        for row in table.cssselect("tbody tr"):
            line = _diff_row_text(row)
            if line is None:
                continue
            if len(lines) >= MAX_DIFF_LINES_PER_FILE:
                hit_line_cap = True
                break
            lines.append(line)
        if hit_line_cap:
            reasons.append("github_diff_line_limit")
            lines.append("... diff truncated at configured line limit ...")
        if not lines:
            reasons.append("github_diff_content_missing")
            body = "> Diff content was not present in the fetched page."
        else:
            body = _fenced_block("\n".join(lines), "diff")
        sections.append(f"### {_inline_code(raw_path)}\n\n{body}")
    return sections, list(dict.fromkeys(reasons))


def _extract_change_page(
    root: Any,
    kind: GitHubPageKind,
) -> GitHubExtraction | None:
    scope_candidates = root.cssselect("main")
    scope = scope_candidates[0] if scope_candidates else root
    title = _page_title(root)
    # A compare page legitimately summarizes a commit count. A single-commit
    # page does not: GitHub currently exposes an unrelated hidden "0 commit"
    # control in its main subtree, which must not leak into extracted content.
    summary, files_changed = _change_summary(
        scope,
        include_commit_count=kind == GitHubPageKind.COMPARE,
    )
    diff_sections, reasons = _extract_diff_sections(scope)
    visible_text = _safe_generated_text(scope.text_content(), limit=300_000)
    comparison_unavailable = bool(
        kind == GitHubPageKind.COMPARE
        and _COMPARE_UNAVAILABLE_RE.search(visible_text)
    )

    if comparison_unavailable:
        reasons.append("github_comparison_diff_unavailable")
    elif not diff_sections and files_changed not in {None, 0}:
        reasons.append("github_change_diff_unavailable")
    elif not diff_sections and files_changed is None:
        # A title alone is not enough evidence for a useful, route-specific
        # result. Let the caller use its generic fallback.
        return None
    reasons = list(dict.fromkeys(reasons))

    parts: list[str] = []
    if title:
        parts.append(f"# {title}")
    if summary:
        parts.append("## Summary\n\n" + "\n".join(f"- {item}" for item in summary))
    if comparison_unavailable:
        parts.append(
            "> [!WARNING]\n"
            "> Partial result: GitHub did not render the comparison diff in "
            "the fetched page."
        )
    elif "github_change_diff_unavailable" in reasons:
        parts.append(
            "> [!WARNING]\n"
            "> Partial result: GitHub reported changed files, but their diff "
            "was not present in the fetched page."
        )
    if diff_sections:
        parts.append("## Diff\n\n" + "\n\n".join(diff_sections))
    text = "\n\n".join(parts).strip()
    if not _dedup_key(text):
        return None

    if len(text) > MAX_EXTRACTED_CHARS:
        reasons.append("github_change_output_char_limit")
        reasons = list(dict.fromkeys(reasons))
    if reasons:
        marker = (
            "<!-- partial: GitHub change page; reasons="
            + ",".join(reasons)
            + " -->"
        )
        budget = max(0, MAX_EXTRACTED_CHARS - len(marker) - 2)
        text, _ = _cap_markdown_with_status(text, budget)
        text = text.rstrip() + "\n\n" + marker

    route_name = "commit" if kind == GitHubPageKind.COMMIT else "compare"
    strategy = f"github-{route_name}" + ("-partial" if reasons else "")
    return GitHubExtraction(
        text=text,
        title=title,
        strategy=strategy,
        truncated=bool(reasons),
        truncation_reason=",".join(reasons),
    )


def _extract_release(root: Any) -> GitHubExtraction | None:
    scope_candidates = root.cssselect("main")
    scope = scope_candidates[0] if scope_candidates else root
    title = _first_title(
        scope,
        (
            '[data-testid="release-title"]',
            ".release-header h1",
            ".release-header h2",
            "h1",
        ),
    )
    bodies = _unique_markdown(
        _ordered_elements(
            scope,
            (
                '[data-testid="release-body"] .markdown-body',
                '[data-testid="release-body"]',
                ".release-body .markdown-body",
                ".release-body",
                ".markdown-body",
            ),
        ),
        MAX_RELEASE_BODIES,
    )
    text, _ = _compose_thread(
        title,
        bodies[0] if bodies else "",
        bodies[1:],
        [],
    )
    if not _dedup_key(text):
        return None
    return GitHubExtraction(text=text, title=title, strategy="github-release")


def extract_github_page(html_content: str, url: str) -> GitHubExtraction | None:
    """Extract one bounded, route-specific view from public GitHub HTML."""
    github_url = classify_github_url(url)
    if github_url is None:
        return None
    root = _parse_html(html_content)
    if root is None:
        return None
    if github_url.kind == GitHubPageKind.REPOSITORY:
        return _extract_readme(root)
    if github_url.kind == GitHubPageKind.TREE:
        return _extract_tree(root, github_url)
    if github_url.kind in {
        GitHubPageKind.ISSUES,
        GitHubPageKind.PULL,
        GitHubPageKind.DISCUSSIONS,
    }:
        # Listing/create pages contain timeline-shaped chrome but no first post.
        if github_url.number is None:
            return None
        return _extract_thread(root)
    if github_url.kind == GitHubPageKind.RELEASES:
        return _extract_release(root)
    if github_url.kind in {GitHubPageKind.COMMIT, GitHubPageKind.COMPARE}:
        return _extract_change_page(root, github_url.kind)
    return None


def _trusted_raw_candidate(
    candidate: str,
    owner: str,
    repository: str,
    expected_filename: str,
) -> str | None:
    candidate = candidate.strip()
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme.lower() != "https"
        or host not in {*_GITHUB_HOSTS, _RAW_HOST}
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        return None
    parts = _path_segments(parsed.path)
    common_valid = (
        len(parts) >= 4
        and parts[0].casefold() == owner.casefold()
        and parts[1].casefold() == repository.casefold()
        and parts[-1].casefold() == expected_filename.casefold()
    )
    github_raw_valid = host in _GITHUB_HOSTS and len(parts) >= 5 and parts[2].casefold() == "raw"
    if not common_valid or (host != _RAW_HOST and not github_raw_valid):
        return None
    canonical_host = "github.com" if host in _GITHUB_HOSTS else _RAW_HOST
    return urlunsplit(("https", canonical_host, parsed.path, parsed.query, ""))


def find_blob_raw_url(html_content: str, blob_url: str) -> str | None:
    """Return GitHub's canonical raw link without guessing the ref boundary.

    A branch or tag may itself contain slashes, so transforming a ``/blob/``
    path is ambiguous. We instead consume only a server-rendered Raw control,
    then independently validate its host, route, and repository identity.
    """
    github_url = classify_github_url(blob_url)
    if github_url is None or github_url.kind != GitHubPageKind.BLOB:
        return None
    blob_parts = _path_segments(urlsplit(blob_url).path)
    if len(blob_parts) < 5:
        return None
    expected_filename = blob_parts[-1]
    root = _parse_html(html_content)
    if root is None:
        return None

    candidates: list[Any] = []
    for selector in (
        'a[data-testid="raw-button"][href]',
        "a#raw-url[href]",
        'a[data-hotkey="r"][href]',
        'a[aria-label="Raw"][href]',
        'a[href*="raw.githubusercontent.com"][href]',
        'link[rel="alternate"][href]',
    ):
        try:
            candidates.extend(root.cssselect(selector))
        except Exception:
            continue
    seen: set[str] = set()
    for element in candidates:
        href = (element.get("href") or "").strip()
        if not href or href in seen:
            continue
        seen.add(href)
        trusted = _trusted_raw_candidate(
            href,
            github_url.owner,
            github_url.repository,
            expected_filename,
        )
        if trusted is not None:
            return trusted
    return None


_EXTENSION_LANGUAGES = {
    ".bash": "bash",
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".css": "css",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".htm": "html",
    ".html": "html",
    ".java": "java",
    ".js": "javascript",
    ".json": "json",
    ".jsx": "jsx",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".md": "markdown",
    ".mdx": "mdx",
    ".php": "php",
    ".py": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".scss": "scss",
    ".sh": "bash",
    ".sql": "sql",
    ".swift": "swift",
    ".toml": "toml",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".txt": "text",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
}
_CONTENT_TYPE_LANGUAGES = {
    "application/json": "json",
    "application/toml": "toml",
    "application/xml": "xml",
    "application/yaml": "yaml",
    "text/css": "css",
    "text/csv": "csv",
    "text/html": "html",
    "text/javascript": "javascript",
    "text/markdown": "markdown",
    "text/x-python": "python",
    "text/xml": "xml",
    "text/yaml": "yaml",
}
_SPECIAL_FILENAMES = {
    "dockerfile": "dockerfile",
    "gemfile": "ruby",
    "makefile": "makefile",
    "rakefile": "ruby",
}


def infer_source_language(url: str, content_type: str = "") -> str:
    filename = unquote(PurePosixPath(urlsplit(url).path).name)
    special = _SPECIAL_FILENAMES.get(filename.casefold())
    if special:
        return special
    suffix = PurePosixPath(filename).suffix.casefold()
    if suffix in _EXTENSION_LANGUAGES:
        return _EXTENSION_LANGUAGES[suffix]
    media_type = content_type.partition(";")[0].strip().casefold()
    return _CONTENT_TYPE_LANGUAGES.get(media_type, "text")


def wrap_github_source(
    content: str,
    url: str,
    content_type: str = "",
) -> GitHubExtraction | None:
    """Wrap fetched raw/blob source as bounded, valid Markdown for the crawler."""
    content = content.replace("\x00", "").strip("\ufeff")
    if not content.strip():
        return None
    filename = unquote(PurePosixPath(urlsplit(url).path).name) or "source"
    language = infer_source_language(url, content_type)
    if language in {"markdown", "mdx"}:
        text = _cap_markdown(content, MAX_SOURCE_CHARS)
    else:
        longest_run = max((len(run) for run in re.findall(r"`+", content)), default=0)
        fence = "`" * max(3, longest_run + 1)
        header = f"# {filename}\n\n{fence}{language}\n"
        footer = f"\n{fence}"
        available = max(0, MAX_SOURCE_CHARS - len(header) - len(footer))
        text = header + content[:available] + footer
    return GitHubExtraction(
        text=text,
        title=filename,
        strategy="github-source",
        language=language,
    )

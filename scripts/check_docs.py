#!/usr/bin/env python3
"""Validate first-party Markdown structure and repository-local links."""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]

EXCLUDED_PREFIXES = (
    Path("bench/artifacts"),
    Path("bench/results"),
    Path("native/vendor"),
)
EXCLUDED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
}

MARKDOWN_LINK_RE = re.compile(
    r"!?\[[^\]]*]\(\s*(?P<target><[^>]+>|[^)\s]+)",
)
REFERENCE_TARGET_RE = re.compile(
    r"^\s{0,3}\[[^\]]+]:\s*(?P<target><[^>]+>|[^\s]+)",
)
HTML_TARGET_RE = re.compile(
    r"\b(?:href|src)=[\"'](?P<target>[^\"']+)[\"']",
    re.IGNORECASE,
)
HEADING_RE = re.compile(r"^ {0,3}(?P<marks>#{1,6})[ \t]+(?P<text>.+?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s{0,3}(?P<fence>`{3,}|~{3,})")
SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
INLINE_LINK_RE = re.compile(r"!?\[([^\]]*)]\([^)]*\)")
INLINE_HTML_RE = re.compile(r"<[^>]+>")
PUNCTUATION_RE = re.compile(r"[^\w\s-]", re.UNICODE)


def _is_excluded(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    return any(
        relative == prefix or prefix in relative.parents for prefix in EXCLUDED_PREFIXES
    )


def _markdown_files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*.md")
        if not EXCLUDED_PARTS.intersection(path.parts) and not _is_excluded(path)
    )


def _outside_fences(text: str) -> list[tuple[int, str]]:
    visible: list[tuple[int, str]] = []
    active_char = ""
    active_length = 0

    for line_number, line in enumerate(text.splitlines(), start=1):
        match = FENCE_RE.match(line)
        if match:
            fence = match.group("fence")
            if not active_char:
                active_char = fence[0]
                active_length = len(fence)
            elif fence[0] == active_char and len(fence) >= active_length:
                active_char = ""
                active_length = 0
            continue
        if not active_char:
            visible.append((line_number, line))

    return visible


def _slug_base(heading: str) -> str:
    value = INLINE_LINK_RE.sub(r"\1", heading)
    value = INLINE_HTML_RE.sub("", value)
    value = value.replace("`", "").replace("_", "")
    value = PUNCTUATION_RE.sub("", value.lower().strip())
    return re.sub(r"\s", "-", value)


def _anchors(path: Path) -> set[str]:
    counts: defaultdict[str, int] = defaultdict(int)
    anchors: set[str] = set()
    text = path.read_text(encoding="utf-8")

    for _line_number, line in _outside_fences(text):
        match = HEADING_RE.match(line)
        if match is None:
            continue
        base = _slug_base(match.group("text"))
        suffix = counts[base]
        counts[base] += 1
        anchors.add(base if suffix == 0 else f"{base}-{suffix}")

        for explicit in re.findall(r"\bid=[\"']([^\"']+)[\"']", line):
            anchors.add(explicit)

    return anchors


def _targets(line: str) -> list[str]:
    targets = [match.group("target") for match in MARKDOWN_LINK_RE.finditer(line)]
    reference = REFERENCE_TARGET_RE.match(line)
    if reference is not None:
        targets.append(reference.group("target"))
    targets.extend(match.group("target") for match in HTML_TARGET_RE.finditer(line))
    return targets


def _validate_target(source: Path, target: str) -> str | None:
    target = target.removeprefix("<").removesuffix(">")
    if not target:
        return None
    if not target.startswith("#") and (
        target.startswith("//") or SCHEME_RE.match(target)
    ):
        return None

    path_text, separator, fragment = target.partition("#")
    if path_text.startswith("/"):
        return f"repository-local link must be relative: {target}"

    destination = source if not path_text else (source.parent / unquote(path_text)).resolve()
    try:
        destination.relative_to(ROOT)
    except ValueError:
        return f"link escapes the repository: {target}"

    if not destination.exists():
        return f"missing local target: {target}"

    if separator and fragment and destination.suffix.lower() == ".md":
        decoded_fragment = unquote(fragment)
        if decoded_fragment not in _anchors(destination):
            return f"missing Markdown anchor: {target}"

    return None


def main() -> int:
    errors: list[str] = []
    files = _markdown_files()

    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            errors.append(f"{path.relative_to(ROOT)}: invalid UTF-8: {exc}")
            continue

        if not text.endswith("\n"):
            errors.append(f"{path.relative_to(ROOT)}: missing final newline")
        if text.endswith("\n\n"):
            errors.append(f"{path.relative_to(ROOT)}: extra blank line at EOF")

        for line_number, line in _outside_fences(text):
            if line.endswith((" ", "\t")):
                errors.append(
                    f"{path.relative_to(ROOT)}:{line_number}: trailing whitespace",
                )
            for target in _targets(line):
                problem = _validate_target(path, target)
                if problem is not None:
                    errors.append(
                        f"{path.relative_to(ROOT)}:{line_number}: {problem}",
                    )

    if errors:
        print("Documentation validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(f"Documentation validation passed: {len(files)} Markdown files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

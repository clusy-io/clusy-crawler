"""Shared fail-closed inventory for vendored native benchmark provenance."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_IGNORED_BUILD_DIRECTORIES = frozenset(
    {
        "target",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)
_VENDOR_RUNTIME_TEXT_SUFFIXES = frozenset(
    {
        ".css",
        ".html",
        ".js",
        ".json",
        ".lock",
        ".py",
        ".pyi",
        ".ron",
        ".rs",
        ".toml",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)


class SourceInventoryError(RuntimeError):
    """Git could not produce a complete, safe native-source inventory."""


def _git_visible_files(root: Path, pathspec: str) -> tuple[Path, ...]:
    """Return tracked and non-ignored untracked files under ``pathspec``.

    Cargo build products are excluded by the repository's ignore rules.  The
    explicit directory filter is defense in depth for a mistakenly tracked
    target/cache tree.
    """

    resolved_root = root.resolve()
    command = [
        "git",
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        pathspec,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=resolved_root,
            check=False,
            capture_output=True,
        )
    except OSError as error:
        raise SourceInventoryError(f"could not execute git source inventory: {error}") from error
    if result.returncode != 0:
        detail = os.fsdecode(result.stderr or result.stdout).strip()
        raise SourceInventoryError(
            f"git source inventory failed ({result.returncode}): {detail or 'no detail'}"
        )

    paths: set[Path] = set()
    for encoded_relative in result.stdout.split(b"\0"):
        if not encoded_relative:
            continue
        relative = Path(os.fsdecode(encoded_relative))
        if relative.is_absolute() or ".." in relative.parts:
            raise SourceInventoryError(f"git returned unsafe source path: {relative}")
        if any(part in _IGNORED_BUILD_DIRECTORIES for part in relative.parts):
            continue
        path = resolved_root / relative
        if not path.is_file():
            raise SourceInventoryError(f"git-listed source file is missing: {relative}")
        paths.add(path)
    return tuple(sorted(paths, key=lambda path: path.relative_to(resolved_root).as_posix()))


def git_visible_vendor_files(root: Path) -> tuple[Path, ...]:
    """Return auditable vendored native files without ignored build products."""

    return _git_visible_files(root, "native/vendor")


def git_visible_vendor_runtime_text_files(root: Path) -> tuple[Path, ...]:
    """Return vendored source/config/data files suitable for static token scans."""

    return tuple(
        path
        for path in git_visible_vendor_files(root)
        if path.suffix.lower() in _VENDOR_RUNTIME_TEXT_SUFFIXES
    )

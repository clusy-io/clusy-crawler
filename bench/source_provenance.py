"""Shared fail-closed native-source inventory and benchmark provenance."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

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
_NATIVE_INVENTORY_RELATIVE = Path("native/source-inventory-v1.txt")
_NATIVE_INVENTORY_HEADER = "clusy-native-source-inventory-v1"
_NATIVE_DIGEST_DOMAIN = b"clusy-native-source-digest-v1\0"
_SHA256 = re.compile(r"[0-9a-f]{64}")


class SourceInventoryError(RuntimeError):
    """A complete, safe native-source inventory could not be produced."""


def native_source_patterns(root: Path) -> tuple[str, ...]:
    """Load the canonical build-input patterns shared with ``native/build.rs``."""

    inventory = root.resolve() / _NATIVE_INVENTORY_RELATIVE
    try:
        lines = inventory.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise SourceInventoryError(f"could not read native source inventory: {error}") from error
    if not lines:
        raise SourceInventoryError("native source inventory is empty")
    if lines[0] != _NATIVE_INVENTORY_HEADER:
        raise SourceInventoryError("native source inventory has an unsupported header")

    patterns: list[str] = []
    for line_number, raw_pattern in enumerate(lines[1:], start=2):
        pattern = raw_pattern.strip()
        if not pattern or pattern.startswith("#"):
            continue
        pure_path = PurePosixPath(pattern)
        if (
            pure_path.is_absolute()
            or ".." in pure_path.parts
            or "\\" in pattern
            or "\0" in pattern
        ):
            raise SourceInventoryError(
                f"native source inventory line {line_number} has an unsafe pattern: {pattern}"
            )
        if pattern in patterns:
            raise SourceInventoryError(
                f"native source inventory line {line_number} repeats pattern: {pattern}"
            )
        patterns.append(pattern)
    if not patterns:
        raise SourceInventoryError("native source inventory contains no source patterns")
    return tuple(patterns)


def native_source_files(root: Path) -> tuple[Path, ...]:
    """Resolve the deterministic native build-input inventory without Git."""

    native_root = root.resolve() / "native"
    files: dict[str, Path] = {}
    for pattern in native_source_patterns(root):
        try:
            matches = tuple(native_root.glob(pattern))
        except (OSError, ValueError) as error:
            raise SourceInventoryError(
                f"could not evaluate native source pattern {pattern}: {error}"
            ) from error
        if not matches:
            raise SourceInventoryError(
                f"native source inventory pattern matched no files: {pattern}"
            )
        for path in matches:
            cursor = path
            while cursor != native_root:
                if cursor.is_symlink():
                    raise SourceInventoryError(
                        f"native source inventory contains a symbolic link: {cursor}"
                    )
                parent = cursor.parent
                if parent == cursor:
                    raise SourceInventoryError(
                        f"native source escaped package root: {path}"
                    )
                cursor = parent
            if not path.is_file():
                raise SourceInventoryError(
                    f"native source inventory entry is not a regular file: {path}"
                )
            try:
                relative = path.relative_to(native_root).as_posix()
            except ValueError as error:
                raise SourceInventoryError(
                    f"native source escaped package root: {path}"
                ) from error
            if relative in files:
                raise SourceInventoryError(
                    f"native source file is matched by more than one pattern: {relative}"
                )
            files[relative] = path
    if not files:
        raise SourceInventoryError("native source inventory resolved to no files")
    return tuple(files[relative] for relative in sorted(files))


def _update_digest_frame(hasher: Any, value: bytes) -> None:
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _digest_native_source_files(root: Path, files: tuple[Path, ...]) -> str:
    native_root = root.resolve() / "native"
    hasher = hashlib.sha256()
    hasher.update(_NATIVE_DIGEST_DOMAIN)
    hasher.update(len(files).to_bytes(8, "big"))
    for path in files:
        relative = path.relative_to(native_root).as_posix().encode("utf-8")
        try:
            data = path.read_bytes()
        except OSError as error:
            raise SourceInventoryError(f"could not read native source {path}: {error}") from error
        _update_digest_frame(hasher, relative)
        _update_digest_frame(hasher, data)
    return hasher.hexdigest()


def native_source_digest(root: Path) -> str:
    """Hash canonical relative paths and exact bytes for every native build input."""

    return _digest_native_source_files(root, native_source_files(root))


def verify_loaded_native_source_binding(
    root: Path,
    *,
    packaged_digest: str | None = None,
) -> dict[str, object]:
    """Reject a loaded extension that was built from a different source tree."""

    files = native_source_files(root)
    current_digest = _digest_native_source_files(root, files)
    if packaged_digest is None:
        try:
            from clusy_native import packaged_source_digest
        except (ImportError, RuntimeError) as error:
            raise SourceInventoryError(
                "could not load native source digest from extension: "
                f"{type(error).__name__}: {error}"
            ) from error
        if not callable(packaged_source_digest):
            raise SourceInventoryError(
                "loaded native extension does not expose packaged_source_digest"
            )
        try:
            packaged_digest = packaged_source_digest()
        except (RuntimeError, TypeError) as error:
            raise SourceInventoryError(
                f"loaded native extension could not report its source digest: {error}"
            ) from error

    if not isinstance(packaged_digest, str) or not _SHA256.fullmatch(packaged_digest):
        raise SourceInventoryError("loaded native extension reported an invalid source digest")
    if packaged_digest != current_digest:
        raise SourceInventoryError(
            "loaded native source digest mismatch: "
            f"packaged={packaged_digest}, current={current_digest}"
        )
    return {
        "schema": _NATIVE_INVENTORY_HEADER,
        "inventory": _NATIVE_INVENTORY_RELATIVE.as_posix(),
        "files": len(files),
        "packaged_sha256": packaged_digest,
        "current_sha256": current_digest,
        "matched": True,
    }


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

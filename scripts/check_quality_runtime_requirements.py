"""Verify the reviewed dependency surface of the CPU-only quality image."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

_REQUIREMENT = re.compile(r"^([A-Za-z0-9_.-]+)==")
_CANONICAL_SEPARATOR = re.compile(r"[-_.]+")
_FORBIDDEN_EXACT = frozenset({"accelerate", "triton"})
_FORBIDDEN_PREFIXES = ("cuda-", "nvidia-", "pytest", "torch")
_MAX_REQUIREMENTS_BYTES = 128 * 1024


def _canonical_name(value: str) -> str:
    return _CANONICAL_SEPARATOR.sub("-", value).lower()


def _read_manifest(path: Path) -> tuple[str, ...]:
    values = tuple(
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    if values != tuple(sorted(set(values))):
        raise ValueError("quality package manifest must be unique and sorted")
    if any(value != _canonical_name(value) for value in values):
        raise ValueError("quality package manifest contains a noncanonical name")
    return values


def _read_export(path: Path) -> tuple[str, ...]:
    encoded = path.read_bytes()
    if len(encoded) > _MAX_REQUIREMENTS_BYTES:
        raise ValueError("quality requirements export exceeds the 128 KiB budget")
    text = encoded.decode("utf-8")
    if "git+" in text:
        raise ValueError("quality requirements export contains a VCS dependency")
    names = {
        _canonical_name(match.group(1))
        for line in text.splitlines()
        if (match := _REQUIREMENT.match(line)) is not None
    }
    return tuple(sorted(names))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()

    actual = _read_export(args.requirements)
    expected = _read_manifest(args.manifest)
    forbidden = tuple(
        name
        for name in actual
        if name in _FORBIDDEN_EXACT or name.startswith(_FORBIDDEN_PREFIXES)
    )
    if forbidden:
        raise ValueError(f"quality runtime contains forbidden packages: {forbidden!r}")
    if actual != expected:
        added = tuple(sorted(set(actual) - set(expected)))
        removed = tuple(sorted(set(expected) - set(actual)))
        raise ValueError(
            f"quality package manifest drifted; added={added!r}, removed={removed!r}"
        )
    print(f"quality runtime dependency surface verified: {len(actual)} packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

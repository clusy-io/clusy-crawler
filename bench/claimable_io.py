"""TOCTOU-resistant file primitives for claimable benchmark workers.

The claim protocol never validates a pathname and later reopens it. Inputs are
read from one ``O_NOFOLLOW`` descriptor, verified before and after the read,
and copied into a private content-addressed snapshot directory. Outputs are
created with ``O_EXCL`` and published with a no-replace hard link.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path


class ClaimableIOError(RuntimeError):
    """A file could not be handled with claimable integrity semantics."""


@dataclass(frozen=True, slots=True)
class VerifiedFile:
    path: Path
    sha256: str
    bytes: int
    device: int
    inode: int


def _nofollow_flag() -> int:
    value = getattr(os, "O_NOFOLLOW", None)
    if type(value) is not int:
        raise ClaimableIOError("O_NOFOLLOW is unavailable on this platform")
    return value


def _absolute_without_resolution(path: Path) -> Path:
    return path if path.is_absolute() else Path.cwd() / path


def _directory_flags() -> int:
    directory = getattr(os, "O_DIRECTORY", None)
    if type(directory) is not int:
        raise ClaimableIOError("O_DIRECTORY is unavailable on this platform")
    return os.O_RDONLY | directory | _nofollow_flag()


def _validated_absolute(path: Path) -> Path:
    absolute = _absolute_without_resolution(path)
    if (
        not absolute.is_absolute()
        or not absolute.name
        or any(component in {"", ".", ".."} for component in absolute.parts[1:])
    ):
        raise ClaimableIOError(f"path is not lexically canonical: {absolute}")
    return absolute


def _open_directory(path: Path, *, create: bool = False) -> int:
    """Walk a directory one no-follow descriptor at a time."""

    absolute = _absolute_without_resolution(path)
    if (
        not absolute.is_absolute()
        or any(component in {"", ".", ".."} for component in absolute.parts[1:])
    ):
        raise ClaimableIOError(f"directory path is not lexically canonical: {absolute}")
    flags = _directory_flags()
    try:
        descriptor = os.open(absolute.anchor, flags)
    except OSError as error:
        raise ClaimableIOError(
            f"unable to open directory anchor: {absolute.anchor}"
        ) from error
    try:
        for component in absolute.parts[1:]:
            try:
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                with suppress(FileExistsError):
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
            metadata = os.fstat(next_descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(next_descriptor)
                raise ClaimableIOError(
                    f"path component is not a directory: {component}"
                )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except OSError as error:
        os.close(descriptor)
        raise ClaimableIOError(
            f"unable to open directory without following links: {absolute}"
        ) from error
    except BaseException:
        os.close(descriptor)
        raise


def _open_parent(path: Path) -> tuple[int, str, Path]:
    absolute = _validated_absolute(path)
    return _open_directory(absolute.parent), absolute.name, absolute


def _stable_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_regular_descriptor(
    descriptor: int,
    *,
    display_path: Path,
    maximum_bytes: int,
    expected_sha256: str | None,
) -> tuple[bytes, VerifiedFile]:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise ClaimableIOError(f"input is not a regular file: {display_path}")
    if before.st_nlink != 1:
        raise ClaimableIOError(
            f"input must have exactly one hard link: {display_path}"
        )
    if before.st_size > maximum_bytes:
        raise ClaimableIOError(f"input exceeds its byte budget: {display_path}")
    chunks: list[bytes] = []
    consumed = 0
    digest = hashlib.sha256()
    while True:
        chunk = os.read(
            descriptor,
            min(1024 * 1024, maximum_bytes + 1 - consumed),
        )
        if not chunk:
            break
        consumed += len(chunk)
        if consumed > maximum_bytes:
            raise ClaimableIOError(f"input exceeds its byte budget: {display_path}")
        digest.update(chunk)
        chunks.append(chunk)
    after = os.fstat(descriptor)
    if _stable_identity(before) != _stable_identity(after) or consumed != after.st_size:
        raise ClaimableIOError(f"input changed while it was read: {display_path}")
    observed_sha256 = digest.hexdigest()
    if expected_sha256 is not None and observed_sha256 != expected_sha256:
        raise ClaimableIOError(f"input SHA-256 mismatch: {display_path}")
    return b"".join(chunks), VerifiedFile(
        path=display_path,
        sha256=observed_sha256,
        bytes=consumed,
        device=after.st_dev,
        inode=after.st_ino,
    )


def read_verified_bytes(
    path: Path,
    *,
    maximum_bytes: int,
    expected_sha256: str | None = None,
) -> tuple[bytes, VerifiedFile]:
    """Read one regular file from one stable, non-symlink descriptor."""

    if type(maximum_bytes) is not int or maximum_bytes < 0:
        raise ClaimableIOError("maximum_bytes must be a nonnegative integer")
    parent_descriptor, name, absolute = _open_parent(path)
    flags = os.O_RDONLY | _nofollow_flag()
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as error:
        raise ClaimableIOError(f"unable to open input: {absolute}") from error
    finally:
        os.close(parent_descriptor)
    try:
        return _read_regular_descriptor(
            descriptor,
            display_path=absolute,
            maximum_bytes=maximum_bytes,
            expected_sha256=expected_sha256,
        )
    except OSError as error:
        raise ClaimableIOError(f"unable to read input: {absolute}") from error
    finally:
        os.close(descriptor)


def _write_new_file_at(
    parent_descriptor: int,
    *,
    name: str,
    display_path: Path,
    content: bytes,
    mode: int,
) -> VerifiedFile:
    temporary = f".claim-tmp-{secrets.token_hex(16)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _nofollow_flag()
    descriptor = os.open(temporary, flags, 0o600, dir_fd=parent_descriptor)
    published = False
    complete = False
    temporary_exists = True
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ClaimableIOError("output write made no progress")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as error:
            raise ClaimableIOError(
                f"refusing to replace existing output: {display_path}"
            ) from error
        published = True
        os.unlink(temporary, dir_fd=parent_descriptor)
        temporary_exists = False
        os.fsync(parent_descriptor)
        verification_descriptor = os.open(
            name,
            os.O_RDONLY | _nofollow_flag(),
            dir_fd=parent_descriptor,
        )
        try:
            _, metadata = _read_regular_descriptor(
                verification_descriptor,
                display_path=display_path,
                maximum_bytes=len(content),
                expected_sha256=hashlib.sha256(content).hexdigest(),
            )
        finally:
            os.close(verification_descriptor)
        complete = True
        return metadata
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_exists:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=parent_descriptor)
        if published and not complete:
            with suppress(FileNotFoundError):
                os.unlink(name, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)


def write_new_file(path: Path, content: bytes, *, mode: int = 0o400) -> VerifiedFile:
    """Publish a new file without following or replacing any existing path."""

    if type(content) is not bytes:
        raise TypeError("content must be exact bytes")
    if type(mode) is not int or mode < 0 or mode > 0o777:
        raise ClaimableIOError("mode must be an exact permission bit mask")
    parent_descriptor, name, absolute = _open_parent(path)
    try:
        return _write_new_file_at(
            parent_descriptor,
            name=name,
            display_path=absolute,
            content=content,
            mode=mode,
        )
    except OSError as error:
        raise ClaimableIOError(f"unable to publish output: {absolute}") from error
    finally:
        os.close(parent_descriptor)


def snapshot_file(
    source: Path,
    snapshot_directory: Path,
    *,
    maximum_bytes: int,
    expected_sha256: str | None = None,
) -> VerifiedFile:
    """Copy an input once to an immutable content-addressed pathname."""

    content, source_metadata = read_verified_bytes(
        source,
        maximum_bytes=maximum_bytes,
        expected_sha256=expected_sha256,
    )
    directory = _absolute_without_resolution(snapshot_directory)
    directory_descriptor = _open_directory(directory, create=True)
    try:
        return _write_new_file_at(
            directory_descriptor,
            name=source_metadata.sha256,
            display_path=directory / source_metadata.sha256,
            content=content,
            mode=0o400,
        )
    except OSError as error:
        raise ClaimableIOError(
            f"unable to publish content-addressed snapshot: {directory}"
        ) from error
    finally:
        os.close(directory_descriptor)

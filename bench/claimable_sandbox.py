"""Enforceable Linux sandbox for claimable baseline and decision workers.

Claimability is unavailable unless a fresh isolated Python interpreter can be
started by bubblewrap in a distinct network namespace with no route or egress.
Worker inputs travel through a pipe; worker/code files travel through sealed
memfds consumed by ``bwrap --file``. The source checkout, benchmark dataset,
evaluator, scorer, and labels are never mounted.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

CLAIMABLE_CONCURRENCY = 4
CLAIMABLE_WALL_SECONDS = 180.0
CLAIMABLE_RUNTIME_ROOT = Path("/opt/clusy-claim-runtime")
CLAIMABLE_PYTHON_VERSION = (3, 12)
CLAIMABLE_RUNTIME_SITE = (
    CLAIMABLE_RUNTIME_ROOT
    / "lib"
    / f"python{CLAIMABLE_PYTHON_VERSION[0]}.{CLAIMABLE_PYTHON_VERSION[1]}"
    / "site-packages"
)
_MAX_WORKER_STDERR_BYTES = 1024 * 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_BASE_ENVIRONMENT = MappingProxyType(
    {
        "ANTHROPIC_API_KEY": "",
        "ELSEVIER_API_KEY": "",
        "ENVIRONMENT": "local",
        "EXA_API_KEY": "",
        "EXTRACT_MAX_TEXT_LENGTH": "500000",
        "EXTRACTION_MERGE_MODE": "union",
        "FIRECRAWL_API_KEY": "",
        "HOME": "/nonexistent",
        "IEEE_API_KEY": "",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "MAX_CONCURRENT_EXTRACTIONS": "1",
        "NATIVE_EXTRACTION_ENABLED": "true",
        "NATIVE_EXTRACTION_MIN_CONFIDENCE": "0.60",
        "OPENAI_API_KEY": "",
        "PARALLEL_EXTRACTION_ENABLED": "false",
        "PATH": "/usr/bin:/bin",
        "PWD": "/capsule",
        "PYTHONNOUSERSITE": "1",
        "QUALITY_EXTRACTION_API_KEY": "",
        "QUALITY_EXTRACTION_BASE_URL": "",
        "QUALITY_EXTRACTION_MODEL": "",
        "TMPDIR": "/tmp",
    }
)
_PROBE_SOURCE = b"""\
import hashlib, json, os, socket, sys
assert sys.flags.isolated == 1
assert sys.flags.no_site == 1
assert sys.flags.no_user_site == 1
assert sys.version_info[:2] == (3, 12)
assert "sitecustomize" not in sys.modules
assert "usercustomize" not in sys.modules
parent = int(os.environ["CLUSY_PARENT_NETNS_INODE"])
worker = os.stat("/proc/self/ns/net").st_ino
routes = open("/proc/net/route", "rb").read()
route_lines = routes.decode("ascii", errors="strict").splitlines()[1:]
non_loopback_routes = [
    line for line in route_lines if line.split() and line.split()[0] != "lo"
]
ipv6_routes = open("/proc/net/ipv6_route", "rb").read()
ipv6_route_lines = ipv6_routes.decode("ascii", errors="strict").splitlines()
non_loopback_ipv6_routes = [
    line for line in ipv6_route_lines if line.split() and line.split()[-1] != "lo"
]
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(0.05)
try:
    egress = s.connect_ex(("1.1.1.1", 53))
finally:
    s.close()
s6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
s6.settimeout(0.05)
try:
    egress6 = s6.connect_ex(("2606:4700:4700::1111", 53))
finally:
    s6.close()
mountinfo = open("/proc/self/mountinfo", "rb").read()
result = {
    "egress_connect_ex": egress,
    "ipv6_egress_connect_ex": egress6,
    "ipv6_route_table_sha256": hashlib.sha256(ipv6_routes).hexdigest(),
    "environment": dict(sorted(os.environ.items())),
    "mountinfo_sha256": hashlib.sha256(mountinfo).hexdigest(),
    "net_namespace_distinct": worker != parent,
    "non_loopback_route_rows": len(non_loopback_routes),
    "non_loopback_ipv6_route_rows": len(non_loopback_ipv6_routes),
    "parent_net_namespace_inode": parent,
    "route_table_sha256": hashlib.sha256(routes).hexdigest(),
    "worker_net_namespace_inode": worker,
}
sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")))
"""


class SandboxUnavailableError(RuntimeError):
    """The host cannot provide the mandatory claimable isolation."""


class SandboxExecutionError(RuntimeError):
    """A sandboxed worker did not complete canonically."""


@dataclass(frozen=True, slots=True)
class SandboxObservation:
    available: bool
    reason: str
    platform: str
    bubblewrap_path: str | None
    bubblewrap_sha256: str | None
    env_path: str | None
    env_sha256: str | None
    python_path: str | None
    python_sha256: str | None
    network_probe: Mapping[str, object] | None


@dataclass(frozen=True, slots=True)
class WorkerExecution:
    stdout: bytes
    stderr: bytes
    wall_seconds: float
    launcher: SandboxObservation
    capsule_sha256: Mapping[str, str]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SandboxUnavailableError(
                f"runtime dependency is not regular: {path}"
            )
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise SandboxUnavailableError(f"runtime dependency changed: {path}")
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _runtime_paths() -> tuple[Path, Path, Path, Path]:
    if platform.system() != "Linux":
        raise SandboxUnavailableError("claimable sandbox requires Linux")
    env_path = shutil.which("env", path="/usr/bin:/bin")
    bwrap_path = shutil.which("bwrap", path="/usr/bin:/bin")
    runtime_bin = CLAIMABLE_RUNTIME_ROOT / "bin"
    python_candidate = runtime_bin / "python3"
    runtime_directories = (
        CLAIMABLE_RUNTIME_ROOT,
        runtime_bin,
        CLAIMABLE_RUNTIME_ROOT / "lib",
        CLAIMABLE_RUNTIME_SITE.parent,
        CLAIMABLE_RUNTIME_SITE,
    )
    try:
        directory_metadata = tuple(os.lstat(path) for path in runtime_directories)
        python_metadata = os.lstat(python_candidate)
    except OSError:
        directory_metadata = ()
        python_metadata = None
    copied_python = (
        len(directory_metadata) == len(runtime_directories)
        and all(
            stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)
            for metadata in directory_metadata
        )
        and python_metadata is not None
        and stat.S_ISREG(python_metadata.st_mode)
        and not stat.S_ISLNK(python_metadata.st_mode)
        and python_metadata.st_nlink == 1
    )
    python_path = str(python_candidate) if copied_python else None
    if not env_path or not bwrap_path or not python_path:
        raise SandboxUnavailableError(
            "env, bwrap, copied CPython 3.12, and its fixed site-packages "
            "directory are mandatory"
        )
    paths = [Path(value) for value in (env_path, bwrap_path, python_path)]
    for index, path in enumerate(paths):
        if path.is_symlink():
            if index == 2:
                raise SandboxUnavailableError(
                    "claimable runtime Python must be a copied regular executable"
                )
            resolved = path.resolve(strict=True)
            if not resolved.is_file():
                raise SandboxUnavailableError(f"runtime symlink is invalid: {path}")
            paths[index] = resolved
    return paths[0], paths[1], paths[2], CLAIMABLE_RUNTIME_SITE


def _sealed_memfd(name: str, content: bytes) -> int:
    if not hasattr(os, "memfd_create"):
        raise SandboxUnavailableError("sealed memfd input is unavailable")
    descriptor = int(
        os.memfd_create(
            name,
            flags=getattr(os, "MFD_CLOEXEC", 0)
            | getattr(os, "MFD_ALLOW_SEALING", 0),
        )
    )
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise SandboxUnavailableError("memfd write made no progress")
            view = view[written:]
        os.lseek(descriptor, 0, os.SEEK_SET)
        def fcntl_constant(constant_name: str) -> int:
            value = getattr(fcntl, constant_name, None)
            if type(value) is not int:
                raise SandboxUnavailableError(
                    f"mandatory Linux fcntl constant is absent: {constant_name}"
                )
            return value

        seals = (
            fcntl_constant("F_SEAL_SEAL")
            | fcntl_constant("F_SEAL_SHRINK")
            | fcntl_constant("F_SEAL_GROW")
            | fcntl_constant("F_SEAL_WRITE")
        )
        add_seals = fcntl_constant("F_ADD_SEALS")
        get_seals = fcntl_constant("F_GET_SEALS")
        fcntl.fcntl(descriptor, add_seals, seals)
        if fcntl.fcntl(descriptor, get_seals) != seals:
            raise SandboxUnavailableError("memfd sealing was not enforced")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _canonical_capsule_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or value != path.as_posix()
    ):
        raise SandboxExecutionError(f"invalid capsule path: {value!r}")
    return path.as_posix()


def _system_mount_arguments() -> list[str]:
    arguments: list[str] = []
    for value in ("/usr", "/bin", "/lib", "/lib64"):
        path = Path(value)
        if path.exists():
            arguments.extend(["--ro-bind", value, value])
    if CLAIMABLE_RUNTIME_ROOT.is_dir():
        runtime = str(CLAIMABLE_RUNTIME_ROOT)
        arguments.extend(["--ro-bind", runtime, runtime])
    return arguments


def _build_command(
    *,
    env_path: Path,
    bwrap_path: Path,
    python_path: Path,
    capsule_descriptors: Mapping[str, int],
    parent_net_inode: int,
) -> list[str]:
    directories = {"/capsule", "/tmp"}
    for relative in capsule_descriptors:
        parent = PurePosixPath("/capsule", relative).parent
        while parent != PurePosixPath("/"):
            directories.add(parent.as_posix())
            parent = parent.parent
    command = [
        str(env_path),
        "-i",
        "PATH=/usr/bin:/bin",
        str(bwrap_path),
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--unshare-net",
        "--clearenv",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        *_system_mount_arguments(),
    ]
    for name, value in sorted(
        {
            **_BASE_ENVIRONMENT,
            "CLUSY_PARENT_NETNS_INODE": str(parent_net_inode),
        }.items()
    ):
        command.extend(["--setenv", name, value])
    for directory in sorted(directories, key=lambda item: (item.count("/"), item)):
        if directory != "/tmp":
            command.extend(["--dir", directory])
    for relative, descriptor in sorted(capsule_descriptors.items()):
        command.extend(["--file", str(descriptor), f"/capsule/{relative}"])
    command.extend(
        [
            "--remount-ro",
            "/capsule",
            "--chdir",
            "/capsule",
            str(python_path),
            "-I",
            "-S",
            "-B",
            "/capsule/worker.py",
        ]
    )
    return command


def _run_once(
    capsule: Mapping[str, bytes],
    *,
    stdin: bytes,
    timeout_seconds: float,
) -> tuple[subprocess.CompletedProcess[bytes], Mapping[str, str], tuple[Path, Path, Path]]:
    env_path, bwrap_path, python_path, _ = _runtime_paths()
    parent_net_inode = os.stat("/proc/self/ns/net").st_ino
    normalized = {
        _canonical_capsule_path(name): content for name, content in capsule.items()
    }
    if "worker.py" not in normalized or any(
        type(value) is not bytes for value in normalized.values()
    ):
        raise SandboxExecutionError("capsule requires exact worker.py bytes")
    descriptors: dict[str, int] = {}
    try:
        for name, content in normalized.items():
            descriptors[name] = _sealed_memfd(name.replace("/", "_"), content)
        command = _build_command(
            env_path=env_path,
            bwrap_path=bwrap_path,
            python_path=python_path,
            capsule_descriptors=descriptors,
            parent_net_inode=parent_net_inode,
        )
        completed = subprocess.run(
            command,
            input=stdin,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
            env={},
            pass_fds=tuple(descriptors.values()),
        )
    except subprocess.TimeoutExpired as error:
        raise SandboxExecutionError(
            f"claimable worker exceeded its fixed {timeout_seconds:g} s sandbox limit"
        ) from error
    finally:
        for descriptor in descriptors.values():
            os.close(descriptor)
    hashes = MappingProxyType(
        {
            name: hashlib.sha256(content).hexdigest()
            for name, content in sorted(normalized.items())
        }
    )
    return completed, hashes, (env_path, bwrap_path, python_path)


def probe_sandbox() -> SandboxObservation:
    """Run an actual namespace/no-egress proof; never infer availability."""

    try:
        completed, _, paths = _run_once(
            {"worker.py": _PROBE_SOURCE},
            stdin=b"",
            timeout_seconds=min(CLAIMABLE_WALL_SECONDS, 10.0),
        )
        if completed.returncode != 0:
            detail = completed.stderr[:_MAX_WORKER_STDERR_BYTES].decode(
                "utf-8",
                errors="replace",
            )
            raise SandboxUnavailableError(f"bubblewrap probe failed: {detail}")
        try:
            evidence = json.loads(completed.stdout)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise SandboxUnavailableError(
                "bubblewrap probe emitted invalid evidence"
            ) from error
        expected_environment = {
            **_BASE_ENVIRONMENT,
            "CLUSY_PARENT_NETNS_INODE": str(os.stat("/proc/self/ns/net").st_ino),
        }
        expected_keys = {
            "egress_connect_ex",
            "environment",
            "ipv6_egress_connect_ex",
            "ipv6_route_table_sha256",
            "mountinfo_sha256",
            "net_namespace_distinct",
            "non_loopback_ipv6_route_rows",
            "non_loopback_route_rows",
            "parent_net_namespace_inode",
            "route_table_sha256",
            "worker_net_namespace_inode",
        }
        if (
            not isinstance(evidence, dict)
            or set(evidence) != expected_keys
            or evidence.get("net_namespace_distinct") is not True
            or evidence.get("non_loopback_route_rows") != 0
            or evidence.get("non_loopback_ipv6_route_rows") != 0
            or type(evidence.get("egress_connect_ex")) is not int
            or evidence["egress_connect_ex"] == 0
            or type(evidence.get("ipv6_egress_connect_ex")) is not int
            or evidence["ipv6_egress_connect_ex"] == 0
            or type(evidence.get("parent_net_namespace_inode")) is not int
            or type(evidence.get("worker_net_namespace_inode")) is not int
            or evidence["parent_net_namespace_inode"]
            == evidence["worker_net_namespace_inode"]
            or evidence.get("environment") != dict(sorted(expected_environment.items()))
            or not _SHA256_RE.fullmatch(str(evidence.get("mountinfo_sha256", "")))
            or not _SHA256_RE.fullmatch(
                str(evidence.get("ipv6_route_table_sha256", ""))
            )
            or not _SHA256_RE.fullmatch(str(evidence.get("route_table_sha256", "")))
        ):
            raise SandboxUnavailableError(
                "namespace, environment, or no-egress proof failed"
            )
        env_path, bwrap_path, python_path = paths
        return SandboxObservation(
            available=True,
            reason="observed_distinct_netns_and_failed_egress",
            platform=platform.platform(),
            bubblewrap_path=str(bwrap_path),
            bubblewrap_sha256=_sha256_file(bwrap_path),
            env_path=str(env_path),
            env_sha256=_sha256_file(env_path),
            python_path=str(python_path),
            python_sha256=_sha256_file(python_path),
            network_probe=MappingProxyType(evidence),
        )
    except (OSError, SandboxUnavailableError, SandboxExecutionError) as error:
        return SandboxObservation(
            available=False,
            reason=str(error),
            platform=platform.platform(),
            bubblewrap_path=None,
            bubblewrap_sha256=None,
            env_path=None,
            env_sha256=None,
            python_path=None,
            python_sha256=None,
            network_probe=None,
        )


def run_claimable_worker(
    capsule: Mapping[str, bytes],
    *,
    stdin: bytes,
) -> WorkerExecution:
    """Run one fresh worker under the fixed, non-overridable claim protocol."""

    observation = probe_sandbox()
    if not observation.available:
        raise SandboxUnavailableError(observation.reason)
    started = time.monotonic()
    completed, capsule_hashes, paths = _run_once(
        capsule,
        stdin=stdin,
        timeout_seconds=CLAIMABLE_WALL_SECONDS,
    )
    wall_seconds = time.monotonic() - started
    expected_runtime = (
        observation.env_sha256,
        observation.bubblewrap_sha256,
        observation.python_sha256,
    )
    actual_runtime = tuple(_sha256_file(path) for path in paths)
    if actual_runtime != expected_runtime:
        raise SandboxExecutionError(
            "launcher or interpreter bytes changed after the isolation probe"
        )
    stderr = completed.stderr[:_MAX_WORKER_STDERR_BYTES]
    if completed.returncode != 0:
        raise SandboxExecutionError(
            f"claimable worker exited {completed.returncode}: "
            + stderr.decode("utf-8", errors="replace")
        )
    if completed.stderr:
        raise SandboxExecutionError(
            "claimable worker emitted unexpected stderr: "
            + stderr.decode("utf-8", errors="replace")
        )
    return WorkerExecution(
        stdout=completed.stdout,
        stderr=stderr,
        wall_seconds=wall_seconds,
        launcher=observation,
        capsule_sha256=capsule_hashes,
    )

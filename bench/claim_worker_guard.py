"""Bootstrap/runtime attestations shared by isolated claim workers.

This file is copied into the sandbox as ``/capsule/claim_guard.py``. It has no
benchmark, evaluator, dataset, or application imports.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import socket
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import ModuleType

_EXPECTED_ENVIRONMENT = {
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
    "PYTHONHASHSEED": "0",
    "PYTHONNOUSERSITE": "1",
    "QUALITY_EXTRACTION_API_KEY": "",
    "QUALITY_EXTRACTION_BASE_URL": "",
    "QUALITY_EXTRACTION_MODEL": "",
    "TMPDIR": "/tmp",
}
_FORBIDDEN_PREFIXES = (
    "bench",
    "datasets",
    "evaluate",
    "firecrawl",
    "openai",
    "webmainbench",
)


class WorkerGuardError(RuntimeError):
    """Worker runtime evidence is not claimable."""


def _sha256_file(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise WorkerGuardError(f"module origin is not regular: {path}")
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
            raise WorkerGuardError(f"module origin changed while read: {path}")
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def assert_fresh_interpreter() -> dict[str, Any]:
    """Fail before application import unless the interpreter is isolated."""

    if (
        sys.flags.isolated != 1
        or sys.flags.no_site != 1
        or sys.flags.no_user_site != 1
        or sys.dont_write_bytecode is not True
    ):
        raise WorkerGuardError("worker requires python -I -S -B")
    forbidden_loaded = [
        name
        for name in sys.modules
        if name in {"sitecustomize", "usercustomize"}
        or name == "app"
        or name.startswith("app.")
        or name == "clusy_native"
        or name.startswith("clusy_native.")
        or name == "bench"
        or name.startswith("bench.")
    ]
    if forbidden_loaded:
        raise WorkerGuardError(
            "application, benchmark, or customization modules were preloaded"
        )
    parent_value = os.environ.get("CLUSY_PARENT_NETNS_INODE")
    if parent_value is None or not parent_value.isascii() or not parent_value.isdigit():
        raise WorkerGuardError("parent network namespace evidence is absent")
    expected_environment = {
        **_EXPECTED_ENVIRONMENT,
        "CLUSY_PARENT_NETNS_INODE": parent_value,
    }
    if dict(os.environ) != expected_environment:
        raise WorkerGuardError("worker environment is not the exact observed whitelist")
    worker_net_inode = os.stat("/proc/self/ns/net").st_ino
    parent_net_inode = int(parent_value)
    if worker_net_inode == parent_net_inode:
        raise WorkerGuardError("worker did not enter a distinct network namespace")
    routes = Path("/proc/net/route").read_bytes()
    route_lines = routes.decode("ascii", errors="strict").splitlines()[1:]
    non_loopback_routes = [
        line for line in route_lines if line.split() and line.split()[0] != "lo"
    ]
    if non_loopback_routes:
        raise WorkerGuardError("worker network namespace has a non-loopback route")
    ipv6_routes = Path("/proc/net/ipv6_route").read_bytes()
    ipv6_route_lines = ipv6_routes.decode("ascii", errors="strict").splitlines()
    non_loopback_ipv6_routes = [
        line
        for line in ipv6_route_lines
        if line.split() and line.split()[-1] != "lo"
    ]
    if non_loopback_ipv6_routes:
        raise WorkerGuardError(
            "worker network namespace has a non-loopback IPv6 route"
        )
    network_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    network_socket.settimeout(0.05)
    try:
        egress_result = network_socket.connect_ex(("1.1.1.1", 53))
    finally:
        network_socket.close()
    if egress_result == 0:
        raise WorkerGuardError("worker unexpectedly established network egress")
    network_socket6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    network_socket6.settimeout(0.05)
    try:
        ipv6_egress_result = network_socket6.connect_ex(
            ("2606:4700:4700::1111", 53)
        )
    finally:
        network_socket6.close()
    if ipv6_egress_result == 0:
        raise WorkerGuardError("worker unexpectedly established IPv6 egress")
    mountinfo = Path("/proc/self/mountinfo").read_bytes()
    return {
        "environment": dict(sorted(os.environ.items())),
        "fresh_interpreter": True,
        "mountinfo_sha256": hashlib.sha256(mountinfo).hexdigest(),
        "network": {
            "egress_connect_ex": egress_result,
            "ipv6_egress_connect_ex": ipv6_egress_result,
            "ipv6_route_table_sha256": hashlib.sha256(ipv6_routes).hexdigest(),
            "non_loopback_ipv6_route_rows": 0,
            "non_loopback_route_rows": 0,
            "parent_namespace_inode": parent_net_inode,
            "route_table_sha256": hashlib.sha256(routes).hexdigest(),
            "worker_namespace_inode": worker_net_inode,
        },
        "python": {
            "executable": sys.executable,
            "executable_sha256": _sha256_file(Path(sys.executable)),
            "flags": {
                "dont_write_bytecode": sys.dont_write_bytecode,
                "isolated": sys.flags.isolated,
                "no_site": sys.flags.no_site,
                "no_user_site": sys.flags.no_user_site,
            },
            "version": sys.version,
        },
        "preloaded_forbidden_modules": [],
    }


def read_stdin_envelope(*, maximum_bytes: int) -> dict[str, Any]:
    content = sys.stdin.buffer.read(maximum_bytes + 1)
    if len(content) > maximum_bytes:
        raise WorkerGuardError("worker input exceeds its fixed byte budget")
    try:
        value = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise WorkerGuardError("worker input is not canonical UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise WorkerGuardError("worker input envelope must be an object")
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if content != canonical:
        raise WorkerGuardError("worker input envelope is not canonical JSON")
    return value


def module_identity(module: ModuleType, *, expected_relative: str) -> dict[str, str]:
    raw_path = getattr(module, "__file__", None)
    if type(raw_path) is not str:
        raise WorkerGuardError(f"module has no exact origin: {module.__name__}")
    path = Path(raw_path)
    expected = Path("/capsule") / expected_relative
    if path != expected or not path.is_file():
        raise WorkerGuardError(f"module escaped capsule: {module.__name__}")
    return {
        "module": module.__name__,
        "path": str(path),
        "sha256": _sha256_file(path),
    }


def assert_import_closure() -> dict[str, dict[str, str]]:
    """Reject benchmark/scorer imports and non-system/non-capsule module origins."""

    for prefix in ("bench", "datasets", "evaluate", "webmainbench"):
        if importlib.util.find_spec(prefix) is not None:
            raise WorkerGuardError(
                f"benchmark/evaluator code is importable in worker: {prefix}"
            )
    origins: dict[str, dict[str, str]] = {}
    for name, module in sorted(sys.modules.items()):
        if any(name == prefix or name.startswith(prefix + ".") for prefix in _FORBIDDEN_PREFIXES):
            raise WorkerGuardError(f"forbidden module is importable in worker: {name}")
        raw_origin = getattr(module, "__file__", None)
        if raw_origin is None:
            continue
        if type(raw_origin) is not str:
            raise WorkerGuardError(f"module origin is not a string: {name}")
        origin = str(Path(raw_origin))
        if not (
            origin.startswith("/capsule/")
            or origin.startswith("/opt/clusy-claim-runtime/")
            or origin.startswith("/usr/lib/")
            or origin.startswith("/usr/local/lib/")
        ):
            raise WorkerGuardError(f"module escaped approved runtime: {name}: {origin}")
        origins[name] = {
            "path": origin,
            "sha256": _sha256_file(Path(origin)),
        }
    return origins


def write_canonical_stdout(value: object) -> None:
    content = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    sys.stdout.buffer.write(content)
    sys.stdout.buffer.flush()

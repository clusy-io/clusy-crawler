"""Fail-closed controller for the label-free AEB Trafilatura 2.1.0 replay."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from bench import aeb_current_oss_worker as worker_contract

PROJECT_ROOT = Path(__file__).resolve().parents[1]
AEB_REPOSITORY = "https://github.com/scrapinghub/article-extraction-benchmark.git"
AEB_COMMIT = "4a3bc979f76c0df73cb95fe272e2fc1b96f9f010"
AEB_TREE = "258fee1bb38bcb642afec48cb80e51bd1594c259"
AEB_PAGES = 181
AEB_RUNNER_PATH = "extractors/run_trafilatura.py"
AEB_RUNNER_GIT_BLOB_OID = "0108f536de74e4fdb28103f3f102d26fd7872127"
AEB_RUNNER_SHA256 = "d8766cc6593e40d8a7204158f8d83c90746a1e63d02a494df5b9adbd6c86faf9"
EXPECTED_PACKAGE = "trafilatura"
EXPECTED_VERSION = "2.1.0"
EXPECTED_WHEEL_FILENAME = "trafilatura-2.1.0-py3-none-any.whl"
EXPECTED_WHEEL_SHA256 = "0eded5207a806445ddebbe36eae30b9035fe6a2f233c36f6fe82663fca8b9d30"
EXPECTED_METADATA_SHA256 = "259c02daaccbde01d9a06ed942eaeb71833ab353228b058ea0b1574525a48ba8"
EXPECTED_WHEEL_METADATA_SHA256 = "69e6228a0d35958183cc1812f07c565ce837b95eb51bd8a33acba9fa4f68d6c9"
EXPECTED_RECORD_SHA256 = "9120037e011653c8b68b828b896ecf90bda30136df2a73578b40f0376c39f5c7"
ENVIRONMENT_SCHEMA = "clusy.aeb.current-oss.environment-manifest.v1"
ENVIRONMENT_MANIFEST_PATH = PROJECT_ROOT / "bench" / "aeb_trafilatura_2_1_0_environment.json"
REQUIREMENTS_LOCK_PATH = PROJECT_ROOT / "bench" / "aeb_trafilatura_2_1_0_requirements.lock"
EXPECTED_ENVIRONMENT_MANIFEST_SHA256 = (
    "fa522352d9e0369dbd1e17794adb09c9e47b9f316f30a6ef6971dc1221eb391f"
)
EXPECTED_REQUIREMENTS_LOCK_SHA256 = (
    "68b1fe778be9ec1d65ed930f1b3e57e15d195cce5b9c4b87fae6df3cb22d9d5f"
)
EXPECTED_WORKER_SHA256 = "12fa9f0cde9b89b7c4a77cd4546ac5b31ae5155339388b90db0f965fc5de2f42"
# Filled from the pinned AEB tree by the implementation's independent
# inventory calculation.  A different value means the fixture bytes or key
# set are not the frozen 181-page capsule, even if a caller forges a manifest.
AEB_HTML_INVENTORY_SHA256 = "1c9833287ef2ee3bf3d9d948dbec300f867316e71815c003640d57e7567a04e9"
INPUT_SCHEMA = worker_contract.INPUT_SCHEMA
RESULT_SCHEMA = worker_contract.RESULT_SCHEMA
CONFIG = {
    "callable": "trafilatura.extract",
    "keyword_arguments": {"include_comments": False},
    "positional_arguments": ["decoded_html"],
}
MAX_WORKER_STDIO_BYTES = 1 * 1024 * 1024
MAX_WORKER_RESULT_BYTES = worker_contract.MAX_RESULT_BYTES
WORKER_TIMEOUT_SECONDS = 600
_HEX = frozenset("0123456789abcdef")
_NORMALIZE_DISTRIBUTION = re.compile(r"[-_.]+")
_REQUIREMENT_PIN = re.compile(
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)=="
    r"(?P<version>[A-Za-z0-9][A-Za-z0-9.!+_-]*)"
)


class CurrentOSSBaselineError(RuntimeError):
    """The current open-source comparison cannot be trusted."""


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _hash_json(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


CONFIG_SHA256 = _hash_json(CONFIG)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _git_blob_oid(value: bytes) -> str:
    prefix = f"blob {len(value)}\0".encode("ascii")
    return hashlib.sha1(prefix + value).hexdigest()


def _sha256_file(path: Path, *, maximum_bytes: int | None = None) -> str:
    digest = hashlib.sha256()
    consumed = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            consumed += len(chunk)
            if maximum_bytes is not None and consumed > maximum_bytes:
                raise CurrentOSSBaselineError(f"file exceeds byte cap: {path.name}")
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX for character in value)
    )


def _require_exact_keys(value: Any, expected: set[str], *, context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise CurrentOSSBaselineError(f"{context} schema mismatch")
    return value


def _regular_file(path: Path, *, context: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise CurrentOSSBaselineError(f"{context} is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise CurrentOSSBaselineError(f"{context} must be a regular non-symlink file")
    return metadata


def _python_executable(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    try:
        metadata = absolute.stat()
    except OSError as error:
        raise CurrentOSSBaselineError("Trafilatura interpreter is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode) or not os.access(absolute, os.X_OK):
        raise CurrentOSSBaselineError("Trafilatura interpreter is not executable")
    return absolute


def _run_git_bytes(root: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CurrentOSSBaselineError("could not inspect pinned AEB Git state") from error
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise CurrentOSSBaselineError(f"AEB Git inspection failed: {message[:500]}")
    return result.stdout


def _run_git(root: Path, *arguments: str) -> str:
    return _run_git_bytes(root, *arguments).decode("utf-8", "strict").strip()


def _parse_ls_tree(raw: bytes) -> dict[str, dict[str, str]]:
    entries: dict[str, dict[str, str]] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            mode, object_type, oid = header.decode("ascii").split(" ", 2)
            path = raw_path.decode("utf-8", "strict")
        except (UnicodeDecodeError, ValueError) as error:
            raise CurrentOSSBaselineError("AEB Git tree record is malformed") from error
        if path in entries:
            raise CurrentOSSBaselineError("AEB Git tree contains a duplicate path")
        entries[path] = {"git_blob_oid": oid, "mode": mode, "type": object_type}
    return entries


def _safe_html_key(path: str) -> str:
    prefix = "html/"
    suffix = ".html.gz"
    if not path.startswith(prefix) or not path.endswith(suffix) or path.count("/") != 1:
        raise CurrentOSSBaselineError("AEB HTML tree contains an unexpected path")
    key = path[len(prefix) : -len(suffix)]
    if (
        not key
        or len(key) > 200
        or key in {".", ".."}
        or "/" in key
        or "\\" in key
        or "\x00" in key
    ):
        raise CurrentOSSBaselineError("AEB HTML key is unsafe")
    return key


def _verify_repository_identity(aeb_root: Path) -> None:
    if not aeb_root.is_dir() or not (aeb_root / ".git").exists():
        raise CurrentOSSBaselineError("AEB input must be a Git checkout")
    if _run_git(aeb_root, "rev-parse", "HEAD") != AEB_COMMIT:
        raise CurrentOSSBaselineError("AEB commit mismatch")
    if _run_git(aeb_root, "rev-parse", "HEAD^{tree}") != AEB_TREE:
        raise CurrentOSSBaselineError("AEB tree mismatch")
    if _run_git(aeb_root, "status", "--porcelain", "--untracked-files=no"):
        raise CurrentOSSBaselineError("AEB tracked worktree is dirty")


def build_html_inventory(aeb_root: Path) -> dict[str, Any]:
    """Bind every label-free AEB HTML byte without reading labels or outputs."""

    _verify_repository_identity(aeb_root)
    entries = _parse_ls_tree(
        _run_git_bytes(aeb_root, "ls-tree", "-rz", "--full-tree", "HEAD", "--", "html")
    )
    if len(entries) != AEB_PAGES:
        raise CurrentOSSBaselineError(
            f"AEB tracked HTML cardinality mismatch: {len(entries)}/{AEB_PAGES}"
        )
    try:
        actual_members = {
            path.relative_to(aeb_root).as_posix() for path in (aeb_root / "html").iterdir()
        }
    except OSError as error:
        raise CurrentOSSBaselineError("AEB HTML directory is unavailable") from error
    if actual_members != set(entries):
        raise CurrentOSSBaselineError("AEB HTML directory has missing or extra members")

    items: list[dict[str, Any]] = []
    for relative in sorted(entries, key=lambda value: _safe_html_key(value).encode("utf-8")):
        entry = entries[relative]
        if entry["mode"] != "100644" or entry["type"] != "blob":
            raise CurrentOSSBaselineError("AEB HTML entry is not a regular tracked blob")
        key = _safe_html_key(relative)
        path = aeb_root / relative
        metadata = _regular_file(path, context="AEB HTML fixture")
        compressed = path.read_bytes()
        if metadata.st_size != len(compressed) or not compressed:
            raise CurrentOSSBaselineError("AEB compressed HTML size mismatch")
        if _git_blob_oid(compressed) != entry["git_blob_oid"]:
            raise CurrentOSSBaselineError("AEB HTML bytes do not match the tracked Git blob")
        try:
            decoded = gzip.decompress(compressed)
            decoded.decode("utf-8", "strict")
        except (gzip.BadGzipFile, EOFError, OSError, UnicodeDecodeError) as error:
            raise CurrentOSSBaselineError("AEB HTML is not strict UTF-8 gzip content") from error
        items.append(
            {
                "compressed_bytes": len(compressed),
                "compressed_sha256": _sha256_bytes(compressed),
                "decoded_bytes": len(decoded),
                "decoded_sha256": _sha256_bytes(decoded),
                "git_blob_oid": entry["git_blob_oid"],
                "key": key,
                "path": relative,
            }
        )
    commitment = _hash_json(items)
    if AEB_HTML_INVENTORY_SHA256 != "TO_BE_COMPUTED" and commitment != AEB_HTML_INVENTORY_SHA256:
        raise CurrentOSSBaselineError("AEB HTML inventory differs from the frozen commitment")

    runner_entries = _parse_ls_tree(
        _run_git_bytes(aeb_root, "ls-tree", "-rz", "--full-tree", "HEAD", "--", AEB_RUNNER_PATH)
    )
    if set(runner_entries) != {AEB_RUNNER_PATH}:
        raise CurrentOSSBaselineError("pinned upstream Trafilatura runner is missing")
    runner_entry = runner_entries[AEB_RUNNER_PATH]
    runner_path = aeb_root / AEB_RUNNER_PATH
    if (
        runner_entry
        != {
            "git_blob_oid": AEB_RUNNER_GIT_BLOB_OID,
            "mode": "100644",
            "type": "blob",
        }
        or _sha256_file(runner_path) != AEB_RUNNER_SHA256
    ):
        raise CurrentOSSBaselineError("upstream Trafilatura runner identity mismatch")
    return {
        "dataset": {
            "commit": AEB_COMMIT,
            "repository": AEB_REPOSITORY,
            "tree": AEB_TREE,
        },
        "inventory": {
            "commitment_sha256": commitment,
            "items": items,
            "ordering": "UTF-8 bytewise key order",
            "pages": len(items),
        },
        "schema": INPUT_SCHEMA,
        "schema_version": 1,
        "upstream_runner": {
            "git_blob_oid": AEB_RUNNER_GIT_BLOB_OID,
            "path": AEB_RUNNER_PATH,
            "sha256": AEB_RUNNER_SHA256,
        },
    }


def verify_environment_manifest(
    path: Path = ENVIRONMENT_MANIFEST_PATH,
) -> tuple[dict[str, Any], str]:
    metadata = _regular_file(path, context="current OSS environment manifest")
    if metadata.st_size > 256 * 1024:
        raise CurrentOSSBaselineError("current OSS environment manifest exceeds byte cap")
    manifest_sha256 = _sha256_file(path)
    if manifest_sha256 != EXPECTED_ENVIRONMENT_MANIFEST_SHA256:
        raise CurrentOSSBaselineError("current OSS environment manifest hash mismatch")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CurrentOSSBaselineError(
            "current OSS environment manifest is not valid UTF-8 JSON"
        ) from error
    manifest = _require_exact_keys(
        value,
        {
            "interpreter",
            "packages",
            "schema",
            "schema_version",
            "site_packages",
            "trafilatura_wheel",
            "venv",
        },
        context="current OSS environment manifest",
    )
    if manifest["schema"] != ENVIRONMENT_SCHEMA or manifest["schema_version"] != 1:
        raise CurrentOSSBaselineError("current OSS environment manifest identity mismatch")
    interpreter = _require_exact_keys(
        manifest["interpreter"],
        {"cache_tag", "implementation", "machine", "platform", "python_version"},
        context="current OSS environment interpreter",
    )
    if (
        interpreter["implementation"] != "cpython"
        or interpreter["cache_tag"] != "cpython-313"
        or interpreter["machine"] != "arm64"
        or interpreter["platform"] != "darwin"
        or interpreter["python_version"] != "3.13.5"
    ):
        raise CurrentOSSBaselineError("current OSS environment interpreter mismatch")
    venv = _require_exact_keys(
        manifest["venv"],
        {"builder", "builder_version", "include_system_site_packages"},
        context="current OSS virtual environment",
    )
    if venv != {
        "builder": "uv",
        "builder_version": "0.11.6",
        "include_system_site_packages": False,
    }:
        raise CurrentOSSBaselineError("current OSS virtual environment mismatch")
    packages = manifest["packages"]
    if not isinstance(packages, list) or len(packages) != 17:
        raise CurrentOSSBaselineError("current OSS environment package closure mismatch")
    package_names: list[str] = []
    for index, raw_package in enumerate(packages):
        package = _require_exact_keys(
            raw_package,
            {
                "distribution_inventory_sha256",
                "files",
                "metadata_sha256",
                "name",
                "record_sha256",
                "version",
                "wheel_metadata_sha256",
            },
            context=f"current OSS environment package {index}",
        )
        if (
            not isinstance(package["name"], str)
            or not package["name"]
            or not isinstance(package["version"], str)
            or not package["version"]
            or not isinstance(package["files"], int)
            or isinstance(package["files"], bool)
            or package["files"] <= 0
            or not _is_sha256(package["distribution_inventory_sha256"])
            or not _is_sha256(package["metadata_sha256"])
            or not _is_sha256(package["record_sha256"])
            or not _is_sha256(package["wheel_metadata_sha256"])
        ):
            raise CurrentOSSBaselineError("current OSS environment package value mismatch")
        package_names.append(package["name"])
    if package_names != sorted(package_names, key=lambda name: name.encode("utf-8")):
        raise CurrentOSSBaselineError("current OSS environment package order mismatch")
    if len(package_names) != len(set(package_names)):
        raise CurrentOSSBaselineError("current OSS environment has duplicate packages")
    trafilatura = next(
        (package for package in packages if package["name"] == "trafilatura"),
        None,
    )
    if (
        trafilatura is None
        or trafilatura["version"] != EXPECTED_VERSION
        or trafilatura["metadata_sha256"] != EXPECTED_METADATA_SHA256
        or trafilatura["record_sha256"] != EXPECTED_RECORD_SHA256
        or trafilatura["wheel_metadata_sha256"] != EXPECTED_WHEEL_METADATA_SHA256
    ):
        raise CurrentOSSBaselineError("current OSS Trafilatura distribution mismatch")
    site_packages = _require_exact_keys(
        manifest["site_packages"],
        {"bytes", "files", "inventory_sha256"},
        context="current OSS site-packages",
    )
    if (
        not isinstance(site_packages["bytes"], int)
        or isinstance(site_packages["bytes"], bool)
        or site_packages["bytes"] <= 0
        or not isinstance(site_packages["files"], int)
        or isinstance(site_packages["files"], bool)
        or site_packages["files"] <= 0
        or not _is_sha256(site_packages["inventory_sha256"])
    ):
        raise CurrentOSSBaselineError("current OSS site-packages identity mismatch")
    wheel = _require_exact_keys(
        manifest["trafilatura_wheel"],
        {"filename", "sha256", "source"},
        context="current OSS Trafilatura wheel",
    )
    if wheel != {
        "filename": EXPECTED_WHEEL_FILENAME,
        "sha256": EXPECTED_WHEEL_SHA256,
        "source": "https://pypi.org/simple",
    }:
        raise CurrentOSSBaselineError("current OSS Trafilatura wheel identity mismatch")
    return manifest, manifest_sha256


def _normalized_distribution_name(value: str) -> str:
    normalized = _NORMALIZE_DISTRIBUTION.sub("-", value).lower()
    if not normalized or normalized.startswith("-") or normalized.endswith("-"):
        raise CurrentOSSBaselineError("comparator lock package name is invalid")
    return normalized


def verify_requirements_lock(
    path: Path = REQUIREMENTS_LOCK_PATH,
    *,
    environment_manifest: dict[str, Any] | None = None,
    expected_sha256: str = EXPECTED_REQUIREMENTS_LOCK_SHA256,
) -> dict[str, Any]:
    """Verify the independent hash-pinned comparator dependency closure."""

    if not _is_sha256(expected_sha256):
        raise CurrentOSSBaselineError("comparator lock expected hash is invalid")
    metadata = _regular_file(path, context="current OSS requirements lock")
    if not 0 < metadata.st_size <= 1024 * 1024:
        raise CurrentOSSBaselineError("current OSS requirements lock exceeds byte cap")
    lock_sha256 = _sha256_file(path)
    if lock_sha256 != expected_sha256:
        raise CurrentOSSBaselineError("current OSS requirements lock hash mismatch")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise CurrentOSSBaselineError(
            "current OSS requirements lock is not valid UTF-8"
        ) from error
    if not text.endswith("\n") or "\r" in text or "\\\n" not in text:
        raise CurrentOSSBaselineError("current OSS requirements lock format mismatch")

    packages: dict[str, dict[str, Any]] = {}
    for raw_line in text.replace("\\\n", " ").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        tokens = line.split()
        match = _REQUIREMENT_PIN.fullmatch(tokens[0])
        if match is None:
            raise CurrentOSSBaselineError("current OSS requirements lock pin is invalid")
        name = _normalized_distribution_name(match.group("name"))
        version = match.group("version")
        if name in packages:
            raise CurrentOSSBaselineError("current OSS requirements lock has duplicate packages")
        hashes: list[str] = []
        for token in tokens[1:]:
            prefix = "--hash=sha256:"
            if not token.startswith(prefix) or not _is_sha256(token[len(prefix) :]):
                raise CurrentOSSBaselineError(
                    "current OSS requirements lock contains a non-SHA-256 option"
                )
            hashes.append(token[len(prefix) :])
        if not hashes or len(hashes) != len(set(hashes)):
            raise CurrentOSSBaselineError(
                "current OSS requirements lock has missing or duplicate hashes"
            )
        packages[name] = {
            "hashes": sorted(hashes),
            "version": version,
        }

    if environment_manifest is None:
        environment_manifest, _manifest_sha256 = verify_environment_manifest()
    expected_packages = {
        package["name"]: package["version"] for package in environment_manifest["packages"]
    }
    actual_packages = {name: package["version"] for name, package in packages.items()}
    if actual_packages != expected_packages:
        raise CurrentOSSBaselineError(
            "current OSS requirements lock package closure mismatch"
        )
    trafilatura_hashes = packages[EXPECTED_PACKAGE]["hashes"]
    if EXPECTED_WHEEL_SHA256 not in trafilatura_hashes:
        raise CurrentOSSBaselineError(
            "current OSS requirements lock lacks the expected Trafilatura wheel hash"
        )
    return {
        "packages": dict(sorted(actual_packages.items())),
        "require_hashes": True,
        "sha256": lock_sha256,
        "trafilatura_artifact_sha256": trafilatura_hashes,
        "trafilatura_wheel_sha256": EXPECTED_WHEEL_SHA256,
        "verified": True,
    }


def verify_production_lock_identity(
    lock_path: Path = PROJECT_ROOT / "uv.lock",
) -> dict[str, Any]:
    """Bind the candidate's own lock without coupling it to the comparator closure."""

    try:
        document = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise CurrentOSSBaselineError("could not parse uv.lock") from error
    raw_packages = document.get("package", [])
    if not isinstance(raw_packages, list) or not all(
        isinstance(package, dict) for package in raw_packages
    ):
        raise CurrentOSSBaselineError("uv.lock package table is malformed")
    packages = [package for package in raw_packages if package.get("name") == "trafilatura"]
    if len(packages) != 1:
        raise CurrentOSSBaselineError("uv.lock must contain exactly one Trafilatura package")
    package = packages[0]
    if package.get("version") != EXPECTED_VERSION:
        raise CurrentOSSBaselineError(
            f"uv.lock Trafilatura version mismatch: expected {EXPECTED_VERSION}, "
            f"found {package.get('version')}"
        )
    source = package.get("source")
    if source != {"registry": "https://pypi.org/simple"}:
        raise CurrentOSSBaselineError("uv.lock Trafilatura source mismatch")
    wheels = package.get("wheels")
    if not isinstance(wheels, list):
        raise CurrentOSSBaselineError("uv.lock Trafilatura wheel list is missing")
    matching = [
        wheel
        for wheel in wheels
        if isinstance(wheel, dict)
        and str(wheel.get("url", "")).rsplit("/", 1)[-1] == EXPECTED_WHEEL_FILENAME
    ]
    if len(matching) != 1:
        raise CurrentOSSBaselineError("uv.lock lacks the unique expected Trafilatura wheel")
    wheel = matching[0]
    if wheel.get("hash") != f"sha256:{EXPECTED_WHEEL_SHA256}":
        raise CurrentOSSBaselineError("uv.lock Trafilatura wheel hash mismatch")
    if not isinstance(wheel.get("size"), int) or isinstance(wheel.get("size"), bool):
        raise CurrentOSSBaselineError("uv.lock Trafilatura wheel size is invalid")
    return {
        "lock_sha256": _sha256_file(lock_path),
        "package": "trafilatura",
        "source": source,
        "version": EXPECTED_VERSION,
        "wheel": {
            "filename": EXPECTED_WHEEL_FILENAME,
            "sha256": EXPECTED_WHEEL_SHA256,
            "size": wheel["size"],
            "url": wheel["url"],
        },
    }


def _write_exclusive(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def _build_capsule(
    *,
    aeb_root: Path,
    capsule: Path,
    manifest: dict[str, Any],
) -> str:
    capsule.mkdir(mode=0o700)
    html_root = capsule / "html"
    html_root.mkdir(mode=0o700)
    for item in manifest["inventory"]["items"]:
        source = aeb_root / item["path"]
        target = capsule / item["path"]
        _write_exclusive(target, source.read_bytes())
    manifest_bytes = _canonical_bytes(manifest)
    _write_exclusive(capsule / "input-manifest.json", manifest_bytes)
    return _sha256_bytes(manifest_bytes)


def _verify_capsule(
    capsule: Path,
    *,
    manifest: dict[str, Any],
    manifest_sha256: str,
) -> None:
    expected = {"input-manifest.json"} | {item["path"] for item in manifest["inventory"]["items"]}
    actual: set[str] = set()
    directories: set[str] = set()
    for root, child_directories, child_files in os.walk(capsule, followlinks=False):
        root_path = Path(root)
        for name in child_directories:
            path = root_path / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise CurrentOSSBaselineError("capsule contains a linked or special directory")
            directories.add(path.relative_to(capsule).as_posix())
        for name in child_files:
            path = root_path / name
            _regular_file(path, context="capsule member")
            actual.add(path.relative_to(capsule).as_posix())
    if directories != {"html"} or actual != expected:
        raise CurrentOSSBaselineError("label-free capsule layout changed")
    if _sha256_file(capsule / "input-manifest.json") != manifest_sha256:
        raise CurrentOSSBaselineError("label-free capsule manifest changed")
    for item in manifest["inventory"]["items"]:
        path = capsule / item["path"]
        if (
            path.stat().st_size != item["compressed_bytes"]
            or _sha256_file(path) != item["compressed_sha256"]
        ):
            raise CurrentOSSBaselineError("label-free capsule HTML changed")


def _validate_package(value: Any, *, index: int) -> tuple[dict[str, Any], dict[str, Any]]:
    package = _require_exact_keys(
        value,
        {
            "distribution_inventory",
            "distribution_inventory_sha256",
            "files",
            "metadata_file_sha256",
            "name",
            "record_sha256",
            "version",
        },
        context=f"worker package {index}",
    )
    inventory = package["distribution_inventory"]
    if (
        not isinstance(package["name"], str)
        or not package["name"]
        or not isinstance(package["version"], str)
        or not package["version"]
        or not isinstance(package["files"], int)
        or isinstance(package["files"], bool)
        or not isinstance(inventory, list)
        or package["files"] != len(inventory)
        or not inventory
        or package["distribution_inventory_sha256"] != _hash_json(inventory)
        or not _is_sha256(package["record_sha256"])
    ):
        raise CurrentOSSBaselineError("worker package identity mismatch")
    metadata = package["metadata_file_sha256"]
    if (
        not isinstance(metadata, dict)
        or set(metadata) != {"INSTALLER", "METADATA", "WHEEL"}
        or not all(_is_sha256(digest) for digest in metadata.values())
    ):
        raise CurrentOSSBaselineError("worker wheel metadata identity mismatch")
    record_paths: list[str] = []
    for index, raw_item in enumerate(inventory):
        item = _require_exact_keys(
            raw_item,
            {"bytes", "path_from_prefix", "record_path", "sha256"},
            context=f"worker package distribution item {index}",
        )
        if (
            not isinstance(item["bytes"], int)
            or isinstance(item["bytes"], bool)
            or item["bytes"] < 0
            or not isinstance(item["path_from_prefix"], str)
            or not item["path_from_prefix"]
            or Path(item["path_from_prefix"]).is_absolute()
            or ".." in Path(item["path_from_prefix"]).parts
            or "\x00" in item["path_from_prefix"]
            or not isinstance(item["record_path"], str)
            or not item["record_path"]
            or "\x00" in item["record_path"]
            or not _is_sha256(item["sha256"])
        ):
            raise CurrentOSSBaselineError("worker distribution item is invalid")
        record_paths.append(item["record_path"])
    if record_paths != sorted(record_paths, key=lambda path: path.encode("utf-8")):
        raise CurrentOSSBaselineError("worker distribution inventory order mismatch")
    if len(record_paths) != len(set(record_paths)):
        raise CurrentOSSBaselineError("worker distribution inventory has duplicate paths")
    return package, {
        "distribution_inventory_sha256": package["distribution_inventory_sha256"],
        "files": package["files"],
        "metadata_sha256": metadata["METADATA"],
        "name": package["name"],
        "record_sha256": package["record_sha256"],
        "version": package["version"],
        "wheel_metadata_sha256": metadata["WHEEL"],
    }


def _validate_environment(
    value: Any,
    *,
    environment_manifest: dict[str, Any],
) -> dict[str, Any]:
    environment = _require_exact_keys(
        value,
        {"interpreter", "packages", "packages_sha256", "site_packages", "venv"},
        context="worker environment",
    )
    interpreter = _require_exact_keys(
        environment["interpreter"],
        {"cache_tag", "implementation", "machine", "platform", "python_version"},
        context="worker environment interpreter",
    )
    if interpreter != environment_manifest["interpreter"]:
        raise CurrentOSSBaselineError("worker environment interpreter mismatch")
    if environment["venv"] != environment_manifest["venv"]:
        raise CurrentOSSBaselineError("worker virtual environment mismatch")
    packages = environment["packages"]
    if (
        not isinstance(packages, list)
        or len(packages) != len(environment_manifest["packages"])
        or environment["packages_sha256"] != _hash_json(packages)
    ):
        raise CurrentOSSBaselineError("worker environment package closure mismatch")
    summaries: list[dict[str, Any]] = []
    package_names: list[str] = []
    for index, raw_package in enumerate(packages):
        package, summary = _validate_package(raw_package, index=index)
        package_names.append(package["name"])
        summaries.append(summary)
    if package_names != sorted(package_names, key=lambda name: name.encode("utf-8")):
        raise CurrentOSSBaselineError("worker environment package order mismatch")
    if len(package_names) != len(set(package_names)):
        raise CurrentOSSBaselineError("worker environment has duplicate packages")
    if summaries != environment_manifest["packages"]:
        raise CurrentOSSBaselineError("worker environment differs from frozen manifest")

    site_packages = _require_exact_keys(
        environment["site_packages"],
        {"bytes", "files", "inventory", "inventory_sha256"},
        context="worker site-packages",
    )
    inventory = site_packages["inventory"]
    if (
        not isinstance(inventory, list)
        or not isinstance(site_packages["bytes"], int)
        or isinstance(site_packages["bytes"], bool)
        or not isinstance(site_packages["files"], int)
        or isinstance(site_packages["files"], bool)
        or site_packages["files"] != len(inventory)
        or site_packages["inventory_sha256"] != _hash_json(inventory)
    ):
        raise CurrentOSSBaselineError("worker site-packages inventory mismatch")
    paths: list[str] = []
    total_bytes = 0
    for index, raw_item in enumerate(inventory):
        item = _require_exact_keys(
            raw_item,
            {"bytes", "path", "sha256"},
            context=f"worker site-packages item {index}",
        )
        if (
            not isinstance(item["bytes"], int)
            or isinstance(item["bytes"], bool)
            or item["bytes"] < 0
            or not isinstance(item["path"], str)
            or not item["path"]
            or Path(item["path"]).is_absolute()
            or ".." in Path(item["path"]).parts
            or "\x00" in item["path"]
            or not _is_sha256(item["sha256"])
        ):
            raise CurrentOSSBaselineError("worker site-packages item is invalid")
        paths.append(item["path"])
        total_bytes += item["bytes"]
    if paths != sorted(paths, key=lambda path: path.encode("utf-8")):
        raise CurrentOSSBaselineError("worker site-packages inventory order mismatch")
    if len(paths) != len(set(paths)):
        raise CurrentOSSBaselineError("worker site-packages inventory has duplicate paths")
    summary = {
        "bytes": total_bytes,
        "files": len(inventory),
        "inventory_sha256": site_packages["inventory_sha256"],
    }
    if summary != environment_manifest["site_packages"]:
        raise CurrentOSSBaselineError("worker site-packages differs from frozen manifest")
    return environment


def validate_worker_result(
    value: Any,
    *,
    manifest: dict[str, Any],
    manifest_sha256: str,
    worker_sha256: str,
    python_executable: Path,
    environment_manifest: dict[str, Any],
) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    result = _require_exact_keys(
        value,
        {"predictions", "receipt", "schema", "schema_version"},
        context="worker result",
    )
    if result["schema"] != RESULT_SCHEMA or result["schema_version"] != 2:
        raise CurrentOSSBaselineError("worker result identity mismatch")
    receipt = _require_exact_keys(
        result["receipt"],
        {
            "config",
            "config_sha256",
            "environment",
            "input_inventory_sha256",
            "input_manifest_sha256",
            "pages",
            "predictions_commitment_sha256",
            "python",
            "upstream_runner",
            "wall_ns",
            "worker_sha256",
        },
        context="worker receipt",
    )
    if (
        receipt["config"] != CONFIG
        or receipt["config_sha256"] != CONFIG_SHA256
        or receipt["input_inventory_sha256"] != manifest["inventory"]["commitment_sha256"]
        or receipt["input_manifest_sha256"] != manifest_sha256
        or receipt["pages"] != AEB_PAGES
        or receipt["upstream_runner"] != manifest["upstream_runner"]
        or receipt["worker_sha256"] != worker_sha256
        or not isinstance(receipt["wall_ns"], int)
        or isinstance(receipt["wall_ns"], bool)
        or receipt["wall_ns"] < 0
    ):
        raise CurrentOSSBaselineError("worker receipt binding mismatch")
    _validate_environment(
        receipt["environment"],
        environment_manifest=environment_manifest,
    )
    python = _require_exact_keys(
        receipt["python"],
        {"executable", "implementation", "isolated", "version"},
        context="worker Python",
    )
    if (
        python["implementation"] != "cpython"
        or python["isolated"] is not True
        or not isinstance(python["version"], str)
        or Path(str(python["executable"])).resolve() != python_executable.resolve()
    ):
        raise CurrentOSSBaselineError("worker interpreter identity mismatch")

    rows = result["predictions"]
    items = manifest["inventory"]["items"]
    if not isinstance(rows, list) or len(rows) != len(items):
        raise CurrentOSSBaselineError("worker prediction cardinality mismatch")
    predictions: dict[str, dict[str, str]] = {}
    commitment_rows: list[dict[str, Any]] = []
    for index, (raw_row, item) in enumerate(zip(rows, items, strict=True)):
        row = _require_exact_keys(
            raw_row,
            {
                "articleBody",
                "decoded_input_sha256",
                "key",
                "latency_ns",
                "prediction_bytes",
                "prediction_sha256",
            },
            context=f"worker prediction {index}",
        )
        article = row["articleBody"]
        if (
            row["key"] != item["key"]
            or row["decoded_input_sha256"] != item["decoded_sha256"]
            or not isinstance(article, str)
            or not isinstance(row["latency_ns"], int)
            or isinstance(row["latency_ns"], bool)
            or row["latency_ns"] < 0
            or not isinstance(row["prediction_bytes"], int)
            or isinstance(row["prediction_bytes"], bool)
            or row["prediction_bytes"] != len(article.encode("utf-8"))
            or row["prediction_sha256"] != _sha256_bytes(article.encode("utf-8"))
        ):
            raise CurrentOSSBaselineError("worker prediction binding mismatch")
        if item["key"] in predictions:
            raise CurrentOSSBaselineError("worker emitted a duplicate prediction key")
        predictions[item["key"]] = {"articleBody": article}
        commitment_rows.append(
            {
                "articleBody": article,
                "key": item["key"],
                "prediction_bytes": row["prediction_bytes"],
                "prediction_sha256": row["prediction_sha256"],
            }
        )
    if receipt["predictions_commitment_sha256"] != _hash_json(commitment_rows):
        raise CurrentOSSBaselineError("worker prediction commitment mismatch")
    if set(predictions) != {item["key"] for item in items}:
        raise CurrentOSSBaselineError("worker predictions have missing or extra keys")
    return predictions, receipt


def run_replay(
    *,
    aeb_root: Path,
    output_dir: Path,
    python_executable: Path,
    production_lock_path: Path = PROJECT_ROOT / "uv.lock",
    requirements_lock_path: Path = REQUIREMENTS_LOCK_PATH,
    worker_path: Path = PROJECT_ROOT / "bench" / "aeb_current_oss_worker.py",
    environment_manifest_path: Path = ENVIRONMENT_MANIFEST_PATH,
) -> dict[str, Any]:
    """Run and retain the exact current-OSS baseline before labels are loaded."""

    environment_manifest, environment_manifest_sha256 = verify_environment_manifest(
        environment_manifest_path
    )
    environment_manifest_bytes = environment_manifest_path.read_bytes()
    if _sha256_bytes(environment_manifest_bytes) != environment_manifest_sha256:
        raise CurrentOSSBaselineError("environment manifest changed during verification")
    requirements_lock = verify_requirements_lock(
        requirements_lock_path,
        environment_manifest=environment_manifest,
    )
    requirements_lock_bytes = requirements_lock_path.read_bytes()
    if _sha256_bytes(requirements_lock_bytes) != requirements_lock["sha256"]:
        raise CurrentOSSBaselineError("requirements lock changed during verification")
    production_lock = verify_production_lock_identity(production_lock_path)
    manifest = build_html_inventory(aeb_root)
    inventory_sha256 = manifest["inventory"]["commitment_sha256"]
    if inventory_sha256 != AEB_HTML_INVENTORY_SHA256:
        raise CurrentOSSBaselineError("AEB HTML inventory is not the hard-pinned capsule")
    worker_sha256 = _sha256_file(worker_path)
    if worker_sha256 != EXPECTED_WORKER_SHA256:
        raise CurrentOSSBaselineError("worker source differs from the reviewed implementation")
    interpreter = _python_executable(python_executable)
    baseline_root = output_dir / "baselines"
    baseline_root.mkdir(parents=True, exist_ok=True)
    worker_result_path = baseline_root / "trafilatura_2_1_0_worker_result.json"
    retained_manifest_path = baseline_root / "trafilatura_2_1_0_input_manifest.json"
    retained_environment_path = baseline_root / "trafilatura_2_1_0_environment_manifest.json"
    retained_requirements_path = baseline_root / "trafilatura_2_1_0_requirements.lock"
    predictions_path = baseline_root / "trafilatura_2_1_0_predictions.json"

    with tempfile.TemporaryDirectory(prefix="clusy-aeb-oss-capsule-") as temporary:
        capsule = Path(temporary) / "capsule"
        manifest_sha256 = _build_capsule(
            aeb_root=aeb_root,
            capsule=capsule,
            manifest=manifest,
        )
        _verify_capsule(
            capsule,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
        )
        command = [
            str(interpreter),
            "-I",
            "-B",
            str(worker_path.resolve()),
            "--capsule",
            str(capsule),
            "--output",
            str(worker_result_path.resolve()),
            "--expected-manifest-sha256",
            manifest_sha256,
            "--expected-inventory-sha256",
            inventory_sha256,
            "--expected-worker-sha256",
            worker_sha256,
        ]
        environment = {
            "HOME": str(capsule),
            "LANG": "C.UTF-8",
            "PATH": os.defpath,
            "PYTHONIOENCODING": "utf-8",
        }
        try:
            completed = subprocess.run(
                command,
                cwd=capsule,
                env=environment,
                check=False,
                capture_output=True,
                timeout=WORKER_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CurrentOSSBaselineError("current OSS worker did not complete") from error
        if (
            len(completed.stdout) > MAX_WORKER_STDIO_BYTES
            or len(completed.stderr) > MAX_WORKER_STDIO_BYTES
        ):
            raise CurrentOSSBaselineError("current OSS worker stdio exceeded byte cap")
        if completed.returncode != 0:
            message = completed.stderr.decode("utf-8", "replace").strip()
            raise CurrentOSSBaselineError(f"current OSS worker failed: {message[:1000]}")
        _verify_capsule(
            capsule,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
        )
        if _sha256_file(worker_path) != worker_sha256:
            raise CurrentOSSBaselineError("worker source changed during replay")
        if _sha256_file(environment_manifest_path) != environment_manifest_sha256:
            raise CurrentOSSBaselineError("environment manifest changed during replay")
        if _sha256_file(requirements_lock_path) != requirements_lock["sha256"]:
            raise CurrentOSSBaselineError("requirements lock changed during replay")
        if _sha256_file(production_lock_path) != production_lock["lock_sha256"]:
            raise CurrentOSSBaselineError("production lock changed during replay")
        if build_html_inventory(aeb_root) != manifest:
            raise CurrentOSSBaselineError("AEB HTML source changed during replay")
        _write_exclusive(retained_manifest_path, _canonical_bytes(manifest))
        _write_exclusive(
            retained_environment_path,
            environment_manifest_bytes,
        )
        _write_exclusive(
            retained_requirements_path,
            requirements_lock_bytes,
        )

    worker_metadata = _regular_file(worker_result_path, context="worker result")
    if worker_metadata.st_size > MAX_WORKER_RESULT_BYTES:
        raise CurrentOSSBaselineError("worker result exceeded byte cap")
    try:
        worker_result = json.loads(worker_result_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CurrentOSSBaselineError("worker result is not valid UTF-8 JSON") from error
    predictions, receipt = validate_worker_result(
        worker_result,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        worker_sha256=worker_sha256,
        python_executable=interpreter,
        environment_manifest=environment_manifest,
    )
    _write_exclusive(
        predictions_path,
        _canonical_bytes(
            {
                "output": predictions,
                "version": EXPECTED_VERSION,
            }
        ),
    )
    return {
        "artifacts": {
            "input_manifest": retained_manifest_path.relative_to(output_dir).as_posix(),
            "environment_manifest": retained_environment_path.relative_to(output_dir).as_posix(),
            "requirements_lock": retained_requirements_path.relative_to(output_dir).as_posix(),
            "predictions": predictions_path.relative_to(output_dir).as_posix(),
            "worker_result": worker_result_path.relative_to(output_dir).as_posix(),
        },
        "input_inventory_sha256": inventory_sha256,
        "input_manifest_sha256": manifest_sha256,
        "environment_manifest_sha256": environment_manifest_sha256,
        "production_lock": production_lock,
        "requirements_lock": requirements_lock,
        "predictions": predictions,
        "receipt": receipt,
        "verified": True,
        "version": EXPECTED_VERSION,
        "worker_sha256": worker_sha256,
    }

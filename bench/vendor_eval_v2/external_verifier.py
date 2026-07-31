"""Independent-process verifier entry for v5 synthetic artifacts.

The checked-in trust root is intentionally unsigned. Therefore this verifier
must reject every artifact until an external release process pins a repository
commit, scorer source, assets, protocol, allowed input commitments, public-key
identity, and signature. The scorer process cannot grant itself claimability.
The trust root is rejected before any artifact path is inspected, and every
local file read is regular-file-only and byte-capped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn, cast

TRUST_ROOT_PATH = Path(__file__).with_name("external_trust_root.json")
TRUST_ROOT_SCHEMA = "clusy.blind-vendor.external-trust-root.v1"
PACKAGE_PATH = Path(__file__).resolve().parent
REPOSITORY_PATH = PACKAGE_PATH.parents[1]
REQUIRED_ASSET_NAMES = (
    "UNICODE_LICENSE.txt",
    "confusables-16.0.0.txt",
    "synthetic_fixtures.json",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
_MAX_SAFE_JSON_INTEGER = (1 << 53) - 1
_HARD_MAX_LOCAL_FILE_BYTES = 16_000_000
_TRUST_ROOT_BYTE_CAP = 64_000
_ARTIFACT_BYTE_CAP = 8_000_000
_SCORER_SOURCE_BYTE_CAP = 1_000_000
_ASSET_BYTE_CAPS = {
    "UNICODE_LICENSE.txt": 64_000,
    "confusables-16.0.0.txt": 2_000_000,
    "synthetic_fixtures.json": 1_000_000,
}
_READ_CHUNK_BYTES = 64 * 1024


class ExternalVerificationError(RuntimeError):
    """External attestation is unavailable or invalid."""


def _validated_byte_cap(value: Any, *, label: str) -> int:
    if type(value) is not int or not 1 <= value <= _HARD_MAX_LOCAL_FILE_BYTES:
        raise ExternalVerificationError(
            f"{label} must be an exact built-in integer in [1, {_HARD_MAX_LOCAL_FILE_BYTES}]"
        )
    return value


def _bounded_exact_bytes(
    value: Any,
    *,
    label: str,
    byte_cap: int,
) -> bytes:
    cap = _validated_byte_cap(byte_cap, label=f"{label} byte cap")
    if type(value) is not bytes:
        raise ExternalVerificationError(f"{label} must be exact bytes")
    if len(value) > cap:
        raise ExternalVerificationError(f"{label} exceeds byte cap={cap}")
    return value


def _read_regular_file_capped(
    path: Path,
    *,
    label: str,
    byte_cap: int,
) -> bytes:
    """Preflight and stream one unchanged regular file without exceeding cap."""

    cap = _validated_byte_cap(byte_cap, label=f"{label} byte cap")
    try:
        preflight = os.lstat(path)
    except OSError as exc:
        raise ExternalVerificationError(f"{label} is unavailable") from exc
    if not stat.S_ISREG(preflight.st_mode):
        raise ExternalVerificationError(f"{label} must be a regular file")
    if preflight.st_size < 0 or preflight.st_size > cap:
        raise ExternalVerificationError(f"{label} exceeds byte cap={cap}")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ExternalVerificationError(f"{label} cannot be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ExternalVerificationError(f"{label} must remain a regular file")
        preflight_identity = (
            preflight.st_dev,
            preflight.st_ino,
            preflight.st_size,
            preflight.st_mtime_ns,
            preflight.st_ctime_ns,
        )
        opened_identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
        if opened_identity != preflight_identity:
            raise ExternalVerificationError(f"{label} changed after stat preflight")
        if opened.st_size < 0 or opened.st_size > cap:
            raise ExternalVerificationError(f"{label} exceeds byte cap={cap}")

        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                descriptor,
                min(_READ_CHUNK_BYTES, cap - total + 1),
            )
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                raise ExternalVerificationError(f"{label} exceeds byte cap={cap}")
            chunks.append(chunk)
        after_read = os.fstat(descriptor)
        if not stat.S_ISREG(after_read.st_mode):
            raise ExternalVerificationError(f"{label} must remain a regular file")
        after_read_identity = (
            after_read.st_dev,
            after_read.st_ino,
            after_read.st_size,
            after_read.st_mtime_ns,
            after_read.st_ctime_ns,
        )
        if after_read_identity != opened_identity:
            raise ExternalVerificationError(f"{label} changed during capped read")
        if after_read.st_size != total:
            raise ExternalVerificationError(f"{label} changed during capped read")
        return b"".join(chunks)
    except OSError as exc:
        raise ExternalVerificationError(f"{label} failed during capped read") from exc
    finally:
        try:
            os.close(descriptor)
        except OSError as exc:
            raise ExternalVerificationError(f"{label} failed while closing") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExternalVerificationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json_object(source: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            source.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda token: _reject_nonfinite(token, label=label),
        )
    except (RecursionError, ValueError) as exc:
        raise ExternalVerificationError(f"{label} must be strict UTF-8 JSON") from exc
    if type(value) is not dict:
        raise ExternalVerificationError(f"{label} must be an exact JSON object")
    return cast("dict[str, Any]", value)


def _reject_nonfinite(token: str, *, label: str) -> NoReturn:
    raise ExternalVerificationError(f"{label} contains non-finite number {token}")


def _load_fixed_trust_root() -> tuple[bytes, dict[str, Any]]:
    source = _read_regular_file_capped(
        TRUST_ROOT_PATH,
        label="fixed external trust root",
        byte_cap=_TRUST_ROOT_BYTE_CAP,
    )
    root = _strict_json_object(source, label="external trust root")
    if root.get("schema") != TRUST_ROOT_SCHEMA:
        raise ExternalVerificationError("external trust root schema mismatch")
    return source, root


def _validated_sha256(value: Any, *, label: str) -> str:
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise ExternalVerificationError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _validate_json_value(value: Any, *, path: str = "$") -> None:
    if value is None or type(value) in {bool, str}:
        return
    if type(value) is int:
        if abs(value) > _MAX_SAFE_JSON_INTEGER:
            raise ExternalVerificationError(f"{path} integer exceeds interoperable range")
        return
    if type(value) is float:
        raise ExternalVerificationError(f"{path} must not contain binary floating-point values")
    if type(value) is list:
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]")
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ExternalVerificationError(f"{path} contains a non-string key")
            _validate_json_value(item, path=f"{path}.{key}")
        return
    raise ExternalVerificationError(f"{path} contains unsupported JSON type")


def _canonical_artifact(
    source: Any,
) -> tuple[bytes, dict[str, Any]]:
    source = _bounded_exact_bytes(
        source,
        label="artifact source",
        byte_cap=_ARTIFACT_BYTE_CAP,
    )
    artifact = _strict_json_object(source, label="artifact")
    try:
        _validate_json_value(artifact)
        canonical = json.dumps(
            artifact,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (RecursionError, ValueError) as exc:
        raise ExternalVerificationError("artifact cannot be canonicalized safely") from exc
    if source != canonical:
        raise ExternalVerificationError("artifact bytes are not canonical JSON")
    return canonical, artifact


def _validate_signed_root(root: dict[str, Any]) -> None:
    repository_commit = root.get("repository_commit")
    if type(repository_commit) is not str or not _GIT_COMMIT.fullmatch(repository_commit):
        raise ExternalVerificationError("external repository commit is invalid")
    _validated_sha256(root.get("scorer_source_sha256"), label="external scorer source")
    _validated_sha256(root.get("protocol_manifest_sha256"), label="external protocol manifest")
    asset_sha256 = root.get("asset_sha256")
    if type(asset_sha256) is not dict or set(asset_sha256) != set(REQUIRED_ASSET_NAMES):
        raise ExternalVerificationError("external asset commitments are incomplete")
    for name in REQUIRED_ASSET_NAMES:
        _validated_sha256(asset_sha256.get(name), label=f"external asset {name}")
    allowed_commitments = root.get("allowed_input_commitment_sha256")
    if type(allowed_commitments) is not list or not allowed_commitments:
        raise ExternalVerificationError("external input commitment allow-list is empty")
    for index, commitment in enumerate(allowed_commitments):
        _validated_sha256(commitment, label=f"external input commitment {index}")
    for field in ("signature", "signature_scheme", "signing_key_id"):
        if type(root.get(field)) is not str or not root[field]:
            raise ExternalVerificationError(f"external {field} is invalid")


def _verify_external_signature(_root: dict[str, Any]) -> NoReturn:
    """Fail until an independent authority installs a reviewed public key."""

    raise ExternalVerificationError(
        "no externally approved signature verifier or public key is installed"
    )


def _load_authorized_trust_root() -> dict[str, Any]:
    """Load external authority or fail before any artifact path is touched."""

    _, root = _load_fixed_trust_root()
    required_root_fields = (
        "repository_commit",
        "scorer_source_sha256",
        "protocol_manifest_sha256",
        "signature",
        "signature_scheme",
        "signing_key_id",
    )
    if root.get("status") != "SIGNED_TRUSTED" or any(
        root.get(field) is None for field in required_root_fields
    ):
        raise ExternalVerificationError(
            "external trust root is unsigned; v5 artifacts are not claimable"
        )
    _validate_signed_root(root)
    _verify_external_signature(root)
    return root  # pragma: no cover - v5 has no approved signature verifier


def _current_repository_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(REPOSITORY_PATH), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExternalVerificationError("cannot resolve the repository commit") from exc
    commit = completed.stdout.strip()
    if not _GIT_COMMIT.fullmatch(commit):
        raise ExternalVerificationError("resolved repository commit is invalid")
    return commit


def _verify_bound_state(
    artifact_source: Any,
    *,
    expected_input_commitment_sha256: str,
    root: dict[str, Any],
) -> bytes:
    """Independently check every binding after external signature approval."""

    canonical, artifact = _canonical_artifact(artifact_source)
    if artifact.get("artifact_status") != "SYNTHETIC_ONLY / NOT_CLAIMABLE":
        raise ExternalVerificationError("artifact status is not synthetic-only")
    if artifact.get("claimable") is not False:
        raise ExternalVerificationError("artifact claimable flag is not false")
    if artifact.get("input_provenance") != "verified-synthetic-fixture":
        raise ExternalVerificationError("artifact input provenance is not a verified fixture")
    observed_commitment = _validated_sha256(
        artifact.get("input_commitment_sha256"),
        label="artifact input commitment",
    )
    if observed_commitment != expected_input_commitment_sha256:
        raise ExternalVerificationError("artifact input commitment does not match invocation")
    allowed_commitments = cast("list[str]", root["allowed_input_commitment_sha256"])
    if observed_commitment not in allowed_commitments:
        raise ExternalVerificationError("artifact input commitment is not externally allowed")

    expected_protocol = cast("str", root["protocol_manifest_sha256"])
    if artifact.get("protocol_manifest_sha256") != expected_protocol:
        raise ExternalVerificationError("artifact protocol manifest is not externally committed")
    expected_source = cast("str", root["scorer_source_sha256"])
    scorer_source = _read_regular_file_capped(
        PACKAGE_PATH.joinpath("kernel.py"),
        label="packaged scorer source",
        byte_cap=_SCORER_SOURCE_BYTE_CAP,
    )
    if hashlib.sha256(scorer_source).hexdigest() != expected_source:
        raise ExternalVerificationError("packaged scorer source is not externally committed")
    if artifact.get("packaged_source_sha256") != expected_source:
        raise ExternalVerificationError(
            "artifact scorer source identity is not externally committed"
        )

    expected_assets = cast("dict[str, str]", root["asset_sha256"])
    observed_assets: dict[str, bytes] = {}
    for name in REQUIRED_ASSET_NAMES:
        source = _read_regular_file_capped(
            PACKAGE_PATH.joinpath(name),
            label=f"externally committed asset {name}",
            byte_cap=_ASSET_BYTE_CAPS[name],
        )
        if hashlib.sha256(source).hexdigest() != expected_assets[name]:
            raise ExternalVerificationError(f"asset is not externally committed: {name}")
        observed_assets[name] = source
    if (
        artifact.get("synthetic_fixture_source_sha256")
        != expected_assets["synthetic_fixtures.json"]
    ):
        raise ExternalVerificationError(
            "artifact fixture source identity is not externally committed"
        )
    if (
        artifact.get("unicode_confusables_data_source_sha256")
        != expected_assets["confusables-16.0.0.txt"]
    ):
        raise ExternalVerificationError(
            "artifact confusables-data identity is not externally committed"
        )
    fixture_document = _strict_json_object(
        observed_assets["synthetic_fixtures.json"],
        label="synthetic fixture asset",
    )
    try:
        _validate_json_value(fixture_document)
        fixture_canonical = json.dumps(
            fixture_document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (RecursionError, ValueError) as exc:
        raise ExternalVerificationError(
            "synthetic fixture asset cannot be canonicalized safely"
        ) from exc
    if artifact.get("synthetic_fixture_sha256") != hashlib.sha256(fixture_canonical).hexdigest():
        raise ExternalVerificationError(
            "artifact fixture canonical identity is not externally committed"
        )
    if _current_repository_commit() != root["repository_commit"]:
        raise ExternalVerificationError("repository commit is not externally committed")
    return canonical


def verification_status_document() -> dict[str, Any]:
    """Report why v5 cannot currently receive external claim attestation."""

    source, root = _load_fixed_trust_root()
    return {
        "artifact_status": "SYNTHETIC_ONLY / NOT_CLAIMABLE",
        "claimable": False,
        "external_trust_root_sha256": hashlib.sha256(source).hexdigest(),
        "external_trust_root_status": root.get("status"),
        "protocol_manifest_sha256": root.get("protocol_manifest_sha256"),
        "schema": "clusy.blind-vendor.external-verification-status.v1",
    }


def verify_canonical_artifact(
    artifact_source: Any,
    *,
    expected_input_commitment_sha256: str,
) -> bytes:
    """Verify and return canonical bytes, or fail before emitting any artifact."""

    root = _load_authorized_trust_root()
    if type(expected_input_commitment_sha256) is not str or not _SHA256.fullmatch(
        expected_input_commitment_sha256
    ):
        raise ExternalVerificationError(
            "expected input commitment must be a lowercase SHA-256 digest"
        )
    artifact_source = _bounded_exact_bytes(
        artifact_source,
        label="artifact source",
        byte_cap=_ARTIFACT_BYTE_CAP,
    )
    return _verify_bound_state(
        artifact_source,
        expected_input_commitment_sha256=expected_input_commitment_sha256,
        root=root,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--input-commitment-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        root = _load_authorized_trust_root()
        if type(arguments.input_commitment_sha256) is not str or not _SHA256.fullmatch(
            arguments.input_commitment_sha256
        ):
            raise ExternalVerificationError(
                "expected input commitment must be a lowercase SHA-256 digest"
            )
        source = _read_regular_file_capped(
            arguments.artifact,
            label="artifact",
            byte_cap=_ARTIFACT_BYTE_CAP,
        )
        verified = _verify_bound_state(
            source,
            expected_input_commitment_sha256=arguments.input_commitment_sha256,
            root=root,
        )
    except ExternalVerificationError as exc:
        print(f"external verification failed: {exc}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(verified)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import copy
import gzip
import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from bench import aeb_current_oss_baseline as controller
from bench import aeb_current_oss_worker as worker


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_capsule(tmp_path: Path, *, pages: int = 2) -> tuple[Path, dict[str, Any], str]:
    capsule = tmp_path / "capsule"
    html_root = capsule / "html"
    html_root.mkdir(parents=True)
    items: list[dict[str, Any]] = []
    for index in range(pages):
        key = f"page-{index:02d}"
        decoded = f"<html><main>fixture {index}</main></html>".encode()
        compressed = gzip.compress(decoded, mtime=0)
        relative = f"html/{key}.html.gz"
        (capsule / relative).write_bytes(compressed)
        items.append(
            {
                "compressed_bytes": len(compressed),
                "compressed_sha256": _sha256(compressed),
                "decoded_bytes": len(decoded),
                "decoded_sha256": _sha256(decoded),
                "git_blob_oid": hashlib.sha1(
                    f"blob {len(compressed)}\0".encode() + compressed
                ).hexdigest(),
                "key": key,
                "path": relative,
            }
        )
    manifest = {
        "dataset": {
            "commit": worker.AEB_COMMIT,
            "repository": "https://github.com/scrapinghub/article-extraction-benchmark.git",
            "tree": worker.AEB_TREE,
        },
        "inventory": {
            "commitment_sha256": worker._hash_json(items),
            "items": items,
            "ordering": "UTF-8 bytewise key order",
            "pages": pages,
        },
        "schema": worker.INPUT_SCHEMA,
        "schema_version": 1,
        "upstream_runner": {
            "git_blob_oid": controller.AEB_RUNNER_GIT_BLOB_OID,
            "path": controller.AEB_RUNNER_PATH,
            "sha256": controller.AEB_RUNNER_SHA256,
        },
    }
    manifest_bytes = worker._canonical_bytes(manifest)
    (capsule / "input-manifest.json").write_bytes(manifest_bytes)
    return capsule, manifest, _sha256(manifest_bytes)


@pytest.fixture
def small_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "AEB_PAGES", 2)
    monkeypatch.setattr(controller, "AEB_PAGES", 2)


def _test_environment() -> tuple[dict[str, Any], dict[str, Any]]:
    distribution_inventory = [
        {
            "bytes": 1,
            "path_from_prefix": "lib/python3.13/site-packages/trafilatura/__init__.py",
            "record_path": "trafilatura/__init__.py",
            "sha256": "1" * 64,
        }
    ]
    package = {
        "distribution_inventory": distribution_inventory,
        "distribution_inventory_sha256": controller._hash_json(distribution_inventory),
        "files": 1,
        "metadata_file_sha256": {
            "INSTALLER": "3" * 64,
            "METADATA": controller.EXPECTED_METADATA_SHA256,
            "WHEEL": controller.EXPECTED_WHEEL_METADATA_SHA256,
        },
        "name": "trafilatura",
        "record_sha256": controller.EXPECTED_RECORD_SHA256,
        "version": controller.EXPECTED_VERSION,
    }
    site_inventory = [
        {
            "bytes": 2,
            "path": "trafilatura/__init__.py",
            "sha256": "4" * 64,
        }
    ]
    environment: dict[str, Any] = {
        "interpreter": {
            "cache_tag": "cpython-313",
            "implementation": "cpython",
            "machine": "arm64",
            "platform": "darwin",
            "python_version": "3.13.5",
        },
        "packages": [package],
        "packages_sha256": controller._hash_json([package]),
        "site_packages": {
            "bytes": 2,
            "files": 1,
            "inventory": site_inventory,
            "inventory_sha256": controller._hash_json(site_inventory),
        },
        "venv": {
            "builder": "uv",
            "builder_version": "0.11.6",
            "include_system_site_packages": False,
        },
    }
    manifest: dict[str, Any] = {
        "interpreter": environment["interpreter"],
        "packages": [
            {
                "distribution_inventory_sha256": package["distribution_inventory_sha256"],
                "files": 1,
                "metadata_sha256": controller.EXPECTED_METADATA_SHA256,
                "name": "trafilatura",
                "record_sha256": controller.EXPECTED_RECORD_SHA256,
                "version": controller.EXPECTED_VERSION,
                "wheel_metadata_sha256": controller.EXPECTED_WHEEL_METADATA_SHA256,
            }
        ],
        "site_packages": {
            "bytes": 2,
            "files": 1,
            "inventory_sha256": environment["site_packages"]["inventory_sha256"],
        },
        "venv": environment["venv"],
    }
    return environment, manifest


def _valid_worker_result(
    manifest: dict[str, Any],
    *,
    manifest_sha256: str,
    worker_sha256: str,
    python_executable: Path,
) -> dict[str, Any]:
    rows = []
    commitment_rows = []
    for item in manifest["inventory"]["items"]:
        article = f"prediction for {item['key']}"
        article_bytes = article.encode()
        row = {
            "articleBody": article,
            "decoded_input_sha256": item["decoded_sha256"],
            "key": item["key"],
            "latency_ns": 1,
            "prediction_bytes": len(article_bytes),
            "prediction_sha256": _sha256(article_bytes),
        }
        rows.append(row)
        commitment_rows.append(
            {
                "articleBody": article,
                "key": item["key"],
                "prediction_bytes": len(article_bytes),
                "prediction_sha256": _sha256(article_bytes),
            }
        )
    environment, _environment_manifest = _test_environment()
    return {
        "predictions": rows,
        "receipt": {
            "config": copy.deepcopy(controller.CONFIG),
            "config_sha256": controller.CONFIG_SHA256,
            "environment": environment,
            "input_inventory_sha256": manifest["inventory"]["commitment_sha256"],
            "input_manifest_sha256": manifest_sha256,
            "pages": len(rows),
            "predictions_commitment_sha256": controller._hash_json(commitment_rows),
            "python": {
                "executable": str(python_executable),
                "implementation": "cpython",
                "isolated": True,
                "version": sys.version,
            },
            "upstream_runner": manifest["upstream_runner"],
            "wall_ns": 2,
            "worker_sha256": worker_sha256,
        },
        "schema": controller.RESULT_SCHEMA,
        "schema_version": 2,
    }


def test_worker_rejects_runtime_version_mismatch(
    tmp_path: Path,
    small_contract: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capsule, manifest, manifest_sha256 = _write_capsule(tmp_path)
    worker_path = Path(worker.__file__).resolve()
    monkeypatch.setattr(worker, "EXPECTED_VERSION", "0.0-test-mismatch")

    with pytest.raises(worker.WorkerError, match="runtime package version mismatch"):
        worker.run(
            capsule=capsule,
            output=tmp_path / "result.json",
            expected_manifest_sha256=manifest_sha256,
            expected_inventory_sha256=manifest["inventory"]["commitment_sha256"],
            expected_worker_sha256=worker._sha256_file(worker_path),
        )


def test_worker_rejects_input_drift(
    tmp_path: Path,
    small_contract: None,
) -> None:
    capsule, manifest, manifest_sha256 = _write_capsule(tmp_path)
    first = capsule / manifest["inventory"]["items"][0]["path"]
    first.write_bytes(first.read_bytes() + b"drift")

    with pytest.raises(worker.WorkerError, match="compressed HTML size drift"):
        worker._load_capsule(
            capsule,
            expected_manifest_sha256=manifest_sha256,
            expected_inventory_sha256=manifest["inventory"]["commitment_sha256"],
        )


@pytest.mark.parametrize(
    "relative",
    (
        "ground-truth.json",
        "evaluate.py",
        "output/trafilatura.json",
        "predictions/clusy.json",
    ),
)
def test_worker_rejects_labels_evaluator_or_predictions_visible_in_capsule(
    tmp_path: Path,
    small_contract: None,
    relative: str,
) -> None:
    capsule, manifest, manifest_sha256 = _write_capsule(tmp_path)
    leaked = capsule / relative
    leaked.parent.mkdir(exist_ok=True)
    leaked.write_text("forbidden", encoding="utf-8")

    with pytest.raises(worker.WorkerError, match="labels, evaluator, or predictions"):
        worker._load_capsule(
            capsule,
            expected_manifest_sha256=manifest_sha256,
            expected_inventory_sha256=manifest["inventory"]["commitment_sha256"],
        )


@pytest.mark.parametrize("mutation", ("missing", "extra"))
def test_worker_rejects_missing_or_extra_capsule_member(
    tmp_path: Path,
    small_contract: None,
    mutation: str,
) -> None:
    capsule, manifest, manifest_sha256 = _write_capsule(tmp_path)
    if mutation == "missing":
        (capsule / manifest["inventory"]["items"][0]["path"]).unlink()
    else:
        (capsule / "html/extra.html.gz").write_bytes(gzip.compress(b"extra", mtime=0))

    with pytest.raises(worker.WorkerError, match="missing or extra files"):
        worker._load_capsule(
            capsule,
            expected_manifest_sha256=manifest_sha256,
            expected_inventory_sha256=manifest["inventory"]["commitment_sha256"],
        )


def test_worker_rejects_source_hash_tamper(
    tmp_path: Path,
    small_contract: None,
) -> None:
    capsule, manifest, manifest_sha256 = _write_capsule(tmp_path)

    with pytest.raises(worker.WorkerError, match="worker source SHA-256 mismatch"):
        worker.run(
            capsule=capsule,
            output=tmp_path / "result.json",
            expected_manifest_sha256=manifest_sha256,
            expected_inventory_sha256=manifest["inventory"]["commitment_sha256"],
            expected_worker_sha256="0" * 64,
        )


@pytest.mark.parametrize(
    ("field", "expected_message"),
    (
        ("config", "worker receipt binding mismatch"),
        ("worker_sha256", "worker receipt binding mismatch"),
        ("package_version", "worker environment differs from frozen manifest"),
        ("wheel_metadata", "worker environment differs from frozen manifest"),
        ("record_hash", "worker environment differs from frozen manifest"),
        ("package_inventory", "worker environment differs from frozen manifest"),
        ("site_hash", "worker site-packages differs from frozen manifest"),
        ("venv_builder", "worker virtual environment mismatch"),
        ("extra_package", "worker environment package closure mismatch"),
        ("prediction_hash", "worker prediction binding mismatch"),
        ("missing_prediction", "worker prediction cardinality mismatch"),
        ("extra_prediction", "worker prediction cardinality mismatch"),
    ),
)
def test_controller_rejects_self_consistent_or_structural_result_tamper(
    tmp_path: Path,
    small_contract: None,
    field: str,
    expected_message: str,
) -> None:
    _capsule, manifest, manifest_sha256 = _write_capsule(tmp_path)
    worker_sha256 = "a" * 64
    result = _valid_worker_result(
        manifest,
        manifest_sha256=manifest_sha256,
        worker_sha256=worker_sha256,
        python_executable=Path(sys.executable),
    )
    _environment, environment_manifest = _test_environment()
    if field == "config":
        result["receipt"]["config"]["keyword_arguments"]["include_comments"] = True
        result["receipt"]["config_sha256"] = controller._hash_json(result["receipt"]["config"])
    elif field == "worker_sha256":
        result["receipt"]["worker_sha256"] = "b" * 64
    elif field == "package_version":
        result["receipt"]["environment"]["packages"][0]["version"] = "2.0.0"
        result["receipt"]["environment"]["packages_sha256"] = controller._hash_json(
            result["receipt"]["environment"]["packages"]
        )
    elif field == "wheel_metadata":
        result["receipt"]["environment"]["packages"][0]["metadata_file_sha256"]["WHEEL"] = "c" * 64
        result["receipt"]["environment"]["packages_sha256"] = controller._hash_json(
            result["receipt"]["environment"]["packages"]
        )
    elif field == "record_hash":
        result["receipt"]["environment"]["packages"][0]["record_sha256"] = "d" * 64
        result["receipt"]["environment"]["packages_sha256"] = controller._hash_json(
            result["receipt"]["environment"]["packages"]
        )
    elif field == "package_inventory":
        package = result["receipt"]["environment"]["packages"][0]
        package["distribution_inventory"][0]["sha256"] = "5" * 64
        package["distribution_inventory_sha256"] = controller._hash_json(
            package["distribution_inventory"]
        )
        result["receipt"]["environment"]["packages_sha256"] = controller._hash_json(
            result["receipt"]["environment"]["packages"]
        )
    elif field == "site_hash":
        site_packages = result["receipt"]["environment"]["site_packages"]
        site_packages["inventory"][0]["sha256"] = "6" * 64
        site_packages["inventory_sha256"] = controller._hash_json(site_packages["inventory"])
    elif field == "venv_builder":
        result["receipt"]["environment"]["venv"]["builder_version"] = "tampered"
    elif field == "extra_package":
        package = copy.deepcopy(result["receipt"]["environment"]["packages"][0])
        package["name"] = "unexpected"
        result["receipt"]["environment"]["packages"].append(package)
        result["receipt"]["environment"]["packages_sha256"] = controller._hash_json(
            result["receipt"]["environment"]["packages"]
        )
    elif field == "prediction_hash":
        result["predictions"][0]["articleBody"] = "forged"
    elif field == "missing_prediction":
        result["predictions"].pop()
    elif field == "extra_prediction":
        result["predictions"].append(copy.deepcopy(result["predictions"][0]))

    with pytest.raises(controller.CurrentOSSBaselineError, match=expected_message):
        controller.validate_worker_result(
            result,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            worker_sha256=worker_sha256,
            python_executable=Path(sys.executable),
            environment_manifest=environment_manifest,
        )


def test_controller_accepts_only_exact_result_schema(
    tmp_path: Path,
    small_contract: None,
) -> None:
    _capsule, manifest, manifest_sha256 = _write_capsule(tmp_path)
    worker_sha256 = "a" * 64
    result = _valid_worker_result(
        manifest,
        manifest_sha256=manifest_sha256,
        worker_sha256=worker_sha256,
        python_executable=Path(sys.executable),
    )
    _environment, environment_manifest = _test_environment()

    predictions, receipt = controller.validate_worker_result(
        result,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        worker_sha256=worker_sha256,
        python_executable=Path(sys.executable),
        environment_manifest=environment_manifest,
    )

    assert set(predictions) == {item["key"] for item in manifest["inventory"]["items"]}
    assert receipt["config"] == controller.CONFIG
    assert receipt["environment"]["packages"][0]["version"] == "2.1.0"
    assert "locked_wheel_sha256" not in receipt
    assert "environment_lock_sha256" not in receipt


def test_production_lock_version_and_wheel_hash_are_fail_closed(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        """
version = 1

[[package]]
name = "trafilatura"
version = "2.0.0"
source = { registry = "https://pypi.org/simple" }
[[package.wheels]]
url = "https://example.test/trafilatura-2.1.0-py3-none-any.whl"
hash = "sha256:deadbeef"
size = 1
""",
        encoding="utf-8",
    )

    with pytest.raises(
        controller.CurrentOSSBaselineError,
        match="Trafilatura version mismatch",
    ):
        controller.verify_production_lock_identity(lock)


def test_production_lock_is_not_coupled_to_comparator_transitives(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        f"""
version = 1

[[package]]
name = "trafilatura"
version = "2.1.0"
source = {{ registry = "https://pypi.org/simple" }}
[[package.wheels]]
url = "https://example.test/{controller.EXPECTED_WHEEL_FILENAME}"
hash = "sha256:{controller.EXPECTED_WHEEL_SHA256}"
size = 1
""",
        encoding="utf-8",
    )

    identity = controller.verify_production_lock_identity(lock)

    assert identity["version"] == "2.1.0"
    assert "packages" not in identity


def test_requirements_lock_is_hash_pinned_and_matches_frozen_environment(
    tmp_path: Path,
) -> None:
    manifest, _manifest_sha256 = controller.verify_environment_manifest()
    identity = controller.verify_requirements_lock(environment_manifest=manifest)
    assert identity["verified"] is True
    assert identity["require_hashes"] is True
    assert len(identity["packages"]) == 17
    assert identity["trafilatura_wheel_sha256"] == controller.EXPECTED_WHEEL_SHA256

    tampered = tmp_path / "requirements.lock"
    tampered.write_bytes(controller.REQUIREMENTS_LOCK_PATH.read_bytes() + b"\n")
    with pytest.raises(
        controller.CurrentOSSBaselineError,
        match="requirements lock hash mismatch",
    ):
        controller.verify_requirements_lock(tampered, environment_manifest=manifest)


@pytest.mark.parametrize(
    ("contents", "message"),
    (
        (
            "trafilatura==2.1.0 \\\n",
            "missing or duplicate hashes",
        ),
        (
            f"trafilatura==2.1.0 \\\n"
            f"    --hash=sha256:{controller.EXPECTED_WHEEL_SHA256}\n"
            "unexpected==1.0.0 \\\n"
            f"    --hash=sha256:{'a' * 64}\n",
            "package closure mismatch",
        ),
        (
            f"trafilatura==2.1.0 \\\n"
            f"    --hash=sha256:{'f' * 64}\n",
            "lacks the expected Trafilatura wheel hash",
        ),
        (
            "trafilatura==2.1.0 \\\n"
            "    --trusted-host=example.test\n",
            "non-SHA-256 option",
        ),
    ),
)
def test_requirements_lock_rejects_structural_tamper(
    tmp_path: Path,
    contents: str,
    message: str,
) -> None:
    _environment, environment_manifest = _test_environment()
    path = tmp_path / "requirements.lock"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(controller.CurrentOSSBaselineError, match=message):
        controller.verify_requirements_lock(
            path,
            environment_manifest=environment_manifest,
            expected_sha256=_sha256(path.read_bytes()),
        )


def test_environment_manifest_is_hash_pinned_and_structurally_exact(
    tmp_path: Path,
) -> None:
    manifest, manifest_sha256 = controller.verify_environment_manifest()
    assert manifest_sha256 == controller.EXPECTED_ENVIRONMENT_MANIFEST_SHA256
    assert len(manifest["packages"]) == 17
    assert manifest["packages"][14]["name"] == "trafilatura"
    assert manifest["packages"][14]["record_sha256"] == controller.EXPECTED_RECORD_SHA256

    tampered = tmp_path / "environment.json"
    tampered.write_bytes(controller.ENVIRONMENT_MANIFEST_PATH.read_bytes() + b"\n")
    with pytest.raises(
        controller.CurrentOSSBaselineError,
        match="environment manifest hash mismatch",
    ):
        controller.verify_environment_manifest(tampered)


def test_python_executable_preserves_virtualenv_launcher_symlink(tmp_path: Path) -> None:
    launcher = tmp_path / "venv" / "bin" / "python"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(Path(sys.executable).resolve())

    accepted = controller._python_executable(launcher)

    assert accepted == launcher.absolute()
    assert accepted != launcher.resolve()


def test_subprocess_failure_is_never_a_claimable_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    small_contract: None,
) -> None:
    aeb_root = tmp_path / "aeb"
    aeb_root.mkdir()
    _capsule, manifest, _manifest_sha256 = _write_capsule(tmp_path / "source")
    for item in manifest["inventory"]["items"]:
        source = tmp_path / "source/capsule" / item["path"]
        target = aeb_root / item["path"]
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(source.read_bytes())
    monkeypatch.setattr(
        controller,
        "AEB_HTML_INVENTORY_SHA256",
        manifest["inventory"]["commitment_sha256"],
    )
    _environment, environment_manifest = _test_environment()
    environment_path = tmp_path / "environment.json"
    environment_path.write_bytes(b"test environment")
    environment_sha256 = _sha256(environment_path.read_bytes())
    monkeypatch.setattr(
        controller,
        "verify_environment_manifest",
        lambda _path: (environment_manifest, environment_sha256),
    )
    monkeypatch.setattr(
        controller,
        "verify_requirements_lock",
        lambda _path, **_kwargs: {
            "sha256": _sha256(b"requirements"),
            "verified": True,
        },
    )
    monkeypatch.setattr(
        controller,
        "verify_production_lock_identity",
        lambda _path: {"lock_sha256": _sha256(b"production")},
    )
    monkeypatch.setattr(controller, "build_html_inventory", lambda _root: manifest)
    requirements_path = tmp_path / "requirements.lock"
    requirements_path.write_bytes(b"requirements")
    production_lock_path = tmp_path / "uv.lock"
    production_lock_path.write_bytes(b"production")
    observed: dict[str, Any] = {}

    def fail_worker(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        observed["args"] = args
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(args[0], 2, b"", b"forced failure")

    monkeypatch.setattr(subprocess, "run", fail_worker)

    with pytest.raises(controller.CurrentOSSBaselineError, match="worker failed"):
        controller.run_replay(
            aeb_root=aeb_root,
            output_dir=tmp_path / "output",
            python_executable=Path(sys.executable),
            production_lock_path=production_lock_path,
            requirements_lock_path=requirements_path,
            environment_manifest_path=environment_path,
        )

    command = observed["args"][0]
    assert "-I" in command
    assert observed["kwargs"]["env"].keys() == {
        "HOME",
        "LANG",
        "PATH",
        "PYTHONIOENCODING",
    }


def test_controller_rejects_tampered_worker_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    small_contract: None,
) -> None:
    _capsule, manifest, _manifest_sha256 = _write_capsule(tmp_path / "source")
    tampered_worker = tmp_path / "worker.py"
    tampered_worker.write_bytes(Path(worker.__file__).read_bytes() + b"\n# tamper\n")
    monkeypatch.setattr(
        controller,
        "AEB_HTML_INVENTORY_SHA256",
        manifest["inventory"]["commitment_sha256"],
    )
    _environment, environment_manifest = _test_environment()
    environment_path = tmp_path / "environment.json"
    environment_path.write_bytes(b"test environment")
    environment_sha256 = _sha256(environment_path.read_bytes())
    monkeypatch.setattr(
        controller,
        "verify_environment_manifest",
        lambda _path: (environment_manifest, environment_sha256),
    )
    monkeypatch.setattr(
        controller,
        "verify_requirements_lock",
        lambda _path, **_kwargs: {
            "sha256": _sha256(b"requirements"),
            "verified": True,
        },
    )
    monkeypatch.setattr(
        controller,
        "verify_production_lock_identity",
        lambda _path: {"lock_sha256": _sha256(b"production")},
    )
    monkeypatch.setattr(controller, "build_html_inventory", lambda _root: manifest)
    requirements_path = tmp_path / "requirements.lock"
    requirements_path.write_bytes(b"requirements")
    production_lock_path = tmp_path / "uv.lock"
    production_lock_path.write_bytes(b"production")

    with pytest.raises(
        controller.CurrentOSSBaselineError,
        match="reviewed implementation",
    ):
        controller.run_replay(
            aeb_root=tmp_path,
            output_dir=tmp_path / "output",
            python_executable=Path(sys.executable),
            production_lock_path=production_lock_path,
            requirements_lock_path=requirements_path,
            worker_path=tampered_worker,
            environment_manifest_path=environment_path,
        )

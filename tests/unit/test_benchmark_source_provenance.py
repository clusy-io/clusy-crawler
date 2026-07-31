from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from bench import neutral_benchmark, wcxb_benchmark, webis_benchmark
from bench import webmainbench_benchmark as webmain
from bench import webmainbench_finegrained_benchmark as webmain_fine
from bench.source_provenance import (
    SourceInventoryError,
    git_visible_vendor_files,
    git_visible_vendor_runtime_text_files,
    native_source_digest,
    native_source_files,
    native_source_patterns,
    verify_loaded_native_source_binding,
)

ROOT = Path(__file__).resolve().parents[2]
HELPER_RELATIVE = "bench/source_provenance.py"
NATIVE_MUTATION_CASES = (
    "Cargo.toml",
    "src/lib.rs",
    "python/clusy_native/__init__.py",
    "python/clusy_native/_native.pyi",
    "vendor/backend/Cargo.toml",
    "vendor/backend/LICENSE-APACHE",
    "vendor/backend/.cargo_vcs_info.json",
    "vendor/backend/src/lib.rs",
)


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _write_native_fixture(root: Path) -> None:
    native = root / "native"
    inventory = (
        "clusy-native-source-inventory-v1\n"
        "source-inventory-v1.txt\n"
        "Cargo.toml\n"
        "src/**/*.rs\n"
        "python/**/*.py\n"
        "python/**/*.pyi\n"
        "vendor/*/Cargo.toml\n"
        "vendor/*/.cargo_vcs_info.json\n"
        "vendor/*/LICENSE-*\n"
        "vendor/*/src/**/*.rs\n"
    )
    files = {
        "source-inventory-v1.txt": inventory,
        "Cargo.toml": "[package]\nname = \"fixture\"\n",
        "src/lib.rs": "pub fn fixture() {}\n",
        "python/clusy_native/__init__.py": '"""fixture"""\n',
        "python/clusy_native/_native.pyi": "def fixture() -> None: ...\n",
        "vendor/backend/Cargo.toml": "[package]\nname = \"backend\"\n",
        "vendor/backend/.cargo_vcs_info.json": '{"git":{"sha1":"fixture"}}\n',
        "vendor/backend/LICENSE-APACHE": "Apache-2.0\n",
        "vendor/backend/src/lib.rs": "pub fn extract() {}\n",
    }
    for relative, content in files.items():
        path = native / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def test_vendor_inventory_includes_tracked_and_untracked_source_but_not_ignored_builds(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init", "-q")
    vendor = tmp_path / "native/vendor/backend"
    source = vendor / "src/lib.rs"
    source.parent.mkdir(parents=True)
    source.write_text("pub fn extract() {}\n", encoding="utf-8")
    (vendor / "Cargo.toml").write_text("[package]\nname='backend'\n", encoding="utf-8")
    (vendor / "LICENSE-MIT").write_text("MIT\n", encoding="utf-8")
    (vendor / ".gitignore").write_text("/target/\nCargo.lock\n", encoding="utf-8")
    _git(tmp_path, "add", "native/vendor")

    untracked = vendor / "src/new.rs"
    untracked.write_text("pub fn new_path() {}\n", encoding="utf-8")
    ignored_lock = vendor / "Cargo.lock"
    ignored_lock.write_text("generated\n", encoding="utf-8")
    ignored_target = vendor / "target/debug/build/generated.rs"
    ignored_target.parent.mkdir(parents=True)
    ignored_target.write_text("generated\n", encoding="utf-8")

    relative = {
        path.relative_to(tmp_path).as_posix()
        for path in git_visible_vendor_files(tmp_path)
    }

    assert "native/vendor/backend/src/lib.rs" in relative
    assert "native/vendor/backend/src/new.rs" in relative
    assert "native/vendor/backend/Cargo.toml" in relative
    assert "native/vendor/backend/LICENSE-MIT" in relative
    assert "native/vendor/backend/Cargo.lock" not in relative
    assert "native/vendor/backend/target/debug/build/generated.rs" not in relative


def test_vendor_inventory_fails_closed_outside_git_checkout(tmp_path: Path) -> None:
    (tmp_path / "native/vendor").mkdir(parents=True)

    with pytest.raises(SourceInventoryError, match="git source inventory failed"):
        git_visible_vendor_files(tmp_path)


@pytest.mark.parametrize("relative", NATIVE_MUTATION_CASES)
def test_modifying_each_declared_native_input_changes_digest(
    tmp_path: Path,
    relative: str,
) -> None:
    _write_native_fixture(tmp_path)
    before = native_source_digest(tmp_path)
    path = tmp_path / "native" / relative
    path.write_bytes(path.read_bytes() + b"\nmutation")

    assert native_source_digest(tmp_path) != before


def test_native_digest_ignores_target_and_unrelated_documentation(tmp_path: Path) -> None:
    _write_native_fixture(tmp_path)
    before = native_source_digest(tmp_path)
    ignored_files = {
        "target/release/generated.rs": "generated build output\n",
        "README.md": "package documentation\n",
        "vendor/backend/CHANGELOG.md": "unrelated vendor documentation\n",
        "vendor/backend/tests/not-built.rs": "test-only source\n",
    }
    for relative, content in ignored_files.items():
        path = tmp_path / "native" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    assert native_source_digest(tmp_path) == before


def test_glob_matched_file_addition_and_removal_changes_and_restores_digest(
    tmp_path: Path,
) -> None:
    _write_native_fixture(tmp_path)
    before = native_source_digest(tmp_path)
    added = tmp_path / "native/src/added.rs"
    added.write_text("pub fn added() {}\n", encoding="utf-8")

    assert native_source_digest(tmp_path) != before
    assert added in native_source_files(tmp_path)

    added.unlink()
    assert native_source_digest(tmp_path) == before
    assert added not in native_source_files(tmp_path)


def test_native_inventory_rejects_symbolic_links(tmp_path: Path) -> None:
    native = tmp_path / "native"
    native.mkdir()
    (native / "source-inventory-v1.txt").write_text(
        "clusy-native-source-inventory-v1\n"
        "source-inventory-v1.txt\n"
        "linked.rs\n",
        encoding="utf-8",
    )
    outside = tmp_path / "outside.rs"
    outside.write_text("pub fn outside() {}\n", encoding="utf-8")
    (native / "linked.rs").symlink_to(outside)

    with pytest.raises(SourceInventoryError, match="symbolic link"):
        native_source_digest(tmp_path)


def test_stale_loaded_native_digest_is_rejected(tmp_path: Path) -> None:
    _write_native_fixture(tmp_path)
    current = native_source_digest(tmp_path)

    with pytest.raises(SourceInventoryError, match="loaded native source digest mismatch"):
        verify_loaded_native_source_binding(tmp_path, packaged_digest="0" * 64)

    binding = verify_loaded_native_source_binding(
        tmp_path,
        packaged_digest=current,
    )
    assert binding["matched"] is True
    assert binding["packaged_sha256"] == current
    assert binding["current_sha256"] == current


def test_uv_cache_keys_exactly_cover_native_source_inventory() -> None:
    document = tomllib.loads((ROOT / "native/pyproject.toml").read_text(encoding="utf-8"))
    cache_keys = document["tool"]["uv"]["cache-keys"]
    cache_patterns = [entry["file"] for entry in cache_keys]

    assert len(cache_patterns) == len(set(cache_patterns))
    assert set(cache_patterns) == set(native_source_patterns(ROOT))
    assert all("target" not in Path(entry["file"]).parts for entry in cache_keys)


def test_loaded_extension_matches_current_native_source_tree() -> None:
    binding = verify_loaded_native_source_binding(ROOT)

    assert binding["matched"] is True
    assert binding["files"] == len(native_source_files(ROOT))
    assert binding["packaged_sha256"] == native_source_digest(ROOT)


def test_every_benchmark_fingerprints_helper_and_complete_vendor_inventory() -> None:
    expected_vendor = {
        path.relative_to(ROOT).as_posix()
        for path in git_visible_vendor_files(ROOT)
    }
    expected_native = {
        path.relative_to(ROOT).as_posix()
        for path in native_source_files(ROOT)
    }
    source_hashes = (
        neutral_benchmark._source_hashes(),
        wcxb_benchmark._source_hashes(),
        webmain._source_hashes(),
        webis_benchmark._source_hashes(),
        {
            path.relative_to(ROOT).as_posix(): path
            for path in webmain_fine._source_paths()
        },
    )

    assert expected_vendor
    assert expected_native
    for hashes in source_hashes:
        assert HELPER_RELATIVE in hashes
        assert expected_vendor <= hashes.keys()
        assert expected_native <= hashes.keys()
        assert not any("target" in Path(relative).parts for relative in hashes)


def test_webmain_label_guard_scans_vendor_runtime_text() -> None:
    report = webmain.scan_label_leak_guard()
    expected = {
        path.relative_to(ROOT).as_posix()
        for path in git_visible_vendor_runtime_text_files(ROOT)
    }

    assert expected
    assert expected <= set(report["scanned_files"])


def test_webmain_label_guard_rejects_vendor_source_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _git(tmp_path, "init", "-q")
    source = tmp_path / "native/vendor/backend/src/lib.rs"
    source.parent.mkdir(parents=True)
    source.write_text('const LEAK: &str = "webmainbench";\n', encoding="utf-8")
    monkeypatch.setattr(webmain, "ROOT", tmp_path)

    with pytest.raises(webmain.BenchmarkError, match="label-leak guard found"):
        webmain.scan_label_leak_guard()

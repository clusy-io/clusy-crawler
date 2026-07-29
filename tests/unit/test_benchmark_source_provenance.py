from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bench import neutral_benchmark, wcxb_benchmark, webis_benchmark
from bench import webmainbench_benchmark as webmain
from bench.source_provenance import (
    SourceInventoryError,
    git_visible_vendor_files,
    git_visible_vendor_runtime_text_files,
)

ROOT = Path(__file__).resolve().parents[2]
HELPER_RELATIVE = "bench/source_provenance.py"


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
    )


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


def test_every_benchmark_fingerprints_helper_and_complete_vendor_inventory() -> None:
    expected_vendor = {
        path.relative_to(ROOT).as_posix()
        for path in git_visible_vendor_files(ROOT)
    }
    source_hashes = (
        neutral_benchmark._source_hashes(),
        wcxb_benchmark._source_hashes(),
        webmain._source_hashes(),
        webis_benchmark._source_hashes(),
    )

    assert expected_vendor
    for hashes in source_hashes:
        assert HELPER_RELATIVE in hashes
        assert expected_vendor <= hashes.keys()
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

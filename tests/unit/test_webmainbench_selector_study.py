from __future__ import annotations

import gzip
import hashlib
import json
from typing import TYPE_CHECKING

import pytest

from bench import webmainbench_selector_study as study

if TYPE_CHECKING:
    from pathlib import Path


def _hostname_for_bucket(bucket: int) -> str:
    for suffix in range(10_000):
        hostname = f"bucket-{bucket}-{suffix}.example"
        if study._bucket(hostname) == bucket:
            return hostname
    raise AssertionError(f"could not find hostname for bucket {bucket}")


def _row(index: int, hostname: str) -> dict[str, object]:
    return {
        "track_id": f"track-{index}",
        "url": f"https://{hostname}/article/{index}",
        "html": (
            f'<html><body><main data-anno-uid="selected">Article {index}</main></body></html>'
        ),
        "main_html": f"<main>secret label {index}</main>",
        "convert_main_content": f"secret reference {index}",
        "meta": {"language": "secret-category"},
    }


def _write_fixture_dataset(path: Path) -> bytes:
    hostnames = {bucket: _hostname_for_bucket(bucket) for bucket in range(4)}
    rows = [
        _row(0, "Dev.Example."),
        _row(1, "other-dev.example"),
        _row(2, "dev.example"),
        _row(3, hostnames[0]),
        _row(4, hostnames[1]),
        _row(5, hostnames[2]),
        _row(6, hostnames[3]),
    ]
    content = b"".join(study._canonical_json_line(row) for row in rows)
    path.write_bytes(content)
    return content


def _pin_fixture(monkeypatch: pytest.MonkeyPatch, content: bytes) -> None:
    monkeypatch.setattr(study, "PINNED_DATASET_BYTES", len(content))
    monkeypatch.setattr(
        study,
        "PINNED_DATASET_SHA256",
        hashlib.sha256(content).hexdigest(),
    )
    monkeypatch.setattr(study, "PINNED_DATASET_RECORDS", 7)
    monkeypatch.setattr(study, "DEVELOPMENT_RECORDS", 2)


def _read_rows(path: Path) -> list[dict[str, object]]:
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream]


def test_export_is_label_free_disjoint_audited_and_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = tmp_path / "dataset.jsonl"
    content = _write_fixture_dataset(dataset)
    _pin_fixture(monkeypatch, content)

    first = tmp_path / "selector-study-one"
    second = tmp_path / "selector-study-two"
    manifest = study.export_selector_study(dataset, first)
    study.export_selector_study(dataset, second)

    expected_indices = {
        study.DEVELOPMENT: [0, 1],
        study.LEGACY_VALIDATION: [2, 3],
        study.REPAIR_VALIDATION: [4],
        study.SEALED_FINAL: [5, 6],
    }
    for stage, indices in expected_indices.items():
        first_path = first / study.STAGE_FILES[stage]
        second_path = second / study.STAGE_FILES[stage]
        assert first_path.read_bytes() == second_path.read_bytes()
        assert first_path.read_bytes()[4:8] == b"\x00\x00\x00\x00"
        rows = _read_rows(first_path)
        assert [row["dataset_index"] for row in rows] == indices
        for row in rows:
            assert set(row) == set(study.EXPORTED_FIELDS)
            assert not set(study.LABEL_FIELDS) & set(row)
            assert "data-anno-uid" not in row["html"]
            assert "secret label" not in row["html"]
            assert "secret reference" not in row["html"]
            assert "secret-category" not in row["html"]

    assert manifest["dataset"]["verified_before_staging_created"] is True
    assert manifest["payload_contract"]["label_fields_exported"] == []
    assert manifest["isolation_audit"]["passed"] is True
    assert manifest["isolation_audit"]["unit_kind"] == "exact_hostname"
    assert manifest["isolation_audit"]["registrable_domain_or_public_suffix_grouping_used"] is False
    assert manifest["isolation_audit"]["development_overlap"] == {
        "legacy_validation": 1,
        "repair_validation": 0,
        "sealed_final": 0,
    }
    assert all(
        overlap == 0
        for overlap in manifest["isolation_audit"][
            "post_development_stage_pairwise_overlap"
        ].values()
    )
    assert manifest["routing_records"] == {
        "bucket_0": 1,
        "bucket_1": 1,
        "bucket_2": 1,
        "bucket_3": 1,
        "canonical_development_index": 2,
        "development_hostname_overlap": 1,
    }
    assert (first / "split-manifest.json").is_file()


def test_pin_failure_happens_before_any_staging_or_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = tmp_path / "dataset.jsonl"
    content = _write_fixture_dataset(dataset)
    _pin_fixture(monkeypatch, content)
    monkeypatch.setattr(study, "PINNED_DATASET_SHA256", "0" * 64)
    output = tmp_path / "rejected"

    with pytest.raises(study.SelectorStudyError, match="SHA-256 mismatch"):
        study.export_selector_study(dataset, output)

    assert not output.exists()
    assert list(tmp_path.glob(".rejected.staging-*")) == []


def test_existing_output_is_never_overwritten(tmp_path: Path) -> None:
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "owned-by-user"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(study.SelectorStudyError, match="refusing to overwrite"):
        study.export_selector_study(tmp_path / "not-opened.jsonl", output)

    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    "artifact",
    [
        '<div data-anno-uid="leak">x</div>',
        "&lt;marked-text&gt;x&lt;/marked-text&gt;",
        "&amp;lt;marked-tail&amp;gt;x&amp;lt;/marked-tail&amp;gt;",
        "&#x3c;style id=cc-extraStyle&#x3e;x&#x3c;/style&#x3e;",
    ],
)
def test_annotation_audit_rejects_raw_and_entity_escaped_artifacts(
    artifact: str,
) -> None:
    with pytest.raises(study.SelectorStudyError, match="annotation artifact"):
        study._assert_annotation_free(artifact, context="test payload")


def test_hostname_unit_is_exact_lowercase_hostname_not_registrable_domain() -> None:
    assert (
        study._canonical_hostname(
            "https://News.Example.COM./story",
            dataset_index=0,
        )
        == "news.example.com"
    )
    assert study._canonical_hostname(
        "https://other.example.com/story",
        dataset_index=1,
    ) != study._canonical_hostname(
        "https://news.example.com/story",
        dataset_index=2,
    )

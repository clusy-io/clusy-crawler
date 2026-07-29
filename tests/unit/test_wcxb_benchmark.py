from __future__ import annotations

import gzip
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from bench import wcxb_benchmark

if TYPE_CHECKING:
    from pathlib import Path


def test_profile_cli_is_explicit_and_defaults_to_balanced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["wcxb_benchmark.py"])
    assert (
        wcxb_benchmark.parse_args().extraction_profile
        == wcxb_benchmark.DEFAULT_EXTRACTION_PROFILE
        == "balanced"
    )

    monkeypatch.setattr(
        sys,
        "argv",
        ["wcxb_benchmark.py", "--extraction-profile", "adaptive"],
    )
    assert wcxb_benchmark.parse_args().extraction_profile == "adaptive"


def test_embedded_classifier_provenance_closes_unseen_claim_gate() -> None:
    provenance = wcxb_benchmark._embedded_classifier_provenance()

    assert provenance["embedded"] is True
    assert provenance["version"] == "0.1.0"
    assert provenance["checksum"] == wcxb_benchmark.OPAQUE_CLASSIFIER_CHECKSUM
    assert provenance["publisher_reported_training_pages"] == 1497
    assert provenance["publisher_reported_page_types"] == 7
    assert provenance["training_item_or_split_manifest"] is None
    assert provenance["training_manifest_verified"] is False


@pytest.mark.asyncio
async def test_split_passes_recorded_profile_to_warmup_and_scored_calls(
    tmp_path: Path,
) -> None:
    html_path = tmp_path / "page.html.gz"
    with gzip.open(html_path, "wt", encoding="utf-8") as handle:
        handle.write("<main>profile wiring fixture</main>")

    observed_profiles: list[str] = []

    async def extractor(
        _html: str,
        _url: str,
        *,
        extraction_profile: str,
    ) -> SimpleNamespace:
        observed_profiles.append(extraction_profile)
        return SimpleNamespace(
            text="profile wiring fixture",
            strategy="fixture",
            word_count=3,
            confidence=1.0,
            page_type="article",
        )

    predictions, rows, timing = await wcxb_benchmark._extract_split(
        [
            wcxb_benchmark.PageJob(
                file_id="0001",
                url="https://example.test/",
                html_path=html_path,
            )
        ],
        split="test",
        concurrency=1,
        warmup_pages=1,
        extraction_profile="adaptive",
        extractor=extractor,
    )

    assert observed_profiles == ["adaptive", "adaptive"]
    assert predictions == {"0001": "profile wiring fixture"}
    assert rows["0001"]["error"] is None
    assert timing["concurrency"] == 1

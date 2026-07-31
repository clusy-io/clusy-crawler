from __future__ import annotations

import tomllib
from pathlib import Path

from app.main import app
from app.version import SERVICE_VERSION


def test_openapi_version_matches_project_metadata() -> None:
    project_root = Path(__file__).resolve().parents[2]
    metadata = tomllib.loads((project_root / "pyproject.toml").read_text("utf-8"))
    project_version = metadata["project"]["version"]

    assert project_version == SERVICE_VERSION
    assert app.version == project_version
    assert app.openapi()["info"]["version"] == project_version

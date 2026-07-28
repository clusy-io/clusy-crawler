from __future__ import annotations

import json

import structlog

from app.lib.logging import configure_logging


def test_json_logging_is_single_encoded_and_reconfiguration_is_idempotent(
    capsys,
    monkeypatch,
) -> None:
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    configure_logging()
    configure_logging()

    structlog.get_logger("test").info("probe", answer=42)

    lines = [line for line in capsys.readouterr().err.splitlines() if line]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "probe"
    assert payload["answer"] == 42
    assert isinstance(payload["event"], str)

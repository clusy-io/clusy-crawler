from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _security_profile() -> dict[str, Any]:
    raw_profile = (_REPOSITORY_ROOT / "seccomp_profile.json").read_text(
        encoding="utf-8"
    )
    parsed = json.loads(raw_profile)
    assert isinstance(parsed, dict)
    return parsed


def test_seccomp_profile_supports_current_container_and_browser_runtimes() -> None:
    profile = _security_profile()

    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    syscalls = profile["syscalls"]
    assert isinstance(syscalls, list)

    allowed_names = {
        name
        for rule in syscalls
        if rule.get("action") == "SCMP_ACT_ALLOW"
        for name in rule.get("names", [])
    }
    assert {"clone3", "openat2"} <= allowed_names

    namespace_rule = next(
        rule
        for rule in syscalls
        if rule.get("comment") == "Allow create user namespaces"
    )
    assert {"clone", "clone3", "setns", "unshare"} <= set(
        namespace_rule["names"]
    )

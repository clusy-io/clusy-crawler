from __future__ import annotations

import base64
import copy
import importlib
import json
import site
import subprocess
import sys
import time
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest

from app.services import atomic_structure_overlay_v0 as overlay
from bench import atomic_frozen_replay_v0 as frozen_replay
from bench import claim_worker_guard
from bench.atomic_frozen_replay_v0 import (
    FrozenReplayError,
    _canonical_json,
    _framed_digest,
    _proposal_canonical,
    bind_native_replay_primitives,
    replay_frozen_decision,
)
from bench.atomic_frozen_replay_v0 import (
    _visible_tokens as frozen_visible_tokens,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def _bind_test_native_replay_primitives() -> None:
    native = importlib.import_module("clusy_native._native")
    bind_native_replay_primitives(native)


def test_frozen_replay_has_no_unverified_native_import_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(frozen_replay, "_NATIVE_REPLAY_PRIMITIVES", None)

    with pytest.raises(FrozenReplayError, match="identity-checked.*not bound"):
        frozen_replay._native_replay_primitives()  # noqa: SLF001


def _document(body: str) -> str:
    return (
        "<!doctype html><html><head><title>overlay fixture</title></head>"
        f"<body><main>{body}</main></body></html>"
    )


def _config() -> overlay.AtomicStructureOverlayV0Config:
    return overlay.AtomicStructureOverlayV0Config(
        enabled=True,
        max_certificate_bytes=64 * 1024,
        max_total_certificate_bytes=256 * 1024,
    )


def _config_record(
    config: overlay.AtomicStructureOverlayV0Config,
) -> dict[str, Any]:
    return {
        field.name: object.__getattribute__(config, field.name)
        for field in fields(config)
    }


def _decision_worker() -> Any:
    sys.modules.setdefault("claim_guard", claim_worker_guard)
    return importlib.import_module("bench.atomic_decision_worker")


def _observation(
    html: str,
    candidate: str,
    *,
    config: overlay.AtomicStructureOverlayV0Config | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if config is None:
        config = _config()
    worker = _decision_worker()
    observation = worker._observe(  # noqa: SLF001
        html,
        candidate,
        config=config,
        proposer=overlay.propose_atomic_structure_overlay_v0,
        verifier=overlay.verify_atomic_structure_overlay_v0,
        monotonic_ns=time.monotonic_ns,
        overlay_module=overlay,
    )
    return observation, _config_record(config)


@pytest.mark.parametrize(
    ("html", "candidate", "expected_fragment"),
    [
        (
            _document(
                '<pre><code class="language-python">def answer():\n    return 42</code></pre>'
            ),
            "前缀 😀\n\ndef answer():\n    return 42\n\nsuffix café",
            "```python\ndef answer():\n    return 42\n```",
        ),
        (
            _document(
                "<table><thead><tr><th>Name</th><th>Score</th></tr></thead>"
                "<tbody><tr><td>Ada | Lovelace</td><td>10</td></tr>"
                "<tr><td>Grace</td><td>9</td></tr></tbody></table>"
            ),
            "intro\n\nName Score\nAda | Lovelace 10\nGrace 9\n\noutro",
            "| Ada \\| Lovelace | 10 |",
        ),
    ],
)
def test_frozen_replay_independently_derives_accepted_output(
    html: str,
    candidate: str,
    expected_fragment: str,
) -> None:
    observation, config = _observation(html, candidate)

    replay = replay_frozen_decision(
        html,
        candidate,
        observation,
        config=config,
    )

    assert observation["accepted"] is True
    assert expected_fragment in replay.output_markdown
    assert replay.output_markdown == observation["output_markdown"]
    assert replay.decision_digest == observation["decision_digest"]
    assert replay.output_digest == observation["output_digest"]
    assert observation["proposals"][0]["source_digest"] == observation["source_digest"]


def test_frozen_replay_accepts_table_cell_ending_in_backslash() -> None:
    html = _document(
        "<table><tr><th>Name</th><th>Value</th></tr>"
        "<tr><td>row</td><td>pipe | slash \\</td></tr></table>"
    )
    candidate = "intro padding\n\nName Value\nrow pipe | slash \\\n\noutro padding"
    observation, config = _observation(html, candidate)

    replay = replay_frozen_decision(
        html,
        candidate,
        observation,
        config=config,
    )

    assert observation["accepted"] is True
    assert "| row | pipe \\| slash \\\\ |" in replay.output_markdown


def test_frozen_replay_rejects_arbitrary_output_and_patch_payload() -> None:
    html = _document('<pre><code class="language-python">x = 1\nprint(x)</code></pre>')
    candidate = "before\n\nx = 1\nprint(x)\n\nafter"
    observation, config = _observation(html, candidate)

    hostile_output = copy.deepcopy(observation)
    hostile_output["output_markdown"] += "\nforged"
    with pytest.raises(FrozenReplayError, match="stored output"):
        replay_frozen_decision(
            html,
            candidate,
            hostile_output,
            config=config,
        )

    hostile_patch = copy.deepcopy(observation)
    hostile_patch["proposals"][0]["replacement_markdown"] += " "
    with pytest.raises(FrozenReplayError, match="certificate derivation"):
        replay_frozen_decision(
            html,
            candidate,
            hostile_patch,
            config=config,
        )

    open_schema = copy.deepcopy(observation)
    open_schema["proposals"][0]["unknown"] = True
    with pytest.raises(FrozenReplayError, match="schema is not closed"):
        replay_frozen_decision(
            html,
            candidate,
            open_schema,
            config=config,
        )


def test_frozen_replay_rejects_self_consistent_foreign_source_forgery() -> None:
    source_html = _document("<pre><code>stable\norder</code></pre>")
    foreign_html = _document("<pre><code>xxxxxx\nxxxxx</code></pre>")
    candidate = "prefix\n\nstable\norder\n\nsuffix"
    observation, config = _observation(source_html, candidate)
    assert observation["accepted"] is True
    assert len(source_html.encode()) == len(foreign_html.encode())

    forged = copy.deepcopy(observation)
    proposal = forged["proposals"][0]
    foreign_bytes = foreign_html.encode()
    overlay_source_digest = _framed_digest(
        "clusy-atomic-overlay-source-v0",
        foreign_bytes,
    )
    proposal["source_digest"] = overlay_source_digest
    start = proposal["source_span_start"]
    end = proposal["source_span_end"]
    assert type(start) is int and type(end) is int
    proposal["source_span_digest"] = _framed_digest(
        "clusy-atomic-overlay-source-span-v0",
        foreign_bytes[start:end],
    )
    certificate = bytearray(base64.b64decode(proposal["certificate_base64"]))
    certificate[12:44] = bytes.fromhex(
        _framed_digest(
            "clusy-selection-certificate-source-v0",
            foreign_bytes,
        )
    )
    proposal["certificate_base64"] = base64.b64encode(certificate).decode("ascii")
    proposal["certificate_digest"] = _framed_digest(
        "clusy-selection-certificate-wire-v0",
        bytes(certificate),
    )
    proposal["proposal_id"] = _framed_digest(
        "clusy-atomic-overlay-proposal-v0",
        _canonical_json(_proposal_canonical(proposal)),
    )
    forged["source_digest"] = overlay_source_digest
    forged["applied_proposal_ids"] = [proposal["proposal_id"]]
    decision_canonical = {
        "accepted": forged["accepted"],
        "applied_proposal_ids": tuple(forged["applied_proposal_ids"]),
        "config_digest": forged["config_digest"],
        "enabled": forged["enabled"],
        "growth_bytes": forged["growth_bytes"],
        "input_bytes": forged["input_bytes"],
        "input_digest": forged["input_digest"],
        "output_bytes": forged["output_bytes"],
        "output_digest": forged["output_digest"],
        "proposal_ids": (proposal["proposal_id"],),
        "reason": forged["reason"],
        "source_digest": forged["source_digest"],
        "visible_token_digest": forged["visible_token_digest"],
        "visible_tokens_identical": forged["visible_tokens_identical"],
    }
    decision_digest = _framed_digest(
        "clusy-atomic-overlay-decision-v0",
        _canonical_json(decision_canonical),
    )
    forged["decision_digest"] = decision_digest
    forged["replay"]["decision_digest"] = decision_digest

    with pytest.raises(FrozenReplayError, match="raw native replay"):
        replay_frozen_decision(
            foreign_html,
            candidate,
            forged,
            config=config,
        )


def test_frozen_replay_rejects_selective_suppression_of_safe_proposal() -> None:
    html = _document("<pre><code>stable\norder</code></pre>")
    candidate = "prefix\n\nstable\norder\n\nsuffix"
    observation, config = _observation(html, candidate)
    assert observation["accepted"] is True

    forged = copy.deepcopy(observation)
    proposal = forged["proposals"][0]
    proposal.update(
        {
            "accepted": False,
            "candidate_span_end": None,
            "candidate_span_start": None,
            "certificate_base64": "",
            "certificate_digest": "",
            "certificate_markdown": None,
            "graph_digest": "",
            "growth_bytes": 0,
            "patch_digest": "",
            "proposed_output_bytes": 0,
            "reason": "ambiguous_or_missing_candidate_span",
            "replacement_bytes": 0,
            "replacement_digest": "",
            "replacement_markdown": None,
            "structural_score_after": 0,
            "structural_score_before": 0,
            "visible_token_count": 0,
            "visible_token_digest": "",
        }
    )
    proposal["proposal_id"] = _framed_digest(
        "clusy-atomic-overlay-proposal-v0",
        _canonical_json(_proposal_canonical(proposal)),
    )
    candidate_bytes = candidate.encode()
    output_digest = _framed_digest(
        "clusy-atomic-overlay-output-v0",
        candidate_bytes,
    )
    forged.update(
        {
            "accepted": False,
            "applied_proposal_ids": [],
            "growth_bytes": 0,
            "output_bytes": len(candidate_bytes),
            "output_digest": output_digest,
            "output_markdown": candidate,
            "reason": "no_safe_structural_gain",
        }
    )
    decision_canonical = {
        "accepted": False,
        "applied_proposal_ids": (),
        "config_digest": forged["config_digest"],
        "enabled": True,
        "growth_bytes": 0,
        "input_bytes": forged["input_bytes"],
        "input_digest": forged["input_digest"],
        "output_bytes": len(candidate_bytes),
        "output_digest": output_digest,
        "proposal_ids": (proposal["proposal_id"],),
        "reason": "no_safe_structural_gain",
        "source_digest": forged["source_digest"],
        "visible_token_digest": forged["visible_token_digest"],
        "visible_tokens_identical": True,
    }
    decision_digest = _framed_digest(
        "clusy-atomic-overlay-decision-v0",
        _canonical_json(decision_canonical),
    )
    forged["decision_digest"] = decision_digest
    forged["replay"] = {
        "decision_digest": decision_digest,
        "output_digest": output_digest,
        "reason": "verified",
        "verified": True,
    }

    with pytest.raises(FrozenReplayError, match="raw native replay"):
        replay_frozen_decision(
            html,
            candidate,
            forged,
            config=config,
        )


def test_frozen_replay_derives_total_certificate_budget_across_atoms() -> None:
    html = _document(
        "<pre><code>alpha one\nbeta one</code></pre>"
        "<pre><code>alpha two\nbeta two</code></pre>"
    )
    candidate = (
        "prefix padding padding padding\n\n"
        "alpha one\nbeta one\n\n"
        "middle padding padding padding\n\n"
        "alpha two\nbeta two\n\n"
        "suffix padding padding padding"
    )
    observation, config = _observation(
        html,
        candidate,
        config=overlay.AtomicStructureOverlayV0Config(
            enabled=True,
            max_certificate_bytes=200,
            max_total_certificate_bytes=200,
        ),
    )

    replay = replay_frozen_decision(
        html,
        candidate,
        observation,
        config=config,
    )

    assert observation["accepted"] is True
    assert [proposal["reason"] for proposal in observation["proposals"]] == [
        "accepted",
        "total_certificate_byte_budget",
    ]
    assert len(replay.applied_proposal_ids) == 1


@pytest.mark.parametrize(
    ("html", "candidate", "proposal_reasons"),
    [
        (
            _document("<aside><pre><code>a\nb</code></pre></aside>"),
            "before\n\na\nb\n\nafter",
            ["untrusted_landmark"],
        ),
        (
            _document(
                "<pre><code>a\nb</code></pre><pre><code>a\nb</code></pre>"
            ),
            "before\n\na\nb\n\nafter",
            ["ambiguous_source_tokens", "ambiguous_source_tokens"],
        ),
        (
            _document("<pre><code>a\nb</code></pre>"),
            "a\nb\n\nmiddle\n\na\nb",
            ["ambiguous_or_missing_candidate_span"],
        ),
        (
            _document("<pre><code>a\nb</code></pre>"),
            "```\na\nb\n```",
            ["ambiguous_or_missing_candidate_span"],
        ),
        (
            _document(
                "<table><tr><td>A</td><td>B</td></tr>"
                "<tr><td>1</td><td>2</td></tr></table>"
            ),
            "A B\n1 2",
            ["layout_table_without_header"],
        ),
        (
            _document("<p>a b</p>"),
            "a b",
            [],
        ),
    ],
)
def test_frozen_replay_rederives_rejected_inventory_and_gates(
    html: str,
    candidate: str,
    proposal_reasons: list[str],
) -> None:
    observation, config = _observation(html, candidate)

    replay = replay_frozen_decision(
        html,
        candidate,
        observation,
        config=config,
    )

    assert observation["accepted"] is False
    assert observation["reason"] == "no_safe_structural_gain"
    assert [
        proposal["reason"] for proposal in observation["proposals"]
    ] == proposal_reasons
    assert replay.output_markdown == candidate


def test_frozen_replay_fails_closed_on_wrong_types_for_every_closed_field() -> None:
    html = _document("<pre><code>x = 1\nprint(x)</code></pre>")
    candidate = "before\n\nx = 1\nprint(x)\n\nafter"
    observation, config = _observation(html, candidate)

    for key in observation:
        hostile = copy.deepcopy(observation)
        hostile[key] = []
        with pytest.raises(FrozenReplayError):
            replay_frozen_decision(
                html,
                candidate,
                hostile,
                config=config,
            )
    for key in observation["proposals"][0]:
        hostile = copy.deepcopy(observation)
        hostile["proposals"][0][key] = []
        with pytest.raises(FrozenReplayError):
            replay_frozen_decision(
                html,
                candidate,
                hostile,
                config=config,
            )


def test_frozen_replay_derives_rejected_output_byte_for_byte() -> None:
    html = _document("<pre><code>alpha\nbeta</code></pre>")
    candidate = "```\nalpha\nbeta\n```"
    observation, config = _observation(html, candidate)

    replay = replay_frozen_decision(
        html,
        candidate,
        observation,
        config=config,
    )

    assert observation["accepted"] is False
    assert replay.output_markdown == candidate
    assert replay.output_digest == observation["output_digest"]
    assert all(
        proposal["certificate_base64"] == ""
        and proposal["replacement_markdown"] is None
        and proposal["patch_digest"] == ""
        for proposal in observation["proposals"]
    )


def test_final_rejection_strips_unreplayable_patch_payload() -> None:
    html = _document("<pre><code>alpha\nbeta</code></pre>")
    candidate = "prefix\n\nalpha\nbeta\n\nsuffix"
    config = _config()
    accepted = overlay.propose_atomic_structure_overlay_v0(
        html,
        candidate,
        config=config,
    )
    assert accepted.accepted

    rejected = overlay._global_rejection(  # noqa: SLF001
        candidate,
        list(accepted.proposals),
        accepted.source_digest,
        accepted.input_digest,
        accepted.config_digest,
        "global_growth_budget",
    )

    proposal = rejected.proposals[0]
    assert proposal.accepted is False
    assert proposal.candidate_span_start is None
    assert proposal.candidate_span_end is None
    assert proposal.certificate == b""
    assert proposal.graph_digest == ""
    assert proposal.replacement_digest == ""
    assert proposal.patch_digest == ""
    assert proposal.visible_token_digest == ""
    assert proposal.certificate_digest == ""
    assert proposal.replacement_bytes == 0
    assert proposal.visible_token_count == 0


@pytest.mark.parametrize(
    "value",
    [
        "plain  text\nwith whitespace",
        "# heading\n\n- list item\n\n> quote",
        "```python\nprint('x')\n```",
        "| A | B |\n| --- | --- |\n| 1 | 2 |",
        r"escaped \\*literal\\* and [link](https://invalid.example)",
        "<strong>markup</strong> &amp; entity",
    ],
)
def test_frozen_visible_token_contract_matches_candidate(value: str) -> None:
    assert frozen_visible_tokens(
        value, 20_000
    ) == overlay._visible_tokens(  # noqa: SLF001
        value,
        20_000,
    )


def test_overlay_digest_is_stable_with_randomized_hash_order() -> None:
    site_roots = site.getsitepackages()
    assert site_roots
    html = _document("<pre><code>stable\norder</code></pre>")
    source = f"""
import json, sys
sys.path[:0] = [{str(ROOT)!r}, {site_roots[0]!r}]
from app.services.atomic_structure_overlay_v0 import (
    AtomicStructureOverlayV0Config,
    propose_atomic_structure_overlay_v0,
)
html = {html!r}
candidate = "prefix\\n\\nstable\\norder\\n\\nsuffix"
decision = propose_atomic_structure_overlay_v0(
    html,
    candidate,
    config=AtomicStructureOverlayV0Config(
        enabled=True,
        max_certificate_bytes=65536,
        max_total_certificate_bytes=262144,
    ),
)
print(json.dumps({{
    "decision_digest": decision.decision_digest,
    "hash_randomization": sys.flags.hash_randomization,
    "output_markdown": decision.output_markdown,
    "proposal_ids": [item.proposal_id for item in decision.proposals],
}}, sort_keys=True, separators=(",", ":")))
"""
    observations = []
    for _ in range(3):
        completed = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", source],
            check=True,
            capture_output=True,
            text=True,
        )
        observations.append(json.loads(completed.stdout))

    assert all(item["hash_randomization"] == 1 for item in observations)
    assert observations[1:] == observations[:-1]

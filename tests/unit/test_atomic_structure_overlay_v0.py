from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from app.services import atomic_structure_overlay_v0 as overlay_module
from app.services.atomic_structure_overlay_v0 import (
    ATOMIC_STRUCTURE_OVERLAY_V0_SCHEMA,
    DEFAULT_ATOMIC_STRUCTURE_OVERLAY_V0_CONFIG,
    AtomicStructureOverlayV0Config,
    propose_atomic_structure_overlay_v0,
    verify_atomic_structure_overlay_v0,
)


def _document(body: str) -> str:
    return (
        "<!doctype html><html><head><title>overlay fixture</title></head>"
        f"<body><main>{body}</main></body></html>"
    )


def _enabled(**overrides: object) -> AtomicStructureOverlayV0Config:
    return AtomicStructureOverlayV0Config(enabled=True, **overrides)  # type: ignore[arg-type]


def test_default_is_disabled_unwired_and_byte_identical() -> None:
    html = _document("<pre><code>print('safe')</code></pre>")
    candidate = "prefix\r\nprint('safe')\r\nsuffix"

    decision = propose_atomic_structure_overlay_v0(html, candidate)

    assert not DEFAULT_ATOMIC_STRUCTURE_OVERLAY_V0_CONFIG.enabled
    assert decision.schema_version == ATOMIC_STRUCTURE_OVERLAY_V0_SCHEMA
    assert not decision.enabled
    assert not decision.accepted
    assert decision.reason == "disabled"
    assert decision.output_markdown.encode() == candidate.encode()
    assert not decision.digest_is_authentication


def test_code_overlay_is_source_certified_local_and_replayable() -> None:
    html = _document(
        '<pre><code class="language-python">def answer():\n    return 42</code></pre>'
    )
    candidate = "前缀 😀\n\ndef answer():\n    return 42\n\nsuffix café"
    timings: list[tuple[str, int]] = []

    decision = propose_atomic_structure_overlay_v0(
        html,
        candidate,
        config=_enabled(),
        timing_hook=lambda stage, elapsed: timings.append((stage, elapsed)),
    )

    assert decision.accepted
    assert decision.reason == "accepted"
    assert decision.visible_tokens_identical
    assert len(decision.proposals) == 1
    proposal = decision.proposals[0]
    assert proposal.accepted
    assert proposal.atom_kind == "code"
    assert proposal.structural_score_after > proposal.structural_score_before
    assert proposal.certificate
    assert len(proposal.source_digest) == 64
    assert len(proposal.graph_digest) == 64
    assert len(proposal.source_span_digest) == 64
    assert len(proposal.input_digest) == 64
    assert len(proposal.replacement_digest) == 64
    assert len(proposal.patch_digest) == 64
    assert len(proposal.config_digest) == 64
    assert not proposal.digest_is_authentication
    assert "```python\ndef answer():\n    return 42\n```" in decision.output_markdown
    assert timings and all(elapsed >= 0 for _, elapsed in timings)

    start = proposal.candidate_span_start
    end = proposal.candidate_span_end
    assert start is not None and end is not None
    candidate_bytes = candidate.encode()
    output_bytes = decision.output_markdown.encode()
    assert output_bytes[:start] == candidate_bytes[:start]
    assert (
        output_bytes[start + proposal.replacement_bytes :]
        == candidate_bytes[end:]
    )

    replay = verify_atomic_structure_overlay_v0(
        html,
        candidate,
        decision,
        config=_enabled(),
    )
    assert replay.verified
    assert replay.reason == "verified"
    assert replay.output_markdown == decision.output_markdown
    assert replay.decision_digest == decision.decision_digest
    assert not replay.digest_is_authentication


def test_simple_rectangular_data_table_gets_gfm_structure() -> None:
    html = _document(
        "<table><thead><tr><th>Name</th><th>Score</th></tr></thead>"
        "<tbody><tr><td>Ada | Lovelace</td><td>10</td></tr>"
        "<tr><td>Grace</td><td>9</td></tr></tbody></table>"
    )
    candidate = "intro\n\nName Score\nAda | Lovelace 10\nGrace 9\n\noutro"

    decision = propose_atomic_structure_overlay_v0(html, candidate, config=_enabled())

    assert decision.accepted
    assert len(decision.proposals) == 1
    proposal = decision.proposals[0]
    assert proposal.atom_kind == "table"
    assert proposal.accepted
    assert "| Name | Score |" in decision.output_markdown
    assert "| --- | --- |" in decision.output_markdown
    assert "| Ada \\| Lovelace | 10 |" in decision.output_markdown
    assert decision.output_markdown.startswith("intro\n\n")
    assert decision.output_markdown.endswith("\n\noutro")


@pytest.mark.parametrize(
    ("html", "candidate", "expected_reason"),
    [
        (
            _document("<pre><code>a &amp; b</code></pre>"),
            "a & b",
            "certificate_provenance_rejected",
        ),
        (
            _document("<nav><pre><code>hidden nav code</code></pre></nav>"),
            "hidden nav code",
            "untrusted_landmark",
        ),
        (
            _document(
                "<table><tbody><tr><td>A</td><td>B</td></tr>"
                "<tr><td>C</td><td>D</td></tr></tbody></table>"
            ),
            "A B\nC D",
            "layout_table_without_header",
        ),
        (
            _document("<pre><code>line\rnext</code></pre>"),
            "line\nnext",
            "noncanonical_source_control",
        ),
        (
            _document("<pre><code>line\x00next</code></pre>"),
            "linenext",
            "noncanonical_source_control",
        ),
    ],
)
def test_ambiguity_landmark_layout_entity_and_control_inputs_fail_closed(
    html: str,
    candidate: str,
    expected_reason: str,
) -> None:
    decision = propose_atomic_structure_overlay_v0(html, candidate, config=_enabled())

    assert not decision.accepted
    assert decision.output_markdown.encode() == candidate.encode()
    assert decision.reason == expected_reason or any(
        proposal.reason == expected_reason for proposal in decision.proposals
    )


def test_duplicate_source_or_candidate_text_is_ambiguous() -> None:
    duplicate_source = _document(
        "<pre><code>same exact code</code></pre>"
        "<p>between</p>"
        "<pre><code>same exact code</code></pre>"
    )
    source_decision = propose_atomic_structure_overlay_v0(
        duplicate_source,
        "same exact code",
        config=_enabled(),
    )
    assert not source_decision.accepted
    assert source_decision.output_markdown == "same exact code"
    assert {
        proposal.reason for proposal in source_decision.proposals
    } == {"ambiguous_source_tokens"}

    duplicate_candidate = "same exact code\n\nother\n\nsame exact code"
    candidate_decision = propose_atomic_structure_overlay_v0(
        _document("<pre><code>same exact code</code></pre>"),
        duplicate_candidate,
        config=_enabled(),
    )
    assert not candidate_decision.accepted
    assert candidate_decision.output_markdown == duplicate_candidate
    assert candidate_decision.proposals[0].reason == "ambiguous_or_missing_candidate_span"


def test_nested_tables_are_rejected() -> None:
    nested = _document(
        "<table><thead><tr><th>A</th><th>B</th></tr></thead><tbody><tr>"
        "<td><table><thead><tr><th>X</th><th>Y</th></tr></thead>"
        "<tbody><tr><td>1</td><td>2</td></tr></tbody></table></td>"
        "<td>Z</td></tr></tbody></table>"
    )
    nested_decision = propose_atomic_structure_overlay_v0(
        nested,
        "A B\nX Y\n1 2 Z",
        config=_enabled(),
    )
    assert not nested_decision.accepted
    assert nested_decision.output_markdown == "A B\nX Y\n1 2 Z"
    assert {
        proposal.reason for proposal in nested_decision.proposals
    } <= {"complex_table_descendant", "nested_atomic_structure"}

def test_standard_implicit_tbody_is_proven_but_other_repairs_are_rejected() -> None:
    implicit_tbody = _document(
        "<table><tr><th>A</th><th>B</th></tr>"
        "<tr><td>1</td><td>2</td></tr></table>"
    )
    accepted = propose_atomic_structure_overlay_v0(
        implicit_tbody,
        "A      B\n1      2",
        config=_enabled(),
    )
    assert accepted.accepted
    assert "| A | B |" in accepted.output_markdown
    assert "| 1 | 2 |" in accepted.output_markdown

    repaired_cells = _document("<table><td>A</td><td>B</td></table>")
    rejected = propose_atomic_structure_overlay_v0(
        repaired_cells,
        "A B",
        config=_enabled(),
    )
    assert not rejected.accepted
    assert rejected.output_markdown == "A B"
    assert rejected.proposals[0].reason in {
        "parser_repaired_atom",
        "layout_table_without_header",
        "certificate_provenance_rejected",
        "unreliable_source_span",
    }


def test_fence_collision_uses_a_longer_source_backed_fence() -> None:
    code = 'const ticks = "```";\nreturn ticks;'
    html = _document(f'<pre><code class="language-js">{code}</code></pre>')

    decision = propose_atomic_structure_overlay_v0(html, code, config=_enabled())

    assert decision.accepted
    assert decision.output_markdown.startswith("````js\n")
    assert decision.output_markdown.endswith("\n````")
    assert code in decision.output_markdown


def test_unrelated_parse_error_does_not_block_well_formed_local_atom() -> None:
    html = _document(
        "<div><span>broken outside</div>"
        '<pre><code class="language-python">def locally_safe():\n  return 7</code></pre>'
    )
    candidate = "prefix\n\ndef locally_safe(): return 7\n\nsuffix"

    decision = propose_atomic_structure_overlay_v0(html, candidate, config=_enabled())

    assert decision.accepted
    assert "```python\ndef locally_safe():\n  return 7\n```" in decision.output_markdown
    assert decision.visible_tokens_identical


def test_visible_markdown_index_avoids_existing_structure_and_link_destinations() -> None:
    code_html = _document("<pre><code>unique safe code</code></pre>")

    fenced = "before\n\n```\nunique safe code\n```\n\nafter"
    fenced_decision = propose_atomic_structure_overlay_v0(
        code_html,
        fenced,
        config=_enabled(),
    )
    assert not fenced_decision.accepted
    assert fenced_decision.output_markdown == fenced

    neighboring_structure = (
        "# heading neighbor\n"
        "- list neighbor\n"
        "[destination-only duplicate](unique safe code)\n\n"
        "unique safe code"
    )
    neighboring_decision = propose_atomic_structure_overlay_v0(
        code_html,
        neighboring_structure,
        config=_enabled(),
    )
    assert neighboring_decision.accepted
    assert neighboring_decision.output_markdown.startswith(
        "# heading neighbor\n- list neighbor\n"
        "[destination-only duplicate](unique safe code)\n\n"
    )
    assert neighboring_decision.output_markdown.endswith(
        "```\nunique safe code\n```"
    )

    link_label_duplicate = "[unique safe code](https://example.test)\n\nunique safe code"
    duplicate_decision = propose_atomic_structure_overlay_v0(
        code_html,
        link_label_duplicate,
        config=_enabled(),
    )
    assert not duplicate_decision.accepted
    assert duplicate_decision.output_markdown == link_label_duplicate


def test_already_gfm_table_is_never_rewritten() -> None:
    html = _document(
        "<table><thead><tr><th>A</th><th>B</th></tr></thead>"
        "<tbody><tr><td>1</td><td>2</td></tr></tbody></table>"
    )
    candidate = "| A | B |\n| --- | --- |\n| 1 | 2 |"

    decision = propose_atomic_structure_overlay_v0(html, candidate, config=_enabled())

    assert not decision.accepted
    assert decision.output_markdown == candidate
    assert decision.visible_tokens_identical


def test_plain_rectangular_pipe_rows_gain_gfm_separator_without_token_change() -> None:
    html = _document(
        "<table><thead><tr><th>Method</th><th>Score</th></tr></thead>"
        "<tbody><tr><td>Alpha</td><td>10</td></tr>"
        "<tr><td>Beta</td><td>9</td></tr></tbody></table>"
    )
    candidate = "Method | Score\nAlpha | 10\nBeta | 9"

    decision = propose_atomic_structure_overlay_v0(html, candidate, config=_enabled())

    assert decision.accepted
    assert decision.visible_tokens_identical
    assert decision.output_markdown == (
        "| Method | Score |\n"
        "| --- | --- |\n"
        "| Alpha | 10 |\n"
        "| Beta | 9 |"
    )


def test_many_atoms_build_candidate_and_token_indexes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atom_count = 48
    html = _document(
        "".join(
            f"<pre><code>unique code atom {index}</code></pre>"
            for index in range(atom_count)
        )
    )
    candidate = "\n\n".join(
        f"unique code atom {index}" for index in range(atom_count)
    )
    calls = {"plan": 0, "spans": 0, "positions": 0}
    real_plan = overlay_module._markdown_visibility_plan
    real_spans = overlay_module._raw_token_spans
    real_positions = overlay_module._token_position_index

    def counted_plan(value: str) -> object:
        calls["plan"] += 1
        return real_plan(value)

    def counted_spans(
        value: str,
        maximum: int,
        *,
        offset_source: str | None = None,
    ) -> object:
        calls["spans"] += 1
        return real_spans(value, maximum, offset_source=offset_source)

    def counted_positions(tokens: tuple[str, ...]) -> object:
        calls["positions"] += 1
        return real_positions(tokens)

    monkeypatch.setattr(overlay_module, "_markdown_visibility_plan", counted_plan)
    monkeypatch.setattr(overlay_module, "_raw_token_spans", counted_spans)
    monkeypatch.setattr(overlay_module, "_token_position_index", counted_positions)

    decision = propose_atomic_structure_overlay_v0(
        html,
        candidate,
        config=_enabled(max_atoms=atom_count),
    )

    assert decision.accepted
    assert len(decision.applied_proposal_ids) == atom_count
    assert calls == {"plan": 1, "spans": 1, "positions": 2}


def test_total_certificate_budget_stops_and_strips_later_proposals() -> None:
    probe = propose_atomic_structure_overlay_v0(
        _document("<pre><code>certificate budget probe</code></pre>"),
        "certificate budget probe",
        config=_enabled(),
    )
    assert probe.accepted
    certificate_bytes = len(probe.proposals[0].certificate)
    assert certificate_bytes > 0

    decision = propose_atomic_structure_overlay_v0(
        _document(
            "<pre><code>certificate budget first</code></pre>"
            "<pre><code>certificate budget second</code></pre>"
        ),
        "certificate budget first\n\ncertificate budget second",
        config=_enabled(
            max_certificate_bytes=certificate_bytes,
            max_total_certificate_bytes=certificate_bytes,
        ),
    )

    assert decision.accepted
    assert decision.proposals[0].accepted
    assert decision.proposals[1].reason == "total_certificate_byte_budget"
    assert decision.proposals[1].certificate == b""
    assert sum(len(proposal.certificate) for proposal in decision.proposals) <= (
        certificate_bytes
    )


def test_resource_caps_and_malformed_html_fall_back_byte_for_byte() -> None:
    candidate = "x" * 128
    byte_limited = propose_atomic_structure_overlay_v0(
        _document("<pre><code>x</code></pre>"),
        candidate,
        config=_enabled(max_candidate_bytes=64),
    )
    assert byte_limited.reason == "candidate_byte_budget"
    assert byte_limited.output_markdown.encode() == candidate.encode()

    atom_limited = propose_atomic_structure_overlay_v0(
        _document(
            "<pre><code>first code</code></pre><pre><code>second code</code></pre>"
        ),
        "first code\n\nsecond code",
        config=_enabled(max_atoms=1),
    )
    assert atom_limited.reason == "atom_budget"
    assert atom_limited.output_markdown == "first code\n\nsecond code"

    growth_limited = propose_atomic_structure_overlay_v0(
        _document("<pre><code>safe code</code></pre>"),
        "safe code",
        config=_enabled(max_growth_bytes=1),
    )
    assert not growth_limited.accepted
    assert growth_limited.proposals[0].reason == "local_growth_budget"
    assert growth_limited.output_markdown == "safe code"

    malformed_candidate = "broken"
    malformed = propose_atomic_structure_overlay_v0(
        _document("<pre><code>broken</pre>"),
        malformed_candidate,
        config=_enabled(),
    )
    assert not malformed.accepted
    assert malformed.output_markdown.encode() == malformed_candidate.encode()
    assert malformed.reason == "no_safe_structural_gain"
    assert malformed.proposals[0].reason == "certificate_provenance_rejected"


def test_hostile_hook_record_and_tamper_cannot_change_fallback() -> None:
    html = _document("<pre><code>safe replay</code></pre>")
    candidate = "safe replay"
    config = _enabled()

    def hostile_hook(stage: str, elapsed: int) -> None:
        del stage, elapsed
        raise RuntimeError("timing must be observational")

    expected = propose_atomic_structure_overlay_v0(html, candidate, config=config)
    observed = propose_atomic_structure_overlay_v0(
        html,
        candidate,
        config=config,
        timing_hook=hostile_hook,
    )
    assert observed == expected

    class Hostile:
        def __getattribute__(self, name: str) -> object:
            raise AssertionError(name)

    hostile_replay = verify_atomic_structure_overlay_v0(
        html,
        candidate,
        Hostile(),
        config=config,
    )
    assert not hostile_replay.verified
    assert hostile_replay.reason == "invalid_decision_type"
    assert hostile_replay.output_markdown == candidate

    hostile_field = replace(expected, reason=Hostile())  # type: ignore[arg-type]
    hostile_field_replay = verify_atomic_structure_overlay_v0(
        html,
        candidate,
        hostile_field,
        config=config,
    )
    assert not hostile_field_replay.verified
    assert hostile_field_replay.reason == "decision_record_budget"

    hostile_proposal = replace(  # type: ignore[arg-type]
        expected.proposals[0],
        atom_kind=Hostile(),
    )
    hostile_proposal_replay = verify_atomic_structure_overlay_v0(
        html,
        candidate,
        replace(expected, proposals=(hostile_proposal,)),
        config=config,
    )
    assert not hostile_proposal_replay.verified
    assert hostile_proposal_replay.reason == "decision_record_budget"

    tampered = replace(expected, output_markdown="forged")
    tampered_replay = verify_atomic_structure_overlay_v0(
        html,
        candidate,
        tampered,
        config=config,
    )
    assert not tampered_replay.verified
    assert tampered_replay.reason == "decision_mismatch"
    assert tampered_replay.output_markdown.encode() == candidate.encode()

    oversized_proposal = replace(
        expected.proposals[0],
        certificate=b"x" * (config.max_certificate_bytes + 1),
    )
    oversized = replace(expected, proposals=(oversized_proposal,))
    oversized_replay = verify_atomic_structure_overlay_v0(
        html,
        candidate,
        oversized,
        config=config,
    )
    assert not oversized_replay.verified
    assert oversized_replay.reason == "decision_record_budget"
    assert oversized_replay.output_markdown == candidate


def test_decisions_are_deterministic_in_parallel_and_config_bound() -> None:
    html = _document("<pre><code>parallel deterministic code</code></pre>")
    candidate = "parallel deterministic code"
    config = _enabled()

    with ThreadPoolExecutor(max_workers=4) as pool:
        decisions = list(
            pool.map(
                lambda _: propose_atomic_structure_overlay_v0(
                    html,
                    candidate,
                    config=config,
                ),
                range(12),
            )
        )

    assert len({decision.decision_digest for decision in decisions}) == 1
    assert all(decision == decisions[0] for decision in decisions)
    changed_config = _enabled(max_growth_bytes=config.max_growth_bytes - 1)
    wrong_config_replay = verify_atomic_structure_overlay_v0(
        html,
        candidate,
        decisions[0],
        config=changed_config,
    )
    assert not wrong_config_replay.verified
    assert wrong_config_replay.output_markdown == candidate


@pytest.mark.parametrize(
    "candidate",
    [
        "",
        "same same",
        "&amp;",
        "```python\nunterminated",
        "\r\n\x00\r\n",
        "A | B\n--- | ---",
        "你好 你好",
    ],
)
def test_rejection_property_always_preserves_exact_input_bytes(candidate: str) -> None:
    html = _document("<nav><pre><code>unrelated unique code</code></pre></nav>")

    decision = propose_atomic_structure_overlay_v0(html, candidate, config=_enabled())

    assert not decision.accepted
    assert decision.output_markdown.encode("utf-8") == candidate.encode("utf-8")

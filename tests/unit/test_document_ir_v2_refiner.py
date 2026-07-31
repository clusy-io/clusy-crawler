from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from clusy_native import DocumentIRV2Limits

from app.services.document_ir_v2_refiner import (
    DeterministicRefinerLimits,
    refine_deterministic_candidate_v2,
)


def test_refiner_accepts_only_when_it_adds_source_backed_table_structure() -> None:
    html = """
    <main>
      <h1>Quarterly report</h1>
      <p>Revenue remained stable.</p>
      <table>
        <tr><th>Name</th><th>Score</th></tr>
        <tr><td>Alice</td><td>9</td></tr>
      </table>
    </main>
    """
    candidate = "# Quarterly report\n\nRevenue remained stable.\n\nName Score Alice 9"

    result = refine_deterministic_candidate_v2(html, candidate)

    assert result.accepted
    assert result.reason == "accepted"
    assert result.output_markdown == result.refined_markdown
    assert result.output_markdown != candidate
    assert "<table>" in result.output_markdown
    assert result.candidate_agreement == 1.0
    assert result.source_grounding_agreement == 1.0
    assert result.trusted_prose_non_shrink
    assert result.candidate_structures == ("heading",)
    assert result.added_structures == ("table",)
    assert not result.lost_structures
    assert result.ir_complete
    assert result.reconstruction_complete


def test_refiner_promotes_formula_and_exact_preformatted_code() -> None:
    html = """
    <main>
      <p>Equation follows.</p>
      <math display="block">
        <semantics><msup><mi>x</mi><mn>2</mn></msup>
          <annotation encoding="application/x-tex">x^2</annotation>
        </semantics>
      </math>
      <pre><code class="language-python">def answer():
    return 42</code></pre>
    </main>
    """
    candidate = "Equation follows. x 2 def answer return 42"

    result = refine_deterministic_candidate_v2(html, candidate)

    assert result.accepted
    assert {"math", "code"} <= set(result.added_structures)
    assert "$$\nx^2\n$$" in result.output_markdown
    assert "```python\ndef answer():\n    return 42\n```" in result.output_markdown
    assert result.source_grounding_agreement == 1.0


def test_refiner_promotes_ordered_list_semantics() -> None:
    html = "<main><p>Steps</p><ol start=3><li>install package</li><li>run checks</li></ol></main>"
    candidate = "Steps install package run checks"

    result = refine_deterministic_candidate_v2(html, candidate)

    assert result.accepted
    assert result.added_structures == ("list",)
    assert "3. install package" in result.output_markdown
    assert "4. run checks" in result.output_markdown


def test_nav_repetition_is_rejected_without_changing_candidate() -> None:
    html = """
    <body>
      <nav><a href="/guide">Install guide</a></nav>
      <main><h1>Install guide</h1><p>Configure project settings carefully.</p></main>
    </body>
    """
    candidate = "Install guide Configure project settings carefully."

    result = refine_deterministic_candidate_v2(html, candidate)

    assert not result.accepted
    assert result.reason == "untrusted_landmark_alignment"
    assert result.output_markdown == candidate
    assert result.refined_markdown is None
    assert result.output_digest == result.candidate_digest


def test_duplicate_source_alignment_is_ambiguous_and_fails_closed() -> None:
    phrase = "Repeated unique phrase with enough context"
    html = f"<main><p>{phrase}</p><section><p>{phrase}</p></section></main>"

    result = refine_deterministic_candidate_v2(html, phrase)

    assert not result.accepted
    assert result.reason == "ambiguous_source_alignment"
    assert result.alternative_agreement == result.candidate_agreement == 1.0
    assert result.output_markdown == phrase


def test_reordered_candidate_is_rejected_even_when_bag_overlap_is_complete() -> None:
    html = """
    <main>
      <p>alpha one two three</p>
      <p>beta four five six</p>
      <p>gamma seven eight nine</p>
    </main>
    """
    candidate = "alpha one two three gamma seven eight nine beta four five six"

    result = refine_deterministic_candidate_v2(html, candidate)

    assert not result.accepted
    assert result.reason == "candidate_order_mismatch"
    assert result.candidate_agreement >= 0.65
    assert result.candidate_bag_agreement == 1.0
    assert result.candidate_order_gap > result.limits.max_candidate_order_gap
    assert result.output_markdown == candidate


def test_existing_structure_cannot_be_lost_and_noop_refinement_is_rejected() -> None:
    html = "<main><p>plain source paragraph has enough words</p></main>"
    structured_candidate = "```text\nplain source paragraph has enough words\n```"

    lost = refine_deterministic_candidate_v2(html, structured_candidate)
    noop = refine_deterministic_candidate_v2(
        "<main><h2>Already structured heading</h2></main>",
        "## Already structured heading",
    )

    assert not lost.accepted
    assert lost.reason == "candidate_structure_loss"
    assert lost.output_markdown == structured_candidate
    assert lost.lost_structures == ("code",)
    assert not noop.accepted
    assert noop.reason == "no_missing_structure_added"
    assert noop.output_markdown == "## Already structured heading"


def test_truncated_ir_and_large_source_bounds_reject_unchanged() -> None:
    candidate = "bounded candidate text remains unchanged"
    truncated = refine_deterministic_candidate_v2(
        "<main><p>" + ("source " * 1_000) + "</p></main>",
        candidate,
        ir_limits=DocumentIRV2Limits(max_input_bytes=128),
    )
    bounded = refine_deterministic_candidate_v2(
        "<main><p>" + " ".join(f"token{index}" for index in range(100)) + "</p></main>",
        candidate,
        limits=DeterministicRefinerLimits(max_source_tokens=20),
    )

    assert not truncated.accepted
    assert truncated.reason == "incomplete_ir"
    assert truncated.output_markdown == candidate
    assert not bounded.accepted
    assert bounded.reason == "source_token_budget"
    assert bounded.output_markdown == candidate


def test_alignment_edge_budget_is_strict_for_repetitive_adversarial_input() -> None:
    html = "<main><p>" + ("alpha " * 80) + "</p><table><tr><td>omega</td></tr></table></main>"
    candidate = " ".join(["alpha"] * 20)
    limits = DeterministicRefinerLimits(
        max_occurrences_per_token=100,
        max_alignment_edges=100,
    )

    result = refine_deterministic_candidate_v2(html, candidate, limits=limits)

    assert not result.accepted
    assert result.reason == "alignment_budget"
    assert result.output_markdown == candidate


def test_utf8_cjk_alignment_and_reconstruction_are_deterministic() -> None:
    html = """
    <main>
      <h2>季度报告</h2>
      <p>收入保持稳定</p>
      <table><tr><th>姓名</th><th>分数</th></tr><tr><td>爱丽丝</td><td>九</td></tr></table>
    </main>
    """
    candidate = "## 季度报告\n\n收入保持稳定\n\n姓名 分数 爱丽丝 九"

    first = refine_deterministic_candidate_v2(html, candidate)
    second = refine_deterministic_candidate_v2(html, candidate)

    assert first == second
    assert first.accepted
    assert first.added_structures == ("table",)
    assert first.source_grounding_agreement == 1.0
    assert first.output_markdown.encode().decode() == first.output_markdown


def test_parallel_calls_are_deterministic() -> None:
    html = "<main><p>Items</p><ul><li>first value</li><li>second value</li></ul></main>"
    candidate = "Items first value second value"

    with ThreadPoolExecutor(max_workers=4) as pool:
        outputs = list(
            pool.map(
                lambda _: refine_deterministic_candidate_v2(html, candidate),
                range(16),
            )
        )

    assert outputs[0].accepted
    assert all(output == outputs[0] for output in outputs)

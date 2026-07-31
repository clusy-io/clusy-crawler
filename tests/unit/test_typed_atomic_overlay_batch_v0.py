from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest
from clusy_native import extract_document_ir_v2, reconstruct_document_ir_v2
from clusy_native.typed_atomic_overlay_batch_v0 import (
    TYPED_ATOMIC_OVERLAY_BATCH_V0_SCHEMA,
    TypedAtomicOverlayBatchV0Config,
    build_typed_atomic_overlay_batch_v0,
    verify_typed_atomic_overlay_batch_v0,
    verify_typed_atomic_overlay_certificates_v0,
)

from bench.lattice_reference import TypedSpanCandidate, decode

_STRUCTURED_HTML = (
    "<!doctype html><html><head><title>typed batch</title></head><body><main>"
    '<pre><code class="language-python">def answer():\n    return 42</code></pre>'
    '<table><tbody><tr><th rowspan="2">Name</th><th>Score</th></tr>'
    "<tr><td>Clusy</td></tr></tbody></table>"
    '<ol start="3"><li>first</li><li value="8">second</li></ol>'
    '<math display="block" alttext="x squared"><semantics>'
    "<msup><mi>x</mi><mn>2</mn></msup>"
    '<annotation encoding="application/x-tex">x^2</annotation>'
    "</semantics></math>"
    "</main></body></html>"
)
_ENABLED = TypedAtomicOverlayBatchV0Config(enabled=True)


def _framed_digest(domain: bytes, value: bytes) -> str:
    payload = len(domain).to_bytes(8, "big") + domain + len(value).to_bytes(8, "big") + value
    return hashlib.sha256(payload).hexdigest()


def _identity(batch: object) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            item.atom_kind,
            item.selected_id,
            item.source_order,
            item.source_start,
            item.source_end,
            item.source_span_digest,
            item.source_digest,
            item.graph_digest,
            item.output_digest,
            item.certificate_digest,
            item.markdown,
            item.certificate,
        )
        for item in batch.items
    )


def test_batch_is_disabled_and_unwired_by_default() -> None:
    result = build_typed_atomic_overlay_batch_v0("<not even parsed")

    assert result.schema_version == TYPED_ATOMIC_OVERLAY_BATCH_V0_SCHEMA
    assert not result.enabled
    assert not result.accepted
    assert result.reason == "disabled"
    assert result.items == ()
    assert result.parse_count == 0
    assert result.graph_clone_count == 0


def test_batch_exactly_certifies_code_table_list_and_math_source_spans() -> None:
    batch = build_typed_atomic_overlay_batch_v0(_STRUCTURED_HTML, config=_ENABLED)

    assert batch.accepted, batch.reason
    assert batch.atom_kinds == ("code", "table", "list", "math")
    assert batch.parse_count == 1
    assert batch.graph_clone_count == 1
    assert len({item.graph_digest for item in batch.items}) == 1
    assert len({item.source_digest for item in batch.items}) == 1

    document = extract_document_ir_v2(_STRUCTURED_HTML)
    for item in batch.items:
        source_fragment = _STRUCTURED_HTML[item.source_start : item.source_end]
        assert source_fragment.startswith(
            {
                "code": "<pre",
                "table": "<table",
                "list": "<ol",
                "math": "<math",
            }[item.atom_kind]
        )
        assert item.source_span_digest == _framed_digest(
            b"clusy-typed-atomic-overlay-source-span-v0",
            source_fragment.encode(),
        )
        expected = reconstruct_document_ir_v2(
            document,
            selected_ids=[item.selected_id],
        )
        assert expected.selected_ids == [item.selected_id]
        assert expected.missing_ids == []
        assert item.markdown == expected.markdown
        assert item.verified
        assert item.deterministic

    by_kind = {item.atom_kind: item.markdown for item in batch.items}
    assert by_kind["code"] == "```python\ndef answer():\n    return 42\n```"
    assert 'rowspan="2"' in by_kind["table"]
    assert "Clusy" in by_kind["table"]
    assert by_kind["list"] == "3. first\n\n8. second"
    assert by_kind["math"] == "$$\nx^2\n$$"

    oracle_path = decode(
        [
            TypedSpanCandidate(
                candidate_id=f"candidate-{index}",
                source_identity=item.source_span_digest,
                source_start=item.source_start,
                source_end=item.source_end,
                block_id=item.selected_id,
                type_name=item.atom_kind,
                granularity="atomic",
                base_score=1.0,
            )
            for index, item in enumerate(batch.items)
        ],
        max_document_chars=len(_STRUCTURED_HTML),
    )
    assert tuple(span.block_id for span in oracle_path.spans) == batch.atom_ids

    verification = verify_typed_atomic_overlay_batch_v0(
        _STRUCTURED_HTML,
        batch,
        config=_ENABLED,
    )
    assert verification.verified, verification.reason
    assert verification.parse_count == 1
    assert verification.graph_clone_count == 1
    assert _identity(batch) == _identity(verification)


def test_batch_is_deterministic_across_parallel_calls() -> None:
    def build(_: int) -> tuple[tuple[object, ...], ...]:
        result = build_typed_atomic_overlay_batch_v0(
            _STRUCTURED_HTML,
            config=_ENABLED,
        )
        assert result.accepted
        return _identity(result)

    with ThreadPoolExecutor(max_workers=4) as pool:
        identities = list(pool.map(build, range(12)))

    assert len(set(identities)) == 1


@pytest.mark.parametrize("item_index", range(4))
def test_batch_certificate_tamper_and_cross_source_replay_fail_closed(
    item_index: int,
) -> None:
    batch = build_typed_atomic_overlay_batch_v0(_STRUCTURED_HTML, config=_ENABLED)
    assert batch.accepted
    certificates = [item.certificate for item in batch.items]
    mutated = bytearray(certificates[item_index])
    mutated[len(mutated) // 2] ^= 0x01
    certificates[item_index] = bytes(mutated)

    with pytest.raises(ValueError):
        verify_typed_atomic_overlay_certificates_v0(
            _STRUCTURED_HTML,
            certificates,
            config=_ENABLED,
        )

    original = tuple(item.certificate for item in batch.items)
    with pytest.raises(ValueError, match="source digest mismatch"):
        verify_typed_atomic_overlay_certificates_v0(
            _STRUCTURED_HTML.replace("return 42", "return 43"),
            original,
            config=_ENABLED,
        )


def test_batch_excludes_nested_atoms_and_fails_closed_on_budget() -> None:
    html = (
        "<!doctype html><html><head><title>nested</title></head><body>"
        "<ol><li>outer<ul><li>inner</li></ul></li></ol>"
        "</body></html>"
    )
    batch = build_typed_atomic_overlay_batch_v0(html, config=_ENABLED)

    assert batch.accepted
    assert batch.atom_kinds == ("list",)
    assert "outer" in batch.items[0].markdown
    assert "inner" in batch.items[0].markdown

    over_budget = build_typed_atomic_overlay_batch_v0(
        _STRUCTURED_HTML,
        config=TypedAtomicOverlayBatchV0Config(enabled=True, max_atoms=3),
    )
    assert not over_budget.accepted
    assert over_budget.reason == "atom_budget"
    assert over_budget.items == ()

    figure_barrier = (
        "<!doctype html><html><head><title>figure</title></head><body>"
        "<figure><table><tr><td>nested table</td></tr></table></figure>"
        "<pre><code>sibling_code = 1</code></pre>"
        "</body></html>"
    )
    filtered = build_typed_atomic_overlay_batch_v0(figure_barrier, config=_ENABLED)
    assert filtered.accepted
    assert filtered.atom_kinds == ("code",)
    assert "sibling_code = 1" in filtered.items[0].markdown

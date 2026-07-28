from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from clusy_native import DocumentIRLimits, extract_document_ir


def test_document_ir_preserves_order_structure_and_features() -> None:
    html = """
    <main>
      <h1>  Ordered   IR </h1>
      <p>Read <a href="/docs">the docs</a>.</p>
      <ul><li>one</li><li>two</li></ul>
      <pre><code>first
  second</code></pre>
      <table><tr><td>cell</td></tr></table>
      <script>do_not_emit()</script>
      <p aria-hidden="true">also hidden</p>
    </main>
    """

    result = extract_document_ir(html)

    assert result.schema_version == "ordered-dom-ir.v1"
    assert [block.tag for block in result.blocks] == [
        "main",
        "h1",
        "p",
        "ul",
        "li",
        "li",
        "pre",
        "code",
        "table",
        "tbody",
        "tr",
        "td",
    ]
    assert [block.order for block in result.blocks] == list(range(result.block_count))
    assert [block.id for block in result.blocks] == [
        f"block-{order:06}" for order in range(result.block_count)
    ]
    assert not result.blocks[0].atomic
    assert not result.blocks[0].selectable
    assert [block.tag for block in result.blocks if block.selectable] == [
        "h1",
        "p",
        "li",
        "li",
        "code",
        "td",
    ]
    assert result.blocks[1].text == "Ordered IR"
    assert result.blocks[2].link_count == 1
    assert result.blocks[2].link_text_bytes == len("the docs")
    assert result.blocks[6].preserve_whitespace
    assert "  second" in result.blocks[7].text
    assert result.blocks[4].parent_id == "block-000003"
    assert result.blocks[7].parent_id == "block-000006"
    assert result.blocks[11].parent_id == "block-000010"
    assert not any(
        "do_not_emit" in block.text or "also hidden" in block.text for block in result.blocks
    )
    assert not result.truncated


def test_document_ir_reports_every_applied_limit_and_stays_utf8_safe() -> None:
    html = "<main>" + "".join(f"<p>{'é' * 60}-{index}</p>" for index in range(20)) + "</main>"
    limits = DocumentIRLimits(
        max_input_bytes=1_000,
        max_nodes=100,
        max_blocks=3,
        max_depth=20,
        max_block_text_bytes=15,
        max_block_html_bytes=25,
        max_total_text_bytes=30,
        max_total_html_bytes=50,
    )

    result = extract_document_ir(html, limits=limits)

    assert result.input_bytes == len(html.encode())
    assert result.parsed_bytes <= limits.max_input_bytes
    assert result.block_count <= limits.max_blocks
    assert result.stored_text_bytes <= limits.max_total_text_bytes
    assert result.stored_html_bytes <= limits.max_total_html_bytes
    assert result.truncated
    assert result.input_truncated
    assert "input_bytes" in result.truncation_reasons
    assert all(len(block.text.encode()) <= limits.max_block_text_bytes for block in result.blocks)
    assert all(
        len(block.outer_html.encode()) <= limits.max_block_html_bytes for block in result.blocks
    )
    assert all(block.text.encode().decode() == block.text for block in result.blocks)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_input_bytes", 0),
        ("max_nodes", 0),
        ("max_blocks", 20_000),
        ("max_depth", 300),
        ("max_total_html_bytes", 40 * 1024 * 1024),
    ],
)
def test_document_ir_rejects_unbounded_or_empty_limits(field: str, value: int) -> None:
    values = {
        "max_input_bytes": 1_024,
        "max_nodes": 100,
        "max_blocks": 10,
        "max_depth": 10,
        "max_block_text_bytes": 1_024,
        "max_block_html_bytes": 1_024,
        "max_total_text_bytes": 2_048,
        "max_total_html_bytes": 2_048,
    }
    values[field] = value

    with pytest.raises(ValueError):
        extract_document_ir("<p>text</p>", limits=DocumentIRLimits(**values))


def test_document_ir_is_stable_across_parallel_native_calls() -> None:
    html = "<article><h2>Hello</h2><p>parallel world</p></article>"

    with ThreadPoolExecutor(max_workers=4) as pool:
        outputs = list(pool.map(lambda _: extract_document_ir(html), range(16)))

    expected = [(block.id, block.tag, block.text) for block in outputs[0].blocks]
    assert all(
        [(block.id, block.tag, block.text) for block in output.blocks] == expected
        for output in outputs
    )


def test_node_cap_fails_closed_before_an_unsanitized_tail() -> None:
    html = (
        "<main>"
        + "<p>bounded node</p>" * 100
        + '<script id="after-cap">must never enter a block</script></main>'
    )
    limits = DocumentIRLimits(
        max_input_bytes=64 * 1024,
        max_nodes=8,
        max_blocks=100,
        max_depth=20,
        max_block_text_bytes=4 * 1024,
        max_block_html_bytes=8 * 1024,
        max_total_text_bytes=16 * 1024,
        max_total_html_bytes=32 * 1024,
    )

    result = extract_document_ir(html, limits=limits)

    assert result.nodes_truncated
    assert result.node_count == limits.max_nodes
    assert result.block_count == 0
    assert result.blocks == []
    assert "node_count" in result.truncation_reasons

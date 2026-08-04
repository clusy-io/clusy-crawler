from __future__ import annotations

import pytest

from bench.webmainbench_benchmark import BenchmarkError, scrub_annotation_artifacts


def test_scrubber_preserves_offsets_after_length_changing_unicode_lowercase():
    html = (
        '<p data-anno-uid="paragraph">İstanbul</p>'
        '<script data-anno-uid="external" src="/app.js"></script>'
        '<script data-anno-uid="inline">window.ready = true;</script>'
    )

    scrubbed, counts = scrub_annotation_artifacts(html)

    assert scrubbed == (
        '<p>İstanbul</p><script src="/app.js"></script><script>window.ready = true;</script>'
    )
    assert counts["attribute_data-anno-uid"] == 3


def test_scrubber_drops_only_nonvisible_blocks_containing_annotation_signals():
    html = (
        '<!-- <style id="cc-extraStyle">.mark-selected {outline: blue}</style> -->'
        "<script>window.annotationAttribute = 'data-anno-uid';</script>"
        '<main data-anno-uid="main">Visible article text</main>'
    )

    scrubbed, counts = scrub_annotation_artifacts(html)

    assert scrubbed == "<main>Visible article text</main>"
    assert counts["annotation_comments"] == 1
    assert counts["annotation_script_blocks"] == 1
    assert counts["attribute_data-anno-uid"] == 1


def test_scrubber_removes_entity_escaped_wrappers_without_decoding_page_markup():
    html = (
        "<textarea>&lt;b&gt;&lt;marked-text&gt;Visible &amp; safe"
        "&lt;/marked-text&gt;&lt;a href=&quot;/more&quot;&gt;More&lt;/a&gt;"
        "&lt;marked-tail&gt; tail&lt;/marked-tail&gt;&lt;/b&gt;</textarea>"
    )

    scrubbed, counts = scrub_annotation_artifacts(html)

    assert scrubbed == (
        "<textarea>&lt;b&gt;Visible &amp; safe"
        "&lt;a href=&quot;/more&quot;&gt;More&lt;/a&gt; tail&lt;/b&gt;</textarea>"
    )
    assert counts["entity_escaped_wrapper_tags"] == 4


def test_scrubber_limits_entity_wrapper_removal_to_rcdata():
    html = (
        '<div data-example="&lt;marked-text&gt;attribute&lt;/marked-text&gt;">'
        "<pre>&lt;marked-text&gt;example&lt;/marked-text&gt;</pre></div>"
        '<script>const sample = "&lt;marked-text&gt;script&lt;/marked-text&gt;";</script>'
    )

    scrubbed, counts = scrub_annotation_artifacts(html)

    assert scrubbed == html
    assert "entity_escaped_wrapper_tags" not in counts


@pytest.mark.parametrize(
    "rcdata",
    [
        "&ltmarked-text&gt;unsafe&lt/marked-text&gt;",
        "&#60marked-text&#62;unsafe&#60/marked-text&#62;",
        "&lt;marked-text&gt;unsafe&lt;&#47;marked-text&gt;",
        "&lt;&#32;marked-text&gt;unsafe&lt;/marked-text&gt;",
        "&lt;marked-text data-x=&quot;&gt;&quot;&gt;unsafe&lt;/marked-text&gt;",
        "&lt;marked-text MALFORMED &lt;b&gt;unsafe",
    ],
)
def test_scrubber_fails_closed_on_noncanonical_rcdata_markers(rcdata):
    with pytest.raises(BenchmarkError, match="ambiguous annotation wrapper"):
        scrub_annotation_artifacts(f"<textarea>{rcdata}</textarea>")


def test_scrubber_entity_wrapper_pass_is_idempotent():
    html = "<title>&lt;marked-text&gt;Title&lt;/marked-text&gt;</title>"

    first, first_counts = scrub_annotation_artifacts(html)
    second, second_counts = scrub_annotation_artifacts(first)

    assert first == second == "<title>Title</title>"
    assert first_counts["entity_escaped_wrapper_tags"] == 2
    assert "entity_escaped_wrapper_tags" not in second_counts

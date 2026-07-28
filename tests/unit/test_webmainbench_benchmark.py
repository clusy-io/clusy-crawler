from __future__ import annotations

from bench.webmainbench_benchmark import scrub_annotation_artifacts


def test_scrubber_preserves_offsets_after_length_changing_unicode_lowercase():
    html = (
        '<p data-anno-uid="paragraph">İstanbul</p>'
        '<script data-anno-uid="external" src="/app.js"></script>'
        '<script data-anno-uid="inline">window.ready = true;</script>'
    )

    scrubbed, counts = scrub_annotation_artifacts(html)

    assert scrubbed == (
        "<p>İstanbul</p>"
        '<script src="/app.js"></script>'
        "<script>window.ready = true;</script>"
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

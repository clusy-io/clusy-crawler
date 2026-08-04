# Beta 4 launch animation

This directory contains the source and X-ready output for the Clusy Crawler
Beta 4 launch animation. It uses the official Clusy wordmark without
redrawing, recoloring, or changing its aspect ratio.

The animation is designed for silent autoplay and tells one product story:
page noise becomes selected source content, selected content becomes clean
Markdown, and the product demonstration resolves into the registered evidence
boundary and an invitation to test difficult pages.

## Render

Requirements:

- `rsvg-convert` from librsvg;
- FFmpeg with the `libx264` encoder; and
- the checked-in scene wordmark.

```bash
docs/social/beta4/render.sh
```

The script writes:

- `clusy-crawler-beta4-x.mp4` — the upload-ready H.264 video;
- `clusy-crawler-beta4-poster.png` — the final call-to-action frame; and
- ignored intermediate scene PNGs under `rendered/`.

The proof scene carries the registered historical scope and does not create a
new benchmark claim. The canonical evidence remains in
[`../../BENCHMARKS.md`](../../BENCHMARKS.md).

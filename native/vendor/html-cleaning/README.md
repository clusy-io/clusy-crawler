# html-cleaning

HTML cleaning, sanitization, and text processing utilities for Rust.

[![Crates.io](https://img.shields.io/crates/v/html-cleaning.svg)](https://crates.io/crates/html-cleaning)
[![Documentation](https://docs.rs/html-cleaning/badge.svg)](https://docs.rs/html-cleaning)
[![License](https://img.shields.io/crates/l/html-cleaning.svg)](LICENSE-MIT)

## Features

- **HTML Cleaning**: Remove unwanted elements (scripts, styles, forms)
- **Tag Stripping**: Remove tags while preserving text content
- **Text Normalization**: Collapse whitespace, trim text
- **Link Processing**: Make URLs absolute, filter links
- **Content Deduplication**: LRU-based duplicate detection
- **Markdown Output**: Convert HTML to Markdown with structure preservation
- **Presets**: Ready-to-use configurations for common scenarios

## Quick Start

```rust
use html_cleaning::{HtmlCleaner, presets};
use dom_query::Document;

// Use a preset for quick setup
let cleaner = HtmlCleaner::with_options(presets::standard());

let html = "<html><body><script>bad</script><p>Hello!</p></body></html>";
let doc = Document::from(html);

cleaner.clean(&doc);
// Scripts removed, paragraph content preserved
```

## Installation

Add to your `Cargo.toml`:

```toml
[dependencies]
html-cleaning = "0.1"
```

With all features:

```toml
[dependencies]
html-cleaning = { version = "0.1", features = ["full"] }
```

## Usage Examples

### Basic Cleaning

```rust
use html_cleaning::{HtmlCleaner, CleaningOptions};

let options = CleaningOptions {
    tags_to_remove: vec!["script".into(), "style".into()],
    prune_empty: true,
    normalize_whitespace: true,
    ..Default::default()
};

let cleaner = HtmlCleaner::with_options(options);
```

### Using the Builder Pattern

```rust
use html_cleaning::CleaningOptions;

let options = CleaningOptions::builder()
    .remove_tags(&["script", "style", "noscript"])
    .remove_selectors(&[".advertisement", "#cookie-banner"])
    .prune_empty(true)
    .normalize_whitespace(true)
    .build();
```

### Using Presets

```rust
use html_cleaning::presets;

// Minimal: Just scripts and styles
let minimal = presets::minimal();

// Standard: + forms, iframes, objects
let standard = presets::standard();

// Aggressive: + nav, header, footer, aside
let aggressive = presets::aggressive();

// Article extraction: Optimized for content extraction
let article = presets::article_extraction();
```

### Text Processing

```rust
use html_cleaning::text;

let has_content = text::has_content("  hello  ");  // true
let normalized = text::normalize("  multiple   spaces  ");  // "multiple spaces"
let words = text::word_count("hello world");  // 2
```

### HTML to Markdown

```rust
use html_cleaning::markdown::html_to_markdown;

let html = "<h1>Title</h1><p>Content with <strong>bold</strong></p>";
let md = html_to_markdown(html);
// Output: "# Title\n\nContent with **bold**\n"
```

## Feature Flags

| Feature | Default | Description |
|---------|---------|-------------|
| `presets` | Yes | Include prebuilt cleaning configurations |
| `regex` | No | Enable regex-based selectors |
| `url` | No | Enable URL processing with the `url` crate |
| `markdown` | No | Enable HTML to Markdown conversion |
| `full` | No | Enable all features |

## Modules

| Module | Description |
|--------|-------------|
| `cleaner` | Core `HtmlCleaner` and cleaning operations |
| `text` | Text processing utilities |
| `tree` | lxml-style text/tail tree manipulation |
| `dom` | DOM helper utilities |
| `dedup` | Content deduplication |
| `presets` | Ready-to-use cleaning configurations |
| `links` | URL and link processing (feature: `url`) |
| `markdown` | HTML to Markdown conversion (feature: `markdown`) |

## Presets Reference

### `minimal()`
- Removes: `script`, `style`, `noscript`
- Best for: Quick sanitization

### `standard()`
- Removes: `script`, `style`, `noscript`, `form`, `iframe`, `object`, `embed`, `svg`, `canvas`, `video`, `audio`
- Enables: `prune_empty`, `normalize_whitespace`
- Best for: General web scraping

### `aggressive()`
- Includes all of `standard()` plus:
- Removes: `nav`, `header`, `footer`, `aside`, `figure`, `figcaption`
- Enables: `strip_attributes` (preserves `href`, `src`, `alt`)
- Best for: Maximum content extraction

### `article_extraction()`
- Optimized for article content extraction
- Removes navigation and layout elements
- Strips wrapper tags (`div`, `span`) while preserving content
- Best for: News articles, blog posts

## Related Projects

- [rs-trafilatura](https://github.com/Murrough-Foley/rs-trafilatura) - Web content extraction library (uses html-cleaning)
- [dom_query](https://crates.io/crates/dom_query) - DOM manipulation library

## License

Licensed under either of:

- Apache License, Version 2.0 ([LICENSE-APACHE](LICENSE-APACHE))
- MIT license ([LICENSE-MIT](LICENSE-MIT))

at your option.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

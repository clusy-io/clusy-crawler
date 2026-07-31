# quick_html2md

Fast HTML to Markdown conversion with GitHub Flavored Markdown (GFM) support.

## Features

- **Headings**: `<h1>`-`<h6>` -> `#`-`######`
- **Emphasis**: `<strong>`/`<b>` -> `**bold**`, `<em>`/`<i>` -> `*italic*`
- **Strikethrough**: `<del>`/`<s>` -> `~~struck~~` (GFM)
- **Lists**: `<ul>`/`<ol>` with proper nesting and indentation
- **Links**: `<a href="">` -> `[text](url)`
- **Images**: `<img>` -> `![alt](src)`
- **Code**: `<code>` -> `` `inline` ``, `<pre><code>` -> fenced blocks with language
- **Tables**: Full GFM table support with alignment
- **Blockquotes**: `<blockquote>` -> `> quote`
- **URL Resolution**: Resolve relative URLs against a base URL
- **CommonMark Mode**: Strict CommonMark compliance option
- **Character Escaping**: Optional escaping of markdown special characters

## Quick Start

```rust
use quick_html2md::html_to_markdown;

let html = "<h1>Hello</h1><p>World</p>";
let md = html_to_markdown(html);
assert_eq!(md, "# Hello\n\nWorld\n");
```

## With Options

```rust
use quick_html2md::{html_to_markdown_with_options, MarkdownOptions};

let options = MarkdownOptions::new()
    .include_links(false)  // Strip links, keep text
    .preserve_tables(true);

let md = html_to_markdown_with_options(html, &options);
```

## URL Resolution

Resolve relative URLs in links and images against a base URL:

```rust
use quick_html2md::{html_to_markdown_with_options, MarkdownOptions};

let options = MarkdownOptions::new()
    .base_url("https://example.com/docs/");

let html = r#"<a href="page.html">Link</a>"#;
let md = html_to_markdown_with_options(html, &options);
// Output: [Link](https://example.com/docs/page.html)
```

## CommonMark Mode

For strict CommonMark compliance (disables GFM extensions):

```rust
use quick_html2md::{html_to_markdown_with_options, MarkdownOptions};

let options = MarkdownOptions::commonmark();

let html = "<ul><li>parent<ul><li>child</li></ul></li></ul>";
let md = html_to_markdown_with_options(html, &options);
// Uses 4-space indentation, escapes special chars, no strikethrough/tables
```

## Nested Lists

This crate properly handles nested lists, producing clean markdown output:

```rust
let html = "<ul><li>parent<ul><li>child</li></ul></li></ul>";
let md = html_to_markdown(html);
// Output:
// - parent
//   - child
```

## GFM Tables

HTML tables are converted to GitHub Flavored Markdown tables with alignment support:

```rust
let html = r#"<table>
    <tr><th align="left">Name</th><th align="right">Value</th></tr>
    <tr><td>foo</td><td>42</td></tr>
</table>"#;
let md = html_to_markdown(html);
// Output:
// | Name | Value |
// |:--- | ---:|
// | foo | 42 |
```

## Code Block Language Detection

The converter detects programming languages from common class naming patterns:

- `language-rust` (Prism.js, Highlight.js)
- `lang-python`
- `highlight-javascript`
- `sourceCode rust` (Pandoc)
- Direct class names: `rust`, `python`, `javascript`, etc.

## Image Dimension Handling

In CommonMark mode, images with width/height attributes are output as HTML to preserve dimensions:

```rust
let options = MarkdownOptions::commonmark();
let html = r#"<img src="photo.jpg" alt="Photo" width="200" height="100">"#;
let md = html_to_markdown_with_options(html, &options);
// Output: <img src="photo.jpg" alt="Photo" width="200" height="100" />
```

## Optional Features

- `url` - Enable the `url` crate for more robust URL resolution

```toml
[dependencies]
quick_html2md = { version = "0.1", features = ["url"] }
```

## Migration from html-cleaning

If you were using `html_cleaning::markdown`:

```rust
// Before
use html_cleaning::markdown::html_to_markdown;

// After
use quick_html2md::html_to_markdown;
```

The API is identical - just change the import.

## License

Licensed under either of Apache License, Version 2.0 or MIT license at your option.

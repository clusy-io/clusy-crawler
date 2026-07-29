//! Fast HTML to Markdown conversion with GFM support.
//!
//! # Features
//!
//! - **Headings**: `<h1>`-`<h6>` -> `#`-`######`
//! - **Emphasis**: `<strong>`/`<b>` -> `**bold**`, `<em>`/`<i>` -> `*italic*`
//! - **Strikethrough**: `<del>`/`<s>` -> `~~struck~~`
//! - **Lists**: `<ul>`/`<ol>` with proper nesting and indentation
//! - **Links**: `<a href="">` -> `[text](url)`
//! - **Images**: `<img>` -> `![alt](src)`
//! - **Code**: `<code>` -> `` `inline` ``, `<pre><code>` -> fenced blocks with language
//! - **Tables**: Full GFM table support with alignment
//! - **Blockquotes**: `<blockquote>` -> `> quote`
//!
//! # Quick Start
//!
//! ```
//! use quick_html2md::html_to_markdown;
//!
//! let html = "<h1>Hello</h1><p>World</p>";
//! let md = html_to_markdown(html);
//! assert!(md.contains("# Hello"));
//! assert!(md.contains("World"));
//! ```
//!
//! # With Options
//!
//! ```
//! use quick_html2md::{html_to_markdown_with_options, MarkdownOptions};
//!
//! let options = MarkdownOptions::new()
//!     .include_links(false)
//!     .preserve_tables(true);
//!
//! let html = "<p><a href='url'>link</a></p>";
//! let md = html_to_markdown_with_options(html, &options);
//! ```

mod converter;
mod elements;
pub mod options;
mod utils;

pub use options::MarkdownOptions;

use dom_query::{Document, Selection};

use converter::convert_node;
use utils::normalize_output;

/// Convert HTML document to Markdown.
///
/// Uses default options.
///
/// # Example
///
/// ```
/// use quick_html2md::to_markdown;
/// use dom_query::Document;
///
/// let doc = Document::from("<h1>Title</h1><p>Content</p>");
/// let md = to_markdown(&doc);
/// assert!(md.contains("# Title"));
/// ```
#[must_use]
pub fn to_markdown(doc: &Document) -> String {
    to_markdown_with_options(doc, &MarkdownOptions::new())
}

/// Convert HTML document to Markdown with options.
#[must_use]
pub fn to_markdown_with_options(doc: &Document, options: &MarkdownOptions) -> String {
    let mut output = String::new();
    let body = doc.select("body");

    if body.exists() {
        for node in body.children().nodes() {
            let sel = Selection::from(*node);
            convert_node(&sel, &mut output, options, 0);
        }
    } else {
        // No body, process root
        for node in doc.select("*").first().children().nodes() {
            let sel = Selection::from(*node);
            convert_node(&sel, &mut output, options, 0);
        }
    }

    normalize_output(&output)
}

/// Convert HTML element to Markdown.
#[must_use]
pub fn element_to_markdown(element: &Selection) -> String {
    element_to_markdown_with_options(element, &MarkdownOptions::new())
}

/// Convert HTML element to Markdown with options.
#[must_use]
pub fn element_to_markdown_with_options(element: &Selection, options: &MarkdownOptions) -> String {
    let mut output = String::new();
    convert_node(element, &mut output, options, 0);
    normalize_output(&output)
}

/// Convert HTML string to Markdown.
///
/// # Example
///
/// ```
/// use quick_html2md::html_to_markdown;
///
/// let md = html_to_markdown("<h1>Title</h1><p>Content</p>");
/// assert!(md.contains("# Title"));
/// assert!(md.contains("Content"));
/// ```
#[must_use]
pub fn html_to_markdown(html: &str) -> String {
    let doc = Document::from(html);
    to_markdown(&doc)
}

/// Convert HTML string to Markdown with options.
#[must_use]
pub fn html_to_markdown_with_options(html: &str, options: &MarkdownOptions) -> String {
    let doc = Document::from(html);
    to_markdown_with_options(&doc, options)
}

#[cfg(test)]
mod tests {
    use super::*;
    use pretty_assertions::assert_eq;

    // ==================== Basic Element Tests ====================

    #[test]
    fn test_heading_conversion() {
        let md = html_to_markdown("<h1>Title</h1><h2>Subtitle</h2>");
        assert_eq!(md, "# Title\n\n## Subtitle\n");
    }

    #[test]
    fn test_all_heading_levels() {
        let html = "<h1>H1</h1><h2>H2</h2><h3>H3</h3><h4>H4</h4><h5>H5</h5><h6>H6</h6>";
        let md = html_to_markdown(html);
        assert!(md.contains("# H1"));
        assert!(md.contains("## H2"));
        assert!(md.contains("### H3"));
        assert!(md.contains("#### H4"));
        assert!(md.contains("##### H5"));
        assert!(md.contains("###### H6"));
    }

    #[test]
    fn test_paragraph() {
        let md = html_to_markdown("<p>Hello world</p>");
        assert_eq!(md, "Hello world\n");
    }

    #[test]
    fn test_bold_and_italic() {
        let md = html_to_markdown("<p><strong>bold</strong> and <em>italic</em></p>");
        assert_eq!(md, "**bold** and *italic*\n");
    }

    #[test]
    fn test_strikethrough() {
        let md = html_to_markdown("<p><del>deleted</del> and <s>struck</s></p>");
        assert_eq!(md, "~~deleted~~ and ~~struck~~\n");
    }

    #[test]
    fn test_links() {
        let md = html_to_markdown(r#"<a href="https://example.com">Link</a>"#);
        assert_eq!(md, "[Link](https://example.com)\n");
    }

    #[test]
    fn test_unordered_list() {
        let md = html_to_markdown("<ul><li>One</li><li>Two</li></ul>");
        assert_eq!(md, "- One\n- Two\n");
    }

    #[test]
    fn test_ordered_list() {
        let md = html_to_markdown("<ol><li>First</li><li>Second</li></ol>");
        assert_eq!(md, "1. First\n2. Second\n");
    }

    #[test]
    fn test_code_block() {
        let md = html_to_markdown("<pre><code>fn main() {}</code></pre>");
        assert!(md.contains("```"));
        assert!(md.contains("fn main()"));
    }

    #[test]
    fn test_blockquote() {
        let md = html_to_markdown("<blockquote>Quote text</blockquote>");
        assert_eq!(md, "> Quote text\n");
    }

    // ==================== Nested List Tests ====================

    #[test]
    fn test_nested_unordered_list() {
        let html = "<ul><li>parent<ul><li>child</li></ul></li></ul>";
        let md = html_to_markdown(html);
        assert_eq!(md, "- parent\n  - child\n");
    }

    #[test]
    fn test_deeply_nested_list() {
        let html = "<ul><li>L1<ul><li>L2<ul><li>L3</li></ul></li></ul></li></ul>";
        let md = html_to_markdown(html);
        assert_eq!(md, "- L1\n  - L2\n    - L3\n");
    }

    #[test]
    fn test_siblings_with_nested_children() {
        let html = "<ul><li>A<ul><li>A1</li></ul></li><li>B<ul><li>B1</li></ul></li></ul>";
        let md = html_to_markdown(html);
        // Each item should appear exactly once
        assert_eq!(md.matches("A1").count(), 1);
        assert_eq!(md.matches("B1").count(), 1);
        assert_eq!(md, "- A\n  - A1\n- B\n  - B1\n");
    }

    #[test]
    fn test_nested_ordered_list() {
        let html = "<ol><li>first<ol><li>nested</li></ol></li><li>second</li></ol>";
        let md = html_to_markdown(html);
        assert!(md.contains("1. first"));
        assert!(md.contains("  1. nested"));
        assert!(md.contains("2. second"));
    }

    #[test]
    fn test_mixed_nested_lists() {
        let html = "<ul><li>bullet<ol><li>num 1</li><li>num 2</li></ol></li></ul>";
        let md = html_to_markdown(html);
        assert!(md.contains("- bullet"));
        assert!(md.contains("  1. num 1"));
        assert!(md.contains("  2. num 2"));
    }

    #[test]
    fn test_text_after_nested_list() {
        let html = "<ul><li>before<ul><li>nested</li></ul>after</li></ul>";
        let md = html_to_markdown(html);
        // "after" should appear once
        assert_eq!(md.matches("after").count(), 1);
    }

    // ==================== Table Tests ====================

    #[test]
    fn test_simple_table() {
        let html = "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>";
        let md = html_to_markdown(html);
        assert!(md.contains("| A"));
        assert!(md.contains("| B"));
        assert!(md.contains("---"));
        assert!(md.contains("| 1"));
        assert!(md.contains("| 2"));
    }

    #[test]
    fn test_table_with_pipes_in_content() {
        let html = "<table><tr><th>Cmd</th></tr><tr><td>a | b</td></tr></table>";
        let md = html_to_markdown(html);
        assert!(md.contains(r"a \| b"));
    }

    #[test]
    fn test_table_header_with_pipes() {
        let html = "<table><thead><tr><th>A | B</th></tr></thead><tr><td>1</td></tr></table>";
        let md = html_to_markdown(html);
        assert!(md.contains(r"A \| B"));
    }

    #[test]
    fn test_empty_table() {
        let html = "<table></table>";
        let md = html_to_markdown(html);
        assert!(!md.contains("|"));
    }

    // ==================== Code Language Detection Tests ====================

    #[test]
    fn test_language_prism() {
        let html = r#"<pre><code class="language-rust">fn main() {}</code></pre>"#;
        let md = html_to_markdown(html);
        assert!(md.contains("```rust"));
    }

    #[test]
    fn test_language_highlight() {
        let html = r#"<pre><code class="highlight-python">print()</code></pre>"#;
        let md = html_to_markdown(html);
        assert!(md.contains("```python"));
    }

    #[test]
    fn test_language_pandoc() {
        let html = r#"<pre><code class="sourceCode javascript">const x = 1;</code></pre>"#;
        let md = html_to_markdown(html);
        assert!(md.contains("```javascript"));
    }

    #[test]
    fn test_language_direct_class() {
        let html = r#"<pre><code class="rust">fn main() {}</code></pre>"#;
        let md = html_to_markdown(html);
        assert!(md.contains("```rust"));
    }

    // ==================== Edge Case Tests ====================

    #[test]
    fn test_empty_input() {
        let md = html_to_markdown("");
        assert_eq!(md, "");
    }

    #[test]
    fn test_whitespace_only_input() {
        let md = html_to_markdown("   \n\t  ");
        assert_eq!(md, "");
    }

    #[test]
    fn test_backticks_in_inline_code() {
        let html = "<code>x `y` z</code>";
        let md = html_to_markdown(html);
        // Should use double backticks to escape
        assert!(md.contains("``"));
        assert!(md.contains("x `y` z"));
    }

    #[test]
    fn test_multiple_backticks_in_inline_code() {
        let html = "<code>a ``b`` c</code>";
        let md = html_to_markdown(html);
        // Should use triple backticks
        assert!(md.contains("```"));
    }

    #[test]
    fn test_parentheses_in_url() {
        let html = r#"<a href="https://en.wikipedia.org/wiki/Foo_(bar)">Link</a>"#;
        let md = html_to_markdown(html);
        assert!(md.contains("%28"));
        assert!(md.contains("%29"));
        assert!(!md.contains("Foo_(bar)"));
    }

    #[test]
    fn test_empty_link_no_href() {
        let html = "<a>just text</a>";
        let md = html_to_markdown(html);
        assert_eq!(md, "just text\n");
        assert!(!md.contains("["));
    }

    #[test]
    fn test_empty_image_no_src() {
        let html = "<p>before<img>after</p>";
        let md = html_to_markdown(html);
        assert!(!md.contains("![")); // No image markup
        assert!(md.contains("beforeafter"));
    }

    #[test]
    fn test_empty_emphasis() {
        let html = "<p>a<strong></strong>b<em></em>c</p>";
        let md = html_to_markdown(html);
        // Empty emphasis should be skipped
        assert!(!md.contains("****"));
        assert!(!md.contains("**"));
        assert_eq!(md, "abc\n");
    }

    #[test]
    fn test_nested_blockquote() {
        let html = "<blockquote><blockquote>nested quote</blockquote></blockquote>";
        let md = html_to_markdown(html);
        assert!(md.contains("> > nested quote"));
    }

    #[test]
    fn test_blockquote_with_formatting() {
        let html = "<blockquote><p><strong>bold</strong> text</p></blockquote>";
        let md = html_to_markdown(html);
        assert!(md.contains("> **bold** text"));
    }

    #[test]
    fn test_max_heading_level() {
        let options = MarkdownOptions::new().max_heading_level(2);
        let html = "<h1>H1</h1><h2>H2</h2><h3>H3</h3>";
        let md = html_to_markdown_with_options(html, &options);
        assert!(md.contains("# H1"));
        assert!(md.contains("## H2"));
        // H3 should be converted to paragraph (no ### prefix)
        assert!(!md.contains("### H3"));
        assert!(md.contains("H3"));
    }

    #[test]
    fn test_unicode_content() {
        let html = "<p>Hello 世界 🌍 مرحبا</p>";
        let md = html_to_markdown(html);
        assert!(md.contains("世界"));
        assert!(md.contains("🌍"));
        assert!(md.contains("مرحبا"));
    }

    #[test]
    fn test_html_entities() {
        let html = "<p>&amp; &lt; &gt; &quot;</p>";
        let md = html_to_markdown(html);
        assert!(md.contains("&"));
        assert!(md.contains("<"));
        assert!(md.contains(">"));
    }

    #[test]
    fn test_all_options_disabled() {
        let options = MarkdownOptions::new()
            .preserve_headings(false)
            .include_links(false)
            .include_images(false)
            .preserve_emphasis(false)
            .preserve_strikethrough(false)
            .preserve_lists(false)
            .preserve_code(false)
            .preserve_blockquotes(false)
            .preserve_tables(false);

        let html = "<h1>Title</h1><p><strong>bold</strong></p><a href='#'>link</a>";
        let md = html_to_markdown_with_options(html, &options);

        // Should just contain text, no markdown formatting
        assert!(!md.contains("#"));
        assert!(!md.contains("**"));
        assert!(!md.contains("["));
        assert!(md.contains("Title"));
        assert!(md.contains("bold"));
        assert!(md.contains("link"));
    }

    // ==================== Builder Pattern Tests ====================

    #[test]
    fn test_options_builder() {
        let options = MarkdownOptions::new()
            .preserve_headings(false)
            .include_links(false)
            .max_heading_level(3);

        assert!(!options.preserve_headings);
        assert!(!options.include_links);
        assert_eq!(options.max_heading_level, 3);
    }

    #[test]
    fn test_max_heading_level_clamping() {
        let options = MarkdownOptions::new().max_heading_level(10);
        assert_eq!(options.max_heading_level, 6);

        let options = MarkdownOptions::new().max_heading_level(0);
        assert_eq!(options.max_heading_level, 1);
    }

    // ==================== URL Resolution Tests ====================

    #[test]
    fn test_url_resolution_relative_path() {
        let options = MarkdownOptions::new().base_url("https://example.com/docs/page.html");
        let html = r#"<a href="images/logo.png">Logo</a>"#;
        let md = html_to_markdown_with_options(html, &options);
        assert!(md.contains("https://example.com/docs/images/logo.png"));
    }

    #[test]
    fn test_url_resolution_absolute_path() {
        let options = MarkdownOptions::new().base_url("https://example.com/docs/page.html");
        let html = r#"<a href="/assets/style.css">Style</a>"#;
        let md = html_to_markdown_with_options(html, &options);
        assert!(md.contains("https://example.com/assets/style.css"));
    }

    #[test]
    fn test_url_resolution_already_absolute() {
        let options = MarkdownOptions::new().base_url("https://example.com");
        let html = r#"<a href="https://other.com/page">Link</a>"#;
        let md = html_to_markdown_with_options(html, &options);
        assert!(md.contains("https://other.com/page"));
    }

    #[test]
    fn test_url_resolution_image() {
        let options = MarkdownOptions::new().base_url("https://example.com/docs/");
        let html = r#"<img src="img/photo.jpg" alt="Photo">"#;
        let md = html_to_markdown_with_options(html, &options);
        assert!(md.contains("https://example.com/docs/img/photo.jpg"));
    }

    // ==================== CommonMark Mode Tests ====================

    #[test]
    fn test_commonmark_mode_list_indentation() {
        let options = MarkdownOptions::commonmark();
        let html = "<ul><li>parent<ul><li>child</li></ul></li></ul>";
        let md = html_to_markdown_with_options(html, &options);
        // CommonMark uses 4-space indentation
        assert!(md.contains("    - child"));
    }

    #[test]
    fn test_commonmark_mode_no_strikethrough() {
        let options = MarkdownOptions::commonmark();
        let html = "<p><del>deleted</del></p>";
        let md = html_to_markdown_with_options(html, &options);
        // Strikethrough is GFM extension, should be disabled
        assert!(!md.contains("~~"));
        assert!(md.contains("deleted"));
    }

    #[test]
    fn test_commonmark_mode_no_tables() {
        let options = MarkdownOptions::commonmark();
        let html = "<table><tr><th>A</th></tr><tr><td>1</td></tr></table>";
        let md = html_to_markdown_with_options(html, &options);
        // Tables are GFM extension, should be disabled
        assert!(!md.contains("|"));
        assert!(md.contains("A"));
        assert!(md.contains("1"));
    }

    #[test]
    fn test_gfm_mode_strikethrough() {
        let options = MarkdownOptions::new(); // GFM mode by default
        let html = "<p><del>deleted</del></p>";
        let md = html_to_markdown_with_options(html, &options);
        assert!(md.contains("~~deleted~~"));
    }

    // ==================== Markdown Escaping Tests ====================

    #[test]
    fn test_escape_special_chars_enabled() {
        let options = MarkdownOptions::new().escape_special_chars(true);
        let html = "<p>Hello *world* and _underscores_</p>";
        let md = html_to_markdown_with_options(html, &options);
        assert!(md.contains(r"\*world\*"));
        assert!(md.contains(r"\_underscores\_"));
    }

    #[test]
    fn test_escape_special_chars_disabled() {
        let options = MarkdownOptions::new().escape_special_chars(false);
        let html = "<p>Hello *world*</p>";
        let md = html_to_markdown_with_options(html, &options);
        // Should not escape
        assert!(md.contains("*world*"));
        assert!(!md.contains(r"\*"));
    }

    #[test]
    fn test_commonmark_escapes_by_default() {
        let options = MarkdownOptions::commonmark();
        let html = "<p>Use [brackets] carefully</p>";
        let md = html_to_markdown_with_options(html, &options);
        assert!(md.contains(r"\[brackets\]"));
    }

    // ==================== Image Dimension Handling Tests ====================

    #[test]
    fn test_image_with_dimensions_commonmark() {
        let options = MarkdownOptions::commonmark();
        let html = r#"<img src="photo.jpg" alt="Photo" width="200" height="100">"#;
        let md = html_to_markdown_with_options(html, &options);
        // CommonMark mode with dimensions should output HTML
        assert!(md.contains("<img"));
        assert!(md.contains("width=\"200\""));
        assert!(md.contains("height=\"100\""));
    }

    #[test]
    fn test_image_without_dimensions_commonmark() {
        let options = MarkdownOptions::commonmark();
        let html = r#"<img src="photo.jpg" alt="Photo">"#;
        let md = html_to_markdown_with_options(html, &options);
        // Without dimensions, use standard markdown
        assert!(md.contains("![Photo](photo.jpg)"));
    }

    #[test]
    fn test_image_with_dimensions_gfm() {
        let options = MarkdownOptions::new(); // GFM mode
        let html = r#"<img src="photo.jpg" alt="Photo" width="200">"#;
        let md = html_to_markdown_with_options(html, &options);
        // GFM mode uses standard markdown (dimensions ignored)
        assert!(md.contains("![Photo](photo.jpg)"));
        assert!(!md.contains("<img"));
    }
}

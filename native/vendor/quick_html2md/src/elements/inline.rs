//! Inline element conversion (bold, italic, links, images, etc.)

use dom_query::Selection;

use crate::converter::convert_node;
use crate::options::MarkdownOptions;
use crate::utils::{escape_url, get_tag_name, resolve_url};

/// Convert inline content within an element.
pub(crate) fn convert_inline_content(
    sel: &Selection,
    output: &mut String,
    options: &MarkdownOptions,
    depth: usize,
) {
    convert_inline_content_impl(sel, output, options, false, depth);
}

/// Convert inline content, optionally skipping nested lists.
/// When `skip_lists` is true, skips `<ul>` and `<ol>` elements.
pub(crate) fn convert_inline_content_impl(
    sel: &Selection,
    output: &mut String,
    options: &MarkdownOptions,
    skip_lists: bool,
    depth: usize,
) {
    if let Some(node) = sel.nodes().first() {
        for child in node.children() {
            if child.is_text() {
                let text = child.text();
                if options.escape_special_chars {
                    output.push_str(&escape_markdown_text(&text));
                } else {
                    output.push_str(&text);
                }
            } else if child.is_element() {
                let child_sel = Selection::from(child);
                let tag = get_tag_name(&child_sel);

                // Skip nested lists when processing list item content
                if skip_lists && (tag == "ul" || tag == "ol") {
                    continue;
                }

                convert_node(&child_sel, output, options, depth);
            }
        }
    }
}

/// Escape markdown special characters in text.
fn escape_markdown_text(text: &str) -> String {
    let mut result = String::with_capacity(text.len() + text.len() / 10);

    for c in text.chars() {
        match c {
            '\\' | '`' | '*' | '_' | '{' | '}' | '[' | ']' | '(' | ')' | '#' | '+' | '-' | '.'
            | '!' | '<' | '>' | '~' => {
                result.push('\\');
                result.push(c);
            }
            _ => result.push(c),
        }
    }

    result
}

/// Convert strong/bold element.
/// Returns false if the element is empty and should be skipped.
pub(crate) fn convert_strong(
    sel: &Selection,
    output: &mut String,
    options: &MarkdownOptions,
    depth: usize,
) {
    let start_len = output.len();
    output.push_str("**");
    let content_start = output.len();
    convert_inline_content(sel, output, options, depth);

    // If no content was added, remove the opening marker
    if output.len() == content_start {
        output.truncate(start_len);
    } else {
        output.push_str("**");
    }
}

/// Convert emphasis/italic element.
/// Returns false if the element is empty and should be skipped.
pub(crate) fn convert_emphasis(
    sel: &Selection,
    output: &mut String,
    options: &MarkdownOptions,
    depth: usize,
) {
    let start_len = output.len();
    output.push('*');
    let content_start = output.len();
    convert_inline_content(sel, output, options, depth);

    // If no content was added, remove the opening marker
    if output.len() == content_start {
        output.truncate(start_len);
    } else {
        output.push('*');
    }
}

/// Convert strikethrough element (<del>, <s>, <strike>).
pub(crate) fn convert_strikethrough(
    sel: &Selection,
    output: &mut String,
    options: &MarkdownOptions,
    depth: usize,
) {
    let start_len = output.len();
    output.push_str("~~");
    let content_start = output.len();
    convert_inline_content(sel, output, options, depth);

    // If no content was added, remove the opening marker
    if output.len() == content_start {
        output.truncate(start_len);
    } else {
        output.push_str("~~");
    }
}

/// Convert link element.
/// If href is missing or empty, outputs just the link text.
/// Resolves relative URLs against base_url if configured.
pub(crate) fn convert_link(
    sel: &Selection,
    output: &mut String,
    options: &MarkdownOptions,
    depth: usize,
) {
    let href = sel.attr("href").unwrap_or_default();

    // If no href, just output the text content
    if href.is_empty() {
        convert_inline_content(sel, output, options, depth);
        return;
    }

    // Resolve URL against base if configured
    let resolved_url = resolve_url(&href, options);

    output.push('[');
    convert_inline_content(sel, output, options, depth);
    output.push_str("](");
    output.push_str(&escape_url(&resolved_url));
    output.push(')');
}

/// Convert image element.
/// If src is missing or empty, outputs nothing.
/// In CommonMark mode, images with width/height fall back to HTML.
/// Resolves relative URLs against base_url if configured.
pub(crate) fn convert_image(sel: &Selection, output: &mut String, options: &MarkdownOptions) {
    let src = sel.attr("src").unwrap_or_default();

    // If no src, skip the image entirely
    if src.is_empty() {
        return;
    }

    let alt = sel.attr("alt").unwrap_or_default();
    let width = sel.attr("width");
    let height = sel.attr("height");

    // Resolve URL against base if configured
    let resolved_url = resolve_url(&src, options);

    // In CommonMark mode (or when dimensions are specified), use HTML for images with dimensions
    if options.commonmark && (width.is_some() || height.is_some()) {
        // Output as HTML img tag to preserve dimensions
        output.push_str("<img src=\"");
        output.push_str(&resolved_url);
        output.push('"');

        if !alt.is_empty() {
            output.push_str(" alt=\"");
            output.push_str(&escape_html_attr(&alt));
            output.push('"');
        }

        if let Some(w) = width {
            output.push_str(" width=\"");
            output.push_str(&escape_html_attr(&w));
            output.push('"');
        }

        if let Some(h) = height {
            output.push_str(" height=\"");
            output.push_str(&escape_html_attr(&h));
            output.push('"');
        }

        output.push_str(" />");
    } else {
        // Standard markdown image syntax
        output.push_str("![");
        output.push_str(&alt);
        output.push_str("](");
        output.push_str(&escape_url(&resolved_url));
        output.push(')');
    }
}

/// Escape characters for HTML attribute values.
fn escape_html_attr(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('"', "&quot;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

/// Convert inline code element.
/// Handles backticks in content by using appropriate delimiter.
pub(crate) fn convert_inline_code(sel: &Selection, output: &mut String) {
    let text = sel.text();
    let content = text.as_ref();

    if content.is_empty() {
        return;
    }

    // Count maximum consecutive backticks in content
    let max_backticks = count_max_consecutive_backticks(content);

    if max_backticks == 0 {
        // No backticks in content, use single backtick delimiters
        output.push('`');
        output.push_str(content);
        output.push('`');
    } else {
        // Use one more backtick than the maximum found
        let delimiter = "`".repeat(max_backticks + 1);

        output.push_str(&delimiter);

        // Add space padding if content starts or ends with backtick
        let needs_leading_space = content.starts_with('`');
        let needs_trailing_space = content.ends_with('`');

        if needs_leading_space {
            output.push(' ');
        }
        output.push_str(content);
        if needs_trailing_space {
            output.push(' ');
        }

        output.push_str(&delimiter);
    }
}

/// Count the maximum number of consecutive backticks in a string.
fn count_max_consecutive_backticks(s: &str) -> usize {
    let mut max_count = 0;
    let mut current_count = 0;

    for c in s.chars() {
        if c == '`' {
            current_count += 1;
            max_count = max_count.max(current_count);
        } else {
            current_count = 0;
        }
    }

    max_count
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_count_max_consecutive_backticks() {
        assert_eq!(count_max_consecutive_backticks("hello"), 0);
        assert_eq!(count_max_consecutive_backticks("hel`lo"), 1);
        assert_eq!(count_max_consecutive_backticks("hel``lo"), 2);
        assert_eq!(count_max_consecutive_backticks("`a`b``c"), 2);
        assert_eq!(count_max_consecutive_backticks("```"), 3);
    }

    #[test]
    fn test_escape_html_attr() {
        assert_eq!(escape_html_attr("hello"), "hello");
        assert_eq!(escape_html_attr("a & b"), "a &amp; b");
        assert_eq!(escape_html_attr("a \"quoted\""), "a &quot;quoted&quot;");
        assert_eq!(escape_html_attr("<tag>"), "&lt;tag&gt;");
    }

    #[test]
    fn test_escape_markdown_text() {
        assert_eq!(escape_markdown_text("hello"), "hello");
        assert_eq!(escape_markdown_text("*bold*"), r"\*bold\*");
        assert_eq!(escape_markdown_text("[link]"), r"\[link\]");
    }
}

//! Block element conversion (paragraphs, blockquotes, hr).

use dom_query::Selection;

use crate::converter::convert_node;
use crate::options::MarkdownOptions;

use super::inline::convert_inline_content;

/// Convert paragraph element.
pub(crate) fn convert_paragraph(
    sel: &Selection,
    output: &mut String,
    options: &MarkdownOptions,
    depth: usize,
) {
    convert_inline_content(sel, output, options, depth);
    output.push_str("\n\n");
}

/// Convert blockquote element.
/// Recursively converts nested content while prefixing each line with `> `.
pub(crate) fn convert_blockquote(
    sel: &Selection,
    output: &mut String,
    options: &MarkdownOptions,
    depth: usize,
) {
    // Convert the blockquote content to a temporary buffer
    let mut inner = String::new();

    if let Some(node) = sel.nodes().first() {
        for child in node.children() {
            if child.is_text() {
                let text = child.text();
                let trimmed = text.trim();
                if !trimmed.is_empty() {
                    inner.push_str(trimmed);
                }
            } else if child.is_element() {
                let child_sel = Selection::from(child);
                convert_node(&child_sel, &mut inner, options, depth);
            }
        }
    }

    // Prefix each line with `> `
    let inner_trimmed = inner.trim();
    if inner_trimmed.is_empty() {
        return;
    }

    for line in inner_trimmed.lines() {
        output.push_str("> ");
        output.push_str(line);
        output.push('\n');
    }
    output.push('\n');
}

/// Convert horizontal rule element.
pub(crate) fn convert_hr(output: &mut String) {
    output.push_str("\n---\n\n");
}

/// Convert line break element.
pub(crate) fn convert_br(output: &mut String) {
    output.push_str("  \n");
}

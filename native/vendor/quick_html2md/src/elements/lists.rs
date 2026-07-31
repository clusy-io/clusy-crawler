//! List element conversion with proper nested list handling.

use std::fmt::Write;

use dom_query::Selection;

use crate::options::MarkdownOptions;
use crate::utils::get_tag_name;

use super::inline::convert_inline_content_impl;

/// Convert list element (ul/ol) to markdown.
pub(crate) fn convert_list(
    sel: &Selection,
    output: &mut String,
    options: &MarkdownOptions,
    ordered: bool,
    depth: usize,
) {
    let indent_str = options.list_indent();
    let indent = indent_str.repeat(depth);
    let mut index = 1;

    for node in sel.children().nodes() {
        let child = Selection::from(*node);
        let tag = get_tag_name(&child);

        if tag == "li" {
            output.push_str(&indent);
            if ordered {
                let _ = write!(output, "{index}. ");
                index += 1;
            } else {
                output.push_str("- ");
            }

            // Convert list item content, excluding nested lists
            convert_li_content(&child, output, options, depth);
            output.push('\n');

            // Handle nested lists (these were excluded from inline content)
            for nested in child.children().nodes() {
                let nested_sel = Selection::from(*nested);
                let nested_tag = get_tag_name(&nested_sel);
                if nested_tag == "ul" || nested_tag == "ol" {
                    convert_list(&nested_sel, output, options, nested_tag == "ol", depth + 1);
                }
            }
        }
    }
}

/// Convert list item content, excluding nested lists.
fn convert_li_content(
    li: &Selection,
    output: &mut String,
    options: &MarkdownOptions,
    depth: usize,
) {
    // Use the skip_lists variant to exclude nested ul/ol elements
    convert_inline_content_impl(li, output, options, true, depth);
}

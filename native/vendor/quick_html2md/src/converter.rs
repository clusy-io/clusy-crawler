//! Core conversion logic for HTML to Markdown.

use dom_query::Selection;

use crate::elements::{blocks, code, headings, inline, lists, tables};
use crate::options::MarkdownOptions;
use crate::utils::get_tag_name;

/// Convert a DOM node to markdown.
///
/// # Arguments
/// * `sel` - The DOM selection to convert
/// * `output` - The output string buffer
/// * `options` - Markdown conversion options
/// * `depth` - Current nesting depth (used for list indentation)
pub(crate) fn convert_node(
    sel: &Selection,
    output: &mut String,
    options: &MarkdownOptions,
    depth: usize,
) {
    let tag = get_tag_name(sel);

    match tag.as_str() {
        // Headings
        "h1" | "h2" | "h3" | "h4" | "h5" | "h6" if options.preserve_headings => {
            if let Some(level) = tag.chars().nth(1).and_then(|c| c.to_digit(10)) {
                headings::convert_heading(sel, output, options, level as usize, depth);
            }
        }

        // Paragraphs
        "p" => {
            blocks::convert_paragraph(sel, output, options, depth);
        }

        // Line breaks
        "br" => {
            blocks::convert_br(output);
        }

        // Horizontal rules
        "hr" => {
            blocks::convert_hr(output);
        }

        // Lists
        "ul" if options.preserve_lists => {
            lists::convert_list(sel, output, options, false, depth);
            output.push('\n');
        }
        "ol" if options.preserve_lists => {
            lists::convert_list(sel, output, options, true, depth);
            output.push('\n');
        }

        // Blockquotes
        "blockquote" if options.preserve_blockquotes => {
            blocks::convert_blockquote(sel, output, options, depth);
        }

        // Code
        "pre" if options.preserve_code => {
            code::convert_code_block(sel, output);
        }
        "code" if options.preserve_code => {
            inline::convert_inline_code(sel, output);
        }

        // Emphasis
        "strong" | "b" if options.preserve_emphasis => {
            inline::convert_strong(sel, output, options, depth);
        }
        "em" | "i" if options.preserve_emphasis => {
            inline::convert_emphasis(sel, output, options, depth);
        }

        // Strikethrough (GFM extension - check both option and commonmark mode)
        "del" | "s" | "strike" if options.preserve_strikethrough && !options.commonmark => {
            inline::convert_strikethrough(sel, output, options, depth);
        }

        // Links
        "a" if options.include_links => {
            inline::convert_link(sel, output, options, depth);
        }

        // Images
        "img" if options.include_images => {
            inline::convert_image(sel, output, options);
        }

        // Tables (GFM extension - check both option and commonmark mode)
        "table" if options.preserve_tables && !options.commonmark => {
            tables::convert_table(sel, output);
            output.push('\n');
        }

        // Unknown elements or disabled options: recurse into children
        _ => {
            recurse_children(sel, output, options, depth);
        }
    }
}

/// Recursively process children of an element.
/// Handles both text nodes and element nodes.
fn recurse_children(
    sel: &Selection,
    output: &mut String,
    options: &MarkdownOptions,
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

//! Heading element conversion.

use dom_query::Selection;

use crate::options::MarkdownOptions;

use super::inline::convert_inline_content;

/// Pre-computed heading prefixes to avoid repeated allocations.
const HEADING_PREFIXES: [&str; 7] = ["", "# ", "## ", "### ", "#### ", "##### ", "###### "];

/// Convert heading element (h1-h6) to markdown.
/// If the heading level exceeds max_heading_level, it is converted to a paragraph.
pub(crate) fn convert_heading(
    sel: &Selection,
    output: &mut String,
    options: &MarkdownOptions,
    level: usize,
    depth: usize,
) {
    if level <= options.max_heading_level as usize && level < HEADING_PREFIXES.len() {
        output.push_str(HEADING_PREFIXES[level]);
        convert_inline_content(sel, output, options, depth);
        output.push_str("\n\n");
    } else {
        // Heading level exceeds max_heading_level, convert to paragraph
        convert_inline_content(sel, output, options, depth);
        output.push_str("\n\n");
    }
}

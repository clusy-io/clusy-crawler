//! Tree manipulation with lxml-style text/tail model.
//!
//! This module provides functions for working with the text/tail model
//! used in lxml-style HTML processing.
//!
//! ## Text vs Tail
//!
//! In this model, elements have:
//! - **Text**: Text content BEFORE the first child element
//! - **Tail**: Text content AFTER the element's closing tag
//!
//! ```html
//! <div>
//!   TEXT HERE          <!-- div's "text" -->
//!   <span>inner</span>
//!   TAIL HERE          <!-- span's "tail" -->
//! </div>
//! ```

use dom_query::Selection;
pub use dom_query::Document;

/// Get text before first child element.
///
/// Returns text nodes that appear before any child element.
#[must_use]
pub fn text(sel: &Selection) -> String {
    let mut result = String::new();

    if let Some(node) = sel.nodes().first() {
        for child in node.children() {
            if child.is_element() {
                break; // Stop at first element
            }
            if child.is_text() {
                let text_content = child.text();
                result.push_str(&text_content);
            }
        }
    }

    result
}

/// Get text after element's closing tag (tail).
///
/// Returns text nodes that follow this element until the next sibling element.
#[must_use]
pub fn tail(sel: &Selection) -> String {
    let mut result = String::new();

    if let Some(node) = sel.nodes().first() {
        let mut next = node.next_sibling();

        while let Some(sibling) = next {
            if sibling.is_element() {
                break; // Stop at next element
            }
            if sibling.is_text() {
                let text_content = sibling.text();
                result.push_str(&text_content);
            }
            next = sibling.next_sibling();
        }
    }

    result
}

/// Set text before first child element.
///
/// Removes existing pre-element text and inserts new text.
pub fn set_text(sel: &Selection, new_text: &str) {
    // Remove existing text nodes before first element
    if let Some(node) = sel.nodes().first() {
        let mut to_remove = Vec::new();

        for child in node.children() {
            if child.is_element() {
                break;
            }
            if child.is_text() {
                to_remove.push(child);
            }
        }

        for text_node in to_remove {
            Selection::from(text_node).remove();
        }
    }

    // Prepend new text
    if !new_text.is_empty() {
        let escaped = escape_html(new_text);
        sel.prepend_html(escaped.as_str());
    }
}

/// Set tail text after element.
///
/// Removes existing tail text nodes and inserts new text after element.
pub fn set_tail(sel: &Selection, new_tail: &str) {
    // Remove existing tail nodes using helper
    for tail_node in tail_nodes(sel) {
        Selection::from(tail_node).remove();
    }

    // Insert new tail text after element
    if !new_tail.is_empty() {
        let escaped = escape_html(new_tail);
        sel.after_html(escaped.as_str());
    }
}

/// Get all tail text nodes for an element.
///
/// Returns a vector of text nodes that follow this element.
#[must_use]
pub fn tail_nodes<'a>(sel: &Selection<'a>) -> Vec<dom_query::NodeRef<'a>> {
    let mut nodes = Vec::new();

    if let Some(node) = sel.nodes().first() {
        let mut next = node.next_sibling();

        while let Some(sibling) = next {
            if sibling.is_element() {
                break;
            }
            if sibling.is_text() {
                nodes.push(sibling);
            }
            next = sibling.next_sibling();
        }
    }

    nodes
}

/// Check if tag is a void element (self-closing).
///
/// Void elements like `<br>`, `<hr>`, `<img>` cannot have children.
#[must_use]
pub fn is_void_element(tag: &str) -> bool {
    matches!(
        tag.to_lowercase().as_str(),
        "area" | "base" | "br" | "col" | "embed" | "hr" | "img"
            | "input" | "link" | "meta" | "param" | "source" | "track" | "wbr"
    )
}

/// Escape HTML entities for safe insertion.
fn escape_html(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#39;")
}

/// Get all text content with separator at level changes.
#[must_use]
pub fn iter_text(sel: &Selection, separator: &str) -> String {
    let mut result = String::new();
    let mut last_level = 0;

    if let Some(node) = sel.nodes().first() {
        traverse_for_text(node, 0, &mut last_level, separator, &mut result);
    }

    result.trim().to_string()
}

fn traverse_for_text(
    node: &dom_query::NodeRef,
    level: usize,
    last_level: &mut usize,
    sep: &str,
    result: &mut String,
) {
    if node.is_text() {
        if level != *last_level && !result.is_empty() {
            result.push_str(sep);
        }
        let text_content = node.text();
        result.push_str(&text_content);
    } else if node.is_element() {
        // Check if void element - add separator
        if let Some(tag) = node.node_name() {
            if is_void_element(&tag) && !result.is_empty() {
                result.push_str(sep);
            }
        }
    }
    *last_level = level;

    for child in node.children() {
        traverse_for_text(&child, level + 1, last_level, sep, result);
    }
}

/// Create a new element as a Document.
///
/// Table elements (tr, th, td, tbody, thead, tfoot) are wrapped in proper
/// table context for correct HTML parsing.
#[must_use]
pub fn element(tag: &str) -> Document {
    // Table elements need to be wrapped in proper context for parsing
    match tag.to_lowercase().as_str() {
        "tr" | "th" | "td" | "tbody" | "thead" | "tfoot" => {
            Document::from(format!("<table><{tag}></{tag}></table>"))
        }
        _ => Document::from(format!("<{tag}></{tag}>")),
    }
}

/// Create child element and append to parent.
#[must_use]
pub fn sub_element<'a>(parent: &Selection<'a>, tag: &str) -> Selection<'a> {
    let html = format!("<{tag}></{tag}>");
    parent.append_html(html.as_str());
    parent.children().last()
}

/// Remove element from tree.
///
/// # Arguments
/// * `sel` - Element to remove
/// * `keep_tail` - If true, preserve tail text
pub fn remove(sel: &Selection, keep_tail: bool) {
    if !keep_tail {
        // Also remove tail text nodes
        if let Some(node) = sel.nodes().first() {
            let mut next = node.next_sibling();
            let mut to_remove = Vec::new();

            while let Some(sibling) = next {
                if sibling.is_element() {
                    break;
                }
                if sibling.is_text() {
                    to_remove.push(sibling);
                }
                next = sibling.next_sibling();
            }

            for text_node in to_remove {
                Selection::from(text_node).remove();
            }
        }
    }
    sel.remove();
}

/// Strip element but keep children.
///
/// Moves children to parent, then removes the element.
pub fn strip(sel: &Selection) {
    if let Some(node) = sel.nodes().first() {
        // Move first child (and all its siblings) before this node
        if let Some(first_child) = node.first_child() {
            node.insert_siblings_before(&first_child);
        }
        // Remove the now-empty element
        node.remove_from_parent();
    }
}

/// Check if tag is safe to use as a CSS selector.
///
/// Prevents CSS injection by ensuring tag contains only valid characters.
fn is_safe_tag_selector(tag: &str) -> bool {
    !tag.is_empty()
        && tag
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
}

/// Remove all elements with given tags.
pub fn strip_elements(tree: &Selection, keep_tail: bool, tags: &[&str]) {
    for tag in tags {
        let nodes: Vec<_> = if is_safe_tag_selector(tag) {
            tree.select(tag).nodes().to_vec()
        } else {
            // Fallback to manual filtering for unsafe selectors
            let target = tag.to_ascii_lowercase();
            tree.select("*")
                .nodes()
                .iter()
                .copied()
                .filter(|n| {
                    n.node_name()
                        .is_some_and(|name| name.to_ascii_lowercase() == target)
                })
                .collect()
        };
        for node in nodes.into_iter().rev() {
            let sel = Selection::from(node);
            remove(&sel, keep_tail);
        }
    }
}

/// Iterate elements matching tags.
#[must_use]
pub fn iter<'a>(sel: &Selection<'a>, tags: &[&str]) -> Selection<'a> {
    if tags.is_empty() {
        sel.select("*")
    } else {
        sel.select(&tags.join(","))
    }
}

/// Like `iter` but excludes the element itself.
#[must_use]
pub fn iter_descendants<'a>(sel: &Selection<'a>, tags: &[&str]) -> Selection<'a> {
    // select() already excludes self, so same as iter
    iter(sel, tags)
}

/// Strip tags from selection, keeping their content.
///
/// Similar to `strip_elements` but uses `strip()` instead of `remove()`.
pub fn strip_tags(tree: &Selection, tags: &[&str]) {
    for tag in tags {
        let nodes: Vec<_> = if is_safe_tag_selector(tag) {
            tree.select(tag).nodes().to_vec()
        } else {
            // Fallback to manual filtering for unsafe selectors
            let target = tag.to_ascii_lowercase();
            tree.select("*")
                .nodes()
                .iter()
                .copied()
                .filter(|n| {
                    n.node_name()
                        .is_some_and(|name| name.to_ascii_lowercase() == target)
                })
                .collect()
        };
        for node in nodes.into_iter().rev() {
            let sel = Selection::from(node);
            strip(&sel);
        }
    }
}

/// Append child element.
pub fn append(parent: &Selection, child: &Selection) {
    parent.append_selection(child);
}

/// Append multiple children.
pub fn extend(parent: &Selection, children: &[&Selection]) {
    for child in children {
        append(parent, child);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_text_before_children() {
        let doc = Document::from("<div>Hello <span>World</span></div>");
        let div = doc.select("div");
        assert_eq!(text(&div), "Hello ");
    }

    #[test]
    fn test_text_no_children() {
        let doc = Document::from("<p>Just text</p>");
        let p = doc.select("p");
        assert_eq!(text(&p), "Just text");
    }

    #[test]
    fn test_text_empty() {
        let doc = Document::from("<div><span>only child</span></div>");
        let div = doc.select("div");
        assert_eq!(text(&div), "");
    }

    #[test]
    fn test_tail_after_element() {
        let doc = Document::from("<div><span>inner</span> tail text</div>");
        let span = doc.select("span");
        assert_eq!(tail(&span), " tail text");
    }

    #[test]
    fn test_tail_no_tail() {
        let doc = Document::from("<div><span>inner</span></div>");
        let span = doc.select("span");
        assert_eq!(tail(&span), "");
    }

    #[test]
    fn test_tail_stops_at_next_element() {
        let doc = Document::from("<div><span>1</span> tail <span>2</span></div>");
        let first_span = doc.select("span").first();
        assert_eq!(tail(&first_span), " tail ");
    }

    #[test]
    fn test_tail_nodes() {
        let doc = Document::from("<div><span>1</span> text1 text2 <span>2</span></div>");
        let first_span = doc.select("span").first();
        let nodes = tail_nodes(&first_span);
        assert!(!nodes.is_empty());
    }

    #[test]
    fn test_set_text() {
        let doc = Document::from("<div>Old text<span>child</span></div>");
        let div = doc.select("div");
        set_text(&div, "New text");
        assert_eq!(text(&div), "New text");
        assert!(doc.select("span").exists());
    }

    #[test]
    fn test_set_tail() {
        let doc = Document::from("<div><span>inner</span>Old tail</div>");
        let span = doc.select("span");
        set_tail(&span, "New tail");
        assert_eq!(tail(&span), "New tail");
    }

    #[test]
    fn test_element_creation() {
        let doc = element("p");
        assert!(doc.select("p").exists());
    }

    #[test]
    fn test_sub_element() {
        let doc = Document::from("<div></div>");
        let div = doc.select("div");
        let _span = sub_element(&div, "span");
        assert!(doc.select("div > span").exists());
    }

    #[test]
    fn test_remove_with_tail() {
        let doc = Document::from("<div>text <span>remove</span> keep this</div>");
        let span = doc.select("span");
        remove(&span, true); // Keep tail
        assert!(doc.select("span").is_empty());
        assert!(doc.select("div").text().contains("keep this"));
    }

    #[test]
    fn test_remove_without_tail() {
        let doc = Document::from("<div>text <span>remove</span> remove this too</div>");
        let span = doc.select("span");
        remove(&span, false); // Remove tail too
        let div_text = doc.select("div").text().to_string();
        assert!(!div_text.contains("remove this"));
    }

    #[test]
    fn test_strip() {
        let doc = Document::from("<div><p><span>inner</span> text</p></div>");
        let p = doc.select("p");
        strip(&p);
        assert!(doc.select("p").is_empty());
        assert!(doc.select("span").exists());
    }

    #[test]
    fn test_strip_preserves_children() {
        let doc = Document::from("<div><p><span>inner</span> text</p></div>");
        let p = doc.select("p");
        strip(&p);
        assert!(doc.select("p").is_empty());
        assert_eq!(doc.select("span").length(), 1);
        assert!(doc.select("div").text().contains("inner"));
    }

    #[test]
    fn test_strip_empty_element() {
        let doc = Document::from("<div><p></p><span>kept</span></div>");
        let p = doc.select("p");
        strip(&p);
        assert!(doc.select("p").is_empty());
        assert_eq!(doc.select("span").length(), 1);
    }

    #[test]
    fn test_strip_elements_keep_tail() {
        let doc = Document::from("<div><b>bold</b> tail<i>italic</i> more</div>");
        let div = doc.select("div");
        strip_elements(&div, true, &["b", "i"]);
        assert!(doc.select("b").is_empty());
        assert!(doc.select("i").is_empty());
        let text_result = div.text().to_string();
        assert!(text_result.contains("tail"));
        assert!(text_result.contains("more"));
    }

    #[test]
    fn test_strip_elements_remove_tail() {
        let doc = Document::from("<div><b>bold</b> tail<i>italic</i> more</div>");
        let div = doc.select("div");
        strip_elements(&div, false, &["b", "i"]);
        assert!(doc.select("b").is_empty());
        assert!(doc.select("i").is_empty());
        let text_result = div.text().to_string();
        assert!(!text_result.contains("tail"));
        assert!(!text_result.contains("more"));
    }

    #[test]
    fn test_strip_tags() {
        let doc = Document::from("<div><b>bold</b> text <i>italic</i></div>");
        let div = doc.select("div");
        strip_tags(&div, &["b", "i"]);
        assert!(doc.select("b").is_empty());
        assert!(doc.select("i").is_empty());
        // Content should be preserved
        let text_result = div.text().to_string();
        assert!(text_result.contains("bold"));
        assert!(text_result.contains("italic"));
    }

    #[test]
    fn test_iter_all_elements() {
        let doc = Document::from("<div><p>1</p><span>2</span><p>3</p></div>");
        let div = doc.select("div");
        let all = iter(&div, &[]);
        assert_eq!(all.length(), 3); // p, span, p
    }

    #[test]
    fn test_iter_filtered() {
        let doc = Document::from("<div><p>1</p><span>2</span><p>3</p></div>");
        let div = doc.select("div");
        let only_p = iter(&div, &["p"]);
        assert_eq!(only_p.length(), 2); // Both p tags
    }

    #[test]
    fn test_iter_text_with_separator() {
        let doc = Document::from("<p>Hello<span>World</span>!</p>");
        let p = doc.select("p");
        let result = iter_text(&p, " ");
        assert_eq!(result, "Hello World !");
    }

    #[test]
    fn test_is_void_element() {
        assert!(is_void_element("br"));
        assert!(is_void_element("BR"));
        assert!(is_void_element("img"));
        assert!(is_void_element("hr"));
        assert!(!is_void_element("div"));
        assert!(!is_void_element("span"));
    }

    #[test]
    fn test_extend() {
        let doc = Document::from("<div></div>");
        let div = doc.select("div");

        let doc1 = Document::from("<span>1</span>");
        let child1 = doc1.select("span");
        let doc2 = Document::from("<span>2</span>");
        let child2 = doc2.select("span");

        extend(&div, &[&child1, &child2]);

        assert_eq!(doc.select("div > span").length(), 2);
    }
}

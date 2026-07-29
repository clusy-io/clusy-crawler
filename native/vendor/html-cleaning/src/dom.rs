//! DOM helper utilities.
//!
//! Convenience functions for common DOM operations.

use dom_query::Selection;
pub use dom_query::Document;

/// Get text content of element (recursive).
///
/// # Example
///
/// ```
/// use html_cleaning::dom;
///
/// let doc = dom::parse("<div>Hello <span>World</span></div>");
/// assert_eq!(dom::text_content(&doc.select("div")), "Hello World");
/// ```
#[must_use]
pub fn text_content(sel: &Selection) -> String {
    sel.text().to_string()
}

/// Get direct text of element (non-recursive, excludes nested element text).
///
/// # Example
///
/// ```
/// use html_cleaning::dom;
///
/// let doc = dom::parse("<div>Direct <span>Nested</span> text</div>");
/// let direct = dom::direct_text(&doc.select("div"));
/// assert!(direct.contains("Direct"));
/// assert!(!direct.contains("Nested"));
/// ```
#[must_use]
pub fn direct_text(sel: &Selection) -> String {
    sel.nodes()
        .first()
        .map(|node| {
            node.children()
                .into_iter()
                .filter(dom_query::NodeRef::is_text)
                .map(|text_node| text_node.text().to_string())
                .collect::<String>()
        })
        .unwrap_or_default()
}

/// Get tag name (lowercase).
///
/// # Example
///
/// ```
/// use html_cleaning::dom;
///
/// let doc = dom::parse("<ARTICLE>Content</ARTICLE>");
/// assert_eq!(dom::tag_name(&doc.select("article")), Some("article".to_string()));
/// ```
#[must_use]
pub fn tag_name(sel: &Selection) -> Option<String> {
    sel.nodes()
        .first()
        .and_then(dom_query::NodeRef::node_name)
        .map(|t| t.to_string())
}

/// Get attribute value.
///
/// # Example
///
/// ```
/// use html_cleaning::dom;
///
/// let doc = dom::parse(r#"<a href="https://example.com">Link</a>"#);
/// assert_eq!(dom::get_attribute(&doc.select("a"), "href"), Some("https://example.com".to_string()));
/// ```
#[must_use]
pub fn get_attribute(sel: &Selection, name: &str) -> Option<String> {
    sel.attr(name).map(|s| s.to_string())
}

/// Set attribute value.
pub fn set_attribute(sel: &Selection, name: &str, value: &str) {
    sel.set_attr(name, value);
}

/// Remove attribute.
pub fn remove_attribute(sel: &Selection, name: &str) {
    sel.remove_attr(name);
}

/// Check if attribute exists.
#[must_use]
pub fn has_attribute(sel: &Selection, name: &str) -> bool {
    sel.has_attr(name)
}

/// Get all attributes as key-value pairs.
#[must_use]
pub fn get_all_attributes(sel: &Selection) -> Vec<(String, String)> {
    sel.nodes()
        .first()
        .map(|node| {
            node.attrs()
                .iter()
                .map(|attr| (attr.name.local.to_string(), attr.value.to_string()))
                .collect()
        })
        .unwrap_or_default()
}

/// Get direct element children.
#[must_use]
pub fn children<'a>(sel: &Selection<'a>) -> Selection<'a> {
    sel.children()
}

/// Get parent element.
#[must_use]
pub fn parent<'a>(sel: &Selection<'a>) -> Selection<'a> {
    sel.parent()
}

/// Get next element sibling (skipping text nodes).
#[must_use]
pub fn next_element_sibling<'a>(sel: &Selection<'a>) -> Option<Selection<'a>> {
    sel.nodes().first().and_then(|node| {
        let mut sibling = node.next_sibling();
        while let Some(s) = sibling {
            if s.is_element() {
                return Some(Selection::from(s));
            }
            sibling = s.next_sibling();
        }
        None
    })
}

/// Get previous element sibling (skipping text nodes).
#[must_use]
pub fn previous_element_sibling<'a>(sel: &Selection<'a>) -> Option<Selection<'a>> {
    sel.nodes().first().and_then(|node| {
        let mut sibling = node.prev_sibling();
        while let Some(s) = sibling {
            if s.is_element() {
                return Some(Selection::from(s));
            }
            sibling = s.prev_sibling();
        }
        None
    })
}

/// Check if element is a void element (self-closing).
#[must_use]
pub fn is_void_element(sel: &Selection) -> bool {
    const VOID_ELEMENTS: &[&str] = &[
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
        "source", "track", "wbr",
    ];

    tag_name(sel).is_some_and(|t| VOID_ELEMENTS.contains(&t.as_str()))
}

/// Check if element has specified class.
#[must_use]
pub fn has_class(sel: &Selection, class: &str) -> bool {
    sel.attr("class")
        .is_some_and(|c| c.split_whitespace().any(|c| c == class))
}

/// Add a class to the element.
///
/// # Example
///
/// ```
/// use html_cleaning::dom;
///
/// let doc = dom::parse(r#"<div class="foo">Content</div>"#);
/// let div = doc.select("div");
/// dom::add_class(&div, "bar");
/// assert!(dom::has_class(&div, "foo"));
/// assert!(dom::has_class(&div, "bar"));
/// ```
pub fn add_class(sel: &Selection, class: &str) {
    if class.is_empty() {
        return;
    }

    match sel.attr("class") {
        Some(existing) => {
            // Check if class already exists
            if !existing.split_whitespace().any(|c| c == class) {
                let new_class = format!("{existing} {class}");
                sel.set_attr("class", &new_class);
            }
        }
        None => {
            sel.set_attr("class", class);
        }
    }
}

/// Remove a class from the element.
pub fn remove_class(sel: &Selection, class: &str) {
    if let Some(existing) = sel.attr("class") {
        let new_class: Vec<&str> = existing
            .split_whitespace()
            .filter(|c| *c != class)
            .collect();

        if new_class.is_empty() {
            sel.remove_attr("class");
        } else {
            sel.set_attr("class", &new_class.join(" "));
        }
    }
}

/// Check if element matches a CSS selector.
///
/// # Example
///
/// ```
/// use html_cleaning::dom;
///
/// let doc = dom::parse(r#"<div class="content" id="main">Text</div>"#);
/// let div = doc.select("div");
/// assert!(dom::matches(&div, ".content"));
/// assert!(dom::matches(&div, "#main"));
/// assert!(!dom::matches(&div, ".sidebar"));
/// ```
#[must_use]
pub fn matches(sel: &Selection, selector: &str) -> bool {
    sel.is(selector)
}

/// Get inner HTML content.
#[must_use]
pub fn inner_html(sel: &Selection) -> String {
    sel.inner_html().to_string()
}

/// Get outer HTML content.
#[must_use]
pub fn outer_html(sel: &Selection) -> String {
    sel.html().to_string()
}

/// Parse HTML string into Document.
#[must_use]
pub fn parse(html: &str) -> Document {
    Document::from(html)
}

/// Clone a document.
#[must_use]
pub fn clone_document(doc: &Document) -> Document {
    Document::from(doc.html().to_string())
}

/// Rename element tag.
pub fn rename(sel: &Selection, new_tag: &str) {
    sel.rename(new_tag);
}

/// Remove element from DOM.
pub fn remove(sel: &Selection) {
    sel.remove();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_text_content() {
        let doc = parse("<div>Hello <span>World</span></div>");
        let div = doc.select("div");
        assert_eq!(text_content(&div), "Hello World");
    }

    #[test]
    fn test_tag_name() {
        let doc = parse("<article>Content</article>");
        let article = doc.select("article");
        assert_eq!(tag_name(&article), Some("article".to_string()));
    }

    #[test]
    fn test_attributes() {
        let doc = parse(r#"<a href="url" class="link">Link</a>"#);
        let a = doc.select("a");

        assert_eq!(get_attribute(&a, "href"), Some("url".to_string()));
        assert!(has_attribute(&a, "class"));
        assert!(!has_attribute(&a, "id"));

        let attrs = get_all_attributes(&a);
        assert_eq!(attrs.len(), 2);
    }

    #[test]
    fn test_is_void_element() {
        let doc = parse("<div><br><img src='x'><p>text</p></div>");

        assert!(is_void_element(&doc.select("br")));
        assert!(is_void_element(&doc.select("img")));
        assert!(!is_void_element(&doc.select("p")));
        assert!(!is_void_element(&doc.select("div")));
    }

    #[test]
    fn test_has_class() {
        let doc = parse(r#"<div class="foo bar baz">Content</div>"#);
        let div = doc.select("div");

        assert!(has_class(&div, "foo"));
        assert!(has_class(&div, "bar"));
        assert!(!has_class(&div, "qux"));
    }

    #[test]
    fn test_navigation() {
        let doc = parse("<div><p>1</p><span>2</span><p>3</p></div>");

        let span = doc.select("span");
        let prev = previous_element_sibling(&span);
        let next = next_element_sibling(&span);

        assert!(prev.is_some());
        assert_eq!(tag_name(&prev.unwrap()), Some("p".to_string()));
        assert!(next.is_some());
        assert_eq!(tag_name(&next.unwrap()), Some("p".to_string()));
    }

    #[test]
    fn test_direct_text() {
        let doc = parse("<div>Direct text<span>Nested</span> more direct</div>");
        let div = doc.select("div");

        let direct = direct_text(&div);
        assert!(direct.contains("Direct text"));
        assert!(direct.contains("more direct"));
        assert!(!direct.contains("Nested"));
    }

    #[test]
    fn test_matches() {
        let doc = parse(r#"<div class="foo" id="bar">Content</div>"#);
        let div = doc.select("div");

        assert!(matches(&div, "div"));
        assert!(matches(&div, ".foo"));
        assert!(matches(&div, "#bar"));
        assert!(matches(&div, "div.foo"));
        assert!(!matches(&div, "span"));
        assert!(!matches(&div, ".baz"));
    }

    #[test]
    fn test_add_class() {
        let doc = parse(r#"<div class="existing">Content</div>"#);
        let div = doc.select("div");

        add_class(&div, "new");
        assert!(has_class(&div, "existing"));
        assert!(has_class(&div, "new"));

        // Adding same class again shouldn't duplicate
        add_class(&div, "new");
        let class_attr = get_attribute(&div, "class").unwrap();
        assert_eq!(class_attr.matches("new").count(), 1);
    }

    #[test]
    fn test_add_class_to_element_without_class() {
        let doc = parse("<div>Content</div>");
        let div = doc.select("div");

        add_class(&div, "new");
        assert!(has_class(&div, "new"));
    }

    #[test]
    fn test_remove_class() {
        let doc = parse(r#"<div class="foo bar baz">Content</div>"#);
        let div = doc.select("div");

        remove_class(&div, "bar");
        assert!(has_class(&div, "foo"));
        assert!(!has_class(&div, "bar"));
        assert!(has_class(&div, "baz"));
    }

    #[test]
    fn test_remove_last_class() {
        let doc = parse(r#"<div class="only">Content</div>"#);
        let div = doc.select("div");

        remove_class(&div, "only");
        assert!(!has_class(&div, "only"));
        assert!(!has_attribute(&div, "class"));
    }
}

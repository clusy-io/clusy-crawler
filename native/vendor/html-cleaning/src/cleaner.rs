//! Core HTML cleaning functionality.

use crate::options::CleaningOptions;
use dom_query::Document;
use std::collections::HashSet;

/// HTML cleaning utility.
///
/// Provides methods for removing, stripping, and normalizing HTML elements.
///
/// # Example
///
/// ```
/// use html_cleaning::{HtmlCleaner, CleaningOptions};
/// use dom_query::Document;
///
/// let options = CleaningOptions {
///     tags_to_remove: vec!["script".into(), "style".into()],
///     prune_empty: true,
///     ..Default::default()
/// };
///
/// let cleaner = HtmlCleaner::with_options(options);
/// let doc = Document::from("<div><script>x</script><p>Hello</p></div>");
/// cleaner.clean(&doc);
/// assert!(doc.select("script").is_empty());
/// ```
#[derive(Debug, Clone)]
pub struct HtmlCleaner {
    options: CleaningOptions,
}

impl Default for HtmlCleaner {
    fn default() -> Self {
        Self::new()
    }
}

impl HtmlCleaner {
    /// Create a cleaner with default options.
    #[must_use]
    pub fn new() -> Self {
        Self {
            options: CleaningOptions::default(),
        }
    }

    /// Create a cleaner with custom options.
    #[must_use]
    pub fn with_options(options: CleaningOptions) -> Self {
        Self { options }
    }

    /// Get a reference to the current options.
    #[must_use]
    pub fn options(&self) -> &CleaningOptions {
        &self.options
    }

    /// Apply all configured cleaning operations to the document.
    ///
    /// Operations are applied in this order:
    /// 1. Remove tags (with children)
    /// 2. Strip tags (keep children)
    /// 3. Remove by CSS selector
    /// 4. Prune empty elements
    /// 5. Normalize whitespace
    /// 6. Clean attributes
    pub fn clean(&self, doc: &Document) {
        // 1. Remove tags
        if !self.options.tags_to_remove.is_empty() {
            let tags: Vec<&str> = self.options.tags_to_remove.iter().map(String::as_str).collect();
            self.remove_tags(doc, &tags);
        }

        // 2. Strip tags
        if !self.options.tags_to_strip.is_empty() {
            let tags: Vec<&str> = self.options.tags_to_strip.iter().map(String::as_str).collect();
            self.strip_tags(doc, &tags);
        }

        // 3. Remove by selector
        for selector in &self.options.selectors_to_remove {
            self.remove_by_selector(doc, selector);
        }

        // 4. Prune empty
        if self.options.prune_empty {
            self.prune_empty(doc);
        }

        // 5. Normalize whitespace
        if self.options.normalize_whitespace {
            self.normalize_text(doc);
        }

        // 6. Clean attributes
        if self.options.strip_attributes {
            self.clean_attributes(doc);
        }
    }

    /// Remove elements matching tags (including all children).
    ///
    /// # Example
    ///
    /// ```
    /// use html_cleaning::HtmlCleaner;
    /// use dom_query::Document;
    ///
    /// let cleaner = HtmlCleaner::new();
    /// let doc = Document::from("<div><script>bad</script><p>good</p></div>");
    /// cleaner.remove_tags(&doc, &["script"]);
    /// assert!(doc.select("script").is_empty());
    /// ```
    pub fn remove_tags(&self, doc: &Document, tags: &[&str]) {
        if tags.is_empty() {
            return;
        }
        let selector = tags.join(", ");
        doc.select(&selector).remove();
    }

    /// Strip tags but preserve their children.
    ///
    /// The tag wrapper is removed but inner content (text and child elements)
    /// is moved to the parent.
    ///
    /// # Example
    ///
    /// ```
    /// use html_cleaning::HtmlCleaner;
    /// use dom_query::Document;
    ///
    /// let cleaner = HtmlCleaner::new();
    /// let doc = Document::from("<div><span>text</span></div>");
    /// cleaner.strip_tags(&doc, &["span"]);
    /// assert!(doc.select("span").is_empty());
    /// ```
    pub fn strip_tags(&self, doc: &Document, tags: &[&str]) {
        if tags.is_empty() {
            return;
        }
        let root = doc.select("*").first();
        if root.exists() {
            root.strip_elements(tags);
        }
    }

    /// Remove elements matching a CSS selector.
    ///
    /// # Example
    ///
    /// ```
    /// use html_cleaning::HtmlCleaner;
    /// use dom_query::Document;
    ///
    /// let cleaner = HtmlCleaner::new();
    /// let doc = Document::from(r#"<div class="ad">Ad</div><p>Content</p>"#);
    /// cleaner.remove_by_selector(&doc, ".ad");
    /// assert!(doc.select(".ad").is_empty());
    /// ```
    pub fn remove_by_selector(&self, doc: &Document, selector: &str) {
        doc.select(selector).remove();
    }

    /// Remove empty elements.
    ///
    /// Elements are considered empty if they:
    /// - Have no child elements
    /// - Have no text content (or only whitespace)
    ///
    /// Processes in reverse document order (children before parents).
    pub fn prune_empty(&self, doc: &Document) {
        let empty_tags: Vec<&str> = if self.options.empty_tags.is_empty() {
            vec!["div", "span", "p", "section", "article"]
        } else {
            self.options.empty_tags.iter().map(String::as_str).collect()
        };

        // Loop until no more empty elements found
        loop {
            let mut removed = false;
            for tag in &empty_tags {
                let nodes: Vec<_> = doc.select(tag).nodes().to_vec();
                for node in nodes.into_iter().rev() {
                    let sel = dom_query::Selection::from(node);
                    let children = sel.children();
                    let text = sel.text().to_string();

                    if children.is_empty() && text.trim().is_empty() {
                        sel.remove();
                        removed = true;
                    }
                }
            }
            if !removed {
                break;
            }
        }
    }

    /// Normalize text nodes (trim, collapse whitespace).
    ///
    /// Walks all text nodes and collapses multiple whitespace to single space.
    pub fn normalize_text(&self, doc: &Document) {
        // Process all elements and normalize their text content
        for node in doc.select("*").nodes() {
            let sel = dom_query::Selection::from(*node);

            // Get direct text children and normalize them
            if let Some(n) = sel.nodes().first() {
                for child in n.children() {
                    if child.is_text() {
                        let text = child.text();
                        let text_str = text.to_string();
                        let normalized = crate::text::normalize(&text_str);
                        if text_str != normalized {
                            // Replace text node content by updating via the node
                            child.set_text(normalized);
                        }
                    }
                }
            }
        }
    }

    /// Remove or filter attributes from all elements.
    ///
    /// If `strip_attributes` is true in options:
    /// - Removes all attributes except those in `preserved_attributes`
    pub fn clean_attributes(&self, doc: &Document) {
        let preserved: HashSet<&str> = self
            .options
            .preserved_attributes
            .iter()
            .map(String::as_str)
            .collect();

        for node in doc.select("*").nodes() {
            let sel = dom_query::Selection::from(*node);

            // Get all attribute names first
            let attrs: Vec<String> = sel
                .nodes()
                .first()
                .map(|n| {
                    n.attrs()
                        .iter()
                        .map(|a| a.name.local.to_string())
                        .collect()
                })
                .unwrap_or_default();

            // Remove non-preserved attributes
            for attr in attrs {
                if !preserved.contains(attr.as_str()) {
                    sel.remove_attr(&attr);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_new_cleaner() {
        let cleaner = HtmlCleaner::new();
        assert!(cleaner.options().tags_to_remove.is_empty());
    }

    #[test]
    fn test_remove_tags() {
        let cleaner = HtmlCleaner::new();
        let doc = Document::from("<div><script>bad</script><p>good</p></div>");
        cleaner.remove_tags(&doc, &["script"]);
        assert!(doc.select("script").is_empty());
        assert!(doc.select("p").exists());
    }

    #[test]
    fn test_remove_by_selector() {
        let cleaner = HtmlCleaner::new();
        let doc = Document::from(r#"<div class="ad">Ad</div><p>Content</p>"#);
        cleaner.remove_by_selector(&doc, ".ad");
        assert!(doc.select(".ad").is_empty());
        assert!(doc.select("p").exists());
    }

    #[test]
    fn test_prune_empty() {
        let options = CleaningOptions {
            prune_empty: true,
            ..Default::default()
        };
        let cleaner = HtmlCleaner::with_options(options);
        let doc = Document::from("<div><p></p><p>Content</p></div>");
        cleaner.prune_empty(&doc);
        assert_eq!(doc.select("p").length(), 1);
    }

    #[test]
    fn test_clean_attributes() {
        let options = CleaningOptions {
            strip_attributes: true,
            preserved_attributes: vec!["href".into()],
            ..Default::default()
        };
        let cleaner = HtmlCleaner::with_options(options);
        let doc = Document::from(r#"<a href="url" class="link" id="x">Link</a>"#);
        cleaner.clean_attributes(&doc);

        let a = doc.select("a");
        assert!(a.attr("href").is_some());
        assert!(a.attr("class").is_none());
        assert!(a.attr("id").is_none());
    }

    #[test]
    fn test_strip_tags_preserves_text() {
        let cleaner = HtmlCleaner::new();
        let doc = Document::from("<div><span>Hello</span> <b>World</b></div>");
        cleaner.strip_tags(&doc, &["span", "b"]);

        assert!(doc.select("span").is_empty());
        assert!(doc.select("b").is_empty());
        let text = doc.select("div").text();
        assert!(text.contains("Hello"), "Text 'Hello' should be preserved");
        assert!(text.contains("World"), "Text 'World' should be preserved");
    }

    #[test]
    fn test_normalize_text() {
        let options = CleaningOptions {
            normalize_whitespace: true,
            ..Default::default()
        };
        let cleaner = HtmlCleaner::with_options(options);
        let doc = Document::from("<p>  Multiple   spaces   here  </p>");
        cleaner.normalize_text(&doc);

        let text = doc.select("p").text();
        // Text should have collapsed whitespace
        assert!(!text.contains("  "), "Multiple spaces should be collapsed");
    }
}

//! Configuration options for HTML cleaning.

/// Configuration for HTML cleaning operations.
///
/// # Example
///
/// ```
/// use html_cleaning::CleaningOptions;
///
/// let options = CleaningOptions {
///     tags_to_remove: vec!["script".into(), "style".into()],
///     prune_empty: true,
///     ..Default::default()
/// };
/// ```
#[derive(Debug, Clone, Default)]
pub struct CleaningOptions {
    /// HTML tags to remove completely (including children).
    ///
    /// Example: `["script", "style", "noscript"]`
    pub tags_to_remove: Vec<String>,

    /// HTML tags to strip (remove tag, keep children).
    ///
    /// Example: `["span", "font"]`
    pub tags_to_strip: Vec<String>,

    /// CSS selectors for elements to remove.
    ///
    /// Example: `[".advertisement", "#cookie-banner"]`
    pub selectors_to_remove: Vec<String>,

    /// Remove elements with no text content.
    pub prune_empty: bool,

    /// Tags considered "empty" for pruning.
    ///
    /// Default: `["div", "span", "p", "section", "article"]`
    pub empty_tags: Vec<String>,

    /// Normalize whitespace in text nodes.
    pub normalize_whitespace: bool,

    /// Remove all attributes from elements.
    pub strip_attributes: bool,

    /// Attributes to preserve when `strip_attributes` is true.
    ///
    /// Example: `["href", "src", "alt"]`
    pub preserved_attributes: Vec<String>,
}

impl CleaningOptions {
    /// Create a new builder for `CleaningOptions`.
    #[must_use]
    pub fn builder() -> CleaningOptionsBuilder {
        CleaningOptionsBuilder::default()
    }
}

/// Builder for `CleaningOptions`.
#[derive(Debug, Clone, Default)]
pub struct CleaningOptionsBuilder {
    options: CleaningOptions,
}

impl CleaningOptionsBuilder {
    /// Add tags to remove (including children).
    #[must_use]
    pub fn remove_tags(mut self, tags: &[&str]) -> Self {
        self.options
            .tags_to_remove
            .extend(tags.iter().map(|s| (*s).to_string()));
        self
    }

    /// Add tags to strip (keep children).
    #[must_use]
    pub fn strip_tags(mut self, tags: &[&str]) -> Self {
        self.options
            .tags_to_strip
            .extend(tags.iter().map(|s| (*s).to_string()));
        self
    }

    /// Add CSS selectors to remove.
    #[must_use]
    pub fn remove_selectors(mut self, selectors: &[&str]) -> Self {
        self.options
            .selectors_to_remove
            .extend(selectors.iter().map(|s| (*s).to_string()));
        self
    }

    /// Enable empty element pruning.
    #[must_use]
    pub fn prune_empty(mut self, enabled: bool) -> Self {
        self.options.prune_empty = enabled;
        self
    }

    /// Set tags to consider as empty for pruning.
    #[must_use]
    pub fn empty_tags(mut self, tags: &[&str]) -> Self {
        self.options.empty_tags = tags.iter().map(|s| (*s).to_string()).collect();
        self
    }

    /// Enable whitespace normalization.
    #[must_use]
    pub fn normalize_whitespace(mut self, enabled: bool) -> Self {
        self.options.normalize_whitespace = enabled;
        self
    }

    /// Enable attribute stripping.
    #[must_use]
    pub fn strip_attributes(mut self, enabled: bool) -> Self {
        self.options.strip_attributes = enabled;
        self
    }

    /// Set preserved attributes (when `strip_attributes` is enabled).
    #[must_use]
    pub fn preserve_attributes(mut self, attrs: &[&str]) -> Self {
        self.options.preserved_attributes = attrs.iter().map(|s| (*s).to_string()).collect();
        self
    }

    /// Build the `CleaningOptions`.
    #[must_use]
    pub fn build(self) -> CleaningOptions {
        self.options
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_default_options() {
        let options = CleaningOptions::default();
        assert!(options.tags_to_remove.is_empty());
        assert!(!options.prune_empty);
    }

    #[test]
    fn test_builder() {
        let options = CleaningOptions::builder()
            .remove_tags(&["script", "style"])
            .prune_empty(true)
            .build();

        assert_eq!(options.tags_to_remove.len(), 2);
        assert!(options.prune_empty);
    }
}

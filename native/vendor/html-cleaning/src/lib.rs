//! HTML cleaning, sanitization, and text processing utilities.
//!
//! This crate provides generic HTML cleaning operations useful for web scraping,
//! content extraction, and HTML sanitization.
//!
//! # Quick Start
//!
//! ```
//! use html_cleaning::{HtmlCleaner, CleaningOptions};
//! use dom_query::Document;
//!
//! // Create a cleaner with custom options
//! let options = CleaningOptions::builder()
//!     .remove_tags(&["script", "style"])
//!     .build();
//! let cleaner = HtmlCleaner::with_options(options);
//!
//! let html = "<html><body><script>bad</script><p>Hello!</p></body></html>";
//! let doc = Document::from(html);
//!
//! cleaner.clean(&doc);
//! assert!(doc.select("script").is_empty());
//! assert!(doc.select("p").exists());
//! ```
//!
//! # Features
//!
//! - **HTML Cleaning**: Remove unwanted elements (scripts, styles, forms)
//! - **Tag Stripping**: Remove tags while preserving text content
//! - **Text Normalization**: Collapse whitespace, trim text
//! - **Link Processing**: Make URLs absolute, filter links
//! - **Content Deduplication**: LRU-based duplicate detection
//! - **Presets**: Ready-to-use configurations for common scenarios
//!
//! # Feature Flags
//!
//! | Feature | Default | Description |
//! |---------|---------|-------------|
//! | `presets` | Yes | Include prebuilt cleaning configurations |
//! | `regex` | No | Enable regex-based selectors |
//! | `url` | No | Enable URL processing with the `url` crate |
//! | `full` | No | Enable all features |
//!
//! # Modules
//!
//! - [`cleaner`] - Core `HtmlCleaner` and cleaning operations
//! - [`text`] - Text processing utilities
//! - [`tree`] - lxml-style text/tail tree manipulation
//! - [`dom`] - DOM helper utilities
//! - [`dedup`] - Content deduplication
//! - [`presets`] - Ready-to-use cleaning configurations (feature: `presets`)
//! - [`links`] - URL and link processing (feature: `url`)

#![forbid(unsafe_code)]
#![warn(missing_docs)]

// Core modules - always available
pub mod cleaner;
pub mod dedup;
pub mod dom;
pub mod error;
pub mod options;
pub mod text;
pub mod tree;

// Feature-gated modules
#[cfg(feature = "presets")]
pub mod presets;

// Links module is always available - it provides basic URL utilities without dependencies.
// When the `url` feature is enabled, it uses the `url` crate for more robust parsing.
// When disabled, it uses simple string-based fallbacks.
pub mod links;


// Re-export core types
pub use cleaner::HtmlCleaner;
pub use error::{Error, Result};
pub use options::{CleaningOptions, CleaningOptionsBuilder};

// Re-export dom_query types for convenience
pub use dom_query::{Document, Selection};

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_basic_cleaning() {
        let doc = Document::from("<div><script>bad</script><p>Hello</p></div>");
        let cleaner = HtmlCleaner::new();
        cleaner.remove_tags(&doc, &["script"]);

        assert!(doc.select("script").is_empty());
        assert!(doc.select("p").exists());
    }

    #[test]
    fn test_with_options() {
        let options = CleaningOptions::builder()
            .remove_tags(&["script", "style"])
            .prune_empty(true)
            .build();

        let cleaner = HtmlCleaner::with_options(options);
        assert!(cleaner.options().prune_empty);
    }

    #[cfg(feature = "presets")]
    #[test]
    fn test_presets() {
        let cleaner = HtmlCleaner::with_options(presets::standard());
        assert!(!cleaner.options().tags_to_remove.is_empty());
    }
}

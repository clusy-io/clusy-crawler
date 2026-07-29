//! Prebuilt cleaning configurations.
//!
//! Ready-to-use presets for common cleaning scenarios.

use crate::options::CleaningOptions;

/// Minimal cleaning - just scripts and styles.
///
/// Removes:
/// - `script`, `style`, `noscript`
///
/// Best for: Quick sanitization, preserving most structure.
///
/// # Example
///
/// ```
/// use html_cleaning::{HtmlCleaner, presets};
///
/// let cleaner = HtmlCleaner::with_options(presets::minimal());
/// ```
#[must_use]
pub fn minimal() -> CleaningOptions {
    CleaningOptions {
        tags_to_remove: vec![
            "script".to_string(),
            "style".to_string(),
            "noscript".to_string(),
        ],
        prune_empty: false,
        normalize_whitespace: false,
        ..Default::default()
    }
}

/// Standard cleaning for web scraping.
///
/// Removes:
/// - `script`, `style`, `noscript`
/// - `form`, `iframe`, `object`, `embed`
/// - `svg`, `canvas`, `video`, `audio`
///
/// Enables:
/// - `prune_empty`
/// - `normalize_whitespace`
///
/// Best for: General web scraping, content display.
///
/// # Example
///
/// ```
/// use html_cleaning::{HtmlCleaner, presets};
///
/// let cleaner = HtmlCleaner::with_options(presets::standard());
/// ```
#[must_use]
pub fn standard() -> CleaningOptions {
    CleaningOptions {
        tags_to_remove: vec![
            "script".to_string(),
            "style".to_string(),
            "noscript".to_string(),
            "form".to_string(),
            "iframe".to_string(),
            "object".to_string(),
            "embed".to_string(),
            "svg".to_string(),
            "canvas".to_string(),
            "video".to_string(),
            "audio".to_string(),
        ],
        prune_empty: true,
        normalize_whitespace: true,
        ..Default::default()
    }
}

/// Aggressive cleaning for maximum content extraction.
///
/// Includes everything in `standard()` plus:
/// - Removes: `nav`, `header`, `footer`, `aside`, `figure`, `figcaption`
/// - Enables: `strip_attributes` (preserves `href`, `src`, `alt`)
///
/// Best for: Text extraction, removing all non-content elements.
///
/// # Example
///
/// ```
/// use html_cleaning::{HtmlCleaner, presets};
///
/// let cleaner = HtmlCleaner::with_options(presets::aggressive());
/// ```
#[must_use]
pub fn aggressive() -> CleaningOptions {
    CleaningOptions {
        tags_to_remove: vec![
            // Standard tags
            "script".to_string(),
            "style".to_string(),
            "noscript".to_string(),
            "form".to_string(),
            "iframe".to_string(),
            "object".to_string(),
            "embed".to_string(),
            "svg".to_string(),
            "canvas".to_string(),
            "video".to_string(),
            "audio".to_string(),
            // Layout tags
            "nav".to_string(),
            "header".to_string(),
            "footer".to_string(),
            "aside".to_string(),
            "figure".to_string(),
            "figcaption".to_string(),
        ],
        prune_empty: true,
        normalize_whitespace: true,
        strip_attributes: true,
        preserved_attributes: vec![
            "href".to_string(),
            "src".to_string(),
            "alt".to_string(),
        ],
        ..Default::default()
    }
}

/// Article extraction preset.
///
/// Optimized for extracting article content:
/// - Removes navigation and layout elements
/// - Strips wrapper tags (`div`, `span`) while preserving content
/// - Removes common advertisement selectors
///
/// Best for: News articles, blog posts, content pages.
///
/// # Example
///
/// ```
/// use html_cleaning::{HtmlCleaner, presets};
///
/// let cleaner = HtmlCleaner::with_options(presets::article_extraction());
/// ```
#[must_use]
pub fn article_extraction() -> CleaningOptions {
    CleaningOptions {
        tags_to_remove: vec![
            "script".to_string(),
            "style".to_string(),
            "noscript".to_string(),
            "form".to_string(),
            "iframe".to_string(),
            "object".to_string(),
            "embed".to_string(),
            "svg".to_string(),
            "canvas".to_string(),
            "video".to_string(),
            "audio".to_string(),
            "nav".to_string(),
            "footer".to_string(),
        ],
        tags_to_strip: vec!["span".to_string(), "div".to_string()],
        selectors_to_remove: vec![
            "[role='navigation']".to_string(),
            "[role='banner']".to_string(),
            "[role='complementary']".to_string(),
            ".advertisement".to_string(),
            ".ads".to_string(),
            "#comments".to_string(),
            ".comments".to_string(),
        ],
        prune_empty: true,
        normalize_whitespace: true,
        empty_tags: vec![
            "div".to_string(),
            "span".to_string(),
            "p".to_string(),
            "section".to_string(),
        ],
        ..Default::default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_minimal_preset() {
        let opts = minimal();
        assert_eq!(opts.tags_to_remove.len(), 3);
        assert!(!opts.prune_empty);
    }

    #[test]
    fn test_standard_preset() {
        let opts = standard();
        assert!(opts.tags_to_remove.len() > 5);
        assert!(opts.prune_empty);
        assert!(opts.normalize_whitespace);
    }

    #[test]
    fn test_aggressive_preset() {
        let opts = aggressive();
        assert!(opts.tags_to_remove.contains(&"nav".to_string()));
        assert!(opts.strip_attributes);
        assert!(opts.preserved_attributes.contains(&"href".to_string()));
    }

    #[test]
    fn test_article_extraction_preset() {
        let opts = article_extraction();
        assert!(!opts.selectors_to_remove.is_empty());
        assert!(opts.tags_to_strip.contains(&"span".to_string()));
    }
}

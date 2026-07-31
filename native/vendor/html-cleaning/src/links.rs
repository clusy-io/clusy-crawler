//! URL and link processing utilities.
//!
//! Available with the `url` feature flag.

#[cfg(feature = "url")]
use url::Url;

use dom_query::Document;

/// Check if URL is valid (has scheme and host).
///
/// # Example
///
/// ```
/// use html_cleaning::links;
///
/// assert!(links::is_valid_url("https://example.com"));
/// assert!(!links::is_valid_url("/relative/path"));
/// ```
#[must_use]
pub fn is_valid_url(url_str: &str) -> bool {
    let url_str = url_str.trim();
    if url_str.is_empty() {
        return false;
    }

    #[cfg(feature = "url")]
    {
        Url::parse(url_str).is_ok()
    }

    #[cfg(not(feature = "url"))]
    {
        url_str.starts_with("http://") || url_str.starts_with("https://")
    }
}

/// Check if URL is absolute (has scheme).
///
/// # Example
///
/// ```
/// use html_cleaning::links;
///
/// assert!(links::is_absolute("https://example.com/page"));
/// assert!(!links::is_absolute("/relative/path"));
/// ```
#[must_use]
pub fn is_absolute(url_str: &str) -> bool {
    let url_str = url_str.trim();
    url_str.starts_with("http://")
        || url_str.starts_with("https://")
        || url_str.starts_with("//")
}

/// Resolve relative URL against base.
///
/// # Example
///
/// ```
/// use html_cleaning::links;
///
/// let abs = links::resolve("/page", "https://example.com/articles/");
/// assert_eq!(abs, Some("https://example.com/page".to_string()));
/// ```
#[must_use]
pub fn resolve(relative: &str, base: &str) -> Option<String> {
    let relative = relative.trim();
    let base = base.trim();

    if relative.is_empty() {
        return None;
    }

    // Already absolute
    if is_absolute(relative) {
        if relative.starts_with("//") {
            return Some(format!("https:{relative}"));
        }
        return Some(relative.to_string());
    }

    // Special URLs
    if relative.starts_with("data:")
        || relative.starts_with("javascript:")
        || relative.starts_with("mailto:")
        || relative.starts_with("tel:")
        || relative.starts_with('#')
    {
        return Some(relative.to_string());
    }

    #[cfg(feature = "url")]
    {
        let base_url = Url::parse(base).ok()?;
        let resolved = base_url.join(relative).ok()?;
        Some(resolved.to_string())
    }

    #[cfg(not(feature = "url"))]
    {
        // Simple fallback without url crate
        if relative.starts_with('/') {
            // Absolute path - extract base domain
            let base_parts: Vec<&str> = base.splitn(4, '/').collect();
            if base_parts.len() >= 3 {
                return Some(format!("{}//{}{relative}", base_parts[0], base_parts[2]));
            }
        }
        // Can't resolve without url crate
        None
    }
}

/// Normalize URL (remove fragments, trailing slashes).
///
/// # Example
///
/// ```
/// use html_cleaning::links;
///
/// assert_eq!(
///     links::normalize_url("https://example.com/page#section"),
///     Some("https://example.com/page".to_string())
/// );
/// ```
#[must_use]
pub fn normalize_url(url_str: &str) -> Option<String> {
    #[cfg(feature = "url")]
    {
        let mut url = Url::parse(url_str).ok()?;
        url.set_fragment(None);

        let path = url.path().to_string();
        if path.len() > 1 && path.ends_with('/') {
            url.set_path(&path[..path.len() - 1]);
        }

        Some(url.to_string())
    }

    #[cfg(not(feature = "url"))]
    {
        let url_str = url_str.trim();
        if url_str.is_empty() {
            return None;
        }

        // Remove fragment
        let without_fragment = url_str.split('#').next()?;

        // Remove trailing slash (unless it's the root)
        let normalized = if without_fragment.ends_with('/')
            && !without_fragment.ends_with("://")
            && without_fragment.matches('/').count() > 3
        {
            &without_fragment[..without_fragment.len() - 1]
        } else {
            without_fragment
        };

        Some(normalized.to_string())
    }
}

/// Extract domain from URL.
///
/// # Example
///
/// ```
/// use html_cleaning::links;
///
/// assert_eq!(
///     links::get_domain("https://www.example.com/page"),
///     Some("www.example.com".to_string())
/// );
/// ```
#[must_use]
pub fn get_domain(url_str: &str) -> Option<String> {
    #[cfg(feature = "url")]
    {
        let url = Url::parse(url_str).ok()?;
        url.host_str().map(std::string::ToString::to_string)
    }

    #[cfg(not(feature = "url"))]
    {
        let url_str = url_str.trim();
        let without_scheme = url_str
            .strip_prefix("https://")
            .or_else(|| url_str.strip_prefix("http://"))?;

        let domain = without_scheme.split('/').next()?;
        let domain = domain.split(':').next()?; // Remove port

        if domain.is_empty() {
            None
        } else {
            Some(domain.to_string())
        }
    }
}

/// Check if two URLs point to same resource.
///
/// Compares normalized URLs (without fragments).
#[must_use]
pub fn urls_match(url1: &str, url2: &str) -> bool {
    match (normalize_url(url1), normalize_url(url2)) {
        (Some(n1), Some(n2)) => n1 == n2,
        _ => false,
    }
}

/// Make all relative URLs in document absolute.
///
/// Converts relative `href` attributes on `<a>` tags and `src` attributes
/// on `<img>` tags to absolute URLs using the provided base URL.
///
/// # Example
///
/// ```
/// use html_cleaning::links;
/// use dom_query::Document;
///
/// let doc = Document::from(r#"<a href="/page">Link</a><img src="img.jpg">"#);
/// links::make_absolute(&doc, "https://example.com/articles/");
///
/// assert!(doc.select("a").attr("href").unwrap().starts_with("https://example.com"));
/// ```
pub fn make_absolute(doc: &Document, base_url: &str) {
    // Process links
    for node in doc.select("a[href]").nodes() {
        let sel = dom_query::Selection::from(*node);
        if let Some(href) = sel.attr("href") {
            if !is_absolute(&href) {
                if let Some(absolute) = resolve(&href, base_url) {
                    sel.set_attr("href", &absolute);
                }
            }
        }
    }

    // Process images
    for node in doc.select("img[src]").nodes() {
        let sel = dom_query::Selection::from(*node);
        if let Some(src) = sel.attr("src") {
            if !is_absolute(&src) {
                if let Some(absolute) = resolve(&src, base_url) {
                    sel.set_attr("src", &absolute);
                }
            }
        }
    }
}

/// Remove all links (keep text content).
///
/// Removes all `<a>` tags from the document while preserving their text content.
///
/// # Example
///
/// ```
/// use html_cleaning::links;
/// use dom_query::Document;
///
/// let doc = Document::from("<p>Click <a href='#'>here</a> for more.</p>");
/// links::strip_all(&doc);
///
/// assert_eq!(doc.select("a").length(), 0);
/// assert!(doc.select("p").text().contains("here"));
/// ```
pub fn strip_all(doc: &Document) {
    // Get root element and strip all anchor tags
    let root = doc.select("*").first();
    if root.exists() {
        crate::tree::strip_tags(&root, &["a"]);
    }
}

/// Filter links based on predicate.
///
/// Removes all `<a>` tags that don't match the predicate.
///
/// # Example
///
/// ```
/// use html_cleaning::links;
/// use dom_query::Document;
///
/// let doc = Document::from(r#"<a href="https://good.com">Keep</a><a href="https://bad.com">Remove</a>"#);
/// links::filter(&doc, |sel| {
///     sel.attr("href").map(|h| h.contains("good")).unwrap_or(false)
/// });
///
/// assert_eq!(doc.select("a").length(), 1);
/// ```
pub fn filter<F>(doc: &Document, keep: F)
where
    F: Fn(&dom_query::Selection) -> bool,
{
    let links: Vec<_> = doc.select("a").nodes().to_vec();
    for node in links {
        let sel = dom_query::Selection::from(node);
        if !keep(&sel) {
            sel.remove();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_is_valid_url() {
        assert!(is_valid_url("https://example.com"));
        assert!(is_valid_url("http://example.com/path"));
        assert!(!is_valid_url("/relative"));
        assert!(!is_valid_url(""));
    }

    #[test]
    fn test_is_absolute() {
        assert!(is_absolute("https://example.com"));
        assert!(is_absolute("http://example.com"));
        assert!(is_absolute("//cdn.example.com"));
        assert!(!is_absolute("/path"));
        assert!(!is_absolute("relative"));
    }

    #[test]
    fn test_get_domain() {
        assert_eq!(
            get_domain("https://example.com/path"),
            Some("example.com".to_string())
        );
        assert_eq!(
            get_domain("https://sub.example.com/"),
            Some("sub.example.com".to_string())
        );
    }

    #[test]
    fn test_urls_match() {
        assert!(urls_match(
            "https://example.com/page#section1",
            "https://example.com/page#section2"
        ));
        assert!(!urls_match(
            "https://example.com/page1",
            "https://example.com/page2"
        ));
    }

    #[test]
    fn test_make_absolute() {
        let doc = Document::from(r#"<a href="/page">Link</a><img src="image.jpg">"#);
        make_absolute(&doc, "https://example.com/articles/");

        let href = doc.select("a").attr("href");
        assert!(href.is_some());
        assert!(href.unwrap().starts_with("https://"));
    }

    #[test]
    fn test_resolve_absolute_passthrough() {
        // Already absolute URLs should pass through unchanged
        assert_eq!(
            resolve("https://other.com/page", "https://example.com"),
            Some("https://other.com/page".to_string())
        );
    }

    #[test]
    fn test_resolve_protocol_relative() {
        // Protocol-relative URLs should get https:
        assert_eq!(
            resolve("//cdn.example.com/script.js", "https://example.com"),
            Some("https://cdn.example.com/script.js".to_string())
        );
    }

    #[test]
    fn test_resolve_special_urls() {
        // Special URLs should pass through unchanged
        assert_eq!(
            resolve("data:image/png;base64,abc", "https://example.com"),
            Some("data:image/png;base64,abc".to_string())
        );
        assert_eq!(
            resolve("javascript:void(0)", "https://example.com"),
            Some("javascript:void(0)".to_string())
        );
        assert_eq!(
            resolve("mailto:test@example.com", "https://example.com"),
            Some("mailto:test@example.com".to_string())
        );
        assert_eq!(
            resolve("#section", "https://example.com"),
            Some("#section".to_string())
        );
    }

    #[test]
    fn test_normalize_url_removes_fragment() {
        assert_eq!(
            normalize_url("https://example.com/page#section"),
            Some("https://example.com/page".to_string())
        );
    }

    #[test]
    fn test_normalize_url_removes_trailing_slash() {
        assert_eq!(
            normalize_url("https://example.com/page/"),
            Some("https://example.com/page".to_string())
        );
    }

    #[test]
    fn test_strip_all_links() {
        let doc = Document::from("<div><a href='#'>Link 1</a> text <a href='#'>Link 2</a></div>");
        strip_all(&doc);
        // Links should be removed but text preserved
        assert_eq!(doc.select("a").length(), 0);
        let text = doc.select("div").text();
        assert!(text.contains("Link 1"), "Text 'Link 1' should be preserved");
        assert!(text.contains("Link 2"), "Text 'Link 2' should be preserved");
        assert!(text.contains("text"), "Text 'text' should be preserved");
    }

    #[test]
    fn test_filter_links() {
        let doc = Document::from(r#"<div><a href="http://good.com">Good</a><a href="http://bad.com">Bad</a></div>"#);
        filter(&doc, |sel| {
            sel.attr("href")
                .map(|h| h.contains("good"))
                .unwrap_or(false)
        });
        assert_eq!(doc.select("a").length(), 1);
        assert!(doc.select("a").text().contains("Good"));
    }
}

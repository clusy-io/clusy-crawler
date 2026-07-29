//! Text processing utilities.
//!
//! Generic functions for analyzing and normalizing text content.

/// Check if text contains meaningful alphanumeric content.
///
/// Returns `true` if text contains at least one alphanumeric character.
///
/// # Example
///
/// ```
/// use html_cleaning::text;
///
/// assert!(text::has_content("Hello"));
/// assert!(text::has_content("  123  "));
/// assert!(!text::has_content("  ...  "));
/// assert!(!text::has_content(""));
/// ```
#[must_use]
pub fn has_content(text: &str) -> bool {
    text.chars().any(char::is_alphanumeric)
}

/// Check if text contains only whitespace.
///
/// # Example
///
/// ```
/// use html_cleaning::text;
///
/// assert!(text::is_whitespace_only("   "));
/// assert!(text::is_whitespace_only("\t\n"));
/// assert!(!text::is_whitespace_only(" a "));
/// ```
#[must_use]
pub fn is_whitespace_only(text: &str) -> bool {
    text.chars().all(char::is_whitespace)
}

/// Normalize whitespace in text.
///
/// - Trims leading/trailing whitespace
/// - Collapses multiple whitespace characters to single space
///
/// # Example
///
/// ```
/// use html_cleaning::text;
///
/// assert_eq!(text::normalize("  hello   world  "), "hello world");
/// ```
#[must_use]
pub fn normalize(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Count words in text.
///
/// Words are defined as whitespace-separated sequences.
///
/// # Example
///
/// ```
/// use html_cleaning::text;
///
/// assert_eq!(text::word_count("hello world"), 2);
/// assert_eq!(text::word_count(""), 0);
/// ```
#[must_use]
pub fn word_count(text: &str) -> usize {
    text.split_whitespace().count()
}

/// Count sentences in text (approximate).
///
/// Counts sentence-ending punctuation (. ! ?).
///
/// # Example
///
/// ```
/// use html_cleaning::text;
///
/// assert_eq!(text::sentence_count("Hello. World!"), 2);
/// ```
#[must_use]
pub fn sentence_count(text: &str) -> usize {
    text.chars()
        .filter(|c| matches!(c, '.' | '!' | '?'))
        .count()
}

/// Clean text for fuzzy comparison.
///
/// - Converts to lowercase
/// - Removes punctuation
/// - Normalizes whitespace
///
/// # Example
///
/// ```
/// use html_cleaning::text;
///
/// assert_eq!(text::clean_for_comparison("Hello, World!"), "hello world");
/// ```
#[must_use]
pub fn clean_for_comparison(text: &str) -> String {
    text.chars()
        .filter(|c| c.is_alphanumeric() || c.is_whitespace())
        .collect::<String>()
        .to_lowercase()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_has_content() {
        assert!(has_content("Hello"));
        assert!(has_content("123"));
        assert!(has_content("  a  "));
        assert!(!has_content(""));
        assert!(!has_content("   "));
        assert!(!has_content("..."));
        assert!(!has_content("!@#$%"));
    }

    #[test]
    fn test_is_whitespace_only() {
        assert!(is_whitespace_only(""));
        assert!(is_whitespace_only("   "));
        assert!(is_whitespace_only("\t\n\r"));
        assert!(!is_whitespace_only("a"));
        assert!(!is_whitespace_only(" a "));
    }

    #[test]
    fn test_normalize() {
        assert_eq!(normalize("hello"), "hello");
        assert_eq!(normalize("  hello  "), "hello");
        assert_eq!(normalize("hello   world"), "hello world");
        assert_eq!(normalize("  a  b  c  "), "a b c");
        assert_eq!(normalize(""), "");
        // Newlines and tabs should collapse to single space
        assert_eq!(normalize("a\n\nb"), "a b");
        assert_eq!(normalize("a\t\tb"), "a b");
        assert_eq!(normalize("hello\n\n\nworld"), "hello world");
    }

    #[test]
    fn test_word_count() {
        assert_eq!(word_count("hello world"), 2);
        assert_eq!(word_count("one"), 1);
        assert_eq!(word_count(""), 0);
        assert_eq!(word_count("   "), 0);
        assert_eq!(word_count("a b c d e"), 5);
    }

    #[test]
    fn test_sentence_count() {
        assert_eq!(sentence_count("Hello."), 1);
        assert_eq!(sentence_count("Hello. World!"), 2);
        assert_eq!(sentence_count("What? Really! Yes."), 3);
        assert_eq!(sentence_count("No punctuation"), 0);
    }

    #[test]
    fn test_clean_for_comparison() {
        assert_eq!(clean_for_comparison("Hello, World!"), "hello world");
        assert_eq!(clean_for_comparison("Test123"), "test123");
        assert_eq!(clean_for_comparison("  UPPER  lower  "), "upper lower");
    }
}

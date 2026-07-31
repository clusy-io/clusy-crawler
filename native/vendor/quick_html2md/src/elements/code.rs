//! Code element conversion with language detection.

use dom_query::Selection;

/// Known programming language names for detection.
const KNOWN_LANGS: &[&str] = &[
    "rust", "python", "javascript", "typescript", "go", "java", "c", "cpp", "csharp", "ruby",
    "php", "swift", "kotlin", "sql", "bash", "shell", "json", "yaml", "xml", "html", "css",
];

/// Extract programming language from code element classes.
/// Uses a single pass through the class list for efficiency.
pub(crate) fn extract_language(code: &Selection) -> String {
    let class = code.attr("class").unwrap_or_default();

    let mut source_code_lang = None;
    let mut direct_lang = None;

    // Single pass through all classes
    for cls in class.split_whitespace() {
        // Pattern: language-{lang} (Prism.js, Highlight.js) - highest priority
        if let Some(lang) = cls.strip_prefix("language-") {
            return lang.to_string();
        }
        // Pattern: lang-{lang}
        if let Some(lang) = cls.strip_prefix("lang-") {
            return lang.to_string();
        }
        // Pattern: highlight-{lang}
        if let Some(lang) = cls.strip_prefix("highlight-") {
            return lang.to_string();
        }

        // Track sourceCode companion class (Pandoc pattern)
        if cls != "sourceCode"
            && !cls.contains('-')
            && source_code_lang.is_none()
            && class.contains("sourceCode")
        {
            source_code_lang = Some(cls.to_string());
        }

        // Track direct language class names
        if direct_lang.is_none() && KNOWN_LANGS.contains(&cls.to_lowercase().as_str()) {
            direct_lang = Some(cls.to_lowercase());
        }
    }

    // Return in priority order
    source_code_lang
        .or(direct_lang)
        .unwrap_or_default()
}

/// Convert code block element (pre/code).
pub(crate) fn convert_code_block(sel: &Selection, output: &mut String) {
    let code = sel.select("code");
    let lang = if code.exists() {
        extract_language(&code)
    } else {
        extract_language(sel)
    };

    output.push_str("```");
    output.push_str(&lang);
    output.push('\n');
    output.push_str(sel.text().as_ref());
    if !output.ends_with('\n') {
        output.push('\n');
    }
    output.push_str("```\n\n");
}

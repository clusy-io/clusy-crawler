//! Configuration for Markdown output.

/// Configuration for Markdown output.
#[derive(Debug, Clone, Default)]
#[allow(clippy::struct_excessive_bools)]
pub struct MarkdownOptions {
    /// Preserve heading hierarchy (h1-h6 -> #-######)
    pub preserve_headings: bool,
    /// Include links as `[text](url)`
    pub include_links: bool,
    /// Include images as `![alt](src)`
    pub include_images: bool,
    /// Preserve emphasis (bold, italic)
    pub preserve_emphasis: bool,
    /// Preserve strikethrough (<del>, <s>, <strike> -> ~~text~~)
    /// Note: Strikethrough is a GFM extension, disabled in CommonMark mode.
    pub preserve_strikethrough: bool,
    /// Preserve lists (ul/ol -> -/1.)
    pub preserve_lists: bool,
    /// Preserve code blocks and inline code
    pub preserve_code: bool,
    /// Preserve blockquotes
    pub preserve_blockquotes: bool,
    /// Preserve tables (GFM format)
    /// Note: Tables are a GFM extension, disabled in CommonMark mode.
    pub preserve_tables: bool,
    /// Maximum heading level to preserve (1-6).
    /// Headings beyond this level are converted to plain paragraphs.
    pub max_heading_level: u8,

    /// Enable strict CommonMark mode.
    /// When enabled:
    /// - Uses 4-space indentation for nested lists (instead of 2)
    /// - Disables GFM extensions (strikethrough, tables)
    /// - Uses HTML fallback for images with dimensions
    pub commonmark: bool,

    /// Escape special markdown characters in text content.
    /// Characters escaped: * _ ~ [ ] ( ) < > \ `
    pub escape_special_chars: bool,

    /// Base URL for resolving relative URLs in links and images.
    /// When set, relative URLs (starting with `/` or not starting with a scheme)
    /// will be resolved against this base URL.
    pub base_url: Option<String>,
}

impl MarkdownOptions {
    /// Create a new `MarkdownOptions` with default values (GFM mode).
    pub fn new() -> Self {
        Self {
            preserve_headings: true,
            include_links: true,
            include_images: true,
            preserve_emphasis: true,
            preserve_strikethrough: true,
            preserve_lists: true,
            preserve_code: true,
            preserve_blockquotes: true,
            preserve_tables: true,
            max_heading_level: 6,
            commonmark: false,
            escape_special_chars: false,
            base_url: None,
        }
    }

    /// Create options for strict CommonMark compliance.
    /// Disables GFM extensions like strikethrough and tables.
    pub fn commonmark() -> Self {
        Self {
            preserve_headings: true,
            include_links: true,
            include_images: true,
            preserve_emphasis: true,
            preserve_strikethrough: false, // GFM extension
            preserve_lists: true,
            preserve_code: true,
            preserve_blockquotes: true,
            preserve_tables: false, // GFM extension
            max_heading_level: 6,
            commonmark: true,
            escape_special_chars: true,
            base_url: None,
        }
    }

    /// Set whether to preserve headings.
    pub fn preserve_headings(mut self, value: bool) -> Self {
        self.preserve_headings = value;
        self
    }

    /// Set whether to include links.
    pub fn include_links(mut self, value: bool) -> Self {
        self.include_links = value;
        self
    }

    /// Set whether to include images.
    pub fn include_images(mut self, value: bool) -> Self {
        self.include_images = value;
        self
    }

    /// Set whether to preserve emphasis (bold, italic).
    pub fn preserve_emphasis(mut self, value: bool) -> Self {
        self.preserve_emphasis = value;
        self
    }

    /// Set whether to preserve strikethrough.
    pub fn preserve_strikethrough(mut self, value: bool) -> Self {
        self.preserve_strikethrough = value;
        self
    }

    /// Set whether to preserve lists.
    pub fn preserve_lists(mut self, value: bool) -> Self {
        self.preserve_lists = value;
        self
    }

    /// Set whether to preserve code blocks and inline code.
    pub fn preserve_code(mut self, value: bool) -> Self {
        self.preserve_code = value;
        self
    }

    /// Set whether to preserve blockquotes.
    pub fn preserve_blockquotes(mut self, value: bool) -> Self {
        self.preserve_blockquotes = value;
        self
    }

    /// Set whether to preserve tables.
    pub fn preserve_tables(mut self, value: bool) -> Self {
        self.preserve_tables = value;
        self
    }

    /// Set the maximum heading level to preserve.
    /// Headings beyond this level are converted to plain paragraphs.
    pub fn max_heading_level(mut self, level: u8) -> Self {
        self.max_heading_level = level.clamp(1, 6);
        self
    }

    /// Enable or disable CommonMark mode.
    /// When enabled, uses stricter CommonMark formatting and disables GFM extensions.
    pub fn commonmark_mode(mut self, value: bool) -> Self {
        self.commonmark = value;
        if value {
            // Disable GFM extensions in CommonMark mode
            self.preserve_strikethrough = false;
            self.preserve_tables = false;
        }
        self
    }

    /// Enable or disable escaping of special markdown characters in text.
    pub fn escape_special_chars(mut self, value: bool) -> Self {
        self.escape_special_chars = value;
        self
    }

    /// Set the base URL for resolving relative URLs.
    pub fn base_url(mut self, url: impl Into<String>) -> Self {
        self.base_url = Some(url.into());
        self
    }

    /// Get the list indentation string based on mode.
    /// CommonMark uses 4 spaces, GFM uses 2 spaces.
    pub(crate) fn list_indent(&self) -> &'static str {
        if self.commonmark {
            "    "
        } else {
            "  "
        }
    }
}

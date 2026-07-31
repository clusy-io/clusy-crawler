//! Error types for html-cleaning.

use thiserror::Error;

/// Error type for html-cleaning operations.
#[derive(Debug, Error)]
pub enum Error {
    /// Invalid CSS selector syntax.
    #[error("invalid selector: {0}")]
    InvalidSelector(String),

    /// HTML parsing error.
    #[error("parse error: {0}")]
    ParseError(String),

    /// URL parsing error (with url feature).
    #[cfg(feature = "url")]
    #[error("url error: {0}")]
    UrlError(#[from] url::ParseError),
}

/// Result type alias for html-cleaning.
pub type Result<T> = std::result::Result<T, Error>;

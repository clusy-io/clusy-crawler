# Changelog

## [0.2.0] - 2026-01-03

### Breaking Changes

- **Removed `markdown` module and feature flag**

  The markdown conversion functionality has been extracted to a dedicated crate
  `quick_html2md` to maintain separation of concerns.

  **Migration:**

  Before:
  ```rust
  use html_cleaning::markdown::html_to_markdown;
  let md = html_to_markdown(html);
  ```

  After:
  ```rust
  use quick_html2md::html_to_markdown;
  let md = html_to_markdown(html);
  ```

  The new crate also includes:
  - Fixed nested list handling
  - GFM table conversion
  - Improved code block language detection
  - Strikethrough support

## [0.1.0] - Initial Release

- HTML cleaning and sanitization
- Tag stripping and text normalization
- Link processing
- Content deduplication
- Presets for common cleaning scenarios

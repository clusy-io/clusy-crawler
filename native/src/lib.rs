mod document_ir;
mod document_ir_v2;

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use rs_trafilatura_article::extract as extract_article;
use rs_trafilatura_broad::{extract_with_options, Options};

/// Result returned to the Python cascade.
///
/// Keeping this as a compact immutable object avoids serializing large
/// Markdown strings through JSON or a subprocess boundary.
#[pyclass(frozen, get_all)]
struct NativeExtraction {
    text: String,
    plain_text: String,
    article_text: String,
    title: String,
    description: String,
    language: String,
    page_type: String,
    word_count: usize,
    confidence: f64,
    strategy: String,
}

/// Extract main content with the pinned rs-trafilatura backend.
///
/// The HTML and URL are owned strings before `detach`, so the CPU-heavy Rust
/// parse can run without holding CPython's interpreter lock. That lets the
/// bounded Python thread pool process separate pages on separate cores.
#[pyfunction]
#[pyo3(signature = (html, url="", article_body=false))]
fn extract_html(
    py: Python<'_>,
    html: String,
    url: &str,
    article_body: bool,
) -> PyResult<NativeExtraction> {
    let source_url = url.to_owned();
    let result = py.detach(move || {
        // The current release supplies multi-type classification and calibrated
        // confidence. Its article-body behavior changed after the independently
        // benchmarked revision, so the explicit article_body profile uses the
        // exact pinned backend recorded by AEB.
        let clean_options = Options {
            url: (!source_url.is_empty()).then_some(source_url.clone()),
            ..Options::default()
        };
        let clean = extract_with_options(&html, &clean_options)?;
        let article_text = if article_body {
            extract_article(&html)
                .ok()
                .map(|value| value.content_text.replace('\u{00ad}', ""))
                .filter(|value| !value.trim().is_empty())
        } else {
            None
        };

        Ok::<_, rs_trafilatura_broad::Error>((clean, article_text))
    });
    let (result, article_text) =
        result.map_err(|error| PyRuntimeError::new_err(error.to_string()))?;

    let plain_text = result.content_text;
    let article_text = article_text.unwrap_or_default();
    // Clean body text is valid Markdown. A second link-rich pass changed the
    // selected content as well as its formatting and measurably reintroduced
    // boilerplate on the broad WCXB corpus.
    let text = if article_body && !article_text.is_empty() {
        article_text.clone()
    } else {
        plain_text.clone()
    };
    let word_count = text.split_whitespace().count();
    let metadata = result.metadata;

    Ok(NativeExtraction {
        text,
        plain_text,
        article_text,
        title: metadata.title.unwrap_or_default(),
        description: metadata.description.unwrap_or_default(),
        language: metadata.language.unwrap_or_default(),
        page_type: metadata.page_type.unwrap_or_default(),
        word_count,
        confidence: result.extraction_quality,
        strategy: "rs-trafilatura-0.2.2".to_owned(),
    })
}

#[pyfunction]
fn backend_version() -> &'static str {
    "rs-trafilatura article@9261e08 + broad@0.2.2"
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<NativeExtraction>()?;
    module.add_function(wrap_pyfunction!(extract_html, module)?)?;
    module.add_function(wrap_pyfunction!(backend_version, module)?)?;
    document_ir::register(module)?;
    document_ir_v2::register(module)?;
    Ok(())
}

use std::collections::HashMap;

use dom_query::{Document, NodeId, NodeRef};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

const DEFAULT_MAX_INPUT_BYTES: usize = 4 * 1024 * 1024;
const DEFAULT_MAX_NODES: usize = 100_000;
const DEFAULT_MAX_BLOCKS: usize = 4_096;
const DEFAULT_MAX_DEPTH: usize = 128;
const DEFAULT_MAX_BLOCK_TEXT_BYTES: usize = 32 * 1024;
const DEFAULT_MAX_BLOCK_HTML_BYTES: usize = 64 * 1024;
const DEFAULT_MAX_TOTAL_TEXT_BYTES: usize = 4 * 1024 * 1024;
const DEFAULT_MAX_TOTAL_HTML_BYTES: usize = 8 * 1024 * 1024;

const HARD_MAX_INPUT_BYTES: usize = 16 * 1024 * 1024;
const HARD_MAX_NODES: usize = 500_000;
const HARD_MAX_BLOCKS: usize = 16_384;
const HARD_MAX_DEPTH: usize = 256;
const HARD_MAX_BLOCK_TEXT_BYTES: usize = 256 * 1024;
const HARD_MAX_BLOCK_HTML_BYTES: usize = 512 * 1024;
const HARD_MAX_TOTAL_TEXT_BYTES: usize = 16 * 1024 * 1024;
const HARD_MAX_TOTAL_HTML_BYTES: usize = 32 * 1024 * 1024;

const SCHEMA_VERSION: &str = "ordered-dom-ir.v1";

#[derive(Clone, Copy, Debug)]
struct Limits {
    max_input_bytes: usize,
    max_nodes: usize,
    max_blocks: usize,
    max_depth: usize,
    max_block_text_bytes: usize,
    max_block_html_bytes: usize,
    max_total_text_bytes: usize,
    max_total_html_bytes: usize,
}

impl Limits {
    #[allow(clippy::too_many_arguments)]
    fn validated(
        max_input_bytes: usize,
        max_nodes: usize,
        max_blocks: usize,
        max_depth: usize,
        max_block_text_bytes: usize,
        max_block_html_bytes: usize,
        max_total_text_bytes: usize,
        max_total_html_bytes: usize,
    ) -> PyResult<Self> {
        validate_limit("max_input_bytes", max_input_bytes, HARD_MAX_INPUT_BYTES)?;
        validate_limit("max_nodes", max_nodes, HARD_MAX_NODES)?;
        validate_limit("max_blocks", max_blocks, HARD_MAX_BLOCKS)?;
        validate_limit("max_depth", max_depth, HARD_MAX_DEPTH)?;
        validate_limit(
            "max_block_text_bytes",
            max_block_text_bytes,
            HARD_MAX_BLOCK_TEXT_BYTES,
        )?;
        validate_limit(
            "max_block_html_bytes",
            max_block_html_bytes,
            HARD_MAX_BLOCK_HTML_BYTES,
        )?;
        validate_limit(
            "max_total_text_bytes",
            max_total_text_bytes,
            HARD_MAX_TOTAL_TEXT_BYTES,
        )?;
        validate_limit(
            "max_total_html_bytes",
            max_total_html_bytes,
            HARD_MAX_TOTAL_HTML_BYTES,
        )?;

        Ok(Self {
            max_input_bytes,
            max_nodes,
            max_blocks,
            max_depth,
            max_block_text_bytes,
            max_block_html_bytes,
            max_total_text_bytes,
            max_total_html_bytes,
        })
    }
}

fn validate_limit(name: &str, value: usize, hard_max: usize) -> PyResult<()> {
    if value == 0 || value > hard_max {
        return Err(PyValueError::new_err(format!(
            "{name} must be between 1 and {hard_max}"
        )));
    }
    Ok(())
}

/// A document-order semantic block from the retained DOM.
///
/// `atomic` blocks never contain another emitted atomic block. `selectable`
/// separates classifier units from serialization-only ancestor containers.
/// Container blocks can contain atomic children and are the only values named
/// by `parent_id`.
#[pyclass(frozen, get_all)]
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct NativeSemanticBlock {
    id: String,
    order: usize,
    parent_id: Option<String>,
    tag: String,
    role: String,
    atomic: bool,
    selectable: bool,
    preserve_whitespace: bool,
    text: String,
    outer_html: String,
    depth: usize,
    word_count: usize,
    text_bytes: usize,
    html_bytes: usize,
    link_count: usize,
    link_text_bytes: usize,
    descendant_element_count: usize,
    text_density: f64,
    link_density: f64,
    text_truncated: bool,
    html_truncated: bool,
    features_truncated: bool,
}

/// Bounded result and provenance for one DOM-to-block conversion.
#[pyclass(frozen)]
pub(crate) struct NativeDocumentBlocks {
    blocks: Vec<Py<NativeSemanticBlock>>,
    #[pyo3(get)]
    schema_version: &'static str,
    #[pyo3(get)]
    input_bytes: usize,
    #[pyo3(get)]
    parsed_bytes: usize,
    #[pyo3(get)]
    node_count: usize,
    #[pyo3(get)]
    removed_node_count: usize,
    #[pyo3(get)]
    parse_error_count: usize,
    #[pyo3(get)]
    stored_text_bytes: usize,
    #[pyo3(get)]
    stored_html_bytes: usize,
    #[pyo3(get)]
    input_truncated: bool,
    #[pyo3(get)]
    nodes_truncated: bool,
    #[pyo3(get)]
    depth_truncated: bool,
    #[pyo3(get)]
    blocks_truncated: bool,
    #[pyo3(get)]
    text_truncated_blocks: usize,
    #[pyo3(get)]
    html_truncated_blocks: usize,
    #[pyo3(get)]
    features_truncated_blocks: usize,
    #[pyo3(get)]
    truncated: bool,
    #[pyo3(get)]
    truncation_reasons: Vec<String>,
    #[pyo3(get)]
    max_input_bytes: usize,
    #[pyo3(get)]
    max_nodes: usize,
    #[pyo3(get)]
    max_blocks: usize,
    #[pyo3(get)]
    max_depth: usize,
    #[pyo3(get)]
    max_block_text_bytes: usize,
    #[pyo3(get)]
    max_block_html_bytes: usize,
    #[pyo3(get)]
    max_total_text_bytes: usize,
    #[pyo3(get)]
    max_total_html_bytes: usize,
}

#[pymethods]
impl NativeDocumentBlocks {
    #[getter]
    fn blocks(&self, py: Python<'_>) -> Vec<Py<NativeSemanticBlock>> {
        self.blocks
            .iter()
            .map(|block| block.clone_ref(py))
            .collect()
    }

    #[getter]
    fn block_count(&self) -> usize {
        self.blocks.len()
    }
}

#[derive(Debug)]
struct DocumentBlocks {
    blocks: Vec<NativeSemanticBlock>,
    input_bytes: usize,
    parsed_bytes: usize,
    node_count: usize,
    removed_node_count: usize,
    parse_error_count: usize,
    stored_text_bytes: usize,
    stored_html_bytes: usize,
    input_truncated: bool,
    nodes_truncated: bool,
    depth_truncated: bool,
    blocks_truncated: bool,
    text_truncated_blocks: usize,
    html_truncated_blocks: usize,
    features_truncated_blocks: usize,
    truncation_reasons: Vec<String>,
    limits: Limits,
}

impl DocumentBlocks {
    fn is_truncated(&self) -> bool {
        self.input_truncated
            || self.nodes_truncated
            || self.depth_truncated
            || self.blocks_truncated
            || self.text_truncated_blocks > 0
            || self.html_truncated_blocks > 0
            || self.features_truncated_blocks > 0
    }

    fn into_python(self, py: Python<'_>) -> PyResult<NativeDocumentBlocks> {
        let truncated = self.is_truncated();
        let blocks = self
            .blocks
            .into_iter()
            .map(|block| Py::new(py, block))
            .collect::<PyResult<Vec<_>>>()?;

        Ok(NativeDocumentBlocks {
            blocks,
            schema_version: SCHEMA_VERSION,
            input_bytes: self.input_bytes,
            parsed_bytes: self.parsed_bytes,
            node_count: self.node_count,
            removed_node_count: self.removed_node_count,
            parse_error_count: self.parse_error_count,
            stored_text_bytes: self.stored_text_bytes,
            stored_html_bytes: self.stored_html_bytes,
            input_truncated: self.input_truncated,
            nodes_truncated: self.nodes_truncated,
            depth_truncated: self.depth_truncated,
            blocks_truncated: self.blocks_truncated,
            text_truncated_blocks: self.text_truncated_blocks,
            html_truncated_blocks: self.html_truncated_blocks,
            features_truncated_blocks: self.features_truncated_blocks,
            truncated,
            truncation_reasons: self.truncation_reasons,
            max_input_bytes: self.limits.max_input_bytes,
            max_nodes: self.limits.max_nodes,
            max_blocks: self.limits.max_blocks,
            max_depth: self.limits.max_depth,
            max_block_text_bytes: self.limits.max_block_text_bytes,
            max_block_html_bytes: self.limits.max_block_html_bytes,
            max_total_text_bytes: self.limits.max_total_text_bytes,
            max_total_html_bytes: self.limits.max_total_html_bytes,
        })
    }
}

#[derive(Clone, Debug, Default)]
struct NodeContext {
    depth: usize,
    semantic_parent_id: Option<String>,
    suppress_atomic_descendants: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum BlockKind {
    Container,
    Atomic,
}

impl BlockKind {
    fn is_atomic(self) -> bool {
        self == Self::Atomic
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct BlockClass {
    kind: BlockKind,
    selectable: bool,
}

impl BlockClass {
    const fn structural_container() -> Self {
        Self {
            kind: BlockKind::Container,
            selectable: false,
        }
    }

    const fn selectable_container() -> Self {
        Self {
            kind: BlockKind::Container,
            selectable: true,
        }
    }

    const fn atomic() -> Self {
        Self {
            kind: BlockKind::Atomic,
            selectable: true,
        }
    }
}

#[derive(Debug)]
struct Features {
    link_count: usize,
    link_text_bytes: usize,
    descendant_element_count: usize,
    truncated: bool,
}

#[allow(clippy::too_many_arguments)]
#[pyfunction]
#[pyo3(signature = (
    html,
    max_input_bytes=DEFAULT_MAX_INPUT_BYTES,
    max_nodes=DEFAULT_MAX_NODES,
    max_blocks=DEFAULT_MAX_BLOCKS,
    max_depth=DEFAULT_MAX_DEPTH,
    max_block_text_bytes=DEFAULT_MAX_BLOCK_TEXT_BYTES,
    max_block_html_bytes=DEFAULT_MAX_BLOCK_HTML_BYTES,
    max_total_text_bytes=DEFAULT_MAX_TOTAL_TEXT_BYTES,
    max_total_html_bytes=DEFAULT_MAX_TOTAL_HTML_BYTES,
))]
pub(crate) fn extract_document_blocks(
    py: Python<'_>,
    html: &str,
    max_input_bytes: usize,
    max_nodes: usize,
    max_blocks: usize,
    max_depth: usize,
    max_block_text_bytes: usize,
    max_block_html_bytes: usize,
    max_total_text_bytes: usize,
    max_total_html_bytes: usize,
) -> PyResult<NativeDocumentBlocks> {
    let limits = Limits::validated(
        max_input_bytes,
        max_nodes,
        max_blocks,
        max_depth,
        max_block_text_bytes,
        max_block_html_bytes,
        max_total_text_bytes,
        max_total_html_bytes,
    )?;

    // Borrow Python's UTF-8 view long enough to copy only the accepted prefix.
    // A `String` argument would copy an arbitrarily large caller input before
    // this API had an opportunity to enforce max_input_bytes.
    let input_bytes = html.len();
    let (parsed_html, input_truncated) = bounded_utf8_prefix(html, limits.max_input_bytes);
    let parsed_html = parsed_html.to_owned();

    // The bounded owned source and the plain Rust result make the parse and
    // feature pass safe to execute without CPython's interpreter lock.
    let result = py.detach(move || {
        build_prepared_document_blocks(parsed_html, input_bytes, input_truncated, limits)
    });
    result.into_python(py)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<NativeSemanticBlock>()?;
    module.add_class::<NativeDocumentBlocks>()?;
    module.add_function(wrap_pyfunction!(extract_document_blocks, module)?)?;
    Ok(())
}

#[cfg(test)]
fn build_document_blocks(mut html: String, limits: Limits) -> DocumentBlocks {
    let input_bytes = html.len();
    let input_truncated = truncate_utf8_in_place(&mut html, limits.max_input_bytes);
    build_prepared_document_blocks(html, input_bytes, input_truncated, limits)
}

fn build_prepared_document_blocks(
    html: String,
    input_bytes: usize,
    input_truncated: bool,
    limits: Limits,
) -> DocumentBlocks {
    let parsed_bytes = html.len();
    let document = Document::from(html);

    let (removed_node_count, removal_scan_truncated) =
        remove_non_content_nodes(&document, limits.max_nodes);
    let parse_error_count = document.errors.borrow().len();
    if removal_scan_truncated {
        // Sanitization is a prerequisite for every emitted block. Continuing
        // with a partially scanned DOM could let an early container's text or
        // HTML serialization include a script/hidden descendant beyond the
        // cap. Fail closed instead of exposing unsanitized partial content.
        let mut truncation_reasons = Vec::with_capacity(2);
        if input_truncated {
            truncation_reasons.push("input_bytes".to_owned());
        }
        truncation_reasons.push("node_count".to_owned());
        return DocumentBlocks {
            blocks: Vec::new(),
            input_bytes,
            parsed_bytes,
            node_count: limits.max_nodes,
            removed_node_count,
            parse_error_count,
            stored_text_bytes: 0,
            stored_html_bytes: 0,
            input_truncated,
            nodes_truncated: true,
            depth_truncated: false,
            blocks_truncated: false,
            text_truncated_blocks: 0,
            html_truncated_blocks: 0,
            features_truncated_blocks: 0,
            truncation_reasons,
            limits,
        };
    }
    let mut blocks = Vec::with_capacity(limits.max_blocks.min(256));
    let mut contexts: HashMap<NodeId, NodeContext> =
        HashMap::with_capacity(limits.max_nodes.min(4_096));
    let mut node_count = 0;
    let mut nodes_truncated = removal_scan_truncated;
    let mut depth_truncated = false;
    let mut blocks_truncated = false;
    let mut stored_text_bytes = 0;
    let mut stored_html_bytes = 0;

    for node in document.root().descendants_it() {
        node_count += 1;
        if node_count > limits.max_nodes {
            node_count = limits.max_nodes;
            nodes_truncated = true;
            break;
        }
        if !node.is_element() {
            continue;
        }

        let parent_context = node
            .parent()
            .and_then(|parent| contexts.get(&parent.id))
            .cloned()
            .unwrap_or_default();
        let depth = parent_context.depth.saturating_add(1);
        let mut context = NodeContext {
            depth,
            semantic_parent_id: parent_context.semantic_parent_id.clone(),
            suppress_atomic_descendants: parent_context.suppress_atomic_descendants,
        };

        if depth > limits.max_depth {
            depth_truncated = true;
            context.suppress_atomic_descendants = true;
            contexts.insert(node.id, context);
            continue;
        }

        let tag = node
            .node_name()
            .map(|value| value.to_string().to_ascii_lowercase())
            .unwrap_or_default();
        let explicit_role = normalized_role(&node);
        let class = classify_block(&node, &tag, explicit_role.as_deref(), limits.max_nodes);

        if !context.suppress_atomic_descendants {
            if let Some(class) = class {
                if blocks.len() >= limits.max_blocks
                    || (stored_text_bytes >= limits.max_total_text_bytes
                        && stored_html_bytes >= limits.max_total_html_bytes)
                {
                    blocks_truncated = true;
                    break;
                }

                let role = explicit_role.unwrap_or_else(|| role_for_tag(&tag).to_owned());
                if let Some(block) = make_block(
                    &node,
                    blocks.len(),
                    context.semantic_parent_id.clone(),
                    tag,
                    role,
                    class,
                    depth,
                    &limits,
                    &mut stored_text_bytes,
                    &mut stored_html_bytes,
                ) {
                    let block_id = block.id.clone();
                    blocks.push(block);
                    match class.kind {
                        BlockKind::Container => context.semantic_parent_id = Some(block_id),
                        BlockKind::Atomic => context.suppress_atomic_descendants = true,
                    }
                }
            }
        }

        contexts.insert(node.id, context);
    }

    let text_truncated_blocks = blocks.iter().filter(|block| block.text_truncated).count();
    let html_truncated_blocks = blocks.iter().filter(|block| block.html_truncated).count();
    let features_truncated_blocks = blocks
        .iter()
        .filter(|block| block.features_truncated)
        .count();
    let mut truncation_reasons = Vec::with_capacity(7);
    if input_truncated {
        truncation_reasons.push("input_bytes".to_owned());
    }
    if nodes_truncated {
        truncation_reasons.push("node_count".to_owned());
    }
    if depth_truncated {
        truncation_reasons.push("dom_depth".to_owned());
    }
    if blocks_truncated {
        truncation_reasons.push("block_count_or_total_output".to_owned());
    }
    if text_truncated_blocks > 0 {
        truncation_reasons.push("block_text".to_owned());
    }
    if html_truncated_blocks > 0 {
        truncation_reasons.push("block_html".to_owned());
    }
    if features_truncated_blocks > 0 {
        truncation_reasons.push("feature_scan".to_owned());
    }

    DocumentBlocks {
        blocks,
        input_bytes,
        parsed_bytes,
        node_count,
        removed_node_count,
        parse_error_count,
        stored_text_bytes,
        stored_html_bytes,
        input_truncated,
        nodes_truncated,
        depth_truncated,
        blocks_truncated,
        text_truncated_blocks,
        html_truncated_blocks,
        features_truncated_blocks,
        truncation_reasons,
        limits,
    }
}

fn remove_non_content_nodes(document: &Document, max_nodes: usize) -> (usize, bool) {
    let mut remove = Vec::new();
    let mut visited = 0;
    let mut scan_truncated = false;

    for node in document.root().descendants_it() {
        visited += 1;
        if visited > max_nodes {
            scan_truncated = true;
            break;
        }
        if node.is_element() && should_remove(&node) {
            remove.push(node.id);
        }
    }

    let removed = remove.len();
    for id in remove {
        if let Some(node) = document.tree.get(&id) {
            node.remove_from_parent();
        }
    }
    (removed, scan_truncated)
}

fn should_remove(node: &NodeRef<'_>) -> bool {
    let tag = node
        .node_name()
        .map(|value| value.to_string().to_ascii_lowercase())
        .unwrap_or_default();
    if matches!(
        tag.as_str(),
        "head"
            | "script"
            | "style"
            | "template"
            | "noscript"
            | "meta"
            | "link"
            | "base"
            | "svg"
            | "canvas"
            | "iframe"
            | "object"
            | "embed"
            | "source"
            | "track"
            | "input"
            | "button"
            | "select"
            | "option"
            | "textarea"
    ) {
        return true;
    }
    if node.has_attr("hidden") || node.has_attr("inert") {
        return true;
    }
    if node
        .attr("aria-hidden")
        .is_some_and(|value| value.eq_ignore_ascii_case("true"))
    {
        return true;
    }
    if tag == "input"
        && node
            .attr("type")
            .is_some_and(|value| value.eq_ignore_ascii_case("hidden"))
    {
        return true;
    }
    node.attr("style").is_some_and(|style| {
        let compact: String = style
            .chars()
            .take(1_024)
            .filter(|character| !character.is_ascii_whitespace())
            .flat_map(char::to_lowercase)
            .collect();
        compact.contains("display:none")
            || compact.contains("visibility:hidden")
            || compact.contains("content-visibility:hidden")
    })
}

fn normalized_role(node: &NodeRef<'_>) -> Option<String> {
    node.attr("role").and_then(|role| {
        let role = role
            .split_ascii_whitespace()
            .next()
            .unwrap_or_default()
            .chars()
            .take(64)
            .flat_map(char::to_lowercase)
            .collect::<String>();
        (!role.is_empty() && role != "none" && role != "presentation").then_some(role)
    })
}

fn classify_block(
    node: &NodeRef<'_>,
    tag: &str,
    explicit_role: Option<&str>,
    max_nodes: usize,
) -> Option<BlockClass> {
    if matches!(
        tag,
        "main" | "article" | "section" | "nav" | "aside" | "header" | "footer"
    ) {
        return Some(if has_semantic_descendant(node, max_nodes) {
            BlockClass::structural_container()
        } else {
            BlockClass::selectable_container()
        });
    }
    if matches!(
        tag,
        "table" | "thead" | "tbody" | "tfoot" | "tr" | "ul" | "ol" | "dl"
    ) {
        return Some(BlockClass::structural_container());
    }
    if matches!(
        tag,
        "td" | "th" | "li" | "dt" | "dd" | "blockquote" | "pre" | "figure" | "details"
    ) {
        return Some(if has_semantic_descendant(node, max_nodes) {
            BlockClass::structural_container()
        } else {
            BlockClass::selectable_container()
        });
    }
    if matches!(
        tag,
        "h1" | "h2"
            | "h3"
            | "h4"
            | "h5"
            | "h6"
            | "p"
            | "code"
            | "caption"
            | "figcaption"
            | "summary"
            | "address"
            | "img"
            | "math"
    ) {
        return Some(BlockClass::atomic());
    }
    if tag == "a" && is_bounded_inline_leaf(node) {
        return Some(BlockClass::atomic());
    }
    if let Some(role) = explicit_role {
        return Some(class_for_role(
            role,
            has_semantic_descendant(node, max_nodes),
        ));
    }
    if matches!(tag, "div" | "body" | "center") && !has_semantic_descendant(node, max_nodes) {
        return Some(BlockClass::atomic());
    }
    None
}

fn has_semantic_descendant(node: &NodeRef<'_>, max_nodes: usize) -> bool {
    node.descendants_it()
        .take(max_nodes)
        .filter(|descendant| descendant.is_element())
        .any(|descendant| {
            let tag = descendant
                .node_name()
                .map(|value| value.to_string().to_ascii_lowercase())
                .unwrap_or_default();
            matches!(
                tag.as_str(),
                "main"
                    | "article"
                    | "section"
                    | "nav"
                    | "aside"
                    | "header"
                    | "footer"
                    | "h1"
                    | "h2"
                    | "h3"
                    | "h4"
                    | "h5"
                    | "h6"
                    | "p"
                    | "blockquote"
                    | "pre"
                    | "code"
                    | "table"
                    | "thead"
                    | "tbody"
                    | "tfoot"
                    | "tr"
                    | "td"
                    | "th"
                    | "ul"
                    | "ol"
                    | "dl"
                    | "li"
                    | "dt"
                    | "dd"
                    | "figure"
                    | "figcaption"
                    | "details"
                    | "summary"
                    | "address"
                    | "img"
                    | "math"
                    | "div"
            ) || (tag == "a" && is_bounded_inline_leaf(&descendant))
                || normalized_role(&descendant).is_some()
        })
}

fn is_bounded_inline_leaf(node: &NodeRef<'_>) -> bool {
    for ancestor in node.ancestors_it(Some(64)) {
        let tag = ancestor
            .node_name()
            .map(|value| value.to_string().to_ascii_lowercase())
            .unwrap_or_default();
        if matches!(tag.as_str(), "td" | "th" | "li" | "dt" | "dd") {
            return true;
        }
        if matches!(
            tag.as_str(),
            "p" | "h1"
                | "h2"
                | "h3"
                | "h4"
                | "h5"
                | "h6"
                | "pre"
                | "code"
                | "caption"
                | "figcaption"
        ) {
            return false;
        }
    }
    false
}

fn class_for_role(role: &str, has_semantic_descendant: bool) -> BlockClass {
    if matches!(
        role,
        "main"
            | "article"
            | "region"
            | "navigation"
            | "complementary"
            | "banner"
            | "contentinfo"
            | "feed"
            | "group"
    ) {
        if has_semantic_descendant {
            BlockClass::structural_container()
        } else {
            BlockClass::selectable_container()
        }
    } else {
        // An explicit, non-presentational ARIA role is semantic even when it is
        // not one of the native HTML landmark roles. Keeping it as one atomic
        // block avoids dropping custom roles such as `note` or `status`.
        BlockClass::atomic()
    }
}

fn role_for_tag(tag: &str) -> &'static str {
    match tag {
        "main" => "main",
        "article" => "article",
        "section" => "section",
        "nav" => "navigation",
        "aside" => "complementary",
        "header" => "banner",
        "footer" => "contentinfo",
        "h1" | "h2" | "h3" | "h4" | "h5" | "h6" => "heading",
        "p" => "paragraph",
        "blockquote" => "blockquote",
        "pre" => "preformatted",
        "code" => "code",
        "table" => "table",
        "thead" | "tbody" | "tfoot" => "rowgroup",
        "tr" => "row",
        "td" => "cell",
        "th" => "columnheader",
        "caption" => "caption",
        "ul" | "ol" => "list",
        "dl" => "description-list",
        "li" => "listitem",
        "dt" => "term",
        "dd" => "definition",
        "figure" => "figure",
        "figcaption" => "caption",
        "details" => "details",
        "summary" => "summary",
        "address" => "address",
        "img" => "image",
        "math" => "math",
        "a" => "link",
        _ => "generic",
    }
}

#[allow(clippy::too_many_arguments)]
fn make_block(
    node: &NodeRef<'_>,
    order: usize,
    parent_id: Option<String>,
    tag: String,
    role: String,
    class: BlockClass,
    depth: usize,
    limits: &Limits,
    stored_text_bytes: &mut usize,
    stored_html_bytes: &mut usize,
) -> Option<NativeSemanticBlock> {
    let preserve_whitespace = matches!(tag.as_str(), "pre" | "code");
    let raw_text = if tag == "img" {
        node.attr("alt")
            .map(|value| value.to_string())
            .unwrap_or_default()
    } else {
        node.text().to_string()
    };
    let normalized_text = if preserve_whitespace {
        normalize_preformatted_text(&raw_text)
    } else {
        normalize_text(&raw_text)
    };
    let outer_html = node
        .try_html()
        .map(|value| value.to_string())
        .unwrap_or_default();

    if normalized_text.is_empty()
        && !matches!(
            tag.as_str(),
            "img" | "table" | "figure" | "math" | "pre" | "code"
        )
    {
        return None;
    }

    let text_bytes = normalized_text.len();
    let html_bytes = outer_html.len();
    let word_count = normalized_text.split_whitespace().count();
    let features = collect_features(node, limits.max_nodes);
    let text_density = if html_bytes == 0 {
        0.0
    } else {
        (text_bytes as f64 / html_bytes as f64).clamp(0.0, 1.0)
    };
    let link_density = if text_bytes == 0 {
        0.0
    } else {
        (features.link_text_bytes as f64 / text_bytes as f64).clamp(0.0, 1.0)
    };

    let text_budget = limits.max_block_text_bytes.min(
        limits
            .max_total_text_bytes
            .saturating_sub(*stored_text_bytes),
    );
    let html_budget = limits.max_block_html_bytes.min(
        limits
            .max_total_html_bytes
            .saturating_sub(*stored_html_bytes),
    );
    let (text, text_truncated) = truncate_utf8(normalized_text, text_budget);
    let (outer_html, html_truncated) = truncate_utf8(outer_html, html_budget);
    *stored_text_bytes += text.len();
    *stored_html_bytes += outer_html.len();

    Some(NativeSemanticBlock {
        id: format!("block-{order:06}"),
        order,
        parent_id,
        tag,
        role,
        atomic: class.kind.is_atomic(),
        selectable: class.selectable,
        preserve_whitespace,
        text,
        outer_html,
        depth,
        word_count,
        text_bytes,
        html_bytes,
        link_count: features.link_count,
        link_text_bytes: features.link_text_bytes,
        descendant_element_count: features.descendant_element_count,
        text_density,
        link_density,
        text_truncated,
        html_truncated,
        features_truncated: features.truncated,
    })
}

fn collect_features(node: &NodeRef<'_>, max_nodes: usize) -> Features {
    let mut link_count = usize::from(
        node.node_name()
            .is_some_and(|name| name.eq_ignore_ascii_case("a"))
            && node.has_attr("href"),
    );
    let mut link_text_bytes = if link_count == 1 {
        normalize_text(&node.text()).len()
    } else {
        0
    };
    let mut descendant_element_count = 0;
    let mut visited = 0;
    let mut truncated = false;

    for descendant in node.descendants_it() {
        visited += 1;
        if visited > max_nodes {
            truncated = true;
            break;
        }
        if !descendant.is_element() {
            continue;
        }
        descendant_element_count += 1;
        if descendant
            .node_name()
            .is_some_and(|name| name.eq_ignore_ascii_case("a"))
            && descendant.has_attr("href")
        {
            link_count += 1;
            link_text_bytes += normalize_text(&descendant.text()).len();
        }
    }

    Features {
        link_count,
        link_text_bytes,
        descendant_element_count,
        truncated,
    }
}

fn normalize_text(value: &str) -> String {
    let mut result = String::with_capacity(value.len().min(4_096));
    let mut pending_space = false;
    for character in value.chars().filter(|character| *character != '\0') {
        if character.is_whitespace() {
            pending_space = !result.is_empty();
        } else {
            if pending_space {
                result.push(' ');
                pending_space = false;
            }
            result.push(character);
        }
    }
    result
}

fn normalize_preformatted_text(value: &str) -> String {
    value
        .replace("\r\n", "\n")
        .replace('\r', "\n")
        .replace('\0', "")
        .trim_matches('\n')
        .to_owned()
}

fn truncate_utf8(mut value: String, max_bytes: usize) -> (String, bool) {
    let truncated = truncate_utf8_in_place(&mut value, max_bytes);
    (value, truncated)
}

fn truncate_utf8_in_place(value: &mut String, max_bytes: usize) -> bool {
    let (prefix, truncated) = bounded_utf8_prefix(value, max_bytes);
    if !truncated {
        return false;
    }
    let end = prefix.len();
    value.truncate(end);
    true
}

fn bounded_utf8_prefix(value: &str, max_bytes: usize) -> (&str, bool) {
    if value.len() <= max_bytes {
        return (value, false);
    }
    let mut end = max_bytes;
    while end > 0 && !value.is_char_boundary(end) {
        end -= 1;
    }
    (&value[..end], true)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn generous_limits() -> Limits {
        Limits {
            max_input_bytes: DEFAULT_MAX_INPUT_BYTES,
            max_nodes: DEFAULT_MAX_NODES,
            max_blocks: DEFAULT_MAX_BLOCKS,
            max_depth: DEFAULT_MAX_DEPTH,
            max_block_text_bytes: DEFAULT_MAX_BLOCK_TEXT_BYTES,
            max_block_html_bytes: DEFAULT_MAX_BLOCK_HTML_BYTES,
            max_total_text_bytes: DEFAULT_MAX_TOTAL_TEXT_BYTES,
            max_total_html_bytes: DEFAULT_MAX_TOTAL_HTML_BYTES,
        }
    }

    #[test]
    fn emits_ordered_semantic_blocks_without_nested_atomic_duplicates() {
        let html = r#"
            <main>
              <script>secret()</script>
              <h1>  A   title </h1>
              <p>Read <a href="/x">this link</a>.</p>
              <ul><li>one</li><li hidden>hidden</li><li>two</li></ul>
              <pre><code>let x = 1;
  x += 1;</code></pre>
              <table><tr><td>cell</td></tr></table>
              <p style="display: none">not visible</p>
            </main>
        "#;
        let result = build_document_blocks(html.to_owned(), generous_limits());
        let tags = result
            .blocks
            .iter()
            .map(|block| block.tag.as_str())
            .collect::<Vec<_>>();

        assert_eq!(
            tags,
            vec!["main", "h1", "p", "ul", "li", "li", "pre", "code", "table", "tbody", "tr", "td"]
        );
        assert_eq!(
            result
                .blocks
                .iter()
                .filter(|block| block.selectable)
                .map(|block| block.tag.as_str())
                .collect::<Vec<_>>(),
            vec!["h1", "p", "li", "li", "code", "td"]
        );
        assert_eq!(result.blocks[4].parent_id.as_deref(), Some("block-000003"));
        assert_eq!(result.blocks[7].parent_id.as_deref(), Some("block-000006"));
        assert_eq!(result.blocks[11].parent_id.as_deref(), Some("block-000010"));
        assert_eq!(result.blocks[1].text, "A title");
        assert_eq!(result.blocks[2].link_count, 1);
        assert_eq!(result.blocks[2].link_text_bytes, "this link".len());
        assert!(result.blocks[6].preserve_whitespace);
        assert!(result.blocks[7].text.contains("  x += 1;"));
        assert!(result
            .blocks
            .iter()
            .filter(|block| block.atomic)
            .all(|block| block.selectable));
        assert!(!result
            .blocks
            .iter()
            .any(|block| block.text.contains("secret") || block.text.contains("not visible")));
    }

    #[test]
    fn compound_containers_expose_local_selectable_descendants() {
        let html = r#"
            <table class="layout">
              <tr><td><p>noise</p><p>target</p></td><td>other cell</td></tr>
            </table>
        "#;
        let result = build_document_blocks(html.to_owned(), generous_limits());
        let selectable = result
            .blocks
            .iter()
            .filter(|block| block.selectable)
            .collect::<Vec<_>>();

        assert_eq!(
            selectable
                .iter()
                .map(|block| (block.tag.as_str(), block.text.as_str()))
                .collect::<Vec<_>>(),
            vec![("p", "noise"), ("p", "target"), ("td", "other cell")]
        );
        let target = selectable[1];
        let cell = result
            .blocks
            .iter()
            .find(|block| block.id == target.parent_id.as_deref().unwrap())
            .unwrap();
        assert_eq!(cell.tag, "td");
        assert!(!cell.selectable);
        assert_eq!(cell.parent_id.as_deref(), Some("block-000002"));
    }

    #[test]
    fn mixed_layout_cell_exposes_bounded_link_leaf() {
        let html =
            r#"<table><tr><td>noise <a href="/target">target</a> trailing noise</td></tr></table>"#;
        let result = build_document_blocks(html.to_owned(), generous_limits());
        let selectable = result
            .blocks
            .iter()
            .filter(|block| block.selectable)
            .collect::<Vec<_>>();

        assert_eq!(selectable.len(), 1);
        assert_eq!(selectable[0].tag, "a");
        assert_eq!(selectable[0].role, "link");
        assert_eq!(selectable[0].text, "target");
        let cell = result
            .blocks
            .iter()
            .find(|block| block.id == selectable[0].parent_id.as_deref().unwrap())
            .unwrap();
        assert_eq!(cell.tag, "td");
        assert!(!cell.selectable);
    }

    #[test]
    fn ids_and_output_are_stable_for_the_same_document() {
        let html = "<article><h2>Hello</h2><p>world</p></article>";
        let first = build_document_blocks(html.to_owned(), generous_limits());
        let second = build_document_blocks(html.to_owned(), generous_limits());
        assert_eq!(first.blocks, second.blocks);
    }

    #[test]
    fn all_output_budgets_are_explicit_and_utf8_safe() {
        let mut limits = generous_limits();
        limits.max_input_bytes = 90;
        limits.max_blocks = 2;
        limits.max_block_text_bytes = 7;
        limits.max_block_html_bytes = 13;
        limits.max_total_text_bytes = 10;
        limits.max_total_html_bytes = 20;
        let html = format!(
            "<main><p>{}</p><p>second block</p><p>third block</p></main>",
            "é".repeat(80)
        );
        let result = build_document_blocks(html, limits);

        assert!(result.input_truncated);
        assert!(result.blocks.len() <= 2);
        assert!(result.stored_text_bytes <= limits.max_total_text_bytes);
        assert!(result.stored_html_bytes <= limits.max_total_html_bytes);
        assert!(result
            .blocks
            .iter()
            .all(|block| block.text.is_char_boundary(block.text.len())));
        assert!(result.is_truncated());
        assert!(result
            .truncation_reasons
            .iter()
            .any(|reason| reason == "input_bytes"));
    }

    #[test]
    fn captures_plain_leaf_div_and_link_density() {
        let html = r#"<body><div>Hello <a href="/a">linked world</a></div></body>"#;
        let result = build_document_blocks(html.to_owned(), generous_limits());
        assert_eq!(result.blocks.len(), 1);
        let block = &result.blocks[0];
        assert_eq!(block.tag, "div");
        assert_eq!(block.text, "Hello linked world");
        assert_eq!(block.link_count, 1);
        assert!(block.link_density > 0.5);
        assert!(block.text_density > 0.0);
    }

    #[test]
    fn node_and_depth_caps_stop_hostile_trees_with_provenance() {
        let mut node_limits = generous_limits();
        node_limits.max_nodes = 8;
        let many_nodes = format!(
            "<main>{}<script>hidden tail</script></main>",
            "<p>node</p>".repeat(100)
        );
        let node_result = build_document_blocks(many_nodes, node_limits);
        assert!(node_result.nodes_truncated);
        assert_eq!(node_result.node_count, node_limits.max_nodes);
        assert!(node_result
            .truncation_reasons
            .iter()
            .any(|reason| reason == "node_count"));
        assert!(node_result.blocks.is_empty());
        assert!(!node_result
            .blocks
            .iter()
            .any(|block| block.text.contains("hidden tail")));

        let mut depth_limits = generous_limits();
        depth_limits.max_depth = 4;
        let deep_tree =
            "<main><section><article><div><div><div><p>too deep</p></div></div></div></article></section></main>";
        let depth_result = build_document_blocks(deep_tree.to_owned(), depth_limits);
        assert!(depth_result.depth_truncated);
        assert!(!depth_result.blocks.iter().any(|block| block.tag == "p"));
        assert!(depth_result
            .truncation_reasons
            .iter()
            .any(|reason| reason == "dom_depth"));
    }

    #[test]
    fn removal_scan_never_visits_or_removes_nodes_after_the_node_cap() {
        let document = Document::from(
            r#"<main><p>before cap</p><script id="after-cap">secret</script></main>"#.to_owned(),
        );

        let (removed, truncated) = remove_non_content_nodes(&document, 1);

        assert!(truncated);
        assert_eq!(removed, 0);
        assert_eq!(document.select("#after-cap").length(), 1);
        assert_eq!(document.select("#after-cap").text().to_string(), "secret");
    }
}

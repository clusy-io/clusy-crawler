use std::collections::{HashMap, HashSet};

use dom_query::{Document, NodeData, NodeId, NodeRef};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

mod selection_certificate_v0;
mod source_text_mapper_v2;

const SCHEMA_VERSION: &str = "ordered-dom-ir.v2";
const SERIALIZATION_CONTRACT: &str = "ordered-dom-ir.v2.markdown.1";

const DEFAULT_MAX_INPUT_BYTES: usize = 4 * 1024 * 1024;
const DEFAULT_MAX_NODES: usize = 200_000;
const DEFAULT_MAX_ELEMENTS: usize = 100_000;
const DEFAULT_MAX_TEXT_RUNS: usize = 200_000;
const DEFAULT_MAX_DEPTH: usize = 256;
const DEFAULT_MAX_TEXT_RUN_BYTES: usize = 256 * 1024;
const DEFAULT_MAX_TOTAL_TEXT_BYTES: usize = 8 * 1024 * 1024;
const DEFAULT_MAX_MATH_BYTES: usize = 256 * 1024;
const DEFAULT_MAX_TABLE_COLUMNS: usize = 1_024;

const HARD_MAX_INPUT_BYTES: usize = 16 * 1024 * 1024;
const HARD_MAX_NODES: usize = 500_000;
const HARD_MAX_ELEMENTS: usize = 250_000;
const HARD_MAX_TEXT_RUNS: usize = 500_000;
const HARD_MAX_DEPTH: usize = 512;
const HARD_MAX_TEXT_RUN_BYTES: usize = 1024 * 1024;
const HARD_MAX_TOTAL_TEXT_BYTES: usize = 32 * 1024 * 1024;
const HARD_MAX_MATH_BYTES: usize = 2 * 1024 * 1024;
const HARD_MAX_TABLE_COLUMNS: usize = 4_096;

#[derive(Clone, Copy, Debug)]
struct LimitsV2 {
    max_input_bytes: usize,
    max_nodes: usize,
    max_elements: usize,
    max_text_runs: usize,
    max_depth: usize,
    max_text_run_bytes: usize,
    max_total_text_bytes: usize,
    max_math_bytes: usize,
    max_table_columns: usize,
}

impl LimitsV2 {
    #[allow(clippy::too_many_arguments)]
    fn validated(
        max_input_bytes: usize,
        max_nodes: usize,
        max_elements: usize,
        max_text_runs: usize,
        max_depth: usize,
        max_text_run_bytes: usize,
        max_total_text_bytes: usize,
        max_math_bytes: usize,
        max_table_columns: usize,
    ) -> PyResult<Self> {
        validate_limit("max_input_bytes", max_input_bytes, HARD_MAX_INPUT_BYTES)?;
        validate_limit("max_nodes", max_nodes, HARD_MAX_NODES)?;
        validate_limit("max_elements", max_elements, HARD_MAX_ELEMENTS)?;
        validate_limit("max_text_runs", max_text_runs, HARD_MAX_TEXT_RUNS)?;
        validate_limit("max_depth", max_depth, HARD_MAX_DEPTH)?;
        validate_limit(
            "max_text_run_bytes",
            max_text_run_bytes,
            HARD_MAX_TEXT_RUN_BYTES,
        )?;
        validate_limit(
            "max_total_text_bytes",
            max_total_text_bytes,
            HARD_MAX_TOTAL_TEXT_BYTES,
        )?;
        validate_limit("max_math_bytes", max_math_bytes, HARD_MAX_MATH_BYTES)?;
        validate_limit(
            "max_table_columns",
            max_table_columns,
            HARD_MAX_TABLE_COLUMNS,
        )?;
        Ok(Self {
            max_input_bytes,
            max_nodes,
            max_elements,
            max_text_runs,
            max_depth,
            max_text_run_bytes,
            max_total_text_bytes,
            max_math_bytes,
            max_table_columns,
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

/// One retained DOM element. IDs are derived from the deterministic DOM path,
/// while `order` is the shared preorder across elements and text runs.
#[pyclass(frozen, get_all)]
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct NativeIRElementV2 {
    id: String,
    order: usize,
    parent_id: Option<String>,
    child_ids: Vec<String>,
    text_run_ids: Vec<String>,
    tag: String,
    role: String,
    path: String,
    depth: usize,
    block: bool,
    preserve_whitespace: bool,
    implicit: bool,
    source_start: Option<usize>,
    source_start_tag_end: Option<usize>,
    source_end: Option<usize>,
    source_span_reliable: bool,
    heading_level: Option<u8>,
    href: Option<String>,
    src: Option<String>,
    alt: Option<String>,
    language: Option<String>,
}

/// An exact decoded DOM text node. Whitespace is not normalized in the IR.
#[pyclass(frozen, get_all)]
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct NativeIRTextRunV2 {
    id: String,
    order: usize,
    parent_id: String,
    path: String,
    text: String,
    preserve_whitespace: bool,
    original_bytes: usize,
    stored_bytes: usize,
    truncated: bool,
    source_start: Option<usize>,
    source_end: Option<usize>,
    source_span_reliable: bool,
}

#[pyclass(frozen, get_all)]
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct NativeIRTableV2 {
    id: String,
    node_id: String,
    order: usize,
    row_count: usize,
    column_count: usize,
    cell_ids: Vec<String>,
    grid_complete: bool,
}

#[pyclass(frozen, get_all)]
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct NativeIRTableCellV2 {
    id: String,
    node_id: String,
    table_id: String,
    order: usize,
    row_index: usize,
    column_index: usize,
    row_span: usize,
    column_span: usize,
    row_group: String,
    header: bool,
    scope: String,
    text_run_ids: Vec<String>,
    grid_complete: bool,
}

#[pyclass(frozen, get_all)]
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct NativeIRListV2 {
    id: String,
    node_id: String,
    order: usize,
    kind: String,
    depth: usize,
    start: Option<i64>,
    reversed: bool,
    marker_type: String,
    item_ids: Vec<String>,
}

#[pyclass(frozen, get_all)]
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct NativeIRListItemV2 {
    id: String,
    node_id: String,
    list_id: String,
    order: usize,
    depth: usize,
    index: usize,
    kind: String,
    ordinal: Option<i64>,
    explicit_value: Option<i64>,
    text_run_ids: Vec<String>,
}

#[pyclass(frozen, get_all)]
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct NativeIRMathV2 {
    id: String,
    node_id: String,
    order: usize,
    format: String,
    display: String,
    tex: Option<String>,
    mathml: Option<String>,
    source_markup: String,
    alt_text: Option<String>,
    source_backed: bool,
    truncated: bool,
}

/// Deterministic output plus the exact selection and completeness provenance.
#[pyclass(frozen, get_all)]
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct NativeIRSerializationV2 {
    contract_version: &'static str,
    markdown: String,
    selected_ids: Vec<String>,
    missing_ids: Vec<String>,
    deterministic: bool,
    exact_code_whitespace: bool,
    table_grid_complete: bool,
    source_complete: bool,
    truncated: bool,
    truncation_reasons: Vec<String>,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
enum EventRef {
    Element(usize),
    Text(usize),
}

#[derive(Clone, Debug)]
struct ElementRecord {
    exposed: NativeIRElementV2,
    raw_node_id: NodeId,
    parent_index: Option<usize>,
    children: Vec<EventRef>,
    attrs: HashMap<String, String>,
    nearest_table: Option<usize>,
    nearest_row: Option<usize>,
    nearest_row_group: Option<usize>,
    nearest_list: Option<usize>,
    nearest_math: Option<usize>,
}

#[derive(Clone, Debug)]
struct TextRecord {
    exposed: NativeIRTextRunV2,
    parent_index: usize,
    nearest_cell: Option<usize>,
    nearest_list_item: Option<usize>,
}

#[derive(Clone, Debug, Default)]
struct Graph {
    roots: Vec<EventRef>,
    elements: Vec<ElementRecord>,
    texts: Vec<TextRecord>,
    element_by_id: HashMap<String, usize>,
    tables: Vec<NativeIRTableV2>,
    cells: Vec<NativeIRTableCellV2>,
    lists: Vec<NativeIRListV2>,
    list_items: Vec<NativeIRListItemV2>,
    maths: Vec<NativeIRMathV2>,
}

/// Source-backed v2 graph. The source is the bounded UTF-8 prefix actually
/// parsed, so every exposed byte span is directly indexable into `source`.
#[pyclass(frozen)]
pub(crate) struct NativeDocumentIRV2 {
    elements: Vec<Py<NativeIRElementV2>>,
    text_runs: Vec<Py<NativeIRTextRunV2>>,
    tables: Vec<Py<NativeIRTableV2>>,
    table_cells: Vec<Py<NativeIRTableCellV2>>,
    lists: Vec<Py<NativeIRListV2>>,
    list_items: Vec<Py<NativeIRListItemV2>>,
    math: Vec<Py<NativeIRMathV2>>,
    graph: Graph,
    #[pyo3(get)]
    schema_version: &'static str,
    #[pyo3(get)]
    serialization_contract: &'static str,
    #[pyo3(get)]
    source: String,
    #[pyo3(get)]
    input_bytes: usize,
    #[pyo3(get)]
    parsed_bytes: usize,
    #[pyo3(get)]
    source_complete: bool,
    #[pyo3(get)]
    node_count: usize,
    #[pyo3(get)]
    parse_error_count: usize,
    #[pyo3(get)]
    event_count: usize,
    #[pyo3(get)]
    mapped_element_count: usize,
    #[pyo3(get)]
    implicit_element_count: usize,
    #[pyo3(get)]
    unmapped_explicit_element_count: usize,
    #[pyo3(get)]
    source_mapping_complete: bool,
    #[pyo3(get)]
    stored_text_bytes: usize,
    #[pyo3(get)]
    input_truncated: bool,
    #[pyo3(get)]
    nodes_truncated: bool,
    #[pyo3(get)]
    depth_truncated: bool,
    #[pyo3(get)]
    elements_truncated: bool,
    #[pyo3(get)]
    text_runs_truncated: bool,
    #[pyo3(get)]
    text_truncated_runs: usize,
    #[pyo3(get)]
    table_grid_truncated: bool,
    #[pyo3(get)]
    math_truncated_nodes: usize,
    #[pyo3(get)]
    truncated: bool,
    #[pyo3(get)]
    truncation_reasons: Vec<String>,
    #[pyo3(get)]
    root_ids: Vec<String>,
    #[pyo3(get)]
    max_input_bytes: usize,
    #[pyo3(get)]
    max_nodes: usize,
    #[pyo3(get)]
    max_elements: usize,
    #[pyo3(get)]
    max_text_runs: usize,
    #[pyo3(get)]
    max_depth: usize,
    #[pyo3(get)]
    max_text_run_bytes: usize,
    #[pyo3(get)]
    max_total_text_bytes: usize,
    #[pyo3(get)]
    max_math_bytes: usize,
    #[pyo3(get)]
    max_table_columns: usize,
}

#[pymethods]
impl NativeDocumentIRV2 {
    #[getter]
    fn elements(&self, py: Python<'_>) -> Vec<Py<NativeIRElementV2>> {
        self.elements
            .iter()
            .map(|value| value.clone_ref(py))
            .collect()
    }

    #[getter]
    fn text_runs(&self, py: Python<'_>) -> Vec<Py<NativeIRTextRunV2>> {
        self.text_runs
            .iter()
            .map(|value| value.clone_ref(py))
            .collect()
    }

    #[getter]
    fn tables(&self, py: Python<'_>) -> Vec<Py<NativeIRTableV2>> {
        self.tables
            .iter()
            .map(|value| value.clone_ref(py))
            .collect()
    }

    #[getter]
    fn table_cells(&self, py: Python<'_>) -> Vec<Py<NativeIRTableCellV2>> {
        self.table_cells
            .iter()
            .map(|value| value.clone_ref(py))
            .collect()
    }

    #[getter]
    fn lists(&self, py: Python<'_>) -> Vec<Py<NativeIRListV2>> {
        self.lists.iter().map(|value| value.clone_ref(py)).collect()
    }

    #[getter]
    fn list_items(&self, py: Python<'_>) -> Vec<Py<NativeIRListItemV2>> {
        self.list_items
            .iter()
            .map(|value| value.clone_ref(py))
            .collect()
    }

    #[getter]
    fn math(&self, py: Python<'_>) -> Vec<Py<NativeIRMathV2>> {
        self.math.iter().map(|value| value.clone_ref(py)).collect()
    }

    #[getter]
    fn element_count(&self) -> usize {
        self.elements.len()
    }

    #[getter]
    fn text_run_count(&self) -> usize {
        self.text_runs.len()
    }

    #[getter]
    fn table_count(&self) -> usize {
        self.tables.len()
    }

    #[getter]
    fn table_cell_count(&self) -> usize {
        self.table_cells.len()
    }

    #[getter]
    fn list_count(&self) -> usize {
        self.lists.len()
    }

    #[getter]
    fn list_item_count(&self) -> usize {
        self.list_items.len()
    }

    #[getter]
    fn math_count(&self) -> usize {
        self.math.len()
    }

    #[pyo3(signature = (selected_ids=None))]
    fn reconstruct(&self, selected_ids: Option<Vec<String>>) -> NativeIRSerializationV2 {
        serialize_graph(
            &self.graph,
            selected_ids,
            self.source_complete,
            self.truncated,
            &self.truncation_reasons,
        )
    }
}

#[derive(Debug)]
struct BuildResult {
    graph: Graph,
    source: String,
    input_bytes: usize,
    parsed_bytes: usize,
    node_count: usize,
    parse_error_count: usize,
    mapped_element_count: usize,
    implicit_element_count: usize,
    unmapped_explicit_element_count: usize,
    stored_text_bytes: usize,
    input_truncated: bool,
    nodes_truncated: bool,
    depth_truncated: bool,
    elements_truncated: bool,
    text_runs_truncated: bool,
    text_truncated_runs: usize,
    table_grid_truncated: bool,
    math_truncated_nodes: usize,
    truncation_reasons: Vec<String>,
    limits: LimitsV2,
}

impl BuildResult {
    fn is_truncated(&self) -> bool {
        self.input_truncated
            || self.nodes_truncated
            || self.depth_truncated
            || self.elements_truncated
            || self.text_runs_truncated
            || self.text_truncated_runs > 0
            || self.table_grid_truncated
            || self.math_truncated_nodes > 0
    }

    fn into_python(self, py: Python<'_>) -> PyResult<NativeDocumentIRV2> {
        let truncated = self.is_truncated();
        let source_mapping_complete = self.unmapped_explicit_element_count == 0;
        let root_ids = self
            .graph
            .roots
            .iter()
            .map(|event| event_id(&self.graph, *event).to_owned())
            .collect();
        let elements = self
            .graph
            .elements
            .iter()
            .map(|value| Py::new(py, value.exposed.clone()))
            .collect::<PyResult<Vec<_>>>()?;
        let text_runs = self
            .graph
            .texts
            .iter()
            .map(|value| Py::new(py, value.exposed.clone()))
            .collect::<PyResult<Vec<_>>>()?;
        let tables = self
            .graph
            .tables
            .iter()
            .cloned()
            .map(|value| Py::new(py, value))
            .collect::<PyResult<Vec<_>>>()?;
        let table_cells = self
            .graph
            .cells
            .iter()
            .cloned()
            .map(|value| Py::new(py, value))
            .collect::<PyResult<Vec<_>>>()?;
        let lists = self
            .graph
            .lists
            .iter()
            .cloned()
            .map(|value| Py::new(py, value))
            .collect::<PyResult<Vec<_>>>()?;
        let list_items = self
            .graph
            .list_items
            .iter()
            .cloned()
            .map(|value| Py::new(py, value))
            .collect::<PyResult<Vec<_>>>()?;
        let math = self
            .graph
            .maths
            .iter()
            .cloned()
            .map(|value| Py::new(py, value))
            .collect::<PyResult<Vec<_>>>()?;
        let event_count = self.graph.elements.len() + self.graph.texts.len();

        Ok(NativeDocumentIRV2 {
            elements,
            text_runs,
            tables,
            table_cells,
            lists,
            list_items,
            math,
            graph: self.graph,
            schema_version: SCHEMA_VERSION,
            serialization_contract: SERIALIZATION_CONTRACT,
            source: self.source,
            input_bytes: self.input_bytes,
            parsed_bytes: self.parsed_bytes,
            source_complete: !self.input_truncated,
            node_count: self.node_count,
            parse_error_count: self.parse_error_count,
            event_count,
            mapped_element_count: self.mapped_element_count,
            implicit_element_count: self.implicit_element_count,
            unmapped_explicit_element_count: self.unmapped_explicit_element_count,
            source_mapping_complete,
            stored_text_bytes: self.stored_text_bytes,
            input_truncated: self.input_truncated,
            nodes_truncated: self.nodes_truncated,
            depth_truncated: self.depth_truncated,
            elements_truncated: self.elements_truncated,
            text_runs_truncated: self.text_runs_truncated,
            text_truncated_runs: self.text_truncated_runs,
            table_grid_truncated: self.table_grid_truncated,
            math_truncated_nodes: self.math_truncated_nodes,
            truncated,
            truncation_reasons: self.truncation_reasons,
            root_ids,
            max_input_bytes: self.limits.max_input_bytes,
            max_nodes: self.limits.max_nodes,
            max_elements: self.limits.max_elements,
            max_text_runs: self.limits.max_text_runs,
            max_depth: self.limits.max_depth,
            max_text_run_bytes: self.limits.max_text_run_bytes,
            max_total_text_bytes: self.limits.max_total_text_bytes,
            max_math_bytes: self.limits.max_math_bytes,
            max_table_columns: self.limits.max_table_columns,
        })
    }
}

#[allow(clippy::too_many_arguments)]
#[pyfunction]
#[pyo3(signature = (
    html,
    max_input_bytes=DEFAULT_MAX_INPUT_BYTES,
    max_nodes=DEFAULT_MAX_NODES,
    max_elements=DEFAULT_MAX_ELEMENTS,
    max_text_runs=DEFAULT_MAX_TEXT_RUNS,
    max_depth=DEFAULT_MAX_DEPTH,
    max_text_run_bytes=DEFAULT_MAX_TEXT_RUN_BYTES,
    max_total_text_bytes=DEFAULT_MAX_TOTAL_TEXT_BYTES,
    max_math_bytes=DEFAULT_MAX_MATH_BYTES,
    max_table_columns=DEFAULT_MAX_TABLE_COLUMNS,
))]
pub(crate) fn extract_document_ir_v2_native(
    py: Python<'_>,
    html: &str,
    max_input_bytes: usize,
    max_nodes: usize,
    max_elements: usize,
    max_text_runs: usize,
    max_depth: usize,
    max_text_run_bytes: usize,
    max_total_text_bytes: usize,
    max_math_bytes: usize,
    max_table_columns: usize,
) -> PyResult<NativeDocumentIRV2> {
    let limits = LimitsV2::validated(
        max_input_bytes,
        max_nodes,
        max_elements,
        max_text_runs,
        max_depth,
        max_text_run_bytes,
        max_total_text_bytes,
        max_math_bytes,
        max_table_columns,
    )?;
    let input_bytes = html.len();
    let (source, input_truncated) = bounded_utf8_prefix(html, limits.max_input_bytes);
    let source = source.to_owned();
    let result = py.detach(move || build_ir_v2(source, input_bytes, input_truncated, limits));
    result.into_python(py)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<NativeIRElementV2>()?;
    module.add_class::<NativeIRTextRunV2>()?;
    module.add_class::<NativeIRTableV2>()?;
    module.add_class::<NativeIRTableCellV2>()?;
    module.add_class::<NativeIRListV2>()?;
    module.add_class::<NativeIRListItemV2>()?;
    module.add_class::<NativeIRMathV2>()?;
    module.add_class::<NativeIRSerializationV2>()?;
    module.add_class::<NativeDocumentIRV2>()?;
    module.add_function(wrap_pyfunction!(extract_document_ir_v2_native, module)?)?;
    selection_certificate_v0::register(module)?;
    source_text_mapper_v2::register(module)?;
    Ok(())
}

#[derive(Clone, Debug, Default)]
struct WalkContext {
    depth: usize,
    path: String,
    retained_parent: Option<usize>,
    hidden: bool,
    preserve_whitespace: bool,
    nearest_table: Option<usize>,
    nearest_row: Option<usize>,
    nearest_row_group: Option<usize>,
    nearest_cell: Option<usize>,
    nearest_list: Option<usize>,
    nearest_list_item: Option<usize>,
    nearest_math: Option<usize>,
}

fn build_ir_v2(
    source: String,
    input_bytes: usize,
    input_truncated: bool,
    limits: LimitsV2,
) -> BuildResult {
    let parsed_bytes = source.len();
    let source_elements = scan_source_elements(&source);
    let document = Document::from(source.clone());
    let parse_error_count = document.errors.borrow().len();
    let mut graph = Graph::default();
    let mut contexts: HashMap<NodeId, WalkContext> =
        HashMap::with_capacity(limits.max_nodes.min(8_192));
    let mut element_sibling_counts: HashMap<NodeId, HashMap<String, usize>> = HashMap::new();
    let mut text_sibling_counts: HashMap<NodeId, usize> = HashMap::new();
    let mut seen_dom_elements = Vec::new();
    let mut used_ids = HashSet::new();
    let mut node_count = 0;
    let mut event_order = 0;
    let mut stored_text_bytes = 0;
    let mut nodes_truncated = false;
    let mut depth_truncated = false;
    let mut elements_truncated = false;
    let mut text_runs_truncated = false;
    let mut text_truncated_runs = 0;

    for node in document.root().descendants_it() {
        node_count += 1;
        if node_count > limits.max_nodes {
            node_count = limits.max_nodes;
            nodes_truncated = true;
            break;
        }
        let parent_id = node.parent().map(|parent| parent.id);
        let parent_context = parent_id
            .and_then(|id| contexts.get(&id))
            .cloned()
            .unwrap_or_default();

        if node.is_element() {
            let tag = node_tag(&node);
            seen_dom_elements.push((node.id, tag.clone()));
            let sibling_index = parent_id
                .map(|id| {
                    let counts = element_sibling_counts.entry(id).or_default();
                    let count = counts.entry(tag.clone()).or_default();
                    *count += 1;
                    *count
                })
                .unwrap_or(1);
            let path = child_path(&parent_context.path, &tag, sibling_index);
            let depth = parent_context.depth.saturating_add(1);
            let excluded = parent_context.hidden || should_exclude_v2(&node, &tag);
            let too_deep = depth > limits.max_depth;
            if too_deep {
                depth_truncated = true;
            }
            let hidden = excluded || too_deep;
            let mut context = WalkContext {
                depth,
                path: path.clone(),
                retained_parent: parent_context.retained_parent,
                hidden,
                preserve_whitespace: parent_context.preserve_whitespace
                    || matches!(tag.as_str(), "pre" | "code" | "textarea"),
                nearest_table: parent_context.nearest_table,
                nearest_row: parent_context.nearest_row,
                nearest_row_group: parent_context.nearest_row_group,
                nearest_cell: parent_context.nearest_cell,
                nearest_list: parent_context.nearest_list,
                nearest_list_item: parent_context.nearest_list_item,
                nearest_math: parent_context.nearest_math,
            };
            if !hidden {
                if graph.elements.len() >= limits.max_elements {
                    elements_truncated = true;
                    break;
                }
                let element_index = graph.elements.len();
                let id = stable_id("node", &path, &mut used_ids);
                let attrs = retained_attributes(&node);
                let parent_id = context
                    .retained_parent
                    .map(|index| graph.elements[index].exposed.id.clone());
                let exposed = NativeIRElementV2 {
                    id,
                    order: event_order,
                    parent_id,
                    child_ids: Vec::new(),
                    text_run_ids: Vec::new(),
                    tag: tag.clone(),
                    role: role_for_tag_v2(&tag, attrs.get("role").map(String::as_str)).to_owned(),
                    path,
                    depth,
                    block: is_block_tag(&tag),
                    preserve_whitespace: context.preserve_whitespace,
                    implicit: true,
                    source_start: None,
                    source_start_tag_end: None,
                    source_end: None,
                    source_span_reliable: false,
                    heading_level: heading_level(&tag),
                    href: attrs.get("href").cloned(),
                    src: attrs.get("src").cloned(),
                    alt: attrs
                        .get("alt")
                        .or_else(|| attrs.get("aria-label"))
                        .cloned(),
                    language: language_for(&tag, &attrs),
                };
                let record = ElementRecord {
                    exposed,
                    raw_node_id: node.id,
                    parent_index: context.retained_parent,
                    children: Vec::new(),
                    attrs,
                    nearest_table: context.nearest_table,
                    nearest_row: context.nearest_row,
                    nearest_row_group: context.nearest_row_group,
                    nearest_list: context.nearest_list,
                    nearest_math: context.nearest_math,
                };
                graph.elements.push(record);
                let event = EventRef::Element(element_index);
                if let Some(parent) = context.retained_parent {
                    graph.elements[parent].children.push(event);
                } else {
                    graph.roots.push(event);
                }
                event_order += 1;
                context.retained_parent = Some(element_index);
                if tag == "table" {
                    context.nearest_table = Some(element_index);
                }
                if tag == "tr" {
                    context.nearest_row = Some(element_index);
                }
                if matches!(tag.as_str(), "thead" | "tbody" | "tfoot") {
                    context.nearest_row_group = Some(element_index);
                }
                if matches!(tag.as_str(), "td" | "th") {
                    context.nearest_cell = Some(element_index);
                }
                if matches!(tag.as_str(), "ul" | "ol" | "dl") {
                    context.nearest_list = Some(element_index);
                }
                if matches!(tag.as_str(), "li" | "dt" | "dd") {
                    context.nearest_list_item = Some(element_index);
                }
                if is_math_element(&tag, &graph.elements[element_index].attrs) {
                    context.nearest_math = Some(element_index);
                }
            }
            contexts.insert(node.id, context);
            continue;
        }

        if node.is_text() {
            let sibling_index = parent_id
                .map(|id| {
                    let count = text_sibling_counts.entry(id).or_default();
                    *count += 1;
                    *count
                })
                .unwrap_or(1);
            if parent_context.hidden {
                contexts.insert(node.id, parent_context);
                continue;
            }
            let Some(parent_index) = parent_context.retained_parent else {
                contexts.insert(node.id, parent_context);
                continue;
            };
            if graph.texts.len() >= limits.max_text_runs {
                text_runs_truncated = true;
                break;
            }
            let text = text_contents(&node);
            if text.is_empty() {
                contexts.insert(node.id, parent_context);
                continue;
            }
            let original_bytes = text.len();
            let remaining = limits
                .max_total_text_bytes
                .saturating_sub(stored_text_bytes);
            let budget = limits.max_text_run_bytes.min(remaining);
            let (text, truncated) = truncate_utf8_owned(text, budget);
            let stored_bytes = text.len();
            if truncated {
                text_truncated_runs += 1;
            }
            stored_text_bytes += stored_bytes;
            let path = format!("{}/#text[{sibling_index}]", parent_context.path);
            let id = stable_id("text", &path, &mut used_ids);
            let parent_element_id = graph.elements[parent_index].exposed.id.clone();
            let text_index = graph.texts.len();
            let exposed = NativeIRTextRunV2 {
                id,
                order: event_order,
                parent_id: parent_element_id,
                path,
                text,
                preserve_whitespace: parent_context.preserve_whitespace,
                original_bytes,
                stored_bytes,
                truncated,
                source_start: None,
                source_end: None,
                source_span_reliable: false,
            };
            graph.texts.push(TextRecord {
                exposed,
                parent_index,
                nearest_cell: parent_context.nearest_cell,
                nearest_list_item: parent_context.nearest_list_item,
            });
            graph.elements[parent_index]
                .children
                .push(EventRef::Text(text_index));
            event_order += 1;
            if truncated && stored_text_bytes >= limits.max_total_text_bytes {
                break;
            }
        }
        contexts.insert(node.id, parent_context);
    }

    apply_source_mapping(&source, &source_elements, &seen_dom_elements, &mut graph);
    populate_child_ids(&mut graph);
    let table_grid_truncated = build_tables(&mut graph, limits.max_table_columns);
    build_lists(&mut graph);
    let math_truncated_nodes = build_math(&source, &document, &mut graph, limits.max_math_bytes);

    let mapped_element_count = graph
        .elements
        .iter()
        .filter(|element| element.exposed.source_start.is_some())
        .count();
    let implicit_element_count = graph
        .elements
        .iter()
        .filter(|element| element.exposed.implicit)
        .count();
    let unmapped_explicit_element_count = graph
        .elements
        .iter()
        .filter(|element| !element.exposed.implicit && element.exposed.source_start.is_none())
        .count();
    let mut truncation_reasons = Vec::new();
    if input_truncated {
        truncation_reasons.push("input_bytes".to_owned());
    }
    if nodes_truncated {
        truncation_reasons.push("node_count".to_owned());
    }
    if depth_truncated {
        truncation_reasons.push("dom_depth".to_owned());
    }
    if elements_truncated {
        truncation_reasons.push("element_count".to_owned());
    }
    if text_runs_truncated {
        truncation_reasons.push("text_run_count".to_owned());
    }
    if text_truncated_runs > 0 {
        truncation_reasons.push("text_bytes".to_owned());
    }
    if table_grid_truncated {
        truncation_reasons.push("table_columns".to_owned());
    }
    if math_truncated_nodes > 0 {
        truncation_reasons.push("math_markup".to_owned());
    }

    BuildResult {
        graph,
        source,
        input_bytes,
        parsed_bytes,
        node_count,
        parse_error_count,
        mapped_element_count,
        implicit_element_count,
        unmapped_explicit_element_count,
        stored_text_bytes,
        input_truncated,
        nodes_truncated,
        depth_truncated,
        elements_truncated,
        text_runs_truncated,
        text_truncated_runs,
        table_grid_truncated,
        math_truncated_nodes,
        truncation_reasons,
        limits,
    }
}

fn node_tag(node: &NodeRef<'_>) -> String {
    node.node_name()
        .map(|value| value.to_string().to_ascii_lowercase())
        .unwrap_or_default()
}

fn text_contents(node: &NodeRef<'_>) -> String {
    node.query_or(String::new(), |value| match &value.data {
        NodeData::Text { contents } => contents.to_string(),
        _ => String::new(),
    })
}

fn child_path(parent: &str, tag: &str, index: usize) -> String {
    if parent.is_empty() {
        format!("/{tag}[{index}]")
    } else {
        format!("{parent}/{tag}[{index}]")
    }
}

fn stable_id(prefix: &str, path: &str, used: &mut HashSet<String>) -> String {
    let mut hash = 0xcbf29ce484222325_u64;
    for byte in path.as_bytes() {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x100000001b3);
    }
    let base = format!("{prefix}-{hash:016x}");
    let mut value = base.clone();
    let mut collision = 1;
    while !used.insert(value.clone()) {
        value = format!("{base}-{collision}");
        collision += 1;
    }
    value
}

fn should_exclude_v2(node: &NodeRef<'_>, tag: &str) -> bool {
    if matches!(
        tag,
        "head"
            | "style"
            | "template"
            | "noscript"
            | "meta"
            | "link"
            | "base"
            | "iframe"
            | "object"
            | "embed"
            | "source"
            | "track"
            | "canvas"
    ) {
        return true;
    }
    if tag == "script" && !is_tex_script(node) {
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
    node.attr("style").is_some_and(|style| {
        let compact: String = style
            .chars()
            .take(2_048)
            .filter(|character| !character.is_ascii_whitespace())
            .flat_map(char::to_lowercase)
            .collect();
        compact.contains("display:none")
            || compact.contains("visibility:hidden")
            || compact.contains("content-visibility:hidden")
    })
}

fn is_tex_script(node: &NodeRef<'_>) -> bool {
    node.attr("type").is_some_and(|value| {
        let value = value.to_ascii_lowercase();
        value.starts_with("math/tex") || value.starts_with("application/x-tex")
    })
}

fn retained_attributes(node: &NodeRef<'_>) -> HashMap<String, String> {
    const RETAINED: &[&str] = &[
        "alt",
        "alttext",
        "aria-label",
        "class",
        "colspan",
        "data-latex",
        "data-tex",
        "display",
        "encoding",
        "href",
        "id",
        "lang",
        "latex",
        "reversed",
        "role",
        "rowspan",
        "scope",
        "src",
        "start",
        "type",
        "value",
        "xml:lang",
    ];
    RETAINED
        .iter()
        .filter_map(|name| {
            node.attr(name).map(|value| {
                let (bounded, _) = bounded_utf8_prefix(&value, 8 * 1024);
                ((*name).to_owned(), bounded.to_owned())
            })
        })
        .collect()
}

fn role_for_tag_v2<'a>(tag: &str, explicit: Option<&'a str>) -> &'a str {
    if let Some(role) = explicit {
        return role;
    }
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
        "caption" | "figcaption" => "caption",
        "ul" | "ol" => "list",
        "dl" => "description-list",
        "li" => "listitem",
        "dt" => "term",
        "dd" => "definition",
        "figure" => "figure",
        "img" => "image",
        "math" => "math",
        "a" => "link",
        "button" => "button",
        "input" => "input",
        _ => "generic",
    }
}

fn is_block_tag(tag: &str) -> bool {
    matches!(
        tag,
        "html"
            | "body"
            | "main"
            | "article"
            | "section"
            | "nav"
            | "aside"
            | "header"
            | "footer"
            | "div"
            | "h1"
            | "h2"
            | "h3"
            | "h4"
            | "h5"
            | "h6"
            | "p"
            | "blockquote"
            | "pre"
            | "table"
            | "thead"
            | "tbody"
            | "tfoot"
            | "tr"
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
            | "hr"
    )
}

fn heading_level(tag: &str) -> Option<u8> {
    tag.strip_prefix('h')
        .and_then(|value| value.parse::<u8>().ok())
        .filter(|value| (1..=6).contains(value))
}

fn language_for(tag: &str, attrs: &HashMap<String, String>) -> Option<String> {
    attrs
        .get("lang")
        .or_else(|| attrs.get("xml:lang"))
        .cloned()
        .or_else(|| {
            matches!(tag, "code" | "pre")
                .then(|| attrs.get("class"))
                .flatten()
                .and_then(|classes| {
                    classes.split_ascii_whitespace().find_map(|class| {
                        class
                            .strip_prefix("language-")
                            .or_else(|| class.strip_prefix("lang-"))
                            .map(ToOwned::to_owned)
                    })
                })
        })
}

fn is_math_element(tag: &str, attrs: &HashMap<String, String>) -> bool {
    tag == "math"
        || (tag == "script"
            && attrs.get("type").is_some_and(|value| {
                let value = value.to_ascii_lowercase();
                value.starts_with("math/tex") || value.starts_with("application/x-tex")
            }))
}

#[derive(Clone, Debug)]
struct SourceElement {
    tag: String,
    start: usize,
    start_tag_end: usize,
    end_tag_start: Option<usize>,
    end: Option<usize>,
}

fn scan_source_elements(source: &str) -> Vec<SourceElement> {
    let bytes = source.as_bytes();
    let mut records: Vec<SourceElement> = Vec::new();
    let mut stack: Vec<usize> = Vec::new();
    let mut index = 0;
    while index < bytes.len() {
        let Some(relative) = bytes[index..].iter().position(|byte| *byte == b'<') else {
            break;
        };
        index += relative;
        if starts_ascii_case_insensitive(bytes, index, b"<!--") {
            index = find_bytes(bytes, index + 4, b"-->")
                .map(|value| value + 3)
                .unwrap_or(bytes.len());
            continue;
        }
        if index + 1 >= bytes.len() {
            break;
        }
        if matches!(bytes[index + 1], b'!' | b'?') {
            index = find_tag_end(bytes, index + 2).unwrap_or(bytes.len());
            continue;
        }
        let closing = bytes[index + 1] == b'/';
        let name_start = index + if closing { 2 } else { 1 };
        let mut name_end = name_start;
        while name_end < bytes.len() && is_tag_name_byte(bytes[name_end]) {
            name_end += 1;
        }
        if name_end == name_start {
            index += 1;
            continue;
        }
        let tag = source[name_start..name_end].to_ascii_lowercase();
        let Some(tag_end) = find_tag_end(bytes, name_end) else {
            break;
        };
        if closing {
            if let Some(position) = stack
                .iter()
                .rposition(|record_index| records[*record_index].tag == tag)
            {
                while stack.len() > position + 1 {
                    stack.pop();
                }
                if let Some(record_index) = stack.pop() {
                    records[record_index].end_tag_start = Some(index);
                    records[record_index].end = Some(tag_end);
                }
            }
            index = tag_end;
            continue;
        }

        let self_closing = source[index..tag_end]
            .trim_end_matches('>')
            .trim_end()
            .ends_with('/');
        let void = is_void_tag(&tag);
        let record_index = records.len();
        records.push(SourceElement {
            tag: tag.clone(),
            start: index,
            start_tag_end: tag_end,
            end_tag_start: (self_closing || void).then_some(tag_end),
            end: (self_closing || void).then_some(tag_end),
        });
        if self_closing || void {
            index = tag_end;
            continue;
        }
        if is_raw_text_tag(&tag) {
            if let Some((close_start, close_end)) = find_raw_text_close(source, tag_end, &tag) {
                records[record_index].end_tag_start = Some(close_start);
                records[record_index].end = Some(close_end);
                index = close_end;
            } else {
                index = bytes.len();
            }
            continue;
        }
        stack.push(record_index);
        index = tag_end;
    }
    records
}

fn is_tag_name_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || matches!(byte, b':' | b'-' | b'_')
}

fn find_tag_end(bytes: &[u8], mut index: usize) -> Option<usize> {
    let mut quote = None;
    while index < bytes.len() {
        let byte = bytes[index];
        match quote {
            Some(active) if byte == active => quote = None,
            Some(_) => {}
            None if matches!(byte, b'\'' | b'"') => quote = Some(byte),
            None if byte == b'>' => return Some(index + 1),
            None => {}
        }
        index += 1;
    }
    None
}

fn starts_ascii_case_insensitive(bytes: &[u8], start: usize, needle: &[u8]) -> bool {
    bytes
        .get(start..start.saturating_add(needle.len()))
        .is_some_and(|value| value.eq_ignore_ascii_case(needle))
}

fn find_bytes(bytes: &[u8], start: usize, needle: &[u8]) -> Option<usize> {
    bytes
        .get(start..)?
        .windows(needle.len())
        .position(|window| window == needle)
        .map(|relative| start + relative)
}

fn find_raw_text_close(source: &str, mut start: usize, tag: &str) -> Option<(usize, usize)> {
    let bytes = source.as_bytes();
    let needle = format!("</{tag}");
    while start < bytes.len() {
        let relative = bytes[start..].iter().position(|byte| *byte == b'<')?;
        let candidate = start + relative;
        if starts_ascii_case_insensitive(bytes, candidate, needle.as_bytes()) {
            let after_name = candidate + needle.len();
            if bytes
                .get(after_name)
                .is_none_or(|byte| byte.is_ascii_whitespace() || *byte == b'>')
            {
                return find_tag_end(bytes, after_name).map(|end| (candidate, end));
            }
        }
        start = candidate + 1;
    }
    None
}

fn is_raw_text_tag(tag: &str) -> bool {
    matches!(
        tag,
        "script"
            | "style"
            | "textarea"
            | "title"
            | "xmp"
            | "iframe"
            | "noembed"
            | "noframes"
            | "plaintext"
    )
}

fn is_void_tag(tag: &str) -> bool {
    matches!(
        tag,
        "area"
            | "base"
            | "br"
            | "col"
            | "embed"
            | "hr"
            | "img"
            | "input"
            | "link"
            | "meta"
            | "param"
            | "source"
            | "track"
            | "wbr"
    )
}

fn apply_source_mapping(
    source: &str,
    source_elements: &[SourceElement],
    seen_dom_elements: &[(NodeId, String)],
    graph: &mut Graph,
) {
    let mapping = align_source_elements(source_elements, seen_dom_elements);
    let source_tag_counts =
        source_elements
            .iter()
            .fold(HashMap::<&str, usize>::new(), |mut counts, element| {
                *counts.entry(element.tag.as_str()).or_default() += 1;
                counts
            });
    let mut dom_tag_ordinals = HashMap::<NodeId, usize>::new();
    let mut seen_tag_counts = HashMap::<&str, usize>::new();
    for (node_id, tag) in seen_dom_elements {
        let ordinal = seen_tag_counts.entry(tag.as_str()).or_default();
        *ordinal += 1;
        dom_tag_ordinals.insert(*node_id, *ordinal);
    }
    for element in &mut graph.elements {
        let source_count = source_tag_counts
            .get(element.exposed.tag.as_str())
            .copied()
            .unwrap_or_default();
        let dom_ordinal = dom_tag_ordinals
            .get(&element.raw_node_id)
            .copied()
            .unwrap_or_default();
        element.exposed.implicit = dom_ordinal > source_count;
        if let Some(record_index) = mapping.get(&element.raw_node_id).copied() {
            let record = &source_elements[record_index];
            element.exposed.implicit = false;
            element.exposed.source_start = Some(record.start);
            element.exposed.source_start_tag_end = Some(record.start_tag_end);
            element.exposed.source_end = record.end;
            element.exposed.source_span_reliable = record.end.is_some();
        }
    }

    let content_end_by_start = source_elements
        .iter()
        .map(|record| (record.start, record.end_tag_start))
        .collect::<HashMap<_, _>>();
    for text in &mut graph.texts {
        let parent = &graph.elements[text.parent_index].exposed;
        let (Some(content_start), Some(element_end)) =
            (parent.source_start_tag_end, parent.source_end)
        else {
            continue;
        };
        let content_end = parent
            .source_start
            .and_then(|start| content_end_by_start.get(&start).copied().flatten())
            .unwrap_or(element_end);
        if content_start > content_end || content_end > source.len() || text.exposed.text.is_empty()
        {
            continue;
        }
        let haystack = &source[content_start..content_end];
        let mut matches = haystack.match_indices(&text.exposed.text);
        let Some((relative, _)) = matches.next() else {
            continue;
        };
        if matches.next().is_some() {
            continue;
        }
        let start = content_start + relative;
        text.exposed.source_start = Some(start);
        text.exposed.source_end = Some(start + text.exposed.text.len());
        text.exposed.source_span_reliable = !text.exposed.truncated;
    }
}

fn align_source_elements(
    source_elements: &[SourceElement],
    seen_dom_elements: &[(NodeId, String)],
) -> HashMap<NodeId, usize> {
    let mut mapping = HashMap::new();
    let mut source_index = 0;
    let mut complete = true;
    for (node_id, tag) in seen_dom_elements {
        if source_elements
            .get(source_index)
            .is_some_and(|record| record.tag == *tag)
        {
            mapping.insert(*node_id, source_index);
            source_index += 1;
        } else if !is_parser_insertable(tag) {
            complete = false;
            break;
        }
    }
    if complete && source_index == source_elements.len() {
        return mapping;
    }
    // A repaired or foster-parented DOM is deliberately left unmapped. Paths
    // remain stable, but claiming byte spans in that case would be misleading.
    HashMap::new()
}

fn is_parser_insertable(tag: &str) -> bool {
    matches!(tag, "html" | "head" | "body" | "tbody")
}

fn populate_child_ids(graph: &mut Graph) {
    for index in 0..graph.elements.len() {
        let children = graph.elements[index].children.clone();
        let child_ids = children
            .iter()
            .map(|event| event_id(graph, *event).to_owned())
            .collect::<Vec<_>>();
        let text_run_ids = children
            .iter()
            .filter_map(|event| match event {
                EventRef::Text(text_index) => Some(graph.texts[*text_index].exposed.id.clone()),
                EventRef::Element(_) => None,
            })
            .collect();
        graph.elements[index].exposed.child_ids = child_ids;
        graph.elements[index].exposed.text_run_ids = text_run_ids;
    }
    graph.element_by_id = graph
        .elements
        .iter()
        .enumerate()
        .map(|(index, element)| (element.exposed.id.clone(), index))
        .collect();
}

fn event_id(graph: &Graph, event: EventRef) -> &str {
    match event {
        EventRef::Element(index) => &graph.elements[index].exposed.id,
        EventRef::Text(index) => &graph.texts[index].exposed.id,
    }
}

fn build_tables(graph: &mut Graph, max_columns: usize) -> bool {
    let mut table_indices = Vec::new();
    let mut rows_by_table: HashMap<usize, Vec<usize>> = HashMap::new();
    let mut cells_by_row: HashMap<usize, Vec<usize>> = HashMap::new();
    for (index, element) in graph.elements.iter().enumerate() {
        match element.exposed.tag.as_str() {
            "table" => table_indices.push(index),
            "tr" => {
                if let Some(table_index) = element.nearest_table {
                    rows_by_table.entry(table_index).or_default().push(index);
                }
            }
            "td" | "th" => {
                if let Some(row_index) = element.nearest_row {
                    cells_by_row.entry(row_index).or_default().push(index);
                }
            }
            _ => {}
        }
    }
    let mut text_ids_by_cell: HashMap<usize, Vec<String>> = HashMap::new();
    for text in &graph.texts {
        if let Some(cell_index) = text.nearest_cell {
            text_ids_by_cell
                .entry(cell_index)
                .or_default()
                .push(text.exposed.id.clone());
        }
    }
    let mut any_truncated = false;
    for table_index in table_indices {
        let table_id = format!(
            "table-{:016x}",
            stable_hash(&graph.elements[table_index].exposed.path)
        );
        let rows = rows_by_table.remove(&table_index).unwrap_or_default();
        let mut occupied_until: Vec<usize> = Vec::new();
        let mut table_cell_ids = Vec::new();
        let mut column_count = 0;
        let mut table_complete = true;
        for (row_index, row_element_index) in rows.iter().copied().enumerate() {
            let cells = cells_by_row.remove(&row_element_index).unwrap_or_default();
            let row_group_index = graph.elements[row_element_index].nearest_row_group;
            let row_group = row_group_index
                .map(|index| graph.elements[index].exposed.tag.clone())
                .unwrap_or_else(|| "table".to_owned());
            let remaining_group_rows = rows[row_index..]
                .iter()
                .take_while(|candidate| {
                    graph.elements[**candidate].nearest_row_group == row_group_index
                })
                .count()
                .max(1);
            let mut column = 0;
            for cell_index in cells {
                while column < max_columns
                    && occupied_until
                        .get(column)
                        .is_some_and(|until| *until > row_index)
                {
                    column += 1;
                }
                let requested_column_span =
                    positive_span(graph.elements[cell_index].attrs.get("colspan"), 1);
                let raw_row_span =
                    nonnegative_span(graph.elements[cell_index].attrs.get("rowspan"), 1);
                let row_span = if raw_row_span == 0 {
                    remaining_group_rows
                } else {
                    raw_row_span
                };
                let column_span = requested_column_span.min(max_columns);
                while column < max_columns
                    && !range_is_free(&occupied_until, column, column_span, row_index)
                {
                    column += 1;
                }
                let cell_complete = column < max_columns
                    && requested_column_span <= max_columns
                    && column.saturating_add(column_span) <= max_columns;
                if !cell_complete {
                    table_complete = false;
                    any_truncated = true;
                    column = column.min(max_columns.saturating_sub(1));
                }
                let usable_span = column_span.min(max_columns.saturating_sub(column)).max(1);
                if occupied_until.len() < column + usable_span {
                    occupied_until.resize(column + usable_span, 0);
                }
                for slot in &mut occupied_until[column..column + usable_span] {
                    *slot = (*slot).max(row_index.saturating_add(row_span));
                }
                column_count = column_count.max(column.saturating_add(usable_span));
                let node_id = graph.elements[cell_index].exposed.id.clone();
                let id = format!(
                    "cell-{:016x}",
                    stable_hash(&graph.elements[cell_index].exposed.path)
                );
                let text_run_ids = text_ids_by_cell.remove(&cell_index).unwrap_or_default();
                table_cell_ids.push(id.clone());
                graph.cells.push(NativeIRTableCellV2 {
                    id,
                    node_id,
                    table_id: table_id.clone(),
                    order: graph.elements[cell_index].exposed.order,
                    row_index,
                    column_index: column,
                    row_span,
                    column_span: usable_span,
                    row_group: row_group.clone(),
                    header: graph.elements[cell_index].exposed.tag == "th",
                    scope: graph.elements[cell_index]
                        .attrs
                        .get("scope")
                        .cloned()
                        .unwrap_or_default(),
                    text_run_ids,
                    grid_complete: cell_complete,
                });
                column = column.saturating_add(usable_span);
            }
        }
        graph.tables.push(NativeIRTableV2 {
            id: table_id,
            node_id: graph.elements[table_index].exposed.id.clone(),
            order: graph.elements[table_index].exposed.order,
            row_count: rows.len(),
            column_count,
            cell_ids: table_cell_ids,
            grid_complete: table_complete,
        });
    }
    any_truncated
}

fn positive_span(value: Option<&String>, default: usize) -> usize {
    value
        .and_then(|value| value.trim().parse::<usize>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(default)
}

fn nonnegative_span(value: Option<&String>, default: usize) -> usize {
    value
        .and_then(|value| value.trim().parse::<usize>().ok())
        .unwrap_or(default)
}

fn range_is_free(occupied_until: &[usize], start: usize, span: usize, row: usize) -> bool {
    (start..start.saturating_add(span))
        .all(|column| occupied_until.get(column).is_none_or(|until| *until <= row))
}

fn build_lists(graph: &mut Graph) {
    let mut list_indices = Vec::new();
    let mut items_by_list: HashMap<usize, Vec<usize>> = HashMap::new();
    for (index, element) in graph.elements.iter().enumerate() {
        if matches!(element.exposed.tag.as_str(), "ul" | "ol" | "dl") {
            list_indices.push(index);
        } else if matches!(element.exposed.tag.as_str(), "li" | "dt" | "dd") {
            if let Some(list_index) = element.nearest_list {
                items_by_list.entry(list_index).or_default().push(index);
            }
        }
    }
    let mut text_ids_by_item: HashMap<usize, Vec<String>> = HashMap::new();
    for text in &graph.texts {
        if let Some(item_index) = text.nearest_list_item {
            text_ids_by_item
                .entry(item_index)
                .or_default()
                .push(text.exposed.id.clone());
        }
    }
    for list_index in list_indices {
        let element = &graph.elements[list_index];
        let kind = match element.exposed.tag.as_str() {
            "ol" => "ordered",
            "dl" => "description",
            _ => "unordered",
        };
        let id = format!("list-{:016x}", stable_hash(&element.exposed.path));
        let reversed = element.attrs.contains_key("reversed");
        let item_element_indices = items_by_list
            .remove(&list_index)
            .unwrap_or_default()
            .into_iter()
            .filter(|index| match kind {
                "description" => {
                    matches!(graph.elements[*index].exposed.tag.as_str(), "dt" | "dd")
                }
                _ => graph.elements[*index].exposed.tag == "li",
            })
            .collect::<Vec<_>>();
        let configured_start = element
            .attrs
            .get("start")
            .and_then(|value| value.trim().parse::<i64>().ok());
        let start = if kind == "ordered" {
            Some(configured_start.unwrap_or({
                if reversed {
                    item_element_indices.len() as i64
                } else {
                    1
                }
            }))
        } else {
            None
        };
        let depth = list_depth(graph, list_index);
        let marker_type = element
            .attrs
            .get("type")
            .cloned()
            .unwrap_or_else(|| if kind == "ordered" { "1" } else { "disc" }.to_owned());
        let mut item_ids = Vec::new();
        let mut ordinal = start;
        for (item_index, element_index) in item_element_indices.into_iter().enumerate() {
            let item = &graph.elements[element_index];
            let explicit_value = item
                .attrs
                .get("value")
                .and_then(|value| value.trim().parse::<i64>().ok());
            if explicit_value.is_some() {
                ordinal = explicit_value;
            }
            let item_id = format!("item-{:016x}", stable_hash(&item.exposed.path));
            let text_run_ids = text_ids_by_item.remove(&element_index).unwrap_or_default();
            item_ids.push(item_id.clone());
            graph.list_items.push(NativeIRListItemV2 {
                id: item_id,
                node_id: item.exposed.id.clone(),
                list_id: id.clone(),
                order: item.exposed.order,
                depth,
                index: item_index,
                kind: match item.exposed.tag.as_str() {
                    "dt" => "term",
                    "dd" => "definition",
                    _ => "item",
                }
                .to_owned(),
                ordinal,
                explicit_value,
                text_run_ids,
            });
            if let Some(current) = ordinal.as_mut() {
                *current += if reversed { -1 } else { 1 };
            }
        }
        graph.lists.push(NativeIRListV2 {
            id,
            node_id: element.exposed.id.clone(),
            order: element.exposed.order,
            kind: kind.to_owned(),
            depth,
            start,
            reversed,
            marker_type,
            item_ids,
        });
    }
}

fn list_depth(graph: &Graph, list_index: usize) -> usize {
    let mut depth = 0;
    let mut parent = graph.elements[list_index].parent_index;
    while let Some(index) = parent {
        if matches!(
            graph.elements[index].exposed.tag.as_str(),
            "ul" | "ol" | "dl"
        ) {
            depth += 1;
        }
        parent = graph.elements[index].parent_index;
    }
    depth
}

fn build_math(
    source: &str,
    document: &Document,
    graph: &mut Graph,
    max_math_bytes: usize,
) -> usize {
    let mut math_indices = Vec::new();
    let mut tex_annotations: HashMap<usize, Vec<usize>> = HashMap::new();
    for (index, element) in graph.elements.iter().enumerate() {
        if is_math_element(&element.exposed.tag, &element.attrs) {
            math_indices.push(index);
        }
        if element.exposed.tag == "annotation"
            && element.attrs.get("encoding").is_some_and(|encoding| {
                let encoding = encoding.to_ascii_lowercase();
                encoding.contains("tex") || encoding.contains("latex")
            })
        {
            if let Some(math_index) = element.nearest_math {
                tex_annotations.entry(math_index).or_default().push(index);
            }
        }
    }
    let mut truncated_count = 0;
    let mut stored_math_bytes = 0;
    for math_index in math_indices {
        let element = &graph.elements[math_index];
        let source_slice = element
            .exposed
            .source_start
            .zip(element.exposed.source_end)
            .and_then(|(start, end)| source.get(start..end));
        let fallback = if source_slice.is_none() && element.nearest_math.is_none() {
            NodeRef::new(element.raw_node_id, &document.tree)
                .try_html()
                .map(|value| value.to_string())
                .unwrap_or_default()
        } else {
            String::new()
        };
        let raw_markup = source_slice.unwrap_or(&fallback);
        let remaining = max_math_bytes.saturating_sub(stored_math_bytes);
        let (source_markup, truncated) = bounded_utf8_prefix(raw_markup, remaining);
        let source_markup = source_markup.to_owned();
        stored_math_bytes += source_markup.len();
        if truncated {
            truncated_count += 1;
        }
        let script_tex = (element.exposed.tag == "script").then(|| {
            descendant_text(graph, math_index, true)
                .trim_matches(['\r', '\n'])
                .to_owned()
        });
        let attr_tex = ["data-tex", "data-latex", "latex"]
            .iter()
            .find_map(|name| element.attrs.get(*name).cloned());
        let annotation_tex = tex_annotations
            .get(&math_index)
            .and_then(|annotations| annotations.first())
            .map(|index| descendant_text(graph, *index, true).trim().to_owned())
            .filter(|value| !value.is_empty());
        let tex = script_tex
            .filter(|value| !value.is_empty())
            .or(attr_tex)
            .or(annotation_tex);
        let format = if element.exposed.tag == "math" {
            "mathml"
        } else {
            "tex"
        };
        let display = element
            .attrs
            .get("display")
            .map(|value| value.to_ascii_lowercase())
            .or_else(|| {
                element.attrs.get("type").and_then(|value| {
                    value
                        .to_ascii_lowercase()
                        .contains("mode=display")
                        .then(|| "block".to_owned())
                })
            })
            .unwrap_or_else(|| "inline".to_owned());
        let display = if matches!(display.as_str(), "block" | "display") {
            "block"
        } else {
            "inline"
        };
        graph.maths.push(NativeIRMathV2 {
            id: format!("math-{:016x}", stable_hash(&element.exposed.path)),
            node_id: element.exposed.id.clone(),
            order: element.exposed.order,
            format: format.to_owned(),
            display: display.to_owned(),
            tex,
            mathml: (format == "mathml").then_some(source_markup.clone()),
            source_markup,
            alt_text: element
                .attrs
                .get("alttext")
                .or_else(|| element.attrs.get("aria-label"))
                .cloned(),
            source_backed: source_slice.is_some(),
            truncated,
        });
    }
    truncated_count
}

fn descendant_text(graph: &Graph, element_index: usize, exact: bool) -> String {
    let mut output = String::new();
    collect_text(graph, EventRef::Element(element_index), &mut output);
    if exact {
        output
    } else {
        normalize_visible_text(&output)
    }
}

fn collect_text(graph: &Graph, event: EventRef, output: &mut String) {
    match event {
        EventRef::Text(index) => output.push_str(&graph.texts[index].exposed.text),
        EventRef::Element(index) => {
            for child in &graph.elements[index].children {
                collect_text(graph, *child, output);
            }
        }
    }
}

fn stable_hash(value: &str) -> u64 {
    let mut hash = 0xcbf29ce484222325_u64;
    for byte in value.as_bytes() {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x100000001b3);
    }
    hash
}

fn serialize_graph(
    graph: &Graph,
    selected_ids: Option<Vec<String>>,
    source_complete: bool,
    document_truncated: bool,
    document_truncation_reasons: &[String],
) -> NativeIRSerializationV2 {
    let selection_active = selected_ids.is_some();
    let mut known: HashMap<&str, EventRef> =
        HashMap::with_capacity(graph.elements.len() + graph.texts.len());
    for (index, element) in graph.elements.iter().enumerate() {
        known.insert(&element.exposed.id, EventRef::Element(index));
    }
    for (index, text) in graph.texts.iter().enumerate() {
        known.insert(&text.exposed.id, EventRef::Text(index));
    }
    let mut missing_ids = Vec::new();
    let mut selected_events = HashSet::new();
    let mut normalized_ids = Vec::new();
    if let Some(ids) = selected_ids {
        let mut seen = HashSet::new();
        for id in ids {
            if !seen.insert(id.clone()) {
                continue;
            }
            if let Some(event) = known.get(id.as_str()).copied() {
                selected_events.insert(event);
                normalized_ids.push(id);
            } else {
                missing_ids.push(id);
            }
        }
        normalized_ids.sort_by_key(|id| event_order(graph, known[id.as_str()]));
    }
    let mut included = HashSet::new();
    if selection_active {
        for event in selected_events.iter().copied() {
            mark_event_and_ancestors(graph, event, &mut included);
        }
    }
    let context = RenderContext {
        graph,
        selection_active,
        selected: &selected_events,
        included: &included,
    };
    let mut pieces = Vec::new();
    for root in &graph.roots {
        if let Some(piece) = render_event(&context, *root, false) {
            if !piece.trim().is_empty() {
                pieces.push(piece);
            }
        }
    }
    let markdown = join_blocks(pieces);
    let exact_code_whitespace = graph
        .texts
        .iter()
        .filter(|text| text.exposed.preserve_whitespace)
        .all(|text| !text.exposed.truncated);
    let table_grid_complete = graph.tables.iter().all(|table| table.grid_complete);
    NativeIRSerializationV2 {
        contract_version: SERIALIZATION_CONTRACT,
        markdown,
        selected_ids: normalized_ids,
        missing_ids,
        deterministic: true,
        exact_code_whitespace,
        table_grid_complete,
        source_complete,
        truncated: document_truncated,
        truncation_reasons: document_truncation_reasons.to_vec(),
    }
}

fn event_order(graph: &Graph, event: EventRef) -> usize {
    match event {
        EventRef::Element(index) => graph.elements[index].exposed.order,
        EventRef::Text(index) => graph.texts[index].exposed.order,
    }
}

fn mark_event_and_ancestors(graph: &Graph, event: EventRef, included: &mut HashSet<EventRef>) {
    included.insert(event);
    let mut parent = match event {
        EventRef::Element(index) => graph.elements[index].parent_index,
        EventRef::Text(index) => Some(graph.texts[index].parent_index),
    };
    while let Some(index) = parent {
        included.insert(EventRef::Element(index));
        parent = graph.elements[index].parent_index;
    }
}

struct RenderContext<'a> {
    graph: &'a Graph,
    selection_active: bool,
    selected: &'a HashSet<EventRef>,
    included: &'a HashSet<EventRef>,
}

fn render_event(context: &RenderContext<'_>, event: EventRef, force_full: bool) -> Option<String> {
    let full = force_full || !context.selection_active || context.selected.contains(&event);
    if context.selection_active && !full && !context.included.contains(&event) {
        return None;
    }
    match event {
        EventRef::Text(index) => {
            let run = &context.graph.texts[index].exposed;
            Some(if run.preserve_whitespace {
                run.text.clone()
            } else {
                escape_markdown_text(&normalize_visible_text(&run.text))
            })
        }
        EventRef::Element(index) => render_element(context, index, full),
    }
}

fn render_element(context: &RenderContext<'_>, index: usize, force_full: bool) -> Option<String> {
    let element = &context.graph.elements[index];
    if element.exposed.tag == "table" {
        return Some(render_table(context, index, force_full));
    }
    if element.exposed.tag == "pre" {
        let code = selected_descendant_text(context, index, force_full, true);
        let language = element
            .exposed
            .language
            .clone()
            .or_else(|| descendant_code_language(context.graph, index));
        return (!code.is_empty()).then(|| fenced_code(&code, language.as_deref()));
    }
    if is_math_element(&element.exposed.tag, &element.attrs) {
        return render_math(context.graph, index);
    }
    if element.exposed.tag == "img" {
        let alt = element.exposed.alt.as_deref().unwrap_or_default();
        let src = element.exposed.src.as_deref().unwrap_or_default();
        return Some(format!("![{}]({})", escape_markdown_text(alt), src));
    }
    if element.exposed.tag == "br" {
        return Some("\n".to_owned());
    }
    if element.exposed.tag == "hr" {
        return Some("---".to_owned());
    }
    let content = render_children(context, element, force_full);
    if content.is_empty() && !matches!(element.exposed.tag.as_str(), "input") {
        return None;
    }
    let rendered = match element.exposed.tag.as_str() {
        "h1" | "h2" | "h3" | "h4" | "h5" | "h6" => format!(
            "{} {}",
            "#".repeat(usize::from(element.exposed.heading_level.unwrap_or(1))),
            content.trim()
        ),
        "strong" | "b" => format!("**{}**", content.trim()),
        "em" | "i" => format!("*{}*", content.trim()),
        "s" | "del" | "strike" => format!("~~{}~~", content.trim()),
        "code" => inline_code(&selected_descendant_text(context, index, force_full, true)),
        "a" => {
            let href = element.exposed.href.as_deref().unwrap_or_default();
            if href.is_empty() {
                content
            } else {
                format!("[{}]({href})", content.trim())
            }
        }
        "blockquote" => content
            .lines()
            .map(|line| format!("> {line}"))
            .collect::<Vec<_>>()
            .join("\n"),
        "li" => render_list_item(context.graph, index, &content),
        "dt" => format!("{}\n", content.trim()),
        "dd" => format!(": {}", content.trim()),
        "ul" | "ol" | "dl" => content,
        "p" | "div" | "main" | "article" | "section" | "nav" | "aside" | "header" | "footer"
        | "body" | "html" | "figure" | "figcaption" | "details" | "summary" | "address"
        | "form" => content,
        "input" => element
            .attrs
            .get("value")
            .or_else(|| element.attrs.get("aria-label"))
            .map(|value| escape_markdown_text(value))
            .unwrap_or_default(),
        _ => content,
    };
    Some(rendered)
}

fn descendant_code_language(graph: &Graph, element_index: usize) -> Option<String> {
    let mut pending = graph.elements[element_index]
        .children
        .iter()
        .rev()
        .filter_map(|event| match event {
            EventRef::Element(index) => Some(*index),
            EventRef::Text(_) => None,
        })
        .collect::<Vec<_>>();
    while let Some(index) = pending.pop() {
        let element = &graph.elements[index];
        if element.exposed.tag == "code" && element.exposed.language.is_some() {
            return element.exposed.language.clone();
        }
        pending.extend(
            element
                .children
                .iter()
                .rev()
                .filter_map(|event| match event {
                    EventRef::Element(child_index) => Some(*child_index),
                    EventRef::Text(_) => None,
                }),
        );
    }
    None
}

fn selected_descendant_text(
    context: &RenderContext<'_>,
    element_index: usize,
    force_full: bool,
    exact: bool,
) -> String {
    fn visit(context: &RenderContext<'_>, event: EventRef, force_full: bool, output: &mut String) {
        let full = force_full || !context.selection_active || context.selected.contains(&event);
        if context.selection_active && !full && !context.included.contains(&event) {
            return;
        }
        match event {
            EventRef::Text(index) => output.push_str(&context.graph.texts[index].exposed.text),
            EventRef::Element(index) => {
                for child in &context.graph.elements[index].children {
                    visit(context, *child, full, output);
                }
            }
        }
    }
    let mut output = String::new();
    for child in &context.graph.elements[element_index].children {
        visit(context, *child, force_full, &mut output);
    }
    if exact {
        output
    } else {
        normalize_visible_text(&output)
    }
}

fn render_children(
    context: &RenderContext<'_>,
    element: &ElementRecord,
    force_full: bool,
) -> String {
    let mut output = String::new();
    let mut previous_was_block = false;
    for child in &element.children {
        let Some(rendered) = render_event(context, *child, force_full) else {
            continue;
        };
        if rendered.is_empty() {
            continue;
        }
        let child_is_block = matches!(
            child,
            EventRef::Element(index) if context.graph.elements[*index].exposed.block
        );
        if !output.is_empty() && (child_is_block || previous_was_block) && !output.ends_with("\n\n")
        {
            output.push_str("\n\n");
        }
        output.push_str(&rendered);
        previous_was_block = child_is_block;
    }
    output
}

fn render_list_item(graph: &Graph, element_index: usize, content: &str) -> String {
    let item = graph
        .list_items
        .iter()
        .find(|item| item.node_id == graph.elements[element_index].exposed.id);
    let Some(item) = item else {
        return format!("- {}", content.trim());
    };
    let marker = item
        .ordinal
        .map(|ordinal| format!("{ordinal}."))
        .unwrap_or_else(|| "-".to_owned());
    let indent = "  ".repeat(item.depth);
    let body = content
        .trim()
        .lines()
        .enumerate()
        .map(|(line_index, line)| {
            if line_index == 0 {
                format!("{indent}{marker} {line}")
            } else {
                format!("{indent}  {line}")
            }
        })
        .collect::<Vec<_>>()
        .join("\n");
    body
}

fn render_table(context: &RenderContext<'_>, table_index: usize, force_full: bool) -> String {
    let Some(table) = context
        .graph
        .tables
        .iter()
        .find(|table| table.node_id == context.graph.elements[table_index].exposed.id)
    else {
        return String::new();
    };
    let mut rows: Vec<Vec<&NativeIRTableCellV2>> = vec![Vec::new(); table.row_count];
    for cell in context
        .graph
        .cells
        .iter()
        .filter(|cell| cell.table_id == table.id)
    {
        let node_index = context.graph.element_by_id.get(&cell.node_id).copied();
        let include = node_index.is_some_and(|index| {
            force_full
                || !context.selection_active
                || context.included.contains(&EventRef::Element(index))
        });
        if include {
            rows[cell.row_index].push(cell);
        }
    }
    let mut output = String::from("<table>\n<tbody>\n");
    for row in rows {
        if row.is_empty() {
            continue;
        }
        output.push_str("<tr>\n");
        for cell in row {
            let tag = if cell.header { "th" } else { "td" };
            let mut attributes = format!(
                " data-row=\"{}\" data-column=\"{}\"",
                cell.row_index, cell.column_index
            );
            if cell.row_span != 1 {
                attributes.push_str(&format!(" rowspan=\"{}\"", cell.row_span));
            }
            if cell.column_span != 1 {
                attributes.push_str(&format!(" colspan=\"{}\"", cell.column_span));
            }
            if !cell.scope.is_empty() {
                attributes.push_str(&format!(
                    " scope=\"{}\"",
                    escape_html_attribute(&cell.scope)
                ));
            }
            let Some(cell_index) = context.graph.element_by_id.get(&cell.node_id).copied() else {
                continue;
            };
            // A directly selected cell owns its complete subtree, just like
            // every other selected element. The table renderer is special
            // because it renders cells itself rather than recursing through
            // `render_event`, so propagate that selected-element closure
            // explicitly. Text-run-only selections remain narrow.
            let cell_force_full =
                force_full || context.selected.contains(&EventRef::Element(cell_index));
            let text = selected_descendant_text(context, cell_index, cell_force_full, false);
            output.push_str(&format!(
                "<{tag}{attributes}>{}</{tag}>\n",
                escape_html_text(text.trim())
            ));
        }
        output.push_str("</tr>\n");
    }
    output.push_str("</tbody>\n</table>");
    output
}

fn render_math(graph: &Graph, element_index: usize) -> Option<String> {
    let math = graph
        .maths
        .iter()
        .find(|math| math.node_id == graph.elements[element_index].exposed.id)?;
    if let Some(tex) = math.tex.as_ref() {
        return Some(if math.display == "block" {
            format!("$$\n{tex}\n$$")
        } else {
            format!("\\({tex}\\)")
        });
    }
    Some(math.source_markup.clone())
}

fn fenced_code(code: &str, language: Option<&str>) -> String {
    let fence = "`".repeat(max_backtick_run(code).saturating_add(1).max(3));
    let language = language.unwrap_or_default();
    format!("{fence}{language}\n{code}\n{fence}")
}

fn inline_code(code: &str) -> String {
    let fence = "`".repeat(max_backtick_run(code).saturating_add(1).max(1));
    format!("{fence}{code}{fence}")
}

fn max_backtick_run(value: &str) -> usize {
    let mut maximum = 0;
    let mut current = 0;
    for character in value.chars() {
        if character == '`' {
            current += 1;
            maximum = maximum.max(current);
        } else {
            current = 0;
        }
    }
    maximum
}

fn normalize_visible_text(value: &str) -> String {
    let mut output = String::with_capacity(value.len());
    let mut pending_space = false;
    for character in value.chars().filter(|character| *character != '\0') {
        if character.is_whitespace() {
            pending_space = true;
        } else {
            if pending_space {
                output.push(' ');
                pending_space = false;
            }
            output.push(character);
        }
    }
    if pending_space {
        output.push(' ');
    }
    output
}

fn escape_markdown_text(value: &str) -> String {
    let mut output = String::with_capacity(value.len());
    for character in value.chars() {
        if matches!(character, '\\' | '*' | '_' | '[' | ']' | '`') {
            output.push('\\');
        }
        output.push(character);
    }
    output
}

fn escape_html_text(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

fn escape_html_attribute(value: &str) -> String {
    escape_html_text(value)
        .replace('"', "&quot;")
        .replace('\'', "&#39;")
}

fn join_blocks(values: Vec<String>) -> String {
    let mut output = String::new();
    for value in values {
        let value = value.trim_matches('\n');
        if value.trim().is_empty() {
            continue;
        }
        if !output.is_empty() {
            output.push_str("\n\n");
        }
        output.push_str(value);
    }
    output
}

fn truncate_utf8_owned(mut value: String, max_bytes: usize) -> (String, bool) {
    if value.len() <= max_bytes {
        return (value, false);
    }
    let mut end = max_bytes;
    while end > 0 && !value.is_char_boundary(end) {
        end -= 1;
    }
    value.truncate(end);
    (value, true)
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

    fn limits() -> LimitsV2 {
        LimitsV2 {
            max_input_bytes: DEFAULT_MAX_INPUT_BYTES,
            max_nodes: DEFAULT_MAX_NODES,
            max_elements: DEFAULT_MAX_ELEMENTS,
            max_text_runs: DEFAULT_MAX_TEXT_RUNS,
            max_depth: DEFAULT_MAX_DEPTH,
            max_text_run_bytes: DEFAULT_MAX_TEXT_RUN_BYTES,
            max_total_text_bytes: DEFAULT_MAX_TOTAL_TEXT_BYTES,
            max_math_bytes: DEFAULT_MAX_MATH_BYTES,
            max_table_columns: DEFAULT_MAX_TABLE_COLUMNS,
        }
    }

    fn build(html: &str) -> BuildResult {
        build_ir_v2(html.to_owned(), html.len(), false, limits())
    }

    #[test]
    fn stable_paths_spans_and_exact_code_are_source_backed() {
        let html = "<main id=\"m\"><p>A <em>B</em> C</p><pre><code>fn main() {\n  go();\n}</code></pre></main>";
        let first = build(html);
        let second = build(html);
        assert_eq!(
            first
                .graph
                .elements
                .iter()
                .map(|element| (&element.exposed.id, &element.exposed.path))
                .collect::<Vec<_>>(),
            second
                .graph
                .elements
                .iter()
                .map(|element| (&element.exposed.id, &element.exposed.path))
                .collect::<Vec<_>>()
        );
        let main = first
            .graph
            .elements
            .iter()
            .find(|element| element.exposed.tag == "main")
            .unwrap();
        assert_eq!(
            &first.source[main.exposed.source_start.unwrap()..main.exposed.source_end.unwrap()],
            html
        );
        assert!(main.exposed.source_span_reliable);
        let code = first
            .graph
            .texts
            .iter()
            .find(|text| text.exposed.text.contains("go();"))
            .unwrap();
        assert_eq!(code.exposed.text, "fn main() {\n  go();\n}");
        assert!(code.exposed.preserve_whitespace);
        assert!(code.exposed.source_span_reliable);
    }

    #[test]
    fn table_grid_accounts_for_row_and_column_spans() {
        let result = build(
            "<table><tr><th rowspan=\"2\">A</th><th colspan=\"2\">B</th></tr><tr><td>C</td><td>D</td></tr></table>",
        );
        let cells = &result.graph.cells;
        assert_eq!(cells.len(), 4);
        assert_eq!(
            cells
                .iter()
                .map(|cell| (
                    cell.row_index,
                    cell.column_index,
                    cell.row_span,
                    cell.column_span
                ))
                .collect::<Vec<_>>(),
            vec![(0, 0, 2, 1), (0, 1, 1, 2), (1, 1, 1, 1), (1, 2, 1, 1)]
        );
        assert_eq!(result.graph.tables[0].column_count, 3);
        assert!(result.graph.tables[0].grid_complete);
    }

    #[test]
    fn ordered_reversed_nested_and_description_lists_are_explicit() {
        let result = build(
            "<ol start=\"5\"><li>A<ul><li>B</li></ul></li><li value=\"9\">C</li></ol><ol reversed><li>R1</li><li>R2</li></ol><dl><dt>T</dt><dd>D</dd></dl>",
        );
        assert_eq!(result.graph.lists.len(), 4);
        let ordered = result
            .graph
            .lists
            .iter()
            .find(|list| list.kind == "ordered")
            .unwrap();
        assert_eq!(ordered.start, Some(5));
        let ordinals = result
            .graph
            .list_items
            .iter()
            .filter(|item| item.list_id == ordered.id)
            .map(|item| item.ordinal)
            .collect::<Vec<_>>();
        assert_eq!(ordinals, vec![Some(5), Some(9)]);
        let nested = result
            .graph
            .lists
            .iter()
            .find(|list| list.kind == "unordered")
            .unwrap();
        assert_eq!(nested.depth, 1);
        let reversed = result
            .graph
            .lists
            .iter()
            .find(|list| list.kind == "ordered" && list.reversed)
            .unwrap();
        assert_eq!(reversed.start, Some(2));
        assert_eq!(
            result
                .graph
                .list_items
                .iter()
                .filter(|item| item.list_id == reversed.id)
                .map(|item| item.ordinal)
                .collect::<Vec<_>>(),
            vec![Some(2), Some(1)]
        );
        let description_kinds = result
            .graph
            .list_items
            .iter()
            .filter(|item| {
                result
                    .graph
                    .lists
                    .iter()
                    .any(|list| list.id == item.list_id && list.kind == "description")
            })
            .map(|item| item.kind.as_str())
            .collect::<Vec<_>>();
        assert_eq!(description_kinds, vec!["term", "definition"]);
    }

    #[test]
    fn mathml_annotations_and_tex_scripts_are_typed() {
        let result = build(
            r#"<p>Inline <math display="block" alttext="x"><semantics><mi>x</mi><annotation encoding="application/x-tex">x^2</annotation></semantics></math></p><script type="math/tex">y_1</script>"#,
        );
        assert_eq!(result.graph.maths.len(), 2);
        assert_eq!(result.graph.maths[0].format, "mathml");
        assert_eq!(result.graph.maths[0].tex.as_deref(), Some("x^2"));
        assert_eq!(result.graph.maths[0].display, "block");
        assert!(result.graph.maths[0].source_backed);
        assert_eq!(result.graph.maths[1].tex.as_deref(), Some("y_1"));
    }

    #[test]
    fn deterministic_reconstruction_preserves_code_and_table_structure() {
        let result = build(
            "<main><h2>T</h2><p>Hello <em>careful</em> world.</p><pre><code class=\"language-rust\">a``b\n  c</code></pre><table><tr><th rowspan=\"2\">H</th><td>A</td></tr><tr><td>B</td></tr></table></main>",
        );
        let first = serialize_graph(&result.graph, None, true, false, &[]);
        let second = serialize_graph(&result.graph, None, true, false, &[]);
        assert_eq!(first, second);
        assert!(first.markdown.contains("```"));
        assert!(first.markdown.contains("```rust"));
        assert!(first.markdown.contains("a``b\n  c"));
        assert!(first.markdown.contains("Hello *careful* world."));
        assert!(first.markdown.contains("rowspan=\"2\""));
        assert!(first.markdown.contains("<table>"));
        assert!(first.exact_code_whitespace);
        assert!(first.table_grid_complete);
    }

    #[test]
    fn every_limit_has_machine_readable_provenance() {
        let mut constrained = limits();
        constrained.max_input_bytes = 120;
        constrained.max_text_run_bytes = 5;
        constrained.max_total_text_bytes = 10;
        constrained.max_table_columns = 2;
        constrained.max_math_bytes = 8;
        let html = format!(
            "<main><p>{}</p><table><tr><td colspan=\"99\">wide</td></tr></table><math><mi>long-math</mi></math></main>",
            "é".repeat(100)
        );
        let result = build_ir_v2(html.clone(), html.len(), true, constrained);
        assert!(result.is_truncated());
        assert!(result
            .truncation_reasons
            .iter()
            .any(|reason| reason == "input_bytes"));
        assert!(result
            .graph
            .texts
            .iter()
            .all(|text| text.exposed.text.is_char_boundary(text.exposed.text.len())));
    }

    #[test]
    fn math_markup_uses_one_document_level_budget() {
        let mut constrained = limits();
        constrained.max_math_bytes = 24;
        let html = "<main><math><mi>first</mi></math><math><mi>second</mi></math></main>";
        let result = build_ir_v2(html.to_owned(), html.len(), false, constrained);

        assert!(
            result
                .graph
                .maths
                .iter()
                .map(|math| math.source_markup.len())
                .sum::<usize>()
                <= constrained.max_math_bytes
        );
        assert!(result.math_truncated_nodes > 0);
        assert!(result
            .truncation_reasons
            .iter()
            .any(|reason| reason == "math_markup"));
    }
}

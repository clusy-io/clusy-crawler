//! Strict, additive selection-certificate replay for ordered DOM IR v2.
//!
//! This module is deliberately unwired from every production route. V0 only
//! certifies selections that the current graph can prove are complete and
//! source-backed. Unsupported or ambiguous coordinates are rejected rather
//! than approximated.

use std::collections::{HashMap, HashSet};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList, PyString};
use sha2::{Digest as _, Sha256};

use super::{
    is_math_element, serialize_graph, EventRef, Graph, NativeDocumentIRV2, NativeIRSerializationV2,
};

const CONTRACT_VERSION: &str = "selection-certificate.v0";
const WIRE_MAGIC: &[u8; 8] = b"CLSYSCV0";
const WIRE_VERSION: u16 = 0;
const WIRE_FLAG_LOCAL_ATOMIC: u16 = 1 << 0;

const DEFAULT_MAX_OUTPUT_BYTES: usize = 4 * 1024 * 1024;
const HARD_MAX_OUTPUT_BYTES: usize = 16 * 1024 * 1024;
const HARD_MAX_CERTIFICATE_BYTES: usize = 2 * 1024 * 1024;
const HARD_MAX_SELECTIONS: usize = 16_384;
const HARD_MAX_ID_BYTES: usize = 256;

const ENTRY_KIND_ELEMENT: u8 = 1;
const ENTRY_KIND_TEXT: u8 = 2;
const ENTRY_RESERVED: u8 = 0;
const FIXED_HEADER_BYTES: usize = 8 + 2 + 2 + 32 + 32 + 32 + 8 + 8 + 4;
const FIXED_ENTRY_BYTES: usize = 1 + 1 + 2 + 8 + 8 + 8;

type CertificateResult<T> = Result<T, CertificateError>;

#[derive(Clone, Debug, Eq, PartialEq)]
struct CertificateError(String);

impl CertificateError {
    fn new(message: impl Into<String>) -> Self {
        Self(message.into())
    }

    fn python(self) -> PyErr {
        PyValueError::new_err(format!("{CONTRACT_VERSION}: {}", self.0))
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum SelectionKind {
    Element,
    Text,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ValidationScope {
    FullDocument,
    LocalAtomic,
}

impl SelectionKind {
    fn wire(self) -> u8 {
        match self {
            Self::Element => ENTRY_KIND_ELEMENT,
            Self::Text => ENTRY_KIND_TEXT,
        }
    }

    fn from_wire(value: u8) -> CertificateResult<Self> {
        match value {
            ENTRY_KIND_ELEMENT => Ok(Self::Element),
            ENTRY_KIND_TEXT => Ok(Self::Text),
            _ => Err(CertificateError::new("unknown selection kind")),
        }
    }
}

impl ValidationScope {
    fn wire_flags(self) -> u16 {
        match self {
            Self::FullDocument => 0,
            Self::LocalAtomic => WIRE_FLAG_LOCAL_ATOMIC,
        }
    }

    fn from_wire_flags(value: u16) -> CertificateResult<Self> {
        match value {
            0 => Ok(Self::FullDocument),
            WIRE_FLAG_LOCAL_ATOMIC => Ok(Self::LocalAtomic),
            _ => Err(CertificateError::new(
                "unknown or noncanonical certificate scope flags",
            )),
        }
    }

    fn name(self) -> &'static str {
        match self {
            Self::FullDocument => "full_document",
            Self::LocalAtomic => "local_atomic",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct SelectionEntry {
    kind: SelectionKind,
    id: String,
    order: u64,
    source_start: u64,
    source_end: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct DecodedCertificate {
    scope: ValidationScope,
    source_digest: [u8; 32],
    graph_digest: [u8; 32],
    output_digest: [u8; 32],
    output_bytes: u64,
    max_output_bytes: u64,
    entries: Vec<SelectionEntry>,
}

#[derive(Clone, Copy)]
struct CertificateDocument<'a> {
    graph: &'a Graph,
    source: &'a str,
    source_complete: bool,
    source_mapping_complete: bool,
    document_truncated: bool,
    parse_error_count: usize,
}

#[derive(Clone)]
struct OwnedCertificateDocument {
    graph: Graph,
    source: String,
    source_complete: bool,
    source_mapping_complete: bool,
    document_truncated: bool,
    parse_error_count: usize,
}

impl OwnedCertificateDocument {
    fn from_native(document: &NativeDocumentIRV2) -> Self {
        Self {
            graph: document.graph.clone(),
            source: document.source.clone(),
            source_complete: document.source_complete,
            source_mapping_complete: document.source_mapping_complete,
            document_truncated: document.truncated,
            parse_error_count: document.parse_error_count,
        }
    }

    fn view(&self) -> CertificateDocument<'_> {
        CertificateDocument {
            graph: &self.graph,
            source: &self.source,
            source_complete: self.source_complete,
            source_mapping_complete: self.source_mapping_complete,
            document_truncated: self.document_truncated,
            parse_error_count: self.parse_error_count,
        }
    }
}

impl<'a> From<&'a NativeDocumentIRV2> for CertificateDocument<'a> {
    fn from(document: &'a NativeDocumentIRV2) -> Self {
        Self {
            graph: &document.graph,
            source: &document.source,
            source_complete: document.source_complete,
            source_mapping_complete: document.source_mapping_complete,
            document_truncated: document.truncated,
            parse_error_count: document.parse_error_count,
        }
    }
}

/// Canonical replay identity, not an authentication token or signature.
///
/// Only `verify_and_replay_selection_certificate_v0_native` establishes that
/// these bytes match a particular immutable native document graph.
#[pyclass(frozen)]
pub(super) struct NativeSelectionCertificateV0 {
    encoded: Vec<u8>,
    #[pyo3(get)]
    contract_version: &'static str,
    #[pyo3(get)]
    wire_version: u16,
    #[pyo3(get)]
    validation_scope: &'static str,
    #[pyo3(get)]
    source_digest: String,
    #[pyo3(get)]
    graph_digest: String,
    #[pyo3(get)]
    output_digest: String,
    #[pyo3(get)]
    output_bytes: usize,
    #[pyo3(get)]
    max_output_bytes: usize,
    #[pyo3(get)]
    selection_count: usize,
    #[pyo3(get)]
    selected_ids: Vec<String>,
    #[pyo3(get)]
    certificate_digest: String,
}

#[pymethods]
impl NativeSelectionCertificateV0 {
    #[getter]
    fn encoded<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.encoded)
    }
}

#[pyclass(frozen, get_all)]
pub(super) struct NativeSelectionReceiptV0 {
    contract_version: &'static str,
    wire_version: u16,
    validation_scope: &'static str,
    certificate_digest: String,
    source_digest: String,
    graph_digest: String,
    output_digest: String,
    output_bytes: usize,
    certificate_output_limit_bytes: usize,
    verifier_output_limit_bytes: usize,
    selection_count: usize,
    selected_ids: Vec<String>,
    verified: bool,
    deterministic: bool,
}

#[pyclass(frozen)]
pub(super) struct NativeSelectionReplayV0 {
    #[pyo3(get)]
    markdown: String,
    receipt: Py<NativeSelectionReceiptV0>,
}

#[pymethods]
impl NativeSelectionReplayV0 {
    #[getter]
    fn receipt(&self, py: Python<'_>) -> Py<NativeSelectionReceiptV0> {
        self.receipt.clone_ref(py)
    }
}

#[pyfunction]
#[pyo3(signature = (
    document,
    selected_ids,
    max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
))]
fn create_selection_certificate_v0_native(
    py: Python<'_>,
    document: PyRef<'_, NativeDocumentIRV2>,
    selected_ids: &Bound<'_, PyList>,
    max_output_bytes: usize,
) -> PyResult<NativeSelectionCertificateV0> {
    let output_limit = validate_output_limit(max_output_bytes).map_err(CertificateError::python)?;
    let selected_ids = bounded_selected_ids(selected_ids)?;
    let owned = OwnedCertificateDocument::from_native(&document);
    drop(document);
    py.detach(move || {
        let view = owned.view();
        let certificate = create_certificate(&view, &selected_ids, output_limit)
            .map_err(CertificateError::python)?;
        let encoded = encode_certificate(&certificate).map_err(CertificateError::python)?;
        native_certificate(encoded, &certificate).map_err(CertificateError::python)
    })
}

#[pyfunction]
#[pyo3(signature = (
    document,
    selected_ids,
    max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
))]
fn create_local_atomic_selection_certificate_v0_native(
    py: Python<'_>,
    document: PyRef<'_, NativeDocumentIRV2>,
    selected_ids: &Bound<'_, PyList>,
    max_output_bytes: usize,
) -> PyResult<NativeSelectionCertificateV0> {
    let output_limit = validate_output_limit(max_output_bytes).map_err(CertificateError::python)?;
    let selected_ids = bounded_selected_ids(selected_ids)?;
    let owned = OwnedCertificateDocument::from_native(&document);
    drop(document);
    py.detach(move || {
        let view = owned.view();
        let certificate = create_local_atomic_certificate(&view, &selected_ids, output_limit)
            .map_err(CertificateError::python)?;
        let encoded = encode_certificate(&certificate).map_err(CertificateError::python)?;
        native_certificate(encoded, &certificate).map_err(CertificateError::python)
    })
}

#[pyfunction]
fn decode_selection_certificate_v0_native(
    py: Python<'_>,
    encoded: &[u8],
) -> PyResult<NativeSelectionCertificateV0> {
    if encoded.len() > HARD_MAX_CERTIFICATE_BYTES {
        return Err(CertificateError::new("certificate exceeds hard byte limit").python());
    }
    let encoded = encoded.to_vec();
    py.detach(move || {
        let certificate = decode_certificate(&encoded).map_err(CertificateError::python)?;
        native_certificate(encoded, &certificate).map_err(CertificateError::python)
    })
}

#[pyfunction]
#[pyo3(signature = (
    document,
    encoded,
    max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
))]
fn verify_and_replay_selection_certificate_v0_native(
    py: Python<'_>,
    document: PyRef<'_, NativeDocumentIRV2>,
    encoded: &[u8],
    max_output_bytes: usize,
) -> PyResult<NativeSelectionReplayV0> {
    let verifier_limit =
        validate_output_limit(max_output_bytes).map_err(CertificateError::python)?;
    if encoded.len() > HARD_MAX_CERTIFICATE_BYTES {
        return Err(CertificateError::new("certificate exceeds hard byte limit").python());
    }
    let encoded = encoded.to_vec();
    let certificate = py.detach({
        let encoded = &encoded;
        move || decode_certificate(encoded).map_err(CertificateError::python)
    })?;
    let owned = OwnedCertificateDocument::from_native(&document);
    drop(document);
    let replay = py.detach(move || {
        verify_decoded_and_replay(&owned.view(), certificate, &encoded, verifier_limit)
            .map_err(CertificateError::python)
    })?;
    let receipt = Py::new(
        py,
        NativeSelectionReceiptV0 {
            contract_version: CONTRACT_VERSION,
            wire_version: WIRE_VERSION,
            validation_scope: replay.certificate.scope.name(),
            certificate_digest: hex_digest(&certificate_digest_v0(&replay.encoded)),
            source_digest: hex_digest(&replay.certificate.source_digest),
            graph_digest: hex_digest(&replay.certificate.graph_digest),
            output_digest: hex_digest(&replay.certificate.output_digest),
            output_bytes: usize_from_u64(replay.certificate.output_bytes)
                .map_err(CertificateError::python)?,
            certificate_output_limit_bytes: usize_from_u64(replay.certificate.max_output_bytes)
                .map_err(CertificateError::python)?,
            verifier_output_limit_bytes: verifier_limit,
            selection_count: replay.certificate.entries.len(),
            selected_ids: replay
                .certificate
                .entries
                .iter()
                .map(|entry| entry.id.clone())
                .collect(),
            verified: true,
            deterministic: true,
        },
    )?;
    Ok(NativeSelectionReplayV0 {
        markdown: replay.markdown,
        receipt,
    })
}

#[pyfunction]
#[pyo3(signature = (
    document,
    encoded,
    max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
))]
fn verify_and_replay_local_atomic_selection_certificate_v0_native(
    py: Python<'_>,
    document: PyRef<'_, NativeDocumentIRV2>,
    encoded: &[u8],
    max_output_bytes: usize,
) -> PyResult<NativeSelectionReplayV0> {
    let verifier_limit =
        validate_output_limit(max_output_bytes).map_err(CertificateError::python)?;
    if encoded.len() > HARD_MAX_CERTIFICATE_BYTES {
        return Err(CertificateError::new("certificate exceeds hard byte limit").python());
    }
    let encoded = encoded.to_vec();
    let certificate = py.detach({
        let encoded = &encoded;
        move || decode_certificate(encoded).map_err(CertificateError::python)
    })?;
    let owned = OwnedCertificateDocument::from_native(&document);
    drop(document);
    let replay = py.detach(move || {
        verify_decoded_and_replay_scoped(
            &owned.view(),
            certificate,
            &encoded,
            verifier_limit,
            ValidationScope::LocalAtomic,
        )
        .map_err(CertificateError::python)
    })?;
    let receipt = Py::new(
        py,
        NativeSelectionReceiptV0 {
            contract_version: CONTRACT_VERSION,
            wire_version: WIRE_VERSION,
            validation_scope: replay.certificate.scope.name(),
            certificate_digest: hex_digest(&certificate_digest_v0(&replay.encoded)),
            source_digest: hex_digest(&replay.certificate.source_digest),
            graph_digest: hex_digest(&replay.certificate.graph_digest),
            output_digest: hex_digest(&replay.certificate.output_digest),
            output_bytes: usize_from_u64(replay.certificate.output_bytes)
                .map_err(CertificateError::python)?,
            certificate_output_limit_bytes: usize_from_u64(replay.certificate.max_output_bytes)
                .map_err(CertificateError::python)?,
            verifier_output_limit_bytes: verifier_limit,
            selection_count: replay.certificate.entries.len(),
            selected_ids: replay
                .certificate
                .entries
                .iter()
                .map(|entry| entry.id.clone())
                .collect(),
            verified: true,
            deterministic: true,
        },
    )?;
    Ok(NativeSelectionReplayV0 {
        markdown: replay.markdown,
        receipt,
    })
}

pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<NativeSelectionCertificateV0>()?;
    module.add_class::<NativeSelectionReceiptV0>()?;
    module.add_class::<NativeSelectionReplayV0>()?;
    module.add_function(wrap_pyfunction!(
        create_selection_certificate_v0_native,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        create_local_atomic_selection_certificate_v0_native,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        decode_selection_certificate_v0_native,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        verify_and_replay_selection_certificate_v0_native,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        verify_and_replay_local_atomic_selection_certificate_v0_native,
        module
    )?)?;
    Ok(())
}

#[derive(Debug)]
struct VerifiedReplay {
    certificate: DecodedCertificate,
    encoded: Vec<u8>,
    markdown: String,
}

fn create_certificate(
    document: &CertificateDocument<'_>,
    selected_ids: &[String],
    max_output_bytes: usize,
) -> CertificateResult<DecodedCertificate> {
    create_certificate_scoped(
        document,
        selected_ids,
        max_output_bytes,
        ValidationScope::FullDocument,
    )
}

fn create_local_atomic_certificate(
    document: &CertificateDocument<'_>,
    selected_ids: &[String],
    max_output_bytes: usize,
) -> CertificateResult<DecodedCertificate> {
    create_certificate_scoped(
        document,
        selected_ids,
        max_output_bytes,
        ValidationScope::LocalAtomic,
    )
}

fn create_certificate_scoped(
    document: &CertificateDocument<'_>,
    selected_ids: &[String],
    max_output_bytes: usize,
    scope: ValidationScope,
) -> CertificateResult<DecodedCertificate> {
    validate_selection_shape(selected_ids)?;
    validate_document_scoped(document, scope)?;
    if scope == ValidationScope::LocalAtomic {
        validate_local_atomic_selection_shape(document, selected_ids)?;
    }
    let entries = validate_selection_scoped(document, selected_ids, scope)?;
    if scope == ValidationScope::LocalAtomic {
        validate_local_atomic_selection(document, selected_ids, &entries)?;
    }
    let markdown = render_selection(document, selected_ids, max_output_bytes)?;
    Ok(DecodedCertificate {
        scope,
        source_digest: source_digest_v0(document.source.as_bytes()),
        graph_digest: graph_digest(document)?,
        output_digest: output_digest_v0(markdown.as_bytes()),
        output_bytes: u64_from_usize(markdown.len())?,
        max_output_bytes: u64_from_usize(max_output_bytes)?,
        entries,
    })
}

#[cfg(test)]
fn verify_and_replay(
    document: &CertificateDocument<'_>,
    encoded: &[u8],
    verifier_output_limit: usize,
) -> CertificateResult<VerifiedReplay> {
    let certificate = decode_certificate(encoded)?;
    verify_decoded_and_replay(document, certificate, encoded, verifier_output_limit)
}

fn verify_decoded_and_replay(
    document: &CertificateDocument<'_>,
    certificate: DecodedCertificate,
    encoded: &[u8],
    verifier_output_limit: usize,
) -> CertificateResult<VerifiedReplay> {
    verify_decoded_and_replay_scoped(
        document,
        certificate,
        encoded,
        verifier_output_limit,
        ValidationScope::FullDocument,
    )
}

fn verify_decoded_and_replay_scoped(
    document: &CertificateDocument<'_>,
    certificate: DecodedCertificate,
    encoded: &[u8],
    verifier_output_limit: usize,
    scope: ValidationScope,
) -> CertificateResult<VerifiedReplay> {
    if certificate.scope != scope {
        return Err(CertificateError::new(
            "certificate validation scope does not match verifier scope",
        ));
    }
    validate_document_scoped(document, scope)?;
    let expected_source_digest = source_digest_v0(document.source.as_bytes());
    if certificate.source_digest != expected_source_digest {
        return Err(CertificateError::new("source digest mismatch"));
    }
    let expected_graph_digest = graph_digest(document)?;
    if certificate.graph_digest != expected_graph_digest {
        return Err(CertificateError::new("graph digest mismatch"));
    }
    let selected_ids = certificate
        .entries
        .iter()
        .map(|entry| entry.id.clone())
        .collect::<Vec<_>>();
    if scope == ValidationScope::LocalAtomic {
        validate_local_atomic_selection_shape(document, &selected_ids)?;
    }
    let expected_entries = validate_selection_scoped(document, &selected_ids, scope)?;
    if scope == ValidationScope::LocalAtomic {
        validate_local_atomic_selection(document, &selected_ids, &expected_entries)?;
    }
    if certificate.entries != expected_entries {
        return Err(CertificateError::new(
            "selection ID, event order, or source span mismatch",
        ));
    }
    let certificate_limit = usize_from_u64(certificate.max_output_bytes)?;
    let effective_limit = certificate_limit.min(verifier_output_limit);
    let markdown = render_selection(document, &selected_ids, effective_limit)?;
    if u64_from_usize(markdown.len())? != certificate.output_bytes {
        return Err(CertificateError::new("serialized output length mismatch"));
    }
    if output_digest_v0(markdown.as_bytes()) != certificate.output_digest {
        return Err(CertificateError::new("serialized output digest mismatch"));
    }
    Ok(VerifiedReplay {
        certificate,
        encoded: encoded.to_vec(),
        markdown,
    })
}

fn validate_document_scoped(
    document: &CertificateDocument<'_>,
    scope: ValidationScope,
) -> CertificateResult<()> {
    if !document.source_complete {
        return Err(CertificateError::new("decoded UTF-8 source is incomplete"));
    }
    if document.document_truncated {
        return Err(CertificateError::new("document graph is truncated"));
    }
    if document.parse_error_count != 0 {
        return Err(CertificateError::new(
            "HTML parse errors prevent exact source provenance",
        ));
    }
    validate_graph_topology(document.graph)?;
    validate_structure_references(document.graph)?;
    if scope == ValidationScope::FullDocument {
        if !document.source_mapping_complete {
            return Err(CertificateError::new(
                "explicit DOM/source mapping is incomplete",
            ));
        }
        if document
            .graph
            .elements
            .iter()
            .any(|element| !element.exposed.implicit && !element.exposed.source_span_reliable)
        {
            return Err(CertificateError::new(
                "an explicit element lacks a complete source span",
            ));
        }
        validate_source_nesting(document)?;
        validate_exact_text_provenance(document)?;
    }
    Ok(())
}

#[cfg(test)]
fn validate_document(document: &CertificateDocument<'_>) -> CertificateResult<()> {
    validate_document_scoped(document, ValidationScope::FullDocument)
}

fn validate_graph_topology(graph: &Graph) -> CertificateResult<()> {
    ordered_events(graph)?;

    let mut all_ids = HashSet::with_capacity(graph.elements.len() + graph.texts.len());
    for element in &graph.elements {
        if !all_ids.insert(element.exposed.id.as_str()) {
            return Err(CertificateError::new("graph contains duplicate IDs"));
        }
    }
    for text in &graph.texts {
        if !all_ids.insert(text.exposed.id.as_str()) {
            return Err(CertificateError::new("graph contains duplicate IDs"));
        }
    }

    if graph.element_by_id.len() != graph.elements.len() {
        return Err(CertificateError::new(
            "internal element index is incomplete",
        ));
    }
    for (index, element) in graph.elements.iter().enumerate() {
        if graph.element_by_id.get(&element.exposed.id) != Some(&index) {
            return Err(CertificateError::new(
                "internal element index disagrees with graph storage",
            ));
        }
    }

    let mut owned_elements = vec![false; graph.elements.len()];
    let mut owned_texts = vec![false; graph.texts.len()];
    for root in &graph.roots {
        match *root {
            EventRef::Element(index) => {
                let element = graph
                    .elements
                    .get(index)
                    .ok_or_else(|| CertificateError::new("root element is out of bounds"))?;
                if element.parent_index.is_some() || element.exposed.parent_id.is_some() {
                    return Err(CertificateError::new(
                        "root element unexpectedly has a parent",
                    ));
                }
                mark_single_owner(&mut owned_elements, index, "element")?;
            }
            EventRef::Text(index) => {
                graph
                    .texts
                    .get(index)
                    .ok_or_else(|| CertificateError::new("root text run is out of bounds"))?;
                mark_single_owner(&mut owned_texts, index, "text run")?;
                return Err(CertificateError::new(
                    "retained text run cannot be a graph root",
                ));
            }
        }
    }

    for (parent_index, element) in graph.elements.iter().enumerate() {
        let expected_parent_id = element
            .parent_index
            .map(|index| {
                graph
                    .elements
                    .get(index)
                    .map(|parent| parent.exposed.id.as_str())
                    .ok_or_else(|| CertificateError::new("element parent is out of bounds"))
            })
            .transpose()?;
        if element.exposed.parent_id.as_deref() != expected_parent_id {
            return Err(CertificateError::new(
                "exposed element parent disagrees with internal topology",
            ));
        }

        let expected_child_ids = element
            .children
            .iter()
            .map(|event| checked_event_id(graph, *event).map(ToOwned::to_owned))
            .collect::<CertificateResult<Vec<_>>>()?;
        if element.exposed.child_ids != expected_child_ids {
            return Err(CertificateError::new(
                "exposed child IDs disagree with ordered internal children",
            ));
        }
        let expected_text_ids = element
            .children
            .iter()
            .filter_map(|event| match *event {
                EventRef::Text(index) => Some(
                    graph
                        .texts
                        .get(index)
                        .map(|text| text.exposed.id.clone())
                        .ok_or_else(|| CertificateError::new("child text run is out of bounds")),
                ),
                EventRef::Element(_) => None,
            })
            .collect::<CertificateResult<Vec<_>>>()?;
        if element.exposed.text_run_ids != expected_text_ids {
            return Err(CertificateError::new(
                "exposed text IDs disagree with ordered internal children",
            ));
        }

        let mut previous_child_order = None;
        for child in &element.children {
            let child_order = event_order_u64(graph, *child)?;
            if child_order <= u64_from_usize(element.exposed.order)?
                || previous_child_order.is_some_and(|previous| child_order <= previous)
            {
                return Err(CertificateError::new(
                    "internal children are not in canonical event order",
                ));
            }
            previous_child_order = Some(child_order);
            match *child {
                EventRef::Element(index) => {
                    let child = graph
                        .elements
                        .get(index)
                        .ok_or_else(|| CertificateError::new("child element is out of bounds"))?;
                    if child.parent_index != Some(parent_index) {
                        return Err(CertificateError::new(
                            "child element parent disagrees with owner",
                        ));
                    }
                    mark_single_owner(&mut owned_elements, index, "element")?;
                }
                EventRef::Text(index) => {
                    let child = graph
                        .texts
                        .get(index)
                        .ok_or_else(|| CertificateError::new("child text run is out of bounds"))?;
                    if child.parent_index != parent_index
                        || child.exposed.parent_id != element.exposed.id
                    {
                        return Err(CertificateError::new(
                            "child text parent disagrees with owner",
                        ));
                    }
                    mark_single_owner(&mut owned_texts, index, "text run")?;
                }
            }
        }
    }

    if owned_elements.iter().any(|owned| !owned) || owned_texts.iter().any(|owned| !owned) {
        return Err(CertificateError::new(
            "internal graph contains an unreachable event",
        ));
    }
    Ok(())
}

fn mark_single_owner(owners: &mut [bool], index: usize, kind: &str) -> CertificateResult<()> {
    let owned = owners
        .get_mut(index)
        .ok_or_else(|| CertificateError::new(format!("{kind} index is out of bounds")))?;
    if *owned {
        return Err(CertificateError::new(format!(
            "{kind} has more than one internal owner"
        )));
    }
    *owned = true;
    Ok(())
}

fn checked_event_id(graph: &Graph, event: EventRef) -> CertificateResult<&str> {
    match event {
        EventRef::Element(index) => graph
            .elements
            .get(index)
            .map(|element| element.exposed.id.as_str())
            .ok_or_else(|| CertificateError::new("element reference is out of bounds")),
        EventRef::Text(index) => graph
            .texts
            .get(index)
            .map(|text| text.exposed.id.as_str())
            .ok_or_else(|| CertificateError::new("text reference is out of bounds")),
    }
}

fn validate_structure_references(graph: &Graph) -> CertificateResult<()> {
    let element_by_id = &graph.element_by_id;
    let text_ids = graph
        .texts
        .iter()
        .map(|text| text.exposed.id.as_str())
        .collect::<HashSet<_>>();

    let mut table_ids = HashSet::with_capacity(graph.tables.len());
    let mut cell_ids = HashSet::with_capacity(graph.cells.len());
    for cell in &graph.cells {
        if !cell_ids.insert(cell.id.as_str())
            || !text_references_exist(&cell.text_run_ids, &text_ids)
        {
            return Err(CertificateError::new(
                "table cell references are incomplete or duplicated",
            ));
        }
        let element = element_by_id
            .get(&cell.node_id)
            .and_then(|index| graph.elements.get(*index))
            .ok_or_else(|| CertificateError::new("table cell node is unknown"))?;
        if !matches!(element.exposed.tag.as_str(), "td" | "th")
            || element.exposed.order != cell.order
        {
            return Err(CertificateError::new(
                "table cell metadata disagrees with its element",
            ));
        }
    }
    for table in &graph.tables {
        if !table_ids.insert(table.id.as_str()) {
            return Err(CertificateError::new("table IDs are duplicated"));
        }
        let element = element_by_id
            .get(&table.node_id)
            .and_then(|index| graph.elements.get(*index))
            .ok_or_else(|| CertificateError::new("table node is unknown"))?;
        if element.exposed.tag != "table" || element.exposed.order != table.order {
            return Err(CertificateError::new(
                "table metadata disagrees with its element",
            ));
        }
        let expected = graph
            .cells
            .iter()
            .filter(|cell| cell.table_id == table.id)
            .map(|cell| cell.id.as_str())
            .collect::<Vec<_>>();
        if table
            .cell_ids
            .iter()
            .map(String::as_str)
            .collect::<Vec<_>>()
            != expected
        {
            return Err(CertificateError::new(
                "table cell membership is incomplete or out of order",
            ));
        }
    }
    if graph
        .cells
        .iter()
        .any(|cell| !table_ids.contains(cell.table_id.as_str()))
    {
        return Err(CertificateError::new(
            "table cell refers to an unknown table",
        ));
    }

    let mut list_ids = HashSet::with_capacity(graph.lists.len());
    let mut item_ids = HashSet::with_capacity(graph.list_items.len());
    for item in &graph.list_items {
        if !item_ids.insert(item.id.as_str())
            || !text_references_exist(&item.text_run_ids, &text_ids)
        {
            return Err(CertificateError::new(
                "list item references are incomplete or duplicated",
            ));
        }
        let element = element_by_id
            .get(&item.node_id)
            .and_then(|index| graph.elements.get(*index))
            .ok_or_else(|| CertificateError::new("list item node is unknown"))?;
        if !matches!(element.exposed.tag.as_str(), "li" | "dt" | "dd")
            || element.exposed.order != item.order
        {
            return Err(CertificateError::new(
                "list item metadata disagrees with its element",
            ));
        }
    }
    for list in &graph.lists {
        if !list_ids.insert(list.id.as_str()) {
            return Err(CertificateError::new("list IDs are duplicated"));
        }
        let element = element_by_id
            .get(&list.node_id)
            .and_then(|index| graph.elements.get(*index))
            .ok_or_else(|| CertificateError::new("list node is unknown"))?;
        if !matches!(element.exposed.tag.as_str(), "ul" | "ol" | "dl")
            || element.exposed.order != list.order
        {
            return Err(CertificateError::new(
                "list metadata disagrees with its element",
            ));
        }
        let expected = graph
            .list_items
            .iter()
            .filter(|item| item.list_id == list.id)
            .map(|item| item.id.as_str())
            .collect::<Vec<_>>();
        if list.item_ids.iter().map(String::as_str).collect::<Vec<_>>() != expected {
            return Err(CertificateError::new(
                "list item membership is incomplete or out of order",
            ));
        }
    }
    if graph
        .list_items
        .iter()
        .any(|item| !list_ids.contains(item.list_id.as_str()))
    {
        return Err(CertificateError::new("list item refers to an unknown list"));
    }

    let mut math_ids = HashSet::with_capacity(graph.maths.len());
    for math in &graph.maths {
        if !math_ids.insert(math.id.as_str()) {
            return Err(CertificateError::new("math IDs are duplicated"));
        }
        let element = element_by_id
            .get(&math.node_id)
            .and_then(|index| graph.elements.get(*index))
            .ok_or_else(|| CertificateError::new("math node is unknown"))?;
        if !is_math_element(&element.exposed.tag, &element.attrs)
            || element.exposed.order != math.order
        {
            return Err(CertificateError::new(
                "math metadata disagrees with its element",
            ));
        }
    }
    Ok(())
}

fn text_references_exist(values: &[String], known: &HashSet<&str>) -> bool {
    let mut seen = HashSet::with_capacity(values.len());
    values
        .iter()
        .all(|value| known.contains(value.as_str()) && seen.insert(value.as_str()))
}

#[derive(Clone, Copy)]
struct OrderedSourceSpan {
    start: usize,
    end: usize,
    event: EventRef,
}

fn validate_source_nesting(document: &CertificateDocument<'_>) -> CertificateResult<()> {
    let graph = document.graph;
    let mut spans = Vec::with_capacity(graph.elements.len() + graph.texts.len());
    for (index, element) in graph.elements.iter().enumerate() {
        if element.exposed.source_span_reliable {
            let (Some(start), Some(end)) =
                (element.exposed.source_start, element.exposed.source_end)
            else {
                return Err(CertificateError::new(
                    "reliable element is missing its source span",
                ));
            };
            spans.push(OrderedSourceSpan {
                start,
                end,
                event: EventRef::Element(index),
            });
        }
    }
    for (index, text) in graph.texts.iter().enumerate() {
        if text.exposed.source_span_reliable {
            let (Some(start), Some(end)) = (text.exposed.source_start, text.exposed.source_end)
            else {
                return Err(CertificateError::new(
                    "reliable text run is missing its source span",
                ));
            };
            spans.push(OrderedSourceSpan {
                start,
                end,
                event: EventRef::Text(index),
            });
        }
    }
    spans.sort_unstable_by(|left, right| {
        left.start
            .cmp(&right.start)
            .then_with(|| right.end.cmp(&left.end))
            .then_with(|| source_event_rank(left.event).cmp(&source_event_rank(right.event)))
    });

    let mut element_stack: Vec<OrderedSourceSpan> = Vec::new();
    let mut previous_event_order = None;
    for span in spans {
        if span.start >= span.end || span.end > document.source.len() {
            return Err(CertificateError::new(
                "graph contains an invalid reliable source span",
            ));
        }
        while element_stack
            .last()
            .is_some_and(|parent| span.start >= parent.end)
        {
            element_stack.pop();
        }
        if element_stack
            .last()
            .is_some_and(|parent| span.end > parent.end)
        {
            return Err(CertificateError::new(
                "reliable source spans cross instead of nesting",
            ));
        }
        let expected_parent = nearest_reliable_dom_ancestor(graph, span.event);
        let source_parent = element_stack.last().and_then(|parent| match parent.event {
            EventRef::Element(index) => Some(index),
            EventRef::Text(_) => None,
        });
        if expected_parent != source_parent {
            return Err(CertificateError::new(
                "DOM ancestry disagrees with reliable source-span nesting",
            ));
        }
        let event_order = event_order_u64(graph, span.event)?;
        if previous_event_order.is_some_and(|previous| event_order <= previous) {
            return Err(CertificateError::new(
                "DOM event order disagrees with reliable source-span order",
            ));
        }
        previous_event_order = Some(event_order);
        if matches!(span.event, EventRef::Element(_)) {
            element_stack.push(span);
        }
    }
    Ok(())
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum SourceTextTokenKind {
    Data,
    RcData,
    RawText,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct SourceTextToken {
    start: usize,
    end: usize,
    parent_start: Option<usize>,
    kind: SourceTextTokenKind,
}

#[derive(Clone, Debug)]
struct OpenSourceElement {
    tag: String,
    start: usize,
    start_tag_end: usize,
    parent_start: Option<usize>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct SourceElementToken {
    tag: String,
    start: usize,
    start_tag_end: usize,
    end: usize,
    parent_start: Option<usize>,
}

#[derive(Debug, Default)]
struct ExactSourceTokenization {
    texts: Vec<SourceTextToken>,
    elements: Vec<SourceElementToken>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ExactTokenizationPolicy {
    FullDocument,
    LocalAtomic,
}

fn validate_exact_text_provenance(document: &CertificateDocument<'_>) -> CertificateResult<()> {
    let tokenization =
        tokenize_exact_source_text(document.source, ExactTokenizationPolicy::FullDocument)?;
    let tokens = &tokenization.texts;
    let token_by_span = tokens
        .iter()
        .map(|token| ((token.start, token.end), token))
        .collect::<HashMap<_, _>>();
    if token_by_span.len() != tokens.len() {
        return Err(CertificateError::new(
            "source tokenizer produced duplicate text spans",
        ));
    }

    let mut mapped = document
        .graph
        .texts
        .iter()
        .enumerate()
        .filter(|(_, text)| text.exposed.source_span_reliable)
        .map(|(index, text)| {
            let (Some(start), Some(end)) = (text.exposed.source_start, text.exposed.source_end)
            else {
                return Err(CertificateError::new(
                    "reliable text run is missing its source span",
                ));
            };
            Ok((start, end, index))
        })
        .collect::<CertificateResult<Vec<_>>>()?;
    mapped.sort_unstable_by_key(|(start, end, _)| (*start, *end));

    let mut previous_end = None;
    let mut previous_order = None;
    for (start, end, index) in mapped {
        if previous_end.is_some_and(|previous| start < previous) {
            return Err(CertificateError::new(
                "text source tokens overlap or alias one another",
            ));
        }
        let text = &document.graph.texts[index];
        let token = token_by_span.get(&(start, end)).ok_or_else(|| {
            CertificateError::new("text run is not backed by one complete tokenizer text token")
        })?;
        let parent = document
            .graph
            .elements
            .get(text.parent_index)
            .ok_or_else(|| CertificateError::new("text parent is out of bounds"))?;
        if parent.exposed.source_start != token.parent_start {
            return Err(CertificateError::new(
                "tokenizer text parent disagrees with the DOM parent",
            ));
        }
        let source_slice = document
            .source
            .get(start..end)
            .ok_or_else(|| CertificateError::new("tokenizer text span is not UTF-8 aligned"))?;
        if source_slice != text.exposed.text {
            return Err(CertificateError::new(
                "tokenizer source bytes do not match decoded DOM text",
            ));
        }
        if source_slice
            .as_bytes()
            .iter()
            .any(|byte| matches!(*byte, b'&' | b'\r' | b'\0'))
        {
            return Err(CertificateError::new(
                "text token requires an unsupported HTML decoding transform",
            ));
        }
        if token.kind == SourceTextTokenKind::RcData && source_slice.as_bytes().contains(&b'<') {
            return Err(CertificateError::new(
                "RCDATA token contains an unsupported less-than transition",
            ));
        }
        let order = event_order_u64(document.graph, EventRef::Text(index))?;
        if previous_order.is_some_and(|previous| order <= previous) {
            return Err(CertificateError::new(
                "tokenizer text order disagrees with DOM event order",
            ));
        }
        previous_end = Some(end);
        previous_order = Some(order);
    }
    Ok(())
}

fn tokenize_exact_source_text(
    source: &str,
    policy: ExactTokenizationPolicy,
) -> CertificateResult<ExactSourceTokenization> {
    let bytes = source.as_bytes();
    let mut tokenization = ExactSourceTokenization::default();
    let mut stack: Vec<OpenSourceElement> = Vec::new();
    let mut cursor = 0;
    while cursor < bytes.len() {
        let Some(relative) = bytes[cursor..].iter().position(|byte| *byte == b'<') else {
            push_source_text_token(
                &mut tokenization.texts,
                cursor,
                bytes.len(),
                stack.last().map(|element| element.start),
                SourceTextTokenKind::Data,
            );
            break;
        };
        let markup_start = cursor + relative;
        push_source_text_token(
            &mut tokenization.texts,
            cursor,
            markup_start,
            stack.last().map(|element| element.start),
            SourceTextTokenKind::Data,
        );

        if super::starts_ascii_case_insensitive(bytes, markup_start, b"<!--") {
            if policy == ExactTokenizationPolicy::LocalAtomic {
                return Err(CertificateError::new(
                    "local atomic comments are not represented by the selected graph",
                ));
            }
            cursor = super::find_bytes(bytes, markup_start + 4, b"-->")
                .map(|value| value + 3)
                .ok_or_else(|| CertificateError::new("unterminated HTML comment"))?;
            continue;
        }
        if markup_start + 1 >= bytes.len() {
            return Err(CertificateError::new("unterminated less-than transition"));
        }
        if matches!(bytes[markup_start + 1], b'!' | b'?') {
            if policy == ExactTokenizationPolicy::LocalAtomic {
                return Err(CertificateError::new(
                    "local atomic markup declarations are not graph-backed",
                ));
            }
            cursor = super::find_tag_end(bytes, markup_start + 2)
                .ok_or_else(|| CertificateError::new("unterminated markup declaration"))?;
            continue;
        }

        let closing = bytes[markup_start + 1] == b'/';
        let name_start = markup_start + if closing { 2 } else { 1 };
        let mut name_end = name_start;
        while name_end < bytes.len() && super::is_tag_name_byte(bytes[name_end]) {
            name_end += 1;
        }
        if name_end == name_start {
            if policy == ExactTokenizationPolicy::LocalAtomic {
                return Err(CertificateError::new(
                    "local atomic source contains an ambiguous less-than transition",
                ));
            }
            // A literal '<' is deliberately not part of an eligible exact
            // token. Resume after it so unrelated later text can still be
            // certified without interpreting the ambiguous transition.
            cursor = markup_start + 1;
            continue;
        }
        let tag = source[name_start..name_end].to_ascii_lowercase();

        if closing {
            let tag_end = exact_end_tag_end(bytes, name_end)?;
            let position = stack
                .iter()
                .rposition(|element| element.tag == tag)
                .ok_or_else(|| CertificateError::new("unmatched HTML end tag"))?;
            if position + 1 != stack.len() {
                return Err(CertificateError::new(
                    "source element nesting requires parser repair",
                ));
            }
            let opened = stack
                .pop()
                .ok_or_else(|| CertificateError::new("source element stack is empty"))?;
            tokenization.elements.push(SourceElementToken {
                tag,
                start: opened.start,
                start_tag_end: opened.start_tag_end,
                end: tag_end,
                parent_start: opened.parent_start,
            });
            cursor = tag_end;
            continue;
        }

        let tag_end = super::find_tag_end(bytes, name_end)
            .ok_or_else(|| CertificateError::new("unterminated HTML tag"))?;
        let self_closing = source[markup_start..tag_end]
            .trim_end_matches('>')
            .trim_end()
            .ends_with('/');
        if self_closing || super::is_void_tag(&tag) {
            tokenization.elements.push(SourceElementToken {
                tag,
                start: markup_start,
                start_tag_end: tag_end,
                end: tag_end,
                parent_start: stack.last().map(|element| element.start),
            });
            cursor = tag_end;
            continue;
        }

        let element_start = markup_start;
        let parent_start = stack.last().map(|element| element.start);
        stack.push(OpenSourceElement {
            tag: tag.clone(),
            start: element_start,
            start_tag_end: tag_end,
            parent_start,
        });
        if super::is_raw_text_tag(&tag) {
            let (close_start, close_end) = super::find_raw_text_close(source, tag_end, &tag)
                .ok_or_else(|| CertificateError::new("unterminated raw-text element"))?;
            let close_name_end = close_start
                .checked_add(2 + tag.len())
                .ok_or_else(|| CertificateError::new("raw-text end-tag offset overflow"))?;
            if exact_end_tag_end(bytes, close_name_end)? != close_end {
                return Err(CertificateError::new(
                    "raw-text end tag is not source-canonical",
                ));
            }
            let kind = if matches!(tag.as_str(), "title" | "textarea") {
                SourceTextTokenKind::RcData
            } else {
                SourceTextTokenKind::RawText
            };
            push_source_text_token(
                &mut tokenization.texts,
                tag_end,
                close_start,
                Some(element_start),
                kind,
            );
            let opened = stack
                .pop()
                .ok_or_else(|| CertificateError::new("raw-text element stack is empty"))?;
            tokenization.elements.push(SourceElementToken {
                tag,
                start: opened.start,
                start_tag_end: opened.start_tag_end,
                end: close_end,
                parent_start: opened.parent_start,
            });
            cursor = close_end;
            continue;
        }
        cursor = tag_end;
    }
    if !stack.is_empty() {
        return Err(CertificateError::new(
            "unterminated source element prevents exact provenance",
        ));
    }
    tokenization
        .elements
        .sort_unstable_by_key(|element| element.start);
    Ok(tokenization)
}

fn exact_end_tag_end(bytes: &[u8], mut cursor: usize) -> CertificateResult<usize> {
    while cursor < bytes.len() {
        match bytes[cursor] {
            b'>' => return Ok(cursor + 1),
            b' ' | b'\t' | b'\n' | 0x0c => cursor += 1,
            _ => {
                return Err(CertificateError::new(
                    "HTML end tag contains attributes, controls, or noncanonical trivia",
                ));
            }
        }
    }
    Err(CertificateError::new("unterminated HTML end tag"))
}

fn push_source_text_token(
    tokens: &mut Vec<SourceTextToken>,
    start: usize,
    end: usize,
    parent_start: Option<usize>,
    kind: SourceTextTokenKind,
) {
    if start < end {
        tokens.push(SourceTextToken {
            start,
            end,
            parent_start,
            kind,
        });
    }
}

fn source_event_rank(event: EventRef) -> u8 {
    match event {
        EventRef::Element(_) => 0,
        EventRef::Text(_) => 1,
    }
}

fn nearest_reliable_dom_ancestor(graph: &Graph, event: EventRef) -> Option<usize> {
    let mut parent = match event {
        EventRef::Element(index) => graph.elements[index].parent_index,
        EventRef::Text(index) => Some(graph.texts[index].parent_index),
    };
    while let Some(index) = parent {
        let element = &graph.elements[index];
        if element.exposed.source_span_reliable {
            return Some(index);
        }
        parent = element.parent_index;
    }
    None
}

fn validate_output_limit(value: usize) -> CertificateResult<usize> {
    if value == 0 || value > HARD_MAX_OUTPUT_BYTES {
        return Err(CertificateError::new(format!(
            "max_output_bytes must be between 1 and {HARD_MAX_OUTPUT_BYTES}"
        )));
    }
    Ok(value)
}

fn bounded_selected_ids(selected_ids: &Bound<'_, PyList>) -> PyResult<Vec<String>> {
    let selection_count = selected_ids.len();
    if selection_count == 0 {
        return Err(CertificateError::new("selection must not be empty").python());
    }
    if selection_count > HARD_MAX_SELECTIONS {
        return Err(CertificateError::new("too many selected events").python());
    }
    let mut output = Vec::with_capacity(selection_count);
    for value in selected_ids.iter() {
        let value = value
            .cast::<PyString>()
            .map_err(|_| CertificateError::new("selection ID must be a string").python())?;
        let character_count = value.len()?;
        if character_count == 0 || character_count > HARD_MAX_ID_BYTES {
            return Err(CertificateError::new("selection ID is not canonical").python());
        }
        let value = value.to_str()?;
        validate_id(value).map_err(CertificateError::python)?;
        output.push(value.to_owned());
    }
    Ok(output)
}

fn validate_selection_shape(selected_ids: &[String]) -> CertificateResult<()> {
    if selected_ids.is_empty() {
        return Err(CertificateError::new("selection must not be empty"));
    }
    if selected_ids.len() > HARD_MAX_SELECTIONS {
        return Err(CertificateError::new("too many selected events"));
    }
    for id in selected_ids {
        validate_id(id)?;
    }
    Ok(())
}

#[cfg(test)]
fn validate_selection(
    document: &CertificateDocument<'_>,
    selected_ids: &[String],
) -> CertificateResult<Vec<SelectionEntry>> {
    validate_selection_scoped(document, selected_ids, ValidationScope::FullDocument)
}

fn validate_selection_scoped(
    document: &CertificateDocument<'_>,
    selected_ids: &[String],
    scope: ValidationScope,
) -> CertificateResult<Vec<SelectionEntry>> {
    validate_selection_shape(selected_ids)?;
    let graph = document.graph;
    let mut known = HashMap::with_capacity(graph.elements.len() + graph.texts.len());
    for (index, element) in graph.elements.iter().enumerate() {
        known.insert(element.exposed.id.as_str(), EventRef::Element(index));
    }
    for (index, text) in graph.texts.iter().enumerate() {
        if known
            .insert(text.exposed.id.as_str(), EventRef::Text(index))
            .is_some()
        {
            return Err(CertificateError::new("graph contains duplicate IDs"));
        }
    }

    let mut seen_ids = HashSet::with_capacity(selected_ids.len());
    let mut selected_events = HashSet::with_capacity(selected_ids.len());
    let mut entries = Vec::with_capacity(selected_ids.len());
    let mut previous_order = None;
    let mut previous_source_end = None;
    for id in selected_ids {
        validate_id(id)?;
        if !seen_ids.insert(id.as_str()) {
            return Err(CertificateError::new("duplicate selection ID"));
        }
        let event = known
            .get(id.as_str())
            .copied()
            .ok_or_else(|| CertificateError::new("unknown selection ID"))?;
        let order = event_order_u64(graph, event)?;
        if previous_order.is_some_and(|previous| order <= previous) {
            return Err(CertificateError::new(
                "selection IDs are not in strict event order",
            ));
        }
        if has_selected_ancestor(graph, event, &selected_events) {
            return Err(CertificateError::new(
                "ancestor-overlapping selections are forbidden",
            ));
        }
        let (kind, start, end) = if scope == ValidationScope::LocalAtomic {
            let EventRef::Element(index) = event else {
                return Err(CertificateError::new(
                    "local atomic certificate requires an element selection",
                ));
            };
            let span = validate_local_complete_subtree(document, index)?;
            (SelectionKind::Element, span.0, span.1)
        } else {
            validate_selected_event(document, event)?
        };
        if previous_source_end.is_some_and(|previous| start < previous) {
            return Err(CertificateError::new(
                "selection source spans overlap or are out of order",
            ));
        }
        selected_events.insert(event);
        entries.push(SelectionEntry {
            kind,
            id: id.clone(),
            order,
            source_start: u64_from_usize(start)?,
            source_end: u64_from_usize(end)?,
        });
        previous_order = Some(order);
        previous_source_end = Some(end);
    }
    Ok(entries)
}

fn validate_local_atomic_selection(
    document: &CertificateDocument<'_>,
    selected_ids: &[String],
    entries: &[SelectionEntry],
) -> CertificateResult<()> {
    let root_index = validate_local_atomic_selection_shape(document, selected_ids)?;
    let entry = entries
        .first()
        .ok_or_else(|| CertificateError::new("local atomic certificate entry is unavailable"))?;
    if entries.len() != 1 || entry.kind != SelectionKind::Element || entry.id != selected_ids[0] {
        return Err(CertificateError::new(
            "local atomic certificate entry disagrees with the selection",
        ));
    }
    let root_span = (
        usize_from_u64(entry.source_start)?,
        usize_from_u64(entry.source_end)?,
    );
    validate_local_atomic_ancestors(document, root_index, root_span)?;
    validate_local_exact_text_provenance(document, root_index, root_span)
}

fn validate_local_atomic_selection_shape(
    document: &CertificateDocument<'_>,
    selected_ids: &[String],
) -> CertificateResult<usize> {
    if selected_ids.len() != 1 {
        return Err(CertificateError::new(
            "local atomic certificate requires exactly one selection",
        ));
    }
    let root_index = document
        .graph
        .element_by_id
        .get(&selected_ids[0])
        .copied()
        .ok_or_else(|| {
            CertificateError::new("local atomic certificate requires an element selection")
        })?;
    let root = &document.graph.elements[root_index];
    if !matches!(root.exposed.tag.as_str(), "pre" | "table") {
        return Err(CertificateError::new(
            "local atomic certificate only supports pre and table elements",
        ));
    }
    Ok(root_index)
}

fn validate_local_atomic_ancestors(
    document: &CertificateDocument<'_>,
    root_index: usize,
    root_span: (usize, usize),
) -> CertificateResult<()> {
    let graph = document.graph;
    let mut parent = graph.elements[root_index].parent_index;
    let mut seen = HashSet::new();
    while let Some(index) = parent {
        if !seen.insert(index) {
            return Err(CertificateError::new(
                "local atomic ancestor chain contains a cycle",
            ));
        }
        let element = graph
            .elements
            .get(index)
            .ok_or_else(|| CertificateError::new("local atomic ancestor is out of bounds"))?;
        if is_atomic_element(graph, index) {
            return Err(CertificateError::new(
                "local atomic selection is nested in another atomic structure",
            ));
        }
        if element.exposed.implicit {
            if !matches!(element.exposed.tag.as_str(), "html" | "head" | "body") {
                return Err(CertificateError::new(
                    "local atomic ancestor requires parser repair",
                ));
            }
        } else {
            let ancestor_span = validate_element_span(document, index)?;
            if ancestor_span.0 > root_span.0 || ancestor_span.1 < root_span.1 {
                return Err(CertificateError::new(
                    "local atomic ancestor does not contain the selected source span",
                ));
            }
        }
        parent = element.parent_index;
    }
    Ok(())
}

fn validate_local_exact_text_provenance(
    document: &CertificateDocument<'_>,
    root_index: usize,
    root_span: (usize, usize),
) -> CertificateResult<()> {
    let fragment = document
        .source
        .get(root_span.0..root_span.1)
        .ok_or_else(|| CertificateError::new("local atomic source span is not UTF-8 aligned"))?;
    let tokenization = tokenize_exact_source_text(fragment, ExactTokenizationPolicy::LocalAtomic)?;
    validate_local_element_token_parity(document, root_index, root_span, &tokenization.elements)?;
    let tokens = &tokenization.texts;
    let token_by_span = tokens
        .iter()
        .map(|token| ((token.start, token.end), token))
        .collect::<HashMap<_, _>>();
    if token_by_span.len() != tokens.len() {
        return Err(CertificateError::new(
            "local atomic tokenizer produced duplicate text spans",
        ));
    }

    let graph = document.graph;
    let mut pending = vec![root_index];
    let mut text_indexes = Vec::new();
    while let Some(index) = pending.pop() {
        for child in &graph.elements[index].children {
            match *child {
                EventRef::Element(child_index) => pending.push(child_index),
                EventRef::Text(text_index) => text_indexes.push(text_index),
            }
        }
    }
    text_indexes.sort_unstable_by_key(|index| graph.texts[*index].exposed.order);
    let allow_table_trivia = graph.elements[root_index].exposed.tag == "table";
    let mut previous_end = None;
    let mut consumed_source_spans = HashSet::with_capacity(text_indexes.len());
    for text_index in text_indexes {
        let Some((start, end)) =
            validate_local_text_span(document, text_index, allow_table_trivia)?
        else {
            continue;
        };
        if start < root_span.0 || end > root_span.1 {
            return Err(CertificateError::new(
                "local atomic text escapes the selected source span",
            ));
        }
        if previous_end.is_some_and(|previous| start < previous) {
            return Err(CertificateError::new(
                "local atomic text spans overlap or are out of order",
            ));
        }
        let relative = (start - root_span.0, end - root_span.0);
        let token = token_by_span.get(&relative).ok_or_else(|| {
            CertificateError::new("local atomic text is not backed by one complete tokenizer token")
        })?;
        if !consumed_source_spans.insert(relative) {
            return Err(CertificateError::new(
                "local atomic source text span is consumed more than once",
            ));
        }
        let text = &graph.texts[text_index];
        let parent = graph
            .elements
            .get(text.parent_index)
            .ok_or_else(|| CertificateError::new("local atomic text parent is out of bounds"))?;
        let parent_start = parent.exposed.source_start.ok_or_else(|| {
            CertificateError::new("local atomic text parent is not source-backed")
        })?;
        if parent_start < root_span.0 || token.parent_start != Some(parent_start - root_span.0) {
            return Err(CertificateError::new(
                "local atomic tokenizer parent disagrees with the DOM parent",
            ));
        }
        let source_slice = fragment.get(relative.0..relative.1).ok_or_else(|| {
            CertificateError::new("local atomic tokenizer span is not UTF-8 aligned")
        })?;
        if source_slice != text.exposed.text {
            return Err(CertificateError::new(
                "local atomic tokenizer bytes do not match decoded DOM text",
            ));
        }
        if source_slice
            .as_bytes()
            .iter()
            .any(|byte| matches!(*byte, b'&' | b'\r' | b'\0'))
        {
            return Err(CertificateError::new(
                "local atomic text requires an unsupported HTML decoding transform",
            ));
        }
        if token.kind == SourceTextTokenKind::RcData && source_slice.as_bytes().contains(&b'<') {
            return Err(CertificateError::new(
                "local atomic RCDATA contains an unsupported less-than transition",
            ));
        }
        previous_end = Some(end);
    }
    for token in tokens {
        if consumed_source_spans.contains(&(token.start, token.end)) {
            continue;
        }
        let source_slice = fragment.get(token.start..token.end).ok_or_else(|| {
            CertificateError::new("local atomic tokenizer span is not UTF-8 aligned")
        })?;
        if allow_table_trivia && is_ascii_html_whitespace(source_slice.as_bytes()) {
            continue;
        }
        return Err(CertificateError::new(
            "local atomic source text is not represented by the selected subtree",
        ));
    }
    Ok(())
}

fn validate_local_element_token_parity(
    document: &CertificateDocument<'_>,
    root_index: usize,
    root_span: (usize, usize),
    source_elements: &[SourceElementToken],
) -> CertificateResult<()> {
    let graph = document.graph;
    let mut pending = vec![root_index];
    let mut explicit_indexes = Vec::new();
    while let Some(index) = pending.pop() {
        let element = graph
            .elements
            .get(index)
            .ok_or_else(|| CertificateError::new("local element index is out of bounds"))?;
        if !element.exposed.implicit {
            explicit_indexes.push(index);
        }
        for child in element.children.iter().rev() {
            if let EventRef::Element(child_index) = *child {
                pending.push(child_index);
            }
        }
    }
    explicit_indexes.sort_unstable_by_key(|index| graph.elements[*index].exposed.source_start);
    if explicit_indexes.len() != source_elements.len() {
        return Err(CertificateError::new(
            "local tokenizer and DOM element event counts disagree",
        ));
    }

    for (index, token) in explicit_indexes.into_iter().zip(source_elements) {
        let element = &graph.elements[index];
        let (Some(start), Some(start_tag_end), Some(end)) = (
            element.exposed.source_start,
            element.exposed.source_start_tag_end,
            element.exposed.source_end,
        ) else {
            return Err(CertificateError::new(
                "local explicit element lacks complete source coordinates",
            ));
        };
        if start < root_span.0 || start_tag_end > root_span.1 || end > root_span.1 {
            return Err(CertificateError::new(
                "local explicit element escapes the selected source span",
            ));
        }
        let expected_parent_start =
            nearest_local_explicit_parent_start(graph, index, root_index, root_span)?;
        if token.tag != element.exposed.tag
            || token.start != start - root_span.0
            || token.start_tag_end != start_tag_end - root_span.0
            || token.end != end - root_span.0
            || token.parent_start != expected_parent_start
        {
            return Err(CertificateError::new(
                "local tokenizer element events disagree with the DOM graph",
            ));
        }
    }
    Ok(())
}

fn nearest_local_explicit_parent_start(
    graph: &Graph,
    index: usize,
    root_index: usize,
    root_span: (usize, usize),
) -> CertificateResult<Option<usize>> {
    if index == root_index {
        return Ok(None);
    }
    let mut parent = graph.elements[index].parent_index;
    let mut seen = HashSet::new();
    while let Some(parent_index) = parent {
        if !seen.insert(parent_index) {
            return Err(CertificateError::new(
                "local explicit parent chain contains a cycle",
            ));
        }
        let parent_element = graph
            .elements
            .get(parent_index)
            .ok_or_else(|| CertificateError::new("local explicit parent is out of bounds"))?;
        if !parent_element.exposed.implicit {
            let start = parent_element.exposed.source_start.ok_or_else(|| {
                CertificateError::new("local explicit parent is not source-backed")
            })?;
            if start < root_span.0 {
                return Ok(None);
            }
            return Ok(Some(start - root_span.0));
        }
        parent = parent_element.parent_index;
    }
    Err(CertificateError::new(
        "local descendant has no explicit selected parent",
    ))
}

fn validate_selected_event(
    document: &CertificateDocument<'_>,
    event: EventRef,
) -> CertificateResult<(SelectionKind, usize, usize)> {
    let graph = document.graph;
    if let Some(atomic_root) = outermost_atomic_ancestor(graph, event) {
        if event != EventRef::Element(atomic_root) {
            return Err(CertificateError::new(
                "table/list/code/math/figure selections must select the whole structure",
            ));
        }
    }
    match event {
        EventRef::Element(index) => {
            let span = validate_complete_subtree(document, index)?;
            Ok((SelectionKind::Element, span.0, span.1))
        }
        EventRef::Text(index) => {
            let span = validate_text_span(document, index)?;
            Ok((SelectionKind::Text, span.0, span.1))
        }
    }
}

fn validate_local_complete_subtree(
    document: &CertificateDocument<'_>,
    root_index: usize,
) -> CertificateResult<(usize, usize)> {
    let graph = document.graph;
    let root_span = validate_element_span(document, root_index)?;
    let allow_table_trivia = graph.elements[root_index].exposed.tag == "table";
    let mut pending = vec![root_index];
    while let Some(index) = pending.pop() {
        let (element_span, content_start) =
            local_element_span(document, index, root_index, root_span)?;
        if !graph.elements[index].exposed.implicit {
            validate_local_start_tag(document, index)?;
        }
        if element_span.0 < root_span.0 || element_span.1 > root_span.1 {
            return Err(CertificateError::new(
                "descendant element escapes selected source span",
            ));
        }
        validate_atomic_structure(graph, index)?;
        let element = &graph.elements[index];
        let mut previous_child_end = content_start;
        for child in &element.children {
            let child_span = match *child {
                EventRef::Element(child_index) => {
                    let (span, _) =
                        local_element_span(document, child_index, root_index, root_span)?;
                    pending.push(child_index);
                    Some(span)
                }
                EventRef::Text(text_index) => {
                    validate_local_text_span(document, text_index, allow_table_trivia)?
                }
            };
            let Some(child_span) = child_span else {
                continue;
            };
            if child_span.0 < content_start
                || child_span.1 > element_span.1
                || child_span.0 < previous_child_end
            {
                return Err(CertificateError::new(
                    "child source spans are incomplete, overlapping, or out of order",
                ));
            }
            previous_child_end = child_span.1;
        }
    }
    Ok(root_span)
}

fn validate_local_start_tag(
    document: &CertificateDocument<'_>,
    index: usize,
) -> CertificateResult<()> {
    let element = document
        .graph
        .elements
        .get(index)
        .ok_or_else(|| CertificateError::new("local start-tag element is out of bounds"))?;
    if element.exposed.implicit {
        return Err(CertificateError::new(
            "implicit elements do not have exact local start tags",
        ));
    }
    let (Some(start), Some(end)) = (
        element.exposed.source_start,
        element.exposed.source_start_tag_end,
    ) else {
        return Err(CertificateError::new(
            "local start-tag source span is unavailable",
        ));
    };
    let tag = document
        .source
        .get(start..end)
        .ok_or_else(|| CertificateError::new("local start tag is not UTF-8 aligned"))?;
    let bytes = tag.as_bytes();
    if bytes.first() != Some(&b'<') || bytes.last() != Some(&b'>') || bytes.len() < 3 {
        return Err(CertificateError::new(
            "local start tag is not source-canonical",
        ));
    }
    if bytes.iter().any(|byte| matches!(*byte, b'\r' | b'\0')) {
        return Err(CertificateError::new(
            "local start tag contains a noncanonical control byte",
        ));
    }
    let mut cursor = 1;
    let name_start = cursor;
    while cursor < bytes.len() && super::is_tag_name_byte(bytes[cursor]) {
        cursor += 1;
    }
    if cursor == name_start || !tag[name_start..cursor].eq_ignore_ascii_case(&element.exposed.tag) {
        return Err(CertificateError::new(
            "local start-tag name disagrees with the DOM",
        ));
    }

    let mut seen_names = HashSet::new();
    let mut retained = HashMap::new();
    loop {
        let before_whitespace = cursor;
        while cursor < bytes.len() && is_html_whitespace_byte(bytes[cursor]) {
            cursor += 1;
        }
        let had_whitespace = cursor > before_whitespace;
        if cursor + 1 == bytes.len() && bytes[cursor] == b'>' {
            break;
        }
        if cursor + 2 == bytes.len() && bytes[cursor..] == *b"/>" {
            if !super::is_void_tag(&element.exposed.tag) {
                return Err(CertificateError::new(
                    "local non-void element uses self-closing syntax",
                ));
            }
            break;
        }
        if !had_whitespace || cursor >= bytes.len().saturating_sub(1) {
            return Err(CertificateError::new(
                "local start-tag attributes are not lexically separated",
            ));
        }

        let attribute_start = cursor;
        while cursor < bytes.len()
            && !is_html_whitespace_byte(bytes[cursor])
            && !matches!(
                bytes[cursor],
                b'/' | b'>' | b'=' | b'\'' | b'"' | b'<' | b'`'
            )
        {
            if !bytes[cursor].is_ascii() || bytes[cursor].is_ascii_control() {
                return Err(CertificateError::new(
                    "local start-tag attribute name is not canonical ASCII",
                ));
            }
            cursor += 1;
        }
        if cursor == attribute_start {
            return Err(CertificateError::new(
                "local start-tag attribute name is invalid",
            ));
        }
        let attribute_name = tag[attribute_start..cursor].to_ascii_lowercase();
        if !seen_names.insert(attribute_name.clone()) {
            return Err(CertificateError::new(
                "local start tag contains a duplicate attribute",
            ));
        }
        while cursor < bytes.len() && is_html_whitespace_byte(bytes[cursor]) {
            cursor += 1;
        }

        let mut value = "";
        if bytes.get(cursor) == Some(&b'=') {
            cursor += 1;
            while cursor < bytes.len() && is_html_whitespace_byte(bytes[cursor]) {
                cursor += 1;
            }
            let Some(first) = bytes.get(cursor).copied() else {
                return Err(CertificateError::new(
                    "local start-tag attribute value is missing",
                ));
            };
            if matches!(first, b'\'' | b'"') {
                let quote = first;
                cursor += 1;
                let value_start = cursor;
                while cursor < bytes.len() && bytes[cursor] != quote {
                    if matches!(bytes[cursor], b'<' | b'\r' | b'\0' | b'&') {
                        return Err(CertificateError::new(
                            "local start-tag attribute requires HTML repair or decoding",
                        ));
                    }
                    cursor += 1;
                }
                if cursor >= bytes.len() {
                    return Err(CertificateError::new(
                        "local start-tag quoted attribute is unterminated",
                    ));
                }
                value = tag.get(value_start..cursor).ok_or_else(|| {
                    CertificateError::new("local attribute value is not UTF-8 aligned")
                })?;
                cursor += 1;
            } else {
                let value_start = cursor;
                while cursor < bytes.len()
                    && !is_html_whitespace_byte(bytes[cursor])
                    && bytes[cursor] != b'>'
                {
                    if matches!(
                        bytes[cursor],
                        b'\'' | b'"' | b'<' | b'=' | b'`' | b'\r' | b'\0' | b'&'
                    ) {
                        return Err(CertificateError::new(
                            "local unquoted attribute requires HTML repair or decoding",
                        ));
                    }
                    cursor += 1;
                }
                if cursor == value_start {
                    return Err(CertificateError::new(
                        "local start-tag attribute value is empty",
                    ));
                }
                value = tag.get(value_start..cursor).ok_or_else(|| {
                    CertificateError::new("local attribute value is not UTF-8 aligned")
                })?;
            }
        }
        if element.attrs.contains_key(&attribute_name) {
            retained.insert(attribute_name, value.to_owned());
        }
    }
    if retained != element.attrs {
        return Err(CertificateError::new(
            "local start-tag retained attributes disagree with the DOM",
        ));
    }
    Ok(())
}

fn validate_local_text_span(
    document: &CertificateDocument<'_>,
    index: usize,
    allow_table_trivia: bool,
) -> CertificateResult<Option<(usize, usize)>> {
    let text = document
        .graph
        .texts
        .get(index)
        .ok_or_else(|| CertificateError::new("text-run index is out of bounds"))?;
    if allow_table_trivia
        && !text.exposed.truncated
        && is_ascii_html_whitespace(text.exposed.text.as_bytes())
    {
        return Ok(None);
    }
    validate_text_span(document, index).map(Some)
}

fn is_ascii_html_whitespace(value: &[u8]) -> bool {
    value
        .iter()
        .all(|byte| matches!(*byte, b' ' | b'\t' | b'\n' | 0x0c))
}

fn is_html_whitespace_byte(value: u8) -> bool {
    matches!(value, b' ' | b'\t' | b'\n' | 0x0c | b'\r')
}

fn local_element_span(
    document: &CertificateDocument<'_>,
    index: usize,
    root_index: usize,
    root_span: (usize, usize),
) -> CertificateResult<((usize, usize), usize)> {
    let element = document
        .graph
        .elements
        .get(index)
        .ok_or_else(|| CertificateError::new("element index is out of bounds"))?;
    if element.exposed.implicit {
        let span = validate_local_implicit_tbody(document, index, root_index, root_span)?;
        return Ok((span, span.0));
    }
    let span = validate_element_span(document, index)?;
    let content_start = element
        .exposed
        .source_start_tag_end
        .ok_or_else(|| CertificateError::new("element content span is unavailable"))?;
    Ok((span, content_start))
}

fn validate_local_implicit_tbody(
    document: &CertificateDocument<'_>,
    index: usize,
    root_index: usize,
    root_span: (usize, usize),
) -> CertificateResult<(usize, usize)> {
    let graph = document.graph;
    let element = graph
        .elements
        .get(index)
        .ok_or_else(|| CertificateError::new("implicit tbody index is out of bounds"))?;
    let root = graph
        .elements
        .get(root_index)
        .ok_or_else(|| CertificateError::new("local atomic root is out of bounds"))?;
    if element.exposed.tag != "tbody"
        || element.parent_index != Some(root_index)
        || root.exposed.tag != "table"
        || element.exposed.source_span_reliable
        || element.exposed.source_start.is_some()
        || element.exposed.source_start_tag_end.is_some()
        || element.exposed.source_end.is_some()
        || !element.attrs.is_empty()
        || element.children.is_empty()
    {
        return Err(CertificateError::new(
            "local atomic subtree requires unsupported parser repair",
        ));
    }

    let mut first_start = None;
    let mut previous_end = None;
    for child in &element.children {
        let row_index = match *child {
            EventRef::Element(row_index) => row_index,
            EventRef::Text(text_index) => {
                if validate_local_text_span(document, text_index, true)?.is_none() {
                    continue;
                }
                return Err(CertificateError::new(
                    "implicit tbody contains a non-row event",
                ));
            }
        };
        let row = graph
            .elements
            .get(row_index)
            .ok_or_else(|| CertificateError::new("implicit tbody row is out of bounds"))?;
        if row.exposed.implicit || row.exposed.tag != "tr" || row.parent_index != Some(index) {
            return Err(CertificateError::new(
                "implicit tbody contains a repaired or non-row child",
            ));
        }
        let row_span = validate_element_span(document, row_index)?;
        if row_span.0 < root_span.0 || row_span.1 > root_span.1 {
            return Err(CertificateError::new(
                "implicit tbody row escapes the selected table",
            ));
        }
        if let Some(end) = previous_end {
            if row_span.0 < end
                || !is_html_inter_row_trivia(document.source.get(end..row_span.0).ok_or_else(
                    || CertificateError::new("implicit tbody inter-row source is unavailable"),
                )?)
            {
                return Err(CertificateError::new(
                    "implicit tbody rows are not source-adjacent",
                ));
            }
        } else {
            first_start = Some(row_span.0);
        }
        previous_end = Some(row_span.1);
    }
    let start = first_start
        .ok_or_else(|| CertificateError::new("implicit tbody has no source-backed rows"))?;
    let end = previous_end
        .ok_or_else(|| CertificateError::new("implicit tbody has no source-backed rows"))?;
    Ok((start, end))
}

fn is_html_inter_row_trivia(value: &str) -> bool {
    let bytes = value.as_bytes();
    let mut cursor = 0;
    while cursor < bytes.len() {
        if bytes[cursor].is_ascii_whitespace() {
            cursor += 1;
            continue;
        }
        if bytes[cursor..].starts_with(b"<!--") {
            let Some(relative_end) = value[cursor + 4..].find("-->") else {
                return false;
            };
            cursor = cursor + 4 + relative_end + 3;
            continue;
        }
        return false;
    }
    true
}

fn validate_complete_subtree(
    document: &CertificateDocument<'_>,
    root_index: usize,
) -> CertificateResult<(usize, usize)> {
    let graph = document.graph;
    let root_span = validate_element_span(document, root_index)?;
    let mut pending = vec![root_index];
    while let Some(index) = pending.pop() {
        let element_span = validate_element_span(document, index)?;
        if element_span.0 < root_span.0 || element_span.1 > root_span.1 {
            return Err(CertificateError::new(
                "descendant element escapes selected source span",
            ));
        }
        validate_atomic_structure(graph, index)?;
        let element = &graph.elements[index];
        let content_start = element
            .exposed
            .source_start_tag_end
            .ok_or_else(|| CertificateError::new("element content span is unavailable"))?;
        let mut previous_child_end = content_start;
        for child in &element.children {
            let child_span = match *child {
                EventRef::Element(child_index) => {
                    pending.push(child_index);
                    validate_element_span(document, child_index)?
                }
                EventRef::Text(text_index) => validate_text_span(document, text_index)?,
            };
            if child_span.0 < content_start
                || child_span.1 > element_span.1
                || child_span.0 < previous_child_end
            {
                return Err(CertificateError::new(
                    "child source spans are incomplete, overlapping, or out of order",
                ));
            }
            previous_child_end = child_span.1;
        }
    }
    Ok(root_span)
}

fn validate_element_span(
    document: &CertificateDocument<'_>,
    index: usize,
) -> CertificateResult<(usize, usize)> {
    let element = document
        .graph
        .elements
        .get(index)
        .ok_or_else(|| CertificateError::new("element index is out of bounds"))?;
    if element.exposed.implicit || !element.exposed.source_span_reliable {
        return Err(CertificateError::new(
            "element subtree does not have a reliable full source span",
        ));
    }
    let (Some(start), Some(start_tag_end), Some(end)) = (
        element.exposed.source_start,
        element.exposed.source_start_tag_end,
        element.exposed.source_end,
    ) else {
        return Err(CertificateError::new(
            "element subtree source span is incomplete",
        ));
    };
    if start >= start_tag_end || start_tag_end > end {
        return Err(CertificateError::new("element source span is invalid"));
    }
    let source = document.source.as_bytes();
    if end > source.len()
        || !document.source.is_char_boundary(start)
        || !document.source.is_char_boundary(start_tag_end)
        || !document.source.is_char_boundary(end)
        || source.get(start) != Some(&b'<')
        || source.get(start_tag_end.saturating_sub(1)) != Some(&b'>')
        || source.get(end.saturating_sub(1)) != Some(&b'>')
    {
        return Err(CertificateError::new(
            "element source span does not match decoded UTF-8 bytes",
        ));
    }
    Ok((start, end))
}

fn validate_text_span(
    document: &CertificateDocument<'_>,
    index: usize,
) -> CertificateResult<(usize, usize)> {
    let text = document
        .graph
        .texts
        .get(index)
        .ok_or_else(|| CertificateError::new("text-run index is out of bounds"))?;
    if text.exposed.truncated || !text.exposed.source_span_reliable {
        return Err(CertificateError::new(
            "text run does not have a reliable complete source span",
        ));
    }
    let (Some(start), Some(end)) = (text.exposed.source_start, text.exposed.source_end) else {
        return Err(CertificateError::new("text-run source span is unavailable"));
    };
    if start >= end
        || end > document.source.len()
        || !document.source.is_char_boundary(start)
        || !document.source.is_char_boundary(end)
    {
        return Err(CertificateError::new("text-run source span is invalid"));
    }
    let source_slice = document
        .source
        .get(start..end)
        .ok_or_else(|| CertificateError::new("text-run source span is not UTF-8 aligned"))?;
    if source_slice != text.exposed.text {
        return Err(CertificateError::new(
            "text-run source bytes do not match decoded text",
        ));
    }
    // The current v2 mapper searches decoded text in raw HTML. A decoded
    // single "&" can otherwise alias the first byte of an entity such as
    // "&amp;". V0 rejects all ampersand-bearing runs until tokenizer-level
    // normalized-to-source maps are available.
    if source_slice.as_bytes().contains(&b'&') {
        return Err(CertificateError::new(
            "ampersand-bearing text runs require tokenizer source maps",
        ));
    }
    Ok((start, end))
}

fn validate_atomic_structure(graph: &Graph, index: usize) -> CertificateResult<()> {
    let element = &graph.elements[index];
    match element.exposed.tag.as_str() {
        "table" => {
            let tables = graph
                .tables
                .iter()
                .filter(|table| table.node_id == element.exposed.id)
                .collect::<Vec<_>>();
            if tables.len() != 1
                || !tables[0].grid_complete
                || graph
                    .cells
                    .iter()
                    .filter(|cell| cell.table_id == tables[0].id)
                    .any(|cell| !cell.grid_complete)
            {
                return Err(CertificateError::new(
                    "table structure is missing or incomplete",
                ));
            }
        }
        "ul" | "ol" | "dl" => {
            let lists = graph
                .lists
                .iter()
                .filter(|list| list.node_id == element.exposed.id)
                .collect::<Vec<_>>();
            if lists.len() != 1 {
                return Err(CertificateError::new("list structure is missing"));
            }
            let known_items = graph
                .list_items
                .iter()
                .filter(|item| item.list_id == lists[0].id)
                .map(|item| item.id.as_str())
                .collect::<HashSet<_>>();
            if lists[0]
                .item_ids
                .iter()
                .any(|id| !known_items.contains(id.as_str()))
                || known_items.len() != lists[0].item_ids.len()
            {
                return Err(CertificateError::new("list structure is incomplete"));
            }
        }
        tag if is_math_element(tag, &element.attrs) => {
            let maths = graph
                .maths
                .iter()
                .filter(|math| math.node_id == element.exposed.id)
                .collect::<Vec<_>>();
            if maths.len() != 1 || !maths[0].source_backed || maths[0].truncated {
                return Err(CertificateError::new(
                    "math structure is not completely source-backed",
                ));
            }
        }
        _ => {}
    }
    Ok(())
}

fn outermost_atomic_ancestor(graph: &Graph, event: EventRef) -> Option<usize> {
    let mut current = match event {
        EventRef::Element(index) => Some(index),
        EventRef::Text(index) => graph.texts.get(index).map(|text| text.parent_index),
    };
    let mut outermost = None;
    while let Some(index) = current {
        if is_atomic_element(graph, index) {
            outermost = Some(index);
        }
        current = graph.elements[index].parent_index;
    }
    outermost
}

fn is_atomic_element(graph: &Graph, index: usize) -> bool {
    let element = &graph.elements[index];
    matches!(
        element.exposed.tag.as_str(),
        "table" | "ul" | "ol" | "dl" | "pre" | "code" | "figure"
    ) || is_math_element(&element.exposed.tag, &element.attrs)
}

fn has_selected_ancestor(graph: &Graph, event: EventRef, selected: &HashSet<EventRef>) -> bool {
    let mut parent = match event {
        EventRef::Element(index) => graph.elements[index].parent_index,
        EventRef::Text(index) => Some(graph.texts[index].parent_index),
    };
    while let Some(index) = parent {
        if selected.contains(&EventRef::Element(index)) {
            return true;
        }
        parent = graph.elements[index].parent_index;
    }
    false
}

fn render_selection(
    document: &CertificateDocument<'_>,
    selected_ids: &[String],
    max_output_bytes: usize,
) -> CertificateResult<String> {
    enforce_serializer_hard_bound(document.graph, selected_ids)?;
    let serialized: NativeIRSerializationV2 = serialize_graph(
        document.graph,
        Some(selected_ids.to_vec()),
        document.source_complete,
        document.document_truncated,
        &[],
    );
    if !serialized.missing_ids.is_empty() || serialized.selected_ids != selected_ids {
        return Err(CertificateError::new(
            "native serializer did not preserve the verified selection",
        ));
    }
    if serialized.truncated || serialized.markdown.len() > max_output_bytes {
        return Err(CertificateError::new(
            "serialized output exceeds the all-or-nothing output limit",
        ));
    }
    Ok(serialized.markdown)
}

fn enforce_serializer_hard_bound(graph: &Graph, selected_ids: &[String]) -> CertificateResult<()> {
    let mut known = HashMap::with_capacity(graph.elements.len() + graph.texts.len());
    for (index, element) in graph.elements.iter().enumerate() {
        known.insert(element.exposed.id.as_str(), EventRef::Element(index));
    }
    for (index, text) in graph.texts.iter().enumerate() {
        known.insert(text.exposed.id.as_str(), EventRef::Text(index));
    }

    let mut included = HashSet::new();
    for id in selected_ids {
        let event = known
            .get(id.as_str())
            .copied()
            .ok_or_else(|| CertificateError::new("unknown selection ID during serialization"))?;
        let mut pending = vec![event];
        while let Some(current) = pending.pop() {
            if !included.insert(current) {
                continue;
            }
            if let EventRef::Element(index) = current {
                pending.extend(graph.elements[index].children.iter().copied());
            }
        }
        let mut parent = match event {
            EventRef::Element(index) => graph.elements[index].parent_index,
            EventRef::Text(index) => Some(graph.texts[index].parent_index),
        };
        while let Some(index) = parent {
            included.insert(EventRef::Element(index));
            parent = graph.elements[index].parent_index;
        }
    }

    let mut text_bytes = 0_u128;
    let mut attribute_bytes = 0_u128;
    let mut exact_line_count = 0_u128;
    let mut element_count = 0_u128;
    let mut maximum_depth = 0_u128;
    let mut included_element_ids = HashSet::new();
    for event in &included {
        match *event {
            EventRef::Element(index) => {
                let element = &graph.elements[index];
                element_count = element_count.saturating_add(1);
                maximum_depth = maximum_depth.max(element.exposed.depth as u128);
                included_element_ids.insert(element.exposed.id.as_str());
                for value in element.attrs.values() {
                    attribute_bytes = attribute_bytes.saturating_add(value.len() as u128);
                    exact_line_count =
                        exact_line_count.saturating_add(
                            value.bytes().filter(|byte| *byte == b'\n').count() as u128,
                        );
                }
            }
            EventRef::Text(index) => {
                let text = &graph.texts[index].exposed.text;
                text_bytes = text_bytes.saturating_add(text.len() as u128);
                exact_line_count = exact_line_count
                    .saturating_add(text.bytes().filter(|byte| *byte == b'\n').count() as u128)
                    .saturating_add(1);
            }
        }
    }

    let mut structure_bytes = 0_u128;
    let mut structure_count = 0_u128;
    for math in &graph.maths {
        if included_element_ids.contains(math.node_id.as_str()) {
            structure_count = structure_count.saturating_add(1);
            for value in [
                Some(math.source_markup.as_str()),
                math.tex.as_deref(),
                math.mathml.as_deref(),
                math.alt_text.as_deref(),
            ]
            .into_iter()
            .flatten()
            {
                structure_bytes = structure_bytes.saturating_add(value.len() as u128);
                exact_line_count = exact_line_count
                    .saturating_add(value.bytes().filter(|byte| *byte == b'\n').count() as u128);
            }
        }
    }
    for table in &graph.tables {
        if included_element_ids.contains(table.node_id.as_str()) {
            structure_count = structure_count
                .saturating_add(1)
                .saturating_add(table.cell_ids.len() as u128);
        }
    }
    for list in &graph.lists {
        if included_element_ids.contains(list.node_id.as_str()) {
            structure_count = structure_count
                .saturating_add(1)
                .saturating_add(list.item_ids.len() as u128);
        }
    }

    // This intentionally overestimates every expansion used by the existing
    // Markdown serializer: HTML entities (<=6 bytes), Markdown escapes,
    // code fences, link/image attributes, per-line list/blockquote prefixes,
    // structure tags, and block separators. The serializer is called only
    // when its complete result is proven to fit the native hard allocation
    // envelope; the caller-specific limit is then checked exactly.
    let possible_lines = exact_line_count
        .saturating_add(element_count.saturating_mul(4))
        .saturating_add(structure_count.saturating_mul(4))
        .saturating_add(1);
    let upper_bound = text_bytes
        .saturating_add(attribute_bytes)
        .saturating_add(structure_bytes)
        .saturating_mul(8)
        .saturating_add(
            possible_lines.saturating_mul(maximum_depth.saturating_mul(8).saturating_add(128)),
        )
        .saturating_add(element_count.saturating_mul(512))
        .saturating_add(structure_count.saturating_mul(512))
        .saturating_add(8_192);
    if upper_bound > HARD_MAX_OUTPUT_BYTES as u128 {
        return Err(CertificateError::new(
            "selection exceeds the conservative native serializer hard bound",
        ));
    }
    Ok(())
}

fn native_certificate(
    encoded: Vec<u8>,
    certificate: &DecodedCertificate,
) -> CertificateResult<NativeSelectionCertificateV0> {
    Ok(NativeSelectionCertificateV0 {
        certificate_digest: hex_digest(&certificate_digest_v0(&encoded)),
        encoded,
        contract_version: CONTRACT_VERSION,
        wire_version: WIRE_VERSION,
        validation_scope: certificate.scope.name(),
        source_digest: hex_digest(&certificate.source_digest),
        graph_digest: hex_digest(&certificate.graph_digest),
        output_digest: hex_digest(&certificate.output_digest),
        output_bytes: usize_from_u64(certificate.output_bytes)?,
        max_output_bytes: usize_from_u64(certificate.max_output_bytes)?,
        selection_count: certificate.entries.len(),
        selected_ids: certificate
            .entries
            .iter()
            .map(|entry| entry.id.clone())
            .collect(),
    })
}

fn encode_certificate(certificate: &DecodedCertificate) -> CertificateResult<Vec<u8>> {
    let output_limit = usize_from_u64(certificate.max_output_bytes)?;
    validate_output_limit(output_limit)?;
    if certificate.output_bytes > certificate.max_output_bytes {
        return Err(CertificateError::new(
            "output length exceeds certificate output limit",
        ));
    }
    if certificate.entries.is_empty() {
        return Err(CertificateError::new("selection must not be empty"));
    }
    if certificate.entries.len() > HARD_MAX_SELECTIONS {
        return Err(CertificateError::new("too many selected events"));
    }
    let estimated = FIXED_HEADER_BYTES
        .checked_add(
            certificate
                .entries
                .iter()
                .try_fold(0usize, |total, entry| {
                    total
                        .checked_add(FIXED_ENTRY_BYTES)
                        .and_then(|value| value.checked_add(entry.id.len()))
                })
                .ok_or_else(|| CertificateError::new("certificate size overflow"))?,
        )
        .ok_or_else(|| CertificateError::new("certificate size overflow"))?;
    if estimated > HARD_MAX_CERTIFICATE_BYTES {
        return Err(CertificateError::new("certificate exceeds hard byte limit"));
    }

    let mut encoded = Vec::with_capacity(estimated);
    encoded.extend_from_slice(WIRE_MAGIC);
    push_u16(&mut encoded, WIRE_VERSION);
    push_u16(&mut encoded, certificate.scope.wire_flags());
    encoded.extend_from_slice(&certificate.source_digest);
    encoded.extend_from_slice(&certificate.graph_digest);
    encoded.extend_from_slice(&certificate.output_digest);
    push_u64(&mut encoded, certificate.output_bytes);
    push_u64(&mut encoded, certificate.max_output_bytes);
    push_u32(
        &mut encoded,
        u32::try_from(certificate.entries.len())
            .map_err(|_| CertificateError::new("selection count is not encodable"))?,
    );
    let mut seen = HashSet::with_capacity(certificate.entries.len());
    let mut previous_order = None;
    let mut previous_source_end = None;
    for entry in &certificate.entries {
        validate_id(&entry.id)?;
        if !seen.insert(entry.id.as_str()) {
            return Err(CertificateError::new("duplicate selection ID"));
        }
        if entry.source_start >= entry.source_end {
            return Err(CertificateError::new("selection source span is invalid"));
        }
        if previous_order.is_some_and(|previous| entry.order <= previous) {
            return Err(CertificateError::new(
                "selection entries are not in strict event order",
            ));
        }
        if previous_source_end.is_some_and(|previous| entry.source_start < previous) {
            return Err(CertificateError::new(
                "selection source spans overlap or are out of order",
            ));
        }
        encoded.push(entry.kind.wire());
        encoded.push(ENTRY_RESERVED);
        push_u16(
            &mut encoded,
            u16::try_from(entry.id.len())
                .map_err(|_| CertificateError::new("selection ID is too long"))?,
        );
        push_u64(&mut encoded, entry.order);
        push_u64(&mut encoded, entry.source_start);
        push_u64(&mut encoded, entry.source_end);
        encoded.extend_from_slice(entry.id.as_bytes());
        previous_order = Some(entry.order);
        previous_source_end = Some(entry.source_end);
    }
    if encoded.len() != estimated {
        return Err(CertificateError::new(
            "internal canonical certificate size mismatch",
        ));
    }
    Ok(encoded)
}

fn decode_certificate(encoded: &[u8]) -> CertificateResult<DecodedCertificate> {
    if encoded.len() > HARD_MAX_CERTIFICATE_BYTES {
        return Err(CertificateError::new("certificate exceeds hard byte limit"));
    }
    if encoded.len() < FIXED_HEADER_BYTES {
        return Err(CertificateError::new("certificate is truncated"));
    }
    let mut reader = Reader::new(encoded);
    if reader.take(8)? != WIRE_MAGIC {
        return Err(CertificateError::new("certificate magic mismatch"));
    }
    if reader.u16()? != WIRE_VERSION {
        return Err(CertificateError::new(
            "unsupported certificate wire version",
        ));
    }
    let scope = ValidationScope::from_wire_flags(reader.u16()?)?;
    let source_digest = reader.digest()?;
    let graph_digest = reader.digest()?;
    let output_digest = reader.digest()?;
    let output_bytes = reader.u64()?;
    let max_output_bytes = reader.u64()?;
    validate_output_limit(usize_from_u64(max_output_bytes)?)?;
    if output_bytes > max_output_bytes {
        return Err(CertificateError::new(
            "output length exceeds certificate output limit",
        ));
    }
    let selection_count = usize::try_from(reader.u32()?)
        .map_err(|_| CertificateError::new("selection count does not fit this platform"))?;
    if selection_count == 0 {
        return Err(CertificateError::new("selection must not be empty"));
    }
    if selection_count > HARD_MAX_SELECTIONS {
        return Err(CertificateError::new("too many selected events"));
    }
    if selection_count
        .checked_mul(FIXED_ENTRY_BYTES)
        .is_none_or(|minimum| minimum > reader.remaining())
    {
        return Err(CertificateError::new("certificate entries are truncated"));
    }

    let mut entries = Vec::with_capacity(selection_count);
    let mut seen = HashSet::with_capacity(selection_count);
    let mut previous_order = None;
    let mut previous_source_end = None;
    for _ in 0..selection_count {
        let kind = SelectionKind::from_wire(reader.u8()?)?;
        if reader.u8()? != ENTRY_RESERVED {
            return Err(CertificateError::new(
                "non-zero selection-entry reserved byte",
            ));
        }
        let id_len = usize::from(reader.u16()?);
        if id_len == 0 || id_len > HARD_MAX_ID_BYTES {
            return Err(CertificateError::new("selection ID length is invalid"));
        }
        let order = reader.u64()?;
        let source_start = reader.u64()?;
        let source_end = reader.u64()?;
        let id = std::str::from_utf8(reader.take(id_len)?)
            .map_err(|_| CertificateError::new("selection ID is not UTF-8"))?
            .to_owned();
        validate_id(&id)?;
        if !seen.insert(id.clone()) {
            return Err(CertificateError::new("duplicate selection ID"));
        }
        if source_start >= source_end {
            return Err(CertificateError::new("selection source span is invalid"));
        }
        if previous_order.is_some_and(|previous| order <= previous) {
            return Err(CertificateError::new(
                "selection entries are not in strict event order",
            ));
        }
        if previous_source_end.is_some_and(|previous| source_start < previous) {
            return Err(CertificateError::new(
                "selection source spans overlap or are out of order",
            ));
        }
        entries.push(SelectionEntry {
            kind,
            id,
            order,
            source_start,
            source_end,
        });
        previous_order = Some(order);
        previous_source_end = Some(source_end);
    }
    if reader.remaining() != 0 {
        return Err(CertificateError::new("trailing certificate bytes"));
    }
    let certificate = DecodedCertificate {
        scope,
        source_digest,
        graph_digest,
        output_digest,
        output_bytes,
        max_output_bytes,
        entries,
    };
    if encode_certificate(&certificate)? != encoded {
        return Err(CertificateError::new(
            "certificate is not canonically encoded",
        ));
    }
    Ok(certificate)
}

fn validate_id(id: &str) -> CertificateResult<()> {
    if id.is_empty()
        || id.len() > HARD_MAX_ID_BYTES
        || !id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
    {
        return Err(CertificateError::new("selection ID is not canonical"));
    }
    Ok(())
}

fn ordered_events(graph: &Graph) -> CertificateResult<Vec<EventRef>> {
    let count = graph
        .elements
        .len()
        .checked_add(graph.texts.len())
        .ok_or_else(|| CertificateError::new("graph event count overflow"))?;
    let mut ordered = vec![None; count];
    for (index, element) in graph.elements.iter().enumerate() {
        place_ordered_event(
            &mut ordered,
            element.exposed.order,
            EventRef::Element(index),
        )?;
    }
    for (index, text) in graph.texts.iter().enumerate() {
        place_ordered_event(&mut ordered, text.exposed.order, EventRef::Text(index))?;
    }
    ordered
        .into_iter()
        .map(|event| event.ok_or_else(|| CertificateError::new("graph event order has a gap")))
        .collect()
}

fn place_ordered_event(
    ordered: &mut [Option<EventRef>],
    order: usize,
    event: EventRef,
) -> CertificateResult<()> {
    let slot = ordered
        .get_mut(order)
        .ok_or_else(|| CertificateError::new("graph event order is out of bounds"))?;
    if slot.replace(event).is_some() {
        return Err(CertificateError::new("graph event order is duplicated"));
    }
    Ok(())
}

fn event_order_u64(graph: &Graph, event: EventRef) -> CertificateResult<u64> {
    let order = match event {
        EventRef::Element(index) => {
            graph
                .elements
                .get(index)
                .ok_or_else(|| CertificateError::new("element index is out of bounds"))?
                .exposed
                .order
        }
        EventRef::Text(index) => {
            graph
                .texts
                .get(index)
                .ok_or_else(|| CertificateError::new("text-run index is out of bounds"))?
                .exposed
                .order
        }
    };
    u64_from_usize(order)
}

fn graph_digest(document: &CertificateDocument<'_>) -> CertificateResult<[u8; 32]> {
    let graph = document.graph;
    validate_graph_topology(graph)?;
    validate_structure_references(graph)?;
    let ordered = ordered_events(graph)?;
    let mut digest = CanonicalDigest::new(b"clusy-ordered-dom-graph-digest-v0");
    digest.string(super::SCHEMA_VERSION);
    digest.string(super::SERIALIZATION_CONTRACT);
    digest.bytes(&source_digest_v0(document.source.as_bytes()));
    digest.bool(document.source_complete);
    digest.bool(document.source_mapping_complete);
    digest.bool(document.document_truncated);
    digest.usize(document.parse_error_count)?;

    digest.usize(graph.roots.len())?;
    for root in &graph.roots {
        digest.event_ref(graph, *root)?;
    }
    digest.usize(ordered.len())?;
    for event in ordered {
        match event {
            EventRef::Element(index) => {
                digest.u8(ENTRY_KIND_ELEMENT);
                let element = &graph.elements[index];
                digest.string(&element.exposed.id);
                digest.usize(element.exposed.order)?;
                digest.option_event_ref(graph, element.parent_index.map(EventRef::Element))?;
                digest.event_refs(graph, &element.children)?;
                digest.option_string(element.exposed.parent_id.as_deref());
                digest.strings(&element.exposed.child_ids)?;
                digest.strings(&element.exposed.text_run_ids)?;
                digest.string(&element.exposed.tag);
                digest.string(&element.exposed.role);
                digest.string(&element.exposed.path);
                digest.usize(element.exposed.depth)?;
                digest.bool(element.exposed.block);
                digest.bool(element.exposed.preserve_whitespace);
                digest.bool(element.exposed.implicit);
                digest.option_usize(element.exposed.source_start)?;
                digest.option_usize(element.exposed.source_start_tag_end)?;
                digest.option_usize(element.exposed.source_end)?;
                digest.bool(element.exposed.source_span_reliable);
                digest.option_u8(element.exposed.heading_level);
                digest.option_string(element.exposed.href.as_deref());
                digest.option_string(element.exposed.src.as_deref());
                digest.option_string(element.exposed.alt.as_deref());
                digest.option_string(element.exposed.language.as_deref());
                let mut attrs = element.attrs.iter().collect::<Vec<_>>();
                attrs.sort_unstable_by(|left, right| left.0.cmp(right.0));
                digest.usize(attrs.len())?;
                for (name, value) in attrs {
                    digest.string(name);
                    digest.string(value);
                }
            }
            EventRef::Text(index) => {
                digest.u8(ENTRY_KIND_TEXT);
                let record = &graph.texts[index];
                let text = &record.exposed;
                digest.string(&text.id);
                digest.usize(text.order)?;
                digest.event_ref(graph, EventRef::Element(record.parent_index))?;
                digest.string(&text.parent_id);
                digest.string(&text.path);
                digest.string(&text.text);
                digest.bool(text.preserve_whitespace);
                digest.usize(text.original_bytes)?;
                digest.usize(text.stored_bytes)?;
                digest.bool(text.truncated);
                digest.option_usize(text.source_start)?;
                digest.option_usize(text.source_end)?;
                digest.bool(text.source_span_reliable);
            }
        }
    }

    let mut element_index = graph.element_by_id.iter().collect::<Vec<_>>();
    element_index.sort_unstable_by(|left, right| left.0.cmp(right.0));
    digest.usize(element_index.len())?;
    for (id, index) in element_index {
        digest.string(id);
        digest.usize(*index)?;
    }

    digest.usize(graph.tables.len())?;
    for table in &graph.tables {
        digest.string(&table.id);
        digest.string(&table.node_id);
        digest.usize(table.order)?;
        digest.usize(table.row_count)?;
        digest.usize(table.column_count)?;
        digest.strings(&table.cell_ids)?;
        digest.bool(table.grid_complete);
    }
    digest.usize(graph.cells.len())?;
    for cell in &graph.cells {
        digest.string(&cell.id);
        digest.string(&cell.node_id);
        digest.string(&cell.table_id);
        digest.usize(cell.order)?;
        digest.usize(cell.row_index)?;
        digest.usize(cell.column_index)?;
        digest.usize(cell.row_span)?;
        digest.usize(cell.column_span)?;
        digest.string(&cell.row_group);
        digest.bool(cell.header);
        digest.string(&cell.scope);
        digest.strings(&cell.text_run_ids)?;
        digest.bool(cell.grid_complete);
    }
    digest.usize(graph.lists.len())?;
    for list in &graph.lists {
        digest.string(&list.id);
        digest.string(&list.node_id);
        digest.usize(list.order)?;
        digest.string(&list.kind);
        digest.usize(list.depth)?;
        digest.option_i64(list.start);
        digest.bool(list.reversed);
        digest.string(&list.marker_type);
        digest.strings(&list.item_ids)?;
    }
    digest.usize(graph.list_items.len())?;
    for item in &graph.list_items {
        digest.string(&item.id);
        digest.string(&item.node_id);
        digest.string(&item.list_id);
        digest.usize(item.order)?;
        digest.usize(item.depth)?;
        digest.usize(item.index)?;
        digest.string(&item.kind);
        digest.option_i64(item.ordinal);
        digest.option_i64(item.explicit_value);
        digest.strings(&item.text_run_ids)?;
    }
    digest.usize(graph.maths.len())?;
    for math in &graph.maths {
        digest.string(&math.id);
        digest.string(&math.node_id);
        digest.usize(math.order)?;
        digest.string(&math.format);
        digest.string(&math.display);
        digest.option_string(math.tex.as_deref());
        digest.option_string(math.mathml.as_deref());
        digest.string(&math.source_markup);
        digest.option_string(math.alt_text.as_deref());
        digest.bool(math.source_backed);
        digest.bool(math.truncated);
    }
    Ok(digest.finish())
}

struct CanonicalDigest(Sha256);

impl CanonicalDigest {
    fn new(domain: &[u8]) -> Self {
        let mut inner = Sha256::new();
        inner.update(
            u64::try_from(domain.len())
                .expect("fixed digest domain length fits u64")
                .to_be_bytes(),
        );
        inner.update(domain);
        Self(inner)
    }

    fn finish(self) -> [u8; 32] {
        self.0.finalize().into()
    }

    fn u8(&mut self, value: u8) {
        self.0.update([value]);
    }

    fn u64(&mut self, value: u64) {
        self.0.update(value.to_be_bytes());
    }

    fn usize(&mut self, value: usize) -> CertificateResult<()> {
        self.u64(u64_from_usize(value)?);
        Ok(())
    }

    fn bool(&mut self, value: bool) {
        self.u8(u8::from(value));
    }

    fn bytes(&mut self, value: &[u8]) {
        self.u64(u64::try_from(value.len()).expect("slice length fits u64 on supported platforms"));
        self.0.update(value);
    }

    fn string(&mut self, value: &str) {
        self.bytes(value.as_bytes());
    }

    fn strings(&mut self, values: &[String]) -> CertificateResult<()> {
        self.usize(values.len())?;
        for value in values {
            self.string(value);
        }
        Ok(())
    }

    fn option_string(&mut self, value: Option<&str>) {
        self.bool(value.is_some());
        if let Some(value) = value {
            self.string(value);
        }
    }

    fn option_usize(&mut self, value: Option<usize>) -> CertificateResult<()> {
        self.bool(value.is_some());
        if let Some(value) = value {
            self.usize(value)?;
        }
        Ok(())
    }

    fn option_u8(&mut self, value: Option<u8>) {
        self.bool(value.is_some());
        if let Some(value) = value {
            self.u8(value);
        }
    }

    fn option_i64(&mut self, value: Option<i64>) {
        self.bool(value.is_some());
        if let Some(value) = value {
            self.0.update(value.to_be_bytes());
        }
    }

    fn event_ref(&mut self, graph: &Graph, event: EventRef) -> CertificateResult<()> {
        match event {
            EventRef::Element(index) => {
                self.u8(ENTRY_KIND_ELEMENT);
                self.string(
                    &graph
                        .elements
                        .get(index)
                        .ok_or_else(|| CertificateError::new("root element is out of bounds"))?
                        .exposed
                        .id,
                );
            }
            EventRef::Text(index) => {
                self.u8(ENTRY_KIND_TEXT);
                self.string(
                    &graph
                        .texts
                        .get(index)
                        .ok_or_else(|| CertificateError::new("root text run is out of bounds"))?
                        .exposed
                        .id,
                );
            }
        }
        Ok(())
    }

    fn option_event_ref(
        &mut self,
        graph: &Graph,
        event: Option<EventRef>,
    ) -> CertificateResult<()> {
        self.bool(event.is_some());
        if let Some(event) = event {
            self.event_ref(graph, event)?;
        }
        Ok(())
    }

    fn event_refs(&mut self, graph: &Graph, events: &[EventRef]) -> CertificateResult<()> {
        self.usize(events.len())?;
        for event in events {
            self.event_ref(graph, *event)?;
        }
        Ok(())
    }
}

struct Reader<'a> {
    value: &'a [u8],
    cursor: usize,
}

impl<'a> Reader<'a> {
    fn new(value: &'a [u8]) -> Self {
        Self { value, cursor: 0 }
    }

    fn remaining(&self) -> usize {
        self.value.len().saturating_sub(self.cursor)
    }

    fn take(&mut self, length: usize) -> CertificateResult<&'a [u8]> {
        let end = self
            .cursor
            .checked_add(length)
            .ok_or_else(|| CertificateError::new("certificate cursor overflow"))?;
        let result = self
            .value
            .get(self.cursor..end)
            .ok_or_else(|| CertificateError::new("certificate is truncated"))?;
        self.cursor = end;
        Ok(result)
    }

    fn u8(&mut self) -> CertificateResult<u8> {
        Ok(self.take(1)?[0])
    }

    fn u16(&mut self) -> CertificateResult<u16> {
        let value = self.take(2)?;
        Ok(u16::from_be_bytes([value[0], value[1]]))
    }

    fn u32(&mut self) -> CertificateResult<u32> {
        let value = self.take(4)?;
        Ok(u32::from_be_bytes([value[0], value[1], value[2], value[3]]))
    }

    fn u64(&mut self) -> CertificateResult<u64> {
        let value = self.take(8)?;
        Ok(u64::from_be_bytes([
            value[0], value[1], value[2], value[3], value[4], value[5], value[6], value[7],
        ]))
    }

    fn digest(&mut self) -> CertificateResult<[u8; 32]> {
        let mut output = [0_u8; 32];
        output.copy_from_slice(self.take(32)?);
        Ok(output)
    }
}

fn framed_digest(domain: &[u8], value: &[u8]) -> [u8; 32] {
    let mut digest = CanonicalDigest::new(domain);
    digest.bytes(value);
    digest.finish()
}

fn source_digest_v0(value: &[u8]) -> [u8; 32] {
    framed_digest(b"clusy-selection-certificate-source-v0", value)
}

fn output_digest_v0(value: &[u8]) -> [u8; 32] {
    framed_digest(b"clusy-selection-certificate-output-v0", value)
}

fn certificate_digest_v0(value: &[u8]) -> [u8; 32] {
    framed_digest(b"clusy-selection-certificate-wire-v0", value)
}

fn hex_digest(value: &[u8; 32]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(64);
    for byte in value {
        output.push(char::from(HEX[usize::from(byte >> 4)]));
        output.push(char::from(HEX[usize::from(byte & 0x0f)]));
    }
    output
}

fn push_u16(output: &mut Vec<u8>, value: u16) {
    output.extend_from_slice(&value.to_be_bytes());
}

fn push_u32(output: &mut Vec<u8>, value: u32) {
    output.extend_from_slice(&value.to_be_bytes());
}

fn push_u64(output: &mut Vec<u8>, value: u64) {
    output.extend_from_slice(&value.to_be_bytes());
}

fn u64_from_usize(value: usize) -> CertificateResult<u64> {
    u64::try_from(value).map_err(|_| CertificateError::new("value does not fit canonical u64"))
}

fn usize_from_u64(value: u64) -> CertificateResult<usize> {
    usize::try_from(value).map_err(|_| CertificateError::new("value does not fit this platform"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::document_ir_v2::{
        build_ir_v2, BuildResult, LimitsV2, DEFAULT_MAX_DEPTH, DEFAULT_MAX_ELEMENTS,
        DEFAULT_MAX_INPUT_BYTES, DEFAULT_MAX_MATH_BYTES, DEFAULT_MAX_NODES,
        DEFAULT_MAX_TABLE_COLUMNS, DEFAULT_MAX_TEXT_RUNS, DEFAULT_MAX_TEXT_RUN_BYTES,
        DEFAULT_MAX_TOTAL_TEXT_BYTES,
    };

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

    fn view(result: &BuildResult) -> CertificateDocument<'_> {
        CertificateDocument {
            graph: &result.graph,
            source: &result.source,
            source_complete: !result.input_truncated,
            source_mapping_complete: result.unmapped_explicit_element_count == 0,
            document_truncated: result.is_truncated(),
            parse_error_count: result.parse_error_count,
        }
    }

    fn id_for_text(result: &BuildResult, needle: &str) -> String {
        result
            .graph
            .texts
            .iter()
            .find(|text| text.exposed.text == needle)
            .unwrap()
            .exposed
            .id
            .clone()
    }

    fn id_for_tag(result: &BuildResult, tag: &str) -> String {
        result
            .graph
            .elements
            .iter()
            .find(|element| element.exposed.tag == tag)
            .unwrap()
            .exposed
            .id
            .clone()
    }

    #[test]
    fn canonical_round_trip_has_stable_golden_digests() {
        let html = "<!doctype html><html><head><title>fixture</title></head><body><main><p>你好 😀 café</p><p>second</p></main></body></html>";
        let first = build(html);
        let second = build(html);
        let ids = vec![
            id_for_text(&first, "你好 😀 café"),
            id_for_text(&first, "second"),
        ];
        let certificate = create_certificate(&view(&first), &ids, 4_096).unwrap();
        let encoded = encode_certificate(&certificate).unwrap();

        assert_eq!(encoded.len(), 226);
        assert_eq!(certificate.output_bytes, 25);
        assert_eq!(decode_certificate(&encoded).unwrap(), certificate);
        assert_eq!(
            graph_digest(&view(&first)).unwrap(),
            graph_digest(&view(&second)).unwrap()
        );
        assert_eq!(
            hex_digest(&certificate.source_digest),
            "92bd546cd347def109d6be9932eee4925591d228020f69bc7ae14d0dd1e1576c"
        );
        assert_eq!(
            hex_digest(&certificate.graph_digest),
            "bca59063c57f985b414ade0569d9c0d1ca6e3f7e9731f3a834f800fa109d9afb"
        );
        assert_eq!(
            hex_digest(&certificate.output_digest),
            "90b21f56c118e1a4ccd2d15eab75c98b4517d057a50c2731820243d17dcb35ce"
        );
        // This fixture pins field ordering, integer widths, and digest domains.
        assert_eq!(
            hex_digest(&certificate_digest_v0(&encoded)),
            "f67b4854e7cbe71da87723691da20895d728ee9f24127d100975f368686135ee"
        );
    }

    #[test]
    fn strict_decoder_rejects_trailing_truncated_reserved_and_oversize_inputs() {
        let result = build(
            "<!doctype html><html><head><title>x</title></head><body><p>safe</p></body></html>",
        );
        let certificate =
            create_certificate(&view(&result), &[id_for_text(&result, "safe")], 1_024).unwrap();
        let encoded = encode_certificate(&certificate).unwrap();

        let mut trailing = encoded.clone();
        trailing.push(0);
        assert!(decode_certificate(&trailing)
            .unwrap_err()
            .0
            .contains("trailing"));
        for end in 0..encoded.len() {
            assert!(decode_certificate(&encoded[..end]).is_err());
        }
        for suffix in u8::MIN..=u8::MAX {
            let mut value = encoded.clone();
            value.push(suffix);
            assert!(decode_certificate(&value).is_err());
        }
        let mut reserved = encoded.clone();
        reserved[FIXED_HEADER_BYTES + 1] = 1;
        assert!(decode_certificate(&reserved)
            .unwrap_err()
            .0
            .contains("reserved"));
        assert!(decode_certificate(&vec![0; HARD_MAX_CERTIFICATE_BYTES + 1])
            .unwrap_err()
            .0
            .contains("hard byte limit"));
    }

    #[test]
    fn duplicate_order_span_unknown_and_ancestor_overlap_fail_closed() {
        let result = build(
            "<!doctype html><html><head><title>x</title></head><body><main><p>one</p><p>two</p></main></body></html>",
        );
        let one = id_for_text(&result, "one");
        let two = id_for_text(&result, "two");
        let main = id_for_tag(&result, "main");

        assert!(validate_selection(&view(&result), &[one.clone(), one.clone()]).is_err());
        assert!(validate_selection(&view(&result), &[two, one.clone()]).is_err());
        assert!(validate_selection(&view(&result), &["unknown".to_owned()]).is_err());
        assert!(validate_selection(&view(&result), &[main, one]).is_err());

        let mut entries = validate_selection(
            &view(&result),
            &[id_for_text(&result, "one"), id_for_text(&result, "two")],
        )
        .unwrap();
        entries[1].source_start = entries[0].source_end - 1;
        let malformed = DecodedCertificate {
            scope: ValidationScope::FullDocument,
            source_digest: [0; 32],
            graph_digest: [0; 32],
            output_digest: [0; 32],
            output_bytes: 0,
            max_output_bytes: 1_024,
            entries,
        };
        assert!(encode_certificate(&malformed).is_err());
    }

    #[test]
    fn source_span_order_must_match_native_event_order() {
        let mut result = build(
            "<!doctype html><html><head><title>x</title></head><body><p>first</p><p>second</p></body></html>",
        );
        let first_index = result
            .graph
            .texts
            .iter()
            .position(|text| text.exposed.text == "first")
            .unwrap();
        let second_index = result
            .graph
            .texts
            .iter()
            .position(|text| text.exposed.text == "second")
            .unwrap();
        let first_order = result.graph.texts[first_index].exposed.order;
        result.graph.texts[first_index].exposed.order =
            result.graph.texts[second_index].exposed.order;
        result.graph.texts[second_index].exposed.order = first_order;

        assert!(validate_document(&view(&result))
            .unwrap_err()
            .0
            .contains("event order"));
    }

    #[test]
    fn whole_atomic_structures_are_eligible_but_descendants_are_not() {
        let html = "<!doctype html><html><head><title>x</title></head><body><main><table><tbody><tr><td>A</td></tr></tbody></table><ol><li>B</li></ol><pre><code>C</code></pre><math><mi>x</mi></math><figure><figcaption>D</figcaption></figure></main></body></html>";
        let result = build(html);

        for tag in ["table", "ol", "pre", "math", "figure"] {
            let id = id_for_tag(&result, tag);
            create_certificate(&view(&result), &[id], 16_384).unwrap();
        }
        for text in ["A", "B", "C", "x", "D"] {
            let id = id_for_text(&result, text);
            assert!(create_certificate(&view(&result), &[id], 16_384).is_err());
        }
        assert!(
            create_certificate(&view(&result), &[id_for_tag(&result, "code")], 16_384).is_err()
        );
    }

    #[test]
    fn hostile_decoder_bytes_never_bypass_canonical_validation() {
        let mut state = 0x9e37_79b9_7f4a_7c15_u64;
        for length in 0..=1_024 {
            let mut value = vec![0_u8; length];
            for byte in &mut value {
                state ^= state << 13;
                state ^= state >> 7;
                state ^= state << 17;
                *byte = state.to_le_bytes()[0];
            }
            assert!(decode_certificate(&value).is_err());
        }
    }

    #[test]
    fn ambiguous_entities_crlf_repeated_text_foster_and_truncation_reject() {
        for (html, needle) in [
            (
                "<!doctype html><html><head><title>x</title></head><body><p>&amp;</p></body></html>",
                "&",
            ),
            (
                "<!doctype html><html><head><title>x</title></head><body><p>a\r\nb</p></body></html>",
                "a\nb",
            ),
            (
                "<!doctype html><html><head><title>x</title></head><body><p>same<em>x</em>same</p></body></html>",
                "same",
            ),
            (
                "<!doctype html><html><head><title>x</title></head><body><table>foster<tr><td>x</td></tr></table></body></html>",
                "foster",
            ),
        ] {
            let result = build(html);
            let selected_id = id_for_text(&result, needle);
            assert!(
                validate_document(&view(&result)).is_err()
                    || validate_selection(&view(&result), &[selected_id]).is_err(),
                "unexpectedly eligible adversarial text: {needle:?}"
            );
        }

        let html = "<p>0123456789</p>";
        let mut tiny = limits();
        tiny.max_text_run_bytes = 4;
        let truncated = build_ir_v2(html.to_owned(), html.len(), false, tiny);
        assert!(validate_document(&view(&truncated)).is_err());
    }

    #[test]
    fn tokenizer_provenance_rejects_comment_hidden_and_normalization_aliases() {
        for (html, needle) in [
            (
                "<!doctype html><html><head><title>x</title></head><body><p>&#65;<!--A--></p></body></html>",
                "A",
            ),
            (
                "<!doctype html><html><head><title>x</title></head><body><p>&copy;<!--©--></p></body></html>",
                "©",
            ),
            (
                "<!doctype html><html><head><title>x</title></head><body><p>\r\n<!--\n--></p></body></html>",
                "\n",
            ),
            (
                "<!doctype html><html><head><title>x</title></head><body><p>&#65;<span hidden>A</span></p></body></html>",
                "A",
            ),
        ] {
            let result = build(html);
            let selected_id = id_for_text(&result, needle);
            assert!(
                create_certificate(&view(&result), &[selected_id], 1_024).is_err(),
                "unexpectedly certified transformed or non-text bytes: {html:?}"
            );
        }
    }

    #[test]
    fn tokenizer_provenance_accepts_distinct_complete_literal_tokens() {
        let result = build(
            "<!doctype html><html><head><title>plain title</title></head><body><p>safe<!-- split -->tail</p></body></html>",
        );
        let selected_ids = vec![id_for_text(&result, "safe"), id_for_text(&result, "tail")];
        let certificate = create_certificate(&view(&result), &selected_ids, 1_024).unwrap();
        let replay = verify_and_replay(
            &view(&result),
            &encode_certificate(&certificate).unwrap(),
            1_024,
        )
        .unwrap();
        assert_eq!(replay.markdown, "safetail");
    }

    #[test]
    fn local_atomic_rejects_every_global_parse_error() {
        let result = build(
            "<!doctype html><html><head><title>x</title></head><body>\
             <div><span>broken</div><pre><code>safe</code></pre></body></html>",
        );
        assert!(result.parse_error_count > 0);
        let error =
            create_local_atomic_certificate(&view(&result), &[id_for_tag(&result, "pre")], 1_024)
                .unwrap_err();
        assert!(error.0.contains("HTML parse errors"));
    }

    #[test]
    fn local_atomic_tokenizer_enforces_exact_end_tags_and_no_unpaired_markup() {
        assert!(tokenize_exact_source_text(
            "<pre><code>x</CoDe \t\n></PrE\u{c}>",
            ExactTokenizationPolicy::LocalAtomic,
        )
        .is_ok());
        for fragment in [
            "<pre><code>x</code data-x></pre>",
            "<pre><code>x</code\r></pre>",
            "<pre><code>x</code\0></pre>",
            "<pre><code>x</code/></pre>",
            "<pre><code>x</code><!--hidden--></pre>",
            "<pre><code>x</code><!bogus></pre>",
            "<pre><code>x</code><?target?></pre>",
        ] {
            assert!(
                tokenize_exact_source_text(fragment, ExactTokenizationPolicy::LocalAtomic).is_err(),
                "unexpectedly accepted local fragment: {fragment:?}",
            );
        }
    }

    #[test]
    fn local_atomic_element_event_parity_rejects_graph_near_match() {
        let mut result = build(
            "<!doctype html><html><head><title>x</title></head><body>\
             <pre><code>safe</code></pre></body></html>",
        );
        let code_index = result
            .graph
            .elements
            .iter()
            .position(|element| element.exposed.tag == "code")
            .unwrap();
        result.graph.elements[code_index].exposed.source_end = result.graph.elements[code_index]
            .exposed
            .source_end
            .map(|value| value - 1);
        assert!(create_local_atomic_certificate(
            &view(&result),
            &[id_for_tag(&result, "pre")],
            1_024,
        )
        .is_err());
    }

    #[test]
    fn internal_topology_and_indices_are_validated_and_digest_bound() {
        let result = build(
            "<!doctype html><html><head><title>x</title></head><body><main><p>one</p><p>two</p></main></body></html>",
        );
        let original_digest = graph_digest(&view(&result)).unwrap();

        let mut missing_child = build(
            "<!doctype html><html><head><title>x</title></head><body><main><p>one</p><p>two</p></main></body></html>",
        );
        let main_index = missing_child
            .graph
            .elements
            .iter()
            .position(|element| element.exposed.tag == "main")
            .unwrap();
        missing_child.graph.elements[main_index].children.clear();
        assert!(graph_digest(&view(&missing_child)).is_err());

        let mut wrong_index = build(
            "<!doctype html><html><head><title>x</title></head><body><main><p>one</p><p>two</p></main></body></html>",
        );
        let first_id = wrong_index.graph.elements[0].exposed.id.clone();
        wrong_index.graph.element_by_id.insert(first_id, 1);
        assert!(graph_digest(&view(&wrong_index)).is_err());

        let rebuilt = build(
            "<!doctype html><html><head><title>x</title></head><body><main><section><p>one</p></section><p>two</p></main></body></html>",
        );
        assert_ne!(original_digest, graph_digest(&view(&rebuilt)).unwrap());
    }

    #[test]
    fn empty_selections_are_not_canonical_certificates() {
        let result = build(
            "<!doctype html><html><head><title>x</title></head><body><p>safe</p></body></html>",
        );
        assert!(create_certificate(&view(&result), &[], 1_024).is_err());
        let malformed = DecodedCertificate {
            scope: ValidationScope::FullDocument,
            source_digest: source_digest_v0(result.source.as_bytes()),
            graph_digest: graph_digest(&view(&result)).unwrap(),
            output_digest: output_digest_v0(b""),
            output_bytes: 0,
            max_output_bytes: 1_024,
            entries: Vec::new(),
        };
        assert!(encode_certificate(&malformed).is_err());
    }

    #[test]
    fn nfc_nfd_and_cross_document_identity_are_distinct() {
        let nfc = build(
            "<!doctype html><html><head><title>x</title></head><body><p>café 😀</p></body></html>",
        );
        let nfd = build(
            "<!doctype html><html><head><title>x</title></head><body><p>cafe\u{301} 😀</p></body></html>",
        );
        let nfc_certificate =
            create_certificate(&view(&nfc), &[id_for_text(&nfc, "café 😀")], 1_024).unwrap();
        let encoded = encode_certificate(&nfc_certificate).unwrap();

        assert_ne!(
            source_digest_v0(nfc.source.as_bytes()),
            source_digest_v0(nfd.source.as_bytes())
        );
        assert!(verify_and_replay(&view(&nfd), &encoded, 1_024)
            .unwrap_err()
            .0
            .contains("source digest mismatch"));
    }

    #[test]
    fn verifier_rejects_id_order_span_and_output_tampering() {
        let result = build(
            "<!doctype html><html><head><title>x</title></head><body><p>alpha</p><p>beta</p></body></html>",
        );
        let ids = vec![id_for_text(&result, "alpha"), id_for_text(&result, "beta")];
        let certificate = create_certificate(&view(&result), &ids, 1_024).unwrap();
        let encoded = encode_certificate(&certificate).unwrap();
        assert_eq!(
            verify_and_replay(&view(&result), &encoded, 1_024)
                .unwrap()
                .markdown,
            "alpha\n\nbeta"
        );

        let mut order = certificate.clone();
        order.entries[0].order += 1;
        assert!(
            verify_and_replay(&view(&result), &encode_certificate(&order).unwrap(), 1_024).is_err()
        );

        let mut span = certificate.clone();
        span.entries[0].source_start += 1;
        assert!(
            verify_and_replay(&view(&result), &encode_certificate(&span).unwrap(), 1_024).is_err()
        );

        let mut kind = certificate.clone();
        kind.entries[0].kind = SelectionKind::Element;
        assert!(
            verify_and_replay(&view(&result), &encode_certificate(&kind).unwrap(), 1_024).is_err()
        );

        let mut output = certificate.clone();
        output.output_digest[0] ^= 1;
        assert!(
            verify_and_replay(&view(&result), &encode_certificate(&output).unwrap(), 1_024)
                .is_err()
        );
    }

    #[test]
    fn output_caps_are_all_or_nothing_and_deterministic() {
        let result = build(
            "<!doctype html><html><head><title>x</title></head><body><p>abcdefghij</p></body></html>",
        );
        let ids = vec![id_for_text(&result, "abcdefghij")];
        assert!(create_certificate(&view(&result), &ids, 9).is_err());
        let certificate = create_certificate(&view(&result), &ids, 10).unwrap();
        let encoded = encode_certificate(&certificate).unwrap();
        assert!(verify_and_replay(&view(&result), &encoded, 9).is_err());
        let first = verify_and_replay(&view(&result), &encoded, 10).unwrap();
        let second = verify_and_replay(&view(&result), &encoded, 10).unwrap();
        assert_eq!(first.markdown, second.markdown);
        assert_eq!(
            first.certificate.output_digest,
            second.certificate.output_digest
        );
    }
}

//! Ordered raw-source text mapping for ordered DOM IR v2.
//!
//! The mapper is an explicit, additive API. It never changes reconstruction or
//! crawler output. Raw source events are scanned once in byte order, decoded by
//! the exact html5ever tokenizer version used by `dom_query`, and paired with
//! retained DOM text nodes only when order, direct parent, and decoded identity
//! form a complete bijection. Parser-reparented runs may be omitted only when
//! both sides are exact HTML whitespace outside whitespace-preserving elements;
//! those omissions are counted and bound into the map digest.

use std::cell::{Cell, RefCell};
use std::collections::HashMap;

use html5ever::tendril::StrTendril;
use html5ever::tokenizer::states::{RawKind, State};
use html5ever::tokenizer::{
    BufferQueue, Token, TokenSink, TokenSinkResult, Tokenizer, TokenizerOpts,
};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use sha2::{Digest as _, Sha256};

use super::{Graph, NativeDocumentIRV2, SCHEMA_VERSION};

pub(super) const MAP_SCHEMA_VERSION: &str = "ordered-source-text-map.v2";
pub(super) const SPAN_SCHEMA_VERSION: &str = "ordered-source-text-span.v2";

const DEFAULT_MAX_SOURCE_BYTES: usize = 4 * 1024 * 1024;
const DEFAULT_MAX_SOURCE_EVENTS: usize = 500_000;
const DEFAULT_MAX_TEXT_RUNS: usize = 200_000;
const DEFAULT_MAX_RAW_FRAGMENT_BYTES: usize = 1024 * 1024;
const DEFAULT_MAX_TOTAL_RAW_BYTES: usize = 8 * 1024 * 1024;
const DEFAULT_MAX_STACK_DEPTH: usize = 256;

const HARD_MAX_SOURCE_BYTES: usize = 16 * 1024 * 1024;
const HARD_MAX_SOURCE_EVENTS: usize = 1_000_000;
const HARD_MAX_TEXT_RUNS: usize = 500_000;
const HARD_MAX_RAW_FRAGMENT_BYTES: usize = 16 * 1024 * 1024;
const HARD_MAX_TOTAL_RAW_BYTES: usize = 16 * 1024 * 1024;
const HARD_MAX_STACK_DEPTH: usize = 512;

#[derive(Clone, Copy, Debug)]
struct MapperLimits {
    max_source_bytes: usize,
    max_source_events: usize,
    max_text_runs: usize,
    max_raw_fragment_bytes: usize,
    max_total_raw_bytes: usize,
    max_stack_depth: usize,
}

impl MapperLimits {
    fn validated(
        max_source_bytes: usize,
        max_source_events: usize,
        max_text_runs: usize,
        max_raw_fragment_bytes: usize,
        max_total_raw_bytes: usize,
        max_stack_depth: usize,
    ) -> PyResult<Self> {
        validate_limit("max_source_bytes", max_source_bytes, HARD_MAX_SOURCE_BYTES)?;
        validate_limit(
            "max_source_events",
            max_source_events,
            HARD_MAX_SOURCE_EVENTS,
        )?;
        validate_limit("max_text_runs", max_text_runs, HARD_MAX_TEXT_RUNS)?;
        validate_limit(
            "max_raw_fragment_bytes",
            max_raw_fragment_bytes,
            HARD_MAX_RAW_FRAGMENT_BYTES,
        )?;
        validate_limit(
            "max_total_raw_bytes",
            max_total_raw_bytes,
            HARD_MAX_TOTAL_RAW_BYTES,
        )?;
        validate_limit("max_stack_depth", max_stack_depth, HARD_MAX_STACK_DEPTH)?;
        Ok(Self {
            max_source_bytes,
            max_source_events,
            max_text_runs,
            max_raw_fragment_bytes,
            max_total_raw_bytes,
            max_stack_depth,
        })
    }
}

fn validate_limit(name: &str, value: usize, hard_maximum: usize) -> PyResult<()> {
    if value == 0 || value > hard_maximum {
        return Err(PyValueError::new_err(format!(
            "{name} must be between 1 and {hard_maximum}"
        )));
    }
    Ok(())
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(super) struct SourceTextMapError {
    reason: &'static str,
    detail: String,
}

impl SourceTextMapError {
    fn new(reason: &'static str, detail: impl Into<String>) -> Self {
        Self {
            reason,
            detail: detail.into(),
        }
    }

    pub(super) fn reason(&self) -> &'static str {
        self.reason
    }

    pub(super) fn detail(&self) -> &str {
        &self.detail
    }
}

type SourceTextMapResult<T> = Result<T, SourceTextMapError>;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum SourceTextTokenKind {
    Data,
    RcData,
    RawText,
}

impl SourceTextTokenKind {
    fn digest_name(self) -> &'static str {
        match self {
            Self::Data => "data",
            Self::RcData => "rcdata",
            Self::RawText => "raw_text",
        }
    }

    fn name(self, parent_tag: &str) -> &'static str {
        match self {
            Self::Data => "data",
            Self::RcData => "rcdata",
            Self::RawText if parent_tag == "script" => "script_data",
            Self::RawText => "raw_text",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct SourceTextToken {
    pub(super) start: usize,
    pub(super) end: usize,
    pub(super) parent_start: Option<usize>,
    pub(super) kind: SourceTextTokenKind,
}

#[derive(Clone, Debug)]
struct OpenSourceElement {
    tag: String,
    start: usize,
    start_tag_end: usize,
    parent_start: Option<usize>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(super) struct SourceElementToken {
    pub(super) tag: String,
    pub(super) start: usize,
    pub(super) start_tag_end: usize,
    pub(super) end: usize,
    pub(super) parent_start: Option<usize>,
}

#[derive(Debug, Default)]
pub(super) struct ExactSourceTokenization {
    pub(super) texts: Vec<SourceTextToken>,
    pub(super) elements: Vec<SourceElementToken>,
    pub(super) source_event_count: usize,
    pub(super) max_stack_depth: usize,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum ExactTokenizationPolicy {
    FullDocument,
    LocalAtomic,
}

#[derive(Clone, Copy, Debug)]
pub(super) struct SourceTokenizerLimits {
    max_source_events: usize,
    max_stack_depth: usize,
}

impl SourceTokenizerLimits {
    fn hard() -> Self {
        Self {
            max_source_events: HARD_MAX_SOURCE_EVENTS,
            max_stack_depth: HARD_MAX_STACK_DEPTH,
        }
    }

    fn from_mapper(limits: MapperLimits) -> Self {
        Self {
            max_source_events: limits.max_source_events,
            max_stack_depth: limits.max_stack_depth,
        }
    }
}

pub(super) fn tokenize_exact_source_text(
    source: &str,
    policy: ExactTokenizationPolicy,
) -> SourceTextMapResult<ExactSourceTokenization> {
    tokenize_exact_source_text_bounded(source, policy, SourceTokenizerLimits::hard())
}

pub(super) fn tokenize_exact_source_text_bounded(
    source: &str,
    policy: ExactTokenizationPolicy,
    limits: SourceTokenizerLimits,
) -> SourceTextMapResult<ExactSourceTokenization> {
    let bytes = source.as_bytes();
    let mut tokenization = ExactSourceTokenization::default();
    let mut stack: Vec<OpenSourceElement> = Vec::new();
    let mut cursor = 0;
    while cursor < bytes.len() {
        let Some(relative) = bytes[cursor..].iter().position(|byte| *byte == b'<') else {
            push_source_text_token(
                &mut tokenization,
                cursor,
                bytes.len(),
                stack.last().map(|element| element.start),
                SourceTextTokenKind::Data,
                limits,
            )?;
            break;
        };
        let markup_start = cursor + relative;
        push_source_text_token(
            &mut tokenization,
            cursor,
            markup_start,
            stack.last().map(|element| element.start),
            SourceTextTokenKind::Data,
            limits,
        )?;
        observe_source_event(&mut tokenization, limits)?;

        if super::starts_ascii_case_insensitive(bytes, markup_start, b"<!--") {
            if policy == ExactTokenizationPolicy::LocalAtomic {
                return Err(SourceTextMapError::new(
                    "tokenization_failure",
                    "local atomic comments are not represented by the selected graph",
                ));
            }
            cursor = super::find_bytes(bytes, markup_start + 4, b"-->")
                .map(|value| value + 3)
                .ok_or_else(|| {
                    SourceTextMapError::new("tokenization_failure", "unterminated HTML comment")
                })?;
            continue;
        }
        if markup_start + 1 >= bytes.len() {
            return Err(SourceTextMapError::new(
                "tokenization_failure",
                "unterminated less-than transition",
            ));
        }
        if matches!(bytes[markup_start + 1], b'!' | b'?') {
            if policy == ExactTokenizationPolicy::LocalAtomic {
                return Err(SourceTextMapError::new(
                    "tokenization_failure",
                    "local atomic markup declarations are not graph-backed",
                ));
            }
            cursor = super::find_tag_end(bytes, markup_start + 2).ok_or_else(|| {
                SourceTextMapError::new("tokenization_failure", "unterminated markup declaration")
            })?;
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
                return Err(SourceTextMapError::new(
                    "tokenization_failure",
                    "local atomic source contains an ambiguous less-than transition",
                ));
            }
            cursor = markup_start + 1;
            continue;
        }
        let tag = source[name_start..name_end].to_ascii_lowercase();

        if closing {
            let tag_end = exact_end_tag_end(bytes, name_end)?;
            let position = stack
                .iter()
                .rposition(|element| element.tag == tag)
                .ok_or_else(|| {
                    SourceTextMapError::new("tokenization_failure", "unmatched HTML end tag")
                })?;
            if position + 1 != stack.len() {
                return Err(SourceTextMapError::new(
                    "tokenization_failure",
                    "source element nesting requires parser repair",
                ));
            }
            let opened = stack.pop().ok_or_else(|| {
                SourceTextMapError::new("tokenization_failure", "source element stack is empty")
            })?;
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

        let tag_end = super::find_tag_end(bytes, name_end).ok_or_else(|| {
            SourceTextMapError::new("tokenization_failure", "unterminated HTML tag")
        })?;
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
        tokenization.max_stack_depth = tokenization.max_stack_depth.max(stack.len());
        if stack.len() > limits.max_stack_depth {
            return Err(SourceTextMapError::new(
                "stack_depth_budget",
                "source element stack exceeds the configured depth budget",
            ));
        }
        if super::is_raw_text_tag(&tag) {
            let (close_start, close_end) = super::find_raw_text_close(source, tag_end, &tag)
                .ok_or_else(|| {
                    SourceTextMapError::new("tokenization_failure", "unterminated raw-text element")
                })?;
            let close_name_end = close_start.checked_add(2 + tag.len()).ok_or_else(|| {
                SourceTextMapError::new("tokenization_failure", "raw-text end-tag offset overflow")
            })?;
            if exact_end_tag_end(bytes, close_name_end)? != close_end {
                return Err(SourceTextMapError::new(
                    "tokenization_failure",
                    "raw-text end tag is not source-canonical",
                ));
            }
            let kind = if matches!(tag.as_str(), "title" | "textarea") {
                SourceTextTokenKind::RcData
            } else {
                SourceTextTokenKind::RawText
            };
            push_source_text_token(
                &mut tokenization,
                tag_end,
                close_start,
                Some(element_start),
                kind,
                limits,
            )?;
            let opened = stack.pop().ok_or_else(|| {
                SourceTextMapError::new("tokenization_failure", "raw-text element stack is empty")
            })?;
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
        return Err(SourceTextMapError::new(
            "tokenization_failure",
            "unterminated source element prevents exact provenance",
        ));
    }
    tokenization
        .elements
        .sort_unstable_by_key(|element| element.start);
    Ok(tokenization)
}

fn observe_source_event(
    tokenization: &mut ExactSourceTokenization,
    limits: SourceTokenizerLimits,
) -> SourceTextMapResult<()> {
    tokenization.source_event_count =
        tokenization
            .source_event_count
            .checked_add(1)
            .ok_or_else(|| {
                SourceTextMapError::new("source_event_budget", "source event count overflowed")
            })?;
    if tokenization.source_event_count > limits.max_source_events {
        return Err(SourceTextMapError::new(
            "source_event_budget",
            "source event count exceeds the configured budget",
        ));
    }
    Ok(())
}

fn exact_end_tag_end(bytes: &[u8], mut cursor: usize) -> SourceTextMapResult<usize> {
    while cursor < bytes.len() {
        match bytes[cursor] {
            b'>' => return Ok(cursor + 1),
            b' ' | b'\t' | b'\n' | 0x0c => cursor += 1,
            _ => {
                return Err(SourceTextMapError::new(
                    "tokenization_failure",
                    "HTML end tag contains attributes, controls, or noncanonical trivia",
                ));
            }
        }
    }
    Err(SourceTextMapError::new(
        "tokenization_failure",
        "unterminated HTML end tag",
    ))
}

fn push_source_text_token(
    tokenization: &mut ExactSourceTokenization,
    start: usize,
    end: usize,
    parent_start: Option<usize>,
    kind: SourceTextTokenKind,
    limits: SourceTokenizerLimits,
) -> SourceTextMapResult<()> {
    if start < end {
        observe_source_event(tokenization, limits)?;
        tokenization.texts.push(SourceTextToken {
            start,
            end,
            parent_start,
            kind,
        });
    }
    Ok(())
}

#[derive(Default)]
struct DecodeSink {
    output: RefCell<String>,
    tokenizer_error_count: Cell<usize>,
    invalid_token: Cell<bool>,
}

impl TokenSink for DecodeSink {
    type Handle = ();

    fn process_token(&self, token: Token, _line_number: u64) -> TokenSinkResult<Self::Handle> {
        match token {
            Token::CharacterTokens(value) => self.output.borrow_mut().push_str(&value),
            Token::ParseError(_) => self
                .tokenizer_error_count
                .set(self.tokenizer_error_count.get().saturating_add(1)),
            Token::EOFToken => {}
            Token::NullCharacterToken
            | Token::TagToken(_)
            | Token::CommentToken(_)
            | Token::DoctypeToken(_) => self.invalid_token.set(true),
        }
        TokenSinkResult::Continue
    }
}

struct DecodedSourceText {
    text: String,
    tokenizer_error_count: usize,
}

fn decode_source_text(
    raw: &str,
    kind: SourceTextTokenKind,
    parent_tag: &str,
) -> SourceTextMapResult<DecodedSourceText> {
    let initial_state = match kind {
        SourceTextTokenKind::Data => State::Data,
        SourceTextTokenKind::RcData => State::RawData(RawKind::Rcdata),
        SourceTextTokenKind::RawText if parent_tag == "script" => {
            State::RawData(RawKind::ScriptData)
        }
        SourceTextTokenKind::RawText => State::RawData(RawKind::Rawtext),
    };
    let sink = DecodeSink::default();
    let tokenizer = Tokenizer::new(
        sink,
        TokenizerOpts {
            exact_errors: true,
            discard_bom: false,
            profile: false,
            initial_state: Some(initial_state),
            last_start_tag_name: (kind != SourceTextTokenKind::Data).then(|| parent_tag.to_owned()),
        },
    );
    let input = BufferQueue::default();
    input.push_back(StrTendril::from(raw));
    let _ = tokenizer.feed(&input);
    tokenizer.end();
    if !input.is_empty() || tokenizer.sink.invalid_token.get() {
        return Err(SourceTextMapError::new(
            "decode_failure",
            "source text fragment emitted a non-text tokenizer token",
        ));
    }
    Ok(DecodedSourceText {
        text: tokenizer.sink.output.take(),
        tokenizer_error_count: tokenizer.sink.tokenizer_error_count.get(),
    })
}

#[derive(Clone)]
struct MapperDocument {
    graph: Graph,
    source: String,
    document_schema_version: &'static str,
    input_bytes: usize,
    parsed_bytes: usize,
    source_complete: bool,
    source_mapping_complete: bool,
    parse_error_count: usize,
    truncated: bool,
    truncation_reasons: Vec<String>,
}

impl From<&NativeDocumentIRV2> for MapperDocument {
    fn from(document: &NativeDocumentIRV2) -> Self {
        Self {
            graph: document.graph.clone(),
            source: document.source.clone(),
            document_schema_version: document.schema_version,
            input_bytes: document.input_bytes,
            parsed_bytes: document.parsed_bytes,
            source_complete: document.source_complete,
            source_mapping_complete: document.source_mapping_complete,
            parse_error_count: document.parse_error_count,
            truncated: document.truncated,
            truncation_reasons: document.truncation_reasons.clone(),
        }
    }
}

#[derive(Clone, Debug)]
struct MappedSpan {
    text_run_id: String,
    order: usize,
    source_order: usize,
    parent_id: String,
    decoded_text: String,
    decoded_bytes: usize,
    decoded_text_sha256: String,
    raw_source_start: usize,
    raw_source_end: usize,
    raw_source_bytes: usize,
    raw_fragment: String,
    raw_fragment_sha256: String,
    decoding_mode: &'static str,
    transform_kind: &'static str,
    transformed: bool,
    tokenizer_error_count: usize,
    certificate_sha256: String,
}

#[derive(Debug)]
struct AcceptedMap {
    spans: Vec<MappedSpan>,
    source_event_count: usize,
    source_text_token_count: usize,
    skipped_source_text_tokens: Vec<SkippedSourceTextToken>,
    skipped_dom_text_run_ids: Vec<String>,
    max_stack_depth: usize,
    total_raw_bytes: usize,
    transformed_span_count: usize,
    character_reference_span_count: usize,
    tokenizer_error_count: usize,
    map_digest: String,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum SkippedSourceTextReason {
    UnownedHtmlWhitespace,
    ExcludedSourceParent,
    IgnorableWhitespaceMismatch,
}

impl SkippedSourceTextReason {
    fn name(self) -> &'static str {
        match self {
            Self::UnownedHtmlWhitespace => "unowned_html_whitespace",
            Self::ExcludedSourceParent => "excluded_source_parent",
            Self::IgnorableWhitespaceMismatch => "ignorable_whitespace_mismatch",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct SkippedSourceTextToken {
    start: usize,
    end: usize,
    parent_start: Option<usize>,
    kind: SourceTextTokenKind,
    reason: SkippedSourceTextReason,
}

impl SkippedSourceTextToken {
    fn from_token(token: &SourceTextToken, reason: SkippedSourceTextReason) -> Self {
        Self {
            start: token.start,
            end: token.end,
            parent_start: token.parent_start,
            kind: token.kind,
            reason,
        }
    }
}

fn build_ordered_map(
    document: &MapperDocument,
    limits: MapperLimits,
    source_digest: &str,
) -> SourceTextMapResult<AcceptedMap> {
    if document.document_schema_version != SCHEMA_VERSION {
        return Err(SourceTextMapError::new(
            "unsupported_document_schema",
            "document schema is not ordered-dom-ir.v2",
        ));
    }
    if document.source.len() > limits.max_source_bytes {
        return Err(SourceTextMapError::new(
            "source_byte_budget",
            "document source exceeds the configured byte budget",
        ));
    }
    if !document.source_complete || document.input_bytes != document.parsed_bytes {
        return Err(SourceTextMapError::new(
            "incomplete_source",
            "document source is incomplete",
        ));
    }
    if document.truncated {
        return Err(SourceTextMapError::new(
            "truncated_document",
            "document IR is truncated",
        ));
    }
    if !document.source_mapping_complete {
        return Err(SourceTextMapError::new(
            "incomplete_element_mapping",
            "document element mapping is incomplete",
        ));
    }
    if document.graph.texts.len() > limits.max_text_runs {
        return Err(SourceTextMapError::new(
            "text_run_budget",
            "document text-run count exceeds the configured budget",
        ));
    }

    let tokenization = tokenize_exact_source_text_bounded(
        &document.source,
        ExactTokenizationPolicy::FullDocument,
        SourceTokenizerLimits::from_mapper(limits),
    )?;
    let retained_parents = retained_parent_by_source_start(&document.graph)?;
    let mut candidates = Vec::new();
    let mut skipped_source_text_tokens = Vec::new();
    let mut candidate_raw_bytes = 0usize;
    for token in &tokenization.texts {
        let Some(parent_start) = token.parent_start else {
            let raw = document.source.get(token.start..token.end).ok_or_else(|| {
                SourceTextMapError::new(
                    "source_dom_mismatch",
                    "unowned source text span is not UTF-8 aligned",
                )
            })?;
            if raw.chars().all(is_html_space) {
                skipped_source_text_tokens.push(SkippedSourceTextToken::from_token(
                    token,
                    SkippedSourceTextReason::UnownedHtmlWhitespace,
                ));
                continue;
            }
            return Err(SourceTextMapError::new(
                "source_dom_mismatch",
                "non-whitespace source text has no explicit source parent",
            ));
        };
        let Some(parent_index) = retained_parents.get(&parent_start).copied() else {
            skipped_source_text_tokens.push(SkippedSourceTextToken::from_token(
                token,
                SkippedSourceTextReason::ExcludedSourceParent,
            ));
            continue;
        };
        let parent = &document.graph.elements[parent_index];
        let raw = document.source.get(token.start..token.end).ok_or_else(|| {
            SourceTextMapError::new(
                "source_dom_mismatch",
                "source text span is not UTF-8 aligned",
            )
        })?;
        if raw.len() > limits.max_raw_fragment_bytes {
            return Err(SourceTextMapError::new(
                "raw_fragment_budget",
                "one source text candidate exceeds the configured byte budget",
            ));
        }
        candidate_raw_bytes = candidate_raw_bytes.checked_add(raw.len()).ok_or_else(|| {
            SourceTextMapError::new("total_raw_byte_budget", "source text byte total overflowed")
        })?;
        if candidate_raw_bytes > limits.max_total_raw_bytes {
            return Err(SourceTextMapError::new(
                "total_raw_byte_budget",
                "source text candidates exceed the configured aggregate budget",
            ));
        }
        let decoded = decode_source_text(raw, token.kind, &parent.exposed.tag)?;
        candidates.push((token, parent_index, raw, decoded));
    }

    let mut spans = Vec::with_capacity(candidates.len().min(document.graph.texts.len()));
    let mut skipped_dom_text_run_ids = Vec::new();
    let mut total_raw_bytes = 0usize;
    let mut transformed_span_count = 0usize;
    let mut character_reference_span_count = 0usize;
    let mut tokenizer_error_count = 0usize;
    let mut previous_source_end = None;
    let mut previous_dom_order = None;
    let mut source_candidates = candidates.into_iter().peekable();
    let mut dom_texts = document.graph.texts.iter().peekable();
    loop {
        let exact_match = match (source_candidates.peek(), dom_texts.peek()) {
            (Some((_, parent_index, _, decoded)), Some(text)) => {
                *parent_index == text.parent_index && decoded.text == text.exposed.text
            }
            _ => false,
        };
        if !exact_match {
            if source_candidates
                .peek()
                .is_some_and(|(_, parent_index, _, decoded)| {
                    source_candidate_is_ignorable_whitespace(
                        decoded,
                        &document.graph,
                        *parent_index,
                    )
                })
            {
                let (token, _, _, _) = source_candidates
                    .next()
                    .expect("peeked ignorable source text must be present");
                skipped_source_text_tokens.push(SkippedSourceTextToken::from_token(
                    token,
                    SkippedSourceTextReason::IgnorableWhitespaceMismatch,
                ));
                continue;
            }
            if dom_texts
                .peek()
                .is_some_and(|text| dom_text_is_ignorable_whitespace(text))
            {
                let text = dom_texts
                    .next()
                    .expect("peeked ignorable DOM text must be present");
                skipped_dom_text_run_ids.push(text.exposed.id.clone());
                continue;
            }
            match (source_candidates.peek(), dom_texts.peek()) {
                (None, None) => break,
                (None, Some(_)) | (Some(_), None) => {
                    return Err(SourceTextMapError::new(
                        "unmapped_text",
                        "non-ignorable source and DOM retained text counts differ",
                    ));
                }
                (Some(_), Some(_)) => {
                    return Err(SourceTextMapError::new(
                        "source_dom_mismatch",
                        "ordered source text disagrees with DOM parent or decoded identity",
                    ));
                }
            }
        }

        let (token, parent_index, raw, decoded) = source_candidates
            .next()
            .expect("exact source candidate must be present");
        let text = dom_texts
            .next()
            .expect("exact DOM text candidate must be present");
        let map_order = spans.len();
        if text.exposed.truncated {
            return Err(SourceTextMapError::new(
                "truncated_document",
                "retained text run is truncated",
            ));
        }
        if previous_source_end.is_some_and(|end| token.start < end) {
            return Err(SourceTextMapError::new(
                "source_event_order_mismatch",
                "ordered source text spans overlap",
            ));
        }
        if previous_dom_order.is_some_and(|order| text.exposed.order <= order) {
            return Err(SourceTextMapError::new(
                "source_event_order_mismatch",
                "DOM text order is not strictly increasing",
            ));
        }
        validate_text_within_parent(token, &document.graph, parent_index)?;
        let raw_bytes = token.end - token.start;
        if raw_bytes > limits.max_raw_fragment_bytes {
            return Err(SourceTextMapError::new(
                "raw_fragment_budget",
                "one raw source fragment exceeds the configured byte budget",
            ));
        }
        total_raw_bytes = total_raw_bytes.checked_add(raw_bytes).ok_or_else(|| {
            SourceTextMapError::new("total_raw_byte_budget", "raw source byte total overflowed")
        })?;
        if total_raw_bytes > limits.max_total_raw_bytes {
            return Err(SourceTextMapError::new(
                "total_raw_byte_budget",
                "raw source fragments exceed the configured aggregate budget",
            ));
        }

        let transformed = raw != decoded.text;
        let has_ampersand = raw.as_bytes().contains(&b'&');
        let has_carriage_return = raw.as_bytes().contains(&b'\r');
        let transform_kind = match (transformed, has_ampersand, has_carriage_return) {
            (false, _, _) => "identity",
            (true, true, true) => "mixed",
            (true, true, false) => "html_character_reference",
            (true, false, true) => "newline_normalization",
            (true, false, false) => "tokenizer_normalization",
        };
        transformed_span_count += usize::from(transformed);
        character_reference_span_count += usize::from(transformed && has_ampersand);
        tokenizer_error_count = tokenizer_error_count
            .checked_add(decoded.tokenizer_error_count)
            .ok_or_else(|| {
                SourceTextMapError::new("decode_failure", "tokenizer error count overflowed")
            })?;

        let raw_fragment_sha256 = sha256_hex(raw.as_bytes());
        let decoded_text_sha256 = sha256_hex(decoded.text.as_bytes());
        let decoding_mode = token
            .kind
            .name(&document.graph.elements[parent_index].exposed.tag);
        let certificate_sha256 = span_certificate_digest(
            source_digest,
            &text.exposed.id,
            text.exposed.order,
            token.start,
            token.end,
            &raw_fragment_sha256,
            &decoded_text_sha256,
            decoding_mode,
            transform_kind,
        );
        spans.push(MappedSpan {
            text_run_id: text.exposed.id.clone(),
            order: map_order,
            source_order: text.exposed.order,
            parent_id: text.exposed.parent_id.clone(),
            decoded_text: decoded.text,
            decoded_bytes: text.exposed.stored_bytes,
            decoded_text_sha256,
            raw_source_start: token.start,
            raw_source_end: token.end,
            raw_source_bytes: raw_bytes,
            raw_fragment: raw.to_owned(),
            raw_fragment_sha256,
            decoding_mode,
            transform_kind,
            transformed,
            tokenizer_error_count: decoded.tokenizer_error_count,
            certificate_sha256,
        });
        previous_source_end = Some(token.end);
        previous_dom_order = Some(text.exposed.order);
    }

    let map_digest = map_digest(
        source_digest,
        &spans,
        &skipped_source_text_tokens,
        &skipped_dom_text_run_ids,
    );
    Ok(AcceptedMap {
        spans,
        source_event_count: tokenization.source_event_count,
        source_text_token_count: tokenization.texts.len(),
        skipped_source_text_tokens,
        skipped_dom_text_run_ids,
        max_stack_depth: tokenization.max_stack_depth,
        total_raw_bytes,
        transformed_span_count,
        character_reference_span_count,
        tokenizer_error_count,
        map_digest,
    })
}

fn source_candidate_is_ignorable_whitespace(
    decoded: &DecodedSourceText,
    graph: &Graph,
    parent_index: usize,
) -> bool {
    graph.elements.get(parent_index).is_some_and(|parent| {
        !parent.exposed.preserve_whitespace && decoded.text.chars().all(is_html_space)
    })
}

fn dom_text_is_ignorable_whitespace(text: &super::TextRecord) -> bool {
    !text.exposed.preserve_whitespace && text.exposed.text.chars().all(is_html_space)
}

fn retained_parent_by_source_start(graph: &Graph) -> SourceTextMapResult<HashMap<usize, usize>> {
    let mut output = HashMap::with_capacity(graph.elements.len());
    for (index, element) in graph.elements.iter().enumerate() {
        if element.exposed.implicit {
            continue;
        }
        if !element.exposed.source_span_reliable {
            return Err(SourceTextMapError::new(
                "incomplete_element_mapping",
                "retained explicit element lacks a reliable source span",
            ));
        }
        let start = element.exposed.source_start.ok_or_else(|| {
            SourceTextMapError::new(
                "incomplete_element_mapping",
                "retained explicit element lacks a source start",
            )
        })?;
        if output.insert(start, index).is_some() {
            return Err(SourceTextMapError::new(
                "source_dom_mismatch",
                "retained elements alias one source start",
            ));
        }
    }
    Ok(output)
}

fn validate_text_within_parent(
    token: &SourceTextToken,
    graph: &Graph,
    parent_index: usize,
) -> SourceTextMapResult<()> {
    let parent = graph.elements.get(parent_index).ok_or_else(|| {
        SourceTextMapError::new("source_dom_mismatch", "text parent index is out of bounds")
    })?;
    let (Some(parent_start), Some(content_start), Some(parent_end)) = (
        parent.exposed.source_start,
        parent.exposed.source_start_tag_end,
        parent.exposed.source_end,
    ) else {
        return Err(SourceTextMapError::new(
            "incomplete_element_mapping",
            "text parent source closure is incomplete",
        ));
    };
    if token.parent_start != Some(parent_start)
        || token.start < content_start
        || token.end > parent_end
        || token.start >= token.end
    {
        return Err(SourceTextMapError::new(
            "source_dom_mismatch",
            "text span is not contained by its direct source parent",
        ));
    }
    Ok(())
}

fn is_html_space(character: char) -> bool {
    matches!(character, '\t' | '\n' | '\u{000c}' | '\r' | ' ')
}

fn sha256_hex(value: &[u8]) -> String {
    bytes_hex(&Sha256::digest(value))
}

fn bytes_hex(value: &[u8]) -> String {
    let mut output = String::with_capacity(64);
    for byte in value {
        use std::fmt::Write as _;
        let _ = write!(output, "{byte:02x}");
    }
    output
}

fn digest_field(digest: &mut Sha256, value: &[u8]) {
    digest.update((value.len() as u64).to_be_bytes());
    digest.update(value);
}

#[allow(clippy::too_many_arguments)]
fn span_certificate_digest(
    source_digest: &str,
    text_run_id: &str,
    source_order: usize,
    raw_start: usize,
    raw_end: usize,
    raw_digest: &str,
    decoded_digest: &str,
    decoding_mode: &str,
    transform_kind: &str,
) -> String {
    let mut digest = Sha256::new();
    digest_field(&mut digest, SPAN_SCHEMA_VERSION.as_bytes());
    digest_field(&mut digest, source_digest.as_bytes());
    digest_field(&mut digest, text_run_id.as_bytes());
    digest.update((source_order as u64).to_be_bytes());
    digest.update((raw_start as u64).to_be_bytes());
    digest.update((raw_end as u64).to_be_bytes());
    digest_field(&mut digest, raw_digest.as_bytes());
    digest_field(&mut digest, decoded_digest.as_bytes());
    digest_field(&mut digest, decoding_mode.as_bytes());
    digest_field(&mut digest, transform_kind.as_bytes());
    bytes_hex(&digest.finalize())
}

fn map_digest(
    source_digest: &str,
    spans: &[MappedSpan],
    skipped_source_text_tokens: &[SkippedSourceTextToken],
    skipped_dom_text_run_ids: &[String],
) -> String {
    let mut digest = Sha256::new();
    digest_field(&mut digest, MAP_SCHEMA_VERSION.as_bytes());
    digest_field(&mut digest, source_digest.as_bytes());
    digest.update((spans.len() as u64).to_be_bytes());
    for span in spans {
        digest_field(&mut digest, span.certificate_sha256.as_bytes());
    }
    let mut ordered_skipped_source_tokens = skipped_source_text_tokens.iter().collect::<Vec<_>>();
    ordered_skipped_source_tokens.sort_unstable_by_key(|token| {
        (
            token.start,
            token.end,
            token.parent_start,
            token.kind.digest_name(),
            token.reason.name(),
        )
    });
    digest.update((ordered_skipped_source_tokens.len() as u64).to_be_bytes());
    for token in ordered_skipped_source_tokens {
        digest.update((token.start as u64).to_be_bytes());
        digest.update((token.end as u64).to_be_bytes());
        match token.parent_start {
            Some(parent_start) => {
                digest.update([1]);
                digest.update((parent_start as u64).to_be_bytes());
            }
            None => digest.update([0]),
        }
        digest_field(&mut digest, token.kind.digest_name().as_bytes());
        digest_field(&mut digest, token.reason.name().as_bytes());
    }
    digest.update((skipped_dom_text_run_ids.len() as u64).to_be_bytes());
    for text_run_id in skipped_dom_text_run_ids {
        digest_field(&mut digest, text_run_id.as_bytes());
    }
    bytes_hex(&digest.finalize())
}

#[pyclass(frozen, get_all)]
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct NativeOrderedSourceTextSpanV2 {
    schema_version: &'static str,
    text_run_id: String,
    order: usize,
    source_order: usize,
    parent_id: String,
    decoded_text: String,
    decoded_bytes: usize,
    decoded_text_sha256: String,
    raw_source_start: usize,
    raw_source_end: usize,
    raw_source_bytes: usize,
    raw_fragment: String,
    raw_fragment_sha256: String,
    decoding_mode: &'static str,
    transform_kind: &'static str,
    transformed: bool,
    tokenizer_error_count: usize,
    decode_verified: bool,
    certificate_sha256: String,
    digest_is_authentication: bool,
}

#[pyclass(frozen)]
pub(crate) struct NativeOrderedSourceTextMapV2 {
    spans: Vec<Py<NativeOrderedSourceTextSpanV2>>,
    #[pyo3(get)]
    schema_version: &'static str,
    #[pyo3(get)]
    document_schema_version: &'static str,
    #[pyo3(get)]
    accepted: bool,
    #[pyo3(get)]
    reason: &'static str,
    #[pyo3(get)]
    source_digest: String,
    #[pyo3(get)]
    map_digest: String,
    #[pyo3(get)]
    input_bytes: usize,
    #[pyo3(get)]
    parsed_bytes: usize,
    #[pyo3(get)]
    source_complete: bool,
    #[pyo3(get)]
    source_mapping_complete: bool,
    #[pyo3(get)]
    parse_error_count: usize,
    #[pyo3(get)]
    document_truncated: bool,
    #[pyo3(get)]
    truncation_reasons: Vec<String>,
    #[pyo3(get)]
    candidate_text_run_count: usize,
    #[pyo3(get)]
    mapped_text_run_count: usize,
    #[pyo3(get)]
    source_event_count: usize,
    #[pyo3(get)]
    source_text_token_count: usize,
    #[pyo3(get)]
    skipped_source_text_token_count: usize,
    #[pyo3(get)]
    skipped_dom_text_run_count: usize,
    #[pyo3(get)]
    max_stack_depth_seen: usize,
    #[pyo3(get)]
    total_raw_bytes: usize,
    #[pyo3(get)]
    transformed_span_count: usize,
    #[pyo3(get)]
    character_reference_span_count: usize,
    #[pyo3(get)]
    tokenizer_error_count: usize,
    #[pyo3(get)]
    max_source_bytes: usize,
    #[pyo3(get)]
    max_source_events: usize,
    #[pyo3(get)]
    max_text_runs: usize,
    #[pyo3(get)]
    max_raw_fragment_bytes: usize,
    #[pyo3(get)]
    max_total_raw_bytes: usize,
    #[pyo3(get)]
    max_stack_depth: usize,
    #[pyo3(get)]
    deterministic: bool,
    #[pyo3(get)]
    digest_is_authentication: bool,
}

#[pymethods]
impl NativeOrderedSourceTextMapV2 {
    #[getter]
    fn spans(&self, py: Python<'_>) -> Vec<Py<NativeOrderedSourceTextSpanV2>> {
        self.spans.iter().map(|span| span.clone_ref(py)).collect()
    }
}

#[allow(clippy::too_many_arguments)]
#[pyfunction]
#[pyo3(signature = (
    document,
    max_source_bytes=DEFAULT_MAX_SOURCE_BYTES,
    max_source_events=DEFAULT_MAX_SOURCE_EVENTS,
    max_text_runs=DEFAULT_MAX_TEXT_RUNS,
    max_raw_fragment_bytes=DEFAULT_MAX_RAW_FRAGMENT_BYTES,
    max_total_raw_bytes=DEFAULT_MAX_TOTAL_RAW_BYTES,
    max_stack_depth=DEFAULT_MAX_STACK_DEPTH,
))]
pub(crate) fn map_ordered_source_text_v2_native(
    py: Python<'_>,
    document: &NativeDocumentIRV2,
    max_source_bytes: usize,
    max_source_events: usize,
    max_text_runs: usize,
    max_raw_fragment_bytes: usize,
    max_total_raw_bytes: usize,
    max_stack_depth: usize,
) -> PyResult<NativeOrderedSourceTextMapV2> {
    let limits = MapperLimits::validated(
        max_source_bytes,
        max_source_events,
        max_text_runs,
        max_raw_fragment_bytes,
        max_total_raw_bytes,
        max_stack_depth,
    )?;
    let owned = MapperDocument::from(document);
    let source_digest = sha256_hex(owned.source.as_bytes());
    let candidate_text_run_count = owned.graph.texts.len();
    let document_schema_version = owned.document_schema_version;
    let input_bytes = owned.input_bytes;
    let parsed_bytes = owned.parsed_bytes;
    let source_complete = owned.source_complete;
    let source_mapping_complete = owned.source_mapping_complete;
    let parse_error_count = owned.parse_error_count;
    let document_truncated = owned.truncated;
    let truncation_reasons = owned.truncation_reasons.clone();
    let digest_for_mapping = source_digest.clone();
    let outcome = py.detach(move || build_ordered_map(&owned, limits, digest_for_mapping.as_str()));

    let (
        accepted,
        reason,
        mapped,
        map_digest,
        source_event_count,
        source_text_token_count,
        skipped_source_text_token_count,
        skipped_dom_text_run_count,
        max_stack_depth_seen,
        total_raw_bytes,
        transformed_span_count,
        character_reference_span_count,
        tokenizer_error_count,
    ) = match outcome {
        Ok(value) => (
            true,
            "accepted",
            value.spans,
            value.map_digest,
            value.source_event_count,
            value.source_text_token_count,
            value.skipped_source_text_tokens.len(),
            value.skipped_dom_text_run_ids.len(),
            value.max_stack_depth,
            value.total_raw_bytes,
            value.transformed_span_count,
            value.character_reference_span_count,
            value.tokenizer_error_count,
        ),
        Err(error) => (
            false,
            error.reason(),
            Vec::new(),
            String::new(),
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        ),
    };
    let mapped_text_run_count = mapped.len();
    let spans = mapped
        .into_iter()
        .map(|span| {
            Py::new(
                py,
                NativeOrderedSourceTextSpanV2 {
                    schema_version: SPAN_SCHEMA_VERSION,
                    text_run_id: span.text_run_id,
                    order: span.order,
                    source_order: span.source_order,
                    parent_id: span.parent_id,
                    decoded_text: span.decoded_text,
                    decoded_bytes: span.decoded_bytes,
                    decoded_text_sha256: span.decoded_text_sha256,
                    raw_source_start: span.raw_source_start,
                    raw_source_end: span.raw_source_end,
                    raw_source_bytes: span.raw_source_bytes,
                    raw_fragment: span.raw_fragment,
                    raw_fragment_sha256: span.raw_fragment_sha256,
                    decoding_mode: span.decoding_mode,
                    transform_kind: span.transform_kind,
                    transformed: span.transformed,
                    tokenizer_error_count: span.tokenizer_error_count,
                    decode_verified: true,
                    certificate_sha256: span.certificate_sha256,
                    digest_is_authentication: false,
                },
            )
        })
        .collect::<PyResult<Vec<_>>>()?;

    Ok(NativeOrderedSourceTextMapV2 {
        spans,
        schema_version: MAP_SCHEMA_VERSION,
        document_schema_version,
        accepted,
        reason,
        source_digest,
        map_digest,
        input_bytes,
        parsed_bytes,
        source_complete,
        source_mapping_complete,
        parse_error_count,
        document_truncated,
        truncation_reasons,
        candidate_text_run_count,
        mapped_text_run_count,
        source_event_count,
        source_text_token_count,
        skipped_source_text_token_count,
        skipped_dom_text_run_count,
        max_stack_depth_seen,
        total_raw_bytes,
        transformed_span_count,
        character_reference_span_count,
        tokenizer_error_count,
        max_source_bytes: limits.max_source_bytes,
        max_source_events: limits.max_source_events,
        max_text_runs: limits.max_text_runs,
        max_raw_fragment_bytes: limits.max_raw_fragment_bytes,
        max_total_raw_bytes: limits.max_total_raw_bytes,
        max_stack_depth: limits.max_stack_depth,
        deterministic: true,
        digest_is_authentication: false,
    })
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<NativeOrderedSourceTextSpanV2>()?;
    module.add_class::<NativeOrderedSourceTextMapV2>()?;
    module.add_function(wrap_pyfunction!(map_ordered_source_text_v2_native, module)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mapper_limits() -> MapperLimits {
        MapperLimits {
            max_source_bytes: DEFAULT_MAX_SOURCE_BYTES,
            max_source_events: DEFAULT_MAX_SOURCE_EVENTS,
            max_text_runs: DEFAULT_MAX_TEXT_RUNS,
            max_raw_fragment_bytes: DEFAULT_MAX_RAW_FRAGMENT_BYTES,
            max_total_raw_bytes: DEFAULT_MAX_TOTAL_RAW_BYTES,
            max_stack_depth: DEFAULT_MAX_STACK_DEPTH,
        }
    }

    fn ir_limits() -> super::super::LimitsV2 {
        super::super::LimitsV2 {
            max_input_bytes: super::super::DEFAULT_MAX_INPUT_BYTES,
            max_nodes: super::super::DEFAULT_MAX_NODES,
            max_elements: super::super::DEFAULT_MAX_ELEMENTS,
            max_text_runs: super::super::DEFAULT_MAX_TEXT_RUNS,
            max_depth: super::super::DEFAULT_MAX_DEPTH,
            max_text_run_bytes: super::super::DEFAULT_MAX_TEXT_RUN_BYTES,
            max_total_text_bytes: super::super::DEFAULT_MAX_TOTAL_TEXT_BYTES,
            max_math_bytes: super::super::DEFAULT_MAX_MATH_BYTES,
            max_table_columns: super::super::DEFAULT_MAX_TABLE_COLUMNS,
        }
    }

    fn document(html: &str) -> MapperDocument {
        let result = super::super::build_ir_v2(html.to_owned(), html.len(), false, ir_limits());
        let truncated = result.is_truncated();
        let source_mapping_complete = result.unmapped_explicit_element_count == 0;
        MapperDocument {
            graph: result.graph,
            source: result.source,
            document_schema_version: SCHEMA_VERSION,
            input_bytes: result.input_bytes,
            parsed_bytes: result.parsed_bytes,
            source_complete: !result.input_truncated,
            source_mapping_complete,
            parse_error_count: result.parse_error_count,
            truncated,
            truncation_reasons: result.truncation_reasons,
        }
    }

    fn map(html: &str) -> SourceTextMapResult<AcceptedMap> {
        let document = document(html);
        let digest = sha256_hex(document.source.as_bytes());
        build_ordered_map(&document, mapper_limits(), &digest)
    }

    #[test]
    fn html5ever_decoder_covers_named_numeric_and_multibyte_text() {
        let decoded = decode_source_text(
            "A &amp; &#38; &#x1F642; 中文",
            SourceTextTokenKind::Data,
            "p",
        )
        .unwrap();
        assert_eq!(decoded.text, "A & & 🙂 中文");
    }

    #[test]
    fn html5ever_decoder_uses_rcdata_and_raw_text_states() {
        let rcdata =
            decode_source_text("&amp;<b>x</b>", SourceTextTokenKind::RcData, "textarea").unwrap();
        assert_eq!(rcdata.text, "&<b>x</b>");
        let raw =
            decode_source_text("&amp;<b>x</b>", SourceTextTokenKind::RawText, "style").unwrap();
        assert_eq!(raw.text, "&amp;<b>x</b>");
    }

    #[test]
    fn maps_entities_multibyte_and_repeated_siblings_in_source_order() {
        let html =
            "<main><p>same &amp; 中文</p><p>same &amp; 中文</p><p>&#38; &#x1F642;</p></main>";
        let mapped = map(html).unwrap();
        assert_eq!(mapped.spans.len(), 3);
        assert_eq!(
            mapped
                .spans
                .iter()
                .map(|span| span.decoded_text.as_str())
                .collect::<Vec<_>>(),
            ["same & 中文", "same & 中文", "& 🙂"]
        );
        assert_eq!(
            mapped
                .spans
                .iter()
                .map(|span| span.raw_fragment.as_str())
                .collect::<Vec<_>>(),
            ["same &amp; 中文", "same &amp; 中文", "&#38; &#x1F642;"]
        );
        assert!(mapped
            .spans
            .windows(2)
            .all(|pair| pair[0].raw_source_end < pair[1].raw_source_start));
        assert_eq!(mapped.transformed_span_count, 3);
        assert_eq!(mapped.character_reference_span_count, 3);
    }

    #[test]
    fn skips_comments_and_hidden_script_style_source_text() {
        let html = concat!(
            "<main><p>A<!-- not text -->B</p>",
            "<script>window.hidden = '&amp;';</script>",
            "<style>.hidden::after { content: '&amp;' }</style>",
            "<p>shown</p></main>"
        );
        let mapped = map(html).unwrap();
        assert_eq!(
            mapped
                .spans
                .iter()
                .map(|span| span.decoded_text.as_str())
                .collect::<Vec<_>>(),
            ["A", "B", "shown"]
        );
        assert_eq!(mapped.skipped_source_text_tokens.len(), 2);
    }

    #[test]
    fn skips_only_ignorable_parser_reparented_document_edge_whitespace() {
        let html = "<!doctype html><html><body>\n<p>shown</p>\n</body></html>\n";
        let mapped = map(html).unwrap();
        assert_eq!(
            mapped
                .spans
                .iter()
                .filter(|span| !span.decoded_text.chars().all(is_html_space))
                .map(|span| span.decoded_text.as_str())
                .collect::<Vec<_>>(),
            ["shown"]
        );
        assert_eq!(mapped.skipped_dom_text_run_ids.len(), 1);
        assert!(!mapped.skipped_source_text_tokens.is_empty());
    }

    #[test]
    fn map_digest_binds_every_skipped_source_token_field_and_skipped_dom_identity() {
        let baseline_token = SkippedSourceTextToken {
            start: 10,
            end: 13,
            parent_start: Some(4),
            kind: SourceTextTokenKind::Data,
            reason: SkippedSourceTextReason::IgnorableWhitespaceMismatch,
        };
        let baseline = map_digest(
            "source-digest",
            &[],
            &[baseline_token],
            &["text-run-a".to_owned()],
        );
        assert_eq!(
            baseline,
            map_digest(
                "source-digest",
                &[],
                &[baseline_token],
                &["text-run-a".to_owned()],
            )
        );

        let mutations = [
            SkippedSourceTextToken {
                start: 9,
                ..baseline_token
            },
            SkippedSourceTextToken {
                end: 14,
                ..baseline_token
            },
            SkippedSourceTextToken {
                parent_start: None,
                ..baseline_token
            },
            SkippedSourceTextToken {
                parent_start: Some(5),
                ..baseline_token
            },
            SkippedSourceTextToken {
                kind: SourceTextTokenKind::RcData,
                ..baseline_token
            },
            SkippedSourceTextToken {
                kind: SourceTextTokenKind::RawText,
                ..baseline_token
            },
            SkippedSourceTextToken {
                reason: SkippedSourceTextReason::ExcludedSourceParent,
                ..baseline_token
            },
            SkippedSourceTextToken {
                reason: SkippedSourceTextReason::UnownedHtmlWhitespace,
                ..baseline_token
            },
        ];
        for mutation in mutations {
            assert_ne!(
                baseline,
                map_digest(
                    "source-digest",
                    &[],
                    &[mutation],
                    &["text-run-a".to_owned()],
                )
            );
        }
        assert_ne!(
            baseline,
            map_digest(
                "source-digest",
                &[],
                &[baseline_token],
                &["text-run-b".to_owned()],
            )
        );

        let second_token = SkippedSourceTextToken {
            start: 20,
            end: 22,
            parent_start: None,
            kind: SourceTextTokenKind::RawText,
            reason: SkippedSourceTextReason::UnownedHtmlWhitespace,
        };
        assert_eq!(
            map_digest(
                "source-digest",
                &[],
                &[baseline_token, second_token],
                &["text-run-a".to_owned()],
            ),
            map_digest(
                "source-digest",
                &[],
                &[second_token, baseline_token],
                &["text-run-a".to_owned()],
            )
        );
    }

    #[test]
    fn fails_closed_for_parser_repairs_and_foster_parenting() {
        let malformed = map("<main><p>one<div>two</div></p></main>").unwrap_err();
        assert!(matches!(
            malformed.reason(),
            "tokenization_failure" | "incomplete_element_mapping" | "source_dom_mismatch"
        ));

        let fostered = map("<table>outside<tr><td>inside</td></tr></table>").unwrap_err();
        assert!(matches!(
            fostered.reason(),
            "incomplete_element_mapping" | "source_dom_mismatch"
        ));

        let explicit_body_fostered =
            map("<html><body><table>outside<tr><td>inside</td></tr></table></body></html>")
                .unwrap_err();
        assert!(matches!(
            explicit_body_fostered.reason(),
            "incomplete_element_mapping" | "source_dom_mismatch"
        ));
    }

    #[test]
    fn source_scanner_budgets_fail_closed() {
        let document = document("<main><p>one</p><p>two</p></main>");
        let digest = sha256_hex(document.source.as_bytes());

        let mut limits = mapper_limits();
        limits.max_source_events = 2;
        assert_eq!(
            build_ordered_map(&document, limits, &digest)
                .unwrap_err()
                .reason(),
            "source_event_budget"
        );

        let mut limits = mapper_limits();
        limits.max_raw_fragment_bytes = 2;
        assert_eq!(
            build_ordered_map(&document, limits, &digest)
                .unwrap_err()
                .reason(),
            "raw_fragment_budget"
        );

        let mut limits = mapper_limits();
        limits.max_total_raw_bytes = 5;
        assert_eq!(
            build_ordered_map(&document, limits, &digest)
                .unwrap_err()
                .reason(),
            "total_raw_byte_budget"
        );

        let mut limits = mapper_limits();
        limits.max_stack_depth = 1;
        assert_eq!(
            build_ordered_map(&document, limits, &digest)
                .unwrap_err()
                .reason(),
            "stack_depth_budget"
        );
    }

    #[test]
    fn generated_entity_and_multibyte_runs_round_trip_exact_raw_spans() {
        let atoms = [
            ("plain", "plain"),
            ("&amp;", "&"),
            ("&#38;", "&"),
            ("&#x1F642;", "🙂"),
            ("中文", "中文"),
            ("é", "é"),
        ];
        for case in 0..48 {
            let mut raw = String::new();
            let mut decoded = String::new();
            for offset in 0..8 {
                if offset > 0 {
                    raw.push(' ');
                    decoded.push(' ');
                }
                let (raw_atom, decoded_atom) = atoms[(case * 5 + offset * 3) % atoms.len()];
                raw.push_str(raw_atom);
                decoded.push_str(decoded_atom);
            }
            let html = format!("<main><p>{raw}</p><p>{raw}</p></main>");
            let mapped = map(&html).unwrap();
            assert_eq!(mapped.spans.len(), 2);
            for span in &mapped.spans {
                assert_eq!(span.decoded_text, decoded);
                assert_eq!(span.raw_fragment, raw);
                assert_eq!(
                    &html[span.raw_source_start..span.raw_source_end],
                    span.raw_fragment
                );
                assert_eq!(
                    span.raw_fragment_sha256,
                    sha256_hex(span.raw_fragment.as_bytes())
                );
                assert_eq!(
                    span.decoded_text_sha256,
                    sha256_hex(span.decoded_text.as_bytes())
                );
            }
        }
    }
}

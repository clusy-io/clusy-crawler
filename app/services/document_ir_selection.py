"""Bounded classifier and reconstruction contracts for ``ordered-dom-ir.v1``.

This module has no production routing side effects. It turns the native
document IR into a compact, deterministic JSONL classifier input, validates a
minimal model response fail-closed, and reconstructs selected blocks from
their stored DOM ``outer_html`` in original order.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, NoReturn, Protocol, cast

DOCUMENT_IR_SCHEMA_VERSION = "ordered-dom-ir.v1"
CLASSIFIER_INPUT_SCHEMA_VERSION = "ordered-dom-ir.classifier-input.v1"
SELECTION_SCHEMA_VERSION = "ordered-dom-ir.selection.v1"
RECONSTRUCTION_STRATEGY = "stored-outer-html-dom-order-v1"
UTF8_TOKEN_ACCOUNTING = "utf8-byte-upper-bound"

_BLOCK_ID_RE = re.compile(r"block-\d{6}\Z", flags=re.ASCII)
_SELECTION_ITEM_RE = re.compile(
    r"(block-\d{6})(?:\.\.(block-\d{6}))?\Z",
    flags=re.ASCII,
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", flags=re.ASCII)
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

_HARD_MAX_SOURCE_BLOCKS = 16_384
_HARD_MAX_BLOCK_TEXT_CHARS = 256 * 1024
_HARD_MAX_BLOCK_HTML_CHARS = 512 * 1024
_HARD_MAX_CLASSIFIER_CHARS = 2 * 1024 * 1024
_HARD_MAX_CLASSIFIER_TOKENS = 2 * 1024 * 1024
_HARD_MAX_SELECTION_RESPONSE_CHARS = 256 * 1024
_HARD_MAX_RECONSTRUCTION_CHARS = 32 * 1024 * 1024


def _validate_limit(name: str, value: int, hard_max: int) -> None:
    if type(value) is not int or value <= 0 or value > hard_max:
        raise ValueError(f"{name} must be between 1 and {hard_max}")


class SemanticBlock(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def order(self) -> int: ...

    @property
    def parent_id(self) -> str | None: ...

    @property
    def tag(self) -> str: ...

    @property
    def role(self) -> str: ...

    @property
    def atomic(self) -> bool: ...

    @property
    def selectable(self) -> bool: ...

    @property
    def preserve_whitespace(self) -> bool: ...

    @property
    def text(self) -> str: ...

    @property
    def outer_html(self) -> str: ...

    @property
    def depth(self) -> int: ...

    @property
    def word_count(self) -> int: ...

    @property
    def text_bytes(self) -> int: ...

    @property
    def html_bytes(self) -> int: ...

    @property
    def link_count(self) -> int: ...

    @property
    def link_text_bytes(self) -> int: ...

    @property
    def descendant_element_count(self) -> int: ...

    @property
    def text_density(self) -> float: ...

    @property
    def link_density(self) -> float: ...

    @property
    def text_truncated(self) -> bool: ...

    @property
    def html_truncated(self) -> bool: ...

    @property
    def features_truncated(self) -> bool: ...


class DocumentBlocks(Protocol):
    @property
    def blocks(self) -> Sequence[SemanticBlock]: ...

    @property
    def block_count(self) -> int: ...

    @property
    def schema_version(self) -> str: ...

    @property
    def truncated(self) -> bool: ...

    @property
    def truncation_reasons(self) -> Sequence[str]: ...


TokenCounter = Callable[[str], int]


class BlockContractError(ValueError):
    """Base exception for a fail-closed IR selection contract."""


class InvalidDocumentIR(BlockContractError):  # noqa: N818
    """The source IR violates the ``ordered-dom-ir.v1`` invariants."""


class ClassifierBudgetError(BlockContractError):
    """The fixed classifier envelope cannot fit within caller budgets."""


class InvalidBlockSelection(BlockContractError):  # noqa: N818
    """A model selection response is malformed or unsafe."""


@dataclass(frozen=True, slots=True)
class ClassifierInputLimits:
    max_chars: int = 64 * 1024
    max_tokens: int = 64 * 1024
    max_blocks: int = 512
    max_text_chars_per_block: int = 1_024

    def __post_init__(self) -> None:
        _validate_limit("max_chars", self.max_chars, _HARD_MAX_CLASSIFIER_CHARS)
        _validate_limit("max_tokens", self.max_tokens, _HARD_MAX_CLASSIFIER_TOKENS)
        _validate_limit("max_blocks", self.max_blocks, _HARD_MAX_SOURCE_BLOCKS)
        _validate_limit(
            "max_text_chars_per_block",
            self.max_text_chars_per_block,
            _HARD_MAX_BLOCK_TEXT_CHARS,
        )


@dataclass(frozen=True, slots=True)
class SelectionLimits:
    max_response_chars: int = 32 * 1024
    max_items: int = 1_024
    max_selected_blocks: int = 4_096

    def __post_init__(self) -> None:
        _validate_limit(
            "max_response_chars",
            self.max_response_chars,
            _HARD_MAX_SELECTION_RESPONSE_CHARS,
        )
        _validate_limit("max_items", self.max_items, _HARD_MAX_SOURCE_BLOCKS)
        _validate_limit(
            "max_selected_blocks",
            self.max_selected_blocks,
            _HARD_MAX_SOURCE_BLOCKS,
        )


@dataclass(frozen=True, slots=True)
class ReconstructionLimits:
    max_chars: int = 8 * 1024 * 1024
    max_blocks: int = 4_096

    def __post_init__(self) -> None:
        _validate_limit("max_chars", self.max_chars, _HARD_MAX_RECONSTRUCTION_CHARS)
        _validate_limit("max_blocks", self.max_blocks, _HARD_MAX_SOURCE_BLOCKS)


DEFAULT_CLASSIFIER_INPUT_LIMITS = ClassifierInputLimits()
DEFAULT_SELECTION_LIMITS = SelectionLimits()
DEFAULT_RECONSTRUCTION_LIMITS = ReconstructionLimits()


@dataclass(frozen=True, slots=True)
class ClassifierInput:
    """Bounded payload plus complete exposure and granularity provenance.

    ``coarse_selectable_container_ids`` identifies direct-text compound
    elements for which the current DOM IR cannot expose a smaller element
    leaf. Callers can measure that residual representation ceiling instead of
    silently treating a whole cell/list item as leaf-level selection.
    """

    payload: str
    schema_version: str
    source_schema_version: str
    source_digest: str
    payload_digest: str
    token_accounting: str
    chars: int
    tokens: int
    source_block_count: int
    source_selectable_block_count: int
    serialization_container_ids: tuple[str, ...]
    coarse_selectable_container_ids: tuple[str, ...]
    included_block_ids: tuple[str, ...]
    omitted_selectable_block_ids: tuple[str, ...]
    text_truncated_block_ids: tuple[str, ...]
    ancestor_path_truncated_block_ids: tuple[str, ...]
    truncated: bool
    truncation_reasons: tuple[str, ...]
    source_ir_truncated: bool
    source_ir_truncation_reasons: tuple[str, ...]
    limits: ClassifierInputLimits

    @property
    def included_block_count(self) -> int:
        return len(self.included_block_ids)

    @property
    def omitted_block_count(self) -> int:
        return len(self.omitted_selectable_block_ids)


@dataclass(frozen=True, slots=True)
class BlockSelection:
    schema_version: str
    source_digest: str
    classifier_payload_digest: str
    response_digest: str
    selected_ids: tuple[str, ...]
    raw_item_count: int
    range_count: int
    response_chars: int

    @property
    def selected_count(self) -> int:
        return len(self.selected_ids)


@dataclass(frozen=True, slots=True)
class Reconstruction:
    html: str
    strategy: str
    source_digest: str
    selection_response_digest: str
    selected_ids: tuple[str, ...]
    emitted_ids: tuple[str, ...]
    omitted_ids: tuple[str, ...]
    wrapper_ids: tuple[str, ...]
    source_text_truncated_ids: tuple[str, ...]
    source_html_truncated_ids: tuple[str, ...]
    source_container_html_truncated_ids: tuple[str, ...]
    chars: int
    selected_complete: bool
    complete: bool
    truncated: bool
    truncation_reasons: tuple[str, ...]
    source_ir_truncated: bool
    source_ir_truncation_reasons: tuple[str, ...]
    output_digest: str
    limits: ReconstructionLimits

    @property
    def emitted_count(self) -> int:
        return len(self.emitted_ids)

    @property
    def omitted_count(self) -> int:
        return len(self.omitted_ids)


@dataclass(frozen=True, slots=True)
class _ValidatedDocument:
    blocks: tuple[SemanticBlock, ...]
    selectable_blocks: tuple[SemanticBlock, ...]
    by_id: dict[str, SemanticBlock]
    digest: str
    source_truncated: bool
    source_truncation_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreparedDocumentIR:
    """One validated and hashed IR context reusable across all three stages."""

    _validated: _ValidatedDocument

    @property
    def source_digest(self) -> str:
        return self._validated.digest

    @property
    def source_block_count(self) -> int:
        return len(self._validated.blocks)

    @property
    def source_selectable_block_count(self) -> int:
        return len(self._validated.selectable_blocks)


DocumentInput = DocumentBlocks | PreparedDocumentIR


def utf8_token_upper_bound(value: str) -> int:
    """Conservative token units for byte-level model tokenizers."""

    return len(value.encode("utf-8"))


def prepare_document_ir(document: DocumentInput) -> PreparedDocumentIR:
    """Validate and hash a source IR once for a multi-stage selection request."""

    if isinstance(document, PreparedDocumentIR):
        return document
    return PreparedDocumentIR(_validate_document(document))


def build_classifier_input(
    document: DocumentInput,
    *,
    limits: ClassifierInputLimits = DEFAULT_CLASSIFIER_INPUT_LIMITS,
    token_counter: TokenCounter = utf8_token_upper_bound,
    token_accounting: str = UTF8_TOKEN_ACCOUNTING,
) -> ClassifierInput:
    """Build deterministic JSONL block records within all supplied budgets."""

    validated = _coerce_document(document)
    _validate_token_accounting(token_accounting)
    header = _classifier_header(validated)
    payload = _json_dumps(header)
    header_tokens = _count_tokens(token_counter, payload)
    if len(payload) > limits.max_chars or header_tokens > limits.max_tokens:
        raise ClassifierBudgetError("classifier header exceeds the configured budget")

    included_ids: list[str] = []
    text_truncated_ids: list[str] = []
    path_truncated_ids: list[str] = []
    truncation_reasons: list[str] = []
    stopped_for_budget = False

    for block in validated.selectable_blocks:
        if len(included_ids) >= limits.max_blocks:
            _add_reason(truncation_reasons, "max_blocks")
            break

        bounded_text = block.text[: limits.max_text_chars_per_block]
        text_was_truncated = len(bounded_text) < len(block.text)
        ancestor_path, path_was_truncated = _ancestor_path(block, validated)
        full_record = _serialize_classifier_record(
            block,
            bounded_text,
            text_was_truncated,
            ancestor_path,
            path_was_truncated,
        )
        full_candidate = f"{payload}\n{full_record}"
        full_chars = len(full_candidate)
        full_tokens = _count_tokens(token_counter, full_candidate)

        if full_chars <= limits.max_chars and full_tokens <= limits.max_tokens:
            payload = full_candidate
            included_ids.append(block.id)
            if text_was_truncated:
                text_truncated_ids.append(block.id)
                _add_reason(truncation_reasons, "block_text_chars")
            if path_was_truncated:
                path_truncated_ids.append(block.id)
                _add_reason(truncation_reasons, "ancestor_path_chars")
            continue

        if full_chars > limits.max_chars:
            _add_reason(truncation_reasons, "max_chars")
        if full_tokens > limits.max_tokens:
            _add_reason(truncation_reasons, "max_tokens")

        fitted_record = _fit_classifier_record(
            payload,
            block,
            bounded_text,
            ancestor_path,
            path_was_truncated,
            limits,
            token_counter,
        )
        if fitted_record is not None:
            payload = f"{payload}\n{fitted_record}"
            included_ids.append(block.id)
            text_truncated_ids.append(block.id)
            _add_reason(truncation_reasons, "block_text_chars")
            if path_was_truncated:
                path_truncated_ids.append(block.id)
                _add_reason(truncation_reasons, "ancestor_path_chars")
        stopped_for_budget = True
        break

    all_ids = tuple(block.id for block in validated.selectable_blocks)
    included = tuple(included_ids)
    omitted = all_ids[len(included) :]
    if omitted and not stopped_for_budget and len(included) >= limits.max_blocks:
        _add_reason(truncation_reasons, "max_blocks")
    if omitted and not truncation_reasons:
        _add_reason(truncation_reasons, "classifier_budget")
    if validated.source_truncated:
        _add_reason(truncation_reasons, "source_ir")

    tokens = _count_tokens(token_counter, payload)
    if len(payload) > limits.max_chars or tokens > limits.max_tokens:
        raise ClassifierBudgetError("internal error: classifier budget was exceeded")

    return ClassifierInput(
        payload=payload,
        schema_version=CLASSIFIER_INPUT_SCHEMA_VERSION,
        source_schema_version=DOCUMENT_IR_SCHEMA_VERSION,
        source_digest=validated.digest,
        payload_digest=_sha256(payload),
        token_accounting=token_accounting,
        chars=len(payload),
        tokens=tokens,
        source_block_count=len(validated.blocks),
        source_selectable_block_count=len(validated.selectable_blocks),
        serialization_container_ids=tuple(
            block.id for block in validated.blocks if not block.selectable
        ),
        coarse_selectable_container_ids=tuple(
            block.id for block in validated.selectable_blocks if not block.atomic
        ),
        included_block_ids=included,
        omitted_selectable_block_ids=omitted,
        text_truncated_block_ids=tuple(text_truncated_ids),
        ancestor_path_truncated_block_ids=tuple(path_truncated_ids),
        truncated=bool(
            omitted or text_truncated_ids or path_truncated_ids or validated.source_truncated
        ),
        truncation_reasons=tuple(truncation_reasons),
        source_ir_truncated=validated.source_truncated,
        source_ir_truncation_reasons=validated.source_truncation_reasons,
        limits=limits,
    )


def parse_block_selection(
    response: str,
    *,
    classifier_input: ClassifierInput,
    document: DocumentInput,
    limits: SelectionLimits = DEFAULT_SELECTION_LIMITS,
) -> BlockSelection:
    """Strictly parse and validate one model selection response."""

    validated = _coerce_document(document)
    _validate_classifier_input(classifier_input, validated)
    if type(response) is not str:
        raise InvalidBlockSelection("selection response must be a string")
    if len(response) > limits.max_response_chars:
        raise InvalidBlockSelection("selection response exceeds max_response_chars")

    decoded = _strict_json_object(response)
    expected_keys = {"schema_version", "source_digest", "selected"}
    if set(decoded) != expected_keys:
        raise InvalidBlockSelection("selection response has missing or unknown keys")
    if decoded["schema_version"] != SELECTION_SCHEMA_VERSION:
        raise InvalidBlockSelection("unsupported selection schema_version")
    if decoded["source_digest"] != validated.digest:
        raise InvalidBlockSelection("selection source_digest does not match the document")

    raw_selected = decoded["selected"]
    if type(raw_selected) is not list:
        raise InvalidBlockSelection("selected must be a JSON array")
    selected_items = cast("list[object]", raw_selected)
    if len(selected_items) > limits.max_items:
        raise InvalidBlockSelection("selection contains too many raw items")

    allowed_ids = classifier_input.included_block_ids
    allowed_index = {block_id: index for index, block_id in enumerate(allowed_ids)}
    expanded: list[str] = []
    seen_raw: set[str] = set()
    seen_ids: set[str] = set()
    previous_order = -1
    range_count = 0

    for raw_item in selected_items:
        if type(raw_item) is not str:
            raise InvalidBlockSelection("every selected item must be a string")
        item = raw_item
        if item in seen_raw:
            raise InvalidBlockSelection("selection contains a duplicate raw item")
        seen_raw.add(item)

        match = _SELECTION_ITEM_RE.fullmatch(item)
        if match is None:
            raise InvalidBlockSelection("selection item is not a block ID or contiguous range")
        start_id = match.group(1)
        end_id = match.group(2)
        if start_id not in allowed_index:
            raise InvalidBlockSelection(f"unknown or unexposed block ID: {start_id}")

        if end_id is None:
            item_ids: tuple[str, ...] = (start_id,)
        else:
            range_count += 1
            if end_id not in allowed_index:
                raise InvalidBlockSelection(f"unknown or unexposed block ID: {end_id}")
            start_index = allowed_index[start_id]
            end_index = allowed_index[end_id]
            if start_index >= end_index:
                raise InvalidBlockSelection("selection ranges must be forward and non-singleton")
            item_ids = allowed_ids[start_index : end_index + 1]

        if len(expanded) + len(item_ids) > limits.max_selected_blocks:
            raise InvalidBlockSelection("selection expands to too many blocks")
        for block_id in item_ids:
            order = allowed_index[block_id]
            if block_id in seen_ids:
                raise InvalidBlockSelection("selection contains duplicate or overlapping blocks")
            if order <= previous_order:
                raise InvalidBlockSelection("selection IDs are not in strict DOM order")
            seen_ids.add(block_id)
            expanded.append(block_id)
            previous_order = order

    selected_ids = tuple(expanded)
    _reject_ancestor_descendant_overlap(selected_ids, validated)
    return BlockSelection(
        schema_version=SELECTION_SCHEMA_VERSION,
        source_digest=validated.digest,
        classifier_payload_digest=classifier_input.payload_digest,
        response_digest=_sha256(response),
        selected_ids=selected_ids,
        raw_item_count=len(selected_items),
        range_count=range_count,
        response_chars=len(response),
    )


def reconstruct_block_selection(
    document: DocumentInput,
    selection: BlockSelection,
    *,
    limits: ReconstructionLimits = DEFAULT_RECONSTRUCTION_LIMITS,
) -> Reconstruction:
    """Reconstruct a minimal ancestor skeleton and complete leaves in DOM order."""

    validated = _coerce_document(document)
    _validate_selection(selection, validated)
    selected = selection.selected_ids
    reasons: list[str] = []
    wrapper_cache: dict[str, tuple[str, str]] = {}

    source_text_truncated = tuple(
        block_id for block_id in selected if validated.by_id[block_id].text_truncated
    )
    source_html_truncated = tuple(
        block_id for block_id in selected if validated.by_id[block_id].html_truncated
    )

    candidate_count = min(len(selected), limits.max_blocks)
    if candidate_count < len(selected):
        _add_reason(reasons, "max_blocks")
    for index, block_id in enumerate(selected[:candidate_count]):
        block = validated.by_id[block_id]
        if block.html_truncated:
            candidate_count = index
            _add_reason(reasons, f"source_block_html_truncated:{block_id}")
            break
        if not block.outer_html:
            candidate_count = index
            _add_reason(reasons, f"source_block_html_missing:{block_id}")
            break
        if not _selected_outer_html_is_complete(block):
            candidate_count = index
            _add_reason(reasons, f"source_block_html_invalid:{block_id}")
            break
        ancestor_issue = _ancestor_wrapper_issue(
            validated,
            block_id,
            wrapper_cache,
        )
        if ancestor_issue is not None:
            candidate_count = index
            _add_reason(reasons, f"{ancestor_issue[0]}:{ancestor_issue[1]}")
            break

    emitted_count = candidate_count
    html, wrapper_ids, container_truncated_ids = _render_selected_prefix(
        validated,
        selected[:emitted_count],
        wrapper_cache,
    )
    if len(html) > limits.max_chars:
        low = 0
        high = emitted_count
        best_count = 0
        best_render: tuple[str, tuple[str, ...], tuple[str, ...]] = ("", (), ())
        while low <= high:
            middle = (low + high) // 2
            rendered = _render_selected_prefix(
                validated,
                selected[:middle],
                wrapper_cache,
            )
            if len(rendered[0]) <= limits.max_chars:
                best_count = middle
                best_render = rendered
                low = middle + 1
            else:
                high = middle - 1
        emitted_count = best_count
        html, wrapper_ids, container_truncated_ids = best_render
        _add_reason(reasons, "max_chars")

    emitted_ids = selected[:emitted_count]
    omitted_ids = selected[emitted_count:]
    if validated.source_truncated:
        _add_reason(reasons, "source_ir")
    if container_truncated_ids:
        _add_reason(reasons, "source_container_html_truncated")
    selected_complete = not omitted_ids
    complete = selected_complete and not validated.source_truncated
    return Reconstruction(
        html=html,
        strategy=RECONSTRUCTION_STRATEGY,
        source_digest=validated.digest,
        selection_response_digest=selection.response_digest,
        selected_ids=selected,
        emitted_ids=emitted_ids,
        omitted_ids=omitted_ids,
        wrapper_ids=wrapper_ids,
        source_text_truncated_ids=source_text_truncated,
        source_html_truncated_ids=source_html_truncated,
        source_container_html_truncated_ids=container_truncated_ids,
        chars=len(html),
        selected_complete=selected_complete,
        complete=complete,
        truncated=not complete,
        truncation_reasons=tuple(reasons),
        source_ir_truncated=validated.source_truncated,
        source_ir_truncation_reasons=validated.source_truncation_reasons,
        output_digest=_sha256(html),
        limits=limits,
    )


class _RootStartTagParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.tag: str | None = None
        self.raw_start_tag: str | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del attrs
        if self.tag is None:
            self.tag = tag
            self.raw_start_tag = self.get_starttag_text()

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)


def _container_wrapper(block: SemanticBlock) -> tuple[str, str]:
    parser = _RootStartTagParser()
    try:
        parser.feed(block.outer_html)
        parser.close()
    except Exception as error:
        raise InvalidDocumentIR("container outer_html is not parseable") from error
    if parser.tag != block.tag or parser.raw_start_tag is None:
        raise InvalidDocumentIR("container outer_html does not start with its declared tag")
    return parser.raw_start_tag, f"</{block.tag}>"


def _selected_outer_html_is_complete(block: SemanticBlock) -> bool:
    try:
        opening, closing = _container_wrapper(block)
    except InvalidDocumentIR:
        return False
    if not block.outer_html.startswith(opening):
        return False
    return block.tag in _VOID_TAGS or block.outer_html.rstrip().endswith(closing)


def _ancestor_wrapper_issue(
    validated: _ValidatedDocument,
    block_id: str,
    wrapper_cache: dict[str, tuple[str, str]],
) -> tuple[str, str] | None:
    parent_id = validated.by_id[block_id].parent_id
    while parent_id is not None:
        parent = validated.by_id[parent_id]
        if not parent.outer_html:
            return "source_container_html_missing", parent_id
        if parent_id not in wrapper_cache:
            try:
                wrapper_cache[parent_id] = _container_wrapper(parent)
            except InvalidDocumentIR:
                return "source_container_html_invalid", parent_id
        parent_id = parent.parent_id
    return None


def _render_selected_prefix(
    validated: _ValidatedDocument,
    selected_ids: tuple[str, ...],
    wrapper_cache: dict[str, tuple[str, str]],
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    if not selected_ids:
        return "", (), ()

    required: set[str] = set(selected_ids)
    for block_id in selected_ids:
        parent_id = validated.by_id[block_id].parent_id
        while parent_id is not None:
            required.add(parent_id)
            parent_id = validated.by_id[parent_id].parent_id

    children: dict[str | None, list[str]] = {}
    for block in validated.blocks:
        if block.id not in required:
            continue
        parent_id = block.parent_id if block.parent_id in required else None
        children.setdefault(parent_id, []).append(block.id)

    selected = set(selected_ids)
    wrapper_ids = tuple(
        block.id for block in validated.blocks if block.id in required and block.id not in selected
    )
    container_truncated_ids = tuple(
        block_id for block_id in wrapper_ids if validated.by_id[block_id].html_truncated
    )

    def render(block_id: str) -> str:
        block = validated.by_id[block_id]
        if block_id in selected:
            return block.outer_html
        wrapper = wrapper_cache.get(block_id)
        if wrapper is None:
            wrapper = _container_wrapper(block)
            wrapper_cache[block_id] = wrapper
        inner = "".join(render(child_id) for child_id in children.get(block_id, ()))
        return f"{wrapper[0]}{inner}{wrapper[1]}"

    html = "\n".join(render(root_id) for root_id in children.get(None, ()))
    return html, wrapper_ids, container_truncated_ids


def _coerce_document(document: DocumentInput) -> _ValidatedDocument:
    if isinstance(document, PreparedDocumentIR):
        if not isinstance(document._validated, _ValidatedDocument):
            raise InvalidDocumentIR("prepared document context is invalid")
        return document._validated
    return _validate_document(document)


def _validate_document(document: DocumentBlocks) -> _ValidatedDocument:
    if getattr(document, "schema_version", None) != DOCUMENT_IR_SCHEMA_VERSION:
        raise InvalidDocumentIR("unsupported or missing document IR schema_version")
    try:
        blocks = tuple(document.blocks)
    except (AttributeError, TypeError) as error:
        raise InvalidDocumentIR("document blocks must be a finite sequence") from error
    if len(blocks) > _HARD_MAX_SOURCE_BLOCKS:
        raise InvalidDocumentIR("document contains too many blocks")
    if type(getattr(document, "block_count", None)) is not int:
        raise InvalidDocumentIR("document block_count must be an integer")
    if document.block_count != len(blocks):
        raise InvalidDocumentIR("document block_count does not match blocks")
    if type(getattr(document, "truncated", None)) is not bool:
        raise InvalidDocumentIR("document truncated flag must be boolean")

    reasons_value = getattr(document, "truncation_reasons", None)
    if not isinstance(reasons_value, Sequence) or isinstance(reasons_value, (str, bytes)):
        raise InvalidDocumentIR("document truncation_reasons must be a sequence")
    reasons = tuple(reasons_value)
    if len(reasons) > 32 or any(type(reason) is not str or len(reason) > 128 for reason in reasons):
        raise InvalidDocumentIR("document truncation_reasons are invalid")

    by_id: dict[str, SemanticBlock] = {}
    for expected_order, block in enumerate(blocks):
        _validate_block(block, expected_order, by_id)
        by_id[block.id] = block

    digest = _document_digest(
        blocks,
        source_truncated=document.truncated,
        source_truncation_reasons=reasons,
    )
    return _ValidatedDocument(
        blocks=blocks,
        selectable_blocks=tuple(block for block in blocks if block.selectable),
        by_id=by_id,
        digest=digest,
        source_truncated=document.truncated,
        source_truncation_reasons=reasons,
    )


def _validate_block(
    block: SemanticBlock,
    expected_order: int,
    earlier: dict[str, SemanticBlock],
) -> None:
    block_id = getattr(block, "id", None)
    if type(block_id) is not str or _BLOCK_ID_RE.fullmatch(block_id) is None:
        raise InvalidDocumentIR("block has an invalid stable ID")
    if block_id != f"block-{expected_order:06}":
        raise InvalidDocumentIR("block IDs must match contiguous DOM order")
    if block_id in earlier:
        raise InvalidDocumentIR("document contains duplicate block IDs")
    if type(getattr(block, "order", None)) is not int or block.order != expected_order:
        raise InvalidDocumentIR("block orders must be contiguous and stable")

    parent_id = getattr(block, "parent_id", None)
    if parent_id is not None and type(parent_id) is not str:
        raise InvalidDocumentIR("block parent_id must be a string or null")
    if parent_id is not None:
        parent = earlier.get(parent_id)
        if parent is None:
            raise InvalidDocumentIR("block parent_id must name an earlier block")
        if parent.atomic:
            raise InvalidDocumentIR("an atomic block cannot be a semantic parent")
        if block.depth <= parent.depth:
            raise InvalidDocumentIR("child block depth must exceed its semantic parent")

    for field_name in ("tag", "role"):
        value = getattr(block, field_name, None)
        if type(value) is not str or not value or len(value) > 128:
            raise InvalidDocumentIR(f"block {field_name} is invalid")
    for field_name in (
        "atomic",
        "selectable",
        "preserve_whitespace",
        "text_truncated",
        "html_truncated",
        "features_truncated",
    ):
        if type(getattr(block, field_name, None)) is not bool:
            raise InvalidDocumentIR(f"block {field_name} must be boolean")
    if block.atomic and not block.selectable:
        raise InvalidDocumentIR("atomic blocks must be selectable")
    if type(getattr(block, "text", None)) is not str:
        raise InvalidDocumentIR("block text must be a string")
    if type(getattr(block, "outer_html", None)) is not str:
        raise InvalidDocumentIR("block outer_html must be a string")
    if len(block.text) > _HARD_MAX_BLOCK_TEXT_CHARS:
        raise InvalidDocumentIR("block text exceeds the hard contract limit")
    if len(block.outer_html) > _HARD_MAX_BLOCK_HTML_CHARS:
        raise InvalidDocumentIR("block outer_html exceeds the hard contract limit")

    for field_name in (
        "depth",
        "word_count",
        "text_bytes",
        "html_bytes",
        "link_count",
        "link_text_bytes",
        "descendant_element_count",
    ):
        _validate_nonnegative_int(field_name, getattr(block, field_name, None))
    if len(block.text.encode("utf-8")) > block.text_bytes:
        raise InvalidDocumentIR("stored block text exceeds declared text_bytes")
    if len(block.outer_html.encode("utf-8")) > block.html_bytes:
        raise InvalidDocumentIR("stored block HTML exceeds declared html_bytes")
    for field_name in ("text_density", "link_density"):
        value = getattr(block, field_name, None)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidDocumentIR(f"block {field_name} must be numeric")
        numeric = float(value)
        if not math.isfinite(numeric) or not 0.0 <= numeric <= 1.0:
            raise InvalidDocumentIR(f"block {field_name} must be between zero and one")


def _classifier_header(validated: _ValidatedDocument) -> dict[str, object]:
    return {
        "fields": {
            "a": "atomic",
            "d": "depth",
            "de": "descendant_elements",
            "h": "source_html_bytes",
            "i": "stable_block_id",
            "l": "link_count",
            "ld": "link_density",
            "o": "dom_order",
            "p": "semantic_parent_id",
            "q": "ancestor_tag_path",
            "qt": "ancestor_path_truncated",
            "r": "role",
            "s": "selectable",
            "t": "tag",
            "tb": "source_text_bytes",
            "td": "text_density",
            "w": "word_count",
            "x": "normalized_text",
            "xt": "text_truncated_for_classifier",
        },
        "response": {
            "keys": ["schema_version", "source_digest", "selected"],
            "range_semantics": "inclusive over exposed units in DOM order",
            "schema_version": SELECTION_SCHEMA_VERSION,
            "selected": [],
            "selected_item_grammar": "EXPOSED_ID or START_ID..END_ID",
            "source_digest": validated.digest,
        },
        "schema_version": CLASSIFIER_INPUT_SCHEMA_VERSION,
        "source_block_count": len(validated.blocks),
        "source_selectable_block_count": len(validated.selectable_blocks),
        "source_digest": validated.digest,
        "source_ir_truncated": validated.source_truncated,
        "source_schema_version": DOCUMENT_IR_SCHEMA_VERSION,
    }


def _ancestor_path(
    block: SemanticBlock,
    validated: _ValidatedDocument,
) -> tuple[str, bool]:
    ancestors: list[str] = []
    parent_id = block.parent_id
    while parent_id is not None:
        parent = validated.by_id[parent_id]
        ancestors.append(parent.tag)
        parent_id = parent.parent_id
    ancestors.reverse()
    path = "/".join(ancestors)
    if len(path) <= 512:
        return path, False
    # Preserve the nearest ancestry, which carries the structural context
    # needed for cell/list/code selection, while marking the compression.
    return path[-512:], True


def _serialize_classifier_record(
    block: SemanticBlock,
    text: str,
    text_truncated: bool,
    ancestor_path: str,
    ancestor_path_truncated: bool,
) -> str:
    return _json_dumps(
        {
            "a": block.atomic,
            "d": block.depth,
            "de": block.descendant_element_count,
            "h": block.html_bytes,
            "i": block.id,
            "l": block.link_count,
            "ld": round(float(block.link_density), 6),
            "o": block.order,
            "p": block.parent_id,
            "q": ancestor_path,
            "qt": ancestor_path_truncated,
            "r": block.role,
            "s": block.selectable,
            "t": block.tag,
            "tb": block.text_bytes,
            "td": round(float(block.text_density), 6),
            "w": block.word_count,
            "x": text,
            "xt": text_truncated,
        }
    )


def _fit_classifier_record(
    payload: str,
    block: SemanticBlock,
    text: str,
    ancestor_path: str,
    ancestor_path_truncated: bool,
    limits: ClassifierInputLimits,
    token_counter: TokenCounter,
) -> str | None:
    low = 0
    high = len(text)
    best: str | None = None
    while low <= high:
        middle = (low + high) // 2
        record = _serialize_classifier_record(
            block,
            text[:middle],
            True,
            ancestor_path,
            ancestor_path_truncated,
        )
        candidate = f"{payload}\n{record}"
        fits = len(candidate) <= limits.max_chars
        if fits:
            fits = _count_tokens(token_counter, candidate) <= limits.max_tokens
        if fits:
            best = record
            low = middle + 1
        else:
            high = middle - 1
    return best


def _strict_json_object(response: str) -> dict[str, object]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise InvalidBlockSelection(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> NoReturn:
        raise InvalidBlockSelection(f"invalid JSON constant: {value}")

    try:
        decoded: object = json.loads(
            response,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as error:
        raise InvalidBlockSelection("selection response is not valid JSON") from error
    if type(decoded) is not dict:
        raise InvalidBlockSelection("selection response must be a JSON object")
    return cast("dict[str, object]", decoded)


def _validate_classifier_input(
    classifier_input: ClassifierInput,
    validated: _ValidatedDocument,
) -> None:
    if classifier_input.schema_version != CLASSIFIER_INPUT_SCHEMA_VERSION:
        raise InvalidBlockSelection("classifier input schema_version is invalid")
    if classifier_input.source_schema_version != DOCUMENT_IR_SCHEMA_VERSION:
        raise InvalidBlockSelection("classifier source schema_version is invalid")
    if classifier_input.source_digest != validated.digest:
        raise InvalidBlockSelection("classifier input belongs to a different document")
    if _sha256(classifier_input.payload) != classifier_input.payload_digest:
        raise InvalidBlockSelection("classifier payload digest is invalid")
    if classifier_input.chars != len(classifier_input.payload):
        raise InvalidBlockSelection("classifier payload character provenance is invalid")
    if (
        classifier_input.chars > classifier_input.limits.max_chars
        or classifier_input.tokens > classifier_input.limits.max_tokens
    ):
        raise InvalidBlockSelection("classifier payload budget provenance is invalid")
    all_ids = tuple(block.id for block in validated.selectable_blocks)
    included = classifier_input.included_block_ids
    omitted = classifier_input.omitted_selectable_block_ids
    if included != all_ids[: len(included)] or omitted != all_ids[len(included) :]:
        raise InvalidBlockSelection(
            "classifier exposed selectable IDs are not a DOM-order prefix"
        )
    if classifier_input.source_block_count != len(validated.blocks):
        raise InvalidBlockSelection("classifier source block count is invalid")
    if classifier_input.source_selectable_block_count != len(all_ids):
        raise InvalidBlockSelection("classifier selectable block count is invalid")
    expected_containers = tuple(
        block.id for block in validated.blocks if not block.selectable
    )
    if classifier_input.serialization_container_ids != expected_containers:
        raise InvalidBlockSelection("classifier serialization-container provenance is invalid")
    expected_coarse = tuple(
        block.id for block in validated.selectable_blocks if not block.atomic
    )
    if classifier_input.coarse_selectable_container_ids != expected_coarse:
        raise InvalidBlockSelection("classifier coarse-container provenance is invalid")
    included_set = set(included)
    if not set(classifier_input.text_truncated_block_ids).issubset(included_set):
        raise InvalidBlockSelection("classifier text-truncation provenance is invalid")
    if not set(classifier_input.ancestor_path_truncated_block_ids).issubset(included_set):
        raise InvalidBlockSelection("classifier path-truncation provenance is invalid")


def _validate_selection(
    selection: BlockSelection,
    validated: _ValidatedDocument,
) -> None:
    if selection.schema_version != SELECTION_SCHEMA_VERSION:
        raise InvalidBlockSelection("selection schema_version is invalid")
    if selection.source_digest != validated.digest:
        raise InvalidBlockSelection("selection belongs to a different document")
    if _SHA256_RE.fullmatch(selection.response_digest) is None:
        raise InvalidBlockSelection("selection response digest is invalid")
    if _SHA256_RE.fullmatch(selection.classifier_payload_digest) is None:
        raise InvalidBlockSelection("selection classifier digest is invalid")
    for field_name in ("raw_item_count", "range_count", "response_chars"):
        value = getattr(selection, field_name)
        if type(value) is not int or value < 0:
            raise InvalidBlockSelection(f"selection {field_name} provenance is invalid")
    if selection.range_count > selection.raw_item_count:
        raise InvalidBlockSelection("selection range provenance is invalid")
    if type(selection.selected_ids) is not tuple:
        raise InvalidBlockSelection("selection IDs must be an immutable tuple")

    seen: set[str] = set()
    previous_order = -1
    for block_id in selection.selected_ids:
        if type(block_id) is not str:
            raise InvalidBlockSelection("selection ID must be a string")
        block = validated.by_id.get(block_id)
        if block is None:
            raise InvalidBlockSelection(f"selection contains unknown block ID: {block_id}")
        if not block.selectable:
            raise InvalidBlockSelection(
                f"selection contains a serialization-only block ID: {block_id}"
            )
        if block_id in seen:
            raise InvalidBlockSelection("selection contains duplicate block IDs")
        if block.order <= previous_order:
            raise InvalidBlockSelection("selection IDs are not in strict DOM order")
        seen.add(block_id)
        previous_order = block.order
    _reject_ancestor_descendant_overlap(selection.selected_ids, validated)


def _reject_ancestor_descendant_overlap(
    selected_ids: tuple[str, ...],
    validated: _ValidatedDocument,
) -> None:
    selected = set(selected_ids)
    for block_id in selected_ids:
        parent_id = validated.by_id[block_id].parent_id
        while parent_id is not None:
            if parent_id in selected:
                raise InvalidBlockSelection(
                    f"selection overlaps ancestor {parent_id} and descendant {block_id}"
                )
            parent_id = validated.by_id[parent_id].parent_id


def _document_digest(
    blocks: tuple[SemanticBlock, ...],
    *,
    source_truncated: bool,
    source_truncation_reasons: tuple[str, ...],
) -> str:
    digest = hashlib.sha256()
    digest.update(
        _json_dumps(
            {
                "schema_version": DOCUMENT_IR_SCHEMA_VERSION,
                "source_truncated": source_truncated,
                "source_truncation_reasons": source_truncation_reasons,
            }
        ).encode("utf-8")
    )
    for block in blocks:
        digest.update(b"\n")
        digest.update(
            _json_dumps(
                {
                    "atomic": block.atomic,
                    "depth": block.depth,
                    "features_truncated": block.features_truncated,
                    "html_truncated": block.html_truncated,
                    "id": block.id,
                    "order": block.order,
                    "outer_html": block.outer_html,
                    "parent_id": block.parent_id,
                    "role": block.role,
                    "selectable": block.selectable,
                    "tag": block.tag,
                    "text": block.text,
                    "text_truncated": block.text_truncated,
                }
            ).encode("utf-8")
        )
    return digest.hexdigest()


def _validate_nonnegative_int(name: str, value: object) -> None:
    if type(value) is not int or value < 0:
        raise InvalidDocumentIR(f"block {name} must be a non-negative integer")


def _validate_token_accounting(value: str) -> None:
    if type(value) is not str or not value or len(value) > 128 or value.strip() != value:
        raise ValueError("token_accounting must be a short, non-blank label")


def _count_tokens(counter: TokenCounter, value: str) -> int:
    try:
        count = counter(value)
    except Exception as error:
        raise BlockContractError("token counter failed") from error
    if type(count) is not int or count < 0:
        raise BlockContractError("token counter must return a non-negative integer")
    return count


def _json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _add_reason(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


__all__ = [
    "CLASSIFIER_INPUT_SCHEMA_VERSION",
    "DEFAULT_CLASSIFIER_INPUT_LIMITS",
    "DEFAULT_RECONSTRUCTION_LIMITS",
    "DEFAULT_SELECTION_LIMITS",
    "DOCUMENT_IR_SCHEMA_VERSION",
    "RECONSTRUCTION_STRATEGY",
    "SELECTION_SCHEMA_VERSION",
    "UTF8_TOKEN_ACCOUNTING",
    "BlockContractError",
    "BlockSelection",
    "ClassifierBudgetError",
    "ClassifierInput",
    "ClassifierInputLimits",
    "DocumentBlocks",
    "DocumentInput",
    "InvalidBlockSelection",
    "InvalidDocumentIR",
    "PreparedDocumentIR",
    "Reconstruction",
    "ReconstructionLimits",
    "SelectionLimits",
    "SemanticBlock",
    "TokenCounter",
    "build_classifier_input",
    "parse_block_selection",
    "prepare_document_ir",
    "reconstruct_block_selection",
    "utf8_token_upper_bound",
]

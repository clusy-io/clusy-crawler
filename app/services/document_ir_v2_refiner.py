"""Fail-closed deterministic structure refinement using ``ordered-dom-ir.v2``.

The refiner is deliberately not wired into the extraction cascade. It accepts
an existing deterministic Markdown candidate, grounds its normalized visible
tokens in the v2 source graph in strict DOM order, promotes grounded text runs
to enclosing source structures, and asks the native v2 serializer—not a model—
to reconstruct the result.

Every acceptance gate is monotonic. A rejection always returns the caller's
candidate byte-for-byte and never exposes a partial reconstruction.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass
from html import unescape

from clusy_native import (
    DEFAULT_DOCUMENT_IR_V2_LIMITS,
    DocumentIRV2Limits,
    NativeDocumentIRV2,
    NativeIRElementV2,
    extract_document_ir_v2,
    reconstruct_document_ir_v2,
)

REFINER_SCHEMA_VERSION = "ordered-dom-ir.v2.deterministic-refiner.1"
MINIMUM_CANDIDATE_AGREEMENT = 0.65

_STRUCTURE_ORDER = ("heading", "list", "code", "table", "math")
_STRUCTURE_TAGS = {
    "h1": "heading",
    "h2": "heading",
    "h3": "heading",
    "h4": "heading",
    "h5": "heading",
    "h6": "heading",
    "ul": "list",
    "ol": "list",
    "dl": "list",
    "pre": "code",
    "code": "code",
    "table": "table",
    "math": "math",
}
_UNTRUSTED_LANDMARK_TAGS = frozenset({"nav", "footer", "aside"})
_UNTRUSTED_LANDMARK_ROLES = frozenset({"navigation", "contentinfo", "complementary"})

_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_ATX_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}(?:\s+|$)")
_SETEXT_RE = re.compile(r"^\s{0,3}(?:=+|-+)\s*$")
_LIST_RE = re.compile(r"^\s{0,3}(?:[-+*]|\d{1,9}[.)])\s+")
_GFM_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
_HTML_HEADING_RE = re.compile(r"<\s*h[1-6]\b", re.IGNORECASE)
_HTML_LIST_RE = re.compile(r"<\s*(?:ul|ol|dl)\b", re.IGNORECASE)
_HTML_CODE_RE = re.compile(r"<\s*(?:pre|code)\b", re.IGNORECASE)
_HTML_TABLE_RE = re.compile(r"<\s*table\b", re.IGNORECASE)
_HTML_MATH_RE = re.compile(r"<\s*math\b", re.IGNORECASE)

_HARD_MAX_CANDIDATE_CHARS = 2 * 1024 * 1024
_HARD_MAX_CANDIDATE_TOKENS = 200_000
_HARD_MAX_SOURCE_TOKENS = 500_000
_HARD_MAX_OCCURRENCES_PER_TOKEN = 4_096
_HARD_MAX_ALIGNMENT_EDGES = 2_000_000
_HARD_MAX_SELECTED_IDS = 100_000
_HARD_MAX_ANCESTRY_STEPS = 20_000_000
_HARD_MAX_OUTPUT_CHARS = 32 * 1024 * 1024


def _validate_int_limit(name: str, value: int, hard_max: int) -> None:
    if type(value) is not int or value <= 0 or value > hard_max:
        raise ValueError(f"{name} must be between 1 and {hard_max}")


def _validate_fraction(name: str, value: float, minimum: float, maximum: float) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a finite number")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < minimum or numeric > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")


@dataclass(frozen=True, slots=True)
class DeterministicRefinerLimits:
    """Hard-bounded alignment, promotion, and verification limits."""

    max_candidate_chars: int = 512 * 1024
    max_candidate_tokens: int = 50_000
    max_source_tokens: int = 250_000
    max_occurrences_per_token: int = 128
    max_alignment_edges: int = 300_000
    max_selected_ids: int = 8_192
    max_ancestry_steps: int = 1_000_000
    max_output_chars: int = 8 * 1024 * 1024
    min_candidate_tokens: int = 3
    min_candidate_agreement: float = MINIMUM_CANDIDATE_AGREEMENT
    max_candidate_order_gap: float = 0.05
    ambiguity_margin: float = 0.02
    min_structure_token_coverage: float = 0.20
    min_plain_run_token_coverage: float = 0.50
    max_visible_token_expansion: float = 3.0

    def __post_init__(self) -> None:
        _validate_int_limit(
            "max_candidate_chars",
            self.max_candidate_chars,
            _HARD_MAX_CANDIDATE_CHARS,
        )
        _validate_int_limit(
            "max_candidate_tokens",
            self.max_candidate_tokens,
            _HARD_MAX_CANDIDATE_TOKENS,
        )
        _validate_int_limit(
            "max_source_tokens",
            self.max_source_tokens,
            _HARD_MAX_SOURCE_TOKENS,
        )
        _validate_int_limit(
            "max_occurrences_per_token",
            self.max_occurrences_per_token,
            _HARD_MAX_OCCURRENCES_PER_TOKEN,
        )
        _validate_int_limit(
            "max_alignment_edges",
            self.max_alignment_edges,
            _HARD_MAX_ALIGNMENT_EDGES,
        )
        _validate_int_limit(
            "max_selected_ids",
            self.max_selected_ids,
            _HARD_MAX_SELECTED_IDS,
        )
        _validate_int_limit(
            "max_ancestry_steps",
            self.max_ancestry_steps,
            _HARD_MAX_ANCESTRY_STEPS,
        )
        _validate_int_limit(
            "max_output_chars",
            self.max_output_chars,
            _HARD_MAX_OUTPUT_CHARS,
        )
        _validate_int_limit(
            "min_candidate_tokens",
            self.min_candidate_tokens,
            self.max_candidate_tokens,
        )
        _validate_fraction(
            "min_candidate_agreement",
            self.min_candidate_agreement,
            MINIMUM_CANDIDATE_AGREEMENT,
            1.0,
        )
        _validate_fraction(
            "max_candidate_order_gap",
            self.max_candidate_order_gap,
            0.0,
            0.35,
        )
        _validate_fraction("ambiguity_margin", self.ambiguity_margin, 0.0, 0.25)
        _validate_fraction(
            "min_structure_token_coverage",
            self.min_structure_token_coverage,
            0.0,
            1.0,
        )
        _validate_fraction(
            "min_plain_run_token_coverage",
            self.min_plain_run_token_coverage,
            0.0,
            1.0,
        )
        _validate_fraction(
            "max_visible_token_expansion",
            self.max_visible_token_expansion,
            1.0,
            20.0,
        )


DEFAULT_DETERMINISTIC_REFINER_LIMITS = DeterministicRefinerLimits()


@dataclass(frozen=True, slots=True)
class DeterministicRefinementResult:
    """Complete accept/reject result; rejected output is always the candidate."""

    schema_version: str
    accepted: bool
    reason: str
    rejection_reasons: tuple[str, ...]
    candidate_markdown: str
    output_markdown: str
    refined_markdown: str | None
    candidate_digest: str
    output_digest: str
    ir_schema_version: str
    ir_complete: bool
    reconstruction_complete: bool
    candidate_token_count: int
    source_token_count: int
    refined_token_count: int
    matched_candidate_token_count: int
    candidate_agreement: float
    candidate_bag_agreement: float
    candidate_order_gap: float
    alternative_agreement: float
    retained_candidate_agreement: float
    source_grounding_agreement: float
    visible_token_expansion: float
    trusted_prose_non_shrink: bool
    alignment_edge_count: int
    ancestry_step_count: int
    matched_text_run_ids: tuple[str, ...]
    promoted_element_ids: tuple[str, ...]
    selected_ids: tuple[str, ...]
    candidate_structures: tuple[str, ...]
    refined_structures: tuple[str, ...]
    added_structures: tuple[str, ...]
    lost_structures: tuple[str, ...]
    limits: DeterministicRefinerLimits


@dataclass(slots=True)
class _State:
    candidate_token_count: int = 0
    source_token_count: int = 0
    refined_token_count: int = 0
    matched_candidate_token_count: int = 0
    candidate_agreement: float = 0.0
    candidate_bag_agreement: float = 0.0
    candidate_order_gap: float = 0.0
    alternative_agreement: float = 0.0
    retained_candidate_agreement: float = 0.0
    source_grounding_agreement: float = 0.0
    visible_token_expansion: float = 0.0
    trusted_prose_non_shrink: bool = False
    alignment_edge_count: int = 0
    ancestry_step_count: int = 0
    ir_schema_version: str = ""
    ir_complete: bool = False
    reconstruction_complete: bool = False
    matched_text_run_ids: tuple[str, ...] = ()
    promoted_element_ids: tuple[str, ...] = ()
    selected_ids: tuple[str, ...] = ()
    candidate_structures: tuple[str, ...] = ()
    refined_structures: tuple[str, ...] = ()
    added_structures: tuple[str, ...] = ()
    lost_structures: tuple[str, ...] = ()


class _RefinementRejectedError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class _TokenBudgetExceededError(Exception):
    pass


class _AlignmentBudgetExceededError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _Alignment:
    pairs: tuple[tuple[int, int], ...]
    edge_count: int

    @property
    def match_count(self) -> int:
        return len(self.pairs)


@dataclass(frozen=True, slots=True)
class _IRIndex:
    elements_by_id: dict[str, NativeIRElementV2]
    source_tokens: tuple[str, ...]
    source_run_ids: tuple[str, ...]
    run_token_counts: dict[str, int]
    run_structures: dict[str, tuple[str, ...]]
    landmark_run_ids: frozenset[str]
    structure_categories: dict[str, str]
    structure_token_counts: dict[str, int]
    ancestry_steps: int


def refine_deterministic_candidate_v2(
    html: str,
    candidate_markdown: str,
    *,
    limits: DeterministicRefinerLimits = DEFAULT_DETERMINISTIC_REFINER_LIMITS,
    ir_limits: DocumentIRV2Limits = DEFAULT_DOCUMENT_IR_V2_LIMITS,
) -> DeterministicRefinementResult:
    """Add missing source-backed structure or return *candidate_markdown* unchanged."""

    if type(html) is not str:
        raise TypeError("html must be a string")
    if type(candidate_markdown) is not str:
        raise TypeError("candidate_markdown must be a string")

    state = _State()
    state.candidate_structures = _ordered_structures(_structure_categories(candidate_markdown))
    candidate_digest = _sha256(candidate_markdown)
    try:
        if not candidate_markdown.strip():
            raise _RefinementRejectedError("empty_candidate")
        if len(candidate_markdown) > limits.max_candidate_chars:
            raise _RefinementRejectedError("candidate_char_budget")
        candidate_visible = _visible_markdown(candidate_markdown)
        candidate_tokens = _normalized_tokens(
            candidate_visible,
            limits.max_candidate_tokens,
        )
        state.candidate_token_count = len(candidate_tokens)
        if len(candidate_tokens) < limits.min_candidate_tokens:
            raise _RefinementRejectedError("insufficient_candidate_tokens")

        try:
            document = extract_document_ir_v2(html, limits=ir_limits)
        except Exception as error:
            raise _RefinementRejectedError("ir_extraction_failure") from error
        state.ir_schema_version = document.schema_version
        state.ir_complete = _ir_is_complete(document)
        if not state.ir_complete:
            raise _RefinementRejectedError("incomplete_ir")

        try:
            index = _build_ir_index(document, limits)
        except _TokenBudgetExceededError as error:
            raise _RefinementRejectedError("source_token_budget") from error
        except _AlignmentBudgetExceededError as error:
            raise _RefinementRejectedError("ancestry_budget") from error
        state.source_token_count = len(index.source_tokens)
        state.ancestry_step_count = index.ancestry_steps
        if not index.source_tokens:
            raise _RefinementRejectedError("empty_source_tokens")

        occurrences = _bounded_occurrences(
            index.source_tokens,
            limits.max_occurrences_per_token,
        )
        try:
            alignment = _align_tokens(
                candidate_tokens,
                occurrences,
                limits.max_alignment_edges,
            )
        except _AlignmentBudgetExceededError as error:
            raise _RefinementRejectedError("alignment_budget") from error
        state.alignment_edge_count = alignment.edge_count
        state.matched_candidate_token_count = alignment.match_count
        state.candidate_agreement = alignment.match_count / len(candidate_tokens)
        state.candidate_bag_agreement = _bag_agreement(
            candidate_tokens,
            index.source_tokens,
        )
        state.candidate_order_gap = max(
            0.0,
            state.candidate_bag_agreement - state.candidate_agreement,
        )
        if state.candidate_agreement < limits.min_candidate_agreement:
            raise _RefinementRejectedError("candidate_agreement")
        if state.candidate_order_gap > limits.max_candidate_order_gap:
            raise _RefinementRejectedError("candidate_order_mismatch")

        if alignment.pairs:
            first_source = alignment.pairs[0][1]
            last_source = alignment.pairs[-1][1]
            try:
                remaining_alignment_edges = limits.max_alignment_edges - state.alignment_edge_count
                alternative = _align_tokens(
                    candidate_tokens,
                    occurrences,
                    remaining_alignment_edges,
                    excluded_source_range=(first_source, last_source),
                )
            except _AlignmentBudgetExceededError as error:
                raise _RefinementRejectedError("alignment_budget") from error
            state.alignment_edge_count += alternative.edge_count
            state.alternative_agreement = alternative.match_count / len(candidate_tokens)
            ambiguity_floor = max(
                limits.min_candidate_agreement,
                state.candidate_agreement - limits.ambiguity_margin,
            )
            if state.alternative_agreement >= ambiguity_floor:
                raise _RefinementRejectedError("ambiguous_source_alignment")

        (
            matched_run_ids,
            promoted_ids,
            selected_ids,
        ) = _build_selection(alignment, index, document, limits)
        state.matched_text_run_ids = matched_run_ids
        state.promoted_element_ids = promoted_ids
        state.selected_ids = selected_ids
        if not selected_ids:
            raise _RefinementRejectedError("empty_selection")

        try:
            reconstructed = reconstruct_document_ir_v2(
                document,
                selected_ids=selected_ids,
            )
        except Exception as error:
            raise _RefinementRejectedError("reconstruction_failure") from error
        state.reconstruction_complete = (
            not reconstructed.truncated
            and reconstructed.source_complete
            and not reconstructed.missing_ids
            and reconstructed.table_grid_complete
            and reconstructed.exact_code_whitespace
        )
        if not state.reconstruction_complete:
            raise _RefinementRejectedError("incomplete_reconstruction")
        refined = reconstructed.markdown
        if not refined.strip():
            raise _RefinementRejectedError("empty_reconstruction")
        if len(refined) > limits.max_output_chars:
            raise _RefinementRejectedError("output_char_budget")

        refined_structures = _structure_categories(refined)
        candidate_structures = set(state.candidate_structures)
        state.refined_structures = _ordered_structures(refined_structures)
        state.added_structures = _ordered_structures(refined_structures - candidate_structures)
        state.lost_structures = _ordered_structures(candidate_structures - refined_structures)
        if state.lost_structures:
            raise _RefinementRejectedError("candidate_structure_loss")
        if not state.added_structures:
            raise _RefinementRejectedError("no_missing_structure_added")

        refined_visible = _visible_markdown(refined)
        try:
            refined_tokens = _normalized_tokens(
                refined_visible,
                limits.max_source_tokens,
            )
        except _TokenBudgetExceededError as error:
            raise _RefinementRejectedError("refined_token_budget") from error
        state.refined_token_count = len(refined_tokens)
        if not refined_tokens:
            raise _RefinementRejectedError("empty_refined_tokens")
        grounded_count = _subsequence_match_count(
            refined_tokens,
            index.source_tokens,
        )
        state.source_grounding_agreement = grounded_count / len(refined_tokens)
        if grounded_count != len(refined_tokens):
            raise _RefinementRejectedError("refined_source_grounding")

        try:
            remaining_alignment_edges = limits.max_alignment_edges - state.alignment_edge_count
            retained_alignment = _align_tokens(
                candidate_tokens,
                _bounded_occurrences(
                    refined_tokens,
                    limits.max_occurrences_per_token,
                ),
                remaining_alignment_edges,
            )
        except _AlignmentBudgetExceededError as error:
            raise _RefinementRejectedError("retention_alignment_budget") from error
        state.alignment_edge_count += retained_alignment.edge_count
        state.retained_candidate_agreement = retained_alignment.match_count / len(candidate_tokens)
        if state.retained_candidate_agreement + 1e-12 < state.candidate_agreement:
            raise _RefinementRejectedError("candidate_grounding_shrink")

        initially_grounded_candidate_indices = {
            candidate_index for candidate_index, _ in alignment.pairs
        }
        retained_candidate_indices = {
            candidate_index for candidate_index, _ in retained_alignment.pairs
        }
        state.trusted_prose_non_shrink = initially_grounded_candidate_indices.issubset(
            retained_candidate_indices
        )
        if not state.trusted_prose_non_shrink:
            raise _RefinementRejectedError("trusted_prose_shrink")
        state.visible_token_expansion = len(refined_tokens) / len(candidate_tokens)
        if state.visible_token_expansion > limits.max_visible_token_expansion:
            raise _RefinementRejectedError("visible_token_expansion")

        return _finish(
            state,
            candidate_markdown,
            candidate_digest,
            limits,
            accepted=True,
            reason="accepted",
            refined=refined,
        )
    except _TokenBudgetExceededError:
        return _finish(
            state,
            candidate_markdown,
            candidate_digest,
            limits,
            accepted=False,
            reason="candidate_token_budget",
        )
    except _RefinementRejectedError as rejection:
        return _finish(
            state,
            candidate_markdown,
            candidate_digest,
            limits,
            accepted=False,
            reason=rejection.reason,
        )


def _finish(
    state: _State,
    candidate: str,
    candidate_digest: str,
    limits: DeterministicRefinerLimits,
    *,
    accepted: bool,
    reason: str,
    refined: str | None = None,
) -> DeterministicRefinementResult:
    output = refined if accepted and refined is not None else candidate
    return DeterministicRefinementResult(
        schema_version=REFINER_SCHEMA_VERSION,
        accepted=accepted,
        reason=reason,
        rejection_reasons=() if accepted else (reason,),
        candidate_markdown=candidate,
        output_markdown=output,
        refined_markdown=refined if accepted else None,
        candidate_digest=candidate_digest,
        output_digest=_sha256(output),
        ir_schema_version=state.ir_schema_version,
        ir_complete=state.ir_complete,
        reconstruction_complete=state.reconstruction_complete,
        candidate_token_count=state.candidate_token_count,
        source_token_count=state.source_token_count,
        refined_token_count=state.refined_token_count,
        matched_candidate_token_count=state.matched_candidate_token_count,
        candidate_agreement=state.candidate_agreement,
        candidate_bag_agreement=state.candidate_bag_agreement,
        candidate_order_gap=state.candidate_order_gap,
        alternative_agreement=state.alternative_agreement,
        retained_candidate_agreement=state.retained_candidate_agreement,
        source_grounding_agreement=state.source_grounding_agreement,
        visible_token_expansion=state.visible_token_expansion,
        trusted_prose_non_shrink=state.trusted_prose_non_shrink,
        alignment_edge_count=state.alignment_edge_count,
        ancestry_step_count=state.ancestry_step_count,
        matched_text_run_ids=state.matched_text_run_ids,
        promoted_element_ids=state.promoted_element_ids,
        selected_ids=state.selected_ids,
        candidate_structures=state.candidate_structures,
        refined_structures=state.refined_structures,
        added_structures=state.added_structures,
        lost_structures=state.lost_structures,
        limits=limits,
    )


def _ir_is_complete(document: NativeDocumentIRV2) -> bool:
    return (
        document.schema_version == "ordered-dom-ir.v2"
        and document.source_complete
        and not document.truncated
        and not document.input_truncated
        and not document.nodes_truncated
        and not document.depth_truncated
        and not document.elements_truncated
        and not document.text_runs_truncated
        and document.text_truncated_runs == 0
        and not document.table_grid_truncated
        and document.math_truncated_nodes == 0
        and document.source_mapping_complete
        and document.unmapped_explicit_element_count == 0
    )


def _build_ir_index(
    document: NativeDocumentIRV2,
    limits: DeterministicRefinerLimits,
) -> _IRIndex:
    elements_by_id: dict[str, NativeIRElementV2] = {}
    for element in document.elements:
        if element.id in elements_by_id:
            raise _RefinementRejectedError("invalid_ir_duplicate_element")
        elements_by_id[element.id] = element

    source_tokens: list[str] = []
    source_run_ids: list[str] = []
    run_token_counts: dict[str, int] = {}
    text_runs = sorted(document.text_runs, key=lambda run: run.order)
    seen_run_ids: set[str] = set()
    for run in text_runs:
        if run.id in seen_run_ids or run.parent_id not in elements_by_id:
            raise _RefinementRejectedError("invalid_ir_text_run")
        seen_run_ids.add(run.id)
        remaining = limits.max_source_tokens - len(source_tokens)
        if remaining <= 0:
            raise _TokenBudgetExceededError
        tokens = _normalized_tokens(run.text, remaining)
        run_token_counts[run.id] = len(tokens)
        source_tokens.extend(tokens)
        source_run_ids.extend([run.id] * len(tokens))
        if len(source_tokens) > limits.max_source_tokens:
            raise _TokenBudgetExceededError

    run_structures: dict[str, tuple[str, ...]] = {}
    landmark_run_ids: set[str] = set()
    structure_categories: dict[str, str] = {}
    structure_token_counts: dict[str, int] = defaultdict(int)
    ancestry_steps = 0
    for run in text_runs:
        structures: list[str] = []
        seen_ancestors: set[str] = set()
        parent_id: str | None = run.parent_id
        landmark = False
        while parent_id is not None:
            ancestry_steps += 1
            if ancestry_steps > limits.max_ancestry_steps:
                raise _AlignmentBudgetExceededError
            if parent_id in seen_ancestors:
                raise _RefinementRejectedError("invalid_ir_parent_cycle")
            seen_ancestors.add(parent_id)
            ancestor = elements_by_id.get(parent_id)
            if ancestor is None:
                raise _RefinementRejectedError("invalid_ir_parent")
            category = _STRUCTURE_TAGS.get(ancestor.tag)
            if category is not None:
                structures.append(ancestor.id)
                structure_categories[ancestor.id] = category
            if (
                ancestor.tag in _UNTRUSTED_LANDMARK_TAGS
                or ancestor.role in _UNTRUSTED_LANDMARK_ROLES
            ):
                landmark = True
            parent_id = ancestor.parent_id
        run_structures[run.id] = tuple(structures)
        if landmark:
            landmark_run_ids.add(run.id)
        token_count = run_token_counts[run.id]
        for structure_id in structures:
            structure_token_counts[structure_id] += token_count

    return _IRIndex(
        elements_by_id=elements_by_id,
        source_tokens=tuple(source_tokens),
        source_run_ids=tuple(source_run_ids),
        run_token_counts=run_token_counts,
        run_structures=run_structures,
        landmark_run_ids=frozenset(landmark_run_ids),
        structure_categories=structure_categories,
        structure_token_counts=dict(structure_token_counts),
        ancestry_steps=ancestry_steps,
    )


def _build_selection(
    alignment: _Alignment,
    index: _IRIndex,
    document: NativeDocumentIRV2,
    limits: DeterministicRefinerLimits,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    matched_run_counts: Counter[str] = Counter()
    matched_structure_counts: Counter[str] = Counter()
    first_source_position: dict[str, int] = {}
    for _, source_position in alignment.pairs:
        run_id = index.source_run_ids[source_position]
        matched_run_counts[run_id] += 1
        first_source_position.setdefault(run_id, source_position)
        for structure_id in index.run_structures[run_id]:
            matched_structure_counts[structure_id] += 1

    matched_run_ids = tuple(
        sorted(matched_run_counts, key=lambda run_id: first_source_position[run_id])
    )
    if any(run_id in index.landmark_run_ids for run_id in matched_run_ids):
        raise _RefinementRejectedError("untrusted_landmark_alignment")

    promoted_ids: set[str] = set()
    plain_run_ids: set[str] = set()
    for run_id in matched_run_ids:
        structures = index.run_structures[run_id]
        if structures:
            promoted_ids.add(structures[-1])
        else:
            total = index.run_token_counts[run_id]
            coverage = matched_run_counts[run_id] / total if total else 0.0
            if coverage < limits.min_plain_run_token_coverage:
                raise _RefinementRejectedError("plain_run_coverage")
            plain_run_ids.add(run_id)

    for structure_id in promoted_ids:
        total = index.structure_token_counts.get(structure_id, 0)
        matched = matched_structure_counts[structure_id]
        coverage = matched / total if total else 0.0
        if coverage < limits.min_structure_token_coverage:
            raise _RefinementRejectedError("structure_token_coverage")

    element_orders = {element.id: element.order for element in document.elements}
    text_orders = {run.id: run.order for run in document.text_runs}
    promoted = tuple(sorted(promoted_ids, key=element_orders.__getitem__))
    selected = tuple(
        sorted(
            promoted_ids | plain_run_ids,
            key=lambda node_id: element_orders.get(
                node_id,
                text_orders.get(node_id, 2**63 - 1),
            ),
        )
    )
    if len(selected) > limits.max_selected_ids:
        raise _RefinementRejectedError("selection_id_budget")
    return matched_run_ids, promoted, selected


def _bounded_occurrences(
    source_tokens: tuple[str, ...],
    max_occurrences: int,
) -> dict[str, tuple[int, ...]]:
    counts = Counter(source_tokens)
    positions: dict[str, list[int]] = {
        token: [] for token, count in counts.items() if count <= max_occurrences
    }
    for index, token in enumerate(source_tokens):
        if token in positions:
            positions[token].append(index)
    return {token: tuple(values) for token, values in positions.items()}


def _align_tokens(
    candidate_tokens: tuple[str, ...],
    occurrences: dict[str, tuple[int, ...]],
    max_edges: int,
    *,
    excluded_source_range: tuple[int, int] | None = None,
) -> _Alignment:
    edge_count = 0
    tails_source: list[int] = []
    tails_edge: list[int] = []
    edge_candidate: list[int] = []
    edge_source: list[int] = []
    edge_previous: list[int | None] = []

    for candidate_index, token in enumerate(candidate_tokens):
        positions = occurrences.get(token, ())
        if excluded_source_range is not None:
            lower, upper = excluded_source_range
            positions = tuple(position for position in positions if not lower <= position <= upper)
        edge_count += len(positions)
        if edge_count > max_edges:
            raise _AlignmentBudgetExceededError
        for source_position in reversed(positions):
            length = bisect_left(tails_source, source_position)
            previous = tails_edge[length - 1] if length > 0 else None
            edge_index = len(edge_source)
            edge_candidate.append(candidate_index)
            edge_source.append(source_position)
            edge_previous.append(previous)
            if length == len(tails_source):
                tails_source.append(source_position)
                tails_edge.append(edge_index)
            elif source_position < tails_source[length]:
                tails_source[length] = source_position
                tails_edge[length] = edge_index

    if not tails_edge:
        return _Alignment(pairs=(), edge_count=edge_count)
    pairs: list[tuple[int, int]] = []
    edge: int | None = tails_edge[-1]
    while edge is not None:
        pairs.append((edge_candidate[edge], edge_source[edge]))
        edge = edge_previous[edge]
    pairs.reverse()
    return _Alignment(pairs=tuple(pairs), edge_count=edge_count)


def _bag_agreement(candidate: tuple[str, ...], source: tuple[str, ...]) -> float:
    source_counts = Counter(source)
    matched = 0
    for token, count in Counter(candidate).items():
        matched += min(count, source_counts.get(token, 0))
    return matched / len(candidate) if candidate else 0.0


def _subsequence_match_count(
    selected: tuple[str, ...],
    source: tuple[str, ...],
) -> int:
    if not selected:
        return 0
    selected_index = 0
    for token in source:
        if token == selected[selected_index]:
            selected_index += 1
            if selected_index == len(selected):
                break
    return selected_index


def _normalized_tokens(value: str, max_tokens: int) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    tokens: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            tokens.append("".join(buffer))
            buffer.clear()
            if len(tokens) > max_tokens:
                raise _TokenBudgetExceededError

    for character in normalized:
        category = unicodedata.category(character)
        if _is_cjk_character(character):
            flush()
            tokens.append(character)
        elif category[0] in {"L", "N"} or category.startswith("M") or character == "_":
            buffer.append(character)
        elif character in {"'", "’"} and buffer:
            buffer.append("'")
        elif category.startswith("S") or character in "+*/=<>^":
            flush()
            tokens.append(character)
        else:
            flush()
        if len(tokens) > max_tokens:
            raise _TokenBudgetExceededError
    flush()
    return tuple(tokens)


def _is_cjk_character(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x3040 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
    )


def _visible_markdown(value: str) -> str:
    lines: list[str] = []
    fence: str | None = None
    for raw_line in value.splitlines():
        if raw_line.strip() in {"$$", r"\[", r"\]", r"\(", r"\)"}:
            continue
        fence_match = _FENCE_RE.match(raw_line)
        if fence_match is not None:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker[0]
                continue
            if marker[0] == fence:
                fence = None
                continue
        line = raw_line
        if fence is None:
            line = _ATX_HEADING_RE.sub("", line, count=1)
            line = _LIST_RE.sub("", line, count=1)
            line = line.lstrip()
            while line.startswith(">"):
                line = line[1:].lstrip()
        line = _strip_markdown_link_destinations(line)
        line = _strip_html_tags(line)
        lines.append(line)
    return unescape("\n".join(lines))


def _strip_markdown_link_destinations(value: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "]" and index + 1 < len(value) and value[index + 1] == "(":
            output.append("]")
            index += 2
            depth = 1
            escaped = False
            while index < len(value) and depth > 0:
                character = value[index]
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == "(":
                    depth += 1
                elif character == ")":
                    depth -= 1
                index += 1
            continue
        output.append(value[index])
        index += 1
    return "".join(output)


def _strip_html_tags(value: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(value):
        if value[index] != "<":
            output.append(value[index])
            index += 1
            continue
        cursor = index + 1
        quote: str | None = None
        while cursor < len(value):
            character = value[cursor]
            if quote is not None:
                if character == quote:
                    quote = None
            elif character in {'"', "'"}:
                quote = character
            elif character == ">":
                break
            cursor += 1
        if cursor >= len(value):
            output.append(value[index])
            index += 1
        else:
            output.append(" ")
            index = cursor + 1
    return "".join(output)


def _structure_categories(markdown: str) -> set[str]:
    categories: set[str] = set()
    if _HTML_HEADING_RE.search(markdown):
        categories.add("heading")
    if _HTML_LIST_RE.search(markdown):
        categories.add("list")
    if _HTML_CODE_RE.search(markdown):
        categories.add("code")
    if _HTML_TABLE_RE.search(markdown):
        categories.add("table")
    if _HTML_MATH_RE.search(markdown):
        categories.add("math")
    if "$$" in markdown or "\\(" in markdown or "\\[" in markdown:
        categories.add("math")

    lines = markdown.splitlines()
    in_fence = False
    for index, line in enumerate(lines):
        if _FENCE_RE.match(line):
            categories.add("code")
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _ATX_HEADING_RE.match(line):
            categories.add("heading")
        if _LIST_RE.match(line):
            categories.add("list")
        if index + 1 < len(lines) and line.strip() and _SETEXT_RE.match(lines[index + 1]):
            categories.add("heading")
        if (
            "|" in line
            and index + 1 < len(lines)
            and _GFM_TABLE_SEPARATOR_RE.match(lines[index + 1])
        ):
            categories.add("table")
        if "`" in line and re.search(r"`+[^`]+`+", line):
            categories.add("code")
    return categories


def _ordered_structures(categories: set[str]) -> tuple[str, ...]:
    return tuple(category for category in _STRUCTURE_ORDER if category in categories)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

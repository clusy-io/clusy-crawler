"""Exact, source-backed atomic structure overlay v0.

This module is deliberately unwired.  Its default configuration is disabled,
and importing it has no effect on the extraction cascade.  In an explicitly
enabled shadow call it may replace only a complete plain-text region with the
native IR v2 replay of one ``pre`` or one simple rectangular data table.

The native selection certificate proves local source/graph/output integrity.
It is a deterministic replay identity, not a signature, authorization token,
or proof that the source itself is trustworthy.  This layer additionally
requires unique source and candidate alignment, byte-identical prefix/suffix,
strict structural gain, bounded growth, and an identical global normalized
visible-token sequence.  Any failed global gate returns the input Markdown
byte-for-byte.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from bisect import bisect_right
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from functools import partial
from html import unescape
from typing import Literal

from clusy_native import (
    DocumentIRV2Limits,
    NativeDocumentIRV2,
    NativeIRElementV2,
    NativeIRTableCellV2,
    NativeIRTableV2,
    NativeIRTextRunV2,
    create_local_atomic_selection_certificate_v0,
    extract_document_ir_v2,
    verify_and_replay_local_atomic_selection_certificate_v0,
)

ATOMIC_STRUCTURE_OVERLAY_V0_SCHEMA = "exact-atomic-structure-overlay.v0"
ATOMIC_STRUCTURE_PROPOSAL_V0_SCHEMA = "exact-atomic-structure-overlay.proposal.v0"
ATOMIC_STRUCTURE_REPLAY_V0_SCHEMA = "exact-atomic-structure-overlay.replay.v0"

_HARD_MAX_SOURCE_BYTES = 4 * 1024 * 1024
_HARD_MAX_CANDIDATE_BYTES = 4 * 1024 * 1024
_HARD_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
_HARD_MAX_ATOMS = 1_024
_HARD_MAX_TOKENS = 500_000
_HARD_MAX_ATOM_TOKENS = 100_000
_HARD_MAX_CODE_BYTES = 1024 * 1024
_HARD_MAX_REPLACEMENT_BYTES = 2 * 1024 * 1024
_HARD_MAX_CERTIFICATE_BYTES = 2 * 1024 * 1024
_HARD_MAX_TOTAL_CERTIFICATE_BYTES = 8 * 1024 * 1024
_HARD_MAX_TABLE_ROWS = 1_024
_HARD_MAX_TABLE_COLUMNS = 256
_HARD_MAX_TABLE_CELLS = 65_536
_HARD_MAX_GROWTH_BYTES = 4 * 1024 * 1024
_HARD_MAX_GROWTH_RATIO_MILLI = 20_000

_UNTRUSTED_LANDMARK_TAGS = frozenset({"aside", "footer", "header", "nav"})
_UNTRUSTED_LANDMARK_ROLES = frozenset(
    {"banner", "complementary", "contentinfo", "navigation"}
)
_CODE_DESCENDANT_TAGS = frozenset(
    {"b", "code", "del", "em", "i", "mark", "pre", "s", "span", "strike", "strong"}
)
_TABLE_DESCENDANT_TAGS = frozenset(
    {
        "a",
        "abbr",
        "b",
        "br",
        "code",
        "col",
        "colgroup",
        "del",
        "em",
        "i",
        "s",
        "span",
        "strike",
        "strong",
        "sub",
        "sup",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
    }
)

_FENCE_OPEN_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
_ATX_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}(?:\s+|$)")
_LIST_RE = re.compile(r"^\s{0,3}(?:[-+*]|\d{1,9}[.)])\s+")
_GFM_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)

type AtomKindV0 = Literal["code", "table"]


type AtomicOverlayTimingHookV0 = Callable[[str, int], object | None]


def _validate_int(name: str, value: int, hard_maximum: int) -> None:
    if type(value) is not int or value <= 0 or value > hard_maximum:
        raise ValueError(f"{name} must be between 1 and {hard_maximum}")


@dataclass(frozen=True, slots=True)
class AtomicStructureOverlayV0Config:
    """Closed, hard-bounded shadow configuration.

    ``enabled`` is false by default.  There is intentionally no environment
    variable, service setting, or import-time switch that can enable this API.
    """

    enabled: bool = False
    enable_code: bool = True
    enable_tables: bool = True
    max_source_bytes: int = 4 * 1024 * 1024
    max_candidate_bytes: int = 2 * 1024 * 1024
    max_output_bytes: int = 4 * 1024 * 1024
    max_atoms: int = 256
    max_tokens: int = 200_000
    max_atom_tokens: int = 20_000
    max_code_bytes: int = 256 * 1024
    max_replacement_bytes: int = 512 * 1024
    max_certificate_bytes: int = 512 * 1024
    max_total_certificate_bytes: int = 2 * 1024 * 1024
    max_table_rows: int = 128
    max_table_columns: int = 64
    max_table_cells: int = 2_048
    max_growth_bytes: int = 256 * 1024
    max_growth_ratio_milli: int = 4_000

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be a bool")
        if type(self.enable_code) is not bool:
            raise ValueError("enable_code must be a bool")
        if type(self.enable_tables) is not bool:
            raise ValueError("enable_tables must be a bool")
        _validate_int("max_source_bytes", self.max_source_bytes, _HARD_MAX_SOURCE_BYTES)
        _validate_int(
            "max_candidate_bytes",
            self.max_candidate_bytes,
            _HARD_MAX_CANDIDATE_BYTES,
        )
        _validate_int("max_output_bytes", self.max_output_bytes, _HARD_MAX_OUTPUT_BYTES)
        _validate_int("max_atoms", self.max_atoms, _HARD_MAX_ATOMS)
        _validate_int("max_tokens", self.max_tokens, _HARD_MAX_TOKENS)
        _validate_int("max_atom_tokens", self.max_atom_tokens, _HARD_MAX_ATOM_TOKENS)
        _validate_int("max_code_bytes", self.max_code_bytes, _HARD_MAX_CODE_BYTES)
        _validate_int(
            "max_replacement_bytes",
            self.max_replacement_bytes,
            _HARD_MAX_REPLACEMENT_BYTES,
        )
        _validate_int(
            "max_certificate_bytes",
            self.max_certificate_bytes,
            _HARD_MAX_CERTIFICATE_BYTES,
        )
        _validate_int(
            "max_total_certificate_bytes",
            self.max_total_certificate_bytes,
            _HARD_MAX_TOTAL_CERTIFICATE_BYTES,
        )
        _validate_int("max_table_rows", self.max_table_rows, _HARD_MAX_TABLE_ROWS)
        _validate_int(
            "max_table_columns",
            self.max_table_columns,
            _HARD_MAX_TABLE_COLUMNS,
        )
        _validate_int("max_table_cells", self.max_table_cells, _HARD_MAX_TABLE_CELLS)
        _validate_int(
            "max_growth_bytes",
            self.max_growth_bytes,
            _HARD_MAX_GROWTH_BYTES,
        )
        _validate_int(
            "max_growth_ratio_milli",
            self.max_growth_ratio_milli,
            _HARD_MAX_GROWTH_RATIO_MILLI,
        )
        if self.max_atom_tokens > self.max_tokens:
            raise ValueError("max_atom_tokens must not exceed max_tokens")
        if self.max_replacement_bytes > self.max_output_bytes:
            raise ValueError("max_replacement_bytes must not exceed max_output_bytes")
        if self.max_certificate_bytes > self.max_total_certificate_bytes:
            raise ValueError(
                "max_certificate_bytes must not exceed max_total_certificate_bytes"
            )


DEFAULT_ATOMIC_STRUCTURE_OVERLAY_V0_CONFIG = AtomicStructureOverlayV0Config()


@dataclass(frozen=True, slots=True)
class AtomicStructureProposalV0:
    """One deterministic local accept/reject record."""

    schema_version: str
    proposal_id: str
    atom_kind: AtomKindV0
    selected_id: str
    source_order: int
    accepted: bool
    reason: str
    source_span_start: int | None
    source_span_end: int | None
    candidate_span_start: int | None
    candidate_span_end: int | None
    source_digest: str
    graph_digest: str
    source_span_digest: str
    input_digest: str
    replacement_digest: str
    patch_digest: str
    config_digest: str
    visible_token_digest: str
    certificate_digest: str
    certificate: bytes
    visible_token_count: int
    input_bytes: int
    replacement_bytes: int
    proposed_output_bytes: int
    growth_bytes: int
    structural_score_before: int
    structural_score_after: int
    digest_is_authentication: bool = False


@dataclass(frozen=True, slots=True)
class AtomicStructureOverlayDecisionV0:
    """All-or-nothing overlay result.

    If ``accepted`` is false, ``output_markdown`` is guaranteed to equal
    ``candidate_markdown`` exactly.
    """

    schema_version: str
    enabled: bool
    accepted: bool
    reason: str
    candidate_markdown: str
    output_markdown: str
    proposals: tuple[AtomicStructureProposalV0, ...]
    applied_proposal_ids: tuple[str, ...]
    source_digest: str
    input_digest: str
    output_digest: str
    config_digest: str
    visible_token_digest: str
    decision_digest: str
    visible_tokens_identical: bool
    input_bytes: int
    output_bytes: int
    growth_bytes: int
    digest_is_authentication: bool = False


@dataclass(frozen=True, slots=True)
class AtomicStructureOverlayReplayV0:
    """Fail-closed replay receipt."""

    schema_version: str
    verified: bool
    reason: str
    output_markdown: str
    output_digest: str
    decision_digest: str
    digest_is_authentication: bool = False


@dataclass(frozen=True, slots=True)
class _TokenSpan:
    value: str
    start: int
    end: int
    byte_start: int
    byte_end: int


@dataclass(frozen=True, slots=True)
class _TextIdentity:
    materialized: bytes | None
    byte_length: int
    valid_utf8: bool
    encoding_errors: Literal["strict", "surrogatepass"]


@dataclass(frozen=True, slots=True)
class _CandidateSpan:
    char_start: int
    char_end: int
    byte_start: int
    byte_end: int


@dataclass(frozen=True, slots=True)
class _MarkdownVisibilityPlan:
    mask: str
    protected_spans: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class _Atom:
    kind: AtomKindV0
    element: NativeIRElementV2
    table: NativeIRTableV2 | None


@dataclass(frozen=True, slots=True)
class _ProposalBase:
    atom_kind: AtomKindV0
    selected_id: str
    source_order: int
    source_span_start: int | None
    source_span_end: int | None
    source_digest: str
    source_span_digest: str
    input_digest: str
    config_digest: str
    input_bytes: int


@dataclass(frozen=True, slots=True)
class _OverlayContext:
    document: NativeDocumentIRV2
    source_bytes: bytes
    candidate_markdown: str
    candidate_bytes: bytes
    candidate_tokens: tuple[str, ...]
    candidate_visible_token_spans: tuple[_TokenSpan, ...]
    candidate_token_positions: dict[str, tuple[int, ...]]
    candidate_protected_spans: tuple[tuple[int, int], ...]
    candidate_protected_starts: tuple[int, ...]
    source_tokens: tuple[str, ...]
    source_token_positions: dict[str, tuple[int, ...]]
    elements_by_id: dict[str, NativeIRElementV2]
    children_by_parent: dict[str, tuple[NativeIRElementV2, ...]]
    text_runs_by_parent: dict[str, tuple[NativeIRTextRunV2, ...]]
    text_runs_by_id: dict[str, NativeIRTextRunV2]
    tables_by_node: dict[str, NativeIRTableV2]
    table_cells_by_table: dict[str, tuple[NativeIRTableCellV2, ...]]


def propose_atomic_structure_overlay_v0(
    html: str,
    candidate_markdown: str,
    *,
    config: AtomicStructureOverlayV0Config = DEFAULT_ATOMIC_STRUCTURE_OVERLAY_V0_CONFIG,
    timing_hook: AtomicOverlayTimingHookV0 | None = None,
) -> AtomicStructureOverlayDecisionV0:
    """Propose overlays and publish timing only after the decision is immutable."""

    timings: list[tuple[str, int]] = []
    decision = _propose_atomic_structure_overlay_v0(
        html,
        candidate_markdown,
        config=config,
        timing_hook=lambda stage, elapsed: timings.append((stage, elapsed)),
    )
    _publish_timings(timing_hook, timings)
    return decision


def _propose_atomic_structure_overlay_v0(
    html: str,
    candidate_markdown: str,
    *,
    config: AtomicStructureOverlayV0Config,
    timing_hook: AtomicOverlayTimingHookV0 | None,
) -> AtomicStructureOverlayDecisionV0:
    """Compute an exact decision without invoking caller-controlled code."""

    if type(html) is not str:
        raise TypeError("html must be a string")
    if type(candidate_markdown) is not str:
        raise TypeError("candidate_markdown must be a string")
    if type(config) is not AtomicStructureOverlayV0Config:
        raise TypeError("config must be AtomicStructureOverlayV0Config")

    source_identity = _text_identity(html, config.max_source_bytes)
    candidate_identity = _text_identity(
        candidate_markdown,
        config.max_candidate_bytes,
    )
    source_digest = _framed_text_digest(
        "clusy-atomic-overlay-source-v0",
        html,
        source_identity,
    )
    input_digest = _framed_text_digest(
        "clusy-atomic-overlay-input-v0",
        candidate_markdown,
        candidate_identity,
    )
    config_digest = _config_digest(config)

    if not config.enabled:
        return _finish_decision(
            candidate_markdown=candidate_markdown,
            output_markdown=candidate_markdown,
            proposals=(),
            applied_proposal_ids=(),
            source_digest=source_digest,
            input_digest=input_digest,
            config_digest=config_digest,
            enabled=False,
            accepted=False,
            reason="disabled",
            visible_tokens_identical=True,
            candidate_identity=candidate_identity,
            output_identity=candidate_identity,
        )
    if not source_identity.valid_utf8 or not candidate_identity.valid_utf8:
        return _fallback(
            candidate_markdown,
            source_digest,
            input_digest,
            config_digest,
            "invalid_unicode",
            candidate_identity=candidate_identity,
        )
    if source_identity.materialized is None:
        return _fallback(
            candidate_markdown,
            source_digest,
            input_digest,
            config_digest,
            "source_byte_budget",
            candidate_identity=candidate_identity,
        )
    if candidate_identity.materialized is None:
        return _fallback(
            candidate_markdown,
            source_digest,
            input_digest,
            config_digest,
            "candidate_byte_budget",
            candidate_identity=candidate_identity,
        )
    source_bytes = source_identity.materialized
    candidate_bytes = candidate_identity.materialized

    try:
        candidate_tokens = _timed(
            timing_hook,
            "candidate_tokens",
            lambda: _visible_tokens(candidate_markdown, config.max_tokens),
        )
        visibility_plan = _timed(
            timing_hook,
            "candidate_visibility_plan",
            lambda: _markdown_visibility_plan(candidate_markdown),
        )
        candidate_visible_token_spans = _timed(
            timing_hook,
            "candidate_visible_token_index",
            lambda: _raw_token_spans(
                visibility_plan.mask,
                config.max_tokens,
                offset_source=candidate_markdown,
            ),
        )
    except _BudgetExceededError:
        return _fallback(
            candidate_markdown,
            source_digest,
            input_digest,
            config_digest,
            "candidate_token_budget",
        )
    if tuple(token.value for token in candidate_visible_token_spans) != candidate_tokens:
        return _fallback(
            candidate_markdown,
            source_digest,
            input_digest,
            config_digest,
            "candidate_token_mapping_ambiguous",
        )

    ir_limits = DocumentIRV2Limits(
        max_input_bytes=config.max_source_bytes,
        max_nodes=200_000,
        max_elements=100_000,
        max_text_runs=200_000,
        max_depth=256,
        max_text_run_bytes=min(config.max_source_bytes, 256 * 1024),
        max_total_text_bytes=min(8 * 1024 * 1024, config.max_source_bytes * 2),
        max_math_bytes=min(config.max_source_bytes, 256 * 1024),
        max_table_columns=config.max_table_columns,
    )
    try:
        document = _timed(
            timing_hook,
            "extract_ir_v2",
            lambda: extract_document_ir_v2(html, limits=ir_limits),
        )
    except Exception:
        return _fallback(
            candidate_markdown,
            source_digest,
            input_digest,
            config_digest,
            "ir_extraction_failure",
        )
    incomplete_reason = _document_incomplete_reason(document)
    if incomplete_reason is not None:
        return _fallback(
            candidate_markdown,
            source_digest,
            input_digest,
            config_digest,
            incomplete_reason,
        )

    try:
        source_tokens = _source_tokens(document, config.max_tokens)
    except _BudgetExceededError:
        return _fallback(
            candidate_markdown,
            source_digest,
            input_digest,
            config_digest,
            "source_token_budget",
        )
    context = _OverlayContext(
        document=document,
        source_bytes=source_bytes,
        candidate_markdown=candidate_markdown,
        candidate_bytes=candidate_bytes,
        candidate_tokens=candidate_tokens,
        candidate_visible_token_spans=candidate_visible_token_spans,
        candidate_token_positions=_token_position_index(candidate_tokens),
        candidate_protected_spans=visibility_plan.protected_spans,
        candidate_protected_starts=tuple(
            start for start, _ in visibility_plan.protected_spans
        ),
        source_tokens=source_tokens,
        source_token_positions=_token_position_index(source_tokens),
        elements_by_id={element.id: element for element in document.elements},
        children_by_parent=_children_by_parent(document),
        text_runs_by_parent=_text_runs_by_parent(document),
        text_runs_by_id={run.id: run for run in document.text_runs},
        tables_by_node={table.node_id: table for table in document.tables},
        table_cells_by_table=_table_cells_by_table(document),
    )
    atoms = _enumerate_atoms(document, config)
    if len(atoms) > config.max_atoms:
        return _fallback(
            candidate_markdown,
            source_digest,
            input_digest,
            config_digest,
            "atom_budget",
        )

    proposals: list[AtomicStructureProposalV0] = []
    total_certificate_bytes = 0
    certificate_budget_exhausted = False
    for atom in atoms:
        if certificate_budget_exhausted:
            proposals.append(
                _make_proposal(
                    _proposal_base(
                        atom,
                        source_digest=source_digest,
                        input_digest=input_digest,
                        config_digest=config_digest,
                        input_bytes=len(candidate_bytes),
                    ),
                    reason="total_certificate_byte_budget",
                )
            )
            continue
        proposal = _timed(
            timing_hook,
            f"proposal_{atom.kind}",
            partial(
                _evaluate_atom,
                atom,
                context=context,
                source_digest=source_digest,
                input_digest=input_digest,
                config_digest=config_digest,
                config=config,
            ),
        )
        next_certificate_bytes = total_certificate_bytes + len(proposal.certificate)
        if next_certificate_bytes > config.max_total_certificate_bytes:
            certificate_budget_exhausted = True
            proposal = _reject_proposal_without_certificate(
                proposal,
                "total_certificate_byte_budget",
            )
        else:
            total_certificate_bytes = next_certificate_bytes
        proposals.append(proposal)

    proposals = _reject_overlaps(proposals)
    accepted_proposals = [proposal for proposal in proposals if proposal.accepted]
    if not accepted_proposals:
        return _finish_decision(
            candidate_markdown=candidate_markdown,
            output_markdown=candidate_markdown,
            proposals=tuple(proposals),
            applied_proposal_ids=(),
            source_digest=source_digest,
            input_digest=input_digest,
            config_digest=config_digest,
            enabled=True,
            accepted=False,
            reason="no_safe_structural_gain",
            visible_tokens_identical=True,
        )

    output_bytes = candidate_bytes
    for proposal in sorted(
        accepted_proposals,
        key=lambda item: _required_span(item)[0],
        reverse=True,
    ):
        start, end = _required_span(proposal)
        replay = verify_and_replay_local_atomic_selection_certificate_v0(
            document,
            proposal.certificate,
            max_output_bytes=config.max_replacement_bytes,
        )
        replacement_bytes = _certified_replacement(
            context,
            proposal.atom_kind,
            proposal.selected_id,
            replay.markdown,
        ).encode("utf-8")
        if _framed_digest(
            "clusy-atomic-overlay-replacement-v0", replacement_bytes
        ) != proposal.replacement_digest:
            return _global_rejection(
                candidate_markdown,
                proposals,
                source_digest,
                input_digest,
                config_digest,
                "certificate_replay_mismatch",
            )
        output_bytes = output_bytes[:start] + replacement_bytes + output_bytes[end:]

    try:
        output_markdown = output_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return _global_rejection(
            candidate_markdown,
            proposals,
            source_digest,
            input_digest,
            config_digest,
            "invalid_utf8_output",
        )
    if len(output_bytes) > config.max_output_bytes:
        return _global_rejection(
            candidate_markdown,
            proposals,
            source_digest,
            input_digest,
            config_digest,
            "output_byte_budget",
        )
    growth = len(output_bytes) - len(candidate_bytes)
    if growth > config.max_growth_bytes or not _within_growth_ratio(
        len(candidate_bytes),
        len(output_bytes),
        config.max_growth_ratio_milli,
    ):
        return _global_rejection(
            candidate_markdown,
            proposals,
            source_digest,
            input_digest,
            config_digest,
            "global_growth_budget",
        )
    try:
        output_tokens = _timed(
            timing_hook,
            "verify_visible_tokens",
            lambda: _visible_tokens(output_markdown, config.max_tokens),
        )
    except _BudgetExceededError:
        return _global_rejection(
            candidate_markdown,
            proposals,
            source_digest,
            input_digest,
            config_digest,
            "output_token_budget",
        )
    if output_tokens != candidate_tokens:
        return _global_rejection(
            candidate_markdown,
            proposals,
            source_digest,
            input_digest,
            config_digest,
            "global_visible_token_mismatch",
        )

    return _finish_decision(
        candidate_markdown=candidate_markdown,
        output_markdown=output_markdown,
        proposals=tuple(proposals),
        applied_proposal_ids=tuple(proposal.proposal_id for proposal in accepted_proposals),
        source_digest=source_digest,
        input_digest=input_digest,
        config_digest=config_digest,
        enabled=True,
        accepted=True,
        reason="accepted",
        visible_tokens_identical=True,
    )


def verify_atomic_structure_overlay_v0(
    html: str,
    candidate_markdown: str,
    decision: object,
    *,
    config: AtomicStructureOverlayV0Config,
    timing_hook: AtomicOverlayTimingHookV0 | None = None,
) -> AtomicStructureOverlayReplayV0:
    """Verify a decision and publish timing only after the receipt is immutable."""

    timings: list[tuple[str, int]] = []
    replay = _verify_atomic_structure_overlay_v0(
        html,
        candidate_markdown,
        decision,
        config=config,
        timing_hook=lambda stage, elapsed: timings.append((stage, elapsed)),
    )
    _publish_timings(timing_hook, timings)
    return replay


def _verify_atomic_structure_overlay_v0(
    html: str,
    candidate_markdown: str,
    decision: object,
    *,
    config: AtomicStructureOverlayV0Config,
    timing_hook: AtomicOverlayTimingHookV0 | None,
) -> AtomicStructureOverlayReplayV0:
    """Recompute and verify a decision; failure returns *candidate_markdown*.

    Exact type and size checks happen before equality or certificate replay so
    a hostile object cannot trigger iteration, coercion, or unbounded copying.
    """

    if type(html) is not str:
        raise TypeError("html must be a string")
    if type(candidate_markdown) is not str:
        raise TypeError("candidate_markdown must be a string")
    if type(config) is not AtomicStructureOverlayV0Config:
        raise TypeError("config must be AtomicStructureOverlayV0Config")
    candidate_identity = _text_identity(
        candidate_markdown,
        config.max_candidate_bytes,
    )
    fallback_digest = _framed_text_digest(
        "clusy-atomic-overlay-output-v0",
        candidate_markdown,
        candidate_identity,
    )
    if type(decision) is not AtomicStructureOverlayDecisionV0:
        return AtomicStructureOverlayReplayV0(
            schema_version=ATOMIC_STRUCTURE_REPLAY_V0_SCHEMA,
            verified=False,
            reason="invalid_decision_type",
            output_markdown=candidate_markdown,
            output_digest=fallback_digest,
            decision_digest="",
        )
    if not _bounded_record(decision, config):
        return AtomicStructureOverlayReplayV0(
            schema_version=ATOMIC_STRUCTURE_REPLAY_V0_SCHEMA,
            verified=False,
            reason="decision_record_budget",
            output_markdown=candidate_markdown,
            output_digest=fallback_digest,
            decision_digest="",
        )
    expected = _timed(
        timing_hook,
        "replay_decision",
        lambda: propose_atomic_structure_overlay_v0(
            html,
            candidate_markdown,
            config=config,
            timing_hook=timing_hook,
        ),
    )
    if expected != decision:
        return AtomicStructureOverlayReplayV0(
            schema_version=ATOMIC_STRUCTURE_REPLAY_V0_SCHEMA,
            verified=False,
            reason="decision_mismatch",
            output_markdown=candidate_markdown,
            output_digest=fallback_digest,
            decision_digest=expected.decision_digest,
        )
    return AtomicStructureOverlayReplayV0(
        schema_version=ATOMIC_STRUCTURE_REPLAY_V0_SCHEMA,
        verified=True,
        reason="verified",
        output_markdown=expected.output_markdown,
        output_digest=expected.output_digest,
        decision_digest=expected.decision_digest,
    )


def _evaluate_atom(
    atom: _Atom,
    *,
    context: _OverlayContext,
    source_digest: str,
    input_digest: str,
    config_digest: str,
    config: AtomicStructureOverlayV0Config,
) -> AtomicStructureProposalV0:
    document = context.document
    element = atom.element
    base = _proposal_base(
        atom,
        source_digest=source_digest,
        input_digest=input_digest,
        config_digest=config_digest,
        input_bytes=len(context.candidate_bytes),
    )
    source_span = _source_span(element, context.source_bytes)
    if source_span is None:
        return _make_proposal(base, reason="unreliable_source_span")
    source_start, source_end, source_fragment = source_span
    base = replace(
        base,
        source_span_start=source_start,
        source_span_end=source_end,
        source_span_digest=_framed_digest(
            "clusy-atomic-overlay-source-span-v0",
            source_fragment,
        ),
    )
    if b"\x00" in source_fragment or b"\r" in source_fragment:
        return _make_proposal(base, reason="noncanonical_source_control")
    descendants = _descendants(element.id, context.children_by_parent)
    atom_element_ids = {element.id, *(descendant.id for descendant in descendants)}
    atom_runs = tuple(
        run
        for parent_id in atom_element_ids
        for run in context.text_runs_by_parent.get(parent_id, ())
    )
    if any(
        descendant.implicit
        and not (
            atom.kind == "table"
            and descendant.tag == "tbody"
            and descendant.parent_id == element.id
        )
        for descendant in descendants
    ):
        return _make_proposal(base, reason="parser_repaired_atom")
    if _inside_untrusted_landmark(element, context.elements_by_id):
        return _make_proposal(base, reason="untrusted_landmark")
    if _has_atomic_ancestor(element, context.elements_by_id):
        return _make_proposal(base, reason="nested_atomic_structure")
    if any(
        descendant.tag in _UNTRUSTED_LANDMARK_TAGS
        or descendant.role in _UNTRUSTED_LANDMARK_ROLES
        for descendant in descendants
    ):
        return _make_proposal(base, reason="nested_untrusted_landmark")

    if atom.kind == "code":
        eligibility_reason = _code_eligibility_reason(
            element,
            descendants,
            atom_runs,
            config,
        )
    else:
        eligibility_reason = _table_eligibility_reason(
            atom,
            descendants,
            context,
            config,
        )
    if eligibility_reason is not None:
        return _make_proposal(base, reason=eligibility_reason)

    try:
        certificate = create_local_atomic_selection_certificate_v0(
            document,
            [element.id],
            max_output_bytes=config.max_replacement_bytes,
        )
        replay = verify_and_replay_local_atomic_selection_certificate_v0(
            document,
            certificate,
            max_output_bytes=config.max_replacement_bytes,
        )
    except Exception:
        return _make_proposal(base, reason="certificate_provenance_rejected")
    certificate_bytes = certificate.encoded
    replacement_text = _certified_replacement(
        context,
        atom.kind,
        element.id,
        replay.markdown,
    )
    replacement_bytes = replacement_text.encode("utf-8")
    if len(certificate_bytes) > config.max_certificate_bytes:
        return _make_proposal(base, reason="certificate_byte_budget")
    if not replacement_bytes or len(replacement_bytes) > config.max_replacement_bytes:
        return _make_proposal(base, reason="replacement_byte_budget")
    try:
        atom_tokens = _visible_tokens(replacement_text, config.max_atom_tokens)
    except _BudgetExceededError:
        return _make_proposal(base, reason="atom_token_budget")
    if not atom_tokens:
        return _make_proposal(base, reason="empty_visible_atom")
    if (
        len(
            _indexed_occurrence_positions(
                context.source_tokens,
                context.source_token_positions,
                atom_tokens,
                2,
            )
        )
        != 1
    ):
        return _make_proposal(base, reason="ambiguous_source_tokens")

    candidate_span = _locate_candidate_span(
        context,
        atom_tokens=atom_tokens,
        config=config,
    )
    if candidate_span is None:
        return _make_proposal(base, reason="ambiguous_or_missing_candidate_span")
    candidate_byte_start = candidate_span.byte_start
    candidate_byte_end = candidate_span.byte_end
    original_fragment = context.candidate_bytes[candidate_byte_start:candidate_byte_end]
    structural_before = _structural_score(atom.kind, original_fragment.decode("utf-8"))
    structural_after = _structural_score(atom.kind, replacement_text)
    if structural_after <= structural_before:
        return _make_proposal(base, reason="no_strict_structural_gain")
    proposed_output_bytes = (
        len(context.candidate_bytes) - len(original_fragment) + len(replacement_bytes)
    )
    growth = proposed_output_bytes - len(context.candidate_bytes)
    if growth > config.max_growth_bytes or not _within_growth_ratio(
        len(original_fragment),
        len(replacement_bytes),
        config.max_growth_ratio_milli,
    ):
        return _make_proposal(base, reason="local_growth_budget")
    if proposed_output_bytes > config.max_output_bytes:
        return _make_proposal(base, reason="output_byte_budget")
    try:
        original_tokens = _visible_tokens(
            original_fragment.decode("utf-8"),
            config.max_atom_tokens,
        )
    except (UnicodeDecodeError, _BudgetExceededError):
        return _make_proposal(base, reason="candidate_fragment_token_budget")
    if original_tokens != atom_tokens:
        return _make_proposal(base, reason="local_visible_token_mismatch")

    visible_token_digest = _token_digest(atom_tokens)
    base = replace(base, source_digest=certificate.source_digest)
    return _make_proposal(
        base,
        accepted=True,
        reason="accepted",
        candidate_span_start=candidate_byte_start,
        candidate_span_end=candidate_byte_end,
        graph_digest=certificate.graph_digest,
        replacement_digest=_framed_digest(
            "clusy-atomic-overlay-replacement-v0",
            replacement_bytes,
        ),
        patch_digest=_framed_digest(
            "clusy-atomic-overlay-patch-v0",
            input_digest.encode("ascii"),
            candidate_byte_start.to_bytes(8, "big"),
            candidate_byte_end.to_bytes(8, "big"),
            replacement_bytes,
        ),
        visible_token_digest=visible_token_digest,
        certificate_digest=certificate.certificate_digest,
        certificate=certificate_bytes,
        visible_token_count=len(atom_tokens),
        replacement_bytes=len(replacement_bytes),
        proposed_output_bytes=proposed_output_bytes,
        growth_bytes=growth,
        structural_score_before=structural_before,
        structural_score_after=structural_after,
    )


def _proposal_base(
    atom: _Atom,
    *,
    source_digest: str,
    input_digest: str,
    config_digest: str,
    input_bytes: int,
) -> _ProposalBase:
    return _ProposalBase(
        atom_kind=atom.kind,
        selected_id=atom.element.id,
        source_order=atom.element.order,
        source_span_start=atom.element.source_start,
        source_span_end=atom.element.source_end,
        source_digest=source_digest,
        source_span_digest="",
        input_digest=input_digest,
        config_digest=config_digest,
        input_bytes=input_bytes,
    )


def _make_proposal(
    base: _ProposalBase,
    *,
    reason: str,
    accepted: bool = False,
    candidate_span_start: int | None = None,
    candidate_span_end: int | None = None,
    graph_digest: str = "",
    replacement_digest: str = "",
    patch_digest: str = "",
    visible_token_digest: str = "",
    certificate_digest: str = "",
    certificate: bytes = b"",
    visible_token_count: int = 0,
    replacement_bytes: int = 0,
    proposed_output_bytes: int = 0,
    growth_bytes: int = 0,
    structural_score_before: int = 0,
    structural_score_after: int = 0,
) -> AtomicStructureProposalV0:
    canonical = {
        "accepted": accepted,
        "atom_kind": base.atom_kind,
        "candidate_span_end": candidate_span_end,
        "candidate_span_start": candidate_span_start,
        "certificate_digest": certificate_digest,
        "config_digest": base.config_digest,
        "graph_digest": graph_digest,
        "growth_bytes": growth_bytes,
        "input_bytes": base.input_bytes,
        "input_digest": base.input_digest,
        "proposed_output_bytes": proposed_output_bytes,
        "patch_digest": patch_digest,
        "reason": reason,
        "replacement_bytes": replacement_bytes,
        "replacement_digest": replacement_digest,
        "selected_id": base.selected_id,
        "source_digest": base.source_digest,
        "source_order": base.source_order,
        "source_span_digest": base.source_span_digest,
        "source_span_end": base.source_span_end,
        "source_span_start": base.source_span_start,
        "structural_score_after": structural_score_after,
        "structural_score_before": structural_score_before,
        "visible_token_count": visible_token_count,
        "visible_token_digest": visible_token_digest,
    }
    proposal_id = _framed_digest(
        "clusy-atomic-overlay-proposal-v0",
        _canonical_json(canonical),
    )
    return AtomicStructureProposalV0(
        schema_version=ATOMIC_STRUCTURE_PROPOSAL_V0_SCHEMA,
        proposal_id=proposal_id,
        atom_kind=base.atom_kind,
        selected_id=base.selected_id,
        source_order=base.source_order,
        accepted=accepted,
        reason=reason,
        source_span_start=base.source_span_start,
        source_span_end=base.source_span_end,
        candidate_span_start=candidate_span_start,
        candidate_span_end=candidate_span_end,
        source_digest=base.source_digest,
        graph_digest=graph_digest,
        source_span_digest=base.source_span_digest,
        input_digest=base.input_digest,
        replacement_digest=replacement_digest,
        patch_digest=patch_digest,
        config_digest=base.config_digest,
        visible_token_digest=visible_token_digest,
        certificate_digest=certificate_digest,
        certificate=certificate,
        visible_token_count=visible_token_count,
        input_bytes=base.input_bytes,
        replacement_bytes=replacement_bytes,
        proposed_output_bytes=proposed_output_bytes,
        growth_bytes=growth_bytes,
        structural_score_before=structural_score_before,
        structural_score_after=structural_score_after,
    )


def _reject_proposal(
    proposal: AtomicStructureProposalV0,
    reason: str,
) -> AtomicStructureProposalV0:
    base = _ProposalBase(
        atom_kind=proposal.atom_kind,
        selected_id=proposal.selected_id,
        source_order=proposal.source_order,
        source_span_start=proposal.source_span_start,
        source_span_end=proposal.source_span_end,
        source_digest=proposal.source_digest,
        source_span_digest=proposal.source_span_digest,
        input_digest=proposal.input_digest,
        config_digest=proposal.config_digest,
        input_bytes=proposal.input_bytes,
    )
    return _make_proposal(
        base,
        accepted=False,
        reason=reason,
        candidate_span_start=proposal.candidate_span_start,
        candidate_span_end=proposal.candidate_span_end,
        graph_digest=proposal.graph_digest,
        replacement_digest=proposal.replacement_digest,
        patch_digest=proposal.patch_digest,
        visible_token_digest=proposal.visible_token_digest,
        certificate_digest=proposal.certificate_digest,
        certificate=proposal.certificate,
        visible_token_count=proposal.visible_token_count,
        replacement_bytes=proposal.replacement_bytes,
        proposed_output_bytes=proposal.proposed_output_bytes,
        growth_bytes=proposal.growth_bytes,
        structural_score_before=proposal.structural_score_before,
        structural_score_after=proposal.structural_score_after,
    )


def _reject_proposal_without_certificate(
    proposal: AtomicStructureProposalV0,
    reason: str,
) -> AtomicStructureProposalV0:
    base = _ProposalBase(
        atom_kind=proposal.atom_kind,
        selected_id=proposal.selected_id,
        source_order=proposal.source_order,
        source_span_start=proposal.source_span_start,
        source_span_end=proposal.source_span_end,
        source_digest=proposal.source_digest,
        source_span_digest=proposal.source_span_digest,
        input_digest=proposal.input_digest,
        config_digest=proposal.config_digest,
        input_bytes=proposal.input_bytes,
    )
    return _make_proposal(base, reason=reason)


def _reject_overlaps(
    proposals: list[AtomicStructureProposalV0],
) -> list[AtomicStructureProposalV0]:
    accepted = sorted(
        (proposal for proposal in proposals if proposal.accepted),
        key=lambda item: _required_span(item),
    )
    rejected_ids: set[str] = set()
    previous: AtomicStructureProposalV0 | None = None
    for proposal in accepted:
        if previous is not None and _required_span(proposal)[0] < _required_span(previous)[1]:
            rejected_ids.add(previous.proposal_id)
            rejected_ids.add(proposal.proposal_id)
        previous = proposal
    return [
        _reject_proposal(proposal, "candidate_span_overlap")
        if proposal.proposal_id in rejected_ids
        else proposal
        for proposal in proposals
    ]


def _required_span(proposal: AtomicStructureProposalV0) -> tuple[int, int]:
    start = proposal.candidate_span_start
    end = proposal.candidate_span_end
    if start is None or end is None:
        raise ValueError("accepted proposal lacks a candidate span")
    return start, end


def _document_incomplete_reason(document: NativeDocumentIRV2) -> str | None:
    if document.schema_version != "ordered-dom-ir.v2":
        return "unsupported_ir_schema"
    if not document.source_complete or document.input_truncated:
        return "truncated_source"
    if (
        document.truncated
        or document.nodes_truncated
        or document.depth_truncated
        or document.elements_truncated
        or document.text_runs_truncated
        or document.text_truncated_runs != 0
        or document.table_grid_truncated
        or document.math_truncated_nodes != 0
    ):
        return "truncated_ir"
    return None


def _enumerate_atoms(
    document: NativeDocumentIRV2,
    config: AtomicStructureOverlayV0Config,
) -> tuple[_Atom, ...]:
    tables_by_node = {table.node_id: table for table in document.tables}
    atoms: list[_Atom] = []
    for element in sorted(document.elements, key=lambda item: item.order):
        if element.tag == "pre" and config.enable_code:
            atoms.append(_Atom("code", element, None))
        elif element.tag == "table" and config.enable_tables:
            atoms.append(_Atom("table", element, tables_by_node.get(element.id)))
    return tuple(atoms)


def _children_by_parent(
    document: NativeDocumentIRV2,
) -> dict[str, tuple[NativeIRElementV2, ...]]:
    mutable: dict[str, list[NativeIRElementV2]] = {}
    for element in document.elements:
        if element.parent_id is not None:
            mutable.setdefault(element.parent_id, []).append(element)
    return {
        parent_id: tuple(sorted(children, key=lambda item: item.order))
        for parent_id, children in mutable.items()
    }


def _text_runs_by_parent(
    document: NativeDocumentIRV2,
) -> dict[str, tuple[NativeIRTextRunV2, ...]]:
    mutable: dict[str, list[NativeIRTextRunV2]] = {}
    for run in document.text_runs:
        mutable.setdefault(run.parent_id, []).append(run)
    return {
        parent_id: tuple(sorted(runs, key=lambda item: item.order))
        for parent_id, runs in mutable.items()
    }


def _table_cells_by_table(
    document: NativeDocumentIRV2,
) -> dict[str, tuple[NativeIRTableCellV2, ...]]:
    mutable: dict[str, list[NativeIRTableCellV2]] = {}
    for cell in document.table_cells:
        mutable.setdefault(cell.table_id, []).append(cell)
    return {
        table_id: tuple(
            sorted(
                cells,
                key=lambda item: (item.row_index, item.column_index, item.order),
            )
        )
        for table_id, cells in mutable.items()
    }


def _descendants(
    element_id: str,
    children_by_parent: dict[str, tuple[NativeIRElementV2, ...]],
) -> tuple[NativeIRElementV2, ...]:
    output: list[NativeIRElementV2] = []
    pending = list(reversed(children_by_parent.get(element_id, ())))
    while pending:
        element = pending.pop()
        output.append(element)
        pending.extend(reversed(children_by_parent.get(element.id, ())))
    return tuple(output)


def _inside_untrusted_landmark(
    element: NativeIRElementV2,
    elements_by_id: dict[str, NativeIRElementV2],
) -> bool:
    parent_id = element.parent_id
    seen: set[str] = set()
    while parent_id is not None:
        if parent_id in seen:
            return True
        seen.add(parent_id)
        parent = elements_by_id.get(parent_id)
        if parent is None:
            return True
        if (
            parent.tag in _UNTRUSTED_LANDMARK_TAGS
            or parent.role in _UNTRUSTED_LANDMARK_ROLES
        ):
            return True
        parent_id = parent.parent_id
    return False


def _has_atomic_ancestor(
    element: NativeIRElementV2,
    elements_by_id: dict[str, NativeIRElementV2],
) -> bool:
    parent_id = element.parent_id
    seen: set[str] = set()
    while parent_id is not None:
        if parent_id in seen:
            return True
        seen.add(parent_id)
        parent = elements_by_id.get(parent_id)
        if parent is None:
            return True
        if parent.tag in {"pre", "table"}:
            return True
        parent_id = parent.parent_id
    return False


def _code_eligibility_reason(
    element: NativeIRElementV2,
    descendants: tuple[NativeIRElementV2, ...],
    runs: tuple[NativeIRTextRunV2, ...],
    config: AtomicStructureOverlayV0Config,
) -> str | None:
    if any(descendant.tag not in _CODE_DESCENDANT_TAGS for descendant in descendants):
        return "complex_code_descendant"
    del element
    if not runs or any(run.truncated or not run.preserve_whitespace for run in runs):
        return "inexact_code_text"
    code_bytes = "".join(
        run.text for run in sorted(runs, key=lambda item: item.order)
    ).encode("utf-8")
    if len(code_bytes) > config.max_code_bytes:
        return "code_byte_budget"
    return None


def _table_eligibility_reason(
    atom: _Atom,
    descendants: tuple[NativeIRElementV2, ...],
    context: _OverlayContext,
    config: AtomicStructureOverlayV0Config,
) -> str | None:
    table = atom.table
    if table is None or not table.grid_complete:
        return "incomplete_table_grid"
    if (
        table.row_count < 2
        or table.column_count < 2
        or table.row_count > config.max_table_rows
        or table.column_count > config.max_table_columns
    ):
        return "non_data_table_shape"
    cells = context.table_cells_by_table.get(table.id, ())
    expected_cells = table.row_count * table.column_count
    if len(cells) != expected_cells or len(cells) > config.max_table_cells:
        return "non_rectangular_table"
    coordinates = {(cell.row_index, cell.column_index) for cell in cells}
    if len(coordinates) != expected_cells or any(
        cell.row_span != 1 or cell.column_span != 1 or not cell.grid_complete
        for cell in cells
    ):
        return "non_rectangular_table"
    first_row = [cell for cell in cells if cell.row_index == 0]
    body_rows = [cell for cell in cells if cell.row_index > 0]
    if not first_row or not all(cell.header for cell in first_row):
        return "layout_table_without_header"
    if any(cell.header for cell in body_rows):
        return "complex_header_table"
    if atom.element.role != "table":
        return "layout_table_role"
    if any(
        descendant.tag == "table" or descendant.tag not in _TABLE_DESCENDANT_TAGS
        for descendant in descendants
    ):
        return "complex_table_descendant"
    for cell in cells:
        cell_runs = [context.text_runs_by_id.get(run_id) for run_id in cell.text_run_ids]
        if (
            not cell_runs
            or any(run is None or run.truncated for run in cell_runs)
            or not _tokens(
                " ".join(run.text for run in cell_runs if run is not None),
                config.max_atom_tokens,
            )
        ):
            return "empty_or_inexact_table_cell"
    return None


def _certified_replacement(
    context: _OverlayContext,
    kind: AtomKindV0,
    selected_id: str,
    native_replay: str,
) -> str:
    if kind == "code":
        return native_replay
    table = context.tables_by_node.get(selected_id)
    if table is None:
        return ""
    cells = context.table_cells_by_table.get(table.id, ())
    rows: list[list[str]] = [
        ["" for _ in range(table.column_count)] for _ in range(table.row_count)
    ]
    for cell in cells:
        text = "".join(
            context.text_runs_by_id[run_id].text
            for run_id in cell.text_run_ids
            if run_id in context.text_runs_by_id
        )
        normalized = " ".join(text.replace("\x00", "").split())
        rows[cell.row_index][cell.column_index] = (
            normalized.replace("\\", "\\\\").replace("|", "\\|")
        )
    if not rows:
        return ""
    rendered = ["| " + " | ".join(row) + " |" for row in rows]
    rendered.insert(
        1,
        "| " + " | ".join("---" for _ in range(table.column_count)) + " |",
    )
    return "\n".join(rendered)


def _source_span(
    element: NativeIRElementV2,
    source_bytes: bytes,
) -> tuple[int, int, bytes] | None:
    start = element.source_start
    end = element.source_end
    if (
        not element.source_span_reliable
        or start is None
        or end is None
        or start < 0
        or end <= start
        or end > len(source_bytes)
    ):
        return None
    fragment = source_bytes[start:end]
    try:
        fragment.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return start, end, fragment


def _source_tokens(
    document: NativeDocumentIRV2,
    maximum: int,
) -> tuple[str, ...]:
    output: list[str] = []
    for run in sorted(document.text_runs, key=lambda item: item.order):
        remaining = maximum - len(output)
        if remaining <= 0:
            raise _BudgetExceededError
        output.extend(_tokens(run.text, remaining))
        if len(output) > maximum:
            raise _BudgetExceededError
    return tuple(output)


def _locate_candidate_span(
    context: _OverlayContext,
    *,
    atom_tokens: tuple[str, ...],
    config: AtomicStructureOverlayV0Config,
) -> _CandidateSpan | None:
    positions = _indexed_occurrence_positions(
        context.candidate_tokens,
        context.candidate_token_positions,
        atom_tokens,
        2,
    )
    if len(positions) != 1:
        return None
    token_index = positions[0]
    first_token = context.candidate_visible_token_spans[token_index]
    last_token = context.candidate_visible_token_spans[
        token_index + len(atom_tokens) - 1
    ]
    span = _expand_full_lines(
        context.candidate_markdown,
        first_token.start,
        last_token.end,
    )
    if span is None:
        return None
    char_start, char_end = span
    if _overlaps_protected_span(
        char_start,
        char_end,
        context.candidate_protected_spans,
        context.candidate_protected_starts,
    ):
        return None
    fragment = context.candidate_markdown[char_start:char_end]
    if _visible_tokens(fragment, config.max_atom_tokens) != atom_tokens:
        return None
    byte_start = first_token.byte_start - len(
        context.candidate_markdown[char_start:first_token.start].encode("utf-8")
    )
    byte_end = last_token.byte_end + len(
        context.candidate_markdown[last_token.end:char_end].encode("utf-8")
    )
    return _CandidateSpan(
        char_start=char_start,
        char_end=char_end,
        byte_start=byte_start,
        byte_end=byte_end,
    )


def _expand_full_lines(value: str, start: int, end: int) -> tuple[int, int] | None:
    line_start = value.rfind("\n", 0, start) + 1
    next_newline = value.find("\n", end)
    line_end = len(value) if next_newline < 0 else next_newline
    try:
        leading_tokens = _visible_tokens(value[line_start:start], 16)
        trailing_tokens = _visible_tokens(value[end:line_end], 16)
    except _BudgetExceededError:
        return None
    if leading_tokens or trailing_tokens:
        return None
    return line_start, line_end


def _structural_score(kind: AtomKindV0, value: str) -> int:
    lowered = value.casefold()
    lines = value.splitlines()
    if kind == "code":
        if re.search(r"<\s*(?:pre|code)\b", lowered):
            return 2
        if any(_FENCE_OPEN_RE.match(line) for line in lines):
            return 2
        nonempty = [line for line in lines if line.strip()]
        if nonempty and all(line.startswith(("    ", "\t")) for line in nonempty):
            return 1
        return 0
    if re.search(r"<\s*table\b", lowered):
        return 2
    if any(_GFM_TABLE_SEPARATOR_RE.match(line) for line in lines):
        return 2
    if sum("|" in line for line in lines) >= 2:
        return 1
    return 0


def _raw_token_spans(
    value: str,
    maximum: int,
    *,
    offset_source: str | None = None,
) -> tuple[_TokenSpan, ...]:
    if offset_source is None:
        offset_source = value
    if len(offset_source) != len(value):
        raise ValueError("offset_source must have the same character length as value")
    tokens: list[_TokenSpan] = []
    buffer: list[str] = []
    buffer_start = 0
    buffer_end = 0
    buffer_byte_start = 0
    buffer_byte_end = 0

    def flush() -> None:
        nonlocal buffer_byte_end, buffer_byte_start, buffer_end, buffer_start
        if buffer:
            tokens.append(
                _TokenSpan(
                    "".join(buffer),
                    buffer_start,
                    buffer_end,
                    buffer_byte_start,
                    buffer_byte_end,
                )
            )
            buffer.clear()
            if len(tokens) > maximum:
                raise _BudgetExceededError

    source_byte_start = 0
    for source_index, source_character in enumerate(value):
        offset_character = offset_source[source_index]
        source_byte_end = source_byte_start + len(offset_character.encode("utf-8"))
        normalized_piece = unicodedata.normalize("NFKC", source_character).casefold()
        for character in normalized_piece:
            category = unicodedata.category(character)
            if _is_cjk(character):
                flush()
                tokens.append(
                    _TokenSpan(
                        character,
                        source_index,
                        source_index + 1,
                        source_byte_start,
                        source_byte_end,
                    )
                )
            elif category[0] in {"L", "N"} or category.startswith("M") or character == "_":
                if not buffer:
                    buffer_start = source_index
                    buffer_byte_start = source_byte_start
                buffer.append(character)
                buffer_end = source_index + 1
                buffer_byte_end = source_byte_end
            elif character in {"'", "’"} and buffer:
                buffer.append("'")
                buffer_end = source_index + 1
                buffer_byte_end = source_byte_end
            elif category.startswith("S") or character in "+*/=<>^":
                flush()
                tokens.append(
                    _TokenSpan(
                        character,
                        source_index,
                        source_index + 1,
                        source_byte_start,
                        source_byte_end,
                    )
                )
            else:
                flush()
            if len(tokens) > maximum:
                raise _BudgetExceededError
        source_byte_start = source_byte_end
    flush()
    return tuple(tokens)


def _visible_tokens(value: str, maximum: int) -> tuple[str, ...]:
    return _tokens(_visible_markdown(value), maximum)


def _tokens(value: str, maximum: int) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    output: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            output.append("".join(buffer))
            buffer.clear()
            if len(output) > maximum:
                raise _BudgetExceededError

    for character in normalized:
        category = unicodedata.category(character)
        if _is_cjk(character):
            flush()
            output.append(character)
        elif category[0] in {"L", "N"} or category.startswith("M") or character == "_":
            buffer.append(character)
        elif character in {"'", "’"} and buffer:
            buffer.append("'")
        elif category.startswith("S") or character in "+*/=<>^":
            flush()
            output.append(character)
        else:
            flush()
        if len(output) > maximum:
            raise _BudgetExceededError
    flush()
    return tuple(output)


_ENTITY_RE = re.compile(
    r"&(?:#[xX][0-9A-Fa-f]+|#[0-9]+|[A-Za-z][A-Za-z0-9]+);"
)
_LINE_BREAK_CHARACTERS = frozenset(
    {"\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"}
)


def _markdown_visibility_plan(value: str) -> _MarkdownVisibilityPlan:
    """Build a same-length visible-text mask plus unsafe Markdown intervals.

    The mask lets normalized visible tokens retain exact offsets into the
    original candidate.  Intervals cover existing block/inline structure whose
    removal would otherwise look token-preserving (fences, GFM tables, headings,
    lists, quotes, links, HTML tags, inline code, and entities).
    """

    mask = list(value)
    protected: list[tuple[int, int]] = []

    def mask_range(start: int, end: int) -> None:
        for index in range(start, end):
            if mask[index] not in _LINE_BREAK_CHARACTERS:
                mask[index] = " "

    for match in _ENTITY_RE.finditer(value):
        decoded = unescape(match.group(0))
        if decoded == match.group(0) or len(decoded) > match.end() - match.start():
            continue
        mask_range(match.start(), match.end())
        for offset, character in enumerate(decoded):
            mask[match.start() + offset] = character
        protected.append((match.start(), match.end()))

    lines: list[tuple[int, int, int, str]] = []
    cursor = 0
    for full_line in value.splitlines(keepends=True):
        body_length = len(full_line)
        while (
            body_length > 0
            and full_line[body_length - 1] in _LINE_BREAK_CHARACTERS
        ):
            body_length -= 1
        body_end = cursor + body_length
        full_end = cursor + len(full_line)
        lines.append((cursor, body_end, full_end, value[cursor:body_end]))
        cursor = full_end
    if cursor < len(value):
        lines.append((cursor, len(value), len(value), value[cursor:]))

    raw_lines = [line for _, _, _, line in lines]
    table_rows, table_separators = _gfm_table_line_indexes(raw_lines)
    protected_table_lines = table_rows | table_separators
    plain_pipe_rows = _plain_pipe_table_line_indexes(
        raw_lines,
        protected_table_lines,
    )
    pipe_rows = table_rows | plain_pipe_rows

    fence_character: str | None = None
    fence_length = 0
    fence_start = 0
    for line_index, (line_start, body_end, full_end, raw_line) in enumerate(lines):
        if fence_character is not None:
            stripped = raw_line.lstrip(" ")
            indent = len(raw_line) - len(stripped)
            marker_length = len(stripped) - len(stripped.lstrip(fence_character))
            trailing = stripped[marker_length:]
            if indent <= 3 and marker_length >= fence_length and not trailing.strip():
                mask_range(line_start, body_end)
                protected.append((fence_start, full_end))
                fence_character = None
                fence_length = 0
            continue

        fence_match = _FENCE_OPEN_RE.match(raw_line)
        if fence_match is not None:
            marker = fence_match.group(2)
            info = fence_match.group(3)
            if marker[0] == "~" or "`" not in info:
                mask_range(line_start, body_end)
                fence_character = marker[0]
                fence_length = len(marker)
                fence_start = line_start
                continue

        if line_index in protected_table_lines:
            protected.append((line_start, full_end))
        if line_index in table_separators:
            mask_range(line_start, body_end)
        elif line_index in pipe_rows:
            preceding_backslashes = 0
            for offset, character in enumerate(raw_line):
                if character == "\\":
                    preceding_backslashes += 1
                    continue
                if character == "|" and preceding_backslashes % 2 == 0:
                    mask_range(line_start + offset, line_start + offset + 1)
                preceding_backslashes = 0

        prefix_end = 0
        heading_match = _ATX_HEADING_RE.match(raw_line)
        has_prefix_structure = heading_match is not None
        if heading_match is not None:
            prefix_end = heading_match.end()
            mask_range(line_start, line_start + prefix_end)
        list_match = _LIST_RE.match(raw_line[prefix_end:])
        if list_match is not None:
            has_prefix_structure = True
            list_end = prefix_end + list_match.end()
            mask_range(line_start + prefix_end, line_start + list_end)
            prefix_end = list_end

        quote_cursor = prefix_end
        while quote_cursor < len(raw_line) and raw_line[quote_cursor].isspace():
            quote_cursor += 1
        while quote_cursor < len(raw_line) and raw_line[quote_cursor] == ">":
            has_prefix_structure = True
            mask_range(line_start + quote_cursor, line_start + quote_cursor + 1)
            quote_cursor += 1
            while quote_cursor < len(raw_line) and raw_line[quote_cursor].isspace():
                quote_cursor += 1
        if has_prefix_structure:
            protected.append((line_start, full_end))

        link_cursor = 0
        while link_cursor < len(raw_line):
            marker_index = raw_line.find("](", link_cursor)
            if marker_index < 0:
                break
            destination_start = marker_index + 1
            destination_cursor = marker_index + 2
            depth = 1
            escaped = False
            while destination_cursor < len(raw_line) and depth:
                character = raw_line[destination_cursor]
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == "(":
                    depth += 1
                elif character == ")":
                    depth -= 1
                destination_cursor += 1
            mask_range(
                line_start + destination_start,
                line_start + destination_cursor,
            )
            protected.append(
                (
                    line_start + marker_index,
                    line_start + destination_cursor,
                )
            )
            link_cursor = destination_cursor

        html_cursor = 0
        while html_cursor < len(raw_line):
            tag_start = raw_line.find("<", html_cursor)
            if tag_start < 0:
                break
            tag_cursor = tag_start + 1
            quote: str | None = None
            while tag_cursor < len(raw_line):
                character = raw_line[tag_cursor]
                if quote is not None:
                    if character == quote:
                        quote = None
                elif character in {'"', "'"}:
                    quote = character
                elif character == ">":
                    break
                tag_cursor += 1
            if tag_cursor >= len(raw_line):
                break
            tag_end = tag_cursor + 1
            mask_range(line_start + tag_start, line_start + tag_end)
            protected.append(
                (line_start + tag_start, line_start + tag_end)
            )
            html_cursor = tag_end

        code_cursor = 0
        while code_cursor < len(raw_line):
            opening = raw_line.find("`", code_cursor)
            if opening < 0:
                break
            run_end = opening
            while run_end < len(raw_line) and raw_line[run_end] == "`":
                run_end += 1
            marker = raw_line[opening:run_end]
            closing = raw_line.find(marker, run_end)
            if closing < 0:
                code_cursor = run_end
                continue
            protected.append(
                (line_start + opening, line_start + closing + len(marker))
            )
            code_cursor = closing + len(marker)

    if fence_character is not None:
        protected.append((fence_start, len(value)))

    return _MarkdownVisibilityPlan(
        mask="".join(mask),
        protected_spans=_merge_spans(protected),
    )


def _merge_spans(spans: list[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _overlaps_protected_span(
    start: int,
    end: int,
    spans: tuple[tuple[int, int], ...],
    starts: tuple[int, ...],
) -> bool:
    if start >= end or not spans:
        return False
    index = bisect_right(starts, start) - 1
    if index >= 0 and spans[index][1] > start:
        return True
    next_index = index + 1
    return next_index < len(spans) and spans[next_index][0] < end


def _visible_markdown(value: str) -> str:
    output: list[str] = []
    fence_character: str | None = None
    fence_length = 0
    raw_lines = value.splitlines()
    table_rows, table_separators = _gfm_table_line_indexes(raw_lines)
    plain_pipe_rows = _plain_pipe_table_line_indexes(
        raw_lines,
        table_rows | table_separators,
    )
    for line_index, raw_line in enumerate(raw_lines):
        if fence_character is not None:
            stripped = raw_line.lstrip(" ")
            indent = len(raw_line) - len(stripped)
            marker_length = len(stripped) - len(stripped.lstrip(fence_character))
            trailing = stripped[marker_length:]
            if indent <= 3 and marker_length >= fence_length and not trailing.strip():
                fence_character = None
                fence_length = 0
                continue
            output.append(raw_line)
            continue
        match = _FENCE_OPEN_RE.match(raw_line)
        if match is not None:
            marker = match.group(2)
            info = match.group(3)
            if marker[0] == "~" or "`" not in info:
                fence_character = marker[0]
                fence_length = len(marker)
                continue
        if line_index in table_separators:
            continue
        if line_index in table_rows or line_index in plain_pipe_rows:
            raw_line = _strip_unescaped_table_pipes(raw_line)
        line = _ATX_HEADING_RE.sub("", raw_line, count=1)
        line = _LIST_RE.sub("", line, count=1)
        line = line.lstrip()
        while line.startswith(">"):
            line = line[1:].lstrip()
        line = _strip_markdown_link_destinations(line)
        output.append(_strip_html_tags(line))
    return unescape("\n".join(output))


def _gfm_table_line_indexes(lines: list[str]) -> tuple[set[int], set[int]]:
    rows: set[int] = set()
    separators: set[int] = set()
    for index, line in enumerate(lines):
        if index == 0 or not _GFM_TABLE_SEPARATOR_RE.match(line):
            continue
        if "|" not in lines[index - 1]:
            continue
        separators.add(index)
        rows.add(index - 1)
        cursor = index + 1
        while cursor < len(lines) and lines[cursor].strip() and "|" in lines[cursor]:
            rows.add(cursor)
            cursor += 1
    return rows, separators


def _plain_pipe_table_line_indexes(
    lines: list[str],
    excluded: set[int],
) -> set[int]:
    rows: set[int] = set()
    cursor = 0
    while cursor < len(lines):
        if cursor in excluded:
            cursor += 1
            continue
        column_count = _unescaped_pipe_cell_count(lines[cursor])
        if column_count is None:
            cursor += 1
            continue
        block_start = cursor
        cursor += 1
        while (
            cursor < len(lines)
            and cursor not in excluded
            and _unescaped_pipe_cell_count(lines[cursor]) == column_count
        ):
            cursor += 1
        if cursor - block_start >= 2:
            rows.update(range(block_start, cursor))
    return rows


def _unescaped_pipe_cell_count(value: str) -> int | None:
    cells = [0]
    preceding_backslashes = 0
    for character in value:
        if character == "\\":
            preceding_backslashes += 1
            cells[-1] += 1
            continue
        if character == "|" and preceding_backslashes % 2 == 0:
            cells.append(0)
        else:
            cells[-1] += 1
        preceding_backslashes = 0
    if len(cells) < 2:
        return None
    if cells[0] == 0:
        cells.pop(0)
    if cells and cells[-1] == 0:
        cells.pop()
    return len(cells) if len(cells) >= 2 else None


def _strip_unescaped_table_pipes(value: str) -> str:
    output: list[str] = []
    preceding_backslashes = 0
    for character in value:
        if character == "\\":
            preceding_backslashes += 1
            output.append(character)
            continue
        if character == "|" and preceding_backslashes % 2 == 0:
            output.append(" ")
        else:
            output.append(character)
        preceding_backslashes = 0
    return "".join(output)


def _strip_markdown_link_destinations(value: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "]" and index + 1 < len(value) and value[index + 1] == "(":
            output.append("]")
            index += 2
            depth = 1
            escaped = False
            while index < len(value) and depth:
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


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x3040 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
    )


def _substring_positions(value: str, needle: str, limit: int) -> tuple[int, ...]:
    if not needle:
        return ()
    output: list[int] = []
    start = 0
    while len(output) < limit:
        found = value.find(needle, start)
        if found < 0:
            break
        output.append(found)
        start = found + 1
    return tuple(output)


def _token_position_index(
    tokens: tuple[str, ...],
) -> dict[str, tuple[int, ...]]:
    mutable: dict[str, list[int]] = {}
    for index, token in enumerate(tokens):
        mutable.setdefault(token, []).append(index)
    return {token: tuple(positions) for token, positions in mutable.items()}


def _indexed_occurrence_positions(
    haystack: tuple[str, ...],
    positions_by_token: dict[str, tuple[int, ...]],
    needle: tuple[str, ...],
    limit: int,
) -> tuple[int, ...]:
    if not needle or len(needle) > len(haystack) or limit <= 0:
        return ()
    indexed = [
        (len(positions_by_token.get(token, ())), offset, token)
        for offset, token in enumerate(needle)
    ]
    _, anchor_offset, anchor_token = min(indexed)
    anchor_positions = positions_by_token.get(anchor_token, ())
    output: list[int] = []
    for anchor_position in anchor_positions:
        start = anchor_position - anchor_offset
        end = start + len(needle)
        if start < 0 or end > len(haystack):
            continue
        if haystack[start:end] == needle:
            output.append(start)
            if len(output) == limit:
                break
    return tuple(output)


def _within_growth_ratio(before: int, after: int, maximum_milli: int) -> bool:
    return after * 1_000 <= max(1, before) * maximum_milli


def _timed[T](
    hook: AtomicOverlayTimingHookV0 | None,
    stage: str,
    operation: Callable[[], T],
) -> T:
    started = time.perf_counter_ns()
    try:
        return operation()
    finally:
        if hook is not None:
            with suppress(Exception):
                hook(stage, max(0, time.perf_counter_ns() - started))


def _publish_timings(
    hook: AtomicOverlayTimingHookV0 | None,
    timings: list[tuple[str, int]],
) -> None:
    if hook is None:
        return
    for stage, elapsed in timings:
        with suppress(Exception):
            hook(stage, elapsed)


def _text_identity(value: str, materialize_limit: int) -> _TextIdentity:
    def scan(
        errors: Literal["strict", "surrogatepass"],
    ) -> tuple[bytes | None, int]:
        materialized: bytearray | None = bytearray()
        byte_length = 0
        for offset in range(0, len(value), 16_384):
            encoded = value[offset : offset + 16_384].encode(
                "utf-8",
                errors=errors,
            )
            byte_length += len(encoded)
            if materialized is not None:
                if byte_length <= materialize_limit:
                    materialized.extend(encoded)
                else:
                    materialized = None
        return (
            bytes(materialized) if materialized is not None else None,
            byte_length,
        )

    try:
        materialized, byte_length = scan("strict")
    except UnicodeEncodeError:
        materialized, byte_length = scan("surrogatepass")
        return _TextIdentity(
            materialized=materialized,
            byte_length=byte_length,
            valid_utf8=False,
            encoding_errors="surrogatepass",
        )
    return _TextIdentity(
        materialized=materialized,
        byte_length=byte_length,
        valid_utf8=True,
        encoding_errors="strict",
    )


def _framed_text_digest(
    domain: str,
    value: str,
    identity: _TextIdentity,
) -> str:
    if identity.materialized is not None:
        return _framed_digest(domain, identity.materialized)
    digest = hashlib.sha256()
    domain_bytes = domain.encode("ascii")
    digest.update(len(domain_bytes).to_bytes(8, "big"))
    digest.update(domain_bytes)
    digest.update(identity.byte_length.to_bytes(8, "big"))
    for offset in range(0, len(value), 16_384):
        digest.update(
            value[offset : offset + 16_384].encode(
                "utf-8",
                errors=identity.encoding_errors,
            )
        )
    return digest.hexdigest()


def _strict_utf8_within_budget(value: str, maximum_bytes: int) -> bool:
    if len(value) > maximum_bytes:
        return False
    byte_length = 0
    try:
        for offset in range(0, len(value), 16_384):
            byte_length += len(value[offset : offset + 16_384].encode("utf-8"))
            if byte_length > maximum_bytes:
                return False
    except UnicodeEncodeError:
        return False
    return True


def _framed_digest(domain: str, *parts: bytes) -> str:
    digest = hashlib.sha256()
    domain_bytes = domain.encode("ascii")
    digest.update(len(domain_bytes).to_bytes(8, "big"))
    digest.update(domain_bytes)
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _config_digest(config: AtomicStructureOverlayV0Config) -> str:
    return _framed_digest(
        "clusy-atomic-overlay-config-v0",
        _canonical_json(asdict(config)),
    )


def _token_digest(tokens: tuple[str, ...]) -> str:
    return _framed_digest(
        "clusy-atomic-overlay-visible-tokens-v0",
        *(token.encode("utf-8") for token in tokens),
    )


def _finish_decision(
    *,
    candidate_markdown: str,
    output_markdown: str,
    proposals: tuple[AtomicStructureProposalV0, ...],
    applied_proposal_ids: tuple[str, ...],
    source_digest: str,
    input_digest: str,
    config_digest: str,
    enabled: bool,
    accepted: bool,
    reason: str,
    visible_tokens_identical: bool,
    candidate_identity: _TextIdentity | None = None,
    output_identity: _TextIdentity | None = None,
) -> AtomicStructureOverlayDecisionV0:
    if candidate_identity is None:
        candidate_identity = _text_identity(
            candidate_markdown,
            _HARD_MAX_CANDIDATE_BYTES,
        )
    if output_identity is None:
        output_identity = (
            candidate_identity
            if output_markdown == candidate_markdown
            else _text_identity(output_markdown, _HARD_MAX_OUTPUT_BYTES)
        )
    output_digest = _framed_text_digest(
        "clusy-atomic-overlay-output-v0",
        output_markdown,
        output_identity,
    )
    try:
        visible_token_digest = _token_digest(_visible_tokens(output_markdown, _HARD_MAX_TOKENS))
    except (_BudgetExceededError, UnicodeEncodeError):
        visible_token_digest = ""
    canonical = {
        "accepted": accepted,
        "applied_proposal_ids": applied_proposal_ids,
        "config_digest": config_digest,
        "enabled": enabled,
        "growth_bytes": output_identity.byte_length - candidate_identity.byte_length,
        "input_bytes": candidate_identity.byte_length,
        "input_digest": input_digest,
        "output_bytes": output_identity.byte_length,
        "output_digest": output_digest,
        "proposal_ids": tuple(proposal.proposal_id for proposal in proposals),
        "reason": reason,
        "source_digest": source_digest,
        "visible_token_digest": visible_token_digest,
        "visible_tokens_identical": visible_tokens_identical,
    }
    decision_digest = _framed_digest(
        "clusy-atomic-overlay-decision-v0",
        _canonical_json(canonical),
    )
    return AtomicStructureOverlayDecisionV0(
        schema_version=ATOMIC_STRUCTURE_OVERLAY_V0_SCHEMA,
        enabled=enabled,
        accepted=accepted,
        reason=reason,
        candidate_markdown=candidate_markdown,
        output_markdown=output_markdown,
        proposals=proposals,
        applied_proposal_ids=applied_proposal_ids,
        source_digest=source_digest,
        input_digest=input_digest,
        output_digest=output_digest,
        config_digest=config_digest,
        visible_token_digest=visible_token_digest,
        decision_digest=decision_digest,
        visible_tokens_identical=visible_tokens_identical,
        input_bytes=candidate_identity.byte_length,
        output_bytes=output_identity.byte_length,
        growth_bytes=output_identity.byte_length - candidate_identity.byte_length,
    )


def _fallback(
    candidate_markdown: str,
    source_digest: str,
    input_digest: str,
    config_digest: str,
    reason: str,
    *,
    candidate_identity: _TextIdentity | None = None,
) -> AtomicStructureOverlayDecisionV0:
    return _finish_decision(
        candidate_markdown=candidate_markdown,
        output_markdown=candidate_markdown,
        proposals=(),
        applied_proposal_ids=(),
        source_digest=source_digest,
        input_digest=input_digest,
        config_digest=config_digest,
        enabled=True,
        accepted=False,
        reason=reason,
        visible_tokens_identical=True,
        candidate_identity=candidate_identity,
        output_identity=candidate_identity,
    )


def _global_rejection(
    candidate_markdown: str,
    proposals: list[AtomicStructureProposalV0],
    source_digest: str,
    input_digest: str,
    config_digest: str,
    reason: str,
) -> AtomicStructureOverlayDecisionV0:
    rejected = tuple(
        _reject_proposal(proposal, reason) if proposal.accepted else proposal
        for proposal in proposals
    )
    return _finish_decision(
        candidate_markdown=candidate_markdown,
        output_markdown=candidate_markdown,
        proposals=rejected,
        applied_proposal_ids=(),
        source_digest=source_digest,
        input_digest=input_digest,
        config_digest=config_digest,
        enabled=True,
        accepted=False,
        reason=reason,
        visible_tokens_identical=reason != "global_visible_token_mismatch",
    )


def _bounded_record(
    decision: AtomicStructureOverlayDecisionV0,
    config: AtomicStructureOverlayV0Config,
) -> bool:
    if (
        type(decision.schema_version) is not str
        or decision.schema_version != ATOMIC_STRUCTURE_OVERLAY_V0_SCHEMA
        or type(decision.enabled) is not bool
        or type(decision.accepted) is not bool
        or type(decision.reason) is not str
        or len(decision.reason) > 128
        or type(decision.candidate_markdown) is not str
        or type(decision.output_markdown) is not str
        or len(decision.candidate_markdown) > config.max_candidate_bytes
        or len(decision.output_markdown) > config.max_output_bytes
        or type(decision.proposals) is not tuple
        or len(decision.proposals) > config.max_atoms
        or type(decision.applied_proposal_ids) is not tuple
        or len(decision.applied_proposal_ids) > config.max_atoms
        or any(
            type(proposal_id) is not str or len(proposal_id) != 64
            for proposal_id in decision.applied_proposal_ids
        )
        or not _bounded_digest(decision.source_digest)
        or not _bounded_digest(decision.input_digest)
        or not _bounded_digest(decision.output_digest)
        or not _bounded_digest(decision.config_digest)
        or not _bounded_digest(decision.visible_token_digest, allow_empty=True)
        or not _bounded_digest(decision.decision_digest)
        or type(decision.visible_tokens_identical) is not bool
        or not _bounded_int(decision.input_bytes, config.max_candidate_bytes)
        or not _bounded_int(decision.output_bytes, config.max_output_bytes)
        or not _bounded_signed_int(decision.growth_bytes, config.max_output_bytes)
        or type(decision.digest_is_authentication) is not bool
        or not _strict_utf8_within_budget(
            decision.candidate_markdown,
            config.max_candidate_bytes,
        )
        or not _strict_utf8_within_budget(
            decision.output_markdown,
            config.max_output_bytes,
        )
    ):
        return False
    total_certificate_bytes = 0
    for proposal in decision.proposals:
        if (
            type(proposal) is not AtomicStructureProposalV0
            or type(proposal.schema_version) is not str
            or proposal.schema_version != ATOMIC_STRUCTURE_PROPOSAL_V0_SCHEMA
            or not _bounded_digest(proposal.proposal_id)
            or type(proposal.atom_kind) is not str
            or proposal.atom_kind not in {"code", "table"}
            or type(proposal.selected_id) is not str
            or not proposal.selected_id
            or len(proposal.selected_id) > 128
            or not _bounded_int(proposal.source_order, 1_000_000_000)
            or type(proposal.accepted) is not bool
            or type(proposal.reason) is not str
            or len(proposal.reason) > 128
            or not _bounded_optional_offset(
                proposal.source_span_start,
                config.max_source_bytes,
            )
            or not _bounded_optional_offset(
                proposal.source_span_end,
                config.max_source_bytes,
            )
            or not _bounded_optional_offset(
                proposal.candidate_span_start,
                config.max_candidate_bytes,
            )
            or not _bounded_optional_offset(
                proposal.candidate_span_end,
                config.max_candidate_bytes,
            )
            or not _bounded_digest(proposal.source_digest)
            or not _bounded_digest(proposal.graph_digest, allow_empty=True)
            or not _bounded_digest(proposal.source_span_digest, allow_empty=True)
            or not _bounded_digest(proposal.input_digest)
            or not _bounded_digest(proposal.replacement_digest, allow_empty=True)
            or not _bounded_digest(proposal.patch_digest, allow_empty=True)
            or not _bounded_digest(proposal.config_digest)
            or not _bounded_digest(proposal.visible_token_digest, allow_empty=True)
            or not _bounded_digest(proposal.certificate_digest, allow_empty=True)
            or type(proposal.certificate) is not bytes
            or len(proposal.certificate) > config.max_certificate_bytes
            or not _bounded_int(
                proposal.visible_token_count,
                config.max_atom_tokens,
            )
            or not _bounded_int(proposal.input_bytes, config.max_candidate_bytes)
            or not _bounded_int(
                proposal.replacement_bytes,
                config.max_replacement_bytes,
            )
            or not _bounded_int(
                proposal.proposed_output_bytes,
                config.max_output_bytes,
            )
            or not _bounded_signed_int(
                proposal.growth_bytes,
                config.max_output_bytes,
            )
            or not _bounded_int(proposal.structural_score_before, 2)
            or not _bounded_int(proposal.structural_score_after, 2)
            or type(proposal.digest_is_authentication) is not bool
        ):
            return False
        total_certificate_bytes += len(proposal.certificate)
        if total_certificate_bytes > config.max_total_certificate_bytes:
            return False
    return True


def _bounded_digest(value: object, *, allow_empty: bool = False) -> bool:
    if type(value) is not str:
        return False
    if allow_empty and not value:
        return True
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _bounded_int(value: object, maximum: int) -> bool:
    return type(value) is int and 0 <= value <= maximum


def _bounded_signed_int(value: object, maximum_absolute: int) -> bool:
    return type(value) is int and -maximum_absolute <= value <= maximum_absolute


def _bounded_optional_offset(value: object, maximum: int) -> bool:
    return value is None or _bounded_int(value, maximum)


class _BudgetExceededError(Exception):
    pass

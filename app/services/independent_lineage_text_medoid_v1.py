"""Default-off, lineage-aware character-5-gram text medoid selection.

This module is intentionally pure and unwired.  It consumes already-produced
text candidates and terminal strategy/role receipts; it does not import HTML
parsers, extractors, or crawler routing code.  The algorithm is new and has not
been validated by the frozen public-benchmark protocol, whose exploratory
selector used jieba-tokenized ROUGE-5.  Its protocol revision therefore names the actual
similarity here: exact Unicode-codepoint character-5-gram multiset Dice.

Independence is not caller-declared.  Receipts keep the candidate's orchestration
role separate from the actual terminal strategy.  A closed policy derives
``selection_lineage`` only from that actual terminal strategy.  Native and
Python Trafilatura share ``trafilatura_derived``; Markdownify, raw-lxml,
documentation/GitHub subtrees, and semantic DOM views share
``dom_rendered_views``.  This deliberately underclaims independence.

Byte-identical texts are evaluated once while retaining the distinct lineages
that produced them.  A selectable text is eligible only when at least two
*other* lineages each meet the exact support floor.  The v1 floor is 100,000
parts per million (Dice >= 0.10); callers may raise but cannot lower it.  Zero
or insufficient support returns the caller's fallback object exactly.

SHA-256 values in the receipts are deterministic integrity identities, not
authentication.  The caller remains responsible for accepting terminal
receipts only from its trusted candidate orchestration boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from typing import Final, Literal, cast

INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_SCHEMA: Final = "clusy.independent-lineage-text-medoid.v1"
INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_PROTOCOL_REVISION: Final = (
    "unicode-codepoint-character-5gram-multiset-dice-independent-lineage-v1"
)
TERMINAL_STRATEGY_ROLE_RECEIPT_V1_SCHEMA: Final = "clusy.terminal-strategy-role-receipt.v1"
INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_SIMILARITY: Final = (
    "unicode-codepoint-character-5gram-multiset-dice"
)
INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_TIE_BREAK: Final = (
    "maximize-mean-independent-lineage-support-then-maximize-weakest-support-"
    "then-fixed-candidate-role-priority-then-lexicographically-smallest-"
    "text-sha256"
)
INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_GRAM_WIDTH: Final = 5
INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_MINIMUM_OTHER_LINEAGES: Final = 2
INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_MINIMUM_SUPPORT_PPM: Final = 100_000

_PARTS_PER_MILLION: Final = 1_000_000
_MAX_CANDIDATES: Final = 64
_MAX_CANDIDATE_BYTES: Final = 8 * 1024 * 1024
_MAX_TOTAL_CANDIDATE_BYTES: Final = 128 * 1024 * 1024
_MAX_UNIQUE_GRAMS: Final = 16_000_000
_MAX_WORK: Final = 256_000_000
_MAX_RECEIPT_BYTES: Final = 1024 * 1024
_MIN_RECEIPT_BYTES: Final = 4 * 1024
_MAX_IDENTIFIER_BYTES: Final = 256
_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}\Z", flags=re.ASCII)

CandidateRoleV1 = Literal[
    "production_balanced",
    "production_article_body",
    "python_trafilatura",
    "readability",
    "semantic_article_body",
    "semantic_article",
    "semantic_content_hint",
    "semantic_main",
    "markdownify",
]


@dataclass(frozen=True, slots=True)
class _CandidateRolePolicyV1:
    selectable: bool
    fallback: bool
    priority: int
    compatible_terminal_strategies: frozenset[str]


_PRODUCTION_TERMINAL_STRATEGIES: Final = frozenset(
    {
        "rs-trafilatura",
        "trafilatura",
        "readability",
        "markdownify",
        "raw_lxml",
        "documentation",
        "github-repository",
        "github-readme",
        "github-thread",
        "github-tree",
        "github-commit",
        "github-commit-partial",
        "github-compare",
        "github-compare-partial",
        "github-release",
        "github-source",
    }
)

# Candidate roles control only fallback/selectability.  They never establish
# independence, and their compatible terminal strategies are closed.
_CANDIDATE_ROLE_POLICY_V1: Final = {
    "production_balanced": _CandidateRolePolicyV1(
        True,
        True,
        0,
        _PRODUCTION_TERMINAL_STRATEGIES,
    ),
    "production_article_body": _CandidateRolePolicyV1(
        True,
        False,
        1,
        _PRODUCTION_TERMINAL_STRATEGIES,
    ),
    "python_trafilatura": _CandidateRolePolicyV1(
        True,
        False,
        2,
        frozenset({"trafilatura"}),
    ),
    "readability": _CandidateRolePolicyV1(
        True,
        False,
        3,
        frozenset({"readability"}),
    ),
    "semantic_article_body": _CandidateRolePolicyV1(
        True,
        False,
        4,
        frozenset({"semantic_article_body"}),
    ),
    "semantic_article": _CandidateRolePolicyV1(
        True,
        False,
        5,
        frozenset({"semantic_article"}),
    ),
    "semantic_content_hint": _CandidateRolePolicyV1(
        True,
        False,
        6,
        frozenset({"semantic_content_hint"}),
    ),
    "semantic_main": _CandidateRolePolicyV1(
        True,
        False,
        7,
        frozenset({"semantic_main"}),
    ),
    "markdownify": _CandidateRolePolicyV1(
        False,
        False,
        8,
        frozenset({"markdownify"}),
    ),
}

# This actual-terminal map is the independence boundary.  Receipts contain no
# caller-selectable family/lineage field.  Mixed/union terminal strategies are
# intentionally absent because assigning them one independent vote is unsafe.
_SELECTION_LINEAGE_BY_TERMINAL_STRATEGY_V1: Final = {
    "rs-trafilatura": "trafilatura_derived",
    "trafilatura": "trafilatura_derived",
    "readability": "readability",
    "markdownify": "dom_rendered_views",
    "raw_lxml": "dom_rendered_views",
    "documentation": "dom_rendered_views",
    "github-repository": "dom_rendered_views",
    "github-readme": "dom_rendered_views",
    "github-thread": "dom_rendered_views",
    "github-tree": "dom_rendered_views",
    "github-commit": "dom_rendered_views",
    "github-commit-partial": "dom_rendered_views",
    "github-compare": "dom_rendered_views",
    "github-compare-partial": "dom_rendered_views",
    "github-release": "dom_rendered_views",
    "github-source": "dom_rendered_views",
    "semantic_article_body": "dom_rendered_views",
    "semantic_article": "dom_rendered_views",
    "semantic_content_hint": "dom_rendered_views",
    "semantic_main": "dom_rendered_views",
}


class IndependentLineageTextMedoidV1Error(ValueError):
    """A terminal receipt could not be constructed by a trusted producer."""


class _SelectionRejectedError(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class TerminalStrategyRoleReceiptV1:
    """Integrity binding for one trusted terminal candidate invocation.

    The receipt intentionally has no lineage field.  Its digest detects
    accidental mutation but does not establish who produced it.
    """

    schema_version: str
    candidate_role: str
    terminal_strategy: str
    text_sha256: str
    text_bytes: int
    terminal_verified: bool
    digest_is_authentication: bool
    receipt_sha256: str


@dataclass(frozen=True, slots=True)
class IndependentLineageCandidateV1:
    """One exact candidate text plus its trusted terminal receipt."""

    text: str
    terminal_receipt: TerminalStrategyRoleReceiptV1


@dataclass(frozen=True, slots=True)
class IndependentLineageTextMedoidV1Config:
    """Explicit opt-in and caller-lowerable resource ceilings.

    ``minimum_support_ppm`` is different: 100,000 ppm is a protocol safety
    floor, so a caller may make selection stricter but cannot weaken it.
    """

    enabled: bool = False
    max_candidates: int = 16
    max_candidate_bytes: int = 2 * 1024 * 1024
    max_total_candidate_bytes: int = 16 * 1024 * 1024
    max_unique_grams: int = 4_000_000
    max_work: int = 64_000_000
    max_receipt_bytes: int = 64 * 1024
    minimum_support_ppm: int = INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_MINIMUM_SUPPORT_PPM

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be an exact bool")
        _bounded_positive_int("max_candidates", self.max_candidates, _MAX_CANDIDATES)
        _bounded_positive_int(
            "max_candidate_bytes",
            self.max_candidate_bytes,
            _MAX_CANDIDATE_BYTES,
        )
        _bounded_positive_int(
            "max_total_candidate_bytes",
            self.max_total_candidate_bytes,
            _MAX_TOTAL_CANDIDATE_BYTES,
        )
        if self.max_total_candidate_bytes < self.max_candidate_bytes:
            raise ValueError("total candidate budget must cover one candidate")
        _bounded_positive_int(
            "max_unique_grams",
            self.max_unique_grams,
            _MAX_UNIQUE_GRAMS,
        )
        _bounded_positive_int("max_work", self.max_work, _MAX_WORK)
        if (
            type(self.max_receipt_bytes) is not int
            or not _MIN_RECEIPT_BYTES <= self.max_receipt_bytes <= _MAX_RECEIPT_BYTES
        ):
            raise ValueError(
                f"max_receipt_bytes must be between {_MIN_RECEIPT_BYTES} and {_MAX_RECEIPT_BYTES}"
            )
        if (
            type(self.minimum_support_ppm) is not int
            or not INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_MINIMUM_SUPPORT_PPM
            <= self.minimum_support_ppm
            <= _PARTS_PER_MILLION
        ):
            raise ValueError("minimum_support_ppm cannot be below the 100000 ppm v1 safety floor")


@dataclass(frozen=True, slots=True)
class IndependentLineageSupportV1:
    """Exact selected-candidate support from one other lineage."""

    selection_lineage: str
    numerator: int
    denominator: int
    support_ppm: int
    passes_minimum_support: bool


@dataclass(frozen=True, slots=True)
class IndependentLineageTextMedoidReceiptV1:
    """Bounded decision receipt; it never embeds candidate text."""

    schema_version: str
    protocol_revision: str
    enabled: bool
    accepted: bool
    reason: str
    similarity: str
    gram_width: int
    minimum_other_lineages: int
    minimum_support_ppm: int
    tie_break: str
    config_sha256: str
    fallback_sha256: str
    fallback_bytes: int
    input_ledger_sha256: str
    decision_ledger_sha256: str
    candidate_count: int
    unique_text_count: int
    duplicate_candidate_count: int
    selection_lineage_count: int
    selectable_unique_text_count: int
    eligible_unique_text_count: int
    selected_candidate_role: str
    selected_terminal_strategy: str
    selected_selection_lineage: str
    selected_text_sha256: str
    selected_text_bytes: int
    selected_gram_count: int
    selected_mean_support_ppm: int
    selected_minimum_support_ppm: int
    selected_supports: tuple[IndependentLineageSupportV1, ...]
    deterministic: bool
    digest_is_authentication: bool
    receipt_sha256: str


@dataclass(frozen=True, slots=True)
class IndependentLineageTextMedoidResultV1:
    """A decision receipt and either the selected text or exact fallback."""

    receipt: IndependentLineageTextMedoidReceiptV1
    output: str

    @property
    def accepted(self) -> bool:
        return self.receipt.accepted

    @property
    def reason(self) -> str:
        return self.receipt.reason


@dataclass(frozen=True, slots=True)
class _VerifiedCandidateV1:
    text: str
    text_sha256: str
    text_bytes: int
    candidate_role: str
    terminal_strategy: str
    role_policy: _CandidateRolePolicyV1
    selection_lineage: str
    terminal_receipt_sha256: str


@dataclass(frozen=True, slots=True)
class _TextGroupV1:
    text: str
    text_sha256: str
    text_bytes: int
    members: tuple[_VerifiedCandidateV1, ...]
    lineages: frozenset[str]
    grams: Counter[str]
    gram_count: int


@dataclass(frozen=True, slots=True)
class _DecisionV1:
    group_index: int
    candidate: _VerifiedCandidateV1
    supports: tuple[tuple[str, Fraction], ...]
    mean_support: Fraction
    minimum_support: Fraction
    qualifying_lineages: int
    eligible: bool


@dataclass(slots=True)
class _WorkV1:
    limit: int
    spent: int = 0

    def spend(self, amount: int) -> None:
        if amount < 0 or amount > self.limit - self.spent:
            raise _SelectionRejectedError("work_budget")
        self.spent += amount


def _bounded_positive_int(name: str, value: int, maximum: int) -> None:
    if type(value) is not int or value <= 0 or value > maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: object) -> str:
    return _sha256_bytes(_canonical_json_bytes(value))


def _bounded_identifier(name: str, value: object) -> str:
    if type(value) is not str or not value:
        raise IndependentLineageTextMedoidV1Error(f"{name} must be a non-empty exact str")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise IndependentLineageTextMedoidV1Error(
            f"{name} must contain valid UTF-8 text"
        ) from error
    if len(encoded) > _MAX_IDENTIFIER_BYTES:
        raise IndependentLineageTextMedoidV1Error(f"{name} exceeds its byte budget")
    return value


def _hard_bounded_text(value: object) -> tuple[str, bytes]:
    if type(value) is not str or not value.strip():
        raise IndependentLineageTextMedoidV1Error("candidate text must be a non-empty exact str")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise IndependentLineageTextMedoidV1Error(
            "candidate text must contain valid UTF-8 text"
        ) from error
    if len(encoded) > _MAX_CANDIDATE_BYTES:
        raise IndependentLineageTextMedoidV1Error("candidate text exceeds the hard byte budget")
    return value, encoded


def _terminal_receipt_identity(
    *,
    candidate_role: str,
    terminal_strategy: str,
    text_sha256: str,
    text_bytes: int,
) -> dict[str, object]:
    return {
        "schema_version": TERMINAL_STRATEGY_ROLE_RECEIPT_V1_SCHEMA,
        "candidate_role": candidate_role,
        "terminal_strategy": terminal_strategy,
        "text_sha256": text_sha256,
        "text_bytes": text_bytes,
        "terminal_verified": True,
        "digest_is_authentication": False,
    }


def build_terminal_strategy_role_receipt_v1(
    *,
    text: str,
    candidate_role: CandidateRoleV1,
    terminal_strategy: str,
) -> TerminalStrategyRoleReceiptV1:
    """Bind trusted candidate-role/terminal-strategy provenance to exact text."""

    candidate_role_value = _bounded_identifier("candidate_role", candidate_role)
    terminal_strategy = _bounded_identifier("terminal_strategy", terminal_strategy)
    role_policy = _CANDIDATE_ROLE_POLICY_V1.get(candidate_role_value)
    selection_lineage = _SELECTION_LINEAGE_BY_TERMINAL_STRATEGY_V1.get(terminal_strategy)
    if (
        role_policy is None
        or selection_lineage is None
        or terminal_strategy not in role_policy.compatible_terminal_strategies
    ):
        raise IndependentLineageTextMedoidV1Error(
            "candidate role/terminal strategy pair is not in the closed v1 policy"
        )
    text, encoded = _hard_bounded_text(text)
    text_sha256 = _sha256_bytes(encoded)
    identity = _terminal_receipt_identity(
        candidate_role=candidate_role_value,
        terminal_strategy=terminal_strategy,
        text_sha256=text_sha256,
        text_bytes=len(encoded),
    )
    return TerminalStrategyRoleReceiptV1(
        schema_version=TERMINAL_STRATEGY_ROLE_RECEIPT_V1_SCHEMA,
        candidate_role=candidate_role_value,
        terminal_strategy=terminal_strategy,
        text_sha256=text_sha256,
        text_bytes=len(encoded),
        terminal_verified=True,
        digest_is_authentication=False,
        receipt_sha256=_sha256_json(identity),
    )


def verify_terminal_strategy_role_receipt_v1(
    receipt: object,
    *,
    text: object,
) -> bool:
    """Verify canonical integrity and exact text binding, not authenticity."""

    if type(receipt) is not TerminalStrategyRoleReceiptV1:
        return False
    try:
        terminal_strategy = _bounded_identifier(
            "terminal_strategy",
            receipt.terminal_strategy,
        )
        candidate_role = _bounded_identifier("candidate_role", receipt.candidate_role)
        role_policy = _CANDIDATE_ROLE_POLICY_V1.get(candidate_role)
        selection_lineage = _SELECTION_LINEAGE_BY_TERMINAL_STRATEGY_V1.get(terminal_strategy)
        if (
            role_policy is None
            or selection_lineage is None
            or terminal_strategy not in role_policy.compatible_terminal_strategies
        ):
            return False
        text, encoded = _hard_bounded_text(text)
        del text
        if (
            type(receipt.schema_version) is not str
            or receipt.schema_version != TERMINAL_STRATEGY_ROLE_RECEIPT_V1_SCHEMA
            or type(receipt.text_sha256) is not str
            or _SHA256_RE.fullmatch(receipt.text_sha256) is None
            or type(receipt.text_bytes) is not int
            or receipt.text_bytes <= 0
            or receipt.text_bytes > _MAX_CANDIDATE_BYTES
            or receipt.terminal_verified is not True
            or receipt.digest_is_authentication is not False
            or type(receipt.receipt_sha256) is not str
            or _SHA256_RE.fullmatch(receipt.receipt_sha256) is None
        ):
            return False
        identity = _terminal_receipt_identity(
            candidate_role=candidate_role,
            terminal_strategy=terminal_strategy,
            text_sha256=receipt.text_sha256,
            text_bytes=receipt.text_bytes,
        )
        return (
            receipt.text_bytes == len(encoded)
            and receipt.text_sha256 == _sha256_bytes(encoded)
            and receipt.receipt_sha256 == _sha256_json(identity)
        )
    except (AttributeError, IndependentLineageTextMedoidV1Error, TypeError, ValueError):
        return False


def _config_identity(config: IndependentLineageTextMedoidV1Config) -> dict[str, object]:
    return {
        "enabled": config.enabled,
        "max_candidates": config.max_candidates,
        "max_candidate_bytes": config.max_candidate_bytes,
        "max_total_candidate_bytes": config.max_total_candidate_bytes,
        "max_unique_grams": config.max_unique_grams,
        "max_work": config.max_work,
        "max_receipt_bytes": config.max_receipt_bytes,
        "minimum_support_ppm": config.minimum_support_ppm,
    }


def _fraction_ppm(value: Fraction) -> int:
    return value.numerator * _PARTS_PER_MILLION // value.denominator


def _passes_support(value: Fraction, minimum_support_ppm: int) -> bool:
    return (
        value.numerator > 0
        and value.numerator * _PARTS_PER_MILLION >= minimum_support_ppm * value.denominator
    )


def _support_payload(
    selection_lineage: str,
    value: Fraction,
    minimum_support_ppm: int,
) -> dict[str, object]:
    return {
        "selection_lineage": selection_lineage,
        "numerator": value.numerator,
        "denominator": value.denominator,
        "support_ppm": _fraction_ppm(value),
        "passes_minimum_support": _passes_support(value, minimum_support_ppm),
    }


def _public_support(
    selection_lineage: str,
    value: Fraction,
    minimum_support_ppm: int,
) -> IndependentLineageSupportV1:
    return IndependentLineageSupportV1(
        selection_lineage=selection_lineage,
        numerator=value.numerator,
        denominator=value.denominator,
        support_ppm=_fraction_ppm(value),
        passes_minimum_support=_passes_support(value, minimum_support_ppm),
    )


def _receipt_identity(
    *,
    config: IndependentLineageTextMedoidV1Config,
    enabled: bool,
    accepted: bool,
    reason: str,
    fallback_sha256: str,
    fallback_bytes: int,
    input_ledger_sha256: str,
    decision_ledger_sha256: str,
    candidate_count: int,
    unique_text_count: int,
    duplicate_candidate_count: int,
    selection_lineage_count: int,
    selectable_unique_text_count: int,
    eligible_unique_text_count: int,
    selected_candidate_role: str,
    selected_terminal_strategy: str,
    selected_selection_lineage: str,
    selected_text_sha256: str,
    selected_text_bytes: int,
    selected_gram_count: int,
    selected_mean_support_ppm: int,
    selected_minimum_support_ppm: int,
    selected_supports: tuple[IndependentLineageSupportV1, ...],
) -> dict[str, object]:
    return {
        "schema_version": INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_SCHEMA,
        "protocol_revision": INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_PROTOCOL_REVISION,
        "enabled": enabled,
        "accepted": accepted,
        "reason": reason,
        "similarity": INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_SIMILARITY,
        "gram_width": INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_GRAM_WIDTH,
        "minimum_other_lineages": (INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_MINIMUM_OTHER_LINEAGES),
        "minimum_support_ppm": config.minimum_support_ppm,
        "tie_break": INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_TIE_BREAK,
        "config_sha256": _sha256_json(_config_identity(config)),
        "fallback_sha256": fallback_sha256,
        "fallback_bytes": fallback_bytes,
        "input_ledger_sha256": input_ledger_sha256,
        "decision_ledger_sha256": decision_ledger_sha256,
        "candidate_count": candidate_count,
        "unique_text_count": unique_text_count,
        "duplicate_candidate_count": duplicate_candidate_count,
        "selection_lineage_count": selection_lineage_count,
        "selectable_unique_text_count": selectable_unique_text_count,
        "eligible_unique_text_count": eligible_unique_text_count,
        "selected_candidate_role": selected_candidate_role,
        "selected_terminal_strategy": selected_terminal_strategy,
        "selected_selection_lineage": selected_selection_lineage,
        "selected_text_sha256": selected_text_sha256,
        "selected_text_bytes": selected_text_bytes,
        "selected_gram_count": selected_gram_count,
        "selected_mean_support_ppm": selected_mean_support_ppm,
        "selected_minimum_support_ppm": selected_minimum_support_ppm,
        "selected_supports": [
            {
                "selection_lineage": support.selection_lineage,
                "numerator": support.numerator,
                "denominator": support.denominator,
                "support_ppm": support.support_ppm,
                "passes_minimum_support": support.passes_minimum_support,
            }
            for support in selected_supports
        ],
        "deterministic": True,
        "digest_is_authentication": False,
    }


def _make_receipt(
    *,
    config: IndependentLineageTextMedoidV1Config,
    enabled: bool,
    accepted: bool,
    reason: str,
    fallback_sha256: str = "",
    fallback_bytes: int = 0,
    input_ledger_sha256: str = "",
    decision_ledger_sha256: str = "",
    candidate_count: int = 0,
    unique_text_count: int = 0,
    duplicate_candidate_count: int = 0,
    selection_lineage_count: int = 0,
    selectable_unique_text_count: int = 0,
    eligible_unique_text_count: int = 0,
    selected_candidate_role: str = "",
    selected_terminal_strategy: str = "",
    selected_selection_lineage: str = "",
    selected_text_sha256: str = "",
    selected_text_bytes: int = 0,
    selected_gram_count: int = 0,
    selected_mean_support_ppm: int = 0,
    selected_minimum_support_ppm: int = 0,
    selected_supports: tuple[IndependentLineageSupportV1, ...] = (),
) -> IndependentLineageTextMedoidReceiptV1:
    identity = _receipt_identity(
        config=config,
        enabled=enabled,
        accepted=accepted,
        reason=reason,
        fallback_sha256=fallback_sha256,
        fallback_bytes=fallback_bytes,
        input_ledger_sha256=input_ledger_sha256,
        decision_ledger_sha256=decision_ledger_sha256,
        candidate_count=candidate_count,
        unique_text_count=unique_text_count,
        duplicate_candidate_count=duplicate_candidate_count,
        selection_lineage_count=selection_lineage_count,
        selectable_unique_text_count=selectable_unique_text_count,
        eligible_unique_text_count=eligible_unique_text_count,
        selected_candidate_role=selected_candidate_role,
        selected_terminal_strategy=selected_terminal_strategy,
        selected_selection_lineage=selected_selection_lineage,
        selected_text_sha256=selected_text_sha256,
        selected_text_bytes=selected_text_bytes,
        selected_gram_count=selected_gram_count,
        selected_mean_support_ppm=selected_mean_support_ppm,
        selected_minimum_support_ppm=selected_minimum_support_ppm,
        selected_supports=selected_supports,
    )
    encoded = _canonical_json_bytes(identity)
    if len(encoded) > config.max_receipt_bytes:
        raise _SelectionRejectedError("receipt_byte_budget")
    return IndependentLineageTextMedoidReceiptV1(
        schema_version=INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_SCHEMA,
        protocol_revision=INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_PROTOCOL_REVISION,
        enabled=enabled,
        accepted=accepted,
        reason=reason,
        similarity=INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_SIMILARITY,
        gram_width=INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_GRAM_WIDTH,
        minimum_other_lineages=(INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_MINIMUM_OTHER_LINEAGES),
        minimum_support_ppm=config.minimum_support_ppm,
        tie_break=INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_TIE_BREAK,
        config_sha256=_sha256_json(_config_identity(config)),
        fallback_sha256=fallback_sha256,
        fallback_bytes=fallback_bytes,
        input_ledger_sha256=input_ledger_sha256,
        decision_ledger_sha256=decision_ledger_sha256,
        candidate_count=candidate_count,
        unique_text_count=unique_text_count,
        duplicate_candidate_count=duplicate_candidate_count,
        selection_lineage_count=selection_lineage_count,
        selectable_unique_text_count=selectable_unique_text_count,
        eligible_unique_text_count=eligible_unique_text_count,
        selected_candidate_role=selected_candidate_role,
        selected_terminal_strategy=selected_terminal_strategy,
        selected_selection_lineage=selected_selection_lineage,
        selected_text_sha256=selected_text_sha256,
        selected_text_bytes=selected_text_bytes,
        selected_gram_count=selected_gram_count,
        selected_mean_support_ppm=selected_mean_support_ppm,
        selected_minimum_support_ppm=selected_minimum_support_ppm,
        selected_supports=selected_supports,
        deterministic=True,
        digest_is_authentication=False,
        receipt_sha256=_sha256_bytes(encoded),
    )


def _fallback_result(
    fallback_text: str,
    *,
    config: IndependentLineageTextMedoidV1Config,
    reason: str,
    enabled: bool,
    fallback_sha256: str = "",
    fallback_bytes: int = 0,
) -> IndependentLineageTextMedoidResultV1:
    receipt = _make_receipt(
        config=config,
        enabled=enabled,
        accepted=False,
        reason=reason,
        fallback_sha256=fallback_sha256,
        fallback_bytes=fallback_bytes,
    )
    return IndependentLineageTextMedoidResultV1(receipt=receipt, output=fallback_text)


def _validate_candidates(
    candidates: object,
    *,
    fallback_text: str,
    fallback_sha256: str,
    config: IndependentLineageTextMedoidV1Config,
) -> tuple[_VerifiedCandidateV1, ...]:
    if type(candidates) not in {tuple, list}:
        raise _SelectionRejectedError("invalid_candidate_container")
    candidate_values = cast("tuple[object, ...] | list[object]", candidates)
    if not 1 <= len(candidate_values) <= config.max_candidates:
        raise _SelectionRejectedError("candidate_count_budget")
    candidate_snapshot = (
        candidate_values if type(candidate_values) is tuple else tuple(candidate_values)
    )
    if not 1 <= len(candidate_snapshot) <= config.max_candidates:
        raise _SelectionRejectedError("candidate_count_budget")

    verified: list[_VerifiedCandidateV1] = []
    candidate_roles: set[str] = set()
    total_bytes = 0
    fallback_count = 0
    for candidate in candidate_snapshot:
        if type(candidate) is not IndependentLineageCandidateV1:
            raise _SelectionRejectedError("invalid_candidate")
        if type(candidate.text) is not str or not candidate.text.strip():
            raise _SelectionRejectedError("invalid_candidate")
        try:
            encoded = candidate.text.encode("utf-8")
        except UnicodeEncodeError as error:
            raise _SelectionRejectedError("invalid_candidate_utf8") from error
        if len(encoded) > config.max_candidate_bytes:
            raise _SelectionRejectedError("candidate_byte_budget")
        total_bytes += len(encoded)
        if total_bytes > config.max_total_candidate_bytes:
            raise _SelectionRejectedError("total_candidate_byte_budget")
        if not verify_terminal_strategy_role_receipt_v1(
            candidate.terminal_receipt,
            text=candidate.text,
        ):
            raise _SelectionRejectedError("invalid_terminal_receipt")
        receipt = candidate.terminal_receipt
        strategy = receipt.terminal_strategy
        candidate_role = receipt.candidate_role
        if candidate_role in candidate_roles:
            raise _SelectionRejectedError("duplicate_candidate_role")
        candidate_roles.add(candidate_role)
        role_policy = _CANDIDATE_ROLE_POLICY_V1[candidate_role]
        selection_lineage = _SELECTION_LINEAGE_BY_TERMINAL_STRATEGY_V1[strategy]
        if role_policy.fallback:
            fallback_count += 1
            if candidate.text != fallback_text or receipt.text_sha256 != fallback_sha256:
                raise _SelectionRejectedError("fallback_receipt_mismatch")
        verified.append(
            _VerifiedCandidateV1(
                text=candidate.text,
                text_sha256=receipt.text_sha256,
                text_bytes=receipt.text_bytes,
                candidate_role=candidate_role,
                terminal_strategy=strategy,
                role_policy=role_policy,
                selection_lineage=selection_lineage,
                terminal_receipt_sha256=receipt.receipt_sha256,
            )
        )
    if fallback_count != 1:
        raise _SelectionRejectedError("missing_fallback_receipt")
    return tuple(sorted(verified, key=lambda item: item.role_policy.priority))


def _character_grams(value: str) -> Counter[str]:
    width = INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_GRAM_WIDTH
    return Counter(value[index : index + width] for index in range(max(len(value) - width + 1, 0)))


def _multiset_dice(
    left: _TextGroupV1,
    right: _TextGroupV1,
    *,
    work: _WorkV1,
) -> Fraction:
    denominator = left.gram_count + right.gram_count
    if denominator == 0:
        return Fraction(0, 1)
    smaller, larger = (
        (left.grams, right.grams)
        if len(left.grams) <= len(right.grams)
        else (right.grams, left.grams)
    )
    work.spend(len(smaller))
    overlap = sum(min(count, larger.get(gram, 0)) for gram, count in smaller.items())
    return Fraction(2 * overlap, denominator)


def _build_groups(
    candidates: tuple[_VerifiedCandidateV1, ...],
    *,
    config: IndependentLineageTextMedoidV1Config,
    work: _WorkV1,
) -> tuple[_TextGroupV1, ...]:
    members_by_text: dict[str, list[_VerifiedCandidateV1]] = {}
    for candidate in candidates:
        members_by_text.setdefault(candidate.text, []).append(candidate)

    identities = sorted(
        (
            (_sha256_bytes(text.encode("utf-8")), text, members)
            for text, members in members_by_text.items()
        ),
        key=lambda item: (item[0], item[1]),
    )
    total_unique_grams = sum(
        max(len(text) - INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_GRAM_WIDTH + 1, 0)
        for _, text, _ in identities
    )
    if total_unique_grams > config.max_unique_grams:
        raise _SelectionRejectedError("unique_gram_budget")
    work.spend(total_unique_grams)

    groups: list[_TextGroupV1] = []
    for text_sha256, text, members in identities:
        grams = _character_grams(text)
        groups.append(
            _TextGroupV1(
                text=text,
                text_sha256=text_sha256,
                text_bytes=len(text.encode("utf-8")),
                members=tuple(members),
                lineages=frozenset(member.selection_lineage for member in members),
                grams=grams,
                gram_count=sum(grams.values()),
            )
        )
    return tuple(groups)


def _representative(group: _TextGroupV1) -> _VerifiedCandidateV1 | None:
    selectable = [member for member in group.members if member.role_policy.selectable]
    if not selectable:
        return None
    return min(selectable, key=lambda item: item.role_policy.priority)


def _decision_payload(
    decision: _DecisionV1,
    groups: tuple[_TextGroupV1, ...],
    minimum_support_ppm: int,
) -> dict[str, object]:
    group = groups[decision.group_index]
    return {
        "candidate_role": decision.candidate.candidate_role,
        "terminal_strategy": decision.candidate.terminal_strategy,
        "selection_lineage": decision.candidate.selection_lineage,
        "text_sha256": group.text_sha256,
        "text_bytes": group.text_bytes,
        "gram_count": group.gram_count,
        "duplicate_members": len(group.members) - 1,
        "contributing_lineages": sorted(group.lineages),
        "supports": [
            _support_payload(lineage, value, minimum_support_ppm)
            for lineage, value in decision.supports
        ],
        "mean_support": {
            "numerator": decision.mean_support.numerator,
            "denominator": decision.mean_support.denominator,
        },
        "minimum_support": {
            "numerator": decision.minimum_support.numerator,
            "denominator": decision.minimum_support.denominator,
        },
        "qualifying_lineages": decision.qualifying_lineages,
        "eligible": decision.eligible,
    }


def _make_decisions(
    groups: tuple[_TextGroupV1, ...],
    *,
    config: IndependentLineageTextMedoidV1Config,
    work: _WorkV1,
) -> tuple[_DecisionV1, ...]:
    similarities: dict[tuple[int, int], Fraction] = {}
    for left_index, left in enumerate(groups):
        similarities[(left_index, left_index)] = (
            Fraction(1, 1) if left.gram_count else Fraction(0, 1)
        )
        for right_index in range(left_index + 1, len(groups)):
            value = _multiset_dice(left, groups[right_index], work=work)
            similarities[(left_index, right_index)] = value

    all_lineages = sorted({lineage for group in groups for lineage in group.lineages})
    decisions: list[_DecisionV1] = []
    for group_index, group in enumerate(groups):
        representative = _representative(group)
        if representative is None:
            continue
        own_lineage = representative.selection_lineage
        supports: list[tuple[str, Fraction]] = []
        for lineage in all_lineages:
            if lineage == own_lineage:
                continue
            peers = [
                peer_index for peer_index, peer in enumerate(groups) if lineage in peer.lineages
            ]
            if not peers:
                continue
            values = (
                similarities[
                    (
                        min(group_index, peer_index),
                        max(group_index, peer_index),
                    )
                ]
                for peer_index in peers
            )
            supports.append((lineage, max(values)))
        support_values = [value for _, value in supports]
        mean_support = (
            sum(support_values, start=Fraction(0, 1)) / len(support_values)
            if support_values
            else Fraction(0, 1)
        )
        minimum_support = min(support_values, default=Fraction(0, 1))
        qualifying = sum(
            _passes_support(value, config.minimum_support_ppm) for value in support_values
        )
        decisions.append(
            _DecisionV1(
                group_index=group_index,
                candidate=representative,
                supports=tuple(supports),
                mean_support=mean_support,
                minimum_support=minimum_support,
                qualifying_lineages=qualifying,
                eligible=(qualifying >= INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_MINIMUM_OTHER_LINEAGES),
            )
        )
    return tuple(decisions)


def _is_better_decision(
    candidate: _DecisionV1,
    incumbent: _DecisionV1,
    groups: tuple[_TextGroupV1, ...],
) -> bool:
    if candidate.mean_support != incumbent.mean_support:
        return candidate.mean_support > incumbent.mean_support
    if candidate.minimum_support != incumbent.minimum_support:
        return candidate.minimum_support > incumbent.minimum_support
    if candidate.candidate.role_policy.priority != incumbent.candidate.role_policy.priority:
        return candidate.candidate.role_policy.priority < incumbent.candidate.role_policy.priority
    candidate_sha = groups[candidate.group_index].text_sha256
    incumbent_sha = groups[incumbent.group_index].text_sha256
    return candidate_sha < incumbent_sha


def select_independent_lineage_text_medoid_v1(
    fallback_text: str,
    candidates: object,
    *,
    config: IndependentLineageTextMedoidV1Config | None = None,
) -> IndependentLineageTextMedoidResultV1:
    """Select a bounded independent-lineage medoid or return exact fallback.

    Disabled mode inspects neither ``fallback_text`` nor ``candidates``.  In
    enabled mode every malformed receipt, budget failure, absent fallback
    binding, or insufficient two-lineage support fails closed.
    """

    selected_config = config or IndependentLineageTextMedoidV1Config()
    if not selected_config.enabled:
        return _fallback_result(
            fallback_text,
            config=selected_config,
            reason="disabled",
            enabled=False,
        )

    try:
        if not isinstance(fallback_text, str):
            raise _SelectionRejectedError("invalid_fallback")
        fallback_snapshot = str.__getitem__(fallback_text, slice(None))
        if not fallback_snapshot.strip():
            raise _SelectionRejectedError("invalid_fallback")
        try:
            fallback_encoded = fallback_snapshot.encode("utf-8")
        except UnicodeEncodeError as error:
            raise _SelectionRejectedError("invalid_fallback_utf8") from error
        if len(fallback_encoded) > selected_config.max_candidate_bytes:
            raise _SelectionRejectedError("fallback_byte_budget")
        fallback_sha256 = _sha256_bytes(fallback_encoded)
    except _SelectionRejectedError as rejection:
        return _fallback_result(
            fallback_text,
            config=selected_config,
            reason=rejection.reason,
            enabled=True,
        )

    try:
        verified = _validate_candidates(
            candidates,
            fallback_text=fallback_snapshot,
            fallback_sha256=fallback_sha256,
            config=selected_config,
        )
        input_ledger = [
            {
                "candidate_role": candidate.candidate_role,
                "terminal_strategy": candidate.terminal_strategy,
                "selection_lineage": candidate.selection_lineage,
                "text_sha256": candidate.text_sha256,
                "text_bytes": candidate.text_bytes,
                "terminal_receipt_sha256": candidate.terminal_receipt_sha256,
            }
            for candidate in verified
        ]
        input_ledger_sha256 = _sha256_json(input_ledger)
        work = _WorkV1(selected_config.max_work)
        groups = _build_groups(verified, config=selected_config, work=work)
        decisions = _make_decisions(groups, config=selected_config, work=work)
        decision_ledger_sha256 = _sha256_json(
            [
                _decision_payload(decision, groups, selected_config.minimum_support_ppm)
                for decision in decisions
            ]
        )
        eligible = [decision for decision in decisions if decision.eligible]
        selection_lineage_count = len({candidate.selection_lineage for candidate in verified})
        if not eligible:
            fallback_candidate = next(
                candidate for candidate in verified if candidate.role_policy.fallback
            )
            receipt = _make_receipt(
                config=selected_config,
                accepted=False,
                reason="insufficient_independent_lineage_support",
                enabled=True,
                fallback_sha256=fallback_sha256,
                fallback_bytes=len(fallback_encoded),
                selected_candidate_role=fallback_candidate.candidate_role,
                selected_terminal_strategy=fallback_candidate.terminal_strategy,
                selected_selection_lineage=fallback_candidate.selection_lineage,
                selected_text_sha256=fallback_sha256,
                selected_text_bytes=len(fallback_encoded),
                selected_gram_count=max(
                    len(fallback_snapshot) - INDEPENDENT_LINEAGE_TEXT_MEDOID_V1_GRAM_WIDTH + 1,
                    0,
                ),
                input_ledger_sha256=input_ledger_sha256,
                decision_ledger_sha256=decision_ledger_sha256,
                candidate_count=len(verified),
                unique_text_count=len(groups),
                duplicate_candidate_count=len(verified) - len(groups),
                selection_lineage_count=selection_lineage_count,
                selectable_unique_text_count=len(decisions),
                eligible_unique_text_count=0,
            )
            return IndependentLineageTextMedoidResultV1(
                receipt=receipt,
                output=fallback_text,
            )

        selected = eligible[0]
        for decision in eligible[1:]:
            if _is_better_decision(decision, selected, groups):
                selected = decision
        selected_group = groups[selected.group_index]
        selected_supports = tuple(
            _public_support(lineage, value, selected_config.minimum_support_ppm)
            for lineage, value in selected.supports
        )
        receipt = _make_receipt(
            config=selected_config,
            enabled=True,
            accepted=True,
            reason="accepted_independent_lineage_character_5gram_medoid",
            fallback_sha256=fallback_sha256,
            fallback_bytes=len(fallback_encoded),
            selected_candidate_role=selected.candidate.candidate_role,
            selected_terminal_strategy=selected.candidate.terminal_strategy,
            selected_selection_lineage=selected.candidate.selection_lineage,
            selected_text_sha256=selected_group.text_sha256,
            selected_text_bytes=selected_group.text_bytes,
            selected_gram_count=selected_group.gram_count,
            selected_mean_support_ppm=_fraction_ppm(selected.mean_support),
            selected_minimum_support_ppm=_fraction_ppm(selected.minimum_support),
            selected_supports=selected_supports,
            input_ledger_sha256=input_ledger_sha256,
            decision_ledger_sha256=decision_ledger_sha256,
            candidate_count=len(verified),
            unique_text_count=len(groups),
            duplicate_candidate_count=len(verified) - len(groups),
            selection_lineage_count=selection_lineage_count,
            selectable_unique_text_count=len(decisions),
            eligible_unique_text_count=len(eligible),
        )
        return IndependentLineageTextMedoidResultV1(
            receipt=receipt,
            output=selected.candidate.text,
        )
    except _SelectionRejectedError as rejection:
        return _fallback_result(
            fallback_text,
            config=selected_config,
            reason=rejection.reason,
            enabled=True,
            fallback_sha256=fallback_sha256,
            fallback_bytes=len(fallback_encoded),
        )

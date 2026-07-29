"""Exact reference decoder for a canonical typed source-span lattice.

This module is deliberately outside ``app`` and is not imported by production.
It defines the invariants and oracle used to evaluate a future native decoder.

The scheduling objective has an explicit numerical contract. Local
log-sum-exp/posterior primitives are evaluated as finite binary64 values. Those
values and all frozen weights are then lifted into exact rational arithmetic
before node, transition, path, and tie comparisons. The public score is rounded
to binary64 once, after decoding, and an unrepresentable result is rejected.

The polynomial decoder relies on canonical source identities:

* all local type interpretations of one source identity have the same span and
  structural metadata;
* one block identity maps to one canonical span, granularity, and ancestor set;
  aliases are allowed only when all of that metadata agrees; and
* a represented ancestor span contains every represented descendant span.

Those invariants turn source-identity and ancestor exclusion into interval
exclusion. Arbitrary "coloured interval" inputs are rejected instead of being
silently decoded by an exponential algorithm.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import islice
from math import gcd
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence


_HARD_MAX_CANDIDATES = 4096
_HARD_MAX_ANCESTOR_REFERENCES = 65_536
_HARD_MAX_BRUTE_FORCE_CANDIDATES = 64
_HARD_MAX_BRUTE_FORCE_SPANS = 16
_HARD_MAX_DOCUMENT_CHARS = 10_000_000
_HARD_MAX_COORDINATE_BITS = _HARD_MAX_DOCUMENT_CHARS.bit_length()
_HARD_MAX_STRING_CODEPOINTS = 1_000_000
_HARD_MAX_WEIGHT_ENTRIES = 1024
_HARD_MAX_BINARY64_INTEGER_BITS = 1024
_HARD_MAX_RATIONAL_COMPONENT_BITS = 16_384
_HARD_MAX_RATIONAL_ADMISSION_FUEL = 1 << 42


def _finite_float(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    try:
        if isinstance(value, float):
            numeric = float.__float__(value)
        else:
            # ``int.__int__`` copies an int subclass into a base int. Preflight
            # through the base bit-length operation so a caller cannot force a
            # giant allocation before binary64 conversion rejects the value.
            if int.bit_length(value) > _HARD_MAX_BINARY64_INTEGER_BITS:
                raise ValueError(f"{label} must be a finite number")
            numeric = float(int.__int__(value))
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(numeric):
        raise ValueError(f"{label} must be a finite number")
    return numeric


def _strict_int(
    value: object,
    *,
    label: str,
    max_bits: int,
    positive: bool = False,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    # The base preflight is deliberately before ``int.__int__``: converting an
    # int subclass allocates a base-int copy proportional to its magnitude.
    if int.bit_length(value) > max_bits:
        raise ValueError(f"{label} exceeds the compiled bit-length limit")
    snapshot = int.__int__(value)
    if positive and snapshot <= 0:
        raise ValueError(f"{label} must be positive")
    return snapshot


def _bounded_budget(value: object, *, label: str, hard_limit: int) -> int:
    budget = _strict_int(
        value,
        label=label,
        max_bits=hard_limit.bit_length(),
        positive=True,
    )
    if budget > hard_limit:
        raise ValueError(f"{label} exceeds the hard limit of {hard_limit}")
    return budget


@dataclass(frozen=True, slots=True)
class _CatalogBudgets:
    candidates: int
    document_chars: int
    ancestor_references: int
    string_codepoints: int


@dataclass(slots=True)
class _StringSnapshotBudget:
    limit: int
    used: int = 0

    def snapshot(self, value: object, *, label: str) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{label} must be a non-empty string")
        # ``str.__str__`` copies a str subclass into a base str. Charge its
        # immutable base length before that allocation, without dispatching a
        # hostile ``__len__`` override.
        codepoints = str.__len__(value)
        if codepoints == 0:
            raise ValueError(f"{label} must be a non-empty string")
        if codepoints > self.limit - self.used:
            raise ValueError("string codepoint budget exceeded")
        snapshot = str.__str__(value)
        self.used += codepoints
        return snapshot


def _snapshot_tuple(
    value: object,
    *,
    label: str,
    max_items: int | None = None,
    overflow_message: str | None = None,
) -> tuple[object, ...]:
    if not isinstance(value, tuple):
        raise ValueError(f"{label} must be a tuple")
    if max_items is not None and tuple.__len__(value) > max_items:
        raise ValueError(overflow_message or f"{label} exceeds its item budget")
    # Bypass a tuple subclass's stateful __iter__/__len__ overrides.
    return tuple(tuple.__iter__(value))


def _validated_catalog_budgets(
    *,
    max_candidates: object,
    candidate_label: str,
    candidate_hard_limit: int,
    max_document_chars: object,
    max_ancestor_references: object,
    max_string_codepoints: object,
) -> _CatalogBudgets:
    return _CatalogBudgets(
        candidates=_bounded_budget(
            max_candidates,
            label=candidate_label,
            hard_limit=candidate_hard_limit,
        ),
        document_chars=_bounded_budget(
            max_document_chars,
            label="document budget",
            hard_limit=_HARD_MAX_DOCUMENT_CHARS,
        ),
        ancestor_references=_bounded_budget(
            max_ancestor_references,
            label="ancestor reference budget",
            hard_limit=_HARD_MAX_ANCESTOR_REFERENCES,
        ),
        string_codepoints=_bounded_budget(
            max_string_codepoints,
            label="string codepoint budget",
            hard_limit=_HARD_MAX_STRING_CODEPOINTS,
        ),
    )


def _checked_fsum(values: Iterable[float], *, label: str) -> float:
    try:
        result = math.fsum(values)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{label} must remain finite") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must remain finite")
    return result


def _integer_bits(value: int) -> int:
    return max(1, int.bit_length(value))


@dataclass(frozen=True, slots=True)
class _RationalArithmetic:
    """Fail-closed exact arithmetic with component and admission bounds.

    Every accepted Fraction component and every pre-reduction integer product
    created by result-producing arithmetic has magnitude below
    ``2**max_component_bits``. Exact ``Fraction`` comparisons may instead
    create stdlib cross-products below ``2**(2 * max_component_bits)``; those
    comparisons do not consume admission fuel. Each public lift or unary
    operation on component width ``b`` consumes ``b**2`` deterministic
    admission-fuel units; each binary operation consumes ``4*b**2``. Charging
    happens after input/canonical validation (and division-by-zero rejection)
    but before any algebraic fast path or result-producing arithmetic.
    Admission fuel is a deterministic policy brake, not a cost model or an
    upper bound for validation, comparison, GCD, multiplication, allocation,
    bit complexity, or wall-clock work. Cross-cancellation and denominator-GCD
    reduction happen before any result-producing product, and the product is
    proved to fit before it is allocated.

    Thus accepted operations are ordinary ``Fraction`` operations and retain
    exactness, while an input sequence whose exact denominator would keep
    accumulating independent factors fails before allocating that denominator.
    """

    max_component_bits: int = _HARD_MAX_RATIONAL_COMPONENT_BITS
    max_admission_fuel: int = _HARD_MAX_RATIONAL_ADMISSION_FUEL
    used_admission_fuel: int = field(default=0, init=False)
    _max_magnitude: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        component_bits = _bounded_budget(
            self.max_component_bits,
            label="exact rational component bit limit",
            hard_limit=_HARD_MAX_RATIONAL_COMPONENT_BITS,
        )
        admission_fuel = _bounded_budget(
            self.max_admission_fuel,
            label="exact rational admission fuel",
            hard_limit=_HARD_MAX_RATIONAL_ADMISSION_FUEL,
        )
        object.__setattr__(self, "max_component_bits", component_bits)
        object.__setattr__(self, "max_admission_fuel", admission_fuel)
        object.__setattr__(self, "_max_magnitude", (1 << component_bits) - 1)

    def _validate(self, value: Fraction) -> Fraction:
        if type(value) is not Fraction:
            raise ValueError("exact rational operands must be base Fractions")
        # ``Fraction`` is not deeply immutable: ``object.__setattr__`` can
        # replace its slotted components. Read each caller-owned component
        # exactly once, reject injected subclasses/non-canonical state, and
        # rebuild a fresh base Fraction. All later reads are from that snapshot.
        try:
            numerator = object.__getattribute__(value, "_numerator")
            denominator = object.__getattribute__(value, "_denominator")
        except AttributeError as exc:
            raise ValueError("exact rational components must be present") from exc
        if type(numerator) is not int or type(denominator) is not int:
            raise ValueError("exact rational components must be base ints")
        if denominator <= 0:
            raise ValueError("exact rational denominator must be positive")
        if (
            _integer_bits(numerator) > self.max_component_bits
            or _integer_bits(denominator) > self.max_component_bits
        ):
            raise ValueError("exact rational component bit limit exceeded")
        if numerator == 0:
            if denominator != 1:
                raise ValueError("exact rational zero must have denominator one")
        elif gcd(abs(numerator), denominator) != 1:
            raise ValueError("exact rational components must be in lowest terms")
        return Fraction(numerator, denominator)

    def _charge(self, width: int, *, binary: bool) -> None:
        fuel = width * width * (4 if binary else 1)
        if fuel > self.max_admission_fuel - self.used_admission_fuel:
            raise ValueError("exact rational admission fuel exhausted")
        object.__setattr__(
            self,
            "used_admission_fuel",
            self.used_admission_fuel + fuel,
        )

    def _charge_binary(self, left: Fraction, right: Fraction) -> None:
        width = max(
            _integer_bits(left.numerator),
            _integer_bits(left.denominator),
            _integer_bits(right.numerator),
            _integer_bits(right.denominator),
        )
        self._charge(width, binary=True)

    def _bounded_product(self, left: int, right: int) -> int:
        if left == 0 or right == 0:
            return 0
        left_magnitude = abs(left)
        right_magnitude = abs(right)
        combined_bits = _integer_bits(left_magnitude) + _integer_bits(right_magnitude)
        if combined_bits > self.max_component_bits + 1:
            raise ValueError("exact rational component bit limit exceeded")
        if (
            combined_bits == self.max_component_bits + 1
            and left_magnitude > self._max_magnitude // right_magnitude
        ):
            raise ValueError("exact rational component bit limit exceeded")
        return left * right

    @staticmethod
    def _cross_gcd(left: int, right: int) -> int:
        # CPython's general GCD path may copy a giant operand even when the
        # other operand proves the answer is one. Keep that common sparse
        # rational case allocation-constant.
        if abs(left) == 1 or abs(right) == 1:
            return 1
        return gcd(left, right)

    @staticmethod
    def _exact_quotient(value: int, divisor: int) -> int:
        # Avoid a proportional base-int copy for the no-op quotient.
        return value if divisor == 1 else value // divisor

    def from_float(self, value: object, *, label: str) -> Fraction:
        numerator, denominator = _finite_float(value, label=label).as_integer_ratio()
        width = max(_integer_bits(numerator), _integer_bits(denominator))
        if width > self.max_component_bits:
            raise ValueError("exact rational component bit limit exceeded")
        self._charge(width, binary=False)
        return Fraction(numerator, denominator)

    def from_int(self, value: object) -> Fraction:
        snapshot = _strict_int(
            value,
            label="exact rational integer",
            max_bits=self.max_component_bits,
        )
        self._charge(_integer_bits(snapshot), binary=False)
        return Fraction(snapshot)

    def negate(self, value: Fraction) -> Fraction:
        snapshot = self._validate(value)
        width = max(
            _integer_bits(snapshot.numerator),
            _integer_bits(snapshot.denominator),
        )
        self._charge(width, binary=False)
        return self._negated(snapshot)

    @staticmethod
    def _negated(value: Fraction) -> Fraction:
        return Fraction(-value.numerator, value.denominator)

    def _add_or_subtract(
        self,
        left: Fraction,
        right: Fraction,
        *,
        subtract: bool,
    ) -> Fraction:
        left = self._validate(left)
        right = self._validate(right)
        self._charge_binary(left, right)
        if right.numerator == 0:
            return left
        if left.numerator == 0:
            return self._negated(right) if subtract else right

        denominator_gcd = gcd(left.denominator, right.denominator)
        left_scale = self._exact_quotient(right.denominator, denominator_gcd)
        right_scale = self._exact_quotient(left.denominator, denominator_gcd)
        denominator = self._bounded_product(left.denominator, left_scale)
        left_term = self._bounded_product(left.numerator, left_scale)
        right_numerator = -right.numerator if subtract else right.numerator
        right_term = self._bounded_product(right_numerator, right_scale)

        if (left_term < 0) == (right_term < 0):
            left_magnitude = abs(left_term)
            right_magnitude = abs(right_term)
            if left_magnitude > self._max_magnitude - right_magnitude:
                raise ValueError("exact rational component bit limit exceeded")
        numerator = left_term + right_term
        return self._validate(Fraction(numerator, denominator))

    def add(self, left: Fraction, right: Fraction) -> Fraction:
        return self._add_or_subtract(left, right, subtract=False)

    def subtract(self, left: Fraction, right: Fraction) -> Fraction:
        return self._add_or_subtract(left, right, subtract=True)

    def multiply(self, left: Fraction, right: Fraction) -> Fraction:
        left = self._validate(left)
        right = self._validate(right)
        self._charge_binary(left, right)
        if left.numerator == 0 or right.numerator == 0:
            return Fraction(0)
        if left.numerator == left.denominator:
            return right
        if right.numerator == right.denominator:
            return left
        if left.numerator < 0 and abs(left.numerator) == left.denominator:
            return self._negated(right)
        if right.numerator < 0 and abs(right.numerator) == right.denominator:
            return self._negated(left)

        left_gcd = self._cross_gcd(left.numerator, right.denominator)
        right_gcd = self._cross_gcd(right.numerator, left.denominator)
        left_numerator = self._exact_quotient(left.numerator, left_gcd)
        right_denominator = self._exact_quotient(right.denominator, left_gcd)
        right_numerator = self._exact_quotient(right.numerator, right_gcd)
        left_denominator = self._exact_quotient(left.denominator, right_gcd)
        numerator = self._bounded_product(left_numerator, right_numerator)
        denominator = self._bounded_product(left_denominator, right_denominator)
        return self._validate(Fraction(numerator, denominator))

    def divide(self, left: Fraction, right: Fraction) -> Fraction:
        left = self._validate(left)
        right = self._validate(right)
        if right.numerator == 0:
            raise ValueError("exact rational division requires a non-zero divisor")
        self._charge_binary(left, right)
        if left.numerator == 0:
            return Fraction(0)
        if right.numerator == right.denominator:
            return left
        if right.numerator < 0 and abs(right.numerator) == right.denominator:
            return self._negated(left)

        numerator_gcd = self._cross_gcd(left.numerator, right.numerator)
        denominator_gcd = gcd(left.denominator, right.denominator)
        left_numerator = self._exact_quotient(left.numerator, numerator_gcd)
        right_numerator = self._exact_quotient(right.numerator, numerator_gcd)
        right_denominator = self._exact_quotient(right.denominator, denominator_gcd)
        left_denominator = self._exact_quotient(left.denominator, denominator_gcd)
        numerator = self._bounded_product(left_numerator, right_denominator)
        denominator = self._bounded_product(left_denominator, abs(right_numerator))
        if right_numerator < 0:
            numerator = -numerator
        return self._validate(Fraction(numerator, denominator))

    def sum(self, values: Iterable[Fraction]) -> Fraction:
        result = Fraction(0)
        for value in values:
            result = self.add(result, value)
        return result


def _exact_float(
    value: object,
    *,
    label: str,
    arithmetic: _RationalArithmetic,
) -> Fraction:
    return arithmetic.from_float(value, label=label)


def _finite_result_score(score: Fraction) -> float:
    try:
        result = float(score)
    except (OverflowError, ValueError) as exc:
        raise ValueError("decoded score is outside the finite binary64 range") from exc
    if not math.isfinite(result):
        raise ValueError("decoded score is outside the finite binary64 range")
    return result


@dataclass(frozen=True, slots=True)
class TypedSpanCandidate:
    """One latent type interpretation of a source-backed span."""

    candidate_id: str
    source_identity: str
    source_start: int
    source_end: int
    block_id: str
    type_name: str
    granularity: str
    base_score: float
    type_logit: float = 0.0
    ancestor_block_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DecoderWeights:
    """Frozen, preregisterable scoring weights for the reference decoder."""

    coverage_reward_per_char: float = 0.0
    gap_penalty_per_char: float = 0.0
    fragmentation_penalty: float = 0.0
    contiguous_gap_chars: int = 0
    heading_to_body_bonus: float = 0.0
    type_compatibility: tuple[tuple[str, str, float], ...] = ()
    heading_types: tuple[str, ...] = ("heading", "title")
    body_types: tuple[str, ...] = (
        "body",
        "paragraph",
        "list",
        "table",
        "code",
    )

    def validated_compatibility(self) -> Mapping[tuple[str, str], float]:
        numeric_values = {
            "coverage reward": _finite_float(
                self.coverage_reward_per_char,
                label="coverage reward",
            ),
            "gap penalty": _finite_float(
                self.gap_penalty_per_char,
                label="gap penalty",
            ),
            "fragmentation penalty": _finite_float(
                self.fragmentation_penalty,
                label="fragmentation penalty",
            ),
            "heading-to-body bonus": _finite_float(
                self.heading_to_body_bonus,
                label="heading-to-body bonus",
            ),
        }
        for label in ("coverage reward", "gap penalty", "fragmentation penalty"):
            if numeric_values[label] < 0:
                raise ValueError(f"{label} must be non-negative")

        contiguous_gap = _strict_int(
            self.contiguous_gap_chars,
            label="contiguous gap",
            max_bits=_HARD_MAX_COORDINATE_BITS,
        )
        if contiguous_gap < 0:
            raise ValueError("contiguous gap must be non-negative")

        for label, names in (
            ("heading types", self.heading_types),
            ("body types", self.body_types),
        ):
            if not isinstance(names, tuple) or any(
                not isinstance(name, str) or not name for name in names
            ):
                raise ValueError(f"{label} must contain non-empty strings")
            if len(set(names)) != len(names):
                raise ValueError(f"{label} must be unique")

        if not isinstance(self.type_compatibility, tuple):
            raise ValueError("type compatibility must be a tuple")
        compatibility: dict[tuple[str, str], float] = {}
        for entry in self.type_compatibility:
            if not isinstance(entry, tuple) or len(entry) != 3:
                raise ValueError("type compatibility entries must be triples")
            previous_type, current_type, raw_value = entry
            if (
                not isinstance(previous_type, str)
                or not previous_type
                or not isinstance(current_type, str)
                or not current_type
            ):
                raise ValueError("type compatibility names must be non-empty strings")
            value = _finite_float(raw_value, label="type compatibility weight")
            key = (previous_type, current_type)
            if key in compatibility:
                raise ValueError(f"duplicate type compatibility entry: {key}")
            compatibility[key] = value
        return compatibility


def _snapshot_decoder_weights(
    weights: object,
    *,
    string_budget: _StringSnapshotBudget,
    max_weight_entries: int,
) -> DecoderWeights:
    if not isinstance(weights, DecoderWeights):
        raise ValueError("weights must be DecoderWeights")

    # Read every potentially overridden attribute exactly once, then discard
    # the caller-owned object before validation or scoring.
    raw_coverage_reward = weights.coverage_reward_per_char
    raw_gap_penalty = weights.gap_penalty_per_char
    raw_fragmentation_penalty = weights.fragmentation_penalty
    raw_contiguous_gap = weights.contiguous_gap_chars
    raw_heading_bonus = weights.heading_to_body_bonus
    raw_compatibility = weights.type_compatibility
    raw_heading_types = weights.heading_types
    raw_body_types = weights.body_types

    compatibility_entries = _snapshot_tuple(
        raw_compatibility,
        label="type compatibility",
        max_items=max_weight_entries,
        overflow_message="weight entry budget exceeded",
    )
    heading_type_entries = _snapshot_tuple(
        raw_heading_types,
        label="heading types",
        max_items=max_weight_entries,
        overflow_message="weight entry budget exceeded",
    )
    body_type_entries = _snapshot_tuple(
        raw_body_types,
        label="body types",
        max_items=max_weight_entries,
        overflow_message="weight entry budget exceeded",
    )
    entry_count = len(compatibility_entries) + len(heading_type_entries) + len(body_type_entries)
    if entry_count > max_weight_entries:
        raise ValueError("weight entry budget exceeded")

    heading_types = tuple(
        string_budget.snapshot(value, label="heading type") for value in heading_type_entries
    )
    body_types = tuple(
        string_budget.snapshot(value, label="body type") for value in body_type_entries
    )

    compatibility: list[tuple[str, str, float]] = []
    for raw_entry in compatibility_entries:
        entry = _snapshot_tuple(
            raw_entry,
            label="type compatibility entry",
            max_items=3,
            overflow_message="type compatibility entries must be triples",
        )
        if len(entry) != 3:
            raise ValueError("type compatibility entries must be triples")
        previous_type, current_type, raw_value = entry
        compatibility.append(
            (
                string_budget.snapshot(
                    previous_type,
                    label="previous compatibility type",
                ),
                string_budget.snapshot(
                    current_type,
                    label="current compatibility type",
                ),
                _finite_float(raw_value, label="type compatibility weight"),
            )
        )

    contiguous_gap = _strict_int(
        raw_contiguous_gap,
        label="contiguous gap",
        max_bits=_HARD_MAX_COORDINATE_BITS,
    )
    if (
        int.bit_length(contiguous_gap) > _HARD_MAX_COORDINATE_BITS
        or contiguous_gap > _HARD_MAX_DOCUMENT_CHARS
    ):
        raise ValueError("contiguous gap exceeds the compiled coordinate limit")

    return DecoderWeights(
        coverage_reward_per_char=_finite_float(
            raw_coverage_reward,
            label="coverage reward",
        ),
        gap_penalty_per_char=_finite_float(
            raw_gap_penalty,
            label="gap penalty",
        ),
        fragmentation_penalty=_finite_float(
            raw_fragmentation_penalty,
            label="fragmentation penalty",
        ),
        contiguous_gap_chars=contiguous_gap,
        heading_to_body_bonus=_finite_float(
            raw_heading_bonus,
            label="heading-to-body bonus",
        ),
        type_compatibility=tuple(compatibility),
        heading_types=heading_types,
        body_types=body_types,
    )


_DEFAULT_WEIGHTS = DecoderWeights()


@dataclass(frozen=True, slots=True)
class MarginalSpan:
    """One source span after its local latent type interpretations are marginalized."""

    source_identity: str
    source_start: int
    source_end: int
    block_id: str
    granularity: str
    ancestor_block_ids: tuple[str, ...]
    marginal_score: float
    best_type: str
    type_probabilities: tuple[tuple[str, float], ...]
    interpretation_ids: tuple[str, ...]
    _ancestor_block_id_set: frozenset[str] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_ancestor_block_id_set",
            frozenset(self.ancestor_block_ids),
        )

    @property
    def length(self) -> int:
        return self.source_end - self.source_start


@dataclass(frozen=True, slots=True)
class DecodedPath:
    """The deterministic top-1 path and its auditable score components."""

    spans: tuple[MarginalSpan, ...]
    score: float
    covered_characters: int
    fragments: int

    @property
    def source_identities(self) -> tuple[str, ...]:
        return tuple(span.source_identity for span in self.spans)


@dataclass(frozen=True, slots=True)
class _BlockMetadata:
    source_start: int
    source_end: int
    granularity: str
    ancestor_block_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class _ExactScoringContext:
    arithmetic: _RationalArithmetic
    coverage_reward_per_char: Fraction
    gap_penalty_per_char: Fraction
    fragmentation_penalty: Fraction
    contiguous_gap_chars: int
    heading_to_body_bonus: Fraction
    type_compatibility: dict[tuple[str, str], Fraction]
    heading_types: frozenset[str]
    body_types: frozenset[str]


@dataclass(frozen=True, slots=True)
class _ScoredSpan:
    node_score: Fraction
    type_probabilities: tuple[tuple[str, Fraction], ...]
    heading_probability: Fraction
    body_probability: Fraction


@dataclass(frozen=True, slots=True)
class _PathState:
    terminal_index: int | None = None
    predecessor_index: int | None = None
    score: Fraction = Fraction(0)
    covered_characters: int = 0
    span_count: int = 0


_EMPTY_STATE = _PathState()


class _ExactPathLexIndex:
    """Exact lexicographic index over the finalized backpointer tree."""

    __slots__ = ("_depths", "_jumps", "_spans")

    def __init__(self, spans: Sequence[MarginalSpan]) -> None:
        self._spans = spans
        # Node zero is the empty path. Finalized state index i is node i + 1.
        self._depths: list[int] = [0]
        self._jumps: list[tuple[int, ...]] = [(0,)]

    def append(self, state: _PathState, *, current_index: int) -> None:
        node = current_index + 1
        if len(self._depths) != node or state.terminal_index != current_index:
            raise RuntimeError("lexicographic index states must be appended in terminal order")

        predecessor_index = state.predecessor_index
        if predecessor_index is None:
            parent_node = 0
        else:
            if predecessor_index < 0 or predecessor_index >= current_index:
                raise RuntimeError(
                    "lexicographic index predecessor is outside the finalized prefix"
                )
            parent_node = predecessor_index + 1

        depth = self._depths[parent_node] + 1
        if state.span_count != depth:
            raise RuntimeError("lexicographic index depth disagrees with path cardinality")

        jumps = [parent_node]
        while 1 << len(jumps) <= depth:
            level = len(jumps)
            halfway = jumps[level - 1]
            jumps.append(self._jumps[halfway][level - 1])
        self._depths.append(depth)
        self._jumps.append(tuple(jumps))

    def _lift(self, node: int, distance: int) -> int:
        if node < 0 or node >= len(self._depths) or distance < 0 or distance > self._depths[node]:
            raise RuntimeError("invalid lexicographic ancestor lookup")
        level = 0
        while distance:
            if distance & 1:
                node = self._jumps[node][level]
            distance >>= 1
            level += 1
        return node

    def _lowest_common_ancestor(self, left: int, right: int) -> int:
        if left < 0 or left >= len(self._depths):
            raise RuntimeError("left lexicographic path is not finalized")
        if right < 0 or right >= len(self._depths):
            raise RuntimeError("right lexicographic path is not finalized")

        if self._depths[left] > self._depths[right]:
            left = self._lift(left, self._depths[left] - self._depths[right])
        elif self._depths[right] > self._depths[left]:
            right = self._lift(right, self._depths[right] - self._depths[left])
        if left == right:
            return left

        for level in range(self._depths[left].bit_length() - 1, -1, -1):
            left_ancestor = self._jumps[left][level] if level < len(self._jumps[left]) else 0
            right_ancestor = self._jumps[right][level] if level < len(self._jumps[right]) else 0
            if left_ancestor != right_ancestor:
                left = left_ancestor
                right = right_ancestor
        return self._jumps[left][0]

    def is_less(self, left: int, right: int) -> bool:
        """Return whether one finalized source-identity path is lexicographically less."""

        if left == right:
            return False
        common = self._lowest_common_ancestor(left, right)
        if common == left:
            return True
        if common == right:
            return False

        common_depth = self._depths[common]
        left_child = self._lift(left, self._depths[left] - common_depth - 1)
        right_child = self._lift(right, self._depths[right] - common_depth - 1)
        left_identity = self._spans[left_child - 1].source_identity
        right_identity = self._spans[right_child - 1].source_identity
        if left_identity == right_identity:
            raise RuntimeError("distinct lexicographic branches share a source identity")
        return left_identity < right_identity


def _stable_logsumexp(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("log-sum-exp requires at least one value")
    maximum = max(values)
    _finite_float(maximum, label="candidate combined score")

    exponentials: list[float] = []
    for value in values:
        delta = _checked_fsum(
            (value, -maximum),
            label="candidate score difference",
        )
        exponential = math.exp(delta)
        if not math.isfinite(exponential):
            raise ValueError("candidate posterior exponential must remain finite")
        exponentials.append(exponential)
    normalizer = _checked_fsum(
        exponentials,
        label="candidate posterior normalizer",
    )
    if normalizer <= 0:
        raise ValueError("candidate posterior normalizer must be positive")
    return _checked_fsum(
        (maximum, math.log(normalizer)),
        label="marginal candidate score",
    )


def _snapshot_candidate(
    candidate: object,
    *,
    max_document_chars: int,
    remaining_ancestor_references: int,
    string_budget: _StringSnapshotBudget,
) -> TypedSpanCandidate:
    if not isinstance(candidate, TypedSpanCandidate):
        raise ValueError("all candidates must be TypedSpanCandidate instances")

    # Read each caller-owned attribute exactly once. Every value used after this
    # point is an exact built-in snapshot stored in a fresh base dataclass.
    raw_candidate_id = candidate.candidate_id
    raw_source_identity = candidate.source_identity
    raw_source_start = candidate.source_start
    raw_source_end = candidate.source_end
    raw_block_id = candidate.block_id
    raw_type_name = candidate.type_name
    raw_granularity = candidate.granularity
    raw_base_score = candidate.base_score
    raw_type_logit = candidate.type_logit
    raw_ancestor_block_ids = candidate.ancestor_block_ids

    source_start = _strict_int(
        raw_source_start,
        label="candidate source coordinate",
        max_bits=_HARD_MAX_COORDINATE_BITS,
    )
    source_end = _strict_int(
        raw_source_end,
        label="candidate source coordinate",
        max_bits=_HARD_MAX_COORDINATE_BITS,
    )
    if source_start < 0 or source_end <= source_start:
        raise ValueError("candidate source span must be positive and non-empty")
    if source_end > max_document_chars:
        raise ValueError("candidate source span exceeds the document budget")

    raw_ancestors = _snapshot_tuple(
        raw_ancestor_block_ids,
        label="ancestor identities",
        max_items=remaining_ancestor_references,
        overflow_message="ancestor reference budget exceeded",
    )
    ancestor_block_ids = tuple(
        string_budget.snapshot(ancestor, label="ancestor identity") for ancestor in raw_ancestors
    )

    candidate_id = string_budget.snapshot(raw_candidate_id, label="candidate id")
    source_identity = string_budget.snapshot(
        raw_source_identity,
        label="source identity",
    )
    block_id = string_budget.snapshot(raw_block_id, label="block id")
    type_name = string_budget.snapshot(raw_type_name, label="type name")
    granularity = string_budget.snapshot(raw_granularity, label="granularity")
    if block_id in ancestor_block_ids:
        raise ValueError("a block cannot be its own ancestor")
    if len(set(ancestor_block_ids)) != len(ancestor_block_ids):
        raise ValueError("ancestor identities must be unique")

    return TypedSpanCandidate(
        candidate_id=candidate_id,
        source_identity=source_identity,
        source_start=source_start,
        source_end=source_end,
        block_id=block_id,
        type_name=type_name,
        granularity=granularity,
        base_score=_finite_float(raw_base_score, label="candidate base score"),
        type_logit=_finite_float(raw_type_logit, label="candidate type logit"),
        ancestor_block_ids=ancestor_block_ids,
    )


def marginalize_candidates(
    candidates: Iterable[TypedSpanCandidate],
    *,
    max_candidates: int = _HARD_MAX_CANDIDATES,
    max_document_chars: int = _HARD_MAX_DOCUMENT_CHARS,
    max_ancestor_references: int = _HARD_MAX_ANCESTOR_REFERENCES,
    max_string_codepoints: int = _HARD_MAX_STRING_CODEPOINTS,
) -> tuple[MarginalSpan, ...]:
    """Validate and locally marginalize latent types by source identity."""

    budgets = _validated_catalog_budgets(
        max_candidates=max_candidates,
        candidate_label="candidate budget",
        candidate_hard_limit=_HARD_MAX_CANDIDATES,
        max_document_chars=max_document_chars,
        max_ancestor_references=max_ancestor_references,
        max_string_codepoints=max_string_codepoints,
    )
    return _marginalize_candidates(
        candidates,
        budgets=budgets,
        string_budget=_StringSnapshotBudget(budgets.string_codepoints),
    )


def _marginalize_candidates(
    candidates: Iterable[TypedSpanCandidate],
    *,
    budgets: _CatalogBudgets,
    string_budget: _StringSnapshotBudget,
) -> tuple[MarginalSpan, ...]:
    """Marginalize after all scalar budgets have been frozen."""

    # Consume at most one item beyond the declared cap. Converting an arbitrary
    # iterable directly to a tuple would let an infinite or hostile producer
    # bypass the resource contract before the length check could run.
    materialized = tuple(islice(iter(candidates), budgets.candidates + 1))
    if len(materialized) > budgets.candidates:
        raise ValueError("candidate budget exceeded")

    seen_candidate_ids: set[str] = set()
    seen_identity_types: set[tuple[str, str]] = set()
    grouped: dict[str, list[TypedSpanCandidate]] = {}
    ancestor_reference_count = 0
    for raw_candidate in materialized:
        candidate = _snapshot_candidate(
            raw_candidate,
            max_document_chars=budgets.document_chars,
            remaining_ancestor_references=(budgets.ancestor_references - ancestor_reference_count),
            string_budget=string_budget,
        )
        ancestor_reference_count += len(candidate.ancestor_block_ids)
        if candidate.candidate_id in seen_candidate_ids:
            raise ValueError(f"duplicate candidate id: {candidate.candidate_id}")
        seen_candidate_ids.add(candidate.candidate_id)
        identity_type = (candidate.source_identity, candidate.type_name)
        if identity_type in seen_identity_types:
            raise ValueError(f"duplicate latent type interpretation: {identity_type}")
        seen_identity_types.add(identity_type)
        grouped.setdefault(candidate.source_identity, []).append(candidate)

    marginalized: list[MarginalSpan] = []
    for source_identity, interpretations in grouped.items():
        first = interpretations[0]
        structural_signature = (
            first.source_start,
            first.source_end,
            first.block_id,
            first.granularity,
            tuple(sorted(first.ancestor_block_ids)),
        )
        for interpretation in interpretations[1:]:
            candidate_signature = (
                interpretation.source_start,
                interpretation.source_end,
                interpretation.block_id,
                interpretation.granularity,
                tuple(sorted(interpretation.ancestor_block_ids)),
            )
            if candidate_signature != structural_signature:
                raise ValueError(
                    f"source identity {source_identity!r} has inconsistent structural metadata"
                )

        ordered = sorted(
            interpretations,
            key=lambda item: (item.type_name, item.candidate_id),
        )
        type_scores = tuple(
            _checked_fsum(
                (
                    _finite_float(item.base_score, label="candidate base score"),
                    _finite_float(item.type_logit, label="candidate type logit"),
                ),
                label="candidate combined score",
            )
            for item in ordered
        )
        marginal_score = _stable_logsumexp(type_scores)

        maximum = max(type_scores)
        unnormalized = tuple(
            math.exp(
                _checked_fsum(
                    (score, -maximum),
                    label="candidate score difference",
                )
            )
            for score in type_scores
        )
        normalizer = _checked_fsum(
            unnormalized,
            label="candidate posterior normalizer",
        )
        probabilities = tuple(
            (item.type_name, value / normalizer)
            for item, value in zip(ordered, unnormalized, strict=True)
        )
        if any(
            not math.isfinite(probability) or probability < 0 for _, probability in probabilities
        ):
            raise ValueError("candidate posterior probabilities must be finite")

        best_index = min(
            range(len(ordered)),
            key=lambda index: (
                -type_scores[index],
                ordered[index].type_name,
                ordered[index].candidate_id,
            ),
        )
        marginalized.append(
            MarginalSpan(
                source_identity=source_identity,
                source_start=first.source_start,
                source_end=first.source_end,
                block_id=first.block_id,
                granularity=first.granularity,
                ancestor_block_ids=tuple(sorted(first.ancestor_block_ids)),
                marginal_score=marginal_score,
                best_type=ordered[best_index].type_name,
                type_probabilities=probabilities,
                interpretation_ids=tuple(item.candidate_id for item in ordered),
            )
        )

    spans = tuple(
        sorted(
            marginalized,
            key=lambda item: (
                item.source_end,
                item.source_start,
                item.source_identity,
            ),
        )
    )
    _validate_canonical_catalog(spans)
    return spans


def _validate_canonical_catalog(spans: Sequence[MarginalSpan]) -> None:
    block_metadata: dict[str, _BlockMetadata] = {}
    for span in spans:
        metadata = _BlockMetadata(
            source_start=span.source_start,
            source_end=span.source_end,
            granularity=span.granularity,
            ancestor_block_ids=span._ancestor_block_id_set,
        )
        previous = block_metadata.get(span.block_id)
        if previous is None:
            block_metadata[span.block_id] = metadata
            continue
        if (
            previous.source_start,
            previous.source_end,
        ) != (
            metadata.source_start,
            metadata.source_end,
        ):
            raise ValueError(f"block identity {span.block_id!r} maps to multiple source spans")
        if (
            previous.granularity != metadata.granularity
            or previous.ancestor_block_ids != metadata.ancestor_block_ids
        ):
            raise ValueError(
                f"block identity {span.block_id!r} has inconsistent canonical metadata"
            )

    for block_id, descendant in block_metadata.items():
        for ancestor_id in descendant.ancestor_block_ids:
            ancestor = block_metadata.get(ancestor_id)
            if ancestor is None:
                raise ValueError(
                    f"ancestor {ancestor_id!r} referenced by block {block_id!r} is not represented"
                )
            contains_descendant = (
                ancestor.source_start <= descendant.source_start
                and ancestor.source_end >= descendant.source_end
            )
            if not contains_descendant:
                raise ValueError(f"ancestor {ancestor_id!r} does not contain block {block_id!r}")

    ancestor_graph = {
        block_id: metadata.ancestor_block_ids for block_id, metadata in block_metadata.items()
    }
    incoming = {block_id: 0 for block_id in ancestor_graph}
    for ancestor_ids in ancestor_graph.values():
        for ancestor_id in ancestor_ids:
            incoming[ancestor_id] += 1

    ready = deque(block_id for block_id, degree in incoming.items() if degree == 0)
    visited = 0
    while ready:
        block_id = ready.popleft()
        visited += 1
        for ancestor_id in ancestor_graph[block_id]:
            incoming[ancestor_id] -= 1
            if incoming[ancestor_id] == 0:
                ready.append(ancestor_id)
    if visited != len(ancestor_graph):
        raise ValueError("represented ancestor graph contains a cycle")


def _build_scoring_context(weights: DecoderWeights) -> _ExactScoringContext:
    compatibility = DecoderWeights.validated_compatibility(weights)
    arithmetic = _RationalArithmetic()
    return _ExactScoringContext(
        arithmetic=arithmetic,
        coverage_reward_per_char=_exact_float(
            weights.coverage_reward_per_char,
            label="coverage reward",
            arithmetic=arithmetic,
        ),
        gap_penalty_per_char=_exact_float(
            weights.gap_penalty_per_char,
            label="gap penalty",
            arithmetic=arithmetic,
        ),
        fragmentation_penalty=_exact_float(
            weights.fragmentation_penalty,
            label="fragmentation penalty",
            arithmetic=arithmetic,
        ),
        contiguous_gap_chars=_strict_int(
            weights.contiguous_gap_chars,
            label="contiguous gap",
            max_bits=_HARD_MAX_COORDINATE_BITS,
        ),
        heading_to_body_bonus=_exact_float(
            weights.heading_to_body_bonus,
            label="heading-to-body bonus",
            arithmetic=arithmetic,
        ),
        type_compatibility={
            key: _exact_float(
                value,
                label="type compatibility weight",
                arithmetic=arithmetic,
            )
            for key, value in compatibility.items()
        },
        heading_types=frozenset(weights.heading_types),
        body_types=frozenset(weights.body_types),
    )


def _exact_type_probabilities(
    span: MarginalSpan,
    arithmetic: _RationalArithmetic,
) -> tuple[tuple[str, Fraction], ...]:
    lifted = tuple(
        (
            type_name,
            _exact_float(
                probability,
                label="candidate posterior probability",
                arithmetic=arithmetic,
            ),
        )
        for type_name, probability in span.type_probabilities
    )
    normalizer = arithmetic.sum(probability for _, probability in lifted)
    if normalizer <= 0:
        raise ValueError("candidate posterior probability mass must be positive")
    return tuple(
        (type_name, arithmetic.divide(probability, normalizer)) for type_name, probability in lifted
    )


def _prepare_scored_spans(
    spans: Sequence[MarginalSpan],
    context: _ExactScoringContext,
) -> tuple[_ScoredSpan, ...]:
    scored: list[_ScoredSpan] = []
    for span in spans:
        probabilities = _exact_type_probabilities(span, context.arithmetic)
        heading_probability = context.arithmetic.sum(
            (
                probability
                for type_name, probability in probabilities
                if type_name in context.heading_types
            )
        )
        body_probability = context.arithmetic.sum(
            (
                probability
                for type_name, probability in probabilities
                if type_name in context.body_types
            )
        )
        coverage_score = context.arithmetic.multiply(
            context.coverage_reward_per_char,
            context.arithmetic.from_int(span.length),
        )
        scored.append(
            _ScoredSpan(
                node_score=context.arithmetic.add(
                    _exact_float(
                        span.marginal_score,
                        label="marginal candidate score",
                        arithmetic=context.arithmetic,
                    ),
                    coverage_score,
                ),
                type_probabilities=probabilities,
                heading_probability=heading_probability,
                body_probability=body_probability,
            )
        )
    return tuple(scored)


def _transition_score(
    previous: MarginalSpan,
    current: MarginalSpan,
    previous_scored: _ScoredSpan,
    current_scored: _ScoredSpan,
    context: _ExactScoringContext,
) -> Fraction:
    gap = current.source_start - previous.source_end
    score = context.arithmetic.negate(
        context.arithmetic.multiply(
            context.gap_penalty_per_char,
            context.arithmetic.from_int(gap),
        )
    )
    if gap > context.contiguous_gap_chars:
        score = context.arithmetic.subtract(score, context.fragmentation_penalty)

    if context.type_compatibility:
        for previous_type, previous_probability in previous_scored.type_probabilities:
            for current_type, current_probability in current_scored.type_probabilities:
                probability_product = context.arithmetic.multiply(
                    previous_probability,
                    current_probability,
                )
                weighted_product = context.arithmetic.multiply(
                    probability_product,
                    context.type_compatibility.get(
                        (previous_type, current_type),
                        Fraction(0),
                    ),
                )
                score = context.arithmetic.add(score, weighted_product)

    if context.heading_to_body_bonus:
        heading_body_mass = context.arithmetic.multiply(
            previous_scored.heading_probability,
            current_scored.body_probability,
        )
        score = context.arithmetic.add(
            score,
            context.arithmetic.multiply(
                context.heading_to_body_bonus,
                heading_body_mass,
            ),
        )
    return score


def _spans_are_compatible(previous: MarginalSpan, current: MarginalSpan) -> bool:
    if previous.source_end > current.source_start:
        return False
    if previous.source_identity == current.source_identity:
        return False
    if previous.block_id == current.block_id:
        return False
    if previous.block_id in current._ancestor_block_id_set:
        return False
    return current.block_id not in previous._ancestor_block_id_set


def _covered_characters(spans: Sequence[MarginalSpan]) -> int:
    return sum(span.length for span in spans)


def _fragment_count(spans: Sequence[MarginalSpan], contiguous_gap_chars: int) -> int:
    if not spans:
        return 0
    return 1 + sum(
        current.source_start - previous.source_end > contiguous_gap_chars
        for previous, current in zip(spans, spans[1:], strict=False)
    )


def _state_span_indices(
    state: _PathState,
    states: Sequence[_PathState],
) -> tuple[int, ...]:
    if state.terminal_index is None:
        return ()

    indices = [state.terminal_index]
    predecessor_index = state.predecessor_index
    while predecessor_index is not None:
        if predecessor_index < 0 or predecessor_index >= len(states):
            raise RuntimeError("decoder backpointer is outside the completed state table")
        predecessor = states[predecessor_index]
        if predecessor.terminal_index != predecessor_index:
            raise RuntimeError("decoder backpointer does not reference its terminal state")
        indices.append(predecessor_index)
        predecessor_index = predecessor.predecessor_index
    indices.reverse()
    return tuple(indices)


def _is_better(
    candidate: _PathState,
    incumbent: _PathState,
    *,
    lex_index: _ExactPathLexIndex,
    common_terminal: bool,
) -> bool:
    if candidate.score != incumbent.score:
        return candidate.score > incumbent.score
    if candidate.covered_characters != incumbent.covered_characters:
        return candidate.covered_characters > incumbent.covered_characters
    if candidate.span_count != incumbent.span_count:
        return candidate.span_count < incumbent.span_count

    if common_terminal:
        if candidate.terminal_index != incumbent.terminal_index:
            raise RuntimeError("common-terminal tie comparison received different terminals")
        candidate_node = (
            0 if candidate.predecessor_index is None else candidate.predecessor_index + 1
        )
        incumbent_node = (
            0 if incumbent.predecessor_index is None else incumbent.predecessor_index + 1
        )
    else:
        candidate_node = 0 if candidate.terminal_index is None else candidate.terminal_index + 1
        incumbent_node = 0 if incumbent.terminal_index is None else incumbent.terminal_index + 1
    return lex_index.is_less(candidate_node, incumbent_node)


def _to_result(
    state: _PathState,
    *,
    spans: Sequence[MarginalSpan],
    states: Sequence[_PathState],
    weights: DecoderWeights,
) -> DecodedPath:
    selected = tuple(spans[index] for index in _state_span_indices(state, states))
    return DecodedPath(
        spans=selected,
        score=_finite_result_score(state.score),
        covered_characters=state.covered_characters,
        fragments=_fragment_count(selected, weights.contiguous_gap_chars),
    )


def decode(
    candidates: Iterable[TypedSpanCandidate],
    *,
    weights: DecoderWeights = _DEFAULT_WEIGHTS,
    max_candidates: int = _HARD_MAX_CANDIDATES,
    max_document_chars: int = _HARD_MAX_DOCUMENT_CHARS,
    max_ancestor_references: int = _HARD_MAX_ANCESTOR_REFERENCES,
    max_string_codepoints: int = _HARD_MAX_STRING_CODEPOINTS,
    max_weight_entries: int = _HARD_MAX_WEIGHT_ENTRIES,
) -> DecodedPath:
    """Return the exact top-1 path for the frozen local-posterior objective."""

    budgets = _validated_catalog_budgets(
        max_candidates=max_candidates,
        candidate_label="candidate budget",
        candidate_hard_limit=_HARD_MAX_CANDIDATES,
        max_document_chars=max_document_chars,
        max_ancestor_references=max_ancestor_references,
        max_string_codepoints=max_string_codepoints,
    )
    weight_entry_budget = _bounded_budget(
        max_weight_entries,
        label="weight entry budget",
        hard_limit=_HARD_MAX_WEIGHT_ENTRIES,
    )
    string_budget = _StringSnapshotBudget(budgets.string_codepoints)
    frozen_weights = _snapshot_decoder_weights(
        weights,
        string_budget=string_budget,
        max_weight_entries=weight_entry_budget,
    )
    context = _build_scoring_context(frozen_weights)
    spans = _marginalize_candidates(
        candidates,
        budgets=budgets,
        string_budget=string_budget,
    )
    scored_spans = _prepare_scored_spans(spans, context)
    best_ending_at: list[_PathState] = []
    lex_index = _ExactPathLexIndex(spans)
    best_overall = _EMPTY_STATE

    for current_index, (current, current_scored) in enumerate(
        zip(spans, scored_spans, strict=True)
    ):
        best_current = _PathState(
            terminal_index=current_index,
            score=current_scored.node_score,
            covered_characters=current.length,
            span_count=1,
        )
        for previous_index in range(current_index):
            previous = spans[previous_index]
            if not _spans_are_compatible(previous, current):
                continue
            predecessor = best_ending_at[previous_index]
            transition_score = _transition_score(
                previous,
                current,
                scored_spans[previous_index],
                current_scored,
                context,
            )
            proposed = _PathState(
                terminal_index=current_index,
                predecessor_index=previous_index,
                score=context.arithmetic.add(
                    context.arithmetic.add(
                        predecessor.score,
                        current_scored.node_score,
                    ),
                    transition_score,
                ),
                covered_characters=predecessor.covered_characters + current.length,
                span_count=predecessor.span_count + 1,
            )
            if _is_better(
                proposed,
                best_current,
                lex_index=lex_index,
                common_terminal=True,
            ):
                best_current = proposed
        best_ending_at.append(best_current)
        lex_index.append(best_current, current_index=current_index)
        if _is_better(
            best_current,
            best_overall,
            lex_index=lex_index,
            common_terminal=False,
        ):
            best_overall = best_current

    return _to_result(
        best_overall,
        spans=spans,
        states=best_ending_at,
        weights=frozen_weights,
    )


def _oracle_type_probabilities(
    span: MarginalSpan,
    arithmetic: _RationalArithmetic,
) -> tuple[tuple[str, Fraction], ...]:
    probabilities = tuple(
        (
            type_name,
            arithmetic.from_float(
                probability,
                label="oracle candidate posterior probability",
            ),
        )
        for type_name, probability in span.type_probabilities
    )
    mass = arithmetic.sum(probability for _, probability in probabilities)
    if mass <= 0:
        raise ValueError("oracle candidate posterior mass must be positive")
    return tuple(
        (type_name, arithmetic.divide(probability, mass))
        for type_name, probability in probabilities
    )


def _oracle_node_score(
    span: MarginalSpan,
    context: _ExactScoringContext,
) -> Fraction:
    marginal_score = context.arithmetic.from_float(
        span.marginal_score,
        label="oracle marginal candidate score",
    )
    coverage_score = context.arithmetic.multiply(
        context.coverage_reward_per_char,
        context.arithmetic.from_int(span.length),
    )
    return context.arithmetic.add(marginal_score, coverage_score)


def _oracle_transition_score(
    previous: MarginalSpan,
    current: MarginalSpan,
    context: _ExactScoringContext,
) -> Fraction:
    gap = current.source_start - previous.source_end
    result = context.arithmetic.negate(
        context.arithmetic.multiply(
            context.gap_penalty_per_char,
            context.arithmetic.from_int(gap),
        )
    )
    if gap > context.contiguous_gap_chars:
        result = context.arithmetic.subtract(result, context.fragmentation_penalty)

    if context.type_compatibility or context.heading_to_body_bonus:
        previous_probabilities = _oracle_type_probabilities(previous, context.arithmetic)
        current_probabilities = _oracle_type_probabilities(current, context.arithmetic)

        if context.type_compatibility:
            for previous_type, previous_probability in previous_probabilities:
                for current_type, current_probability in current_probabilities:
                    pair_weight = context.type_compatibility.get(
                        (previous_type, current_type),
                        Fraction(0),
                    )
                    pair_probability = context.arithmetic.multiply(
                        previous_probability,
                        current_probability,
                    )
                    result = context.arithmetic.add(
                        result,
                        context.arithmetic.multiply(pair_probability, pair_weight),
                    )

        if context.heading_to_body_bonus:
            previous_heading_mass = context.arithmetic.sum(
                (
                    probability
                    for type_name, probability in previous_probabilities
                    if type_name in context.heading_types
                )
            )
            current_body_mass = context.arithmetic.sum(
                (
                    probability
                    for type_name, probability in current_probabilities
                    if type_name in context.body_types
                )
            )
            heading_body_mass = context.arithmetic.multiply(
                previous_heading_mass,
                current_body_mass,
            )
            result = context.arithmetic.add(
                result,
                context.arithmetic.multiply(
                    context.heading_to_body_bonus,
                    heading_body_mass,
                ),
            )
    return result


def _oracle_path_is_compatible(spans: Sequence[MarginalSpan]) -> bool:
    for previous_index, previous in enumerate(spans):
        for current in spans[previous_index + 1 :]:
            if previous.source_end > current.source_start:
                return False
            if previous.source_identity == current.source_identity:
                return False
            if previous.block_id == current.block_id:
                return False
            if previous.block_id in current._ancestor_block_id_set:
                return False
            if current.block_id in previous._ancestor_block_id_set:
                return False
    return True


def _oracle_is_better(
    *,
    score: Fraction,
    spans: Sequence[MarginalSpan],
    incumbent_score: Fraction,
    incumbent_spans: Sequence[MarginalSpan],
) -> bool:
    if score != incumbent_score:
        return score > incumbent_score
    coverage = sum(span.length for span in spans)
    incumbent_coverage = sum(span.length for span in incumbent_spans)
    if coverage != incumbent_coverage:
        return coverage > incumbent_coverage
    if len(spans) != len(incumbent_spans):
        return len(spans) < len(incumbent_spans)
    return tuple(span.source_identity for span in spans) < tuple(
        span.source_identity for span in incumbent_spans
    )


def brute_force_decode(
    candidates: Iterable[TypedSpanCandidate],
    *,
    weights: DecoderWeights = _DEFAULT_WEIGHTS,
    max_spans: int = _HARD_MAX_BRUTE_FORCE_SPANS,
    max_candidates: int = _HARD_MAX_BRUTE_FORCE_CANDIDATES,
    max_document_chars: int = _HARD_MAX_DOCUMENT_CHARS,
    max_ancestor_references: int = _HARD_MAX_ANCESTOR_REFERENCES,
    max_string_codepoints: int = _HARD_MAX_STRING_CODEPOINTS,
    max_weight_entries: int = _HARD_MAX_WEIGHT_ENTRIES,
) -> DecodedPath:
    """Return an independent exhaustive oracle for small canonical lattices."""

    span_budget = _bounded_budget(
        max_spans,
        label="brute-force span budget",
        hard_limit=_HARD_MAX_BRUTE_FORCE_SPANS,
    )
    budgets = _validated_catalog_budgets(
        max_candidates=max_candidates,
        candidate_label="brute-force candidate budget",
        candidate_hard_limit=_HARD_MAX_BRUTE_FORCE_CANDIDATES,
        max_document_chars=max_document_chars,
        max_ancestor_references=max_ancestor_references,
        max_string_codepoints=max_string_codepoints,
    )
    weight_entry_budget = _bounded_budget(
        max_weight_entries,
        label="weight entry budget",
        hard_limit=_HARD_MAX_WEIGHT_ENTRIES,
    )
    string_budget = _StringSnapshotBudget(budgets.string_codepoints)
    frozen_weights = _snapshot_decoder_weights(
        weights,
        string_budget=string_budget,
        max_weight_entries=weight_entry_budget,
    )
    context = _build_scoring_context(frozen_weights)
    spans = _marginalize_candidates(
        candidates,
        budgets=budgets,
        string_budget=string_budget,
    )
    if len(spans) > span_budget:
        raise ValueError("brute-force span budget exceeded")

    # Keep the exhaustive search independent from the DP while avoiding
    # repeated latent-type arithmetic for the same ordered pair in every
    # subset. The hard span ceiling makes this exact O(n²) oracle cache small.
    oracle_node_scores = tuple(_oracle_node_score(span, context) for span in spans)
    oracle_transition_scores = {
        (previous_index, current_index): _oracle_transition_score(
            spans[previous_index],
            spans[current_index],
            context,
        )
        for current_index in range(len(spans))
        for previous_index in range(current_index)
    }

    best_spans: tuple[MarginalSpan, ...] = ()
    best_score = Fraction(0)
    for mask in range(1, 1 << len(spans)):
        selected_indices = tuple(index for index in range(len(spans)) if mask & (1 << index))
        selected = tuple(spans[index] for index in selected_indices)
        if not _oracle_path_is_compatible(selected):
            continue

        score = context.arithmetic.sum(oracle_node_scores[index] for index in selected_indices)
        score = context.arithmetic.add(
            score,
            context.arithmetic.sum(
                (
                    oracle_transition_scores[(previous_index, current_index)]
                    for previous_index, current_index in zip(
                        selected_indices,
                        selected_indices[1:],
                        strict=False,
                    )
                )
            ),
        )
        if _oracle_is_better(
            score=score,
            spans=selected,
            incumbent_score=best_score,
            incumbent_spans=best_spans,
        ):
            best_spans = selected
            best_score = score

    fragments = 0
    if best_spans:
        fragments = 1 + sum(
            current.source_start - previous.source_end > context.contiguous_gap_chars
            for previous, current in zip(best_spans, best_spans[1:], strict=False)
        )
    return DecodedPath(
        spans=best_spans,
        score=_finite_result_score(best_score),
        covered_characters=sum(span.length for span in best_spans),
        fragments=fragments,
    )


def _score_index_path_exact(
    indices: Sequence[int],
    spans: Sequence[MarginalSpan],
    scored: Sequence[_ScoredSpan],
    context: _ExactScoringContext,
) -> Fraction:
    score = context.arithmetic.sum(scored[index].node_score for index in indices)
    score = context.arithmetic.add(
        score,
        context.arithmetic.sum(
            (
                _transition_score(
                    spans[previous_index],
                    spans[current_index],
                    scored[previous_index],
                    scored[current_index],
                    context,
                )
                for previous_index, current_index in zip(
                    indices,
                    indices[1:],
                    strict=False,
                )
            )
        ),
    )
    return score


def _greedy_decode(
    candidates: Iterable[TypedSpanCandidate],
    *,
    weights: DecoderWeights,
    max_candidates: int,
    max_document_chars: int,
    max_ancestor_references: int,
    max_string_codepoints: int,
    max_weight_entries: int,
    score_first: bool,
) -> DecodedPath:
    budgets = _validated_catalog_budgets(
        max_candidates=max_candidates,
        candidate_label="candidate budget",
        candidate_hard_limit=_HARD_MAX_CANDIDATES,
        max_document_chars=max_document_chars,
        max_ancestor_references=max_ancestor_references,
        max_string_codepoints=max_string_codepoints,
    )
    weight_entry_budget = _bounded_budget(
        max_weight_entries,
        label="weight entry budget",
        hard_limit=_HARD_MAX_WEIGHT_ENTRIES,
    )
    string_budget = _StringSnapshotBudget(budgets.string_codepoints)
    frozen_weights = _snapshot_decoder_weights(
        weights,
        string_budget=string_budget,
        max_weight_entries=weight_entry_budget,
    )
    context = _build_scoring_context(frozen_weights)
    spans = _marginalize_candidates(
        candidates,
        budgets=budgets,
        string_budget=string_budget,
    )
    scored = _prepare_scored_spans(spans, context)
    if score_first:
        # Python's sort is stable: establish the canonical source-order
        # tiebreak first, then rank admitted node scores descending.  Avoid
        # synthesizing unmetered negative Fraction keys solely for sorting.
        ranked_indices = sorted(
            range(len(spans)),
            key=lambda index: (
                spans[index].source_start,
                spans[index].source_end,
                spans[index].source_identity,
            ),
        )
        ranked_indices.sort(
            key=lambda index: scored[index].node_score,
            reverse=True,
        )
    else:
        ranked_indices = sorted(
            range(len(spans)),
            key=lambda index: (
                spans[index].source_start,
                spans[index].source_end,
                spans[index].source_identity,
            ),
        )

    # The baseline is a monotone local-improvement procedure over the same
    # frozen objective as the exact decoder. Its incumbent is the empty path;
    # feasibility alone never forces a negative-score or tie-inferior span into
    # the result.
    selected_indices: tuple[int, ...] = ()
    selected_score = Fraction(0)
    for index in ranked_indices:
        proposed_span = spans[index]
        if all(
            _spans_are_compatible(spans[previous_index], proposed_span)
            if spans[previous_index].source_start <= proposed_span.source_start
            else _spans_are_compatible(proposed_span, spans[previous_index])
            for previous_index in selected_indices
        ):
            proposed_indices = tuple(
                sorted(
                    (*selected_indices, index),
                    key=lambda span_index: (
                        spans[span_index].source_start,
                        spans[span_index].source_end,
                        spans[span_index].source_identity,
                    ),
                )
            )
            proposed_score = _score_index_path_exact(
                proposed_indices,
                spans,
                scored,
                context,
            )
            proposed_spans = tuple(spans[span_index] for span_index in proposed_indices)
            selected_spans = tuple(spans[span_index] for span_index in selected_indices)
            if _oracle_is_better(
                score=proposed_score,
                spans=proposed_spans,
                incumbent_score=selected_score,
                incumbent_spans=selected_spans,
            ):
                selected_indices = proposed_indices
                selected_score = proposed_score

    selected = tuple(spans[index] for index in selected_indices)
    return DecodedPath(
        spans=selected,
        score=_finite_result_score(selected_score),
        covered_characters=_covered_characters(selected),
        fragments=_fragment_count(selected, context.contiguous_gap_chars),
    )


def score_first_greedy_decode(
    candidates: Iterable[TypedSpanCandidate],
    *,
    weights: DecoderWeights = _DEFAULT_WEIGHTS,
    max_candidates: int = _HARD_MAX_CANDIDATES,
    max_document_chars: int = _HARD_MAX_DOCUMENT_CHARS,
    max_ancestor_references: int = _HARD_MAX_ANCESTOR_REFERENCES,
    max_string_codepoints: int = _HARD_MAX_STRING_CODEPOINTS,
    max_weight_entries: int = _HARD_MAX_WEIGHT_ENTRIES,
) -> DecodedPath:
    """Return the deterministic score-first local-improvement ablation."""

    return _greedy_decode(
        candidates,
        weights=weights,
        max_candidates=max_candidates,
        max_document_chars=max_document_chars,
        max_ancestor_references=max_ancestor_references,
        max_string_codepoints=max_string_codepoints,
        max_weight_entries=max_weight_entries,
        score_first=True,
    )


def source_order_greedy_decode(
    candidates: Iterable[TypedSpanCandidate],
    *,
    weights: DecoderWeights = _DEFAULT_WEIGHTS,
    max_candidates: int = _HARD_MAX_CANDIDATES,
    max_document_chars: int = _HARD_MAX_DOCUMENT_CHARS,
    max_ancestor_references: int = _HARD_MAX_ANCESTOR_REFERENCES,
    max_string_codepoints: int = _HARD_MAX_STRING_CODEPOINTS,
    max_weight_entries: int = _HARD_MAX_WEIGHT_ENTRIES,
) -> DecodedPath:
    """Return the deterministic source-order local-improvement ablation."""

    return _greedy_decode(
        candidates,
        weights=weights,
        max_candidates=max_candidates,
        max_document_chars=max_document_chars,
        max_ancestor_references=max_ancestor_references,
        max_string_codepoints=max_string_codepoints,
        max_weight_entries=max_weight_entries,
        score_first=False,
    )


def greedy_decode(
    candidates: Iterable[TypedSpanCandidate],
    *,
    weights: DecoderWeights = _DEFAULT_WEIGHTS,
    max_candidates: int = _HARD_MAX_CANDIDATES,
    max_document_chars: int = _HARD_MAX_DOCUMENT_CHARS,
    max_ancestor_references: int = _HARD_MAX_ANCESTOR_REFERENCES,
    max_string_codepoints: int = _HARD_MAX_STRING_CODEPOINTS,
    max_weight_entries: int = _HARD_MAX_WEIGHT_ENTRIES,
) -> DecodedPath:
    """Compatibility name for :func:`score_first_greedy_decode`."""

    return score_first_greedy_decode(
        candidates,
        weights=weights,
        max_candidates=max_candidates,
        max_document_chars=max_document_chars,
        max_ancestor_references=max_ancestor_references,
        max_string_codepoints=max_string_codepoints,
        max_weight_entries=max_weight_entries,
    )

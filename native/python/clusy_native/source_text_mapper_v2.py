"""Bounded exact raw-source text spans for ``ordered-dom-ir.v2``.

This opt-in mapper does not mutate the document IR or crawler output. It
accepts only when non-ignorable retained DOM text runs form a complete,
ordered bijection with strict raw HTML source events. Parser-reparented runs
may be omitted only when both sides are exact HTML whitespace outside
whitespace-preserving elements; both omission counts are explicit. Each
accepted span retains decoded lexical identity and the exact raw UTF-8
fragment and offsets that produced it. Parser repairs, foster parenting,
reordered non-whitespace text, incomplete source, and budget exhaustion reject
the entire map when they violate the retained explicit-element mapping or the
direct-parent, decoded-identity, and order bijection. Standards-defined
implicit structure such as an inserted ``tbody`` remains eligible when that
contract stays exact.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ._native import (
    NativeDocumentIRV2,
    NativeOrderedSourceTextMapV2,
    NativeOrderedSourceTextSpanV2,
    map_ordered_source_text_v2_native,
)

ORDERED_SOURCE_TEXT_MAP_V2_SCHEMA: Final = "ordered-source-text-map.v2"
ORDERED_SOURCE_TEXT_SPAN_V2_SCHEMA: Final = "ordered-source-text-span.v2"

_HARD_MAX_SOURCE_BYTES: Final = 16 * 1024 * 1024
_HARD_MAX_SOURCE_EVENTS: Final = 1_000_000
_HARD_MAX_TEXT_RUNS: Final = 500_000
_HARD_MAX_RAW_FRAGMENT_BYTES: Final = 16 * 1024 * 1024
_HARD_MAX_TOTAL_RAW_BYTES: Final = 16 * 1024 * 1024
_HARD_MAX_STACK_DEPTH: Final = 512

OrderedSourceTextSpanV2 = NativeOrderedSourceTextSpanV2
OrderedSourceTextMapV2 = NativeOrderedSourceTextMapV2


def _bounded_int(name: str, value: int, maximum: int) -> None:
    if type(value) is not int or value <= 0 or value > maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")


@dataclass(frozen=True, slots=True)
class OrderedSourceTextMapV2Limits:
    """Caller-lowerable limits, independently revalidated in native code."""

    max_source_bytes: int = 4 * 1024 * 1024
    max_source_events: int = 500_000
    max_text_runs: int = 200_000
    max_raw_fragment_bytes: int = 1024 * 1024
    max_total_raw_bytes: int = 8 * 1024 * 1024
    max_stack_depth: int = 256

    def __post_init__(self) -> None:
        _bounded_int("max_source_bytes", self.max_source_bytes, _HARD_MAX_SOURCE_BYTES)
        _bounded_int("max_source_events", self.max_source_events, _HARD_MAX_SOURCE_EVENTS)
        _bounded_int("max_text_runs", self.max_text_runs, _HARD_MAX_TEXT_RUNS)
        _bounded_int(
            "max_raw_fragment_bytes",
            self.max_raw_fragment_bytes,
            _HARD_MAX_RAW_FRAGMENT_BYTES,
        )
        _bounded_int(
            "max_total_raw_bytes",
            self.max_total_raw_bytes,
            _HARD_MAX_TOTAL_RAW_BYTES,
        )
        _bounded_int("max_stack_depth", self.max_stack_depth, _HARD_MAX_STACK_DEPTH)


DEFAULT_ORDERED_SOURCE_TEXT_MAP_V2_LIMITS = OrderedSourceTextMapV2Limits()


def map_ordered_source_text_v2(
    document: NativeDocumentIRV2,
    *,
    limits: OrderedSourceTextMapV2Limits = DEFAULT_ORDERED_SOURCE_TEXT_MAP_V2_LIMITS,
) -> NativeOrderedSourceTextMapV2:
    """Return an all-or-nothing raw-source map for one already-parsed v2 IR."""

    if type(document) is not NativeDocumentIRV2:
        raise TypeError("document must be a NativeDocumentIRV2")
    limits = _canonical_limits(limits)
    return map_ordered_source_text_v2_native(
        document,
        max_source_bytes=limits.max_source_bytes,
        max_source_events=limits.max_source_events,
        max_text_runs=limits.max_text_runs,
        max_raw_fragment_bytes=limits.max_raw_fragment_bytes,
        max_total_raw_bytes=limits.max_total_raw_bytes,
        max_stack_depth=limits.max_stack_depth,
    )


def _canonical_limits(limits: OrderedSourceTextMapV2Limits) -> OrderedSourceTextMapV2Limits:
    if type(limits) is not OrderedSourceTextMapV2Limits:
        raise TypeError("limits must be an OrderedSourceTextMapV2Limits")
    return OrderedSourceTextMapV2Limits(
        max_source_bytes=object.__getattribute__(limits, "max_source_bytes"),
        max_source_events=object.__getattribute__(limits, "max_source_events"),
        max_text_runs=object.__getattribute__(limits, "max_text_runs"),
        max_raw_fragment_bytes=object.__getattribute__(limits, "max_raw_fragment_bytes"),
        max_total_raw_bytes=object.__getattribute__(limits, "max_total_raw_bytes"),
        max_stack_depth=object.__getattribute__(limits, "max_stack_depth"),
    )

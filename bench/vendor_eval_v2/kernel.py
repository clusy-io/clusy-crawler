"""Fail-closed v5 synthetic scorer for a future blind live-vendor benchmark.

The module is deliberately provider- and dataset-agnostic.  It accepts visible
text that an eventual, separately sealed adapter will supply.  It never reads
environment variables, performs network I/O, or imports crawler runtime code.
It binds synthetic artifacts to packaged source bytes, version-pinned Unicode
confusables data, canonical fixtures, and immutable protocol/policy manifests.
The in-process semantic-code check is only an accidental-drift canary. It is
not a tamper-resistant attestation or a source of claim authority.
"""

from __future__ import annotations

import hashlib
import json
import math
import platform
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import CodeType, FunctionType, MappingProxyType
from typing import Any, Final, Literal, cast

_MODULE_EXECUTION_CODE: Final = sys._getframe().f_code

PROTOCOL_FAMILY: Final = "clusy.blind-vendor.scorer.synthetic.v5"
ARTIFACT_STATUS: Final = "SYNTHETIC_ONLY / NOT_CLAIMABLE"
UNICODE_DATA_VERSION: Final = unicodedata.unidata_version
SYNTHETIC_FIXTURE_SCHEMA: Final = "clusy.blind-vendor.synthetic-fixtures.v5"
SYNTHETIC_FIXTURE_SHA256: Final = "c33b3e9a4dd953c95b329b102efede43a244ae0e4c7b5ce2c6ecfd2289217774"
UTS39_CONFUSABLES_VERSION: Final = "16.0.0"
UTS39_CONFUSABLES_FILENAME: Final = "confusables-16.0.0.txt"
UTS39_CONFUSABLES_SOURCE_SHA256: Final = (
    "95bd0aad6dced5ebc63436f459c06ab21a8d107cd842fb57f5c3a1e91bca8611"
)
INTERNAL_SKELETON_PROFILE: Final = (
    "unicode-confusables-data-"
    f"{UTS39_CONFUSABLES_VERSION}.internal-skeleton-diagnostic"
    f".source-{UTS39_CONFUSABLES_SOURCE_SHA256}"
)


def _loaded_source_sha256() -> str:
    """Bind artifacts to the exact scorer source bytes or fail closed."""

    try:
        with open(__file__, "rb") as source_file:
            return hashlib.sha256(source_file.read()).hexdigest()
    except OSError as exc:  # pragma: no cover - packaging failure, not a score path
        raise RuntimeError("scorer source bytes are required for protocol identity") from exc


SCORER_SOURCE_SHA256: Final = _loaded_source_sha256()
try:
    SYNTHETIC_FIXTURE_SOURCE_SHA256: Final = hashlib.sha256(
        Path(__file__).with_name("synthetic_fixtures.json").read_bytes()
    ).hexdigest()
except OSError as exc:  # pragma: no cover - packaging failure, not a score path
    raise RuntimeError("synthetic fixture bytes are required for protocol identity") from exc
RUNTIME_BUILD_SHA256: Final = hashlib.sha256(
    "\0".join(
        (
            sys.implementation.name,
            platform.python_version(),
            sys.version,
            repr(platform.python_build()),
            platform.python_compiler(),
            platform.machine(),
            sys.byteorder,
            UNICODE_DATA_VERSION,
        )
    ).encode("utf-8")
).hexdigest()
UNICODE_RUNTIME_PROFILE: Final = (
    f"{sys.implementation.name}-{platform.python_version()}"
    f".ucd-{UNICODE_DATA_VERSION}.build-{RUNTIME_BUILD_SHA256[:16]}"
)
# These identities are finalized after all callable code objects exist.
PROTOCOL_VERSION: str
PROTOCOL_MANIFEST: Mapping[str, Any]
PROTOCOL_MANIFEST_SHA256: str
CLAIM_MANIFEST: Mapping[str, Any]
CLAIM_MANIFEST_SHA256: str
DRIFT_CANARY_MANIFEST: Mapping[str, Any]
DRIFT_CANARY_MANIFEST_SHA256: str

QGRAM_SIZE: Final = 5
POSITION_BUCKETS: Final = 64
ORDERED_LCS_WORD_BITS: Final = 64
WINDOW_LENGTH_RATIOS: Final = (
    (1, 2),
    (3, 4),
    (1, 1),
    (5, 4),
    (3, 2),
    (2, 1),
)
METRIC_REGISTRY: Final[Mapping[str, Mapping[str, str]]] = MappingProxyType(
    {
        "joint_utility": MappingProxyType(
            {
                "direction": "higher",
                "role": "primary",
                "minimum_superiority_delta": "0.015000000000",
            }
        ),
        "truth_quality": MappingProxyType(
            {
                "direction": "higher",
                "role": "guardrail",
                "noninferiority_delta": "-0.005000000000",
            }
        ),
        "truth_whole_output_f1": MappingProxyType(
            {
                "direction": "higher",
                "role": "guardrail",
                "noninferiority_delta": "-0.005000000000",
            }
        ),
        "truth_positional_f1": MappingProxyType(
            {
                "direction": "higher",
                "role": "guardrail",
                "noninferiority_delta": "-0.005000000000",
            }
        ),
        "truth_ordered_f1": MappingProxyType(
            {
                "direction": "higher",
                "role": "guardrail",
                "noninferiority_delta": "-0.005000000000",
            }
        ),
        "truth_best_window_f1": MappingProxyType(
            {
                "direction": "higher",
                "role": "guardrail",
                "noninferiority_delta": "-0.005000000000",
            }
        ),
        "lie_leakage_f1": MappingProxyType(
            {
                "direction": "lower",
                "role": "guardrail",
                "noninferiority_delta": "-0.005000000000",
            }
        ),
        "lie_leakage_normal_f1": MappingProxyType(
            {
                "direction": "lower",
                "role": "diagnostic",
            }
        ),
        "lie_leakage_internal_skeleton_diagnostic_f1": MappingProxyType(
            {
                "direction": "lower",
                "role": "diagnostic",
            }
        ),
        "discriminative_margin": MappingProxyType(
            {
                "direction": "higher",
                "role": "diagnostic",
                "noninferiority_delta": "-0.005000000000",
            }
        ),
    }
)
_METRIC_BOUNDS: Final[Mapping[str, tuple[float, float]]] = MappingProxyType(
    {
        "joint_utility": (0.0, 1.0),
        "truth_quality": (0.0, 1.0),
        "truth_whole_output_f1": (0.0, 1.0),
        "truth_positional_f1": (0.0, 1.0),
        "truth_ordered_f1": (0.0, 1.0),
        "truth_best_window_f1": (0.0, 1.0),
        "lie_leakage_f1": (0.0, 1.0),
        "lie_leakage_normal_f1": (0.0, 1.0),
        "lie_leakage_internal_skeleton_diagnostic_f1": (0.0, 1.0),
        "discriminative_margin": (-1.0, 1.0),
    }
)

_METRIC_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_UNIT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_SAFE_JSON_INTEGER: Final = (1 << 53) - 1
_HARD_MAX_INPUT_CODEPOINTS: Final = 200_000
_HARD_MAX_INPUT_UTF8_BYTES: Final = 1_000_000
_HARD_MAX_NORMALIZED_CODEPOINTS: Final = 250_000
_HARD_MAX_LIES: Final = 16
_HARD_MAX_WINDOW_EVALUATIONS: Final = 5_000_000
_HARD_MAX_WHOLE_OUTPUT_GRAM_OPERATIONS: Final = 500_000
_HARD_MAX_ORDERED_LCS_WORD_OPERATIONS: Final = 5_000_000
_HARD_MAX_BOOTSTRAP_PAIRS: Final = 20_000
_HARD_MAX_BOOTSTRAP_CLUSTERS: Final = 20_000
_HARD_MAX_BOOTSTRAP_LANGUAGE_GROUPS: Final = 1_000
_HARD_MAX_BOOTSTRAP_SAMPLES: Final = 100_000
_HARD_MAX_BOOTSTRAP_DRAWS: Final = 20_000_000
_HARD_MAX_CANONICAL_NODES: Final = 250_000
_HARD_MAX_CANONICAL_MAPPING_ITEMS: Final = 100_000
_HARD_MAX_CANONICAL_KEY_CODEPOINTS: Final = 250_000
_HARD_MAX_CANONICAL_STRING_CODEPOINTS: Final = 1_000_000
_HARD_MAX_CANONICAL_OUTPUT_BYTES: Final = 8_000_000
_HARD_MAX_AGGREGATE_INPUT_CODEPOINTS: Final = 1_000_000
_HARD_MAX_AGGREGATE_INPUT_UTF8_BYTES: Final = 4_000_000
_HARD_MAX_AGGREGATE_NORMALIZED_CODEPOINTS: Final = 1_000_000
_HARD_MAX_AGGREGATE_SKELETON_CODEPOINTS: Final = 1_000_000
_MAX_ABS_BOOTSTRAP_VALUE: Final = 1_000_000_000_000.0
_MAX_CANONICAL_JSON_DEPTH: Final = 128
_MAX_BOOTSTRAP_REJECTION_ATTEMPTS: Final = 128
CLAIM_BOOTSTRAP_SAMPLES: Final = 10_000
CLAIM_BOOTSTRAP_SEED: Final = 7_291_337

Direction = Literal["higher", "lower"]
JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


class KernelError(ValueError):
    """The input is invalid under the pre-registered scorer protocol."""


class BudgetExceededError(KernelError):
    """A bounded scorer operation would exceed a declared hard budget."""


BudgetExceeded = BudgetExceededError


def _positive_bounded_int(name: str, value: int, ceiling: int) -> None:
    if type(value) is not int or not 1 <= value <= ceiling:
        raise KernelError(f"{name} must be an integer in [1, {ceiling}]")


@dataclass(frozen=True, slots=True)
class KernelLimits:
    """Reducible resource caps; every compiled default is also a hard ceiling."""

    max_input_codepoints: int = 200_000
    max_input_utf8_bytes: int = 1_000_000
    max_normalized_codepoints: int = 250_000
    max_lies: int = 16
    max_window_evaluations: int = 5_000_000
    max_whole_output_gram_operations: int = 500_000
    max_ordered_lcs_word_operations: int = 5_000_000
    max_bootstrap_pairs: int = 20_000
    max_bootstrap_clusters: int = 20_000
    max_bootstrap_language_groups: int = 1_000
    max_bootstrap_samples: int = 100_000
    max_bootstrap_draws: int = 20_000_000
    max_canonical_nodes: int = 250_000
    max_canonical_mapping_items: int = 100_000
    max_canonical_key_codepoints: int = 250_000
    max_canonical_string_codepoints: int = 1_000_000
    max_canonical_output_bytes: int = 8_000_000
    max_aggregate_input_codepoints: int = 1_000_000
    max_aggregate_input_utf8_bytes: int = 4_000_000
    max_aggregate_normalized_codepoints: int = 1_000_000
    max_aggregate_skeleton_codepoints: int = 1_000_000

    def __post_init__(self) -> None:
        _positive_bounded_int(
            "max_input_codepoints",
            self.max_input_codepoints,
            _HARD_MAX_INPUT_CODEPOINTS,
        )
        _positive_bounded_int(
            "max_input_utf8_bytes",
            self.max_input_utf8_bytes,
            _HARD_MAX_INPUT_UTF8_BYTES,
        )
        _positive_bounded_int(
            "max_normalized_codepoints",
            self.max_normalized_codepoints,
            _HARD_MAX_NORMALIZED_CODEPOINTS,
        )
        _positive_bounded_int("max_lies", self.max_lies, _HARD_MAX_LIES)
        _positive_bounded_int(
            "max_window_evaluations",
            self.max_window_evaluations,
            _HARD_MAX_WINDOW_EVALUATIONS,
        )
        _positive_bounded_int(
            "max_whole_output_gram_operations",
            self.max_whole_output_gram_operations,
            _HARD_MAX_WHOLE_OUTPUT_GRAM_OPERATIONS,
        )
        _positive_bounded_int(
            "max_ordered_lcs_word_operations",
            self.max_ordered_lcs_word_operations,
            _HARD_MAX_ORDERED_LCS_WORD_OPERATIONS,
        )
        _positive_bounded_int(
            "max_bootstrap_pairs",
            self.max_bootstrap_pairs,
            _HARD_MAX_BOOTSTRAP_PAIRS,
        )
        _positive_bounded_int(
            "max_bootstrap_clusters",
            self.max_bootstrap_clusters,
            _HARD_MAX_BOOTSTRAP_CLUSTERS,
        )
        _positive_bounded_int(
            "max_bootstrap_language_groups",
            self.max_bootstrap_language_groups,
            _HARD_MAX_BOOTSTRAP_LANGUAGE_GROUPS,
        )
        _positive_bounded_int(
            "max_bootstrap_samples",
            self.max_bootstrap_samples,
            _HARD_MAX_BOOTSTRAP_SAMPLES,
        )
        _positive_bounded_int(
            "max_bootstrap_draws",
            self.max_bootstrap_draws,
            _HARD_MAX_BOOTSTRAP_DRAWS,
        )
        _positive_bounded_int(
            "max_canonical_nodes",
            self.max_canonical_nodes,
            _HARD_MAX_CANONICAL_NODES,
        )
        _positive_bounded_int(
            "max_canonical_mapping_items",
            self.max_canonical_mapping_items,
            _HARD_MAX_CANONICAL_MAPPING_ITEMS,
        )
        _positive_bounded_int(
            "max_canonical_key_codepoints",
            self.max_canonical_key_codepoints,
            _HARD_MAX_CANONICAL_KEY_CODEPOINTS,
        )
        _positive_bounded_int(
            "max_canonical_string_codepoints",
            self.max_canonical_string_codepoints,
            _HARD_MAX_CANONICAL_STRING_CODEPOINTS,
        )
        _positive_bounded_int(
            "max_canonical_output_bytes",
            self.max_canonical_output_bytes,
            _HARD_MAX_CANONICAL_OUTPUT_BYTES,
        )
        _positive_bounded_int(
            "max_aggregate_input_codepoints",
            self.max_aggregate_input_codepoints,
            _HARD_MAX_AGGREGATE_INPUT_CODEPOINTS,
        )
        _positive_bounded_int(
            "max_aggregate_input_utf8_bytes",
            self.max_aggregate_input_utf8_bytes,
            _HARD_MAX_AGGREGATE_INPUT_UTF8_BYTES,
        )
        _positive_bounded_int(
            "max_aggregate_normalized_codepoints",
            self.max_aggregate_normalized_codepoints,
            _HARD_MAX_AGGREGATE_NORMALIZED_CODEPOINTS,
        )
        _positive_bounded_int(
            "max_aggregate_skeleton_codepoints",
            self.max_aggregate_skeleton_codepoints,
            _HARD_MAX_AGGREGATE_SKELETON_CODEPOINTS,
        )


DEFAULT_LIMITS: Final = KernelLimits()


def _validated_limits(limits: Any) -> KernelLimits:
    """Return a validated value snapshot, rejecting forged or mutated limits."""

    if type(limits) is not KernelLimits:
        raise KernelError("limits must be exactly KernelLimits")
    return KernelLimits(
        max_input_codepoints=limits.max_input_codepoints,
        max_input_utf8_bytes=limits.max_input_utf8_bytes,
        max_normalized_codepoints=limits.max_normalized_codepoints,
        max_lies=limits.max_lies,
        max_window_evaluations=limits.max_window_evaluations,
        max_whole_output_gram_operations=limits.max_whole_output_gram_operations,
        max_ordered_lcs_word_operations=limits.max_ordered_lcs_word_operations,
        max_bootstrap_pairs=limits.max_bootstrap_pairs,
        max_bootstrap_clusters=limits.max_bootstrap_clusters,
        max_bootstrap_language_groups=limits.max_bootstrap_language_groups,
        max_bootstrap_samples=limits.max_bootstrap_samples,
        max_bootstrap_draws=limits.max_bootstrap_draws,
        max_canonical_nodes=limits.max_canonical_nodes,
        max_canonical_mapping_items=limits.max_canonical_mapping_items,
        max_canonical_key_codepoints=limits.max_canonical_key_codepoints,
        max_canonical_string_codepoints=limits.max_canonical_string_codepoints,
        max_canonical_output_bytes=limits.max_canonical_output_bytes,
        max_aggregate_input_codepoints=limits.max_aggregate_input_codepoints,
        max_aggregate_input_utf8_bytes=limits.max_aggregate_input_utf8_bytes,
        max_aggregate_normalized_codepoints=limits.max_aggregate_normalized_codepoints,
        max_aggregate_skeleton_codepoints=limits.max_aggregate_skeleton_codepoints,
    )


def _measure_unicode_string(
    value: Any,
    *,
    label: str,
    limits: KernelLimits,
) -> tuple[str, int]:
    """Validate exact text and count UTF-8 bytes without allocating an encoding."""

    if type(value) is not str:
        raise KernelError(f"{label} must be a Unicode string")
    if len(value) > limits.max_input_codepoints:
        raise BudgetExceeded(f"{label} exceeds max_input_codepoints={limits.max_input_codepoints}")
    utf8_bytes = 0
    for character in value:
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            raise KernelError(f"{label} contains an unpaired surrogate")
        utf8_bytes += (
            1 if codepoint < 0x80 else 2 if codepoint < 0x800 else 3 if codepoint < 0x10000 else 4
        )
        if utf8_bytes > limits.max_input_utf8_bytes:
            raise BudgetExceeded(
                f"{label} exceeds max_input_utf8_bytes={limits.max_input_utf8_bytes}"
            )
    return value, utf8_bytes


def _validate_unicode_string(value: Any, *, label: str, limits: KernelLimits) -> str:
    checked, _ = _measure_unicode_string(value, label=label, limits=limits)
    return checked


def _bounded_unicode_transform(value: str, *, label: str, limits: KernelLimits) -> str:
    transformed = unicodedata.normalize("NFKC", value)
    if len(transformed) > limits.max_normalized_codepoints:
        raise BudgetExceeded(f"{label} exceeds max_normalized_codepoints after NFKC")
    transformed = transformed.casefold()
    if len(transformed) > limits.max_normalized_codepoints:
        raise BudgetExceeded(f"{label} exceeds max_normalized_codepoints after casefold")
    # A second NFKC makes casefold expansions canonical as well.  This remains
    # the explicitly documented NFKC -> casefold -> NFKC pipeline.
    transformed = unicodedata.normalize("NFKC", transformed)
    if len(transformed) > limits.max_normalized_codepoints:
        raise BudgetExceeded(f"{label} exceeds max_normalized_codepoints after final NFKC")
    return transformed


def _is_default_ignorable_code_point(character: str) -> bool:
    """Pinned Default_Ignorable_Code_Point coverage needed by visible text.

    Most members are already category ``Cf`` and would be removed by the
    general control filter.  Explicit ranges are still required for combining
    grapheme joiner and variation selectors, whose category is ``Mn``.
    """

    codepoint = ord(character)
    return (
        codepoint in {0x00AD, 0x034F, 0x061C, 0x3164, 0xFEFF, 0xFFA0}
        or 0x115F <= codepoint <= 0x1160
        or 0x17B4 <= codepoint <= 0x17B5
        or 0x180B <= codepoint <= 0x180F
        or 0x200B <= codepoint <= 0x200F
        or 0x202A <= codepoint <= 0x202E
        or 0x2060 <= codepoint <= 0x206F
        or 0xFE00 <= codepoint <= 0xFE0F
        or 0xFFF0 <= codepoint <= 0xFFF8
        or 0x1BCA0 <= codepoint <= 0x1BCA3
        or 0x1D173 <= codepoint <= 0x1D17A
        or 0xE0000 <= codepoint <= 0xE0FFF
    )


def normalize_visible_text(
    value: str,
    *,
    label: str = "text",
    limits: KernelLimits = DEFAULT_LIMITS,
) -> str:
    """Normalize already-visible text with NFKC/casefold and one-space runs.

    All Unicode whitespace becomes ASCII space.  Other ``C*`` category code
    points (controls, format controls, surrogates, private-use and unassigned
    values) and pinned default-ignorables are removed. Surrogates are rejected
    before normalization. Markup-to-visible-text conversion is outside this
    kernel.
    """

    limits = _validated_limits(limits)
    checked = _validate_unicode_string(value, label=label, limits=limits)
    # Remove normalization blockers (notably U+034F) before NFKC, then apply
    # the same filter again after normalization in case a future UCD mapping
    # emits a default-ignorable.
    prefiltered = "".join(
        character for character in checked if not _is_default_ignorable_code_point(character)
    )
    transformed = _bounded_unicode_transform(prefiltered, label=label, limits=limits)
    output: list[str] = []
    pending_space = False
    for character in transformed:
        if character.isspace():
            pending_space = bool(output)
            continue
        if _is_default_ignorable_code_point(character) or unicodedata.category(
            character
        ).startswith("C"):
            continue
        if pending_space:
            output.append(" ")
            pending_space = False
        output.append(character)
        if len(output) > limits.max_normalized_codepoints:
            raise BudgetExceeded(
                f"{label} exceeds max_normalized_codepoints after visible-text filtering"
            )
    return "".join(output)


def _load_uts39_confusables() -> Mapping[int, str]:
    path = Path(__file__).with_name(UTS39_CONFUSABLES_FILENAME)
    try:
        source = path.read_bytes()
    except OSError as exc:  # pragma: no cover - invalid package assembly
        raise RuntimeError("version-pinned UTS #39 confusables data is required") from exc
    observed_sha256 = hashlib.sha256(source).hexdigest()
    if observed_sha256 != UTS39_CONFUSABLES_SOURCE_SHA256:
        raise RuntimeError(
            "UTS #39 confusables source digest mismatch: "
            f"expected {UTS39_CONFUSABLES_SOURCE_SHA256}, observed {observed_sha256}"
        )
    try:
        text = source.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:  # pragma: no cover - digest-pinned asset
        raise RuntimeError("UTS #39 confusables data must be UTF-8") from exc
    if f"# Version: {UTS39_CONFUSABLES_VERSION}" not in text:
        raise RuntimeError("UTS #39 confusables version header mismatch")

    translation: dict[int, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        payload = raw_line.partition("#")[0].strip()
        if not payload:
            continue
        fields = [field.strip() for field in payload.split(";")]
        if len(fields) != 3:
            raise RuntimeError(f"invalid UTS #39 row at line {line_number}")
        source_points = fields[0].split()
        if len(source_points) != 1:
            raise RuntimeError(f"unsupported multi-code-point UTS #39 source at line {line_number}")
        try:
            source_point = int(source_points[0], 16)
            target = "".join(chr(int(codepoint, 16)) for codepoint in fields[1].split())
        except (ValueError, OverflowError) as exc:
            raise RuntimeError(f"invalid UTS #39 code point at line {line_number}") from exc
        if not target or source_point in translation:
            raise RuntimeError(f"invalid duplicate/empty UTS #39 row at line {line_number}")
        translation[source_point] = target
    if not translation:
        raise RuntimeError("UTS #39 confusables data has no mappings")
    return MappingProxyType(translation)


_UTS39_CONFUSABLE_TRANSLATION: Final[Mapping[int, str]] = _load_uts39_confusables()


def _internal_skeleton_diagnostic_normalized(
    text: str,
    *,
    label: str,
    limits: KernelLimits,
) -> str:
    # This follows the internalSkeleton-shaped mapping sequence with pinned
    # Unicode 16 confusables data and the bound runtime's NFD implementation.
    # It is not the Unicode 16 skeleton operation, which additionally requires
    # bidiSkeleton(LTR, X), or a cross-runtime Unicode-conformance claim.
    decomposed = unicodedata.normalize("NFD", text)
    if len(decomposed) > limits.max_normalized_codepoints:
        raise BudgetExceeded(f"{label} exceeds max_normalized_codepoints before UTS #39")
    output: list[str] = []
    output_codepoints = 0
    for character in decomposed:
        mapped = _UTS39_CONFUSABLE_TRANSLATION.get(ord(character), character)
        output_codepoints += len(mapped)
        if output_codepoints > limits.max_normalized_codepoints:
            raise BudgetExceeded(f"{label} exceeds max_normalized_codepoints during UTS #39")
        output.append(mapped)
    skeleton = unicodedata.normalize("NFD", "".join(output))
    if len(skeleton) > limits.max_normalized_codepoints:
        raise BudgetExceeded(f"{label} exceeds max_normalized_codepoints after UTS #39")
    return skeleton


def confusable_internal_skeleton_diagnostic(
    value: str,
    *,
    label: str = "text",
    limits: KernelLimits = DEFAULT_LIMITS,
) -> str:
    """Return an internalSkeleton-shaped, non-primary confusable diagnostic.

    This is deliberately not named or represented as UTS #39 ``skeleton``.
    Unicode 16 defines that operation through ``bidiSkeleton(LTR, X)``, whose
    complete UBA dependency is outside this synthetic kernel. NFD uses the
    bound Python runtime UCD, so this is not a Unicode-16 conformance claim.
    """

    _assert_accidental_drift_canary()
    limits = _validated_limits(limits)
    normalized = normalize_visible_text(value, label=label, limits=limits)
    return _internal_skeleton_diagnostic_normalized(normalized, label=label, limits=limits)


def _validated_qgram_size(size: Any) -> int:
    if type(size) is not int or size < 1:
        raise KernelError("n-gram size must be a positive integer")
    return size


def _character_ngrams_unchecked(text: str, *, size: int = QGRAM_SIZE) -> Counter[str]:
    if len(text) < size:
        return Counter()
    return Counter(text[index : index + size] for index in range(len(text) - size + 1))


def character_ngrams(
    text: str,
    *,
    size: int = QGRAM_SIZE,
    limits: KernelLimits = DEFAULT_LIMITS,
) -> Counter[str]:
    """Return a bounded character n-gram multiset for exact built-in text."""

    limits = _validated_limits(limits)
    checked = _validate_unicode_string(text, label="text", limits=limits)
    size = _validated_qgram_size(size)
    operations = max(0, len(checked) - size + 1)
    if operations > limits.max_whole_output_gram_operations:
        raise BudgetExceeded(
            f"q-gram work {operations} exceeds max_whole_output_gram_operations="
            f"{limits.max_whole_output_gram_operations}"
        )
    return _character_ngrams_unchecked(checked, size=size)


def _character_ngrams_sequence_unchecked(
    text: str,
    *,
    size: int = QGRAM_SIZE,
) -> tuple[str, ...]:
    if len(text) < size:
        return ()
    return tuple(text[index : index + size] for index in range(len(text) - size + 1))


def character_ngrams_sequence(
    text: str,
    *,
    size: int = QGRAM_SIZE,
    limits: KernelLimits = DEFAULT_LIMITS,
) -> tuple[str, ...]:
    """Return a bounded q-gram sequence for exact built-in text."""

    limits = _validated_limits(limits)
    checked = _validate_unicode_string(text, label="text", limits=limits)
    size = _validated_qgram_size(size)
    operations = max(0, len(checked) - size + 1)
    if operations > limits.max_whole_output_gram_operations:
        raise BudgetExceeded(
            f"q-gram sequence work {operations} exceeds "
            f"max_whole_output_gram_operations={limits.max_whole_output_gram_operations}"
        )
    return _character_ngrams_sequence_unchecked(checked, size=size)


def _positional_character_ngrams(
    text: str,
    *,
    buckets: int = POSITION_BUCKETS,
) -> Counter[tuple[int, str]]:
    gram_count = max(0, len(text) - QGRAM_SIZE + 1)
    if gram_count == 0:
        return Counter()
    return Counter(
        (
            min(buckets - 1, (index * buckets) // gram_count),
            text[index : index + QGRAM_SIZE],
        )
        for index in range(gram_count)
    )


@dataclass(frozen=True, slots=True)
class WindowScore:
    precision: float
    recall: float
    f1: float
    overlap_grams: int
    candidate_window_grams: int
    reference_grams: int
    normalized_start: int
    normalized_end: int
    synthetic_fail_closed: bool = False

    def to_document(self) -> dict[str, JsonValue]:
        return {
            "candidate_window_grams": self.candidate_window_grams,
            "f1": _fixed_decimal(self.f1),
            "normalized_end": self.normalized_end,
            "normalized_start": self.normalized_start,
            "overlap_grams": self.overlap_grams,
            "precision": _fixed_decimal(self.precision),
            "recall": _fixed_decimal(self.recall),
            "reference_grams": self.reference_grams,
            "synthetic_fail_closed": self.synthetic_fail_closed,
        }


@dataclass(frozen=True, slots=True)
class MultisetScore:
    precision: float
    recall: float
    f1: float
    overlap_grams: int
    candidate_grams: int
    reference_grams: int

    def to_document(self) -> dict[str, JsonValue]:
        return {
            "candidate_grams": self.candidate_grams,
            "f1": _fixed_decimal(self.f1),
            "overlap_grams": self.overlap_grams,
            "precision": _fixed_decimal(self.precision),
            "recall": _fixed_decimal(self.recall),
            "reference_grams": self.reference_grams,
        }


@dataclass(frozen=True, slots=True)
class PositionalScore:
    precision: float
    recall: float
    f1: float
    overlap_grams: int
    candidate_grams: int
    reference_grams: int
    buckets: int

    def to_document(self) -> dict[str, JsonValue]:
        return {
            "buckets": self.buckets,
            "candidate_grams": self.candidate_grams,
            "f1": _fixed_decimal(self.f1),
            "overlap_grams": self.overlap_grams,
            "precision": _fixed_decimal(self.precision),
            "recall": _fixed_decimal(self.recall),
            "reference_grams": self.reference_grams,
        }


@dataclass(frozen=True, slots=True)
class OrderedScore:
    """Edit-aligned q-gram LCS over the full normalized candidate."""

    precision: float
    recall: float
    f1: float
    overlap_grams: int
    candidate_grams: int
    reference_grams: int
    lcs_word_operations: int

    def to_document(self) -> dict[str, JsonValue]:
        return {
            "alignment": "full-candidate-bit-parallel-qgram-lcs.v2",
            "candidate_grams": self.candidate_grams,
            "f1": _fixed_decimal(self.f1),
            "lcs_word_operations": self.lcs_word_operations,
            "overlap_grams": self.overlap_grams,
            "precision": _fixed_decimal(self.precision),
            "recall": _fixed_decimal(self.recall),
            "reference_grams": self.reference_grams,
        }


def _multiset_f1_normalized(candidate: str, reference: str) -> MultisetScore:
    candidate_counts = _character_ngrams_unchecked(candidate)
    reference_counts = _character_ngrams_unchecked(reference)
    candidate_grams = sum(candidate_counts.values())
    reference_grams = sum(reference_counts.values())
    if reference_grams == 0:
        raise KernelError(f"reference must normalize to at least {QGRAM_SIZE} code points")
    overlap = sum((candidate_counts & reference_counts).values())
    precision = overlap / candidate_grams if candidate_grams else 0.0
    recall = overlap / reference_grams
    f1 = (2 * overlap) / (candidate_grams + reference_grams) if candidate_grams else 0.0
    return MultisetScore(
        precision=precision,
        recall=recall,
        f1=f1,
        overlap_grams=overlap,
        candidate_grams=candidate_grams,
        reference_grams=reference_grams,
    )


def _positional_f1_normalized(candidate: str, reference: str) -> PositionalScore:
    candidate_counts = _positional_character_ngrams(candidate)
    reference_counts = _positional_character_ngrams(reference)
    candidate_grams = sum(candidate_counts.values())
    reference_grams = sum(reference_counts.values())
    if reference_grams == 0:
        raise KernelError(f"reference must normalize to at least {QGRAM_SIZE} code points")
    overlap = sum((candidate_counts & reference_counts).values())
    precision = overlap / candidate_grams if candidate_grams else 0.0
    recall = overlap / reference_grams
    f1 = (2 * overlap) / (candidate_grams + reference_grams) if candidate_grams else 0.0
    return PositionalScore(
        precision=precision,
        recall=recall,
        f1=f1,
        overlap_grams=overlap,
        candidate_grams=candidate_grams,
        reference_grams=reference_grams,
        buckets=POSITION_BUCKETS,
    )


def _ordered_f1_normalized(
    candidate: str,
    reference: str,
    *,
    limits: KernelLimits,
) -> OrderedScore:
    """Return exact full-candidate q-gram LCS F1 using bit-parallel DP."""

    candidate_grams = max(0, len(candidate) - QGRAM_SIZE + 1)
    reference_grams = max(0, len(reference) - QGRAM_SIZE + 1)
    if reference_grams == 0:
        raise KernelError(f"reference must normalize to at least {QGRAM_SIZE} code points")
    complete_words, partial_grams = divmod(reference_grams, ORDERED_LCS_WORD_BITS)
    mask_build_word_operations = ORDERED_LCS_WORD_BITS * complete_words * (
        complete_words + 1
    ) // 2 + partial_grams * (complete_words + 1)
    transition_word_operations = candidate_grams * (
        (reference_grams + ORDERED_LCS_WORD_BITS - 1) // ORDERED_LCS_WORD_BITS
    )
    word_operations = mask_build_word_operations + transition_word_operations
    if word_operations > limits.max_ordered_lcs_word_operations:
        raise BudgetExceeded(
            f"ordered LCS work {word_operations} exceeds "
            "max_ordered_lcs_word_operations="
            f"{limits.max_ordered_lcs_word_operations}"
        )
    positions: dict[str, int] = {}
    for index in range(reference_grams):
        gram = reference[index : index + QGRAM_SIZE]
        positions[gram] = positions.get(gram, 0) | (1 << index)
    row = 0
    for index in range(candidate_grams):
        gram = candidate[index : index + QGRAM_SIZE]
        matches_or_row = positions.get(gram, 0) | row
        row = matches_or_row & ~(matches_or_row - ((row << 1) | 1))
    overlap = row.bit_count()
    precision = overlap / candidate_grams if candidate_grams else 0.0
    recall = overlap / reference_grams
    f1 = (2 * overlap) / (candidate_grams + reference_grams) if candidate_grams else 0.0
    return OrderedScore(
        precision=precision,
        recall=recall,
        f1=f1,
        overlap_grams=overlap,
        candidate_grams=candidate_grams,
        reference_grams=reference_grams,
        lcs_word_operations=word_operations,
    )


def _whole_output_work(candidate: str, reference: str) -> int:
    return max(0, len(candidate) - QGRAM_SIZE + 1) + max(0, len(reference) - QGRAM_SIZE + 1)


def _normalize_scoring_pair(
    candidate: Any,
    reference: Any,
    *,
    limits: KernelLimits,
) -> tuple[str, str]:
    _preflight_aggregate_inputs(
        (("candidate", candidate), ("reference", reference)),
        limits=limits,
    )
    normalized_candidate = normalize_visible_text(candidate, label="candidate", limits=limits)
    normalized_reference = normalize_visible_text(reference, label="reference", limits=limits)
    _check_aggregate_total(
        len(normalized_candidate),
        len(normalized_reference),
        limit=limits.max_aggregate_normalized_codepoints,
        limit_name="max_aggregate_normalized_codepoints",
    )
    return normalized_candidate, normalized_reference


def whole_output_f1(
    candidate: str,
    reference: str,
    *,
    limits: KernelLimits = DEFAULT_LIMITS,
) -> MultisetScore:
    """Score the complete normalized candidate against one truth multiset."""

    _assert_accidental_drift_canary()
    limits = _validated_limits(limits)
    normalized_candidate, normalized_reference = _normalize_scoring_pair(
        candidate,
        reference,
        limits=limits,
    )
    if len(normalized_reference) < QGRAM_SIZE:
        raise KernelError(f"reference must normalize to at least {QGRAM_SIZE} code points")
    work = _whole_output_work(normalized_candidate, normalized_reference)
    if work > limits.max_whole_output_gram_operations:
        raise BudgetExceeded(
            f"whole-output work {work} exceeds max_whole_output_gram_operations="
            f"{limits.max_whole_output_gram_operations}"
        )
    return _multiset_f1_normalized(normalized_candidate, normalized_reference)


def positional_output_f1(
    candidate: str,
    reference: str,
    *,
    limits: KernelLimits = DEFAULT_LIMITS,
) -> PositionalScore:
    """Score exact q-gram multisets inside fixed relative-position buckets."""

    _assert_accidental_drift_canary()
    limits = _validated_limits(limits)
    normalized_candidate, normalized_reference = _normalize_scoring_pair(
        candidate,
        reference,
        limits=limits,
    )
    if len(normalized_reference) < QGRAM_SIZE:
        raise KernelError(f"reference must normalize to at least {QGRAM_SIZE} code points")
    work = _whole_output_work(normalized_candidate, normalized_reference)
    if work > limits.max_whole_output_gram_operations:
        raise BudgetExceeded(
            f"positional-output work {work} exceeds max_whole_output_gram_operations="
            f"{limits.max_whole_output_gram_operations}"
        )
    return _positional_f1_normalized(normalized_candidate, normalized_reference)


def ordered_output_f1(
    candidate: str,
    reference: str,
    *,
    limits: KernelLimits = DEFAULT_LIMITS,
) -> OrderedScore:
    """Score edit-aligned order agreement over the full normalized candidate."""

    _assert_accidental_drift_canary()
    limits = _validated_limits(limits)
    normalized_candidate, normalized_reference = _normalize_scoring_pair(
        candidate,
        reference,
        limits=limits,
    )
    if len(normalized_reference) < QGRAM_SIZE:
        raise KernelError(f"reference must normalize to at least {QGRAM_SIZE} code points")
    return _ordered_f1_normalized(
        normalized_candidate,
        normalized_reference,
        limits=limits,
    )


def _rounded_ratio_length(reference_length: int, numerator: int, denominator: int) -> int:
    # Positive integer round-half-up; unlike round(), this has no tie-to-even
    # behavior and therefore has an exact language-independent definition.
    return (reference_length * numerator * 2 + denominator) // (2 * denominator)


def _window_lengths(candidate_length: int, reference_length: int) -> tuple[int, ...]:
    if candidate_length < QGRAM_SIZE:
        return ()
    lengths = {
        min(
            candidate_length,
            max(
                QGRAM_SIZE,
                _rounded_ratio_length(reference_length, numerator, denominator),
            ),
        )
        for numerator, denominator in WINDOW_LENGTH_RATIOS
    }
    return tuple(sorted(lengths))


def _is_better_window(
    *,
    overlap: int,
    window_grams: int,
    start: int,
    reference_length: int,
    best_overlap: int,
    best_window_grams: int,
    best_start: int,
) -> bool:
    # F1 simplifies to 2*overlap/(reference_grams+window_grams).  Integer
    # cross-products avoid floating-point tie instability.
    reference_grams = reference_length - QGRAM_SIZE + 1
    left = overlap * (reference_grams + best_window_grams)
    right = best_overlap * (reference_grams + window_grams)
    if left != right:
        return left > right
    if overlap != best_overlap:  # recall, because reference_grams is fixed
        return overlap > best_overlap
    left_precision = overlap * best_window_grams
    right_precision = best_overlap * window_grams
    if left_precision != right_precision:
        return left_precision > right_precision
    window_length = window_grams + QGRAM_SIZE - 1
    best_window_length = best_window_grams + QGRAM_SIZE - 1
    distance = abs(window_length - reference_length)
    best_distance = abs(best_window_length - reference_length)
    if distance != best_distance:
        return distance < best_distance
    if window_length != best_window_length:
        return window_length < best_window_length
    return start < best_start


def _zero_window_score(candidate_length: int, reference_grams: int) -> WindowScore:
    return WindowScore(
        precision=0.0,
        recall=0.0,
        f1=0.0,
        overlap_grams=0,
        candidate_window_grams=max(0, candidate_length - QGRAM_SIZE + 1),
        reference_grams=reference_grams,
        normalized_start=0,
        normalized_end=candidate_length,
    )


def _best_window_f1_normalized(candidate: str, reference: str) -> WindowScore:
    reference_counts = _character_ngrams_unchecked(reference)
    reference_grams = sum(reference_counts.values())
    if reference_grams == 0:
        raise KernelError(f"reference must normalize to at least {QGRAM_SIZE} code points")
    lengths = _window_lengths(len(candidate), len(reference))
    if not lengths:
        return _zero_window_score(len(candidate), reference_grams)

    best_overlap = -1
    best_window_grams = 0
    best_start = 0
    for window_length in lengths:
        window_grams = window_length - QGRAM_SIZE + 1
        counts = _character_ngrams_unchecked(candidate[:window_length])
        overlap = sum((counts & reference_counts).values())
        if best_overlap < 0 or _is_better_window(
            overlap=overlap,
            window_grams=window_grams,
            start=0,
            reference_length=len(reference),
            best_overlap=best_overlap,
            best_window_grams=best_window_grams,
            best_start=best_start,
        ):
            best_overlap = overlap
            best_window_grams = window_grams
            best_start = 0

        for start in range(1, len(candidate) - window_length + 1):
            removed = candidate[start - 1 : start - 1 + QGRAM_SIZE]
            old_count = counts[removed]
            if old_count <= reference_counts.get(removed, 0):
                overlap -= 1
            if old_count == 1:
                del counts[removed]
            else:
                counts[removed] = old_count - 1

            added_at = start + window_length - QGRAM_SIZE
            added = candidate[added_at : added_at + QGRAM_SIZE]
            old_count = counts.get(added, 0)
            if old_count < reference_counts.get(added, 0):
                overlap += 1
            counts[added] = old_count + 1

            if _is_better_window(
                overlap=overlap,
                window_grams=window_grams,
                start=start,
                reference_length=len(reference),
                best_overlap=best_overlap,
                best_window_grams=best_window_grams,
                best_start=best_start,
            ):
                best_overlap = overlap
                best_window_grams = window_grams
                best_start = start

    precision = best_overlap / best_window_grams if best_window_grams else 0.0
    recall = best_overlap / reference_grams
    f1 = (2 * best_overlap) / (best_window_grams + reference_grams)
    best_length = best_window_grams + QGRAM_SIZE - 1
    return WindowScore(
        precision=precision,
        recall=recall,
        f1=f1,
        overlap_grams=best_overlap,
        candidate_window_grams=best_window_grams,
        reference_grams=reference_grams,
        normalized_start=best_start,
        normalized_end=best_start + best_length,
    )


def _total_window_qgram_work(candidate: str, references: Sequence[str]) -> int:
    """Charge reference Counter construction plus candidate window q-grams."""

    candidate_grams = max(0, len(candidate) - QGRAM_SIZE + 1)
    return sum(
        max(0, len(reference) - QGRAM_SIZE + 1)
        + len(_window_lengths(len(candidate), len(reference))) * candidate_grams
        for reference in references
    )


def best_window_f1(
    candidate: str,
    reference: str,
    *,
    limits: KernelLimits = DEFAULT_LIMITS,
) -> WindowScore:
    """Score the best deterministic candidate window against one truth."""

    _assert_accidental_drift_canary()
    limits = _validated_limits(limits)
    normalized_candidate, normalized_reference = _normalize_scoring_pair(
        candidate,
        reference,
        limits=limits,
    )
    if len(normalized_reference) < QGRAM_SIZE:
        raise KernelError(f"reference must normalize to at least {QGRAM_SIZE} code points")
    work = _total_window_qgram_work(normalized_candidate, (normalized_reference,))
    if work > limits.max_window_evaluations:
        raise BudgetExceeded(
            f"total q-gram/window work {work} exceeds max_window_evaluations="
            f"{limits.max_window_evaluations}"
        )
    return _best_window_f1_normalized(normalized_candidate, normalized_reference)


def _reduce_ratio(numerator: int, denominator: int) -> tuple[int, int]:
    if denominator <= 0:
        raise KernelError("exact metric ratio denominator must be positive")
    if numerator == 0:
        return 0, 1
    divisor = math.gcd(abs(numerator), denominator)
    return numerator // divisor, denominator // divisor


def _window_f1_ratio(score: WindowScore) -> tuple[int, int]:
    return _reduce_ratio(
        2 * score.overlap_grams,
        score.candidate_window_grams + score.reference_grams,
    )


def _multiset_f1_ratio(score: MultisetScore) -> tuple[int, int]:
    if score.candidate_grams == 0:
        return 0, 1
    return _reduce_ratio(
        2 * score.overlap_grams,
        score.candidate_grams + score.reference_grams,
    )


def _positional_f1_ratio(score: PositionalScore) -> tuple[int, int]:
    if score.candidate_grams == 0:
        return 0, 1
    return _reduce_ratio(
        2 * score.overlap_grams,
        score.candidate_grams + score.reference_grams,
    )


def _ordered_f1_ratio(score: OrderedScore) -> tuple[int, int]:
    if score.candidate_grams == 0:
        return 0, 1
    return _reduce_ratio(
        2 * score.overlap_grams,
        score.candidate_grams + score.reference_grams,
    )


def _harmonic_ratio(*ratios: tuple[int, int]) -> tuple[int, int]:
    if not ratios:
        raise KernelError("harmonic ratio requires at least one input")
    if any(numerator == 0 for numerator, _ in ratios):
        return 0, 1
    numerator_product = math.prod(numerator for numerator, _ in ratios)
    denominator = sum(
        ratio_denominator * (numerator_product // ratio_numerator)
        for ratio_numerator, ratio_denominator in ratios
    )
    return _reduce_ratio(
        len(ratios) * numerator_product,
        denominator,
    )


@dataclass(frozen=True, slots=True)
class ExampleScore:
    truth: WindowScore
    truth_whole_output: MultisetScore
    truth_positional: PositionalScore
    truth_ordered: OrderedScore
    truth_quality: float
    truth_quality_numerator: int
    truth_quality_denominator: int
    selected_lie: WindowScore
    lie_leakage_f1: float
    lie_index: int
    lie_leakage_mode: Literal["normal"]
    normal_lie: WindowScore
    normal_lie_leakage_f1: float
    normal_lie_index: int
    internal_skeleton_lie: WindowScore
    internal_skeleton_lie_leakage_f1: float
    internal_skeleton_lie_index: int
    discriminative_margin: float
    discriminative_margin_numerator: int
    discriminative_margin_denominator: int
    lie_rejection: float
    joint_utility: float
    joint_utility_numerator: int
    joint_utility_denominator: int
    candidate_normalized_codepoints: int
    candidate_empty: bool

    def to_document(self) -> dict[str, JsonValue]:
        """Return a float-free document suitable for protocol-local hashing."""

        _assert_accidental_drift_canary()
        return {
            "candidate_empty": self.candidate_empty,
            "candidate_normalized_codepoints": self.candidate_normalized_codepoints,
            "artifact_status": "SYNTHETIC_ONLY / NOT_CLAIMABLE",
            "claimable": False,
            "external_attestation": "ABSENT",
            "input_provenance": "caller-supplied-unverified",
            "metric_encoding": "fixed-decimal-12",
            "metrics": {
                "discriminative_margin": _fixed_decimal(self.discriminative_margin),
                "joint_utility": _fixed_decimal(self.joint_utility),
                "lie_leakage_f1": _fixed_decimal(self.lie_leakage_f1),
                "lie_leakage_internal_skeleton_diagnostic_f1": _fixed_decimal(
                    self.internal_skeleton_lie_leakage_f1
                ),
                "lie_leakage_normal_f1": _fixed_decimal(self.normal_lie_leakage_f1),
                "lie_rejection": _fixed_decimal(self.lie_rejection),
                "truth_best_window_f1": _fixed_decimal(self.truth.f1),
                "truth_ordered_f1": _fixed_decimal(self.truth_ordered.f1),
                "truth_positional_f1": _fixed_decimal(self.truth_positional.f1),
                "truth_quality": _fixed_decimal(self.truth_quality),
                "truth_whole_output_f1": _fixed_decimal(self.truth_whole_output.f1),
            },
            "exact_ratios": {
                "discriminative_margin": {
                    "denominator": str(self.discriminative_margin_denominator),
                    "numerator": str(self.discriminative_margin_numerator),
                },
                "joint_utility": {
                    "denominator": str(self.joint_utility_denominator),
                    "numerator": str(self.joint_utility_numerator),
                },
                "truth_quality": {
                    "denominator": str(self.truth_quality_denominator),
                    "numerator": str(self.truth_quality_numerator),
                },
            },
            "lie_leakage_mode": self.lie_leakage_mode,
            "internal_skeleton_profile": INTERNAL_SKELETON_PROFILE,
            "internal_skeleton_primary_eligible": False,
            "protocol_version": PROTOCOL_VERSION,
            "protocol_manifest_sha256": PROTOCOL_MANIFEST_SHA256,
            "claim_manifest_sha256": CLAIM_MANIFEST_SHA256,
            "normalization_profile": UNICODE_RUNTIME_PROFILE,
            "runtime_build_sha256": RUNTIME_BUILD_SHA256,
            "packaged_source_sha256": SCORER_SOURCE_SHA256,
            "drift_canary_manifest_sha256": DRIFT_CANARY_MANIFEST_SHA256,
            "synthetic_fixture_sha256": SYNTHETIC_FIXTURE_SHA256,
            "synthetic_fixture_source_sha256": SYNTHETIC_FIXTURE_SOURCE_SHA256,
            "unicode_confusables_data_source_sha256": UTS39_CONFUSABLES_SOURCE_SHA256,
            "normal_lie_index": self.normal_lie_index,
            "normal_lie_window": self.normal_lie.to_document(),
            "internal_skeleton_lie_index": self.internal_skeleton_lie_index,
            "internal_skeleton_lie_window": self.internal_skeleton_lie.to_document(),
            "selected_lie_index": self.lie_index,
            "selected_lie_window": self.selected_lie.to_document(),
            "truth_positional": self.truth_positional.to_document(),
            "truth_ordered": self.truth_ordered.to_document(),
            "truth_whole_output": self.truth_whole_output.to_document(),
            "truth_window": self.truth.to_document(),
        }


@dataclass(frozen=True, slots=True)
class VerifiedFixtureScore:
    """A score whose inputs were loaded internally from the verified fixture."""

    fixture_id: str
    input_commitment_sha256: str
    score: ExampleScore

    def to_document(self) -> dict[str, JsonValue]:
        _assert_accidental_drift_canary()
        document = self.score.to_document()
        document["input_provenance"] = "verified-synthetic-fixture"
        document["fixture_id"] = self.fixture_id
        document["input_commitment_sha256"] = self.input_commitment_sha256
        return document

    def canonical_artifact_bytes(
        self,
        *,
        limits: KernelLimits = DEFAULT_LIMITS,
    ) -> bytes:
        _assert_accidental_drift_canary()
        return canonical_json_bytes(self.to_document(), limits=limits)


def _score_fraction_greater(left: WindowScore, right: WindowScore) -> bool:
    return left.overlap_grams * (
        right.reference_grams + right.candidate_window_grams
    ) > right.overlap_grams * (left.reference_grams + left.candidate_window_grams)


def _bounded_sequence_items(
    value: Any,
    *,
    label: str,
    maximum: int,
    limit_name: str,
    minimum: int,
) -> tuple[Any, ...]:
    """Materialize at most ``maximum + 1`` actual items without trusting len()."""

    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise KernelError(f"{label} must be a sequence")
    items: list[Any] = []
    for index, item in enumerate(value):
        if index >= maximum:
            raise BudgetExceeded(f"{label} exceeds {limit_name}={maximum}")
        items.append(item)
    if len(items) < minimum:
        if minimum == 1:
            raise KernelError(f"at least one {label.removesuffix('s')} is required")
        raise KernelError(f"at least {minimum} {label} are required")
    return tuple(items)


def _maximum_window_score(scores: Sequence[WindowScore]) -> tuple[int, WindowScore]:
    best_index = 0
    best = scores[0]
    for index, score in enumerate(scores[1:], start=1):
        if _score_fraction_greater(score, best):
            best_index = index
            best = score
    return best_index, best


def _preflight_aggregate_inputs(
    inputs: Sequence[tuple[str, Any]],
    *,
    limits: KernelLimits,
) -> None:
    total_codepoints = 0
    total_utf8_bytes = 0
    for label, value in inputs:
        checked, utf8_bytes = _measure_unicode_string(value, label=label, limits=limits)
        total_codepoints += len(checked)
        total_utf8_bytes += utf8_bytes
        if total_codepoints > limits.max_aggregate_input_codepoints:
            raise BudgetExceeded(
                "score inputs exceed max_aggregate_input_codepoints="
                f"{limits.max_aggregate_input_codepoints}"
            )
        if total_utf8_bytes > limits.max_aggregate_input_utf8_bytes:
            raise BudgetExceeded(
                "score inputs exceed max_aggregate_input_utf8_bytes="
                f"{limits.max_aggregate_input_utf8_bytes}"
            )


def _check_aggregate_total(
    total: int,
    addition: int,
    *,
    limit: int,
    limit_name: str,
) -> int:
    total += addition
    if total > limit:
        raise BudgetExceeded(f"score inputs exceed {limit_name}={limit}")
    return total


def score_example(
    candidate: str,
    truth: str,
    lies: Sequence[str],
    *,
    limits: KernelLimits = DEFAULT_LIMITS,
) -> ExampleScore:
    """Compute truth retention, lie leakage, discrimination, and joint utility."""

    _assert_accidental_drift_canary()
    limits = _validated_limits(limits)
    lie_items = _bounded_sequence_items(
        lies,
        label="lies",
        maximum=limits.max_lies,
        limit_name="max_lies",
        minimum=1,
    )
    _preflight_aggregate_inputs(
        [
            ("candidate", candidate),
            ("truth", truth),
            *[(f"lies[{index}]", lie) for index, lie in enumerate(lie_items)],
        ],
        limits=limits,
    )

    normalized_candidate = normalize_visible_text(candidate, label="candidate", limits=limits)
    normalized_truth = normalize_visible_text(truth, label="truth", limits=limits)
    aggregate_normalized = _check_aggregate_total(
        len(normalized_candidate),
        len(normalized_truth),
        limit=limits.max_aggregate_normalized_codepoints,
        limit_name="max_aggregate_normalized_codepoints",
    )
    if len(normalized_truth) < QGRAM_SIZE:
        raise KernelError(f"truth must normalize to at least {QGRAM_SIZE} code points")

    normalized_lies: list[str] = []
    for index, lie in enumerate(lie_items):
        normalized = normalize_visible_text(lie, label=f"lies[{index}]", limits=limits)
        if len(normalized) < QGRAM_SIZE:
            raise KernelError(f"lies[{index}] must normalize to at least {QGRAM_SIZE} code points")
        aggregate_normalized = _check_aggregate_total(
            aggregate_normalized,
            len(normalized),
            limit=limits.max_aggregate_normalized_codepoints,
            limit_name="max_aggregate_normalized_codepoints",
        )
        normalized_lies.append(normalized)

    skeleton_candidate = _internal_skeleton_diagnostic_normalized(
        normalized_candidate,
        label="candidate",
        limits=limits,
    )
    aggregate_skeleton = _check_aggregate_total(
        0,
        len(skeleton_candidate),
        limit=limits.max_aggregate_skeleton_codepoints,
        limit_name="max_aggregate_skeleton_codepoints",
    )
    skeleton_lies: list[str] = []
    for index, normalized_lie in enumerate(normalized_lies):
        skeleton_lie = _internal_skeleton_diagnostic_normalized(
            normalized_lie,
            label=f"lies[{index}]",
            limits=limits,
        )
        aggregate_skeleton = _check_aggregate_total(
            aggregate_skeleton,
            len(skeleton_lie),
            limit=limits.max_aggregate_skeleton_codepoints,
            limit_name="max_aggregate_skeleton_codepoints",
        )
        skeleton_lies.append(skeleton_lie)
    references = [normalized_truth, *normalized_lies]
    work = _total_window_qgram_work(normalized_candidate, references)
    for normalized_lie, skeleton_lie in zip(
        normalized_lies,
        skeleton_lies,
        strict=True,
    ):
        if skeleton_candidate != normalized_candidate or skeleton_lie != normalized_lie:
            work += _total_window_qgram_work(skeleton_candidate, (skeleton_lie,))
    if work > limits.max_window_evaluations:
        raise BudgetExceeded(
            f"total q-gram/window work {work} exceeds max_window_evaluations="
            f"{limits.max_window_evaluations}"
        )

    whole_output_work = _whole_output_work(normalized_candidate, normalized_truth)
    if whole_output_work > limits.max_whole_output_gram_operations:
        raise BudgetExceeded(
            f"whole-output work {whole_output_work} exceeds "
            "max_whole_output_gram_operations="
            f"{limits.max_whole_output_gram_operations}"
        )

    truth_score = _best_window_f1_normalized(normalized_candidate, normalized_truth)
    truth_whole_output = _multiset_f1_normalized(normalized_candidate, normalized_truth)
    truth_positional = _positional_f1_normalized(
        normalized_candidate,
        normalized_truth,
    )
    truth_ordered = _ordered_f1_normalized(
        normalized_candidate,
        normalized_truth,
        limits=limits,
    )
    best_numerator, best_denominator = _window_f1_ratio(truth_score)
    whole_numerator, whole_denominator = _multiset_f1_ratio(truth_whole_output)
    positional_numerator, positional_denominator = _positional_f1_ratio(truth_positional)
    ordered_numerator, ordered_denominator = _ordered_f1_ratio(truth_ordered)
    quality_numerator, quality_denominator = _harmonic_ratio(
        (best_numerator, best_denominator),
        (whole_numerator, whole_denominator),
        (positional_numerator, positional_denominator),
        (ordered_numerator, ordered_denominator),
    )
    truth_quality = quality_numerator / quality_denominator
    normal_lie_scores = [
        _best_window_f1_normalized(normalized_candidate, lie) for lie in normalized_lies
    ]
    internal_skeleton_lie_scores = [
        (
            normal_score
            if (skeleton_candidate == normalized_candidate and skeleton_lie == normalized_lie)
            else _best_window_f1_normalized(skeleton_candidate, skeleton_lie)
        )
        for normal_score, normalized_lie, skeleton_lie in zip(
            normal_lie_scores,
            normalized_lies,
            skeleton_lies,
            strict=True,
        )
    ]
    normal_lie_index, normal_lie = _maximum_window_score(normal_lie_scores)
    (
        internal_skeleton_lie_index,
        internal_skeleton_lie,
    ) = _maximum_window_score(internal_skeleton_lie_scores)
    selected_lie_index = normal_lie_index
    selected_lie = normal_lie
    lie_leakage_mode: Literal["normal"] = "normal"

    leakage_numerator, leakage_denominator = _window_f1_ratio(selected_lie)
    leakage = leakage_numerator / leakage_denominator
    normal_leakage_numerator, normal_leakage_denominator = _window_f1_ratio(normal_lie)
    normal_leakage = normal_leakage_numerator / normal_leakage_denominator
    (
        internal_skeleton_leakage_numerator,
        internal_skeleton_leakage_denominator,
    ) = _window_f1_ratio(internal_skeleton_lie)
    internal_skeleton_leakage = (
        internal_skeleton_leakage_numerator / internal_skeleton_leakage_denominator
    )
    margin_numerator, margin_denominator = _reduce_ratio(
        quality_numerator * leakage_denominator - leakage_numerator * quality_denominator,
        quality_denominator * leakage_denominator,
    )
    margin = margin_numerator / margin_denominator
    rejection_numerator = leakage_denominator - leakage_numerator
    rejection = rejection_numerator / leakage_denominator
    joint_numerator, joint_denominator = _reduce_ratio(
        quality_numerator * rejection_numerator,
        quality_denominator * leakage_denominator,
    )
    joint = joint_numerator / joint_denominator
    return ExampleScore(
        truth=truth_score,
        truth_whole_output=truth_whole_output,
        truth_positional=truth_positional,
        truth_ordered=truth_ordered,
        truth_quality=truth_quality,
        truth_quality_numerator=quality_numerator,
        truth_quality_denominator=quality_denominator,
        selected_lie=selected_lie,
        lie_leakage_f1=leakage,
        lie_index=selected_lie_index,
        lie_leakage_mode=lie_leakage_mode,
        normal_lie=normal_lie,
        normal_lie_leakage_f1=normal_leakage,
        normal_lie_index=normal_lie_index,
        internal_skeleton_lie=internal_skeleton_lie,
        internal_skeleton_lie_leakage_f1=internal_skeleton_leakage,
        internal_skeleton_lie_index=internal_skeleton_lie_index,
        discriminative_margin=margin,
        discriminative_margin_numerator=margin_numerator,
        discriminative_margin_denominator=margin_denominator,
        lie_rejection=rejection,
        joint_utility=joint,
        joint_utility_numerator=joint_numerator,
        joint_utility_denominator=joint_denominator,
        candidate_normalized_codepoints=len(normalized_candidate),
        candidate_empty=not normalized_candidate,
    )


def _fixed_decimal(value: float) -> str:
    if not math.isfinite(value):
        raise KernelError("metric must be finite")
    if value == 0.0:
        value = 0.0
    return f"{value:.12f}"


@dataclass(slots=True)
class _CanonicalBudget:
    nodes: int = 0
    mapping_items: int = 0
    key_codepoints: int = 0
    string_codepoints: int = 0

    def consume_node(self, *, path: str, limits: KernelLimits) -> None:
        self.nodes += 1
        if self.nodes > limits.max_canonical_nodes:
            raise BudgetExceeded(f"{path} exceeds max_canonical_nodes={limits.max_canonical_nodes}")

    def consume_mapping_item(self, *, path: str, limits: KernelLimits) -> None:
        self.mapping_items += 1
        if self.mapping_items > limits.max_canonical_mapping_items:
            raise BudgetExceeded(
                f"{path} exceeds max_canonical_mapping_items={limits.max_canonical_mapping_items}"
            )

    def consume_key(self, key: str, *, path: str, limits: KernelLimits) -> None:
        self.key_codepoints += len(key)
        if self.key_codepoints > limits.max_canonical_key_codepoints:
            raise BudgetExceeded(
                f"{path} exceeds max_canonical_key_codepoints={limits.max_canonical_key_codepoints}"
            )

    def consume_string(self, value: str, *, path: str, limits: KernelLimits) -> None:
        self.string_codepoints += len(value)
        if self.string_codepoints > limits.max_canonical_string_codepoints:
            raise BudgetExceeded(
                f"{path} exceeds max_canonical_string_codepoints="
                f"{limits.max_canonical_string_codepoints}"
            )


def _canonicalize_json(
    value: Any,
    *,
    path: str = "$",
    depth: int = 0,
    active_containers: set[int] | None = None,
    budget: _CanonicalBudget,
    limits: KernelLimits,
) -> JsonValue:
    if depth > _MAX_CANONICAL_JSON_DEPTH:
        raise KernelError(
            f"{path} exceeds maximum canonical JSON depth={_MAX_CANONICAL_JSON_DEPTH}"
        )
    budget.consume_node(path=path, limits=limits)
    if active_containers is None:
        active_containers = set()
    if value is None:
        return None
    if type(value) is bool:
        return bool(value)
    if type(value) is str:
        checked_string = str(value)
        try:
            checked_string.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise KernelError(f"{path} contains an unpaired surrogate") from exc
        budget.consume_string(checked_string, path=path, limits=limits)
        return checked_string
    if type(value) is int:
        if abs(value) > _MAX_SAFE_JSON_INTEGER:
            raise KernelError(f"{path} integer exceeds the interoperable JSON range")
        return value
    if isinstance(value, int):
        raise KernelError(f"{path} contains unsupported JSON type {type(value).__name__}")
    if type(value) is float:
        if not math.isfinite(value):
            raise KernelError(f"{path} contains a non-finite number")
        if value == 0.0 and math.copysign(1.0, value) < 0:
            raise KernelError(f"{path} contains negative zero")
        return value
    if isinstance(value, float):
        raise KernelError(f"{path} contains unsupported JSON type {type(value).__name__}")
    if type(value) is dict:
        identity = id(value)
        if identity in active_containers:
            raise KernelError(f"{path} contains a cyclic object")
        active_containers.add(identity)
        try:
            result: dict[str, JsonValue] = {}
            for key, item in value.items():
                budget.consume_mapping_item(path=path, limits=limits)
                if type(key) is not str:
                    raise KernelError(f"{path} contains a non-string object key")
                try:
                    key.encode("utf-8", errors="strict")
                except UnicodeEncodeError as exc:
                    raise KernelError(
                        f"{path} contains an object key with an unpaired surrogate"
                    ) from exc
                budget.consume_key(key, path=path, limits=limits)
                if key in result:
                    raise KernelError(f"{path} contains a duplicate object key")
                result[key] = _canonicalize_json(
                    item,
                    path=f"{path}.{key}",
                    depth=depth + 1,
                    active_containers=active_containers,
                    budget=budget,
                    limits=limits,
                )
            return result
        finally:
            active_containers.remove(identity)
    if isinstance(value, Mapping):
        raise KernelError(f"{path} contains unsupported JSON type {type(value).__name__}")
    if type(value) is list:
        identity = id(value)
        if identity in active_containers:
            raise KernelError(f"{path} contains a cyclic array")
        active_containers.add(identity)
        try:
            return [
                _canonicalize_json(
                    item,
                    path=f"{path}[{index}]",
                    depth=depth + 1,
                    active_containers=active_containers,
                    budget=budget,
                    limits=limits,
                )
                for index, item in enumerate(value)
            ]
        finally:
            active_containers.remove(identity)
    if isinstance(value, (list, tuple)):
        raise KernelError(f"{path} contains unsupported JSON type {type(value).__name__}")
    raise KernelError(f"{path} contains unsupported JSON type {type(value).__name__}")


def _json_string_encoded_size(value: str) -> int:
    """Return exact UTF-8 byte length of ensure_ascii=False JSON string syntax."""

    size = 2  # opening and closing quotation marks
    for character in value:
        codepoint = ord(character)
        if character in {'"', "\\"} or character in {"\b", "\f", "\n", "\r", "\t"}:
            size += 2
        elif codepoint < 0x20:
            size += 6
        elif codepoint < 0x80:
            size += 1
        elif codepoint < 0x800:
            size += 2
        elif codepoint < 0x10000:
            size += 3
        else:
            size += 4
    return size


def _canonical_json_encoded_size(value: JsonValue) -> int:
    """Preflight exact encoded size without constructing the full serialization."""

    if value is None:
        return 4
    if type(value) is bool:
        return 4 if value else 5
    if type(value) is str:
        return _json_string_encoded_size(value)
    if type(value) is int:
        return len(str(value))
    if type(value) is float:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("ascii")
        )
    if type(value) is list:
        if not value:
            return 2
        return 2 + len(value) - 1 + sum(_canonical_json_encoded_size(item) for item in value)
    if type(value) is dict:
        if not value:
            return 2
        return (
            2
            + len(value)
            - 1
            + sum(
                _json_string_encoded_size(key) + 1 + _canonical_json_encoded_size(item)
                for key, item in value.items()
            )
        )
    raise AssertionError("canonicalization returned an impossible JSON type")


def canonical_json_bytes(
    value: Mapping[str, Any],
    *,
    limits: KernelLimits = DEFAULT_LIMITS,
) -> bytes:
    """Encode protocol-local canonical JSON as compact, sorted UTF-8."""

    limits = _validated_limits(limits)
    if type(value) is not dict:
        raise KernelError("canonical JSON top level must be an exact built-in object")
    canonical = _canonicalize_json(
        value,
        budget=_CanonicalBudget(),
        limits=limits,
    )
    encoded_size = _canonical_json_encoded_size(canonical)
    if encoded_size > limits.max_canonical_output_bytes:
        raise BudgetExceeded(
            f"canonical JSON output exceeds max_canonical_output_bytes="
            f"{limits.max_canonical_output_bytes}"
        )
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) != encoded_size:  # pragma: no cover - stdlib contract canary
        raise RuntimeError("canonical JSON preflight size disagrees with encoder")
    return encoded


def canonical_sha256(
    value: Mapping[str, Any],
    *,
    limits: KernelLimits = DEFAULT_LIMITS,
) -> str:
    return hashlib.sha256(canonical_json_bytes(value, limits=limits)).hexdigest()


@dataclass(frozen=True, slots=True)
class PairedObservation:
    unit_id: str
    left: float
    right: float
    cluster_id: str
    language_group: str


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    metric_id: str
    direction: Direction
    pairs: int
    clusters: int
    language_groups: int
    samples: int
    seed: int
    oriented_mean_delta: float
    ci95_low: float
    ci95_high: float
    tie_adjusted_win_probability: float
    positive_replicates: int
    tie_replicates: int
    negative_replicates: int
    win_probability_numerator: int
    win_probability_denominator: int
    input_sha256: str
    arm_invariant_input_sha256: str
    resample_design_sha256: str
    resample_stream_sha256: str

    def to_document(self) -> dict[str, JsonValue]:
        _assert_accidental_drift_canary()
        return {
            "claim_configuration": "synthetic-v5-not-claimable",
            "claim_manifest_sha256": CLAIM_MANIFEST_SHA256,
            "artifact_status": "SYNTHETIC_ONLY / NOT_CLAIMABLE",
            "claimable": False,
            "external_attestation": "ABSENT",
            "ci_method": "language-macro-cluster-percentile-bootstrap-type7.v1",
            "aggregation": "equal-language-then-equal-cluster",
            "arm_invariant_input_sha256": self.arm_invariant_input_sha256,
            "clusters": self.clusters,
            "direction": self.direction,
            "input_sha256": self.input_sha256,
            "language_groups": self.language_groups,
            "metric_encoding": "fixed-decimal-12",
            "metric_id": self.metric_id,
            "normalization_profile": UNICODE_RUNTIME_PROFILE,
            "oriented_ci95": [
                _fixed_decimal(self.ci95_low),
                _fixed_decimal(self.ci95_high),
            ],
            "oriented_mean_delta": _fixed_decimal(self.oriented_mean_delta),
            "pairs": self.pairs,
            "protocol_version": PROTOCOL_VERSION,
            "protocol_manifest_sha256": PROTOCOL_MANIFEST_SHA256,
            "runtime_build_sha256": RUNTIME_BUILD_SHA256,
            "packaged_source_sha256": SCORER_SOURCE_SHA256,
            "drift_canary_manifest_sha256": DRIFT_CANARY_MANIFEST_SHA256,
            "resample_prng": "sha3-shake256-domain-draw-attempt-64-rejection.v2",
            "resample_design_sha256": self.resample_design_sha256,
            "resample_stream_sha256": self.resample_stream_sha256,
            "samples": self.samples,
            "seed": self.seed,
            "synthetic_fixture_sha256": SYNTHETIC_FIXTURE_SHA256,
            "synthetic_fixture_source_sha256": SYNTHETIC_FIXTURE_SOURCE_SHA256,
            "unicode_confusables_data_source_sha256": UTS39_CONFUSABLES_SOURCE_SHA256,
            "tie_adjusted_win_probability": _fixed_decimal(self.tie_adjusted_win_probability),
            "win_evidence": {
                "negative_replicates": self.negative_replicates,
                "positive_replicates": self.positive_replicates,
                "tie_replicates": self.tie_replicates,
                "tie_adjusted_probability": {
                    "denominator": str(self.win_probability_denominator),
                    "numerator": str(self.win_probability_numerator),
                },
            },
        }


def _validate_unit_id(unit_id: Any) -> str:
    if type(unit_id) is not str or not _UNIT_ID.fullmatch(unit_id):
        raise KernelError("unit_id must match ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    return unit_id


def _validate_bootstrap_value(value: Any, *, label: str) -> float:
    if type(value) not in {int, float}:
        raise KernelError(f"{label} must be a finite number")
    if type(value) is int and abs(value) > int(_MAX_ABS_BOOTSTRAP_VALUE):
        raise KernelError(
            f"{label} must be finite with absolute value <= {_MAX_ABS_BOOTSTRAP_VALUE:g}"
        )
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise KernelError(f"{label} cannot be represented as a bounded binary64 number") from exc
    if not math.isfinite(number) or abs(number) > _MAX_ABS_BOOTSTRAP_VALUE:
        raise KernelError(
            f"{label} must be finite with absolute value <= {_MAX_ABS_BOOTSTRAP_VALUE:g}"
        )
    return 0.0 if number == 0.0 else number


def _percentile_type7(values: Sequence[float], probability: float) -> float:
    if not values:
        raise KernelError("cannot calculate a percentile of no values")
    position = probability * (len(values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    weight = position - lower
    return values[lower] * (1.0 - weight) + values[upper] * weight


def _percentile_type7_fraction(
    values: Sequence[float],
    numerator: int,
    denominator: int,
) -> float:
    """Type-7 quantile with exact complementary ranks for arm antisymmetry."""

    if not values:
        raise KernelError("cannot calculate a percentile of no values")
    scaled_position = numerator * (len(values) - 1)
    lower, remainder = divmod(scaled_position, denominator)
    if remainder == 0:
        return values[lower]
    upper = lower + 1
    return (
        math.fsum(
            (
                values[lower] * (denominator - remainder),
                values[upper] * remainder,
            )
        )
        / denominator
    )


def _uniform_rejection_draw(
    *,
    domain: bytes,
    replicate: int,
    draw_ordinal: int,
    upper_bound: int,
    stream_hasher: Any,
) -> int:
    """Return an unbiased deterministic draw while hashing every consumed block."""

    acceptance_limit = ((1 << 64) // upper_bound) * upper_bound
    for attempt in range(_MAX_BOOTSTRAP_REJECTION_ATTEMPTS):
        material = (
            domain
            + replicate.to_bytes(8, "big")
            + draw_ordinal.to_bytes(8, "big")
            + attempt.to_bytes(2, "big")
        )
        block = hashlib.shake_256(material).digest(8)
        stream_hasher.update(block)
        value = int.from_bytes(block, "big")
        if value < acceptance_limit:
            return value % upper_bound
    raise BudgetExceeded(
        f"bootstrap rejection sampler exceeded {_MAX_BOOTSTRAP_REJECTION_ATTEMPTS} attempts"
    )


def paired_bootstrap(
    observations: Sequence[PairedObservation],
    *,
    metric_id: str,
    limits: KernelLimits = DEFAULT_LIMITS,
) -> BootstrapResult:
    """Return the frozen, stratified cluster-aware claim bootstrap.

    Rows are macro-averaged equally by language group and then by independent
    cluster. Clusters are resampled within language groups. The resampling
    domain binds only the paired design—not ordered arm outcomes—so swapping
    arm labels uses byte-identical draws and produces an antisymmetric result.
    """

    _assert_accidental_drift_canary()
    limits = _validated_limits(limits)
    normalized_observations = _bounded_sequence_items(
        observations,
        label="paired observations",
        maximum=limits.max_bootstrap_pairs,
        limit_name="max_bootstrap_pairs",
        minimum=2,
    )
    if (
        type(metric_id) is not str
        or not _METRIC_ID.fullmatch(metric_id)
        or metric_id not in METRIC_REGISTRY
    ):
        raise KernelError("metric_id must name a pre-registered v5 metric")
    registered_direction = METRIC_REGISTRY[metric_id]["direction"]
    metric_minimum, metric_maximum = _METRIC_BOUNDS[metric_id]
    direction: Direction = "higher" if registered_direction == "higher" else "lower"
    samples = CLAIM_BOOTSTRAP_SAMPLES
    seed = CLAIM_BOOTSTRAP_SEED
    if samples > limits.max_bootstrap_samples:
        raise BudgetExceeded(
            f"samples exceeds max_bootstrap_samples={limits.max_bootstrap_samples}"
        )
    normalized: list[tuple[str, str, str, float, float]] = []
    seen: set[str] = set()
    for index, observation in enumerate(normalized_observations):
        if type(observation) is not PairedObservation:
            raise KernelError(f"observations[{index}] must be PairedObservation")
        unit_id = _validate_unit_id(observation.unit_id)
        if unit_id in seen:
            raise KernelError(f"duplicate paired unit_id: {unit_id}")
        seen.add(unit_id)
        cluster_id = _validate_unit_id(observation.cluster_id)
        language_group = _validate_unit_id(observation.language_group)
        if language_group.casefold() == "und":
            raise KernelError("language_group must be explicit and cannot be und")
        left = _validate_bootstrap_value(observation.left, label=f"{unit_id}.left")
        right = _validate_bootstrap_value(observation.right, label=f"{unit_id}.right")
        if not metric_minimum <= left <= metric_maximum:
            raise KernelError(
                f"{unit_id}.left must be in the registered metric range "
                f"[{metric_minimum:g}, {metric_maximum:g}]"
            )
        if not metric_minimum <= right <= metric_maximum:
            raise KernelError(
                f"{unit_id}.right must be in the registered metric range "
                f"[{metric_minimum:g}, {metric_maximum:g}]"
            )
        normalized.append((language_group, cluster_id, unit_id, left, right))
    normalized.sort(key=lambda row: (row[0], row[1], row[2]))

    language_to_clusters: dict[str, dict[str, list[tuple[str, float, float]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    cluster_languages: dict[str, str] = {}
    for language_group, cluster_id, unit_id, left, right in normalized:
        previous_language = cluster_languages.setdefault(cluster_id, language_group)
        if previous_language != language_group:
            raise KernelError(
                f"cluster_id {cluster_id} spans multiple language groups; "
                "v5 requires one sealed language group per cluster"
            )
        language_to_clusters[language_group][cluster_id].append((unit_id, left, right))
    language_count = len(language_to_clusters)
    cluster_count = sum(len(clusters) for clusters in language_to_clusters.values())
    if language_count > limits.max_bootstrap_language_groups:
        raise BudgetExceeded(
            "language groups exceed max_bootstrap_language_groups="
            f"{limits.max_bootstrap_language_groups}"
        )
    if cluster_count < 2:
        raise KernelError("at least 2 independent clusters are required")
    singleton_languages = sorted(
        language_group
        for language_group, clusters in language_to_clusters.items()
        if len(clusters) < 2
    )
    if singleton_languages:
        raise KernelError(
            "at least 2 independent clusters are required per language group: "
            + ",".join(singleton_languages)
        )
    if cluster_count > limits.max_bootstrap_clusters:
        raise BudgetExceeded(
            f"clusters exceed max_bootstrap_clusters={limits.max_bootstrap_clusters}"
        )
    draws = samples * cluster_count
    if draws > limits.max_bootstrap_draws:
        raise BudgetExceeded(
            f"bootstrap draws {draws} exceed max_bootstrap_draws={limits.max_bootstrap_draws}"
        )

    input_document: dict[str, JsonValue] = {
        "aggregation": "equal-language-then-equal-cluster",
        "direction": direction,
        "metric_id": metric_id,
        "pairs": [
            {
                "cluster_id": cluster_id,
                "language_group": language_group,
                "left_binary64": left.hex(),
                "right_binary64": right.hex(),
                "unit_id": unit_id,
            }
            for language_group, cluster_id, unit_id, left, right in normalized
        ],
        "protocol_version": PROTOCOL_VERSION,
        "samples": samples,
        "seed": seed,
    }
    input_sha256 = canonical_sha256(input_document, limits=limits)
    arm_invariant_document: dict[str, JsonValue] = {
        **input_document,
        "pairs": [
            {
                "arm_values_binary64": cast(
                    "list[JsonValue]",
                    sorted((left.hex(), right.hex())),
                ),
                "cluster_id": cluster_id,
                "language_group": language_group,
                "unit_id": unit_id,
            }
            for language_group, cluster_id, unit_id, left, right in normalized
        ],
    }
    arm_invariant_input_sha256 = canonical_sha256(arm_invariant_document, limits=limits)
    design_document: dict[str, JsonValue] = {
        "aggregation": "equal-language-then-equal-cluster",
        "clusters": [
            {
                "cluster_id": cluster_id,
                "language_group": language_group,
                "unit_ids": [
                    unit_id for unit_id, _, _ in language_to_clusters[language_group][cluster_id]
                ],
            }
            for language_group in sorted(language_to_clusters)
            for cluster_id in sorted(language_to_clusters[language_group])
        ],
        "metric_id": metric_id,
        "protocol_version": PROTOCOL_VERSION,
        "samples": samples,
        "seed": seed,
    }
    resample_design_sha256 = canonical_sha256(design_document, limits=limits)
    sign = 1.0 if direction == "higher" else -1.0
    language_cluster_deltas: list[list[float]] = []
    for language_group in sorted(language_to_clusters):
        cluster_deltas: list[float] = []
        for cluster_id in sorted(language_to_clusters[language_group]):
            units = language_to_clusters[language_group][cluster_id]
            unit_deltas = [sign * (left - right) for _, left, right in units]
            cluster_deltas.append(math.fsum(unit_deltas) / len(unit_deltas))
        language_cluster_deltas.append(cluster_deltas)
    pair_count = len(normalized)
    point_language_means = [
        math.fsum(cluster_deltas) / len(cluster_deltas)
        for cluster_deltas in language_cluster_deltas
    ]
    oriented_mean_delta = math.fsum(point_language_means) / language_count
    replicate_means: list[float] = []
    stream_hasher = hashlib.sha256()
    domain = (
        PROTOCOL_VERSION.encode("ascii")
        + b"\0language-cluster-bootstrap\0"
        + resample_design_sha256.encode("ascii")
    )
    for replicate in range(samples):
        draw_ordinal = 0
        replicate_language_means: list[float] = []
        for cluster_deltas in language_cluster_deltas:
            sampled: list[float] = []
            cluster_total = len(cluster_deltas)
            for _ in range(cluster_total):
                selected = _uniform_rejection_draw(
                    domain=domain,
                    replicate=replicate,
                    draw_ordinal=draw_ordinal,
                    upper_bound=cluster_total,
                    stream_hasher=stream_hasher,
                )
                draw_ordinal += 1
                sampled.append(cluster_deltas[selected])
            replicate_language_means.append(math.fsum(sampled) / cluster_total)
        replicate_means.append(math.fsum(replicate_language_means) / language_count)

    replicate_means.sort()
    positives = sum(value > 0.0 for value in replicate_means)
    ties = sum(value == 0.0 for value in replicate_means)
    negatives = samples - positives - ties
    win_probability_numerator = 2 * positives + ties
    win_probability_denominator = 2 * samples
    return BootstrapResult(
        metric_id=metric_id,
        direction=direction,
        pairs=pair_count,
        clusters=cluster_count,
        language_groups=language_count,
        samples=samples,
        seed=seed,
        oriented_mean_delta=oriented_mean_delta,
        ci95_low=_percentile_type7_fraction(replicate_means, 1, 40),
        ci95_high=_percentile_type7_fraction(replicate_means, 39, 40),
        tie_adjusted_win_probability=win_probability_numerator / win_probability_denominator,
        positive_replicates=positives,
        tie_replicates=ties,
        negative_replicates=negatives,
        win_probability_numerator=win_probability_numerator,
        win_probability_denominator=win_probability_denominator,
        input_sha256=input_sha256,
        arm_invariant_input_sha256=arm_invariant_input_sha256,
        resample_design_sha256=resample_design_sha256,
        resample_stream_sha256=stream_hasher.hexdigest(),
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError(f"synthetic fixture contains duplicate key: {key}")
        result[key] = value
    return result


def _runtime_fixture_document() -> tuple[str, str, dict[str, Any]]:
    """Re-read and validate fixture bytes, canonical meaning, and root type."""

    path = Path(__file__).with_name("synthetic_fixtures.json")
    try:
        source = path.read_bytes()
    except OSError as exc:
        raise RuntimeError("synthetic fixture bytes are required at artifact time") from exc
    source_sha256 = hashlib.sha256(source).hexdigest()
    if source_sha256 != SYNTHETIC_FIXTURE_SOURCE_SHA256:
        raise RuntimeError("synthetic fixture source changed after scorer import")
    try:
        document = json.loads(
            source.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("synthetic fixture must be strict UTF-8 JSON") from exc
    if type(document) is not dict or document.get("fixture_schema") != SYNTHETIC_FIXTURE_SCHEMA:
        raise RuntimeError("synthetic fixture schema does not match protocol v5")
    canonical = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    canonical_sha256 = hashlib.sha256(canonical).hexdigest()
    if canonical_sha256 != SYNTHETIC_FIXTURE_SHA256:
        raise RuntimeError("synthetic fixture canonical digest does not match protocol v5")
    return source_sha256, canonical_sha256, document


def _runtime_fixture_identity() -> tuple[str, str]:
    source_sha256, canonical_sha256, _ = _runtime_fixture_document()
    return source_sha256, canonical_sha256


def score_verified_fixture(
    fixture_id: str,
    *,
    limits: KernelLimits = DEFAULT_LIMITS,
) -> VerifiedFixtureScore:
    """Load one verified synthetic fixture by ID and bind its canonical inputs."""

    _assert_accidental_drift_canary()
    limits = _validated_limits(limits)
    fixture_id = _validate_unit_id(fixture_id)
    _, _, document = _runtime_fixture_document()
    fixtures = document.get("fixtures")
    if type(fixtures) is not list:
        raise RuntimeError("synthetic fixture document must contain an exact fixture array")
    selected: dict[str, Any] | None = None
    seen_ids: set[str] = set()
    for index, fixture in enumerate(fixtures):
        if type(fixture) is not dict or set(fixture) != {"candidate", "id", "lies", "truth"}:
            raise RuntimeError(f"synthetic fixture at index {index} has invalid structure")
        observed_id = fixture.get("id")
        if type(observed_id) is not str or not _UNIT_ID.fullmatch(observed_id):
            raise RuntimeError(f"synthetic fixture at index {index} has invalid id")
        if observed_id in seen_ids:
            raise RuntimeError(f"duplicate synthetic fixture id: {observed_id}")
        seen_ids.add(observed_id)
        if observed_id == fixture_id:
            selected = fixture
    if selected is None:
        raise KernelError(f"unknown verified synthetic fixture id: {fixture_id}")
    candidate = selected["candidate"]
    truth = selected["truth"]
    lies = selected["lies"]
    if type(candidate) is not str or type(truth) is not str or type(lies) is not list:
        raise RuntimeError(f"synthetic fixture {fixture_id} contains invalid input types")
    if any(type(lie) is not str for lie in lies):
        raise RuntimeError(f"synthetic fixture {fixture_id} contains a non-string lie")
    commitment_document: dict[str, Any] = {
        "fixture": selected,
        "fixture_schema": SYNTHETIC_FIXTURE_SCHEMA,
    }
    input_commitment_sha256 = canonical_sha256(commitment_document, limits=limits)
    score = score_example(candidate, truth, lies, limits=limits)
    return VerifiedFixtureScore(
        fixture_id=fixture_id,
        input_commitment_sha256=input_commitment_sha256,
        score=score,
    )


def _callable_bindings() -> tuple[tuple[str, FunctionType, Any], ...]:
    """Snapshot module-local function objects and their executed code objects."""

    bindings: list[tuple[str, FunctionType, Any]] = []
    for name, value in sorted(globals().items()):
        if isinstance(value, FunctionType) and value.__module__ == __name__:
            bindings.append((name, value, value.__code__))
        if isinstance(value, type) and value.__module__ == __name__:
            for attribute_name, attribute in sorted(vars(value).items()):
                if isinstance(attribute, FunctionType) and attribute.__module__ == __name__:
                    bindings.append((f"{name}.{attribute_name}", attribute, attribute.__code__))
    return tuple(bindings)


def _semantic_constant_document(value: Any) -> JsonValue:
    if value is None or type(value) in {bool, int, str}:
        return cast("JsonValue", value)
    if type(value) is KernelLimits:
        return {
            "kernel_limits": {
                "max_aggregate_input_codepoints": value.max_aggregate_input_codepoints,
                "max_aggregate_input_utf8_bytes": value.max_aggregate_input_utf8_bytes,
                "max_aggregate_normalized_codepoints": value.max_aggregate_normalized_codepoints,
                "max_aggregate_skeleton_codepoints": value.max_aggregate_skeleton_codepoints,
                "max_bootstrap_clusters": value.max_bootstrap_clusters,
                "max_bootstrap_draws": value.max_bootstrap_draws,
                "max_bootstrap_language_groups": value.max_bootstrap_language_groups,
                "max_bootstrap_pairs": value.max_bootstrap_pairs,
                "max_bootstrap_samples": value.max_bootstrap_samples,
                "max_canonical_key_codepoints": value.max_canonical_key_codepoints,
                "max_canonical_mapping_items": value.max_canonical_mapping_items,
                "max_canonical_nodes": value.max_canonical_nodes,
                "max_canonical_output_bytes": value.max_canonical_output_bytes,
                "max_canonical_string_codepoints": value.max_canonical_string_codepoints,
                "max_input_codepoints": value.max_input_codepoints,
                "max_input_utf8_bytes": value.max_input_utf8_bytes,
                "max_lies": value.max_lies,
                "max_normalized_codepoints": value.max_normalized_codepoints,
                "max_ordered_lcs_word_operations": value.max_ordered_lcs_word_operations,
                "max_whole_output_gram_operations": value.max_whole_output_gram_operations,
                "max_window_evaluations": value.max_window_evaluations,
            }
        }
    if type(value) is float:
        return {"float_hex": value.hex()}
    if type(value) is complex:
        return {
            "complex_imag_hex": value.imag.hex(),
            "complex_real_hex": value.real.hex(),
        }
    if type(value) is bytes:
        return {"bytes_hex": value.hex()}
    if type(value) is tuple:
        return [_semantic_constant_document(item) for item in value]
    if type(value) is frozenset:
        items = [_semantic_constant_document(item) for item in value]
        items.sort(
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return {"frozenset": items}
    if type(value) is CodeType:
        return {"code": _semantic_code_document(value)}
    if value is Ellipsis:
        return {"singleton": "Ellipsis"}
    if value is NotImplemented:
        return {"singleton": "NotImplemented"}
    raise RuntimeError(f"unsupported code constant type: {type(value).__name__}")


def _semantic_code_document(code: CodeType) -> dict[str, JsonValue]:
    """Serialize execution-semantic code fields without live marshal state."""

    return {
        "argcount": code.co_argcount,
        "cellvars": list(code.co_cellvars),
        "code_hex": code.co_code.hex(),
        "consts": [_semantic_constant_document(item) for item in code.co_consts],
        "exceptiontable_hex": code.co_exceptiontable.hex(),
        "flags": code.co_flags,
        "freevars": list(code.co_freevars),
        "kwonlyargcount": code.co_kwonlyargcount,
        "name": code.co_name,
        "names": list(code.co_names),
        "nlocals": code.co_nlocals,
        "posonlyargcount": code.co_posonlyargcount,
        "qualname": code.co_qualname,
        "stacksize": code.co_stacksize,
        "varnames": list(code.co_varnames),
    }


def _semantic_code_sha256(code: CodeType) -> str:
    return _manifest_sha256(_semantic_code_document(code))


def _semantic_function_configuration_sha256(function: FunctionType) -> str:
    kwdefaults = function.__kwdefaults__ or {}
    document: dict[str, JsonValue] = {
        "defaults": [_semantic_constant_document(value) for value in (function.__defaults__ or ())],
        "kwdefaults": {
            key: _semantic_constant_document(kwdefaults[key]) for key in sorted(kwdefaults)
        },
    }
    return _manifest_sha256(document)


def _callable_manifest_document(
    bindings: Sequence[tuple[str, FunctionType, Any]],
) -> dict[str, JsonValue]:
    return {
        "callables": [
            {
                "semantic_code_sha256": _semantic_code_sha256(code),
                "semantic_configuration_sha256": _semantic_function_configuration_sha256(function),
                "path": path,
                "qualname": function.__qualname__,
            }
            for path, function, code in bindings
        ],
        "authority": "accidental-drift-canary-only",
        "encoding": "stable-semantic-code-and-defaults.v2",
        "module_semantic_code_sha256": _semantic_code_sha256(_MODULE_EXECUTION_CODE),
        "runtime_build_sha256": RUNTIME_BUILD_SHA256,
        "schema": "clusy.drift-canary-manifest.v2",
    }


def _manifest_sha256(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _deep_freeze(value: Any) -> Any:
    if type(value) is dict:
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_deep_freeze(item) for item in value)
    return value


def _deep_thaw_json(value: Any) -> JsonValue:
    if isinstance(value, Mapping):
        return {str(key): _deep_thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw_json(item) for item in value]
    if value is None or type(value) in {bool, int, float, str}:
        return cast("JsonValue", value)
    raise RuntimeError(f"manifest contains unsupported value: {type(value).__name__}")


def protocol_manifest_document() -> dict[str, JsonValue]:
    """Return a mutable JSON snapshot whose canonical hash is the v5 identity."""

    document = _deep_thaw_json(PROTOCOL_MANIFEST)
    if type(document) is not dict:  # pragma: no cover - initialized below
        raise RuntimeError("protocol manifest root must be an object")
    return document


def claim_manifest_document() -> dict[str, JsonValue]:
    """Return a mutable JSON snapshot whose canonical hash is the claim identity."""

    document = _deep_thaw_json(CLAIM_MANIFEST)
    if type(document) is not dict:  # pragma: no cover - initialized below
        raise RuntimeError("claim manifest root must be an object")
    return document


def drift_canary_manifest_document() -> dict[str, JsonValue]:
    """Return the reproducible in-process drift canary, never an attestation."""

    document = _deep_thaw_json(DRIFT_CANARY_MANIFEST)
    if type(document) is not dict:  # pragma: no cover - initialized below
        raise RuntimeError("drift-canary manifest root must be an object")
    return document


def _resolve_callable(path: str) -> FunctionType | None:
    owner_name, separator, attribute_name = path.partition(".")
    if not separator:
        value = globals().get(owner_name)
    else:
        owner = globals().get(owner_name)
        value = getattr(owner, attribute_name, None)
    return value if isinstance(value, FunctionType) else None


def _assert_accidental_drift_canary() -> None:
    """Detect ordinary in-process drift; this is not a security boundary."""

    if (
        _semantic_code_sha256(_MODULE_EXECUTION_CODE)
        != DRIFT_CANARY_MANIFEST["module_semantic_code_sha256"]
    ):
        raise RuntimeError("synthetic scorer module semantic code drifted")
    manifest_entries = {
        cast("str", item["path"]): (
            cast("str", item["semantic_code_sha256"]),
            cast("str", item["semantic_configuration_sha256"]),
        )
        for item in cast("Sequence[Mapping[str, JsonValue]]", DRIFT_CANARY_MANIFEST["callables"])
    }
    for path, expected_function, expected_code in _EXPECTED_CALLABLE_BINDINGS:
        observed = _resolve_callable(path)
        if observed is not expected_function or observed.__code__ is not expected_code:
            raise RuntimeError(f"synthetic scorer callable drifted: {path}")
        code_sha256, configuration_sha256 = manifest_entries[path]
        if _semantic_code_sha256(observed.__code__) != code_sha256:
            raise RuntimeError(f"synthetic scorer semantic code drifted: {path}")
        if _semantic_function_configuration_sha256(observed) != configuration_sha256:
            raise RuntimeError(f"synthetic scorer callable configuration drifted: {path}")
    for name, expected_value in _EXPECTED_SEMANTIC_BINDINGS:
        observed = globals().get(name)
        if observed is not expected_value:
            raise RuntimeError(f"synthetic scorer semantic binding drifted: {name}")
    if _loaded_source_sha256() != SCORER_SOURCE_SHA256:
        raise RuntimeError("synthetic scorer source changed after initialization")
    _runtime_fixture_identity()


_EXPECTED_CALLABLE_BINDINGS: Final = _callable_bindings()
_drift_canary_manifest_document = _callable_manifest_document(_EXPECTED_CALLABLE_BINDINGS)
DRIFT_CANARY_MANIFEST_SHA256 = _manifest_sha256(_drift_canary_manifest_document)
DRIFT_CANARY_MANIFEST = _deep_freeze(_drift_canary_manifest_document)

_claim_manifest_document: dict[str, Any] = {
    "aggregation": {
        "cluster_weighting": "equal",
        "language_group_weighting": "equal",
        "unit_weighting_within_cluster": "equal",
    },
    "artifact_status": ARTIFACT_STATUS,
    "claimable": False,
    "claim_authority": "external-signed-trust-root-required",
    "bootstrap": {
        "ci": "two-sided-percentile-type7",
        "cluster_resampling": "within-language-group",
        "language_and_cluster_metadata": "explicit-required",
        "resample_domain_contains_ordered_arm_outcomes": False,
        "sampling": "uniform-64-bit-rejection",
        "samples": CLAIM_BOOTSTRAP_SAMPLES,
        "seed": CLAIM_BOOTSTRAP_SEED,
    },
    "metrics": {key: dict(value) for key, value in METRIC_REGISTRY.items()},
    "prohibited_uses": [
        "vendor-content-persistence",
        "vendor-content-retention",
        "vendor-output-training",
        "vendor-output-fine-tuning",
        "vendor-output-distillation",
        "vendor-output-calibration",
        "vendor-output-prompt-tuning",
        "vendor-output-scorer-tuning",
        "vendor-output-model-selection",
        "vendor-output-scorer-selection",
        "vendor-win-publication",
    ],
    "schema": "clusy.blind-vendor.claim-policy.v5",
}
CLAIM_MANIFEST_SHA256 = _manifest_sha256(_claim_manifest_document)
CLAIM_MANIFEST = _deep_freeze(_claim_manifest_document)

_protocol_manifest_document: dict[str, Any] = {
    "algorithms": {
        "canonical_json": "preflight-sized-sorted-compact-utf8.v2",
        "confusable_diagnostic": (
            f"unicode-{UTS39_CONFUSABLES_VERSION}-confusables-data-internal-skeleton"
        ),
        "confusable_primary_eligible": False,
        "normalization": "NFKC-casefold-NFKC-visible-default-ignorable-filter.v2",
        "ordered": "full-candidate-bit-parallel-qgram-lcs.v2",
        "positional": f"relative-{POSITION_BUCKETS}-bucket-qgram-multiset.v1",
        "window": "bounded-ratio-exhaustive-start-total-qgram-work.v2",
    },
    "artifact_status": ARTIFACT_STATUS,
    "claimable": False,
    "claim_manifest_sha256": CLAIM_MANIFEST_SHA256,
    "drift_canary_authority": "accidental-drift-only-not-attestation",
    "drift_canary_manifest_sha256": DRIFT_CANARY_MANIFEST_SHA256,
    "external_attestation": "required-and-currently-absent",
    "fixture": {
        "canonical_sha256": SYNTHETIC_FIXTURE_SHA256,
        "schema": SYNTHETIC_FIXTURE_SCHEMA,
        "source_sha256": SYNTHETIC_FIXTURE_SOURCE_SHA256,
    },
    "packaged_source_sha256": SCORER_SOURCE_SHA256,
    "protocol_family": PROTOCOL_FAMILY,
    "runtime_build_sha256": RUNTIME_BUILD_SHA256,
    "schema": "clusy.blind-vendor.protocol-manifest.v5",
    "unicode_runtime_profile": UNICODE_RUNTIME_PROFILE,
    "unicode_confusables_data": {
        "operation": "internalSkeleton-shaped-diagnostic-not-UTS39-skeleton",
        "source_sha256": UTS39_CONFUSABLES_SOURCE_SHA256,
        "version": UTS39_CONFUSABLES_VERSION,
    },
}
PROTOCOL_MANIFEST_SHA256 = _manifest_sha256(_protocol_manifest_document)
PROTOCOL_MANIFEST = _deep_freeze(_protocol_manifest_document)
PROTOCOL_VERSION = (
    f"{PROTOCOL_FAMILY}.{UNICODE_RUNTIME_PROFILE}"
    f".protocol-{PROTOCOL_MANIFEST_SHA256}"
    f".claim-{CLAIM_MANIFEST_SHA256}"
    f".drift-{DRIFT_CANARY_MANIFEST_SHA256}"
    f".source-{SCORER_SOURCE_SHA256}"
    f".fixture-source-{SYNTHETIC_FIXTURE_SOURCE_SHA256}"
    f".fixture-canonical-{SYNTHETIC_FIXTURE_SHA256}"
)
_EXPECTED_SEMANTIC_BINDINGS: Final = tuple(
    (name, globals()[name])
    for name in (
        "ARTIFACT_STATUS",
        "CLAIM_BOOTSTRAP_SAMPLES",
        "CLAIM_BOOTSTRAP_SEED",
        "CLAIM_MANIFEST",
        "CLAIM_MANIFEST_SHA256",
        "DEFAULT_LIMITS",
        "DRIFT_CANARY_MANIFEST",
        "DRIFT_CANARY_MANIFEST_SHA256",
        "INTERNAL_SKELETON_PROFILE",
        "METRIC_REGISTRY",
        "ORDERED_LCS_WORD_BITS",
        "POSITION_BUCKETS",
        "PROTOCOL_FAMILY",
        "PROTOCOL_MANIFEST",
        "PROTOCOL_MANIFEST_SHA256",
        "PROTOCOL_VERSION",
        "QGRAM_SIZE",
        "RUNTIME_BUILD_SHA256",
        "SCORER_SOURCE_SHA256",
        "SYNTHETIC_FIXTURE_SHA256",
        "SYNTHETIC_FIXTURE_SCHEMA",
        "SYNTHETIC_FIXTURE_SOURCE_SHA256",
        "UNICODE_DATA_VERSION",
        "UNICODE_RUNTIME_PROFILE",
        "UTS39_CONFUSABLES_FILENAME",
        "UTS39_CONFUSABLES_VERSION",
        "UTS39_CONFUSABLES_SOURCE_SHA256",
        "WINDOW_LENGTH_RATIOS",
        "_HARD_MAX_AGGREGATE_INPUT_CODEPOINTS",
        "_HARD_MAX_AGGREGATE_INPUT_UTF8_BYTES",
        "_HARD_MAX_AGGREGATE_NORMALIZED_CODEPOINTS",
        "_HARD_MAX_AGGREGATE_SKELETON_CODEPOINTS",
        "_HARD_MAX_BOOTSTRAP_CLUSTERS",
        "_HARD_MAX_BOOTSTRAP_DRAWS",
        "_HARD_MAX_BOOTSTRAP_LANGUAGE_GROUPS",
        "_HARD_MAX_BOOTSTRAP_PAIRS",
        "_HARD_MAX_BOOTSTRAP_SAMPLES",
        "_HARD_MAX_CANONICAL_KEY_CODEPOINTS",
        "_HARD_MAX_CANONICAL_MAPPING_ITEMS",
        "_HARD_MAX_CANONICAL_NODES",
        "_HARD_MAX_CANONICAL_OUTPUT_BYTES",
        "_HARD_MAX_CANONICAL_STRING_CODEPOINTS",
        "_HARD_MAX_INPUT_CODEPOINTS",
        "_HARD_MAX_INPUT_UTF8_BYTES",
        "_HARD_MAX_LIES",
        "_HARD_MAX_NORMALIZED_CODEPOINTS",
        "_HARD_MAX_ORDERED_LCS_WORD_OPERATIONS",
        "_HARD_MAX_WHOLE_OUTPUT_GRAM_OPERATIONS",
        "_HARD_MAX_WINDOW_EVALUATIONS",
        "_MAX_ABS_BOOTSTRAP_VALUE",
        "_MAX_BOOTSTRAP_REJECTION_ATTEMPTS",
        "_MAX_CANONICAL_JSON_DEPTH",
        "_MAX_SAFE_JSON_INTEGER",
        "_METRIC_BOUNDS",
        "_METRIC_ID",
        "_MODULE_EXECUTION_CODE",
        "_UNIT_ID",
        "_UTS39_CONFUSABLE_TRANSLATION",
    )
)
_runtime_fixture_identity()

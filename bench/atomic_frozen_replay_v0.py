"""Independent frozen replay for atomic-overlay claim artifacts.

This module deliberately duplicates the v0 framing, acceptance gates, and
visible-token contract.  It does not import the overlay, extractor, dataset, or
evaluator.  It uses only the separately hash-bound native graph/certificate
primitive to reconstruct each proposal from raw HTML before label access.
Artifact-supplied Markdown is diagnostic equality evidence, never a replay
preimage.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import re
from bisect import bisect_right
from dataclasses import dataclass, replace
from typing import Any

OVERLAY_SCHEMA = "exact-atomic-structure-overlay.v0"
PROPOSAL_SCHEMA = "exact-atomic-structure-overlay.proposal.v0"
MAX_VISIBLE_TOKENS = 500_000
_WHITESPACE_IDENTITY_TOKEN = "\x00clusy-whitespace\x00"
_FENCE_OPEN_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
_ATX_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}(?:\s+|$)")
_LIST_RE = re.compile(r"^\s{0,3}(?:[-+*]|\d{1,9}[.)])\s+")
_GFM_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)
_CANONICAL_TABLE_CELL_RE = re.compile(
    r'<(th|td) data-row="(0|[1-9][0-9]*)" '
    r'data-column="(0|[1-9][0-9]*)"'
    r'(?: scope="([^"]*)")?>([^<>]*)</(th|td)>'
)
_MARKDOWN_ESCAPABLE_ASCII = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
_ENTITY_RE = re.compile(r"&(?:#[xX][0-9A-Fa-f]+|#[0-9]+|[A-Za-z][A-Za-z0-9]+);")
_LINE_BREAK_CHARACTERS = frozenset(
    {
        "\n",
        "\r",
        "\v",
        "\f",
        "\x1c",
        "\x1d",
        "\x1e",
        "\x85",
        "\u2028",
        "\u2029",
    }
)
_HEX_64_RE = re.compile(r"[0-9a-f]{64}")
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
_PROPOSAL_KEYS = frozenset(
    {
        "accepted",
        "atom_kind",
        "candidate_span_end",
        "candidate_span_start",
        "certificate_digest",
        "certificate_base64",
        "certificate_markdown",
        "config_digest",
        "digest_is_authentication",
        "graph_digest",
        "growth_bytes",
        "input_bytes",
        "input_digest",
        "patch_digest",
        "proposal_id",
        "proposed_output_bytes",
        "reason",
        "replacement_bytes",
        "replacement_digest",
        "replacement_markdown",
        "schema_version",
        "selected_id",
        "source_digest",
        "source_order",
        "source_span_digest",
        "source_span_end",
        "source_span_start",
        "structural_score_after",
        "structural_score_before",
        "visible_token_count",
        "visible_token_digest",
    }
)
OBSERVATION_KEYS = frozenset(
    {
        "accepted",
        "applied_proposal_ids",
        "candidate_markdown_sha256",
        "config_digest",
        "decision_digest",
        "digest_is_authentication",
        "enabled",
        "growth_bytes",
        "input_bytes",
        "input_digest",
        "output_bytes",
        "output_digest",
        "output_markdown",
        "proposals",
        "reason",
        "replay",
        "schema_version",
        "source_digest",
        "timing",
        "visible_token_digest",
        "visible_tokens_identical",
    }
)


class FrozenReplayError(RuntimeError):
    """A frozen decision cannot be independently reproduced."""


class _TokenBudgetError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _TokenSpan:
    value: str
    start: int
    end: int
    byte_start: int
    byte_end: int


@dataclass(frozen=True, slots=True)
class _VisibilityPlan:
    mask: str
    ignored_scalars: tuple[bool, ...]
    protected_spans: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class _ExpectedProposal:
    accepted: bool
    atom_kind: str
    candidate_span_end: int | None
    candidate_span_start: int | None
    certificate: bytes
    certificate_markdown: str | None
    graph_digest: str
    reason: str
    replacement_markdown: str | None
    selected_id: str
    source_order: int
    source_span_digest: str
    source_span_end: int | None
    source_span_start: int | None


@dataclass(frozen=True, slots=True)
class FrozenReplayResult:
    """Independently derived replay values; digests remain identities."""

    output_markdown: str
    output_digest: str
    decision_digest: str
    source_digest: str
    input_digest: str
    visible_token_digest: str
    proposal_ids: tuple[str, ...]
    applied_proposal_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _NativeReplayPrimitives:
    create_certificate: Any
    extract_document: Any
    verify_certificate: Any


_NATIVE_REPLAY_PRIMITIVES: _NativeReplayPrimitives | None = None


def bind_native_replay_primitives(native_module: Any) -> None:
    """Bind already identity-checked native builtins once for scorer replay."""

    global _NATIVE_REPLAY_PRIMITIVES
    primitives = _NativeReplayPrimitives(
        create_certificate=native_module.create_local_atomic_selection_certificate_v0_native,
        extract_document=native_module.extract_document_ir_v2_native,
        verify_certificate=(
            native_module.verify_and_replay_local_atomic_selection_certificate_v0_native
        ),
    )
    if (
        _NATIVE_REPLAY_PRIMITIVES is not None
        and primitives != _NATIVE_REPLAY_PRIMITIVES
    ):
        raise FrozenReplayError("native replay primitives were already rebound")
    _NATIVE_REPLAY_PRIMITIVES = primitives


def _native_replay_primitives() -> _NativeReplayPrimitives:
    if _NATIVE_REPLAY_PRIMITIVES is None:
        raise FrozenReplayError(
            "identity-checked native replay primitives were not bound"
        )
    return _NATIVE_REPLAY_PRIMITIVES


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _framed_digest(domain: str, *parts: bytes) -> str:
    digest = hashlib.sha256()
    domain_bytes = domain.encode("ascii")
    digest.update(len(domain_bytes).to_bytes(8, "big"))
    digest.update(domain_bytes)
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


def _text_bytes(value: object, *, maximum: int, field: str) -> bytes:
    if type(value) is not str:
        raise FrozenReplayError(f"{field} is not an exact string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise FrozenReplayError(f"{field} is not strict UTF-8") from error
    if len(encoded) > maximum:
        raise FrozenReplayError(f"{field} exceeds its byte budget")
    return encoded


def _digest(value: object, *, field: str, allow_empty: bool = False) -> str:
    if type(value) is not str or (value != "" and _HEX_64_RE.fullmatch(value) is None):
        raise FrozenReplayError(f"{field} is not a canonical digest")
    if not allow_empty and not value:
        raise FrozenReplayError(f"{field} is empty")
    return value


def _integer(
    value: object,
    *,
    field: str,
    minimum: int = 0,
    maximum: int,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise FrozenReplayError(f"{field} is outside its integer bounds")
    return value


def _optional_offset(value: object, *, field: str, maximum: int) -> int | None:
    if value is None:
        return None
    return _integer(value, field=field, maximum=maximum)


def _raw_token_spans(
    value: str,
    maximum: int,
    *,
    offset_source: str,
    ignored_scalars: tuple[bool, ...],
) -> tuple[_TokenSpan, ...]:
    if len(offset_source) != len(value) or len(ignored_scalars) != len(value):
        raise FrozenReplayError("visible-token mask has inconsistent offsets")
    tokens: list[_TokenSpan] = []
    whitespace_start: int | None = None
    whitespace_byte_start = 0
    whitespace_end = 0
    whitespace_byte_end = 0

    def flush_whitespace() -> None:
        nonlocal whitespace_start
        if whitespace_start is None:
            return
        tokens.append(
            _TokenSpan(
                _WHITESPACE_IDENTITY_TOKEN,
                whitespace_start,
                whitespace_end,
                whitespace_byte_start,
                whitespace_byte_end,
            )
        )
        whitespace_start = None

    source_byte_start = 0
    for source_index, source_character in enumerate(value):
        offset_character = offset_source[source_index]
        source_byte_end = source_byte_start + len(offset_character.encode("utf-8"))
        if ignored_scalars[source_index]:
            source_byte_start = source_byte_end
            continue
        if source_character.isspace():
            if whitespace_start is None:
                whitespace_start = source_index
                whitespace_byte_start = source_byte_start
            whitespace_end = source_index + 1
            whitespace_byte_end = source_byte_end
        else:
            flush_whitespace()
            tokens.append(
                _TokenSpan(
                    source_character,
                    source_index,
                    source_index + 1,
                    source_byte_start,
                    source_byte_end,
                )
            )
        if len(tokens) > maximum:
            raise _TokenBudgetError
        source_byte_start = source_byte_end
    flush_whitespace()
    if len(tokens) > maximum:
        raise _TokenBudgetError
    return tuple(tokens)


def _trim_identity_spans(
    spans: tuple[_TokenSpan, ...],
) -> tuple[_TokenSpan, ...]:
    start = 0
    end = len(spans)
    while start < end and spans[start].value == _WHITESPACE_IDENTITY_TOKEN:
        start += 1
    while end > start and spans[end - 1].value == _WHITESPACE_IDENTITY_TOKEN:
        end -= 1
    return spans[start:end]


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


def _merge_spans(
    spans: list[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
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


def _markdown_visibility_plan(value: str) -> _VisibilityPlan:
    mask = list(value)
    ignored_scalars = [False] * len(value)
    protected: list[tuple[int, int]] = []

    def mask_range(start: int, end: int) -> None:
        for index in range(start, end):
            if mask[index] not in _LINE_BREAK_CHARACTERS:
                ignored_scalars[index] = True

    for match in _ENTITY_RE.finditer(value):
        protected.append((match.start(), match.end()))

    lines: list[tuple[int, int, int, str]] = []
    cursor = 0
    for full_line in value.splitlines(keepends=True):
        body_length = len(full_line)
        while body_length > 0 and full_line[body_length - 1] in _LINE_BREAK_CHARACTERS:
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
                (line_start + marker_index, line_start + destination_cursor)
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
            protected.append((line_start + tag_start, line_start + tag_end))
            html_cursor = tag_end

        inline_code_ranges: list[tuple[int, int]] = []
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
            inline_code_ranges.append((opening, closing + len(marker)))
            protected.append((line_start + opening, line_start + closing + len(marker)))
            code_cursor = closing + len(marker)

        escape_cursor = 0
        while escape_cursor + 1 < len(raw_line):
            if (
                raw_line[escape_cursor] == "\\"
                and raw_line[escape_cursor + 1] in _MARKDOWN_ESCAPABLE_ASCII
                and not any(
                    start <= escape_cursor < end for start, end in inline_code_ranges
                )
            ):
                mask_range(
                    line_start + escape_cursor,
                    line_start + escape_cursor + 1,
                )
                escape_cursor += 2
                continue
            escape_cursor += 1

    if fence_character is not None:
        protected.append((fence_start, len(value)))
    return _VisibilityPlan(
        mask="".join(mask),
        ignored_scalars=tuple(ignored_scalars),
        protected_spans=_merge_spans(protected),
    )


def _visible_tokens(value: str, maximum: int) -> tuple[str, ...]:
    plan = _markdown_visibility_plan(value)
    return tuple(
        token.value
        for token in _trim_identity_spans(
            _raw_token_spans(
                plan.mask,
                maximum,
                offset_source=value,
                ignored_scalars=plan.ignored_scalars,
            )
        )
    )


def _plain_token_spans(value: str, maximum: int) -> tuple[_TokenSpan, ...]:
    return _trim_identity_spans(
        _raw_token_spans(
            value,
            maximum,
            offset_source=value,
            ignored_scalars=(False,) * len(value),
        )
    )


def _plain_tokens(value: str, maximum: int) -> tuple[str, ...]:
    return tuple(token.value for token in _plain_token_spans(value, maximum))


def _token_position_index(
    tokens: tuple[str, ...],
) -> dict[str, tuple[int, ...]]:
    mutable: dict[str, list[int]] = {}
    for index, token in enumerate(tokens):
        mutable.setdefault(token, []).append(index)
    return {token: tuple(positions) for token, positions in mutable.items()}


def _is_identity_word_scalar(value: str) -> bool:
    return (
        len(value) == 1
        and (
            value == "_"
            or value.isalnum()
            or ("a" + value).isidentifier()
        )
    )


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
    output: list[int] = []
    for anchor_position in positions_by_token.get(anchor_token, ()):
        start = anchor_position - anchor_offset
        end = start + len(needle)
        if start < 0 or end > len(haystack):
            continue
        if (
            start > 0
            and _is_identity_word_scalar(haystack[start - 1])
            and _is_identity_word_scalar(needle[0])
        ):
            continue
        if (
            end < len(haystack)
            and _is_identity_word_scalar(needle[-1])
            and _is_identity_word_scalar(haystack[end])
        ):
            continue
        if haystack[start:end] == needle:
            output.append(start)
            if len(output) == limit:
                break
    return tuple(output)


def _expand_full_lines(value: str, start: int, end: int) -> tuple[int, int] | None:
    line_start = value.rfind("\n", 0, start) + 1
    next_newline = value.find("\n", end)
    line_end = len(value) if next_newline < 0 else next_newline
    try:
        leading_tokens = _visible_tokens(value[line_start:start], 16)
        trailing_tokens = _visible_tokens(value[end:line_end], 16)
    except _TokenBudgetError:
        return None
    if leading_tokens or trailing_tokens:
        return None
    return line_start, line_end


def _overlaps_protected_span(
    start: int,
    end: int,
    spans: tuple[tuple[int, int], ...],
) -> bool:
    if start >= end or not spans:
        return False
    starts = tuple(span_start for span_start, _ in spans)
    index = bisect_right(starts, start) - 1
    if index >= 0 and spans[index][1] > start:
        return True
    next_index = index + 1
    return next_index < len(spans) and spans[next_index][0] < end


def _locate_candidate_span(
    candidate: str,
    *,
    candidate_tokens: tuple[str, ...],
    candidate_token_spans: tuple[_TokenSpan, ...],
    candidate_positions: dict[str, tuple[int, ...]],
    protected_spans: tuple[tuple[int, int], ...],
    atom_tokens: tuple[str, ...],
    maximum_atom_tokens: int,
) -> tuple[int, int] | None:
    positions = _indexed_occurrence_positions(
        candidate_tokens,
        candidate_positions,
        atom_tokens,
        2,
    )
    if len(positions) != 1:
        return None
    token_index = positions[0]
    first_token = candidate_token_spans[token_index]
    last_token = candidate_token_spans[token_index + len(atom_tokens) - 1]
    expanded = _expand_full_lines(
        candidate,
        first_token.start,
        last_token.end,
    )
    if expanded is None:
        return None
    char_start, char_end = expanded
    if _overlaps_protected_span(char_start, char_end, protected_spans):
        return None
    if _visible_tokens(candidate[char_start:char_end], maximum_atom_tokens) != atom_tokens:
        return None
    byte_start = first_token.byte_start - len(
        candidate[char_start:first_token.start].encode("utf-8")
    )
    byte_end = last_token.byte_end + len(
        candidate[last_token.end:char_end].encode("utf-8")
    )
    return byte_start, byte_end


def _token_digest(tokens: tuple[str, ...]) -> str:
    return _framed_digest(
        "clusy-atomic-overlay-visible-tokens-v0",
        *(token.encode("utf-8") for token in tokens),
    )


def _canonical_html_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _canonical_html_attribute(value: str) -> str:
    return _canonical_html_text(value).replace('"', "&quot;").replace("'", "&#39;")


def _derive_table_replacement(
    certificate_markdown: str,
    *,
    maximum_rows: int,
    maximum_columns: int,
    maximum_cells: int,
) -> str:
    """Replay the overlay's table renderer from native canonical table output."""

    lines = certificate_markdown.split("\n")
    if (
        len(lines) < 8
        or lines[:2] != ["<table>", "<tbody>"]
        or lines[-2:] != ["</tbody>", "</table>"]
    ):
        raise FrozenReplayError("certificate table wrapper is not canonical")
    rows: list[list[str]] = []
    cursor = 2
    cells_seen = 0
    while cursor < len(lines) - 2:
        if lines[cursor] != "<tr>":
            raise FrozenReplayError("certificate table row open is not canonical")
        cursor += 1
        row_index = len(rows)
        row: list[str] = []
        while cursor < len(lines) - 2 and lines[cursor] != "</tr>":
            match = _CANONICAL_TABLE_CELL_RE.fullmatch(lines[cursor])
            if match is None:
                raise FrozenReplayError("certificate table cell is not canonical")
            (
                tag,
                encoded_row,
                encoded_column,
                encoded_scope,
                encoded_text,
                closing_tag,
            ) = match.groups()
            if (
                tag != closing_tag
                or int(encoded_row) != row_index
                or int(encoded_column) != len(row)
                or (row_index == 0) != (tag == "th")
            ):
                raise FrozenReplayError("certificate table grid is not rectangular")
            decoded_text = html.unescape(encoded_text)
            if _canonical_html_text(decoded_text) != encoded_text:
                raise FrozenReplayError(
                    "certificate table text escaping is noncanonical"
                )
            if encoded_scope is not None:
                decoded_scope = html.unescape(encoded_scope)
                if _canonical_html_attribute(decoded_scope) != encoded_scope:
                    raise FrozenReplayError(
                        "certificate table scope escaping is noncanonical"
                    )
            normalized = " ".join(decoded_text.replace("\x00", "").split())
            row.append(normalized.replace("\\", "\\\\").replace("|", "\\|"))
            cells_seen += 1
            if cells_seen > maximum_cells or len(row) > maximum_columns:
                raise FrozenReplayError("certificate table exceeds frozen bounds")
            cursor += 1
        if cursor >= len(lines) - 2 or lines[cursor] != "</tr>":
            raise FrozenReplayError("certificate table row close is absent")
        if not row:
            raise FrozenReplayError("certificate table contains an empty row")
        rows.append(row)
        if len(rows) > maximum_rows:
            raise FrozenReplayError("certificate table exceeds frozen row bounds")
        cursor += 1
    if (
        cursor != len(lines) - 2
        or len(rows) < 2
        or len(rows[0]) < 2
        or any(len(row) != len(rows[0]) for row in rows)
        or cells_seen != len(rows) * len(rows[0])
    ):
        raise FrozenReplayError("certificate table shape is not exact")
    rendered = ["| " + " | ".join(row) + " |" for row in rows]
    rendered.insert(
        1,
        "| " + " | ".join("---" for _ in rows[0]) + " |",
    )
    return "\n".join(rendered)


def _structural_score(kind: str, value: str) -> int:
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


def _extract_native_document(raw_html: str, config: dict[str, Any]) -> Any:
    try:
        return _native_replay_primitives().extract_document(
            raw_html,
            max_input_bytes=config["max_source_bytes"],
            max_nodes=200_000,
            max_elements=100_000,
            max_text_runs=200_000,
            max_depth=256,
            max_text_run_bytes=min(config["max_source_bytes"], 256 * 1024),
            max_total_text_bytes=min(
                8 * 1024 * 1024,
                config["max_source_bytes"] * 2,
            ),
            max_math_bytes=min(config["max_source_bytes"], 256 * 1024),
            max_table_columns=config["max_table_columns"],
        )
    except (ImportError, RuntimeError, TypeError, ValueError) as error:
        raise FrozenReplayError(
            "hash-bound native graph reconstruction failed"
        ) from error


def _native_document_incomplete_reason(document: Any) -> str | None:
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


def _native_children_by_parent(
    document: Any,
) -> dict[str, tuple[Any, ...]]:
    mutable: dict[str, list[Any]] = {}
    for element in document.elements:
        if element.parent_id is not None:
            mutable.setdefault(element.parent_id, []).append(element)
    return {
        parent_id: tuple(sorted(children, key=lambda item: item.order))
        for parent_id, children in mutable.items()
    }


def _native_descendants(
    element_id: str,
    children_by_parent: dict[str, tuple[Any, ...]],
) -> tuple[Any, ...]:
    output: list[Any] = []
    pending = list(reversed(children_by_parent.get(element_id, ())))
    while pending:
        element = pending.pop()
        output.append(element)
        pending.extend(reversed(children_by_parent.get(element.id, ())))
    return tuple(output)


def _inside_untrusted_landmark(
    element: Any,
    elements_by_id: dict[str, Any],
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
    element: Any,
    elements_by_id: dict[str, Any],
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


def _native_source_tokens(document: Any, maximum: int) -> tuple[str, ...]:
    output: list[str] = []
    for run in sorted(document.text_runs, key=lambda item: item.order):
        run_tokens = _plain_tokens(run.text, maximum)
        if not run_tokens:
            continue
        if output:
            output.append(_WHITESPACE_IDENTITY_TOKEN)
        remaining = maximum - len(output)
        if remaining <= 0 or len(run_tokens) > remaining:
            raise _TokenBudgetError
        output.extend(run_tokens)
        if len(output) > maximum:
            raise _TokenBudgetError
    return tuple(output)


def _native_atom_eligibility_reason(
    *,
    kind: str,
    element: Any,
    descendants: tuple[Any, ...],
    document: Any,
    config: dict[str, Any],
    elements_by_id: dict[str, Any],
    text_runs_by_parent: dict[str, tuple[Any, ...]],
    text_runs_by_id: dict[str, Any],
    tables_by_node: dict[str, Any],
    table_cells_by_table: dict[str, tuple[Any, ...]],
) -> str | None:
    atom_ids = {element.id, *(descendant.id for descendant in descendants)}
    atom_runs = tuple(
        run
        for parent_id in atom_ids
        for run in text_runs_by_parent.get(parent_id, ())
    )
    if any(
        descendant.implicit
        and not (
            kind == "table"
            and descendant.tag == "tbody"
            and descendant.parent_id == element.id
        )
        for descendant in descendants
    ):
        return "parser_repaired_atom"
    if _inside_untrusted_landmark(element, elements_by_id):
        return "untrusted_landmark"
    if _has_atomic_ancestor(element, elements_by_id):
        return "nested_atomic_structure"
    if any(
        descendant.tag in _UNTRUSTED_LANDMARK_TAGS
        or descendant.role in _UNTRUSTED_LANDMARK_ROLES
        for descendant in descendants
    ):
        return "nested_untrusted_landmark"
    if kind == "code":
        if any(
            descendant.tag not in _CODE_DESCENDANT_TAGS
            for descendant in descendants
        ):
            return "complex_code_descendant"
        if not atom_runs or any(
            run.truncated or not run.preserve_whitespace for run in atom_runs
        ):
            return "inexact_code_text"
        code_bytes = "".join(
            run.text for run in sorted(atom_runs, key=lambda item: item.order)
        ).encode("utf-8")
        if len(code_bytes) > config["max_code_bytes"]:
            return "code_byte_budget"
        return None

    table = tables_by_node.get(element.id)
    if table is None or not table.grid_complete:
        return "incomplete_table_grid"
    if (
        table.row_count < 2
        or table.column_count < 2
        or table.row_count > config["max_table_rows"]
        or table.column_count > config["max_table_columns"]
    ):
        return "non_data_table_shape"
    cells = table_cells_by_table.get(table.id, ())
    expected_cells = table.row_count * table.column_count
    if len(cells) != expected_cells or len(cells) > config["max_table_cells"]:
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
    if element.role != "table":
        return "layout_table_role"
    if any(
        descendant.tag == "table" or descendant.tag not in _TABLE_DESCENDANT_TAGS
        for descendant in descendants
    ):
        return "complex_table_descendant"
    for cell in cells:
        cell_runs = [text_runs_by_id.get(run_id) for run_id in cell.text_run_ids]
        if (
            not cell_runs
            or any(run is None or run.truncated for run in cell_runs)
            or not _plain_tokens(
                " ".join(run.text for run in cell_runs if run is not None),
                config["max_atom_tokens"],
            )
        ):
            return "empty_or_inexact_table_cell"
    del document
    return None


def _rejected_expected(
    expected: _ExpectedProposal,
    reason: str,
) -> _ExpectedProposal:
    return replace(
        expected,
        accepted=False,
        candidate_span_end=None,
        candidate_span_start=None,
        certificate=b"",
        certificate_markdown=None,
        graph_digest="",
        reason=reason,
        replacement_markdown=None,
    )


def _derive_native_proposals(
    raw_html: str,
    raw_html_bytes: bytes,
    candidate_text: str,
    candidate: bytes,
    *,
    config: dict[str, Any],
) -> tuple[
    Any,
    tuple[_ExpectedProposal, ...],
    bool,
    str,
    bytes,
]:
    try:
        visibility_plan = _markdown_visibility_plan(candidate_text)
        candidate_token_spans = _trim_identity_spans(
            _raw_token_spans(
                visibility_plan.mask,
                config["max_tokens"],
                offset_source=candidate_text,
                ignored_scalars=visibility_plan.ignored_scalars,
            )
        )
    except _TokenBudgetError:
        return None, (), False, "candidate_token_budget", candidate
    document = _extract_native_document(raw_html, config)
    incomplete_reason = _native_document_incomplete_reason(document)
    if incomplete_reason is not None:
        return document, (), False, incomplete_reason, candidate
    try:
        source_tokens = _native_source_tokens(document, config["max_tokens"])
    except _TokenBudgetError:
        return document, (), False, "source_token_budget", candidate
    candidate_tokens = tuple(token.value for token in candidate_token_spans)
    candidate_positions = _token_position_index(candidate_tokens)
    source_positions = _token_position_index(source_tokens)
    elements_by_id = {element.id: element for element in document.elements}
    children_by_parent = _native_children_by_parent(document)
    text_runs_by_parent_mutable: dict[str, list[Any]] = {}
    for run in document.text_runs:
        text_runs_by_parent_mutable.setdefault(run.parent_id, []).append(run)
    text_runs_by_parent = {
        parent_id: tuple(sorted(runs, key=lambda item: item.order))
        for parent_id, runs in text_runs_by_parent_mutable.items()
    }
    text_runs_by_id = {run.id: run for run in document.text_runs}
    tables_by_node = {table.node_id: table for table in document.tables}
    table_cells_mutable: dict[str, list[Any]] = {}
    for cell in document.table_cells:
        table_cells_mutable.setdefault(cell.table_id, []).append(cell)
    table_cells_by_table = {
        table_id: tuple(
            sorted(
                cells,
                key=lambda item: (item.row_index, item.column_index, item.order),
            )
        )
        for table_id, cells in table_cells_mutable.items()
    }
    atoms = [
        ("code", element)
        if element.tag == "pre"
        else ("table", element)
        for element in sorted(document.elements, key=lambda item: item.order)
        if (
            element.tag == "pre"
            and config["enable_code"]
            or element.tag == "table"
            and config["enable_tables"]
        )
    ]
    if len(atoms) > config["max_atoms"]:
        return document, (), False, "atom_budget", candidate

    native = _native_replay_primitives()

    expected: list[_ExpectedProposal] = []
    total_certificate_bytes = 0
    certificate_budget_exhausted = False
    for kind, element in atoms:
        source_start = element.source_start
        source_end = element.source_end
        base = _ExpectedProposal(
            accepted=False,
            atom_kind=kind,
            candidate_span_end=None,
            candidate_span_start=None,
            certificate=b"",
            certificate_markdown=None,
            graph_digest="",
            reason="unreliable_source_span",
            replacement_markdown=None,
            selected_id=element.id,
            source_order=element.order,
            source_span_digest="",
            source_span_end=source_end,
            source_span_start=source_start,
        )
        if certificate_budget_exhausted:
            expected.append(
                _rejected_expected(base, "total_certificate_byte_budget")
            )
            continue
        if (
            not element.source_span_reliable
            or type(source_start) is not int
            or type(source_end) is not int
            or source_start < 0
            or source_end <= source_start
            or source_end > len(raw_html_bytes)
        ):
            expected.append(base)
            continue
        source_fragment = raw_html_bytes[source_start:source_end]
        try:
            source_fragment.decode("utf-8")
        except UnicodeDecodeError:
            expected.append(base)
            continue
        source_span_digest = _framed_digest(
            "clusy-atomic-overlay-source-span-v0",
            source_fragment,
        )
        base = replace(base, source_span_digest=source_span_digest)
        if b"\x00" in source_fragment or b"\r" in source_fragment:
            expected.append(
                _rejected_expected(base, "noncanonical_source_control")
            )
            continue
        descendants = _native_descendants(element.id, children_by_parent)
        reason = _native_atom_eligibility_reason(
            kind=kind,
            element=element,
            descendants=descendants,
            document=document,
            config=config,
            elements_by_id=elements_by_id,
            text_runs_by_parent=text_runs_by_parent,
            text_runs_by_id=text_runs_by_id,
            tables_by_node=tables_by_node,
            table_cells_by_table=table_cells_by_table,
        )
        if reason is not None:
            expected.append(_rejected_expected(base, reason))
            continue
        try:
            certificate = native.create_certificate(
                document,
                [element.id],
                max_output_bytes=config["max_replacement_bytes"],
            )
            native_replay = (
                native.verify_certificate(
                    document,
                    certificate.encoded,
                    max_output_bytes=config["max_replacement_bytes"],
                )
            )
        except (RuntimeError, TypeError, ValueError):
            expected.append(
                _rejected_expected(base, "certificate_provenance_rejected")
            )
            continue
        certificate_bytes = certificate.encoded
        replacement_markdown = (
            native_replay.markdown
            if kind == "code"
            else _derive_table_replacement(
                native_replay.markdown,
                maximum_rows=config["max_table_rows"],
                maximum_columns=config["max_table_columns"],
                maximum_cells=config["max_table_cells"],
            )
        )
        replacement = replacement_markdown.encode("utf-8")
        if len(certificate_bytes) > config["max_certificate_bytes"]:
            expected.append(
                _rejected_expected(base, "certificate_byte_budget")
            )
            continue
        if not replacement or len(replacement) > config["max_replacement_bytes"]:
            expected.append(
                _rejected_expected(base, "replacement_byte_budget")
            )
            continue
        try:
            atom_tokens = _visible_tokens(
                replacement_markdown,
                config["max_atom_tokens"],
            )
        except _TokenBudgetError:
            expected.append(_rejected_expected(base, "atom_token_budget"))
            continue
        if not atom_tokens:
            expected.append(_rejected_expected(base, "empty_visible_atom"))
            continue
        if (
            len(
                _indexed_occurrence_positions(
                    source_tokens,
                    source_positions,
                    atom_tokens,
                    2,
                )
            )
            != 1
        ):
            expected.append(
                _rejected_expected(base, "ambiguous_source_tokens")
            )
            continue
        candidate_span = _locate_candidate_span(
            candidate_text,
            candidate_tokens=candidate_tokens,
            candidate_token_spans=candidate_token_spans,
            candidate_positions=candidate_positions,
            protected_spans=visibility_plan.protected_spans,
            atom_tokens=atom_tokens,
            maximum_atom_tokens=config["max_atom_tokens"],
        )
        if candidate_span is None:
            expected.append(
                _rejected_expected(
                    base,
                    "ambiguous_or_missing_candidate_span",
                )
            )
            continue
        candidate_start, candidate_end = candidate_span
        candidate_fragment = candidate[candidate_start:candidate_end]
        structural_before = _structural_score(
            kind,
            candidate_fragment.decode("utf-8"),
        )
        structural_after = _structural_score(kind, replacement_markdown)
        if structural_after <= structural_before:
            expected.append(
                _rejected_expected(base, "no_strict_structural_gain")
            )
            continue
        proposed_output_bytes = (
            len(candidate) - len(candidate_fragment) + len(replacement)
        )
        growth_bytes = proposed_output_bytes - len(candidate)
        if (
            growth_bytes > config["max_growth_bytes"]
            or len(replacement) * 1_000
            > max(1, len(candidate_fragment)) * config["max_growth_ratio_milli"]
        ):
            expected.append(_rejected_expected(base, "local_growth_budget"))
            continue
        if proposed_output_bytes > config["max_output_bytes"]:
            expected.append(_rejected_expected(base, "output_byte_budget"))
            continue
        try:
            original_tokens = _visible_tokens(
                candidate_fragment.decode("utf-8"),
                config["max_atom_tokens"],
            )
        except (UnicodeDecodeError, _TokenBudgetError):
            expected.append(
                _rejected_expected(base, "candidate_fragment_token_budget")
            )
            continue
        if original_tokens != atom_tokens:
            expected.append(
                _rejected_expected(base, "local_visible_token_mismatch")
            )
            continue
        accepted_expected = replace(
            base,
            accepted=True,
            candidate_span_end=candidate_end,
            candidate_span_start=candidate_start,
            certificate=certificate_bytes,
            certificate_markdown=native_replay.markdown,
            graph_digest=certificate.graph_digest,
            reason="accepted",
            replacement_markdown=replacement_markdown,
        )
        next_certificate_bytes = total_certificate_bytes + len(certificate_bytes)
        if next_certificate_bytes > config["max_total_certificate_bytes"]:
            certificate_budget_exhausted = True
            expected.append(
                _rejected_expected(
                    accepted_expected,
                    "total_certificate_byte_budget",
                )
            )
        else:
            total_certificate_bytes = next_certificate_bytes
            expected.append(accepted_expected)

    accepted_by_span = sorted(
        (proposal for proposal in expected if proposal.accepted),
        key=lambda proposal: (
            proposal.candidate_span_start,
            proposal.candidate_span_end,
        ),
    )
    rejected_ids: set[str] = set()
    previous: _ExpectedProposal | None = None
    for proposal in accepted_by_span:
        if (
            previous is not None
            and proposal.candidate_span_start is not None
            and previous.candidate_span_end is not None
            and proposal.candidate_span_start < previous.candidate_span_end
        ):
            rejected_ids.add(previous.selected_id)
            rejected_ids.add(proposal.selected_id)
        previous = proposal
    expected = [
        _rejected_expected(proposal, "candidate_span_overlap")
        if proposal.selected_id in rejected_ids
        else proposal
        for proposal in expected
    ]
    accepted_proposals = [proposal for proposal in expected if proposal.accepted]
    if not accepted_proposals:
        return document, tuple(expected), False, "no_safe_structural_gain", candidate

    derived_output = candidate
    for proposal in sorted(
        accepted_proposals,
        key=lambda item: (
            item.candidate_span_start,
            item.candidate_span_end,
        ),
        reverse=True,
    ):
        if (
            proposal.candidate_span_start is None
            or proposal.candidate_span_end is None
            or proposal.replacement_markdown is None
        ):
            raise FrozenReplayError("native expected proposal is incomplete")
        derived_output = (
            derived_output[: proposal.candidate_span_start]
            + proposal.replacement_markdown.encode("utf-8")
            + derived_output[proposal.candidate_span_end :]
        )
    global_reason: str | None = None
    if len(derived_output) > config["max_output_bytes"]:
        global_reason = "output_byte_budget"
    elif (
        len(derived_output) - len(candidate) > config["max_growth_bytes"]
        or len(derived_output) * 1_000
        > max(1, len(candidate)) * config["max_growth_ratio_milli"]
    ):
        global_reason = "global_growth_budget"
    else:
        try:
            output_tokens = _visible_tokens(
                derived_output.decode("utf-8"),
                config["max_tokens"],
            )
        except (UnicodeDecodeError, _TokenBudgetError):
            global_reason = "output_token_budget"
        else:
            if output_tokens != candidate_tokens:
                global_reason = "global_visible_token_mismatch"
    if global_reason is not None:
        expected = [
            _rejected_expected(proposal, global_reason)
            if proposal.accepted
            else proposal
            for proposal in expected
        ]
        return document, tuple(expected), False, global_reason, candidate
    return document, tuple(expected), True, "accepted", derived_output


def _verify_native_certificate(
    document: Any,
    encoded: bytes,
    *,
    certificate_markdown: str,
    proposal: dict[str, Any],
    replacement_limit: int,
) -> None:
    try:
        replay = _native_replay_primitives().verify_certificate(
            document,
            encoded,
            max_output_bytes=replacement_limit,
        )
    except (ImportError, RuntimeError, TypeError, ValueError) as error:
        raise FrozenReplayError(
            "native certificate replay rejected raw projection"
        ) from error
    receipt = replay.receipt
    if (
        replay.markdown != certificate_markdown
        or receipt.verified is not True
        or receipt.deterministic is not True
        or receipt.validation_scope != "local_atomic"
        or receipt.selection_count != 1
        or receipt.selected_ids != [proposal["selected_id"]]
        or receipt.graph_digest != proposal["graph_digest"]
        or receipt.output_bytes != len(certificate_markdown.encode("utf-8"))
        or receipt.certificate_digest != proposal["certificate_digest"]
    ):
        raise FrozenReplayError(
            "native certificate receipt differs from raw projection"
        )


def _decode_certificate(
    encoded: bytes,
    *,
    raw_html: bytes,
    certificate_markdown: str,
    proposal: dict[str, Any],
    replacement_limit: int,
) -> None:
    if len(encoded) < 128 or len(encoded) > 2 * 1024 * 1024:
        raise FrozenReplayError("certificate byte length is invalid")
    cursor = 0

    def take(length: int) -> bytes:
        nonlocal cursor
        end = cursor + length
        if end > len(encoded):
            raise FrozenReplayError("certificate is truncated")
        value = encoded[cursor:end]
        cursor = end
        return value

    def number(length: int) -> int:
        return int.from_bytes(take(length), "big")

    if take(8) != b"CLSYSCV0" or number(2) != 0 or number(2) != 1:
        raise FrozenReplayError("certificate header/scope is not local-atomic v0")
    source_digest = take(32).hex()
    graph_digest = take(32).hex()
    output_digest = take(32).hex()
    output_bytes = number(8)
    output_limit = number(8)
    selection_count = number(4)
    if selection_count != 1 or output_limit != replacement_limit:
        raise FrozenReplayError("certificate selection/limit is not exact")
    kind = number(1)
    reserved = number(1)
    identifier_length = number(2)
    source_order = number(8)
    source_start = number(8)
    source_end = number(8)
    identifier_bytes = take(identifier_length)
    if cursor != len(encoded):
        raise FrozenReplayError("certificate has trailing bytes")
    try:
        identifier = identifier_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise FrozenReplayError("certificate ID is not UTF-8") from error
    if (
        kind != 1
        or reserved != 0
        or not 0 < identifier_length <= 256
        or not identifier
        or not all(
            character.isascii() and (character.isalnum() or character in "-_")
            for character in identifier
        )
        or source_start >= source_end
    ):
        raise FrozenReplayError("certificate entry is not canonical")
    markdown = _text_bytes(
        certificate_markdown,
        maximum=replacement_limit,
        field="certificate_markdown",
    )
    expected_source = _framed_digest(
        "clusy-selection-certificate-source-v0",
        raw_html,
    )
    if (
        source_digest != expected_source
        or graph_digest != proposal["graph_digest"]
        or identifier != proposal["selected_id"]
        or source_order != proposal["source_order"]
        or source_start != proposal["source_span_start"]
        or source_end != proposal["source_span_end"]
        or output_bytes != len(markdown)
        or output_digest
        != _framed_digest("clusy-selection-certificate-output-v0", markdown)
        or proposal["certificate_digest"]
        != _framed_digest("clusy-selection-certificate-wire-v0", encoded)
    ):
        raise FrozenReplayError("certificate identities do not replay")


def _proposal_canonical(proposal: dict[str, Any]) -> dict[str, Any]:
    return {
        "accepted": proposal["accepted"],
        "atom_kind": proposal["atom_kind"],
        "candidate_span_end": proposal["candidate_span_end"],
        "candidate_span_start": proposal["candidate_span_start"],
        "certificate_digest": proposal["certificate_digest"],
        "config_digest": proposal["config_digest"],
        "graph_digest": proposal["graph_digest"],
        "growth_bytes": proposal["growth_bytes"],
        "input_bytes": proposal["input_bytes"],
        "input_digest": proposal["input_digest"],
        "patch_digest": proposal["patch_digest"],
        "proposed_output_bytes": proposal["proposed_output_bytes"],
        "reason": proposal["reason"],
        "replacement_bytes": proposal["replacement_bytes"],
        "replacement_digest": proposal["replacement_digest"],
        "selected_id": proposal["selected_id"],
        "source_digest": proposal["source_digest"],
        "source_order": proposal["source_order"],
        "source_span_digest": proposal["source_span_digest"],
        "source_span_end": proposal["source_span_end"],
        "source_span_start": proposal["source_span_start"],
        "structural_score_after": proposal["structural_score_after"],
        "structural_score_before": proposal["structural_score_before"],
        "visible_token_count": proposal["visible_token_count"],
        "visible_token_digest": proposal["visible_token_digest"],
    }


def _validate_proposal(
    value: object,
    *,
    expected: _ExpectedProposal,
    native_document: Any,
    raw_html: bytes,
    candidate: bytes,
    overlay_source_digest: str,
    input_digest: str,
    config_digest: str,
    config: dict[str, Any],
) -> tuple[dict[str, Any], bytes | None]:
    if not isinstance(value, dict) or set(value) != _PROPOSAL_KEYS:
        raise FrozenReplayError("proposal schema is not closed")
    proposal = value
    if (
        proposal["schema_version"] != PROPOSAL_SCHEMA
        or type(proposal["accepted"]) is not bool
        or type(proposal["atom_kind"]) is not str
        or proposal["atom_kind"] not in {"code", "table"}
        or type(proposal["selected_id"]) is not str
        or not 0 < len(proposal["selected_id"]) <= 128
        or not all(
            character.isascii() and (character.isalnum() or character in "-_")
            for character in proposal["selected_id"]
        )
        or type(proposal["reason"]) is not str
        or len(proposal["reason"]) > 128
        or proposal["digest_is_authentication"] is not False
    ):
        raise FrozenReplayError("proposal primitive identity is invalid")
    if (
        proposal["accepted"] is not expected.accepted
        or proposal["atom_kind"] != expected.atom_kind
        or proposal["selected_id"] != expected.selected_id
        or proposal["source_order"] != expected.source_order
        or proposal["candidate_span_start"] != expected.candidate_span_start
        or proposal["candidate_span_end"] != expected.candidate_span_end
        or proposal["source_span_start"] != expected.source_span_start
        or proposal["source_span_end"] != expected.source_span_end
        or proposal["source_span_digest"] != expected.source_span_digest
        or proposal["reason"] != expected.reason
    ):
        raise FrozenReplayError(
            "proposal inventory/acceptance differs from raw native graph replay"
        )
    _integer(
        proposal["source_order"],
        field="proposal.source_order",
        maximum=1_000_000_000,
    )
    source_start = _optional_offset(
        proposal["source_span_start"],
        field="proposal.source_span_start",
        maximum=config["max_source_bytes"],
    )
    source_end = _optional_offset(
        proposal["source_span_end"],
        field="proposal.source_span_end",
        maximum=config["max_source_bytes"],
    )
    candidate_start = _optional_offset(
        proposal["candidate_span_start"],
        field="proposal.candidate_span_start",
        maximum=config["max_candidate_bytes"],
    )
    candidate_end = _optional_offset(
        proposal["candidate_span_end"],
        field="proposal.candidate_span_end",
        maximum=config["max_candidate_bytes"],
    )
    if (source_start is None) != (source_end is None) or (candidate_start is None) != (
        candidate_end is None
    ):
        raise FrozenReplayError("proposal span endpoints are only partially present")
    for field in (
        "graph_digest",
        "source_span_digest",
        "replacement_digest",
        "patch_digest",
        "visible_token_digest",
        "certificate_digest",
    ):
        _digest(proposal[field], field=f"proposal.{field}", allow_empty=True)
    _digest(proposal["proposal_id"], field="proposal.proposal_id")
    _digest(proposal["source_digest"], field="proposal.source_digest")
    _digest(proposal["input_digest"], field="proposal.input_digest")
    _digest(proposal["config_digest"], field="proposal.config_digest")
    if (
        proposal["source_digest"] != overlay_source_digest
        or proposal["input_digest"] != input_digest
        or proposal["config_digest"] != config_digest
        or proposal["input_bytes"] != len(candidate)
    ):
        raise FrozenReplayError("proposal source/input/config identity differs")
    _integer(
        proposal["input_bytes"],
        field="proposal.input_bytes",
        maximum=config["max_candidate_bytes"],
    )
    _integer(
        proposal["replacement_bytes"],
        field="proposal.replacement_bytes",
        maximum=config["max_replacement_bytes"],
    )
    _integer(
        proposal["proposed_output_bytes"],
        field="proposal.proposed_output_bytes",
        maximum=config["max_output_bytes"],
    )
    _integer(
        proposal["growth_bytes"],
        field="proposal.growth_bytes",
        minimum=-config["max_output_bytes"],
        maximum=config["max_output_bytes"],
    )
    _integer(
        proposal["visible_token_count"],
        field="proposal.visible_token_count",
        maximum=config["max_atom_tokens"],
    )
    _integer(
        proposal["structural_score_before"],
        field="proposal.structural_score_before",
        maximum=2,
    )
    _integer(
        proposal["structural_score_after"],
        field="proposal.structural_score_after",
        maximum=2,
    )
    certificate_base64 = proposal["certificate_base64"]
    if (
        type(certificate_base64) is not str
        or len(certificate_base64) > ((config["max_certificate_bytes"] + 2) // 3) * 4
    ):
        raise FrozenReplayError("proposal certificate encoding is invalid")
    try:
        certificate = base64.b64decode(certificate_base64, validate=True)
    except (ValueError, binascii.Error) as error:
        raise FrozenReplayError(
            "proposal certificate is not canonical base64"
        ) from error
    if (
        len(certificate) > config["max_certificate_bytes"]
        or base64.b64encode(certificate).decode("ascii") != certificate_base64
    ):
        raise FrozenReplayError("proposal certificate is not canonical base64")
    source_span_digest = proposal["source_span_digest"]
    if source_span_digest and (
        source_start is None
        or source_end is None
        or source_end <= source_start
        or source_end > len(raw_html)
        or source_span_digest
        != _framed_digest(
            "clusy-atomic-overlay-source-span-v0",
            raw_html[source_start:source_end],
        )
    ):
        raise FrozenReplayError("proposal source span digest does not replay")
    expected_proposal_id = _framed_digest(
        "clusy-atomic-overlay-proposal-v0",
        _canonical_json(_proposal_canonical(proposal)),
    )
    if proposal["proposal_id"] != expected_proposal_id:
        raise FrozenReplayError("proposal ID does not replay")

    if proposal["accepted"] is False:
        if (
            expected.accepted
            or proposal["reason"] == "accepted"
            or candidate_start is not None
            or candidate_end is not None
            or certificate
            or proposal["certificate_markdown"] is not None
            or proposal["replacement_markdown"] is not None
            or any(
                proposal[field] != ""
                for field in (
                    "graph_digest",
                    "replacement_digest",
                    "patch_digest",
                    "visible_token_digest",
                    "certificate_digest",
                )
            )
            or any(
                proposal[field] != 0
                for field in (
                    "growth_bytes",
                    "proposed_output_bytes",
                    "replacement_bytes",
                    "structural_score_after",
                    "structural_score_before",
                    "visible_token_count",
                )
            )
        ):
            raise FrozenReplayError(
                "rejected proposal retained an opaque patch payload"
            )
        return proposal, None

    if (
        not expected.accepted
        or proposal["reason"] != "accepted"
        or candidate_start is None
        or candidate_end is None
        or candidate_end <= candidate_start
        or candidate_end > len(candidate)
        or source_start is None
        or source_end is None
        or not source_span_digest
        or proposal["source_digest"] != overlay_source_digest
        or type(proposal["replacement_markdown"]) is not str
        or type(proposal["certificate_markdown"]) is not str
        or not certificate
        or not all(
            proposal[field]
            for field in (
                "graph_digest",
                "replacement_digest",
                "patch_digest",
                "visible_token_digest",
                "certificate_digest",
            )
        )
    ):
        raise FrozenReplayError("accepted proposal payload is incomplete")
    certificate_markdown = expected.certificate_markdown
    derived_replacement = expected.replacement_markdown
    if certificate_markdown is None or derived_replacement is None:
        raise FrozenReplayError("native accepted proposal replay is incomplete")
    if (
        certificate != expected.certificate
        or proposal["graph_digest"] != expected.graph_digest
        or proposal["certificate_markdown"] != certificate_markdown
        or proposal["replacement_markdown"] != derived_replacement
    ):
        raise FrozenReplayError(
            "stored certificate/replacement differs from raw certificate derivation"
        )
    _verify_native_certificate(
        native_document,
        certificate,
        certificate_markdown=certificate_markdown,
        proposal=proposal,
        replacement_limit=config["max_replacement_bytes"],
    )
    replacement = _text_bytes(
        derived_replacement,
        maximum=config["max_replacement_bytes"],
        field="replacement_markdown",
    )
    try:
        candidate_fragment = candidate[candidate_start:candidate_end].decode("utf-8")
    except UnicodeDecodeError as error:
        raise FrozenReplayError("candidate patch span splits UTF-8") from error
    structural_before = _structural_score(
        proposal["atom_kind"],
        candidate_fragment,
    )
    structural_after = _structural_score(
        proposal["atom_kind"],
        derived_replacement,
    )
    if (
        not replacement
        or proposal["replacement_bytes"] != len(replacement)
        or proposal["proposed_output_bytes"]
        != len(candidate) - (candidate_end - candidate_start) + len(replacement)
        or proposal["growth_bytes"]
        != proposal["proposed_output_bytes"] - len(candidate)
        or proposal["growth_bytes"] > config["max_growth_bytes"]
        or len(replacement) * 1_000
        > max(1, candidate_end - candidate_start) * config["max_growth_ratio_milli"]
        or proposal["structural_score_before"] != structural_before
        or proposal["structural_score_after"] != structural_after
        or structural_after <= structural_before
        or proposal["replacement_digest"]
        != _framed_digest(
            "clusy-atomic-overlay-replacement-v0",
            replacement,
        )
        or proposal["patch_digest"]
        != _framed_digest(
            "clusy-atomic-overlay-patch-v0",
            input_digest.encode("ascii"),
            candidate_start.to_bytes(8, "big"),
            candidate_end.to_bytes(8, "big"),
            replacement,
        )
    ):
        raise FrozenReplayError("accepted proposal patch does not replay")
    try:
        replacement_tokens = _visible_tokens(
            derived_replacement,
            config["max_atom_tokens"],
        )
        candidate_tokens = _visible_tokens(
            candidate_fragment,
            config["max_atom_tokens"],
        )
    except (UnicodeDecodeError, UnicodeEncodeError, _TokenBudgetError) as error:
        raise FrozenReplayError(
            "accepted proposal visible tokens are invalid"
        ) from error
    if (
        not replacement_tokens
        or candidate_tokens != replacement_tokens
        or proposal["visible_token_count"] != len(replacement_tokens)
        or proposal["visible_token_digest"] != _token_digest(replacement_tokens)
    ):
        raise FrozenReplayError("accepted proposal visible tokens do not replay")
    _decode_certificate(
        certificate,
        raw_html=raw_html,
        certificate_markdown=proposal["certificate_markdown"],
        proposal=proposal,
        replacement_limit=config["max_replacement_bytes"],
    )
    return proposal, replacement


def replay_frozen_decision(
    raw_html: str,
    baseline_prediction: str,
    observation: object,
    *,
    config: dict[str, Any],
) -> FrozenReplayResult:
    """Validate a closed decision and derive its scored Markdown."""

    raw_html_bytes = _text_bytes(
        raw_html,
        maximum=config["max_source_bytes"],
        field="raw_html",
    )
    candidate = _text_bytes(
        baseline_prediction,
        maximum=config["max_candidate_bytes"],
        field="baseline_prediction",
    )
    if not isinstance(observation, dict) or set(observation) != OBSERVATION_KEYS:
        raise FrozenReplayError("decision observation schema is not closed")
    if (
        observation["schema_version"] != OVERLAY_SCHEMA
        or observation["enabled"] is not True
        or type(observation["accepted"]) is not bool
        or observation["digest_is_authentication"] is not False
        or type(observation["reason"]) is not str
        or len(observation["reason"]) > 128
        or observation["visible_tokens_identical"] is not True
        or type(observation["output_markdown"]) is not str
        or type(observation["proposals"]) is not list
        or len(observation["proposals"]) > config["max_atoms"]
        or type(observation["applied_proposal_ids"]) is not list
    ):
        raise FrozenReplayError("decision primitive identity is invalid")
    output_record = _text_bytes(
        observation["output_markdown"],
        maximum=config["max_output_bytes"],
        field="output_markdown",
    )
    overlay_source_digest = _framed_digest(
        "clusy-atomic-overlay-source-v0",
        raw_html_bytes,
    )
    input_digest = _framed_digest(
        "clusy-atomic-overlay-input-v0",
        candidate,
    )
    config_digest = _framed_digest(
        "clusy-atomic-overlay-config-v0",
        _canonical_json(config),
    )
    if (
        observation["candidate_markdown_sha256"]
        != hashlib.sha256(candidate).hexdigest()
        or observation["source_digest"] != overlay_source_digest
        or observation["input_digest"] != input_digest
        or observation["config_digest"] != config_digest
        or observation["input_bytes"] != len(candidate)
    ):
        raise FrozenReplayError("decision source/input/config digest does not replay")
    (
        native_document,
        expected_proposals,
        expected_accepted,
        expected_reason,
        expected_output,
    ) = _derive_native_proposals(
        raw_html,
        raw_html_bytes,
        baseline_prediction,
        candidate,
        config=config,
    )
    if (
        len(observation["proposals"]) != len(expected_proposals)
        or observation["accepted"] is not expected_accepted
        or observation["reason"] != expected_reason
    ):
        raise FrozenReplayError(
            "decision inventory/outcome differs from raw native replay"
        )
    for field in (
        "candidate_markdown_sha256",
        "source_digest",
        "input_digest",
        "config_digest",
        "output_digest",
        "visible_token_digest",
        "decision_digest",
    ):
        _digest(observation[field], field=f"decision.{field}")
    _integer(
        observation["input_bytes"],
        field="decision.input_bytes",
        maximum=config["max_candidate_bytes"],
    )
    _integer(
        observation["output_bytes"],
        field="decision.output_bytes",
        maximum=config["max_output_bytes"],
    )
    _integer(
        observation["growth_bytes"],
        field="decision.growth_bytes",
        minimum=-config["max_output_bytes"],
        maximum=config["max_output_bytes"],
    )
    proposals: list[dict[str, Any]] = []
    replacements: dict[str, bytes] = {}
    for raw_proposal, expected_proposal in zip(
        observation["proposals"],
        expected_proposals,
        strict=True,
    ):
        proposal, replacement = _validate_proposal(
            raw_proposal,
            expected=expected_proposal,
            native_document=native_document,
            raw_html=raw_html_bytes,
            candidate=candidate,
            overlay_source_digest=overlay_source_digest,
            input_digest=input_digest,
            config_digest=config_digest,
            config=config,
        )
        proposals.append(proposal)
        if replacement is not None:
            replacements[proposal["proposal_id"]] = replacement
    proposal_ids = tuple(proposal["proposal_id"] for proposal in proposals)
    if len(set(proposal_ids)) != len(proposal_ids):
        raise FrozenReplayError("proposal IDs are not unique")
    accepted = tuple(proposal for proposal in proposals if proposal["accepted"])
    applied_ids = tuple(proposal["proposal_id"] for proposal in accepted)
    if observation["applied_proposal_ids"] != list(applied_ids) or len(
        replacements
    ) != len(accepted):
        raise FrozenReplayError("applied proposal inventory differs")
    ordered = sorted(
        accepted,
        key=lambda proposal: (
            proposal["candidate_span_start"],
            proposal["candidate_span_end"],
        ),
    )
    previous_end = -1
    for proposal in ordered:
        start = proposal["candidate_span_start"]
        end = proposal["candidate_span_end"]
        if start < previous_end:
            raise FrozenReplayError("accepted proposal spans overlap")
        previous_end = end
    derived_output = candidate
    for proposal in reversed(ordered):
        start = proposal["candidate_span_start"]
        end = proposal["candidate_span_end"]
        derived_output = (
            derived_output[:start]
            + replacements[proposal["proposal_id"]]
            + derived_output[end:]
        )
    if derived_output != expected_output:
        raise FrozenReplayError(
            "frozen patch replay differs from raw native decision replay"
        )
    if observation["accepted"]:
        if not accepted or observation["reason"] != "accepted":
            raise FrozenReplayError("accepted decision has no accepted proposal")
    elif (
        accepted
        or applied_ids
        or observation["reason"] == "accepted"
        or derived_output != candidate
    ):
        raise FrozenReplayError("rejected decision has an applied proposal")
    if output_record != derived_output:
        raise FrozenReplayError("stored output differs from frozen patch replay")
    try:
        candidate_tokens = _visible_tokens(
            baseline_prediction,
            MAX_VISIBLE_TOKENS,
        )
        output_text = derived_output.decode("utf-8")
        output_tokens = _visible_tokens(output_text, MAX_VISIBLE_TOKENS)
    except (UnicodeDecodeError, UnicodeEncodeError, _TokenBudgetError) as error:
        raise FrozenReplayError(
            "decision visible tokens exceed frozen bounds"
        ) from error
    visible_digest = _token_digest(output_tokens)
    output_digest = _framed_digest(
        "clusy-atomic-overlay-output-v0",
        derived_output,
    )
    if (
        candidate_tokens != output_tokens
        or observation["visible_token_digest"] != visible_digest
        or observation["output_digest"] != output_digest
        or observation["output_bytes"] != len(derived_output)
        or observation["growth_bytes"] != len(derived_output) - len(candidate)
    ):
        raise FrozenReplayError("decision output/visible digest does not replay")
    if observation["accepted"]:
        try:
            bounded_candidate_tokens = _visible_tokens(
                baseline_prediction,
                config["max_tokens"],
            )
            bounded_output_tokens = _visible_tokens(
                output_text,
                config["max_tokens"],
            )
        except _TokenBudgetError as error:
            raise FrozenReplayError(
                "accepted decision exceeds configured visible-token budget"
            ) from error
        if bounded_candidate_tokens != bounded_output_tokens:
            raise FrozenReplayError(
                "accepted decision configured visible tokens differ"
            )
        if (
            len(derived_output) - len(candidate) > config["max_growth_bytes"]
            or len(derived_output) * 1_000
            > max(1, len(candidate)) * config["max_growth_ratio_milli"]
        ):
            raise FrozenReplayError("accepted decision exceeds global growth bounds")
    decision_canonical = {
        "accepted": observation["accepted"],
        "applied_proposal_ids": applied_ids,
        "config_digest": config_digest,
        "enabled": True,
        "growth_bytes": len(derived_output) - len(candidate),
        "input_bytes": len(candidate),
        "input_digest": input_digest,
        "output_bytes": len(derived_output),
        "output_digest": output_digest,
        "proposal_ids": proposal_ids,
        "reason": observation["reason"],
        "source_digest": overlay_source_digest,
        "visible_token_digest": visible_digest,
        "visible_tokens_identical": True,
    }
    decision_digest = _framed_digest(
        "clusy-atomic-overlay-decision-v0",
        _canonical_json(decision_canonical),
    )
    replay = observation["replay"]
    timing = observation["timing"]
    if (
        observation["decision_digest"] != decision_digest
        or not isinstance(replay, dict)
        or set(replay) != {"decision_digest", "output_digest", "reason", "verified"}
        or replay
        != {
            "decision_digest": decision_digest,
            "output_digest": output_digest,
            "reason": "verified",
            "verified": True,
        }
        or not isinstance(timing, dict)
        or set(timing) != {"decision_elapsed_ns", "replay_elapsed_ns"}
        or not all(type(value) is int and value >= 0 for value in timing.values())
    ):
        raise FrozenReplayError("decision/replay receipt does not reproduce")
    return FrozenReplayResult(
        output_markdown=output_text,
        output_digest=output_digest,
        decision_digest=decision_digest,
        source_digest=overlay_source_digest,
        input_digest=input_digest,
        visible_token_digest=visible_digest,
        proposal_ids=proposal_ids,
        applied_proposal_ids=applied_ids,
    )

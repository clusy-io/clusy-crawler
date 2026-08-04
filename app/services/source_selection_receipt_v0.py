"""Fail-closed receipts for model-selected, source-derived HTML blocks.

The optional MinerU lane classifies bounded ``_item_id`` values as ``main`` or
``other``.  It does not need authority to generate page text.  This module
turns that implicit pointer contract into an independently replayed receipt:

* every prompt item must have exactly one canonical label;
* the exact raw model response must parse without duplicate or repaired labels;
* prompt and mapped-DOM item catalogues must agree;
* selected nodes are replayed from the mapped DOM without consulting model
  text; and
* the replayed DOM must match the upstream selected HTML before its
  deterministic serializer output can be considered.

The receipt contains only hashes, counts, and selected source pointers.  It is
safe to expose as provenance, but its SHA-256 digests are integrity identities,
not authentication tags.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Final

from lxml import html as lxml_html
from lxml.etree import ParserError

SOURCE_SELECTION_RECEIPT_V0_SCHEMA: Final = "quality-source-selection.v0"
SOURCE_SELECTION_RECEIPT_V0_MAX_ITEMS: Final = 100_000
SOURCE_SELECTION_RECEIPT_V0_MAX_INTERNAL_CHARS: Final = 8 * 1024 * 1024

_ITEM_ID_ATTRIBUTE: Final = "_item_id"
_TAIL_BLOCK_TAG: Final = "cc-alg-uc-text"
_CANONICAL_ITEM_ID = re.compile(r"[1-9][0-9]{0,8}\Z")
_COMPACT_LABEL_TOKEN = re.compile(r"([1-9][0-9]{0,8})(main|other)")
_SHA256_IDENTITY = re.compile(r"[0-9a-f]{64}\Z")
_ALLOWED_LABELS = frozenset({"main", "other"})
_ALLOWED_RESPONSE_FORMATS = frozenset({"json", "compact"})


class SourceSelectionReceiptError(ValueError):
    """The source-selection proof could not be constructed or replayed."""


class _JSONObjectPairs(list[tuple[object, object]]):
    """Preserve JSON object pairs so duplicate keys cannot collapse."""


@dataclass(frozen=True, slots=True)
class QualitySourceSelectionReceiptV0:
    """Bounded provenance for one independently replayed pointer selection."""

    schema_version: str
    upstream_revision: str
    prompt_profile: str
    response_format: str
    source_sha256: str
    model_response_sha256: str
    simplified_html_sha256: str
    mapped_html_sha256: str
    selected_html_sha256: str
    labels_sha256: str
    item_count: int
    selected_count: int
    selected_item_ids: tuple[str, ...]
    replay_verified: bool
    receipt_sha256: str
    digest_is_authentication: bool = False


@dataclass(frozen=True, slots=True)
class QualitySourceSelectionReplayV0:
    """A v0 receipt plus the exact canonical DOM produced by its replay."""

    receipt: QualitySourceSelectionReceiptV0
    selected_html: str
    selected_visible_text_chars: int


@dataclass(frozen=True, slots=True)
class QualitySourceDerivedReplayV0:
    """Canonical source artifacts replayed from a trusted preprocessor output.

    This deliberately carries no model response.  Its sole purpose is to prove
    that a bounded set of selected item pointers resolves to a subgraph of the
    independently derived mapped DOM.
    """

    simplified_html_sha256: str
    mapped_html_sha256: str
    selected_html_sha256: str
    item_count: int
    selected_count: int
    selected_html: str


def build_quality_source_selection_replay_v0(
    *,
    raw_html: str,
    raw_model_response: str,
    response_format: str,
    simplified_html: str,
    mapped_html: str,
    item_labels: object,
    selected_html: str,
    upstream_revision: str,
    prompt_profile: str,
) -> QualitySourceSelectionReplayV0:
    """Validate and return one complete item-label selection and its replay."""

    raw_html = _bounded_exact_string("raw_html", raw_html)
    raw_model_response = _bounded_exact_string(
        "raw_model_response",
        raw_model_response,
    )
    response_format = _canonical_response_format(response_format)
    simplified_html = _bounded_exact_string("simplified_html", simplified_html)
    mapped_html = _bounded_exact_string("mapped_html", mapped_html)
    selected_html = _bounded_exact_string("selected_html", selected_html)
    upstream_revision = _bounded_identity("upstream_revision", upstream_revision)
    prompt_profile = _bounded_identity("prompt_profile", prompt_profile)

    simplified_root = _parse_html(simplified_html, field="simplified_html")
    mapped_root = _parse_html(mapped_html, field="mapped_html")
    prompt_ids = _ordered_item_ids(simplified_root, field="simplified_html")
    mapped_ids = _ordered_item_ids(mapped_root, field="mapped_html")
    if not prompt_ids:
        raise SourceSelectionReceiptError("source selection has no prompt items")
    if prompt_ids != mapped_ids:
        raise SourceSelectionReceiptError(
            "prompt and mapped-DOM item catalogues differ"
        )
    # Replay prunes ``mapped_root`` in place. Snapshot both complete inputs
    # before that mutation so their receipt identities cannot silently collapse
    # to the selected subgraph.
    simplified_canonical = _canonical_html(simplified_root)
    mapped_canonical = _canonical_html(mapped_root)

    labels = _canonical_labels(item_labels, prompt_ids)
    response_labels = _strict_model_labels(
        raw_model_response,
        response_format=response_format,
        prompt_ids=prompt_ids,
    )
    if response_labels != labels:
        raise SourceSelectionReceiptError(
            "raw model response labels differ from parsed model labels"
        )
    selected_ids = tuple(item_id for item_id in prompt_ids if labels[item_id] == "main")
    if not selected_ids:
        raise SourceSelectionReceiptError("source selection contains no main items")

    replayed_html = _replay_selected_html(mapped_root, selected_ids)
    expected_root = _parse_html(selected_html, field="selected_html")
    replayed_canonical = _canonical_html(replayed_html)
    selected_canonical = _canonical_html(expected_root)
    if replayed_canonical != selected_canonical:
        raise SourceSelectionReceiptError(
            "independent selected-DOM replay does not match upstream output"
        )

    label_pairs = tuple((item_id, labels[item_id]) for item_id in prompt_ids)
    source_sha256 = _sha256_text(raw_html)
    model_response_sha256 = _sha256_text(raw_model_response)
    simplified_html_sha256 = _sha256_text(simplified_canonical)
    mapped_html_sha256 = _sha256_text(mapped_canonical)
    selected_html_sha256 = _sha256_text(selected_canonical)
    labels_sha256 = _sha256_json(label_pairs)
    identity: dict[str, object] = {
        "schema_version": SOURCE_SELECTION_RECEIPT_V0_SCHEMA,
        "upstream_revision": upstream_revision,
        "prompt_profile": prompt_profile,
        "response_format": response_format,
        "source_sha256": source_sha256,
        "model_response_sha256": model_response_sha256,
        "simplified_html_sha256": simplified_html_sha256,
        "mapped_html_sha256": mapped_html_sha256,
        "selected_html_sha256": selected_html_sha256,
        "labels_sha256": labels_sha256,
        "item_count": len(prompt_ids),
        "selected_count": len(selected_ids),
        "selected_item_ids": selected_ids,
        "replay_verified": True,
        "digest_is_authentication": False,
    }
    receipt = QualitySourceSelectionReceiptV0(
        schema_version=SOURCE_SELECTION_RECEIPT_V0_SCHEMA,
        upstream_revision=upstream_revision,
        prompt_profile=prompt_profile,
        response_format=response_format,
        source_sha256=source_sha256,
        model_response_sha256=model_response_sha256,
        simplified_html_sha256=simplified_html_sha256,
        mapped_html_sha256=mapped_html_sha256,
        selected_html_sha256=selected_html_sha256,
        labels_sha256=labels_sha256,
        item_count=len(prompt_ids),
        selected_count=len(selected_ids),
        selected_item_ids=selected_ids,
        replay_verified=True,
        receipt_sha256=_sha256_json(identity),
        digest_is_authentication=False,
    )
    return QualitySourceSelectionReplayV0(
        receipt=receipt,
        selected_html=replayed_canonical,
        selected_visible_text_chars=sum(
            len(part) for part in replayed_html.itertext()
        ),
    )


def build_quality_source_selection_receipt_v0(
    *,
    raw_html: str,
    raw_model_response: str,
    response_format: str,
    simplified_html: str,
    mapped_html: str,
    item_labels: object,
    selected_html: str,
    upstream_revision: str,
    prompt_profile: str,
) -> QualitySourceSelectionReceiptV0:
    """Validate and independently replay one complete item-label selection."""

    return build_quality_source_selection_replay_v0(
        raw_html=raw_html,
        raw_model_response=raw_model_response,
        response_format=response_format,
        simplified_html=simplified_html,
        mapped_html=mapped_html,
        item_labels=item_labels,
        selected_html=selected_html,
        upstream_revision=upstream_revision,
        prompt_profile=prompt_profile,
    ).receipt


def verify_quality_source_selection_receipt_v0(
    receipt: object,
    *,
    raw_html: str,
) -> bool:
    """Verify a receipt's canonical identity and binding to the current source."""

    if type(receipt) is not QualitySourceSelectionReceiptV0:
        return False
    try:
        raw_html = _bounded_exact_string("raw_html", raw_html)
        identity = _receipt_identity(receipt)
    except (AttributeError, SourceSelectionReceiptError, TypeError, ValueError):
        return False
    return (
        receipt.schema_version == SOURCE_SELECTION_RECEIPT_V0_SCHEMA
        and receipt.replay_verified is True
        and receipt.digest_is_authentication is False
        and receipt.source_sha256 == _sha256_text(raw_html)
        and receipt.receipt_sha256 == _sha256_json(identity)
    )


def replay_quality_source_selection_from_derived_v0(
    *,
    simplified_html: str,
    mapped_html: str,
    selected_item_ids: object,
) -> QualitySourceDerivedReplayV0:
    """Replay selected pointers only from independently derived source DOMs.

    The caller is responsible for obtaining ``simplified_html`` and
    ``mapped_html`` from a trusted, pinned preprocessor.  This function closes
    the remaining structural boundary: both DOMs must expose the same complete
    contiguous item catalogue, and every retained node is replayed from the
    mapped DOM rather than accepted from a caller-provided fragment.
    """

    simplified_html = _bounded_exact_string("simplified_html", simplified_html)
    mapped_html = _bounded_exact_string("mapped_html", mapped_html)
    simplified_root = _parse_html(simplified_html, field="simplified_html")
    mapped_root = _parse_html(mapped_html, field="mapped_html")
    prompt_ids = _ordered_item_ids(simplified_root, field="simplified_html")
    mapped_ids = _ordered_item_ids(mapped_root, field="mapped_html")
    if not prompt_ids:
        raise SourceSelectionReceiptError("source selection has no prompt items")
    if prompt_ids != mapped_ids:
        raise SourceSelectionReceiptError(
            "prompt and mapped-DOM item catalogues differ"
        )

    selected_ids = _canonical_selected_item_ids(
        selected_item_ids,
        prompt_ids=prompt_ids,
    )
    simplified_canonical = _canonical_html(simplified_root)
    mapped_canonical = _canonical_html(mapped_root)
    selected_root = _replay_selected_html(mapped_root, selected_ids)
    selected_canonical = _canonical_html(selected_root)
    return QualitySourceDerivedReplayV0(
        simplified_html_sha256=_sha256_text(simplified_canonical),
        mapped_html_sha256=_sha256_text(mapped_canonical),
        selected_html_sha256=_sha256_text(selected_canonical),
        item_count=len(prompt_ids),
        selected_count=len(selected_ids),
        selected_html=selected_canonical,
    )


def _receipt_identity(
    receipt: QualitySourceSelectionReceiptV0,
) -> dict[str, object]:
    identity_fields = (
        "schema_version",
        "upstream_revision",
        "prompt_profile",
        "response_format",
    )
    digest_fields = (
        "source_sha256",
        "model_response_sha256",
        "simplified_html_sha256",
        "mapped_html_sha256",
        "selected_html_sha256",
        "labels_sha256",
    )
    values: dict[str, object] = {}
    for name in identity_fields:
        value = object.__getattribute__(receipt, name)
        values[name] = _bounded_identity(name, value)
    if values["response_format"] not in _ALLOWED_RESPONSE_FORMATS:
        raise ValueError("response_format is not supported")
    for name in digest_fields:
        value = object.__getattribute__(receipt, name)
        if type(value) is not str or _SHA256_IDENTITY.fullmatch(value) is None:
            raise ValueError(f"{name} must be a canonical SHA-256 identity")
        values[name] = value
    receipt_sha256 = object.__getattribute__(receipt, "receipt_sha256")
    if (
        type(receipt_sha256) is not str
        or _SHA256_IDENTITY.fullmatch(receipt_sha256) is None
    ):
        raise ValueError("receipt_sha256 must be a canonical SHA-256 identity")

    item_count = object.__getattribute__(receipt, "item_count")
    selected_count = object.__getattribute__(receipt, "selected_count")
    selected_item_ids = object.__getattribute__(receipt, "selected_item_ids")
    replay_verified = object.__getattribute__(receipt, "replay_verified")
    digest_is_authentication = object.__getattribute__(
        receipt,
        "digest_is_authentication",
    )
    if type(item_count) is not int or not 1 <= item_count <= SOURCE_SELECTION_RECEIPT_V0_MAX_ITEMS:
        raise ValueError("invalid item_count")
    if type(selected_count) is not int or not 1 <= selected_count <= item_count:
        raise ValueError("invalid selected_count")
    if type(selected_item_ids) is not tuple or len(selected_item_ids) != selected_count:
        raise ValueError("invalid selected_item_ids")
    if type(replay_verified) is not bool or type(digest_is_authentication) is not bool:
        raise TypeError("receipt booleans must be exact bools")

    previous = 0
    for item_id in selected_item_ids:
        if type(item_id) is not str or _CANONICAL_ITEM_ID.fullmatch(item_id) is None:
            raise ValueError("invalid selected item ID")
        numeric = int(item_id)
        if numeric <= previous or numeric > item_count:
            raise ValueError("selected item IDs must be strictly ordered")
        previous = numeric

    values.update(
        {
            "item_count": item_count,
            "selected_count": selected_count,
            "selected_item_ids": selected_item_ids,
            "replay_verified": replay_verified,
            "digest_is_authentication": digest_is_authentication,
        }
    )
    return values


def _bounded_exact_string(name: str, value: object) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be an exact string")
    if not value:
        raise SourceSelectionReceiptError(f"{name} must not be empty")
    if len(value) > SOURCE_SELECTION_RECEIPT_V0_MAX_INTERNAL_CHARS:
        raise SourceSelectionReceiptError(f"{name} exceeds the character budget")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise SourceSelectionReceiptError(f"{name} contains invalid Unicode") from error
    return value


def _bounded_identity(name: str, value: object) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be an exact string")
    if not value or len(value) > 128 or not value.isascii():
        raise SourceSelectionReceiptError(f"{name} is not a bounded ASCII identity")
    return value


def _canonical_response_format(value: object) -> str:
    value = _bounded_identity("response_format", value)
    if value not in _ALLOWED_RESPONSE_FORMATS:
        raise SourceSelectionReceiptError("response_format is not supported")
    return value


def _parse_html(value: str, *, field: str) -> lxml_html.HtmlElement:
    parser = lxml_html.HTMLParser(
        collect_ids=False,
        encoding="utf-8",
        remove_blank_text=True,
        remove_comments=True,
        remove_pis=True,
        no_network=True,
        recover=True,
    )
    try:
        return lxml_html.fromstring(value.encode("utf-8"), parser=parser)
    except (ParserError, TypeError, ValueError) as error:
        raise SourceSelectionReceiptError(f"{field} is not parseable HTML") from error


def _ordered_item_ids(
    root: lxml_html.HtmlElement,
    *,
    field: str,
) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for element in root.iter():
        item_id = element.get(_ITEM_ID_ATTRIBUTE)
        if item_id is None:
            continue
        if (
            type(item_id) is not str
            or _CANONICAL_ITEM_ID.fullmatch(item_id) is None
            or item_id in seen
        ):
            raise SourceSelectionReceiptError(
                f"{field} contains an invalid or duplicate item ID"
            )
        seen.add(item_id)
        output.append(item_id)
        if len(output) > SOURCE_SELECTION_RECEIPT_V0_MAX_ITEMS:
            raise SourceSelectionReceiptError(f"{field} exceeds the item budget")
    expected = tuple(str(index) for index in range(1, len(output) + 1))
    if tuple(output) != expected:
        raise SourceSelectionReceiptError(
            f"{field} item IDs are not canonical contiguous source order"
        )
    return tuple(output)


def _canonical_labels(
    item_labels: object,
    prompt_ids: tuple[str, ...],
) -> dict[str, str]:
    if type(item_labels) is not dict:
        raise TypeError("item_labels must be an exact dict")
    if len(item_labels) != len(prompt_ids):
        raise SourceSelectionReceiptError("model labels do not cover every prompt item")
    output: dict[str, str] = {}
    for key, value in item_labels.items():
        if (
            type(key) is not str
            or type(value) is not str
            or _CANONICAL_ITEM_ID.fullmatch(key) is None
            or value not in _ALLOWED_LABELS
        ):
            raise SourceSelectionReceiptError("model labels are not canonical")
        output[key] = value
    if set(output) != set(prompt_ids):
        raise SourceSelectionReceiptError("model label IDs differ from prompt IDs")
    return output


def _canonical_selected_item_ids(
    selected_item_ids: object,
    *,
    prompt_ids: tuple[str, ...],
) -> tuple[str, ...]:
    if type(selected_item_ids) is not tuple or not selected_item_ids:
        raise SourceSelectionReceiptError(
            "selected item IDs must be a non-empty exact tuple"
        )
    selected_ids = selected_item_ids
    if len(selected_ids) > len(prompt_ids):
        raise SourceSelectionReceiptError("selected item IDs exceed the source catalogue")
    selected_set: set[str] = set()
    previous = 0
    for item_id in selected_ids:
        if type(item_id) is not str or _CANONICAL_ITEM_ID.fullmatch(item_id) is None:
            raise SourceSelectionReceiptError("selected item IDs are not canonical")
        numeric = int(item_id)
        if numeric <= previous or numeric > len(prompt_ids) or item_id in selected_set:
            raise SourceSelectionReceiptError(
                "selected item IDs are not strictly ordered source pointers"
            )
        selected_set.add(item_id)
        previous = numeric
    if not selected_set.issubset(prompt_ids):
        raise SourceSelectionReceiptError("selected item IDs differ from the source catalogue")
    return selected_ids


def _strict_model_labels(
    raw_model_response: str,
    *,
    response_format: str,
    prompt_ids: tuple[str, ...],
) -> dict[str, str]:
    pairs: list[tuple[object, object]]
    if response_format == "json":
        try:
            parsed = json.loads(
                raw_model_response,
                object_pairs_hook=_JSONObjectPairs,
            )
        except (json.JSONDecodeError, RecursionError) as error:
            raise SourceSelectionReceiptError(
                "raw JSON model response is malformed"
            ) from error
        if type(parsed) is not _JSONObjectPairs:
            raise SourceSelectionReceiptError(
                "raw JSON model response must be an object"
            )
        pairs = list(parsed)
    elif response_format == "compact":
        pairs = []
        position = 0
        while position < len(raw_model_response):
            match = _COMPACT_LABEL_TOKEN.match(raw_model_response, position)
            if match is None:
                raise SourceSelectionReceiptError(
                    "raw compact model response is malformed"
                )
            pairs.append((match.group(1), match.group(2)))
            if len(pairs) > SOURCE_SELECTION_RECEIPT_V0_MAX_ITEMS:
                raise SourceSelectionReceiptError(
                    "raw compact model response exceeds the item budget"
                )
            position = match.end()
        if tuple(key for key, _ in pairs) != prompt_ids:
            raise SourceSelectionReceiptError(
                "raw compact model response IDs are not in exact prompt order"
            )
    else:
        raise SourceSelectionReceiptError("response_format is not supported")

    output: dict[str, str] = {}
    for key, value in pairs:
        if (
            type(key) is not str
            or type(value) is not str
            or _CANONICAL_ITEM_ID.fullmatch(key) is None
            or value not in _ALLOWED_LABELS
        ):
            raise SourceSelectionReceiptError(
                "raw model response labels are not canonical"
            )
        if key in output:
            raise SourceSelectionReceiptError(
                "raw model response contains a duplicate item ID"
            )
        output[key] = value
    if len(pairs) != len(prompt_ids):
        raise SourceSelectionReceiptError(
            "raw model response does not label every prompt item"
        )
    if set(output) != set(prompt_ids):
        raise SourceSelectionReceiptError(
            "raw model response IDs differ from prompt IDs"
        )
    return output


def _replay_selected_html(
    mapped_root: lxml_html.HtmlElement,
    selected_ids: tuple[str, ...],
) -> lxml_html.HtmlElement:
    selected_set = set(selected_ids)
    retained: set[lxml_html.HtmlElement] = set()
    for element in mapped_root.iter():
        if element.get(_ITEM_ID_ATTRIBUTE) not in selected_set:
            continue
        retained.update(element.iter())
        retained.update(element.iterancestors())

    previous: lxml_html.HtmlElement | None = None
    for element in mapped_root.iter():
        if previous is not None:
            if element.tag == "br" and previous in retained and previous.tag != "br":
                retained.add(element)
            if previous.tag == "br" and element in retained and element.tag != "br":
                retained.add(previous)
        previous = element

    def prune(element: lxml_html.HtmlElement) -> None:
        for child in list(element.iterchildren()):
            if child not in retained:
                element.remove(child)
            else:
                prune(child)

    if mapped_root not in retained:
        raise SourceSelectionReceiptError("selected items have no retained root")
    prune(mapped_root)
    for tail_block in mapped_root.xpath(f"//{_TAIL_BLOCK_TAG}"):
        tail_block.drop_tag()
    return mapped_root


def _canonical_html(root: lxml_html.HtmlElement) -> str:
    value = lxml_html.tostring(
        root,
        pretty_print=False,
        encoding="utf-8",
        method="html",
    )
    return bytes(value).decode("utf-8")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()

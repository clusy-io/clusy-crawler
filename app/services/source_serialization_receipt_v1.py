"""Closed receipts for source-selected, locally serialized quality output.

Version 0 proves that a model supplied only a complete set of DOM pointers.
Version 1 preserves that receipt verbatim, independently re-derives the mapped
DOM from the raw source with the pinned MinerU-HTML preprocessor, replays only
the selected source pointers, and serializes that DOM with the pinned local
MinerU Webkit ``mm_md`` converter.  The model never supplies accepted text.

SHA-256 values in this module are deterministic integrity identities, not
authentication tags.  The trusted builder re-executes both pinned stages and
then authenticates the closed identity with an ephemeral process-local HMAC.
Acceptance verifies that capability, so a caller-constructed, self-consistently
rehashed dataclass cannot authorize a foreign mapped DOM or different output.
The capability is intentionally nonportable and must not be treated as a
persistent signature.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from typing import Final, Protocol, cast

from lxml import html as lxml_html
from lxml.etree import ParserError

from app.services.source_selection_receipt_v0 import (
    SOURCE_SELECTION_RECEIPT_V0_MAX_INTERNAL_CHARS,
    QualitySourceSelectionReceiptV0,
    build_quality_source_selection_replay_v0,
    verify_quality_source_selection_receipt_v0,
)

SOURCE_SERIALIZATION_RECEIPT_V1_SCHEMA: Final = "quality-source-selection-serialization.v1"
MINERU_HTML_DISTRIBUTION: Final = "mineru-html"
MINERU_HTML_VERSION: Final = "1.1.2"
MINERU_HTML_REVISION: Final = "73cf266690befd209cae7e6fdff9716d5b31a976"
MINERU_HTML_PREPROCESSOR_ENTRYPOINT: Final = "mineru_html.process.simplify_html.simplify_html"
MINERU_HTML_PREPROCESSOR_CUTOFF_LENGTH: Final = 500
MINERU_WEBKIT_DISTRIBUTION: Final = "mineru-webkit"
MINERU_WEBKIT_VERSION: Final = "0.1.6"
MINERU_WEBKIT_ENTRYPOINT: Final = "webpage_converter.convert.convert_html_to_structured_data"
MINERU_WEBKIT_OUTPUT_FORMAT: Final = "mm_md"
SOURCE_SERIALIZATION_RECEIPT_V1_MAX_OUTPUT_CHARS: Final = 8 * 1024 * 1024
SOURCE_SERIALIZATION_RECEIPT_V1_MAX_URL_CHARS: Final = 4096
SOURCE_SERIALIZATION_RECEIPT_V1_MAX_SELECTED_TEXT_CHARS: Final = 256 * 1024
SOURCE_SERIALIZATION_RECEIPT_V1_MAX_INPUT_CHARS: Final = 768 * 1024
SOURCE_SERIALIZATION_RECEIPT_V1_MAX_DOM_NODES: Final = 20_000
SOURCE_SERIALIZATION_RECEIPT_V1_MAX_DOM_DEPTH: Final = 64
SOURCE_SERIALIZATION_RECEIPT_V1_MAX_TEXT_FRAGMENTS: Final = 20_000
SOURCE_SERIALIZATION_RECEIPT_V1_MAX_LIST_DEPTH: Final = 32
SOURCE_SERIALIZATION_RECEIPT_V1_MAX_TABLE_GRID_SLOTS: Final = 100_000
SOURCE_SERIALIZATION_RECEIPT_V1_MAX_PROJECTED_IMAGE_CHARS: Final = 512 * 1024
SOURCE_SERIALIZATION_RECEIPT_V1_MAX_LIST_INDENT_CHARS: Final = 256 * 1024
SOURCE_SERIALIZATION_RECEIPT_V1_MAX_CODE_WORK: Final = 4_000_000
SOURCE_SERIALIZATION_RECEIPT_V1_MAX_MATH_DELIMITER_TOKENS: Final = 2048
SOURCE_SERIALIZATION_RECEIPT_V1_MAX_LATEX_CONTROL_TOKENS: Final = 8192
SOURCE_SERIALIZATION_RECEIPT_V1_MAX_LATEX_GROUP_DEPTH: Final = 128
QUALITY_SOURCE_PREPROCESSOR_MAX_INPUT_CHARS: Final = 1_000_000
QUALITY_SOURCE_PREPROCESSOR_MAX_DOM_NODES: Final = 5000
QUALITY_SOURCE_PREPROCESSOR_MAX_DOM_DEPTH: Final = 64
QUALITY_SOURCE_PREPROCESSOR_MAX_TEXT_FRAGMENTS: Final = 8000
PROCESS_AUTHENTICATION_SCOPE: Final = "process-local-hmac-sha256.v1"

_SHA256_IDENTITY = re.compile(r"[0-9a-f]{64}\Z")
_PROCESS_AUTHENTICATION_DOMAIN = b"clusy.quality-source-selection-serialization.v1"
_PROCESS_AUTHENTICATION_KEY_LOCK = threading.Lock()
_PROCESS_AUTHENTICATION_PID: int | None = None
_PROCESS_AUTHENTICATION_KEY: bytes | None = None
_SERIALIZER_LOCK = threading.Lock()
_LIST_CONTAINER_TAGS = frozenset({"ul", "ol", "dl", "menu", "dir"})
_LIST_ITEM_TAGS = frozenset({"li", "dt", "dd"})
_MATHJAX_TEXT_SKIP_TAGS = frozenset({"script", "noscript", "style", "textarea", "pre", "code"})
_IMAGE_CANDIDATE_TAGS = frozenset(
    {
        "article",
        "audio",
        "canvas",
        "embed",
        "figure",
        "iframe",
        "image",
        "img",
        "object",
        "picture",
        "video",
    }
)
_MATHML_TAGS = frozenset(
    {
        "annotation",
        "annotation-xml",
        "maction",
        "math",
        "menclose",
        "merror",
        "mfenced",
        "mfrac",
        "mi",
        "mmultiscripts",
        "mn",
        "mo",
        "mover",
        "mpadded",
        "mphantom",
        "mprescripts",
        "mroot",
        "mrow",
        "ms",
        "mspace",
        "msqrt",
        "mstyle",
        "msub",
        "msubsup",
        "msup",
        "mtable",
        "mtd",
        "mtext",
        "mtr",
        "munder",
        "munderover",
        "semantics",
    }
)
_QUALITY_SERIALIZATION_BLOCKED_HOSTS = frozenset({"mathinsight.org"})
_SOURCE_RESERVED_ATTRIBUTES = frozenset({"_item_id", "data-uid"})


class SourceSerializationReceiptError(ValueError):
    """The local source-to-serialization proof could not be constructed."""


class QualitySourceInputIneligibleError(SourceSerializationReceiptError):
    """The page is outside the optional quality lane's fixed work contract."""


class _MinerUWebkitSerializer(Protocol):
    def __call__(
        self,
        *,
        main_html: str,
        url: str | None,
        output_format: str,
    ) -> object: ...


class _MinerUHTMLPreprocessor(Protocol):
    def __call__(self, html_str: str, cutoff_length: int = 500) -> object: ...


@dataclass(frozen=True, slots=True)
class QualitySourceSerializationReceiptV1:
    """A closed v0 DOM-selection proof chained to exact local serialization."""

    schema_version: str
    preprocessor_distribution: str
    preprocessor_version: str
    preprocessor_revision: str
    preprocessor_entrypoint: str
    preprocessor_cutoff_length: int
    model_prompt_version: str
    serializer_distribution: str
    serializer_version: str
    serializer_entrypoint: str
    serializer_output_format: str
    source_url_sha256: str
    serializer_input_sha256: str
    replayed_selected_html: str = field(repr=False)
    output_sha256: str
    output_bytes: int
    selection_receipt_sha256: str
    selection_receipt: QualitySourceSelectionReceiptV0
    source_derivation_replay_verified: bool
    selection_replay_verified: bool
    serialization_replay_verified: bool
    process_authentication_scope: str
    process_authentication_mac: bytes = field(repr=False)
    receipt_sha256: str
    digest_is_authentication: bool = False

    @property
    def item_count(self) -> int:
        return self.selection_receipt.item_count

    @property
    def selected_count(self) -> int:
        return self.selection_receipt.selected_count

    @property
    def replay_verified(self) -> bool:
        return (
            self.source_derivation_replay_verified
            and self.selection_replay_verified
            and self.serialization_replay_verified
        )


@dataclass(frozen=True, slots=True)
class QualitySourceSerializationMintV1:
    """Trusted builder output: exact local text plus its process capability."""

    text: str
    receipt: QualitySourceSerializationReceiptV1


@lru_cache(maxsize=1)
def load_pinned_mineru_html_preprocessor_v1() -> _MinerUHTMLPreprocessor:
    """Load the exact raw-source preprocessor only when quality is enabled."""

    try:
        installed_version = version(MINERU_HTML_DISTRIBUTION)
    except PackageNotFoundError as error:
        raise SourceSerializationReceiptError(
            "pinned MinerU-HTML preprocessor is unavailable"
        ) from error
    if type(installed_version) is not str or installed_version != MINERU_HTML_VERSION:
        raise SourceSerializationReceiptError(
            "installed MinerU-HTML preprocessor version is not pinned"
        )
    try:
        module = import_module("mineru_html.process.simplify_html")
        preprocessor = module.simplify_html
    except (AttributeError, ImportError, ModuleNotFoundError) as error:
        raise SourceSerializationReceiptError(
            "pinned MinerU-HTML preprocessor entrypoint is unavailable"
        ) from error
    if not callable(preprocessor):
        raise SourceSerializationReceiptError(
            "pinned MinerU-HTML preprocessor entrypoint is not callable"
        )
    return cast("_MinerUHTMLPreprocessor", preprocessor)


@lru_cache(maxsize=1)
def load_pinned_mineru_webkit_serializer_v1() -> _MinerUWebkitSerializer:
    """Load the exact optional serializer only when the quality lane is used."""

    try:
        installed_version = version(MINERU_WEBKIT_DISTRIBUTION)
    except PackageNotFoundError as error:
        raise SourceSerializationReceiptError(
            "pinned MinerU Webkit serializer is unavailable"
        ) from error
    if type(installed_version) is not str or installed_version != MINERU_WEBKIT_VERSION:
        raise SourceSerializationReceiptError(
            "installed MinerU Webkit serializer version is not pinned"
        )
    try:
        module = import_module("webpage_converter.convert")
        serializer = module.convert_html_to_structured_data
    except (AttributeError, ImportError, ModuleNotFoundError) as error:
        raise SourceSerializationReceiptError(
            "pinned MinerU Webkit serializer entrypoint is unavailable"
        ) from error
    if not callable(serializer):
        raise SourceSerializationReceiptError(
            "pinned MinerU Webkit serializer entrypoint is not callable"
        )
    return cast("_MinerUWebkitSerializer", serializer)


def probe_pinned_quality_serialization_runtime_v1() -> None:
    """Exercise the complete pinned local capability used by a v1 mint.

    Distribution metadata and importability alone do not prove that the
    preprocessor and converter can execute in the current image.  Readiness
    calls this through its own one-entry cache and startup warms that cache off
    the event loop when the quality lane is configured.
    """

    smoke_text = "Clusy quality receipt v1 readiness"
    simplified_html, mapped_html = _derive_source_artifacts(
        raw_html=(f"<html><body><main><p>{smoke_text}</p></main></body></html>"),
    )
    if "_item_id" not in simplified_html or "_item_id" not in mapped_html:
        raise SourceSerializationReceiptError(
            "pinned raw-source preprocessor did not expose source item IDs"
        )
    serializer = load_pinned_mineru_webkit_serializer_v1()
    try:
        with _SERIALIZER_LOCK:
            output = serializer(
                main_html=mapped_html,
                url=None,
                output_format=MINERU_WEBKIT_OUTPUT_FORMAT,
            )
    except Exception as error:
        raise SourceSerializationReceiptError(
            "pinned local serializer readiness probe failed"
        ) from error
    if type(output) is not str or smoke_text not in output:
        raise SourceSerializationReceiptError(
            "pinned local serializer readiness probe returned invalid text"
        )


def mint_quality_source_serialization_v1(
    *,
    raw_html: str,
    source_url: str,
    raw_model_response: str,
    response_format: str,
    simplified_html: str,
    mapped_html: str,
    item_labels: object,
    selected_html: str,
    upstream_revision: str,
    prompt_profile: str,
    max_output_chars: int = SOURCE_SERIALIZATION_RECEIPT_V1_MAX_OUTPUT_CHARS,
) -> QualitySourceSerializationMintV1:
    """Mint exact local text only after a full raw-source derivation replay."""

    # Keep the trusted mint safe when called independently of QualityExtractor.
    # The extractor repeats this cheap admission before breaker/capacity state so
    # ineligible pages cannot consume a backend slot or count as an outage.
    preflight_quality_source_input_v1(raw_html)
    source_url = _bounded_source_url(source_url)
    model_prompt_version = _validate_upstream_contract(
        upstream_revision=upstream_revision,
        prompt_profile=prompt_profile,
        response_format=response_format,
    )
    derived_simplified_html, derived_mapped_html = _derive_source_artifacts(
        raw_html=raw_html,
    )
    if (
        type(simplified_html) is not str
        or type(mapped_html) is not str
        or simplified_html != derived_simplified_html
        or mapped_html != derived_mapped_html
    ):
        raise SourceSerializationReceiptError(
            "exact source-derived preprocessing replay differs from quality artifacts"
        )
    replay = build_quality_source_selection_replay_v0(
        raw_html=raw_html,
        raw_model_response=raw_model_response,
        response_format=response_format,
        simplified_html=derived_simplified_html,
        mapped_html=derived_mapped_html,
        item_labels=item_labels,
        selected_html=selected_html,
        upstream_revision=upstream_revision,
        prompt_profile=prompt_profile,
    )
    nested = replay.receipt
    output_limit = _bounded_output_limit(max_output_chars)
    selected_text_limit = min(
        output_limit,
        SOURCE_SERIALIZATION_RECEIPT_V1_MAX_SELECTED_TEXT_CHARS,
    )
    if replay.selected_visible_text_chars > selected_text_limit:
        raise QualitySourceInputIneligibleError(
            "selected source text exceeds the bounded serializer work budget"
        )
    serializer_input_limit = min(
        SOURCE_SERIALIZATION_RECEIPT_V1_MAX_INPUT_CHARS,
        (2 * output_limit) + (256 * 1024),
    )
    if len(replay.selected_html) > serializer_input_limit:
        raise QualitySourceInputIneligibleError(
            "selected source DOM exceeds the bounded serializer input budget"
        )
    try:
        _preflight_serializer_structure(
            replay.selected_html,
            source_url=source_url,
            max_output_chars=output_limit,
        )
    except SourceSerializationReceiptError as error:
        raise QualitySourceInputIneligibleError(str(error)) from error

    serializer = load_pinned_mineru_webkit_serializer_v1()
    try:
        with _SERIALIZER_LOCK:
            serialized = serializer(
                main_html=replay.selected_html,
                url=source_url or None,
                output_format=MINERU_WEBKIT_OUTPUT_FORMAT,
            )
    except Exception as error:
        raise SourceSerializationReceiptError("pinned local serialization replay failed") from error
    if type(serialized) is not str:
        raise SourceSerializationReceiptError(
            "pinned local serialization replay did not return exact text"
        )
    output_text = _canonical_output(
        serialized.strip(),
        max_output_chars=output_limit,
    )

    output_encoded = output_text.encode("utf-8")
    identity: dict[str, object] = {
        "schema_version": SOURCE_SERIALIZATION_RECEIPT_V1_SCHEMA,
        "preprocessor_distribution": MINERU_HTML_DISTRIBUTION,
        "preprocessor_version": MINERU_HTML_VERSION,
        "preprocessor_revision": MINERU_HTML_REVISION,
        "preprocessor_entrypoint": MINERU_HTML_PREPROCESSOR_ENTRYPOINT,
        "preprocessor_cutoff_length": MINERU_HTML_PREPROCESSOR_CUTOFF_LENGTH,
        "model_prompt_version": model_prompt_version,
        "serializer_distribution": MINERU_WEBKIT_DISTRIBUTION,
        "serializer_version": MINERU_WEBKIT_VERSION,
        "serializer_entrypoint": MINERU_WEBKIT_ENTRYPOINT,
        "serializer_output_format": MINERU_WEBKIT_OUTPUT_FORMAT,
        "source_url_sha256": _sha256_text(source_url),
        "serializer_input_sha256": nested.selected_html_sha256,
        "output_sha256": hashlib.sha256(output_encoded).hexdigest(),
        "output_bytes": len(output_encoded),
        "selection_receipt_sha256": nested.receipt_sha256,
        "selection_receipt": _selection_receipt_payload(nested),
        "source_derivation_replay_verified": True,
        "selection_replay_verified": True,
        "serialization_replay_verified": True,
        "process_authentication_scope": PROCESS_AUTHENTICATION_SCOPE,
        "digest_is_authentication": False,
    }
    receipt_sha256 = _sha256_json(identity)
    receipt = QualitySourceSerializationReceiptV1(
        schema_version=SOURCE_SERIALIZATION_RECEIPT_V1_SCHEMA,
        preprocessor_distribution=MINERU_HTML_DISTRIBUTION,
        preprocessor_version=MINERU_HTML_VERSION,
        preprocessor_revision=MINERU_HTML_REVISION,
        preprocessor_entrypoint=MINERU_HTML_PREPROCESSOR_ENTRYPOINT,
        preprocessor_cutoff_length=MINERU_HTML_PREPROCESSOR_CUTOFF_LENGTH,
        model_prompt_version=model_prompt_version,
        serializer_distribution=MINERU_WEBKIT_DISTRIBUTION,
        serializer_version=MINERU_WEBKIT_VERSION,
        serializer_entrypoint=MINERU_WEBKIT_ENTRYPOINT,
        serializer_output_format=MINERU_WEBKIT_OUTPUT_FORMAT,
        source_url_sha256=_sha256_text(source_url),
        serializer_input_sha256=nested.selected_html_sha256,
        replayed_selected_html=replay.selected_html,
        output_sha256=hashlib.sha256(output_encoded).hexdigest(),
        output_bytes=len(output_encoded),
        selection_receipt_sha256=nested.receipt_sha256,
        selection_receipt=nested,
        source_derivation_replay_verified=True,
        selection_replay_verified=True,
        serialization_replay_verified=True,
        process_authentication_scope=PROCESS_AUTHENTICATION_SCOPE,
        process_authentication_mac=_authenticate_identity(identity),
        receipt_sha256=receipt_sha256,
        digest_is_authentication=False,
    )
    return QualitySourceSerializationMintV1(text=output_text, receipt=receipt)


def build_quality_source_serialization_receipt_v1(
    *,
    raw_html: str,
    source_url: str,
    raw_model_response: str,
    response_format: str,
    simplified_html: str,
    mapped_html: str,
    item_labels: object,
    selected_html: str,
    output_text: str,
    upstream_revision: str,
    prompt_profile: str,
    max_output_chars: int = SOURCE_SERIALIZATION_RECEIPT_V1_MAX_OUTPUT_CHARS,
) -> QualitySourceSerializationReceiptV1:
    """Compatibility wrapper requiring an expected exact serialized output."""

    output_text = _canonical_output(
        output_text,
        max_output_chars=max_output_chars,
    )
    minted = mint_quality_source_serialization_v1(
        raw_html=raw_html,
        source_url=source_url,
        raw_model_response=raw_model_response,
        response_format=response_format,
        simplified_html=simplified_html,
        mapped_html=mapped_html,
        item_labels=item_labels,
        selected_html=selected_html,
        upstream_revision=upstream_revision,
        prompt_profile=prompt_profile,
        max_output_chars=max_output_chars,
    )
    if minted.text != output_text:
        raise SourceSerializationReceiptError(
            "local serialization replay differs from quality output"
        )
    return minted.receipt


def verify_quality_source_serialization_receipt_v1(
    receipt: object,
    *,
    raw_html: str,
    source_url: str,
    output_text: str,
) -> bool:
    """Verify source derivation, pointer replay, and exact serialization."""

    if type(receipt) is not QualitySourceSerializationReceiptV1:
        return False
    try:
        source_url = _bounded_source_url(source_url)
        output_text = _canonical_output(output_text)
        nested = object.__getattribute__(receipt, "selection_receipt")
        if not verify_quality_source_selection_receipt_v0(
            nested,
            raw_html=raw_html,
        ):
            return False
        expected_prompt_version = _validate_upstream_contract(
            upstream_revision=object.__getattribute__(nested, "upstream_revision"),
            prompt_profile=object.__getattribute__(nested, "prompt_profile"),
            response_format=object.__getattribute__(nested, "response_format"),
        )
        identity = _receipt_identity(receipt, nested)
        output_encoded = output_text.encode("utf-8")
    except (
        AttributeError,
        SourceSerializationReceiptError,
        TypeError,
        UnicodeEncodeError,
        ValueError,
    ):
        return False

    return (
        object.__getattribute__(receipt, "schema_version") == SOURCE_SERIALIZATION_RECEIPT_V1_SCHEMA
        and object.__getattribute__(receipt, "preprocessor_distribution")
        == MINERU_HTML_DISTRIBUTION
        and object.__getattribute__(receipt, "preprocessor_version") == MINERU_HTML_VERSION
        and object.__getattribute__(receipt, "preprocessor_revision") == MINERU_HTML_REVISION
        and object.__getattribute__(receipt, "preprocessor_entrypoint")
        == MINERU_HTML_PREPROCESSOR_ENTRYPOINT
        and object.__getattribute__(receipt, "preprocessor_cutoff_length")
        == MINERU_HTML_PREPROCESSOR_CUTOFF_LENGTH
        and object.__getattribute__(receipt, "model_prompt_version") == expected_prompt_version
        and object.__getattribute__(receipt, "serializer_distribution")
        == MINERU_WEBKIT_DISTRIBUTION
        and object.__getattribute__(receipt, "serializer_version") == MINERU_WEBKIT_VERSION
        and object.__getattribute__(receipt, "serializer_entrypoint") == MINERU_WEBKIT_ENTRYPOINT
        and object.__getattribute__(receipt, "serializer_output_format")
        == MINERU_WEBKIT_OUTPUT_FORMAT
        and object.__getattribute__(receipt, "source_url_sha256") == _sha256_text(source_url)
        and object.__getattribute__(receipt, "serializer_input_sha256")
        == nested.selected_html_sha256
        and object.__getattribute__(receipt, "output_sha256")
        == hashlib.sha256(output_encoded).hexdigest()
        and object.__getattribute__(receipt, "output_bytes") == len(output_encoded)
        and object.__getattribute__(receipt, "selection_receipt_sha256") == nested.receipt_sha256
        and object.__getattribute__(receipt, "source_derivation_replay_verified") is True
        and object.__getattribute__(receipt, "selection_replay_verified") is True
        and object.__getattribute__(receipt, "serialization_replay_verified") is True
        and object.__getattribute__(receipt, "process_authentication_scope")
        == PROCESS_AUTHENTICATION_SCOPE
        and hmac.compare_digest(
            object.__getattribute__(receipt, "process_authentication_mac"),
            _authenticate_identity(identity),
        )
        and object.__getattribute__(receipt, "digest_is_authentication") is False
        and object.__getattribute__(receipt, "receipt_sha256") == _sha256_json(identity)
    )


def _receipt_identity(
    receipt: QualitySourceSerializationReceiptV1,
    nested: QualitySourceSelectionReceiptV0,
) -> dict[str, object]:
    identity_names = (
        "schema_version",
        "preprocessor_distribution",
        "preprocessor_version",
        "preprocessor_revision",
        "preprocessor_entrypoint",
        "model_prompt_version",
        "serializer_distribution",
        "serializer_version",
        "serializer_entrypoint",
        "serializer_output_format",
        "process_authentication_scope",
    )
    digest_names = (
        "source_url_sha256",
        "serializer_input_sha256",
        "output_sha256",
        "selection_receipt_sha256",
    )
    values: dict[str, object] = {}
    for name in identity_names:
        values[name] = _bounded_identity(
            name,
            object.__getattribute__(receipt, name),
        )
    for name in digest_names:
        value = object.__getattribute__(receipt, name)
        if type(value) is not str or _SHA256_IDENTITY.fullmatch(value) is None:
            raise ValueError(f"{name} must be a canonical SHA-256 identity")
        values[name] = value

    replayed_selected_html = _bounded_serializer_input(
        object.__getattribute__(receipt, "replayed_selected_html")
    )
    if _sha256_text(replayed_selected_html) != values["serializer_input_sha256"]:
        raise ValueError("replayed selected HTML differs from its input identity")

    output_bytes = object.__getattribute__(receipt, "output_bytes")
    preprocessor_cutoff_length = object.__getattribute__(
        receipt,
        "preprocessor_cutoff_length",
    )
    source_derivation_replay_verified = object.__getattribute__(
        receipt,
        "source_derivation_replay_verified",
    )
    selection_replay_verified = object.__getattribute__(
        receipt,
        "selection_replay_verified",
    )
    serialization_replay_verified = object.__getattribute__(
        receipt,
        "serialization_replay_verified",
    )
    digest_is_authentication = object.__getattribute__(
        receipt,
        "digest_is_authentication",
    )
    receipt_sha256 = object.__getattribute__(receipt, "receipt_sha256")
    process_authentication_mac = object.__getattribute__(
        receipt,
        "process_authentication_mac",
    )
    if type(output_bytes) is not int or not 1 <= output_bytes <= (
        SOURCE_SERIALIZATION_RECEIPT_V1_MAX_OUTPUT_CHARS * 4
    ):
        raise ValueError("output_bytes is outside the receipt budget")
    if (
        type(preprocessor_cutoff_length) is not int
        or preprocessor_cutoff_length != MINERU_HTML_PREPROCESSOR_CUTOFF_LENGTH
    ):
        raise ValueError("preprocessor cutoff length is not pinned")
    if (
        type(source_derivation_replay_verified) is not bool
        or type(selection_replay_verified) is not bool
        or type(serialization_replay_verified) is not bool
        or type(digest_is_authentication) is not bool
    ):
        raise TypeError("receipt booleans must be exact bools")
    if type(receipt_sha256) is not str or _SHA256_IDENTITY.fullmatch(receipt_sha256) is None:
        raise ValueError("receipt_sha256 must be a canonical SHA-256 identity")
    if type(process_authentication_mac) is not bytes or len(process_authentication_mac) != 32:
        raise ValueError("process authentication MAC must be exactly 32 bytes")

    values.update(
        {
            "preprocessor_cutoff_length": preprocessor_cutoff_length,
            "output_bytes": output_bytes,
            "selection_receipt": _selection_receipt_payload(nested),
            "source_derivation_replay_verified": source_derivation_replay_verified,
            "selection_replay_verified": selection_replay_verified,
            "serialization_replay_verified": serialization_replay_verified,
            "digest_is_authentication": digest_is_authentication,
        }
    )
    return values


def _derive_source_artifacts(
    *,
    raw_html: object,
) -> tuple[str, str]:
    if type(raw_html) is not str:
        raise TypeError("raw_html must be an exact string")
    if not raw_html or len(raw_html) > SOURCE_SELECTION_RECEIPT_V0_MAX_INTERNAL_CHARS:
        raise SourceSerializationReceiptError("raw_html is outside the source budget")
    try:
        raw_html.encode("utf-8")
    except UnicodeEncodeError as error:
        raise SourceSerializationReceiptError("raw_html contains invalid Unicode") from error

    preprocessor = load_pinned_mineru_html_preprocessor_v1()
    try:
        result = preprocessor(
            raw_html,
            cutoff_length=MINERU_HTML_PREPROCESSOR_CUTOFF_LENGTH,
        )
    except Exception as error:
        raise SourceSerializationReceiptError(
            "pinned raw-source preprocessing replay failed"
        ) from error
    if type(result) is not tuple or len(result) != 2:
        raise SourceSerializationReceiptError(
            "pinned raw-source preprocessing replay returned an invalid shape"
        )
    simplified_html, mapped_html = result
    if type(simplified_html) is not str or type(mapped_html) is not str:
        raise SourceSerializationReceiptError(
            "pinned raw-source preprocessing replay did not return exact HTML"
        )
    return simplified_html, mapped_html


def _validate_upstream_contract(
    *,
    upstream_revision: object,
    prompt_profile: object,
    response_format: object,
) -> str:
    if type(upstream_revision) is not str or upstream_revision != MINERU_HTML_REVISION:
        raise SourceSerializationReceiptError("MinerU-HTML source revision is not pinned")
    expected_contracts = {
        "openai_json": ("v2", "json"),
        "mineru_compact": ("short_compact", "compact"),
    }
    if type(prompt_profile) is not str or type(response_format) is not str:
        raise TypeError("quality prompt contract identities must be exact strings")
    expected = expected_contracts.get(prompt_profile)
    if expected is None or expected[1] != response_format:
        raise SourceSerializationReceiptError(
            "quality prompt profile and response format do not match"
        )
    return expected[0]


def _selection_receipt_payload(
    receipt: QualitySourceSelectionReceiptV0,
) -> dict[str, object]:
    """Return every v0 field explicitly so the nested schema stays closed."""

    return {
        "schema_version": object.__getattribute__(receipt, "schema_version"),
        "upstream_revision": object.__getattribute__(receipt, "upstream_revision"),
        "prompt_profile": object.__getattribute__(receipt, "prompt_profile"),
        "response_format": object.__getattribute__(receipt, "response_format"),
        "source_sha256": object.__getattribute__(receipt, "source_sha256"),
        "model_response_sha256": object.__getattribute__(
            receipt,
            "model_response_sha256",
        ),
        "simplified_html_sha256": object.__getattribute__(
            receipt,
            "simplified_html_sha256",
        ),
        "mapped_html_sha256": object.__getattribute__(receipt, "mapped_html_sha256"),
        "selected_html_sha256": object.__getattribute__(
            receipt,
            "selected_html_sha256",
        ),
        "labels_sha256": object.__getattribute__(receipt, "labels_sha256"),
        "item_count": object.__getattribute__(receipt, "item_count"),
        "selected_count": object.__getattribute__(receipt, "selected_count"),
        "selected_item_ids": object.__getattribute__(receipt, "selected_item_ids"),
        "replay_verified": object.__getattribute__(receipt, "replay_verified"),
        "receipt_sha256": object.__getattribute__(receipt, "receipt_sha256"),
        "digest_is_authentication": object.__getattribute__(
            receipt,
            "digest_is_authentication",
        ),
    }


def _bounded_source_url(value: object) -> str:
    if type(value) is not str:
        raise TypeError("source_url must be an exact string")
    if len(value) > SOURCE_SERIALIZATION_RECEIPT_V1_MAX_URL_CHARS:
        raise QualitySourceInputIneligibleError("source_url exceeds the character budget")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise QualitySourceInputIneligibleError("source_url contains invalid Unicode") from error
    return value


def _bounded_serializer_input(value: object) -> str:
    if type(value) is not str:
        raise TypeError("replayed_selected_html must be an exact string")
    if not value:
        raise SourceSerializationReceiptError("replayed_selected_html must not be empty")
    if len(value) > SOURCE_SERIALIZATION_RECEIPT_V1_MAX_INPUT_CHARS:
        raise SourceSerializationReceiptError("replayed_selected_html exceeds the character budget")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise SourceSerializationReceiptError(
            "replayed_selected_html contains invalid Unicode"
        ) from error
    return value


def preflight_quality_source_input_v1(raw_html: object) -> None:
    """Bound MinerU-HTML preprocessing before dependency initialization.

    The pinned preprocessor recursively walks and copies the DOM and invokes
    descendant scans while constructing prompt items. A raw character limit
    alone does not bound that work: compact markup can encode tens of thousands
    of elements. This admission is intentionally stricter than the general
    deterministic crawler, which remains the fallback for ineligible pages.
    """

    if type(raw_html) is not str:
        raise TypeError("raw_html must be an exact string")
    if not raw_html or len(raw_html) > QUALITY_SOURCE_PREPROCESSOR_MAX_INPUT_CHARS:
        raise QualitySourceInputIneligibleError(
            "raw source is outside the quality preprocessor character budget"
        )
    try:
        encoded = raw_html.encode("utf-8")
    except UnicodeEncodeError as error:
        raise QualitySourceInputIneligibleError("raw source contains invalid Unicode") from error

    parser = lxml_html.HTMLParser(
        collect_ids=False,
        encoding="utf-8",
        remove_comments=True,
        remove_pis=True,
        no_network=True,
        recover=True,
        huge_tree=False,
    )
    try:
        root = lxml_html.fromstring(encoded, parser=parser)
    except (ParserError, TypeError, ValueError) as error:
        raise QualitySourceInputIneligibleError(
            "raw source is not parseable for quality preprocessing"
        ) from error

    node_count = 0
    text_fragment_count = 0
    stack: list[tuple[lxml_html.HtmlElement, int]] = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        node_count += 1
        if node_count > QUALITY_SOURCE_PREPROCESSOR_MAX_DOM_NODES:
            raise QualitySourceInputIneligibleError(
                "raw source exceeds the quality preprocessor node budget"
            )
        if depth > QUALITY_SOURCE_PREPROCESSOR_MAX_DOM_DEPTH:
            raise QualitySourceInputIneligibleError(
                "raw source exceeds the quality preprocessor depth budget"
            )

        tag = _local_html_tag(element.tag)
        if tag.startswith("cc"):
            raise QualitySourceInputIneligibleError("raw source contains a reserved quality tag")
        source_attributes = tuple(_local_html_tag(name) for name in element.attrib)
        if any(
            name in _SOURCE_RESERVED_ATTRIBUTES or name.startswith("cc-")
            for name in source_attributes
        ):
            raise QualitySourceInputIneligibleError(
                "raw source contains a reserved quality attribute"
            )

        text_fragment_count += sum(bool(fragment) for fragment in (element.text, element.tail))
        if text_fragment_count > QUALITY_SOURCE_PREPROCESSOR_MAX_TEXT_FRAGMENTS:
            raise QualitySourceInputIneligibleError(
                "raw source exceeds the quality preprocessor text-fragment budget"
            )

        for child in reversed(element.getchildren()):
            if isinstance(child, lxml_html.HtmlElement):
                stack.append((child, depth + 1))


def _preflight_serializer_structure(
    selected_html: str,
    *,
    source_url: str,
    max_output_chars: int,
) -> int:
    """Reject structures with superlinear MinerU-Webkit 0.1.6 work.

    The pinned converter materializes inline SVG as an uncapped Cairo surface,
    pads sparse Markdown tables to ``rows * max_columns``, repeats the base URL
    for relative images, and emits two spaces per nested-list level.  Its final
    output limit is necessarily too late to protect those allocations.  This
    preflight walks a separately parsed, already source-replayed DOM, applies
    fixed work bounds before the converter is called, and returns a conservative
    output/work projection for runtime conformance smoke tests.
    """

    parser = lxml_html.HTMLParser(
        collect_ids=False,
        encoding="utf-8",
        remove_blank_text=True,
        remove_comments=True,
        remove_pis=True,
        no_network=True,
        recover=True,
        huge_tree=False,
    )
    try:
        root = lxml_html.fromstring(selected_html.encode("utf-8"), parser=parser)
    except (ParserError, TypeError, ValueError) as error:
        raise SourceSerializationReceiptError(
            "selected source DOM is not parseable for serializer preflight"
        ) from error

    # MinerU-Webkit 0.1.6 selects host-specific transforms with a raw,
    # case-sensitive URL substring predicate. Reject a conservative
    # case-insensitive superset of every URL that can enter that mutable branch
    # (including lookalike hosts, userinfo, query strings, and paths).
    lowered_source_url = source_url.lower()
    if any(blocked in lowered_source_url for blocked in _QUALITY_SERIALIZATION_BLOCKED_HOSTS):
        raise SourceSerializationReceiptError(
            "source URL has unbounded serializer-specific transformations"
        )

    node_count = 0
    text_fragment_count = 0
    visible_text_chars = 0
    entity_escape_expansion_chars = 0
    code_element_count = 0
    math_delimiter_tokens = 0
    latex_control_tokens = 0
    projected_image_chars = 0
    list_indent_chars = 0
    top_level_tables: list[lxml_html.HtmlElement] = []
    stack: list[tuple[lxml_html.HtmlElement, int, int, int, bool]] = [(root, 1, 0, 0, False)]
    while stack:
        element, depth, list_depth, table_depth, ancestor_math_suppressed = stack.pop()
        node_count += 1
        if node_count > SOURCE_SERIALIZATION_RECEIPT_V1_MAX_DOM_NODES:
            raise SourceSerializationReceiptError(
                "selected source DOM exceeds the serializer node budget"
            )
        if depth > SOURCE_SERIALIZATION_RECEIPT_V1_MAX_DOM_DEPTH:
            raise SourceSerializationReceiptError(
                "selected source DOM exceeds the serializer depth budget"
            )

        tag = _local_html_tag(element.tag)
        if tag.startswith("cc"):
            raise SourceSerializationReceiptError(
                "internal converter tags are outside the source DOM contract"
            )
        if tag == "script":
            raise SourceSerializationReceiptError(
                "scripts are outside the bounded serializer contract"
            )
        if tag == "svg":
            raise SourceSerializationReceiptError(
                "inline SVG is outside the bounded serializer contract"
            )
        if tag in _MATHML_TAGS:
            raise SourceSerializationReceiptError(
                "MathML is outside the bounded serializer contract"
            )
        if _has_math_markup_signal(element):
            raise SourceSerializationReceiptError(
                "formula markup is outside the bounded serializer contract"
            )

        current_math_suppressed = ancestor_math_suppressed or tag in _MATHJAX_TEXT_SKIP_TAGS
        fragments = (
            (element.text, current_math_suppressed),
            # An element's tail belongs to its parent and is therefore still
            # scanned when only the element itself is a MathJax skip tag.
            (element.tail, ancestor_math_suppressed),
        )
        for fragment, math_suppressed in fragments:
            if fragment:
                text_fragment_count += 1
                visible_text_chars += len(fragment)
                # TextParagraphRecognizer expands each literal greater-than
                # sign to ``&gt;``. Count the three added characters even when
                # canonical source markup already encoded the input; this is a
                # deliberately conservative bound across every recognizer.
                entity_escape_expansion_chars += 3 * fragment.count(">")
                if not math_suppressed:
                    math_delimiter_tokens += _math_delimiter_token_count(fragment)
                    latex_control_tokens += _latex_control_token_count(fragment)
                    if (
                        _latex_group_depth(fragment)
                        > SOURCE_SERIALIZATION_RECEIPT_V1_MAX_LATEX_GROUP_DEPTH
                    ):
                        raise SourceSerializationReceiptError(
                            "selected source text exceeds the serializer LaTeX-group-depth budget"
                        )
                    if (
                        math_delimiter_tokens
                        > SOURCE_SERIALIZATION_RECEIPT_V1_MAX_MATH_DELIMITER_TOKENS
                    ):
                        raise SourceSerializationReceiptError(
                            "selected source text exceeds the serializer math-delimiter budget"
                        )
                    if (
                        latex_control_tokens
                        > SOURCE_SERIALIZATION_RECEIPT_V1_MAX_LATEX_CONTROL_TOKENS
                    ):
                        raise SourceSerializationReceiptError(
                            "selected source text exceeds the serializer LaTeX-control budget"
                        )
        if text_fragment_count > SOURCE_SERIALIZATION_RECEIPT_V1_MAX_TEXT_FRAGMENTS:
            raise SourceSerializationReceiptError(
                "selected source DOM exceeds the serializer text-fragment budget"
            )

        if tag in {"code", "pre"}:
            code_element_count += 1

        next_list_depth = list_depth + 1 if tag in _LIST_CONTAINER_TAGS else list_depth
        if next_list_depth > SOURCE_SERIALIZATION_RECEIPT_V1_MAX_LIST_DEPTH:
            raise SourceSerializationReceiptError(
                "selected source lists exceed the serializer nesting budget"
            )
        if tag in _LIST_ITEM_TAGS and next_list_depth:
            list_indent_chars += (2 * (next_list_depth - 1)) + 16
            list_budget = max(
                16 * 1024,
                min(
                    SOURCE_SERIALIZATION_RECEIPT_V1_MAX_LIST_INDENT_CHARS,
                    max_output_chars,
                ),
            )
            if list_indent_chars > list_budget:
                raise SourceSerializationReceiptError(
                    "selected source lists exceed the serializer indentation budget"
                )

        if tag == "table" and table_depth == 0:
            top_level_tables.append(element)
        next_table_depth = table_depth + 1 if tag == "table" else table_depth

        if _is_image_candidate(element, tag):
            largest_attribute = max(
                (len(value) for value in element.attrib.values()),
                default=0,
            )
            projected_image_chars += len(source_url) + largest_attribute + 128
            image_budget = max(
                16 * 1024,
                min(
                    SOURCE_SERIALIZATION_RECEIPT_V1_MAX_PROJECTED_IMAGE_CHARS,
                    max_output_chars,
                ),
            )
            if projected_image_chars > image_budget:
                raise SourceSerializationReceiptError(
                    "selected source images exceed the serializer expansion budget"
                )

        children = element.getchildren()
        for child in reversed(children):
            if isinstance(child, lxml_html.HtmlElement):
                stack.append(
                    (
                        child,
                        depth + 1,
                        next_list_depth,
                        next_table_depth,
                        current_math_suppressed,
                    )
                )

    code_work = code_element_count * (node_count + text_fragment_count)
    if code_work > SOURCE_SERIALIZATION_RECEIPT_V1_MAX_CODE_WORK:
        raise SourceSerializationReceiptError(
            "selected source code exceeds the serializer work budget"
        )
    table_slot_budget = max(
        64,
        min(
            SOURCE_SERIALIZATION_RECEIPT_V1_MAX_TABLE_GRID_SLOTS,
            max_output_chars // 3,
        ),
    )
    table_grid_slots = 0
    table_projection_chars = 0
    for table in top_level_tables:
        descendants = list(table.iterdescendants())
        if any(_local_html_tag(element.tag) == "table" for element in descendants):
            # MinerU emits nested/complex tables as bounded source HTML rather
            # than padding them into a rectangular Markdown grid.
            continue
        rows = [element for element in descendants if _local_html_tag(element.tag) == "tr"]
        max_columns = 0
        escaped_cell_text_chars = 0
        for row in rows:
            row_descendants = list(row.iterdescendants())
            if any(_local_html_tag(element.tag) == "tr" for element in row_descendants):
                raise SourceSerializationReceiptError(
                    "nested table rows are outside the bounded serializer contract"
                )
            cells = [
                element
                for element in row_descendants
                if _local_html_tag(element.tag) in {"td", "th"}
            ]
            columns = len(cells)
            escaped_cell_text_chars += sum(
                len(_escaped_table_cell_text(cell.text_content())) for cell in cells
            )
            max_columns = max(max_columns, columns)
        grid_slots = len(rows) * max_columns
        table_grid_slots += grid_slots
        if rows and max_columns:
            table_projection_chars += (
                escaped_cell_text_chars + (3 * grid_slots) + (2 * len(rows)) + (4 * max_columns) + 1
            )
        if table_grid_slots > table_slot_budget:
            raise SourceSerializationReceiptError(
                "selected source tables exceed the serializer grid budget"
            )

    projected_work_chars = (
        len(selected_html)
        + visible_text_chars
        + (18 * node_count)
        + entity_escape_expansion_chars
        + (64 * math_delimiter_tokens)
        + (8 * latex_control_tokens)
        + projected_image_chars
        + table_projection_chars
        + list_indent_chars
    )
    if projected_work_chars > max_output_chars:
        raise SourceSerializationReceiptError(
            "selected source DOM exceeds the projected serializer output budget"
        )
    return projected_work_chars


def _math_delimiter_token_count(value: str) -> int:
    """Count every pinned default MathJax delimiter or environment boundary."""

    lowered = value.lower()
    return (
        value.count("$")
        + value.count(r"\(")
        + value.count(r"\)")
        + value.count(r"\[")
        + value.count(r"\]")
        + lowered.count("[itex]")
        + lowered.count("[/itex]")
        + lowered.count("[tex]")
        + lowered.count("[/tex]")
        + lowered.count(r"\begin{")
        + lowered.count(r"\end{")
    )


def _latex_control_token_count(value: str) -> int:
    """Bound LatexWalker work even when text has no matched math delimiter."""

    return value.count("\\") + value.count("{") + value.count("}")


def _latex_group_depth(value: str) -> int:
    """Conservatively bound recursive LatexWalker group parsing per fragment."""

    depth = 0
    maximum = 0
    for character in value:
        if character == "{":
            depth += 1
            maximum = max(maximum, depth)
        elif character == "}" and depth:
            depth -= 1
    return maximum


def _local_html_tag(value: object) -> str:
    if type(value) is not str:
        return ""
    return value.rsplit("}", 1)[-1].lower()


def _is_image_candidate(element: lxml_html.HtmlElement, tag: str) -> bool:
    if tag in _IMAGE_CANDIDATE_TAGS:
        return True
    lowered_attributes = {
        _local_html_tag(key): value.lower() for key, value in element.attrib.items()
    }
    if lowered_attributes.get("src", "").startswith("data:image/"):
        return True
    if lowered_attributes.get("href", "").startswith("data:image/"):
        return True
    return "image-embed" in lowered_attributes.get(
        "class",
        "",
    ) or "image-embed" in lowered_attributes.get("id", "")


def _escaped_table_cell_text(value: str) -> str:
    return re.sub(r"[\r\n]+", " ", value.strip()).replace("|", "\\|")


def _has_math_markup_signal(element: lxml_html.HtmlElement) -> bool:
    attributes = {_local_html_tag(key): value.lower() for key, value in element.attrib.items()}
    class_tokens = frozenset(attributes.get("class", "").split())
    if any(
        token == "math"
        or token.startswith("katex")
        or token.startswith("mathjax")
        or token == "ztext-math"
        for token in class_tokens
    ):
        return True
    if {"alttext", "data-math", "data-tex"} & attributes.keys():
        return True
    return any(
        marker in attributes.get(name, "")
        for name in ("src", "role")
        for marker in ("katex", "latex", "mathjax")
    )


def _canonical_output(
    value: object,
    *,
    max_output_chars: int = SOURCE_SERIALIZATION_RECEIPT_V1_MAX_OUTPUT_CHARS,
) -> str:
    if type(value) is not str:
        raise TypeError("output_text must be an exact string")
    if not value or value != value.strip():
        raise SourceSerializationReceiptError(
            "output_text must be non-empty and canonically stripped"
        )
    if len(value) > _bounded_output_limit(max_output_chars):
        raise SourceSerializationReceiptError("output_text exceeds the character budget")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise SourceSerializationReceiptError("output_text contains invalid Unicode") from error
    return value


def _bounded_output_limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= (
        SOURCE_SERIALIZATION_RECEIPT_V1_MAX_OUTPUT_CHARS
    ):
        raise SourceSerializationReceiptError(
            "max_output_chars is outside the serializer output budget"
        )
    return value


def _bounded_identity(name: str, value: object) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be an exact string")
    if not value or len(value) > 128 or not value.isascii():
        raise SourceSerializationReceiptError(f"{name} is not a bounded ASCII identity")
    return value


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


def _authenticate_identity(value: object) -> bytes:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    authenticated = _PROCESS_AUTHENTICATION_DOMAIN + b"\x00" + payload
    return hmac.digest(_process_authentication_key(), authenticated, "sha256")


def _process_authentication_key() -> bytes:
    """Return a lazy key that automatically rotates in post-fork workers."""

    global _PROCESS_AUTHENTICATION_KEY, _PROCESS_AUTHENTICATION_PID
    current_pid = os.getpid()
    key = _PROCESS_AUTHENTICATION_KEY
    if current_pid == _PROCESS_AUTHENTICATION_PID and type(key) is bytes:
        return key
    with _PROCESS_AUTHENTICATION_KEY_LOCK:
        key = _PROCESS_AUTHENTICATION_KEY
        if current_pid != _PROCESS_AUTHENTICATION_PID or type(key) is not bytes:
            key = secrets.token_bytes(32)
            _PROCESS_AUTHENTICATION_KEY = key
            _PROCESS_AUTHENTICATION_PID = current_pid
        return key

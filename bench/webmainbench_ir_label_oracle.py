#!/usr/bin/env python3
"""Non-claimable WebMainBench label oracle for ordered-DOM IR.

This diagnostic deliberately reads WebMainBench ground-truth marker attributes
to answer one narrow pre-training question: if a perfect classifier knew which
source regions were labelled, how much of the official score can the current
``ordered-dom-ir.v1`` selectable-unit and reconstruction contract represent?

It is benchmark code, never a production extraction strategy.  It does not
call or modify the production extractor, and every artifact is permanently
marked ``label_oracle`` and ``claimable: false``.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from lxml import etree
from lxml import html as lxml_html

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from clusy_native import (  # noqa: E402
    DEFAULT_DOCUMENT_IR_LIMITS,
    extract_document_ir,
)

from app.services.document_ir_selection import (  # noqa: E402
    BlockSelection,
    ReconstructionLimits,
    prepare_document_ir,
    reconstruct_block_selection,
)
from bench.webmainbench_benchmark import (  # noqa: E402
    BREAKDOWN_FIELDS,
    DATASET_RECORDS,
    EVALUATOR_COMMIT,
    BenchmarkError,
    _official_score,
    load_official_scorer,
    verify_dataset,
    verify_evaluator,
)

DIAGNOSTIC_SCHEMA_VERSION = "ordered-dom-ir.label-oracle.v1"
LABEL_ORACLE = "label_oracle"
SELECTION_POLICY = "ground-truth-marker-intersection-over-selectable-units.v1"
NON_CLAIMABLE_WARNING = (
    "LABEL_ORACLE — NOT CLAIMABLE: ground-truth annotations directly choose "
    "IR blocks; this measures a representation/reconstruction ceiling, not "
    "model quality, production extraction quality, or a leaderboard result."
)
ACKNOWLEDGEMENT_FLAG = "--acknowledge-label-oracle-not-claimable"

CANONICALIZER_RELATIVE_PATH = Path("eval_baselines/baselines/base.py")
CANONICALIZER_SHA256 = "96d5475f48a78061a9ba98fa1a87a12bc7f3d4e83c4ff8269ecb3980f1ebaa36"
CANONICALIZER_DEPENDENCIES = {
    "html2text": "2025.4.15",
    "html-text": "0.7.0",
}

GROUND_TRUTH_ATTRIBUTE = "cc-select"
GROUND_TRUTH_VALUE = "true"
GROUND_TRUTH_UID_ATTRIBUTE = "data-anno-uid"
TABLE_TAGS = frozenset({"table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption"})
LIST_TAGS = frozenset({"ul", "ol", "li", "dl", "dt", "dd"})
NONVISIBLE_TAGS = frozenset(
    {
        "head",
        "script",
        "style",
        "template",
        "noscript",
        "svg",
    }
)
DIAGNOSTIC_FEATURES = (
    "ground_truth_table_markup",
    "ground_truth_list_markup",
    "selected_table_unit",
    "selected_list_unit",
    "mixed_selected_block",
    "mixed_table_or_list_block",
    "coarse_selected_block",
    "overlapping_selectable_units",
    "label_alignment_ambiguous",
    "selected_marker_alignment_incomplete",
    "unrepresented_label_marker",
    "unselectable_label_marker",
    "unrepresented_table_or_list_marker",
    "reconstruction_dropped_label_marker",
    "incomplete_label_char_coverage",
    "incomplete_selectable_label_char_coverage",
    "reconstruction_dropped_label_chars",
    "ir_truncated",
    "ir_input_truncated",
    "ir_nodes_truncated",
    "ir_depth_truncated",
    "ir_blocks_truncated",
    "ir_block_text_truncated",
    "ir_block_html_truncated",
    "selectable_source_html_missing",
    "selectable_source_html_unparseable",
    "reconstruction_incomplete",
    "selected_source_html_truncated",
    "zero_selected_blocks",
)
_TABLE_MARKUP_RE = re.compile(r"<\s*(?:table|thead|tbody|tfoot|tr|td|th|caption)\b", re.I)
_LIST_MARKUP_RE = re.compile(r"<\s*(?:ul|ol|li|dl|dt|dd)\b", re.I)
_DIAGNOSTIC_IMPORT = "bench.webmainbench_ir_label_oracle"
_SHA256_ZERO = hashlib.sha256(b"").hexdigest()


class LabelOracleError(RuntimeError):
    """A label, integrity, isolation, or diagnostic condition failed closed."""


@dataclass(frozen=True, slots=True)
class OracleRecord:
    dataset_index: int
    track_id: str
    url: str
    html: str
    main_html: str
    reference: str
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LabelProfile:
    marker_count: int
    marker_without_uid_count: int
    marker_uid_to_id: dict[str, int]
    marker_context_tags: dict[int, frozenset[str]]
    uid_to_marker_ids: dict[str, frozenset[int]]
    fully_selected_uids: frozenset[str]
    duplicate_uids: tuple[str, ...]
    root_uid: str | None
    visible_non_whitespace_chars: int
    selected_non_whitespace_chars: int


@dataclass(frozen=True, slots=True)
class SelectedUnit:
    block_id: str
    tag: str
    context_tags: frozenset[str]
    marker_ids: frozenset[int]
    visible_non_whitespace_chars: int
    selected_non_whitespace_chars: int
    noise_non_whitespace_chars: int
    mixed: bool
    coarse: bool
    html_truncated: bool
    root_uid: str | None
    marker_alignment_complete: bool


@dataclass(frozen=True, slots=True)
class OracleUnitSelection:
    candidates: tuple[SelectedUnit, ...]
    selected: tuple[SelectedUnit, ...]
    overlap_pairs: int
    missing_source_html_blocks: int
    unparseable_source_html_blocks: int

    @property
    def dropped_overlap_candidates(self) -> int:
        return len(self.candidates) - len(self.selected)


@dataclass
class ScoreAggregate:
    pages: int = 0
    precision_sum: float = 0.0
    recall_sum: float = 0.0
    f1_sum: float = 0.0

    def add(self, score: Mapping[str, Any]) -> None:
        precision = _finite_score(score.get("precision"), "precision")
        recall = _finite_score(score.get("recall"), "recall")
        f1 = _finite_score(score.get("f1"), "f1")
        self.pages += 1
        self.precision_sum += precision
        self.recall_sum += recall
        self.f1_sum += f1

    def export(self) -> dict[str, int | float]:
        denominator = self.pages or 1
        return {
            "pages": self.pages,
            "precision": self.precision_sum / denominator,
            "recall": self.recall_sum / denominator,
            "f1": self.f1_sum / denominator,
        }


@dataclass
class OracleAggregate:
    score: ScoreAggregate = field(default_factory=ScoreAggregate)
    ground_truth_recanonicalization_score: ScoreAggregate = field(
        default_factory=ScoreAggregate
    )
    metadata: dict[str, dict[str, ScoreAggregate]] = field(
        default_factory=lambda: {
            name: defaultdict(ScoreAggregate) for name in BREAKDOWN_FIELDS
        }
    )
    diagnostic: dict[str, dict[str, ScoreAggregate]] = field(
        default_factory=lambda: {
            name: defaultdict(ScoreAggregate) for name in DIAGNOSTIC_FEATURES
        }
    )
    counters: Counter[str] = field(default_factory=Counter)
    selected_tags: Counter[str] = field(default_factory=Counter)
    issue_pages: Counter[str] = field(default_factory=Counter)
    exact_ground_truth_recanonicalizations: int = 0
    completed_track_ids: set[str] = field(default_factory=set)

    def add(self, page: Mapping[str, Any]) -> None:
        track_id = _required_string(page, "track_id")
        if track_id in self.completed_track_ids:
            raise LabelOracleError(f"duplicate diagnostic track_id: {track_id}")
        self.completed_track_ids.add(track_id)

        score = _required_mapping(page, "score")
        recanonicalization = _required_mapping(
            page,
            "ground_truth_recanonicalization_score",
        )
        metadata = _required_mapping(page, "metadata")
        features = _required_mapping(page, "features")
        diagnostics = _required_mapping(page, "diagnostics")

        self.score.add(score)
        self.ground_truth_recanonicalization_score.add(recanonicalization)
        self.exact_ground_truth_recanonicalizations += int(
            page.get("ground_truth_recanonicalization_exact") is True
        )
        for field_name in BREAKDOWN_FIELDS:
            category = _category(metadata.get(field_name))
            self.metadata[field_name][category].add(score)
        for feature_name in DIAGNOSTIC_FEATURES:
            value = features.get(feature_name)
            if type(value) is not bool:
                raise LabelOracleError(f"feature {feature_name} must be boolean")
            category = "true" if value else "false"
            self.diagnostic[feature_name][category].add(score)
            if value:
                self.issue_pages[feature_name] += 1

        for name in (
            "source_label_markers",
            "source_duplicate_annotation_uids",
            "source_markers_without_uid",
            "source_table_label_markers",
            "source_list_label_markers",
            "selected_label_markers",
            "selected_table_label_markers",
            "selected_list_label_markers",
            "emitted_label_markers",
            "emitted_table_label_markers",
            "emitted_list_label_markers",
            "source_selected_non_whitespace_chars",
            "selected_label_non_whitespace_chars",
            "emitted_label_non_whitespace_chars",
            "selected_noise_non_whitespace_chars",
            "emitted_noise_non_whitespace_chars",
            "ir_blocks",
            "ir_selectable_blocks",
            "raw_intersecting_selectable_blocks",
            "overlap_pairs",
            "dropped_overlap_candidates",
            "missing_selectable_source_html_blocks",
            "unparseable_selectable_source_html_blocks",
            "selected_blocks",
            "emitted_blocks",
            "mixed_selected_blocks",
            "mixed_table_or_list_blocks",
            "coarse_selected_blocks",
        ):
            self.counters[name] += _required_nonnegative_int(diagnostics, name)
        selected_tag_counts = _required_mapping(diagnostics, "selected_tag_counts")
        for tag, value in selected_tag_counts.items():
            if not isinstance(tag, str):
                raise LabelOracleError("selected tag name must be a string")
            self.selected_tags[tag] += _nonnegative_int(value, f"selected tag {tag}")

    def export(self) -> dict[str, Any]:
        pages = self.score.pages
        if self.ground_truth_recanonicalization_score.pages != pages:
            raise LabelOracleError(
                "ground-truth recanonicalization page count diverged from oracle score"
            )
        if not 0 <= self.exact_ground_truth_recanonicalizations <= pages:
            raise LabelOracleError(
                "ground-truth recanonicalization exact count is invalid"
            )
        source_markers = self.counters["source_label_markers"]
        source_chars = self.counters["source_selected_non_whitespace_chars"]
        return {
            "pages": pages,
            "score_ceiling": self.score.export(),
            "ground_truth_recanonicalization": {
                **self.ground_truth_recanonicalization_score.export(),
                "exact_pages": self.exact_ground_truth_recanonicalizations,
                "exact_rate": (
                    self.exact_ground_truth_recanonicalizations / pages if pages else 0.0
                ),
            },
            "metadata_score_ceilings": {
                field_name: {
                    category: aggregate.export()
                    for category, aggregate in sorted(categories.items())
                }
                for field_name, categories in self.metadata.items()
            },
            "diagnostic_score_ceilings": {
                feature_name: {
                    category: aggregate.export()
                    for category, aggregate in sorted(categories.items())
                }
                for feature_name, categories in self.diagnostic.items()
            },
            "coverage": {
                "source_label_markers": source_markers,
                "source_duplicate_annotation_uids": self.counters[
                    "source_duplicate_annotation_uids"
                ],
                "source_markers_without_uid": self.counters[
                    "source_markers_without_uid"
                ],
                "source_table_label_markers": self.counters[
                    "source_table_label_markers"
                ],
                "source_list_label_markers": self.counters[
                    "source_list_label_markers"
                ],
                "selected_label_markers": self.counters["selected_label_markers"],
                "selected_table_label_markers": self.counters[
                    "selected_table_label_markers"
                ],
                "selected_list_label_markers": self.counters[
                    "selected_list_label_markers"
                ],
                "emitted_label_markers": self.counters["emitted_label_markers"],
                "emitted_table_label_markers": self.counters[
                    "emitted_table_label_markers"
                ],
                "emitted_list_label_markers": self.counters[
                    "emitted_list_label_markers"
                ],
                "selected_marker_recall": _ratio(
                    self.counters["selected_label_markers"],
                    source_markers,
                ),
                "emitted_marker_recall": _ratio(
                    self.counters["emitted_label_markers"],
                    source_markers,
                ),
                "source_selected_non_whitespace_chars": source_chars,
                "selected_label_non_whitespace_chars": self.counters[
                    "selected_label_non_whitespace_chars"
                ],
                "emitted_label_non_whitespace_chars": self.counters[
                    "emitted_label_non_whitespace_chars"
                ],
                "selected_label_char_recall": _ratio(
                    self.counters["selected_label_non_whitespace_chars"],
                    source_chars,
                ),
                "emitted_label_char_recall": _ratio(
                    self.counters["emitted_label_non_whitespace_chars"],
                    source_chars,
                ),
                "selected_noise_non_whitespace_chars": self.counters[
                    "selected_noise_non_whitespace_chars"
                ],
                "emitted_noise_non_whitespace_chars": self.counters[
                    "emitted_noise_non_whitespace_chars"
                ],
            },
            "unit_totals": {
                key: self.counters[key]
                for key in (
                    "ir_blocks",
                    "ir_selectable_blocks",
                    "raw_intersecting_selectable_blocks",
                    "overlap_pairs",
                    "dropped_overlap_candidates",
                    "missing_selectable_source_html_blocks",
                    "unparseable_selectable_source_html_blocks",
                    "selected_blocks",
                    "emitted_blocks",
                    "mixed_selected_blocks",
                    "mixed_table_or_list_blocks",
                    "coarse_selected_blocks",
                )
            },
            "issue_pages": dict(sorted(self.issue_pages.items())),
            "selected_tag_counts": dict(sorted(self.selected_tags.items())),
        }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Measure the non-claimable label-oracle representation ceiling of "
            "ordered-dom-ir.v1 on pinned WebMainBench."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path(tempfile.gettempdir()) / "clusy-webmainbench/webmainbench.jsonl",
        help="exact pinned WebMainBench JSONL",
    )
    parser.add_argument(
        "--evaluator-root",
        type=Path,
        default=Path(tempfile.gettempdir()) / "clusy-mineru-html-src",
        help="clean checkout of the pinned official evaluator",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "bench/results/webmainbench-ir-label-oracle/latest",
        help="new artifact directory; existing directories are refused",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        help=(
            "deterministic hash-ranked corpus sample; omitted means all 7,809 "
            "rows, and every size remains non-claimable"
        ),
    )
    parser.add_argument(
        "--sample-seed",
        default=DIAGNOSTIC_SCHEMA_VERSION,
        help="stable UTF-8 seed for deterministic index hash ranking",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=100,
        help="print one progress line after this many completed pages",
    )
    parser.add_argument(
        ACKNOWLEDGEMENT_FLAG,
        action="store_true",
        dest="acknowledge_label_oracle_not_claimable",
        help="required acknowledgement that this label-using result is never claimable",
    )
    return parser.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    if not args.acknowledge_label_oracle_not_claimable:
        raise LabelOracleError(
            f"{ACKNOWLEDGEMENT_FLAG} is required; {NON_CLAIMABLE_WARNING}"
        )
    if args.sample_size is not None and not 1 <= args.sample_size <= DATASET_RECORDS:
        raise LabelOracleError(f"sample_size must be between 1 and {DATASET_RECORDS}")
    if not isinstance(args.sample_seed, str) or not args.sample_seed:
        raise LabelOracleError("sample_seed must be a non-empty string")
    if type(args.progress_every) is not int or args.progress_every <= 0:
        raise LabelOracleError("progress_every must be a positive integer")


def assert_benchmark_only_isolation(root: Path = ROOT) -> dict[str, Any]:
    """Fail if any production Python source imports benchmark code."""

    app_root = root / "app"
    scanned: list[str] = []
    violations: list[str] = []
    if not app_root.is_dir():
        raise LabelOracleError(f"production app directory is missing: {app_root}")
    for path in sorted(app_root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        scanned.append(relative)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, SyntaxError) as error:
            raise LabelOracleError(
                f"could not inspect production source {relative}: {error}"
            ) from error
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name == "bench" or name.startswith("bench.") for name in names):
                violations.append(f"{relative}:{node.lineno}")
    if violations:
        raise LabelOracleError(
            "production source imports benchmark code: " + ", ".join(violations)
        )
    return {
        "passed": True,
        "policy": (
            "no app/**/*.py import may target bench or any bench submodule; "
            f"the diagnostic module {_DIAGNOSTIC_IMPORT} remains benchmark-only"
        ),
        "scanned_files": scanned,
        "scanned_files_sha256": _hash_json(scanned),
        "violations": [],
    }


def deterministic_indices(sample_size: int | None, seed: str) -> tuple[int, ...]:
    """Return full order or a stable hash-ranked sample in corpus order."""

    if sample_size is None:
        return tuple(range(DATASET_RECORDS))
    if not 1 <= sample_size <= DATASET_RECORDS:
        raise LabelOracleError(f"sample_size must be between 1 and {DATASET_RECORDS}")
    encoded_seed = seed.encode("utf-8")
    ranked = sorted(
        range(DATASET_RECORDS),
        key=lambda index: (
            hashlib.sha256(encoded_seed + b"\0" + str(index).encode("ascii")).digest(),
            index,
        ),
    )
    return tuple(sorted(ranked[:sample_size]))


def parse_oracle_record(line: str, dataset_index: int) -> OracleRecord:
    try:
        document = json.loads(line)
    except json.JSONDecodeError as error:
        raise LabelOracleError(
            f"invalid dataset JSON at zero-based row {dataset_index}: {error}"
        ) from error
    if not isinstance(document, dict):
        raise LabelOracleError(f"dataset row {dataset_index} is not an object")

    track_id = document.get("track_id")
    html = document.get("html")
    main_html = document.get("main_html")
    reference = document.get("convert_main_content")
    metadata = document.get("meta")
    if not isinstance(track_id, str) or not track_id:
        raise LabelOracleError(f"dataset row {dataset_index} has invalid track_id")
    if not isinstance(html, str) or not html:
        raise LabelOracleError(f"dataset row {dataset_index} has invalid HTML")
    if not isinstance(main_html, str) or not main_html:
        raise LabelOracleError(
            f"dataset row {dataset_index} must contain ground-truth main_html"
        )
    if not isinstance(reference, str):
        raise LabelOracleError(
            f"dataset row {dataset_index} has invalid convert_main_content"
        )
    if not isinstance(metadata, dict):
        raise LabelOracleError(f"dataset row {dataset_index} has invalid metadata")
    url_value = document.get("url", "")
    return OracleRecord(
        dataset_index=dataset_index,
        track_id=track_id,
        url=str(url_value or ""),
        html=html,
        main_html=main_html,
        reference=reference,
        metadata=cast("dict[str, Any]", metadata),
    )


def selected_records(dataset: Path, indices: Sequence[int]) -> Iterator[OracleRecord]:
    wanted = iter(indices)
    next_index = next(wanted, None)
    found = 0
    with dataset.open("r", encoding="utf-8") as stream:
        for dataset_index, line in enumerate(stream):
            if next_index is None:
                break
            if dataset_index != next_index:
                continue
            yield parse_oracle_record(line, dataset_index)
            found += 1
            next_index = next(wanted, None)
    if found != len(indices) or next_index is not None:
        raise LabelOracleError(
            f"dataset selection expected {len(indices)} rows, found {found}"
        )


def build_label_profile(
    html: str,
    *,
    context: str,
    require_ground_truth_marker: bool,
) -> LabelProfile:
    """Parse marker ancestry and visible labelled characters deterministically."""

    if not isinstance(html, str) or not html:
        raise LabelOracleError(f"{context} HTML must be a non-empty string")
    parser = lxml_html.HTMLParser(recover=True, huge_tree=True)
    try:
        # ``fromstring`` preserves a block fragment's actual root element and
        # its stable annotation UID; ``document_fromstring`` would inject an
        # unlabelled ``html`` wrapper around fragments.
        root = lxml_html.fromstring(html, parser=parser)
    except (etree.ParserError, ValueError, TypeError) as error:
        raise LabelOracleError(f"{context} HTML could not be parsed: {error}") from error

    elements = [element for element in root.iter() if isinstance(element.tag, str)]
    uid_owner: dict[str, Any] = {}
    duplicate_uids: set[str] = set()
    for element in elements:
        uid = _uid(element)
        if uid is None:
            continue
        if uid in uid_owner:
            duplicate_uids.add(uid)
        else:
            uid_owner[uid] = element

    markers = [element for element in elements if _is_ground_truth_marker(element)]
    if require_ground_truth_marker and not markers:
        raise LabelOracleError(
            f"{context} is missing required {GROUND_TRUTH_ATTRIBUTE}="
            f"{GROUND_TRUTH_VALUE!r} ground-truth markers"
        )

    marker_ids = {element: index for index, element in enumerate(markers)}
    marker_uid_to_id: dict[str, int] = {}
    marker_context_tags: dict[int, frozenset[str]] = {}
    marker_without_uid_count = 0
    for marker, marker_id in marker_ids.items():
        marker_context_tags[marker_id] = frozenset(
            _tag(element) for element in (marker, *marker.iterancestors())
        )
        uid = _uid(marker)
        if uid is None:
            marker_without_uid_count += 1
        elif uid in duplicate_uids:
            continue
        elif uid in marker_uid_to_id:
            duplicate_uids.add(uid)
            marker_uid_to_id.pop(uid, None)
        else:
            marker_uid_to_id[uid] = marker_id

    fully_selected_elements: set[Any] = set()
    uid_to_marker_ids_mutable: dict[str, set[int]] = defaultdict(set)
    for marker, marker_id in marker_ids.items():
        for element in (marker, *marker.iterancestors()):
            uid = _uid(element)
            if uid is not None:
                uid_to_marker_ids_mutable[uid].add(marker_id)
        for element in marker.iter():
            if not isinstance(element.tag, str):
                continue
            fully_selected_elements.add(element)
            uid = _uid(element)
            if uid is not None:
                uid_to_marker_ids_mutable[uid].add(marker_id)

    visible_context: dict[Any, bool] = {}
    visible_chars = 0
    selected_chars = 0
    for element in elements:
        parent = element.getparent()
        parent_visible = (
            visible_context.get(parent, True) if parent is not None else True
        )
        visible = parent_visible and _tag(element) not in NONVISIBLE_TAGS
        visible_context[element] = visible
        if visible and element.text:
            count = _non_whitespace_chars(element.text)
            visible_chars += count
            if element in fully_selected_elements:
                selected_chars += count
        if element.tail and parent_visible:
            count = _non_whitespace_chars(element.tail)
            visible_chars += count
            if parent in fully_selected_elements:
                selected_chars += count

    root_uid = _uid(root)
    return LabelProfile(
        marker_count=len(markers),
        marker_without_uid_count=marker_without_uid_count,
        marker_uid_to_id=marker_uid_to_id,
        marker_context_tags=marker_context_tags,
        uid_to_marker_ids={
            uid: frozenset(ids)
            for uid, ids in sorted(uid_to_marker_ids_mutable.items())
            if uid not in duplicate_uids
        },
        fully_selected_uids=frozenset(
            uid
            for element in fully_selected_elements
            if (uid := _uid(element)) is not None
            and uid not in duplicate_uids
        ),
        duplicate_uids=tuple(sorted(duplicate_uids)),
        root_uid=root_uid,
        visible_non_whitespace_chars=visible_chars,
        selected_non_whitespace_chars=selected_chars,
    )


def oracle_selected_units(
    document: Any,
    source_profile: LabelProfile,
) -> OracleUnitSelection:
    """Choose a non-overlapping selectable cover of all representable markers."""

    blocks = tuple(document.blocks)
    by_id = {block.id: block for block in blocks}
    selected: list[SelectedUnit] = []
    missing_source_html_blocks = 0
    unparseable_source_html_blocks = 0
    for block in blocks:
        if not block.selectable:
            continue
        if not block.outer_html:
            missing_source_html_blocks += 1
            continue
        try:
            local = build_label_profile(
                block.outer_html,
                context=f"IR block {block.id}",
                require_ground_truth_marker=False,
            )
        except LabelOracleError:
            if not block.html_truncated and not document.truncated:
                raise
            unparseable_source_html_blocks += 1
            continue
        root_uid = local.root_uid
        marker_ids = (
            source_profile.uid_to_marker_ids.get(root_uid, frozenset())
            if root_uid is not None
            else frozenset()
        )
        if not marker_ids and local.marker_count:
            marker_ids = frozenset(
                source_profile.marker_uid_to_id[uid]
                for uid in local.marker_uid_to_id
                if uid in source_profile.marker_uid_to_id
            )
        if not marker_ids and local.marker_count == 0:
            continue

        if root_uid is not None and root_uid in source_profile.fully_selected_uids:
            labelled_chars = local.visible_non_whitespace_chars
        else:
            labelled_chars = local.selected_non_whitespace_chars
        noise_chars = max(0, local.visible_non_whitespace_chars - labelled_chars)
        context_tags = _block_context_tags(block, by_id)
        selected.append(
            SelectedUnit(
                block_id=block.id,
                tag=block.tag,
                context_tags=context_tags,
                marker_ids=marker_ids,
                visible_non_whitespace_chars=local.visible_non_whitespace_chars,
                selected_non_whitespace_chars=labelled_chars,
                noise_non_whitespace_chars=noise_chars,
                mixed=labelled_chars > 0 and noise_chars > 0,
                coarse=not block.atomic,
                html_truncated=block.html_truncated,
                root_uid=root_uid,
                marker_alignment_complete=bool(marker_ids),
            )
        )
    candidates = tuple(selected)
    candidate_by_id = {unit.block_id: unit for unit in candidates}
    candidate_children: dict[str | None, list[str]] = defaultdict(list)
    overlap_pairs = 0
    for unit in candidates:
        parent_id = by_id[unit.block_id].parent_id
        candidate_parent_id: str | None = None
        while parent_id is not None:
            if parent_id in candidate_by_id:
                candidate_parent_id = parent_id
                overlap_pairs += 1
                break
            parent_id = by_id[parent_id].parent_id
        candidate_children[candidate_parent_id].append(unit.block_id)

    def resolve(block_id: str) -> tuple[SelectedUnit, ...]:
        unit = candidate_by_id[block_id]
        children = tuple(
            selected_unit
            for child_id in candidate_children.get(block_id, ())
            for selected_unit in resolve(child_id)
        )
        if not children:
            return (unit,)
        child_markers = _unit_marker_ids(children)
        # Prefer the smallest descendant cover only when it preserves every
        # marker and every labelled character attributed to the ancestor.
        # Otherwise the ancestor is the sole contract-valid unit that can
        # cover its residual labelled text.
        child_label_chars = sum(
            child.selected_non_whitespace_chars for child in children
        )
        if (
            unit.marker_ids
            and unit.marker_ids <= child_markers
            and unit.selected_non_whitespace_chars <= child_label_chars
        ):
            return children
        return (unit,)

    resolved = tuple(
        selected_unit
        for root_id in candidate_children.get(None, ())
        for selected_unit in resolve(root_id)
    )
    resolved = tuple(
        sorted(resolved, key=lambda unit: by_id[unit.block_id].order)
    )
    return OracleUnitSelection(
        candidates=candidates,
        selected=resolved,
        overlap_pairs=overlap_pairs,
        missing_source_html_blocks=missing_source_html_blocks,
        unparseable_source_html_blocks=unparseable_source_html_blocks,
    )


def analyze_record(
    record: OracleRecord,
    *,
    canonicalize: Callable[[str, str], str],
    score: Callable[[str, str], Mapping[str, Any]],
) -> dict[str, Any]:
    """Run one explicitly label-using IR ceiling observation."""

    source_profile = build_label_profile(
        record.html,
        context=f"dataset row {record.dataset_index} raw HTML",
        require_ground_truth_marker=True,
    )
    main_profile = build_label_profile(
        record.main_html,
        context=f"dataset row {record.dataset_index} main_html",
        require_ground_truth_marker=True,
    )

    document = extract_document_ir(
        record.html,
        limits=DEFAULT_DOCUMENT_IR_LIMITS,
    )
    prepared = prepare_document_ir(document)
    unit_selection = oracle_selected_units(document, source_profile)
    units = unit_selection.selected
    selected_ids = tuple(unit.block_id for unit in units)
    selection_payload = json.dumps(
        {
            "diagnostic": LABEL_ORACLE,
            "policy": SELECTION_POLICY,
            "selected_ids": selected_ids,
            "source_digest": prepared.source_digest,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    selection = BlockSelection(
        schema_version="ordered-dom-ir.selection.v1",
        source_digest=prepared.source_digest,
        classifier_payload_digest=hashlib.sha256(
            SELECTION_POLICY.encode("utf-8")
        ).hexdigest(),
        response_digest=hashlib.sha256(selection_payload.encode("utf-8")).hexdigest(),
        selected_ids=selected_ids,
        raw_item_count=len(selected_ids),
        range_count=0,
        response_chars=len(selection_payload),
    )
    reconstruction = reconstruct_block_selection(
        prepared,
        selection,
        limits=ReconstructionLimits(
            max_chars=32 * 1024 * 1024,
            max_blocks=4_096,
        ),
    )

    by_unit_id = {unit.block_id: unit for unit in units}
    emitted_units = tuple(by_unit_id[block_id] for block_id in reconstruction.emitted_ids)
    selected_markers = _unit_marker_ids(units)
    emitted_markers = _unit_marker_ids(emitted_units)
    selected_label_chars = sum(unit.selected_non_whitespace_chars for unit in units)
    emitted_label_chars = sum(
        unit.selected_non_whitespace_chars for unit in emitted_units
    )
    selected_noise_chars = sum(unit.noise_non_whitespace_chars for unit in units)
    emitted_noise_chars = sum(unit.noise_non_whitespace_chars for unit in emitted_units)
    source_table_markers = _markers_in_context(
        source_profile,
        TABLE_TAGS,
    )
    source_list_markers = _markers_in_context(
        source_profile,
        LIST_TAGS,
    )
    selected_table_markers = selected_markers & source_table_markers
    selected_list_markers = selected_markers & source_list_markers
    emitted_table_markers = emitted_markers & source_table_markers
    emitted_list_markers = emitted_markers & source_list_markers
    unrepresented_markers = (
        frozenset(range(source_profile.marker_count)) - emitted_markers
    )
    truncation_reasons = set(document.truncation_reasons)

    try:
        prediction = canonicalize(reconstruction.html, record.url)
        recanonicalized_ground_truth = canonicalize(record.main_html, record.url)
    except Exception as error:
        raise LabelOracleError(
            f"official html2text failed at row {record.dataset_index}: "
            f"{type(error).__name__}: {error}"
        ) from error
    if not isinstance(prediction, str) or not isinstance(
        recanonicalized_ground_truth,
        str,
    ):
        raise LabelOracleError("official html2text canonicalizer returned a non-string")

    page_score = _normalized_score(score(record.reference, prediction))
    recanonicalization_score = _normalized_score(
        score(record.reference, recanonicalized_ground_truth)
    )
    selected_contexts = [unit.context_tags for unit in units]
    mixed_contexts = [unit.context_tags for unit in units if unit.mixed]
    features = {
        "ground_truth_table_markup": bool(_TABLE_MARKUP_RE.search(record.main_html)),
        "ground_truth_list_markup": bool(_LIST_MARKUP_RE.search(record.main_html)),
        "selected_table_unit": any(tags & TABLE_TAGS for tags in selected_contexts),
        "selected_list_unit": any(tags & LIST_TAGS for tags in selected_contexts),
        "mixed_selected_block": any(unit.mixed for unit in units),
        "mixed_table_or_list_block": any(
            tags & (TABLE_TAGS | LIST_TAGS) for tags in mixed_contexts
        ),
        "coarse_selected_block": any(unit.coarse for unit in units),
        "overlapping_selectable_units": unit_selection.overlap_pairs > 0,
        "label_alignment_ambiguous": bool(
            source_profile.duplicate_uids
            or source_profile.marker_without_uid_count
        ),
        "selected_marker_alignment_incomplete": any(
            not unit.marker_alignment_complete for unit in units
        ),
        "unrepresented_label_marker": len(emitted_markers)
        < source_profile.marker_count,
        "unselectable_label_marker": len(selected_markers)
        < source_profile.marker_count,
        "unrepresented_table_or_list_marker": bool(
            unrepresented_markers & (source_table_markers | source_list_markers)
        ),
        "reconstruction_dropped_label_marker": len(emitted_markers)
        < len(selected_markers),
        "incomplete_label_char_coverage": emitted_label_chars
        < source_profile.selected_non_whitespace_chars,
        "incomplete_selectable_label_char_coverage": selected_label_chars
        < source_profile.selected_non_whitespace_chars,
        "reconstruction_dropped_label_chars": emitted_label_chars
        < selected_label_chars,
        "ir_truncated": bool(document.truncated),
        "ir_input_truncated": bool(document.input_truncated),
        "ir_nodes_truncated": bool(document.nodes_truncated),
        "ir_depth_truncated": bool(document.depth_truncated),
        "ir_blocks_truncated": bool(document.blocks_truncated),
        "ir_block_text_truncated": "block_text" in truncation_reasons,
        "ir_block_html_truncated": "block_html" in truncation_reasons,
        "selectable_source_html_missing": (
            unit_selection.missing_source_html_blocks > 0
        ),
        "selectable_source_html_unparseable": (
            unit_selection.unparseable_source_html_blocks > 0
        ),
        "reconstruction_incomplete": not reconstruction.complete,
        "selected_source_html_truncated": any(unit.html_truncated for unit in units),
        "zero_selected_blocks": not units,
    }
    selected_tag_counts = Counter(unit.tag for unit in units)
    return {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "diagnostic": LABEL_ORACLE,
        "label_oracle": True,
        "claimable": False,
        "dataset_index": record.dataset_index,
        "track_id": record.track_id,
        "url_sha256": hashlib.sha256(record.url.encode("utf-8")).hexdigest(),
        "metadata": record.metadata,
        "score": page_score,
        "ground_truth_recanonicalization_score": recanonicalization_score,
        "ground_truth_recanonicalization_exact": (
            recanonicalized_ground_truth == record.reference
        ),
        "features": features,
        "diagnostics": {
            "source_label_markers": source_profile.marker_count,
            "source_duplicate_annotation_uids": len(
                source_profile.duplicate_uids
            ),
            "source_table_label_markers": len(source_table_markers),
            "source_list_label_markers": len(source_list_markers),
            "main_html_label_markers": main_profile.marker_count,
            "source_markers_without_uid": source_profile.marker_without_uid_count,
            "duplicate_annotation_uid_sample": list(
                source_profile.duplicate_uids[:20]
            ),
            "selected_label_markers": len(selected_markers),
            "selected_table_label_markers": len(selected_table_markers),
            "selected_list_label_markers": len(selected_list_markers),
            "emitted_label_markers": len(emitted_markers),
            "emitted_table_label_markers": len(emitted_table_markers),
            "emitted_list_label_markers": len(emitted_list_markers),
            "source_selected_non_whitespace_chars": (
                source_profile.selected_non_whitespace_chars
            ),
            "selected_label_non_whitespace_chars": selected_label_chars,
            "emitted_label_non_whitespace_chars": emitted_label_chars,
            "selected_noise_non_whitespace_chars": selected_noise_chars,
            "emitted_noise_non_whitespace_chars": emitted_noise_chars,
            "ir_blocks": document.block_count,
            "ir_selectable_blocks": sum(block.selectable for block in document.blocks),
            "raw_intersecting_selectable_blocks": len(
                unit_selection.candidates
            ),
            "overlap_pairs": unit_selection.overlap_pairs,
            "dropped_overlap_candidates": (
                unit_selection.dropped_overlap_candidates
            ),
            "missing_selectable_source_html_blocks": (
                unit_selection.missing_source_html_blocks
            ),
            "unparseable_selectable_source_html_blocks": (
                unit_selection.unparseable_source_html_blocks
            ),
            "selected_blocks": len(units),
            "emitted_blocks": len(emitted_units),
            "mixed_selected_blocks": sum(unit.mixed for unit in units),
            "mixed_table_or_list_blocks": sum(
                unit.mixed and bool(unit.context_tags & (TABLE_TAGS | LIST_TAGS))
                for unit in units
            ),
            "coarse_selected_blocks": sum(unit.coarse for unit in units),
            "selected_tag_counts": dict(sorted(selected_tag_counts.items())),
            "ir_truncated": document.truncated,
            "ir_truncation_reasons": list(document.truncation_reasons),
            "reconstruction_complete": reconstruction.complete,
            "reconstruction_truncation_reasons": list(
                reconstruction.truncation_reasons
            ),
            "reconstruction_wrapper_count": len(reconstruction.wrapper_ids),
            "selection_policy": SELECTION_POLICY,
            "classifier_invoked": False,
        },
        "artifacts": {
            "source_html_sha256": hashlib.sha256(
                record.html.encode("utf-8")
            ).hexdigest(),
            "main_html_sha256": hashlib.sha256(
                record.main_html.encode("utf-8")
            ).hexdigest(),
            "reference_sha256": hashlib.sha256(
                record.reference.encode("utf-8")
            ).hexdigest(),
            "selected_ids_sha256": _hash_json(selected_ids),
            "reconstructed_html_sha256": reconstruction.output_digest,
            "canonical_prediction_sha256": hashlib.sha256(
                prediction.encode("utf-8")
            ).hexdigest(),
            "canonical_prediction_characters": len(prediction),
            "reconstructed_html_characters": reconstruction.chars,
        },
    }


def verify_and_load_canonicalizer(
    evaluator_root: Path,
) -> tuple[Callable[[str, str], str], dict[str, Any]]:
    path = evaluator_root / CANONICALIZER_RELATIVE_PATH
    if not path.is_file():
        raise LabelOracleError(f"official canonicalizer source is missing: {path}")
    digest = _sha256(path)
    if digest != CANONICALIZER_SHA256:
        raise LabelOracleError(
            "official canonicalizer source hash mismatch: "
            f"expected {CANONICALIZER_SHA256}, found {digest}"
        )
    versions: dict[str, str] = {}
    for distribution, expected in CANONICALIZER_DEPENDENCIES.items():
        try:
            actual = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError as error:
            raise LabelOracleError(
                f"benchmark-only canonicalizer dependency missing: "
                f"{distribution}=={expected}"
            ) from error
        if actual != expected:
            raise LabelOracleError(
                f"benchmark-only canonicalizer dependency drift for {distribution}: "
                f"expected {expected}, found {actual}"
            )
        versions[distribution] = actual

    module_name = f"clusy_wmb_html2text_{EVALUATOR_COMMIT[:12]}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise LabelOracleError(f"could not load official canonicalizer: {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as error:
        raise LabelOracleError(
            f"official canonicalizer import failed: {type(error).__name__}: {error}"
        ) from error
    wrapper_type = getattr(module, "HTML2TextWrapper", None)
    if not isinstance(wrapper_type, type):
        raise LabelOracleError("official canonicalizer lacks HTML2TextWrapper")
    canonicalize = _fresh_wrapper_canonicalizer(wrapper_type)

    return canonicalize, {
        "relative_path": CANONICALIZER_RELATIVE_PATH.as_posix(),
        "sha256": digest,
        "class": "HTML2TextWrapper",
        "configuration": {
            "bodywidth": 0,
            "ignore_links": True,
            "ignore_images": True,
        },
        "instance_lifetime": (
            "fresh HTML2TextWrapper per conversion, matching the official CPU "
            "runner's batch_size=1 extractor lifetime and preventing parser "
            "state from leaking across pages"
        ),
        "dependencies": versions,
        "verified": True,
    }


def _fresh_wrapper_canonicalizer(
    wrapper_type: type[Any],
) -> Callable[[str, str], str]:
    def canonicalize(html: str, url: str) -> str:
        result = wrapper_type()(html, url)
        if not isinstance(result, str):
            raise LabelOracleError("official HTML2TextWrapper returned a non-string")
        return result

    return canonicalize


def run_diagnostic(args: argparse.Namespace) -> int:
    validate_args(args)
    print(NON_CLAIMABLE_WARNING, flush=True)
    dataset_path = args.dataset.resolve()
    evaluator_root = args.evaluator_root.resolve()
    output = args.output_dir.resolve()
    if output.exists():
        raise LabelOracleError(f"output directory already exists: {output}")

    isolation = assert_benchmark_only_isolation()
    dataset = verify_dataset(dataset_path)
    evaluator = verify_evaluator(evaluator_root)
    canonicalize, canonicalizer = verify_and_load_canonicalizer(evaluator_root)
    try:
        official_scorer, scorer_dependencies = load_official_scorer(evaluator_root)
    except BenchmarkError as error:
        raise LabelOracleError(str(error)) from error

    def score(reference: str, prediction: str) -> Mapping[str, Any]:
        try:
            return _official_score(official_scorer, reference, prediction)
        except BenchmarkError as error:
            raise LabelOracleError(str(error)) from error

    indices = deterministic_indices(args.sample_size, args.sample_seed)
    output.mkdir(parents=True)
    partial_pages = output / "pages.jsonl.partial"
    final_pages = output / "pages.jsonl"
    aggregate = OracleAggregate()
    with partial_pages.open("x", encoding="utf-8") as stream:
        for completed, record in enumerate(
            selected_records(dataset_path, indices),
            start=1,
        ):
            page = analyze_record(
                record,
                canonicalize=canonicalize,
                score=score,
            )
            aggregate.add(page)
            stream.write(_json_dumps(page) + "\n")
            if completed % args.progress_every == 0 or completed == len(indices):
                stream.flush()
                os.fsync(stream.fileno())
                current = aggregate.score.export()
                print(
                    f"label_oracle progress {completed}/{len(indices)} "
                    f"F1={float(current['f1']):.6f}",
                    flush=True,
                )
    partial_pages.replace(final_pages)

    aggregate_export = aggregate.export()
    summary = {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "diagnostic": LABEL_ORACLE,
        "label_oracle": True,
        "claimable": False,
        "warning": NON_CLAIMABLE_WARNING,
        "scope": (
            "ground-truth-marker-selected ordered-dom-ir.v1 units, minimal "
            "ancestor reconstruction, official HTML2TextWrapper canonicalization, "
            "and official per-page ROUGE-5 arithmetic-mean scoring"
        ),
        "selection_policy": {
            "name": SELECTION_POLICY,
            "uses_ground_truth": True,
            "classifier_invoked": False,
            "rule": (
                "select every selectable IR unit whose source UID subtree or "
                "ancestor relation intersects cc-select=true; reconstruct only "
                "those complete stored units plus the minimum ancestor skeleton"
            ),
        },
        "claimability": {
            "claimable": False,
            "label_oracle": True,
            "reasons": [
                "public ground-truth marker attributes directly determine selected blocks",
                "no learned classifier or production routing decision is evaluated",
                "the result is an architecture representation/reconstruction ceiling only",
                "WebMainBench does not evaluate live crawling or platform reliability",
            ],
            "leaderboard_comparable_system_score": False,
            "sota_claimable": False,
        },
        "selection": {
            "kind": "full" if args.sample_size is None else "deterministic_sample",
            "requested_sample_size": args.sample_size,
            "selected_pages": len(indices),
            "sample_seed": args.sample_seed,
            "algorithm": (
                "smallest SHA-256(seed UTF-8 || NUL || decimal corpus index), "
                "then restored to corpus order"
            ),
            "indices_sha256": _hash_json(indices),
            "indices": list(indices),
        },
        "dataset": dataset,
        "evaluator": evaluator,
        "official_scorer": {
            "function": "eval_baselines/utils.py::calc_rouge_n_score",
            "n": 5,
            "dependencies": scorer_dependencies,
        },
        "official_html2text": canonicalizer,
        "ordered_ir": {
            "schema_version": "ordered-dom-ir.v1",
            "limits": {
                name: getattr(DEFAULT_DOCUMENT_IR_LIMITS, name)
                for name in DEFAULT_DOCUMENT_IR_LIMITS.__dataclass_fields__
            },
            "reconstruction_strategy": "stored-outer-html-dom-order-v1",
        },
        "aggregate": aggregate_export,
        "isolation": isolation,
        "source": {
            relative: _sha256(ROOT / relative)
            for relative in (
                "bench/webmainbench_ir_label_oracle.py",
                "app/services/document_ir_selection.py",
                "native/src/document_ir.rs",
            )
        },
        "determinism": {
            "corpus_order": True,
            "hash_ranked_sampling": True,
            "ground_truth_mapping": "DOM ancestry and stable data-anno-uid",
            "page_artifact_contains_timing": False,
            "prediction_text_persisted": False,
        },
    }
    _write_json(output / "summary.json", summary)
    notice = (
        NON_CLAIMABLE_WARNING
        + "\n\n"
        + "This directory intentionally cannot support a production or SOTA claim.\n"
        + "It contains a diagnostic in which public labels directly selected IR units.\n"
    )
    (output / "NOT_CLAIMABLE_LABEL_ORACLE.txt").write_text(
        notice,
        encoding="utf-8",
    )
    manifest = {
        path.name: {
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in sorted(output.iterdir())
        if path.is_file() and path.name != "manifest.json"
    }
    _write_json(
        output / "manifest.json",
        {
            "schema_version": 1,
            "label_oracle": True,
            "claimable": False,
            "files": manifest,
        },
    )

    ceiling = cast("Mapping[str, Any]", aggregate_export["score_ceiling"])
    print(
        f"LABEL_ORACLE ceiling: pages={ceiling['pages']} "
        f"P={float(ceiling['precision']):.6f} "
        f"R={float(ceiling['recall']):.6f} "
        f"F1={float(ceiling['f1']):.6f}",
        flush=True,
    )
    print(NON_CLAIMABLE_WARNING, flush=True)
    print(f"artifacts: {output}", flush=True)
    return 0


def _is_ground_truth_marker(element: Any) -> bool:
    value = element.attrib.get(GROUND_TRUTH_ATTRIBUTE)
    return isinstance(value, str) and value.strip().lower() == GROUND_TRUTH_VALUE


def _uid(element: Any) -> str | None:
    if element is None or not hasattr(element, "attrib"):
        return None
    value = element.attrib.get(GROUND_TRUTH_UID_ATTRIBUTE)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _tag(element: Any) -> str:
    value = getattr(element, "tag", "")
    if not isinstance(value, str):
        return ""
    return value.rsplit("}", 1)[-1].lower()


def _block_context_tags(block: Any, by_id: Mapping[str, Any]) -> frozenset[str]:
    tags = {_tag_name(block.tag)}
    parent_id = block.parent_id
    while parent_id is not None:
        parent = by_id.get(parent_id)
        if parent is None:
            raise LabelOracleError(
                f"IR block {block.id} has missing semantic parent {parent_id}"
            )
        tags.add(_tag_name(parent.tag))
        parent_id = parent.parent_id
    return frozenset(tags)


def _tag_name(value: str) -> str:
    return str(value).rsplit("}", 1)[-1].lower()


def _unit_marker_ids(units: Sequence[SelectedUnit]) -> frozenset[int]:
    result: set[int] = set()
    for unit in units:
        result.update(unit.marker_ids)
    return frozenset(result)


def _markers_in_context(
    profile: LabelProfile,
    tags: frozenset[str],
) -> frozenset[int]:
    return frozenset(
        marker_id
        for marker_id, context_tags in profile.marker_context_tags.items()
        if context_tags & tags
    )


def _non_whitespace_chars(value: str) -> int:
    return sum(not character.isspace() for character in value)


def _finite_score(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LabelOracleError(f"official score {name} is not numeric")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise LabelOracleError(f"official score {name} is outside [0, 1]: {result}")
    return result


def _normalized_score(value: Mapping[str, Any]) -> dict[str, float]:
    return {
        name: _finite_score(value.get(name), name)
        for name in ("precision", "recall", "f1")
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _category(value: Any) -> str:
    if value is None:
        return "<missing>"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (str, int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _required_mapping(value: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    result = value.get(name)
    if not isinstance(result, Mapping):
        raise LabelOracleError(f"{name} must be an object")
    return result


def _required_string(value: Mapping[str, Any], name: str) -> str:
    result = value.get(name)
    if not isinstance(result, str) or not result:
        raise LabelOracleError(f"{name} must be a non-empty string")
    return result


def _nonnegative_int(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise LabelOracleError(f"{name} must be a nonnegative integer")
    return value


def _required_nonnegative_int(value: Mapping[str, Any], name: str) -> int:
    return _nonnegative_int(value.get(name), name)


def _hash_json(value: Any) -> str:
    return hashlib.sha256(_json_dumps(value).encode("utf-8")).hexdigest()


def _json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(
            value,
            stream,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run_diagnostic(parse_args(argv))
    except (LabelOracleError, BenchmarkError) as error:
        print(f"LABEL_ORACLE INVALID: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

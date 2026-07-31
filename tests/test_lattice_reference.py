from __future__ import annotations

import math
import random
import tracemalloc
from dataclasses import FrozenInstanceError, fields, replace
from fractions import Fraction
from itertools import product
from typing import TYPE_CHECKING, cast

import pytest

import bench.lattice_reference.decoder as lattice_decoder
from bench.lattice_reference import (
    DecoderWeights,
    TypedSpanCandidate,
    brute_force_decode,
    decode,
    greedy_decode,
    marginalize_candidates,
    score_first_greedy_decode,
    source_order_greedy_decode,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


def candidate(
    identity: str,
    start: int,
    end: int,
    score: float,
    *,
    type_name: str = "body",
    block_id: str | None = None,
    ancestors: tuple[str, ...] = (),
    granularity: str = "leaf",
) -> TypedSpanCandidate:
    return TypedSpanCandidate(
        candidate_id=f"{identity}-{type_name}",
        source_identity=identity,
        source_start=start,
        source_end=end,
        block_id=block_id or identity,
        type_name=type_name,
        granularity=granularity,
        base_score=score,
        ancestor_block_ids=ancestors,
    )


def test_latent_types_use_stable_logsumexp_and_probabilities() -> None:
    spans = marginalize_candidates(
        [
            candidate("source", 0, 10, 1000.0, type_name="heading"),
            candidate("source", 0, 10, 999.0, type_name="body"),
        ]
    )

    assert len(spans) == 1
    assert spans[0].marginal_score == pytest.approx(1000.0 + math.log1p(math.exp(-1.0)))
    probability_sum = math.fsum(probability for _, probability in spans[0].type_probabilities)
    assert probability_sum == pytest.approx(1.0)
    assert spans[0].best_type == "heading"


def test_exact_decoder_beats_score_first_greedy_on_overlapping_granularities() -> None:
    candidates = [
        candidate("coarse", 0, 10, 8.0, granularity="container"),
        candidate("left", 0, 5, 5.0),
        candidate("right", 5, 10, 5.0),
    ]

    exact = decode(candidates)
    greedy = score_first_greedy_decode(candidates)

    assert exact.source_identities == ("left", "right")
    assert exact.score == pytest.approx(10.0)
    assert greedy.source_identities == ("coarse",)
    assert greedy.score == pytest.approx(8.0)


def test_exact_decoder_dominates_the_named_source_order_greedy_fixture() -> None:
    candidates = [
        candidate("early-low", 0, 10, 1.0),
        candidate("later-high", 5, 15, 5.0),
    ]

    exact = decode(candidates)
    source_order = source_order_greedy_decode(candidates)
    score_first = score_first_greedy_decode(candidates)

    assert exact.source_identities == ("later-high",)
    assert exact.score == 5.0
    assert source_order.source_identities == ("early-low",)
    assert source_order.score == 1.0
    assert score_first == exact


def test_greedy_baselines_start_empty_and_never_force_a_negative_straw_path() -> None:
    candidates = [
        candidate("negative-left", 0, 2, -1.0),
        candidate("negative-right", 2, 4, -2.0),
    ]

    for baseline in (
        score_first_greedy_decode,
        source_order_greedy_decode,
        greedy_decode,
    ):
        result = baseline(candidates)
        assert result.source_identities == ()
        assert result.score == 0.0
        assert result.covered_characters == 0
        assert result.fragments == 0


def test_greedy_additions_must_improve_the_exact_current_path_contract() -> None:
    candidates = [
        candidate("first", 0, 2, 2.0),
        candidate("distant", 102, 104, 1.0),
    ]
    weights = DecoderWeights(gap_penalty_per_char=0.1)

    for baseline in (score_first_greedy_decode, source_order_greedy_decode):
        result = baseline(candidates, weights=weights)
        assert result.source_identities == ("first",)
        assert result.score == 2.0

    zero_tie = [
        candidate("zero-left", 0, 2, 0.0),
        candidate("zero-right", 2, 4, 0.0),
    ]
    for baseline in (score_first_greedy_decode, source_order_greedy_decode):
        result = baseline(zero_tie)
        assert result.source_identities == ("zero-left", "zero-right")
        assert result.score == 0.0
        assert result.covered_characters == 4


def test_heading_body_continuity_uses_type_posteriors() -> None:
    candidates = [
        candidate("heading", 0, 2, 1.0, type_name="heading"),
        candidate("body", 2, 10, 1.0, type_name="body"),
        candidate("coarse", 0, 10, 2.5, type_name="other"),
    ]

    without_bonus = decode(candidates)
    with_bonus = decode(candidates, weights=DecoderWeights(heading_to_body_bonus=1.0))

    assert without_bonus.source_identities == ("coarse",)
    assert with_bonus.source_identities == ("heading", "body")
    assert with_bonus.score == pytest.approx(3.0)


def test_gap_and_fragmentation_penalties_can_reject_a_distant_span() -> None:
    candidates = [
        candidate("first", 0, 10, 2.0),
        candidate("distant", 100, 110, 1.0),
    ]
    weights = DecoderWeights(
        gap_penalty_per_char=0.01,
        fragmentation_penalty=1.0,
        contiguous_gap_chars=4,
    )

    result = decode(candidates, weights=weights)

    assert result.source_identities == ("first",)
    assert result.fragments == 1


def test_nested_table_list_and_code_children_can_beat_their_parent() -> None:
    candidates = [
        candidate("parent", 0, 30, 5.0, block_id="parent", granularity="container"),
        candidate("table", 0, 10, 2.0, block_id="table", ancestors=("parent",)),
        candidate("list", 10, 20, 2.0, block_id="list", ancestors=("parent",)),
        candidate("code", 20, 30, 2.0, block_id="code", ancestors=("parent",)),
    ]

    result = decode(candidates)

    assert result.source_identities == ("table", "list", "code")
    assert result.score == pytest.approx(6.0)


def test_distinct_repeated_cards_and_multiple_roots_remain_selectable() -> None:
    candidates = [
        candidate("root-a-card-1", 0, 5, 1.0),
        candidate("root-a-card-2", 5, 10, 1.0),
        candidate("root-b-card-1", 20, 25, 1.0),
        candidate("root-b-card-2", 25, 30, 1.0),
    ]

    result = decode(candidates)

    assert result.source_identities == (
        "root-a-card-1",
        "root-a-card-2",
        "root-b-card-1",
        "root-b-card-2",
    )


def test_inconsistent_source_identity_is_rejected_instead_of_becoming_coloured_interval() -> None:
    with pytest.raises(ValueError, match="inconsistent structural metadata"):
        marginalize_candidates(
            [
                candidate("same", 0, 5, 1.0, type_name="body"),
                candidate("same", 10, 15, 1.0, type_name="heading"),
            ]
        )


def test_inconsistent_block_span_and_ancestor_catalog_are_rejected() -> None:
    with pytest.raises(ValueError, match="maps to multiple source spans"):
        marginalize_candidates(
            [
                candidate("one", 0, 5, 1.0, block_id="shared"),
                candidate("two", 5, 10, 1.0, block_id="shared"),
            ]
        )

    with pytest.raises(ValueError, match="does not contain"):
        marginalize_candidates(
            [
                candidate("parent", 0, 5, 1.0, block_id="parent"),
                candidate("child", 10, 15, 1.0, block_id="child", ancestors=("parent",)),
            ]
        )


def test_invalid_candidates_and_budgets_fail_closed() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        marginalize_candidates([candidate("empty", 3, 3, 1.0)])
    with pytest.raises(ValueError, match="finite"):
        marginalize_candidates([candidate("nan", 0, 1, math.nan)])
    with pytest.raises(ValueError, match="candidate budget exceeded"):
        marginalize_candidates(
            [candidate("one", 0, 1, 1.0), candidate("two", 1, 2, 1.0)],
            max_candidates=1,
        )
    with pytest.raises(ValueError, match="document budget"):
        marginalize_candidates([candidate("large", 0, 11, 1.0)], max_document_chars=10)


def test_candidate_budget_bounds_iterable_consumption() -> None:
    consumed = 0

    def unbounded_candidates() -> Iterator[TypedSpanCandidate]:
        nonlocal consumed
        index = 0
        while True:
            consumed += 1
            yield candidate(f"source-{index}", index, index + 1, 1.0)
            index += 1

    with pytest.raises(ValueError, match="candidate budget exceeded"):
        marginalize_candidates(unbounded_candidates(), max_candidates=3)

    assert consumed == 4


def test_compiled_candidate_and_ancestor_limits_cannot_be_bypassed() -> None:
    consumed = 0

    def candidates() -> Iterator[TypedSpanCandidate]:
        nonlocal consumed
        consumed += 1
        yield candidate("one", 0, 1, 1.0)

    with pytest.raises(ValueError, match="candidate budget exceeds the hard limit of 4096"):
        marginalize_candidates(candidates(), max_candidates=4097)
    assert consumed == 0

    with pytest.raises(
        ValueError,
        match="ancestor reference budget exceeds the hard limit of 65536",
    ):
        marginalize_candidates(candidates(), max_ancestor_references=65_537)
    assert consumed == 0


def test_hostile_primitive_and_tuple_subclasses_are_snapshotted_exactly() -> None:
    override_calls = 0

    class HostileInt(int):
        def __int__(self) -> int:
            nonlocal override_calls
            override_calls += 1
            return 999

        def __sub__(self, _other: object) -> int:
            raise AssertionError("caller-owned integer reached arithmetic")

    class HostileFloat(float):
        def __float__(self) -> float:
            nonlocal override_calls
            override_calls += 1
            return -999.0

    class HostileStr(str):
        def __str__(self) -> str:
            nonlocal override_calls
            override_calls += 1
            return "corrupted"

        def __hash__(self) -> int:
            raise AssertionError("caller-owned string reached hashing")

        def __lt__(self, _other: object) -> bool:
            raise AssertionError("caller-owned string reached sorting")

        def __len__(self) -> int:
            raise AssertionError("caller-owned string reached length accounting")

    class HostileTuple(tuple[object, ...]):
        def __iter__(self) -> Iterator[object]:
            raise AssertionError("caller-owned tuple iterator was invoked")

        def __len__(self) -> int:
            raise AssertionError("caller-owned tuple length override was invoked")

    raw = TypedSpanCandidate(
        candidate_id=cast("str", HostileStr("candidate")),
        source_identity=cast("str", HostileStr("source")),
        source_start=cast("int", HostileInt(0)),
        source_end=cast("int", HostileInt(2)),
        block_id=cast("str", HostileStr("block")),
        type_name=cast("str", HostileStr("body")),
        granularity=cast("str", HostileStr("leaf")),
        base_score=cast("float", HostileFloat(1.0)),
        type_logit=cast("float", HostileFloat(0.0)),
        ancestor_block_ids=cast("tuple[str, ...]", HostileTuple(())),
    )

    result = decode(
        [raw],
        max_candidates=cast("int", HostileInt(1)),
        max_document_chars=cast("int", HostileInt(10)),
    )
    span = result.spans[0]
    standalone_span = marginalize_candidates([raw])[0]

    assert result.source_identities == ("source",)
    assert result.score == 1.0
    assert type(span.source_identity) is str
    assert type(span.source_start) is int
    assert type(span.marginal_score) is float
    assert type(span.ancestor_block_ids) is tuple
    assert standalone_span == span
    assert override_calls == 0


def test_oversized_string_subclass_is_rejected_before_base_string_copy() -> None:
    class OversizedStr(str):
        pass

    oversized = cast("str", OversizedStr("x" * 8_000_000))
    raw = replace(candidate("source", 0, 1, 0.0), candidate_id=oversized)

    tracemalloc.start()
    try:
        with pytest.raises(ValueError, match="string codepoint budget exceeded"):
            marginalize_candidates([raw], max_string_codepoints=1)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # A premature ``str.__str__`` copy peaks above eight megabytes here.
    assert peak_bytes < 512_000


def test_oversized_int_subclasses_are_rejected_before_base_int_copy() -> None:
    class OversizedInt(int):
        pass

    oversized = OversizedInt(1 << 8_000_000)
    oversized_coordinate = replace(
        candidate("coordinate", 0, 1, 0.0),
        source_end=cast("int", oversized),
    )
    oversized_score = replace(
        candidate("score", 0, 1, 0.0),
        base_score=cast("float", oversized),
    )

    for action, message in (
        (
            lambda: marginalize_candidates([oversized_coordinate]),
            "compiled bit-length limit",
        ),
        (
            lambda: marginalize_candidates([oversized_score]),
            "must be a finite number",
        ),
        (
            lambda: decode([], max_candidates=oversized),
            "compiled bit-length limit",
        ),
    ):
        tracemalloc.start()
        try:
            with pytest.raises(ValueError, match=message):
                action()
            _, peak_bytes = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        # A premature ``int.__int__`` copy is about one megabyte for this value.
        assert peak_bytes < 512_000


def test_integer_binary64_preflight_accepts_only_the_finite_convertible_bit_range() -> None:
    accepted = replace(
        candidate("finite-int", 0, 1, 0.0),
        base_score=cast("float", 1 << 1023),
    )
    rejected = replace(
        candidate("overflowing-int", 0, 1, 0.0),
        base_score=cast("float", 1 << 1024),
    )

    assert math.isfinite(decode([accepted]).score)
    with pytest.raises(ValueError, match="must be a finite number"):
        marginalize_candidates([rejected])


def test_candidate_subclass_fields_are_read_once_before_snapshot() -> None:
    tracked_fields = {
        "candidate_id",
        "source_identity",
        "source_start",
        "source_end",
        "block_id",
        "type_name",
        "granularity",
        "base_score",
        "type_logit",
        "ancestor_block_ids",
    }
    reads = {field_name: 0 for field_name in tracked_fields}

    class CountingCandidate(TypedSpanCandidate):
        def __getattribute__(self, name: str) -> object:
            if name in tracked_fields:
                reads[name] += 1
            return super().__getattribute__(name)

    raw = CountingCandidate(
        candidate_id="candidate",
        source_identity="source",
        source_start=0,
        source_end=1,
        block_id="block",
        type_name="body",
        granularity="leaf",
        base_score=1.0,
    )

    result = decode([raw])

    assert result.source_identities == ("source",)
    assert reads == {field_name: 1 for field_name in tracked_fields}


def test_weights_are_snapshotted_without_subclass_dispatch() -> None:
    override_calls = 0
    tracked_fields = {
        "coverage_reward_per_char",
        "gap_penalty_per_char",
        "fragmentation_penalty",
        "contiguous_gap_chars",
        "heading_to_body_bonus",
        "type_compatibility",
        "heading_types",
        "body_types",
    }
    reads = {field_name: 0 for field_name in tracked_fields}

    class HostileFloat(float):
        def __float__(self) -> float:
            nonlocal override_calls
            override_calls += 1
            return -999.0

    class HostileStr(str):
        def __str__(self) -> str:
            nonlocal override_calls
            override_calls += 1
            return "corrupted"

        def __hash__(self) -> int:
            raise AssertionError("caller-owned weight string reached hashing")

    class HostileTuple(tuple[object, ...]):
        def __iter__(self) -> Iterator[object]:
            raise AssertionError("caller-owned weight tuple iterator was invoked")

    class OverriddenWeights(DecoderWeights):
        def __getattribute__(self, name: str) -> object:
            if name in tracked_fields:
                reads[name] += 1
            return super().__getattribute__(name)

        def validated_compatibility(self) -> dict[tuple[str, str], float]:
            raise AssertionError("DecoderWeights subclass method was dispatched")

    compatibility_entry = HostileTuple(
        (
            HostileStr("body"),
            HostileStr("body"),
            HostileFloat(0.5),
        )
    )
    weights = OverriddenWeights(
        type_compatibility=cast(
            "tuple[tuple[str, str, float], ...]",
            HostileTuple((compatibility_entry,)),
        ),
        heading_types=cast("tuple[str, ...]", HostileTuple(())),
        body_types=cast("tuple[str, ...]", HostileTuple(())),
    )

    for decoder in (decode, brute_force_decode, greedy_decode):
        result = decoder(
            [
                candidate("left", 0, 1, 1.0),
                candidate("right", 1, 2, 1.0),
            ],
            weights=weights,
        )
        assert result.source_identities == ("left", "right")
        assert result.score == 2.5

    assert override_calls == 0
    assert reads == {field_name: 3 for field_name in tracked_fields}


def test_document_coordinate_and_string_hard_limits_fail_before_work() -> None:
    consumed = 0

    def candidates() -> Iterator[TypedSpanCandidate]:
        nonlocal consumed
        consumed += 1
        yield candidate("one", 0, 1, 1.0)

    with pytest.raises(
        ValueError,
        match="document budget exceeds the hard limit of 10000000",
    ):
        decode(candidates(), max_document_chars=10_000_001)
    assert consumed == 0

    with pytest.raises(
        ValueError,
        match="string codepoint budget exceeds the hard limit of 1000000",
    ):
        decode(candidates(), max_string_codepoints=1_000_001)
    assert consumed == 0

    oversized_coordinate = replace(
        candidate("oversized", 0, 1, 1.0),
        source_end=1 << 1000,
    )
    with pytest.raises(ValueError, match="compiled bit-length limit"):
        marginalize_candidates([oversized_coordinate])

    with pytest.raises(ValueError, match="compiled coordinate limit"):
        decode([], weights=DecoderWeights(contiguous_gap_chars=10_000_001))


def test_string_codepoint_budget_counts_exact_snapshots_before_hashing() -> None:
    raw = TypedSpanCandidate(
        candidate_id="c",
        source_identity="s",
        source_start=0,
        source_end=1,
        block_id="b",
        type_name="t",
        granularity="g",
        base_score=0.0,
    )

    spans = marginalize_candidates([raw], max_string_codepoints=5)
    assert spans[0].source_identity == "s"

    with pytest.raises(ValueError, match="string codepoint budget exceeded"):
        marginalize_candidates([raw], max_string_codepoints=4)

    default_weights = DecoderWeights()
    weight_codepoints = sum(
        len(value) for value in (*default_weights.heading_types, *default_weights.body_types)
    )
    assert decode(
        [raw],
        max_string_codepoints=weight_codepoints + 5,
    ).source_identities == ("s",)
    with pytest.raises(ValueError, match="string codepoint budget exceeded"):
        decode([raw], max_string_codepoints=weight_codepoints + 4)


def test_weight_entry_budgets_include_all_weight_tables() -> None:
    default_entry_count = len(DecoderWeights().heading_types) + len(DecoderWeights().body_types)
    assert decode([], max_weight_entries=default_entry_count).spans == ()

    with pytest.raises(ValueError, match="weight entry budget exceeded"):
        decode([], max_weight_entries=default_entry_count - 1)

    with pytest.raises(
        ValueError,
        match="weight entry budget exceeds the hard limit of 1024",
    ):
        decode([], max_weight_entries=1025)

    compatibility = tuple((f"previous-{index}", f"current-{index}", 0.0) for index in range(3))
    with pytest.raises(ValueError, match="weight entry budget exceeded"):
        decode(
            [],
            weights=DecoderWeights(
                type_compatibility=compatibility,
                heading_types=(),
                body_types=(),
            ),
            max_weight_entries=2,
        )

    class IterationBomb(tuple[tuple[str, str, float], ...]):
        def __iter__(self) -> Iterator[tuple[str, str, float]]:
            raise AssertionError("oversized weight tuple was copied before its length check")

    with pytest.raises(ValueError, match="weight entry budget exceeded"):
        decode(
            [],
            weights=DecoderWeights(
                type_compatibility=IterationBomb(compatibility),
                heading_types=(),
                body_types=(),
            ),
            max_weight_entries=2,
        )


def test_total_raw_ancestor_reference_budget_fails_before_large_set_work() -> None:
    candidates = [
        candidate("parent", 0, 10, 0.0, block_id="parent"),
        candidate(
            "child",
            0,
            5,
            0.0,
            block_id="child",
            type_name="body",
            ancestors=("parent",),
        ),
        candidate(
            "child",
            0,
            5,
            0.0,
            block_id="child",
            type_name="heading",
            ancestors=("parent",),
        ),
    ]

    with pytest.raises(ValueError, match="ancestor reference budget exceeded"):
        marginalize_candidates(candidates, max_ancestor_references=1)

    class IterationBomb(tuple[str, ...]):
        def __iter__(self) -> Iterator[str]:
            raise AssertionError("oversized ancestor tuple was copied before its length check")

    oversized_child = replace(
        candidates[1],
        ancestor_block_ids=IterationBomb(("parent", "another-parent")),
    )
    with pytest.raises(ValueError, match="ancestor reference budget exceeded"):
        marginalize_candidates(
            [candidates[0], oversized_child],
            max_ancestor_references=1,
        )


def test_duplicate_ids_types_and_ancestor_cycles_fail_closed() -> None:
    duplicated_id = candidate("one", 0, 5, 1.0)
    with pytest.raises(ValueError, match="duplicate candidate id"):
        marginalize_candidates([duplicated_id, duplicated_id])

    with pytest.raises(ValueError, match="duplicate latent type"):
        marginalize_candidates(
            [
                candidate("one", 0, 5, 1.0),
                TypedSpanCandidate(
                    candidate_id="different-id",
                    source_identity="one",
                    source_start=0,
                    source_end=5,
                    block_id="one",
                    type_name="body",
                    granularity="leaf",
                    base_score=2.0,
                ),
            ]
        )

    with pytest.raises(ValueError, match="own ancestor"):
        marginalize_candidates(
            [candidate("cycle", 0, 5, 1.0, block_id="cycle", ancestors=("cycle",))]
        )

    with pytest.raises(ValueError, match="ancestor identities must be unique"):
        marginalize_candidates(
            [
                candidate("parent", 0, 5, 1.0, block_id="parent"),
                candidate(
                    "child",
                    0,
                    2,
                    1.0,
                    block_id="child",
                    ancestors=("parent", "parent"),
                ),
            ]
        )

    with pytest.raises(ValueError, match="is not represented"):
        marginalize_candidates(
            [
                candidate(
                    "dangling",
                    0,
                    5,
                    1.0,
                    block_id="dangling",
                    ancestors=("missing-parent",),
                )
            ]
        )

    with pytest.raises(ValueError, match="ancestor graph contains a cycle"):
        marginalize_candidates(
            [
                candidate("left", 0, 5, 1.0, block_id="left", ancestors=("right",)),
                candidate("right", 0, 5, 1.0, block_id="right", ancestors=("left",)),
            ]
        )


def test_ancestry_is_preindexed_as_an_exact_immutable_set() -> None:
    spans = marginalize_candidates(
        [
            candidate("parent", 0, 10, 0.0, block_id="parent"),
            candidate(
                "child",
                2,
                8,
                1.0,
                block_id="child",
                ancestors=("parent",),
            ),
        ]
    )
    child = next(span for span in spans if span.block_id == "child")

    assert child._ancestor_block_id_set == frozenset({"parent"})
    assert isinstance(child._ancestor_block_id_set, frozenset)


def test_pairwise_compatibility_does_not_rescan_ancestor_tuples() -> None:
    class MembershipBomb(tuple[str, ...]):
        def __contains__(self, _item: object) -> bool:
            raise AssertionError("compatibility rescanned canonical ancestor tuples")

    left, right = marginalize_candidates(
        [
            candidate("left", 0, 1, 1.0),
            candidate("right", 2, 3, 1.0),
        ]
    )
    object.__setattr__(left, "ancestor_block_ids", MembershipBomb())
    object.__setattr__(right, "ancestor_block_ids", MembershipBomb())

    assert lattice_decoder._spans_are_compatible(left, right)


def test_brute_force_budget_counts_spans_not_latent_interpretations() -> None:
    candidates = [
        candidate("one", 0, 5, 1.0, type_name="body"),
        candidate("one", 0, 5, 0.0, type_name="heading"),
    ]

    result = brute_force_decode(candidates, max_spans=1)

    assert result.source_identities == ("one",)


def test_brute_force_hard_limits_reject_before_consuming_candidates() -> None:
    consumed = 0

    def candidates() -> Iterator[TypedSpanCandidate]:
        nonlocal consumed
        consumed += 1
        yield candidate("one", 0, 1, 1.0)

    with pytest.raises(
        ValueError,
        match="brute-force span budget exceeds the hard limit of 16",
    ):
        brute_force_decode(candidates(), max_spans=17)
    assert consumed == 0

    with pytest.raises(
        ValueError,
        match="brute-force candidate budget exceeds the hard limit of 64",
    ):
        brute_force_decode(candidates(), max_candidates=65)
    assert consumed == 0


def test_brute_force_zero_transition_features_skip_equivalent_posterior_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = [
        candidate("left", 0, 2, 1.0, type_name="body"),
        candidate("left", 0, 2, 0.25, type_name="heading"),
        candidate("right", 2, 4, 2.0, type_name="body"),
        candidate("right", 2, 4, -0.5, type_name="heading"),
    ]
    spans = marginalize_candidates(candidates)
    expected_score = float(
        sum(
            (Fraction.from_float(span.marginal_score) for span in spans),
            Fraction(0),
        )
    )
    monkeypatch.setattr(
        lattice_decoder,
        "_oracle_type_probabilities",
        lambda _span: pytest.fail(
            "zero transition features must not evaluate posterior transition work"
        ),
    )

    result = brute_force_decode(candidates)

    assert result.source_identities == ("left", "right")
    assert result.score == expected_score


def test_brute_force_precomputes_each_independent_transition_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = [candidate(f"span-{index}", 2 * index, 2 * index + 1, 1.0) for index in range(8)]
    original = lattice_decoder._oracle_transition_score
    calls = 0

    def counting_transition(
        previous: lattice_decoder.MarginalSpan,
        current: lattice_decoder.MarginalSpan,
        context: lattice_decoder._ExactScoringContext,
    ) -> Fraction:
        nonlocal calls
        calls += 1
        return original(previous, current, context)

    monkeypatch.setattr(
        lattice_decoder,
        "_oracle_transition_score",
        counting_transition,
    )

    result = brute_force_decode(candidates)

    assert len(result.spans) == len(candidates)
    assert calls == len(candidates) * (len(candidates) - 1) // 2


def test_ties_are_deterministic_across_input_order() -> None:
    candidates = [
        candidate("b", 0, 5, 1.0),
        candidate("a", 0, 5, 1.0),
    ]

    forward = decode(candidates)
    reverse = decode(reversed(candidates))

    assert forward.source_identities == ("a",)
    assert reverse == forward


def test_exact_score_arithmetic_prevents_accumulation_order_from_changing_top_1() -> None:
    half_ulp = 2.0**-53
    candidates = [
        candidate("a", 0, 1, 1.0),
        candidate("b", 1, 2, half_ulp),
        candidate("c", 2, 3, half_ulp),
        candidate("coarse", 0, 3, 1.0),
    ]

    dynamic = decode(candidates)
    oracle = brute_force_decode(candidates)

    assert dynamic.source_identities == ("a", "b", "c")
    assert oracle.source_identities == dynamic.source_identities
    assert dynamic.score == oracle.score == 1.0 + 2.0**-52


def test_bounded_rational_operations_match_fraction_exactly_in_the_accepted_domain() -> None:
    arithmetic = lattice_decoder._RationalArithmetic(
        max_component_bits=256,
        max_admission_fuel=1 << 30,
    )
    operands = (
        (Fraction(17, 19), Fraction(-23, 29)),
        (Fraction(1, 65_521), Fraction(1, 65_519)),
        (Fraction(-(1 << 80) + 1, (1 << 79) - 1), Fraction(31, 37)),
    )

    for left, right in operands:
        assert arithmetic.add(left, right) == left + right
        assert arithmetic.subtract(left, right) == left - right
        assert arithmetic.multiply(left, right) == left * right
        assert arithmetic.divide(left, right) == left / right


def test_rational_denominator_growth_fails_before_product_allocation() -> None:
    component_bits = lattice_decoder._HARD_MAX_RATIONAL_COMPONENT_BITS
    arithmetic = lattice_decoder._RationalArithmetic(
        max_component_bits=component_bits,
        max_admission_fuel=lattice_decoder._HARD_MAX_RATIONAL_ADMISSION_FUEL,
    )
    left = Fraction(1, (1 << component_bits) - 1)
    right = Fraction(1, (1 << (component_bits - 1)) - 1)

    tracemalloc.start()
    try:
        with pytest.raises(ValueError, match="component bit limit exceeded"):
            arithmetic.multiply(left, right)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # The bounded-product preflight rejects before materializing a component
    # wider than the compiled ceiling.
    assert peak_bytes < 256_000


def test_rational_admission_fuel_fails_after_validation_before_result_arithmetic() -> None:
    arithmetic = lattice_decoder._RationalArithmetic(
        max_component_bits=64,
        max_admission_fuel=255,
    )

    with pytest.raises(ValueError, match="admission fuel exhausted"):
        arithmetic.multiply(Fraction(1, 255), Fraction(1, 253))
    assert arithmetic.used_admission_fuel == 0


def test_rational_limits_are_base_ints_frozen_and_hard_bounded() -> None:
    override_calls = 0

    class EvilInt(int):
        def __int__(self) -> int:
            nonlocal override_calls
            override_calls += 1
            return 1

        def bit_length(self) -> int:
            nonlocal override_calls
            override_calls += 1
            return 1

    arithmetic = lattice_decoder._RationalArithmetic(
        max_component_bits=cast("int", EvilInt(64)),
        max_admission_fuel=cast("int", EvilInt(1024)),
    )

    assert type(arithmetic.max_component_bits) is int
    assert type(arithmetic.max_admission_fuel) is int
    assert override_calls == 0

    def assign_frozen_limit(attribute: str, value: int) -> None:
        setattr(arithmetic, attribute, value)

    with pytest.raises(FrozenInstanceError):
        assign_frozen_limit("max_component_bits", 32)
    with pytest.raises(FrozenInstanceError):
        assign_frozen_limit("max_admission_fuel", 512)
    with pytest.raises(ValueError, match="component bit limit exceeds the hard limit"):
        lattice_decoder._RationalArithmetic(
            max_component_bits=lattice_decoder._HARD_MAX_RATIONAL_COMPONENT_BITS + 1,
        )
    with pytest.raises(ValueError, match="admission fuel exceeds the hard limit"):
        lattice_decoder._RationalArithmetic(
            max_admission_fuel=lattice_decoder._HARD_MAX_RATIONAL_ADMISSION_FUEL + 1,
        )


def test_rational_from_int_snapshots_without_subclass_dispatch() -> None:
    override_calls = 0

    class EvilInt(int):
        def __int__(self) -> int:
            nonlocal override_calls
            override_calls += 1
            return -999

        def bit_length(self) -> int:
            nonlocal override_calls
            override_calls += 1
            return 1

    arithmetic = lattice_decoder._RationalArithmetic(
        max_component_bits=64,
        max_admission_fuel=25,
    )
    result = arithmetic.from_int(EvilInt(17))

    assert result == Fraction(17)
    assert type(result.numerator) is int
    assert arithmetic.used_admission_fuel == 25
    assert override_calls == 0
    with pytest.raises(ValueError, match="admission fuel exhausted"):
        arithmetic.from_int(EvilInt(17))
    assert arithmetic.used_admission_fuel == 25
    assert override_calls == 0


def test_rational_from_int_rejects_oversized_subclass_before_base_copy() -> None:
    class OversizedInt(int):
        pass

    oversized = OversizedInt(1 << 8_000_000)
    arithmetic = lattice_decoder._RationalArithmetic()

    tracemalloc.start()
    try:
        with pytest.raises(ValueError, match="compiled bit-length limit"):
            arithmetic.from_int(oversized)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert arithmetic.used_admission_fuel == 0
    assert peak_bytes < 512_000


def test_rational_validation_rejects_fraction_subclasses_before_field_reads() -> None:
    field_reads = 0

    class StatefulFraction(Fraction):
        @property
        def numerator(self) -> int:
            nonlocal field_reads
            field_reads += 1
            raise AssertionError("Fraction subclass numerator was read")

        @property
        def denominator(self) -> int:
            nonlocal field_reads
            field_reads += 1
            raise AssertionError("Fraction subclass denominator was read")

    arithmetic = lattice_decoder._RationalArithmetic()
    hostile = StatefulFraction(1, 3)

    with pytest.raises(ValueError, match="must be base Fractions"):
        arithmetic.add(hostile, Fraction(1))
    with pytest.raises(ValueError, match="must be base Fractions"):
        arithmetic.negate(hostile)
    assert field_reads == 0
    assert arithmetic.used_admission_fuel == 0


def _mutated_fraction(numerator: object, denominator: object) -> Fraction:
    value = Fraction(1)
    object.__setattr__(value, "_numerator", numerator)
    object.__setattr__(value, "_denominator", denominator)
    return value


def _assert_invalid_fraction_fails_all_arithmetic_entries(
    value: Fraction,
    *,
    message: str,
) -> None:
    arithmetic = lattice_decoder._RationalArithmetic(
        max_component_bits=64,
        max_admission_fuel=4096,
    )
    zero = Fraction(0)
    one = Fraction(1)
    operations: tuple[Callable[[], Fraction], ...] = (
        lambda: arithmetic.negate(value),
        lambda: arithmetic.add(value, zero),
        lambda: arithmetic.add(zero, value),
        lambda: arithmetic.subtract(value, zero),
        lambda: arithmetic.subtract(zero, value),
        lambda: arithmetic.multiply(value, zero),
        lambda: arithmetic.multiply(zero, value),
        lambda: arithmetic.multiply(value, one),
        lambda: arithmetic.multiply(one, value),
        lambda: arithmetic.divide(value, zero),
        lambda: arithmetic.divide(value, one),
        lambda: arithmetic.divide(zero, value),
        lambda: arithmetic.divide(one, value),
        lambda: arithmetic.sum((value,)),
    )

    for operation in operations:
        with pytest.raises(ValueError, match=message):
            operation()
        assert arithmetic.used_admission_fuel == 0


@pytest.mark.parametrize(
    ("numerator", "denominator", "message"),
    [
        (1, 0, "denominator must be positive"),
        (1, -2, "denominator must be positive"),
        (2, 4, "components must be in lowest terms"),
        (0, 2, "zero must have denominator one"),
    ],
)
def test_mutated_base_fraction_noncanonical_states_fail_all_entries(
    numerator: int,
    denominator: int,
    message: str,
) -> None:
    _assert_invalid_fraction_fails_all_arithmetic_entries(
        _mutated_fraction(numerator, denominator),
        message=message,
    )


def test_mutated_base_fraction_rejects_hostile_int_components_without_dispatch() -> None:
    override_calls = 0

    class HostileInt(int):
        def __eq__(self, _other: object) -> bool:
            nonlocal override_calls
            override_calls += 1
            raise AssertionError("hostile component reached equality")

        def __le__(self, _other: object) -> bool:
            nonlocal override_calls
            override_calls += 1
            raise AssertionError("hostile component reached ordering")

        def __abs__(self) -> int:
            nonlocal override_calls
            override_calls += 1
            raise AssertionError("hostile component reached abs")

        def bit_length(self) -> int:
            nonlocal override_calls
            override_calls += 1
            raise AssertionError("hostile component reached bit_length")

    for value in (
        _mutated_fraction(HostileInt(1), 2),
        _mutated_fraction(1, HostileInt(2)),
    ):
        _assert_invalid_fraction_fails_all_arithmetic_entries(
            value,
            message="components must be base ints",
        )
    assert override_calls == 0


def test_mutated_base_fraction_missing_components_fail_all_entries() -> None:
    for missing_component in ("_numerator", "_denominator"):
        value = Fraction(1, 2)
        object.__delattr__(value, missing_component)
        _assert_invalid_fraction_fails_all_arithmetic_entries(
            value,
            message="components must be present",
        )


def test_rational_fast_paths_return_rebuilt_snapshots() -> None:
    arithmetic = lattice_decoder._RationalArithmetic(
        max_component_bits=64,
        max_admission_fuel=100,
    )
    value = Fraction(17, 19)

    result = arithmetic.multiply(value, Fraction(1))

    assert result == value
    assert result is not value


def test_rational_fast_paths_consume_binary_admission_fuel_before_returning() -> None:
    value = Fraction(17, 19)
    zero = Fraction(0)
    one = Fraction(1)
    negative_one = Fraction(-1)
    width = 5
    fuel = 4 * width * width
    cases = (
        ("add", (value, zero), value),
        ("add", (zero, value), value),
        ("subtract", (value, zero), value),
        ("subtract", (zero, value), -value),
        ("multiply", (value, zero), zero),
        ("multiply", (value, one), value),
        ("multiply", (value, negative_one), -value),
        ("divide", (zero, value), zero),
        ("divide", (value, one), value),
        ("divide", (value, negative_one), -value),
    )

    for method_name, operands, expected in cases:
        arithmetic = lattice_decoder._RationalArithmetic(
            max_component_bits=64,
            max_admission_fuel=fuel,
        )
        operation = getattr(arithmetic, method_name)
        assert operation(*operands) == expected
        assert arithmetic.used_admission_fuel == fuel
        with pytest.raises(ValueError, match="admission fuel exhausted"):
            operation(*operands)
        assert arithmetic.used_admission_fuel == fuel


def test_rational_negation_consumes_unary_admission_fuel_before_returning() -> None:
    value = Fraction(17, 19)
    arithmetic = lattice_decoder._RationalArithmetic(
        max_component_bits=64,
        max_admission_fuel=25,
    )

    assert arithmetic.negate(value) == -value
    assert arithmetic.used_admission_fuel == 25
    with pytest.raises(ValueError, match="admission fuel exhausted"):
        arithmetic.negate(value)
    assert arithmetic.used_admission_fuel == 25


def test_rational_large_fast_path_cannot_bypass_minimal_admission_fuel() -> None:
    arithmetic = lattice_decoder._RationalArithmetic(
        max_component_bits=lattice_decoder._HARD_MAX_RATIONAL_COMPONENT_BITS,
        max_admission_fuel=1,
    )
    large = _mutated_fraction((1 << 16_383) - 1, 1)

    for operands in (
        (large, Fraction(0)),
        (large, Fraction(1)),
        (large, Fraction(-1)),
    ):
        with pytest.raises(ValueError, match="admission fuel exhausted"):
            arithmetic.multiply(*operands)
        assert arithmetic.used_admission_fuel == 0


def test_derived_score_overflow_fails_closed_in_both_decoders() -> None:
    candidates = [
        candidate("short", 0, 2, 1.7e308),
        candidate("long", 0, 3, -1.7e308),
    ]
    weights = DecoderWeights(coverage_reward_per_char=1e308)

    for decoder in (decode, brute_force_decode):
        with pytest.raises(ValueError, match="finite binary64 range"):
            decoder(candidates, weights=weights)

    combined_overflow = replace(
        candidate("combined-overflow", 0, 1, 1e308),
        type_logit=1e308,
    )
    with pytest.raises(ValueError, match="combined score must remain finite"):
        marginalize_candidates([combined_overflow])


@pytest.mark.parametrize(
    ("source_start", "source_end"),
    [
        (0.5, 1.5),
        (math.nan, 1),
        (0, math.nan),
        (False, 1),
    ],
)
def test_source_coordinates_are_strict_integers(
    source_start: object,
    source_end: object,
) -> None:
    invalid = replace(
        candidate("invalid-coordinate", 0, 1, 1.0),
        source_start=cast("int", source_start),
        source_end=cast("int", source_end),
    )

    with pytest.raises(ValueError, match="must be an integer"):
        marginalize_candidates([invalid])


def test_thresholds_and_budgets_reject_non_integer_non_finite_values() -> None:
    candidates = [candidate("one", 0, 1, 1.0)]

    with pytest.raises(ValueError, match="contiguous gap must be an integer"):
        decode(
            candidates,
            weights=DecoderWeights(contiguous_gap_chars=cast("int", math.nan)),
        )
    with pytest.raises(ValueError, match="candidate budget must be an integer"):
        decode(candidates, max_candidates=cast("int", math.nan))
    with pytest.raises(ValueError, match="document budget must be an integer"):
        decode(candidates, max_document_chars=cast("int", math.inf))


def test_iterative_ancestor_validation_accepts_the_full_depth_budget() -> None:
    count = 4096
    candidates: list[TypedSpanCandidate] = []
    for depth in range(count):
        block_number = count - 1 - depth
        block_id = f"block-{block_number:04d}"
        ancestors = () if depth == 0 else (f"block-{block_number + 1:04d}",)
        candidates.append(
            candidate(
                f"source-{block_number:04d}",
                depth,
                2 * count - depth,
                0.0,
                block_id=block_id,
                ancestors=ancestors,
            )
        )

    spans = marginalize_candidates(candidates)

    assert len(spans) == count


@pytest.mark.parametrize(
    ("granularity", "ancestors"),
    [
        ("container", ()),
        ("leaf", ("unrepresented-parent",)),
    ],
)
def test_block_aliases_with_conflicting_canonical_metadata_are_rejected(
    granularity: str,
    ancestors: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="inconsistent canonical metadata"):
        marginalize_candidates(
            [
                candidate("alias-a", 0, 5, 1.0, block_id="shared"),
                candidate(
                    "alias-b",
                    0,
                    5,
                    1.0,
                    block_id="shared",
                    granularity=granularity,
                    ancestors=ancestors,
                ),
            ]
        )


def test_block_aliases_are_allowed_when_all_canonical_metadata_agrees() -> None:
    result = decode(
        [
            candidate("alias-b", 0, 5, 1.0, block_id="shared"),
            candidate("alias-a", 0, 5, 1.0, block_id="shared"),
        ]
    )

    assert result.source_identities == ("alias-a",)


def test_exhaustive_oracle_does_not_reuse_dp_compatibility_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = [
        candidate("a", 0, 2, 1.0),
        candidate("b", 1, 3, 1.0),
    ]
    monkeypatch.setattr(
        lattice_decoder,
        "_spans_are_compatible",
        lambda _previous, _current: True,
    )

    deliberately_corrupted_dynamic = decode(candidates)
    independent_oracle = brute_force_decode(candidates)

    assert deliberately_corrupted_dynamic.source_identities == ("a", "b")
    assert independent_oracle.source_identities == ("a",)


def test_dp_state_is_backpointer_based_instead_of_retaining_path_tuples() -> None:
    state_fields = {field.name for field in fields(lattice_decoder._PathState)}

    assert {"terminal_index", "predecessor_index"} <= state_fields
    assert "spans" not in state_fields

    candidates = [
        candidate(f"span-{index:03d}", index * 2, index * 2 + 1, 0.0) for index in range(128)
    ]
    result = decode(candidates)
    assert len(result.spans) == len(candidates)


@pytest.mark.parametrize("seed", range(20))
def test_exact_path_lex_index_matches_materialized_tuple_order(seed: int) -> None:
    generator = random.Random(seed + 91_000)
    count = 64
    identities = generator.sample(
        [f"identity-{index:04d}" for index in range(512)],
        count,
    )
    spans = marginalize_candidates(
        [
            candidate(identity, 2 * index, 2 * index + 1, 0.0)
            for index, identity in enumerate(identities)
        ]
    )
    lex_index = lattice_decoder._ExactPathLexIndex(spans)
    materialized_paths: list[tuple[str, ...]] = []

    for current_index, span in enumerate(spans):
        predecessor_index = (
            None
            if current_index == 0 or generator.random() < 0.2
            else generator.randrange(current_index)
        )
        predecessor_path = (
            () if predecessor_index is None else materialized_paths[predecessor_index]
        )
        path = (*predecessor_path, span.source_identity)
        lex_index.append(
            lattice_decoder._PathState(
                terminal_index=current_index,
                predecessor_index=predecessor_index,
                span_count=len(path),
            ),
            current_index=current_index,
        )
        materialized_paths.append(path)

    indexed_paths = [(), *materialized_paths]
    for left_node, left_path in enumerate(indexed_paths):
        for right_node, right_path in enumerate(indexed_paths):
            assert lex_index.is_less(left_node, right_node) == (left_path < right_path)


def test_exact_path_lex_index_fits_the_full_compiled_depth_budget() -> None:
    count = 4096
    spans = marginalize_candidates(
        [candidate(f"span-{index:04d}", 2 * index, 2 * index + 1, 0.0) for index in range(count)]
    )
    lex_index = lattice_decoder._ExactPathLexIndex(spans)

    for current_index in range(count):
        lex_index.append(
            lattice_decoder._PathState(
                terminal_index=current_index,
                predecessor_index=(None if current_index == 0 else current_index - 1),
                span_count=current_index + 1,
            ),
            current_index=current_index,
        )

    assert lex_index.is_less(1, count)
    assert not lex_index.is_less(count, 1)
    assert sum(len(row) for row in lex_index._jumps) <= (count + 1) * (count.bit_length() + 1)


def test_tie_heavy_decode_reconstructs_only_the_selected_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    width = 40
    candidates = [
        candidate(f"prefix-{index:03d}", 2 * index, 2 * index + 1, 0.0) for index in range(width)
    ]
    group_start = 2 * width
    candidates.extend(
        candidate(f"predecessor-{index:03d}", group_start, group_start + 1, 0.0)
        for index in range(width)
    )
    candidates.extend(
        candidate(f"current-{index:03d}", group_start + 2, group_start + 3, 0.0)
        for index in range(width)
    )
    original = lattice_decoder._state_span_indices
    reconstruction_calls = 0

    def counting_reconstruction(
        state: lattice_decoder._PathState,
        states: list[lattice_decoder._PathState],
    ) -> tuple[int, ...]:
        nonlocal reconstruction_calls
        reconstruction_calls += 1
        return original(state, states)

    monkeypatch.setattr(
        lattice_decoder,
        "_state_span_indices",
        counting_reconstruction,
    )

    result = decode(candidates)

    assert result.source_identities[-2:] == (
        "predecessor-000",
        "current-000",
    )
    assert reconstruction_calls == 1


@pytest.mark.parametrize("seed", range(50))
def test_tie_heavy_dynamic_program_matches_tuple_oracle(seed: int) -> None:
    generator = random.Random(seed + 123_000)
    candidates = []
    for index in range(generator.randint(1, 12)):
        start = generator.randint(0, 18)
        end = start + generator.randint(1, 7)
        candidates.append(
            candidate(
                f"source-{generator.randrange(10_000):04d}-{index:02d}",
                start,
                end,
                generator.choice((-1.0, 0.0, 1.0)),
            )
        )
    generator.shuffle(candidates)

    dynamic = decode(candidates)
    tuple_oracle = brute_force_decode(candidates)

    assert dynamic == tuple_oracle


@pytest.mark.parametrize("seed", range(30))
def test_dynamic_program_matches_brute_force_oracle(seed: int) -> None:
    generator = random.Random(seed)
    candidates: list[TypedSpanCandidate] = []
    for index in range(generator.randint(1, 9)):
        start = generator.randint(0, 24)
        end = start + generator.randint(1, 8)
        identity = f"source-{index}"
        candidates.append(candidate(identity, start, end, generator.uniform(-2.0, 4.0)))
        if generator.random() < 0.35:
            candidates.append(
                candidate(
                    identity,
                    start,
                    end,
                    generator.uniform(-2.0, 4.0),
                    type_name="heading",
                )
            )
    weights = DecoderWeights(
        coverage_reward_per_char=0.01,
        gap_penalty_per_char=0.02,
        fragmentation_penalty=0.1,
        contiguous_gap_chars=2,
        heading_to_body_bonus=0.25,
        type_compatibility=(("heading", "body", 0.15),),
    )

    dynamic = decode(candidates, weights=weights)
    oracle = brute_force_decode(candidates, weights=weights)

    assert dynamic.source_identities == oracle.source_identities
    assert dynamic.score == pytest.approx(oracle.score)
    assert dynamic.covered_characters == oracle.covered_characters
    assert dynamic.fragments == oracle.fragments


def test_exhaustive_small_score_grid_matches_oracle_and_dominates_greedy() -> None:
    shapes = (
        ("left", 0, 2),
        ("middle", 1, 3),
        ("right", 2, 4),
        ("coarse", 0, 4),
    )

    for scores in product((-1.0, 0.0, 1.0), repeat=len(shapes)):
        candidates = [
            candidate(identity, start, end, score)
            for (identity, start, end), score in zip(shapes, scores, strict=True)
        ]
        for ordered_candidates in (candidates, list(reversed(candidates))):
            exact = decode(ordered_candidates)
            oracle = brute_force_decode(ordered_candidates)
            assert exact == oracle

            for baseline in (
                score_first_greedy_decode(ordered_candidates),
                source_order_greedy_decode(ordered_candidates),
            ):
                assert not lattice_decoder._oracle_is_better(
                    score=Fraction.from_float(baseline.score),
                    spans=baseline.spans,
                    incumbent_score=Fraction.from_float(exact.score),
                    incumbent_spans=exact.spans,
                )

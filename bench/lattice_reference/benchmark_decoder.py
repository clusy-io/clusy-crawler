"""Small deterministic complexity probe for the unwired reference decoder."""

from __future__ import annotations

import argparse
from time import perf_counter

from bench.lattice_reference import TypedSpanCandidate, decode
from bench.lattice_reference.decoder import (
    _HARD_MAX_ANCESTOR_REFERENCES,
    _HARD_MAX_BINARY64_INTEGER_BITS,
    _HARD_MAX_BRUTE_FORCE_CANDIDATES,
    _HARD_MAX_BRUTE_FORCE_SPANS,
    _HARD_MAX_CANDIDATES,
    _HARD_MAX_COORDINATE_BITS,
    _HARD_MAX_DOCUMENT_CHARS,
    _HARD_MAX_RATIONAL_ADMISSION_FUEL,
    _HARD_MAX_RATIONAL_COMPONENT_BITS,
    _HARD_MAX_STRING_CODEPOINTS,
    _HARD_MAX_WEIGHT_ENTRIES,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spans", type=int, default=512)
    args = parser.parse_args()
    if args.spans <= 0 or args.spans > _HARD_MAX_CANDIDATES:
        parser.error(f"--spans must be between 1 and {_HARD_MAX_CANDIDATES}")

    candidates = [
        TypedSpanCandidate(
            candidate_id=f"candidate-{index}",
            source_identity=f"source-{index}",
            source_start=index * 16,
            source_end=index * 16 + 12,
            block_id=f"block-{index}",
            type_name="body",
            granularity="leaf",
            base_score=1.0,
        )
        for index in range(args.spans)
    ]
    started = perf_counter()
    result = decode(candidates)
    elapsed_ms = (perf_counter() - started) * 1000
    print(
        {
            "spans": args.spans,
            "selected": len(result.spans),
            "elapsed_ms": round(elapsed_ms, 3),
            "algorithm": "exact backpointer DP with deterministic LCA tie index",
            "operation_count_bound": ("unit-cost O(n^2*t^2 + q*log(n)), worst O(n^2*(t^2+log(n)))"),
            "ancestry_lookup": "preindexed immutable set; expected O(1)",
            "compiled_limits": {
                "raw_candidates": _HARD_MAX_CANDIDATES,
                "raw_ancestor_references": _HARD_MAX_ANCESTOR_REFERENCES,
                "document_characters": _HARD_MAX_DOCUMENT_CHARS,
                "coordinate_bits": _HARD_MAX_COORDINATE_BITS,
                "binary64_integer_bits": _HARD_MAX_BINARY64_INTEGER_BITS,
                "total_string_codepoints": _HARD_MAX_STRING_CODEPOINTS,
                "weight_entries": _HARD_MAX_WEIGHT_ENTRIES,
                "rational_component_bits": _HARD_MAX_RATIONAL_COMPONENT_BITS,
                "rational_admission_fuel": _HARD_MAX_RATIONAL_ADMISSION_FUEL,
                "oracle_raw_candidates": _HARD_MAX_BRUTE_FORCE_CANDIDATES,
                "oracle_marginal_spans": _HARD_MAX_BRUTE_FORCE_SPANS,
            },
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

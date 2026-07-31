#!/usr/bin/env python3
"""Paired local microbenchmark for the unwired local-atomic batch bridge.

This is a synthetic mechanism benchmark, not quality or SOTA evidence. Both
arms receive the same pre-parsed IR for the native measurement and the same
HTML/candidate pair for the overlay measurement. The script refuses to time
unless certificate bytes, exact replays, decisions, and output bytes match.
"""

from __future__ import annotations

import argparse
import gc
import json
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path

from clusy_native import (
    create_local_atomic_selection_batch_v0,
    create_local_atomic_selection_certificate_v0,
    extract_document_ir_v2,
    verify_and_replay_local_atomic_selection_batch_v0,
    verify_and_replay_local_atomic_selection_certificate_v0,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.atomic_structure_overlay_v0 import (
    AtomicStructureOverlayV0Config,
    _propose_atomic_structure_overlay_v0,
)

_MAX_TOTAL_CERTIFICATE_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_OUTPUT_BYTES = 8 * 1024 * 1024


def _fixture(atom_count: int, filler_paragraphs: int) -> tuple[str, str]:
    atoms: list[str] = []
    candidate_atoms: list[str] = []
    for index in range(atom_count):
        if index % 2 == 0:
            text = f"unique_code_{index} = {index}\nprint(unique_code_{index})"
            atoms.append(
                f'<pre><code class="language-python">{text}</code></pre>'
            )
            candidate_atoms.append(text)
        else:
            atoms.append(
                "<table><thead>"
                f"<tr><th>Name_{index}</th><th>Score_{index}</th></tr>"
                "</thead><tbody>"
                f"<tr><td>Clusy_{index}</td><td>{index}</td></tr>"
                "</tbody></table>"
            )
            candidate_atoms.append(
                f"Name_{index} Score_{index}\nClusy_{index} {index}"
            )
    filler = "".join(
        f"<p>bounded filler paragraph {index} identity {index * 17}</p>"
        for index in range(filler_paragraphs)
    )
    html = (
        "<!doctype html><html><head><title>local atomic micro</title></head>"
        f"<body><main>{filler}{''.join(atoms)}</main></body></html>"
    )
    return html, "\n\n".join(candidate_atoms)


def _quantile(values: list[int], probability: float) -> int:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * probability)))
    return ordered[index]


def _measure(
    left_name: str,
    left: Callable[[], object],
    right_name: str,
    right: Callable[[], object],
    *,
    expected: object,
    iterations: int,
    warmups: int,
) -> dict[str, object]:
    for _ in range(warmups):
        if left() != expected or right() != expected:
            raise RuntimeError("warmup output drift")
    timings = {left_name: [], right_name: []}
    gc.collect()
    gc.disable()
    try:
        for iteration in range(iterations):
            arms = ((left_name, left), (right_name, right))
            if iteration % 2:
                arms = tuple(reversed(arms))
            for name, call in arms:
                started = time.perf_counter_ns()
                observed = call()
                elapsed = time.perf_counter_ns() - started
                if observed != expected:
                    raise RuntimeError(f"{name} output drifted during timing")
                timings[name].append(elapsed)
    finally:
        gc.enable()
    output: dict[str, object] = {}
    for name, values in timings.items():
        output[name] = {
            "median_ns": statistics.median(values),
            "p95_ns": _quantile(values, 0.95),
        }
    left_median = statistics.median(timings[left_name])
    right_median = statistics.median(timings[right_name])
    output["median_speedup"] = left_median / right_median
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--atoms", type=int, default=24)
    parser.add_argument("--filler-paragraphs", type=int, default=500)
    parser.add_argument("--iterations", type=int, default=7)
    parser.add_argument("--warmups", type=int, default=2)
    args = parser.parse_args()
    if args.atoms <= 0 or args.atoms > 256:
        parser.error("--atoms must be between 1 and 256")
    if args.filler_paragraphs < 0 or args.filler_paragraphs > 10_000:
        parser.error("--filler-paragraphs must be between 0 and 10000")
    if args.iterations < 3 or args.iterations > 100:
        parser.error("--iterations must be between 3 and 100")
    if args.warmups < 0 or args.warmups > 20:
        parser.error("--warmups must be between 0 and 20")

    html, candidate = _fixture(args.atoms, args.filler_paragraphs)
    document = extract_document_ir_v2(html)
    atom_ids = tuple(
        element.id
        for element in sorted(document.elements, key=lambda item: item.order)
        if element.tag in {"pre", "table"}
    )
    if len(atom_ids) != args.atoms:
        raise RuntimeError(f"expected {args.atoms} local atoms, found {len(atom_ids)}")

    def legacy_native() -> tuple[tuple[bytes, str], ...]:
        output: list[tuple[bytes, str]] = []
        for selected_id in atom_ids:
            certificate = create_local_atomic_selection_certificate_v0(
                document,
                [selected_id],
                max_output_bytes=512 * 1024,
            )
            replay = verify_and_replay_local_atomic_selection_certificate_v0(
                document,
                certificate,
                max_output_bytes=512 * 1024,
            )
            output.append((certificate.encoded, replay.markdown))
        return tuple(output)

    def batch_native() -> tuple[tuple[bytes, str], ...]:
        created = create_local_atomic_selection_batch_v0(
            document,
            atom_ids,
            max_output_bytes=512 * 1024,
            max_total_certificate_bytes=_MAX_TOTAL_CERTIFICATE_BYTES,
            max_total_output_bytes=_MAX_TOTAL_OUTPUT_BYTES,
        )
        if not all(item.accepted for item in created):
            raise RuntimeError("synthetic local batch creation rejected an atom")
        replayed = verify_and_replay_local_atomic_selection_batch_v0(
            document,
            atom_ids,
            (item.certificate for item in created),
            max_output_bytes=512 * 1024,
            max_total_certificate_bytes=_MAX_TOTAL_CERTIFICATE_BYTES,
            max_total_output_bytes=_MAX_TOTAL_OUTPUT_BYTES,
        )
        if not all(item.accepted and item.verified for item in replayed):
            raise RuntimeError("synthetic local batch replay rejected an atom")
        return tuple((item.certificate, item.markdown) for item in replayed)

    native_expected = legacy_native()
    if batch_native() != native_expected:
        raise RuntimeError("batch certificate bytes or replay differ from legacy")

    config = AtomicStructureOverlayV0Config(
        enabled=True,
        max_atoms=args.atoms,
        max_total_certificate_bytes=2 * 1024 * 1024,
    )

    def legacy_overlay() -> object:
        return _propose_atomic_structure_overlay_v0(
            html,
            candidate,
            config=config,
            use_batch_certificate_bridge=False,
        )

    def batch_overlay() -> object:
        return _propose_atomic_structure_overlay_v0(
            html,
            candidate,
            config=config,
            use_batch_certificate_bridge=True,
        )

    overlay_expected = legacy_overlay()
    if not overlay_expected.accepted:
        raise RuntimeError(
            f"synthetic legacy overlay was rejected: {overlay_expected.reason}"
        )
    if batch_overlay() != overlay_expected:
        raise RuntimeError("batch overlay decision or output differs from legacy")

    report = {
        "schema_version": "local-atomic-selection-batch-v0.micro.1",
        "claim_scope": "synthetic local mechanism benchmark; not quality/SOTA evidence",
        "source_bytes": len(html.encode("utf-8")),
        "candidate_bytes": len(candidate.encode("utf-8")),
        "graph_elements": document.element_count,
        "graph_text_runs": document.text_run_count,
        "atoms": len(atom_ids),
        "iterations": args.iterations,
        "warmups": args.warmups,
        "exact_certificate_replay_and_overlay_decision_identity": True,
        "native_bridge": {
            "legacy_graph_clones_per_iteration": 2 * len(atom_ids),
            "batch_graph_clones_per_iteration": 2,
            **_measure(
                "legacy",
                legacy_native,
                "batch",
                batch_native,
                expected=native_expected,
                iterations=args.iterations,
                warmups=args.warmups,
            ),
        },
        "overlay_proposal": {
            "legacy_graph_clones_per_iteration": 3 * len(atom_ids),
            "batch_graph_clones_per_iteration": 2,
            **_measure(
                "legacy",
                legacy_overlay,
                "batch",
                batch_overlay,
                expected=overlay_expected,
                iterations=args.iterations,
                warmups=args.warmups,
            ),
        },
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

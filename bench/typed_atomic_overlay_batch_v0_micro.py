#!/usr/bin/env python3
"""Paired microbenchmark for the unwired typed atomic batch primitive.

This is a mechanism benchmark, not a WebMainBench quality result. Both arms
parse the same synthetic document once before timing and emit byte-identical
``selection-certificate.v0`` records and Markdown:

* ``legacy`` creates and verifies every atom through separate Python/native
  calls, cloning and validating the full graph twice per atom.
* ``batch`` creates the whole batch in one call and verifies it in one call,
  cloning and validating the full graph twice per batch.
"""

from __future__ import annotations

import argparse
import gc
import json
import statistics
import time
from collections.abc import Callable

from clusy_native import (
    create_selection_certificate_v0,
    extract_document_ir_v2,
    verify_and_replay_selection_certificate_v0,
)
from clusy_native._native import (
    create_typed_atomic_overlay_batch_v0_native,
    verify_typed_atomic_overlay_batch_v0_native,
)
from clusy_native.typed_atomic_overlay_batch_v0 import _outermost_typed_atom_ids


def _html(atom_count: int, filler_paragraphs: int) -> str:
    atoms: list[str] = []
    for index in range(atom_count):
        kind = index % 4
        if kind == 0:
            atoms.append(
                f'<pre><code class="language-python">value_{index} = {index}\n'
                f"print(value_{index})</code></pre>"
            )
        elif kind == 1:
            atoms.append(
                "<table><tbody>"
                f"<tr><th>name_{index}</th><th>score_{index}</th></tr>"
                f"<tr><td>clusy_{index}</td><td>{index}</td></tr>"
                "</tbody></table>"
            )
        elif kind == 2:
            atoms.append(
                f'<ol start="{index + 1}"><li>first_{index}</li>'
                f"<li>second_{index}</li></ol>"
            )
        else:
            atoms.append(
                f'<math display="block"><semantics><mi>x_{index}</mi>'
                '<annotation encoding="application/x-tex">'
                f"x_{index}^2</annotation></semantics></math>"
            )
    filler = "".join(
        f"<p>unique filler paragraph {index} with bounded source text {index * 17}</p>"
        for index in range(filler_paragraphs)
    )
    return (
        "<!doctype html><html><head><title>typed batch micro</title></head>"
        f"<body><main>{filler}{''.join(atoms)}</main></body></html>"
    )


def _quantile(values: list[int], probability: float) -> int:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * probability)))
    return ordered[index]


def _timed(call: Callable[[], object]) -> tuple[int, object]:
    start = time.perf_counter_ns()
    result = call()
    return time.perf_counter_ns() - start, result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--atoms", type=int, default=48)
    parser.add_argument("--filler-paragraphs", type=int, default=1_000)
    parser.add_argument("--iterations", type=int, default=9)
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

    html = _html(args.atoms, args.filler_paragraphs)
    document = extract_document_ir_v2(html)
    if document.truncated or not document.source_mapping_complete or document.parse_error_count:
        raise RuntimeError("synthetic document did not produce a complete exact IR")
    atom_ids = _outermost_typed_atom_ids(document)
    if len(atom_ids) != args.atoms:
        raise RuntimeError(f"expected {args.atoms} atoms, found {len(atom_ids)}")

    def legacy() -> tuple[tuple[bytes, str], ...]:
        output: list[tuple[bytes, str]] = []
        for atom_id in atom_ids:
            certificate = create_selection_certificate_v0(document, [atom_id])
            replay = verify_and_replay_selection_certificate_v0(
                document,
                certificate,
            )
            output.append((certificate.encoded, replay.markdown))
        return tuple(output)

    def batch() -> tuple[tuple[bytes, str], ...]:
        created = create_typed_atomic_overlay_batch_v0_native(
            document,
            list(atom_ids),
        )
        verified = verify_typed_atomic_overlay_batch_v0_native(
            document,
            [item.certificate for item in created],
        )
        return tuple((item.certificate, item.markdown) for item in verified)

    expected = legacy()
    actual = batch()
    if actual != expected:
        raise RuntimeError("batch output or certificate bytes differ from legacy replay")
    for _ in range(args.warmups):
        batch()
        legacy()

    legacy_ns: list[int] = []
    batch_ns: list[int] = []
    gc.collect()
    gc.disable()
    try:
        for iteration in range(args.iterations):
            arms = (("legacy", legacy), ("batch", batch))
            if iteration % 2:
                arms = tuple(reversed(arms))
            for name, arm in arms:
                elapsed, observed = _timed(arm)
                if observed != expected:
                    raise RuntimeError(f"{name} output drifted during measurement")
                (legacy_ns if name == "legacy" else batch_ns).append(elapsed)
    finally:
        gc.enable()

    legacy_median = statistics.median(legacy_ns)
    batch_median = statistics.median(batch_ns)
    report = {
        "schema_version": "typed-atomic-overlay-batch-v0.micro.1",
        "claim_scope": "synthetic mechanism benchmark; not a quality/SOTA claim",
        "source_bytes": len(html.encode()),
        "graph_elements": document.element_count,
        "graph_text_runs": document.text_run_count,
        "atoms": len(atom_ids),
        "iterations": args.iterations,
        "warmups": args.warmups,
        "output_and_certificate_bytes_identical": True,
        "legacy": {
            "graph_clones_per_iteration": 2 * len(atom_ids),
            "median_ns": legacy_median,
            "p95_ns": _quantile(legacy_ns, 0.95),
            "atoms_per_second": len(atom_ids) * 1_000_000_000 / legacy_median,
        },
        "batch": {
            "graph_clones_per_iteration": 2,
            "median_ns": batch_median,
            "p95_ns": _quantile(batch_ns, 0.95),
            "atoms_per_second": len(atom_ids) * 1_000_000_000 / batch_median,
        },
        "median_speedup": legacy_median / batch_median,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

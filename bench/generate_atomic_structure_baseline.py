#!/usr/bin/env python3
"""Generate the fixed production baseline in a fresh no-network sandbox.

The claimable entrypoint has no callable, configuration, environment,
concurrency, dataset, evaluator, scorer, or label injection surface. It is a
thin compatibility CLI over :mod:`bench.atomic_claim_protocol`; the worker
always resolves the exact production ``app.services.extractor.extract_content``
callable and records every effective Settings field.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench.atomic_claim_protocol import (  # noqa: E402
    ClaimProtocolError,
    run_baseline,
)
from bench.claimable_io import ClaimableIOError  # noqa: E402
from bench.claimable_sandbox import (  # noqa: E402
    SandboxExecutionError,
    SandboxUnavailableError,
)


def generate_baseline(decision_inputs: Path, output: Path) -> dict[str, object]:
    """Run the only claimable baseline generator with closed production identity."""

    return run_baseline(decision_inputs, output)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decision-inputs", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = generate_baseline(args.decision_inputs, args.output)
    except (
        ClaimProtocolError,
        ClaimableIOError,
        SandboxExecutionError,
        SandboxUnavailableError,
    ) as error:
        print(f"baseline generation error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result["artifact"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

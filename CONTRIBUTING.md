# Contributing

Clusy Crawler welcomes focused code, test, benchmark, and documentation
contributions. Runtime correctness, source grounding, security, and bounded
resource use take precedence over benchmark-only gains.

## Development environment

Requirements:

- Python 3.12 or newer;
- Rust 1.85;
- [`uv`](https://docs.astral.sh/uv/); and
- Docker for image-boundary checks.

```bash
git clone https://github.com/clusy-io/clusy-crawler.git
cd clusy-crawler

uv sync --locked --extra dev
cargo +1.85 fetch --manifest-path native/Cargo.toml --locked
```

Install Chromium when working on rendering:

```bash
uv run playwright install chromium
```

Start the local API. If Chromium is not installed, set both Playwright switches
to `false` for a fully ready static-only process.

```bash
uv run uvicorn app.main:app \
  --host 127.0.0.1 \
  --port 11235 \
  --reload
```

Local mode can use an empty bearer token on a trusted machine. Never expose an
unauthenticated development instance to an untrusted network.

## Required checks

Run every check that covers the components changed by the patch:

```bash
uv run ruff check .
uv run mypy app
uv run python scripts/check_docs.py
uv run pytest -q

cargo +1.85 fmt --manifest-path native/Cargo.toml --check
cargo +1.85 clippy \
  --manifest-path native/Cargo.toml \
  --locked --all-targets -- -D warnings
cargo +1.85 test --manifest-path native/Cargo.toml --locked
```

Packaging or dependency changes must build the affected runtime boundaries:

```bash
docker build --target static-runtime --tag clusy-crawler:static .
docker build --target browser-runtime --tag clusy-crawler:browser .
docker build --target quality-runtime --tag clusy-crawler:quality .
```

The static image must remain free of Playwright and Chromium artifacts.
Browser tests must verify that Chromium runs with a supported sandbox.

Documentation changes must at minimum pass:

```bash
git diff --check
uv run python scripts/check_docs.py
```

Also verify every command, benchmark number, and runtime-status statement
affected by the edit.

## Change gates

| Change | Minimum evidence |
| --- | --- |
| Bug fix | Regression test that fails before the fix |
| Fetch, redirect, DNS, robots, or scope policy | Unit and integration coverage for every affected safety boundary |
| Extraction behavior | Relevant fixed-protocol benchmark, paired before/after evidence, and non-regression checks on other page families |
| Native performance | Counterbalanced retain-all A/B, complete output equivalence, latency distribution, and memory/resource checks |
| Cache semantics | Key/envelope identity, stale-policy, failure fallback, and recursive-policy tests |
| Browser behavior | Static-path non-regression plus sandboxed render, cancellation, and shutdown checks |
| Optional model path | Deterministic fallback, timeout/saturation/malformed-output tests, immutable model identity, and license review |
| Frontier policy | Determinism, bounded state, robots/scope/trap invariants, and delayed-feedback tests |
| Documentation | Relative links, anchors, formatting, commands, facts, and claim boundary |

Use the protocol index in [`bench/README.md`](bench/README.md). Do not choose a
benchmark after inspecting which suite favors the candidate.

## Benchmark integrity

- Pin source, dataset, evaluator, dependencies, configuration, and seeds.
- Keep development labels, final evaluation labels, and runtime inputs in
  separate authorities.
- Report every retained sample and the preregistered aggregation.
- Distinguish extraction-loop throughput from HTTP, rendering, and end-to-end
  service latency.
- Retain negative and null results when they determine promotion.
- Never use Exa, Firecrawl, or another provider's outputs for training,
  distillation, prompts, routing, or runtime extraction. They are permitted
  only in the sealed comparison protocol.
- Never commit credentials, restricted provider response archives, or
  machine-local paths.

Public benchmark gains are diagnostics unless the protocol provides a valid
unseen authority. A SOTA claim requires a named task, comparable systems,
reproducible artifacts, uncertainty where applicable, and the gates in
[`docs/RESEARCH.md`](docs/RESEARCH.md).

## Pull requests

Keep a pull request narrow and state:

1. the user-visible or internal contract changed;
2. the safety and resource boundaries affected;
3. the exact checks run;
4. the benchmark artifact or reason no benchmark applies;
5. known limitations; and
6. deployment impact.

Do not mix a rejected research candidate into a documentation or runtime
change. Default-off research must remain unimported by the API path until its
promotion gate passes.

## Style and dependencies

- Follow Ruff and strict mypy for Python.
- Format Rust with the pinned toolchain and keep Clippy warning-free.
- Prefer deterministic bounded code over hidden retries or unbounded queues.
- Preserve API compatibility unless a breaking change is explicit.
- Add dependencies only after provenance and Apache-2.0 compatibility review.
- Do not add bot-wall evasion, residential proxy rotation, or TLS
  impersonation.

Report vulnerabilities privately through [`SECURITY.md`](SECURITY.md), not a
public issue.

By contributing, you agree that your contribution is licensed under
Apache-2.0.

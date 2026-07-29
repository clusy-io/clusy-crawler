# Clusy Crawler documentation

This directory documents the current open-source runtime, self-hosting
contract, verified evidence, and research gates. Suite-specific protocols and
immutable implementation records live under [`../bench`](../bench/README.md).

## Start here

| Document | Use it for |
| --- | --- |
| [`../README.md`](../README.md) | Product overview, quick start, API, and current evidence |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Runtime boundaries, extraction design, and failure behavior |
| [`SELF_HOSTING.md`](SELF_HOSTING.md) | Compose, image selection, production configuration, and upgrades |
| [`OPERATIONS.md`](OPERATIONS.md) | Health, observability, release verification, rollback, and incidents |
| [`BENCHMARKS.md`](BENCHMARKS.md) | Results, artifacts, scope, and claim boundaries |
| [`RESEARCH.md`](RESEARCH.md) | Advanced architecture under evaluation and its promotion gates |
| [`../SECURITY.md`](../SECURITY.md) | Threat model and vulnerability reporting |
| [`../CONTRIBUTING.md`](../CONTRIBUTING.md) | Development workflow and change gates |

## Status vocabulary

| State | Meaning |
| --- | --- |
| **Runtime** | Present on the current public branch and reachable through the documented API |
| **Verified** | Reproducible evidence passed its declared scope; deployment is not implied |
| **Research** | Default-off or unwired work that has not passed runtime promotion |
| **Rejected** | A measured candidate failed at least one promotion gate and is not shipped |
| **Historical deployment** | A separately operated immutable revision passed its release checks; the current public branch is not implied to be deployed |

“SOTA” is not a synonym for “newest.” A state-of-the-art claim requires a
named task, fixed protocol, comparable systems, reproducible artifacts,
uncertainty where applicable, and every operational gate for that scope.

## Documentation policy

- Runtime behavior is derived from the current public source.
- Benchmark numbers identify their task, corpus, profile, and measurement
  boundary.
- Public-label results are labelled as diagnostics, not blind tests.
- Live-vendor outputs are evaluation-only and never training or distillation
  data.
- Research code is not described as a default runtime path.
- Legal notices, third-party documentation, and immutable evidence records are
  preserved as historical sources.

# Architecture document index

This path is retained for compatibility with earlier links. The architecture
is now split by responsibility:

| Document | Scope |
| --- | --- |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Current open-source runtime and failure behavior |
| [`SELF_HOSTING.md`](SELF_HOSTING.md) | Compose, containers, production configuration, and upgrades |
| [`OPERATIONS.md`](OPERATIONS.md) | Health, release verification, observability, rollback, and incidents |
| [`BENCHMARKS.md`](BENCHMARKS.md) | Verified evidence and claim boundaries |
| [`RESEARCH.md`](RESEARCH.md) | Target architecture and SOTA promotion gates |

The current branch is not described as universally SOTA. A scoped SOTA claim
is valid only after the named protocol and every gate in
[`RESEARCH.md`](RESEARCH.md#promotion-gates) pass.

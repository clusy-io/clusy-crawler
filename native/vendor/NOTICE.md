# Vendored native dependencies

These sources are vendored for reproducible native builds and source-audited
backend changes. The Git dependencies pinned by the independently benchmarked
`article_body` backend are no longer reliably available from their original
remotes. The broad backend is vendored from its immutable crates.io archive so
its exact baseline remains locally inspectable before any reviewed patch.

| Directory | Upstream | Exact source | Declared license |
|---|---|---|---|
| `rs-trafilatura/` | `https://github.com/Murrough-Foley/rs-trafilatura` | `9261e087deca9c7a38ddc284a60dd62a47de7b33` | MIT OR Apache-2.0 |
| `rs-trafilatura-broad/` | crates.io `rs-trafilatura` 0.2.2 (`https://github.com/Murrough-Foley/rs-trafilatura`) | archive SHA-256 `e7349ec610fed6e49c175da51034aca74c02ec3951a7acbbf10eb2ac3ee04e3b`; packaged VCS revision `a20bb0d58ee17f0315d2e5f7d2a7119560aecd06` | MIT OR Apache-2.0 |
| `html-cleaning/` | `https://github.com/Murrough-Foley/html-cleaning` | `ba5f8e95e4dc8a6af1f6742c8f31957a47b2327e` | MIT OR Apache-2.0 |
| `quick_html2md/` | `https://github.com/Murrough-Foley/quick_html2md` | `6260677b3aed7bfc83bc7fae90599120467c4c55` | MIT OR Apache-2.0 |

The article-backend source trees were copied from Cargo's clean checkouts at
the listed revisions. Repository metadata, tests, fixtures, examples,
benchmarks, documentation collections, scripts, lockfiles, and build artifacts
were intentionally excluded from those production vendored trees.

`rs-trafilatura-broad/` entered the repository as a byte-for-byte unpacking of
all 88 files in the published `rs-trafilatura-0.2.2.crate` archive. Its
normalized and original manifests, packaged lockfile, tests, fixtures,
examples, benchmark, VCS provenance, README, changelog, and exact upstream
license files are retained. No file inside that phase-A vendoring baseline was
authored or normalized by Clusy; subsequent reviewed patches must be identified
separately.

No Rust implementation semantics were changed by vendoring. The source-only
modifications in the article-backend trees remove unused CLI binaries and
existing trailing whitespace. The manifest modifications are:

- the workspace `native/Cargo.toml` points both aliased rs-trafilatura
  backends at their distinct vendored paths;
- the workspace lockfile records broad 0.2.2 as a path package while preserving
  its complete, previously pinned transitive dependency graph;
- `rs-trafilatura/Cargo.toml` points its `html-cleaning` and `quick_html2md`
  dependencies at the adjacent vendored paths;
- all three article-backend vendored manifests disable automatic non-library
  targets, and declarations for excluded dev dependencies, binaries, and
  benchmarks were removed. This keeps clean metadata and `--all-targets`
  checks from referring to intentionally omitted files.

`quick_html2md` declared `MIT OR Apache-2.0` but its cached checkout did not
contain license files. Canonical MIT and Apache-2.0 texts are included beside
it; the MIT attribution uses the upstream commit author and project
contributors recorded by Git. Each other package retains its exact upstream
license files.

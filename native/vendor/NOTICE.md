# Vendored native dependencies

These sources are vendored because the Git dependencies pinned by the
independently benchmarked `article_body` backend are no longer reliably
available from their original remotes. Vendoring preserves that exact backend
instead of silently substituting a different crates.io release.

| Directory | Upstream | Exact revision | Declared license |
|---|---|---|---|
| `rs-trafilatura/` | `https://github.com/Murrough-Foley/rs-trafilatura` | `9261e087deca9c7a38ddc284a60dd62a47de7b33` | MIT OR Apache-2.0 |
| `html-cleaning/` | `https://github.com/Murrough-Foley/html-cleaning` | `ba5f8e95e4dc8a6af1f6742c8f31957a47b2327e` | MIT OR Apache-2.0 |
| `quick_html2md/` | `https://github.com/Murrough-Foley/quick_html2md` | `6260677b3aed7bfc83bc7fae90599120467c4c55` | MIT OR Apache-2.0 |

The source trees were copied from Cargo's clean checkouts at those revisions.
Repository metadata, tests, fixtures, examples, benchmarks, documentation
collections, scripts, lockfiles, and build artifacts were intentionally
excluded from the production vendored trees.

No Rust implementation semantics were changed. The source-only modifications
remove unused CLI binaries and existing trailing whitespace. The manifest
modifications are:

- the workspace `native/Cargo.toml` points `rs-trafilatura-article` at the
  vendored path;
- `rs-trafilatura/Cargo.toml` points its `html-cleaning` and `quick_html2md`
  dependencies at the adjacent vendored paths;
- all three vendored manifests disable automatic non-library targets, and
  declarations for excluded dev dependencies, binaries, and benchmarks were
  removed. This keeps clean metadata and `--all-targets` checks from referring
  to intentionally omitted files.

`quick_html2md` declared `MIT OR Apache-2.0` but its cached checkout did not
contain license files. Canonical MIT and Apache-2.0 texts are included beside
it; the MIT attribution uses the upstream commit author and project
contributors recorded by Git. Each other package retains its exact upstream
license files.

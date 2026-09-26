# A query makes no network call

Date: 2026-09-25. Audit item B-43 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in the configuration)

- `lsp_profiles.RUST_ANALYZER_CONFIGURATION` says "no crate downloads triggered by opening a file",
  but rust-analyzer loads a workspace through `cargo metadata`, and the environment set no offline
  flag, so cargo could fetch the registry and crates into `cache/code-tools` while a query waited.
- `GOPLS_ENVIRONMENT_TEMPLATE` pinned the toolchain (`GOTOOLCHAIN=local`) but not the module proxy,
  so gopls's `go list` could download modules while answering.
- The install-time build environment (`go_source_build.build_environment`) needs the network and is
  a separate template; it is unchanged.

## Source

- Cargo, "Configuration", https://doc.rust-lang.org/cargo/reference/config.html (fetched
  2026-09-25), `net.offline`: "If this is `true`, then Cargo will avoid accessing the network, and
  attempt to proceed with locally cached data." Environment: `CARGO_NET_OFFLINE`.
- Go Modules Reference, https://go.dev/ref/mod (fetched 2026-09-25), `GOPROXY`: "`off` indicates
  that no communication should be attempted."

## Decision

- Query-time environments set `CARGO_NET_OFFLINE=true` (rust-analyzer) and `GOPROXY=off` (gopls). A
  dependency not in the local caches is an unresolved import in the answer, not a download.

## Uncertainty

- The managed caches can still grow from builds the servers run locally (`GOCACHE`); that growth
  is not bounded by this change.

## Files

- `scripts/lsp_profiles.py`
- `tests/test_a_query_makes_no_network_call.py`
- `CHANGELOG.md`

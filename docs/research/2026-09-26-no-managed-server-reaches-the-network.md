# No managed server reaches the network

Date: 2026-09-26. Audit 2026-09-26, finding C-8 (B-43 not finished).

## What was found

- tsserver's Automatic Type Acquisition (ATA) was left on. Fact, read from the
  pinned `typescript-language-server` 6.0.0 `lib/cli.mjs`: the server reads
  `disableAutomaticTypingAcquisition` from `initializationOptions` and only then
  passes `--disableAutomaticTypingAcquisition` to tsserver. We did not send it, so
  tsserver could run npm to fetch `@types` packages while answering a query.
- rust-analyzer in the empty offline managed `CARGO_HOME`. Measured 2026-09-26
  with the pinned toolchain: `cargo metadata` on a crate with one registry
  dependency (`serde = "1"`) exits 101, "no matching package named `serde`
  found"; `cargo metadata --no-deps` exits 0. The pinned rust-analyzer binary
  carries the message "`cargo metadata` failed and returning succeeded result with
  `--no-deps`", so it already fell back. The suspicion that the workspace could not
  load was false; what remained was one failing cargo run per load, and
  dependency crates are unresolved in both cases.

## Decision

- TypeScript: send `disableAutomaticTypingAcquisition: true`.
- Rust: set `rust-analyzer.cargo.noDeps = true`, so the failing run is not made.
- Guard: `tests/test_a_query_makes_no_network_call.py` lists, for every profile in
  the registry, what keeps it offline; a profile added to the registry without an
  entry fails the test.

## Sources

- typescript-language-server, docs/configuration.md, fetched 2026-09-26 from
  https://github.com/typescript-language-server/typescript-language-server/blob/master/docs/configuration.md:
  `disableAutomaticTypingAcquisition` (boolean): "Disables tsserver from
  automatically fetching missing type definitions (`@types` packages) for external
  modules."
- rust-analyzer book, configuration, fetched 2026-09-26 from
  https://rust-analyzer.github.io/book/configuration.html:
  `rust-analyzer.cargo.noDeps`, default `false`: "Whether to skip fetching
  dependencies. If set to "true", the analysis is performed entirely offline, and
  Cargo metadata for dependencies is not fetched."

## Files

- `scripts/lsp_profiles.py`
- `tests/test_a_query_makes_no_network_call.py`

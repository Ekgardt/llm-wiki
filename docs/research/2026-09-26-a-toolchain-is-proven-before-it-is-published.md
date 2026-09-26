# A toolchain is proven before it is published

Date: 2026-09-26. Audit 2026-09-26, finding C-9 (gopls/rust toolchains not verified).

## What was wrong

Facts, `scripts/install_language_server.py` at a2965838:

- A fresh install verified every archive by its pinned digest and recorded the
  server's (and runtime's) SHA-256, but ran nothing of the Rust toolchain: an
  unpack that left `cargo` or `rustc` unable to start (a missing shared library,
  a lost execute bit) was published and failed only at the first query.
- `_validated_existing_install` (a re-run over an existing root) re-checked the
  server and runtime digests only. A toolchain file deleted after install passed.
- gopls runs `go list` while it answers and rust-analyzer runs `cargo metadata`
  and `rustc --print sysroot`; those executables are part of what answers.

## Decision

- A profile declares `toolchain_probes`: the executables its server runs, each
  with the one argument that prints its version (`go version`;
  `cargo --version`, `rustc --version`).
- A fresh install runs every probe in the staged tree, with the profile's own
  environment, before it writes the receipt and publishes; a failure discards the
  staging tree. A re-run requires every probed file to be a regular, executable
  file.
- Measured 2026-09-26 against the installed toolchains of this machine: the Rust
  probes took 0.03 s, the Go probe 0.31 s.
- Guard: `tests/test_a_toolchain_is_proven_before_it_is_published.py` requires
  every profile whose launch environment sets `PATH` (it runs a managed tool) to
  declare probes.
- Not done, and why: the receipt still records no digest per toolchain file.
  Its key set is closed (`lsp_identity._MANIFEST_KEYS`); adding one refuses every
  receipt on installed vaults and forces a reinstall. Presence plus a run at
  install is what this change proves.

## Sources

- Cargo book, `cargo version`, fetched 2026-09-26 from
  https://doc.rust-lang.org/cargo/commands/cargo-version.html: "Displays the
  version of Cargo."
- Go command documentation, fetched 2026-09-26 from https://pkg.go.dev/cmd/go:
  "Version prints the build information for Go binary files." (the `go version`
  subcommand).

## Files

- `scripts/lsp_server_profile.py`
- `scripts/lsp_profiles.py`
- `scripts/install_language_server.py`
- `tests/test_a_toolchain_is_proven_before_it_is_published.py`

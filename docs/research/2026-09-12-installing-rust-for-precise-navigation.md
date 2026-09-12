# Installing Rust so rust-analyzer can answer

Date: 2026-09-12. Trigger: the owner asked whether Rust should join Python,
TypeScript and Go on the precise tier, was given the cost, and answered
«ставь rust». This note pins what gets installed and what has to change to
install it, before any of it is written.

Sources, all read on 2026-09-12: the release manifest
(https://static.rust-lang.org/dist/2026-09-03/channel-rust-1.98.1.toml, whose
own SHA-256 matches the published
https://static.rust-lang.org/dist/2026-09-03/channel-rust-1.98.1.toml.sha256 —
`a7c8774a5fd8441c997d94c029776cbc5eb111e9d72ab5d256fa69866644347e`), the
rust-analyzer manual on the binary and on what it needs
(https://rust-analyzer.github.io/book/rust_analyzer_binary.html,
https://rust-analyzer.github.io/book/non_cargo_based_projects.html,
https://rust-analyzer.github.io/book/configuration), and its sysroot behaviour
(https://rust-analyzer.github.io/book/configuration — `cargo.sysroot` defaults
to `discover`, i.e. `rustc --print sysroot`).

## Why a binary is not enough

Unlike gopls, rust-analyzer *is* published as a per-platform binary, as the
component `rust-analyzer-preview` of every Rust release. But a binary alone
answers almost nothing: semantic analysis of any real crate needs

* **`cargo`**, to read the project (`cargo metadata`) — the documented
  alternative is a hand-written `rust-project.json`, which this vault cannot
  require of a repository;
* **`rustc`**, because the sysroot is discovered by running
  `rustc --print sysroot`; and
* **`rust-src`**, because rust-analyzer reads the *source* of `core`, `std`,
  `alloc` and `proc_macro` — the compiled `.rlib`s that `rustc` uses are not
  enough for an IDE.

`rust-std` comes with them so the sysroot is a real sysroot rather than a
directory with sources and no targets.

So the install is five archives of one release, not one.

## The pins

Release **1.98.1** (`dist/2026-09-03`), `.tar.xz` throughout, each archive
pinned by the SHA-256 the manifest publishes for it:

| component | linux-x86_64 size |
|---|---|
| `rust-analyzer-preview` | 9.7 MB |
| `rustc` | 79.7 MB |
| `rust-std` | 30.7 MB |
| `cargo` | 11.7 MB |
| `rust-src` (platform-independent) | 5.7 MB |

137.5 MB compressed for this platform, and no compilation — the whole reason
this is cheaper than Go, where 67 MB of toolchain had to build the server
itself. Five platforms are pinned per component (linux and macOS on x86_64 and
arm64, Windows on x86_64); `rust-src` is one archive for all of them.

## The layout

Every Rust archive is a `rust-installer` bundle: `<archive-root>/<component>/`
holds the tree as it should appear under the install prefix — verified here on
2026-09-12 by listing both `rust-analyzer-1.98.1-x86_64-unknown-linux-gnu`
(`rust-analyzer-preview/bin/rust-analyzer`) and `rust-src-1.98.1`
(`rust-src/lib/rustlib/src/rust/library/…`). So one rule unpacks all five:
drop the first two path components and write the rest under
`cache/code-tools/rust-analyzer/1.98.1/toolchain/`. The result is an ordinary
toolchain: `toolchain/bin/{rust-analyzer,rustc,cargo}` and
`toolchain/lib/rustlib/…` with the standard library and its sources.

`install.sh`, which ships in each bundle, is deliberately not used: it is a
POSIX shell script, and the same install has to work on Windows.

## What has to change

* **Archive format.** The installer opens `r:gz`; these are `.xz`. The mode
  follows the URL's suffix.
* **More than two artifacts.** The installer knows one server archive and one
  runtime sibling. A profile gains a list of **components**, each with its own
  platform table and its own place in the managed root, and the installer
  unpacks each one after the first.
* **Bounds.** `rustc.tar.xz` alone is 79.7 MB compressed; the toolchain
  unpacks to well over a gigabyte. Both ceilings are already per profile
  (gopls raised them); Rust raises them further and the npm pins keep theirs.

Nothing else moves: the profile is native (the seam gopls needed), the launch
is the verified copy from the owner root (the same, for the same reason —
rust-analyzer also re-reads its own executable), installation stays one
explicit operator action, and no query ever triggers it.

## Measured, after it was installed

On this machine, 2026-09-12, against a two-function crate:

* **Install:** 49 s end to end, 137.5 MB downloaded, **701 MB** on disk. No
  compiler runs: the five archives are unpacked and that is the whole install.
* **Answers:** the first request came back `graph_candidate` in 0.5 s — the
  server was still reading the crate, and the structural tier answered instead,
  which is the documented degradation. The next one was `lsp_confirmed` in
  0.10 s, and `references` likewise.

One thing had to be found by running it: `rust-analyzer` is dynamically linked
against `librustc_driver-*.so` and locates it through an RPATH relative to its
own path. The verified copy runs from the owner root, where that relative path
does not exist, so the process died before the handshake (`error while loading
shared libraries`, exit 127). The profile therefore names
`LD_LIBRARY_PATH`/`DYLD_FALLBACK_LIBRARY_PATH` beside `PATH`; Windows finds its
DLLs through `PATH`, which already points at the same toolchain.

## Bounds and cost

One profile, one managed root, no new runtime path, no daemon, no compiler
invocation at install time. Until the install runs, `.rs` answers from
structural evidence exactly as today. `cache/` stays disposable: deleting it
costs a reinstall.

Files: `scripts/lsp_profiles.py`, `scripts/lsp_server_profile.py`,
`scripts/install_language_server.py`, `scripts/lsp_identity.py`,
`docs/CODE-NAVIGATION.md`, `CHANGELOG.md`,
`tests/test_native_language_server.py`, `tests/test_lsp_server_profile.py`,
`tests/test_language_server_wiring.py`.

# Installing Go to build gopls

Date: 2026-09-12. Trigger: the owner read
`docs/research/2026-09-11-precise-navigation-for-go.md`, which ended in a
question — ship a Go toolchain so gopls can be built, or leave Go on the
structural tier — and answered "ставь". This note pins what gets installed
and states what has to change to install it, before any of it is written.

## The pins

| artifact | version | bytes | sha256 |
|---|---|---|---|
| `go1.27.1.linux-amd64.tar.gz` | Go 1.27.1 | 70 553 950 | `63d339f0da5ab53635a56f2490a7984dfe12dfcff22ad749f63edaf590168445` |
| `go1.27.1.linux-arm64.tar.gz` | Go 1.27.1 | 67 009 954 | `3450b45a3f9ee8568792736a5c5e70a1f2e9b36c35a8f74958c03e51d7d92bec` |
| `go1.27.1.darwin-amd64.tar.gz` | Go 1.27.1 | 71 621 873 | `8f8f52c6649542cf027bbc9b9c68d1ec042f9f34808a40413f0b8b3f66f3caa4` |
| `go1.27.1.darwin-arm64.tar.gz` | Go 1.27.1 | 68 100 347 | `ee215d57e0ec269c60cc9ceca68e6bda321ba9ee5afe24f4b0988703c2d87d12` |
| `go1.27.1.windows-amd64.zip` | Go 1.27.1 | 78 931 360 | `a3911b5e0e1b1053f25ed0675f4c1c6aad1e2bfcf253df2b9be4caabd2edd95d` |

Sources, all read on 2026-09-12: the release index and its checksums
(https://go.dev/dl/, https://go.dev/dl/?mode=json), the module proxy's own
answer for the latest gopls (https://proxy.golang.org/golang.org/x/tools/gopls/@latest
and the module's `go.mod` at
https://proxy.golang.org/golang.org/x/tools/gopls/@v/v0.23.0.mod), the
installation instructions that say gopls is built from source
(https://pkg.go.dev/golang.org/x/tools/gopls, https://go.dev/gopls/), and the
checksum-database documentation (https://go.dev/ref/mod#checksum-database).
The linux-amd64 archive was fetched and hashed here: 70 553 950 bytes,
sha256 `63d339f0…8445`, matching the published checksum.

gopls is pinned at **v0.23.0** (published 2026-07-07, `golang.org/x/tools`
commit `014f87ff5c01`, read from `proxy.golang.org` on 2026-09-12). Its own
`go.mod` declares `go 1.26.0`, so 1.27.1 builds it and 1.25 would not. Both
are read from the module proxy with the default checksum database
(`sum.golang.org`) left on, so every module in the build graph is verified
against a transparency log — the same guarantee the npm pins get from their
integrity hashes, applied to a graph instead of a file.

## Why this is not the TypeScript shape

`scripts/install_language_server.py` downloads one tarball, checks its
Subresource Integrity hash and unpacks it. Three of its assumptions do not
survive Go:

* **Size.** `MAX_COMPRESSED_BYTES` is 32 MiB and `MAX_DECOMPRESSED_BYTES` is
  128 MiB. The Go archive is ~67 MB compressed and ~250 MB unpacked. The
  ceilings become per-artifact rather than module-wide, so the npm pins keep
  the bounds they have today and the toolchain gets its own, stated on the
  profile.
* **Format.** Windows ships Go as a `.zip`; the installer reads `tar` only.
* **A second step.** Unpacking the toolchain produces a compiler, not a
  server. The server is produced by running `go install
  golang.org/x/tools/gopls@v0.23.0` with `GOPATH`, `GOMODCACHE`, `GOCACHE`
  and `GOBIN` inside the managed root, so nothing is written outside
  `cache/code-tools/` and no user-level Go state is read or changed
  (`GOFLAGS=-mod=mod`, `GOTOOLCHAIN=local` so the build cannot silently
  fetch a different toolchain).

## Why this is not the Pyright shape either

Every managed profile today is a Node program: `LanguageServerProfile`
requires `node_major`, and `launch_command` builds `node <server> …`. gopls
is a native executable. The profile gains a native launch — `node_major`
becomes optional, and a native profile's argv is the executable itself —
and identity stops probing Node for it. What does not change is the rule
that matters: the executable's SHA-256 is recorded at install time and
checked before every launch, exactly as Pyright's entry file is.

A built binary is not bit-reproducible, so the digest cannot be pinned in
the source. What is pinned is every input: the toolchain archive by digest,
the gopls version by name, and the module graph by the checksum database.
The manifest records the digest that build produced, and that recorded
digest is what gates each launch.

## What the operator sees

Installation stays a separate explicit action, as
`knowledge/notes/read-only-lsp-navigation-engine-decision.md` requires:
`uv run python scripts/install_language_server.py --profile gopls`. It needs
the network and a few minutes of compilation, and it writes about 350 MB
under `cache/code-tools/` (the toolchain, the module cache, the build cache
and one binary). `cache/` is disposable: deleting it costs a reinstall and
nothing else. Until it is installed, Go questions answer from structural
evidence exactly as they do today, and `doctor` names the install command.

## Measured, after it was built

All on this machine, 2026-09-12, against a two-file Go module:

* **Install:** 44 s end to end, of which the download is ~7 s and the
  compilation the rest. 324 MB on disk: the toolchain 282 MB and the server
  42 MB. The first cut left 863 MB, because the module cache (123 MB) and the
  build cache (383 MB) survive a build; both hold gopls's own dependencies and
  nothing reads them again, so the install now removes them — Go writes that
  cache read-only, directories included, so it has to be unlocked top-down
  before it can be removed.
* **Answers:** `mode=definition` 0.48 s cold (server start included), 0.10 s
  warm; `mode=references` 0.10 s warm. Both `lsp_confirmed`, and the session
  reports `query_ready` with definition, references, implementations, hover,
  type definition, call hierarchy and symbols.

Three things had to change beyond the profile, each found by running it:

1. **The interpreter.** Identity probed Node for every profile and the launch
   built `node <server>`; a native profile now validates and launches the
   executable itself.
2. **The descriptor launch.** Pyright is executed through an unlinked
   descriptor, which closes the verify-then-execute window. gopls cannot be:
   it hashes its own executable for its index and re-executes it for
   telemetry, and both fail once the file has no name (`can't hash gopls
   executable: no such file or directory`, exit 1). The verified copy is
   therefore launched by path from the owner root — created for this process
   tree alone — and the launch invariant is stated exactly: a generation may
   name the inherited descriptor or a file inside its own owner root, and
   nothing else.
3. **The environment.** gopls runs `go list` while it answers, so the pinned
   toolchain has to be the only `go` on its `PATH` and its caches have to sit
   inside the managed root. The profile declares those variables and the
   process layer applies them beside its allowlist.

## Bounds

One profile, one new managed root (`cache/code-tools/go/1.27.1/` and
`cache/code-tools/gopls/v0.23.0/`), no new runtime root, no daemon, no
change to the generation format, and no automatic installation. The
compilation runs with a deadline and is killed by it; a failed build leaves
the staging directory and removes it.

The build step is its own module, `scripts/go_source_build.py`, rather than
more branches inside the installer: it is the only place in this product that
runs a compiler, it needs an environment of its own, and the installer's job —
fetch, verify, unpack — stays what it is. It exports `build_environment` and
`build_source_server`, and the installer calls the second one when a profile
declares a `SourceBuild`.

Files: `scripts/go_source_build.py` (new), `scripts/lsp_server_profile.py`,
`scripts/lsp_profiles.py`, `scripts/install_language_server.py`,
`scripts/lsp_identity.py`, `scripts/doctor.py`, `docs/CODE-NAVIGATION.md`,
`CHANGELOG.md`, and their tests.

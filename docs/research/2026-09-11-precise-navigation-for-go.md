# Precise navigation for Go needs a compiler, not a download

Date: 2026-09-11. Trigger: issue #24 asks for "precise navigation beyond
Python: at minimum TypeScript and Go". TypeScript was wired on 2026-08-28
(`docs/research/2026-08-28-precise-navigation-beyond-python.md`,
`scripts/lsp_profiles.py`). This note is about the Go half, and it ends in a
question for the owner rather than in code.

## What Go already answers here

Go is indexed structurally today: the tree-sitter grammar is in the
extractor's table (`scripts/code_extractor.py`, `"go"`), `.go` maps to it
(`scripts/code_languages.py`), and the heuristic definition and import
patterns exist (`scripts/code_graph.py`). So `callers`, `callees`,
`definition`, `references`, `snippet`, `search` and `impact` answer for Go
from structural evidence, with the same confidence labelling as every other
non-Python language. What is missing is the *precise* tier: the LSP path
that resolves a symbol through a type checker instead of a name match.

## Why the same trick as TypeScript does not work

The TypeScript profile downloads two pinned npm tarballs, verifies their
digests and runs `tsserver.js` on the managed Node. There is no equivalent
for Go: the Go team publishes gopls **only as a Go module**. The documented
installation is `go install golang.org/x/tools/gopls@vX.Y.Z`, which compiles
it from source, and building gopls requires a toolchain of Go 1.21 or later
(https://pkg.go.dev/golang.org/x/tools/gopls, https://go.dev/gopls/). The
current line is v0.21 (golang/go#77520, released 2026). Prebuilt gopls
binaries exist only in third-party channels — Snap, Homebrew, distribution
packages — and the Go team recommends against them; none of them is a pinned
artifact with a published digest, which is what `scripts/lsp_identity.py`
requires before a server may be driven.

Taking gopls from `PATH` is also refused by design: the existing identity
rule exists precisely so that a `typescript-language-server` found on `PATH`
cannot be handed the managed bundle. `scripts/code_graph.py` already detects
`gopls` on `PATH` for its tool report, but detection is not a pin.

## What it would cost

To install gopls the way this vault installs Pyright and tsserver, the
product would have to ship a **Go toolchain** as a managed artifact:
`go.dev/dl` publishes per-platform archives with SHA-256 digests, so it is
verifiable, but it is roughly 80 MB compressed and ~250 MB unpacked per
platform, plus a network-bound `go install` that compiles gopls (minutes, and
a module cache of its own under the state root). That is the largest single
artifact the product would own — larger than the model weights — and it adds
a compiler to a memory system.

## Options, with the trade-off stated

1. **Ship the Go toolchain and build gopls at install time.** Precise Go
   navigation, at ~350 MB of managed artifacts and a multi-minute install
   step that needs the network and a compiler.
2. **Accept a third-party prebuilt gopls with a recorded digest.** Much
   smaller, but the digest pins a build nobody in the Go project publishes,
   which contradicts the identity rule the LSP path is built on.
3. **Leave Go on the structural tier and say so.** No new artifact. Go
   questions keep answering from tree-sitter evidence, labelled as
   structural, and issue #24's "at minimum TypeScript and Go" is met for
   TypeScript only.

## Recommendation

Option 3 until the owner asks otherwise, and option 1 when he does. The
operator's repositories here are Python and TypeScript; a compiler installed
for a language nobody in this vault indexes would cost more than it answers,
and rule 4 is about a system that is fast and maintainable, not complete.

This is an architecture decision (a new managed artifact, a new install
step, a new runtime path), so it is the owner's call, not mine.

Files: none changed by this note. Relevant:
`scripts/lsp_profiles.py`, `scripts/lsp_identity.py`,
`scripts/install_language_server.py`, `docs/CODE-NAVIGATION.md`.

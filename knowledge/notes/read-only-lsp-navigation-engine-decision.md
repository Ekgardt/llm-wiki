---
type: decision
title: "Read-Only LSP Navigation Engine"
description: "LLM Wiki will keep its structural Evidence Graph and add an owned read-only LSP engine for precise live navigation, starting with production-quality Python support."
date: 2026-07-22
confidence: high
source_authority: user
status: active
---
# Read-Only LSP Navigation Engine

One-sentence summary: LLM Wiki will keep its structural Evidence Graph and add an owned read-only LSP engine for precise live navigation, starting with production-quality Python support.

## Decision

Date: 2026-07-22.

LLM Wiki will not introduce Aether, a Serena runtime dependency, a Rust rewrite,
another graph, or a mandatory SCIP pipeline for interactive navigation. It will
implement a small Python 3.10-compatible, read-only LSP runtime owned by LLM Wiki.
The LSP 3.18 specification is the contract; SolidLSP and multilspy are reference
implementations and behavioral comparison oracles, not runtime dependencies.
The runtime is bounded by the owning MCP process and is not a persistent daemon.

The existing Evidence Graph and Tree-sitter extraction remain the fast structural
layer. The new runtime provides precise live definitions, references, type
information, diagnostics, and capability-gated implementations and call hierarchy.
It does not publish query results into the active generation. Exact small results
use a compact deterministic renderer; the existing Context Compiler remains the
packer for broad, multi-source answers.

Python through a pinned Pyright server is the first production slice. Additional
language families are added only after Python passes correctness, freshness,
process-recovery, cross-platform, latency, and token gates.

## Rationale

The prior one-shot SCIP/publication design was reproducible but placed consent,
sealed capture, analyzer jobs, normalization, publication, recovery, and Graph v3
admission in the interactive path. A dated review on 2026-07-22 found that Serena
v1.6.1/SolidLSP had the strongest tested lifecycle behavior but no standalone public
API, while Microsoft multilspy v0.1.0 lacked the required manager and cross-platform
qualification. Depending on Serena's internal API would trade implementation effort
for a product boundary without an independent compatibility contract.

Owning a minimal read-only runtime keeps the maintenance surface bounded while
reusing language-server semantics rather than reimplementing compilers. Keeping
structural graph queries and precise live queries separate preserves fast broad
navigation, honest provenance, graceful degradation, and token-efficient progressive
disclosure.

## Supersedes

This decision supersedes [[persistent-code-intelligence-kernel-decision]] after
completed Tasks 1-5 of its implementation plan. Those completed foundations remain
valid; the one-shot consent/SCIP/publication direction in Tasks 6-16 does not.

## Consequences

- Tasks 6-16 are superseded and must not be executed.
- No Serena or SolidLSP runtime package becomes part of LLM Wiki.
- No new MCP tool, graph store, active pointer, runtime root, or daemon is added.
- The managed Pyright installation lives under
  `cache/code-tools/pyright/1.1.411/`; process-owned scratch lives under
  `run/lsp/<owner-nonce>/` and follows the existing `run/` deletion contract.
- The existing code graph remains available when Pyright is missing, not ready, or
  failed.
- The first release is Python-only at the precise semantic tier; existing 12-language
  structural support remains.
- Market superiority is measured after a production slice exists and is not claimed
  from architecture alone.
- The old plan remains historical and a new plan replaces Tasks 6-16.

## Source / Evidence

- Explicit user approval, 2026-07-22 OpenCode session.
- `docs/superpowers/specs/2026-07-22-read-only-lsp-navigation-design.md`
- LSP 3.18: https://microsoft.github.io/language-server-protocol/specifications/lsp/3.18/specification/
- Serena v1.6.1: https://github.com/oraios/serena/releases/tag/v1.6.1
- Microsoft multilspy: https://github.com/microsoft/multilspy
- Pyright: https://github.com/microsoft/pyright

## Related

- [[persistent-code-intelligence-kernel-decision]]
- [[solo-operator-superset-product-decision]]
- [[derived-evidence-generation-decision]]

---
title: Three Conventions, One Root (2026-04-13)
type: connection
---

# Three Conventions, One Root — 2026-04-13 Memory Review

One-sentence summary: [[Editorial Notes Pattern]], [[Preliminary Flagging]], and [[Pipeline Mirroring]] all emerged from the same memory-review session and are three orthogonal operationalizations of a single CLAUDE.md rule — rule 6, *mark uncertainty explicitly*.

## The connection
These three concepts were surfaced in one pass of [[Memory Subsystem Action Plan]]. On the surface they look unrelated:

| Convention | What it governs |
|---|---|
| [[Editorial Notes Pattern]] | Pages that are vault metadata, not source-derived content |
| [[Preliminary Flagging]] | Pages written ahead of their raw source |
| [[Pipeline Mirroring]] | Structural reuse when introducing new knowledge layers |

But each one is an answer to the same underlying question — *how do we keep provenance honest while still shipping useful pages?* — from a different angle:

- **Editorial notes** admit: *this page has no `Source:` and that is correct; it is editorial.*
- **Preliminary flagging** admits: *this page should have a `Source:` but does not yet; trust it accordingly.*
- **Pipeline mirroring** admits: *new subsystems inherit the provenance machinery of the original pipeline rather than reinventing it.*

All three short-circuit the same failure mode: silently smuggling unsourced or under-sourced claims past CLAUDE.md rule 6.

## Why this matters
When adding a new vault convention, the first question to ask is: *which uncertainty does it surface, and is the answer already covered by one of these three?* If yes, extend the existing convention rather than introducing a fourth.

## Source
- Origin: `memory/daily/2026-04-13.md` — the session in which all three were named and promoted.
- See `wiki/log.md` entries for 2026-04-13 documenting each promotion individually.

This page is a **connection** — a cross-concept observation that is not itself a concept, decision, or pattern, but is worth recording because it changes how future conventions are proposed.

## Related
- [[Editorial Notes Pattern]]
- [[Preliminary Flagging]]
- [[Pipeline Mirroring]]
- [[Memory Subsystem Action Plan]]
- [[memory/knowledge/concepts/provenance-rule-6]]

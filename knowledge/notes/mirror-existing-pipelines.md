---
type: pattern
title: "Mirror Existing Pipelines"
description: "When introducing a new subsystem, mirror the structure of an already-working pipeline in the same repo rather than inventing new shape."
timestamp: 2026-07-03T05:41:37
---
# Mirror Existing Pipelines

One-sentence summary: When introducing a new subsystem, mirror the structure of an already-working pipeline in the same repo rather than inventing new shape.

## Lesson
This vault's core pipeline is `raw/ → inbox/ → knowledge/notes/` (three layers: immutable raw → staging → compiled). The session-memory subsystem mirrors the compile discipline and the index+log pair, but with **two layers only**: `knowledge/daily/` (immutable raw capture) → `knowledge/notes/` (compiled). There is no staging/inbox equivalent in `memory/`. Reusing shape keeps conventions transferable (provenance rules, editorial notes, registration in index+log) and reduces cognitive overhead.

**Correction note (2026-04-19):** Earlier versions of this page claimed memory had "the same immutable-raw / staged / compiled three-layer shape" as the core pipeline. That was inaccurate. Memory has two layers, not three. Both `knowledge/notes/Pipeline Mirroring.md` and `knowledge/notes/Memory Subsystem Action Plan.md` correctly describe only two layers.

Apply when: adding any new durable layer to this vault. First ask *"which existing layer does this resemble?"* before defining its structure.

## When NOT to apply
- Scratch workspaces (`outputs/`, temp caches) — these don't accumulate durable content and don't need the three-layer shape.
- External tooling directories (`scripts/`, `skills/`) — these are implementation, not knowledge.
- Ephemeral state files (`state.json`, `dedupe.json`) — machine-readable, not reader-facing.

Mirror only when the new layer is meant to accumulate durable, human-readable content *over time* and benefit from registration-in-index + append-only-log discipline.

## Failure mode this prevents
The alternative — inventing a new shape per subsystem — produces vaults where `memory/` uses `entries.jsonl`, `tasks/` uses `open.md` + `closed.md`, and `notes/` uses flat `.md` files. Each carries its own compile logic, its own lint rules, its own index format. A year in, the repo has three half-maintained conventions and the cost of adding a fourth subsystem has grown non-linearly. Mirroring keeps that cost flat.

## Evidence
- `knowledge/daily/2026-04-13.md` [01:04:32] audit that framed `memory/` this way.
- Codified in `knowledge/notes/Memory Subsystem Action Plan.md` ("Intended model" section).

## Promoted to wiki
Promoted 2026-04-13 to [[knowledge/notes/pipeline-mirroring|Pipeline Mirroring]] (`knowledge/notes/`) as a named vault convention. This memory page stays as the imperative pattern with the "apply when" heuristic; the wiki page is the public-facing definition.

## Related
- [[knowledge/notes/pipeline-mirroring|Pipeline Mirroring]] — wiki concept counterpart.
- [[knowledge/notes/pipeline-mirroring]] — noun-form memory concept.
- [[Karpathy LLM Wiki Workflow]]
- [[Memory Subsystem Action Plan]]

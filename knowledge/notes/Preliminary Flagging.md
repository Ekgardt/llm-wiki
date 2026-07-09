---
type: concept
title: "Preliminary Flagging"
description: "A convention for writing wiki pages whose content is inferred from operating instructions rather than grounded in a captured `raw/` or `inbox/` source — include the content, but mark it **preliminary*"
timestamp: 2026-07-03T05:41:37
---
# Preliminary Flagging

One-sentence summary: A convention for writing wiki pages whose content is inferred from operating instructions rather than grounded in a captured `raw/` or `inbox/` source — include the content, but mark it **preliminary** and retire the flag once a source arrives.

## What it is
A named practice in this vault: when a wiki page (or section within one) cannot yet cite a real source, the author writes it anyway, but adds a visible callout identifying the section as *preliminary — inferred from [basis]*. The `Source:` line cites the inference basis (e.g. `CLAUDE.md`, an operating doc, a design note) rather than a `raw/` file.

Operationally this means three things:
1. A reader can tell at a glance which parts of a page are source-grounded and which are working inference.
2. The page is still useful now, instead of blocked on acquiring a source.
3. When a raw source later arrives in `inbox/`, the flag is removed and `Source:` is updated to point at it — the page graduates from preliminary to grounded.

## Why it exists
`CLAUDE.md` rule 6 requires explicit uncertainty marking. Without a convention, authors face three bad options: omit useful content, silently promote inference to fact, or stall pages indefinitely waiting for sources. Preliminary flagging operationalizes rule 6 for the common "no source yet, but we still need the page" case.

## When to apply
- Comparison pages where some compared items lack captured sources (see [[Wiki vs Memory Compiler vs Fusion]] — the canonical example).
- Synthesis pages that draw partly on operating instructions rather than `raw/` material.
- Any section that would otherwise need to be cut for lack of a source, but is genuinely useful working knowledge.

## When *not* to apply
- Editorial metadata pages (`index`, `log`, `Vault Home`) — those use the `## Editorial note` convention instead, which signals vault metadata rather than uncertain content.
- Pages where the "inferred" content is actually a design decision made in this repo — record it as a decision in `knowledge/notes/decisions/`, not as a preliminary wiki claim.

## Source
- Origin: session memory — [[knowledge/notes/flag-inferred-content-as-preliminary]] (decision dated 2026-04-13).
- Grounded in: `CLAUDE.md` rule 6 ("mark uncertainty explicitly").

This page is a **promotion from session memory** rather than a compilation of external source material. It was lifted here because the convention applies to any author of this vault, not only to the session in which it was decided.

## Related
- [[knowledge/notes/provenance-rule-6]] — the underlying CLAUDE.md rule this convention operationalizes.
- [[knowledge/notes/flag-inferred-content-as-preliminary]] — the dated decision that established this convention.
- [[Wiki vs Memory Compiler vs Fusion]] — the comparison page that first applied the flag.
- [[Review Workflow]] — flags should be reconsidered during review cadence.
- [[Ingestion Workflow]] — when a `raw/` source arrives, ingestion retires the flag.
- [[knowledge/notes/editorial-notes-pattern|Editorial Notes Pattern]] — sibling convention; the two partition the "no `Source:` line" space (editorial vs uncertain).
- [[knowledge/notes/pipeline-mirroring|Pipeline Mirroring]] — sibling convention surfaced by the same 2026-04-13 session.
- [[2026-04-13 Three Conventions One Root|Three Conventions, One Root]] — connection page placing this convention against the two siblings.
- [[Memory Subsystem Action Plan]] — audit that first applied this flag to memory-compiler content.
- [[Global Multi-Project Migration Plan]] — applies this flag to any inferred claim in per-project `state.md`.
- [[Product Requirements in the AI Era]] — applies the preliminary flag to unverified 2026 source claims (PRO dating, ~95% ROI stat, expert quotes) until primary sources are captured into `raw/`.

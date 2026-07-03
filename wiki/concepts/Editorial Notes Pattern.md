---
title: Editorial Notes Pattern
type: concept
---

# Editorial Notes Pattern

One-sentence summary: A vault convention — pages that serve as navigation, front doors, or changelogs carry a `## Editorial note` footer marking them as **vault metadata** rather than content derived from a `raw/` source.

## What it is
An "editorial note" is a short footer section appended to pages whose purpose is to organize the vault itself — indexes, logs, and front doors — rather than to record external knowledge. The note tells a reader: *this page is not source-derived; it is maintained editorially*.

Pages currently carrying the note in this vault:
- [[index]] — wiki navigation map
- [[log]] — wiki changelog
- [[Vault Home]] — human-facing front door
- `memory/index.md` and `memory/log.md` — mirror of the same convention on the session-memory side.

## Why it matters
Without the marker, a reader applying CLAUDE.md rule 5 ("preserve provenance") would expect every claim on the page to trace back to a `raw/` file. Indexes and logs can't satisfy that — they *are* the vault's editorial layer. The note short-circuits the confusion: readers know not to look for `Source:` lines, and authors know these pages get updated alongside structure changes rather than during ingestion.

## How to recognize it
- Page sits at the top of a directory (`index.md`, `log.md`, `Vault Home.md`) or serves as a changelog.
- Carries a `## Editorial note` section, usually the last section.
- Typically has no `Source:` line, because no raw source exists.

## Source
- Origin: session memory — [[memory/knowledge/concepts/editorial-notes-pattern]]. Distilled from `memory/daily/2026-04-13.md` [00:57:04], where the convention was articulated during a duplicate-match Edit incident on `log.md`.
- Companion to [[Preliminary Flagging]] and [[Pipeline Mirroring]] — all three are vault conventions surfaced by the 2026-04-13 memory-review session.

This page is a **promotion from session memory**, not a compilation of external source material. It was lifted here because the convention applies to any author extending the vault.

## Related
- [[Preliminary Flagging]]
- [[Pipeline Mirroring]]
- [[Review Workflow]]
- [[memory/knowledge/concepts/editorial-notes-pattern]]
- [[2026-04-13 Three Conventions One Root|Three Conventions, One Root]] — connection page placing this convention against the two siblings.

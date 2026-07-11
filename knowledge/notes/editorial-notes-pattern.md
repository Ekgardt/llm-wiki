---
type: concept
title: "Editorial Notes Pattern"
description: "The `## Editorial note` footer marks a page as **vault metadata** (an editorially maintained navigation or changelog artifact) rather than content derived from a `knowledge/raw/` source."
timestamp: 2026-07-03T05:41:37
confidence: medium
source_authority: user
---
# Editorial Notes Pattern

One-sentence summary: The `## Editorial note` footer marks a page as **vault metadata** (an editorially maintained navigation or changelog artifact) rather than content derived from a `knowledge/raw/` source.

## Definition
An "editorial note" is a short footer section appended to pages whose purpose is to organize the vault itself — indexes, logs, and front doors — rather than to record external knowledge. The note explicitly tells a reader: *this page is not source-derived; it is maintained by hand or by compile scripts*.

Pages currently carrying the note: [[index]] (wiki navigation map), [[log]] (wiki changelog), [[knowledge/index|Knowledge index]] (human front door). These were extended to carry editorial notes on 2026-04-18.

## Why it exists
Without the marker, a reader applying CLAUDE.md rule 5 ("preserve provenance") would expect every claim on the page to trace to a `knowledge/raw/` file. Indexes and logs can't satisfy that — they *are* the vault's editorial layer. The note short-circuits the confusion: readers know not to look for `Source:` lines, and authors know these pages get updated alongside structure changes rather than during ingestion.

## How to recognize it
- Page sits at the top of a directory (`index.md`, `log.md`, `knowledge/index.md`) or serves as a changelog.
- Contains a `## Editorial note` section (usually the last section) stating the page is vault metadata.
- Typically has no `Source:` line, because no raw source exists.

## Evidence
- `knowledge/daily/2026-04-13.md` [00:57:04] — convention applied while fixing a duplicate-match Edit error; the `## Editorial note` string repeated across index/log/knowledge-index.
- Codified in `knowledge/log.md` entry dated 2026-04-13 ("Added `## Editorial note` to [[index]], [[knowledge/index|Knowledge index]], and [[log]]").

## Promoted to wiki
Canonical page for this convention (Title Case wiki copy removed in three-zone cleanup).

## Related
- [[knowledge/notes/provenance-rule-6]] — counterpart for source-derived pages.
- [[knowledge/notes/edit-multiple-matches]] — the incident that surfaced this pattern concretely.
- [[2026-04-13 Three Conventions One Root|Three Conventions, One Root]] — connection page placing this convention alongside its siblings.
- [[editorial-disclaimer-over-history-rewrite]] — the editorial disclaimer is a specialized application of the editorial-notes pattern.
- [[Preliminary Flagging]] — preliminary flagging and editorial notes both operationalize provenance rule 6.
- [[state-md-exempt-from-lint]] — the lint exemption for state.md is justified by the editorial-notes pattern.

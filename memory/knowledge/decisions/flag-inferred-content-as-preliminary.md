---
type: decision
title: "Flag Inferred Content As Preliminary"
description: "When writing a wiki page about a topic that has no corresponding `raw/` or `inbox/` source, mark the inferred sections as **preliminary** rather than omitting them or presenting them as settled."
timestamp: 2026-07-03T05:41:37
---
# Flag Inferred Content As Preliminary

One-sentence summary: When writing a wiki page about a topic that has no corresponding `raw/` or `inbox/` source, mark the inferred sections as **preliminary** rather than omitting them or presenting them as settled.

## Decision
Date: 2026-04-13.

Chose: include the content, but add a visible callout/flag identifying it as inferred from project operating instructions (CLAUDE.md, auto-memory docs) rather than a captured source. Cite the inference basis in the `Source:` line.

Rejected: (a) omit the section entirely — loses useful working knowledge; (b) include without flag — silently promotes inference to fact, violating CLAUDE.md rule 6 ("mark uncertainty explicitly"); (c) block on acquiring a raw source — stalls useful work indefinitely.

Why: CLAUDE.md rule 6 requires explicit uncertainty marking. This decision operationalizes it for the common case of "no raw source yet, but we still need the page."

Follow-up: when a raw source later arrives in `inbox/`, the flag is removed and `Source:` updated.

## Evidence
- `memory/daily/2026-04-13.md` [00:57:04] — applied to [[Wiki vs Memory Compiler vs Fusion]]; memory-compiler and fusion sections flagged preliminary.

## Promoted to wiki
Promoted 2026-04-13 to [[Preliminary Flagging]] (`wiki/concepts/`) as a named vault convention. The memory page is kept as the dated decision record; the wiki page is the reusable convention definition.

## Related
- [[Preliminary Flagging]]
- [[Wiki vs Memory Compiler vs Fusion]]
- [[memory/knowledge/concepts/provenance-rule-6]] — the underlying CLAUDE.md rule this decision operationalizes.

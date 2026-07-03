---
type: concept
title: "Provenance Rule 6: Mark Uncertainty Explicitly"
description: "CLAUDE.md rule 6 — 'mark uncertainty explicitly' — is the root constraint that justifies preliminary flagging, editorial notes, and every 'inferred from…' caveat in this vault."
timestamp: 2026-07-03T05:41:37
---
# Provenance Rule 6: Mark Uncertainty Explicitly

One-sentence summary: CLAUDE.md rule 6 — "mark uncertainty explicitly" — is the root constraint that justifies preliminary flagging, editorial notes, and every "inferred from…" caveat in this vault.

## Definition
Rule 6 of the LLM Wiki Project Contract (`CLAUDE.md`) requires that any claim whose grounding is weak, inferred, or contested must say so on the page. It is the provenance backbone of the vault: without it, readers cannot distinguish captured fact from working inference, and the wiki silently drifts from knowledge base into plausible-sounding prose.

## Operationalizations
Rule 6 is abstract. Several concrete conventions implement it:
- **Preliminary flagging** — sections inferred from operating docs are marked *preliminary* until a `raw/` source arrives. See [[Preliminary Flagging]] (wiki concept).
- **Editorial notes** — vault metadata pages disclaim source-derivation via `## Editorial note`. See [[memory/knowledge/concepts/editorial-notes-pattern]].
- **Contradiction tracking** — per CLAUDE.md rule 7, superseded claims are kept with a pointer to the newer version rather than silently rewritten.
- **`Source:` lines** — per rule 5, factual claims cite the source file; absence of a source is itself a signal that rule 6 applies.

## Why treat this as a concept
Rule 6 is cited in multiple session events as the justification for convention choices, not just as a one-off style rule. Naming it as a concept in memory gives those citations a stable target to link to, so future sessions don't re-derive the same justification from scratch.

## Evidence
- `CLAUDE.md` rule 6: "Mark uncertainty explicitly."
- `memory/daily/2026-04-13.md` [00:57:04] — rule 6 invoked when deciding how to handle memory-compiler content that had no raw source.
- `memory/knowledge/decisions/flag-inferred-content-as-preliminary.md` — decision page explicitly grounded in rule 6.

## Related
- [[Preliminary Flagging]] (wiki) — the public-facing convention derived from this rule.
- [[memory/knowledge/decisions/flag-inferred-content-as-preliminary]] — dated decision applying the rule.
- [[memory/knowledge/concepts/editorial-notes-pattern]] — a different operationalization of the same rule.
- [[memory/knowledge/qa/inbox-vs-raw-after-compile]] — settled Q that invokes this rule to justify its answer.

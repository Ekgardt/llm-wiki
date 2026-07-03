---
type: pattern
title: "Editorial Disclaimer Over History Rewrite"
description: "When a changelog's historical entries contradict current code or decisions, add an explicit editorial disclaimer paragraph naming the superseded items and the precedence rule rather than rewriting or "
timestamp: 2026-07-03T05:41:37
---
# Editorial Disclaimer Over History Rewrite

One-sentence summary: When a changelog's historical entries contradict current code or decisions, add an explicit editorial disclaimer paragraph naming the superseded items and the precedence rule rather than rewriting or deleting the original entries.

## Lesson

Changelogs (`wiki/log.md`, `memory/log.md`) are append-only historical records. When implementation evolves and early entries become inaccurate, the temptation is to edit or delete them. Do not: readers rely on the log to understand how the vault arrived at its current state, and rewrites destroy that audit trail.

Instead, add a prose paragraph or callout *after* the affected entries (or in the `## Editorial note` footer) that:
1. Names the specific superseded patterns by entry date or feature.
2. States the precedence rule explicitly: "code / current state is the source of truth; see the later entry or current file for authoritative behavior."
3. Preserves every original entry verbatim.

**Canonical example (2026-04-19):** `wiki/log.md` contained multiple early entries describing "auto-commit yes / auto-push no" behavior that was never implemented. Rather than rewriting those entries, an editorial disclaimer was added to the `## Editorial note` footer:

> "Historical entries may describe behavior that has since been superseded. Examples that remain in this log by design: early 'auto-commit yes / auto-push no' decisions (superseded — see latest entries, commits are manual), references to the pre-centralization `memory/state/state.json` path, and an earlier hardcoded `$LLM_WIKI_STATE_ROOT/qmd\index.sqlite` (superseded by env-derived resolution)."

## When to apply
- Any append-only changelog where a later decision or implementation contradicts an earlier entry.
- `wiki/log.md`, `memory/log.md`, or any `## Changelog` section in a long-lived page.
- When an audit pass flags docs–code drift across multiple historical entries.

## When NOT to apply
- Non-changelog wiki pages (e.g., `wiki/syntheses/`, `wiki/concepts/`) — those should be updated in-place with a dated correction note, since they are not append-only records.
- `memory/knowledge/decisions/` pages — supersede by creating a new decision page that references the old one.
- A single isolated entry that is simply factually wrong with no historical significance — in that case a quiet correction in-place is acceptable.

## Evidence
- `memory/daily/2026-04-19.md` [23:13:01] — audit v7 session; pattern applied to `wiki/log.md` editorial note covering auto-commit drift, hardcoded path references, and qmd path resolution supersession.

## Related
- [[memory/knowledge/patterns/audit-current-vs-intended]] — audit pages use the same "dated fact vs aspiration" discipline.
- [[memory/knowledge/concepts/editorial-notes-pattern]] — the `## Editorial note` footer is the primary vehicle for these disclaimers in logs.
- [[memory/knowledge/debugging/prospective-memory-page-drift]] — the complementary debugging entry for pages written speculatively that go stale.
- [[memory/knowledge/concepts/provenance-rule-6]] — root constraint: code is source of truth; docs are readable explanations of intent, not authoritative specification.

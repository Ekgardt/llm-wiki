---
type: debugging
title: "Prospective Memory Page Drift"
description: "Memory and wiki pages written speculatively during planning go stale after implementation completes, producing false descriptions of vault behavior that no lint check will catch."
timestamp: 2026-07-03T05:41:37
confidence: medium
source_authority: user
---
# Prospective Memory Page Drift

One-sentence summary: Memory and wiki pages written speculatively during planning go stale after implementation completes, producing false descriptions of vault behavior that no lint check will catch.

## Symptom / Cause / Resolution

**Symptom:** A memory or wiki page uses future tense or conditional phrasing — "once extended", "when Phase N completes", "will carry", "after this is done" — for something that has already been completed or changed. The page appears to describe current state but actually describes a planned future that is now the past.

**Cause:** Pages written during planning or mid-implementation are frequently not revisited after the described work finishes. The wiki or memory page and the actual implementation diverge silently; there is no compile-time check, lint rule, or diff that catches semantic drift between intended and actual state.

**Three concrete instances surfaced 2026-04-19:**
1. `knowledge/notes/concepts/editorial-notes-pattern.md` used future-ish language for `knowledge/index.md` and `knowledge/log.md` editorial notes — both had been added on 2026-04-18.
2. `knowledge/notes/patterns/mirror-existing-pipelines.md` described the memory subsystem as having "the same immutable-raw / staged / compiled three-layer shape" as the core pipeline. Memory only has two layers (`daily/` + `knowledge/`); no staging equivalent exists. The wiki counterpart (`knowledge/notes/Pipeline Mirroring.md`) was already correct.
3. `knowledge/notes/Global Multi-Project Migration Plan.md` Phase 2 Step 3 listed five skills to copy to user level. Only one (`knowledge-lookup`) was actually installed; the other four were intentionally kept project-level after scope reduction mid-execution. The step text was never updated after the decision.

**Resolution:**
1. During any review pass, grep for prospective markers: `once`, `will be`, `when X is done`, `after Phase`, `planned`, `TODO`.
2. Cross-check each hit against actual vault state (read the referenced file; run the described behavior).
3. **For non-append-only pages** (syntheses, concept pages, memory knowledge pages): update in-place with a dated correction note.
4. **For append-only changelogs**: add an editorial disclaimer paragraph (see [[knowledge/notes/editorial-disclaimer-over-history-rewrite]]).
5. When the wiki page is ahead of the memory page, note the discrepancy: "Memory page updated YYYY-MM-DD; see the corresponding wiki page for current authoritative description."

## Evidence
- `knowledge/daily/2026-04-19.md` [23:13:01] — review session surfaced all three instances above; fixes applied in the same session.

## Related
- [[knowledge/notes/audit-current-vs-intended]] — the audit-page discipline that distinguishes present-state facts (dated) from future aspirations.
- [[knowledge/notes/editorial-disclaimer-over-history-rewrite]] — the fix pattern for changelog entries that describe superseded behavior.
- [[knowledge/notes/provenance-rule-6]] — marking uncertainty explicitly is the root constraint; stale prospective claims violate it by presenting old intent as current fact.

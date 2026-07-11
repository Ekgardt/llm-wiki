---
type: pattern
title: "Audit Syntheses: Separate Current State From Intended Model"
description: "An audit page must distinguish 'what is true today (dated)' from 'what we want it to become', so later readers can tell fact from aspiration."
timestamp: 2026-07-03T05:41:37
confidence: medium
source_authority: user
---
# Audit Syntheses: Separate Current State From Intended Model

One-sentence summary: An audit page must distinguish "what is true today (dated)" from "what we want it to become", so later readers can tell fact from aspiration.

## Lesson
When producing an audit-style synthesis, use two clearly labeled sections:
- **Current state** — dated, describes what was actually observed at audit time.
- **Intended model** — describes the target shape; explicitly marked aspirational.

Without this split, aspirational claims calcify into perceived facts after a few weeks, and readers can't tell whether a gap was already closed.

Apply when: writing any `knowledge/notes/` page that evaluates the state of a subsystem or proposes change.

## Why dating the "current state" matters
An audit without a date is indistinguishable from a design document. A month later, when the subsystem has evolved, a reader lands on the audit and cannot tell whether the described gaps are still real or already closed. The dated header turns the audit into a time-stamped observation — future edits either append a newer "Current state (YYYY-MM-DD)" block or move the page into `knowledge/raw/` as a historical artifact. Never silently rewrite an old current-state section: that erases the trail and breaks the CLAUDE.md rule 7 expectation that superseded claims stay visible.

## Checklist
- [ ] Does every current-state bullet carry the audit date in its section header?
- [ ] Is every intended-model bullet phrased as a target, not a fact?
- [ ] If an action plan closes a gap, does it update *both* the intended-model statement *and* add a "Done YYYY-MM-DD" inline annotation (rather than deleting the gap)?

## Evidence
- `knowledge/daily/2026-04-13.md` [01:04:32] — applied when creating [[Memory Subsystem Action Plan]].

## Related
- [[Memory Subsystem Action Plan]]
- [[Review Workflow]]
- [[knowledge/notes/add-reciprocal-backlinks-at-creation]] — complementary: reciprocal backlinks make cross-page relationships explicit, audits catch drift between intent and reality.
- [[editorial-disclaimer-over-history-rewrite]] — the editorial disclaimer pattern is an application of the audit-current-vs-intended principle.
- [[prospective-memory-page-drift]] — prospective drift is an instance where the audit pattern reveals fact-vs-aspiration gaps.

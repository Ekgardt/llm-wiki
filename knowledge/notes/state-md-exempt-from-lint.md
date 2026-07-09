---
type: decision
title: "state.md Exempt from Lint"
description: "`state.md` files under `knowledge/projects/<slug>/` are added to `EDITORIAL_NAMES` in `lint_memory.py` and exempted from backlink-obligation and sparse-floor checks, for the same reason that `index.md` and"
timestamp: 2026-07-03T05:41:37
---
# state.md Exempt from Lint

One-sentence summary: `state.md` files under `knowledge/projects/<slug>/` are added to `EDITORIAL_NAMES` in `lint_memory.py` and exempted from backlink-obligation and sparse-floor checks, for the same reason that `index.md` and `log.md` are exempt.

## Decision

**Date:** 2026-04-19

**Context:** Phase 1 of the Global Multi-Project Migration Plan introduced `knowledge/projects/<slug>/state.md` as a "where we left off" page auto-updated by the SessionStart/SessionEnd hook system. Lint immediately flagged these pages for missing backlinks (they reference many pages but receive few) and risked flagging them as sparse (a fresh state.md from the template is under 200 words).

**Choice:** Add `state.md` to `lint_memory.py::EDITORIAL_NAMES`. This exempts per-project state pages from:
- Backlink obligation (no page needs to point back at a project's state.md)
- Sparse-floor check (template-seeded pages are intentionally minimal at first)
- Any future editorial-metadata checks

**Rationale:** Per-project state pages are vault metadata — auto-maintained operational records, not content derived from `raw/` or `inbox/`. They belong in the same exempt category as `index.md` (navigation map), `log.md` (changelog), and `Vault Home.md` (front door). Applying structural content rules to them would generate constant false-positive noise.

**Alternatives rejected:**
- Per-slug exemption: too granular, requires updating lint whenever a new project is added.
- Sparse-floor override only: still leaves backlink noise; the editorial classification is more correct anyway.

## Evidence
- `knowledge/daily/2026-04-19.md` [03:00:46] — decision recorded during Phase 1 execution

## Related
- [[knowledge/notes/editorial-notes-pattern]] — the broader category this decision extends
- [[knowledge/notes/flag-inferred-content-as-preliminary]] — complementary decision about content classification

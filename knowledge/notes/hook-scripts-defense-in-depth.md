---
type: decision
title: "Hook Scripts Defense-in-Depth"
description: "Two hardening decisions made 2026-04-19 to prevent silent failures in session hook scripts: a `_resolve_state_root()` fallback when `LLM_WIKI_STATE_ROOT` is unset, and an explicit guard mapping `.`, `"
timestamp: 2026-07-03T05:41:37
---
# Hook Scripts Defense-in-Depth

One-sentence summary: Two hardening decisions made 2026-04-19 to prevent silent failures in session hook scripts: a `_resolve_state_root()` fallback when `LLM_WIKI_STATE_ROOT` is unset, and an explicit guard mapping `.`, `..`, or empty slugs to `"root"`.

## Decision

**Date:** 2026-04-19

**Context:** Phase 4 soak testing of the multi-project hook system surfaced two silent-failure classes:

1. **Missing `LLM_WIKI_STATE_ROOT`** — if the user-level `settings.json::env` block omits this var, `_safe_write_error()` in both `session_start_project_state.py` and `session_end_project_tag.py` cannot locate `hook-errors.log`. Errors are swallowed silently; the hooks exit 0; the operator sees nothing.

2. **Degenerate slug values** — `Path.resolve()` normally prevents `.`, `..`, or empty strings reaching `_compute_slug()`, but a future refactor (e.g., skipping resolve for performance) could break this. A slug of `"."` would write to `knowledge/projects/./state.md`, which is a path traversal into the projects directory root.

**Choices made:**

- Added `_resolve_state_root()` to both scripts: if `LLM_WIKI_STATE_ROOT` is unset, fall back to `Path(LLM_WIKI_ROOT).parent / "LLM-wiki-state"`. This covers the common single-var setup and ensures error logging is always available.

- Added explicit guard in `_compute_slug()` in both scripts: `if not slug or slug in {".", ".."}:  return "root"`. The guard runs after `Path.resolve()`, so it only fires on pathological inputs; it is cheap insurance with no downside.

**Alternatives considered:**
- Raise an exception on degenerate slugs — rejected: hooks must exit 0 to avoid breaking sessions.
- Document the two-var requirement more prominently — done, but insufficient on its own; silent failures still occur when docs are missed.

## Evidence
- `knowledge/daily/2026-04-19.md` [17:09:56] — `LLM_WIKI_STATE_ROOT` bug surfaced
- `knowledge/daily/2026-04-19.md` [17:14:57] — slug guard rationale articulated
- `knowledge/daily/2026-04-19.md` [17:24:33] — both fixes confirmed and recorded as committed

## Related
- [[knowledge/notes/hook-errors-silent-without-state-root]] — deeper debugging entry for the state-root symptom
- [[knowledge/notes/b-sim-hook-testing]] — the testing technique that uncovered these gaps

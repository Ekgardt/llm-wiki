---
type: concept
title: "wiki/projects/"
timestamp: 2026-07-03T05:41:37
---
# wiki/projects/

Per-project state for every folder you use Claude Code in. Structure:

```
wiki/projects/
  _template/state.md     ← skeleton copied when a new project is first seen
  <slug>/state.md        ← "where we left off" for project <slug>
  <slug>/*.md            ← optional sub-pages as a project accumulates detail
```

## Slug rule
Preferred strategy, in priority order (implemented in `scripts/session_start_project_state.py::_compute_slug`):

1. **Base**: parent folder name, lowercase, hyphens.
2. **On collision**: append parent-of-parent (e.g. `backend` + `your-app` → `backend-your-app`).
3. **On further collision**: `owner-repo` parsed from `.git/config` origin remote.
4. **On further collision**: append grandparent folder name.
5. **Last resort**: 6-char path-hash suffix — guaranteed unique.

Ownership is determined by strict match of `- Project root:` in the existing `state.md`. A state.md without that line is treated as NOT owned by the current project (forces disambiguation). Re-opening the same project always returns the same slug (idempotent).

## What belongs here vs elsewhere
- **Here (`wiki/projects/<slug>/`):** current state, project-specific decisions, project-specific context.
- **In `wiki/concepts/`:** cross-cutting patterns that apply to *any* project.
- **In `memory/daily/`:** raw session captures (tagged `[<slug>]`).
- **In `memory/knowledge/`:** compiled cross-project lessons.

## Conventions
- `state.md` stays ≤ 1 screen. Split into sibling pages when it grows.
- `Source:` line records the project root path (and git remote if any).
- `## Editorial note` footer marks the page as vault metadata.

See [[Global Multi-Project Migration Plan]] for the full model and rollout.

# knowledge/projects/

Per-project state for every folder you use Claude Code in. Structure:

```
knowledge/projects/
  _template/state.md     ← skeleton copied when a new project is first seen
  <slug>/state.md        ← "where we left off" for project <slug>
  <slug>/bootstrap.md    ← detached discovery context exposed on SessionStart
  <slug>/*.md            ← optional sub-pages as a project accumulates detail
```

## Slug rule
Preferred strategy, in priority order (implemented in `scripts/session_start_project_state.py::_compute_slug`):

1. **Base**: parent folder name, lowercase, with every delimiter replaced by `-`; the canonical grammar is at most 128 Unicode letters/digits plus internal `.` and `-`.
2. **On collision**: append parent-of-parent (e.g. `backend` + `your-app` → `backend-your-app`).
3. **On further collision**: `owner-repo` parsed from `.git/config` origin remote.
4. **On further collision**: append grandparent folder name.
5. **Last resort**: verified 6/12/24/40/64-char path-hash suffixes, followed by bounded UUIDv4 retries under the claim lock.

Ownership accepts only bounded native absolute paths and prefers the strict JSON string in `- Project root JSON:`. The legacy backtick `- Project root:` line is used only when JSON metadata is absent; malformed or relative JSON fails closed. `- Runtime slug JSON:` persists the canonical alias, including for an exact-path reused legacy folder, so later projects cannot take it.

Bootstrap discovery is detached and can miss the first SessionStart. After
atomic publication, both SessionStart context paths expose a bounded excerpt as
untrusted project-derived data. `bootstrap.md` is not added to the search index.

## What belongs here vs elsewhere
- **Here (`knowledge/projects/<slug>/`):** current state, project-specific decisions, project-specific context.
- **In `knowledge/notes/`:** cross-cutting patterns that apply to *any* project.
- **In `knowledge/daily/`:** raw session captures (tagged `[<slug>]`).
- **In `knowledge/notes/`:** compiled cross-project lessons.

## Conventions
- `state.md` stays ≤ 1 screen. Split into sibling pages when it grows.
- `Source:` records the machine-readable JSON project root, persisted runtime slug, a legacy human-readable root, and the git remote if any.
- `## Editorial note` footer marks the page as vault metadata.

See [[Global Multi-Project Migration Plan]] for the full model and rollout.

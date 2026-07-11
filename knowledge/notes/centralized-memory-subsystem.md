---
type: decision
title: "Centralized Memory Subsystem Independent Of Worktree Mode"
description: "The memory subsystem (`run/state.json`, `knowledge/daily/`, `knowledge/notes/`) resolves to a single canonical location regardless of whether Claude Code runs from the main checkout or a git worktree."
timestamp: 2026-07-03T05:41:37
confidence: high
source_authority: user
---
# Centralized Memory Subsystem Independent Of Worktree Mode

One-sentence summary: The memory subsystem (`run/state.json`, `knowledge/daily/`, `knowledge/notes/`) resolves to a single canonical location regardless of whether Claude Code runs from the main checkout or a git worktree.

## Decision
Date: 2026-04-18.

Chose: `scripts/memory_state.py` resolves memory paths from a stable root (the main repo / `$LLM_WIKI_STATE_ROOT`), so hooks writing daily captures, state hashes, and knowledge pages land in the same place no matter which worktree the session was launched from. Added `scripts/cleanup_worktrees.py` for periodic hygiene — stale worktrees no longer leave behind divergent memory directories.

Rejected: per-worktree memory directories (the default if paths are resolved relative to `cwd`). That caused hooks to silently write into `worktree/knowledge/daily/...` that never got compiled back, splitting the knowledge trail across throwaway trees.

Why: memory is a long-lived knowledge layer shared across all sessions; worktrees are ephemeral branches for parallel work. Binding memory to the worktree's working copy ties durable knowledge to disposable context. Centralization keeps the compile pipeline coherent — one `daily/`, one `state.json`, one `index.md`.

How to apply: any new script, hook, or skill that reads/writes runtime state must go through `memory_state.py` path resolution, never raw `Path("knowledge/...")` relative to `cwd`. When auditing worktree-related issues, check whether the failing component respects this indirection.

Follow-up: if a new hook appears to "lose" captures, check first whether it was launched from a worktree and whether it imports the path resolver rather than hardcoding a relative path.

## Source
- Decision recorded 2026-04-18 — `knowledge/daily/2026-04-18.md` (private, installed vault only).

## Related
- [[docs/operating-model]] — compile cadence and the `knowledge/daily/` ↔ `knowledge/notes/` boundary this centralization protects.

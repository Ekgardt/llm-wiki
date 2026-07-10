---
type: concept
title: "Global Multi-Project Migration Plan"
description: "plan for extending LLM-wiki from a single-project vault into a global 'second brain' that auto-captures context from every Claude Code session across all projects and restores it on return."
timestamp: 2026-07-03T05:41:37
confidence: medium
source_authority: user
---
# Global Multi-Project Migration Plan

One-sentence summary: plan for extending LLM-wiki from a single-project vault into a global "second brain" that auto-captures context from every Claude Code session across all projects and restores it on return.

## Goal
Turn `$LLM_WIKI_ROOT` (on this machine: `$LLM_WIKI_ROOT/`) into the default knowledge backend for every Claude Code session, regardless of working directory. Per-project state (`knowledge/projects/<slug>/state.md`) is auto-loaded at session start and auto-updated at session end; sessions and daily logs flow into the shared `memory/` pipeline with a project tag.

## Intended end state
- Work in any folder → Claude reads `state.md` for that project → continues where you left off.
- Switch projects → independent state per slug, no cross-contamination.
- Return to a project after a gap → full context restored automatically.
- Cross-project lessons (patterns, conventions) promote into `knowledge/notes/` and become available everywhere.
- GitHub (public `Ekgardt/llm-wiki`) serves as cloud backup and enables multi-machine sync of the vault; runtime state (`$LLM_WIKI_STATE_ROOT`) stays local per machine.

## Design decisions (Phase 0)

- **Slug rule.** Preferred strategy, in priority order (implemented 2026-04-19 in `session_start_project_state.py::_compute_slug`):
  1. Base: parent folder name, lowercase, hyphens.
  2. On collision (different project already owns that slug): append parent-of-parent.
  3. On further collision: `owner-repo` from `.git/config` origin remote.
  4. On further collision: append grandparent suffix.
  5. Last resort: 6-char path-hash suffix (guaranteed unique).
  Ownership is detected by parsing `- Project root:` from the existing `state.md`.
- **Project vs cross-cutting boundary.**
  - Project-scoped (→ `knowledge/projects/<slug>/`): current state, architecture decisions specific to the project, API quirks, task context.
  - Cross-cutting (→ `knowledge/notes/`, `knowledge/notes/`): work habits, universal patterns, tool conventions.
- **Secrets hygiene.** Even in a private repo, `state.md` never stores credentials, tokens, or NDA-bound specifics by name. Habit, enforced by convention in global `CLAUDE.md`.
- **No auto-commit, no auto-push.** (Revised from the original Phase 0 plan of "auto-commit yes, auto-push no" — the auto-commit path was never actually implemented in any hook. `SessionEnd` hooks only *append* to `knowledge/daily/`; they do not run `git commit`. All commits are manual and deliberate. The revised decision is recorded here because it matches observed behavior and avoids the docs-vs-code drift that earlier rounds kept re-introducing.)

## Phases

### Phase 1 — Vault-internal migration (safe, reversible)
Entirely inside `$LLM_WIKI_ROOT/`. No external surface touched.

1. Create `knowledge/projects/` with `_template/state.md` skeleton (Where we left off / Recent decisions / Open threads / Links + `Source:`).
2. Migrate LLM-wiki itself as first project: `knowledge/projects/llm-wiki/state.md` from recent `knowledge/log.md` entries.
3. Register `## Projects` section in [[index]]; append row to [[log]].
4. Update `scripts/lint_memory.py` to treat `knowledge/projects/` as a valid section.
5. Update `scripts/lookup_mode.py` to exclude `projects/*/state.md` from the curated-page count (editorial metadata).
6. Audit `/knowledge-lookup` skill for hard-coded paths; add `knowledge/projects/` where needed.

**Verify:** `python scripts/lint_memory.py` → 0 findings (a transient `orphan_daily_logs` on the day's uncompiled daily log is acceptable; it closes on the next compile pass); `python scripts/lookup_mode.py` → tier still DIRECT.
**Rollback:** `git reset --hard origin/main`.

### Phase 2 — Global layer (`~/.claude/`)
Inject the vault into every Claude Code session regardless of cwd.

1. Set Windows user env var `LLM_WIKI_ROOT=$LLM_WIKI_ROOT`. Scripts read it with cwd fallback.
2. Create `~/.claude/CLAUDE.md` with: pointer to `$LLM_WIKI_ROOT`, slug rule, project/cross-cutting boundary, session-start/end contract.
3. Copy skills (`/knowledge-lookup`, `/bridge-promote-insight`, `/session-memory-compile`, `/knowledge-qa-file-back`, `/contradict-check`) from `$LLM_WIKI_ROOT/skills/` to `~/skills/`. (Symlink migration deferred to Phase 5.)

**Verify:** in an unrelated folder, `/knowledge-lookup` works; `echo $LLM_WIKI_ROOT` resolves.
**Rollback:** remove `~/.claude/CLAUDE.md`, unset env var, delete user-level skills copy.

### Phase 3 — Hook automation
The fragile part. Every hook must fail silently on error — never break a session.

1. `~/.claude/hooks/session_start.py`: compute slug from cwd, find/create `$LLM_WIKI_ROOT/knowledge/projects/<slug>/state.md`, emit as `additionalContext`. Safeguard: if `$LLM_WIKI_ROOT` unset → no-op.
2. `~/.claude/hooks/session_end.py`: append a tagged entry to `knowledge/daily/YYYY-MM-DD.md`, exit cleanly. Safeguard: all errors to stderr. **Note (historical plan vs shipped impl):** this step originally also planned `compile_memory.py --project <slug>` + an optional `git commit`. Neither shipped: the `--project` flag was never added to `compile_memory.py`, and no hook performs git commits. The shipped path is append-only — compile happens separately via the 18:00 cooldown-throttled auto-trigger, and commits are always manual.
3. Register hooks in `~/.claude/settings.json` via the `/update-config` skill (not by hand).
4. ~~Extend `scripts/compile_memory.py` with `--project <slug>` flag~~ — **dropped as superseded.** The per-project compile path was never built; in the shipped system, compile runs once per daily (not per-project), and per-project `state.md` updates are author-driven (Claude or the user edits it during sessions), not hook-driven. See the "Reliability notes" section below for the operative truth.

**Verify:** open Claude Code in a new folder → `state.md` appears in start context (fresh if first time). After session → `state.md` updated, daily log appended.
**Rollback:** remove hook scripts and `settings.json` entries.

### Phase 4 — Real-world test (1 week)
1. Pick a live working project, not a synthetic test.
2. Use Claude Code normally; don't change workflow.
3. Every 2 days check: does `state.md` evolve meaningfully? Are daily entries tagged correctly?
4. Success criteria:
   - [ ] Session starts with usable project context auto-loaded.
   - [ ] Project switching works without manual prompt.
   - [ ] `state.md` stays under ~1 screen.
   - [ ] Lint remains 0.
   - [ ] Lookup tier stays DIRECT or transitions to HYBRID on its own.

### Phase 5 — Polish (opportunistic, not blocking)
- `scripts/archive_daily.py` — already dormant in [[Memory Subsystem Action Plan]]; multi-project pressure makes it useful sooner.
- Per-project QMD collections — when tier crosses into HYBRID/QMD.
- Symlink migration for skills (Phase 2 copy → `~/skills/ → $LLM_WIKI_ROOT/skills/`).
- `knowledge/projects/<slug>/index.md` when a project grows past ~5 pages.
- Privacy profiles — split `LLM_WIKI_ROOT` only if NDA projects appear.
- Multi-machine sync documentation once actually used on a second machine.

## Risk register

| Risk | Phase | Mitigation |
|---|---|---|
| Hook crashes break Claude Code sessions | 3 | All hooks exit 0 on error; unset `$LLM_WIKI_ROOT` → no-op |
| `state.md` bloats | 3, 4 | Convention in global `CLAUDE.md`: ≤ 1 screen, detail in sub-pages |
| Slug collisions across projects with same folder name | 0, 3 | Collision rule documented; fallback to git remote slug |
| Daily log becomes unreadable mix across projects | 4, 5 | Entries tagged `[<slug>]`; accelerate archive automation |
| Cross-project concept name conflicts | 1 | Project knowledge lives in `projects/<slug>/`; only cross-cutting goes to `concepts/` |
| Auto-push leaks sensitive context | 0 | Keep auto-push off in Phase 3; secrets-hygiene convention |
| QMD index gets noisy when vault passes 50 pages | 5 | Per-project collections; global search opt-in |
| Dev-loop shell asymmetry (`qmd` only in Git Bash) | — | Already solved by `lookup_mode.py` reading SQLite mtime directly |

## Reliability notes

This harness is **best-effort glue**, not a guaranteed semantic layer. When documentation (this plan, `~/.claude/CLAUDE.md`, re-setup guide) and implementation (scripts + hooks) disagree, **implementation is the source of truth**. Writing docs before the behavior lands creates promises this section exists to caveat.

- **Hooks exit 0 on error.** Breaking a session is worse than a missed injection, so every hook swallows exceptions and logs to `$LLM_WIKI_STATE_ROOT/hook-errors.log`. Silent success after a catch does NOT mean the work landed — check the error log and `state.json::last_compile_status`.
- **Compile success is gated** (Round 1 fix, commit `1039aae`). A failed LLM compile no longer marks the daily as compiled — the daily stays eligible for the next run. Before the fix, a rate-limited compile would silently lose content.
- **Index rebuild failure is recoverable but visible.** `compile_memory.py` writes knowledge pages first, then rebuilds `knowledge/index.md`. If rebuild fails (missing tool, hardcoded path, permission), pages ARE saved and `last_compile_status=warning` is recorded; the index is stale until the next successful rebuild.
- **Slug collision resolution is active** (Round 2 fix, commit `ff0fbad`) — `_compute_slug` checks `- Project root:` in existing `state.md` and adds suffixes as needed. Pre-fix state.md pages are NOT retroactively migrated; a second project that would have collided gets the disambiguated slug, while the original keeps its base slug.
- **Injected context is best-effort summary, not ground truth.** `session_start_context.py` trims/dedupes/de-noises (Round 3 fix, commit `ed166d9`); it cannot guarantee relevance. If the preview feels off, re-read source files.
- **Compile cooldown** (Round 3 fix) — `MEMORY_COMPILE_COOLDOWN_SECONDS` (default 900s) prevents compile-per-session-end on busy days after 18:00. Content accumulates; next compile picks it up.

## Execution status
- **Phase 0** — decisions recorded above; baseline clean; GitHub remote is private and synced. *(2026-04-19)*
- **Phase 1** — **done 2026-04-19** (committed locally, unpushed). `knowledge/projects/` scaffolded, LLM-wiki migrated as first project, Projects section in [[index]], `lint_memory.py` + `lookup_mode.py` updated, `/knowledge-lookup` skill audited. Lint clean (transient uncompiled-daily finding only), DIRECT tier retained at 15 curated pages.
- **Phase 2** — **done 2026-04-19, live-verified.** Added `LLM_WIKI_ROOT` to user `settings.json::env`; created `~/.claude/CLAUDE.md` with slug rule, project/cross-cutting boundary, and session contract; installed `~/skills/knowledge-lookup/` rewritten to use absolute `$LLM_WIKI_ROOT` paths (other 7 vault-maintenance skills kept project-level only — per 2025-era best-practice separation). Verified from `<test-folder> `/knowledge-lookup` read vault content and answered correctly. Home-level files (CLAUDE.md, skills, settings) are machine-local, not git-tracked.
- **Phase 3** — **done 2026-04-19, end-to-end verified.** Implemented:
  - `scripts/session_start_project_state.py` (vault-tracked) — computes slug from `$CLAUDE_PROJECT_DIR`, reads `knowledge/projects/<slug>/state.md`, emits via `hookSpecificOutput.additionalContext`. Auto-creates gated on project markers (`.git`, `.claude/`, `CLAUDE.md`, `package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, `pom.xml`, `build.gradle(.kts)`, `Gemfile`, `composer.json`, `.csproj`, `mix.exs` — pattern from 2026 hooks research), preventing throwaway-folder clutter. Atomic `.tmp + os.replace` write for concurrency safety.
  - `~/.claude/hooks/session_start.sh` (machine-local shim) — discovery lives in `~/.claude/`, implementation in the vault; shim no-ops safely if `$LLM_WIKI_ROOT` is unset or impl missing.
  - Registered in user `settings.json::hooks::SessionStart` with matcher `startup|resume|clear|compact`.
  - **End-to-end verified**: opened Claude Code in a fresh markered folder — hook fired, state.md auto-created from template with slug and project root filled in, content injected into Claude's session context, zero entries in `hook-errors.log`.
- **Phase 3.5** — **done 2026-04-19.** Implemented:
  - `scripts/session_end_project_tag.py` (vault-tracked) — reads SessionEnd payload from stdin, computes slug, appends tagged entry to `knowledge/daily/YYYY-MM-DD.md` when cwd is **outside** the vault. Vault cwd already handled by existing project-level `session_end_capture.py` (LLM-driven via `flush_memory.py`) — the user-level hook skips that case to avoid duplicates.
  - `~/.claude/hooks/session_end.sh` (machine-local shim) — same wrapper pattern as session_start.
  - Registered in user `settings.json::hooks::SessionEnd` with matcher `""`.
  - Tech-debt sweep: removed 14 lines of unreachable code in `scripts/lookup_mode.py` (dead fallback after a `return info`).
  - Re-setup guide `knowledge/projects/llm-wiki/re-setup.md` — checklist for restoring the global harness on a new machine (env vars, `~/.claude/CLAUDE.md`, skills, hook wrappers).
- **Phase 4** — **done 2026-04-19** (accelerated). 51 automated scenarios + 5 manual e2e checks in fresh Claude Code windows (persistence across 3 re-openings, `/compact` re-injection, cross-project isolation, real-project slug `your-app`, markerless no-op). 3 bugs surfaced and fixed: `LLM_WIKI_STATE_ROOT` missing in user env, `_compute_slug` `.`/`..` edge cases, `$HOME` false-positive on `.claude/` marker.
- **Phase 5** — **done 2026-04-19.** Tractable polish items executed:
  - **Shared editorial module** — `scripts/vault_editorial.py` centralizes `EDITORIAL_NAMES`, `BACKLINK_EXEMPT_NAMES`, `BROKEN_LINK_SKIP_NAMES`, and `editorial_parents_to_skip()`. `lint_memory.py` and `lookup_mode.py` import from it, removing the prior duplicate literals.
  - **`scripts/archive_daily.py`** — dormant infrastructure, ready when `knowledge/daily/` grows past 30 files. Silent no-op below threshold; `--force`, `--commit`, `--threshold`, `--max-age` flags. Archive target: `knowledge/daily/archive/YYYY-MM/`. Respects the "never delete un-compiled logs" rule.
  - **Plugin packaging (researched, skipped)** — Claude Code plugin architecture in April 2026 cannot host a CLAUDE.md-equivalent, cannot modify user-level `settings.json::env`, and enforces a cache directory that breaks relative-path hooks to outside scripts. Plugins are designed for optional, versioned, distributable extensions — not mandatory per-machine harness. Current manual-install path via `knowledge/projects/llm-wiki/re-setup.md` is the correct architecture. If Anthropic ships user-level plugin support for CLAUDE.md + env vars in a future release, revisit.
  - **Symlink migration for skills (attempted, skipped)** — Windows refuses symlink creation without Developer Mode / admin elevation. Also architecturally unsuitable: user-level `knowledge-lookup/SKILL.md` has different content (absolute `$LLM_WIKI_ROOT` paths) than the project-level variant. Copying with the documented divergence is the right pattern.

  **Kept dormant:** per-project QMD collections — triggered when wiki crosses 50 curated pages (currently 16); no point implementing a split that isn't yet useful.
- **Post-audit rounds 1–3** — **done 2026-04-19** (commits `1039aae`, `ff0fbad`, `ed166d9`). External colleague review surfaced 3 critical and 4 important bugs that pre-dated the multi-project work but were not caught by earlier Phase 4 testing (scope was limited to the new hooks). Fixed:
  - **R1 C1** `rebuild_memory_index.py` hardcoded `$LLM_WIKI_ROOT` → imports `memory_state.ROOT` now. Script was non-portable; broke `compile_memory` and `query_memory --file-back` on any machine where the vault lives elsewhere.
  - **R1 C2** `compile_memory.py` silently marked failed compiles as successful — `compiled_daily_hashes` was written unconditionally, so a rate-limited/crashed compile would make the next run skip the daily. Added `_compile_succeeded()` gate; failed compile now exits 1 with `last_compile_status=error` and the daily stays eligible. Class "silent data loss" closed.
  - **R1 I7** `rebuild_index()` returned `None` and used `check=False` — index rebuild failures were invisible. Now returns `bool`, callers surface warnings, state.json records `last_index_rebuild_ok`.
  - **R2 C3** Slug collision handling was documented but not implemented — two projects named `backend/` would have silently merged into one `state.md`. Implemented: base → parent-of-parent → git `owner-repo` → grandparent → path-hash. Ownership check via `- Project root:` line. SessionEnd looks up SessionStart's assigned slug for consistency.
  - **R3 I4** `session_start_context.py` was injecting `Trigger:`, `Transcript:`, `Project root:`, and session-id UUIDs into every new session — pure token waste. Now stripped; injection dropped from 2432 → 2102 chars.
  - **R3 I5** `query_memory.py::slugify` used `[^a-z0-9]+` which destroyed Cyrillic — every Russian question collapsed to `"question"`. Now `[^\w]+` with `re.UNICODE` + deterministic 6-char hash suffix for collision safety.
  - **R3 I6** `flush_memory.maybe_trigger_compile` had no cooldown — on a busy post-18:00 day every session-end respawned a compile. Added `MEMORY_COMPILE_COOLDOWN_SECONDS` (default 900s).

## Source
- Conversation 2026-04-19 scoping the global multi-project model.
- Current state of `$LLM_WIKI_ROOT/` and `$LLM_WIKI_STATE_ROOT/`.
- [[Memory Subsystem Action Plan]] — pattern for an action-plan page and for the compile/lint infrastructure being extended.
- [[Karpathy LLM Wiki Workflow]] — pipeline analogy (knowledge/raw/knowledge/inbox/knowledge/notes → now projects/ as a fourth axis).

## Related
- [[Memory Subsystem Action Plan]] — prior multi-phase plan this one mirrors in structure.
- [[Karpathy LLM Wiki Workflow]]
- [[Wiki vs Memory Compiler vs Fusion]]
- [[knowledge/notes/pipeline-mirroring|Pipeline Mirroring]] — the convention that frames `knowledge/projects/` as a new mirrored axis.
- [[Preliminary Flagging]] — applies to any inferred claims in per-project `state.md`.
- [[docs/USER-GUIDE|User guide]] / `install.ps1` · `install.sh` — machine harness setup (env, hooks, agents). Per-project `re-setup.md` is local-only (gitignored under `knowledge/projects/`).

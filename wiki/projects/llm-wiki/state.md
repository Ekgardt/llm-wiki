---
type: project-state
title: "LLM-wiki — State"
description: "the vault itself — an Obsidian-style markdown knowledge base serving as a global 'second brain' across all Claude Code projects, built on a two-layer (wiki + memory) pipeline inspired by Karpathy's LL"
timestamp: 2026-07-03T05:41:37
---
# LLM-wiki — State

One-sentence summary: the vault itself — an Obsidian-style markdown knowledge base serving as a global "second brain" across all Claude Code projects, built on a two-layer (wiki + memory) pipeline inspired by Karpathy's LLM wiki workflow fused with the claude-memory-compiler pattern.

## Where we left off
- **[[Global Multi-Project Migration Plan]] Phases 1–5 done, plus five post-audit rounds (R1–R8) folded in.** System is in materially-mature state per colleague audit v5 (architecture 8.8/10, operational-readiness 8.2/10).
- Regression suite (37 tests, hermetic, ~2s) guards all critical semantics: compile fail-safe, slug collision, context noise, SessionEnd skip, Unicode slug.
- CI workflow at `.github/workflows/tests.yml` runs lint + pytest on push/PR. Lint uses `--fail-on-findings` so new structural issues fail the build (`orphan_daily_logs` exempt — self-resolves on next compile).
- Local commits ahead of `origin/main`: run `git log origin/main..HEAD --oneline | wc -l` for the live count. All commits lint-clean and pre-commit-hook-passed.
- **Next immediate step:** final `git push origin main`. Nothing is blocking it operationally; remaining items (see Open threads) are dormant or preferences.

## Recent decisions
- **2026-04-19** — **Global multi-project mode.** Vault stops being single-project; `wiki/projects/<slug>/` becomes a new top-level axis alongside `concepts/`, `entities/`, `syntheses/` etc. **Why:** user wants the vault to auto-capture and restore context for every Claude Code session regardless of cwd.
- **2026-04-19** — **Slug: 5-step priority** (base → parent-of-parent → git `owner-repo` from `.git/config` → grandparent → 6-char path-hash). Strict ownership via `- Project root:` line in existing `state.md` — ambiguous state.md (no Source line) is NOT claimed. **Why:** simplest usable rule that guarantees uniqueness in pathological collision cases. Implemented in `session_start_project_state.py::_compute_slug`.
- **2026-04-19** — **No auto-commit, no auto-push.** (Revised from the original Phase 0 idea of "auto-commit yes, auto-push no" — the auto-commit path was never implemented in any hook. SessionEnd hooks append to `memory/daily/` only; commits are always manual and deliberate.)
- **2026-04-14** — **Runtime vs vault separation** via `$LLM_WIKI_STATE_ROOT=$LLM_WIKI_STATE_ROOT/`. Ephemeral hashes, lint reports, QMD indexes live outside the git-tracked vault. Obsidian and GitHub remote track only durable content.
- **2026-04-14** — **Three-tier retrieval** (DIRECT <50 / HYBRID 50–300 / QMD >300). Current tier = DIRECT. Run `python scripts/lookup_mode.py` for the live page count instead of trusting a number in this file.

## Open threads
- **Final `git push origin main`** — awaiting explicit approval. Nothing operationally blocks it.
- **Dormant (triggered by metrics, not time):**
  - Per-project QMD collections — when curated pages cross 50 (currently in DIRECT tier; run `lookup_mode.py` for live count).
  - `archive_daily.py` — auto-trigger when `memory/daily/` passes ~30 flat files.
  - Plugin packaging — revisit if Anthropic ships user-level env + CLAUDE.md plugin support.
- **Soak watchlist (continuous):** state.md bloat, slug collisions between similarly-named folders, hook errors in `hook-errors.log`, daily-log noise, UX papercuts. No time deadline; fix opportunistically.

## Links
- [[Global Multi-Project Migration Plan]] — the driving plan for the current phase.
- [[wiki/projects/llm-wiki/re-setup|LLM-wiki re-setup guide]] — checklist for restoring the global harness on a new machine.
- [[Memory Subsystem Action Plan]] — sibling multi-phase plan, pattern for this one.
- [[Karpathy LLM Wiki Workflow]] — pipeline inspiration.
- [[Wiki vs Memory Compiler vs Fusion]] — comparison of the three approaches this vault fuses.
- [[Pipeline Mirroring]] — convention that frames `wiki/projects/` as a new mirrored axis.
- [[Vault Home]] — human-readable front door.
- [[index]] — wiki navigation map.
- [[log]] — editorial changelog.

## Source
- Project root: `$LLM_WIKI_ROOT/`
- Git remote: `git@github.com:Ekgardt/llm-wiki.git` (private)
- Runtime state: `$LLM_WIKI_STATE_ROOT/` (not tracked)

## Editorial note
This page is the state record for the LLM-wiki project itself — the vault as one of its own projects. **Read and injected** by the user-level SessionStart hook (`scripts/session_start_project_state.py`); content is maintained by Claude or the user during sessions, not auto-written by any hook. Keep to ≤ 1 screen; detail belongs on dedicated pages.

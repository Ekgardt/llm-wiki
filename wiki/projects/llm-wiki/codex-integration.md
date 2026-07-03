---
type: concept
title: "Codex Integration for LLM-wiki"
description: "Codex now uses the same `$LLM_WIKI_ROOT` and `$LLM_WIKI_STATE_ROOT` memory backend as Claude Code, via global `~/.codex/AGENTS.md`, a user-level skill, and a thin wrapper over the existing session-state scr"
timestamp: 2026-07-03T05:41:37
---
# Codex Integration for LLM-wiki

One-sentence summary: Codex now uses the same `$LLM_WIKI_ROOT` and `$LLM_WIKI_STATE_ROOT` memory backend as Claude Code, via global `~/.codex/AGENTS.md`, a user-level skill, and a thin wrapper over the existing session-state scripts.

## What changed
- Added `scripts/codex_memory.py` as a Codex-facing wrapper around:
  - `scripts/session_start_project_state.py`
  - `scripts/session_end_project_tag.py`
  - `scripts/lookup_mode.py`
- Added global Codex instructions in `~/.codex/AGENTS.md`.
- Added a user-level Codex skill `~/.codex/skills/llm-wiki-memory/SKILL.md`.

## Why this exists
Claude Code has native session hooks. Codex does not expose the same hook surface, so the integration point is different:

- Claude Code: automatic SessionStart / SessionEnd hooks.
- Codex: always-on global instructions plus explicit helper commands.

The important constraint is preserved: **both tools use the same vault, the same per-project state files, the same slugging logic, and the same shared daily log**.

## Codex workflow
1. At the start of meaningful work in any project, run:
   - `python $LLM_WIKI_ROOT/scripts\codex_memory.py project-state --cwd "<project dir>"`
2. Use the emitted `wiki/projects/<slug>/state.md` as the project's handoff note.
3. For cross-project or historical questions, run:
   - `python $LLM_WIKI_ROOT/scripts\codex_memory.py lookup-tier`
   - then answer from `wiki/` first.
4. When durable knowledge changes, update `state.md` and, when appropriate, the curated `wiki/`.
5. At the end of substantial work, append a breadcrumb:
   - `python $LLM_WIKI_ROOT/scripts\codex_memory.py daily-log --cwd "<project dir>" --reason codex-turn-end`

## User-facing use cases

### 1. Resume a project after a break
You open Codex in a repo you worked on earlier and ask to continue.

What happens:
- Codex pulls `wiki/projects/<slug>/state.md` through `codex_memory.py project-state`.
- The "Where we left off" section becomes the starting handoff note.
- If the file says "stopped after wiring auth middleware; next step is integration test", Codex resumes from there instead of rediscovering context from scratch.

### 2. Switch between multiple projects
You alternate between several repos during the day.

What happens:
- Each repo resolves to its own slug and its own `wiki/projects/<slug>/state.md`.
- Codex reads the state page for the current repo only.
- This prevents cross-project contamination while still keeping all projects in the same shared vault.

### 3. Ask "what do we already know about this project?"
You ask Codex for prior context, decisions, blockers, or next steps.

What happens:
- Codex reads the per-project state first.
- If the answer is not fully there, it expands into the shared wiki.
- Result: short local history comes from `state.md`; broader durable knowledge comes from `wiki/`.

### 4. Ask a cross-project historical question
You ask about a reusable convention, an old decision, or a pattern that may have appeared in other work.

What happens:
- Codex checks the retrieval tier via `lookup-tier`.
- It answers from the curated wiki first.
- If the curated layer is insufficient, only then does it fall back to `raw/` or `inbox/`.

### 5. End a substantial coding session
You and Codex finish meaningful work in a non-vault repo.

What happens:
- Codex should update `wiki/projects/<slug>/state.md` with the new handoff state.
- It can append a breadcrumb into `memory/daily/YYYY-MM-DD.md` via `codex_memory.py daily-log`.
- This leaves a shared trace that Claude Code can later see too.

### 6. Promote a project-local lesson into durable shared knowledge
While working in one repo, you discover a reusable pattern.

What happens:
- Short local impact goes into that repo's `state.md`.
- Reusable knowledge should be promoted into the main `wiki/`.
- Once promoted, both Codex and Claude can retrieve it later as shared durable knowledge rather than as one repo's temporary note.

### 7. First visit to a new real project
You open Codex in a repo that has markers like `.git`, `pyproject.toml`, or `package.json`, but no prior state page exists.

What happens:
- `codex_memory.py project-state` reuses the same creation logic as the Claude SessionStart script.
- A new `wiki/projects/<slug>/state.md` can be created from the shared template.
- From then on, that repo has persistent memory in the same system as Claude.

### 8. Work inside the vault itself
You open Codex directly in `$LLM_WIKI_ROOT`.

What happens:
- The same slug/state logic still resolves the vault's own project state.
- But here the authoritative contract is the vault itself: `CLAUDE.md`, `wiki/`, `memory/`, and the existing scripts.
- Codex is effectively operating on the memory system itself rather than merely consuming it.

## Limits
- Codex does **not** have a true equivalent of Claude's automatic SessionStart/SessionEnd hooks here.
- Therefore the Codex integration is behaviorally close, but not byte-for-byte identical.
- The memory backend is shared; the trigger surface is different.

## Source
- `scripts/session_start_project_state.py`
- `scripts/session_end_project_tag.py`
- `scripts/lookup_mode.py`
- `~/.codex/AGENTS.md`
- `~/.codex/skills/llm-wiki-memory/SKILL.md`

## Related
- [[wiki/projects/llm-wiki/re-setup|LLM-wiki — Re-setup guide]]
- [[Global Multi-Project Migration Plan]]

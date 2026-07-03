---
type: concept
title: "LLM-wiki — Re-setup on a new machine"
description: "step-by-step checklist for restoring the global multi-project 'second brain' on a fresh machine — the machine-local half of the system lives in `~/.claude/` and is **not** part of the git-tracked vaul"
timestamp: 2026-07-03T05:41:37
---
# LLM-wiki — Re-setup on a new machine

One-sentence summary: step-by-step checklist for restoring the global multi-project "second brain" on a fresh machine — the machine-local half of the system lives in `~/.claude/` and is **not** part of the git-tracked vault.

## Why this page exists
The vault at `$LLM_WIKI_ROOT/` is fully git-tracked and recoverable via `git clone git@github.com:Ekgardt/llm-wiki.git`. The **global harness** (per-project state injection, `$LLM_WIKI_ROOT` env var, user-level `knowledge-lookup` skill, hook wrappers) lives in `~/.claude/` (machine-local per design) and is **not** tracked. On a new machine — or after OS reinstall — these pieces must be recreated manually. This page is the checklist.

For the Codex-side setup that reuses the same backend, see [[wiki/projects/llm-wiki/codex-integration|LLM-wiki — Codex integration]].

## Prerequisites on the new machine
- Claude Code installed.
- Python 3.10+ on PATH.
- Optional: `uv` on PATH (the hook wrappers prefer it; fall back to plain `python` if absent).
- Git configured with SSH access to the vault remote.

## Step 1 — Clone the vault
```
git clone git@github.com:Ekgardt/llm-wiki.git $LLM_WIKI_ROOT
```
(Or any path you prefer — the env var below points at it.)

## Step 2 — Choose a runtime-state directory
Runtime state (hashes, lint reports, QMD index, hook-errors log) must live **outside** the vault so git doesn't track it. The convention is `$LLM_WIKI_STATE_ROOT/`.
```
mkdir $LLM_WIKI_STATE_ROOT
```

## Step 3 — Create `~/.claude/` layout
Four surfaces to set up, in order:

### 3a — `~/.claude/settings.json::env`
Add to the `env` block (create the file if absent):
```json
"LLM_WIKI_ROOT": "$LLM_WIKI_ROOT",
"LLM_WIKI_STATE_ROOT": "$LLM_WIKI_ROOT-state",
"XDG_CACHE_HOME": "$LLM_WIKI_ROOT-state\\xdg-cache",
"XDG_CONFIG_HOME": "$LLM_WIKI_ROOT-state\\xdg-config",
"INDEX_PATH": "$LLM_WIKI_ROOT-state\\qmd\\index.sqlite"
```
Also transfer any existing hooks (e.g. the Notification hook pointing at `$LLM_WIKI_ROOT\scripts\notify.ps1` on Windows).

### 3b — Global user instructions
Copy `~/.claude/CLAUDE.md` from the reference template (see "Reference copies" below). Contents set the slug rule, project/cross-cutting boundary, and session contract.

### 3c — Global `knowledge-lookup` skill
Copy `~/.claude/skills/knowledge-lookup/SKILL.md` — the user-level variant with `$LLM_WIKI_ROOT`-absolute paths. The other 7 vault-maintenance skills (`bridge-promote-insight`, `contradict-check`, `knowledge-compile`, `knowledge-qa-file-back`, `knowledge-review`, `session-memory-compile`, `session-memory-review`) are intentionally NOT installed at user level — they only make sense when cwd = vault, where project-level `.claude/skills/` serves them.

### 3d — Hook wrappers
Create `~/.claude/hooks/` and drop in two shims:
- `session_start.sh` — invokes `$LLM_WIKI_ROOT/scripts/session_start_project_state.py`
- `session_end.sh` — invokes `$LLM_WIKI_ROOT/scripts/session_end_project_tag.py`

Both shims are 10–15 lines and do nothing if `$LLM_WIKI_ROOT` is unset (safe no-op). Mark executable: `chmod +x ~/.claude/hooks/*.sh`.

Register the hooks in `~/.claude/settings.json::hooks`:
- `SessionStart` with matcher `startup|resume|clear|compact` → `bash ~/.claude/hooks/session_start.sh`
- `SessionEnd` with matcher `""` → `bash ~/.claude/hooks/session_end.sh`

## Step 4 — Verify
1. Open a fresh terminal (so the new env vars are in the Claude Code environment).
2. `echo $LLM_WIKI_ROOT` — must print the vault path.
3. Open Claude Code in any markered folder (a git repo works).
4. Ask Claude: "what do you know about this project from per-project state?" — it should mention the slug and either cite the freshly-created `state.md` or confirm one exists.
5. Check that `$LLM_WIKI_ROOT/wiki/projects/<slug>/state.md` appeared.
6. Check `$LLM_WIKI_ROOT-state\hook-errors.log` — should be absent or empty.

## Reference copies (for transfer)
The authoritative current state of the machine-local files is whatever is installed on the working machine. To transfer to a new box without re-deriving:

1. On the old machine, copy the five files into a transfer bundle:
   - `~/.claude/CLAUDE.md`
   - `~/.claude/settings.json` (scrub creds if sharing across trust boundaries)
   - `~/.claude/skills/knowledge-lookup/SKILL.md`
   - `~/.claude/hooks/session_start.sh`
   - `~/.claude/hooks/session_end.sh`
2. Place them at the same paths on the new machine, adjust `LLM_WIKI_ROOT` and `LLM_WIKI_STATE_ROOT` values if paths differ.

## Troubleshooting
- **Hooks not firing**: run `claude --debug` to see hook invocation logs. Confirm `~/.claude/hooks/*.sh` are executable and `LLM_WIKI_ROOT` is set at the OS level too (some terminals don't inherit Claude Code's env-injection).
- **Skill not available globally**: restart Claude Code after creating `~/.claude/skills/` — the skill registry is read at startup.
- **Daily log mojibake on Windows**: confirm `XDG_*` env vars point to ASCII paths and that `python` has UTF-8 output (the scripts already `reconfigure(encoding="utf-8")` on startup).

## Source
- Live reconstruction exercise — this page is written to be followed, not compiled.
- [[Global Multi-Project Migration Plan]] — the plan that established each surface.
- `~/.claude/` state on the reference machine as of 2026-04-19.

## Editorial note
This page is **operational documentation** — a checklist, not compiled knowledge from `raw/`. It belongs under `wiki/projects/llm-wiki/` because restoring LLM-wiki's global harness is a project-specific procedure, not a cross-project convention.


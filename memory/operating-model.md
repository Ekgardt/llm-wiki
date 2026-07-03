# Session Memory Operating Model

One-sentence summary: Session memory captures what Claude Code and the human learned while working, then compiles it into durable project memory.

## Raw layer
- `memory/daily/YYYY-MM-DD.md` stores captured session-end and (optionally) pre-compact summaries.
- Baseline path is the `SessionEnd` hook: just work and close Claude — `scripts/session_end_capture.py` spawns `flush_memory.py` and a daily-log entry lands automatically. No `/compact` required.
- The `PreCompact` hook is a safety net for long sessions that auto-compact; `/compact` is an **optional manual tool**, not part of the regular capture regimen.

## Compiled layer
- `memory/knowledge/` stores durable pages for decisions, patterns, debugging notes, concepts, and Q&A.

## Rules
- Not every chat detail deserves permanence.
- Save durable decisions, lessons, repeatable commands, architectural constraints, and gotchas.
- Keep project memory distinct from external-source research in `wiki/`.

## Compile Procedure

### When to run `/session-memory-compile`
- Optional — not required for the baseline "just work and close Claude" flow. The `SessionEnd` hook already captures the raw daily log; compilation into `memory/knowledge/` is a separate, deliberate step.
- Run it when a working session produced non-trivial decisions or lessons worth lifting.
- Run it before closing a multi-day initiative, to consolidate scattered daily notes.
- If `/compact` was used and a pre-compact summary landed in `memory/daily/`, that's a reasonable cue to compile — but `/compact` itself is never required.
- Skip if the day's daily log is only status chatter — nothing to lift.

### What from `memory/daily/` is worth lifting
Lift an item into `memory/knowledge/` only if it is:
- reusable beyond the session it came from,
- not already captured in code, config, or `wiki/`,
- specific enough to act on next time (not "we should be careful with X").

### Knowledge categories
- **concepts/** — project-specific mental models and vocabulary (e.g. "raw → inbox → wiki pipeline"). Noun-shaped.
- **decisions/** — a choice made, with the alternatives rejected and the reason. Dated. Immutable once written; supersede rather than edit.
- **patterns/** — a recurring approach that worked more than once ("when X, do Y because Z"). Verb-shaped.
- **debugging/** — a concrete failure mode and its fix/diagnostic. Symptom → cause → resolution.
- **qa/** — a question the human asked and its settled answer, when the answer is non-obvious and likely to be asked again.

If an item could fit two categories, prefer the more actionable one (patterns > concepts; debugging > qa).

### Do not lift into durable memory
- Status updates, task progress, "what I did today."
- Restatements of code, file paths, or structure discoverable by reading the repo.
- One-off chat preferences already covered by `CLAUDE.md` or auto-memory.
- Summaries of `raw/` or `inbox/` material — those belong in `wiki/`.
- Speculation not yet validated by use.

### Updating the indexes
For every new knowledge page:
1. Add a one-line bullet under the correct section in `memory/index.md` (format: `- [[memory/knowledge/<category>/<slug>]] — one-line hook`).
2. Append a dated entry to `memory/log.md` describing what was compiled and from which daily source(s).
3. Keep `memory/index.md` section bullets alphabetized within their section.

### `memory/` vs `wiki/` boundary

**Two-question checklist** — answer these in order, first clear *yes* wins:

| # | Question | If yes → |
|---|---|---|
| 1 | Would this be useful to someone who has **never seen this repo's session history**? | `wiki/` |
| 2 | Does it cite `raw/`, a public reference, or a named external author? | `wiki/` |

If both are **no** → it's `memory/`. That's it — don't overthink it.

Examples:
- "When `Edit` fails with multiple matches, expand `old_string` with preceding unique context." → both **no** (internal tool workflow, no external source) → `memory/knowledge/debugging/`.
- "Karpathy's April 2026 pattern for LLM-maintained wikis works at ~100 articles without RAG." → both **yes** (useful to anyone, cites Karpathy) → `wiki/concepts/` or `wiki/syntheses/`.
- "Preliminary flagging as a vault convention" → started as **no** (project lore) so was born in `memory/`, later became **yes** (generalizes) so was promoted via `bridge-promote-insight` to `wiki/concepts/`. The memory page remains as the dated decision; the wiki page is the reusable convention.

**Expanded criteria** (use only when the two-question check is ambiguous):
- `wiki/` if the insight generalizes beyond this project's internal workflow.
- `memory/` if it references specific session events, internal file layout, or conventions only meaningful given knowledge of this repo.

Rule of thumb: `memory/` is first-person project lore; `wiki/` is third-person compiled knowledge. Promote, don't duplicate — when an item moves to `wiki/`, replace the `memory/` page with a short stub linking to the wiki page.

# Session Memory Operating Model

One-sentence summary: Session memory captures what Claude Code and the human learned while working, then compiles it into durable project memory.

## Raw layer
- `knowledge/daily/YYYY-MM-DD.md` stores captured session-end and (optionally) pre-compact summaries.
- Baseline path is the `SessionEnd` hook: just work and close Claude — `scripts/session_end_capture.py` spawns `flush_memory.py` and a daily-log entry lands automatically. No `/compact` required.
- The `PreCompact` hook is a safety net for long sessions that auto-compact; `/compact` is an **optional manual tool**, not part of the regular capture regimen.

## Compiled layer
- `knowledge/notes/` stores durable pages for decisions, patterns, debugging notes, concepts, and Q&A.

## Rules
- Not every chat detail deserves permanence.
- Save durable decisions, lessons, repeatable commands, architectural constraints, and gotchas.
- Keep project memory distinct from external-source research in `knowledge/notes/`.

## Compile Procedure

### When to run `/session-memory-compile`
- Optional — not required for the baseline "just work and close Claude" flow. The `SessionEnd` hook already captures the raw daily log; compilation into `knowledge/notes/` is a separate, deliberate step.
- Run it when a working session produced non-trivial decisions or lessons worth lifting.
- Run it before closing a multi-day initiative, to consolidate scattered daily notes.
- If `/compact` was used and a pre-compact summary landed in `knowledge/daily/`, that's a reasonable cue to compile — but `/compact` itself is never required.
- Skip if the day's daily log is only status chatter — nothing to lift.

### What from `knowledge/daily/` is worth lifting
Lift an item into `knowledge/notes/` only if it is:
- reusable beyond the session it came from,
- not already captured in code, config, or `knowledge/notes/`,
- specific enough to act on next time (not "we should be careful with X").

### Knowledge categories
All pages live flat under `knowledge/notes/<slug>.md` with a `type:` frontmatter field. The categories below describe that field, not subdirectories.
- **concepts** — project-specific mental models and vocabulary (e.g. "raw → inbox → wiki pipeline"). Noun-shaped.
- **decisions** — a choice made, with the alternatives rejected and the reason. Dated. Immutable once written; supersede rather than edit.
- **patterns** — a recurring approach that worked more than once ("when X, do Y because Z"). Verb-shaped.
- **debugging** — a concrete failure mode and its fix/diagnostic. Symptom → cause → resolution.
- **qa** — a question the human asked and its settled answer, when the answer is non-obvious and likely to be asked again.

If an item could fit two categories, prefer the more actionable one (patterns > concepts; debugging > qa).

### Do not lift into durable memory
- Status updates, task progress, "what I did today."
- Restatements of code, file paths, or structure discoverable by reading the repo.
- One-off chat preferences already covered by `CLAUDE.md` or auto-memory.
- Summaries of `knowledge/raw/` or `knowledge/inbox/` material — those belong in `knowledge/notes/`.
- Speculation not yet validated by use.

### Updating the indexes
For every new knowledge page:
1. Add a one-line bullet under the correct section in `knowledge/index.md` (format: `- [[knowledge/notes/<slug>]] — one-line hook`).
2. Append a dated entry to `knowledge/log.md` describing what was compiled and from which daily source(s).
3. Keep `knowledge/index.md` section bullets alphabetized within their section.

### `knowledge/daily/` vs `knowledge/notes/` boundary

**Two-question checklist** — answer these in order, first clear *yes* wins:

| # | Question | If yes → |
|---|---|---|
| 1 | Would this be useful to someone who has **never seen this repo's session history**? | `knowledge/notes/` |
| 2 | Does it cite `knowledge/raw/`, a public reference, or a named external author? | `knowledge/notes/` |

If both are **no** → keep it in episodic form (`knowledge/daily/`) until compile lifts a durable slice. That's it — don't overthink it.

Examples:
- "When `Edit` fails with multiple matches, expand `old_string` with preceding unique context." → both **no** as raw session chatter, but after compile becomes a notes page (`type: debugging`).
- "Karpathy's April 2026 pattern for LLM-maintained wikis works at ~100 articles without RAG." → both **yes** → `knowledge/notes/`.
- "Preliminary flagging as a vault convention" → may start as daily capture, then compile into a notes decision/pattern page.

**Expanded criteria** (use only when the two-question check is ambiguous):
- `knowledge/notes/` if the insight generalizes beyond a single session.
- `knowledge/daily/` if it is still episodic (session-bound, not yet distilled).

Rule of thumb: daily is first-person episodic capture; notes are third-person compiled knowledge.

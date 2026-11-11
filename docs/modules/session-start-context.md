# SessionStart hook — inject a compact memory context into the new session

`scripts/session_start_context.py` and the tests that exercise them most.

Emits a JSON object on stdout with `hookSpecificOutput.additionalContext` containing a trimmed view of project memory: - `knowledge/index.md` — H1, Entry points, first 3 non-empty knowledge sections. Lines are kept whole; the section budgets decide what fits. - Latest daily log — a short excerpt of the most recent meaningful session block. Empty hook-trigger blocks, XML `<analysis>`/`<summary>` wrappers, and mojibake lines are stripped. If nothing clean remains, falls back to a one-line note. - the private vault log (`vault_log.LOG_RELATIVE`) — last 3 dated entries, whole.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

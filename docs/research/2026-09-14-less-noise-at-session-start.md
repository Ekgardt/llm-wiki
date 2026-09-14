# Less noise at session start

Dated 2026-09-14. Part of item 5.3 of `docs/AUDIT-2026-09-14-2.md`. The research before
the change.

## What was found

- **The code-impact block.** `session_start_context._impact_block` →
  `impact_analysis.format_for_advisory` injects `stale_pages`. When changed ranges do not
  resolve to canonical symbols, `_textual_fallback` adds pages that merely contain a
  changed name as a word, marked `method: textual-name-match`, `confidence: low`,
  `classification: conservative`. The session start of this very session carried
  "1 diff record(s), 0 canonical symbol(s), 0 affected artifact(s)" followed by three
  pages flagged for mentioning the symbol `words` — pages about Andrej Karpathy. That is
  tokens in every session for a signal the analysis itself labels conservative.
- **Guard-rail summaries cut mid-word.** `build_guardrails` stores a rule's summary as
  `summary[:150]`, so the injected rules end in the middle of a word ("…because a w").
- **The promised debug copy.** `session_start_context`'s docstring and
  `docs/STRUCTURE.md` say the payload is written to `logs/session-start-last.txt`. Only
  the module's own CLI writes it; the hooks go through
  `integration_adapter._session_start` → `_append_context`, which never does.
- Not changed, with reasons: the index section repeats `@knowledge/index.md` only for
  Claude Code — Codex and OpenCode do not expand that import, so the section is their
  only copy; the daily and log sections that the character ceiling drops cost
  milliseconds, not tokens; four reads of `run/state.json` cost milliseconds.
- The code graph: `format_for_advisory` ← `_impact_block` ← `build_context_items` ←
  `integration_adapter.build_session_start_context`; `_knowledge_rule`,
  `_feedback_correction` ← `build_guardrails`; `write_debug` ← `session_start_context.main`.

## Practice on this date

- Context given to a model should be relevant and high-signal; low-confidence content
  dilutes attention and costs tokens on every turn (Anthropic, "Effective context
  engineering for AI agents",
  [anthropic.com/engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)).

## The decision

- `format_for_advisory` leaves out textual-name-match pages; an impact with nothing else
  prints nothing. The CLI and MCP outputs still carry them.
- Guard-rail summaries are clipped at the last word boundary within 150 characters, with
  an ellipsis when something was cut.
- The adapter writes the SessionStart payload it returns to
  `logs/session-start-last.txt` through the same `write_debug`, best effort.

Files: `scripts/impact_analysis.py`, `scripts/build_guardrails.py`,
`scripts/integration_adapter.py`, `tests/test_less_noise_at_session_start.py`,
`docs/research/2026-09-14-less-noise-at-session-start.md`.

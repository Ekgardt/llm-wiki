# The session-start advisory prints what the graph proved

Dated 2026-09-17. Audit 3, finding K-B30. The research before the change.

## What was found

- `session_start_context._impact_block` runs the whole `impact_analysis.analyze_impact()` on
  every session start, with the default five-second ceiling, including the note scan of
  `find_stale_wiki_pages` (up to 2,000 notes and 32 MiB read).
- `format_for_advisory` then prints only `stale_pages`, and since 2026-09-14 drops every item
  whose `method` is `textual-name-match`. `_textual_fallback` stamps that method on every item
  it returns, and `stale_pages` is that list. So in production the block can print nothing,
  whatever changed. The three tests that show output build pages by hand without `method`.
- The analysis does compute a graph-proven answer: `affected["pages"]` and
  `affected["decisions"]` — knowledge pages and decisions reached from the changed symbols over
  confirmed edges (`REFERENCES_SYMBOL` is written by `knowledge_extractor` when a page names a
  symbol explicitly), each with its evidence span. The advisory never read them.
- The code graph: `format_for_advisory` ← `_impact_block` ← `build_context_items` ←
  `integration_adapter.build_session_start_context`. Pinned by
  `tests/test_impact_analysis.py::TestFormatForAdvisory` and
  `tests/test_less_noise_at_session_start.py`.

## Practice on this date

- Context handed to a model should be the smallest set of high-signal tokens; low-confidence
  content costs on every turn (Anthropic, "Effective context engineering for AI agents",
  https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents). The
  2026-09-14 decision applied that to word-match guesses; it still holds.
- A hook on the start path gets a small named budget of its own, as the health block already
  has (`HEALTH_BUDGET_SECONDS`), instead of inheriting a five-second interactive ceiling.

## The decision

The owner delegated the choice (fix or remove). Fixed, because the proven half exists and is
cheap:

- `format_for_advisory` prints the graph-proven pages and decisions from `affected`, at most
  `max_pages` lines plus the one-line summary, and nothing when there are none. Word-match
  guesses stay out, as decided on 2026-09-14.
- `analyze_impact` takes `textual_fallback=False`; the session start passes it, so the note
  scan — whose result the advisory refuses anyway — is not paid on every session.
- The session start gives the analysis `IMPACT_BUDGET_SECONDS = 1.0`; past it the block is
  empty, as any failure of this block already is.
- Measured here: with a clean tree the analysis costs 0.13 s (one `git` call). The cost on a
  dirty tree against a live generation was not measured (the live vault is not a workbench);
  the budget bounds it.

Files: `scripts/impact_analysis.py`, `scripts/session_start_context.py`,
`tests/test_impact_analysis.py`, `tests/test_less_noise_at_session_start.py`,
`tests/test_the_session_start_advisory_prints_graph_proven_pages.py`,
`docs/research/2026-09-17-the-session-start-advisory-prints-what-the-graph-proved.md`.

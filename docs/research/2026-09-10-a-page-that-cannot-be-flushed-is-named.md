# A page that cannot be flushed is named

Date: 2026-09-10. Trigger: audit finding H2. `scripts/access_tracking.py`
was the one file under `scripts/` the owner's gate refused, on eleven
counts: `_parse_frontmatter_integer` CCN 6, `flush_access_to_frontmatter`
CCN 12, `_flush_candidates_with_cursor` CCN 13, `get_access_stats` CCN 15,
nesting 3 in three of them, and six bare `except Exception`. Inside, a page
whose flush raises (oversize, malformed frontmatter, a refused transaction)
is skipped with `continue`, the export cursor still advances, and nothing
says which page or why.

## Sources

1. Rule 5 as enforced on this machine (`~/.claude/tools/ccn_gate.py`): CCN
   ≤ 5, nesting ≤ 2, guard clauses and a pipeline of small steps.
2. This repository: `maintenance_helpers.run_step` and, since this evening,
   `secret_redact.describe_error` — a failure is reported as
   `Class: redacted message`; `capture_diagnostics` records losses that must
   not break the caller. The flush is an explicit operator action
   (`access_tracking.py --flush`, the weekly pass), so its report is its
   own output.
3. The decision this module already states: frontmatter promotion is
   manual and bounded; the cursor advances past a page so one bad page
   cannot stall the scan, and a wrapped cursor retries it later.

## Decision

1. The four functions become pipelines of steps under CCN 5, behaviour
   unchanged: the same regexes, the same operation id, the same
   precondition on the source bytes, the same cursor and bounds.
2. A page whose flush fails is named: `flush_access_to_frontmatter`
   records `{slug, error}` in `last_flush_failures()`, prints one redacted
   line to stderr, and still advances the cursor (the bounded-scan rule
   stays). `--flush` prints the failures after the count.
3. `get_access_stats` keeps its two sources and their bounds; a legacy log
   that cannot be read is reported through the same list once, not
   swallowed.

Files: `scripts/access_tracking.py`, `tests/test_access_tracking.py`,
`CHANGELOG.md`, `docs/AUDIT-2026-09-10-memory-and-retrieval.md`.

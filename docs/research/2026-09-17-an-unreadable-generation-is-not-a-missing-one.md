# An unreadable generation is not a missing one

Dated 2026-09-17. Audit 3, finding K-B26 (the `impact_analysis` half; the `code_graph` half is
handled in the same round by the graph agent). The research before the change.

## What was found

`impact_analysis._active_graph` wrapped the whole open in
`except (OSError, PermissionError, TypeError, ValueError): return None`. Every one of those
became the same answer as "this repository has no active generation":

- a corrupt or unreadable `cache/evidence-graph/catalog.sqlite3` (`sqlite3.Error` surfaces as a
  read failure inside the open),
- `PermissionError("active Evidence Graph changed while opening")`, which
  `EvidenceGraph.open_active_for_repository` raises after three attempts when the active
  pointer keeps moving,
- a plain programming error: a `TypeError` from a wrong argument, caught and hidden.

The caller then recorded the warning "No valid active Evidence Graph generation is available."
and classified the impact `unresolved`. The operator reads that as "the index has not been
built", which is a different action from "the index is damaged" — and for a `TypeError` it is
simply false.

## Practice on this date

- Python's own guidance on exception handling is that a handler should name the exceptions it
  can actually answer for; catching broad classes hides errors that the code cannot handle
  (`docs.python.org`, tutorial §8.3 "Handling Exceptions": an `except` clause may name multiple
  exceptions, and the tutorial's examples catch the specific errors the code knows how to
  treat). `TypeError` from the module's own call is never one of those.
- The vault's own rule in `CLAUDE.md`: derived generations are disposable, but Markdown and the
  answer's provenance are authority — an answer must say which of the two states it was in.

## The decision

- `_active_graph` keeps returning `None` for the one honest missing case (no catalog file, or
  no active generation bound to this repository) and raises `GenerationUnreadable` — carrying
  the original exception type and message — for `OSError`, `ValueError` and `sqlite3.Error`.
  `TimeoutError` still propagates untouched, as the deadline tests require.
- `TypeError` is no longer caught anywhere on this path: a wrong argument is a defect, and it
  now fails loudly in tests instead of being reported to the operator as missing evidence.
- `_ImpactRun.map_changes` catches `GenerationUnreadable` and records
  "The active Evidence Graph is unreadable: <type>: <message>", still classifying the impact
  `unresolved`. No answer becomes less bounded; only the reason becomes true.

Files: `scripts/impact_analysis.py`,
`tests/test_an_unreadable_generation_is_not_a_missing_one.py`,
`docs/research/2026-09-17-an-unreadable-generation-is-not-a-missing-one.md`.

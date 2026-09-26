# An impact answer outlives its note scan

Date: 2026-09-25. Audit item C-35 (docs/AUDIT-2026-09-25-full.md).

## Fact
- `scripts/impact_analysis.py`: `analyze_impact` calls `_textual_fallback` after the
  graph answer is computed, outside any handler. `_note_text` raises
  `PermissionError` when a note changed after discovery; `_NoteWalk` raises
  `ValueError` at its directory, file and byte ceilings. Either one discards the
  computed changes, symbols and affected artifacts.
- The other stages of the same run (`collect`, `_map_graph`) already turn
  `ValueError`/`OSError` into a warning and `partial = True`, and let
  `TimeoutError` (deadline or cancel) propagate.

## Source (fetched 2026-09-25)
Python documentation, Built-in Exceptions,
https://docs.python.org/3/library/exceptions.html:
- PermissionError: "Raised when trying to run an operation without the adequate
  access rights - for example filesystem permissions." Its base class is `OSError`.
- "The following exceptions are subclasses of OSError, they get raised depending
  on the system error code."
So catching `OSError` covers the refusal `PermissionError` and any other read error.

## Decision
The textual fallback is an optional extra on top of the graph answer. Its
`OSError`/`ValueError` becomes a run warning ("Textual fallback unavailable: ..."),
the run is marked partial, and the fallback is empty. `TimeoutError` still
propagates, as in every other stage: a cancel or an exhausted deadline is the
caller's stop, not a scan failure.

## Conclusion / uncertainty
The answer is kept with a named, conservative classification. Not known: how
often a note changes mid-scan on the live vault; the fix does not depend on it.

## Files
- scripts/impact_analysis.py
- tests/test_an_impact_answer_outlives_its_note_scan.py

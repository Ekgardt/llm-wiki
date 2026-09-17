# A journal line ends at a newline

Dated 2026-09-17. Findings M13, M14 and M15 of the third audit (project journal). The research
before the fixes.

Files: scripts/project_journal.py,
tests/test_a_journal_line_ends_only_at_a_newline.py,
tests/test_a_rebuild_unblocks_the_project_it_was_run_for.py,
tests/test_rotation_keeps_every_open_blocker.py

## What was found

- M13. `_journal_event_lines` splits the journal with `str.splitlines()`, which also breaks
  at U+2028, U+2029, U+0085 and the other Unicode line boundaries. The writer
  (`canonical_json_bytes`, `ensure_ascii=False`) escapes `\n` and the C0 controls and
  writes those characters raw. One checkpoint whose text holds a line separator commits,
  and from then on `projection()` and every `checkpoint()` raise `JSONDecodeError`: the
  journal is wedged. The text comes from agents (deltas, reasons, branch names).
- M14. When a checkpoint meets a journal that needs a rebuild, its reserved row is marked
  `quarantined`. `recover()` replays only `prepared` and `reserved` rows, and the head check
  blocks every later sequence behind any row that is not committed. After
  `--rebuild` + `recover` the next event still fails with
  `ProjectPendingPriorError`; only the byte-identical original request clears it. The
  docstring and the CLI promise the opposite.
- M15. The session handoff shows every open blocker (`_handoff_sections`), but the rotation
  snapshot keeps the last five (`_MAX_LIST_ITEMS["blockers"]`). Seven open blockers before
  rotation were five after it: two blockers nobody closed vanished from the handoff. The
  event schema allows a hundred operations in `blockers`.
- Code graph: `_journal_event_lines` ← `parse_journal_events` ← `ProjectStore._journal_events`,
  `project_extractor`, the claim tree; `rebuild_journal` ← the `--rebuild` CLI;
  `_snapshot_delta` ← `_snapshot_event` ← `_rotated_journal` ← `_extended_journal`.

## Practice on this date

- The JSON Lines format states its one separator: "Line Terminator is `'\n'`"
  ([jsonlines.org](https://jsonlines.org/)). A reader that splits anywhere else is not
  reading the format the writer produced. JSON itself permits U+2028 and U+2029 unescaped
  inside strings, so a correct writer may emit them.
- A repair command has to leave the thing it repairs usable; a row parked because a repair
  was needed is released by that repair.

## The decision

- M13. The reader splits on `\n` only. This also heals a journal that is already wedged,
  because its bytes were always valid JSON Lines. The writer is left alone: changing the
  canonical encoding would change every hash built on it.
- M14. After the rebuild transaction commits, the slug's `quarantined` rows above the
  rebuilt head go back to `reserved`, so the `recover()` the CLI already runs replays them
  in order. A row that fails again is quarantined again by the existing replay rules.
- M15. The snapshot carries every open blocker up to the schema's hundred; the other lists
  keep the tail a reader can see.

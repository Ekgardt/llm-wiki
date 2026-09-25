# A vanished project is rebuilt by the night

Dated 2026-09-24. Audit item A-4 (`docs/AUDIT-2026-09-24-live.md`).

Files: `scripts/repair_orphaned_checkpoint_names.py`, `scripts/doctor.py`,
`tests/test_a_vanished_project_is_rebuilt_by_the_night.py` (new), `CHANGELOG.md`,
`docs/research/2026-09-24-a-vanished-project-is-rebuilt-by-the-night.md`.

## What was found (live vault, 2026-09-24)

- Project `main` (a checkout named `main` of another repository) has committed checkpoints
  1–36 (2026-08-24..26) and its directory `knowledge/projects/main/` no longer exists, so
  its journal head is 0. Sequence 37, reserved 2026-09-23 21:18, fails
  `_require_journal_head` with `ProjectJournalRebuildRequired: project 'main' sequence 37
  follows journal sequence 0`, is quarantined, and blocks the project.
- The designed repair exists and is tested: `ProjectStore.rebuild_journal` writes the
  journal and `state.md` back from the committed checkpoints (issue #20: "the journal is
  a projection of the store; the store rebuilds it";
  `tests/test_project_journal.py::test_a_deleted_project_directory_is_rebuilt_from_its_committed_checkpoints`),
  and `recover` then settles the parked sequence
  (`tests/test_a_rebuild_unblocks_the_project_it_was_run_for.py`). Its only caller is the
  manual `project_journal.py --rebuild <slug>`.
- The nightly step `repair_orphaned_checkpoint_names.py` re-attempts the row every night,
  meets the same error, prints `failed: ProjectJournalRebuildRequired`, and returns 0, so
  the pass logs `failures=0`. Each attempt adds a row to `project_checkpoint_attempts`.
- Doctor measures a stuck sequence's age from `MAX(created_at)` of its attempts
  (`_checkpoint_head_rows`), so the nightly's own re-attempt resets the age; the health
  report written at 03:09 said `ok` and doctor said `degraded` an hour later. When the
  database cannot be read, the check returns `ok`.
- While `state.md` is missing, `_slug_owns_dir` treats the slug as free, so a second
  checkout named `main` could take it. Restoring `state.md` restores the ownership check.

## Practice on this date

- A projection is rebuilt from its source of truth by the system, not by an operator:
  that is what makes it a projection (Martin Fowler, "Event Sourcing": "We can discard
  the application state completely and rebuild it by re-running the events from the
  event log on an empty application", fetched 2026-09-24). The project's committed checkpoints are that log.
- An automatic repair must fail loudly when it cannot finish, or the pass that runs it
  reports health it does not have.

## The decisions

1. `repair_orphaned_checkpoint_names.py`: when a re-attempt fails with
   `ProjectJournalRebuildRequired` and the journal is *behind* the committed checkpoints
   (`journal_head < sequence - 1`), it runs the same `rebuild_journal` + `recover` the
   manual command runs, under the project lease they take, and reports `rebuilt`. A
   journal *ahead* of the store is never rebuilt over (that would drop entries); it stays
   a failure.
2. The script exits 1 when any row it touched still failed, so the nightly counts it.
3. Doctor ages an unfinished sequence from its first attempt (`MIN`), and a checkpoint
   database it cannot read is `degraded`, not `ok`.

## Limits (rule 3)

A project directory someone deletes on purpose comes back the next night; deleting a
project's history is not a supported operation and was not before (issue #20). Retiring a
project is its own feature and not part of this change.

## Cost, by rule 4

One rebuild per vanished project, once; nothing on a vault without one.

## Sources

- Martin Fowler, "Event Sourcing" — https://martinfowler.com/eaaDev/EventSourcing.html — fetched 2026-09-24.
- `run/markdown-transactions-v3.sqlite3` (read-only) and `journalctl --user -u llm-wiki-nightly.service`
  on the live vault, 2026-09-24.

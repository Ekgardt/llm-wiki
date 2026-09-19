# The writer of an unfinished attempt is named by its start, not only by its number

Dated 2026-09-17. The remainder of finding Q-M11 of the third audit: a `preparing`
transaction row whose process number answers as alive is never recovered. The research
before the fix.

Files: scripts/markdown_transaction.py,
tests/test_a_reused_process_number_does_not_keep_a_dead_attempt.py

## What was found

- A writer that dies inside `prepare` leaves a row in state `preparing` carrying only
  `owner_pid`. Recovery (`_preparing_owner_alive`) and the append path
  (`_preparer_is_alive`) ask `process_liveness.pid_alive`, which by its own docstring
  "cannot be" safe against a reused number. Once the operating system hands the number to
  another process, the row is never recovered, the first round's bounded stall only stops
  the caller, and the doctor reports a nonterminal transaction for as long as that other
  process lives.
- The first round left the rest to the owner because it assumed a new column. Reading the
  schema code shows what a column costs. The adopted coordinator database is compared with
  `_COORDINATOR_V3_TABLE_SQL` text for text (`_coordinator_v3_object_matches`), its digest
  `COORDINATOR_V3_SCHEMA_SHA256` is written into `run/reliability-v3-adopted.json` and
  checked by `installed_memory_repair._expected_schema_digests`, the backup and the
  ownership registry. SQLite states that "The ALTER TABLE command works by modifying the
  SQL text of the schema stored in the sqlite_schema table"
  ([SQLite, ALTER TABLE](https://www.sqlite.org/lang_altertable.html)), so one added column
  makes every adopted vault fail validation until a second offline adoption exists, is run
  by the operator, and rewrites the adoption record. `_add_column_if_missing` serves only
  the pre-adoption database. `upgrade_coordinator_v3_candidate` extends an unpublished
  candidate, never an adopted database, and nothing calls it.
- The identity itself already exists: `operational_ownership.process_start_identity`
  (boot id + start ticks on Linux, creation FILETIME on Windows, start time on macOS) and
  `process_identity_state`, which answers `dead` when the number now belongs to a process
  with another start. The v3 owner tables use it.
- Code graph: `_preparing_owner_alive` ← `_recover_one` ← `_recover_selected` ← `recover`;
  `_preparer_is_alive` ← `_recover_abandoned_preparation` ← `_settle_operation`,
  `_settle_append_candidate`. The artifact directory `run/transactions/<id>/` is created by
  `_create_artifact_roots` before `_insert_preparing_row`, and nothing enumerates it:
  readers open `plan.json`, `manifest.json`, `before/`, `after/` by name.
- The installed vault holds no `preparing` row today (23 305 committed, 13 discarded, 117
  quarantined; read-only count on 2026-09-17).

## Practice on this date

- psutil, the reference process library, binds a process by number and start together. Its
  `Process` docstring: "The PID alone is not enough, as it can be assigned to a new process
  after this one terminates", and "Real process identity is checked (via PID + creation
  time)" ([psutil/__init__.py](https://github.com/giampaolo/psutil/blob/master/psutil/__init__.py)).
  A wall-clock comparison (process start later than the row) was considered and refused: a
  stepped clock would declare a living writer dead; equality of an opaque start identity
  does not depend on any clock.
- The product already has the pattern for this evidence: the LSP owner publishes an
  immutable create-only `owner.json` beside its scratch, read by whoever must judge the
  owner later.

## The decision

- No column. The identity is written where the attempt's other evidence lives: a
  create-only `run/transactions/<id>/owner.json` holding the writer's number and start
  identity, written and fsynced by `_create_artifact_roots` before the row can exist, so
  every row written by this code has it. It needs no schema change, no second adoption, no
  new runtime directory, and it is removed with the attempt's artifacts.
- One function answers "is the preparer alive" for both recovery and the append path. With
  a record: the number must match the row and `process_identity_state` must not answer
  `dead` (`unknown` stays alive — a row is never taken on a guess). Without a record — a
  row written before this change — the old number-only probe stays, because nothing else
  is known about that writer; the installed vault has no such row.
- A writer that is the same living process (a thread that died inside `prepare`) is still
  alive by any identity; the first round's bounded stall remains the answer there.
- `upgrade_coordinator_v3_candidate` and its helpers are deleted: no caller, and the one
  job it could have been given here is refused above.

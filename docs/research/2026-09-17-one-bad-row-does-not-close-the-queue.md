# One bad row does not close the queue

Dated 2026-09-17. Findings M1, M2, M3 and M5 of the third audit (queue slice). The research
before the fixes.

Files: scripts/memory_queue.py, tests/test_memory_queue_cli.py,
tests/test_the_repair_commands_open_the_adopted_queue.py,
tests/test_one_corrupt_task_does_not_close_the_adopted_queue.py,
tests/test_a_corrupt_finished_task_does_not_block_the_purge.py

## What was found

- M1. `quarantine-corrupt` and `purge-corrupt` open `run/queue-v3.candidate.sqlite3`, build
  the ownership registry over the candidate coordinator, and `_corrupt_intent_fence`
  hard-codes the candidate coordinator path. Adoption requires the candidates to be gone, so
  on every adopted vault both commands exit 2. The CLI tests monkeypatch both seams, so the
  product path was never run. The reader already knows its coordinator
  (`coordinator_path`, `ownership_registry()`); the CLI just does not ask it.
- M2 and M5 are one defect seen twice. `active_memory_queue` runs
  `validate_queue_v3_database` on every open: `PRAGMA integrity_check`,
  `PRAGMA foreign_key_check`, and a re-canonicalisation of every retained payload. Cost grows
  with every retained row (measured 1.72 s per open at 300 flush-sized tasks), and one
  tampered payload on a live task makes the whole-file check fail, so no hook can enqueue and
  no worker can claim. The per-row mechanism that was designed for this
  (`_require_valid_task_payload` → `dead / payload_hash_mismatch`) never gets to run.
- M3. The ordinary purge selects every old `succeeded`/`cancelled` row and raises
  `payload_hash_mismatch` for the whole plan when one of them is corrupt. The commit path
  demotes the row and then raises, so the demotion rolls back. Nothing else can move a
  terminal row, so all ordinary purges stop for good.
- Code graph: `active_memory_queue` ← `active_or_legacy_memory_queue` ← every hook, worker,
  doctor and the CLI; `validate_queue_v3_database` ← adoption
  (`installed_memory_repair._DATABASE_SPECS`), backup (`private_vault_backup`), candidate
  initialisation. Those three keep the full check.

## Practice on this date

- SQLite's own documentation puts a price on the whole-file check: "PRAGMA quick_check runs
  in O(N) time whereas PRAGMA integrity_check requires O(NlogN) time where N is the total
  number of rows in the database" ([SQLite PRAGMA statements](https://www.sqlite.org/pragma.html#pragma_quick_check)).
  A check of that order belongs to the moments that certify a file — adoption, backup,
  an operator's health check — not to every open by every short-lived hook.
- The approved Reliability v3 contract in this repository says payload hashes are "checked at
  every transition". That is a per-row rule, and the code has it: claim, redrive and purge
  validate the row they touch and demote it. A whole-file veto on open contradicts the
  per-row rule, because it prevents the very transition that would isolate the bad row.

## The decision

- Opening the adopted queue checks what an open can rely on cheaply and must never skip:
  the path is inside the state root, the file opens read-only under the v3 contract
  (application id, user version, journal mode are checked by `open_operational_db`), and
  the schema is complete. The whole-file check stays where a file is certified: adoption,
  backup, candidate initialisation.
- The repair commands open the queue by the one rule every other caller uses
  (`active_memory_queue` on an adopted vault, the candidate before adoption), take the
  repair owner from the registry the queue names, and the intent fence uses the queue's own
  coordinator path. The CLI tests stop patching the seams and run on an adopted vault.
- Before an ordinary purge plans, corrupt finished rows older than the cutoff are demoted to
  `dead / payload_hash_mismatch` in their own committed transaction. They leave the
  selection, the purge proceeds with the healthy rows, and `quarantine-corrupt` — which
  needs `dead` — can now take them. The in-plan checks stay as they are for a row that goes
  bad between plan and commit.

## What this does not do

- It does not add a periodic whole-file check to doctor; doctor is outside this slice. It is
  named in the report for the owner.

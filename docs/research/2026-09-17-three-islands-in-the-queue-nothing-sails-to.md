# Three islands in the queue nothing sails to

Dated 2026-09-17. Section 4 of the third audit, the parts that live in the queue and the
Markdown coordinator: the legacy JSON write path, the compatibility drain, the coordinator
v3 upgrade tool, and the smaller dead names beside them.

Files: scripts/memory_queue.py, scripts/markdown_transaction.py,
tests/test_memory_queue.py, tests/test_audit_fixes.py

## What was found

Proved by grep over `scripts/`, `integrations/`, `benchmark/`, `.github/`, the install
scripts and the tracked documents, and by reading every remaining reference:

- **The legacy JSON write path.** `_legacy_enqueue_file`, `_legacy_queue_record`,
  `_confirm_legacy_write`, `_check_legacy_marker_race`, `_legacy_write_allowed`: no
  production caller. It also defeats itself — it re-acquires the `"legacy"` queue owner
  while already holding it. `CLAUDE.md` says "Legacy `run/queue/*.json` files are migration
  **input** only", so the contract protects the importer (`migrate_legacy_queue`, reached
  from the `migrate` command and from the doctor), not a writer of new JSON.
- **The compatibility drain.** `drain_with`, `_drain_one_lease`, `_run_compat_processor`,
  `_compat_processor_outcome`, `_settle_compat_outcome`, `_publish_compat_outcome`: no
  production caller. The product's worker is `run_worker`/`_drive_worker`, which runs the
  handler in a child process with a heartbeat, a deadline and a kill sequence; the drain
  runs it inline in the calling process. Six assertions in `tests/test_memory_queue.py` and
  one in `tests/test_audit_fixes.py` exercise queue behaviour *through* the drain rather
  than the drain itself.
- **`mark_attempt`.** No production caller, and broken where it would matter: on an adopted
  vault it raises `AttributeError: '_QueueV3CandidateReader' object has no attribute
  '_claim_task'`. `_settle_legacy_attempt` and `MemoryQueue._claim_task` serve only it.
- **The adopted-owner cluster** (`_ADOPTED_OWNER_ROLES`, `_adopted_owner_reader`,
  `_acquire`/`_heartbeat`/`_release_adopted_queue_owner`, `_ADOPTED_OWNER_LEASES`): only the
  role `"worker"` routes into it, and all four production `_acquire_queue_owner` calls pass
  `"legacy"` or `"migration"`.
- **`upgrade_coordinator_v3_candidate`** and its helpers: one reference, its own definition,
  plus one test file. It extends an *unpublished* candidate database with the blackboard
  tables; the adopted database is never upgraded in place, and adoption pins the schema
  digest. Finding Q-L15 (a crash during its copy leaves a partial candidate the next run
  trusts) is a defect of this tool alone.
- **Byte-identical duplicates**: `_capture_semantic_seal_digest` and `_semantic_seal_digest`;
  `_require_capture_intent_size` and `_check_capture_intent_size`.
- **`publish_capture_intent`** on the queue is not the path the product uses: the hooks
  publish through `integration_adapter._publish_capture_files_and_task`, which calls
  `index_capture_intent_pending` and `mark_capture_intent_ready`. Finding Q-L25 (its path
  check accepts `..`) is a defect of this unused method alone.

Checked and **kept**, against the audit's list:

- `_run_processor_inline` is the injection seam eight test files pass to
  `run_worker(processor_runner=...)`, a real parameter of a real API. Removing it would make
  those tests spawn child processes to observe in-process behaviour.
- `retains_run_directory` is the queue's own answer to a question the `run/` deletion
  contract asks, pinned by three test files. The doctor's separate implementation is a
  duplicate worth one answer some day, but the doctor is another area's file this round.
- `_queue_owner_is_active` has a production caller (line 12707). The audit listed it as
  dead; that is wrong.

## Practice on this date

- The owner's rule for this round, verbatim: «всё что ненужно и бесполезно удаляй, главное
  ничего не сломай» — delete what is unneeded and useless, above all break nothing.
- Kent Beck's rule for tests that exist only to reach dead code is to follow the caller:
  a test that asserts queue behaviour through a facade nothing runs is asserting the facade,
  not the behaviour. Each such assertion here is either already made against `run_worker`
  elsewhere in the same file, or is moved onto the product's worker rather than deleted.

## The decision

- The three islands and the smaller dead names above are removed, with the tests that exist
  only to reach them. Assertions about queue behaviour that happened to be written through
  the drain are moved onto `run_worker` with `_run_processor_inline`, the seam the rest of
  the suite already uses, so no coverage is lost.
- The duplicate pairs collapse to one function each, keeping the name the callers use most.
- Q-L15 and Q-L25 close with the code that carried them: an unused upgrade tool and an
  unused publisher cannot be crash-safe or path-safe, and wiring either in would be adding a
  feature nothing asked for.
- What is kept is listed above with the reason, so a later audit does not re-raise it.

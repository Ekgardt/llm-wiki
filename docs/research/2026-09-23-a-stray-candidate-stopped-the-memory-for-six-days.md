# A stray candidate stopped the memory for six days

Dated 2026-09-23. Found while checking the graph walk on the live vault: the vault's
own memory has not accepted a single Markdown write since 2026-09-17 13:37, and doctor
did not say why.

Files: `scripts/doctor.py`, `tests/conftest.py`,
`tests/test_a_stray_candidate_after_adoption.py`,
`docs/research/2026-09-23-a-stray-candidate-stopped-the-memory-for-six-days.md`.

## What was found

- `logs/capture-failures.jsonl` holds 130 entries; 81 of them since 2026-09-17 13:37 say
  `ReliabilityV3ValidationError: reliability_v3_record_invalid <- ValueError: candidate
  artifacts remain after adoption`, and 25 more carry the first half of that sentence. Every
  session end, every user prompt append, every project checkpoint and the compile of
  2026-09-17 13:59 (`logs/maybe-compile-last.err.log`) failed the same way. There is no
  `knowledge/daily/2026-09-17.md` and nothing later: the memory recorded nothing for six days.
- The cause is one file: `run/markdown-transactions-v3.candidate.sqlite3`, 204 800 bytes,
  created 2026-09-17 13:04:54. It is an empty coordinator v3 schema except for two rows in
  `maintenance_owners`: scopes `project:a` and `project:b`, actors `actor-a` and `actor-b`,
  pid 744389, both expired at 13:05:24. Those names are the test suite's fixtures. A pytest
  session ran with the state root resolved to the live vault and its
  `initialize_coordinator_v3_candidate(state_root / "run" / ...)` created the file there.
- `tests/conftest.py` allows exactly that: with `LLM_WIKI_TEST_USE_EXTERNAL_STATE=1` and no
  `LLM_WIKI_STATE_ROOT`, it sets the state root to `VAULT_ROOT`, the owner's live vault since
  the two directories merged on 2026-08-21. The knowledge-leak guard added 2026-08-24 watches
  `knowledge/`; nothing watches `run/`.
- `installed_memory_repair._require_adoption_artifacts_complete` refuses the whole vault while
  `run/markdown-transactions-v3.candidate.sqlite3` or `run/queue-v3.candidate.sqlite3` exists
  after a complete adoption record. That guard is right: a candidate is evidence of an
  adoption in flight, and a half-adopted vault must not be written. But nothing distinguishes
  an adoption in flight from a stray file, nothing reports the refusal, and nothing repairs it.
- Doctor reported `degraded` for the index, the scheduler, capture, hooks, pyright and the
  generation, and never named the refusal. The adoption check lives only in
  `_run_deletion_check`, whose result is always `run_deletion: ok` with the code buried in
  `blockers`. The capture and scheduler checks, which would have read the failures, gave up
  first: `run/state.json` is 354 509 bytes against the 262 144-byte bound, because
  `project_checkpoint_pending` (232 569 bytes, 91 projects) grows with every refused
  checkpoint. The failure hid its own diagnosis.
- The CI run of PR 38 shows the same class from the other side: a Windows shard died after
  its 101st test with exit code 1 and no traceback, and the per-test progress file that exists
  for exactly that case was never written, because `tests/conftest.py` appends to
  `$LLM_WIKI_TEST_PROGRESS_FILE` with `open(..., "a")` and the directory
  `pytest-timings/` does not exist until pytest writes the junit file at session end. Python
  raises `FileNotFoundError` for a missing parent directory in append mode
  ([open() — Python documentation](https://docs.python.org/3/library/functions.html#open)),
  and the hook swallows `OSError`.

## Practice on this date

- The precedent is issue #17, recorded in `tests/test_doctor.py`: "every capture failed
  silently until the adoption was run by hand", fixed by making doctor name the state and the
  command. The rule it set: a condition that refuses every writer is an `error` finding with
  its cause, not a code inside another check's details.
- pytest's `pytest_runtest_logstart` hook runs before each test
  ([pytest hook reference](https://docs.pytest.org/en/stable/reference/reference.html#pytest.hookspec.pytest_runtest_logstart)),
  which is why the progress line survives a kill — provided the file can be opened.

## The decisions

1. **`tests/conftest.py` refuses a state root that is the vault or inside it.** With external
   state requested and the root resolving to `VAULT_ROOT` or below, the session stops before
   collection with a message naming both paths. The default branch already uses a temp dir;
   this closes the one path that reached the live `run/`.
2. **Doctor has an `adoption` check.** Where the adoption records exist, it runs
   `require_reliability_v3_adopted`; a refusal is an `error` finding whose message carries the
   code and the cause chain, and whose details name each stray candidate found by the plain
   names `installed_memory_repair` checks. It reads two small JSON records and `lstat`s two
   paths, so the `run/state.json` bound cannot hide it.
3. **`doctor --repair` retires a stray coordinator candidate.** Only when all of these hold:
   the adoption record is complete, no `.reliability-v3-*` operation artifact exists (so no
   adoption is in flight), the candidate opens as a valid v3 schema, every row-bearing table
   except `maintenance_owners` is empty, and every maintenance owner has expired. The file is
   moved, never deleted, to `run/coordinator-quarantine/<utc>-markdown-transactions-v3.candidate.sqlite3`,
   which counts as retained evidence for the `run/` deletion contract like `run/queue-quarantine`.
   A candidate holding any transaction, operation, fence, lease or live owner is reported and
   left alone. The step runs before the maintenance owner is taken, because while the stray
   exists no owner can be taken at all.
4. **The progress file's directory is created when the hook is armed.** One `mkdir` at
   conftest import, so the next silent death names its test.

## Cost, by rule 4

The check costs two JSON reads and two `lstat`s per doctor pass. The repair costs one
read-only open of a 200 KB file and one rename, once. Nothing runs on the answer path.

## Sources

- [open() — Python 3 documentation](https://docs.python.org/3/library/functions.html#open) — fetched 2026-09-23.
- [pytest hook reference: pytest_runtest_logstart](https://docs.pytest.org/en/stable/reference/reference.html#pytest.hookspec.pytest_runtest_logstart) — fetched 2026-09-23.
- `logs/capture-failures.jsonl`, `logs/maybe-compile-last.err.log`, `run/markdown-transactions-v3.candidate.sqlite3` on the live vault, read 2026-09-23.
- CI run 35837737925, job `windows_full::py3.14-s2`, 2026-09-23.

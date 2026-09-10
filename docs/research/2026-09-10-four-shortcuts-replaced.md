# Four shortcuts replaced — 2026-09-10

The owner asked whether the day's fixes were shortcuts. Four were. Each is
replaced by the mechanism the code already has, and this note names why.

**1. A writer race is decided by the exception, not by its text.**
`scripts/capture_diagnostics.py` classified a failure as contention by
substring ("owner_busy", "database is locked"). Python's own guidance is that
error categories are types and codes, not messages: PEP 3151 reworked the
OS/IO hierarchy precisely so callers stop parsing `strerror`
(https://peps.python.org/pep-3151/), and `sqlite3` exposes `sqlite_errorcode`
since 3.11 (https://docs.python.org/3/library/sqlite3.html#sqlite3.Error.sqlite_errorcode).
The vault already has typed sources: `OperationalOwnershipError` carries
`code`, `ProjectPendingPriorError` is a class, the writer gate raises
`owner_busy`. Two remained untyped and are typed now: the state-lock timeout
in `scripts/memory_state.py` (`StateLockTimeout`) and the bound-elsewhere
refusal in `scripts/markdown_transaction.py` (`OperationBoundElsewhereError`,
still a `ValueError` for its callers). `record_capture_failure` takes the
exception; `scripts/integration_adapter.py`, `scripts/memory_queue.py`,
`scripts/mcp_server.py`, `scripts/post_tool_capture.py`,
`scripts/user_prompt_capture.py` pass it. Limit: on Python 3.10 an SQLite
busy error has no code and counts as lost; the text log parser in
`scripts/doctor.py` for lines written before the naming is unchanged.

**2. A batch returns its outcome; no module-level list.**
`scripts/compile_memory.py` collected outcomes in a global, mirroring
`DROPPED_CLAIMS`. Global mutable state is the classic obstacle to testing and
re-entrancy (Fowler, "Global data", in *Refactoring*; Google's C++ guide
forbids it for the same reason:
https://google.github.io/styleguide/cppguide.html#Static_and_Global_Variables).
`_run_batch` now returns a `BatchOutcome`; `_run` folds them and hands them
to `_mark_finished`.

**3. Search reports the generation it selected; nobody looks it up twice.**
`scripts/retrieval.py` builds a `RetrievalTrace` for every run, including
an empty one, and `scripts/search_memory.py` flattened it into rows, so an
empty run lost it and `scripts/mcp_server.py` re-read the catalog. The trace
now travels through an out-parameter (`trace_sink`) from `search` to the MCP
envelope; the second lookup is deleted. The MCP schema is unchanged.

**4. Tests exercise the real thing.**
Seven tests asserted a healthy report on a vault that had never adopted
Reliability V3 and were made to pass by patching a private function. They now
adopt the vault through `repair_installed_vault` (0.45 s each, measured), the
way an install does; the doctor's capture check takes the vault root it was
given instead of the process-wide one. The Pyright probe test spawned its
descendant after the probe's clock had started and, on a slow runner, read a
file that did not yet exist; the descendant is now spawned before the
prestarted parent signals ready, so the test is deterministic instead of
tolerant (Google Testing Blog, "Where do our flaky tests come from?",
https://testing.googleblog.com/2017/04/where-do-our-flaky-tests-come-from.html).
Files: `tests/test_doctor.py`, `tests/test_runtime_deletion_contract.py`,
`tests/test_pyright_profile.py`, `tests/test_a_lost_race_is_not_a_hook_failure.py`.

**What the real adoption found.** Adopting the test vaults for real exposed
two product defects the patched answer had hidden: the doctor's queue check
waited for the v2 migration marker on an adopted vault, where the v2 queue is
a tombstone and `memory_queue` never writes the marker again (reported as
`migration: pending`, degraded, for ever); and `doctor --repair` on such a
vault ran the retired migration first and aborted the whole repair on the
tombstone. Both now follow the queue's own rule: adoption retires the v2
migration (`migration: retired`, no repair step). Tests:
`test_an_adopted_vault_owes_no_v2_queue_migration`,
`test_repair_on_an_adopted_vault_does_not_run_the_retired_v2_migration`.
Neither had been reached by the users because the installer's sync step wrote
the marker before adoption; an adoption without a prior queue use did.

**PR #27 on Windows.** The pull request's tests assumed POSIX: the rendered
`env … ~/.local/bin/uv` command, which the doctor recognises on POSIX only,
and `os.kill(pid, 0)` for liveness, which raises `WinError 87` on Windows.
The tests now take the template's command for the platform they run on and
ask the kernel whether the peer is gone; the POSIX rendering tests are marked
as such.

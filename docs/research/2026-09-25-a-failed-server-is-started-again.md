# A failed server is started again

Date: 2026-09-25. Audit item B-40 (`docs/AUDIT-2026-09-25-full.md`; the audit marked it a
suspicion).

## Facts (traced in code by a read-only agent and checked here; confirmed by test)

- `lsp_process` allows one restart; past that the process ends `FAILED`
  (`_intent_exhausted`, `_select_terminal_failure_locked`).
- `PyrightSession._reconcile_process_state_locked` noticed `FAILED` and reset readiness, but kept
  the dead process in `self._process`. `_startup_needed_locked` returns `False` while `_process`
  is set, and `_startup_attempted` stayed `True` from the first start, so the session never
  started another server.
- The session manager returns the same session for its key (`_existing_session_locked`) and only
  closes it when it is idle for 300 s or evicted at capacity; a key in steady use renews its idle
  clock on every request, so it stayed degraded indefinitely.
- The recovery path already had the right move: `_detach_recovered_process_locked` hands a process
  to cleanup as `_startup_process`, and a retained owner is never refused by
  `_startup_claim_refused_locked`, so the next start cleans it up and starts fresh.

## Source

- Erlang/OTP, "Supervisor Behaviour", https://www.erlang.org/doc/system/sup_princ.html (fetched
  2026-09-25): "If more than `MaxR` number of restarts occur in the last `MaxT` seconds, the
  supervisor terminates all the child processes and then itself." A failed child is restarted by
  its owner within a bounded budget, and given up on only past it; here the process's own budget
  (one restart) was the end of the session too.

## Decision

- A `FAILED` process is detached to cleanup by `_retire_failed_process_locked`, and the next query
  starts a new server; the replacements are bounded by the same startup retry budget (3), after
  which the session stays degraded as before.
- The old test that pinned `not_ready` after `FAILED` keeps its `DEGRADED` case; a new test shows a
  failed process replaced and answering.

## Files

- `scripts/pyright_session.py`
- `tests/test_pyright_session.py`
- `tests/test_a_failed_server_is_started_again.py`
- `CHANGELOG.md`

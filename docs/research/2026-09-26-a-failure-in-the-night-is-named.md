# A failure in the night is named

Date: 2026-09-26. Audit 2026-09-26 B-23.

## Facts

- `reclaim_runtime_state.main` printed counts and always exited 0. The transaction
  prune, the history prune and the snapshot report their failures in the result
  (`failed`, `reason`, `status: failed: …`) and the report dropped them, so a night
  whose pruning failed read as a clean one.
- Doctor's queue check degraded only on `ready`, `leased` or `blocked` tasks; 25
  `dead` tasks were reported "Queue state is healthy".
- The live nightly of 2026-09-26 (read-only): "snapshot ok (7602d35); pruned 48
  settled transaction(s) …" — no reclaim failure today, so the stricter exit does
  not turn tonight red.
- Nagios plugin guidelines (https://nagios-plugins.org/doc/guidelines.html, fetched
  2026-09-26): 0 means the check "appeared to be functioning properly", 1 that it
  "did not appear to be working properly" — an exit code is the status, and a
  step that failed must not exit 0.

## Decision

- The reclaim report ends with "FAILED: …" naming each failure; a failed prune,
  history prune, snapshot or backlog project makes the step exit 1, so the
  nightly counts it. A co-activation table that failed is named but does not fail
  the step: it is derived and disposable.
- Doctor treats a task that died within the last 7 days as work needing
  attention (degraded), counted in `details.recent_dead`; older dead tasks are
  history the weekly purge exports.

## Files

- `scripts/reclaim_runtime_state.py`
- `scripts/doctor.py`
- `tests/test_a_failure_in_the_night_is_named.py`
- `CHANGELOG.md`
